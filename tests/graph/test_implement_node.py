"""⑤ 功能改寫 Agent node 的 scaffold 失敗短路邏輯，對應
docs/01_langgraph_architecture.md 五章「scaffold 失敗時的收尾路徑」；
`blocked_reasons` 診斷欄位（純附加、不改排程放行邏輯）見
docs/09b_bug_trace.md #29。
"""
import asyncio

import pytest

from graph.nodes.implement_node import run, should_run_tests_or_give_up
from translator_cli.exceptions import TranslatorCliUpstreamDegradedError
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


class TestEnsurePythonServiceStarted:
    """對應 docs/09b_bug_trace.md #46：把 state 裡的 java_project_path／
    python_structure.config_env_vars 正確轉傳給
    python_service_manager.ensure_started()。"""

    def test_passes_java_project_path_and_config_env_vars_through(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        captured = {}

        async def _fake_ensure_started(python_project_path, python_base_url, java_project_path=None, config_env_vars=None):
            captured["python_project_path"] = python_project_path
            captured["python_base_url"] = python_base_url
            captured["java_project_path"] = java_project_path
            captured["config_env_vars"] = config_env_vars

        monkeypatch.setattr(implement_node.python_service_manager, "ensure_started", _fake_ensure_started)

        state = {
            "python_project_path": "/py-proj",
            "python_base_url": "http://unused",
            "java_project_path": "/java-proj",
            "python_structure": {
                "directory_tree": "", "interfaces": [],
                "config_env_vars": [{"property_key": "language.code", "constant_name": "LANGUAGE_CODE"}],
            },
        }

        asyncio.run(implement_node._ensure_python_service_started(state))

        assert captured == {
            "python_project_path": "/py-proj",
            "python_base_url": "http://unused",
            "java_project_path": "/java-proj",
            "config_env_vars": [{"property_key": "language.code", "constant_name": "LANGUAGE_CODE"}],
        }

    def test_config_env_vars_none_when_python_structure_has_no_such_key(self, monkeypatch):
        """沒有任何 @Value 欄位的專案，python_structure 完全不會有
        config_env_vars 這個 key（NotRequired，見 graph/state.py）。"""
        import graph.nodes.implement_node as implement_node

        captured = {}

        async def _fake_ensure_started(python_project_path, python_base_url, java_project_path=None, config_env_vars=None):
            captured["config_env_vars"] = config_env_vars

        monkeypatch.setattr(implement_node.python_service_manager, "ensure_started", _fake_ensure_started)

        state = {
            "python_project_path": "/py-proj",
            "python_base_url": "http://unused",
            "java_project_path": "/java-proj",
            "python_structure": {"directory_tree": "", "interfaces": []},
        }

        asyncio.run(implement_node._ensure_python_service_started(state))

        assert captured["config_env_vars"] is None


class TestBlockedReasons:
    def test_blocked_module_reports_which_upstream_never_progressed(self, monkeypatch):
        # 對應 docs/09b_bug_trace.md #29：registration 這類下游 module
        # 被上游卡住時，blocked_reasons 要能直接指出是哪個上游、不需要
        # 人工反查 module_list.depends_on。

        # 對應 docs/refactor_bug_trace.md #12 修正後的語意：
        # `_module_deps_satisfied()` 只要求 upstream「開始有進展」（狀態
        # 不是 "pending"），不再要求 upstream 完整 "verified"——upstream
        # 的唯一 task 翻譯失敗也算「有進展」（`mark_task_done()` 不分
        # 成功/失敗都會把狀態從 "pending" 改成 "in_progress"），downstream
        # 因此不會再被這種情況卡住。這裡改用「upstream 的 task 帶一個永遠
        # 對不到的 task 級 depends_on」構造真正卡在 "pending"（一次都沒被
        # 排到）的情境，才是修正後唯一還會讓 downstream 進 blocked_modules
        # 的案例。
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)
        monkeypatch.setattr(implement_node.ollama_client, "check_ollama_reachable", _skip_start)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

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
                # 對到一個從沒存在過的 task_id，_task_deps_satisfied()
                # 永遠不會成立，task_000 永遠排不進去，upstream 的
                # module_status 因此永遠停在初始的 "pending"。
                "depends_on": ["task_that_never_exists"], "class_name": "UpstreamService", "function_name": "do_it",
                "java_method_id": "UpstreamService.java::UpstreamService::doIt",
                "translator_backend": "qwen", "phase": 2,
            },
            {
                "id": "task_001", "module": "downstream", "description": "d",
                "target_files": ["app/services/downstream_service.py"], "context": "",
                "depends_on": [], "class_name": "DownstreamService", "function_name": "do_it",
                "java_method_id": "DownstreamService.java::DownstreamService::doIt",
                "translator_backend": "qwen", "phase": 2,
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
            "java_project_path": "/unused-java",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        result = asyncio.run(run(state))

        assert result["blocked_modules"] == ["upstream", "downstream"]
        assert result["blocked_reasons"] == {"upstream": [], "downstream": ["upstream"]}

    def test_downstream_proceeds_once_upstream_fails_instead_of_staying_blocked(self, monkeypatch):
        # 鎖住 docs/refactor_bug_trace.md #12 修正後的實際行為：upstream
        # 的唯一 task 翻譯失敗（不是「還沒被排到」），現在 downstream
        # 不會再被 module 依賴卡住——它會被排進去正常嘗試（這裡的假
        # fill_function 一律回傳失敗，所以 downstream 也會失敗，但重點
        # 是它「有被嘗試」，不是卡在 blocked_modules 裡連機會都沒有）。
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)
        monkeypatch.setattr(implement_node.ollama_client, "check_ollama_reachable", _skip_start)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

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
                "java_method_id": "UpstreamService.java::UpstreamService::doIt",
                "translator_backend": "qwen", "phase": 2,
            },
            {
                "id": "task_001", "module": "downstream", "description": "d",
                "target_files": ["app/services/downstream_service.py"], "context": "",
                "depends_on": [], "class_name": "DownstreamService", "function_name": "do_it",
                "java_method_id": "DownstreamService.java::DownstreamService::doIt",
                "translator_backend": "qwen", "phase": 2,
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
            "java_project_path": "/unused-java",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        result = asyncio.run(run(state))

        assert result["blocked_modules"] == []
        assert set(result["failed_modules"]) == {"upstream", "downstream"}

class TestUpstreamDegradedStopsEarly:
    def test_aborts_whole_pipeline_when_upstream_degraded(self, monkeypatch):
        """對應 docs/09b_bug_trace.md #35、docs/refactor_bug_trace.md #1：
        module A 的 task 成功、module B（跟 A 完全獨立，同一輪就緒）的
        task 失敗且標記 upstream_degraded——這一輪已經觸發的 A 驗證要照常
        做完（跟 ollama 無關），但偵測到疑似上游模型服務異常之後，不是
        只停這一輪排程繼續往下走（真實案例證實這樣會讓同 module 其餘不
        受影響的 Claude 後端 task 因為 `_backfill_missing_task_deps()`
        被連坐卡住，且 debug／run_tests 繼續空轉燒 retry_count），而是直接
        中止整條 pipeline（拋出 `TranslatorCliUpstreamDegradedError`）；
        module C（依賴 A，原本 A 驗證通過後應該在下一輪變成就緒）的 task
        不該被呼叫到。"""
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)
        monkeypatch.setattr(implement_node.ollama_client, "check_ollama_reachable", _skip_start)

        async def _fake_wait_for_service_reload(*args, **kwargs):
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait_for_service_reload)

        async def _fake_partial_verify(module, db, verifier):
            return {"status": "pass", "reason": None}

        monkeypatch.setattr(implement_node, "_partial_verify", _fake_partial_verify)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

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
                "java_method_id": "AService.java::AService::doIt", "translator_backend": "qwen",
                "phase": 2,
            },
            {
                "id": "task_b", "module": "B", "description": "d",
                "target_files": ["app/services/b_service.py"], "context": "",
                "depends_on": [], "class_name": "BService", "function_name": "do_it",
                "java_method_id": "BService.java::BService::doIt", "translator_backend": "qwen",
                "phase": 2,
            },
            {
                "id": "task_c", "module": "C", "description": "d",
                "target_files": ["app/services/c_service.py"], "context": "",
                "depends_on": [], "class_name": "CService", "function_name": "do_it",
                "java_method_id": "CService.java::CService::doIt", "translator_backend": "qwen",
                "phase": 2,
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
            "java_project_path": "/unused-java",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        with pytest.raises(TranslatorCliUpstreamDegradedError):
            asyncio.run(run(state))

        assert "task_c" not in called_task_ids  # 提早停止，沒有進入下一輪


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


class TestOllamaPreflightCheck:
    """對應 docs/refactor_bug_trace.md #7：真正第一次進入 implement、且
    這次 run 確實有 qwen task 時，先確認 ollama 連得到，不用等真的排到
    task、燒完 UPSTREAM_DEGRADED_THRESHOLD 次才發現連不上。"""

    def _base_state(self, task_list):
        return {
            "run_id": "test_run",
            "scaffold_done": True,
            "module_list": [{"module": "exam", "java_files": [], "depends_on": [], "methods": [], "summary": ""}],
            "task_list": task_list,
            "completed_tasks": [],
            "failed_tasks": [],
            "task_failures": [],
            "skipped_interfaces": [],
            "partial_reports": [],
            "python_project_path": "/unused",
            "java_project_path": "/unused-java",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

    def test_aborts_before_any_task_when_ollama_unreachable(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)

        async def _fake_check_unreachable():
            raise TranslatorCliUpstreamDegradedError("連不到 ollama（測試用假失敗）")

        monkeypatch.setattr(implement_node.ollama_client, "check_ollama_reachable", _fake_check_unreachable)

        async def _fail_if_called(**kwargs):
            raise AssertionError("ollama 預檢失敗時不該呼叫到 fill_function()")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fail_if_called)

        task_list = [
            {
                "id": "task_000", "module": "exam", "description": "d",
                "target_files": ["app/repositories/exam_repository.py"], "context": "",
                "depends_on": [], "class_name": "ExamRepository", "function_name": "find_by_kind",
                "java_method_id": "ExamRepository.java::ExamRepository::findByKind",
                "translator_backend": "qwen",
            },
        ]
        with pytest.raises(TranslatorCliUpstreamDegradedError, match="連不到 ollama"):
            asyncio.run(run(self._base_state(task_list)))

    def test_skipped_when_no_qwen_task_in_this_run(self, monkeypatch):
        # 沒有 qwen task 的 run 不需要 ollama 連線，不該因為這個預檢
        # 被誤擋——即使 ollama 真的連不到，也不該影響這次 run。
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)

        async def _fail_if_called():
            raise AssertionError("沒有 qwen task 時不該呼叫 check_ollama_reachable()")

        monkeypatch.setattr(implement_node.ollama_client, "check_ollama_reachable", _fail_if_called)

        async def _fake_fill_function(**kwargs):
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

        async def _fake_wait_for_service_reload(*args, **kwargs):
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait_for_service_reload)

        async def _fake_partial_verify(module, db, verifier):
            return {"status": "pass", "reason": None}

        monkeypatch.setattr(implement_node, "_partial_verify", _fake_partial_verify)

        task_list = [
            {
                "id": "task_000", "module": "exam", "description": "d",
                "target_files": ["app/routers/exam_router.py"], "context": "",
                "depends_on": [], "class_name": None, "function_name": "get_all",
                "java_method_id": "ExamController.java::ExamController::getAll",
                "translator_backend": "claude",
            },
        ]
        result = asyncio.run(run(self._base_state(task_list)))
        assert result["completed_tasks"] == ["task_000"]


