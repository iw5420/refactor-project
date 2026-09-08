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
from parse_agent import grouping, skip_filter, summarize
from parse_agent.call_graph import parse_java_project


def run_parse_agent(
    *,
    java_project_path: str,
    unfilled_endpoints_path: Path,
    openapi_spec: dict,
    force_include_classes_path: Path,
) -> tuple[list[ModuleInfo], list[ApiMapping], list[tuple[str, str, str]]]:
    """對應 04a 全文：解析 Java 專案，輸出 `(module_list,
    api_to_python_target, skip_excluded_overloads)`，直接對應
    `RefactorState` 的 `module_list`／`api_to_python_target`／
    `skip_excluded_overloads` 三個欄位（見 04a 六章）。

    `skip_excluded_overloads`：使用者填 skip，是人工判斷「這個 endpoint
    整段不進翻譯流程」，不只是跳過自動化測試（見
    `docs/03a_spec_collection_agent_architecture.md`「Decision.SKIP 的
    語意」）。`compute_excluded_methods()` 排除的 `method_id` 不含 HTTP
    method，同名不同 HTTP method 的多載（如 `FileController` 的
    `voice`／`image`）會共用同一個 method_id、只能整組保留或整組排除，
    這裡改用 `compute_skip_excluded_overloads()` 額外算出 HTTP method
    精確的排除清單，交給 ③ `design_agent/design.py` 在重新掃描出每個
    多載各自的 `http_method` 之後，直接把該排除的多載從資料裡濾掉。

    `openapi_spec`：`RefactorState.openapi_spec`，[A] Spec Agent 產出，
    供 `skip_filter` 判定「非-skip 全集」使用（見 04a 二章、04b 八章，
    不是新增的 Claude API 呼叫或檔案讀取，單純從 state 轉傳）。

    `force_include_classes_path`：對應 04a 四章「人工強制納入清單」，
    比照 `unfilled_endpoints_path` 的既有慣例由呼叫端（`parse_node.py`）
    明確傳入固定路徑，不在這裡寫死——這份清單檔案本身是選填的（不存在
    視為空清單，見 `grouping.load_force_include_classes()`），但路徑
    本身仍是必要引數，維持跟 `unfilled_endpoints_path` 一致的呼叫慣例。
    """
    project = parse_java_project(java_project_path)

    force_include_class_names = grouping.load_force_include_classes(project, force_include_classes_path)
    module_drafts, class_to_module = summarize.run_map_reduce(project, force_include_class_names)

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

    skip_excluded_overloads = skip_filter.compute_skip_excluded_overloads(
        skip_endpoints=skip_endpoints,
        route_index=project.route_index,
    )

    return module_list, api_to_python_target, skip_excluded_overloads
