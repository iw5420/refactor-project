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
    skip_excluded_overloads: list[tuple[str, str, str]],
    harness_config_path: str = "config/harness.yaml",
) -> tuple[PythonStructure, dict]:
    """對應 05a 全文：設計 Python 專案結構，輸出
    `(python_structure, route_to_file_mapping)`，直接對應 `RefactorState`
    的 `python_structure`／`route_to_file_mapping` 兩個欄位（見 05a 九章）
    ——同時把 `route_to_file_mapping` 與 `route_to_module_mapping` 一併
    實際寫入 `harness_config_path`（見 `route_mapping.write_route_
    mappings()`），這不是可選的附加行為，是 05a 九章、00 八章明訂的③
    職責本身。`route_to_module_mapping` 不進回傳值、也不進 State（05a
    八章已定案，只寫 yaml）。

    `skip_excluded_overloads`：`RefactorState` 同名欄位，① `parse_agent.
    skip_filter.compute_skip_excluded_overloads()` 算出的 HTTP method
    精確排除清單——使用者填 skip，是人工判斷「這個 endpoint 整段不進
    翻譯流程」，不只是跳過自動化測試，見
    `docs/03a_spec_collection_agent_architecture.md`「Decision.SKIP 的
    語意」。
    """
    interfaces, directory_tree, modules_with_schema_file, config_field_mappings, config_env_vars, java_index = (
        design.design_all_modules(
            module_list, api_to_python_target, openapi_spec, java_project_path, skip_excluded_overloads,
        )
    )
    python_structure = PythonStructure(directory_tree=directory_tree, interfaces=interfaces, java_index=java_index)
    if config_field_mappings:
        python_structure["config_field_mappings"] = config_field_mappings
    if config_env_vars:
        python_structure["config_env_vars"] = config_env_vars
    route_to_file_mapping, route_to_module_mapping = route_mapping.build_route_mappings(
        api_to_python_target, interfaces, modules_with_schema_file
    )
    route_mapping.write_route_mappings(
        route_to_file_mapping, route_to_module_mapping, config_path=harness_config_path
    )
    return python_structure, route_to_file_mapping
