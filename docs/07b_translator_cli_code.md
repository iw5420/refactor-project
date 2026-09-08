# translator-cli 程式碼實作

> 07a 是設計面文件，本文件是實作面文件，一一對應、不重複設計理由——每節開頭註明對應 07a 章節，這裡只講怎麼落地成程式碼。對應 `00_refactor_architecture.md` 十一章文件索引的 `07b_translator_cli_code.md`。

07a 十一章定義的套件結構列了 8 個檔案（`client.py`／`types.py`／`python_adapter.py`／`scaffold.py`／`ollama_client.py`／`prompts.py`／`git_ops.py`／`exceptions.py`）。`client.py`／`types.py` 原本是 stub（見 00 九章、01 七章 stub-first 開發策略），本文件把兩者換成真實實作；其餘 6 個檔案新增。`formatting.py` 是 07a 十一章套件結構之外、實作時新增的第七個模組（比照 04b／05b 對 `exceptions.py` 的既有先例，落地時才產生的基礎設施檔案）。`claude_client.py` 是雙後端（qwen／Claude 二選一，見 07a 七章「分派方式」）的第九個模組。

## 目錄

| 檔案 | 對應章節 | 說明 |
|---|---|---|
| `translator_cli/exceptions.py` | 07a 十三章 | 例外階層 |
| `translator_cli/types.py` | 07a 四、五章 | `FillResult`／`ScaffoldResult`／`SkippedInterface`／`SkippedDbModel`／`PythonStructure`／`InterfaceSpec`／`ParamSpec`／`ReferencedSourceItem`（結構對齊 `graph/state.py`，不 import） |
| `translator_cli/python_adapter.py` | 07a 六、十章 | `LanguageAdapter` Protocol、`PythonAdapter` 實作、`extract_body_statements()` 共用驗證邏輯 |
| `translator_cli/git_ops.py` | 07a 八、九章 | git snapshot、commit 顆粒度、衝突偵測、rollback、`WRITE_LOCK` 寫入段序列化 |
| `translator_cli/prompts.py` | 07a 七章 | qwen／Claude 兩條路徑各自的 system prompt＋共用 `build_user_prompt()` |
| `translator_cli/ollama_client.py` | 07a 七章 | ollama 連線、delimiter 抽取、網路層與格式錯誤兩種重試、`OLLAMA_MODEL_SEMAPHORE` |
| `translator_cli/claude_client.py` | 07a 七章 | Claude API 連線（`common/llm_client.py::call_claude_for_json()`）、Structured Outputs、格式修正重試 |
| `translator_cli/scaffold.py` | 07a 四、五章 | `directory_tree` 三段解析、Schema 定義段合併、`db_models` 併入、兩層 import 解析、逐 `InterfaceSpec` 隔離驗證、填空模式本體 import 解析（`resolve_body_imports()`／`insert_import_lines()`） |
| `translator_cli/client.py` | 07a 二、四、五、七、八章 | 對外唯一入口：`generate_scaffold()`、`fill_function()`（`translator_backend` 分派＋`WRITE_LOCK` 序列化）、`apply_file_fix()` |
| `translator_cli/formatting.py` | — | 硬性依賴 `ruff check --select I --fix`＋`ruff format`，見九章 |
| `graph/state.py` | 07a 二章 | 新增 `RefactorState.python_project_path` 欄位 |
| `main.py` | 07a 二章 | `initial_state` 新增 `python_project_path` |
| `graph/nodes/scaffold_node.py` | 07a 十二章 | 接上 `translator_cli.generate_scaffold()`（取代 stub） |
| `graph/nodes/implement_node.py` | 07a 十二章 | `_run_one_task()` 接上 `translator_cli.fill_function()` 完整引數，含 `translator_backend`／`java_source`／`referenced_source` |
| `graph/java_source_extraction.py` | 07a 五章 | 把 `java_method_id`／`reference_targets` 座標解析成真實原始碼文字，供 `implement_node.py` 呼叫 `fill_function()` 前使用；`java_source` 讀整個 Java 檔案，`referenced_source` 單方法抽取為主、超門檻才裁減（見十章） |
| `tests/translator_cli/test_python_adapter.py` | 07a 六、十章 | `PythonAdapter`／`extract_body_statements()`／`strip_all_function_bodies()`／`extract_specific_functions()` 測試（44 個） |
| `tests/translator_cli/test_scaffold.py` | 07a 四、五章 | 型別正規化、Schema 合併、import 解析、端對端 `build_files()` 測試、填空模式本體 import 解析測試（43 個） |
| `tests/translator_cli/test_git_ops.py` | 07a 八、九章 | 真實 git repo 整合測試，含 rollback 復原（19 個） |
| `tests/translator_cli/test_ollama_client.py` | 07a 七章 | delimiter 抽取、格式錯誤重試、網路層重試、環境變數缺失、`UPSTREAM_DEGRADED_THRESHOLD` 升級、呼叫記錄測試、`check_ollama_reachable()` 預檢（21 個，`monkeypatch` 假造 httpx／ollama 回應） |
| `tests/translator_cli/test_claude_client.py` | 07a 七章 | 成功、格式修正重試成功／耗盡、API 呼叫失敗轉 `TranslatorCliNetworkError` 不重試（4 個，`monkeypatch` 假造 `call_claude_for_json()`） |
| `tests/translator_cli/test_client.py` | 07a 二、四、五、七、八、九章 | `generate_scaffold()`／`fill_function()`／`apply_file_fix()` 端對端測試、`referenced_functions` 抽取與 context 裁減測試、`translator_backend` 分派測試、`WRITE_LOCK` 序列化測試（49 個，真實 tmp_path git repo＋假造模型回應） |
| `tests/translator_cli/test_formatting.py` | — | `formatting.format_paths()` 硬性依賴行為測試（5 個） |

**已驗證**：`python -m pytest tests/translator_cli/ tests/graph/ -q` 242 個通過；全專案 `python -m pytest -q` 868 個通過、4 個跳過（既有、與 translator_cli 無關的 Docker 整合測試，見十二章）、0 個失敗。`translator_cli` 套件在 `graph` 套件完全不可 import 的情況下仍能正常 import（見二章），跟 `graph/`（LangGraph 編排層）之間沒有 import-time 依賴。**`translator_backend` 分派、`java_source`／`referenced_source`、`git_ops.WRITE_LOCK` 這套雙後端＋呼叫鏈 context 契約，尚未對真實 ollama／Claude API／真實併發 task 跑過端對端 pipeline 驗證**——早期單後端契約（`description` 為模型輸入、單一號誌序列化整個 `fill_function()`）曾對真實 ollama／nginx 環境＋真實 Java 專案（93 個檔案）跑過端對端測試（70 個 task 中 69 個成功），但那次驗證的是舊契約，不涵蓋這裡列出的路徑；目前只有 242 個單元／整合測試（`monkeypatch` 假造 ollama／Claude／`asyncio.Lock` 行為，含真實 tmp_path 檔案讀寫）覆蓋，真實環境的 Claude 路徑併發表現與 `WRITE_LOCK` 底下多個真實 task 同時完成模型呼叫的時序仍是待驗證事項，詳見十二章。

---

## 一、`exceptions.py`——例外階層

對應 07a 十三章「錯誤處理範圍」。translator-cli 本身的呼叫失敗不直接觸發 `retry_count` 迴圈，而是反映成 `FillResult(success=False)` 或 `generate_scaffold()` 的 `{"success": False}`；套件內部一律「深處 raise、邊界（`client.py`）統一 catch 轉換」，不讓例外往 `graph/nodes/` 洩漏。

```python
# translator_cli/exceptions.py
"""translator-cli 例外階層，對應 07a 十三章「錯誤處理範圍」。所有例外
共用 `TranslatorCliError` 基底類別，但呼叫端（`client.py`）一律逐一
`except` 特定子類別、轉成 `FillResult(success=False, ...)` 或
`{"success": False, ...}` 回傳，不讓例外往 `graph/nodes/` 洩漏——
translator-cli 本身的呼叫失敗不直接觸發 `retry_count` 迴圈（見 07a 十三
章），是否要重試、要不要把 task 標記失敗，由呼叫端（`implement_node.py`
／`scaffold_node.py`）決定。
"""
from __future__ import annotations


class TranslatorCliError(Exception):
    """所有 translator-cli 例外的共同基底類別。"""


class TranslatorCliNotGitRepoError(TranslatorCliError):
    """`python_project_path` 不是一個已初始化的 git repo（見 07a 二章
    「一次性前置準備」：目錄存在、`git init` 過、沒有任何 commit）。
    `generate_scaffold()` 執行前檢查，環境沒準備好，直接中止，不是可以
    自動補救的情況（見 07a 二章）。
    """


class TranslatorCliDirtyWorkingTreeError(TranslatorCliError):
    """`git status --porcelain` 非空（見 07a 九章「衝突偵測」）。
    `generate_scaffold()`／`fill_function()` 動筆寫任何檔案之前的
    precondition 檢查沒通過，不寫入任何內容，交由人工核對這份意外的
    變更是什麼——不重試，見 07a 十三章。
    """


class TranslatorCliScaffoldMismatchError(TranslatorCliError):
    """`fill_function()` 定位函式失敗（07a 六章步驟 1／4「scaffold/task
    不一致」）：`target_file` 不存在，或 `(class_name, function_name)`
    在該檔案裡找不到。這是防禦性的最後一道檢查——06a 已在上游用「05a
    對多載的消歧」＋「1:1 涵蓋率」兩道機制保證這個三元組理論上必然
    存在，這裡觸發代表更上游的資料有 bug，不重試，直接回報（見 07a
    六章、十三章）。
    """


class TranslatorCliNetworkError(TranslatorCliError):
    """跟 ollama（經 nginx）的連線失敗，對應 07a 七章「Timeout 與重試
    策略」表格的網路層重試耗盡（`ollama_client._call_ollama_once()`）。
    跟 `TranslatorCliModelOutputError`（模型有回應、但內容違反格式契約）
    是刻意分開的兩種錯誤語意——這裡代表根本沒收到可用回應，成因通常是
    另一台 Mac 的網路／nginx／ollama 服務本身有問題，不是模型輸出品質
    問題，混用會誤導事後排查方向（見 00 十章「錯誤訊息看起來像哪個
    Agent 的問題，根因其實在別處」這類風險）。不重試——網路層重試已經
    在 `_call_ollama_once()` 內部做過，這裡拋出代表重試已經耗盡。
    """


class TranslatorCliModelOutputError(TranslatorCliError):
    """ollama 回應違反格式契約，對應 07a 七章「模型輸出格式錯誤的修正
    重試」四種觸發情況：delimiter 抽取失敗、`body_text` 通不過
    `ast.parse()`、`body_text` 解析出的陳述式清單為空（六章步驟
    6a）、`body_text` 恰好是與目標函式同名的巢狀函式定義（六章步驟
    6b「模型重複輸出函式簽名」）。固定重試一次（`ollama_client.
    get_function_body()`），仍失敗才拋出，不無限重試。
    """


class TranslatorCliAssemblyError(TranslatorCliError):
    """`generate_scaffold()` 整檔組裝完成後的最終 `ast.parse()` 驗證
    失敗（07a 四章「語法驗證與寫入」步驟 4，或 Schema 定義段合併後的
    步驟 5）。代表組裝邏輯本身有 bug（如 import 合併沒去重乾淨），不是
    個別介面的型別字串問題——那種情況走 `skipped_interfaces`／
    `skipped_db_models` 隔離，不會走到這裡。不重試，直接中止（見 07a
    十三章）。
    """


class TranslatorCliConfigError(TranslatorCliError):
    """必要的環境變數缺失（`OLLAMA_BASE_URL`／`OLLAMA_API_KEY`，見 07a
    七章「連線方式」）。跟 `TranslatorCliNotGitRepoError` 是同一類「輸入
    端環境沒準備好，不是可以自動補救的情況」（見 07a 二章），只是分別
    對應 git 環境與 ollama 連線環境兩種前置準備。不重試——遺漏的環境
    變數不會因為重試而自己出現；`.env` 若漏了這兩個變數，這條 pipeline
    run 的第一個 task 就會踩到，需要人工補上環境變數後重跑。
    """
```

`TranslatorCliScaffoldMismatchError` 由 `client.py` 的 `_read_target_file()` 輔助函式與「`locate_function()` 找不到函式」兩處 `raise`，`fill_function()` 用 `except TranslatorCliError` 統一接住轉成 `FillResult(success=False, ...)`（見九章）。

---

## 二、`types.py`——輸出契約

對應 07a 四章「回傳契約」、五章「輸出契約」。`FillResult` 是 `fill_function()` 真正建構、回傳的物件（呼叫端用 `result.success` 屬性存取）；`ScaffoldResult`／`SkippedInterface`／`SkippedDbModel` 只是型別提示用的 TypedDict——`generate_scaffold()` 實際回傳相容的 plain dict，呼叫端（`scaffold_node.py`）用 `result["success"]` 字典存取。

**`PythonStructure`／`InterfaceSpec`／`ParamSpec` 是 `generate_scaffold()` 的輸入型別**，也定義在這裡，不從 `graph.state` 匯入——07a 十二章明訂「`translator_cli/` 套件不 import `graph.state`，維持跟 `refactor_harness/`／`spec_collection_agent/` 一致的獨立性」，因此比照 `FillResult`／`ScaffoldResult` 的既有作法，在 `translator_cli/types.py` 自己定義結構對齊 `graph/state.py` 同名 TypedDict 的版本。TypedDict 本質上只是結構化的 dict，呼叫端（`scaffold_node.py`）傳入的實際物件是 `graph.state.PythonStructure`，兩者欄位結構相容，不需要任何轉換就能直接傳入。

`FillResult` 帶 `upstream_degraded` 欄位（見 `translator_cli/exceptions.py::TranslatorCliUpstreamDegradedError`）。`ReferencedSourceItem` 是 `fill_function()` 的 `referenced_source` 引數（見 07a 五章「為什麼是 `java_source`／`referenced_source`」）逐項的型別，結構對齊 `graph/state.py::ReferenceTarget`，多帶一個 `source`（真實原始碼文字）欄位。

```python
# translator_cli/types.py
"""對外輸出契約，對應 07a 四、五章。`ScaffoldResult`／`SkippedInterface`／
`SkippedDbModel` 只是型別提示用的 TypedDict——`generate_scaffold()`
實際回傳相容的 plain dict（見 07a 四章「回傳契約」範例），呼叫端
（`scaffold_node.py`）用 `result["success"]` 這種字典存取，不強制建構
這個型別，這裡的 TypedDict 純粹讓型別檢查工具與閱讀者能對照欄位。

`FillResult` 則是 `fill_function()` 真正建構、回傳的物件（07a 五章
「輸出契約」），呼叫端用 `result.success` 屬性存取。

`ParamSpec`／`InterfaceSpec`／`PythonStructure` 是 `generate_scaffold()`
的輸入型別（07a 四章），結構對齊 `graph/state.py` 的同名 TypedDict，
但刻意在這裡重新定義、不 import `graph.state`（見 07a 十二章
「translator_cli 不依賴 RefactorState 其餘欄位」，維持跟
`refactor_harness/` 一致的獨立性）——TypedDict 只是結構化的 dict，
呼叫端傳入 `graph.state.PythonStructure` 的實際物件時兩者結構相容，
不需要轉換。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict


@dataclass
class FillResult:
    """對應 07a 五章「輸出契約」。`diff` 是這次 commit 的 git diff 全文，
    供人工／⑦ Debug Agent 事後追溯這次改了什麼（見 07a 八章）——不是
    `graph/scheduler.py` 的 regression 偵測依據，那邊只需要
    `target_files[0]` 這個路徑本身。
    """

    success: bool
    error: str | None = None
    diff: str = ""
    # 見 translator_cli/exceptions.py::TranslatorCliUpstreamDegradedError：
    # 連續多次（跨不同 task）都在傳輸層失敗時為 True，讓呼叫端
    # （implement_node.py）能提早停止繼續逐一重試，不是每個 task 各自
    # 燒完重試預算才發現同一個根因，見 docs/09b_bug_trace.md #35。
    upstream_degraded: bool = False


class SkippedInterface(TypedDict):
    """對應 07a 四章「語法驗證與寫入」`skipped_interfaces` 單筆項目。"""

    file_path: str
    class_name: str | None
    function_name: str
    error: str


class SkippedDbModel(TypedDict):
    """對應 07a 四章「db_models」一節 `skipped_db_models` 單筆項目。"""

    file_path: str
    error: str


class ParamSpec(TypedDict):
    name: str
    type: str


class InterfaceSpec(TypedDict):
    file_path: str                 # 相對路徑，如 "app/repositories/user_repository.py"
    class_name: str | None
    function_name: str
    params: list[ParamSpec]
    return_type: str
    # 僅 routers 層 API 邊界方法非 None，見 graph/state.py 同名欄位註解。
    http_method: NotRequired[str | None]
    route_path: NotRequired[str | None]


class PythonStructure(TypedDict):
    directory_tree: str
    interfaces: list[InterfaceSpec]


class ReferencedSourceItem(TypedDict):
    """對應 07a 五章「為什麼是 `java_source`／`referenced_source`」：
    06a 六章 `reference_targets` 座標，經呼叫端（09a／`implement_node.py`）
    解析出的真實原始碼文字。`language="java"` 時 `source` 是 `.java`
    原始碼片段；`language="python"` 時是已翻譯完成的 `.py` 函式原始碼。
    結構對齊 `graph/state.py::ReferenceTarget`，多帶一個 `source` 欄位。
    """

    file_path: str
    class_name: str | None
    function_name: str
    language: Literal["java", "python"]
    source: str


class ScaffoldResult(TypedDict):
    """對應 07a 四章「回傳契約」。`success=True` 但
    `skipped_interfaces`／`skipped_db_models` 非空是預期中會發生、需要
    人工留意但不阻擋 pipeline 的情況，見該章節說明。
    """

    success: bool
    error: str | None
    skipped_interfaces: list[SkippedInterface]
    skipped_db_models: list[SkippedDbModel]
```

**已驗證**：`translator_cli` 整個套件在 `graph` 套件完全不可 import 的情況下（用 `sys.meta_path` 攔截）仍能正常 import 全部模組——證實這是套件層級、不是只有型別檢查層級的獨立性。

---

## 三、`python_adapter.py`——AST 定位／替換／渲染

對應 07a 六章「AST 插入機制」、十章「目標語言 Adapter 介面」。這是 `scaffold.py`／`client.py` 共同依賴的最底層模組，`extract_body_statements()` 是六章步驟 6／6a／6b 的唯一實作，被 `ollama_client.get_function_body()`（判斷是否需要觸發七章「修正重試」）與 `PythonAdapter.splice_body()`（實際替換）共用，避免兩處重複解析／檢查邏輯。

```python
# translator_cli/python_adapter.py
"""目標語言 Adapter 介面與唯一實作，對應 07a 六章「AST 插入機制」、
十章「目標語言 Adapter 介面」。`generate_scaffold()`／`fill_function()`
一律透過 `PythonAdapter` 操作 `ast` 模組，不直接 import `ast`（07a
十章）——這個專案目前只有 Python 一種目標語言，`LanguageAdapter` 只是
劃分模組邊界的抽象基底，不是插件系統。

`extract_body_statements()` 是六章步驟 6／6a／6b 的共用實作：同時被
`ollama_client.get_function_body()`（判斷是否需要觸發七章「修正重試」，
只驗證、不使用回傳值）與 `PythonAdapter.splice_body()`（實際替換，見
步驟 7）呼叫，避免兩處各自重複解析／檢查邏輯——見兩個呼叫端各自的
docstring 說明分工。
"""
from __future__ import annotations

import ast
import copy
from typing import Protocol

from translator_cli.exceptions import TranslatorCliModelOutputError


def strip_all_function_bodies(source: str) -> str:
    """把 `source` 裡「所有」函式／方法本體替換成單一 `pass`，只保留
    簽名、裝飾器、import、class 定義與 class 層級屬性宣告（SQLAlchemy
    Column／Pydantic 欄位這類定義資料形狀的陳述式，不是函式本體，不受
    影響）。對應 09b_bug_trace.md #37：`context_files` 純粹是參考用途，
    模型只需要知道「這裡有哪些函式／類別可用、簽名長怎樣」就能正確
    呼叫，不需要看到其他（非目前要填的）函式的完整實作細節——真實環境
    量化證實，prompt 越大，本地模型跑題與生成耗時暴增的機率越高（見
    `translator_cli/client.py` 的 `_trim_context_files_if_oversized()`）。

    只在呼叫端判斷 context 過大時才會被呼叫，不是無條件套用（見
    `client.py`）。用 `ast.walk()` 找出所有 `FunctionDef`／
    `AsyncFunctionDef`（不分是否巢狀、是否為 class 方法）逐一替換
    `body`，其餘節點原封不動。輸入若通不過 `ast.parse()`，讓
    `SyntaxError` 原樣往外傳，由呼叫端決定要不要退回原始內容（裁減本身
    不該變成新的失敗來源）。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body = [ast.Pass()]
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def extract_referenced_classes(source: str, seed_names: set[str]) -> str:
    """對應 `docs/09b_bug_trace.md #45`：`strip_all_function_bodies()`
    （見上方）對「純資料宣告、沒有函式本體」的檔案（`app/schemas/`／
    `app/models/` 這類 Pydantic／SQLAlchemy 資料類別檔）是完全無效的
    空操作——這些檔案的膨脹來源是「一堆無關的 class 定義」，不是「函式
    本體寫太長」，剝函式本體剝不到東西。受控實驗證實：把這類檔案裁到
    只留這個 task 真的用得到的 class，跟裁到完整未裁剪版本比，成功率
    與耗時差異巨大（12-25s vs 逾時 21 分鐘），不是裁不裁都差不多。

    `seed_names`：呼叫端已經算出、這個 task 可能用到的 class 名稱（通常
    來自函式簽名的型別標註／description 文字裡出現的名稱，見
    `client.py::_referenced_class_names()`）。這裡在種子集合之上做**一層
    遞迴閉包**：被留下的 class，若它自己的欄位型別標註又指到另一個這個
    檔案裡定義的 class（最常見的模式：`ResponseResultXxxRs.data: XxxRs`，
    `XxxRs` 不會出現在目標函式簽名或 description 裡，卻是理解
    `ResponseResultXxxRs` 結構的必要資訊），也一併留下，直到不再有新
    class 被加入為止（`defined_names` 是有限集合，保證會收斂）。

    `seed_names` 為空、或跟這個檔案實際定義的 class 完全沒有交集
    （代表呼叫端的文字比對機制沒抓到任何線索，不是「這個檔案真的用不到
    任何東西」）時，**原樣回傳整份原始內容，不做任何過濾**——寧可裁不動
    也不要錯砍模型真正需要的資訊，這是跟 `strip_all_function_bodies()`
    失敗時「退回原始內容」一致的保守處理方式。

    只處理模組頂層 `ClassDef`，不處理巢狀 class；import／模組層級的
    非 class 陳述式（常數指派等）維持不變，跟 `extract_specific_functions()`
    對非目標函式陳述式的處理方式一致。
    """
    tree = ast.parse(source)

    class_nodes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    if not seed_names & class_nodes.keys():
        return source

    kept = set(seed_names) & class_nodes.keys()
    changed = True
    while changed:
        changed = False
        for name in list(kept):
            for referenced in _referenced_names_in_class_body(class_nodes[name]):
                if referenced in class_nodes and referenced not in kept:
                    kept.add(referenced)
                    changed = True

    new_body = [
        node for node in tree.body
        if not isinstance(node, ast.ClassDef) or node.name in kept
    ]
    tree.body = new_body
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _referenced_names_in_class_body(node: ast.ClassDef) -> set[str]:
    """掃這個 class 定義本身（欄位型別標註、base class 等）裡出現過的
    識別字——不下鑽進巢狀 class／函式，只看這個 class 自己的直接內容，
    足以涵蓋 Pydantic／SQLAlchemy 欄位型別標註這個目標情境。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
    names.discard(node.name)
    return names


