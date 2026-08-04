"""
[B] Collection Agent（程式邏輯 + 少量 LLM）
將 OpenAPI JSON 轉換為 Postman Collection（readonly / mutation）
見 03a_spec_collection_agent_architecture.md 三、03c_collection_agent_code.md

分兩個 node，對應 03a 三章「人工填值機制」的兩階段：

- `run_generate_templates`（graph node id：`gen_manual_fill_templates`）
  ——階段一，落地 openapi.json、產生人工填值模板，不呼叫 Claude API
- `run`（graph node id：`gen_collection`，沿用舊名）——階段二，假設模板
  已經人工填完，跑完剩下的 pipeline（鏈式依賴偵測、folder 分組、套用
  人工填值、注入）

兩個 node 各自搭配一個 conditional edge 判斷函式（`should_await_manual_
fill_templates_or_continue`／`should_await_manual_fill_or_continue`），
跟對應的 node 放同一個檔案（比照 refactor_harness 的
`should_debug_or_done` 跟 run_tests 節點放一起的慣例），見
01_langgraph_architecture.md 五「人工補值關卡」（該章節的 graph 佈線
說明尚待同步這次的兩階段拆分，暫時以本檔案為準）。兩個判斷函式都指向
同一個 `await_manual_fill` 終止節點——不管是「階段一產生完模板」還是
「階段二跑完後仍有缺口」，語意上都是同一件事：`postman/manual_fill/`
底下還有沒填完的 endpoint，交給人工處理。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from graph.state import RefactorState
from spec_collection_agent import generate_manual_fill_templates, run_collection_agent


async def run_generate_templates(state: RefactorState) -> RefactorState:
    """階段一：落地 openapi.json、產生人工填值模板。丟到執行緒跑純粹是
    延續 spec_node.py／本檔案 `run` 的一貫做法，這一步本身不呼叫 Claude
    API、不是真的阻塞很久，但檔案 I/O 仍然是同步呼叫，跟其他 node 保持
    一致的包法比較不容易日後漏包。
    """
    pending = await asyncio.to_thread(
        generate_manual_fill_templates,
        openapi_spec=state["openapi_spec"],
        specs_dir=Path("specs"),
        postman_dir=Path("postman"),
    )

    return {
        **state,
        "collection_manual_fill_pending": pending,
    }


def should_await_manual_fill_templates_or_continue(state: RefactorState) -> str:
    return "await_manual_fill" if state.get("collection_manual_fill_pending") else "continue"


async def run(state: RefactorState) -> RefactorState:
    """階段二：假設 `run_generate_templates` 已經跑過、人工也已經處理完
    `postman/manual_fill/` 底下的模板。run_collection_agent() 內部有
    阻塞呼叫（npx 子進程、Claude API 同步呼叫），丟到執行緒跑，避免卡住
    事件迴圈（與 spec_node.py 做法一致）。
    """
    result = await asyncio.to_thread(
        run_collection_agent,
        specs_dir=Path("specs"),
        postman_dir=Path("postman"),
    )

    return {
        **state,
        "collection_readonly_path": result.collection_readonly_path,
        "collection_mutation_path": result.collection_mutation_path,
        "collection_manual_fill_pending": result.manual_fill_pending,
    }


def should_await_manual_fill_or_continue(state: RefactorState) -> str:
    return "await_manual_fill" if state.get("collection_manual_fill_pending") else "continue"
