# graph/java_source_extraction.py
"""把 `TaskSpec.java_method_id`／`TaskSpec.reference_targets`（06a 六章
機械算出的座標）解析成真正的原始碼文字，組成 `translator_cli.fill_function()`
需要的 `java_source`／`referenced_source` 兩個新引數（見 07a 五章「為什麼
是 `java_source`／`referenced_source`」）。

**這件事刻意不放進 translator_cli／parse_agent**：translator_cli 不
import `graph.state`、不碰 Java 原始碼（見 07a 十二章「維持獨立性」）；
parse_agent/call_graph.py 的職責是建構呼叫圖／class 結構化資訊（04a 三
章），不是「抽取某個方法的原始碼文字」——那是這個重構新增的能力，歸屬
⑤（`graph/nodes/implement_node.py`）在呼叫 `fill_function()` 之前做的
前置工作，因此獨立成這個小模組，供 `implement_node.py` 呼叫（見
07a 十二章討論、與使用者確認後的決定）。

**`java_source`（task 自己對應的 Java 方法）讀整個檔案**：真實
`lang-exam-api-refactor` 專案的 90 個 `.java` 檔案，每個檔案恰好一個
頂層 class，0 個例外——「整個 class」在這個專案裡就是「整個檔案」。
讀整個檔案是單純的 `Path.read_text()`，不需要任何解析或定位，錯誤率
趨近於零。附帶效果：⑤ 翻譯某個方法時，天然看得到同一個 class 內其他
方法的真實原始碼（不管呼叫鏈有沒有正確解析出那條呼叫邊，同 class 的
內容本來就整份都在），不需要額外的「同 class 一定要收錄」判斷邏輯。

**`referenced_source`（見下方 `resolve_referenced_source()`）維持單方法
抽取為主，總量超過門檻才裁減**：`reference_targets` 可能有好幾筆（上限
`PLAN_AGENT_MAX_REFERENCE_TARGETS`），每筆都整份讀入的話，總量疊起來
才是真的需要控制大小的地方，跟 `java_source`（固定一筆）性質不同。
單方法抽取採用「javalang 定位起始行＋手動大括號配對」——javalang 只做
語法層解析，不像 Python `ast` 模組保留原始碼的精確起訖位置（沒有
`end_position`），無法直接切出一段方法原始碼；若改用「javalang 重新
產生程式碼」，等於用機械模板重寫一份 Java，不是給 ⑤ 看真實原始碼
（見 `refactor_plan.md` 一章：「让claude code api直翻...看程式碼比較
準」）。因此改成：javalang 只用來定位方法宣告的起始行
（`node.position.line`），從這行開始在原始檔案文字上手動配對大括號
（跳過字串／字元字面值、單行／區塊註解裡的假括號），找到方法本體真正
的結束位置——`ast.get_source_segment()` 在 Python 端就是精確做這件事
的標準函式，Java 沒有等價 API，這裡等於是 Java 版的土法煉鋼替代品。

**已知簡化**：抽取範圍從方法宣告所在行（含該行同一行的 modifier／
annotation，如 `public int bar(...)`）開始，不往上補抓寫在獨立行的
annotation（如另起一行的 `@Override`）——對 ⑤ 需要的「這個方法做什麼、
怎麼呼叫」判斷不影響，只是偶爾漏掉一行裝飾用的 annotation 文字。
"""
from __future__ import annotations

import ast
import logging
import os
from pathlib import Path

import javalang

from graph.state import ReferenceTarget
from translator_cli.types import ReferencedSourceItem

logger = logging.getLogger(__name__)

# 對應 `resolve_referenced_source()`「超過門檻才裁減」：referenced_source
# 可能有好幾筆（上限見 plan_agent/call_chain.py::MAX_REFERENCE_TARGETS，
# 預設 20），每筆都整份檔案讀進來，總量疊起來才是真的需要控制的地方；
# `java_source`（單一task固定一筆）不會有這個問題，見模組 docstring。
# 門檻值沿用 translator_cli/client.py::TRANSLATOR_CLI_CONTEXT_TRIM_
# THRESHOLD_BYTES 同一個量級（09b_bug_trace.md #37 的既有校準依據），
# 兩者是不同的裁減對象、各自獨立的環境變數，不共用同一個常數。
REFERENCED_SOURCE_TRIM_THRESHOLD_BYTES = int(
    os.environ.get("REFERENCED_SOURCE_TRIM_THRESHOLD_BYTES", "13000")
)


