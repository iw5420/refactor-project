"""
⑦ Debug Agent（Claude API）
輸入：fail 清單 + diff 報告 + 對應 Python 原始碼
分析根本原因，輸出具體修正指令回饋給 Agent ⑤
"""
from graph.state import RefactorState


async def run(state: RefactorState) -> RefactorState:
    # TODO: 實作 Claude API 呼叫，分析 fail 並產生修正指令
    # 不修改 state，只當作 orchestrator 在找問題、等待人工或自動回饋

    # retry_count 只在 failed_modules 非空時才扣減（01 五章／六章）：
    # blocked_modules 是被牽連、還沒真正跑過驗證的下游 module，不該
    # 消耗重試預算，等對應的 failed_modules 修好後排程器會自然釋放。
    increment = 1 if state.get("failed_modules") else 0

    return {
        **state,
        "retry_count": state.get("retry_count", 0) + increment,
    }