class TestDebugReentryPreservesFailedModuleStatus:
    """對應 docs/09b_bug_trace.md #41：module 底下所有 task 都已成功
    （translator_cli 呼叫本身沒問題），但功能驗證（_partial_verify）沒
    通過——這種 module 沒有剩餘 task 可以重排，debug → implement 重入時
    若 scheduler 沒有明確恢復它的 "failed" 狀態，會預設落回 "pending"，
    被誤判成等上游修好就會自然釋放的 blocked_modules，讓 failed_modules
    變空、retry_count 停止遞增、debug ↔ implement 迴圈不會終止。"""

    def test_second_run_still_reports_failed_not_blocked(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)
        monkeypatch.setattr(implement_node.ollama_client, "check_ollama_reachable", _skip_start)

        async def _fake_wait_for_service_reload(*args, **kwargs):
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait_for_service_reload)

        async def _fake_partial_verify(module, db, verifier):
            return {"status": "fail", "reason": "response_mismatch"}

        monkeypatch.setattr(implement_node, "_partial_verify", _fake_partial_verify)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

        async def _fake_fill_function(**kwargs):
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)

        module_list = [
            {"module": "flaky", "java_files": [], "depends_on": [], "methods": [], "summary": ""},
        ]
        task_list = [
            {
                "id": "task_x", "module": "flaky", "description": "d",
                "target_files": ["app/services/flaky_service.py"], "context": "",
                "depends_on": [], "class_name": "FlakyService", "function_name": "do_it",
                "java_method_id": "FlakyService.java::FlakyService::doIt", "translator_backend": "qwen",
            },
        ]
        base_state = {
            "run_id": "test_run",
            "scaffold_done": True,
            "module_list": module_list,
            "task_list": task_list,
            "task_failures": [],
            "skipped_interfaces": [],
            "python_project_path": "/unused",
            "java_project_path": "/unused-java",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        first = asyncio.run(run({**base_state, "completed_tasks": [], "failed_tasks": [], "partial_reports": []}))
        assert first["failed_modules"] == ["flaky"]
        assert first["blocked_modules"] == []

        # 模擬 debug → implement 重入：completed_tasks／partial_reports 帶著
        # 上一輪的結果回來（跟 operator.add 累加後的真實 state 一致）。
        second = asyncio.run(run({
            **base_state,
            "completed_tasks": first["completed_tasks"],
            "failed_tasks": first["failed_tasks"],
            "partial_reports": first["partial_reports"],
        }))
        assert second["failed_modules"] == ["flaky"]
        assert second["blocked_modules"] == []


class TestRunOneTaskReferencedFunctions:
    """對應 06a 七章新設計：TaskSpec.referenced_functions 要正確轉成
    fill_function() 期待的 (file_path, class_name, function_name) 三元組
    清單，見 docs/09b_bug_trace.md #37。"""

    def test_referenced_functions_converted_to_tuples(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        captured = {}

        async def fake_fill_function(**kwargs):
            captured.update(kwargs)
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", fake_fill_function)
        # 不測 java_source／referenced_source 真正的檔案解析（那是
        # graph/java_source_extraction.py 自己的職責），這裡用簡單的假
        # 實作換掉，只驗證 referenced_functions 轉換本身。
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

        task = {
            "id": "task_045", "module": "exam", "description": "d",
            "target_files": ["app/services/exam_service.py"], "context": "",
            "depends_on": [], "class_name": "ExamService", "function_name": "create_random",
            "java_method_id": "ExamService.java::ExamService::createRandom",
            "translator_backend": "qwen",
            "referenced_functions": [
                {"file_path": "app/repositories/exam_repository.py", "class_name": "ExamRepository", "function_name": "find_by_card"},
                {"file_path": "app/routers/exam_router.py", "class_name": None, "function_name": "search"},
            ],
        }

        asyncio.run(implement_node._run_one_task(task, "/unused", "/unused-java", "run_x", {}))

        assert captured["referenced_functions"] == [
            ("app/repositories/exam_repository.py", "ExamRepository", "find_by_card"),
            ("app/routers/exam_router.py", None, "search"),
        ]

    def test_missing_referenced_functions_key_defaults_to_empty_list(self, monkeypatch):
        # 舊資料／測試 fixture 沒帶這個欄位時（NotRequired）不該炸掉。
        import graph.nodes.implement_node as implement_node

        captured = {}

        async def fake_fill_function(**kwargs):
            captured.update(kwargs)
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", fake_fill_function)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

        task = {
            "id": "task_001", "module": "user", "description": "d",
            "target_files": ["app/services/user_service.py"], "context": "",
            "depends_on": [], "class_name": "UserService", "function_name": "get_user",
            "java_method_id": "UserService.java::UserService::getUser",
            "translator_backend": "qwen",
        }

        asyncio.run(implement_node._run_one_task(task, "/unused", "/unused-java", "run_x", {}))

        assert captured["referenced_functions"] == []


class TestRunOneTaskJavaSourceResolution:
    """本輪修訂（call-chain-implement 重構）：`_run_one_task()` 在呼叫
    `fill_function()` 之前，自己負責把 `task["java_method_id"]`／
    `task["reference_targets"]` 解析成真正的原始碼文字（見
    `graph/java_source_extraction.py` 模組 docstring、07a 五章）。
    `fixed_body is None` 時才需要這道解析——`fixed_body` 給定時（⑦ 已經
    給出修正後的完整函式本體）完全跳過，見 `_run_one_task()` docstring。
    """

    def test_resolvers_called_with_right_args_and_results_flow_into_fill_function(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        resolve_java_source_calls = []
        resolve_referenced_source_calls = []

        def fake_resolve_java_source(java_project_path, java_method_id):
            resolve_java_source_calls.append((java_project_path, java_method_id))
            return "public int createRandom() { ... }"

        def fake_resolve_referenced_source(java_project_path, python_project_path, targets):
            resolve_referenced_source_calls.append((java_project_path, python_project_path, targets))
            return [
                {
                    "file_path": "app/repositories/exam_repository.py", "class_name": "ExamRepository",
                    "function_name": "find_by_card", "language": "java", "source": "public Exam findByCard(String card) { ... }",
                }
            ]

        monkeypatch.setattr(implement_node, "resolve_java_source", fake_resolve_java_source)
        monkeypatch.setattr(implement_node, "resolve_referenced_source", fake_resolve_referenced_source)

        captured = {}

        async def fake_fill_function(**kwargs):
            captured.update(kwargs)
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", fake_fill_function)

        reference_targets = [
            {"file_path": "app/repositories/exam_repository.py", "class_name": "ExamRepository", "function_name": "find_by_card", "language": "java"},
        ]
        task = {
            "id": "task_045", "module": "exam", "description": "d",
            "target_files": ["app/services/exam_service.py"], "context": "",
            "depends_on": [], "class_name": "ExamService", "function_name": "create_random",
            "java_method_id": "ExamService.java::ExamService::createRandom",
            "translator_backend": "claude",
            "reference_targets": reference_targets,
        }

        asyncio.run(implement_node._run_one_task(task, "/py-proj", "/java-proj", "run_x", {}))

        assert resolve_java_source_calls == [("/java-proj", "ExamService.java::ExamService::createRandom")]
        assert resolve_referenced_source_calls == [("/java-proj", "/py-proj", reference_targets)]
        assert captured["java_source"] == "public int createRandom() { ... }"
        assert captured["referenced_source"] == [
            {
                "file_path": "app/repositories/exam_repository.py", "class_name": "ExamRepository",
                "function_name": "find_by_card", "language": "java", "source": "public Exam findByCard(String card) { ... }",
            }
        ]
        assert captured["translator_backend"] == "claude"

    def test_resolvers_not_called_when_fixed_body_present(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        async def _should_not_be_called(*args, **kwargs):
            raise AssertionError("fixed_body 給定時不該呼叫 resolve_java_source()／resolve_referenced_source()")

        monkeypatch.setattr(implement_node, "resolve_java_source", _should_not_be_called)
        monkeypatch.setattr(implement_node, "resolve_referenced_source", _should_not_be_called)

        captured = {}

        async def fake_fill_function(**kwargs):
            captured.update(kwargs)
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", fake_fill_function)

        task = {
            "id": "task_045", "module": "exam", "description": "d",
            "target_files": ["app/services/exam_service.py"], "context": "",
            "depends_on": [], "class_name": "ExamService", "function_name": "create_random",
            "translator_backend": "qwen",
            # 故意不帶 java_method_id：fixed_body 給定時完全跳過解析，
            # 這個 task 不需要它也能正確跑，若 resolve_java_source() 被
            # 誤呼叫，上面的 _should_not_be_called 會直接讓測試失敗。
        }

        asyncio.run(implement_node._run_one_task(
            task, "/py-proj", "/java-proj", "run_x", {"task_045": "return rs\n"}
        ))

        assert captured["java_source"] == ""
        assert captured["referenced_source"] == []
        assert captured["fixed_body"] == "return rs\n"


class TestAugmentTaskIo:
    """`_augment_task_io()` 只剩 09a 五章「疊加規則」這一件事——⑦ Debug
    Agent 給的修正不再疊加進 context（見 10a 八章「⑦ 直接產生修正後
    程式碼」），改由 `_run_one_task()` 直接傳給 `fill_function()` 的
    `fixed_body`。"""

    def test_routers_layer_task_context_files_unchanged(self):
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_r1", "module": "exam", "description": "d",
            "target_files": ["app/routers/exam_router.py"], "context": "既有描述",
            "depends_on": [], "class_name": None, "function_name": "search",
        }
        original_target_files = list(task["target_files"])

        context, context_files = implement_node._augment_task_io(task)

        assert context == (
            f"既有描述\n\n{implement_node._RESULT_FACTORY_INSTANCE_METHOD_NOTICE}"
            f"\n\n{implement_node._RESULT_FACTORY_NOT_BYPASSED_NOTICE}"
            f"\n\n{implement_node._SPECIFICATION_PATTERN_NOTICE}"
            f"\n\n{implement_node._MANUAL_DB_SESSION_NOTICE}"
            f"\n\n{implement_node._REPOSITORY_SERVICE_INSTANTIATION_NOTICE}"
            f"\n\n{implement_node._SIMILAR_REPOSITORY_DISAMBIGUATION_NOTICE}"
            f"\n\n{implement_node._ENTITY_ATTRIBUTE_NAMING_NOTICE}"
            f"\n\n{implement_node._SQLALCHEMY_CONDITION_FILTERING_NOTICE}"
            f"\n\n{implement_node._ENTITY_TO_SCHEMA_FIELD_COMPLETENESS_NOTICE}"
            f"\n\n{implement_node._IMPORT_BEFORE_USE_NOTICE}"
        )
        assert context_files == [*original_target_files, implement_node._COMMON_SERVICE_FILE]
        assert task["target_files"] == original_target_files  # 不修改 task 本身

    def test_services_layer_task_gets_relationship_notice(self):
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_s1", "module": "exam", "description": "d",
            "target_files": ["app/services/exam_service.py"], "context": "",
            "depends_on": [], "class_name": "ExamService", "function_name": "create",
        }

        context, context_files = implement_node._augment_task_io(task)

        assert implement_node._RELATIONSHIP_GAP_NOTICE in context
        assert implement_node._ENUMS_FILE in context_files
        assert implement_node._COMMON_SERVICE_FILE in context_files

    def test_common_service_file_applied_to_every_layer(self):
        """對應 docs/09b_bug_trace.md：`app/core/exception_handlers.py`
        （`_global` 模組，不落在 services／repositories 層）這個 session
        三次完整 pipeline 重跑都用錯 ResponseResult.error() 的關鍵字參數
        名稱——根源是 context_files 從來沒有真的帶上 common_service.py
        本身，⑤ 只看得到文字提示、看不到真實原始碼可以核對確切的參數
        名稱。跟 _RESULT_FACTORY_INSTANCE_METHOD_NOTICE 一樣，不看
        target_file 落在哪一層，任何 task 都無條件疊加。"""
        import graph.nodes.implement_node as implement_node

        for target_file in (
            "app/routers/exam_router.py",
            "app/services/exam_service.py",
            "app/repositories/exam_repository.py",
            "app/core/exception_handlers.py",
        ):
            task = {
                "id": "task_x", "module": "exam", "description": "d",
                "target_files": [target_file], "context": "",
                "depends_on": [], "class_name": None, "function_name": "f",
            }
            _, context_files = implement_node._augment_task_io(task)
            assert implement_node._COMMON_SERVICE_FILE in context_files

    def test_common_service_file_not_duplicated_when_it_is_the_target_itself(self):
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_cs", "module": "common", "description": "d",
            "target_files": [implement_node._COMMON_SERVICE_FILE], "context": "",
            "depends_on": [], "class_name": "ResponseResult", "function_name": "error",
        }

        _, context_files = implement_node._augment_task_io(task)

        assert context_files.count(implement_node._COMMON_SERVICE_FILE) == 1

    def test_result_factory_instance_method_notice_applied_to_every_layer(self):
        """對應 docs/09b_bug_trace.md「反覆出現的翻譯模式錯誤」：跟只在
        services／repositories 層才加的 relationship 提示不同，任何層級
        的 task 都可能建構 ResponseResult／Result 回應，routers／
        services／repositories 三層都要無條件疊加這則提示。"""
        import graph.nodes.implement_node as implement_node

        for target_file in (
            "app/routers/exam_router.py",
            "app/services/exam_service.py",
            "app/repositories/exam_repository.py",
        ):
            task = {
                "id": "task_x", "module": "exam", "description": "d",
                "target_files": [target_file], "context": "",
                "depends_on": [], "class_name": None, "function_name": "f",
            }
            context, _ = implement_node._augment_task_io(task)
            assert implement_node._RESULT_FACTORY_INSTANCE_METHOD_NOTICE in context

    def test_result_factory_not_bypassed_notice_applied_to_every_layer(self):
        """對應 docs/refactor_bug_trace.md #39：真實案例 `exam_router.py::
        search()`／`save_answer()` 成功分支手寫 `code=200, msg="ok"`／
        `"success"`，繞過 `ResponseResult().ok_2(data)` 工廠方法——查真實
        Java 原始碼確認兩者都只是 `ResponseResult.ok(data)`，從未自訂
        訊息文字，`msg` 應該用 `ok()` 內建的預設值「操作成功」。任何
        層級的 task 都可能組成功回應，無條件疊加。"""
        import graph.nodes.implement_node as implement_node

        for target_file in (
            "app/routers/exam_router.py",
            "app/services/exam_service.py",
            "app/repositories/exam_repository.py",
        ):
            task = {
                "id": "task_x", "module": "exam", "description": "d",
                "target_files": [target_file], "context": "",
                "depends_on": [], "class_name": None, "function_name": "f",
            }
            context, _ = implement_node._augment_task_io(task)
            assert implement_node._RESULT_FACTORY_NOT_BYPASSED_NOTICE in context

    def test_result_factory_not_bypassed_notice_forbids_literal_msg(self):
        import graph.nodes.implement_node as implement_node

        notice = implement_node._RESULT_FACTORY_NOT_BYPASSED_NOTICE
        assert "ok_2(data)" in notice
        assert "#39" in notice

    def test_fixed_notices_are_the_only_content_when_context_empty(self):
        # 沒有既有 context 時，疊加的固定提示（RESULT_FACTORY／
        # SPECIFICATION 兩則，routers 層不會加 RELATIONSHIP_GAP）應該就是
        # 完整內容，不該因為串接邏輯留下多餘的空字串/換行。
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_r2", "module": "exam", "description": "d",
            "target_files": ["app/routers/exam_router.py"], "context": "",
            "depends_on": [], "class_name": None, "function_name": "search",
        }

        context, _ = implement_node._augment_task_io(task)

        assert context == (
            f"{implement_node._RESULT_FACTORY_INSTANCE_METHOD_NOTICE}"
            f"\n\n{implement_node._RESULT_FACTORY_NOT_BYPASSED_NOTICE}"
            f"\n\n{implement_node._SPECIFICATION_PATTERN_NOTICE}"
            f"\n\n{implement_node._MANUAL_DB_SESSION_NOTICE}"
            f"\n\n{implement_node._REPOSITORY_SERVICE_INSTANTIATION_NOTICE}"
            f"\n\n{implement_node._SIMILAR_REPOSITORY_DISAMBIGUATION_NOTICE}"
            f"\n\n{implement_node._ENTITY_ATTRIBUTE_NAMING_NOTICE}"
            f"\n\n{implement_node._SQLALCHEMY_CONDITION_FILTERING_NOTICE}"
            f"\n\n{implement_node._ENTITY_TO_SCHEMA_FIELD_COMPLETENESS_NOTICE}"
            f"\n\n{implement_node._IMPORT_BEFORE_USE_NOTICE}"
        )

    def test_specification_pattern_notice_applied_to_every_layer(self):
        """對應 docs/refactor_bug_trace.md #14：Specification<T> 動態查詢
        pattern 的定義方（repository 層）跟組合呼叫方（router／service
        層）都可能是任何 task，比照 _RESULT_FACTORY_INSTANCE_METHOD_NOTICE
        無條件疊加，不看 target_file 落在哪一層。"""
        import graph.nodes.implement_node as implement_node

        for target_file in (
            "app/routers/exam_router.py",
            "app/services/exam_service.py",
            "app/repositories/exam_repository.py",
        ):
            task = {
                "id": "task_x", "module": "exam", "description": "d",
                "target_files": [target_file], "context": "",
                "depends_on": [], "class_name": None, "function_name": "f",
            }
            context, _ = implement_node._augment_task_io(task)
            assert implement_node._SPECIFICATION_PATTERN_NOTICE in context

    def test_notice_warns_against_recursive_self_call_in_factory_method_bodies(self):
        """對應 docs/09b_bug_trace.md #53 真實案例：⑤ 把 ResponseResult.ok_2()
        自己的方法本體寫成 `return ResponseResult().ok_2(data)`——呼叫自己，
        無窮遞迴，任何呼叫端都會撞 RecursionError。這條提醒是既有
        _RESULT_FACTORY_INSTANCE_METHOD_NOTICE 的延伸（同一個 class、同一個
        「怎麼正確用 ResponseResult／Result」主題），不是獨立新常數。"""
        import graph.nodes.implement_node as implement_node

        notice = implement_node._RESULT_FACTORY_INSTANCE_METHOD_NOTICE
        assert "無窮遞迴" in notice
        assert "#53" in notice

    def test_manual_db_session_notice_applied_to_every_layer(self):
        """對應 docs/refactor_bug_trace.md #22 真實案例：`CandidateController.
        getCandidate()`／`createRandom()` 被③判定「不需要 db」，但本體邏輯
        呼叫的 service 方法需要 db，⑤ 只能自己想辦法生一個 session，真實
        重跑抓到 `db = next(get_db())` 這種永遠不會被關閉、洩漏連線的寫法
        （本機重現：打一次 `/api/candidate/search` 就會在 `pg_stat_activity`
        留下一條永遠不會消失的 idle in transaction 連線）。這個「③判斷
        跟下游需求不一致」的落差不保證只發生在 router 層，比照
        _RESULT_FACTORY_INSTANCE_METHOD_NOTICE 無條件疊加。"""
        import graph.nodes.implement_node as implement_node

        for target_file in (
            "app/routers/exam_router.py",
            "app/services/exam_service.py",
            "app/repositories/exam_repository.py",
        ):
            task = {
                "id": "task_x", "module": "exam", "description": "d",
                "target_files": [target_file], "context": "",
                "depends_on": [], "class_name": None, "function_name": "f",
            }
            context, _ = implement_node._augment_task_io(task)
            assert implement_node._MANUAL_DB_SESSION_NOTICE in context

    def test_manual_db_session_notice_forbids_next_get_db_and_pins_correct_import(self):
        import graph.nodes.implement_node as implement_node

        notice = implement_node._MANUAL_DB_SESSION_NOTICE
        assert "next(get_db())" in notice
        assert "app.core.database" in notice
        assert "#22" in notice

    def test_repository_service_instantiation_notice_applied_to_every_layer(self):
        """對應 docs/refactor_bug_trace.md #18 真實案例：retranslate 修好
        `exam_router.py::get_all()` 的 import 路徑跟缺 db 參數這兩個問題，
        卻在重寫過程中把 `ExamkindRepository.find_all(db)` 寫成直接對類別
        名稱呼叫（沒有先建立實例），`AttributeError`——Java `@Autowired`
        欄位在 Spring 端已經是現成實例，Python 沒有對應 DI 容器，任何
        repository／service 類別呼叫前都要先建立實例，不限於
        ResponseResult／Result（那個既有規則見
        _RESULT_FACTORY_INSTANCE_METHOD_NOTICE，範圍太窄）。比照既有規則
        無條件疊加，不看 target_file 落在哪一層。"""
        import graph.nodes.implement_node as implement_node

        for target_file in (
            "app/routers/exam_router.py",
            "app/services/exam_service.py",
            "app/repositories/exam_repository.py",
        ):
            task = {
                "id": "task_x", "module": "exam", "description": "d",
                "target_files": [target_file], "context": "",
                "depends_on": [], "class_name": None, "function_name": "f",
            }
            context, _ = implement_node._augment_task_io(task)
            assert implement_node._REPOSITORY_SERVICE_INSTANTIATION_NOTICE in context

    def test_repository_service_instantiation_notice_pins_correct_pattern(self):
        import graph.nodes.implement_node as implement_node

        notice = implement_node._REPOSITORY_SERVICE_INSTANTIATION_NOTICE
        assert "ExamRepository()" in notice
        assert "#18" in notice

    def test_similar_repository_disambiguation_notice_applied_to_every_layer(self):
        """對應 docs/refactor_bug_trace.md #44：真實案例
        `exam_router.py::get_all()`／`get_all_exam_kind()`，Java 原始碼
        裡 `ExamController` 同時宣告了 `examRepository`／
        `examkindRepository` 兩個命名相似的欄位，這兩個函式自己的原始碼
        本體都明確呼叫 `examkindRepository.findAll()`，⑤ 卻翻成
        `ExamRepository()`。用 llmlog 核對過真實 prompt，正確答案就在
        這個函式自己的 Java 方法本體裡，不是 context 缺口——任何層級的
        task 都可能呼叫到宣告了多個相似命名 repository／service 的 Java
        class，無條件疊加。"""
        import graph.nodes.implement_node as implement_node

        for target_file in (
            "app/routers/exam_router.py",
            "app/services/exam_service.py",
            "app/repositories/exam_repository.py",
        ):
            task = {
                "id": "task_x", "module": "exam", "description": "d",
                "target_files": [target_file], "context": "",
                "depends_on": [], "class_name": None, "function_name": "f",
            }
            context, _ = implement_node._augment_task_io(task)
            assert implement_node._SIMILAR_REPOSITORY_DISAMBIGUATION_NOTICE in context

    def test_similar_repository_disambiguation_notice_requires_rechecking_own_source(self):
        import graph.nodes.implement_node as implement_node

        notice = implement_node._SIMILAR_REPOSITORY_DISAMBIGUATION_NOTICE
        assert "examkindRepository" in notice
        assert "#44" in notice

    def test_entity_attribute_naming_notice_applied_to_every_layer(self):
        """對應 docs/refactor_bug_trace.md #24 真實案例：`exam_router.py::
        get_single_exam()`／`get_all()` 把 `ExamkindRepository.
        find_by_kind()` 回傳的 ORM `ExamkindEntity` 實例轉成同名的
        Pydantic `ExamkindEntity` schema 物件時，讀取來源欄位也用了
        schema 的 camelCase 命名（`e.typeNumber`），`AttributeError`
        （ORM 實例實際屬性是 snake_case 的 `type_number`）。用 llmlog
        核對真實 prompt 確認根因：reference_targets 只給了 repository
        方法本身的內容，從未展示過 entity 模型類別自己的欄位宣告，
        `search()` 剛好沒踩到是因為它額外呼叫 `ExamSpecification.
        with_X()`（#14）意外帶出了真正的 snake_case 屬性名稱——這不是
        隨機的翻譯品質問題，是可重現的 context 落差，任何層級的 task
        都可能把 repository 回傳的 ORM entity 轉成 schema 物件，比照
        既有規則無條件疊加。"""
        import graph.nodes.implement_node as implement_node

        for target_file in (
            "app/routers/exam_router.py",
            "app/services/exam_service.py",
            "app/repositories/exam_repository.py",
        ):
            task = {
                "id": "task_x", "module": "exam", "description": "d",
                "target_files": [target_file], "context": "",
                "depends_on": [], "class_name": None, "function_name": "f",
            }
            context, _ = implement_node._augment_task_io(task)
            assert implement_node._ENTITY_ATTRIBUTE_NAMING_NOTICE in context

    def test_entity_attribute_naming_notice_pins_snake_case_convention(self):
        import graph.nodes.implement_node as implement_node

        notice = implement_node._ENTITY_ATTRIBUTE_NAMING_NOTICE
        assert "snake_case" in notice
        assert "type_number" in notice
        assert "#24" in notice

    def test_sqlalchemy_condition_filtering_notice_applied_to_every_layer(self):
        """對應 docs/refactor_bug_trace.md #26 真實案例：
        `SchoolRepository.find_by_grade_and_local()` 用 `list(filter(None,
        conditions))` 想篩掉不適用的 SQLAlchemy 條件，但 SQLAlchemy 的
        `BinaryExpression` 在 `bool()` 底下恆為 `False`（真實驗證：
        `bool(SchoolEntity.grade == "國小")` 回傳 `False`），兩個條件因此
        全部被濾掉，查詢變成沒有任何 WHERE 條件、回傳整張表——真實測試
        帶 `grade=國小` 卻在結果裡混進國中／高中，100% 可重現。跟 #14 的
        `_SPECIFICATION_PATTERN_NOTICE` 是相關但不同的情境：這個函式不是
        走 Specification pattern，是 repository 方法直接內聯組條件，
        任何層級組動態 SQLAlchemy 篩選條件都可能踩到，無條件疊加。"""
        import graph.nodes.implement_node as implement_node

        for target_file in (
            "app/routers/exam_router.py",
            "app/services/exam_service.py",
            "app/repositories/exam_repository.py",
        ):
            task = {
                "id": "task_x", "module": "exam", "description": "d",
                "target_files": [target_file], "context": "",
                "depends_on": [], "class_name": None, "function_name": "f",
            }
            context, _ = implement_node._augment_task_io(task)
            assert implement_node._SQLALCHEMY_CONDITION_FILTERING_NOTICE in context

    def test_sqlalchemy_condition_filtering_notice_forbids_filter_none_pattern(self):
        import graph.nodes.implement_node as implement_node

        notice = implement_node._SQLALCHEMY_CONDITION_FILTERING_NOTICE
        assert "filter(None, conditions)" in notice
        assert "is not None" in notice
        assert "#26" in notice

    def test_entity_to_schema_field_completeness_notice_applied_to_every_layer(self):
        """對應 docs/refactor_bug_trace.md #32：真實案例
        `exam_router.py::get_all()`／`get_single_exam()` 手動逐欄位把
        ORM `ExamkindEntity` 轉成同名 Pydantic schema 物件時，兩個獨立
        生成的函式同時漏掉一模一樣的 4 個欄位（created／updated／
        long_question／text）——schema 有 19 個欄位，⑤手動列舉時只挑了
        看起來跟業務邏輯直接相關的欄位，遺漏的欄位用 Pydantic 預設值
        靜靜通過。任何層級的 task 都可能做這種 entity→schema 手動轉換，
        不限於 #32 真實案例踩到的 routers 層，無條件疊加。"""
        import graph.nodes.implement_node as implement_node

        for target_file in (
            "app/routers/exam_router.py",
            "app/services/exam_service.py",
            "app/repositories/exam_repository.py",
        ):
            task = {
                "id": "task_x", "module": "exam", "description": "d",
                "target_files": [target_file], "context": "",
                "depends_on": [], "class_name": None, "function_name": "f",
            }
            context, _ = implement_node._augment_task_io(task)
            assert implement_node._ENTITY_TO_SCHEMA_FIELD_COMPLETENESS_NOTICE in context

    def test_entity_to_schema_field_completeness_notice_requires_every_field(self):
        import graph.nodes.implement_node as implement_node

        notice = implement_node._ENTITY_TO_SCHEMA_FIELD_COMPLETENESS_NOTICE
        assert "每一個欄位" in notice
        assert "#32" in notice

    def test_entity_to_schema_field_completeness_notice_forbids_getattr_backfill_for_explicit_setters(self):
        """對應 docs/refactor_bug_trace.md #36：#32 原本的措辭（必須填滿
        schema 每個欄位）沒有限定適用範圍，真實重跑證實它會讓 ⑤ 對
        `analyze_router.py::analyze()` 這種 Java 端明確逐一 setXxx() 賦值
        的情境，也用 getattr(entity, "field", None) 硬填 Java 從未設定過
        的欄位，把故意留 null 的欄位翻成有值。提示補上第二支規則：Java
        逐一明確 setXxx() 賦值時，只能填 Java 真的呼叫過 setter 的欄位。"""
        import graph.nodes.implement_node as implement_node

        notice = implement_node._ENTITY_TO_SCHEMA_FIELD_COMPLETENESS_NOTICE
        assert "setXxx" in notice
        assert "getattr" in notice
        assert "#36" in notice

    def test_import_before_use_notice_applied_to_every_layer(self):
        """對應 docs/refactor_bug_trace.md #43：真實案例
        `exam_router.py::save_answer()`，⑤ 把 `is_null_or_empty()` 的
        import 寫在同一個 if 區塊內部、判斷式呼叫之後，執行到判斷式當下
        這個名稱根本還沒被 import，直接 NameError。用 `llmlog` 核對過
        真實 prompt，確認系統／使用者提示完全沒有規範 import 該放哪裡，
        是真正的提示缺口。任何層級的 task 都可能在本體邏輯裡用到延遲
        import 的名稱，無條件疊加。"""
        import graph.nodes.implement_node as implement_node

        for target_file in (
            "app/routers/exam_router.py",
            "app/services/exam_service.py",
            "app/repositories/exam_repository.py",
        ):
            task = {
                "id": "task_x", "module": "exam", "description": "d",
                "target_files": [target_file], "context": "",
                "depends_on": [], "class_name": None, "function_name": "f",
            }
            context, _ = implement_node._augment_task_io(task)
            assert implement_node._IMPORT_BEFORE_USE_NOTICE in context

    def test_import_before_use_notice_requires_import_before_first_use(self):
        import graph.nodes.implement_node as implement_node

        notice = implement_node._IMPORT_BEFORE_USE_NOTICE
        assert "函式最前面" in notice
        assert "NameError" in notice
        assert "#43" in notice

    def test_file_router_task_gets_hardcoded_upload_path_notice(self):
        """對應 docs/09b_bug_trace.md 新增條目：file_router.py 的 voice／
        image 上傳下載方法 Java 端寫死 Windows 磁碟機代號絕對路徑，⑤
        逐字翻譯後在 Linux 容器內把 "C:" 當成一般目錄名稱創出來，弄髒
        git 工作目錄、拖垮同一輪後續所有寫回動作。只限定這一個目標檔案
        才疊加。"""
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_f1", "module": "file", "description": "d",
            "target_files": ["app/routers/file_router.py"], "context": "",
            "depends_on": [], "class_name": None, "function_name": "voice",
        }

        context, _ = implement_node._augment_task_io(task)

        assert implement_node._HARDCODED_UPLOAD_PATH_NOTICE in context
        assert "/srv/uploads/voice" in context
        assert "/srv/uploads/images" in context

    def test_hardcoded_upload_path_notice_forbids_config_import(self):
        """對應 docs/refactor_bug_trace.md #30：這則提示原本要⑤改用
        app.core.config 常數 import（VOICE_UPLOAD_DIR／IMAGE_UPLOAD_DIR），
        但 pipeline 從沒有任何步驟真的建立這兩個常數，⑤忠實照做後 import
        在執行期直接 ImportError，voice／voice_2／image 三個端點全部因此
        500。改成直接內嵌容器內的絕對路徑字面字串，不再要求任何 import。"""
        import graph.nodes.implement_node as implement_node

        notice = implement_node._HARDCODED_UPLOAD_PATH_NOTICE
        assert "也不要 import 任何 app.core.config 常數" in notice
        assert "/srv/uploads/voice" in notice
        assert "/srv/uploads/images" in notice
        assert "#30" in notice

    def test_other_router_tasks_do_not_get_hardcoded_upload_path_notice(self):
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_r3", "module": "exam", "description": "d",
            "target_files": ["app/routers/exam_router.py"], "context": "",
            "depends_on": [], "class_name": None, "function_name": "search",
        }

        context, _ = implement_node._augment_task_io(task)

        assert implement_node._HARDCODED_UPLOAD_PATH_NOTICE not in context

    def test_task_with_response_return_type_gets_raw_response_error_json_notice(self, caplog):
        """對應 docs/refactor_bug_trace.md #46：真實案例 `file_router.py::
        voice_2()`／`image()`（GET 版本）全部錯誤分支都寫成 `Response(
        content=str(err.msg), status_code=XXX)`，回應不是 JSON。這則提示
        只在 `task["return_type"] == "Response"` 時才疊加——這是③在原始
        Java 簽名是 ResponseEntity<...> 時機械覆寫出的因果訊號（見 #30），
        不是看 target_file 這種巧合相關的替代訊號，所以刻意用一個非
        file_router.py 的路徑驗證，確認判斷依據真的是 return_type。"""
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_resp", "module": "file", "description": "d",
            "target_files": ["app/routers/other_router.py"], "context": "",
            "depends_on": [], "class_name": None, "function_name": "voice_2",
            "return_type": "Response",
        }

        with caplog.at_level("INFO"):
            context, _ = implement_node._augment_task_io(task)

        assert implement_node._RAW_RESPONSE_ERROR_JSON_NOTICE in context
        assert "task_resp" in caplog.text
        assert "#46" in caplog.text

    def test_task_with_non_response_return_type_does_not_get_notice(self):
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_r4", "module": "exam", "description": "d",
            "target_files": ["app/routers/exam_router.py"], "context": "",
            "depends_on": [], "class_name": None, "function_name": "search",
            "return_type": "ResponseResultString",
        }

        context, _ = implement_node._augment_task_io(task)

        assert implement_node._RAW_RESPONSE_ERROR_JSON_NOTICE not in context

    def test_task_without_return_type_key_does_not_get_notice(self):
        # 既有建構 TaskSpec 的地方（測試 fixture 等）不需要跟著補這個
        # 欄位——缺席時安全地不觸發，不因為新增這個判斷就整批出錯。
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_r5", "module": "file", "description": "d",
            "target_files": ["app/routers/file_router.py"], "context": "",
            "depends_on": [], "class_name": None, "function_name": "voice",
        }

        context, _ = implement_node._augment_task_io(task)

        assert implement_node._RAW_RESPONSE_ERROR_JSON_NOTICE not in context

    def test_raw_response_error_json_notice_requires_json_response_construction(self):
        import graph.nodes.implement_node as implement_node

        notice = implement_node._RAW_RESPONSE_ERROR_JSON_NOTICE
        assert "JSONResponse" in notice
        assert "err.code" in notice
        assert "#46" in notice

    def test_global_exception_handler_task_gets_status_notice(self):
        """對應 docs/refactor_bug_trace.md #34：Java 端 GlobalExceptionHandler
        的 handleAll(Exception e) 直接回傳一般物件、無 @ResponseStatus，
        Spring 預設 HTTP 200，⑤ 卻直覺把「例外處理」寫成 HTTP 500，
        跟真實 golden fixture（HTTP 200，錯誤碼放 body）對不上。只限定
        這一個目標檔案才疊加。"""
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_g1", "module": "common", "description": "d",
            "target_files": ["app/core/exception_handlers.py"], "context": "",
            "depends_on": [], "class_name": None, "function_name": "handle_all",
        }

        context, _ = implement_node._augment_task_io(task)

        assert implement_node._GLOBAL_EXCEPTION_HANDLER_STATUS_NOTICE in context

    def test_other_files_do_not_get_global_exception_handler_status_notice(self):
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_r4", "module": "exam", "description": "d",
            "target_files": ["app/routers/exam_router.py"], "context": "",
            "depends_on": [], "class_name": None, "function_name": "search",
        }

        context, _ = implement_node._augment_task_io(task)

        assert implement_node._GLOBAL_EXCEPTION_HANDLER_STATUS_NOTICE not in context

    def test_global_exception_handler_status_notice_requires_200(self):
        import graph.nodes.implement_node as implement_node

        notice = implement_node._GLOBAL_EXCEPTION_HANDLER_STATUS_NOTICE
        assert "200" in notice
        assert "#34" in notice