class JavaSourceExtractionError(Exception):
    """`java_method_id`／`ReferenceTarget` 座標在磁碟上的真實檔案裡找不到
    對應的方法──理論上不該發生（06a 已用「05a 對多載的消歧」＋「1:1
    涵蓋率」保證這些座標的來源必然存在，見 06a 六章），觸發代表更上游的
    資料有 bug，比照 `translator_cli.exceptions.TranslatorCliScaffoldMismatchError`
    的既有先例：不重試，直接往外拋，交由呼叫端轉成 task 失敗。
    """


def _skip_string_or_char(text: str, i: int, quote: str) -> int:
    """`i` 指向開頭的引號，回傳這個字串／字元字面值結束後（含結尾引號）
    的位置，正確跳過 `\\"`／`\\\\` 這類跳脫序列。
    """
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        j += 1
    return n


def _find_method_span(source: str, start_line: int) -> tuple[int, int]:
    """從 `start_line`（javalang 給的 1-indexed 行號）開始，找出這個方法
    宣告的完整原始碼範圍 `(start_offset, end_offset)`（`end_offset` 不含）
    ——見模組 docstring「手動大括號配對」。抽象方法／interface 方法宣告
    （沒有方法本體，以 `;` 結尾）也支援，回傳到 `;` 為止。
    """
    lines = source.splitlines(keepends=True)
    start_offset = sum(len(line) for line in lines[: start_line - 1])
    n = len(source)
    i = start_offset
    depth = 0
    brace_start: int | None = None

    while i < n:
        c = source[i]
        if c in ('"', "'"):
            i = _skip_string_or_char(source, i, c)
            continue
        if source[i : i + 2] == "//":
            newline = source.find("\n", i)
            i = newline + 1 if newline != -1 else n
            continue
        if source[i : i + 2] == "/*":
            end = source.find("*/", i + 2)
            i = end + 2 if end != -1 else n
            continue
        if brace_start is None and c == ";":
            # 沒有方法本體（interface／abstract 方法宣告）。
            return start_offset, i + 1
        if c == "{":
            if brace_start is None:
                brace_start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and brace_start is not None:
                return start_offset, i + 1
        i += 1

    # 理論上不該走到這裡（合法 Java 原始碼的大括號／分號必然配對）；
    # 保守回傳到檔案結尾，不拋例外中斷整個呼叫——這是最後一道防線，
    # 不是預期路徑。
    return start_offset, n


def _locate_method_start_line(tree: javalang.tree.CompilationUnit, class_name: str, method_name: str) -> int | None:
    """回傳 `class_name.method_name` 在原始碼裡的起始行（1-indexed），
    找不到回傳 `None`。多載方法只取第一個命中——method_id 本身不區分
    參數簽名（見 `parse_agent/types.py::method_id()` 既有限制），這裡
    延續同一個簡化。建構子（`ConstructorDeclaration`）跟一般方法
    （`MethodDeclaration`）都比對，因為 `java_index`／`java_method_id`
    的來源（`design_agent`）不區分兩者。
    """
    # javalang 的 Node.filter() 只接受單一 type（見其實作：
    # `isinstance(pattern, type)`），傳 tuple 會讓整個 filter 恆為空——
    # 因此改用 tree 本身的 walk（`for path, node in tree`）手動
    # isinstance 判斷，一次涵蓋 MethodDeclaration／ConstructorDeclaration
    # 兩種節點。
    node_types = (javalang.tree.MethodDeclaration, javalang.tree.ConstructorDeclaration)
    for path, node in tree:
        if not isinstance(node, node_types):
            continue
        if node.name != method_name or node.position is None:
            continue
        for ancestor in reversed(path):
            if isinstance(ancestor, (javalang.tree.ClassDeclaration, javalang.tree.InterfaceDeclaration, javalang.tree.EnumDeclaration)):
                if ancestor.name == class_name:
                    return node.position.line
                break
    return None


