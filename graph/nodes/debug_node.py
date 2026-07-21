"""
⑦ Debug Agent（Claude API）
輸入：fail 清單 + diff 報告 + 對應 Python 原始碼
分析根本原因，輸出具體修正指令回饋給 Agent ⑤
"""
from graph.state import RefactorState


async def run(state: RefactorState) -> RefactorState:
    # TODO: 實作 Claude API 呼叫，分析 fail 並產生修正指令
    # 不修改 state，只當作 orchestrator 在找問題、等待人工或自動回饋

    return {
        **state,
        "retry_count": state.get("retry_count", 0) + 1,
    }