class TestRunOneTaskFixedBody:
    """對應 10a 八章「⑦ 直接產生修正後程式碼」：`pending_fixed_bodies`
    命中的 task 直接把程式碼傳給 `fill_function()` 的 `fixed_body`，完全
    跳過 ⑤ 本地模型。"""

    def test_fixed_body_passed_through_when_present(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        captured = {}

        async def _fake_fill_function(**kwargs):
            captured.update(kwargs)
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)
        # fixed_body 給定時完全跳過 java_source／referenced_source 解析
        # （見 _run_one_task() docstring），這裡不需要它們也能正確跑，
        # 故意不 monkeypatch resolve_java_source／resolve_referenced_source，
        # 若被誤呼叫（理論上不該發生）會直接因為 java_method_id 這個 key
        # 不存在而 KeyError，等於順便驗證了「完全跳過」這件事。

        task = {
            "id": "task_svc1", "module": "school", "description": "d",
            "target_files": ["app/services/common_service.py"], "context": "",
            "depends_on": [], "class_name": "CollectionUtil", "function_name": "find_distinct_field",
            "translator_backend": "qwen",
        }
        asyncio.run(implement_node._run_one_task(
            task, "/unused", "/unused-java", "run_x", {"task_svc1": "return list(distinct_values)"}
        ))

        assert captured["fixed_body"] == "return list(distinct_values)"

    def test_fixed_body_none_when_task_not_pending(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        captured = {}

        async def _fake_fill_function(**kwargs):
            captured.update(kwargs)
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

        task = {
            "id": "task_other", "module": "school", "description": "d",
            "target_files": ["app/services/common_service.py"], "context": "",
            "depends_on": [], "class_name": "CollectionUtil", "function_name": "find_distinct_field",
            "java_method_id": "CommonService.java::CollectionUtil::findDistinctField",
            "translator_backend": "qwen",
        }
        asyncio.run(implement_node._run_one_task(
            task, "/unused", "/unused-java", "run_x", {"task_svc1": "return list(distinct_values)"}
        ))

        assert captured["fixed_body"] is None


class TestRunOneTaskRetranslate:
    """對應 docs/refactor_bug_trace.md #9：`pending_retranslate_tasks`
    命中的 task **不**跳過模型呼叫——跟 pending_fixed_bodies 相反，走
    跟⑤首輪翻譯完全相同的路徑（解析真實 java_source／referenced_source、
    真的呼叫 fill_function()），只是把 ⑦ 的 diagnosis 疊加進 context。
    """

    def test_retranslate_task_resolves_real_java_source_and_calls_model(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        captured = {}

        async def _fake_fill_function(**kwargs):
            captured.update(kwargs)
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

        task = {
            "id": "task_svc1", "module": "school", "description": "d",
            "target_files": ["app/services/common_service.py"], "context": "",
            "depends_on": [], "class_name": "CollectionUtil", "function_name": "find_distinct_field",
            "java_method_id": "CommonService.java::CollectionUtil::findDistinctField",
            "translator_backend": "qwen",
        }
        asyncio.run(implement_node._run_one_task(
            task, "/unused", "/unused-java", "run_x", {},
            {"task_svc1": "上一輪漏了排序條件"},
        ))

        # fixed_body 必須是 None——這個 task 走真正的模型呼叫，不是套用
        # ⑦ 給的現成程式碼。
        assert captured["fixed_body"] is None
        assert captured["java_source"] == "java src"
        assert "上一輪漏了排序條件" in captured["context"]

    def test_task_not_in_retranslate_tasks_context_unaffected(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        captured = {}

        async def _fake_fill_function(**kwargs):
            captured.update(kwargs)
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

        task = {
            "id": "task_other", "module": "school", "description": "d",
            "target_files": ["app/routers/school_router.py"], "context": "",
            "depends_on": [], "class_name": None, "function_name": "get_all",
            "java_method_id": "SchoolController.java::SchoolController::getAll",
            "translator_backend": "claude",
        }
        asyncio.run(implement_node._run_one_task(
            task, "/unused", "/unused-java", "run_x", {},
            {"task_svc1": "上一輪漏了排序條件"},  # 不是這個 task 的 id
        ))

        assert "上一輪漏了排序條件" not in captured["context"]


class TestForceRescheduleCallSite:
    """對應 10a 八章「新增：ModuleScheduler.force_reschedule()」：
    pending_fixed_bodies 指向一個上一輪已經 module_status=="verified"
    的 module 時，run() 必須把它打回 pending，讓它的 task 直接套用
    ⑦ 給的程式碼——否則 get_ready_tasks() 永遠不會再排到它。"""

    def test_verified_module_is_rescheduled_and_task_rerun(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)
        monkeypatch.setattr(implement_node.ollama_client, "check_ollama_reachable", _skip_start)

        async def _fake_wait_for_service_reload(*args, **kwargs):
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait_for_service_reload)

        async def _fake_partial_verify(module, db, verifier):
            return {"status": "pass", "reason": None}

        monkeypatch.setattr(implement_node, "_partial_verify", _fake_partial_verify)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

        captured_fixed_bodies = {}

        async def _fake_fill_function(**kwargs):
            captured_fixed_bodies[kwargs["task_id"]] = kwargs["fixed_body"]
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)

        module_list = [
            {"module": "exam", "java_files": [], "depends_on": [], "methods": [], "summary": ""},
        ]
        task_list = [
            {
                "id": "task_e1", "module": "exam", "description": "d",
                "target_files": ["app/services/exam_service.py"], "context": "",
                "depends_on": [], "class_name": "ExamService", "function_name": "create",
                "translator_backend": "qwen",
            },
        ]
        state = {
            "run_id": "test_run",
            "retry_count": 1,
            "scaffold_done": True,
            "module_list": module_list,
            "task_list": task_list,
            # 模擬上一輪 ⑤ 局部驗證判定 exam 為 verified（"pass"）——沒有
            # force_reschedule() 的話，get_ready_tasks() 不會再排它。
            "completed_tasks": ["task_e1"],
            "failed_tasks": [],
            "task_failures": [],
            "skipped_interfaces": [],
            "partial_reports": [{"module": "exam", "round": 0, "report": {"status": "pass"}}],
            "pending_fixed_bodies": {"task_e1": "score = rq.score\nreturn Result.success(score)"},
            "python_project_path": "/unused",
            "java_project_path": "/unused-java",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        result = asyncio.run(implement_node.run(state))

        assert captured_fixed_bodies.get("task_e1") == "score = rq.score\nreturn Result.success(score)"
        assert result["failed_modules"] == []
        assert result["blocked_modules"] == []

    def test_pending_retranslate_tasks_also_reschedules_verified_module(self, monkeypatch):
        """對應 docs/refactor_bug_trace.md #9：pending_retranslate_tasks
        跟 pending_fixed_bodies 一樣要能把一個上一輪已經 verified 的
        module 打回 pending、重新排程——不是只有 pending_fixed_bodies
        才能觸發 force_reschedule()。"""
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)
        monkeypatch.setattr(implement_node.ollama_client, "check_ollama_reachable", _skip_start)

        async def _fake_wait_for_service_reload(*args, **kwargs):
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait_for_service_reload)

        async def _fake_partial_verify(module, db, verifier):
            return {"status": "pass", "reason": None}

        monkeypatch.setattr(implement_node, "_partial_verify", _fake_partial_verify)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

        captured = {}

        async def _fake_fill_function(**kwargs):
            captured["fixed_body"] = kwargs["fixed_body"]
            captured["context"] = kwargs["context"]
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)

        module_list = [
            {"module": "exam", "java_files": [], "depends_on": [], "methods": [], "summary": ""},
        ]
        task_list = [
            {
                "id": "task_e1", "module": "exam", "description": "d",
                "target_files": ["app/services/exam_service.py"], "context": "",
                "depends_on": [], "class_name": "ExamService", "function_name": "create",
                "translator_backend": "qwen",
                "java_method_id": "ExamService.java::ExamService::create",
            },
        ]
        state = {
            "run_id": "test_run",
            "retry_count": 1,
            "scaffold_done": True,
            "module_list": module_list,
            "task_list": task_list,
            "completed_tasks": ["task_e1"],
            "failed_tasks": [],
            "task_failures": [],
            "skipped_interfaces": [],
            "partial_reports": [{"module": "exam", "round": 0, "report": {"status": "pass"}}],
            "pending_fixed_bodies": {},
            "pending_retranslate_tasks": {"task_e1": "漏了排序條件"},
            "python_project_path": "/unused",
            "java_project_path": "/unused-java",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        result = asyncio.run(implement_node.run(state))

        assert captured.get("fixed_body") is None
        assert "漏了排序條件" in captured.get("context", "")
        assert result["failed_modules"] == []
        assert result["blocked_modules"] == []


class TestFillFailedTasksExcludedFromLocalModelRetry:
    """對應 docs/09b_bug_trace.md：一個 task 只要曾經在 task_failures
    留下一筆 reason=="fill_failed"，排程器重建時就該把它排除在「可排給
    ⑤ 本地模型」的池之外，唯一能讓它重新被排到的路徑是
    force_reschedule()（只由 ⑦ 產生的 pending_fixed_bodies 觸發）——
    這是 09a 七章原本「fill_failed 每輪都當新 task 重排」的既有規則
    跟 10a「⑤ 一旦進入除錯迴圈就完全退出」原則沒有對齊留下的落差，
    真實環境重跑證實這是同一批 task 連續多輪透過 Ollama 重試的機制性
    原因，不是 prompt 沒要求 ⑦ 出手。"""

    def test_task_with_prior_fill_failed_is_not_sent_to_local_model_again(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)

        async def _fake_partial_verify(module, db, verifier):
            return {"status": "pass", "reason": None}

        monkeypatch.setattr(implement_node, "_partial_verify", _fake_partial_verify)

        async def _should_not_be_called(**kwargs):
            raise AssertionError(
                "task_e1 上一輪已經是 fill_failed，這一輪 pending_fixed_bodies 也沒有它，"
                "不該再被排進 fill_function()（不管是走本地模型還是別的路徑）"
            )

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _should_not_be_called)

        module_list = [
            {"module": "exam", "java_files": [], "depends_on": [], "methods": [], "summary": ""},
        ]
        task_list = [
            {
                "id": "task_e1", "module": "exam", "description": "d",
                "target_files": ["app/services/exam_service.py"], "context": "",
                "depends_on": [], "class_name": "ExamService", "function_name": "create_random",
            },
        ]
        state = {
            "run_id": "test_run",
            "retry_count": 1,
            "scaffold_done": True,
            "module_list": module_list,
            "task_list": task_list,
            "completed_tasks": [],
            "failed_tasks": [],
            # 上一輪已經記錄過 fill_failed——這是這個測試要鎖住的關鍵輸入。
            "task_failures": [
                {
                    "task_id": "task_e1", "module": "exam", "file_path": "app/services/exam_service.py",
                    "class_name": "ExamService", "function_name": "create_random",
                    "reason": "fill_failed", "error": "delimiter 契約違反",
                },
            ],
            "skipped_interfaces": [],
            "partial_reports": [],
            "pending_fixed_bodies": {},  # ⑦ 這一輪沒有針對 task_e1 出手
            "python_project_path": "/unused",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        result = asyncio.run(implement_node.run(state))

        # task_e1 保持失敗狀態，不會被誤判成完成或憑空消失。
        assert "task_e1" not in result["completed_tasks"]

    def test_task_with_prior_fill_failed_is_rescheduled_once_seven_provides_a_fix(self, monkeypatch):
        """跟上一個測試對照：這次 pending_fixed_bodies 有 task_e1，
        force_reschedule() 應該正確把它從 task_failed 移除、讓它套用
        ⑦ 給的 fixed_body，而不是被剛剛加的排除邏輯永久卡死。"""
        import graph.nodes.implement_node as implement_node

        async def _skip_start(*args, **kwargs):
            return None

        monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)
        monkeypatch.setattr(implement_node.ollama_client, "check_ollama_reachable", _skip_start)

        async def _fake_wait_for_service_reload(*args, **kwargs):
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait_for_service_reload)

        async def _fake_partial_verify(module, db, verifier):
            return {"status": "pass", "reason": None}

        monkeypatch.setattr(implement_node, "_partial_verify", _fake_partial_verify)
        monkeypatch.setattr(implement_node, "resolve_java_source", lambda *a, **kw: "java src")
        monkeypatch.setattr(implement_node, "resolve_referenced_source", lambda *a, **kw: [])

        captured = {}

        async def _fake_fill_function(**kwargs):
            captured[kwargs["task_id"]] = kwargs["fixed_body"]
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)

        module_list = [
            {"module": "exam", "java_files": [], "depends_on": [], "methods": [], "summary": ""},
        ]
        task_list = [
            {
                "id": "task_e1", "module": "exam", "description": "d",
                "target_files": ["app/services/exam_service.py"], "context": "",
                "depends_on": [], "class_name": "ExamService", "function_name": "create_random",
                "translator_backend": "qwen",
            },
        ]
        state = {
            "run_id": "test_run",
            "retry_count": 2,
            "scaffold_done": True,
            "module_list": module_list,
            "task_list": task_list,
            "completed_tasks": [],
            "failed_tasks": [],
            "task_failures": [
                {
                    "task_id": "task_e1", "module": "exam", "file_path": "app/services/exam_service.py",
                    "class_name": "ExamService", "function_name": "create_random",
                    "reason": "fill_failed", "error": "delimiter 契約違反",
                },
            ],
            "skipped_interfaces": [],
            "partial_reports": [],
            "pending_fixed_bodies": {"task_e1": "return Result().success(rs)"},
            "python_project_path": "/unused",
            "java_project_path": "/unused-java",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        result = asyncio.run(implement_node.run(state))

        assert captured.get("task_e1") == "return Result().success(rs)"
        assert "task_e1" in result["completed_tasks"]


class TestPendingFileFixes:
    """對應 10a 八章「phase 2：檔案層級修正」：pending_file_fixes 不
    透過排程器，直接呼叫 translator_cli.apply_file_fix()。"""

    def _base_state(self, **overrides):
        module_list = [
            {"module": "exam", "java_files": [], "depends_on": [], "methods": [], "summary": ""},
        ]
        task_list = [
            {
                "id": "task_r2", "module": "exam", "description": "d",
                "target_files": ["app/routers/exam_router.py"], "context": "",
                "depends_on": [], "class_name": None, "function_name": "search",
            },
        ]
        state = {
            "run_id": "test_run", "retry_count": 1, "scaffold_done": True,
            "module_list": module_list, "task_list": task_list,
            "completed_tasks": ["task_r2"], "failed_tasks": [], "task_failures": [],
            "skipped_interfaces": [],
            "partial_reports": [{"module": "exam", "round": 0, "report": {"status": "pass"}}],
            "pending_fixed_bodies": {},
            "pending_file_fixes": [],
            "python_project_path": "/unused",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }
        state.update(overrides)
        return state

    def test_successful_file_fix_triggers_reload_wait(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        async def _fake_apply_file_fix(**kwargs):
            return FillResult(success=True, diff="...")

        monkeypatch.setattr(implement_node.translator_cli, "apply_file_fix", _fake_apply_file_fix)

        reload_calls = []

        async def _fake_wait(*args, **kwargs):
            reload_calls.append(1)
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait)

        state = self._base_state(pending_file_fixes=[{
            "task_id": "task_r2", "target_file": "app/routers/exam_router.py",
            "old_snippet": "a", "new_snippet": "b",
        }])

        result = asyncio.run(implement_node.run(state))

        assert len(reload_calls) == 1
        assert result["task_failures"] == []

    def test_failed_file_fix_records_task_failure_and_skips_reload_wait(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        async def _fake_apply_file_fix(**kwargs):
            return FillResult(success=False, error="old_snippet 找不到", diff="")

        monkeypatch.setattr(implement_node.translator_cli, "apply_file_fix", _fake_apply_file_fix)

        reload_calls = []

        async def _fake_wait(*args, **kwargs):
            reload_calls.append(1)
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait)

        state = self._base_state(pending_file_fixes=[{
            "task_id": "task_r2", "target_file": "app/routers/exam_router.py",
            "old_snippet": "a", "new_snippet": "b",
        }])

        result = asyncio.run(implement_node.run(state))

        assert len(reload_calls) == 0
        assert len(result["task_failures"]) == 1
        assert result["task_failures"][0]["reason"] == "file_fix_failed"
        assert result["task_failures"][0]["task_id"] == "task_r2"

    def test_no_pending_file_fixes_skips_reload_wait(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        reload_calls = []

        async def _fake_wait(*args, **kwargs):
            reload_calls.append(1)
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait)

        result = asyncio.run(implement_node.run(self._base_state()))

        assert len(reload_calls) == 0
        assert result["task_failures"] == []