def extract_java_method_source(java_project_path: str, file_path: str, class_name: str, function_name: str) -> str:
    """對外主要入口之一：給定一個 Java 方法的座標（`file_path` 相對
    `java_project_path`），回傳這個方法完整的原始碼文字（含簽名、
    modifier；不含往上補抓的獨立行 annotation，見模組 docstring）。

    找不到檔案／解析失敗／找不到對應方法，都拋出 `JavaSourceExtractionError`
    ——這是防禦性的最後一道檢查，理論上不該發生（見該例外類別 docstring）。
    """
    abs_path = Path(java_project_path) / file_path
    try:
        source = abs_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JavaSourceExtractionError(f"讀取 Java 原始碼失敗：{abs_path}：{exc}") from exc

    try:
        tree = javalang.parse.parse(source)
    except javalang.parser.JavaSyntaxError as exc:
        raise JavaSourceExtractionError(f"Java 原始碼解析失敗：{abs_path}：{exc}") from exc

    start_line = _locate_method_start_line(tree, class_name, function_name)
    if start_line is None:
        raise JavaSourceExtractionError(
            f"在 {abs_path} 裡找不到方法 {class_name}.{function_name}（見 06a 六章 reference_targets 座標）"
        )

    start, end = _find_method_span(source, start_line)
    return source[start:end].strip()


def _read_whole_java_file(java_project_path: str, file_path: str) -> str:
    """讀整個 Java 檔案（＝整個 class，見模組 docstring「真實專案 90 個
    檔案每個恰好一個頂層 class」）。不解析、不定位、不配對大括號，純粹
    是 `Path.read_text()`——`resolve_java_source()`／
    `resolve_referenced_source()` 兩處共用同一個讀取邏輯與錯誤處理。
    """
    abs_path = Path(java_project_path) / file_path
    try:
        return abs_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JavaSourceExtractionError(f"讀取 Java 原始碼失敗：{abs_path}：{exc}") from exc


