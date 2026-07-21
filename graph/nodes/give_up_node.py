"""
超過重試次數的收尾節點
通知人工介入
見 01_langgraph_architecture.md 五
"""
from graph.state import RefactorState


async def run(state: RefactorState) -> RefactorState:
    # TODO: 實作通知人工的邏輯（Slack/email）
    # 見 02a 十六待實作清單

    print(f"[GIVE_UP] 超過重試次數 ({state['retry_count']})，等待人工介入")
    print(f"Failed modules: {state['failed_modules']}")
    print(f"Blocked modules: {state['blocked_modules']}")

    return state
