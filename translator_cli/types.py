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
from typing import NotRequired, TypedDict


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


class ScaffoldResult(TypedDict):
    """對應 07a 四章「回傳契約」。`success=True` 但
    `skipped_interfaces`／`skipped_db_models` 非空是預期中會發生、需要
    人工留意但不阻擋 pipeline 的情況，見該章節說明。
    """

    success: bool
    error: str | None
    skipped_interfaces: list[SkippedInterface]
    skipped_db_models: list[SkippedDbModel]
