"""
① 解析 Agent（Claude API）
輸入：Java 專案路徑、[B] Collection Agent 已定案的 postman/unfilled_endpoints.json、
     state 既有的 openapi_spec（供 skip_filter 判定「非-skip 全集」，見 04a 二章、04b 八章）
輸出：module_list, api_to_python_target
見 04a_parse_agent_architecture.md、04b_parse_agent_code.md

排在 gen_collection（[B] Collection Agent 階段二）之後、record_tests
（② 測試 Agent）之前，見 04a 二章、01 五章「parse（① 解析 Agent）排在
[B] 之後」；graph/builder.py 目前的 node 順序尚未同步這個編排（見
00 十章「已知落差」），本檔案的 run() 函式本身不受影響，只是還沒被排在
正確的位置上呼叫。這個順序同時也是 openapi_spec 一定已經在 state 裡的
前提——parse 排在 extract_spec（[A]）之後，state["openapi_spec"] 這時
必定已經填好。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from graph.state import RefactorState
from parse_agent import run_parse_agent


async def run(state: RefactorState) -> RefactorState:
    # run_parse_agent() 內部是同步阻塞呼叫（javalang 掃描＋多次 Claude
    # API 呼叫，且 Map 重試佇列的 5 分鐘等待，見 04a 四章），丟到執行緒
    # 跑，避免卡住事件迴圈（與 spec_node.py／collection_node.py 做法一致）。
    module_list, api_to_python_target = await asyncio.to_thread(
        run_parse_agent,
        java_project_path=state["java_project_path"],
        unfilled_endpoints_path=Path("postman") / "unfilled_endpoints.json",
        openapi_spec=state["openapi_spec"],
    )

    return {
        **state,
        "module_list": module_list,
        "api_to_python_target": api_to_python_target,
    }
