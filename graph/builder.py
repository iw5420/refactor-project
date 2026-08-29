"""
LangGraph StateGraph 組裝：node 註冊、edge 連接
見 01_langgraph_architecture.md 五
"""
from langgraph.graph import StateGraph, END
from graph.state import RefactorState
from graph.nodes import (
    parse_node,
    spec_node,
    collection_node,
    await_manual_fill_node,
    design_node,
    plan_node,
    scaffold_node,
    implement_node,
    debug_node,
    give_up_node,
)
from refactor_harness.langgraph_nodes.test_nodes import (
    record_golden_output,
    run_postman_tests,
    should_debug_or_done,
)


def build_graph():
    builder = StateGraph(RefactorState)

    # 註冊所有節點
    builder.add_node("extract_spec", spec_node.run)
    builder.add_node("gen_manual_fill_templates", collection_node.run_generate_templates)
    builder.add_node("gen_collection", collection_node.run)
    builder.add_node("await_manual_fill", await_manual_fill_node.run)
    builder.add_node("parse", parse_node.run)
    builder.add_node("record_tests", record_golden_output)
    builder.add_node("design", design_node.run)
    builder.add_node("plan", plan_node.run)
    builder.add_node("scaffold", scaffold_node.run)
    builder.add_node("implement", implement_node.run)
    builder.add_node("run_tests", run_postman_tests)
    builder.add_node("debug", debug_node.run)
    builder.add_node("give_up", give_up_node.run)

    # 主線性流程：extract_spec → gen_manual_fill_templates → gen_collection
    # → parse（人工填值機制拆成兩階段，見 03a 三章「人工填值機制」、
    # graph/nodes/collection_node.py 模組 docstring）
    builder.set_entry_point("extract_spec")
    builder.add_edge("extract_spec", "gen_manual_fill_templates")

    # 階段一（產生人工填值模板）後的人工填值關卡：有 endpoint 待填就
    # 暫停在 await_manual_fill，否則（例如重跑時模板早已填完）直接進
    # 階段二，不用人工再確認一次。
    builder.add_conditional_edges(
        "gen_manual_fill_templates",
        collection_node.should_await_manual_fill_templates_or_continue,
        {
            "continue": "gen_collection",
            "await_manual_fill": "await_manual_fill",
        },
    )

    # 階段二（跑完剩下的 pipeline）後的人工補值關卡：正常情況下階段一已經
    # 擋下所有待填 endpoint，這裡是防禦性的第二道關卡（例如階段一之後
    # openapi.json 又變動、或人工填值套用失敗），不是預期中的常態路徑。
    # 「continue」導向 parse（① 解析 Agent），不是 record_tests——① 的
    # skip 呼叫鏈排除（見 04a 五章）要讀 [B] 已定案的
    # postman/unfilled_endpoints.json，排更前面這份輸入不存在。
    builder.add_conditional_edges(
        "gen_collection",
        collection_node.should_await_manual_fill_or_continue,
        {
            "continue": "parse",
            "await_manual_fill": "await_manual_fill",
        },
    )
    builder.add_edge("await_manual_fill", END)

    # 平行分支：parse 完成後，record_tests（②）與 design（③）同時進入
    # 就緒狀態——② 不依賴③的輸出，③ 不依賴 golden_output，見
    # 00 一章流程圖、05a 十一章
    builder.add_edge("parse", "record_tests")
    builder.add_edge("parse", "design")

    # fan-in：plan／scaffold 的前驅是 record_tests 與 design 兩者都完成
    # 才觸發，維持圖上單一明確的合流點，見 01 五章
    builder.add_edge("record_tests", "plan")
    builder.add_edge("design", "plan")
    builder.add_edge("record_tests", "scaffold")
    builder.add_edge("design", "scaffold")

    # fan-in：implement 的兩個前驅都完成後才觸發一次
    builder.add_edge("plan", "implement")
    builder.add_edge("scaffold", "implement")

    # implement → run_tests，或 scaffold 失敗時直接 give_up（見 01 五章
    # 「scaffold 失敗時的收尾路徑」、implement_node.should_run_tests_or_
    # give_up()）。`implement` 只有單一前驅（上面的 fan-in 合流點不受
    # 影響），把這條邊改成 conditional edge 不影響 01 已驗證過的平行
    # 分支語意。
    builder.add_conditional_edges(
        "implement",
        implement_node.should_run_tests_or_give_up,
        {
            "run_tests": "run_tests",
            "give_up": "give_up",
        },
    )

    # Retry 迴圈：run_tests 的 conditional edge
    builder.add_conditional_edges(
        "run_tests",
        should_debug_or_done,
        {
            "done": END,
            "debug": "debug",
            "give_up": "give_up",
        },
    )

    # debug 迴圈：give_up_early（10a 七章：這一輪所有 root_cause module
    # 都判定不可修，或機械分類已經確定沒有任何 module 可能透過重試修好）
    # 路由到 give_up，不進 implement 浪費一輪重試；否則照舊回 implement
    # 讓 pending_fixed_bodies 生效。
    builder.add_conditional_edges(
        "debug",
        debug_node.should_retry_or_give_up,
        {
            "implement": "implement",
            "give_up": "give_up",
        },
    )
    builder.add_edge("give_up", END)

    return builder.compile()
