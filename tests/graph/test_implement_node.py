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

        async def _fake_wait_for_service_reload(*args, **kwargs):
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait_for_service_reload)

        async def _fake_partial_verify(module, db, verifier):
            return {"status": "fail", "reason": "response_mismatch"}

        monkeypatch.setattr(implement_node, "_partial_verify", _fake_partial_verify)

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

        task = {
            "id": "task_045", "module": "exam", "description": "d",
            "target_files": ["app/services/exam_service.py"], "context": "",
            "depends_on": [], "class_name": "ExamService", "function_name": "create_random",
            "referenced_functions": [
                {"file_path": "app/repositories/exam_repository.py", "class_name": "ExamRepository", "function_name": "find_by_card"},
                {"file_path": "app/routers/exam_router.py", "class_name": None, "function_name": "search"},
            ],
        }

        asyncio.run(implement_node._run_one_task(task, "/unused", "run_x", {}))

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

        task = {
            "id": "task_001", "module": "user", "description": "d",
            "target_files": ["app/services/user_service.py"], "context": "",
            "depends_on": [], "class_name": "UserService", "function_name": "get_user",
        }

        asyncio.run(implement_node._run_one_task(task, "/unused", "run_x", {}))

        assert captured["referenced_functions"] == []


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

        assert context == f"既有描述\n\n{implement_node._RESULT_FACTORY_INSTANCE_METHOD_NOTICE}"
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

    def test_result_factory_instance_method_notice_is_the_only_content_when_context_empty(self):
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_r2", "module": "exam", "description": "d",
            "target_files": ["app/routers/exam_router.py"], "context": "",
            "depends_on": [], "class_name": None, "function_name": "search",
        }

        context, _ = implement_node._augment_task_io(task)

        assert context == implement_node._RESULT_FACTORY_INSTANCE_METHOD_NOTICE

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
        assert "VOICE_UPLOAD_DIR" in context

    def test_other_router_tasks_do_not_get_hardcoded_upload_path_notice(self):
        import graph.nodes.implement_node as implement_node

        task = {
            "id": "task_r3", "module": "exam", "description": "d",
            "target_files": ["app/routers/exam_router.py"], "context": "",
            "depends_on": [], "class_name": None, "function_name": "search",
        }

        context, _ = implement_node._augment_task_io(task)

        assert implement_node._HARDCODED_UPLOAD_PATH_NOTICE not in context


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

        task = {
            "id": "task_svc1", "module": "school", "description": "d",
            "target_files": ["app/services/common_service.py"], "context": "",
            "depends_on": [], "class_name": "CollectionUtil", "function_name": "find_distinct_field",
        }
        asyncio.run(implement_node._run_one_task(
            task, "/unused", "run_x", {"task_svc1": "return list(distinct_values)"}
        ))

        assert captured["fixed_body"] == "return list(distinct_values)"

    def test_fixed_body_none_when_task_not_pending(self, monkeypatch):
        import graph.nodes.implement_node as implement_node

        captured = {}

        async def _fake_fill_function(**kwargs):
            captured.update(kwargs)
            return FillResult(success=True, diff="")

        monkeypatch.setattr(implement_node.translator_cli, "fill_function", _fake_fill_function)

        task = {
            "id": "task_other", "module": "school", "description": "d",
            "target_files": ["app/services/common_service.py"], "context": "",
            "depends_on": [], "class_name": "CollectionUtil", "function_name": "find_distinct_field",
        }
        asyncio.run(implement_node._run_one_task(
            task, "/unused", "run_x", {"task_svc1": "return list(distinct_values)"}
        ))

        assert captured["fixed_body"] is None


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

        async def _fake_wait_for_service_reload(*args, **kwargs):
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait_for_service_reload)

        async def _fake_partial_verify(module, db, verifier):
            return {"status": "pass", "reason": None}

        monkeypatch.setattr(implement_node, "_partial_verify", _fake_partial_verify)

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
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
        }

        result = asyncio.run(implement_node.run(state))

        assert captured_fixed_bodies.get("task_e1") == "score = rq.score\nreturn Result.success(score)"
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

        async def _fake_wait_for_service_reload(*args, **kwargs):
            return True

        monkeypatch.setattr(implement_node, "_wait_for_service_reload", _fake_wait_for_service_reload)

        async def _fake_partial_verify(module, db, verifier):
            return {"status": "pass", "reason": None}

        monkeypatch.setattr(implement_node, "_partial_verify", _fake_partial_verify)

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