def extract_python_function_source(python_project_path: str, file_path: str, class_name: str | None, function_name: str) -> str:
    """對外主要入口之一：給定一個已翻譯完成的 Python 函式座標，回傳這個
    函式完整的原始碼文字（含簽名、裝飾器），用 `ast.get_source_segment()`
    精確切出，不是手動字串處理——Python 的 `ast` 模組保留完整的原始碼
    起訖位置資訊，不需要像 Java 那樣自己配對大括號。

    找不到檔案／解析失敗／找不到對應函式，都拋出 `JavaSourceExtractionError`
    （沿用同一個例外類型，語意上都是「06a 算出的座標，在磁碟上找不到
    對應內容」，呼叫端不需要分兩種例外類型處理）。
    """
    abs_path = Path(python_project_path) / file_path
    try:
        source = abs_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JavaSourceExtractionError(f"讀取已翻譯 Python 原始碼失敗：{abs_path}：{exc}") from exc

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise JavaSourceExtractionError(f"已翻譯 Python 原始碼解析失敗：{abs_path}：{exc}") from exc

    target_body = tree.body
    if class_name is not None:
        class_node = next(
            (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None
        )
        if class_node is None:
            raise JavaSourceExtractionError(f"在 {abs_path} 裡找不到 class {class_name}")
        target_body = class_node.body

    func_node = next(
        (
            n
            for n in target_body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function_name
        ),
        None,
    )
    if func_node is None:
        label = f"{class_name}.{function_name}" if class_name else function_name
        raise JavaSourceExtractionError(f"在 {abs_path} 裡找不到函式 {label}")

    segment = ast.get_source_segment(source, func_node)
    if segment is None:
        raise JavaSourceExtractionError(f"{abs_path} 的 {function_name} 無法取得原始碼片段（理論上不應發生）")
    return segment


def resolve_java_source(java_project_path: str, java_method_id: str) -> str:
    """`fill_function()` 的 `java_source` 引數：task 自己對應的 Java 方法
    所在的整個檔案（＝整個 class，見模組 docstring）。`java_method_id`
    就是①的 `method_id()` 格式（"{java_file_path}::{java_class_name}::
    {java_method_name}"，見 `graph/state.py::TaskSpec.java_method_id`），
    這裡只需要第一段（檔案路徑），`class_name`／`method_name` 兩段刻意
    不使用——讀整個檔案不需要定位到特定方法，見模組 docstring「改成讀
    整個檔案」。
    """
    java_file_path = java_method_id.split("::", 1)[0]
    return _read_whole_java_file(java_project_path, java_file_path)


def resolve_referenced_source(
    java_project_path: str, python_project_path: str, targets: list[ReferenceTarget]
) -> list[ReferencedSourceItem]:
    """`fill_function()` 的 `referenced_source` 引數：06a 六章
    `reference_targets` 逐項解析成真實原始碼文字（見 07a 五章）。單一項目
    抽取失敗記錄警告並跳過（不是中止整個呼叫）——`reference_targets`
    的用途是「給模型參考」，不是這個 task 本身的翻譯目標，缺一項不影響
    這個 task 能不能被正確翻譯，中止整個呼叫反而讓一個參考項目的問題
    拖累了核心任務。

    **兩階段：先整份讀，總量超過門檻才裁減成單方法**——`language="java"`
    的項目第一輪先用 `_read_whole_java_file()` 整份讀入（跟
    `resolve_java_source()` 同一個邏輯，同樣不需要大括號配對）；
    `language="python"` 的項目維持既有的 `extract_python_function_source()`
    單函式抽取，不受這裡的裁減邏輯影響——`ast.get_source_segment()`
    本來就精確、單函式抽取結果通常已經很小，沒有理由改成整份讀入再
    裁減。全部項目組好之後，加總位元組數：沒超過
    `REFERENCED_SOURCE_TRIM_THRESHOLD_BYTES` 就直接回傳整份內容；超過
    的話，把 `language="java"` 的項目個別退回單方法抽取
    （`extract_java_method_source()` 既有機制）——這是保底路徑，只在
    極端情況（`reference_targets` 真的逼近上限、且剛好都是大檔案）才會
    觸發，真實資料目前完全不會撞到（真實 78 個 task，單一 task 最多
    4 筆 reference_targets，最大 Java 檔案 16KB，遠低於門檻）。單一項目
    裁減失敗（理論上不該發生——這個檔案剛剛才成功整份讀過一次）時保留
    整份內容，不讓裁減本身變成新的失敗來源。
    """
    items: list[ReferencedSourceItem] = []
    for target in targets:
        try:
            if target["language"] == "java":
                source = _read_whole_java_file(java_project_path, target["file_path"])
            else:
                source = extract_python_function_source(
                    python_project_path, target["file_path"], target["class_name"], target["function_name"]
                )
        except JavaSourceExtractionError as exc:
            logger.warning(
                "reference_targets 抽取失敗，跳過這一項（見 06a 六章）：%s", exc
            )
            continue
        items.append(
            ReferencedSourceItem(
                file_path=target["file_path"],
                class_name=target["class_name"],
                function_name=target["function_name"],
                language=target["language"],
                source=source,
            )
        )

    total_bytes = sum(len(item["source"].encode("utf-8")) for item in items)
    if total_bytes <= REFERENCED_SOURCE_TRIM_THRESHOLD_BYTES:
        return items

    trimmed: list[ReferencedSourceItem] = []
    for item in items:
        if item["language"] != "java":
            trimmed.append(item)
            continue
        try:
            method_source = extract_java_method_source(
                java_project_path, item["file_path"], item["class_name"], item["function_name"]
            )
            trimmed.append({**item, "source": method_source})
        except JavaSourceExtractionError as exc:
            logger.warning(
                "referenced_source 裁減 %s.%s 失敗，保留整份內容：%s",
                item["file_path"], item["function_name"], exc,
            )
            trimmed.append(item)

    trimmed_bytes = sum(len(item["source"].encode("utf-8")) for item in trimmed)
    logger.info(
        "referenced_source 總量 %d bytes 超過門檻 %d bytes，已裁減至 %d bytes",
        total_bytes, REFERENCED_SOURCE_TRIM_THRESHOLD_BYTES, trimmed_bytes,
    )
    return trimmed