def extract_specific_functions(source: str, targets: list[tuple[str | None, str]]) -> str:
    """對應 06a 七章「`referenced_functions`：函式層級抽取」：從
    `source` 裡只抽出 `targets` 指定的 `(class_name, function_name)` 組合
    ——完整保留這些函式的簽名與本體，其餘函式／方法／完全沒被引用到的
    class 一律捨棄；import、模組層級的非函式陳述式（常數指派等）維持
    不變。對應 `docs/09b_bug_trace.md` #37 根因：這類參考檔案不需要看到
    無關函式的實作細節，也不需要它們的簽名——只需要 `targets` 指定的那
    幾個函式的完整定義，不多不少。

    跟 `strip_all_function_bodies()`（門檻式安全網，見
    `translator_cli/client.py::_trim_context_files_if_oversized()`）方向
    相反：那個是「全部保留簽名、砍掉本體」的粗略裁減；這個是「精準只留
    被引用到的函式，其餘整個不出現」，是結構上更精確的做法，優先套用；
    裁減只在精準抽取後 context 仍然過大時才當最後一道安全網介入。

    `targets` 裡指定但在 `source` 找不到的組合（理論上不該發生，
    `interface_id` 來自③的真實輸出，見 06a 五章核對規則）不視為錯誤，
    單純不出現在結果裡——找不到的原因交由呼叫端既有的「`context_files`
    讀取容錯」機制處理，這裡不重複那層責任。
    """
    tree = ast.parse(source)
    wanted = set(targets)

    new_body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            kept_methods = [
                n
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (node.name, n.name) in wanted
            ]
            if kept_methods:
                node.body = kept_methods
                new_body.append(node)
            # 沒有任何方法被引用到的 class 整個捨棄，不留空殼。
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if (None, node.name) in wanted:
                new_body.append(node)
            # 沒被引用的自由函式捨棄。
        else:
            new_body.append(node)

    tree.body = new_body
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def extract_body_statements(body_source: str, function_name: str) -> list[ast.stmt]:
    """對應 07a 六章步驟 6／6a／6b。`body_source` 是 delimiter 抽取出的
    「未縮排」陳述式文字（07a 六章「delimiter 契約：模型回傳格式」）。

    三種失敗情況，統一拋 `TranslatorCliModelOutputError`（`SyntaxError`
    直接包一層轉成同一種例外，讓呼叫端只需要 catch 一種型別）：
    - `body_source` 通不過 `ast.parse()`（七章條件 2）
    - 解析出的陳述式清單為空（六章步驟 6a、七章條件 3）——空字串本身
      對 `ast.parse()` 是合法輸入（等同空 module，`body=[]`），若不在
      這裡攔下，會一路走到 `splice_body()` 把空清單指定給函式節點的
      `body`，`ast.unparse()` 不會報錯，而是產出「有簽名、沒有本體」
      的殘缺程式碼，要等最終檢查重新 parse 這段輸出時才會炸
      `SyntaxError`，但那時候已經不在這裡的重試觸發範圍內（見 07a
      六章步驟 6a 完整說明）
    - 解析出的陳述式清單裡，任何一筆是與 `function_name` 同名的
      `FunctionDef`／`AsyncFunctionDef`（六章步驟 6b「模型重複輸出函式
      簽名」，2026-08 因 `docs/09b_bug_trace.md #52` 從「只有一筆時才算」
      放寬成「不論混在多少其他陳述式之間都算」）——模型把整個函式簽名
      連同本體一起包進 delimiter，這在 AST 層級是合法的巢狀函式定義，
      不會被前兩種檢查攔到，替換後目標函式的 body 會變成「宣告一個從
      未被呼叫的同名巢狀函式」，語法合法但語意錯誤，因此需要單獨判斷。
      原本只檢查「body 恰好只有一筆陳述式」的版本，攔不住 ⑦
      Debug Agent 的 `fixed_body` 夾帶 import／裝飾器等其他陳述式、
      同名巢狀函式只是其中一筆的情況（真實案例：`fixed_body` 開頭帶了
      5 行 import，最後一筆才是同名巢狀 `async def`，`len(body)==1`
      判斷不成立，巢狀污染沒被攔下，見 #52 完整重現）
    """
    try:
        body_tree = ast.parse(body_source)
    except SyntaxError as exc:
        raise TranslatorCliModelOutputError(f"body_text 通不過 ast.parse()：{exc}") from exc

    if not body_tree.body:
        raise TranslatorCliModelOutputError(
            "body_text 解析出的陳述式清單為空（delimiter 標記之間只有空白／換行，見 07a 六章步驟 6a）"
        )

    nested_same_name_defs = [
        stmt.name
        for stmt in body_tree.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == function_name
    ]
    if nested_same_name_defs:
        raise TranslatorCliModelOutputError(
            f"模型重複輸出函式簽名：body_text 裡包含一個與目標函式同名的巢狀函式定義 "
            f"{function_name!r}（不限於整段 body 只有這一筆才算，見 09b_bug_trace.md #52）"
        )

    return body_tree.body


class LanguageAdapter(Protocol):
    """07a 十章：語言相關的機械操作（AST 定位／替換／渲染）跟 git
    snapshot、ollama 呼叫、delimiter 抽取、衝突偵測這類跟目標語言無關
    的通用流程分開，未來若要支援 Python 以外的目標語言，只需要另外
    實作一個 adapter。
    """

    def parse(self, source: str) -> ast.Module: ...

    def locate_function(
        self, tree: ast.Module, class_name: str | None, function_name: str
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None: ...

    def splice_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef, body_source: str) -> None: ...

    def render(self, tree: ast.Module) -> str: ...

    def validate_syntax(self, source: str) -> None: ...  # 失敗拋 SyntaxError


class PythonAdapter:
    """`LanguageAdapter` 的唯一實作，對應 07a 六章描述的 `ast` 模組操作。
    `generate_scaffold()`（`scaffold.py`）與 `fill_function()`
    （`client.py`）都透過這一層，不直接 import `ast`。
    """

    def parse(self, source: str) -> ast.Module:
        return ast.parse(source)

    def locate_function(
        self, tree: ast.Module, class_name: str | None, function_name: str
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        """對應 07a 六章步驟 3：`class_name` 為 `None` 直接在 `tree.body`
        找；非 `None` 先找 `ClassDef`，再到它的 `body` 底下找方法。
        """
        if class_name is None:
            container = tree.body
        else:
            class_node = next(
                (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None
            )
            if class_node is None:
                return None
            container = class_node.body

        return next(
            (
                n
                for n in container
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function_name
            ),
            None,
        )

    def render_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        """07a 七章「User / System Prompt 組裝」：只渲染這個函式的
        `decorator_list`＋簽名列，不含 body，讓模型知道自己在填什麼
        形狀的函式。`ast.unparse()` 沒有「只渲染簽名」的內建選項，這裡
        用一份深拷貝暫時把 body 換成單一 `pass`、`unparse` 之後再把最後
        一行（`pass`）去掉，不動到原始節點。這個方法不在 `LanguageAdapter`
        Protocol 裡——只有 Python 這個目標語言的呼叫端需要它，不影響
        「未來支援其他語言」的抽象邊界。
        """
        stub = copy.deepcopy(node)
        stub.body = [ast.Pass()]
        ast.fix_missing_locations(stub)
        lines = ast.unparse(stub).splitlines()
        return "\n".join(lines[:-1])

    def splice_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef, body_source: str) -> None:
        """對應 07a 六章步驟 6／7：驗證＋替換。驗證邏輯見
        `extract_body_statements()`——這裡是六章描述「步驟 6 之後、
        步驟 7 之前」實際發生的地方，`ollama_client.get_function_body()`
        已經在回傳前用同一個函式驗證過一次（見 07a 七章重試機制），這裡
        是 defense-in-depth，不是唯一防線。
        """
        node.body = extract_body_statements(body_source, node.name)

    def render(self, tree: ast.Module) -> str:
        """對應 07a 六章步驟 8／9：`ast.fix_missing_locations()` ＋
        `ast.unparse()`。`ast.unparse()` 會重新格式化整個檔案，這是
        刻意接受的行為（見 07a 六章「`ast.unparse()` 會重新格式化整個
        檔案」）——`generate_scaffold()` 與 `fill_function()` 一律透過
        這個方法產出最終文字，確保兩階段格式化風格天生一致。
        """
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)

    def validate_syntax(self, source: str) -> None:
        """對應 07a 六章步驟 10、四章步驟 4：寫入磁碟前的最後一道檢查。
        失敗直接讓 `SyntaxError` 往外傳，由呼叫端決定要包成
        `FillResult(success=False, ...)` 還是 `TranslatorCliAssemblyError`
        （兩種情境的處理方式不同，見各自呼叫端）。
        """
        ast.parse(source)
```

**已驗證**（`tests/translator_cli/test_python_adapter.py`，44 個測試）：涵蓋 `locate_function()` 定位、`extract_body_statements()` 的正常／語法錯誤／空 body／簽名重複輸出等驗證分支（含 09b_bug_trace.md #52 放寬版判斷：巢狀同名函式混在其他陳述式之間仍要攔下）、`splice_body()` 成功與失敗兩種路徑，以及 `strip_all_function_bodies()`／`extract_specific_functions()`（09b_bug_trace.md #37 修法）——保留 import／class 定義／屬性宣告／裝飾器、正確裁減巢狀函式與 async 函式、對真實膨脹案例的裁減幅度驗證；`extract_referenced_classes()`（09b_bug_trace.md #45 修法，`TestExtractReferencedClasses`，7 個測試）——只留種子集合命中的 class、遞迴閉包跟著欄位型別標註走、種子集合為空或無交集時原樣回傳不做任何過濾、保留 import。

---

## 四、`git_ops.py`——git snapshot、commit、衝突偵測

對應 07a 八、九章。

```python
# translator_cli/git_ops.py
"""git snapshot、commit 顆粒度、衝突偵測，對應 07a 八、九章。所有 git
指令一律以呼叫端傳入的 `python_project_path` 為 repo 根目錄
（`git -C {python_project_path} ...`），不是 translator-cli 自己執行時
的 cwd（見 07a 八章）。用列表形式呼叫 `subprocess.run`（不經 shell），
比照 `refactor_harness/core/postman_runner.py` 既有的 `run_newman()`
寫法，跨平台一致（見 01 八章「跨平台注意事項」`shell=False` 建議）。

**寫入段用細粒度鎖序列化**（見 07a 八章「決策：每個 task 一個 commit，
寫入段用細粒度鎖序列化」）：雙後端下，Claude 路徑刻意讓模型呼叫可以
平行（見 07a 七章），因此「系統結構上任何時刻最多一個 `fill_function()`
呼叫在跑」不成立——多個 Claude task 可能同時執行到「讀檔→AST替換→
寫入→commit」這段，寫同一個 git repo 有真實 race condition 風險（06a
三章允許同一檔案對應多個 task，如同一個 router 檔案有多個端點函式）。
`WRITE_LOCK` 是這段的細粒度鎖：`client.py::fill_function()` 在組
prompt、呼叫模型取得 `body_text`（不持鎖，qwen／Claude 皆然）之後、
開始讀檔／AST 替換／寫入／commit（本模組其餘函式）之前 `await
WRITE_LOCK.acquire()`，全程持鎖到 commit 完成才釋放——這段快（次秒
等級），序列化不影響整體吞吐量。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from translator_cli.exceptions import TranslatorCliDirtyWorkingTreeError, TranslatorCliError, TranslatorCliNotGitRepoError

logger = logging.getLogger(__name__)

# 見上方模組 docstring「寫入段用細粒度鎖序列化」。不分 backend、不分
# generate_scaffold()／fill_function()，任何時刻只有一個呼叫在動這個
# git repo 的寫入段。
WRITE_LOCK = asyncio.Lock()


def _run_git(python_project_path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", python_project_path, *args], capture_output=True, text=True, encoding="utf-8"
    )


def reset_python_project_dir(python_project_path: str, run_id: str) -> Path | None:
    """對應 docs/refactor_bug_trace.md #28／#29：`python_project_path`
    這個生成專案目錄過去是「每一輪 pipeline 重跑都沿用同一份、只手動
    `git init` 過一次」，導致 Reduce 每輪對模組切法／命名的判斷一旦跟
    上一輪不同，上一輪產生、這一輪已經不需要的舊檔案會原封不動留在
    目錄裡（`write_files()` 只會寫入這一輪需要的檔案，從不刪除任何
    既有檔案）——真實案例：`general_router.py`／`app/models/candidate.py`
    等上一輪的殘留檔案，跟這一輪的 `school_router.py`／`app/models/
    exam.py` 各自宣告同一張資料表，真實觸發 `sqlalchemy.exc.
    InvalidRequestError: Table 'exam_component_config' is already
    defined`。

    修法（跟使用者確認過的方向）：每輪 pipeline 開始前，若
    `python_project_path` 已存在，整個改名成帶這輪 `run_id` 的備份
    資料夾（例如 `exam-platform-api.bak-20260905_161715_4b2116`）
    ——**不刪除**，上一輪的完整內容原封不動保留在備份資料夾裡，事後
    要拿回某個檔案直接從備份資料夾複製過來即可；接著建立一個全新、
    空的資料夾並執行 `git init`，確保這一輪絕對看不到任何上一輪殘留
    的檔案，也不需要使用者每輪開始前手動介入。

    命名沿用 `common/run_context.py::new_run_id()` 的既有格式（呼叫端
    直接傳入同一個 `run_id`，不在這裡另外呼叫一次或自己組時間戳記）——
    這個備份代表的是「這一輪開始之前的舊內容」，用**這一輪**的 run_id
    命名，跟其他所有 log／report 用同一個 run_id 互相對應的既有慣例
    一致，方便事後比對是哪一輪造成了這次改名。

    回傳這次建立的備份資料夾路徑（`python_project_path` 本來就不存在、
    沒有東西可備份時回傳 `None`）——對應 docs/refactor_bug_trace.md
    #31：呼叫端（`main.py`）如果這一輪 pipeline 整個失敗（連
    `write_run_report()` 都沒執行到），需要這個路徑呼叫
    `rollback_python_project_dir()` 把備份還原回來，不能讓一個半成品的
    新目錄取代上一輪的真實產物。
    """
    path = Path(python_project_path)
    backup_path: Path | None = None
    if path.exists():
        backup_path = path.with_name(f"{path.name}.bak-{run_id}")
        logger.warning(
            "%s 已存在（上一輪的殘留內容，見 docs/refactor_bug_trace.md "
            "#28／#29）——整個改名成 %s 保留，不刪除；這一輪重新建立一個"
            "全新的空目錄",
            python_project_path, backup_path,
        )
        path.rename(backup_path)
    path.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["git", "init"], cwd=str(path), capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise TranslatorCliError(f"{python_project_path} 的 git init 失敗：{result.stderr.strip()}")
    return backup_path


def rollback_python_project_dir(python_project_path: str, backup_path: Path | None) -> None:
    """對應 docs/refactor_bug_trace.md #31：這一輪 pipeline 如果整個
    crash（連 `write_run_report()` 都沒執行到，例如真實案例
    `20260906_060030_4019f4` 的 API 額度不足），代表
    `reset_python_project_dir()` 這次建立的新目錄只是半成品，不該留著
    取代上一輪的真實產物，也不該讓下一輪把這個半成品又搬進備份堆、
    真正有意義的舊產物反而被越埋越深。`main.py` 在最外層 catch 到例外時
    呼叫這裡：直接刪掉這次的半成品新目錄，若這一輪有把上一輪內容搬進
    備份（`backup_path` 非 `None`）就改名還原回 `python_project_path`，
    下一輪重新開始時看到的仍是上一輪的真實產物；`backup_path` 是
    `None` 代表這一輪開始前本來就沒有東西可備份，只需要刪掉半成品，
    沒有東西可還原。

    對應真實案例 `20260906_152648_d36540`：這裡原本直接對半成品新目錄
    `shutil.rmtree()`，在 Windows 上遇到剛 `git init` 產生的 `.git`
    內部檔案短暫的作業系統層級鎖定，丟出 `PermissionError:
    [WinError 5]`——若在還原備份「之前」就失敗，會讓真正有價值的備份
    留在原地沒被還原，`python_project_path` 卻已經被刪到一半，兩邊都
    是壞狀態。改成：先把半成品新目錄改名讓出 `python_project_path`
    這個路徑（改名是原子操作，不會半途而廢），還原備份，最後才盡力
    刪除改名後的半成品殘骸——刪不掉只記警告，不影響前面已經完成的
    還原結果。
    """
    path = Path(python_project_path)
    half_finished_path: Path | None = None
    if path.exists():
        half_finished_path = path.with_name(f"{path.name}.rollback-tmp-{uuid.uuid4().hex[:8]}")
        path.rename(half_finished_path)
    if backup_path is not None and backup_path.exists():
        backup_path.rename(path)
    if half_finished_path is not None:
        try:
            shutil.rmtree(half_finished_path)
        except OSError as exc:
            logger.warning(
                "刪除半成品目錄 %s 失敗（不影響前面的還原結果，"
                "見 docs/refactor_bug_trace.md #42）：%s",
                half_finished_path, exc,
            )


def ensure_git_repo(python_project_path: str) -> None:
    """對應 07a 二章「一次性前置準備」：`generate_scaffold()` 執行前
    檢查目標目錄是不是一個 git repo，不是則直接中止並回報明確錯誤——
    這是輸入端環境沒準備好，不是可以自動補救的情況。

    不另外檢查目錄是否存在：`git -C {不存在的路徑}` 本身就會乾淨地
    失敗（`fatal: cannot change to '...': No such file or directory`，
    returncode 128），不需要在呼叫 git 之前自己先判斷一次，那只是重複
    git 已經會做的事。
    """
    result = _run_git(python_project_path, "rev-parse", "--is-inside-work-tree")
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise TranslatorCliNotGitRepoError(
            f"{python_project_path} 不是一個 git repo（見 07a 二章「一次性前置準備」，"
            f"需先在目標目錄執行 git init）：{result.stderr.strip()}"
        )


def check_clean_working_tree(python_project_path: str) -> None:
    """對應 07a 九章「衝突偵測」：`generate_scaffold()`／`fill_function()`
    動筆寫任何檔案之前的 precondition 檢查。輸出非空 → 不寫入任何內容，
    直接回傳失敗，交由人工核對這份意外的變更是什麼。
    """
    result = _run_git(python_project_path, "status", "--porcelain")
    if result.returncode != 0:
        # 指令本身跑不動（權限問題、repo 損毀等）跟「不是 git repo」是
        # 不同語意，不套用 TranslatorCliNotGitRepoError 這個字面說法，
        # 避免誤導排查方向（`ensure_git_repo()` 已經在更早的
        # precondition 檢查過一次是不是 git repo，這裡若還失敗代表狀況
        # 更複雜）。
        raise TranslatorCliError(f"git status 執行失敗：{result.stderr.strip()}")

    if result.stdout.strip():
        raise TranslatorCliDirtyWorkingTreeError(
            f"working tree 不乾淨，拒絕寫入（見 07a 九章）：{result.stdout.strip()}"
        )


def diff_for_file(python_project_path: str, target_file: str) -> str:
    """對應 07a 八章：在 `git add` 之前擷取，此時檔案已寫入磁碟但尚未
    進 staging area，working tree 依九章 precondition 保證動筆前是
    乾淨的，這個 diff 精確反映這次 `fill_function()` 造成的變更。
    """
    result = _run_git(python_project_path, "diff", "--", target_file)
    return result.stdout


def commit_scaffold(python_project_path: str) -> None:
    """對應 07a 八章：`generate_scaffold()` 成功寫入所有骨架檔案後的
    一次性 commit，作為後續所有 `fill_function()` commit 的共同基礎。
    """
    _run_git(python_project_path, "add", "-A")
    result = _run_git(python_project_path, "commit", "-m", "scaffold: initial skeleton from python_structure")
    if result.returncode != 0:
        raise TranslatorCliError(f"scaffold commit 失敗：{result.stderr.strip()}")


def commit_fill(
    python_project_path: str,
    *,
    task_id: str,
    target_file: str,
    class_name: str | None,
    function_name: str,
) -> None:
    """對應 07a 八章：只 add 這次實際寫入的那一個檔案，不用 `-A`——即使
    working tree 因為某種原因存在其他未預期的變更（理論上不該發生，見
    九章），也不會被這次 commit 意外一起帶走。`task_id` 是必要引數
    （見 07a 二章），保留 `class_name.function_name` 是為了讓 `git log`
    一眼看出這個 commit 改的是哪個函式。
    """
    label = f"{class_name}.{function_name}" if class_name else function_name
    _run_git(python_project_path, "add", target_file)
    result = _run_git(
        python_project_path, "commit", "-m", f"implement: {task_id} fill {label} in {target_file}"
    )
    if result.returncode != 0:
        raise TranslatorCliError(f"fill_function commit 失敗（task {task_id}）：{result.stderr.strip()}")


def commit_file_fix(python_project_path: str, *, task_id: str, target_file: str) -> None:
    """對應 `client.apply_file_fix()`（10a 八章「phase 2：檔案層級
    修正」）：⑦ Debug Agent 給的修正若碰的是函式本體以外的內容（如
    import 敘述），不是「填某個函式」，用跟 `commit_fill()` 不同的訊息
    格式，讓 `git log` 能區分「⑤ 生成函式」跟「⑦ 直接修正檔案層級
    內容」這兩種性質不同的變更。
    """
    _run_git(python_project_path, "add", target_file)
    result = _run_git(
        python_project_path, "commit", "-m", f"debug: {task_id} apply file-level fix in {target_file}"
    )
    if result.returncode != 0:
        raise TranslatorCliError(f"apply_file_fix commit 失敗（task {task_id}）：{result.stderr.strip()}")


def discard_file_changes(python_project_path: str, target_file: str) -> bool:
    """`commit_fill()` 失敗後的復原路徑用（見 `client.fill_function()`）：
    把 `target_file` 還原回目前 HEAD 的內容，撤銷這次失敗的 commit 之前
    寫入磁碟的新內容。`git commit` 若因不可抗力（index lock、權限問題）
    失敗，`write_text()` 已經落地的變更會讓 working tree 卡在「不乾淨」
    狀態，之後每一次 `check_clean_working_tree()` precondition 檢查都會
    連帶失敗——這對這一個 task 而言是可歸因、已知成因的變更（就是我們
    自己剛寫入、但沒能進版控的那份內容），不是九章「衝突偵測」要攔的
    那種「來源不明的意外變更」，因此可以安全地自動撤銷，只把這一個
    task 標記失敗，不讓後續所有 task 被這次 commit 失敗拖累卡住。

    **`git reset` 必須先於 `git checkout`**：`commit_fill()` 失敗前已經
    執行過 `git add {target_file}`，內容已經寫進 index，不是只留在
    working tree——`git checkout -- <path>` 預設是「用 index 內容覆蓋
    working tree」，並不會動 index 本身，若 index 裡已經是這次沒能
    commit 成功的新內容，`checkout` 只會把 working tree 覆蓋回同一份
    「壞」內容，等於什麼都沒還原（已用真實 git 行為驗證過：`add` 後
    只執行 `checkout` 不執行 `reset`，working tree 內容完全不變）。
    `git reset -- <path>` 把 index 還原回目前 HEAD 的狀態（沒有 HEAD
    的情況下也能安全執行），之後 `checkout` 才能真正把 working tree
    也拉回 HEAD 的內容。

    回傳是否成功還原（透過檢查最終 `git status` 是否乾淨，而不是任一
    個別指令的 returncode）。
    """
    _run_git(python_project_path, "reset", "--", target_file)
    _run_git(python_project_path, "checkout", "--", target_file)
    status = _run_git(python_project_path, "status", "--porcelain", "--", target_file)
    return status.returncode == 0 and not status.stdout.strip()


def discard_written_files(python_project_path: str, file_paths: list[str]) -> bool:
    """`commit_scaffold()` 失敗（或 `write_files()` 寫到一半發生
    `OSError`）後的復原路徑用（見 `client.generate_scaffold()`），道理
    跟 `discard_file_changes()` 一致，只是骨架階段一次寫入多個檔案。
    `file_paths` 是 `scaffold.build_files()` 實際算出、這次真正打算
    寫入的檔案清單（`client.py` 呼叫時傳 `list(files.keys())`）——**只**
    對這批路徑執行 `reset`／`checkout`／`clean`，不對整個 repo 做無
    差別還原。

    **範圍刻意限縮到這次寫入的精確檔案清單，不是整個 repo**：
    `python_project_path` 依 07a 二章設計是 translator-cli 專屬 repo，
    但這個前提依賴 `.env` 的 `PYTHON_PROJECT_PATH` 設定正確——萬一設定
    錯誤、誤指到一個帶有其他重要內容的目錄，整個 repo 範圍的 `clean`
    會有無法挽回的風險。既然 `build_files()` 當下就已經精確知道這次要
    寫哪些檔案，只對這批路徑動作，不論 `python_project_path` 實際指向
    哪裡，這個函式的破壞範圍都嚴格限制在「這次呼叫自己打算寫入的
    檔案」，不依賴任何關於目錄內容的外部假設。

    **`git reset` 必須先於 `checkout`／`clean`**：`commit_scaffold()`
    失敗前已經執行過 `git add -A`，這批檔案的內容已經寫進 index——
    `checkout` 只會用 index 內容覆蓋 working tree，`clean -fd` 也只
    處理未追蹤的檔案，兩者都不會動 index 本身。若 index 裡已經是這次
    沒能 commit 成功的新內容，光靠 `checkout`＋`clean` 等於什麼都沒
    還原、index 依然停在「已 add」的髒狀態（已用真實 git 行為驗證
    過）。`git reset -- p1 p2 p3` 先把這批路徑的 index 還原回目前 HEAD
    的狀態——不論這個 repo 有沒有任何 commit，`git reset` 都能安全
    執行（沒有 HEAD 時等同「把這批路徑從 index 整個移除」），之後
    `checkout`／`clean` 才能真正發揮作用。`git reset` 對多個 pathspec
    不是「全有全無」（跟下面 `checkout` 的限制不同，已驗證混合「已
    追蹤＋從未追蹤」的路徑一次呼叫就能正確處理），不需要逐一呼叫。

    **`git checkout` 對多個 pathspec 是「全有全無」，逐一呼叫**：若把
    `file_paths` 一次全部傳給同一個 `git checkout -- p1 p2 p3` 指令，
    只要其中一個路徑是全新、從未被追蹤過的檔案（`git` 眼中「不存在的
    pathspec」），整個指令會直接失敗、**其餘合法路徑也不會被還原**
    （已用真實 git 行為驗證過，多路徑不是逐一嘗試、是整批放棄）。
    因此改成逐一對每個路徑呼叫 `checkout`，個別失敗（代表這個路徑是
    全新檔案、本來就沒有舊版本可還原）直接忽略，交給下一步 `git clean`
    處理；`git clean -fd` 支援多個 pathspec 一次處理、不需要逐一呼叫。

    **判斷成功與否看最終狀態，不是每個指令各自的 returncode**：`git
    status --porcelain -- p1 p2 p3` 把檢查範圍限定在這批路徑，只要
    這批路徑都乾淨（不論是成功還原、或是被 `clean` 移除）就算成功。

    **`clean` 之後額外呼叫 `_prune_empty_parent_dirs()`**：`git clean
    -fd` 只刪未追蹤的檔案本身，不處理它留下的空目錄——`write_files()`
    幫全新檔案建的巢狀目錄（`app/models/` 這類）被清空後會原樣留在
    磁碟上，`git status` 判定乾淨（空目錄本來就不受 git 追蹤），但磁碟
    狀態沒有完全回到執行前，這裡補一步清掉。
    """
    if not file_paths:
        return True

    _run_git(python_project_path, "reset", "--", *file_paths)
    for path in file_paths:
        _run_git(python_project_path, "checkout", "--", path)
    _run_git(python_project_path, "clean", "-fd", "--", *file_paths)
    _prune_empty_parent_dirs(python_project_path, file_paths)
    status = _run_git(python_project_path, "status", "--porcelain", "--", *file_paths)
    return status.returncode == 0 and not status.stdout.strip()


def _prune_empty_parent_dirs(python_project_path: str, file_paths: list[str]) -> None:
    """`write_files()` 用 `parent.mkdir(parents=True, exist_ok=True)`
    幫全新檔案建立巢狀目錄（見 `scaffold.write_files()` docstring）；
    `git clean -fd` 只刪未追蹤的檔案本身，不處理它留下的空目錄（已用
    真實 git 行為驗證過：對單一檔案路徑執行 `clean -fd` 後，該檔案剛
    建立的父目錄若因此變空，會原樣留在磁碟上）。這裡對每個 file_path
    的父目錄逐層往上嘗試 `rmdir()`，只在目錄確實是空的才會成功、遇到
    非空或不存在就直接停止（`OSError` 忽略），並且不會往上超出
    `python_project_path` 這個 repo 根目錄，避免動到 rollback 範圍以外
    的任何東西。
    """
    root = Path(python_project_path).resolve()
    for file_path in file_paths:
        parent = (root / file_path).parent.resolve()
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
```

**rollback 不違反「衝突偵測不自動化解」的原則**：07a 九章要防的是「來源不明的意外變更」（人工手動改了什麼、上一輪執行中途被強制中斷）；`discard_file_changes()`／`discard_written_files()` 撤銷的是**這次呼叫自己造成、成因完全已知**的變更（就是剛寫入但沒 commit 成功的內容），可歸因、可安全撤銷，兩者是不同類別的「dirty」。若不這麼做，一次 `git commit` 失敗（index lock、權限問題）就會讓 working tree 卡在「不乾淨」，之後每一次呼叫的 precondition 檢查都會連帶失敗，等同整條 pipeline 卡死。

**已驗證**（`tests/translator_cli/test_git_ops.py`，26 個測試，全部對真實 `tmp_path` git repo 執行，不 mock `subprocess`）：涵蓋 `ensure_git_repo()`／`check_clean_working_tree()` 各種前置狀態、`commit_scaffold()`／`commit_fill()` 的 commit message 與範圍，以及 `discard_file_changes()`／`discard_written_files()` 的還原路徑——包含「檔案已經 `git add` 進 index 但 commit 本身失敗」這個真實時序（`git checkout`／`clean` 都不會動 index，沒有先 `reset` 的話 rollback 會完全失效，見對應函式 docstring）與巢狀目錄清空後一併清除的邊界情況。另涵蓋 `reset_python_project_dir()`／`rollback_python_project_dir()`（docs/refactor_bug_trace.md #28／#29／#31／#42）：既有內容改名備份不刪除、備份路徑用當輪 `run_id` 命名、crash 後刪半成品＋還原備份、`shutil.rmtree()` 半途失敗（`PermissionError`）時備份仍正確還原且殘骸只記警告等情況。`commit_file_fix()`（`apply_file_fix()` 用，見九章）與 `WRITE_LOCK`（`fill_function()`／`apply_file_fix()` 都要取得才能進入寫入段，見九章）本身沒有獨立的 `test_git_ops.py` 測試，透過 `tests/translator_cli/test_client.py` 的 `test_apply_file_fix_*` 系列與 `test_fill_function_waits_for_write_lock_before_writing` 間接涵蓋。

---

## 五、`prompts.py`——system／user prompt 模板

對應 07a 七章「System / User Prompt 組裝」。

兩條路徑各自的 system prompt——`SYSTEM_PROMPT_QWEN`／`SYSTEM_PROMPT_CLAUDE`：qwen 路徑走 delimiter 契約，Claude 路徑走 Structured Outputs（`BODY_STATEMENTS_SCHEMA`，見 07a 七章「Claude 路徑：連線方式」），因此 system prompt 內容不同（Claude 版不需要重述 delimiter 標記格式）。`build_user_prompt()` 不吃 `description`（06a 五章已定調改為純機械模板，只供 log 用途），改吃 `java_source`（這個函式對應的真實 Java 原始碼）與 `referenced_source`（呼叫鏈展開出的真實原始碼），見 07a 五章「為什麼是 `java_source`／`referenced_source`」。兩份 system prompt 都有「呼叫，不要內嵌」核心指示（見 06a 六章「同層 Java 參照的用途」）——`referenced_source` 是給模型看「該怎麼正確呼叫」，不是給它「拿去合併改寫」。

```python
# translator_cli/prompts.py
"""system／user prompt 模板，對應 07a 七章「System / User Prompt 組裝」。
qwen／Claude 兩條路徑各自的 system prompt 固定不變（只定義輸出格式契約，
不含業務內容），user prompt 依每次呼叫組裝，兩條路徑共用同一個
`build_user_prompt()`。

