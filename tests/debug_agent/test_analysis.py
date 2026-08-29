"""debug_agent/analysis.py，對應 docs/10a_debug_agent_architecture.md
四、五、六、七章。用 monkeypatch 換掉 debug_agent.analysis.call_claude_for_json，
不呼叫真實 Claude API（比照 plan_agent/design_agent 既有測試慣例）。
"""
import pytest

import debug_agent.analysis as analysis
from debug_agent.analysis import (
    _is_global_infra_file,
    _validate_task_fixes,
    run_debug_analysis,
)


def _task(task_id: str, module: str, target_files: list[str] | None = None) -> dict:
    return {
        "id": task_id, "module": module, "description": "d",
        "target_files": target_files or [f"app/services/{module}_service.py"],
        "context": "", "depends_on": [], "class_name": None, "function_name": "do_it",
    }


class _FakeRouteMapper:
    """docs/09b_bug_trace.md #49：run_debug_analysis() 有 root_cause
    module 時會建構 RouteMapper() 讀取 config/harness.yaml——這個檔案是
    ③ design_agent 執行期產生的產物，不進 git（見 .gitignore），單元
    測試環境不保證存在。這裡用假物件避免真的碰檔案系統，比照本檔案一貫
    用 monkeypatch 換掉外部依賴（call_claude_for_json）的風格。預設
    module_mapping 為空，等同「沒有任何已知 route-owning 模組」——需要
    驗證 #49 行為的測試在函式內自行覆寫（見 TestZeroEndpointModuleFileInclusion）。
    """
    def __init__(self, module_mapping: dict[str, str] | None = None):
        self.module_mapping = module_mapping or {}


@pytest.fixture(autouse=True)
def _stub_route_mapper(monkeypatch):
    monkeypatch.setattr(analysis, "RouteMapper", _FakeRouteMapper)


class TestIsGlobalInfraFile:
    def test_schemas_models_core_match(self):
        assert _is_global_infra_file("app/schemas/exam.py")
        assert _is_global_infra_file("app/models/exam.py")
        assert _is_global_infra_file("app/core/exception_handlers.py")

    def test_routers_services_repositories_do_not_match(self):
        assert not _is_global_infra_file("app/routers/exam_router.py")
        assert not _is_global_infra_file("app/services/exam_service.py")
        assert not _is_global_infra_file("app/repositories/exam_repository.py")

    def test_zero_endpoint_owning_module_matches(self):
        # docs/09b_bug_trace.md #49：檔案父目錄是 services，機械目錄名
        # 判斷本身不會命中，但擁有它的模組（common）沒有任何 HTTP
        # endpoint，仍應被視為全域基礎設施檔案。
        assert _is_global_infra_file(
            "app/services/common_service.py",
            zero_endpoint_modules=frozenset({"common"}),
            file_to_module={"app/services/common_service.py": "common"},
        )

    def test_endpoint_owning_module_does_not_match(self):
        # 同一個檔名、同一個父目錄，但擁有它的模組（exam）有 HTTP
        # endpoint（不在 zero_endpoint_modules 裡）——不該被當成全域
        # 基礎設施檔案硬塞進去，維持既有「限縮 context」的精神。
        assert not _is_global_infra_file(
            "app/services/exam_service.py",
            zero_endpoint_modules=frozenset({"common"}),
            file_to_module={"app/services/exam_service.py": "exam"},
        )

    def test_unowned_file_does_not_match(self):
        # file_to_module 查無這個路徑（既不是任何 task 的 target_files[0]，
        # 也不落在 schemas/models/core）——維持過濾掉的既有行為。
        assert not _is_global_infra_file(
            "app/services/other_service.py",
            zero_endpoint_modules=frozenset({"common"}),
            file_to_module={"app/services/common_service.py": "common"},
        )


