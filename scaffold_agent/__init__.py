# scaffold_agent/__init__.py
"""④ 骨架實作 Agent：`db_models` 產出，對應 08a 全文。對外唯一入口是
`build_db_models()`（十二章），供 `graph/nodes/scaffold_node.py` 呼叫；
不依賴 `design_agent`／`graph.state`（十一章），只依賴 `common/`。
"""
from __future__ import annotations

from scaffold_agent import model_builder, reference_resolver
from scaffold_agent.types import BuildDbModelsResult, ModuleInfo

__all__ = ["BuildDbModelsResult", "ModuleInfo", "build_db_models"]


def build_db_models(java_project_path: str, module_list: list[ModuleInfo]) -> BuildDbModelsResult:
    """對應十二章：純同步 CPU 運算（`javalang.parse()`＋索引建構＋渲染），
    不呼叫任何 LLM／外部服務（十三章）。呼叫端（`scaffold_node.py`）必須
    用 `asyncio.to_thread()` 包一層，避免佔住 event loop——這是 08a 十二
    章明訂的呼叫慣例，不在這個函式內部處理（`build_db_models()` 本身
    維持同步簽名，才能被 `asyncio.to_thread()` 直接丟進執行緒池）。
    """
    scan_index = reference_resolver.build_scan_index(java_project_path, module_list)
    return model_builder.assemble(scan_index, module_list)