不吃 `description`（06a 五章已定調改為純機械模板，只供 log 用途，不是
模型輸入）——⑤ 直接讀 `java_source`（這個函式所在的整個 Java 檔案真實
原始碼，見 07a 五章「`java_source` 是整個檔案」）與 `referenced_source`
（呼叫鏈展開出的真實原始碼，Java 或已翻譯 Python），見 07a 五章。兩條
路徑的 system prompt 都明確加入「呼叫，不要內嵌」核心指示（見 06a 六章
「同層 Java 參照的用途」）——`referenced_source` 是給模型看「該怎麼正確
呼叫」，不是給它「拿去合併改寫」。
"""
from __future__ import annotations

from typing import Any

from translator_cli.types import ReferencedSourceItem

BODY_START = "<<<TRANSLATOR_CLI_BODY_START>>>"
BODY_END = "<<<TRANSLATOR_CLI_BODY_END>>>"

_CALL_NOT_INLINE_INSTRUCTION = """你的任務：把對應的 Java 邏輯改寫成正確的 Python 實作。你收到的其他函式原始碼是給你參考
「該怎麼呼叫它」（參數、回傳型別、行為），不是要你把那些函式的邏輯合併或複製進來——你的
實作應該真正呼叫（import + invoke）那些函式，就像原始 Java 程式碼本來就是這樣呼叫的一樣。"""

SYSTEM_PROMPT_QWEN = f"""你是一個 Python 程式碼填空工具。你會收到一個已經定義好簽名的 Python 函式（骨架已建好，
可能是空的 `pass`，也可能已有既有邏輯），以及這個函式對應的真實原始碼、以及它呼叫到的其他
函式的真實原始碼（Java 或已翻譯完成的 Python）。

{_CALL_NOT_INLINE_INSTRUCTION}

只回傳這個函式的「本體」陳述式，不要重複函式簽名、不要重複裝飾器、不要加任何解說文字。
也不要在程式碼裡用 `#` 寫任何註解——這個工具用 AST 處理輸出，`#` 註解一律會被直接丟棄，
不會出現在最終寫入的檔案裡，寫了也是白費。回傳格式固定如下，只在這兩個標記之間寫程式碼，
標記本身也要原樣附上：

{BODY_START}
（函式本體陳述式，視為第一層縮排，不要自己加縮排）
{BODY_END}
"""

SYSTEM_PROMPT_CLAUDE = f"""你是一個 Python 程式碼填空工具。你會收到一個已經定義好簽名的 Python 函式（骨架已建好，
可能是空的 `pass`，也可能已有既有邏輯），以及這個函式對應的真實原始碼、以及它呼叫到的其他
函式的真實原始碼（Java 或已翻譯完成的 Python）。

{_CALL_NOT_INLINE_INSTRUCTION}

只回傳這個函式的「本體」陳述式，不要重複函式簽名、不要重複裝飾器、不要加任何解說文字。
也不要在程式碼裡用 `#` 寫任何註解——這個工具用 AST 處理輸出，`#` 註解一律會被直接丟棄，
不會出現在最終寫入的檔案裡，寫了也是白費。
"""

# 對應 07a 七章「Claude 路徑：連線方式」，Structured Outputs 的
# constrained decoding schema——`body_statements` 就是 qwen 路徑 delimiter
# 抽取出來的同一份 body_text，兩條路徑取得之後走同一套六章驗證／替換流程。
BODY_STATEMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "body_statements": {"type": "string"},
    },
    "required": ["body_statements"],
    "additionalProperties": False,
}


def _format_referenced_source(items: list[ReferencedSourceItem]) -> str:
    blocks = []
    for item in items:
        label = f"{item['class_name']}.{item['function_name']}" if item["class_name"] else item["function_name"]
        lang = "Java" if item["language"] == "java" else "已翻譯 Python"
        blocks.append(f"### {item['file_path']} :: {label}（{lang}）\n```\n{item['source']}\n```")
    return "\n\n".join(blocks)


def build_user_prompt(
    *,
    current_signature: str,
    java_source: str,
    referenced_source: list[ReferencedSourceItem],
    context: str,
    context_files: list[tuple[str, str]],
    error_feedback: str | None = None,
) -> str:
    """對應 07a 七章「User / System Prompt 組裝」：函式目前的簽名（不含
    body）、`java_source`、`referenced_source`（逐項列出 `file_path` /
    `class_name` / `function_name` / `language` + 原始碼內容）、`context`
    （若非空）、`context_files` 逐檔案列出「路徑 + 完整內容」。`description`
    不放進這裡（見 06a 五章、07a 五章，純 log 用途）。`error_feedback`
    非 `None` 時，額外附上七章「兩條路徑共用：body_text 格式修正重試」
    規定的錯誤回饋文字。
    """
    parts = [
        f"函式目前的簽名：\n```python\n{current_signature}\n```",
        f"這個函式對應的原始 Java 方法：\n```\n{java_source}\n```",
    ]
    if referenced_source:
        parts.append(
            "這個函式呼叫到的其他函式的真實原始碼（import 路徑必須逐字複製每筆標題裡的 "
            f"file_path，不得自行另猜路徑）：\n\n{_format_referenced_source(referenced_source)}"
        )
    if context:
        parts.append(f"補充說明：\n{context}")
    if context_files:
        file_blocks = "\n\n".join(f"### {path}\n```python\n{content}\n```" for path, content in context_files)
        parts.append(f"相關檔案內容：\n\n{file_blocks}")
    if error_feedback:
        parts.append(
            f"上一次回應違反格式／語法錯誤，錯誤訊息是：{error_feedback}，請重新產生"
        )
    return "\n\n".join(parts)
```

`BODY_START`／`BODY_END` 定義在這裡、被 `ollama_client.py` import，避免 delimiter 字面字串在兩個檔案裡各自寫一份、日後改格式漏改其中一處——Claude 路徑（`claude_client.py`）走 Structured Outputs，不需要 delimiter，不 import 這兩個常數。`BODY_STATEMENTS_SCHEMA` 只給 Claude 路徑用。

---

## 六、`ollama_client.py`——ollama 連線與重試

對應 07a 七章全節。

```python
# translator_cli/ollama_client.py
"""ollama 連線與請求契約，對應 07a 七章。translator-cli 不直接打
ollama，而是打 `.env` 的 `OLLAMA_BASE_URL`（指向另一台 Mac 上的
nginx，已含 `/v1` 路徑前綴），帶 `Authorization: Bearer
{OLLAMA_API_KEY}`（見 00 三、四、五章已定案的架構）。

**呼叫記錄**：對應 `11a_logging_architecture.md` 九章。每個格式修正
attempt 各自記一筆 trace（見 `get_function_body()`），不是只記最後一次；
`_call_ollama_once()` 本身不記錄，因為它只知道單次 HTTP 請求結果，不知道
這是第幾次格式修正重試。每個 attempt 在呼叫發出「之前」先寫入
`status="running"` 的 row（只含 prompt），呼叫結束才 upsert 補齊
response／status／latency_ms——本地模型單次生成可能耗時數十秒到數分鐘，
這段等待期間 prompt 已經可以被 `llmlog` 查到，不必等呼叫結束。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from uuid import uuid4

import httpx

from common.llm_trace import record_llm_call, record_llm_call_start
from common.trace_context import current_trace_id
from translator_cli import python_adapter
from translator_cli.exceptions import (
    TranslatorCliConfigError,
    TranslatorCliModelOutputError,
    TranslatorCliNetworkError,
    TranslatorCliUpstreamDegradedError,
)
from translator_cli.prompts import BODY_END, BODY_START, SYSTEM_PROMPT_QWEN, build_user_prompt
from translator_cli.types import ReferencedSourceItem

logger = logging.getLogger(__name__)

# 00 三章「硬體限制」已把模型選型釘死為單一選擇，不經環境變數——跟
# common/llm_client.py 對 Claude 模型「呼叫端自己決定用哪個模型」的
# 分工原則刻意不同（見 07a 七章「連線方式」）。
OLLAMA_MODEL = "qwen2.5-coder:32b"

# 07a 七章「Timeout 與重試策略」表格，環境變數可調、有預設值。
TRANSLATOR_CLI_TIMEOUT_SECONDS = float(os.environ.get("TRANSLATOR_CLI_TIMEOUT_SECONDS", "300"))
# 對應 docs/refactor_bug_trace.md：qwen 這一路原本網路層重試 2 次、
# 格式修正重試再 2 次，兩層疊加、每層每次最長可等到
# TRANSLATOR_CLI_TIMEOUT_SECONDS（300 秒），真實環境對真實
# qwen2.5-coder:32b 實測，單一個 task 卡在這兩層重試裡最長可以耗掉
# 20~45 分鐘才等到失敗訊號、才輪到 client.py::fill_function() 的
# Claude fallback 接手。qwen 本身已經在掙扎的呼叫，重試不會讓它突然
# 變聰明（重試解決的是「暫時性」問題，不是「這個模型這次就是做不到」的
# 問題）——與其在同一顆會失敗的模型上反覆等待，不如失敗一次就立刻換
# Claude API 重試同一個 task（見 client.py 的 fallback），兩層重試次數
# 都改成 0：只試一次，失敗就馬上交棒。
TRANSLATOR_CLI_NETWORK_RETRIES = int(os.environ.get("TRANSLATOR_CLI_NETWORK_RETRIES", "0"))
_NETWORK_RETRY_DELAY_SECONDS = 3.0
# qwen 專屬的格式修正重試次數——刻意跟 Claude 路徑（claude_client.py）
# 的 FORMAT_RETRY_COUNT 分開成兩個獨立常數，不能共用：上面的理由（重試
# 不會讓一個正在掙扎的模型變聰明）只對 qwen 這條路徑成立，Claude 呼叫
# 快、便宜，沒有「等待時間過長」這個痛點，Claude 路徑仍沿用原本的
# FORMAT_RETRY_COUNT=2，不受這次調整影響。
OLLAMA_FORMAT_RETRY_COUNT = int(os.environ.get("OLLAMA_FORMAT_RETRY_COUNT", "0"))
# Claude 路徑（claude_client.py）自己的格式修正重試上限，從這裡 import，
# 不重複定義；qwen 路徑改用上面的 OLLAMA_FORMAT_RETRY_COUNT，不再共用
# 這個常數（見 07a 七章「兩條路徑共用：body_text 格式修正重試」）。
FORMAT_RETRY_COUNT = 2

# 對應 07a 七章「分派方式」／八章「寫入段序列化」：實體機器單一 qwen
# 模型的併發限制收在這裡自己管理——只序列化「真正打 ollama」這一段，
# 不連帶序列化 Claude 路徑（見 client.py::fill_function()「依 backend
# 決定要不要 acquire 這個號誌」）。
OLLAMA_MODEL_SEMAPHORE = asyncio.Semaphore(1)

# 見 docs/09b_bug_trace.md #35：單一 task 自己的網路層重試（見
# `_call_ollama_once()`）解決不了「上游服務本身異常」這種系統性問題，只
# 是各自獨立地把自己的重試預算燒完。這裡加一個跨 task、模組層級的連續
# 失敗計數器——`MODEL_SEMAPHORE(1)` 本來就把所有呼叫序列化成一個接一個，
# 「連續」在這裡天然對應「最近這幾次呼叫，不管是哪個 task 的，是不是都
# 在傳輸層失敗」，不需要額外的鎖或跨 task 協調機制。任何一次成功拿到
# 回應（含收到錯誤狀態碼的 HTTPStatusError，那也證明連線本身是通的）就
# 歸零；連續失敗次數達到門檻，代表這不太可能是單一 task 的暫時性問題，
# 改拋出 `TranslatorCliUpstreamDegradedError`，讓呼叫端（見
# `client.py::fill_function()`）能提早停下來，不要繼續逐一重試。
UPSTREAM_DEGRADED_THRESHOLD = int(
    os.environ.get("TRANSLATOR_CLI_UPSTREAM_DEGRADED_THRESHOLD", "3")
)
_consecutive_transport_failures = 0

_DELIMITER_RE = re.compile(re.escape(BODY_START) + r"\n?(.*?)\n?" + re.escape(BODY_END), re.DOTALL)


def _extract_delimited_body(raw_text: str) -> str:
    """對應 07a 六章「delimiter 契約：模型回傳格式」抽取邏輯：正則找
    兩個 sentinel 之間的文字。找不到 → 視為「delimiter 契約違反」，
    觸發七章重試策略（見 `get_function_body()`）。
    """
    match = _DELIMITER_RE.search(raw_text)
    if not match:
        raise TranslatorCliModelOutputError(
            f"delimiter 契約違反：模型回應找不到 {BODY_START} / {BODY_END} 標記"
        )
    return match.group(1)


