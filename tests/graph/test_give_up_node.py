"""give_up_node.py 三種抵達路徑各自印出不同訊息，對應
docs/10a_debug_agent_architecture.md 十章「與 LangGraph 整合」
「give_up_node.py 依抵達路徑分三種訊息」。"""
import asyncio

from graph.nodes.give_up_node import run


class TestGiveUpMessageBranches:
    def test_scaffold_failure_branch(self, capsys):
        state = {
            "scaffold_done": False,
            "give_up_early": False,
            "skipped_interfaces": [],
            "skipped_db_models": [],
        }
        asyncio.run(run(state))
        out = capsys.readouterr().out
        assert "骨架生成失敗" in out

    def test_give_up_early_branch_distinct_from_retry_exhausted(self, capsys):
        state = {
            "scaffold_done": True,
            "give_up_early": True,
            "retry_count": 1,
            "debug_rounds": [{"module": "exam", "origin": "root_cause", "fixable": False}],
            "failed_modules": ["exam"],
            "blocked_modules": [],
        }
        asyncio.run(run(state))
        out = capsys.readouterr().out
        assert "⑦ Debug Agent 判斷已無可修" in out
        assert "非重試次數用盡" in out
        assert "超過重試次數" not in out

    def test_retry_exhausted_branch_when_give_up_early_false(self, capsys):
        state = {
            "scaffold_done": True,
            "give_up_early": False,
            "retry_count": 3,
            "failed_modules": ["exam"],
            "blocked_modules": [],
            "unanalyzed_root_cause_modules": [],
        }
        asyncio.run(run(state))
        out = capsys.readouterr().out
        assert "超過重試次數 (3)" in out
        assert "Debug Agent 判斷已無可修" not in out
        assert "llmlog" not in out  # 沒有未分析到的 module，不該印出這條提示

    def test_retry_exhausted_with_unanalyzed_modules_flags_possible_api_issue(self, capsys):
        """對應剛加的判斷：連續打不通 Claude API 跟 ⑦ 判斷邏輯本身有問題，
        這兩種情況目前印出的訊息一模一樣，人工要能從這裡看出「至少有一個
        module 是完全沒能呼叫到 API」這個線索，而不是自己去翻 debug_rounds。
        """
        state = {
            "scaffold_done": True,
            "give_up_early": False,
            "retry_count": 3,
            "failed_modules": ["exam", "grading"],
            "blocked_modules": [],
            "unanalyzed_root_cause_modules": ["exam"],
        }
        asyncio.run(run(state))
        out = capsys.readouterr().out
        assert "llmlog" in out
        assert "['exam']" in out
