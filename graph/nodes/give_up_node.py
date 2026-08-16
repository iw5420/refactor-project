"""
超過重試次數的收尾節點
通知人工介入
見 01_langgraph_architecture.md 五
"""
from graph.state import RefactorState


async def run(state: RefactorState) -> RefactorState:
    # TODO: 實作通知人工的邏輯（Slack/email）
    # 見 02a 十六待實作清單

    # 兩種抵達 give_up 的路徑，訊息分開印：一種是既有的 retry_count 用盡
    # （debug ↔ implement 迴圈），另一種是 01 五章新增的「④ 骨架生成
    # 失敗，直接跳過重試迴圈」路徑——後者 retry_count 通常還是 0，
    # 沿用舊訊息會誤導成「已經重試過」。
    if state.get("scaffold_done") is False:
        print("[GIVE_UP] ④ 骨架生成失敗（generate_scaffold() success=False），跳過重試迴圈，等待人工介入")
        print(f"skipped_interfaces: {state.get('skipped_interfaces')}")
        print(f"skipped_db_models: {state.get('skipped_db_models')}")
    else:
        print(f"[GIVE_UP] 超過重試次數 ({state['retry_count']})，等待人工介入")
        print(f"Failed modules: {state['failed_modules']}")
        print(f"Blocked modules: {state['blocked_modules']}")

    return state