async def _call_ollama_once(system_prompt: str, user_prompt: str) -> str:
    """對應 07a 七章「Timeout 與重試策略」表格：HTTP 連線失敗／timeout
    這類傳輸層錯誤（`httpx.TransportError`／`httpx.TimeoutException`），
    固定間隔 3 秒立即重試 `TRANSLATOR_CLI_NETWORK_RETRIES` 次——這是 LAN
    內部連線的暫時抖動，不是 Claude API 那種需要等配額恢復的限流情境，
    不需要 5 分鐘等待。**`httpx.HTTPStatusError`（收到回應，但狀態碼是
    4xx／5xx）刻意不算進這個重試迴圈**：07a 明確把重試範圍限定在傳輸層
    錯誤，HTTP 狀態碼錯誤是不同性質——例如 nginx token 設錯導致每次都
    回 401，這是必然失敗、重試沒有意義的情況，立即失敗回報，不拖慢
    失敗訊號的傳遞速度。

    `httpx.AsyncClient` 建立在重試迴圈外、只建立一次：同一次呼叫的多次
    網路層重試是短時間內（間隔 `_NETWORK_RETRY_DELAY_SECONDS` 秒）對同
    一個 `base_url` 的連續嘗試，沒有理由每次重試都重新握手 TCP／TLS。
    這個 client 是單一函式呼叫內的區域資源、隨 `async with` 結束自動
    釋放，不跨呼叫、不跨時間共用——`OLLAMA_MODEL_SEMAPHORE(1)`（見上方
    常數）已經把這條路徑序列化，不存在真正的併發連線需求，因此不做跨
    呼叫的 client 重用（module-level singleton）。

    整段函式（含網路層重試迴圈）都在 `OLLAMA_MODEL_SEMAPHORE` 底下執行
    ——見呼叫端 `get_function_body()`，這裡本身不 acquire，避免重試迴圈
    內部重入造成死鎖。
    """
    try:
        base_url = os.environ["OLLAMA_BASE_URL"]
        api_key = os.environ["OLLAMA_API_KEY"]
    except KeyError as exc:
        raise TranslatorCliConfigError(
            f"缺少必要的環境變數 {exc}（見 07a 七章「連線方式」，需在 .env 設定 OLLAMA_BASE_URL／OLLAMA_API_KEY）"
        ) from exc

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    global _consecutive_transport_failures

    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=TRANSLATOR_CLI_TIMEOUT_SECONDS) as client:
        for attempt in range(TRANSLATOR_CLI_NETWORK_RETRIES + 1):
            try:
                response = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # 有收到回應，只是狀態碼是錯誤——連線本身是通的，歸零連續
                # 失敗計數器（見模組層級 _consecutive_transport_failures
                # 說明），立即失敗，不進重試迴圈。
                _consecutive_transport_failures = 0
                raise TranslatorCliNetworkError(
                    f"ollama 回應 HTTP 錯誤狀態碼（非傳輸層錯誤，不重試）：{exc}"
                ) from exc
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt < TRANSLATOR_CLI_NETWORK_RETRIES:
                    logger.warning(
                        "ollama 連線失敗（第 %d/%d 次），%.0f 秒後重試：%s",
                        attempt + 1,
                        TRANSLATOR_CLI_NETWORK_RETRIES,
                        _NETWORK_RETRY_DELAY_SECONDS,
                        exc,
                    )
                    await asyncio.sleep(_NETWORK_RETRY_DELAY_SECONDS)
                continue

            _consecutive_transport_failures = 0
            data = response.json()
            return data["choices"][0]["message"]["content"]

    _consecutive_transport_failures += 1
    message = (
        f"ollama 連線失敗，已重試 {TRANSLATOR_CLI_NETWORK_RETRIES} 次仍失敗：{last_error}"
    )
    # `from last_error` 讓 __cause__ 帶著真正的傳輸層例外——get_function_body()
    # 靠這個判斷 status 該記 "timeout" 還是 "error"（見 11a 九章），也讓
    # traceback 保留原始成因，不只是這裡重新組的訊息字串。
    if _consecutive_transport_failures >= UPSTREAM_DEGRADED_THRESHOLD:
        raise TranslatorCliUpstreamDegradedError(
            f"連續 {_consecutive_transport_failures} 次（跨不同呼叫）都在傳輸層失敗"
            f"（門檻 {UPSTREAM_DEGRADED_THRESHOLD}），懷疑是上游 ollama／nginx 服務本身"
            f"異常（掛了／重啟中／過載），不是單一暫時性問題：{message}"
        ) from last_error
    raise TranslatorCliNetworkError(message) from last_error


# 對應 docs/refactor_bug_trace.md #7：`_call_ollama_once()` 的連續失敗
# 偵測（上方 UPSTREAM_DEGRADED_THRESHOLD）是被動的——要等真的排到
# repository task、燒完門檻次數才會發現。這裡補一個主動預檢，讓
# implement_node.py 在真正第一次進入時（見該檔案 run()）就先確認網路
# 通不通，不用等浪費掉幾次真正的模型呼叫預算才知道連不上。
OLLAMA_PREFLIGHT_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_PREFLIGHT_TIMEOUT_SECONDS", "10"))


async def check_ollama_reachable() -> None:
    """比照 `graph/nodes/implement_node.py::_get_reload_probe_id()` 對
    Python 服務的初始探測同一種「操作前提沒滿足，不吞、直接往上拋」原則
    ——只確認連線本身通不通（收到任何 HTTP 回應，不論狀態碼），不呼叫
    真正的 chat completion（探測本身不該浪費一次模型呼叫的時間與成本）。
    只在呼叫端確認這次 run 確實有 qwen／repository task 時才需要呼叫這個
    函式（見呼叫端），沒有 qwen task 的 run 不需要 ollama 連線，不該因為
    這個預檢而被誤擋。
    """
    try:
        base_url = os.environ["OLLAMA_BASE_URL"]
    except KeyError as exc:
        raise TranslatorCliConfigError(
            f"缺少必要的環境變數 {exc}（見 07a 七章「連線方式」，需在 .env 設定 OLLAMA_BASE_URL／OLLAMA_API_KEY）"
        ) from exc

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_PREFLIGHT_TIMEOUT_SECONDS) as client:
            await client.get(base_url)
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        raise TranslatorCliUpstreamDegradedError(
            f"啟動前預檢：連不到 ollama（{base_url}），懷疑是網路連線或 "
            "ollama／nginx 服務本身異常，不是可以透過 debug 重試修好的翻譯"
            f"品質問題——請確認 OLLAMA_BASE_URL 是否正確、這台機器是否連得到"
            f"該服務後再重跑：{exc}"
        ) from exc
    # 收到任何 HTTP 回應（含 4xx/5xx，例如 base_url 本身不接受 GET）都
    # 代表連線本身是通的，視為預檢通過——這裡刻意不呼叫 raise_for_status()。


async def get_function_body(
    *,
    current_signature: str,
    java_source: str,
    referenced_source: list[ReferencedSourceItem],
    context: str,
    context_files: list[tuple[str, str]],
    function_name: str,
    task_id: str | None = None,
    target_file: str | None = None,
    class_name: str | None = None,
    run_id: str,
) -> str:
    """對外唯一入口，對應 07a 七章「模型輸出格式錯誤的修正重試」。

    整段函式（含網路層重試、格式修正重試）都在 `OLLAMA_MODEL_SEMAPHORE`
    底下執行——見下方迴圈開頭 `async with OLLAMA_MODEL_SEMAPHORE`，這是
    實體機器單一 qwen 模型的硬體限制，收在本模組自己管理（見 07a 七章
    「分派方式」）。

    `java_source`／`referenced_source` 是模型輸入，`description` 不是
    （06a 五章已定調改為純機械模板、只供 log 用途，見 07a 五章）。

    固定重試 2 次（`FORMAT_RETRY_COUNT`）：每次失敗後，下一次呼叫的
    user prompt 額外附上「上一次回應違反格式／語法錯誤訊息...請重新
    產生」（見 `prompts.build_user_prompt()` 的 `error_feedback`
    引數）。四種觸發情況統一由 `_extract_delimited_body()`（delimiter
    抽取）與 `python_adapter.extract_body_statements()`（語法／空
    body／簽名重複輸出）覆蓋，這裡只驗證、不使用回傳的陳述式清單——
    真正的替換發生在 `PythonAdapter.splice_body()`，這裡的驗證只是為了
    決定要不要觸發重試，兩處共用同一份 `extract_body_statements()`
    邏輯，避免重複實作。

    回傳已驗證過的 `body_source` 原始文字（未縮排陳述式）；固定重試
    2 次仍失敗，拋出 `TranslatorCliModelOutputError`，交由呼叫端
    （`client.fill_function()`）轉成 `FillResult(success=False, ...)`。

    `run_id` 是必填參數：`fill_function()` 已經在最外層解析成具體值
    （見 11a 九章「run_id 的解析只在 fill_function() 做一次」），這裡不
    再自己判斷，避免同一個 task 的三次格式修正 attempt 拿到不同 run_id。
    `task_id`／`target_file`／`class_name` 是純記錄用的選填參數。

    **每個 attempt（不論成功、格式錯誤、還是傳輸層例外）恰好產生一筆
    trace row，寫入時機統一在該次 attempt 的 `finally` 區塊**（見 11a
    九章），不是只記最後一次。
    """
    error_feedback: str | None = None
    last_error: Exception | None = None

    # 整段格式修正重試迴圈都在號誌底下——實體機器單一 qwen 模型，即使是
    # 同一個 task 的第二、三次修正 attempt，也不能跟其他 task 的 qwen
    # 呼叫交錯執行（見上方 OLLAMA_MODEL_SEMAPHORE 常數說明）。
    async with OLLAMA_MODEL_SEMAPHORE:
        for attempt in range(OLLAMA_FORMAT_RETRY_COUNT + 1):
            user_prompt = build_user_prompt(
                current_signature=current_signature,
                java_source=java_source,
                referenced_source=referenced_source,
                context=context,
                context_files=context_files,
                error_feedback=error_feedback,
            )

            trace_id = str(uuid4())
            token = current_trace_id.set(trace_id)
            start = time.monotonic()
            status, error_msg, raw_text = "error", None, None
            prompt_text = f"=== SYSTEM ===\n{SYSTEM_PROMPT_QWEN}\n\n=== USER ===\n{user_prompt}"

            # 呼叫實際發出「之前」就寫入 status="running" 的 row（見
            # 11a_logging_architecture.md 七章「呼叫開始即寫入 running row」）：
            # 本地模型單次生成可能耗時數十秒到數分鐘，這段等待期間 prompt
            # 已經可以被 llmlog 查到，不必等下面 finally 補上最終結果——這是
            # 真實環境「長時間等待本地 Ollama、事後查不出當時送了什麼」這個
            # 缺口的直接對策。
            record_llm_call_start(
                trace_id=trace_id, run_id=run_id, vendor="ollama", model=OLLAMA_MODEL,
                caller="ollama_client.get_function_body",
                task_id=task_id, attempt=attempt,
                target_file=target_file, class_name=class_name, function_name=function_name,
                prompt=prompt_text,
            )

            try:
                raw_text = await _call_ollama_once(SYSTEM_PROMPT_QWEN, user_prompt)
                body_source = _extract_delimited_body(raw_text)
                python_adapter.extract_body_statements(body_source, function_name)
                status = "ok"
                return body_source
            except (TranslatorCliNetworkError, TranslatorCliConfigError) as exc:
                # 傳輸層／環境變數缺失：既有設計本來就不進格式重試迴圈，直接
                # 往外拋（見 07a 七章），這裡只是在拋出之前，先讓 finally
                # 保證這次 attempt 留痕。
                error_msg = str(exc)
                status = "timeout" if isinstance(exc.__cause__, httpx.TimeoutException) else "error"
                raise
            except TranslatorCliModelOutputError as exc:
                # delimiter／語法驗證失敗：進格式修正重試迴圈，raw_text 依然
                # 有值（模型確實回應了，只是內容不符契約），留在下面正常記錄。
                error_msg = str(exc)
                last_error = exc
                error_feedback = str(exc)
                if attempt < FORMAT_RETRY_COUNT:
                    logger.warning("模型輸出格式錯誤（第 %d 次），重新呼叫一次修正：%s", attempt + 1, exc)
                    continue
                # 未達上限時已經 continue；這裡是最後一次 attempt 也失敗，
                # 落到迴圈自然結束，finally 仍會先記錄這次 attempt，然後在
                # 迴圈外統一拋出（見下方）。
            except Exception as exc:
                # 涵蓋以上兩類以外的意外例外（如 extract_body_statements()
                # 內部真正非預期的錯誤），理由同 common/llm_client.py 的同類
                # 兜底分支——確保 error_msg 一定有內容，不留一筆查不出原因的
                # status='error' row。
                status = "error"
                error_msg = repr(exc)
                raise
            finally:
                latency_ms = int((time.monotonic() - start) * 1000)
                # current_trace_id.reset() 必須排在 record_llm_call() 之前，
                # 理由同 common/llm_client.py（見 11a 八章「為什麼 reset()
                # 要排在 record_llm_call() 之前」）。
                current_trace_id.reset(token)
                record_llm_call(
                    trace_id=trace_id, run_id=run_id, vendor="ollama", model=OLLAMA_MODEL,
                    caller="ollama_client.get_function_body",
                    task_id=task_id, attempt=attempt,
                    target_file=target_file, class_name=class_name, function_name=function_name,
                    prompt=prompt_text,
                    response=raw_text,
                    latency_ms=latency_ms, status=status, error_msg=error_msg,
                )

    raise TranslatorCliModelOutputError(f"模型輸出格式錯誤，修正重試仍失敗：{last_error}")
```

`_call_ollama_once()` 逾時後累積到 `UPSTREAM_DEGRADED_THRESHOLD` 次連續傳輸層失敗時改拋 `TranslatorCliUpstreamDegradedError`（`TranslatorCliNetworkError` 子類別，見 `docs/09b_bug_trace.md` #35）；`get_function_body()` 每個 attempt 的呼叫記錄機制見 `11a_logging_architecture.md`／`11b_logging_code.md`，不在這裡重複。`OLLAMA_MODEL_SEMAPHORE`／`FORMAT_RETRY_COUNT`（模組層級常數，可直接 import）被 `claude_client.py` 沿用同一個格式修正重試上限，見七章。

**已驗證**（`tests/translator_cli/test_ollama_client.py`，21 個測試，`monkeypatch` 假造 `httpx.AsyncClient`／`_call_ollama_once`／`record_llm_call`／`record_llm_call_start`，不需要真的連線）：涵蓋 delimiter 抽取成功／失敗、格式錯誤重試（含兩次都失敗、第二次重試才成功、三次都失敗才放棄）、網路層重試與耗盡後拋出 `TranslatorCliNetworkError`（不是 `TranslatorCliModelOutputError`，見一章「為什麼分開」）、`httpx.HTTPStatusError` 立即失敗不進重試迴圈、環境變數缺失轉成 `TranslatorCliConfigError`、同一次呼叫的多次重試只建立一個 `httpx.AsyncClient` 實例、連續跨呼叫傳輸層失敗達門檻時升級成 `TranslatorCliUpstreamDegradedError`、成功呼叫歸零連續失敗計數器（`docs/09b_bug_trace.md` #35）、`record_llm_call_start()` 在真正打 ollama 之前就被呼叫且帶完整 prompt、start／finish 共用同一個 `trace_id`；`get_function_body()` 用 `java_source`／`referenced_source` 引數呼叫，行為斷言不變；`check_ollama_reachable()` 預檢——收到任何 HTTP 回應即通過、傳輸層錯誤／逾時升級成 `TranslatorCliUpstreamDegradedError`、缺 `OLLAMA_BASE_URL` 轉成 `TranslatorCliConfigError`（見 `docs/refactor_bug_trace.md` #2）。`OLLAMA_MODEL_SEMAPHORE` 序列化行為本身沒有專屬測試，實體機器單一 qwen 模型的真實併發表現仍是待驗證事項，見十二章。

---

## 七、`claude_client.py`——Claude 連線與重試

對應 07a 七章「Claude 路徑：連線方式」。與 `ollama_client.py` 平行的第二條 `get_function_body()` 路徑，`client.py::fill_function()` 依 `translator_backend` 二選一呼叫（見九章 `_BACKEND_CLIENTS`）。

```python
# translator_cli/claude_client.py
"""Claude API 連線與請求契約，對應 07a 七章「Claude 路徑：連線方式」。
與 `ollama_client.py` 平行的第二條 `get_function_body()` 路徑，串接既有的
`common/llm_client.py::call_claude_for_json()`（[③]／舊版 [P] 已在用的
同一支共用 wrapper，含 `llm_trace.py` 呼叫紀錄），不新增另一套 Claude
連線邏輯、不重複實作 trace 記錄——`call_claude_for_json()` 內部已經做好
`record_llm_call_start()`／`record_llm_call()`，這裡不需要像
`ollama_client.py` 那樣自己再包一層。

**不受 qwen 那種單模型號誌限制**：Claude API 是雲端服務，多個 task 平行
呼叫模型不衝突，這是導入雙後端本來要換到的效能收益（見 07a 七章「Claude
路徑：連線方式」）。寫入磁碟／commit 那一小段仍然跨 backend 序列化，見
`git_ops.py::WRITE_LOCK`、`client.py::fill_function()`。
"""
from __future__ import annotations

import asyncio
import logging
import os

from common.llm_client import DEFAULT_MODEL_FALLBACK, call_claude_for_json
from translator_cli import python_adapter
from translator_cli.exceptions import (
    TranslatorCliModelOutputError,
    TranslatorCliNetworkError,
)
from translator_cli.ollama_client import FORMAT_RETRY_COUNT
from translator_cli.prompts import (
    BODY_STATEMENTS_SCHEMA,
    SYSTEM_PROMPT_CLAUDE,
    build_user_prompt,
)
from translator_cli.types import ReferencedSourceItem

logger = logging.getLogger(__name__)

# ③ design_agent 既有慣例（見 design_agent/llm.py）：每個呼叫端各自的
# 環境變數，不跟其他 Agent 共用同一個，方便個別調整／個別限流。
CLAUDE_MODEL = os.environ.get("TRANSLATOR_CLI_CLAUDE_MODEL", DEFAULT_MODEL_FALLBACK)


async def get_function_body(
    *,
    current_signature: str,
    java_source: str,
    referenced_source: list[ReferencedSourceItem],
    context: str,
    context_files: list[tuple[str, str]],
    function_name: str,
    task_id: str | None = None,
    target_file: str | None = None,
    class_name: str | None = None,
    run_id: str,
) -> str:
    """對外唯一入口，跟 `ollama_client.get_function_body()` 是同一份契約
    （見 07a 七章「分派方式」：`client.py::fill_function()` 依
    `translator_backend` 二選一呼叫，取得 `body_text` 之後兩條路徑走
    同一套六章驗證／替換流程）。

    固定重試 `FORMAT_RETRY_COUNT`（=2，跟 qwen 路徑共用同一個上限，從
    `ollama_client.py` import，見 07a 七章「兩條路徑共用：body_text
    格式修正重試」）次格式修正：`call_claude_for_json()` 的 Structured
    Outputs 已經保證回應是合法 JSON、帶 `body_statements` 欄位，不會有
    qwen 路徑那種「找不到 delimiter」的失敗模式，這裡只需要驗證
    `body_statements` 的內容本身（`ast.parse()`／空 body／簽名重複輸出，
    見 07a 六章步驟 6a／6b，跟 qwen 共用同一份
    `python_adapter.extract_body_statements()`）。

    **API／網路層錯誤不重試**：`call_claude_for_json()` 本身沒有內建
    重試（雲端服務，不像 qwen 那樣需要應付 LAN 內部連線抖動），失敗
    （`LlmJsonError`）直接轉成 `TranslatorCliNetworkError` 往外拋，不進
    這裡的格式修正迴圈——理由同 `ollama_client.get_function_body()`
    既有原則：網路層錯誤救不回來，格式修正重試解決不了這個問題。這是
    07a 十四章「待決定事項」列出的已知簡化，尚未有真實 Claude API 資料
    驗證是否需要另外加一層重試。

    `call_claude_for_json()` 是同步函式（見 `common/llm_client.py`），
    用 `asyncio.to_thread()` 丟到執行緒池，不佔住 event loop——這也是
    Claude 路徑天然不受單一號誌限制的原因：多個 task 的
    `asyncio.to_thread()` 呼叫可以真正併發跑在不同執行緒。
    """
    error_feedback: str | None = None
    last_error: Exception | None = None

    for attempt in range(FORMAT_RETRY_COUNT + 1):
        user_prompt = build_user_prompt(
            current_signature=current_signature,
            java_source=java_source,
            referenced_source=referenced_source,
            context=context,
            context_files=context_files,
            error_feedback=error_feedback,
        )

        try:
            response = await asyncio.to_thread(
                call_claude_for_json,
                system_prompt=SYSTEM_PROMPT_CLAUDE,
                user_prompt=user_prompt,
                schema=BODY_STATEMENTS_SCHEMA,
                model=CLAUDE_MODEL,
                run_id=run_id,
                task_id=task_id,
                target_file=target_file,
                class_name=class_name,
                function_name=function_name,
            )
        except Exception as exc:
            # call_claude_for_json() 對已知的 anthropic.APIError／逾時／
            # JSON 解析失敗都轉成 LlmJsonError，但它自己的兜底分支（見
            # common/llm_client.py 該函式最後一個 except）對真正意外的
            # SDK 內部錯誤是原樣往外拋、不轉型別——這裡用廣義 Exception
            # 接住，統一轉成 TranslatorCliNetworkError，不讓任何原生例外
            # 洩漏擊穿 translator-cli 的錯誤回報契約（見 07a 十三章）。
            raise TranslatorCliNetworkError(f"Claude API 呼叫失敗：{exc}") from exc

        body_source = response["body_statements"]
        try:
            python_adapter.extract_body_statements(body_source, function_name)
            return body_source
        except TranslatorCliModelOutputError as exc:
            last_error = exc
            error_feedback = str(exc)
            if attempt < FORMAT_RETRY_COUNT:
                logger.warning("模型輸出格式錯誤（第 %d 次），重新呼叫一次修正：%s", attempt + 1, exc)
                continue

    raise TranslatorCliModelOutputError(f"模型輸出格式錯誤，修正重試仍失敗：{last_error}")
