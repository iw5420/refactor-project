"""common/run_report.py：pipeline 結束時的制式摘要報告，依日期分資料夾。"""
import json
import os

from common.run_report import write_human_readable_report, write_run_report


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

    def test_module_fixed_vs_still_broken_derivation(self, monkeypatch, tmp_path):
        """曾被 ⑦ 標記為 root_cause 的 module，最後不在
        failed_modules／blocked_modules 裡 → 判定修好；還在裡面 → 判定
        仍未解決。"""
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
        )

        content = open(write_human_readable_report(state), encoding="utf-8").read()

        assert "最終確認修好：1 → exam" in content
        assert "仍未解決：1 → grading" in content

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
