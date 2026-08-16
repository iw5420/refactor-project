"""⑤ 功能改寫 Agent node 的 scaffold 失敗短路邏輯，對應
docs/01_langgraph_architecture.md 五章「scaffold 失敗時的收尾路徑」。
"""
import asyncio

from graph.nodes.implement_node import run, should_run_tests_or_give_up


class TestShouldRunTestsOrGiveUp:
    def test_scaffold_done_false_gives_up(self):
        assert should_run_tests_or_give_up({"scaffold_done": False}) == "give_up"

    def test_scaffold_done_true_runs_tests(self):
        assert should_run_tests_or_give_up({"scaffold_done": True}) == "run_tests"

    def test_missing_key_defaults_to_run_tests(self):
        # 舊版 state（尚未跑過 scaffold node，或測試 fixture 沒帶這個
        # 欄位）不應該被誤判成失敗——只有明確 False 才短路。
        assert should_run_tests_or_give_up({}) == "run_tests"


class TestImplementRunScaffoldFailureShortCircuit:
    def test_scaffold_failure_skips_scheduler_and_marks_all_modules_failed(self, monkeypatch):
        # translator_cli.fill_function() 不應該在這條路徑上被呼叫到——
        # 用一個一呼叫就失敗的假函式確保短路邏輯真的在排程器跑之前生效。
        import graph.nodes.implement_node as implement_node

        async def _should_not_be_called(*args, **kwargs):
            raise AssertionError("scaffold_done=False 時不應該呼叫 translator_cli.fill_function()")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _should_not_be_called)

        state = {
            "scaffold_done": False,
            "module_list": [
                {"module": "hr", "java_files": [], "depends_on": [], "methods": [], "summary": ""},
                {"module": "billing", "java_files": [], "depends_on": [], "methods": [], "summary": ""},
            ],
            "task_list": [],
            "completed_tasks": [],
            "failed_tasks": [],
            "partial_reports": [],
            "python_project_path": "/unused",
        }

        result = asyncio.run(run(state))

        assert result["failed_modules"] == ["hr", "billing"]
        assert result["blocked_modules"] == []
        assert result["completed_tasks"] == []
        assert result["failed_tasks"] == []
        assert result["partial_reports"] == []

    def test_scaffold_done_true_does_not_short_circuit(self, monkeypatch):
        # scaffold_done=True（或缺席）時維持既有排程器行為——這裡只驗證
        # 短路分支沒有被誤觸發（module_list 為空，排程器自然 all_done()
        # 立刻結束，不需要真的呼叫 translator_cli）。
        state = {
            "scaffold_done": True,
            "module_list": [],
            "task_list": [],
            "completed_tasks": [],
            "failed_tasks": [],
            "partial_reports": [],
            "python_project_path": "/unused",
        }

        result = asyncio.run(run(state))

        assert result["failed_modules"] == []
        assert result["blocked_modules"] == []
