"""
⑦ Debug Agent（Claude API）
輸入：test_results（⑥ 全量驗證，權威 bug list）+ task_failures（⑤ 的
task 級失敗根因）+ 對應 Python 原始碼。分析根本原因，輸出具體修正指令
回饋給 Agent ⑤（見 10a_debug_agent_architecture.md）。

真正的分析邏輯在 debug_agent/ 套件（同步、依模組用 ThreadPoolExecutor
平行呼叫 Claude API，比照 plan_agent/design_agent 既有模式）。這裡只是
薄封裝：把同步工作包一層 asyncio.to_thread()，理由見 09a 三章「必須包
asyncio.to_thread()」的既有洞察——LangGraph 對 async def node 不會自動
丟進執行緒池，只有普通 def node 才會，這裡選擇讓 debug_agent/ 維持同步
寫法、由這一層節點負責不卡住事件迴圈，而不是把整個套件改寫成 asyncio
風格（見 10a 十章）。
"""
import asyncio

from debug_agent import run_debug_analysis
from graph.state import RefactorState


async def run(state: RefactorState) -> RefactorState:
    return await asyncio.to_thread(run_debug_analysis, state)


def should_retry_or_give_up(state: RefactorState) -> str:
    """10a 七章「路由方式」：retry_count 上限檢查已經前移到
    should_debug_or_done()（02b），進入 debug 之前就已經確認過還在預算
    內——這裡只需要判斷 give_up_early（由 run_debug_analysis() 判斷並
    寫入 state）。不再需要重複檢查 retry_count，也因此不再有
    should_debug_or_done() 與這裡各自用不同比較符號、產生 off-by-one
    的風險（見 10a 七章「為什麼要統一套用」）。
    """
    return "give_up" if state.get("give_up_early") else "implement"
