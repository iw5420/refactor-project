"""gen_collection 之後的人工補值關卡（見 docs/01_langgraph_architecture.md 五）。"""
import os

from graph.nodes.collection_node import should_await_manual_fill_or_continue


class TestShouldAwaitManualFillOrContinue:
    def test_empty_pending_continues(self):
        state = {"collection_manual_fill_pending": []}
        assert should_await_manual_fill_or_continue(state) == "continue"

    def test_missing_key_continues(self):
        assert should_await_manual_fill_or_continue({}) == "continue"

    def test_non_empty_pending_awaits(self):
        state = {"collection_manual_fill_pending": ["POST /api/x"]}
        assert should_await_manual_fill_or_continue(state) == "await_manual_fill"


class TestGraphBuild:
    def test_graph_builds_with_await_manual_fill_node(self, monkeypatch):
        # build_graph() 不需要真的呼叫外部服務，只是組裝，但 refactor_harness
        # 的 test_nodes.py 在 import 時就讀 JAVA_BASE_URL，要先給一個假值。
        monkeypatch.setenv("JAVA_BASE_URL", "http://localhost:8080")
        from graph.builder import build_graph

        graph = build_graph()
        nodes = set(graph.get_graph().nodes.keys())
        assert "await_manual_fill" in nodes
        assert "gen_collection" in nodes
