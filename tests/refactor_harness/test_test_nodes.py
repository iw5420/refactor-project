"""對應 docs/09b_bug_trace.md #42：02a 十一章設計的 report.json 落地，
02b 從未真的實作——這裡驗證 run_postman_tests() 補上的持久化行為。"""
import json
import os

# test_nodes.py 模組層級讀 os.environ["JAVA_BASE_URL"]（見該檔案「Java
# 服務位置不放進 RefactorState，直接讀 .env」），import 當下就需要這個
# 值——這裡不是真的要連線，只是繞過 import-time 的 KeyError。
os.environ.setdefault("JAVA_BASE_URL", "http://unused")

import refactor_harness.langgraph_nodes.test_nodes as test_nodes


class TestRunPostmanTestsWritesReportJson:
    def test_writes_report_to_logs_with_run_id(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(test_nodes, "_is_service_reachable", lambda *a, **k: True)
        monkeypatch.setattr(test_nodes.time, "sleep", lambda *_: None)
        monkeypatch.setattr(test_nodes, "TABLES", [])

        class _FakeDb:
            def __init__(self, test_dsn):
                pass

            def apply_seed(self, path, tables_to_truncate):
                pass

        monkeypatch.setattr(test_nodes, "DbEnvironment", _FakeDb)

        class _FakeGoldenVerifier:
            def __init__(self, **kwargs):
                pass

            def verify_raw(self, collection_path):
                return [{"case_id": "a", "module": "school", "passed": True}]

        monkeypatch.setattr(test_nodes, "GoldenVerifier", _FakeGoldenVerifier)

        class _FakeMutationVerifier:
            def __init__(self, **kwargs):
                pass

            def verify_all_raw(self):
                return []

            def get_excluded_folders(self):
                return []

        monkeypatch.setattr(test_nodes, "MutationVerifier", _FakeMutationVerifier)

        state = {
            "run_id": "test_run_abc",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
            "api_to_python_target": [],
        }

        result = test_nodes.run_postman_tests(state)

        report_path = tmp_path / "logs" / "report_test_run_abc.json"
        assert report_path.exists()
        with open(report_path, encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk == result["test_results"]

    def test_success_path_clears_stale_service_diagnostics(self, monkeypatch, tmp_path):
        """對應 docs/09b_bug_trace.md：service_diagnostics 只在
        service_unreachable 分支被寫入，過去這條「服務有回應」的成功
        路徑從未清空它——真實環境重跑證實這會讓上一輪服務連不上時的舊
        崩潰日誌，原封不動殘留到這一輪服務其實正常啟動之後，被 ⑦ Debug
        Agent 誤信成「這一輪」的診斷，在已經修好的舊 bug 上原地打轉。
        這裡驗證：只要 run_postman_tests() 走到服務有回應、report 真的
        產出來的成功路徑，不論 state 進來前 service_diagnostics 是不是
        還帶著上一輪的舊值，回傳的 state 都必須把它清成 None。"""
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(test_nodes, "_is_service_reachable", lambda *a, **k: True)
        monkeypatch.setattr(test_nodes.time, "sleep", lambda *_: None)
        monkeypatch.setattr(test_nodes, "TABLES", [])

        class _FakeDb:
            def __init__(self, test_dsn):
                pass

            def apply_seed(self, path, tables_to_truncate):
                pass

        monkeypatch.setattr(test_nodes, "DbEnvironment", _FakeDb)

        class _FakeGoldenVerifier:
            def __init__(self, **kwargs):
                pass

            def verify_raw(self, collection_path):
                return [{"case_id": "a", "module": "school", "passed": True}]

        monkeypatch.setattr(test_nodes, "GoldenVerifier", _FakeGoldenVerifier)

        class _FakeMutationVerifier:
            def __init__(self, **kwargs):
                pass

            def verify_all_raw(self):
                return []

            def get_excluded_folders(self):
                return []

        monkeypatch.setattr(test_nodes, "MutationVerifier", _FakeMutationVerifier)

        state = {
            "run_id": "test_run_stale_diag",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
            "api_to_python_target": [],
            "service_diagnostics": "上一輪服務連不上時擷取的舊崩潰日誌",
        }

        result = test_nodes.run_postman_tests(state)

        assert result["service_diagnostics"] is None

    def test_handles_non_json_serializable_body_diff(self, monkeypatch, tmp_path):
        """body_diff（DeepDiff 格式）可能含 Python type 物件（如
        {'old_type': <class 'int'>}），json.dump 沒有 default=str 兜底
        會直接 TypeError——這是真實環境重跑時實際發生過的內容形狀。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(test_nodes, "_is_service_reachable", lambda *a, **k: True)
        monkeypatch.setattr(test_nodes.time, "sleep", lambda *_: None)
        monkeypatch.setattr(test_nodes, "TABLES", [])

        class _FakeDb:
            def __init__(self, test_dsn):
                pass

            def apply_seed(self, path, tables_to_truncate):
                pass

        monkeypatch.setattr(test_nodes, "DbEnvironment", _FakeDb)

        class _FakeGoldenVerifier:
            def __init__(self, **kwargs):
                pass

            def verify_raw(self, collection_path):
                return [{
                    "case_id": "a",
                    "module": "school",
                    "passed": False,
                    "status_match": False,
                    "expected_status": 200,
                    "actual_status": 500,
                    "body_diff": {"type_changes": {"root['code']": {"old_type": int, "new_type": str}}},
                }]

        monkeypatch.setattr(test_nodes, "GoldenVerifier", _FakeGoldenVerifier)

        class _FakeMutationVerifier:
            def __init__(self, **kwargs):
                pass

            def verify_all_raw(self):
                return []

            def get_excluded_folders(self):
                return []

        monkeypatch.setattr(test_nodes, "MutationVerifier", _FakeMutationVerifier)

        state = {
            "run_id": "test_run_xyz",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
            "api_to_python_target": [],
        }

        test_nodes.run_postman_tests(state)

        report_path = tmp_path / "logs" / "report_test_run_xyz.json"
        assert report_path.exists()

    def test_does_not_leak_reducer_fields_back_into_return_value(self, monkeypatch, tmp_path):
        """對應 docs/09b_bug_trace.md #54：真實重跑發現 task_failures 的
        1 筆真實記錄膨脹成 128 筆重複——根因是這裡的 return 用 `**state`
        帶過 completed_tasks／failed_tasks／task_failures／partial_reports
        ／debug_rounds 這五個掛 operator.add reducer 的欄位，等於每次這
        個 node 完成就把「已經累積的完整值」重複回報一次，LangGraph 的
        reducer 會把它加到既有累積值上，形成指數成長。這裡驗證 state 帶
        著非空的歷史值進來時，回傳值裡這五個欄位必須是空list（delta 為
        零，不是把歷史值原樣吐回去）。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(test_nodes, "_is_service_reachable", lambda *a, **k: True)
        monkeypatch.setattr(test_nodes.time, "sleep", lambda *_: None)
        monkeypatch.setattr(test_nodes, "TABLES", [])

        class _FakeDb:
            def __init__(self, test_dsn):
                pass

            def apply_seed(self, path, tables_to_truncate):
                pass

        monkeypatch.setattr(test_nodes, "DbEnvironment", _FakeDb)

        class _FakeGoldenVerifier:
            def __init__(self, **kwargs):
                pass

            def verify_raw(self, collection_path):
                return []

        monkeypatch.setattr(test_nodes, "GoldenVerifier", _FakeGoldenVerifier)

        class _FakeMutationVerifier:
            def __init__(self, **kwargs):
                pass

            def verify_all_raw(self):
                return []

            def get_excluded_folders(self):
                return []

        monkeypatch.setattr(test_nodes, "MutationVerifier", _FakeMutationVerifier)

        state = {
            "run_id": "test_run_reducer",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
            "api_to_python_target": [],
            "completed_tasks": ["task_1", "task_2"],
            "failed_tasks": ["task_3"],
            "task_failures": [{"task_id": "task_3", "reason": "fill_failed"}],
            "partial_reports": [{"module": "school", "report": {"status": "pass"}}],
            "debug_rounds": [{"round": 0, "module": "school"}],
        }

        result = test_nodes.run_postman_tests(state)

        assert result["completed_tasks"] == []
        assert result["failed_tasks"] == []
        assert result["task_failures"] == []
        assert result["partial_reports"] == []
        assert result["debug_rounds"] == []


class TestIsServiceReachable:
    def test_reachable_when_get_succeeds(self, monkeypatch):
        def _fake_get(url, timeout):
            return None

        monkeypatch.setattr(test_nodes.httpx, "get", _fake_get)

        assert test_nodes._is_service_reachable("http://unused", timeout_seconds=1.0, poll_interval=0.1) is True

    def test_unreachable_when_connection_refused_until_timeout(self, monkeypatch):
        import httpx as httpx_module

        def _always_fails(url, timeout):
            raise httpx_module.ConnectError("connection refused")

        monkeypatch.setattr(test_nodes.httpx, "get", _always_fails)

        assert test_nodes._is_service_reachable("http://unused", timeout_seconds=0.3, poll_interval=0.1) is False


class TestRunPostmanTestsHealthCheck:
    """對應 10a 二章「⑥ 對外呼叫前必須先確認 Python 服務有回應」：服務
    不可達時直接短路，不呼叫 run_newman()，並把容器診斷寫進
    state["service_diagnostics"]。"""

    def test_service_unreachable_short_circuits_without_calling_newman(self, monkeypatch):
        monkeypatch.setattr(test_nodes, "_is_service_reachable", lambda *a, **k: False)
        monkeypatch.setattr(test_nodes.time, "sleep", lambda *_: None)
        monkeypatch.setattr(
            test_nodes.python_service_manager, "get_diagnostics", lambda: "Traceback (most recent call last): ..."
        )

        def _should_not_be_called(*args, **kwargs):
            raise AssertionError("服務不可達時不該呼叫 GoldenVerifier")

        monkeypatch.setattr(test_nodes, "GoldenVerifier", _should_not_be_called)
        monkeypatch.setattr(test_nodes, "MutationVerifier", _should_not_be_called)
        monkeypatch.setattr(test_nodes, "DbEnvironment", _should_not_be_called)

        state = {
            "run_id": "test_run_unreachable",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
            "api_to_python_target": [],
            "task_failures": [{"task_id": "task_3", "reason": "fill_failed"}],
        }

        result = test_nodes.run_postman_tests(state)

        assert result["test_results"]["status"] == "fail"
        assert result["test_results"]["reason"] == "service_unreachable"
        assert result["test_results"]["failures"] == []
        assert result["service_diagnostics"] == "Traceback (most recent call last): ..."
        # 對應 docs/09b_bug_trace.md #54：service_unreachable 短路分支也
        # 是 `**state` 帶過其餘欄位，同樣要確認沒有把 task_failures 等
        # 掛 reducer 的欄位原樣吐回去
        assert result["task_failures"] == []

    def test_sleeps_pre_health_check_buffer_before_checking_reachability(self, monkeypatch):
        """對應 docs/09b_bug_trace.md #47：健康檢查前先固定緩衝，讓可能
        還在進行中的 reload 有機會收斂——這裡鎖住呼叫順序（先 sleep 緩衝
        秒數，再檢查可達性），不是只驗證最終結果。"""
        call_order = []
        monkeypatch.setattr(test_nodes.time, "sleep", lambda seconds: call_order.append(("sleep", seconds)))
        monkeypatch.setattr(
            test_nodes, "_is_service_reachable", lambda *a, **k: call_order.append(("check",)) or False
        )
        monkeypatch.setattr(
            test_nodes.python_service_manager, "get_diagnostics", lambda: None
        )

        state = {
            "run_id": "test_run_buffer",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
            "api_to_python_target": [],
        }

        test_nodes.run_postman_tests(state)

        assert call_order == [
            ("sleep", test_nodes.RUN_TESTS_PRE_HEALTH_CHECK_BUFFER_SECONDS),
            ("check",),
        ]


class TestRunPostmanTestsMidFlightTimeout:
    """對應 docs/09b_bug_trace.md #38 真實重跑撞到的缺口：前置健康檢查
    只確認「這一輪開始前」連得上，readonly／mutation 兩輪合計耗時可能
    遠超過那次健康檢查的短逾時，服務完全可能在健康檢查通過之後、實際
    打 collection 期間才變得沒有回應（uvicorn --reload worker 崩潰但
    socket 還開著）——run_newman() 逾時時拋的 NewmanTimeoutError 必須被
    接住，落到跟前置健康檢查失敗時同一份 fallback，不能讓例外一路炸穿
    graph.ainvoke()。"""

    def test_mid_flight_newman_timeout_falls_back_to_service_unreachable(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(test_nodes, "_is_service_reachable", lambda *a, **k: True)
        monkeypatch.setattr(test_nodes.time, "sleep", lambda *_: None)
        monkeypatch.setattr(test_nodes, "TABLES", [])
        monkeypatch.setattr(
            test_nodes.python_service_manager, "get_diagnostics", lambda: "Traceback (most recent call last): ..."
        )

        class _FakeDb:
            def __init__(self, test_dsn):
                pass

            def apply_seed(self, path, tables_to_truncate):
                pass

        monkeypatch.setattr(test_nodes, "DbEnvironment", _FakeDb)

        class _FakeGoldenVerifier:
            def __init__(self, **kwargs):
                pass

            def verify_raw(self, collection_path):
                raise test_nodes.NewmanTimeoutError("newman 執行逾時（timeout=180.0s）")

        monkeypatch.setattr(test_nodes, "GoldenVerifier", _FakeGoldenVerifier)

        def _should_not_be_called(*args, **kwargs):
            raise AssertionError("readonly 逾時就該短路，不該再呼叫 MutationVerifier")

        monkeypatch.setattr(test_nodes, "MutationVerifier", _should_not_be_called)

        state = {
            "run_id": "test_run_midflight_timeout",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
            "api_to_python_target": [],
            "debug_rounds": [{"round": 0, "module": "school"}],
        }

        result = test_nodes.run_postman_tests(state)

        assert result["test_results"]["status"] == "fail"
        assert result["test_results"]["reason"] == "service_unreachable"
        assert result["service_diagnostics"] == "Traceback (most recent call last): ..."
        # 對應 docs/09b_bug_trace.md #54：這個 mid-flight timeout 短路
        # 分支也是同一段 `**state`，同樣要確認沒有把 debug_rounds 原樣吐回去
        assert result["debug_rounds"] == []

    def test_other_runtime_errors_are_not_swallowed(self, monkeypatch, tmp_path):
        """非逾時的 RuntimeError（如 newman 執行檔找不到、collection 路徑
        錯誤這類設定錯誤）不該被誤判成暫時性的 service_unreachable，必須
        照舊往外拋，讓人工看到真正的錯誤訊息。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(test_nodes, "_is_service_reachable", lambda *a, **k: True)
        monkeypatch.setattr(test_nodes.time, "sleep", lambda *_: None)
        monkeypatch.setattr(test_nodes, "TABLES", [])

        class _FakeDb:
            def __init__(self, test_dsn):
                pass

            def apply_seed(self, path, tables_to_truncate):
                pass

        monkeypatch.setattr(test_nodes, "DbEnvironment", _FakeDb)

        class _FakeGoldenVerifier:
            def __init__(self, **kwargs):
                pass

            def verify_raw(self, collection_path):
                raise RuntimeError("找不到 newman 執行檔")

        monkeypatch.setattr(test_nodes, "GoldenVerifier", _FakeGoldenVerifier)

        def _should_not_be_called(*args, **kwargs):
            raise AssertionError("readonly 已經拋出非逾時錯誤，不該再呼叫 MutationVerifier")

        monkeypatch.setattr(test_nodes, "MutationVerifier", _should_not_be_called)

        state = {
            "run_id": "test_run_config_error",
            "test_dsn": "postgresql://unused",
            "python_base_url": "http://unused",
            "api_to_python_target": [],
        }

        import pytest
        with pytest.raises(RuntimeError, match="找不到 newman"):
            test_nodes.run_postman_tests(state)


class TestRecordGoldenOutputPassesRouteToModuleMapping:
    """對應 docs/09b_bug_trace.md #64：`record_golden_output()`（②）跟
    `run_postman_tests()`（⑥）是平行分支，`config/harness.yaml` 由 ③
    寫入，寫入時機不保證早於 ② 讀取——這裡確認 ② 改成直接從
    `state["api_to_python_target"]` 算出 module 對照傳給 `GoldenRecorder`，
    不再讓它自己去讀當下可能還沒被 ③ 更新過的 `config/harness.yaml`。"""

    def test_passes_module_mapping_derived_from_state(self, monkeypatch):
        monkeypatch.setenv("JAVA_JAR_PATH", "unused.jar")
        monkeypatch.setenv("SPRING_DATASOURCE_URL", "jdbc:unused")
        monkeypatch.setenv("SPRING_DATASOURCE_USERNAME", "unused")
        monkeypatch.setenv("SPRING_DATASOURCE_PASSWORD", "unused")
        monkeypatch.setattr(test_nodes, "resolve_jar_path", lambda p: p)

        class _NoopJavaServiceProcess:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        monkeypatch.setattr(test_nodes, "JavaServiceProcess", _NoopJavaServiceProcess)

        captured = {}

        class _FakeGoldenRecorder:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def record(self, path):
                return {}

            def record_mutation(self, path):
                return {}

            def write_metadata(self, *a):
                pass

        monkeypatch.setattr(test_nodes, "GoldenRecorder", _FakeGoldenRecorder)

        state = {
            "test_dsn": "postgresql://unused",
            "api_to_python_target": [
                {
                    "endpoint": "/api/general/language",
                    "http_method": "GET",
                    "java_controller": "GeneralController.language",
                    "module": "school",
                },
            ],
        }

        test_nodes.record_golden_output(state)

        assert captured["route_to_module_mapping"] == {"GET_api_general_language": "school"}


class TestShouldDebugOrDone:
    """對應 10a 七章「路由方式」：retry_count 上限檢查對所有分支統一
    套用，不再依 failed_modules 是否為空分岔。"""

    def test_pass_status_returns_done(self):
        state = {"test_results": {"status": "pass"}, "retry_count": 0}
        assert test_nodes.should_debug_or_done(state) == "done"

    def test_empty_failed_modules_still_gives_up_at_max_retry(self):
        # 這是校正前的既有 stub 邏輯會漏掉的案例：failed_modules 為空
        # （例如 mutation-only 失敗）但 retry_count 已達上限，必須 give_up，
        # 不能無條件回傳 "debug"。
        state = {
            "test_results": {"status": "fail"},
            "failed_modules": [],
            "retry_count": test_nodes.MAX_RETRY,
        }
        assert test_nodes.should_debug_or_done(state) == "give_up"

    def test_empty_failed_modules_below_max_retry_still_debugs(self):
        state = {
            "test_results": {"status": "fail"},
            "failed_modules": [],
            "retry_count": test_nodes.MAX_RETRY - 1,
        }
        assert test_nodes.should_debug_or_done(state) == "debug"

    def test_nonempty_failed_modules_below_max_retry_debugs(self):
        state = {
            "test_results": {"status": "fail"},
            "failed_modules": ["exam"],
            "retry_count": 0,
        }
        assert test_nodes.should_debug_or_done(state) == "debug"

    def test_nonempty_failed_modules_at_max_retry_gives_up(self):
        state = {
            "test_results": {"status": "fail"},
            "failed_modules": ["exam"],
            "retry_count": test_nodes.MAX_RETRY,
        }
        assert test_nodes.should_debug_or_done(state) == "give_up"