class TestValidateTaskFixes:
    def test_keeps_valid_discards_invalid(self, caplog):
        raw = [
            {"task_id": "t1", "diagnosis": "d1", "fixed_body": "f1", "file_fixes": []},
            {"task_id": "hallucinated", "diagnosis": "d2", "fixed_body": "f2", "file_fixes": []},
        ]
        with caplog.at_level("WARNING"):
            result = _validate_task_fixes(raw, valid_task_ids={"t1", "t2"})
        assert [tf["task_id"] for tf in result] == ["t1"]
        assert any("hallucinated" in r.message for r in caplog.records)

    def test_fixed_body_none_with_file_fixes_kept(self):
        """10a 八章「phase 2」：fixed_body 為 None 但 file_fixes 非空
        時仍是有效的 task_fix——只有兩者都空才捨棄。"""
        raw = [{
            "task_id": "t1", "diagnosis": "d1", "fixed_body": None,
            "file_fixes": [{"target_file": "app/core/x.py", "old_snippet": "a", "new_snippet": "b"}],
        }]
        result = _validate_task_fixes(raw, valid_task_ids={"t1"})
        assert len(result) == 1
        assert result[0]["fixed_body"] is None
        assert result[0]["file_fixes"] == [
            {"task_id": "t1", "target_file": "app/core/x.py", "old_snippet": "a", "new_snippet": "b"}
        ]

    def test_both_fixed_body_and_file_fixes_empty_discarded(self, caplog):
        raw = [{"task_id": "t1", "diagnosis": "d1", "fixed_body": None, "file_fixes": []}]
        with caplog.at_level("WARNING"):
            result = _validate_task_fixes(raw, valid_task_ids={"t1"})
        assert result == []
        assert any("皆空" in r.message for r in caplog.records)


def _base_state(**overrides) -> dict:
    state = {
        "test_results": {"failures": []},
        "task_failures": [],
        "task_list": [],
        "module_list": [],
        "blocked_modules": [],
        "failed_modules": [],
        "partial_reports": [],
        "retry_count": 0,
        "python_project_path": "/unused",
        "service_diagnostics": None,
        "debug_rounds": [],
    }
    state.update(overrides)
    return state


class TestRetryCountIncrement:
    def test_increments_regardless_of_failed_modules(self, monkeypatch):
        # 對應 10a 六章：只要真的進入這個函式就無條件 +1，不看 failed_modules。
        monkeypatch.setattr(analysis, "call_claude_for_json", lambda **kwargs: (_ for _ in ()).throw(AssertionError()))

        state = _base_state(retry_count=2, failed_modules=[])
        result = run_debug_analysis(state)
        assert result["retry_count"] == 3


class TestGiveUpEarlyCase2EmptyRootCause:
    def test_all_blocked_or_scaffold_gap_gives_up_early(self):
        state = _base_state(
            test_results={"failures": [
                {"case_id": "a", "module": "blocked_mod"},
                {"case_id": "b", "module": "gap_mod"},
            ]},
            task_failures=[
                {"task_id": "t_gap", "module": "gap_mod", "file_path": "x", "class_name": None,
                 "function_name": "f", "reason": "scaffold_skipped", "error": "e"},
            ],
            task_list=[_task("t_gap", "gap_mod")],
            blocked_modules=["blocked_mod"],
        )
        result = run_debug_analysis(state)
        assert result["give_up_early"] is True
        assert result["pending_fixed_bodies"] == {}
        origins = {r["module"]: r["origin"] for r in result["debug_rounds"]}
        assert origins == {"blocked_mod": "blocked", "gap_mod": "scaffold_gap"}

    def test_no_failures_at_all_gives_up_early(self):
        # service_unreachable 且 failed_modules 也是空——contexts 為空，
        # root_cause_ctxs 自然也是空，套用同一條一般規則。
        state = _base_state(
            test_results={"status": "fail", "reason": "service_unreachable", "failures": []},
            failed_modules=[],
        )
        result = run_debug_analysis(state)
        assert result["give_up_early"] is True
        assert result["debug_rounds"] == []


