"""
Harness 的 LangGraph 節點：record_golden_output (②) 與 run_postman_tests (⑥)
以及 conditional edge 判斷邏輯 should_debug_or_done
見 02a / 02b（尚未撰寫，暫用 stub）
"""
from graph.state import RefactorState


async def record_golden_output(state: RefactorState) -> RefactorState:
    """
    ② 測試 Agent（Harness 錄製端）
    對 Java 服務執行兩份 Collection，記錄每支 API 的 response 作為 golden output
    見 00_refactor_architecture.md 七/②
    """
    # TODO: 實作 Harness 錄製邏輯
    # 見 02a_harness_architecture.md

    golden_output = {
        "status": "recorded",
        "api_results": {},
    }

    return {
        **state,
        "golden_output": golden_output,
    }


async def run_postman_tests(state: RefactorState) -> RefactorState:
    """
    ⑥ 測試執行 Agent（Harness 驗證端）
    對 Python 服務執行 Postman collection，對比 golden output
    見 00_refactor_architecture.md 七/⑥
    """
    # TODO: 實作 Harness 驗證邏輯
    # 見 02a_harness_architecture.md

    # stub: 回傳 pass 讓圖順利走到 END
    test_results = {
        "status": "pass",
        "details": {},
    }

    return {
        **state,
        "test_results": test_results,
    }


def should_debug_or_done(state: RefactorState) -> str:
    """
    conditional edge 邏輯：根據測試結果與重試次數決定下一步
    見 01_langgraph_architecture.md 五
    """
    # ⚠️ failed_modules/blocked_modules 的語意差異見 implement_node 說明
    failed_modules = state.get("failed_modules", [])
    blocked_modules = state.get("blocked_modules", [])
    retry_count = state.get("retry_count", 0)
    test_results = state.get("test_results", {})

    # 測試全過
    if test_results.get("status") == "pass":
        return "done"

    # 有失敗的 module（非被牽連的 blocked），且未超過重試上限
    if failed_modules and retry_count < 3:
        return "debug"

    # 超過重試上限或沒有可修復的 module
    return "give_up"
