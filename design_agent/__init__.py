# design_agent/__init__.py
"""③ 架構設計 Agent 對外唯一入口，`graph/nodes/design_node.py` 只呼叫
這裡的函式（見 05a 十章）。串接順序：六章逐波設計（`design.
design_all_modules()`，內部依序完成四章簽名掃描、五章型別對應／
openapi 覆寫、六章 LLM 呼叫）→ 八章機械合併 `route_to_file_mapping`。
"""
from __future__ import annotations

from design_agent import design, route_mapping
from graph.state import ApiMapping, ModuleInfo, PythonStructure


def run_design_agent(
    *,
    module_list: list[ModuleInfo],
    api_to_python_target: list[ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    harness_config_path: str = "config/harness.yaml",
) -> tuple[PythonStructure, dict]:
    """對應 05a 全文：設計 Python 專案結構，輸出
    `(python_structure, route_to_file_mapping)`，直接對應 `RefactorState`
    的 `python_structure`／`route_to_file_mapping` 兩個欄位（見 05a 九章）
    ——同時把 `route_to_file_mapping` 實際寫入 `harness_config_path`
    （見 `route_mapping.write_route_to_file_mapping()`），這不是可選的
    附加行為，是 05a 九章、00 八章明訂的③職責本身。
    """
    interfaces, directory_tree, modules_with_schema_file = design.design_all_modules(
        module_list, api_to_python_target, openapi_spec, java_project_path
    )
    python_structure = PythonStructure(directory_tree=directory_tree, interfaces=interfaces)
    route_to_file_mapping = route_mapping.build_route_to_file_mapping(
        api_to_python_target, interfaces, modules_with_schema_file
    )
    route_mapping.write_route_to_file_mapping(route_to_file_mapping, config_path=harness_config_path)
    return python_structure, route_to_file_mapping