class TestGiveUpEarlyRootCauseAnalysis:
    def test_all_analyzed_and_unfixable_gives_up_early(self, monkeypatch, tmp_path):
        def _fake_call(**kwargs):
            return {"root_cause_summary": "沒救", "fixable": False, "task_fixes": [], "unfixable_reasons": ["無法定位"]}

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        state = _base_state(
            test_results={"failures": [{"case_id": "a", "module": "exam", "related_files": []}]},
            task_list=[_task("t1", "exam")],
            module_list=[{"module": "exam", "summary": "考試模組"}],
            python_project_path=str(tmp_path),
        )
        result = run_debug_analysis(state)
        assert result["give_up_early"] is True
        assert result["debug_rounds"][0]["fixable"] is False

    def test_one_fixable_module_does_not_give_up(self, monkeypatch, tmp_path):
        def _fake_call(**kwargs):
            return {
                "root_cause_summary": "缺欄位", "fixable": True,
                "task_fixes": [{"task_id": "t1", "diagnosis": "d", "fixed_body": "補上 score", "file_fixes": []}],
                "unfixable_reasons": [],
            }

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        state = _base_state(
            test_results={"failures": [{"case_id": "a", "module": "exam", "related_files": []}]},
            task_list=[_task("t1", "exam")],
            module_list=[{"module": "exam", "summary": "考試模組"}],
            python_project_path=str(tmp_path),
        )
        result = run_debug_analysis(state)
        assert result["give_up_early"] is False
        assert result["pending_fixed_bodies"] == {"t1": "補上 score"}
        assert result["unanalyzed_root_cause_modules"] == []

    def test_file_fixes_flattened_into_pending_file_fixes_with_task_id(self, monkeypatch, tmp_path):
        """10a 八章「phase 2」：task_fixes[].file_fixes 攤平成
        state["pending_file_fixes"]，每一筆補上來源 task_id。"""
        def _fake_call(**kwargs):
            return {
                "root_cause_summary": "import 錯誤", "fixable": True,
                "task_fixes": [{
                    "task_id": "t1", "diagnosis": "d", "fixed_body": None,
                    "file_fixes": [{
                        "target_file": "app/routers/exam_router.py",
                        "old_snippet": "from app.schemas.a import X",
                        "new_snippet": "from app.schemas.b import X",
                    }],
                }],
                "unfixable_reasons": [],
            }

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        state = _base_state(
            test_results={"failures": [{"case_id": "a", "module": "exam", "related_files": []}]},
            task_list=[_task("t1", "exam")],
            module_list=[{"module": "exam", "summary": "考試模組"}],
            python_project_path=str(tmp_path),
        )
        result = run_debug_analysis(state)
        assert result["give_up_early"] is False
        assert result["pending_fixed_bodies"] == {}
        assert result["pending_file_fixes"] == [{
            "task_id": "t1", "target_file": "app/routers/exam_router.py",
            "old_snippet": "from app.schemas.a import X", "new_snippet": "from app.schemas.b import X",
        }]

    def test_hallucinated_task_id_stripped_and_fixable_recomputed(self, monkeypatch, tmp_path):
        # 10a 4.2：fixable 必須從「4.3 驗證過」的 task_fixes 推導，LLM 宣稱
        # fixable=True 但唯一一筆 task_fix 引用不存在的 task_id 時，剔除
        # 後 task_fixes 變空，fixable 必須跟著變 False。
        def _fake_call(**kwargs):
            return {
                "root_cause_summary": "x", "fixable": True,
                "task_fixes": [{"task_id": "does_not_exist", "diagnosis": "d", "fixed_body": "f", "file_fixes": []}],
                "unfixable_reasons": [],
            }

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        state = _base_state(
            test_results={"failures": [{"case_id": "a", "module": "exam", "related_files": []}]},
            task_list=[_task("t1", "exam")],
            module_list=[{"module": "exam", "summary": ""}],
            python_project_path=str(tmp_path),
        )
        result = run_debug_analysis(state)
        round_ = result["debug_rounds"][0]
        assert round_["fixable"] is False
        assert round_["task_fixes"] == []
        assert any("視為分析失敗" in r for r in round_["unfixable_reasons"])

    def test_api_failure_after_retry_treated_as_unanalyzed_not_unfixable(self, monkeypatch, tmp_path):
        """對應 10a 4.4／七章「give_up_early 的判斷邊界」：兩個
        root_cause module，一個分析成功判定不可修，另一個 API 呼叫失敗
        （重試仍失敗）——give_up_early 必須是 False，不能把「沒分析到」
        當成「分析後判定不可修」的證據。"""
        monkeypatch.setattr(analysis, "time", type("_T", (), {"sleep": staticmethod(lambda s: None)})())

        def _fake_call(**kwargs):
            payload = kwargs["user_prompt"]
            if "flaky" in payload:
                raise analysis.LlmJsonError("暫時性錯誤")
            return {"root_cause_summary": "沒救", "fixable": False, "task_fixes": [], "unfixable_reasons": ["x"]}

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        state = _base_state(
            test_results={"failures": [
                {"case_id": "a", "module": "stable", "related_files": []},
                {"case_id": "b", "module": "flaky", "related_files": []},
            ]},
            task_list=[_task("t1", "stable"), _task("t2", "flaky")],
            module_list=[{"module": "stable", "summary": "stable"}, {"module": "flaky", "summary": "flaky"}],
            python_project_path=str(tmp_path),
        )
        result = run_debug_analysis(state)
        assert result["give_up_early"] is False
        modules_analyzed = {r["module"] for r in result["debug_rounds"]}
        assert modules_analyzed == {"stable"}  # flaky 未分析，不產生 DebugRound
        # 對應 give_up_node.py：把「呼叫失敗」跟「分析後判定不可修」分開
        # 回報，讓人工不用自己翻 debug_rounds 才能發現 flaky 沒被分析到。
        assert result["unanalyzed_root_cause_modules"] == ["flaky"]

    def test_history_in_state_debug_rounds_does_not_affect_this_round(self, monkeypatch, tmp_path):
        """對應 10a 七章「為什麼一定要是這一輪、不能是累積歷史」：舊有
        歷史裡這個 module 曾經 fixable=True，這一輪重新分析判定
        fixable=False，give_up_early 必須正確反映這一輪，不能被歷史裡的
        舊 True 記錄卡住。"""
        def _fake_call(**kwargs):
            return {"root_cause_summary": "沒救", "fixable": False, "task_fixes": [], "unfixable_reasons": ["x"]}

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        state = _base_state(
            test_results={"failures": [{"case_id": "a", "module": "exam", "related_files": []}]},
            task_list=[_task("t1", "exam")],
            module_list=[{"module": "exam", "summary": ""}],
            python_project_path=str(tmp_path),
            debug_rounds=[
                {"round": 0, "module": "exam", "origin": "root_cause", "fixable": True,
                 "root_cause_summary": "x", "task_fixes": [{"task_id": "t1", "diagnosis": "d", "fixed_body": "f", "file_fixes": []}],
                 "unfixable_reasons": []},
            ],
        )
        result = run_debug_analysis(state)
        assert result["give_up_early"] is True


