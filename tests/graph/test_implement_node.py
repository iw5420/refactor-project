"""⑤ 功能改寫 Agent node 的 scaffold 失敗短路邏輯，對應
docs/01_langgraph_architecture.md 五章「scaffold 失敗時的收尾路徑」；
`blocked_reasons` 診斷欄位（純附加、不改排程放行邏輯）見
docs/09b_bug_trace.md #29。
"""
import asyncio

from graph.nodes.implement_node import run, should_run_tests_or_give_up
from translator_cli.types import FillResult


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
        #
        # 09a 三章：run() 開頭在「真正第一次進入 implement」時會呼叫
        # _ensure_python_service_started() 啟動容器化的 Python 服務——
        # 這裡 mock 掉，避免單元測試依賴 Docker。
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)

        state = {
            "scaffold_done": True,
            "module_list": [],
            "task_list": [],
            "completed_tasks": [],
            "failed_tasks": [],
            "task_failures": [],
            "skipped_interfaces": [],
            "partial_reports": [],
            "python_project_path": "/unused",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        result = asyncio.run(run(state))

        assert result["failed_modules"] == []
        assert result["blocked_modules"] == []


class TestBlockedReasons:
    def test_blocked_module_reports_which_upstream_is_not_verified(self, monkeypatch):
        # 對應 docs/09b_bug_trace.md #29：registration 這類下游 module
        # 被上游卡住時，blocked_reasons 要能直接指出是哪個上游、不需要
        # 人工反查 module_list.depends_on。這裡構造一個最小案例：
        # upstream 的唯一 task 翻譯失敗 → upstream 標記 "failed"；
        # downstream 的 task 因為 _module_deps_satisfied() 過不了，
        # 永遠不會被排入就緒佇列，停留在 "pending" → blocked_modules。
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)

        async def _fake_fill_function(**kwargs):
            return FillResult(success=False, error="翻譯失敗（測試用假失敗）", diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)

        module_list = [
            {"module": "upstream", "java_files": [], "depends_on": [], "methods": [], "summary": ""},
            {"module": "downstream", "java_files": [], "depends_on": ["upstream"], "methods": [], "summary": ""},
        ]
        task_list = [
            {
                "id": "task_000", "module": "upstream", "description": "d",
                "target_files": ["app/services/upstream_service.py"], "context": "",
                "depends_on": [], "class_name": "UpstreamService", "function_name": "do_it",
            },
            {
                "id": "task_001", "module": "downstream", "description": "d",
                "target_files": ["app/services/downstream_service.py"], "context": "",
                "depends_on": [], "class_name": "DownstreamService", "function_name": "do_it",
            },
        ]
        state = {
            "run_id": "test_run",
            "scaffold_done": True,
            "module_list": module_list,
            "task_list": task_list,
            "completed_tasks": [],
            "failed_tasks": [],
            "task_failures": [],
            "skipped_interfaces": [],
            "partial_reports": [],
            "python_project_path": "/unused",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        result = asyncio.run(run(state))

        assert result["failed_modules"] == ["upstream"]
        assert result["blocked_modules"] == ["downstream"]
        assert result["blocked_reasons"] == {"downstream": ["upstream"]}

class TestUpstreamDegradedStopsEarly:
    def test_stops_before_next_batch_when_upstream_degraded(self, monkeypatch):
        """對應 docs/09b_bug_trace.md #35：module A 的 task 成功、module B
        （跟 A 完全獨立，同一輪就緒）的 task 失敗且標記 upstream_degraded
        ——這一輪已經觸發的 A 驗證要照常做完（跟 ollama 無關），但因為
        偵測到疑似上游模型服務異常，不該再開始下一輪；module C（依賴 A，
        原本 A 驗證通過後應該在下一輪變成就緒）的 task 不該被呼叫到。"""
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)

        async def _fake_wait_for_service_reload(*args, **kwargs):
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait_for_service_reload)

        async def _fake_partial_verify(module, db, verifier):
            return {"status": "pass", "reason": None}

        monkeypatch.setattr(implement_node, "_partial_verify", _fake_partial_verify)

        called_task_ids = []

        async def _fake_fill_function(**kwargs):
            task_id = kwargs["task_id"]
            called_task_ids.append(task_id)
            if task_id == "task_a":
                return FillResult(success=True, diff="")
            if task_id == "task_b":
                return FillResult(success=False, error="連續多次傳輸層失敗", upstream_degraded=True)
            raise AssertionError(f"不該被呼叫到：{task_id}（提早停止的驗證目標）")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)

        module_list = [
            {"module": "A", "java_files": [], "depends_on": [], "methods": [], "summary": ""},
            {"module": "B", "java_files": [], "depends_on": [], "methods": [], "summary": ""},
            {"module": "C", "java_files": [], "depends_on": ["A"], "methods": [], "summary": ""},
        ]
        task_list = [
            {
                "id": "task_a", "module": "A", "description": "d",
                "target_files": ["app/services/a_service.py"], "context": "",
                "depends_on": [], "class_name": "AService", "function_name": "do_it",
            },
            {
                "id": "task_b", "module": "B", "description": "d",
                "target_files": ["app/services/b_service.py"], "context": "",
                "depends_on": [], "class_name": "BService", "function_name": "do_it",
            },
            {
                "id": "task_c", "module": "C", "description": "d",
                "target_files": ["app/services/c_service.py"], "context": "",
                "depends_on": [], "class_name": "CService", "function_name": "do_it",
            },
        ]
        state = {
            "run_id": "test_run",
            "scaffold_done": True,
            "module_list": module_list,
            "task_list": task_list,
            "completed_tasks": [],
            "failed_tasks": [],
            "task_failures": [],
            "skipped_interfaces": [],
            "partial_reports": [],
            "python_project_path": "/unused",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        result = asyncio.run(run(state))

        assert "task_c" not in called_task_ids  # 提早停止，沒有進入下一輪
        assert result["completed_tasks"] == ["task_a"]
        assert result["failed_tasks"] == ["task_b"]
        assert result["blocked_modules"] == ["C"]  # 從未被排到，停在 pending


    def test_no_blocked_modules_yields_empty_blocked_reasons(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)

        state = {
            "scaffold_done": True,
            "module_list": [],
            "task_list": [],
            "completed_tasks": [],
            "failed_tasks": [],
            "task_failures": [],
            "skipped_interfaces": [],
            "partial_reports": [],
            "python_project_path": "/unused",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        result = asyncio.run(run(state))

        assert result["blocked_reasons"] == {}
