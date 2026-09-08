# plan_agent/__init__.py
"""[P] Plan Agent 對外唯一入口，`graph/nodes/plan_node.py` 只呼叫這裡
的函式（見 06a 九章）。"""
from __future__ import annotations

from graph.state import ModuleInfo, PythonStructure, TaskSpec
from plan_agent import planning


def run_plan_agent(
    *, module_list: list[ModuleInfo], python_structure: PythonStructure, java_project_path: str
) -> tuple[list[TaskSpec], list[ModuleInfo]]:
    """對應 06a 全文：輸出 `(task_list, module_list)`，`task_list` 直接
    對應 `RefactorState.task_list`（見 06a 九章）；`module_list` 是補回
    `_utils` 保留 module 之後的版本，直接覆寫 `RefactorState.module_list`
    （見 `planning.plan_all_modules()` docstring「第二個回傳值」）。
    `java_project_path`：六章「呼叫鏈範圍查找」需要。"""
    return planning.plan_all_modules(module_list, python_structure, java_project_path)
