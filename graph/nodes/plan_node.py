"""
[P] Plan Agent（Claude API）
輸入：Agent ① 的模組清單 + Agent ③ 的架構設計
輸出：task_list
見 00_refactor_architecture.md 七/[P]、06a_plan_agent_architecture.md、06b_plan_agent_code.md

平行分支 node：③（design）完成後與 scaffold（④）同時觸發（見 00 一章流程圖、
01 五章、06a 十章——[P] 不依賴④的輸出），因此只回傳自己實際更動的
key，不能用 `{**state, ...}` 整包展開，避免跟 scaffold 同一個 superstep
對同一個 key 各自寫入。
"""
from __future__ import annotations

import asyncio

from graph.state import RefactorState
from plan_agent import run_plan_agent


async def run(state: RefactorState) -> dict:
    # run_plan_agent() 內部是同步阻塞呼叫（多次 Claude API 呼叫，且五章
    # 重試佇列的 5 分鐘等待，見 06a 五章），丟到執行緒跑，避免卡住事件
    # 迴圈（與 design_node.py／parse_node.py 做法一致）。
    task_list = await asyncio.to_thread(
        run_plan_agent,
        module_list=state["module_list"],
        python_structure=state["python_structure"],
    )

    return {"task_list": task_list}
