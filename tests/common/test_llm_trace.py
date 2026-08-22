# tests/common/test_llm_trace.py
"""common/llm_trace.py，對應 11a_logging_architecture.md 七章「LLM Trace
統一儲存設計」／「查詢與維護層設計」。每個測試各自獨立的 tmp_path 資料庫
與 payload 目錄，避免測試之間互相污染（模組層級的 `_connection` 是
singleton，每個測試前重置成 None，逼下一次呼叫重新走 lazy init）。
"""
import os
import threading
import time

import pytest

from common import llm_trace
from common.llm_trace import (
    flag_trace,
    gc_orphan_payloads,
    get_trace,
    get_traces_for_function,
    get_traces_for_task,
    latest_run_id,
    list_recent,
    record_llm_call,
    record_llm_call_start,
    search_traces,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_TRACE_DB_PATH", str(tmp_path / "llm_traces.db"))
    monkeypatch.setenv("LLM_TRACE_PAYLOAD_DIR", str(tmp_path / "payloads"))
    monkeypatch.setattr(llm_trace, "_connection", None)
    yield
    monkeypatch.setattr(llm_trace, "_connection", None)


def _record(trace_id="t1", **overrides):
    kwargs = dict(
        trace_id=trace_id, run_id="run_A", vendor="claude", model="claude-sonnet-4-6",
        caller="mod.func", task_id="task_1", target_file="app/f.py", class_name="C",
        function_name="g", prompt="hello prompt", response='{"a": 1}',
        input_tokens=10, output_tokens=5, latency_ms=100, status="ok",
    )
    kwargs.update(overrides)
    record_llm_call(**kwargs)


class TestRecordAndGetTrace:
    def test_round_trip_returns_all_fields(self):
        _record()
        record = get_trace("t1")
        assert record is not None
        assert record.trace_id == "t1"
        assert record.run_id == "run_A"
        assert record.prompt == "hello prompt"
        assert record.response == '{"a": 1}'
        assert record.status == "ok"
        assert record.prompt_payload_key is None

    def test_missing_trace_id_returns_none(self):
        assert get_trace("does-not-exist") is None

    def test_write_failure_is_swallowed_not_raised(self, monkeypatch):
        # 稽核用旁路紀錄：寫入失敗只記警告，不能讓例外往外傳、打斷真正在
        # 做的 LLM 呼叫流程（見七章「寫入規則」）。
        real_get_connection = llm_trace._get_connection
        call_count = {"n": 0}

        def _fails_once_then_recovers():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("模擬磁碟壞掉")
            return real_get_connection()

        monkeypatch.setattr(llm_trace, "_get_connection", _fails_once_then_recovers)
        _record()  # 不應該拋出例外
        assert get_trace("t1") is None  # 這次呼叫確實沒有寫入成功


class TestRecordLlmCallStart:
    """對應使用者實際需求：呼叫還沒結束（甚至長時間卡住）就要能查到
    prompt，不必等 finally 補上最終結果（見 11a 七章「呼叫開始即寫入
    running row」）。
    """

    def test_writes_running_row_with_prompt_only(self):
        record_llm_call_start(
            trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
            caller="ollama_client.get_function_body", attempt=1, task_id="task_1",
            target_file="app/f.py", class_name="C", function_name="g",
            prompt="這是還在進行中的 prompt",
        )
        record = get_trace("t1")
        assert record is not None
        assert record.status == "running"
        assert record.prompt == "這是還在進行中的 prompt"
        assert record.response is None
        assert record.latency_ms is None
        assert record.attempt == 1

    def test_running_row_is_queryable_via_list_recent(self):
        record_llm_call_start(
            trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
            caller="mod.func", prompt="進行中",
        )
        assert [r.trace_id for r in list_recent(status="running")] == ["t1"]

    def test_finish_upserts_same_row_not_a_second_row(self):
        record_llm_call_start(
            trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
            caller="mod.func", task_id="task_1", prompt="hello prompt",
        )
        record_llm_call(
            trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
            caller="mod.func", task_id="task_1",
            prompt="hello prompt", response="最終回應",
            latency_ms=500, status="ok",
        )
        all_rows = list_recent()
        assert len(all_rows) == 1
        record = get_trace("t1")
        assert record.status == "ok"
        assert record.response == "最終回應"

    def test_finish_preserves_original_start_timestamp(self):
        record_llm_call_start(
            trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
            caller="mod.func", prompt="hello",
        )
        started_ts = get_trace("t1").ts
        time.sleep(0.01)
        record_llm_call(
            trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
            caller="mod.func", prompt="hello", response="ok",
            latency_ms=10, status="ok",
        )
        assert get_trace("t1").ts == started_ts

    def test_search_finds_running_row_by_prompt_content(self):
        # AFTER INSERT trigger 對只有 prompt 的 start-only row 一樣要正確
        # 同步進 FTS，不是只有 finish 之後才能被搜到。
        record_llm_call_start(
            trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
            caller="mod.func", prompt="need_to_find_this_while_running",
        )
        assert [r.trace_id for r in search_traces("need_to_find_this_while_running")] == ["t1"]

    def test_search_finds_response_after_finish_upsert(self):
        # AFTER UPDATE trigger（upsert 走 UPDATE 分支）也要正確同步 FTS，
        # 讓 finish 後新增的 response 內容一樣能被搜到（見七章「外部
        # content 表需要 trigger 才會同步」）。
        record_llm_call_start(
            trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
            caller="mod.func", prompt="prompt 內容",
        )
        record_llm_call(
            trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
            caller="mod.func", prompt="prompt 內容", response="need_to_find_this_response",
            latency_ms=10, status="ok",
        )
        assert [r.trace_id for r in search_traces("need_to_find_this_response")] == ["t1"]

    def test_large_prompt_written_to_payload_file_at_start(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_TRACE_PAYLOAD_THRESHOLD_BYTES", "5")
        record_llm_call_start(
            trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
            caller="mod.func", prompt="超過門檻的長 prompt 內容",
        )
        record = get_trace("t1")
        assert record.prompt is None
        assert record.prompt_payload_key is not None
        assert (tmp_path / "payloads" / record.prompt_payload_key).exists()

    def test_finish_without_prior_start_falls_back_to_plain_insert(self):
        # record_llm_call_start() 沒被呼叫過（例如呼叫端跳過）時，
        # record_llm_call() 的 UPSERT 在找不到既有 row 時要能退化成一般
        # INSERT，不能因為 ON CONFLICT 子句而整個寫入失敗。
        record_llm_call(
            trace_id="t1", run_id="run_A", vendor="claude", model="claude-sonnet-4-6",
            caller="mod.func", prompt="p", response="r", latency_ms=10, status="ok",
        )
        record = get_trace("t1")
        assert record is not None
        assert record.status == "ok"

    def test_live_visibility_during_a_slow_in_flight_call(self):
        """端對端模擬使用者實際場景：一次呼叫進行中（例如本地 Ollama
        長時間生成），另一個執行緒（模擬 llmlog／Claude Code 查詢）能在
        呼叫「還沒結束」的當下就查到完整 prompt 與 status='running'；
        呼叫結束後同一筆 row 轉成最終結果。這是本次設計要解決的核心
        場景：延遲期間 prompt 內容可查，不必等事後回溯。
        """
        call_finished = threading.Event()
        observed_while_running = {}

        def _slow_call():
            record_llm_call_start(
                trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
                caller="ollama_client.get_function_body", task_id="task_042",
                prompt="這是延遲期間應該要查得到的 prompt 長相",
            )
            time.sleep(0.3)  # 模擬本地模型正在生成、還沒回應
            record_llm_call(
                trace_id="t1", run_id="run_A", vendor="ollama", model="qwen2.5-coder:32b",
                caller="ollama_client.get_function_body", task_id="task_042",
                prompt="這是延遲期間應該要查得到的 prompt 長相", response="最終生成結果",
                latency_ms=300, status="ok",
            )
            call_finished.set()

        worker = threading.Thread(target=_slow_call)
        worker.start()

        # 輪詢直到觀察到 running 狀態，或呼叫已經結束（避免測試在極端
        # 排程延遲下誤判成失敗）。
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not call_finished.is_set():
            record = get_trace("t1")
            if record is not None and record.status == "running":
                observed_while_running["prompt"] = record.prompt
                observed_while_running["response"] = record.response
                observed_while_running["task_id"] = record.task_id
                break
            time.sleep(0.01)

        worker.join(timeout=5)

        assert observed_while_running.get("prompt") == "這是延遲期間應該要查得到的 prompt 長相"
        assert observed_while_running.get("response") is None  # 進行中還沒有回應
        assert observed_while_running.get("task_id") == "task_042"

        final_record = get_trace("t1")
        assert final_record.status == "ok"
        assert final_record.response == "最終生成結果"


class TestPayloadThreshold:
    def test_small_payload_stored_inline(self, monkeypatch):
        monkeypatch.setenv("LLM_TRACE_PAYLOAD_THRESHOLD_BYTES", "100000")
        _record(prompt="short", response="short")
        record = get_trace("t1")
        assert record.prompt == "short"
        assert record.prompt_payload_key is None

    def test_large_payload_written_to_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_TRACE_PAYLOAD_THRESHOLD_BYTES", "10")
        _record(prompt="這是一段超過十個位元組的長文字", response="這也是一段很長的回應內容")
        record = get_trace("t1")
        assert record.prompt is None
        assert record.response is None
        assert record.prompt_payload_key is not None
        assert record.response_payload_key is not None

        payload_dir = tmp_path / "payloads"
        payload_file = payload_dir / record.prompt_payload_key
        assert payload_file.exists()
        content = payload_file.read_text(encoding="utf-8")
        assert "## Prompt" in content
        assert "## Response" in content

    def test_read_payload_section_recovers_content(self, monkeypatch):
        monkeypatch.setenv("LLM_TRACE_PAYLOAD_THRESHOLD_BYTES", "5")
        _record(prompt="prompt 內容超過門檻", response="response 內容超過門檻")
        record = get_trace("t1")
        prompt_text = llm_trace.read_payload_section(record.prompt_payload_key, "Prompt")
        response_text = llm_trace.read_payload_section(record.response_payload_key, "Response")
        assert prompt_text == "prompt 內容超過門檻"
        assert response_text == "response 內容超過門檻"


class TestGetTracesForTask:
    def test_filters_by_task_id_ordered_by_attempt(self):
        _record(trace_id="t1", task_id="task_1", attempt=0)
        _record(trace_id="t2", task_id="task_1", attempt=1)
        _record(trace_id="t3", task_id="task_2", attempt=0)
        records = get_traces_for_task("task_1")
        assert [r.trace_id for r in records] == ["t1", "t2"]

    def test_run_id_filter_narrows_to_specific_run(self):
        _record(trace_id="t1", task_id="task_1", run_id="run_A")
        _record(trace_id="t2", task_id="task_1", run_id="run_B")
        assert [r.trace_id for r in get_traces_for_task("task_1", run_id="run_B")] == ["t2"]
        assert {r.trace_id for r in get_traces_for_task("task_1")} == {"t1", "t2"}


class TestGetTracesForFunction:
    def test_cross_run_lookup_by_function_identity(self):
        _record(trace_id="t1", run_id="run_A", target_file="app/f.py", class_name="C", function_name="g")
        _record(trace_id="t2", run_id="run_B", target_file="app/f.py", class_name="C", function_name="g")
        _record(trace_id="t3", run_id="run_A", target_file="app/other.py", class_name="C", function_name="g")
        records = get_traces_for_function("app/f.py", "C", "g")
        assert {r.trace_id for r in records} == {"t1", "t2"}

    def test_none_class_name_matches_module_level_function(self):
        _record(trace_id="t1", target_file="app/routers/r.py", class_name=None, function_name="handler")
        records = get_traces_for_function("app/routers/r.py", None, "handler")
        assert [r.trace_id for r in records] == ["t1"]


class TestListRecent:
    def test_filters_by_status(self):
        _record(trace_id="t1", status="ok")
        _record(trace_id="t2", status="error")
        assert [r.trace_id for r in list_recent(status="error")] == ["t2"]

    def test_filters_by_run_id(self):
        _record(trace_id="t1", run_id="run_A")
        _record(trace_id="t2", run_id="run_B")
        assert [r.trace_id for r in list_recent(run_id="run_A")] == ["t1"]

    def test_orders_newest_first(self):
        _record(trace_id="t1")
        _record(trace_id="t2")
        assert [r.trace_id for r in list_recent()] == ["t2", "t1"]

    def test_since_excludes_older_than_cutoff(self, monkeypatch):
        # 直接塞一筆時間戳在很久以前的紀錄，驗證 since 過濾生效。
        _record(trace_id="t1")
        conn = llm_trace._get_connection()
        conn.execute("UPDATE llm_traces SET ts = '2000-01-01T00:00:00+00:00' WHERE trace_id = 't1'")
        conn.commit()
        _record(trace_id="t2")
        assert [r.trace_id for r in list_recent(since="24h")] == ["t2"]


class TestSearchTraces:
    def test_fts_matches_prompt_content(self):
        _record(trace_id="t1", prompt="關於 user_repository 的翻譯", response="ok")
        _record(trace_id="t2", prompt="跟這個無關", response="ok")
        results = search_traces("user_repository")
        assert [r.trace_id for r in results] == ["t1"]

    def test_search_reflects_update_via_flag_trigger(self):
        # llm_traces_au trigger：UPDATE 不動 prompt/response 本身，但驗證
        # flag 過的 row 之後仍然能被原本的內容正確搜到（FTS 索引沒有因為
        # UPDATE 而跟主表脫鉤，見七章「外部 content 表需要 trigger 才會
        # 同步」）。用 ASCII 關鍵字，避免 FTS5 unicode61 對連續 CJK 字元
        # 整段視為單一 token、substring 查詢查不到的干擾。
        _record(trace_id="t1", prompt="need_to_be_flagged_content 這是內容", response="ok")
        flag_trace("t1", "hallucination", note="測試")
        results = search_traces("need_to_be_flagged_content")
        assert [r.trace_id for r in results] == ["t1"]

    def test_matches_large_payload_via_file_fallback(self, monkeypatch):
        monkeypatch.setenv("LLM_TRACE_PAYLOAD_THRESHOLD_BYTES", "5")
        _record(trace_id="t1", prompt="超過門檻會落檔的特殊關鍵字ZzZ", response="ok")
        results = search_traces("特殊關鍵字ZzZ")
        assert [r.trace_id for r in results] == ["t1"]

    def test_filters_combine_with_tag(self):
        _record(trace_id="t1", prompt="共同關鍵字", response="ok")
        _record(trace_id="t2", prompt="共同關鍵字", response="ok")
        flag_trace("t1", "logic_flaw")
        assert [r.trace_id for r in search_traces("共同關鍵字", tag="logic_flaw")] == ["t1"]


class TestFlagTrace:
    def test_valid_tag_updates_record(self):
        _record(trace_id="t1")
        flag_trace("t1", "refused", note="模型拒答")
        record = get_trace("t1")
        assert record.issue_tag == "refused"
        assert record.note == "模型拒答"

    def test_invalid_tag_raises_value_error(self):
        _record(trace_id="t1")
        with pytest.raises(ValueError, match="issue_tag"):
            flag_trace("t1", "not_a_real_tag")

    def test_missing_trace_id_raises_value_error(self):
        with pytest.raises(ValueError, match="不存在"):
            flag_trace("does-not-exist", "refused")


class TestLatestRunId:
    def test_returns_none_when_table_empty(self):
        assert latest_run_id() is None

    def test_returns_run_id_of_most_recently_inserted_row(self):
        _record(trace_id="t1", run_id="run_A")
        _record(trace_id="t2", run_id="run_B")
        assert latest_run_id() == "run_B"


class TestGcOrphanPayloads:
    def test_referenced_payload_is_never_deleted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_TRACE_PAYLOAD_THRESHOLD_BYTES", "1")
        _record(trace_id="t1", prompt="會落檔的內容", response="會落檔的回應")
        deleted = gc_orphan_payloads(grace_period_hours=0)
        assert deleted == 0
        record = get_trace("t1")
        assert (tmp_path / "payloads" / record.prompt_payload_key).exists()

    def test_orphan_within_grace_period_is_kept(self, tmp_path):
        payload_dir = tmp_path / "payloads" / "run_X"
        payload_dir.mkdir(parents=True)
        orphan = payload_dir / "orphan.md"
        orphan.write_text("孤兒檔案", encoding="utf-8")
        deleted = gc_orphan_payloads(grace_period_hours=24)
        assert deleted == 0
        assert orphan.exists()

    def test_orphan_past_grace_period_is_deleted(self, tmp_path):
        payload_dir = tmp_path / "payloads" / "run_X"
        payload_dir.mkdir(parents=True)
        orphan = payload_dir / "orphan.md"
        orphan.write_text("孤兒檔案", encoding="utf-8")
        old_time = time.time() - 48 * 3600
        os.utime(orphan, (old_time, old_time))

        deleted = gc_orphan_payloads(grace_period_hours=24)
        assert deleted == 1
        assert not orphan.exists()

    def test_no_payload_dir_returns_zero(self):
        assert gc_orphan_payloads(grace_period_hours=24) == 0
