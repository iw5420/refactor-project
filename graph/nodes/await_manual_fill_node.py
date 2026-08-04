"""
[B] Collection Agent 之後的暫停節點：還有 endpoint 待人工補值，把清單印
出來、指向 postman/manual_fill/，這條 graph run 到此結束。
見 01_langgraph_architecture.md 五「人工補值關卡」。

不是失敗（跟 give_up_node 不同），只是暫停等人工——比照 give_up_node
的終止節點寫法，不需要再往下傳遞任何新資訊，直接印出來、原樣回傳 state。
"""
from graph.state import RefactorState


async def run(state: RefactorState) -> RefactorState:
    pending = state.get("collection_manual_fill_pending", [])
    print(f"[AWAIT_MANUAL_FILL] {len(pending)} 個 endpoint 待人工補值：")
    for endpoint in pending:
        print(f"  - {endpoint}")
    print("請編輯 postman/manual_fill/ 底下對應的模板檔後再重新執行。")

    return state