class TestServiceDiagnosticsPassedUnconditionally:
    def test_passed_even_without_special_reason(self, monkeypatch, tmp_path):
        captured = {}

        def _fake_call(**kwargs):
            captured["user_prompt"] = kwargs["user_prompt"]
            return {"root_cause_summary": "x", "fixable": False, "task_fixes": [], "unfixable_reasons": ["x"]}

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        state = _base_state(
            test_results={"failures": [{"case_id": "a", "module": "exam", "related_files": []}]},
            task_list=[_task("t1", "exam")],
            module_list=[{"module": "exam", "summary": ""}],
            python_project_path=str(tmp_path),
            service_diagnostics="Traceback: ImportError in order_service.py",
        )
        run_debug_analysis(state)
        assert "ImportError in order_service.py" in captured["user_prompt"]


class TestRelatedFilesTargetFilesUnion:
    def test_nonempty_related_files_only_adds_global_infra_from_target_files(self, monkeypatch, tmp_path):
        captured = {}

        def _fake_call(**kwargs):
            import json
            captured["payload"] = json.loads(kwargs["user_prompt"])
            return {"root_cause_summary": "x", "fixable": False, "task_fixes": [], "unfixable_reasons": ["x"]}

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        (tmp_path / "app" / "routers").mkdir(parents=True)
        (tmp_path / "app" / "routers" / "exam_router.py").write_text("router code", encoding="utf-8")
        (tmp_path / "app" / "schemas").mkdir(parents=True)
        (tmp_path / "app" / "schemas" / "exam.py").write_text("schema code", encoding="utf-8")
        (tmp_path / "app" / "services").mkdir(parents=True)
        # referenced_interfaces 帶進來的跨 module 檔案，不該被讀進來——
        # related_files 非空時只補 schemas/models/core。
        (tmp_path / "app" / "services" / "other_service.py").write_text("other", encoding="utf-8")

        state = _base_state(
            test_results={"failures": [
                {"case_id": "a", "module": "exam", "related_files": ["app/routers/exam_router.py"]},
            ]},
            task_list=[_task(
                "t1", "exam",
                target_files=["app/routers/exam_router.py", "app/schemas/exam.py", "app/services/other_service.py"],
            )],
            module_list=[{"module": "exam", "summary": ""}],
            python_project_path=str(tmp_path),
        )
        run_debug_analysis(state)
        source_files = captured["payload"]["source_files"]
        assert "app/routers/exam_router.py" in source_files
        assert "app/schemas/exam.py" in source_files
        assert "app/services/other_service.py" not in source_files

    def test_own_task_target_files_0_always_included_even_if_services_layer(self, monkeypatch, tmp_path):
        """對應真實環境重跑時發現的缺陷：同一 module 底下另一個 task 的
        target_files[0]（自己的主要寫入目標，不是 referenced_interfaces
        帶進來的參考檔案）即使父目錄是 services，也該永遠納入——
        route_to_file_mapping 涵蓋不到這種真正的根因所在（見
        docs/09b_bug_trace.md #43，實測案例：CollectionUtil.
        find_distinct_field() 的 list 遮蔽 builtin），只有
        target_files[1:] 才該套用 schemas/models/core 過濾。"""
        captured = {}

        def _fake_call(**kwargs):
            import json
            captured["payload"] = json.loads(kwargs["user_prompt"])
            return {"root_cause_summary": "x", "fixable": False, "task_fixes": [], "unfixable_reasons": ["x"]}

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        (tmp_path / "app" / "routers").mkdir(parents=True)
        (tmp_path / "app" / "routers" / "exam_router.py").write_text("router code", encoding="utf-8")
        (tmp_path / "app" / "services").mkdir(parents=True)
        (tmp_path / "app" / "services" / "common_service.py").write_text(
            "class CollectionUtil:\n    def find_distinct_field(self, list, mapper):\n        return list(set())\n",
            encoding="utf-8",
        )

        state = _base_state(
            test_results={"failures": [
                {"case_id": "a", "module": "exam", "related_files": ["app/routers/exam_router.py"]},
            ]},
            task_list=[
                _task("t_router", "exam", target_files=["app/routers/exam_router.py"]),
                # 這是這個 module 自己的 task，target_files[0] 就是它自己
                # 的主要寫入目標——不是 t_router 的 referenced_interfaces。
                _task("t_svc", "exam", target_files=["app/services/common_service.py"]),
            ],
            module_list=[{"module": "exam", "summary": ""}],
            python_project_path=str(tmp_path),
        )
        run_debug_analysis(state)
        source_files = captured["payload"]["source_files"]
        assert "app/services/common_service.py" in source_files

    def test_empty_related_files_falls_back_to_full_target_files(self, monkeypatch, tmp_path):
        captured = {}

        def _fake_call(**kwargs):
            import json
            captured["payload"] = json.loads(kwargs["user_prompt"])
            return {"root_cause_summary": "x", "fixable": False, "task_fixes": [], "unfixable_reasons": ["x"]}

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        (tmp_path / "app" / "routers").mkdir(parents=True)
        (tmp_path / "app" / "routers" / "exam_router.py").write_text("router code", encoding="utf-8")

        state = _base_state(
            # golden_not_found：related_files 逐筆是空清單。
            test_results={"failures": [{"case_id": "a", "module": "exam", "related_files": []}]},
            task_list=[_task("t1", "exam", target_files=["app/routers/exam_router.py"])],
            module_list=[{"module": "exam", "summary": ""}],
            python_project_path=str(tmp_path),
        )
        run_debug_analysis(state)
        source_files = captured["payload"]["source_files"]
        assert "app/routers/exam_router.py" in source_files


