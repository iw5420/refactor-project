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
    builder.add_node("parse", parse_node.run)
    builder.add_node("extract_spec", spec_node.run)
    builder.add_node("gen_manual_fill_templates", collection_node.run_generate_templates)
    builder.add_node("gen_collection", collection_node.run)
    builder.add_node("await_manual_fill", await_manual_fill_node.run)
    builder.add_node("record_tests", record_golden_output)
    builder.add_node("design", design_node.run)
    builder.add_node("plan", plan_node.run)
    builder.add_node("scaffold", scaffold_node.run)
    builder.add_node("implement", implement_node.run)
    builder.add_node("run_tests", run_postman_tests)
    builder.add_node("debug", debug_node.run)
    builder.add_node("give_up", give_up_node.run)

    # 主線性流程：parse → extract_spec → gen_manual_fill_templates →
    # gen_collection → record_tests → design（人工填值機制拆成兩階段，
    # 見 03a 三章「人工填值機制」、graph/nodes/collection_node.py 模組
    # docstring）
    builder.set_entry_point("parse")
    builder.add_edge("parse", "extract_spec")
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

    # 階段二（跑完剩下的 pipeline）後的人工補值關卡（見 01 五「人工補值
    # 關卡」，待同步兩階段拆分）：正常情況下階段一已經擋下所有待填
    # endpoint，這裡是防禦性的第二道關卡（例如階段一之後 openapi.json
    # 又變動、或人工填值套用失敗），不是預期中的常態路徑。
    builder.add_conditional_edges(
        "gen_collection",
        collection_node.should_await_manual_fill_or_continue,
        {
            "continue": "record_tests",
            "await_manual_fill": "await_manual_fill",
        },
    )
    builder.add_edge("await_manual_fill", END)

    builder.add_edge("record_tests", "design")

    # 平行分支：design 完成後，plan 與 scaffold 同時進入就緒狀態
    builder.add_edge("design", "plan")
    builder.add_edge("design", "scaffold")

    # fan-in：implement 的兩個前驅都完成後才觸發一次
    builder.add_edge("plan", "implement")
    builder.add_edge("scaffold", "implement")

    # implement → run_tests
    builder.add_edge("implement", "run_tests")

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

    # debug 迴圈回 implement
    builder.add_edge("debug", "implement")
    builder.add_edge("give_up", END)

    return builder.compile()
