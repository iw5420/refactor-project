# translator_cli/client.py
"""translator-cli 對外唯一入口，對應 07a 二章全節：`generate_scaffold()`
（骨架生成模式，④呼叫）與 `fill_function()`（填空模式，⑤呼叫）。兩者
都是 `python_project_path` 顯式引數（見 07a 二章「python_project_path
是顯式引數」）——不讀 `os.environ["PYTHON_PROJECT_PATH"]`，也不 import
`graph.state`，維持跟 `refactor_harness/`／`spec_collection_agent/`
一致的獨立性（見 07a 十二章）。`PythonStructure` 型別定義在
`translator_cli/types.py`（結構對齊 `graph/state.py` 同名 TypedDict，
呼叫端傳入的實際物件兩邊相容，見該檔案 docstring）。

**所有會碰觸子行程或磁碟的呼叫都包在 `asyncio.to_thread()` 裡**：
`git_ops.py`（`subprocess.run`）與 `Path.read_text()`／`write_text()`
本身是純同步 API，直接在 `async def` 函式裡呼叫會佔住 event loop。
`asyncio.to_thread()` 把這些呼叫丟到執行緒池執行，不需要把
`git_ops.py`／`scaffold.py` 本身改寫成 async（那兩個模組維持純同步、
易於獨立測試，只有這裡的呼叫端需要調整）。純 AST 記憶體操作
（`PythonAdapter` 的 `parse`／`locate_function`／`splice_body`／`render`／
`validate_syntax`）不在這個範圍——那些是對單一函式的操作，速度是微秒
等級，不是 I/O，包一層 `to_thread` 只會增加雜訊、換不到實質好處。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from common.run_context import adhoc_run_id
from translator_cli import formatting, git_ops, ollama_client, python_adapter, scaffold
from translator_cli.exceptions import (
    TranslatorCliConfigError,
    TranslatorCliError,
    TranslatorCliModelOutputError,
    TranslatorCliNetworkError,
    TranslatorCliScaffoldMismatchError,
    TranslatorCliUpstreamDegradedError,
)
from translator_cli.python_adapter import PythonAdapter
from translator_cli.types import FillResult, PythonStructure, ScaffoldResult

logger = logging.getLogger(__name__)

# 對應 09b_bug_trace.md #37：真實環境量化證實，context_files 總量超過
# 這個量級時，本地模型（qwen2.5-coder:32b）明顯更容易跑題、生成耗時
# 暴增到正常值的 7～20 倍。門檻值取自真實資料：那次重跑裡所有「成功」
# 呼叫的 context_files 總量最高 12942 bytes，所有「三次 attempt 全部
# 失敗」的呼叫最低 15590 bytes，兩者之間留有餘裕，13000 bytes 取在
# 安全側。之後若有更多真實資料，這個值可以直接調整，不影響裁減機制
# 本身的結構。
TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES = int(
    os.environ.get("TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES", "13000")
)

# 對應 09b_bug_trace.md #45：受控實驗證實 strip_all_function_bodies()
# 對這兩個路徑前綴底下的檔案是空操作（純欄位宣告的 Pydantic／SQLAlchemy
# 資料類別，沒有函式本體可以剝），改用 python_adapter.extract_referenced_classes()
# 依類別名稱過濾，見 _trim_context_files_if_oversized()。
_SCHEMA_MODEL_PATH_PREFIXES = ("app/schemas/", "app/models/")

# 抓「看起來像 class 名稱」的識別字：大寫字母開頭，後面接字母/數字/底線
# ——用來從函式簽名／description／context 這些自然語言＋型別標註混雜的
# 文字裡，粗略抓出可能被引用到的 class 候選名單。抓到的候選字不保證真的
# 是這個檔案定義的 class（一般大寫英文詞也會命中），但
# extract_referenced_classes() 會再跟該檔案實際定義的 class 集合取交集，
# 誤命中的候選字不會造成任何影響，見該函式 docstring。
_CLASS_NAME_CANDIDATE_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")


def _referenced_class_names(*texts: str) -> set[str]:
    names: set[str] = set()
    for text in texts:
        names.update(_CLASS_NAME_CANDIDATE_RE.findall(text))
    return names


async def generate_scaffold(
    python_project_path: str,
    python_structure: PythonStructure,
    db_models: dict[str, str] | None = None,
) -> ScaffoldResult:
    """對應 07a 四章全節。同步、確定性（不呼叫本地模型，見四章
    「決策」）——`async def` 不是因為函式本身需要非同步邏輯，而是每一步
    子行程／磁碟操作都經 `asyncio.to_thread()` 丟到執行緒池（見本模組
    docstring），對外仍是 `await translator_cli.generate_scaffold(...)`
    這個跟 `fill_function()`／LangGraph node 一致的呼叫慣例。

    流程：
    1. precondition 檢查（git repo 存在＋working tree 乾淨，見二、九章）
    2. `scaffold.build_files()` 在記憶體中組裝並驗證全部檔案
    3. 全部驗證通過才呼叫 `scaffold.write_files()` 一次性寫入磁碟
    4. `git_ops.commit_scaffold()` 一次性 commit（見八章）——寫入或
       commit 任一步失敗都會嘗試 `git_ops.discard_written_files()` 只
       還原這次打算寫入的檔案清單（見下方「寫入／commit 失敗時的復原」）
    """
    try:
        await asyncio.to_thread(git_ops.ensure_git_repo, python_project_path)
        await asyncio.to_thread(git_ops.check_clean_working_tree, python_project_path)
    except TranslatorCliError as exc:
        return {"success": False, "error": str(exc), "skipped_interfaces": [], "skipped_db_models": []}

    try:
        files, skipped_interfaces, skipped_db_models = await asyncio.to_thread(
            scaffold.build_files, python_structure, db_models or {}
        )
    except TranslatorCliError as exc:
        return {"success": False, "error": str(exc), "skipped_interfaces": [], "skipped_db_models": []}

    # 寫入／commit 失敗時的復原：write_files() 可能因 OSError（磁碟空間
    # 不足、權限問題）中途失敗，commit_scaffold() 可能因 git 本身的問題
    # （index lock 等）失敗——不論哪一種，這一批檔案已經有部分或全部
    # 落地磁碟但沒能進版控，working tree 會卡在「不乾淨」狀態，讓下一次
    # 呼叫的 precondition 檢查連帶失敗。這批變更是這次呼叫自己造成、
    # 成因已知（就是我們剛寫入但沒 commit 成功的內容），不是九章「衝突
    # 偵測」要攔的「來源不明」情況，因此可以安全地自動撤銷，只把這次
    # 呼叫標記失敗，不讓 working tree 卡住之後所有呼叫。
    try:
        await asyncio.to_thread(scaffold.write_files, python_project_path, files)
        # 格式化（見 formatting.py）是硬性依賴：在 commit 之前跑，讓 commit
        # 進去的內容就是格式化後的版本，不是格式化前後兩次改動。格式化
        # 失敗（含找不到 ruff 執行檔）跟下面 write_files()／commit_scaffold()
        # 失敗走同一條 rollback 路徑——都已經落地磁碟但沒能進版控。
        await asyncio.to_thread(formatting.format_paths, python_project_path, *files.keys())
        await asyncio.to_thread(git_ops.commit_scaffold, python_project_path)
    except (OSError, TranslatorCliError) as exc:
        restored = await asyncio.to_thread(git_ops.discard_written_files, python_project_path, list(files.keys()))
        detail = "已還原這次寫入的檔案" if restored else "還原也失敗，working tree 可能仍不乾淨，需要人工介入核對"
        return {
            "success": False,
            "error": f"骨架寫入或 commit 失敗（{detail}）：{exc}",
            "skipped_interfaces": [],
            "skipped_db_models": [],
        }

    return {
        "success": True,
        "error": None,
        "skipped_interfaces": skipped_interfaces,
        "skipped_db_models": skipped_db_models,
    }


def _read_target_file(root: Path, target_file: str) -> str:
    """對應 07a 六章步驟 1：讀取 `fill_function()` 的目標檔案。
    `FileNotFoundError` 是「scaffold/task 不一致」這個特定語意（見六章
    步驟 4 同一類錯誤）；其餘 `OSError`（權限問題等）是另一類環境層級
    的失敗，訊息不套用「scaffold/task 不一致」這個字面說法，避免誤導
    排查方向——兩者都是 `TranslatorCliError` 子類別，呼叫端可以用同一個
    `except TranslatorCliError` 一併接住。
    """
    try:
        return (root / target_file).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise TranslatorCliScaffoldMismatchError(f"scaffold/task 不一致：{target_file} 不存在") from exc
    except OSError as exc:
        raise TranslatorCliError(f"讀取 {target_file} 失敗：{exc}") from exc


def _read_context_files(
    root: Path,
    context_files: list[str],
    *,
    task_id: str,
    referenced_functions: list[tuple[str, str | None, str]] | None = None,
) -> list[tuple[str, str]]:
    """對應 07a 七章「`context_files` 讀取容錯」：`context_files[0]`
    （＝`target_file`）已在呼叫端（六章步驟 1）確認存在，這裡不會再
    踩到；其餘項目遇到 `FileNotFoundError` 記警告並跳過，不中斷整個
    `fill_function()` 呼叫——常見於沒有對應 DB 表的模組（純外部 API
    串接）或 `db_models` 因型別驗證失敗被跳過的情況。刻意只收斂到
    `FileNotFoundError`（比照 `_read_target_file()` 對這兩類錯誤的區分）：
    其餘 `OSError`（權限問題等）是環境層級的真實錯誤，不屬於 07a
    原文界定的「檔案不存在」容錯範圍，讓它往外傳、中止這次呼叫，避免
    靜默吞掉磁碟權限這類需要人工排查的問題。

    `referenced_functions`：對應 06a 七章新設計「`referenced_interfaces`
    函式層級抽取」（見 `docs/09b_bug_trace.md` #37 根因）。`(file_path,
    class_name, function_name)` 三元組清單——某個 `rel_path` 若在這份
    清單裡有對應項目，代表這個檔案是「因為引用才被拉進來」的參考檔案，
    只抽取被引用到的那幾個函式（`python_adapter.extract_specific_functions()`），
    不整份帶入。沒有對應項目的檔案（`target_files[0]` 自己的檔案、
    schemas／models 這類資料形狀定義檔）維持整份帶入，理由見
    `plan_agent/planning.py::_build_referenced_functions()` 的排除規則。
    抽取失敗（理論上不該發生，內容一定是合法 Python）時退回完整內容，
    裁減本身不該變成新的失敗來源。
    """
    targets_by_file: dict[str, list[tuple[str | None, str]]] = {}
    for file_path, class_name, function_name in referenced_functions or []:
        targets_by_file.setdefault(file_path, []).append((class_name, function_name))

    resolved: list[tuple[str, str]] = []
    for rel_path in context_files:
        try:
            content = (root / rel_path).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            logger.warning(
                "task %s：context_files 讀取 %s 失敗，跳過（見 07a 七章「context_files 讀取容錯」）：%s",
                task_id,
                rel_path,
                exc,
            )
            continue

        targets = targets_by_file.get(rel_path)
        if targets:
            try:
                content = python_adapter.extract_specific_functions(content, targets)
            except SyntaxError as exc:
                logger.warning(
                    "task %s：%s 函式層級抽取失敗，改用完整檔案內容（見 06a 七章新設計）：%s",
                    task_id, rel_path, exc,
                )
        resolved.append((rel_path, content))
    return resolved


def _trim_context_files_if_oversized(
    context_files: list[tuple[str, str]], *, task_id: str, current_signature: str = "", description: str = "",
    context: str = "",
) -> list[tuple[str, str]]:
    """對應 09b_bug_trace.md #37「context 過大時的裁減」：只在總大小
    超過 `TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES` 時才裁減，小
    context 維持原始完整內容不動——真實資料顯示大部分呼叫的
    context_files 遠低於這個門檻，裁減只該發生在真的需要的時候。

    **這是第二道安全網，不是主要修法**：`_read_context_files()` 若拿到
    `referenced_functions`，已經先把「因引用而拉進來」的參考檔案精準
    抽取成只含被引用函式（見 06a 七章新設計、`plan_agent/planning.py::
    _build_referenced_functions()`），多數情況下總量已經降到門檻以下，
    這裡不會被觸發。這裡處理的是精準抽取覆蓋不到的情況（`target_files[0]`
    自己的檔案、schemas／models 這類本來就整份帶入的資料形狀定義檔，
    萬一其中之一本身就異常龐大）。

    **裁減方式依檔案性質分兩種**（見 `docs/09b_bug_trace.md #45` 受控
    實驗）：`app/schemas/`／`app/models/` 底下的檔案是純欄位宣告的
    Pydantic／SQLAlchemy 資料類別，`strip_all_function_bodies()`
    （剝函式本體）對這類檔案是空操作——改用
    `python_adapter.extract_referenced_classes()`，依 `current_signature`／
    `description`／`context` 這三處文字裡出現過的候選名稱，只留這個 task
    可能用到的 class；其餘檔案（真正有函式本體可以剝的一般程式碼檔）
    維持既有的 `strip_all_function_bodies()`。單一檔案裁減失敗（理論上
    不該發生——這裡的內容一定是合法 Python，`ast.parse()` 沒有理由失敗，
    但裁減本身不該變成新的失敗來源）時，那個檔案退回原始內容，不影響
    其他檔案／整體流程。純 AST 記憶體操作，不是 I/O，不需要
    `asyncio.to_thread()`（見模組 docstring）。
    """
    total_bytes = sum(len(content.encode("utf-8")) for _, content in context_files)
    if total_bytes < TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES:
        return context_files

    candidate_class_names = _referenced_class_names(current_signature, description, context)

    trimmed: list[tuple[str, str]] = []
    for path, content in context_files:
        try:
            if path.startswith(_SCHEMA_MODEL_PATH_PREFIXES):
                trimmed.append((path, python_adapter.extract_referenced_classes(content, candidate_class_names)))
            else:
                trimmed.append((path, python_adapter.strip_all_function_bodies(content)))
        except SyntaxError as exc:
            logger.warning(
                "task %s：context_files 裁減 %s 失敗，保留原始內容（見 09b_bug_trace.md #37）：%s",
                task_id, path, exc,
            )
            trimmed.append((path, content))

    trimmed_bytes = sum(len(content.encode("utf-8")) for _, content in trimmed)
    logger.info(
        "task %s：context_files 總量 %d bytes 超過門檻 %d bytes，已裁減至 %d bytes（見 09b_bug_trace.md #37／#45）",
        task_id, total_bytes, TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES, trimmed_bytes,
    )
    return trimmed


async def fill_function(
    python_project_path: str,
    task_id: str,
    target_file: str,
    class_name: str | None,
    function_name: str,
    description: str,
    context: str,
    context_files: list[str],
    run_id: str | None = None,
    referenced_functions: list[tuple[str, str | None, str]] | None = None,
    fixed_body: str | None = None,
) -> FillResult:
    """對應 07a 五、六、七章。呼叫失敗時不寫入任何內容（見五章「呼叫
    失敗時不寫入任何內容」）——每個失敗分支都在寫入磁碟之前 return，
    是九章「衝突偵測」成立的前提。

    `run_id`：選填，省略時用 `adhoc_run_id()`。這裡是 Ollama 呼叫鏈的
    最外層（見 `11a_logging_architecture.md` 九章「run_id 的解析只在
    fill_function() 做一次」），只在這裡解析一次再往下傳給
    `ollama_client.get_function_body()`，同一個 task 的多次格式修正
    attempt 才會落在同一個 run_id 底下，不會各自 fallback 出不同的值。

    `referenced_functions`：選填，`(file_path, class_name, function_name)`
    三元組清單，對應 `graph/state.py::TaskSpec.referenced_functions`（見
    06a 七章新設計）。傳給 `_read_context_files()` 決定 `context_files`
    裡哪些檔案該只抽取指定函式、哪些該整份帶入，見該函式 docstring。

    `fixed_body`：選填，見 10a 八章「⑦ 直接產生修正後程式碼」。非
    `None` 時代表呼叫端（⑦ Debug Agent）已經產生好正確的函式本體（跟
    `ollama_client.get_function_body()` 回傳的格式一樣：未縮排陳述式
    文字，不含 `def` 簽名行），直接拿來 `splice_body()`，完全跳過
    `ollama_client.get_function_body()` 呼叫與它需要的 `context_files`
    讀取——這種情境下不是「重新翻譯」，是「套用已知正確的修正」，不需要
    本地模型參與。定位函式節點、import 解析、語法驗證、寫入＋git commit
    等既有步驟不變。
    """
    resolved_run_id = run_id or adhoc_run_id()
    root = Path(python_project_path)

    try:
        await asyncio.to_thread(git_ops.check_clean_working_tree, python_project_path)
    except TranslatorCliError as exc:
        return FillResult(success=False, error=str(exc), diff="")

    adapter = PythonAdapter()
    try:
        source = await asyncio.to_thread(_read_target_file, root, target_file)
        tree = adapter.parse(source)
        node = adapter.locate_function(tree, class_name, function_name)
        if node is None:
            label = f"{class_name}.{function_name}" if class_name else function_name
            raise TranslatorCliScaffoldMismatchError(f"scaffold/task 不一致：{label} 在 {target_file} 找不到")
    except TranslatorCliError as exc:
        return FillResult(success=False, error=str(exc), diff="")

    if fixed_body is not None:
        body_source = fixed_body
    else:
        current_signature = adapter.render_signature(node)
        try:
            resolved_context_files = await asyncio.to_thread(
                _read_context_files, root, context_files,
                task_id=task_id, referenced_functions=referenced_functions,
            )
        except OSError as exc:
            # _read_context_files() 只吞 FileNotFoundError（見該函式 docstring）；
            # 其餘 OSError（權限問題等）是真實環境錯誤，這裡轉成
            # FillResult(success=False)，不讓原生例外洩漏擊穿合約。
            return FillResult(success=False, error=f"讀取 context_files 失敗：{exc}", diff="")

        resolved_context_files = _trim_context_files_if_oversized(
            resolved_context_files, task_id=task_id,
            current_signature=current_signature, description=description, context=context,
        )

        try:
            body_source = await ollama_client.get_function_body(
                current_signature=current_signature,
                description=description,
                context=context,
                context_files=resolved_context_files,
                function_name=function_name,
                task_id=task_id,
                target_file=target_file,
                class_name=class_name,
                run_id=resolved_run_id,
            )
        except (TranslatorCliModelOutputError, TranslatorCliNetworkError, TranslatorCliConfigError) as exc:
            # TranslatorCliNetworkError（網路層重試耗盡）／TranslatorCliConfigError
            # （缺 OLLAMA_BASE_URL／OLLAMA_API_KEY，見
            # ollama_client._call_ollama_once()）都不會被 get_function_body()
            # 的格式重試迴圈攔截、會直接往外傳到這裡——這是刻意的：網路層
            # 錯誤與環境變數缺失都不該進「模型輸出格式錯誤」的重試邏輯，那
            # 救不了連線失敗或缺環境變數這兩件事，一樣轉成
            # FillResult(success=False) 讓這個 task 明確失敗、不讓例外洩漏
            # 擊穿合約。TranslatorCliUpstreamDegradedError 是
            # TranslatorCliNetworkError 的子類別，一樣會被這裡接住，差別只是
            # 額外標記 upstream_degraded=True，讓 implement_node.py 決定要不要
            # 提早停止重試（見該例外類別 docstring、docs/09b_bug_trace.md #35）。
            return FillResult(
                success=False,
                error=str(exc),
                diff="",
                upstream_degraded=isinstance(exc, TranslatorCliUpstreamDegradedError),
            )

    try:
        adapter.splice_body(node, body_source)
    except TranslatorCliModelOutputError as exc:
        if fixed_body is not None:
            # 見 docs/09b_bug_trace.md #52：這是 ⑦ Debug Agent 給的
            # fixed_body 被 extract_body_statements() 的巢狀同名函式檢查
            # 攔下來的情況（不是 ⑤ 本地模型的一般格式錯誤重試），值得在
            # 即時 log 裡明講——這代表 ⑦ 這次的修正違反了 fixed_body 的
            # 格式契約（可能混進了裝飾器／簽名行），task_id=%s 這筆
            # pending_fixed_bodies 沒有被套用，需要人工或下一輪 ⑦ 重新
            #處理，不會像一般 fill_failed 那樣有排程器自動重試機制。
            logger.warning(
                "task %s：⑦ 給的 fixed_body 被 extract_body_statements() 拒絕（%s），"
                "這筆修正沒有寫入，見 docs/09b_bug_trace.md #52",
                task_id, exc,
            )
        return FillResult(success=False, error=str(exc), diff="")

    # 填空模式的本體 import 解析（見 07a 五章）：node.body 現在是新本體
    # （qwen 生成，或 fixed_body 給定），可能引用簽名以外的名稱（跨檔案
    # 自訂類別、框架例外），骨架階段的 import 解析看不到這些，這裡針對
    # 新本體重新掃一次補上。掃描專案磁碟找自訂型別索引是 I/O，包
    # to_thread；純 AST 插入不是。
    bound_names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
    if node.args.vararg:
        bound_names.add(node.args.vararg.arg)
    if node.args.kwarg:
        bound_names.add(node.args.kwarg.arg)
    missing_imports = await asyncio.to_thread(
        scaffold.resolve_body_imports, python_project_path, tree, node.body, bound_names
    )
    scaffold.insert_import_lines(tree, missing_imports)

    new_source = adapter.render(tree)
    try:
        adapter.validate_syntax(new_source)
    except SyntaxError as exc:
        return FillResult(success=False, error=f"寫入前最終語法驗證失敗（理論上不應發生）：{exc}", diff="")

    # 動筆寫入前重新檢查一次 working tree（見九章「衝突偵測」）：本地
    # 模型單次生成可能耗時數十秒到數分鐘（見 07a 七章），函式最開頭那次
    # check_clean_working_tree() 檢查的是「開始等模型回應之前」的狀態，
    # 這段漫長的 I/O 等待期間人工完全可能手動修改 target_file——若不
    # 在真正落筆前再檢查一次，這裡的 write_text() 會在人不知鬼不覺的
    # 情況下覆蓋掉那份人工修正，正是九章「衝突偵測」要防的事，不能只
    # 在函式入口做一次就視為全程有效。
    try:
        await asyncio.to_thread(git_ops.check_clean_working_tree, python_project_path)
    except TranslatorCliError as exc:
        return FillResult(success=False, error=str(exc), diff="")

    try:
        await asyncio.to_thread((root / target_file).write_text, new_source, encoding="utf-8")
    except OSError as exc:
        return FillResult(success=False, error=f"寫入 {target_file} 失敗：{exc}", diff="")

    # 格式化（見 formatting.py）是硬性依賴：在擷取 diff／commit 之前跑，
    # 讓 diff 與 commit 反映的都是格式化後的最終內容。格式化失敗（含
    # 找不到 ruff 執行檔）不允許半格式化的內容流入 commit，還原這次
    # 寫入——理由跟下方 commit 失敗時的復原邏輯一致。
    try:
        await asyncio.to_thread(formatting.format_paths, python_project_path, target_file)
    except TranslatorCliError as exc:
        restored = await asyncio.to_thread(git_ops.discard_file_changes, python_project_path, target_file)
        detail = "已還原該檔案" if restored else "還原也失敗，working tree 可能仍不乾淨，需要人工介入核對"
        return FillResult(success=False, error=f"格式化失敗（{detail}）：{exc}", diff="")

    diff = await asyncio.to_thread(git_ops.diff_for_file, python_project_path, target_file)

    if not diff:
        # 冪等（見 07a 五章「允許對已有內容的函式重新填空」）：LLM 這次
        # 生成的內容跟磁碟上已經 commit 的版本完全相同（例如 task 被
        # 重複排程），write_text() 寫入後 working tree 其實沒有任何
        # 變更——`git commit`（不帶 --allow-empty）遇到 staging area
        # 是空的會直接失敗（exit code 1），若照下面正常流程呼叫
        # commit_fill() 會把這個「實質上成功」的操作誤判成失敗、觸發不
        # 必要的 rollback。這裡提前偵測 diff 為空就直接視為成功，不必
        # 也不能 commit 一個空變更。
        logger.info("task %s：%s 內容與現有版本相同，視為冪等成功，不建立新 commit", task_id, target_file)
        return FillResult(success=True, error=None, diff="")

    # commit 失敗時的復原：write_text() 已經落地，但沒能進版控會讓
    # working tree 卡在「不乾淨」，之後每個 task 的 precondition 檢查
    # 都會連帶失敗。這是這次呼叫自己造成、成因已知的變更，可以安全地
    # 自動撤銷這一個檔案，只把這一個 task 標記失敗，不讓其餘 task 被
    # 這次 commit 失敗拖累卡住。
    try:
        await asyncio.to_thread(
            git_ops.commit_fill,
            python_project_path,
            task_id=task_id,
            target_file=target_file,
            class_name=class_name,
            function_name=function_name,
        )
    except TranslatorCliError as exc:
        restored = await asyncio.to_thread(git_ops.discard_file_changes, python_project_path, target_file)
        detail = "已還原該檔案" if restored else "還原也失敗，working tree 可能仍不乾淨，需要人工介入核對"
        return FillResult(success=False, error=f"commit 失敗（{detail}）：{exc}", diff="")

    return FillResult(success=True, error=None, diff=diff)


async def apply_file_fix(
    python_project_path: str,
    task_id: str,
    target_file: str,
    old_snippet: str,
    new_snippet: str,
) -> FillResult:
    """對應 `10a_debug_agent_architecture.md` 十二章「phase 2：檔案層級
    修正」——⑦ Debug Agent 給的修正若碰的是函式本體以外的內容（例如檔案
    開頭的 import 敘述），`fill_function()` 的 AST 函式定位機制連這個
    節點都找不到，改用精確字串搜尋替換：`old_snippet` 必須在
    `target_file` 目前內容裡逐字出現剛好一次，找不到或出現不只一次都
    視為失敗（安全起見，不做模糊匹配、不猜測該替換哪一處）。

    寫入前後的既有關卡（clean tree 檢查、格式化、diff／commit、commit
    失敗時的復原）比照 `fill_function()` 既有邏輯——這裡不是新發明一套
    寫入流程，是同一套安全機制套用在「整個檔案」而不是「單一函式節點」
    這個不同的定位方式上。跟 `fill_function()` 不同，這裡不需要在寫入前
    再檢查一次 working tree：`fill_function()` 那個二次檢查是為了防
    「本地模型單次生成可能耗時數十秒到數分鐘」這段等待期間人工插手修改
    檔案，這裡從讀檔到寫檔中間沒有任何長時間等待（`old_snippet`／
    `new_snippet` 是 ⑦ 已經算好的現成資料，不需要再呼叫任何模型）。
    """
    try:
        await asyncio.to_thread(git_ops.check_clean_working_tree, python_project_path)
    except TranslatorCliError as exc:
        return FillResult(success=False, error=str(exc), diff="")

    root = Path(python_project_path)
    try:
        source = await asyncio.to_thread(_read_target_file, root, target_file)
    except TranslatorCliError as exc:
        return FillResult(success=False, error=str(exc), diff="")

    occurrences = source.count(old_snippet)
    if occurrences == 0:
        return FillResult(
            success=False,
            error=f"old_snippet 在 {target_file} 目前內容裡找不到（可能已經被上一輪修正過，或跟目前程式碼不一致）",
            diff="",
        )
    if occurrences > 1:
        return FillResult(
            success=False,
            error=f"old_snippet 在 {target_file} 裡出現 {occurrences} 次，無法安全判斷要替換哪一處",
            diff="",
        )

    new_source = source.replace(old_snippet, new_snippet)

    try:
        PythonAdapter().validate_syntax(new_source)
    except SyntaxError as exc:
        return FillResult(success=False, error=f"套用後語法驗證失敗：{exc}", diff="")

    try:
        await asyncio.to_thread((root / target_file).write_text, new_source, encoding="utf-8")
    except OSError as exc:
        return FillResult(success=False, error=f"寫入 {target_file} 失敗：{exc}", diff="")

    try:
        await asyncio.to_thread(formatting.format_paths, python_project_path, target_file)
    except TranslatorCliError as exc:
        restored = await asyncio.to_thread(git_ops.discard_file_changes, python_project_path, target_file)
        detail = "已還原該檔案" if restored else "還原也失敗，working tree 可能仍不乾淨，需要人工介入核對"
        return FillResult(success=False, error=f"格式化失敗（{detail}）：{exc}", diff="")

    diff = await asyncio.to_thread(git_ops.diff_for_file, python_project_path, target_file)
    if not diff:
        # 冪等，理由同 fill_function()：new_snippet 跟磁碟上已經 commit
        # 的版本相同時（例如同一筆 file_fix 被重複套用），視為成功，不
        # 建立空 commit。
        return FillResult(success=True, error=None, diff="")

    try:
        await asyncio.to_thread(git_ops.commit_file_fix, python_project_path, task_id=task_id, target_file=target_file)
    except TranslatorCliError as exc:
        restored = await asyncio.to_thread(git_ops.discard_file_changes, python_project_path, target_file)
        detail = "已還原該檔案" if restored else "還原也失敗，working tree 可能仍不乾淨，需要人工介入核對"
        return FillResult(success=False, error=f"commit 失敗（{detail}）：{exc}", diff="")

    return FillResult(success=True, error=None, diff=diff)