class TestZeroEndpointModuleFileInclusion:
    """docs/09b_bug_trace.md #49：module_task_ids 分組只看
    test_results.failures 的 module 欄位，一個沒有任何 HTTP endpoint 的
    模組（如 common）永遠不會出現在裡面，3.2 的機械分類永遠選不中它去
    分析——只能靠這裡：它的檔案被別的 module 的 referenced_interfaces
    帶進 target_files[1:] 時，_is_global_infra_file() 不能再只看目錄名。
    """

    def test_file_owned_by_zero_endpoint_module_is_included(self, monkeypatch, tmp_path):
        captured = {}

        def _fake_call(**kwargs):
            import json
            captured["payload"] = json.loads(kwargs["user_prompt"])
            return {"root_cause_summary": "x", "fixable": False, "task_fixes": [], "unfixable_reasons": ["x"]}

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)
        # exam 有 route（route_to_module_mapping 命中），common 沒有
        # ——common 因此進入 zero_endpoint_modules。
        monkeypatch.setattr(analysis, "RouteMapper", lambda: _FakeRouteMapper({"GET_api_exam_x": "exam"}))

        (tmp_path / "app" / "routers").mkdir(parents=True)
        (tmp_path / "app" / "routers" / "exam_router.py").write_text("router code", encoding="utf-8")
        (tmp_path / "app" / "services").mkdir(parents=True)
        (tmp_path / "app" / "services" / "common_service.py").write_text("common code", encoding="utf-8")

        state = _base_state(
            test_results={"failures": [
                {"case_id": "a", "module": "exam", "related_files": ["app/routers/exam_router.py"]},
            ]},
            task_list=[
                # exam 自己的 task 把 common_service.py 當成
                # referenced_interfaces 帶進 target_files[1:]。
                _task("t_exam", "exam", target_files=[
                    "app/routers/exam_router.py", "app/services/common_service.py",
                ]),
                # common_service.py 真正的擁有者：common module 自己的
                # task，沒有任何 endpoint 指向它，test_results.failures
                # 裡永遠不會出現 module=="common"。
                _task("t_common", "common", target_files=["app/services/common_service.py"]),
            ],
            module_list=[{"module": "exam", "summary": ""}, {"module": "common", "summary": ""}],
            python_project_path=str(tmp_path),
        )
        run_debug_analysis(state)
        source_files = captured["payload"]["source_files"]
        assert "app/services/common_service.py" in source_files

    def test_file_owned_by_endpoint_module_still_excluded(self, monkeypatch, tmp_path):
        """對照組：同樣是父目錄 services、同樣被跨 module 引用，但擁有者
        （exam2）自己有 route，不屬於 #49 修的這一類——維持既有「只給
        schemas/models/core」的限縮，不能因為這次擴充而濫入。"""
        captured = {}

        def _fake_call(**kwargs):
            import json
            captured["payload"] = json.loads(kwargs["user_prompt"])
            return {"root_cause_summary": "x", "fixable": False, "task_fixes": [], "unfixable_reasons": ["x"]}

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)
        # exam 跟 exam2 都有 route，都不在 zero_endpoint_modules 裡。
        monkeypatch.setattr(
            analysis, "RouteMapper",
            lambda: _FakeRouteMapper({"GET_api_exam_x": "exam", "GET_api_exam2_y": "exam2"}),
        )

        (tmp_path / "app" / "routers").mkdir(parents=True)
        (tmp_path / "app" / "routers" / "exam_router.py").write_text("router code", encoding="utf-8")
        (tmp_path / "app" / "services").mkdir(parents=True)
        (tmp_path / "app" / "services" / "exam2_service.py").write_text("exam2 code", encoding="utf-8")

        state = _base_state(
            test_results={"failures": [
                {"case_id": "a", "module": "exam", "related_files": ["app/routers/exam_router.py"]},
            ]},
            task_list=[
                _task("t_exam", "exam", target_files=[
                    "app/routers/exam_router.py", "app/services/exam2_service.py",
                ]),
                _task("t_exam2", "exam2", target_files=["app/services/exam2_service.py"]),
            ],
            module_list=[{"module": "exam", "summary": ""}, {"module": "exam2", "summary": ""}],
            python_project_path=str(tmp_path),
        )
        run_debug_analysis(state)
        source_files = captured["payload"]["source_files"]
        assert "app/services/exam2_service.py" not in source_files


