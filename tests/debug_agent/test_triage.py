"""debug_agent/triage.py，對應 docs/10a_debug_agent_architecture.md 三章。"""
from debug_agent.triage import build_module_contexts


def _task(task_id: str, module: str) -> dict:
    return {
        "id": task_id, "module": module, "description": "d",
        "target_files": [f"app/services/{module}_service.py"],
        "context": "", "depends_on": [], "class_name": None, "function_name": "do_it",
    }


class TestBuildModuleContextsOrigins:
    def test_blocked_module_not_from_task_analysis(self):
        state = {
            "test_results": {"failures": [{"case_id": "a", "module": "downstream"}]},
            "task_failures": [],
            "task_list": [_task("t1", "downstream")],
            "blocked_modules": ["downstream"],
            "partial_reports": [],
            "retry_count": 0,
        }
        contexts = build_module_contexts(state)
        assert len(contexts) == 1
        assert contexts[0]["origin"] == "blocked"

    def test_module_mismatch_when_no_matching_task(self):
        state = {
            "test_results": {"failures": [{"case_id": "a", "module": "ghost"}]},
            "task_failures": [],
            "task_list": [_task("t1", "other")],
            "blocked_modules": [],
            "partial_reports": [],
            "retry_count": 0,
        }
        contexts = build_module_contexts(state)
        assert contexts[0]["origin"] == "module_mismatch"

    def test_scaffold_gap_when_all_tasks_skipped(self):
        state = {
            "test_results": {"failures": [{"case_id": "a", "module": "exam"}]},
            "task_failures": [
                {"task_id": "t1", "module": "exam", "file_path": "x", "class_name": None,
                 "function_name": "f", "reason": "scaffold_skipped", "error": "e"},
            ],
            "task_list": [_task("t1", "exam")],
            "blocked_modules": [],
            "partial_reports": [],
            "retry_count": 0,
        }
        contexts = build_module_contexts(state)
        assert contexts[0]["origin"] == "scaffold_gap"

    def test_root_cause_when_mixed_scaffold_gap_and_normal_tasks(self):
        # 同一 module 部分 task 是 scaffold_skipped、部分正常——不能全有
        # 全無地機械判斷，落到 root_cause 交給 LLM。
        state = {
            "test_results": {"failures": [{"case_id": "a", "module": "exam"}]},
            "task_failures": [
                {"task_id": "t1", "module": "exam", "file_path": "x", "class_name": None,
                 "function_name": "f", "reason": "scaffold_skipped", "error": "e"},
            ],
            "task_list": [_task("t1", "exam"), _task("t2", "exam")],
            "blocked_modules": [],
            "partial_reports": [],
            "retry_count": 0,
        }
        contexts = build_module_contexts(state)
        assert contexts[0]["origin"] == "root_cause"
        assert contexts[0]["scaffold_gap_task_ids"] == {"t1"}

    def test_root_cause_default_case(self):
        state = {
            "test_results": {"failures": [{"case_id": "a", "module": "exam"}]},
            "task_failures": [],
            "task_list": [_task("t1", "exam")],
            "blocked_modules": [],
            "partial_reports": [],
            "retry_count": 0,
        }
        contexts = build_module_contexts(state)
        assert contexts[0]["origin"] == "root_cause"


class TestServiceUnreachableFallback:
    def test_falls_back_to_failed_modules_when_service_unreachable(self):
        state = {
            "test_results": {"status": "fail", "reason": "service_unreachable", "failures": []},
            "task_failures": [],
            "task_list": [_task("t1", "exam")],
            "blocked_modules": [],
            "failed_modules": ["exam"],
            "partial_reports": [],
            "retry_count": 0,
        }
        contexts = build_module_contexts(state)
        assert len(contexts) == 1
        assert contexts[0]["module"] == "exam"
        assert contexts[0]["harness_failures"] == []
        assert contexts[0]["origin"] == "root_cause"

    def test_empty_failed_modules_yields_empty_contexts(self):
        state = {
            "test_results": {"status": "fail", "reason": "service_unreachable", "failures": []},
            "task_failures": [],
            "task_list": [],
            "blocked_modules": [],
            "failed_modules": [],
            "partial_reports": [],
            "retry_count": 0,
        }
        assert build_module_contexts(state) == []


class TestBatchSiblingModulesRoundFiltering:
    """對應 10a 3.5「過期資料」：batch_sibling_modules 只看「這一輪」的
    partial_reports 紀錄，不能抓到跨輪次的過期資料。"""

    def test_stale_round_not_included_as_sibling(self):
        state = {
            "test_results": {"failures": [
                {"case_id": "a", "module": "A"},
                {"case_id": "b", "module": "C"},
            ]},
            "task_failures": [],
            "task_list": [_task("ta", "A"), _task("tc", "C")],
            "blocked_modules": [],
            "partial_reports": [
                {"module": "A", "round": 0, "report": {"status": "fail", "reason": "batch_reload_timeout"}},
                {"module": "C", "round": 1, "report": {"status": "fail", "reason": "batch_reload_timeout"}},
            ],
            "retry_count": 1,
        }
        contexts = build_module_contexts(state)
        by_module = {c["module"]: c for c in contexts}
        assert by_module["C"]["special_reason"] == "batch_reload_timeout"
        assert by_module["C"]["batch_sibling_modules"] == []  # A 停在 round 0，不是這一輪
        assert by_module["A"]["special_reason"] is None  # 這一輪沒有 A 的新紀錄

    def test_same_round_siblings_included(self):
        state = {
            "test_results": {"failures": [
                {"case_id": "a", "module": "A"},
                {"case_id": "b", "module": "C"},
            ]},
            "task_failures": [],
            "task_list": [_task("ta", "A"), _task("tc", "C")],
            "blocked_modules": [],
            "partial_reports": [
                {"module": "A", "round": 1, "report": {"status": "fail", "reason": "batch_reload_timeout"}},
                {"module": "C", "round": 1, "report": {"status": "fail", "reason": "batch_reload_timeout"}},
            ],
            "retry_count": 1,
        }
        contexts = build_module_contexts(state)
        by_module = {c["module"]: c for c in contexts}
        assert by_module["C"]["batch_sibling_modules"] == ["A"]
        assert by_module["A"]["batch_sibling_modules"] == ["C"]