```

`CLAUDE_MODEL` 比照 `design_agent/llm.py` 既有慣例，各呼叫端自己的環境變數（`TRANSLATOR_CLI_CLAUDE_MODEL`），沒設定時退回 `common/llm_client.py::DEFAULT_MODEL_FALLBACK`。`get_function_body()` 不自己處理 trace 記錄（不像 `ollama_client.py` 自己包一層 `record_llm_call_start()`／`record_llm_call()`）——`call_claude_for_json()` 內部已經做好，這裡重複實作只會造成兩筆重複的 trace row。

**已驗證**（`tests/translator_cli/test_claude_client.py`，4 個測試，`monkeypatch` 假造 `call_claude_for_json()`，不需要真的呼叫 Claude API）：成功（第一次 attempt）、格式修正重試後成功、重試耗盡後拋出 `TranslatorCliModelOutputError`、`call_claude_for_json()` 呼叫失敗（模擬 `LlmJsonError`／其他例外）直接轉成 `TranslatorCliNetworkError` 且不進格式修正迴圈（呼叫次數斷言為 1）。**尚未驗證**：真實 Claude API 呼叫（含真實 Structured Outputs 行為、真實延遲）、多個 task 平行呼叫 Claude 的真實併發表現，見十二章。

---

## 八、`scaffold.py`——骨架生成模式

對應 07a 四章全節。

> 09b 端對端驗證發現並修正一個既有缺陷：`exam-platform-api` 的
> `app/services/common_service.py` 裡 `ResponseResult.error_4(self, code:
> ErrorCode)` 這個方法簽名引用了同一個檔案裡稍後才定義的另一個 class
> `ErrorCode`，`_resolve_imports()`／`resolve_body_imports()` 原本都沒有
> 排除「正在組裝／掃描的這個檔案自己的 class／函式」，於是各自產生一行
> 恆為 True 的自我 import（`from X import X`），讓該模組 import 階段直接
> `ImportError`。骨架階段（`_resolve_imports()`）新增 `same_file_class_names`
> 參數；填空階段（`resolve_body_imports()`）新增 `same_file_top_level`
> 排除集合（額外涵蓋函式與頂層變數賦值）——同一個問題、兩個不同時機點
> 的獨立實作，各自要排除。見 `09b_bug_trace.md`、`09b_implement_agent_code.md`
> 十章「已知限制」。

```python
# translator_cli/scaffold.py
"""④ 骨架生成模式，對應 07a 四章全節。同步、確定性，不呼叫本地模型
（見四章「決策：不呼叫本地模型，純機械產生」）——`build_files()` 全程
在記憶體中組裝並驗證，`write_files()` 才真正落地寫入磁碟，兩者分開
是為了讓 `client.generate_scaffold()` 能在「全部驗證通過後才一次性
寫入」（四章「寫入時機」）。
"""
from __future__ import annotations

import ast
import builtins
import logging
import re
from pathlib import Path

from common.jpa_base_repository import BASE_REPOSITORY_CLASS, BASE_REPOSITORY_FILE
from translator_cli.exceptions import TranslatorCliAssemblyError
from translator_cli.python_adapter import PythonAdapter
from translator_cli.types import InterfaceSpec, PythonStructure

logger = logging.getLogger(__name__)

# `common/jpa_base_repository.py::BASE_REPOSITORY_FILE` 的 import 路徑形式，
# 對應 docs/refactor_bug_trace.md #10／#16：repositories 層的類別若繼承
# Spring Data 基底介面（`InterfaceSpec.jpa_base_entity` 非 None），渲染成
# `class X(BaseRepository[Entity]):` 時固定需要這行 import，跟 routers 層
# 固定需要 `from fastapi import APIRouter` 是同一種「已知固定樣板」處理。
_BASE_REPOSITORY_MODULE = BASE_REPOSITORY_FILE.removesuffix(".py").replace("/", ".")
_BASE_REPOSITORY_IMPORT = f"from {_BASE_REPOSITORY_MODULE} import {BASE_REPOSITORY_CLASS}"

# 05a 三章「全域基礎設施檔案」：內容就是完整檔案內容，直接寫入，不需要
# 像 Schema 定義段那樣合併（見四章「輸入解析：directory_tree 三段格式」
# 「基礎設施段」）。
_INFRA_FILES = {"app/main.py", "app/core/database.py"}

# 05a 三章「檔名規則」的層級判定，`file_path` 開頭字串即可判斷——跟
# `plan_agent/module_index.py` 的 `_LAYER_SUFFIX` 是同一種各自维护機械
# 慣例的既有先例（見該檔案 docstring），這裡不 import design_agent。
# `app/utils/`：08a 二章「Utils 檔案結構——已定案」，一個 Java class 對
# 一個 Python 檔案、不歸屬任何 module，渲染規則跟 routers／global_advice
# 同一種「class_name=None 自由函式」形狀（見下方 _FREE_FUNCTION_LAYERS）。
_LAYER_PREFIX = {
    "app/routers/": "routers",
    "app/services/": "services",
    "app/repositories/": "repositories",
    "app/utils/": "utils",
}

# `_global` 保留模組固定輸出的全域例外處理檔案（見 design_agent/design.py
# `_design_global_advice_module()`、docs/09b_bug_trace.md #11/#12）——
# 單一固定檔案，不是像三層那樣的目錄前綴，因此用精確比對、獨立於
# `_LAYER_PREFIX` 之外判斷。渲染規則比照 routers 層（`class_name=None`
# 自由函式），但**不**包 `APIRouter` 樣板（這不是真正的路由檔案，見
# `_assemble_file_text()`）——這是真實端對端測試才發現的缺口：
# `_render_interface_files()` 原本假設 interfaces 的 file_path 只會落在
# 三層目錄底下，全域例外處理的檔案會被誤判成 unknown layer 整個跳過。
_GLOBAL_ADVICE_FILE = "app/core/exception_handlers.py"
_GLOBAL_ADVICE_LAYER = "global_advice"

# `class_name=None` 自由函式、不包 APIRouter 樣板的層級集合：routers 層
# 本身（一般路由函式）、global_advice（上方）、utils（同上方 _LAYER_PREFIX
# 註解）——三者渲染形狀相同，統一判斷、避免三處各自重複寫同一份 tuple。
_FREE_FUNCTION_LAYERS = frozenset({"routers", _GLOBAL_ADVICE_LAYER, "utils"})

# 四章「已知關鍵字表」，子字串比對，一個檔案內出現多次只加一次。
_KNOWN_KEYWORD_IMPORTS: list[tuple[str, str]] = [
    ("Session", "from sqlalchemy.orm import Session"),
    ("Depends(", "from fastapi import Depends"),
    ("get_db", "from app.core.database import get_db"),
    ("Request", "from fastapi import Request"),
    # ResponseEntity<T> 型別對應（見 common/java_type_mapping.py
    # map_java_type()）與全域例外處理（見 design_agent/design.py
    # _design_global_advice_module()）都用這個做為 return_type，
    # 對應 docs/09b_bug_trace.md #11/#12/#30。
    ("Response", "from fastapi import Response"),
    ("UploadFile", "from fastapi import UploadFile"),
    ("HTTPException", "from fastapi import HTTPException"),
    ("Decimal", "from decimal import Decimal"),
    # 08a_scaffold_agent_architecture.md 五章新增：common/java_type_mapping.py
    # 的 map_java_type() 補上 java.time／java.util.UUID 對應後，③的方法簽名
    # （非 API 邊界的內部方法，走 map_java_type() 這條路徑）也可能產生這幾個
    # 型別字串，原表沒涵蓋會重演 07a 五章「填空模式：本體 import 解析」修正
    # 前的缺 import 情況。"date"／"datetime"／"time" 三個關鍵字都以英數字元
    # 結尾，走 _keyword_hits() 的 \b 單字邊界比對——"datetime" 內含 "date"／
    # "time" 兩個子字串，但邊界比對不會誤觸發（"date" 與後面的 "time" 之間
    # 沒有字元邊界），三者互不誤判。
    ("date", "from datetime import date"),
    ("datetime", "from datetime import datetime"),
    ("time", "from datetime import time"),
    ("UUID", "from uuid import UUID"),
    ("Callable[", "from typing import Callable"),
]

# 05a 三章「格式慣例」固定格式：`### {file_path}` 標題 + python fenced
# code block，DOTALL 讓 `.` 能跨行比對整個 code block 內容。
_CODE_BLOCK_RE = re.compile(r"### (?P<path>\S+)\n```python\n(?P<code>.*?)```", re.DOTALL)


def _normalize_type(raw_type: str) -> str:
    """對應四章「型別字串正規化」。語法合法性已由
    `design_agent/type_mapping.map_java_type()` 保證（見 05a 五章），
    這裡只是 defense-in-depth：對 ③ 輸出而言恆為 no-op，只防禦其他
    呼叫路徑萬一繞過 ③、或未來 `InterfaceSpec` 出現殘餘寫法。
    """
    return raw_type.replace("<", "[").replace(">", "]")


def _extract_code_blocks(directory_tree: str) -> list[tuple[str, str]]:
    """對應四章「輸入解析：directory_tree 三段格式」第二、三段：逐段
    擷取 `(file_path, code_block_text)`。第一段（目錄結構段）純文字
    樹狀圖不在這裡解析——本實作把「mkdir -p」下放到 `write_files()`
    對每個實際要寫入的檔案各自 `parent.mkdir(parents=True,
    exist_ok=True)`，跟先掃描第一段、預先建好所有子目錄的效果等價，
    因此不需要額外解析第一段。
    """
    return [(m.group("path"), m.group("code")) for m in _CODE_BLOCK_RE.finditer(directory_tree)]