class TestGlobalModuleUnconditionalInclusion:
    """docs/09b_bug_trace.md #49：`_global` 保留模組的檔案（如
    app/core/exception_handlers.py）機械上是「被 app.add_exception_handler()
    這類程式碼直接呼叫掛上去的」，從未出現在任何模組的 referenced_interfaces
    裡，沒有機會透過 target_files 聯集被看到——必須無條件塞給每個
    root_cause module。
    """

    def test_global_module_file_attached_even_without_reference(self, monkeypatch, tmp_path):
        captured = {}

        def _fake_call(**kwargs):
            import json
            captured["payload"] = json.loads(kwargs["user_prompt"])
            return {"root_cause_summary": "x", "fixable": False, "task_fixes": [], "unfixable_reasons": ["x"]}

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        (tmp_path / "app" / "routers").mkdir(parents=True)
        (tmp_path / "app" / "routers" / "exam_router.py").write_text("router code", encoding="utf-8")
        (tmp_path / "app" / "core").mkdir(parents=True)
        (tmp_path / "app" / "core" / "exception_handlers.py").write_text("handler code", encoding="utf-8")

        state = _base_state(
            test_results={"failures": [
                {"case_id": "a", "module": "exam", "related_files": ["app/routers/exam_router.py"]},
            ]},
            task_list=[
                _task("t_exam", "exam", target_files=["app/routers/exam_router.py"]),
                # `_global` 自己的 task——沒有任何一個 exam task 把這個
                # 檔案列進 target_files，純粹靠 module=="_global" 被無條件
                # 撈出來。
                _task("t_global", "_global", target_files=["app/core/exception_handlers.py"]),
            ],
            module_list=[{"module": "exam", "summary": ""}, {"module": "_global", "summary": ""}],
            python_project_path=str(tmp_path),
        )
        run_debug_analysis(state)
        source_files = captured["payload"]["source_files"]
        assert "app/core/exception_handlers.py" in source_files

    def test_global_module_files_not_read_when_no_root_cause_module(self, monkeypatch, tmp_path):
        """root_cause_ctxs 為空（這一輪整批 give_up_early）時，不需要為了
        沒有任何 LLM 呼叫的這一輪去讀 `_global` 的檔案——用
        RouteMapper 完全沒被建構過來間接驗證這條短路確實生效（見
        run_debug_analysis() 內「全部條件在 root_cause_ctxs 非空時才算」）。
        """
        def _boom(*a, **k):
            raise AssertionError("root_cause_ctxs 為空時不該建構 RouteMapper")

        monkeypatch.setattr(analysis, "RouteMapper", _boom)
        monkeypatch.setattr(
            analysis, "call_claude_for_json",
            lambda **kwargs: (_ for _ in ()).throw(AssertionError("不該呼叫 LLM")),
        )

        state = _base_state(
            test_results={"failures": [{"case_id": "a", "module": "gap_mod"}]},
            task_failures=[
                {"task_id": "t_gap", "module": "gap_mod", "file_path": "x", "class_name": None,
                 "function_name": "f", "reason": "scaffold_skipped", "error": "e"},
            ],
            task_list=[_task("t_gap", "gap_mod")],
        )
        result = run_debug_analysis(state)
        assert result["give_up_early"] is True


