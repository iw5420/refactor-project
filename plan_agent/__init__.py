# plan_agent/__init__.py
"""[P] Plan Agent 對外唯一入口，`graph/nodes/plan_node.py` 只呼叫這裡
的函式（見 06a 九章）。"""
from __future__ import annotations

from graph.state import ModuleInfo, PythonStructure, TaskSpec
from plan_agent import planning


def run_plan_agent(*, module_list: list[ModuleInfo], python_structure: PythonStructure) -> list[TaskSpec]:
    """對應 06a 全文：輸出 `task_list`，直接對應 `RefactorState.task_list`
    （見 06a 八章）。"""
    return planning.plan_all_modules(module_list, python_structure)
