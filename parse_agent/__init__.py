# parse_agent/__init__.py
"""① 解析 Agent 對外唯一入口，`graph/nodes/parse_node.py` 只呼叫這裡的
函式（見 04a 七章）。串接順序：call_graph（三章，機械分析）→
summarize.run_map_reduce（四章，Claude API）→ skip_filter（五章，機械
分析，可以跟四章平行做，但兩者都很快，這裡選擇簡單的循序寫法，不為了
省幾秒鐘增加併發複雜度）→ 輸出組裝收尾（六章）。
"""
from __future__ import annotations

from pathlib import Path

from graph.state import ApiMapping, ModuleInfo
from parse_agent import skip_filter, summarize
from parse_agent.call_graph import parse_java_project


def run_parse_agent(
    *, java_project_path: str, unfilled_endpoints_path: Path, openapi_spec: dict
) -> tuple[list[ModuleInfo], list[ApiMapping]]:
    """對應 04a 全文：解析 Java 專案，輸出 `(module_list,
    api_to_python_target)`，直接對應 `RefactorState` 的
    `module_list`／`api_to_python_target` 兩個欄位（見 04a 六章）。

    `openapi_spec`：`RefactorState.openapi_spec`，[A] Spec Agent 產出，
    供 `skip_filter` 判定「非-skip 全集」使用（見 04a 二章、04b 八章，
    不是新增的 Claude API 呼叫或檔案讀取，單純從 state 轉傳）。
    """
    project = parse_java_project(java_project_path)

    module_drafts, class_to_module = summarize.run_map_reduce(project)

    skip_endpoints = skip_filter.load_skip_endpoints(unfilled_endpoints_path)
    excluded_methods = skip_filter.compute_excluded_methods(
        skip_endpoints=skip_endpoints,
        openapi_spec=openapi_spec,
        route_index=project.route_index,
        call_graph=project.call_graph,
    )
    filtered_drafts = summarize.filter_excluded_methods(module_drafts, excluded_methods)

    api_to_python_target = summarize.assemble_api_mapping(project, class_to_module, filtered_drafts, set(skip_endpoints))
    module_list = summarize.finalize_module_list(filtered_drafts)

    return module_list, api_to_python_target