class TestBodyDiffWithNonJsonSerializableTypeObjects:
    """對應真實環境重跑時發現的缺陷：DeepDiff 的 body_diff 可能含 Python
    type 物件（如 {'old_type': <class 'int'>}），_build_user_prompt() 組
    payload 時若沒有 default=str 兜底，json.dumps() 會直接 TypeError，
    在真實 harness_failures 資料上 100% 會炸——跟
    refactor_harness/langgraph_nodes/test_nodes.py::run_postman_tests()
    早就處理過的同一個坑，這裡原本沒接上。"""

    def test_type_object_in_body_diff_does_not_crash(self, monkeypatch, tmp_path):
        def _fake_call(**kwargs):
            return {"root_cause_summary": "x", "fixable": False, "task_fixes": [], "unfixable_reasons": ["x"]}

        monkeypatch.setattr(analysis, "call_claude_for_json", _fake_call)

        state = _base_state(
            test_results={"failures": [{
                "case_id": "a", "module": "exam", "related_files": [],
                "body_diff": {"type_changes": {"root['code']": {"old_type": int, "new_type": str}}},
            }]},
            task_list=[_task("t1", "exam")],
            module_list=[{"module": "exam", "summary": ""}],
            python_project_path=str(tmp_path),
        )

        result = run_debug_analysis(state)  # 不應該拋出 TypeError

        assert result["debug_rounds"][0]["fixable"] is False
