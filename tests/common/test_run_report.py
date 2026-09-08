"""common/run_report.py：pipeline 結束時的制式摘要報告，依日期分資料夾。"""
import json
import os

import pytest

from common import llm_trace
from common.llm_trace import record_llm_call
from common.run_report import write_human_readable_report, write_run_report


@pytest.fixture(autouse=True)
def _isolated_llm_trace_db(tmp_path, monkeypatch):
    # 對應 docs/refactor_bug_trace.md #33：_compute_summary() 現在會查
    # common/llm_trace.py 的 llm_traces.db，跟 tests/common/test_llm_trace.py
    # 用同一套隔離手法（模組層級 _connection 是 singleton，每個測試前後
    # 重置成 None，逼下一次呼叫重新走 lazy init，不共用其他測試留下的連線）。
    monkeypatch.setenv("LLM_TRACE_DB_PATH", str(tmp_path / "llm_traces.db"))
    monkeypatch.setenv("LLM_TRACE_PAYLOAD_DIR", str(tmp_path / "payloads"))
    monkeypatch.setattr(llm_trace, "_connection", None)
    yield
    monkeypatch.setattr(llm_trace, "_connection", None)


def _base_state(**overrides) -> dict:
    state = {
        "run_id": "20260823_143000_a1b2c3",
        "retry_count": 0,
        "test_results": {"status": "pass", "summary": {"total": 10, "passed": 10, "failed": 0, "pass_rate": 1.0}},
        "task_list": [{"id": "t1", "module": "exam"}, {"id": "t2", "module": "exam"}],
        "completed_tasks": ["t1", "t2"],
        "failed_tasks": [],
        "failed_modules": [],
        "blocked_modules": [],
        "verified_modules": [],
        "task_failures": [],
        "debug_rounds": [],
        "give_up_early": False,
        "scaffold_done": True,
        "unanalyzed_root_cause_modules": [],
    }
    state.update(overrides)
    return state


class TestWriteRunReport:
    def test_writes_to_date_folder_derived_from_run_id(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        state = _base_state()

        report_path = write_run_report(state)

        expected_path = os.path.join("logs", "reports", "2026-08-23", "20260823_143000_a1b2c3.json")
        assert report_path == expected_path
        assert (tmp_path / expected_path).exists()

    def test_two_runs_same_day_do_not_collide(self, monkeypatch, tmp_path):
        # run_id 本身帶完整 HHMMSS＋隨機碼（見 common/run_context.py::
        # new_run_id()），同一天多次執行落在同一個日期資料夾，但檔名
        # 各自不同，不會互相覆蓋——這裡鎖住這個行為，不只是口頭保證。
        monkeypatch.chdir(tmp_path)
        run_a = write_run_report(_base_state(run_id="20260823_091500_7f3a2c"))
        run_b = write_run_report(_base_state(run_id="20260823_161842_c4d9e1"))

        assert run_a != run_b
        assert os.path.dirname(run_a) == os.path.dirname(run_b)  # 同一個日期資料夾
        assert (tmp_path / run_a).exists()
        assert (tmp_path / run_b).exists()  # 沒有覆蓋掉前一份

        with open(run_a, encoding="utf-8") as f:
            assert json.load(f)["run_id"] == "20260823_091500_7f3a2c"
        with open(run_b, encoding="utf-8") as f:
            assert json.load(f)["run_id"] == "20260823_161842_c4d9e1"

    def test_report_content_matches_state(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        state = _base_state(
            retry_count=2,
            completed_tasks=["t1", "t2", "t3"],
            failed_tasks=["t4"],
            task_failures=[{"task_id": "t4", "module": "exam", "file_path": "x", "class_name": None,
                             "function_name": "f", "reason": "fill_failed", "error": "e"}],
            debug_rounds=[{"round": 0, "module": "exam", "origin": "root_cause", "fixable": True,
                            "root_cause_summary": "x", "task_fixes": [], "unfixable_reasons": []}],
        )

        report_path = write_run_report(state)

        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)

        assert report["run_id"] == "20260823_143000_a1b2c3"
        assert report["retry_count"] == 2
        assert report["completed_tasks_count"] == 3
        assert report["failed_tasks_count"] == 1
        assert report["task_failures"] == state["task_failures"]
        assert report["debug_rounds"] == state["debug_rounds"]
        assert report["test_summary"] == {"total": 10, "passed": 10, "failed": 0, "pass_rate": 1.0}

    def test_full_failures_list_not_duplicated_only_summary(self, monkeypatch, tmp_path):
        # 詳細的 body_diff 已經在 logs/report_{run_id}.json 裡，這份報告
        # 只要 summary，不重複整份 failures。
        monkeypatch.chdir(tmp_path)
        state = _base_state(test_results={
            "status": "fail",
            "summary": {"total": 5, "passed": 3, "failed": 2, "pass_rate": 0.6},
            "failures": [{"case_id": "a", "body_diff": {"huge": "data"}}],
        })

        report_path = write_run_report(state)
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)

        assert "failures" not in report["test_summary"]
        assert report["test_summary"]["failed"] == 2


