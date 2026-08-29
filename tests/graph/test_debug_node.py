"""graph/nodes/debug_node.py，對應 docs/10a_debug_agent_architecture.md 十章。"""
import asyncio

from graph.nodes.debug_node import run, should_retry_or_give_up


class TestShouldRetryOrGiveUp:
    def test_give_up_early_true_routes_to_give_up(self):
        assert should_retry_or_give_up({"give_up_early": True}) == "give_up"

    def test_give_up_early_false_routes_to_implement(self):
        assert should_retry_or_give_up({"give_up_early": False}) == "implement"


class TestRunWrapsInThreadAsyncioToThread:
    def test_run_delegates_to_run_debug_analysis(self, monkeypatch):
        import graph.nodes.debug_node as debug_node

        captured = {}

        def _fake_run_debug_analysis(state):
            captured["state"] = state
            return {**state, "retry_count": state.get("retry_count", 0) + 1}

        monkeypatch.setattr(debug_node, "run_debug_analysis", _fake_run_debug_analysis)

        state = {"retry_count": 0}
        result = asyncio.run(run(state))

        assert captured["state"] == state
        assert result["retry_count"] == 1
