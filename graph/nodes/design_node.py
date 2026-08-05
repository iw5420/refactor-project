"""
③ 架構設計 Agent（Claude API）
輸入：Agent ① 的解析結果（module_list／api_to_python_target）+ openapi.json
輸出：python_structure, route_to_file_mapping
見 05a_design_agent_architecture.md、05b_design_agent_code.md

平行分支 node：parse 完成後與 record_tests（②）同時觸發（見 00 一章流程圖、
01 五章、05a 十一章——③ 不依賴 golden_output），因此只回傳自己實際更動的
key，不能用 `{**state, ...}` 整包展開，避免跟 record_tests 同一個
superstep 對同一個 key 各自寫入。
"""
from __future__ import annotations

import asyncio

from design_agent import run_design_agent
from graph.state import RefactorState


async def run(state: RefactorState) -> dict:
    # run_design_agent() 內部是同步阻塞呼叫（javalang 掃描＋多次 Claude
    # API 呼叫，且六章重試佇列的 5 分鐘等待，見 05a 六章），丟到執行緒
    # 跑，避免卡住事件迴圈（與 parse_node.py／spec_node.py／collection_node.py
    # 做法一致）。
    python_structure, route_to_file_mapping = await asyncio.to_thread(
        run_design_agent,
        module_list=state["module_list"],
        api_to_python_target=state["api_to_python_target"],
        openapi_spec=state["openapi_spec"],
        java_project_path=state["java_project_path"],
    )

    return {
        "python_structure": python_structure,
        "route_to_file_mapping": route_to_file_mapping,
    }