def _split_block_imports(
    block: str,
) -> tuple[bool, list[tuple[str, str | None]], dict[str, list[str]], str]:
    """用 AST 定位一個 block 裡的 import 陳述式所在行號範圍，而不是逐行
    正則比對——正則版本漏抓 `import xxx` 裸匯入、無法處理 PEP 8 多行
    `from typing import (\\n    List,\\n)` 括號跨行寫法。用 AST 找出
    `ast.Import`／`ast.ImportFrom` 節點的 `lineno`／`end_lineno`，兩種
    形式與跨行寫法都能正確定位。

    **body 保留原始逐行文字，刻意不整段丟進 `ast.unparse()` 重新序列化**：
    `render_class_placeholder_section()`（05a 三章）產出的機械註解
    （Java 原始檔路徑、建構子簽名）是這個佔位機制存在的核心資訊，
    `ast.unparse()` 不保留註解，會把這些資訊整個丟掉——因此這裡只用
    AST 定位 import 陳述式的行號範圍，從原始文字裡精準挖掉這幾行，
    其餘文字（含註解、原始排版）逐字保留，不重新格式化。

    回傳 `(是否含 from __future__ import annotations,
    [(module, alias 或 None), ...] 裸 import 清單,
    {module: [name（含 "X as Y" 別名寫法）, ...]} from-import 清單,
    去掉 import 陳述式後的原始 body 文字)`。
    """
    tree = ast.parse(block)
    lines = block.splitlines()
    future_seen = False
    plain_imports: list[tuple[str, str | None]] = []
    from_imports: dict[str, list[str]] = {}
    excluded_lines: set[int] = set()

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            future_seen = True
            excluded_lines.update(range(node.lineno - 1, node.end_lineno))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = from_imports.setdefault(module, [])
            for alias in node.names:
                name = alias.name if not alias.asname else f"{alias.name} as {alias.asname}"
                if name not in names:
                    names.append(name)
            excluded_lines.update(range(node.lineno - 1, node.end_lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                entry = (alias.name, alias.asname)
                if entry not in plain_imports:
                    plain_imports.append(entry)
            excluded_lines.update(range(node.lineno - 1, node.end_lineno))

    body_lines = [line for i, line in enumerate(lines) if i not in excluded_lines]
    return future_seen, plain_imports, from_imports, "\n".join(body_lines)


def _merge_schema_blocks(blocks: list[str]) -> str:
    """對應四章「Schema 定義段」合併演算法：同一個 `file_path` 可能有
    多個獨立 code block（API 邊界 schema、建構子占位、資料容器占位，
    見 05a 三章），依序合併成一個檔案：

    1. 逐 block 用 `_split_block_imports()` 解析出 import 陳述式與其餘
       內容
    2. 所有 block 的 import 陳述式去重、合併（保留各自 module 內第一次
       出現的名稱順序）
    3. `from __future__ import annotations` 只保留一次、放最前面
    4. import 段落之後，依原本各 block 的順序依序接上各自的內容
    """
    future_seen = False
    plain_import_order: list[tuple[str, str | None]] = []
    from_import_order: list[str] = []
    from_import_names: dict[str, list[str]] = {}
    bodies: list[str] = []

    for block in blocks:
        block_future, block_plain, block_from, body = _split_block_imports(block)
        future_seen = future_seen or block_future
        for entry in block_plain:
            if entry not in plain_import_order:
                plain_import_order.append(entry)
        for module, names in block_from.items():
            existing = from_import_names.setdefault(module, [])
            if module not in from_import_order:
                from_import_order.append(module)
            for name in names:
                if name not in existing:
                    existing.append(name)
        bodies.append(body.strip("\n"))

    header = ["from __future__ import annotations"] if future_seen else []
    header.extend(
        f"import {module}" if alias is None else f"import {module} as {alias}"
        for module, alias in plain_import_order
    )
    header.extend(f"from {module} import {', '.join(from_import_names[module])}" for module in from_import_order)

    parts = (["\n".join(header)] if header else []) + [b for b in bodies if b]
    return "\n\n\n".join(parts) + "\n"


def _table_assignment_names(tree: ast.Module) -> list[str]:
    """找出模組層級「`name = Table(...)`」這種 SQLAlchemy Core `Table`
    變數賦值的名稱——對應 08a_scaffold_agent_architecture.md 八章
    「`@ManyToMany`：產生中介表定義」：`@ManyToMany` 的中介表沒有對應
    的 Java entity class，`scaffold_agent` 渲染成純 `Table(...)` 賦值
    （`ast.Assign`），不是 `ast.ClassDef`——若自訂型別索引只掃
    `ClassDef`（見下方 `_build_custom_type_index()`／
    `_scan_project_custom_types()`），這種名稱永遠進不了索引，⑤填空時
    只要在函式本體引用這個中介表（如 `insert(user_roles).values(...)`，
    見 08a 八章「⑤填空時若需要對這張表直接操作」的既有動機），兩層
    import 解析都比對不到，會被四章「兩層都比對不到時保守不加、不猜」
    這條既有原則吞掉，產出缺 import 的程式碼。

    只認「單一目標、RHS 是呼叫 `Table(...)` 或 `xxx.Table(...)`」這個
    精確形狀，不是任意模組層級賦值——避免誤把一般常數賦值（如
    `DEFAULT_TIMEOUT = 30`）也當成自訂型別收進索引。`func` 可能是
    `ast.Name("Table")`（`from sqlalchemy import Table` 之後直接呼叫）
    或 `ast.Attribute(attr="Table")`（`sqlalchemy.Table(...)` 這種完整
    路徑呼叫），08a 八章的渲染範例走前者，這裡兩種都認，不假設呼叫端
    一定用哪一種匯入風格。
    """
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if func_name == "Table":
            names.append(node.targets[0].id)
    return names


def _build_custom_type_index(
    interfaces: list[InterfaceSpec], schema_trees: dict[str, ast.Module], db_models_valid: dict[str, ast.Module]
) -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    """對應四章「import 解析：兩層機制」自訂型別索引，三個來源合併：

    - 來源一：Schema 定義段合併後的每個檔案，AST 掃出頂層 `ClassDef`
    - 來源二：`interfaces` 的 `class_name → file_path`（排除 `None`）
    - 來源三：`db_models` 合併後每個檔案，AST 掃出頂層 `ClassDef`，
      **以及** `_table_assignment_names()` 找到的 `Table(...)` 賦值
      （08a 八章 `@ManyToMany` 中介表，見該函式 docstring）——這兩種
      頂層宣告形狀只會出現在 `db_models`，來源一／二不需要一併掃

    優先序三／二＞一，用寫入順序達成（後寫入覆蓋先寫入）——三個來源
    對應的 class 集合理論上不重疊，這裡只是定義明確的 tie-break 規則。

    `schema_trees`／`db_models_valid` 都是呼叫端在 `build_files()` 驗證
    階段已經 `ast.parse()` 過的結果，這裡直接複用同一次解析，不重新
    parse 一次相同的原始碼字串——比照 07a 四章對 `db_models` 這個來源
    「不是新增的驗證負擔，直接複用同一次解析結果」的既有原則，兩個
    來源一致處理。

    第二個回傳值 `schema_classes_by_module`（對應
    `docs/refactor_bug_trace.md` #8 真實案例）：`ResponseResult<T>` 這類
    專案自訂泛型的具名包裝類別（如 `ResponseResultString`）常常被多個
    模組各自獨立定義一份——③ 逐模組產生 schema，模組之間互不知道彼此
    定義了同名的東西。上面 `index` 是全域、只認 class 名稱的扁平結構，
    同名 class 在多個 `schema_trees` 檔案裡都出現時，只會留住迭代到
    最後的那一份（字典覆寫），導致其他模組明明自己也定義了同名 class，
    `_resolve_imports()` 查到的卻是別的模組的檔案路徑。這裡額外記錄
    「每個模組（`app/schemas/{module}.py` 的 `module` 段）自己的 schema
    檔案定義了哪些 class 名稱」，供 `_resolve_imports()` 優先查「這個
    檔案自己所屬的模組」，查得到就用同一個模組的 schema 檔案，不必依賴
    可能被覆寫過的全域 `index`。
    """
    index: dict[str, str] = {}
    schema_classes_by_module: dict[str, set[str]] = {}
    for file_path, tree in schema_trees.items():
        module = (
            file_path.removeprefix("app/schemas/").removesuffix(".py")
            if file_path.startswith("app/schemas/") and file_path.endswith(".py")
            else None
        )
        names = schema_classes_by_module.setdefault(module, set()) if module is not None else None
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                index[node.name] = file_path
                if names is not None:
                    names.add(node.name)
    for iface in interfaces:
        if iface["class_name"]:
            index[iface["class_name"]] = iface["file_path"]
    for file_path, tree in db_models_valid.items():
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                index[node.name] = file_path
        for name in _table_assignment_names(tree):
            index[name] = file_path
    return index, {module: frozenset(names) for module, names in schema_classes_by_module.items()}


def _module_for_file_path(file_path: str) -> str | None:
    """從 routers／services／repositories 層的 `file_path` 反推 module
    名稱，對應 `design_agent/layout.py::file_path_for_layer()`「檔名規則：
    `{module}_{layer_singular}.py`」的反向操作——`translator_cli` 各自
    獨立維護同一套機械慣例、不 import `design_agent`（見本檔案既有
    `_LAYER_PREFIX` 旁的說明）。`utils`（橫跨多個 module，見
    `file_path_for_utils()`）與 `_global` 保留模組的固定檔案都沒有
    module 概念，回傳 `None`——呼叫端遇到 `None` 時退回既有的全域索引
    行為，不特別處理。
    """
    for prefix, suffix in (
        ("app/routers/", "_router.py"),
        ("app/services/", "_service.py"),
        ("app/repositories/", "_repository.py"),
    ):
        if file_path.startswith(prefix) and file_path.endswith(suffix):
            return file_path.removeprefix(prefix).removesuffix(suffix)
    return None


def _keyword_hits(keyword: str, combined: str) -> bool:
    """`_KNOWN_KEYWORD_IMPORTS` 命中判斷。以英數字元結尾的關鍵字
    （`Session`／`get_db`／`Request`／`UploadFile`／`HTTPException`／
    `Decimal`——`_KNOWN_KEYWORD_IMPORTS` 裡除了 `Depends(`／`Callable[`
    以外的全部）用 `\\b` 單字邊界比對，避免命中自訂型別名稱裡剛好包含
    這個關鍵字當子字串的情況（如 Java DTO 常見命名 `LoginRequest`／
    `UserSession` 這種以 Request／Session 結尾的類別）——這種名稱在
    `combined` 裡只會是 `custom_type_index` 已知的裸名稱（見本函式
    docstring），單字邊界比對不會誤傷。以標點結尾的關鍵字（`Depends(`／
    `Callable[`）維持原本的純子字串比對：Python 識別字不能包含
    `(`／`[`，這兩個字元本身就已經是足夠精確的錨點（比照 07a 四章
    「命中比對用 `Callable[`（含左中括號）而不是單獨的 `Callable`」的
    既有理由），改用 `\\b` 反而會誤判 `Callable[[int, str], bool]`
    （多引數 functional interface 常見的雙層中括號）這種合法情況。
    """
    if keyword[-1].isalnum():
        return re.search(rf"\b{re.escape(keyword)}\b", combined) is not None
    return keyword in combined


def _resolve_imports(
    type_strings: list[str],
    custom_type_index: dict[str, str],
    same_file_class_names: frozenset[str] = frozenset(),
    schema_classes_by_module: dict[str, frozenset[str]] | None = None,
    own_module: str | None = None,
    is_repository_layer: bool = False,
) -> list[str]:
    """對應四章「import 解析：兩層機制」：已知關鍵字表（子字串比對，見
    `_keyword_hits()`）＋自訂型別索引（AST `ast.Name` 節點比對，不用
    字串裁切／正則，理由見四章「用 AST 而非字串裁切是必要的」）。兩層
    都比對不到時保守不加、不猜（見四章「兩層都比對不到時保守不加、
    不猜」）。

    型別字串保證是裸名稱（`design_agent.map_java_type()` 對自訂類別
    只產出裸名稱，見 05a 五章型別對應表），不會出現 `models.User` 這種
    帶命名空間前綴的寫法，因此不需要對 `ast.Attribute` 額外排除。

    `same_file_class_names` 是正在組裝的這個檔案自己（`_render_interface_
    files()` 這一輪 `file_path` 分組下）的全部 class 名稱——這批類別即使
    命中 `custom_type_index`（該索引涵蓋全專案，本來就會收錄這個檔案
    自己定義的 class），也不需要 import，因為它們就在同一個檔案裡。
    這批 Java 靜態工具類常見「方法簽名引用同檔案另一個 class」（如
    `ResponseResult.error_4(self, code: ErrorCode)`，`ErrorCode` 是同一個
    檔案稍後才定義的另一個 class）——不排除的話會產生一行恆為 True 的
    循環 import，讓這個模組 import 階段直接 ImportError（已用真實案例
    重現：exam-platform-api 的 app/services/common_service.py，見
    09b_implement_agent_code.md 十章「已知限制」）。填空階段的對應
    排除見 `resolve_body_imports()`——這裡是骨架生成階段的簽名層級
    版本，同一個問題、兩個不同時機點的兩份獨立實作，各自要排除。

    `schema_classes_by_module`／`own_module`（對應 `docs/refactor_bug_
    trace.md` #8 真實案例）：同名的具名回應包裝 class（如
    `ResponseResultString`）常常被多個模組各自獨立定義一份，`custom_
    type_index` 這個全域扁平索引在這種情況下只留得住其中一個模組的
    檔案路徑（見 `_build_custom_type_index()` docstring）。這裡查到
    `own_module`（這個檔案自己所屬的模組，`None` 代表 utils／`_global`
    這種沒有 module 概念的檔案）自己的 schema 檔案剛好也定義了同名
    class 時，**優先**直接用同一個模組的 schema 檔案，不查可能已經被
    覆寫過的全域索引——這樣同一個模組的檔案永遠 import 自己的 schema
    定義，不會意外指向別的模組。查不到（`own_module` 是 `None`，或
    這個模組自己沒有定義這個 class 名稱）才退回既有的全域索引行為。

    `is_repository_layer`（對應 `docs/refactor_bug_trace.md` #17 真實
    案例）：repositories 層需要的是 SQLAlchemy ORM model（`app/models/
    {module}.py`），但 `ExamEntity`／`AnswerEntity`／`ExamkindEntity` 這類
    entity 名稱常常剛好也被同一個模組的 schema 檔案（Pydantic DTO）定義
    一份——上面這條 `own_module_schema_classes` 優先序原本是為了 #8
    （`ResponseResultString` 這類跨模組同名回應包裝類別）設計，沒有分
    「這個檔案是哪一層」，repositories 層的檔案因此被這條規則誤導，優先
    選到同名的 schema 定義，把 Pydantic `BaseModel` 子類別傳給 SQLAlchemy
    查詢，在真正執行查詢時才炸 `sqlalchemy.exc.ArgumentError`（本體程式碼
    本身完全正確，純粹被錯誤的 import 拖累）。`is_repository_layer=True`
    時完全跳過這條「優先查同模組 schema」的規則，直接退回下面的全域
    `custom_type_index`——`_build_custom_type_index()` 組裝索引時
    `db_models_valid`（來源三）是最後寫入、覆寫掉同名的 schema 條目（來源
    一），全域索引本來就已經是「同名時 model 贏」，不需要另外查一次
    `app/models/`。
    """
    combined = "\n".join(type_strings)
    lines = [import_line for keyword, import_line in _KNOWN_KEYWORD_IMPORTS if _keyword_hits(keyword, combined)]

    own_module_schema_classes = (
        frozenset()
        if is_repository_layer
        else (schema_classes_by_module or {}).get(own_module, frozenset()) if own_module else frozenset()
    )

    custom_imports: dict[str, list[str]] = {}
    for type_str in type_strings:
        try:
            expr = ast.parse(type_str, mode="eval")
        except SyntaxError:
            continue  # 型別字串已在渲染前個別驗證過，這裡理論上不會發生
        for node in ast.walk(expr):
            if not (isinstance(node, ast.Name) and node.id not in same_file_class_names):
                continue
            if node.id in own_module_schema_classes:
                module_path = f"app.schemas.{own_module}"
            elif node.id in custom_type_index:
                module_path = custom_type_index[node.id].removesuffix(".py").replace("/", ".")
            else:
                continue
            names = custom_imports.setdefault(module_path, [])
            if node.id not in names:
                names.append(node.id)

    lines.extend(f"from {module_path} import {', '.join(sorted(names))}" for module_path, names in sorted(custom_imports.items()))
    return lines


def _render_function_snippet(iface: InterfaceSpec, *, is_class_method: bool) -> str:
    """對應四章「interfaces：函式簽名渲染」。routers 層 API 邊界方法
    （`http_method` 非 `None`）加 `@router.{method}("{route}")` 裝飾器；
    routers 層非邊界方法（私有 helper，`http_method` 為 `None`）不加
    裝飾器；services／repositories 層一律不加裝飾器、加 `self` 前綴。
    """
    params = ", ".join(f"{p['name']}: {_normalize_type(p['type'])}" for p in iface["params"])
    if is_class_method:
        params = f"self, {params}" if params else "self"
    return_type = _normalize_type(iface["return_type"])

    lines = []
    http_method = iface.get("http_method")
    if http_method:
        lines.append(f'@router.{http_method.lower()}("{iface["route_path"]}")')
    lines.append(f"def {iface['function_name']}({params}) -> {return_type}:")
    lines.append("    pass")
    return "\n".join(lines) + "\n"


def _assemble_file_text(
    layer: str,
    import_lines: list[str],
    valid: list[tuple[InterfaceSpec, str]],
    all_class_names: list[str],
    class_bases: dict[str, str] | None = None,
) -> str:
    """把單一檔案已驗證通過的函式片段組成完整檔案原始文字（尚未經過
    `PythonAdapter.parse → render` 的最終格式化，見
    `_render_interface_files()`）。

    `all_class_names`（services／repositories 層專用）是這個檔案裡
    **全部** class 名稱，不是只有 `valid` 裡出現過的——四章「語法驗證
    與寫入」步驟 3：同一個 `class_name` 分組若所有方法都被跳過（極端
    情況），該 `class` 仍要渲染出來，body 用 `pass` 佔位，維持檔案
    語法合法。

    `class_bases`（repositories 層專用，對應 docs/refactor_bug_trace.md
    #10／#16）：`class_name → entity 型別簡單名稱`，來自這個類別任一
    `InterfaceSpec.jpa_base_entity`（見 `_render_interface_files()` 組裝
    端）。有值代表這個 interface 繼承了 Spring Data 基底介面，渲染成
    `class X(BaseRepository[Entity]):` 並注入 `model = Entity` 當第一行
    body——讓 `find_all`/`find_by_id` 等內建方法透過繼承「真的」存在，
    不需要像 `_synthesize_inherited_repository_reads()` 舊版那樣逐一
    合成方法（那個函式仍保留，只是現在合成出的 InterfaceSpec 一樣帶
    `jpa_base_entity`，走同一條繼承渲染路徑，不再是唯一手段）。
    """
    if layer == "routers":
        header = ["from __future__ import annotations", "", "from fastapi import APIRouter", *import_lines, "", "router = APIRouter()", ""]
        body = "\n".join(snippet for _, snippet in valid)
        return "\n".join(header) + "\n" + body

    if layer in (_GLOBAL_ADVICE_LAYER, "utils"):
        # class_name=None 自由函式（跟 routers 層同一種形狀），但不是
        # 真正的路由檔案，不需要 APIRouter 樣板。utils 同一套理由：
        # Java 靜態工具方法對應 Python module-level 函式，不是路由。
        header = ["from __future__ import annotations", "", *import_lines, ""]
        body = "\n".join(snippet for _, snippet in valid)
        return "\n".join(header) + "\n" + body

    by_class: dict[str, list[str]] = {}
    for iface, snippet in valid:
        by_class.setdefault(iface["class_name"], []).append(snippet)

    class_bases = class_bases or {}
    header_import_lines = [*import_lines, _BASE_REPOSITORY_IMPORT] if class_bases else import_lines
    lines = ["from __future__ import annotations", "", *header_import_lines, ""]
    for class_name in all_class_names:
        entity = class_bases.get(class_name)
        if entity is not None:
            lines.append(f"class {class_name}({BASE_REPOSITORY_CLASS}[{entity}]):")
            lines.append(f"    model = {entity}")
        else:
            lines.append(f"class {class_name}:")
        snippets = by_class.get(class_name, [])
        if not snippets and entity is None:
            lines.append("    pass")
        for snippet in snippets:
            lines.extend(f"    {line}" if line else "" for line in snippet.splitlines())
        lines.append("")
    return "\n".join(lines)


def _render_interface_files(
    interfaces: list[InterfaceSpec],
    custom_type_index: dict[str, str],
    schema_classes_by_module: dict[str, frozenset[str]],
) -> tuple[dict[str, str], list[dict]]:
    """對應四章「interfaces：函式簽名渲染」＋「語法驗證與寫入」：逐
    `InterfaceSpec` 隔離失敗（步驟 1／2），最後用 `PythonAdapter` 的
    `parse → render` 統一格式化，確保骨架與填空兩階段格式化風格一致。

    `schema_classes_by_module`：對應 `docs/refactor_bug_trace.md` #8，
    逐檔案用 `_module_for_file_path()` 反推這個檔案自己所屬的模組，
    傳給 `_resolve_imports()` 優先查同一個模組自己的 schema 定義，避免
    同名的具名回應包裝 class（如 `ResponseResultString`）被誤導到別的
    模組（見該函式 docstring）。
    """
    adapter = PythonAdapter()
    by_file: dict[str, list[InterfaceSpec]] = {}
    for iface in interfaces:
        by_file.setdefault(iface["file_path"], []).append(iface)

    files: dict[str, str] = {}
    skipped: list[dict] = []

    for file_path, ifaces in by_file.items():
        if file_path == _GLOBAL_ADVICE_FILE:
            layer = _GLOBAL_ADVICE_LAYER
        else:
            layer = next((v for prefix, v in _LAYER_PREFIX.items() if file_path.startswith(prefix)), None)
        if layer is None:
            # 按照 05a／07a 契約，interfaces 的 file_path 只會落在
            # routers／services／repositories／utils（或上面明確處理過的
            # _GLOBAL_ADVICE_FILE），理論上不會再觸發這裡——但比照本函式
            # 其餘失敗路徑（語法錯誤、組裝驗證失敗）一律顯式記錄成
            # skipped_interfaces，不靜默丟棄，避免上游一旦出現非預期
            # file_path，這批 InterfaceSpec 人不知鬼不覺地從骨架裡消失、
            # 不會被人工發現。
            for iface in ifaces:
                skipped.append(
                    {
                        "file_path": file_path,
                        "class_name": iface["class_name"],
                        "function_name": iface["function_name"],
                        "error": f"{file_path} 不在 routers／services／repositories／utils 目錄底下（unknown layer）",
                    }
                )
            continue

        valid: list[tuple[InterfaceSpec, str]] = []
        for iface in ifaces:
            snippet = _render_function_snippet(iface, is_class_method=(layer not in _FREE_FUNCTION_LAYERS))
            try:
                ast.parse(snippet)
            except SyntaxError as exc:
                skipped.append(
                    {
                        "file_path": file_path,
                        "class_name": iface["class_name"],
                        "function_name": iface["function_name"],
                        "error": str(exc),
                    }
                )
                continue
            valid.append((iface, snippet))

        all_class_names: list[str] = []
        if layer not in _FREE_FUNCTION_LAYERS:
            for iface in ifaces:
                if iface["class_name"] not in all_class_names:
                    all_class_names.append(iface["class_name"])

        # 對應 docs/refactor_bug_trace.md #10／#16：repositories 層才會有
        # `jpa_base_entity`（見 design_agent/design.py 組裝端），逐 iface
        # 找第一個非 None 的值即可——同一個 class 的全部 InterfaceSpec
        # 理論上要嘛全部帶同一個 entity（都繼承自同一個 Java interface），
        # 要嘛全部是 None，不會同一個 class 出現互相矛盾的兩種 entity。
        class_bases: dict[str, str] = {}
        for iface in ifaces:
            entity = iface.get("jpa_base_entity")
            if entity is not None and iface["class_name"] not in class_bases:
                class_bases[iface["class_name"]] = entity

        type_strings = [_normalize_type(p["type"]) for iface, _ in valid for p in iface["params"]]
        type_strings += [_normalize_type(iface["return_type"]) for iface, _ in valid]
        type_strings += [_normalize_type(entity) for entity in class_bases.values()]
        import_lines = _resolve_imports(
            type_strings, custom_type_index, frozenset(all_class_names),
            schema_classes_by_module, _module_for_file_path(file_path),
            is_repository_layer=(layer == "repositories"),
        )

        raw_text = _assemble_file_text(layer, import_lines, valid, all_class_names, class_bases)
        try:
            tree = adapter.parse(raw_text)
        except SyntaxError as exc:
            raise TranslatorCliAssemblyError(
                f"{file_path} 組裝完成後的最終 ast.parse() 驗證失敗（代表組裝邏輯本身有 bug，見四章步驟 4）：{exc}"
            ) from exc
        files[file_path] = adapter.render(tree)

    return files, skipped


def build_files(
    python_structure: PythonStructure,
    db_models: dict[str, str],
) -> tuple[dict[str, str], list[dict], list[dict]]:
    """對應四章全節，回傳 `(file_path -> 最終檔案內容, skipped_interfaces,
    skipped_db_models)`。純記憶體組裝，不碰磁碟——寫入交給
    `write_files()`，對應四章「寫入時機：全部檔案...驗證通過後，一次性
    寫入磁碟」。
    """
    directory_tree = python_structure["directory_tree"]
    interfaces = python_structure["interfaces"]
    code_blocks = _extract_code_blocks(directory_tree)

    files: dict[str, str] = {}

    # 第二、三段：infra 段直接寫入，schema 段依 file_path 分組合併
    grouped_schema_blocks: dict[str, list[str]] = {}
    infra_sources: dict[str, str] = {}
    for file_path, code in code_blocks:
        if file_path in _INFRA_FILES:
            infra_sources[file_path] = code
        else:
            grouped_schema_blocks.setdefault(file_path, []).append(code)

    schema_sources: dict[str, str] = {}
    schema_trees: dict[str, ast.Module] = {}
    for file_path, blocks in grouped_schema_blocks.items():
        # try/except 涵蓋 _merge_schema_blocks() 本身——它內部對每個
        # block 各自呼叫 ast.parse()，單一 block 語法有誤會在合併
        # 「之前」就拋出 SyntaxError，不能只包住合併後的最終驗證。
        try:
            merged = _merge_schema_blocks(blocks)
            tree = ast.parse(merged)
        except SyntaxError as exc:
            raise TranslatorCliAssemblyError(
                f"{file_path} 合併後的 Schema 定義段無法通過 ast.parse()"
                f"（05a 渲染的 pseudocode 本身有問題，見四章）：{exc}"
            ) from exc
        schema_sources[file_path] = merged
        schema_trees[file_path] = tree
    files.update(schema_sources)

    for file_path, code in infra_sources.items():
        try:
            ast.parse(code)
        except SyntaxError as exc:
            raise TranslatorCliAssemblyError(f"{file_path}（基礎設施段）無法通過 ast.parse()：{exc}") from exc
        files[file_path] = code

    # db_models 併入（四章「db_models」一節）：逐項驗證，個別失敗只跳過那一個檔案
    skipped_db_models: list[dict] = []
    db_models_valid: dict[str, ast.Module] = {}
    for file_path, content in (db_models or {}).items():
        try:
            db_models_valid[file_path] = ast.parse(content)
        except SyntaxError as exc:
            skipped_db_models.append({"file_path": file_path, "error": str(exc)})
            continue
        files[file_path] = content

    # 自訂型別索引（四章「import 解析」來源一／二／三）
    custom_type_index, schema_classes_by_module = _build_custom_type_index(interfaces, schema_trees, db_models_valid)

    # interfaces：逐 InterfaceSpec 隔離失敗渲染
    interface_files, skipped_interfaces = _render_interface_files(
        interfaces, custom_type_index, schema_classes_by_module,
    )
    files.update(interface_files)

    # 步驟 4：全域最終檢查（防禦性，見四章「語法驗證與寫入」）
    for file_path, content in files.items():
        try:
            ast.parse(content)
        except SyntaxError as exc:
            raise TranslatorCliAssemblyError(
                f"{file_path} 組裝完成後的最終 ast.parse() 驗證失敗（代表組裝邏輯本身有 bug，見四章步驟 4）：{exc}"
            ) from exc

    return files, skipped_interfaces, skipped_db_models


def write_files(python_project_path: str, files: dict[str, str]) -> None:
    """對應四章「寫入時機」：`build_files()` 全部驗證通過後才呼叫這個
    函式一次性寫入磁碟。每個檔案各自 `parent.mkdir(parents=True,
    exist_ok=True)`，等同於先掃描 directory_tree 第一段預建所有子目錄。
    """
    root = Path(python_project_path)
    for file_path, content in files.items():
        full_path = root / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")


def _scan_project_custom_types(python_project_path: str) -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    """掃描 `python_project_path` 底下 `app/` 目錄所有 `.py` 檔案的頂層
    `ClassDef`（以及 `_table_assignment_names()` 找到的 `Table(...)`
    賦值，見該函式 docstring），建立 `class_name -> file_path` 索引。供
    `resolve_body_imports()` 在填空階段解析本體引用的跨檔案自訂類別
    （及 08a 八章 `@ManyToMany` 中介表變數）使用。

    跟 `_build_custom_type_index()` 不同：那個函式在骨架生成階段用
    「還在記憶體裡、尚未寫入磁碟」的三個結構化來源建索引；這裡則是在
    填空階段直接掃描「已經寫到磁碟上」的真實檔案——填空階段沒有
    `python_structure` 可用，掃描磁碟是唯一能拿到完整類別清單的方式，
    而且天然反映最新狀態（含同一次 pipeline 執行中先前 task 已經填入
    的內容）。單一檔案解析失敗（理論上不該發生，骨架階段已驗證過）就
    跳過那個檔案，不拖累整體掃描。

    第二個回傳值 `schema_classes_by_module`（對應 docs/refactor_bug_
    trace.md #45 真實案例）：跟 `_build_custom_type_index()` 的同名回傳
    值一樣的道理，同名 schema class 若被多個模組各自獨立定義一份，這裡
    的 `index` 是全域扁平索引，`sorted(root.rglob("*.py"))` 掃描順序
    （字母序）決定了同名 class 最後留誰的路徑，跟這個名稱實際是哪個
    模組要用完全無關（真實案例：`GetAllGradeRs` 同時在 `app/schemas/
    exam.py`／`app/schemas/school.py` 各自定義一份，字母序 "exam" <
    "school"，"school" 版本覆寫掉 "exam" 版本，導致 `exam_router.py::
    grades()` 這個明明該用自己模組 schema 的函式，填空階段補 import 時
    被指到 `school.py` 去）。這裡額外記錄「每個模組（`app/schemas/
    {module}.py` 的 `module` 段）自己的 schema 檔案定義了哪些 class
    名稱」，供 `resolve_body_imports()` 依樣比照 `_resolve_imports()`
    （#8）優先查「這個函式自己所屬的模組」，不必依賴可能被覆寫過的
    全域 `index`——這是 #8 當初就記錄下來、暫緩處理的分層盲區（見
    docs/refactor_bug_trace.md #17「附帶風險」），#45 是它第一次真的
    被踩到的真實案例。
    """
    root = Path(python_project_path) / "app"
    index: dict[str, str] = {}
    schema_classes_by_module: dict[str, set[str]] = {}
    if not root.is_dir():
        return index, {}
    for py_file in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        rel_path = py_file.relative_to(Path(python_project_path)).as_posix()
        module = (
            rel_path.removeprefix("app/schemas/").removesuffix(".py")
            if rel_path.startswith("app/schemas/") and rel_path.endswith(".py")
            else None
        )
        names = schema_classes_by_module.setdefault(module, set()) if module is not None else None
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                index[node.name] = rel_path
                if names is not None:
                    names.add(node.name)
        for name in _table_assignment_names(tree):
            index[name] = rel_path
    return index, {module: frozenset(names) for module, names in schema_classes_by_module.items()}


def resolve_body_imports(
    python_project_path: str,
    tree: ast.Module,
    body_statements: list[ast.stmt],
    bound_names: set[str],
    own_module: str | None = None,
) -> list[str]:
    """對應 07a 五章「填空模式：本體 import 解析」。`fill_function()`
    填入的函式本體是 qwen 自由生成的內容，可能引用簽名以外的名稱（跨
    檔案自訂類別、框架例外）——骨架階段的兩層 import 解析（見
    `_resolve_imports()`）只分析函式簽名，看不到這些，因為當時函式
    本體還是空的 `pass`。這裡在填空完成、`splice_body()` 已經把新本體
    接上 `tree` 之後，針對新插入的本體內容重新掃一次，找出遺漏的
    import。

    `bound_names` 是這個函式作用域內已知會被綁定的名稱（函式參數，
    含 `self`）——出現在這裡的名稱即使剛好命中已知關鍵字表／自訂型別
    索引，也不當成需要 import 的對象，因為它們本來就已經可用。

    比照四章「兩層都比對不到時保守不加、不猜」：只對已知關鍵字表跟
    真實掃描出的自訂型別索引（`_scan_project_custom_types()`）命中的
    名稱補 import，其餘一律不猜——本體裡任何其他未定義名稱，維持現狀
    （不主動修正），留給 Harness 的 module 局部驗證與 ⑦ Debug Agent
    處理，不在這裡窮舉。

    `own_module`（對應 docs/refactor_bug_trace.md #45 真實案例，補齊
    #8／#17 當初留下的分層盲區）：這個函式所屬的模組（呼叫端用既有的
    `_module_for_file_path(target_file)` 算出，`None` 代表 utils／
    `_global` 這種沒有 module 概念的檔案）。`_scan_project_custom_types()`
    回傳的 `index` 是全域扁平索引，同名 schema class 若被多個模組各自
    獨立定義一份，掃描順序（字母序）決定留誰的路徑，跟這個名稱實際該
    屬於哪個模組無關（真實案例：`GetAllGradeRs` 同時在 `exam.py`／
    `school.py` 定義，`exam_router.py::grades()` 卻被指到 `school.py`）
    ——這裡比照 `_resolve_imports()`（#8）優先查同一個模組自己的
    schema 定義，查不到才退回全域索引。
    """
    referenced: set[str] = set()
    assigned: set[str] = set(bound_names)
    for stmt in body_statements:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    referenced.add(node.id)
                else:
                    assigned.add(node.id)

    candidates = referenced - assigned - set(dir(builtins))
    if not candidates:
        return []

    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom):
            candidates -= {alias.asname or alias.name for alias in stmt.names}
        elif isinstance(stmt, ast.Import):
            candidates -= {alias.asname or alias.name.split(".")[0] for alias in stmt.names}

    # 這個名稱若本來就是「這個檔案自己」頂層定義的 class／函式／模組
    # 層級變數，同一模組內引用不需要 import——這裡的 candidates 只知道
    # 「在函式本體被引用、且不是參數/區域變數」，無法分辨它是跨檔案的
    # 自訂型別，還是同檔案另一個頂層符號（常見於這批 Java 靜態工具類
    # 翻譯結果：class 自己的方法回傳自己的型別，如
    # `ResponseResult.error() -> ResponseResult(...)`）。`_scan_project_
    # custom_types()` 掃描整個 app/ 目錄（含這個檔案自己），若不排除，
    # 會誤判成「要從自己匯入自己」，產生一行恆為 True 的循環 import，
    # `import` 這個模組時直接 ImportError（已用真實案例重現：
    # exam-platform-api 的 app/services/common_service.py 曾因此整個
    # 服務起不來，見 09b_implement_agent_code.md 十章「已知限制」）。
    same_file_top_level = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in tree.body:
        if isinstance(node, ast.Assign):
            same_file_top_level |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    candidates -= same_file_top_level

    if not candidates:
        return []

    # 已知關鍵字表比對用精確集合成員判斷，不是子字串／單字邊界比對
    # （見 `_keyword_hits()`）——這裡的 candidates 已經是逐一拆開的裸
    # 識別字（AST Name 節點），不是要在一段文字裡搜尋子字串，不會有
    # `_keyword_hits()` 要防的「自訂名稱剛好包含關鍵字子字串」問題，
    # 精確比對本身就是最精確的判斷方式。`Depends(`／`Callable[` 這兩個
    # 帶標點的關鍵字，比對前先用 `rstrip("([")` 去掉標點，因為 AST
    # `Name` 節點的 `.id` 不會帶括號（`Depends(get_db)` 這個呼叫式的
    # 名稱本身是 `"Depends"`，標點是呼叫語法的一部分，不是名稱的一部分）。
    lines = [
        import_line for keyword, import_line in _KNOWN_KEYWORD_IMPORTS if keyword.rstrip("([") in candidates
    ]

    custom_type_index, schema_classes_by_module = _scan_project_custom_types(python_project_path)
    own_module_schema_classes = schema_classes_by_module.get(own_module, frozenset()) if own_module else frozenset()

    for name in sorted(candidates):
        if name in own_module_schema_classes:
            module_path = f"app.schemas.{own_module}"
            if name in custom_type_index and custom_type_index[name] != f"app/schemas/{own_module}.py":
                logger.info(
                    "resolve_body_imports()：本體引用的 %s 在全域索引裡指向 %s，"
                    "但這個函式自己所屬的模組 %s 也定義了同名 class，優先用自己"
                    "模組的定義（見 docs/refactor_bug_trace.md #45／#8）：改成 "
                    "from %s import %s",
                    name, custom_type_index[name], own_module, module_path, name,
                )
        elif name in custom_type_index:
            module_path = custom_type_index[name].removesuffix(".py").replace("/", ".")
        else:
            continue
        lines.append(f"from {module_path} import {name}")

    return lines


def insert_import_lines(tree: ast.Module, import_lines: list[str]) -> None:
    """把 `resolve_body_imports()` 算出的 import 陳述式插入 `tree`，
    放在既有 import 區塊（含開頭可能有的模組 docstring）之後、其餘
    程式碼之前——就地修改 `tree.body`，呼叫端接著跑
    `PythonAdapter.render()` 即可反映這批新 import。
    """
    if not import_lines:
        return
    insert_at = 0
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            insert_at += 1
        elif (
            insert_at == 0
            and isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            insert_at += 1  # 模組 docstring，算在 import 區塊之前
        else:
            break
    new_nodes = [ast.parse(line).body[0] for line in import_lines]
    tree.body[insert_at:insert_at] = new_nodes
```

**已驗證**（`tests/translator_cli/test_scaffold.py`，58 個測試）：涵蓋型別正規化、code block 擷取、Schema 定義段合併（import 去重、機械註解不被 `ast.unparse()` 弄丟）、函式片段渲染、兩層 import 解析（已知關鍵字表含 `Callable`／`UUID`／`date`／`datetime`／`time`，AST 自訂型別索引，都比對不到時不加）、`build_files()` 端對端（正常情況、單一介面隔離失敗、Schema 定義段損毀時中止）、`write_files()` 自動建目錄。`_table_assignment_names()`（08a 八章 `@ManyToMany` 中介表支援）認得 `Table(...)`／`sqlalchemy.Table(...)` 兩種匯入風格且不誤收一般賦值；`resolve_body_imports()`／`insert_import_lines()`（填空模式本體 import 解析）涵蓋跨層掃描、已知關鍵字與跨檔案自訂類別解析、函式參數與 builtin 不誤判、兩層都比對不到時保守不加。新增 4 個測試涵蓋上方「同檔案自我 import」排除：`test_resolve_body_imports_excludes_class_defined_in_same_file`、`test_resolve_body_imports_excludes_function_defined_in_same_file`、`test_resolve_imports_excludes_same_file_class_names`、`test_build_files_same_file_class_signature_reference_not_self_imported`。另涵蓋 docs/refactor_bug_trace.md #45：`_scan_project_custom_types()` 額外回傳 `schema_classes_by_module`、`resolve_body_imports()` 的 `own_module` 參數優先查自己模組（含真實 `GetAllGradeRs` 情境的正反案例、`own_module` 缺乏該類別時正確退回全域索引）。

---

## 九、`client.py`——對外唯一入口

對應 07a 二、四、五、七、八章，串接以上模組。`generate_scaffold()`／`fill_function()`／`apply_file_fix()` 內部所有子行程、磁碟呼叫都用 `asyncio.to_thread()` 包裝，理由見下方模組 docstring。

`fill_function()` 帶 `translator_backend`（依 `_BACKEND_CLIENTS` 查表分派 qwen／Claude，見 07a 七章「分派方式」）、`java_source`／`referenced_source`（取代 `description` 作為模型輸入）兩組引數；內部拆成「鎖外 preview 讀取」（只為了組 `current_signature` 給 prompt 用）與「`git_ops.WRITE_LOCK` 底下重新讀檔／AST 替換／寫入／commit」兩段（見 07a 八章「決策：每個 task 一個 commit，寫入段用細粒度鎖序列化」）；`apply_file_fix()`（10a 十二章「phase 2：檔案層級修正」，⑦ Debug Agent 給的檔案層級修正走精確字串替換，不經 AST 函式定位）也在這裡。

```python
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
from typing import Literal

from common.run_context import adhoc_run_id
from translator_cli import claude_client, formatting, git_ops, ollama_client, python_adapter, scaffold
from translator_cli.exceptions import (
    TranslatorCliConfigError,
    TranslatorCliError,
    TranslatorCliModelOutputError,
    TranslatorCliNetworkError,
    TranslatorCliScaffoldMismatchError,
    TranslatorCliUpstreamDegradedError,
)
from translator_cli.python_adapter import PythonAdapter
from translator_cli.types import FillResult, PythonStructure, ReferencedSourceItem, ScaffoldResult

# 依 translator_backend 分派，見 07a 七章「分派方式」——兩個模組的
# get_function_body() 是同一份契約（引數、回傳、拋出的例外類型都一致），
# 這裡只是查表決定要呼叫哪一個。
_BACKEND_CLIENTS = {"qwen": ollama_client, "claude": claude_client}

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
    context_files: list[tuple[str, str]], *, task_id: str, current_signature: str = "",
    java_source: str = "", referenced_source: list[ReferencedSourceItem] | None = None,
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
    `java_source`／`referenced_source`／`context` 這幾處文字裡出現過的
    候選名稱（見 07a 五章「為什麼是 `java_source`／`referenced_source`」
    ——這兩個欄位是真正帶著原始碼文字的輸入，`description` 不承載這類
    內容，不適合當候選名稱來源），只留這個
    task 可能用到的 class；其餘檔案（真正有函式本體可以剝的一般程式碼檔）
    維持既有的 `strip_all_function_bodies()`。單一檔案裁減失敗（理論上
    不該發生——這裡的內容一定是合法 Python，`ast.parse()` 沒有理由失敗，
    但裁減本身不該變成新的失敗來源）時，那個檔案退回原始內容，不影響
    其他檔案／整體流程。純 AST 記憶體操作，不是 I/O，不需要
    `asyncio.to_thread()`（見模組 docstring）。
    """
    total_bytes = sum(len(content.encode("utf-8")) for _, content in context_files)
    if total_bytes < TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES:
        return context_files

    referenced_texts = [item["source"] for item in (referenced_source or [])]
    candidate_class_names = _referenced_class_names(current_signature, java_source, context, *referenced_texts)

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
    translator_backend: Literal["qwen", "claude"],
    java_source: str,
    referenced_source: list[ReferencedSourceItem],
    description: str,
    context: str,
    context_files: list[str],
    run_id: str | None = None,
    referenced_functions: list[tuple[str, str | None, str]] | None = None,
    fixed_body: str | None = None,
) -> FillResult:
    """對應 07a 五、六、七、八章。呼叫失敗時不寫入任何內容（見五章「呼叫
    失敗時不寫入任何內容」）——每個失敗分支都在寫入磁碟之前 return，
    是九章「衝突偵測」成立的前提。

    `translator_backend` 決定走 qwen 還是 Claude 兩條模型呼叫路徑（見
    `_BACKEND_CLIENTS`、07a 七章「分派方式」）；`java_source`／
    `referenced_source` 是模型輸入，`description` 不是（06a 五章：
    `description` 是純機械模板，只供 log／除錯人眼辨識用途，這裡只用
    一行 `logger.debug()` 留痕，不送進 prompt，見 07a 五章）。

    **寫入段用細粒度鎖序列化**（見 `git_ops.WRITE_LOCK`、07a 八章）：
    模型呼叫（`_BACKEND_CLIENTS[translator_backend].get_function_body()`）
    不持鎖——qwen 靠自己模組內的 `OLLAMA_MODEL_SEMAPHORE` 天然序列化，
    Claude 可以真正平行呼叫。取得 `body_source` 之後才 `await
    git_ops.WRITE_LOCK.acquire()`，在鎖底下**重新讀取一次 `target_file`**
    （不是重用模型呼叫之前那份 `tree`/`node`）——模型呼叫可能耗時數十秒
    到數分鐘，這段等待期間，另一個平行跑的 Claude task 完全可能已經對
    同一個檔案的另一個函式寫入並 commit 過；若沿用舊的 `tree` 直接
    `splice_body()` 再整檔 `ast.unparse()`，等於用一份過時的檔案內容
    覆蓋掉那次已經 commit 的變更（見 07a 八章「為什麼不能只靠『結構上
    不存在並行寫入』這個論證」的 race condition 說明）。取得
    `current_signature`（給 prompt 用）的那次讀取不受這個限制——目標
    函式自己的簽名在骨架階段之後不會被其他
    task 修改，讀到的值必然穩定，不需要在鎖底下重讀。

    `run_id`：選填，省略時用 `adhoc_run_id()`。這裡是模型呼叫鏈的
    最外層（見 `11a_logging_architecture.md` 九章「run_id 的解析只在
    fill_function() 做一次」），只在這裡解析一次再往下傳，同一個 task
    的多次格式修正 attempt 才會落在同一個 run_id 底下，不會各自
    fallback 出不同的值。

    `referenced_functions`：選填，`(file_path, class_name, function_name)`
    三元組清單，對應 `graph/state.py::TaskSpec.referenced_functions`（06a
    改版前的舊機制，[P] 已不再產生這份資料，見 `plan_agent/planning.py`
    ——保留這個參數與底下 `_read_context_files()` 的既有行為，缺席時
    安全地什麼都不做，不強制呼叫端更新）。傳給 `_read_context_files()`
    決定 `context_files` 裡哪些檔案該只抽取指定函式、哪些該整份帶入，
    見該函式 docstring。

    `fixed_body`：選填，見 10a 八章「⑦ 直接產生修正後程式碼」。非
    `None` 時代表呼叫端（⑦ Debug Agent）已經產生好正確的函式本體（跟
    `get_function_body()` 回傳的格式一樣：未縮排陳述式文字，不含 `def`
    簽名行），直接拿來 `splice_body()`，完全跳過模型呼叫與它需要的
    `context_files` 讀取——這種情境下不是「重新翻譯」，是「套用已知
    正確的修正」，不需要模型參與。定位函式節點、import 解析、語法驗證、
    寫入＋git commit 等既有步驟不變。
    """
    resolved_run_id = run_id or adhoc_run_id()
    root = Path(python_project_path)
    adapter = PythonAdapter()
    logger.debug("task %s：%s", task_id, description)

    # 快速失敗（非權威檢查，見上方 docstring「寫入段用細粒度鎖序列化」）：
    # 在花時間讀檔／呼叫模型之前，先看一眼 working tree 是否已經不乾淨
    # （例如上一輪執行被強制中斷留下殘留），避免浪費一次可能數十秒到
    # 數分鐘的模型呼叫。真正權威的檢查在下面取得 WRITE_LOCK 之後再做
    # 一次。
    try:
        await asyncio.to_thread(git_ops.check_clean_working_tree, python_project_path)
    except TranslatorCliError as exc:
        return FillResult(success=False, error=str(exc), diff="")

    if fixed_body is not None:
        body_source = fixed_body
    else:
        try:
            source = await asyncio.to_thread(_read_target_file, root, target_file)
            preview_tree = adapter.parse(source)
            preview_node = adapter.locate_function(preview_tree, class_name, function_name)
            if preview_node is None:
                label = f"{class_name}.{function_name}" if class_name else function_name
                raise TranslatorCliScaffoldMismatchError(f"scaffold/task 不一致：{label} 在 {target_file} 找不到")
        except TranslatorCliError as exc:
            return FillResult(success=False, error=str(exc), diff="")

        current_signature = adapter.render_signature(preview_node)
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
            current_signature=current_signature, java_source=java_source,
            referenced_source=referenced_source, context=context,
        )

        backend_client = _BACKEND_CLIENTS[translator_backend]
        model_call_kwargs = dict(
            current_signature=current_signature,
            java_source=java_source,
            referenced_source=referenced_source,
            context=context,
            context_files=resolved_context_files,
            function_name=function_name,
            task_id=task_id,
            target_file=target_file,
            class_name=class_name,
            run_id=resolved_run_id,
        )
        try:
            body_source = await backend_client.get_function_body(**model_call_kwargs)
        except (TranslatorCliModelOutputError, TranslatorCliNetworkError, TranslatorCliConfigError) as exc:
            # TranslatorCliNetworkError（qwen 網路層重試耗盡／Claude API
            # 呼叫失敗）／TranslatorCliConfigError（qwen 缺
            # OLLAMA_BASE_URL／OLLAMA_API_KEY）都不會被 get_function_body()
            # 的格式重試迴圈攔截、會直接往外傳到這裡。
            #
            # 對應 docs/refactor_bug_trace.md：qwen 這一路失敗時，不直接
            # 判這個 task 失敗——改用 Claude API 對同一個 task 再試一次。
            # 理由：這個 task 一旦真的失敗（進 task_failed），
            # graph/scheduler.py 同 module 同後端的序列鏈上排在它後面的
            # 其他 qwen task 會永遠等不到它進 task_done、卡在不上不下的
            # 懸空狀態，連帶讓全域 Phase 關卡（見 09a 十二章）判斷不了
            # 「全專案 tier 0 是否已到終態」，牽連整條 pipeline 停擺——
            # 這個代價遠比多花一次 Claude API 呼叫嚴重，qwen 只是省錢用
            # 的第一選擇，不是唯一能把這個函式寫出來的辦法。只在
            # `translator_backend == "qwen"` 時才 fallback：claude 本身
            # 失敗沒有下一個更保底的後端可以退，維持原樣直接回報失敗。
            if translator_backend != "qwen":
                return FillResult(
                    success=False,
                    error=str(exc),
                    diff="",
                    upstream_degraded=isinstance(exc, TranslatorCliUpstreamDegradedError),
                )
            logger.warning(
                "task %s：qwen 呼叫失敗（%s），改用 Claude API 對同一個 task 重試", task_id, exc,
            )
            try:
                body_source = await claude_client.get_function_body(**model_call_kwargs)
            except (TranslatorCliModelOutputError, TranslatorCliNetworkError, TranslatorCliConfigError) as claude_exc:
                # 兩條路徑都失敗才真的判定失敗；upstream_degraded 沿用
                # qwen 那次的判斷（是不是連續多次傳輸層失敗），不是
                # claude 這次的——implement_node.py 要知道的是「ollama
                # 是不是本身有系統性問題」，不是「這次 fallback 呼叫本身
                # 有沒有問題」。
                return FillResult(
                    success=False,
                    error=f"qwen 失敗：{exc}；改用 Claude API 重試也失敗：{claude_exc}",
                    diff="",
                    upstream_degraded=isinstance(exc, TranslatorCliUpstreamDegradedError),
                )

    # 寫入段：取得 WRITE_LOCK 之後才是權威的 working-tree 狀態與檔案內容
    # （見上方 docstring）。鎖底下的任何失敗分支都直接 return，鎖隨
    # `async with` 結束自動釋放。
    async with git_ops.WRITE_LOCK:
        try:
            await asyncio.to_thread(git_ops.check_clean_working_tree, python_project_path)
        except TranslatorCliError as exc:
            return FillResult(success=False, error=str(exc), diff="")

        try:
            source = await asyncio.to_thread(_read_target_file, root, target_file)
            tree = adapter.parse(source)
            node = adapter.locate_function(tree, class_name, function_name)
            if node is None:
                label = f"{class_name}.{function_name}" if class_name else function_name
                raise TranslatorCliScaffoldMismatchError(f"scaffold/task 不一致：{label} 在 {target_file} 找不到")
        except TranslatorCliError as exc:
            return FillResult(success=False, error=str(exc), diff="")

        try:
            adapter.splice_body(node, body_source)
        except TranslatorCliModelOutputError as exc:
            if fixed_body is not None:
                # 見 docs/09b_bug_trace.md #52：這是 ⑦ Debug Agent 給的
                # fixed_body 被 extract_body_statements() 的巢狀同名函式檢查
                # 攔下來的情況（不是 ⑤ 模型的一般格式錯誤重試），值得在
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
        # （模型生成，或 fixed_body 給定），可能引用簽名以外的名稱（跨檔案
        # 自訂類別、框架例外），骨架階段的 import 解析看不到這些，這裡針對
        # 新本體重新掃一次補上。掃描專案磁碟找自訂型別索引是 I/O，包
        # to_thread；純 AST 插入不是。
        bound_names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
        if node.args.vararg:
            bound_names.add(node.args.vararg.arg)
        if node.args.kwarg:
            bound_names.add(node.args.kwarg.arg)
        # 對應 docs/refactor_bug_trace.md #45：把 target_file 反推出的
        # own_module 傳給 resolve_body_imports()，讓它比照 #8 優先查
        # 「這個函式自己所屬模組」的 schema 定義，不要被全域索引（掃描
        # 順序決定同名 class 留誰）誤導到別的模組去，見該函式 docstring。
        missing_imports = await asyncio.to_thread(
            scaffold.resolve_body_imports,
            python_project_path,
            tree,
            node.body,
            bound_names,
            scaffold._module_for_file_path(target_file),
        )
        scaffold.insert_import_lines(tree, missing_imports)

        new_source = adapter.render(tree)
        try:
            adapter.validate_syntax(new_source)
        except SyntaxError as exc:
            return FillResult(success=False, error=f"寫入前最終語法驗證失敗（理論上不應發生）：{exc}", diff="")

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
    這個不同的定位方式上。跟 `fill_function()` 不同，這裡不需要在鎖底下
    重讀一次：這裡從讀檔到寫檔中間沒有任何長時間等待（`old_snippet`／
    `new_snippet` 是 ⑦ 已經算好的現成資料，不需要呼叫任何模型），
    `WRITE_LOCK` 底下一次讀檔即可——套用 `git_ops.WRITE_LOCK` 是因為
    跟 `fill_function()` 一樣是這個 git repo 的寫入路徑之一，需要同一把
    鎖序列化，見 07a 八章。
    """
    root = Path(python_project_path)
    async with git_ops.WRITE_LOCK:
        try:
            await asyncio.to_thread(git_ops.check_clean_working_tree, python_project_path)
        except TranslatorCliError as exc:
            return FillResult(success=False, error=str(exc), diff="")

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
```

**已驗證**（`tests/translator_cli/test_client.py`，49 個測試，真實 `tmp_path` git repo＋`monkeypatch` 假造 ollama／Claude／git／磁碟呼叫，`formatting.format_paths()` 用 autouse fixture stub 成 no-op）：`generate_scaffold()` 涵蓋 precondition 失敗與寫入／commit 失敗時自動 rollback；`fill_function()` 端對端涵蓋成功填空並正確 commit、本體引用簽名以外名稱時正確補 import、**同一個 task 重複呼叫且生成內容與現有版本相同時視為冪等成功、不建立空 commit**（見 07a 五章「冪等」）、`target_file`／函式找不到、`context_files` 部分缺失的容錯、working tree 不乾淨時拒絕寫入（含模型生成期間才被改動的競態）、commit／格式化失敗時分別走對應的 rollback 路徑、環境變數缺失轉成 `FillResult(success=False)` 而非原生例外洩漏、`TranslatorCliUpstreamDegradedError` 正確標記 `upstream_degraded=True`；`fixed_body` 給定時跳過模型呼叫、巢狀同名函式被拒絕且正確記 log（09b_bug_trace.md #52，`caplog` 鎖住訊息帶 task_id 與 #52 編號）。另涵蓋 09b_bug_trace.md #37／#45 修法：`_trim_context_files_if_oversized()` 門檻觸發／未觸發兩種情況、`referenced_functions` 精準抽取與未命中檔案維持完整內容、`fill_function()` 端對端確認 `referenced_functions` 正確傳到 `get_function_body()`、`app/schemas/`／`app/models/` 路徑改用 `extract_referenced_classes()` 依候選類別名稱過濾而非剝函式本體、無候選信號時保留完整內容（`TestTrimContextFilesIfOversized`、`TestReadContextFilesReferencedFunctions`、`TestReferencedClassNames`）。另涵蓋：`TestFillFunctionBackendDispatch`（`translator_backend="qwen"`／`"claude"` 分別只呼叫對應的 backend client、不誤觸另一個）、`apply_file_fix()` 端對端（`old_snippet` 找不到／出現多次／替換成功並 commit／套用後語法驗證失敗）、`test_fill_function_waits_for_write_lock_before_writing`（模型呼叫期間 `WRITE_LOCK` 已被另一個呼叫持有時，寫入段確實等待鎖釋放才開始）。

### `formatting.py`

`PythonAdapter.render()`（見三章）的 `ast.unparse()` 輸出語法合法，但不保證符合 PEP8／專案 lint 規則。`formatting.py` 在寫入完成、commit 之前依序呼叫 `ruff check --select I --fix`（import 排序）與 `ruff format`（其餘排版）把輸出正規化——`ruff` 是硬性依賴：任一步找不到執行檔或本身失敗都直接拋出，不吞掉。

**為什麼不是 best-effort**：07a 六章「`ast.unparse()` 會重新格式化整個檔案」這個承諾（風格天生一致，`git diff` 只集中在被填的那個函式）建立在「每次寫入都經過同一條格式化流程」的前提上。若格式化只在部分環境／部分次執行才生效，這個前提就不成立——`scaffold` 若在有裝 `ruff` 的機器上先跑過一次，之後任何一次 `fill_function()` 在沒有 `ruff` 的環境執行，那次的 diff 就會整檔改形（引號慣例、空行數量等），07a 承諾的「diff 範圍必須乾淨」隨之失效。因此格式化失敗一律中止該次呼叫、觸發 rollback（見九章 `generate_scaffold()`／`fill_function()`），寧可讓 task 失敗交給排程器重試，也不讓不一致的格式風格污染 git 歷史。

**`ruff format` 不處理 import 排序**：`ruff format`（格式器）只管排版（引號、空行、縮排），不含 isort 功能——`scaffold._merge_schema_blocks()`（四章）合併多個 Schema 定義段的 import 陳述式時只用清單／字典去重，只保證語法合法與不重複，不保證排序符合慣例。因此格式化流程先跑 `ruff check --select I --fix`（isort 對應的 lint 規則，開 `--fix` 自動排序整理），再跑 `ruff format` 正規化其餘排版。

**只解決格式風格，不解決註解流失**：`ast.parse()`／`ast.unparse()` 這個 translator-cli 核心機制本身不保留任何註解（Python 標準庫 `ast` 模組不把註解當成語法樹的一部分）——LLM 生成的函式本體若含有註解，在 `ollama_client.get_function_body()` 回傳的 `body_source` 被 `extract_body_statements()`（`python_adapter.py`）解析成陳述式清單的那一刻就已經遺失，這是比「寫入之後才格式化」更早的步驟，`ruff format` 救不回已經不存在的東西，它整理的是「已經沒有註解的程式碼」的排版。`prompts.py` 的 `SYSTEM_PROMPT_QWEN`／`SYSTEM_PROMPT_CLAUDE` 明確告知模型不要寫 `#` 註解（見五章），從源頭省下模型生成註解的 token，但這是成本優化，不是解法——根本限制仍在。

```python
# translator_cli/formatting.py
"""寫入後的格式化，統一用 `ruff check --select I --fix`＋`ruff format`
正規化 `PythonAdapter.render()`（見 `python_adapter.py`）產出的內容。
`ast.unparse()` 保證語法合法，但不保證符合 PEP8／專案 lint 規則（引號
慣例、空行數量、import 排序等）。

**`ruff format` 不處理 import 排序**：`ruff format`（格式器）只管排版
（引號、空行、縮排），不含 isort 功能——`scaffold._merge_schema_blocks()`
合併多個 Schema 定義段的 import 陳述式時用清單／字典去重，只保證語法
合法與不重複，不保證排序符合慣例。因此先跑一次
`ruff check --select I --fix`（isort 對應的 lint 規則，開 `--fix` 自動
排序整理），再跑 `ruff format` 正規化其餘排版——兩者職責不同，缺一個
都無法達到「風格完全一致」的目標。

**`ruff` 是硬性依賴，不是 best-effort**：07a 六章「`ast.unparse()` 會
重新格式化整個檔案」承諾「格式風格天生一致」，讓 `git diff` 只集中在
被填的那個函式——這個承諾建立在「每次寫入都經過同一條格式化流程」
這個前提上。若格式化只在部分環境／部分次執行才生效，這個前提就不成
立：`fill_function()` 每次都會先用 `ast.unparse()` 把整個目標檔案重新
序列化（例如把雙引號改成 `ast.unparse()` 固定使用的單引號、壓縮多餘
空行），若這次執行剛好沒有 `ruff` 可以把格式排回去，這個「整檔改形」
會原樣寫入 diff／commit，讓 07a 承諾的「diff 範圍必須乾淨」失效——
`scaffold` 若在有裝 `ruff` 的機器上先跑過一次，之後只要有任何一次
`fill_function()` 在沒有 `ruff` 的環境執行，那次的 diff 就會整檔改形。

因此找不到 `ruff` 執行檔、或 `ruff format` 本身執行失敗，都直接拋出
`TranslatorCliError` 中止這次呼叫（由 `client.py` 轉成
`FillResult(success=False)`／`ScaffoldResult` 失敗並觸發 rollback），
不允許「這次沒排版」的檔案流入 commit——寧可讓這個 task 失敗、交給
排程器記一次失敗重試，也不要讓不一致的格式風格污染 git 歷史。
"""
from __future__ import annotations

import subprocess

from translator_cli.exceptions import TranslatorCliError


def _run_ruff(python_project_path: str, args: list[str]) -> None:
    """執行單一 ruff 指令，找不到執行檔或指令本身失敗都拋出
    `TranslatorCliError`，不吞掉。
    """
    try:
        result = subprocess.run(
            ["ruff", *args],
            cwd=python_project_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise TranslatorCliError(
            "找不到 ruff 執行檔——ruff 是硬性依賴（見 07b 文件「formatting.py」），"
            "請先 `pip install ruff`（或 `pip install -r requirements.txt`）"
        ) from exc

    if result.returncode != 0:
        raise TranslatorCliError(f"ruff {' '.join(args)} 執行失敗：{result.stderr.strip()}")


def format_paths(python_project_path: str, *relative_paths: str) -> None:
    """對 `python_project_path` 底下的一或多個相對路徑（檔案，通常是
    `scaffold.write_files()`／`fill_function()` 剛寫入的那批路徑）依序
    呼叫 `ruff check --select I --fix`（import 排序）與 `ruff format`
    （其餘排版），就地格式化。任一步找不到 `ruff` 執行檔、或本身失敗
    都拋出 `TranslatorCliError`，不吞掉——呼叫端需要這個訊號才能觸發
    rollback，避免半格式化的內容進入 commit。
    """
    if not relative_paths:
        return
    _run_ruff(python_project_path, ["check", "--select", "I", "--fix", *relative_paths])
    _run_ruff(python_project_path, ["format", *relative_paths])
```

`requirements.txt` 新增一行 `ruff>=0.8.0`——執行環境沒有安裝 `ruff` 時，`generate_scaffold()`／`fill_function()` 每次呼叫都會在格式化步驟失敗並觸發 rollback，不會有「跑起來但格式沒套用」的中間狀態。

**已驗證**（`tests/translator_cli/test_formatting.py`，5 個測試）：空路徑清單 no-op、`ruff` 執行檔不存在或 `check`／`format` 任一步失敗時拋出 `TranslatorCliError`、成功時兩步依序執行且組出正確指令。`client.py` 收到這個例外後的 rollback 串接邏輯另見九章 `test_client.py` 的 `test_fill_function_rolls_back_on_format_failure`。

---

## 十、與既有程式碼的介面異動

對應 07a 二章「與既有程式碼的介面異動」，新增 `python_project_path` 並在對應呼叫點傳遞，以下四項全部已套用（`graph/nodes/scaffold_node.py` 這一項後續又被 08b 進一步修改，見該項說明）：

### 1. `graph/state.py`：新增 `python_project_path` 欄位

```python
class RefactorState(TypedDict):
    # 進入點輸入（main.py 組裝 initial_state 時填入，見九）
    java_project_path: str
    # translator-cli 寫入目標的 Python 專案根目錄，對應 .env 的
    # PYTHON_PROJECT_PATH（見 07a_translator_cli_architecture.md 二章
    # 「新輸入：python_project_path」）——與 refactor-project/、
    # java_project_path 同層、各自獨立的 git repo，scaffold_node.py／
    # implement_node.py 呼叫 translator_cli.generate_scaffold()／
    # fill_function() 時顯式傳入，translator_cli 本身不 import
    # graph.state（見 07a 十二章）。
    python_project_path: str
    ...
```

### 2. `main.py`：`initial_state` 新增對應欄位

```python
initial_state = {
    "java_project_path": os.environ["JAVA_PROJECT_PATH"],  # ① 解析 Agent 讀取用，見 00 五章「環境建立」
    "python_project_path": os.environ["PYTHON_PROJECT_PATH"],  # translator-cli 寫入目標，見 07a 二章
    ...
}
```

**需要人工在 `.env` 補上 `PYTHON_PROJECT_PATH`**：`.env` 屬於敏感設定檔，本次實作不代為修改（見 CLAUDE.md 紅線規則）。比照既有 `JAVA_PROJECT_PATH` 的既有慣例，在 `.env` 新增一行（相對或絕對路徑皆可，見 07a 二章）：

```
PYTHON_PROJECT_PATH=../python-target-project
```

且目標目錄需完成 07a 二章「一次性前置準備」：目錄存在、`git init` 過、沒有任何 commit（`generate_scaffold()` 執行前會用 `git_ops.ensure_git_repo()` 檢查，沒準備好會直接回報明確錯誤）。

### 3. `graph/nodes/scaffold_node.py`：接上 `generate_scaffold()`（07b 當時的版本，已被 08b 取代）

07b 這一步只接上 `generate_scaffold()` 本身的呼叫契約：`python_project_path` 顯式傳入，`db_models` 當時固定傳 `None`（`db_models` 這個字典本身怎麼組出來，07a 十二章明訂屬於「08a（骨架實作 Agent 詳細設計，待建立）」的範圍，07b 落地時 08a 還不存在），`result["skipped_interfaces"]`／`["skipped_db_models"]` 當時也還沒有管道寫進 `RefactorState`，只先記警告。

**這個檔案後續被 08a／08b 進一步修改**：`db_models` 改由 `scaffold_agent.build_db_models()` 真正算出，`RefactorState` 新增 `skipped_interfaces`／`skipped_db_models` 兩個欄位承接。目前的權威版本見 `08b_scaffold_agent_code.md` 八章「與既有程式碼的介面異動」，這裡不重複貼一份會跟著實作進度過期的程式碼片段。

### 4. `graph/nodes/implement_node.py`：`_run_one_task()` 補齊完整引數（07b 當時的版本，已被 09a／09b 大幅擴充）

07b 這一步只接上 `fill_function()` 最初的呼叫契約：

```python
"""
⑤ 功能改寫 Agent（translator-cli + scheduler）
依 task list 逐一呼叫 translator-cli 實作業務邏輯
見 01_langgraph_architecture.md 六、07a_translator_cli_architecture.md、07b_translator_cli_code.md

translator-cli 串接已完成（見 07b）；Harness 局部驗證（_partial_verify()
的 db／verifier）仍是 stub，非 07a/07b 範圍，見 02a/02b、09a（待建立）。
"""
import asyncio

from graph.scheduler import ModuleScheduler
from graph.state import RefactorState
from translator_cli import client as translator_cli
from translator_cli.types import FillResult

MODEL_SEMAPHORE = asyncio.Semaphore(1)


async def _run_one_task(task: dict, python_project_path: str) -> FillResult:
    async with MODEL_SEMAPHORE:
        return await translator_cli.fill_function(
            python_project_path=python_project_path,
            task_id=task["id"],
            target_file=task["target_files"][0],
            class_name=task.get("class_name"),
            function_name=task["function_name"],
            description=task["description"],
            context=task.get("context", ""),
            context_files=task["target_files"],
        )
```

**這個檔案後續被 09a／09b 大幅擴充**：`run()` 內對應改動：`_run_one_task()` 呼叫改傳 `state["python_project_path"]`；`result["success"]` 全部改成 `result.success`（`FillResult` 是 dataclass，不是字典）；09a／09b 補上 Harness 局部驗證（`DbEnvironment`／`GoldenVerifier`、熱重載同步屏障）、task 失敗根因追蹤、⑦ Debug Agent 的 `pending_fixed_bodies`／`pending_file_fixes` 套用。模組層級 `MODEL_SEMAPHORE`（曾經包住整個 `fill_function()` 呼叫）已移除——qwen 的單模型序列化搬進 `translator_cli/ollama_client.py::OLLAMA_MODEL_SEMAPHORE`（見六章），寫入段的序列化改由 `translator_cli/git_ops.py::WRITE_LOCK`（見四、九章）負責；`_run_one_task()` 簽名新增 `java_project_path`／`run_id`／`pending_fixed_bodies` 引數，在呼叫 `fill_function()` 之前透過 `graph/java_source_extraction.py`（見下方第 5 項）把 `task["java_method_id"]`／`task["reference_targets"]` 座標解析成 `java_source`／`referenced_source` 兩個真實原始碼字串（`fixed_body is not None` 時完全跳過這段解析，直接套用 ⑦ 給的修正）；`fill_function()` 呼叫補上 `translator_backend=task["translator_backend"]`。目前的權威版本見 `09b_implement_agent_code.md`，這裡不重複貼一份會跟著實作進度過期的完整程式碼片段。

**未變動的部分**：`ModuleScheduler`、regression 偵測、局部驗證觸發時機等排程邏輯完全不受影響——07a／07b 只定義 translator-cli 被呼叫端的契約，node 內部怎麼組 task／怎麼呼叫排程器是 09a／09b 的範圍。

### 5. `graph/java_source_extraction.py`：`implement_node.py` 呼叫 `fill_function()` 前的座標解析

不屬於 `translator_cli` 套件（刻意不放進去，見 07a 十二章「維持獨立性」——`translator_cli` 不 import `graph.state`、不碰 Java 原始碼），是⑤（`implement_node.py`）呼叫 `fill_function()` 之前的前置工作，因此獨立成 `graph/` 底下的小模組。提供 `resolve_java_source(java_project_path, java_method_id) -> str`（`java_source`，見 07a 五章：直接讀 `java_method_id` 指到的整個 Java 檔案，`Path.read_text()`，不定位、不切割——真實 `lang-exam-api-refactor` 90 個檔案每個恰好一個頂層 class，讀整個檔案＝讀整個 class）與 `resolve_referenced_source(java_project_path, python_project_path, reference_targets) -> list[ReferencedSourceItem]`（`referenced_source`：Java 側項目先整份讀入，總量超過 `REFERENCED_SOURCE_TRIM_THRESHOLD_BYTES`（預設 13000 bytes）才個別退回單方法抽取——`extract_java_method_source()`／`_find_method_span()` 這套 javalang 定位起始行＋手動大括號配對機制保留，只在這個裁減路徑使用；Python 側維持 `ast.get_source_segment()` 精確單函式抽取，不受裁減影響。單一項目解析失敗記警告並跳過、不中止整個呼叫）。詳見該檔案模組 docstring；這裡不重複貼完整程式碼，因為它不是 `translator_cli` 被呼叫端契約的一部分。測試見 `tests/graph/test_java_source_extraction.py`（10 個測試：`java_source` 整檔讀取、同 class 方法互見、`java_method_id` 的 class／method 段落刻意不使用；`referenced_source` 未超門檻整份保留、超門檻裁減、Python 項目不受裁減影響、找不到檔案安全跳過）。

---

## 十一、模組結構總覽

```
refactor-project/
└── translator_cli/
    ├── client.py           # 對外唯一入口：generate_scaffold()、fill_function()（translator_backend 分派、WRITE_LOCK 序列化）、apply_file_fix()
    ├── types.py            # FillResult／ScaffoldResult／PythonStructure／InterfaceSpec／ReferencedSourceItem 等輸出入契約
    ├── python_adapter.py   # LanguageAdapter Protocol、PythonAdapter：ast 定位／替換／渲染／語法驗證
    ├── scaffold.py          # directory_tree 解析、interfaces 分組渲染、import 解析、db_models 併入
    ├── ollama_client.py     # httpx 呼叫 ollama（經 nginx）、delimiter 抽取、修正重試、上游異常偵測、呼叫記錄（11a 九章）、OLLAMA_MODEL_SEMAPHORE
    ├── claude_client.py     # Claude API 連線（call_claude_for_json()）、Structured Outputs、格式修正重試
    ├── prompts.py           # 七章 qwen／Claude 兩條路徑各自的 system prompt＋共用 user prompt 模板
    ├── git_ops.py            # commit、git status 衝突偵測、rollback、WRITE_LOCK 寫入段序列化
    ├── formatting.py         # ruff format（硬性依賴）
    └── exceptions.py         # 內部例外型別

refactor-project/
└── graph/
    └── java_source_extraction.py  # java_method_id／reference_targets 座標解析成真實原始碼（不屬於 translator_cli 套件，見十章第 5 項）
```

跟 07a 十一章規劃的結構一致，`client.py`／`types.py` 從 stub 換成真實實作，`formatting.py` 是規劃之外新增的模組，`claude_client.py` 是 `translator_cli` 第九個模組，其餘檔案對應原訂結構。

---

## 十二、已知限制與待驗證事項

- ~~**`app/core/exception_handlers.py`（`_global` 保留模組）被誤判成 unknown layer、整個跳過**~~——已解決（見 07a 十五章、`docs/09b_bug_trace.md` #11/#12）：`_render_interface_files()` 新增精確比對 `_GLOBAL_ADVICE_FILE`，`_assemble_file_text()` 新增對應分支（渲染成自由函式、不包 `APIRouter` 樣板）。已用真實 `../lang-exam-api-refactor` 完整跑過 ①③[P]④⑤⑥ 驗證：`skipped_interfaces=0`，`app/core/exception_handlers.py::handle_all` 正確生成且容器內確認能被 Starlette 例外處理中介層正確呼叫到。單元測試見 `tests/translator_cli/test_scaffold.py::test_build_files_renders_global_advice_file_without_api_router_boilerplate`。
- **已接上真實 ollama／nginx 環境＋真實 Java 專案（93 個檔案）跑過端對端測試**：真實 pipeline（① 解析 → ③ 設計 → [P] 規劃 → ④ 骨架 → ⑤ 實作）70 個 task 中 69 個成功，delimiter 修正重試、網路層重試都真實觸發並驗證過。`FORMAT_RETRY_COUNT` 依實測結果從 1 調整為 2（見六章）；`TRANSLATOR_CLI_TIMEOUT_SECONDS`（300 秒）依實測判斷足夠（簡單任務單次生成 15～19 秒）；`TRANSLATOR_CLI_NETWORK_RETRIES` 維持 2 不調整，理由見 07a 十四章。唯一失敗的真實案例（複雜查詢邏輯、6 個 target_files）已定位根因：qwen 對這個複雜度的任務，第一次回應違反 delimiter 格式的機率偏高，不是偶發——這超出 translator-cli 自己重試機制能保證解決的範圍，屬於 09a（⑤ Agent 詳細設計，待建立）該補的「task 永久失敗後如何交給 ⑦ Debug Agent 重試」機制，07a 十四章已記錄這個分工。**這次驗證的是早期單後端契約**（`description` 為模型輸入、單一號誌序列化整個 `fill_function()`），不涵蓋 `translator_backend` 分派、`java_source`／`referenced_source`、`git_ops.WRITE_LOCK` 這些路徑，見下方項目。
- **填空模式本體 import 解析（`resolve_body_imports()`）已對真實案例驗證**：真實 pipeline 執行中發現 qwen 生成的函式本體確實會引用簽名以外的名稱（如呼叫 `UserRepository`、拋出 `HTTPException`），已實作修正並對同一個真實案例重新對真實 ollama 驗證，import 正確補上、寫入內容語法合法，見八章。
- **（尚未對真實環境驗證）雙後端／呼叫鏈 context 這批新路徑只有單元／整合測試覆蓋，未跑過真實端對端 pipeline**：`translator_backend` 分派（qwen／Claude 二選一）、`java_source`／`referenced_source` 取代 `description`、`claude_client.py` 對真實 Claude API 的 Structured Outputs 行為、`git_ops.WRITE_LOCK` 底下多個平行 Claude task 真實同時完成模型呼叫時的寫入時序——這些都只驗證過 `monkeypatch` 假造行為（242 個測試，見目錄「已驗證」段落），沒有對真實 ollama／Claude API／真實併發 task 跑過一次完整 pipeline。比照 07a 十四章「待決定事項」已經記錄的同類缺口（例如「Claude 路徑的併發上限尚未定案」），這是新增的 TODO：真實環境驗證仍是待辦事項。
- **（已由 08a／08b 解決）`db_models` 曾經固定傳 `None`**：07b 落地當下 `scaffold_node.py` 還沒有能力從 DB 或 Java entity 取得 `db_models` 字典。08a／08b 已補上 `scaffold_agent.build_db_models()`，`scaffold_node.py` 現在傳的是真正的字典，`app/models/{module}.py` 會正常產出，`build_files()`／`write_files()` 一如當初預期不需要改動，見 08b 六、八章。
- **（已由 08a／08b 解決）`skipped_interfaces`／`skipped_db_models` 曾經沒有管道進 `RefactorState`**：08a 十二章已補上這兩個 State 欄位，`scaffold_node.py` 現在會把 `generate_scaffold()` 與 `build_db_models()` 兩邊的跳過清單合併寫入，見 08b 八章。
- **（已解決）`generate_scaffold()` 失敗時 `scaffold_node.py` 不會特別分流**：`result["success"] = False` 時，`scaffold_done=False` 會經由 `implement`→`run_tests` 之間新增的 conditional edge（`implement_node.should_run_tests_or_give_up()`）直接分流到 `give_up`，不進 `debug` 重試迴圈（`debug` 對這類失敗無能為力，進去只會浪費重試次數），見 01 五章「scaffold 失敗時的收尾路徑」與對應測試 `tests/graph/test_implement_node.py`。
- **`_normalize_type()`／型別正規化的「已知殘留限制」未特別測試**：07a 四章提到的萬用字元泛型（如 `List<? extends Foo>`）轉換後仍含非法字元 `?`，測試涵蓋了「隔離失敗、不拖累其餘介面」的行為，但沒有涵蓋其他潛在殘留寫法——這批資料目前的目標專案沒有出現過，維持 07a 原文「暫不特別處理」的既有結論。
- **rollback 本身失敗時，只回報清楚的錯誤訊息，不會再重試或進一步自動處理**：這是刻意的邊界——rollback 本身失敗代表 git 環境有更根本的問題，不是 translator-cli 這一層能安全猜測、自動修復的情況，維持 07a 十三章「需要人工介入的錯誤不重試」的既有原則。
- **`ast.parse()`／`ast.unparse()` 不保留任何註解，是 `ast` 模組本身的根本限制**：LLM 在 `fill_function()` 生成的函式本體若含有解釋性註解，在 `extract_body_statements()` 把 `body_source` 解析成陳述式清單的那一刻就已經遺失（Python 標準庫 `ast` 不把註解視為語法樹的一部分），`formatting.format_paths()` 面對的已經是「沒有註解」的程式碼，救不回來。真正要保留 LLM 生成程式碼裡的註解，需要換掉 `ast.parse`／`ast.unparse` 這個核心機制（例如改用保留具體語法樹的 `libcst`），這是 07a 六章「AST 插入機制」設計決策本身的範圍，記錄在這裡供未來若確定需要保留生成程式碼裡的註解時，回頭重新評估。**`prompts.py` 的 `SYSTEM_PROMPT_QWEN`／`SYSTEM_PROMPT_CLAUDE` 明確告知模型「不要寫 `#` 註解，反正會被丟掉」**——這不解決根本限制，但能省下模型生成註解的 output token（既然一定會被丟掉），也讓模型不會誤以為留了有意義的說明。
- **巢狀空 body（如 `if True: pass`）不在 CLI 層攔截，維持現狀**：`extract_body_statements()`（三章）只擋「陳述式清單完全為空」與「模型重複輸出函式簽名」兩種情況（07a 六章步驟 6a／6b），`if True: pass` 這種「語法合法、陳述式非空，但邏輯上等同沒做事」的退化輸出不會被攔下。這跟 07a 六章步驟 6b 明確點出的同一類問題（「隱含 return None，語法完全合法但語意錯誤，這個錯誤能通過步驟 10 的驗證，卻不是正確的實作」）性質相同——07a 只選擇攔「模型重複輸出函式簽名」這一種**可以無歧義機械判定**的模式，其餘語意層級的錯誤（`if True: pass`、`while False: ...`、算了但沒回傳等）刻意不窮舉，留給 Harness 實際執行測試／⑦ Debug Agent 處理，不是 CLI 層「格式契約」該負責的範圍。額外針對 `if True: pass` 這一種寫法加特判，既不能涵蓋這整類問題（`for _ in []: pass`／`try: pass except: pass` 等都是同樣性質但不同寫法），也違背 07a 這裡已經明確定案的邊界，因此維持現狀，不在 CLI 層新增這類語意檢查。
- **`ruff` 是硬性依賴**：`requirements.txt` 已加入 `ruff>=0.8.0`，執行環境沒裝的話 `generate_scaffold()`／`fill_function()` 每次呼叫都會在格式化步驟失敗並觸發 rollback（見九章 `formatting.py`）——這是刻意的取捨，用「沒裝 ruff 就整條流程都不能跑」換取 07a「diff 範圍必須乾淨」的承諾不因環境差異而破功，而不是讓格式化變成錦上添花的 best-effort。連帶地，`_split_block_imports()`（見四章）合併 Schema 定義段時可能殘留的多餘空行，也因為 `ruff format` 現在保證每次都會跑而自然被壓平，不需要另外處理。
