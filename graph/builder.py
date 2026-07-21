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
    design_node,
    plan_node,
    scaffold_node,
    implement_node,
    debug_node,
    give_up_node,
)
from refactor_harness.langgraph_nodes import (
    record_golden_output,
    run_postman_tests,
    should_debug_or_done,
)


def build_graph():
    builder = StateGraph(RefactorState)

    # 註冊所有節點
    builder.add_node("parse", parse_node.run)
    builder.add_node("extract_spec", spec_node.run)
    builder.add_node("gen_collection", collection_node.run)
    builder.add_node("record_tests", record_golden_output)
    builder.add_node("design", design_node.run)
    builder.add_node("plan", plan_node.run)
    builder.add_node("scaffold", scaffold_node.run)
    builder.add_node("implement", implement_node.run)
    builder.add_node("run_tests", run_postman_tests)
    builder.add_node("debug", debug_node.run)
    builder.add_node("give_up", give_up_node.run)

    # 主線性流程：parse → extract_spec → gen_collection → record_tests → design
    builder.set_entry_point("parse")
    builder.add_edge("parse", "extract_spec")
    builder.add_edge("extract_spec", "gen_collection")
    builder.add_edge("gen_collection", "record_tests")
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
