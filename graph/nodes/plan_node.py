"""
[P] Plan Agent（Claude API）
輸入：Agent ① 的模組清單 + Agent ③ 的架構設計
輸出：task_list、module_list（補回 `_utils` 保留 module 之後的版本）
見 00_refactor_architecture.md 七/[P]、06a_plan_agent_architecture.md、06b_plan_agent_code.md

平行分支 node：③（design）完成後與 scaffold（④）同時觸發（見 00 一章流程圖、
01 五章、06a 十章——[P] 不依賴④的輸出），因此只回傳自己實際更動的
key，不能用 `{**state, ...}` 整包展開，避免跟 scaffold 同一個 superstep
對同一個 key 各自寫入。`module_list` 這裡也是[P]實際更動的 key（見下方
`run_plan_agent()` 呼叫說明）——④（scaffold_node.py）不讀也不寫這個
key，兩個平行 node 不會對同一個 key 各自寫入。
"""
from __future__ import annotations

import asyncio

from graph.state import RefactorState
from plan_agent import run_plan_agent


async def run(state: RefactorState) -> dict:
    # run_plan_agent() 內部是同步函式（純機械組裝，不再呼叫 Claude API，
    # 見 06a 五章、十章「執行特性的變化」），仍丟到執行緒跑，避免這個
    # 同步呼叫卡住事件迴圈（與 design_node.py／parse_node.py 做法一致，
    # 維持同一種呼叫慣例，不因為變快了就特殊處理）。
    #
    # module_list 一併覆寫：真實環境發現 `_utils` 保留 module（utils 檔案
    # 路徑不帶 module 前綴，見 06a 四章特例一）只存在於 task.module 上，
    # ①的 module_list 從來沒有對應條目，導致 graph/scheduler.py::
    # ModuleScheduler 永遠排不到 utils 的 task（見 plan_agent/planning.py::
    # plan_all_modules() docstring 完整說明）。[P] 補回這筆缺的 ModuleInfo，
    # 這裡把補完的版本寫回 state，取代①原始版本，下游（⑤／⑦）一律讀
    # 這個補完後的版本。
    task_list, module_list = await asyncio.to_thread(
        run_plan_agent,
        module_list=state["module_list"],
        python_structure=state["python_structure"],
        java_project_path=state["java_project_path"],
    )

    return {"task_list": task_list, "module_list": module_list}
