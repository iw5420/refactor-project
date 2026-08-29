"""
超過重試次數的收尾節點
通知人工介入
見 01_langgraph_architecture.md 五
"""
from graph.state import RefactorState


async def run(state: RefactorState) -> RefactorState:
    # TODO: 實作通知人工的邏輯（Slack/email）
    # 見 02a 十六待實作清單

    # 三種抵達 give_up 的路徑，訊息分開印：
    # 1. 01 五章「④ 骨架生成失敗，直接跳過重試迴圈」——retry_count 通常
    #    還是 0，沿用其他訊息會誤導成「已經重試過」。
    # 2. 10a 七章「give_up_early」——⑦ Debug Agent 判斷這一輪所有
    #    root_cause module 都無法修（或機械分類已經確定沒有任何 module
    #    可能透過重試修好），走 debug → give_up 直接邊，不經過
    #    should_debug_or_done()。retry_count 是正常遞增的真實次數，不是
    #    被強制設成上限，需要跟「真的試到上限」區分開，避免人工誤以為
    #    已經耗盡所有重試機會才放棄。
    # 3. 既有的 retry_count 用盡（should_debug_or_done() 判定，debug ↔
    #    implement 迴圈跑滿 MAX_RETRY 輪）。
    if state.get("scaffold_done") is False:
        print("[GIVE_UP] ④ 骨架生成失敗（generate_scaffold() success=False），跳過重試迴圈，等待人工介入")
        print(f"skipped_interfaces: {state.get('skipped_interfaces')}")
        print(f"skipped_db_models: {state.get('skipped_db_models')}")
    elif state.get("give_up_early"):
        print(f"[GIVE_UP] ⑦ Debug Agent 判斷已無可修（非重試次數用盡，目前 retry_count={state['retry_count']}），等待人工介入")
        print(f"Debug rounds: {state.get('debug_rounds')}")
        print(f"Failed modules: {state['failed_modules']}")
    else:
        print(f"[GIVE_UP] 超過重試次數 ({state['retry_count']})，等待人工介入")
        print(f"Failed modules: {state['failed_modules']}")
        print(f"Blocked modules: {state['blocked_modules']}")
        # 這條路徑本身無法分辨「連續三輪 Claude API 都打不通」跟「⑦ 判斷
        # 邏輯本身有問題、給的修法一直沒用」——兩者印出的訊息原本一模
        # 一樣。unanalyzed_root_cause_modules 非空代表最後一輪至少有
        # module 完全沒能成功呼叫到 Claude（見 debug_agent/analysis.py），
        # 提示人工先去查 llmlog 而不是急著懷疑 ⑦ 的判斷邏輯。
        if state.get("unanalyzed_root_cause_modules"):
            print(
                f"[GIVE_UP] 注意：最後一輪這些 module 的 Claude API 呼叫（含重試）仍然失敗，"
                f"完全沒能取得分析結果，可能是 API 本身的問題，不是 ⑦ 判斷錯誤，"
                f"建議先用 llmlog 查對應 trace 再判斷：{state['unanalyzed_root_cause_modules']}"
            )

    return state