class TestClassifyOutcome:
    def test_pass_status_is_done(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        report_path = write_run_report(_base_state())
        with open(report_path, encoding="utf-8") as f:
            assert json.load(f)["outcome"] == "done"

    def test_scaffold_failure_takes_priority(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        state = _base_state(
            test_results={"status": "fail", "summary": {}},
            scaffold_done=False,
            give_up_early=True,  # 就算兩個條件都符合，scaffold 失敗優先判斷
        )
        report_path = write_run_report(state)
        with open(report_path, encoding="utf-8") as f:
            assert json.load(f)["outcome"] == "give_up_scaffold_failed"

    def test_give_up_early(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        state = _base_state(test_results={"status": "fail", "summary": {}}, give_up_early=True)
        report_path = write_run_report(state)
        with open(report_path, encoding="utf-8") as f:
            assert json.load(f)["outcome"] == "give_up_early"

    def test_retry_exhausted_is_the_fallback(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        state = _base_state(test_results={"status": "fail", "summary": {}}, give_up_early=False)
        report_path = write_run_report(state)
        with open(report_path, encoding="utf-8") as f:
            assert json.load(f)["outcome"] == "give_up_retry_exhausted"


class TestWriteHumanReadableReport:
    def test_writes_markdown_next_to_json_report_same_date_folder(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        summary_path = write_human_readable_report(_base_state())

        expected_path = os.path.join("logs", "reports", "2026-08-23", "20260823_143000_a1b2c3.md")
        assert summary_path == expected_path
        assert (tmp_path / expected_path).exists()

    def test_writing_markdown_does_not_touch_the_json_report_shape(self, monkeypatch, tmp_path):
        # 對應使用者要求：原本的制式 JSON 報告維持不變，Markdown 是另外
        # 獨立的一份，不是取代或改動 write_run_report() 的輸出。
        monkeypatch.chdir(tmp_path)
        state = _base_state()

        json_path = write_run_report(state)
        md_path = write_human_readable_report(state)

        with open(json_path, encoding="utf-8") as f:
            report = json.load(f)
        assert set(report.keys()) == {
            "run_id", "outcome", "retry_count", "test_summary", "completed_tasks_count",
            "failed_tasks_count", "task_failures", "debug_rounds", "unanalyzed_root_cause_modules",
            "local_model_call_failures",
        }
        assert md_path != json_path

    def test_summary_counts_total_translated_and_issue_counts(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        state = _base_state(
            task_list=[{"id": f"t{i}", "module": "exam"} for i in range(1, 6)],
            completed_tasks=["t1", "t2", "t3", "t4", "t5"],
            task_failures=[
                {"task_id": "t3", "module": "exam", "file_path": "x", "class_name": None,
                 "function_name": "f", "reason": "fill_failed", "error": "e"},
                {"task_id": "t5", "module": "exam", "file_path": "y", "class_name": None,
                 "function_name": "g", "reason": "scaffold_skipped", "error": "e"},
            ],
        )

        content = open(write_human_readable_report(state), encoding="utf-8").read()

        assert "總共需要翻譯的函式數：5" in content
        assert "成功產生程式碼：5" in content
        assert "曾經翻譯失敗（含後來補救成功的）：2" in content
        assert "永久無法生成（骨架缺口，需人工介入）：1 個 → t5" in content

    def test_local_model_call_failures_surfaced_from_llm_trace_db(self, monkeypatch, tmp_path):
        """對應 docs/refactor_bug_trace.md #33：本地模型（qwen）呼叫失敗、
        退回 Claude 才成功的情況不會進 task_failures（task 整體是成功
        的），這個訊號只活在 llm_traces.db 裡——真實案例 `e867f4` 這輪
        13 個函式，事後只能靠手動 `llmlog` 排查才發現。_compute_summary()
        改成直接查 llm_traces.db，讓這個訊號在報告裡看得到。"""
        monkeypatch.chdir(tmp_path)
        state = _base_state(run_id="20260906_060838_e867f4")
        record_llm_call(
            trace_id="tr1", run_id="20260906_060838_e867f4", vendor="ollama", model="qwen2.5-coder:32b",
            caller="ollama_client.get_function_body", task_id="task_036", prompt="p", response="不對題的回應",
            latency_ms=1000, status="error", error_msg="delimiter 契約違反",
        )
        record_llm_call(
            trace_id="tr2", run_id="20260906_060838_e867f4", vendor="claude", model="claude-sonnet-4-6",
            caller="client.fill_function", task_id="task_036", prompt="p", response="ok",
            latency_ms=500, status="ok",
        )
        # 不相干的資料：不同 run_id、vendor 是 claude、task_id 是 None，
        # 都不該被算進去。
        record_llm_call(
            trace_id="tr3", run_id="other_run", vendor="ollama", model="qwen2.5-coder:32b",
            caller="c", task_id="task_999", prompt="p", response="r", latency_ms=1, status="error",
        )
        record_llm_call(
            trace_id="tr4", run_id="20260906_060838_e867f4", vendor="claude", model="claude-sonnet-4-6",
            caller="c", task_id="task_037", prompt="p", response="r", latency_ms=1, status="error",
        )

        content = open(write_human_readable_report(state), encoding="utf-8").read()

        assert "本地模型（qwen）呼叫失敗、退回 Claude 才成功：1 個 → task_036" in content

    def test_local_model_call_failures_zero_when_none(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        state = _base_state()

        content = open(write_human_readable_report(state), encoding="utf-8").read()

        assert "本地模型（qwen）呼叫失敗、退回 Claude 才成功：0" in content

    def test_module_fixed_vs_still_broken_derivation(self, monkeypatch, tmp_path):
        """曾被 ⑦ 標記為 root_cause 的 module，最後真的落在
        verified_modules 裡 → 判定修好；沒有 → 判定仍未解決（用正面訊號
        判斷，不是「不在 failed_modules／blocked_modules 裡」這種負面
        推論，見 docs/refactor_bug_trace.md #13）。"""
        monkeypatch.chdir(tmp_path)
        state = _base_state(
            debug_rounds=[
                {"round": 0, "module": "exam", "origin": "root_cause", "fixable": True,
                 "root_cause_summary": "x", "task_fixes": [{"task_id": "t1", "diagnosis": "d", "fixed_body": "f"}],
                 "unfixable_reasons": []},
                {"round": 0, "module": "grading", "origin": "root_cause", "fixable": False,
                 "root_cause_summary": "x", "task_fixes": [], "unfixable_reasons": ["沒救"]},
            ],
            failed_modules=["grading"],
            verified_modules=["exam"],
        )

        content = open(write_human_readable_report(state), encoding="utf-8").read()

        assert "最終確認修好：1 → exam" in content
        assert "仍未解決：1 → grading" in content

    def test_module_stuck_in_progress_is_not_misreported_as_fixed(self, monkeypatch, tmp_path):
        """對應 docs/refactor_bug_trace.md #13 真實案例：module 卡在
        "in_progress"（既不在 failed_modules，也不在 blocked_modules，
        也不在 verified_modules）時，不該被誤判成「修好了」——它就是
        「還沒能證實修好」，該出現在仍未解決清單裡。"""
        monkeypatch.chdir(tmp_path)
        state = _base_state(
            debug_rounds=[
                {"round": 0, "module": "exam", "origin": "root_cause", "fixable": True,
                 "root_cause_summary": "x", "task_fixes": [{"task_id": "t1", "diagnosis": "d", "fixed_body": "f"}],
                 "unfixable_reasons": []},
            ],
            failed_modules=[],
            blocked_modules=[],
            verified_modules=[],  # exam 卡在 in_progress，三個集合都沒有它
        )

        content = open(write_human_readable_report(state), encoding="utf-8").read()

        assert "最終確認修好：0" in content
        assert "仍未解決：1 → exam" in content

    def test_no_issues_at_all_shows_zero_everywhere(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        content = open(write_human_readable_report(_base_state()), encoding="utf-8").read()

        assert "永久無法生成（骨架缺口）：0" in content
        assert "仍未解決：0" in content
        assert "llmlog" not in content  # 沒有 unanalyzed_root_cause_modules 就不該印出這段

    def test_outcome_label_rendered_in_traditional_chinese(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        content = open(write_human_readable_report(_base_state()), encoding="utf-8").read()
        assert "成功完成" in content

    def test_unanalyzed_modules_flagged_in_markdown(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        state = _base_state(
            test_results={"status": "fail", "summary": {}},
            give_up_early=False,
            unanalyzed_root_cause_modules=["exam"],
        )
        content = open(write_human_readable_report(state), encoding="utf-8").read()
        assert "llmlog" in content
        assert "exam" in content
