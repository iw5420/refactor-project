"""llm_usage_logger.py 的假資料單元測試（見 docs/00_refactor_architecture.md
六章「Claude API 呼叫用量記錄」）。不呼叫真實 Claude API，只測記錄邏輯
本身：自動偵測呼叫端、明確傳入 caller 覆寫、寫檔格式、append 不覆寫、
寫檔失敗不拋例外。
"""
import json
from dataclasses import dataclass

from common import llm_usage_logger
from common.llm_usage_logger import log_usage


@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _FakeResponse:
    usage: _FakeUsage


def _read_entries(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestLogUsage:
    def test_writes_single_jsonl_entry(self, tmp_path, monkeypatch):
        log_path = tmp_path / "usage.jsonl"
        monkeypatch.setattr(llm_usage_logger, "LOG_PATH", log_path)

        log_usage(_FakeResponse(usage=_FakeUsage(input_tokens=170000, output_tokens=50)), model="claude-sonnet-4-6")

        entries = _read_entries(log_path)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["model"] == "claude-sonnet-4-6"
        assert entry["input_tokens"] == 170000
        assert entry["output_tokens"] == 50
        assert "timestamp" in entry
        assert entry["caller"] == "test_llm_usage_logger.test_writes_single_jsonl_entry"

    def test_explicit_caller_overrides_auto_detection(self, tmp_path, monkeypatch):
        log_path = tmp_path / "usage.jsonl"
        monkeypatch.setattr(llm_usage_logger, "LOG_PATH", log_path)

        log_usage(
            _FakeResponse(usage=_FakeUsage()),
            model="claude-sonnet-4-6",
            caller="some_module.some_function",
        )

        entries = _read_entries(log_path)
        assert entries[0]["caller"] == "some_module.some_function"

    def test_appends_not_overwrites(self, tmp_path, monkeypatch):
        log_path = tmp_path / "usage.jsonl"
        monkeypatch.setattr(llm_usage_logger, "LOG_PATH", log_path)

        log_usage(_FakeResponse(usage=_FakeUsage()), model="m1")
        log_usage(_FakeResponse(usage=_FakeUsage()), model="m2")

        entries = _read_entries(log_path)
        assert [e["model"] for e in entries] == ["m1", "m2"]

    def test_creates_parent_directory(self, tmp_path, monkeypatch):
        log_path = tmp_path / "nested" / "dir" / "usage.jsonl"
        monkeypatch.setattr(llm_usage_logger, "LOG_PATH", log_path)

        log_usage(_FakeResponse(usage=_FakeUsage()), model="m")

        assert log_path.exists()

    def test_write_failure_logs_warning_not_raises(self, tmp_path, monkeypatch, caplog):
        # LOG_PATH 指向一個不可能建立子目錄的路徑（把一個已存在的檔案
        # 當成目錄），觸發 OSError；記錄失敗不該讓呼叫端的 LLM 呼叫流程
        # 跟著炸掉。
        blocking_file = tmp_path / "not_a_directory"
        blocking_file.write_text("x")
        log_path = blocking_file / "usage.jsonl"
        monkeypatch.setattr(llm_usage_logger, "LOG_PATH", log_path)

        with caplog.at_level("WARNING"):
            log_usage(_FakeResponse(usage=_FakeUsage()), model="m")  # 不應拋例外

        assert "寫入 Claude API 用量紀錄失敗" in caplog.text

    def test_missing_cache_token_fields_default_to_zero(self, tmp_path, monkeypatch):
        # 舊版 SDK 或某些回應可能沒有 cache_creation/cache_read 欄位，
        # 用 getattr 保底成 0，不要因為缺欄位就整個記錄失敗。
        @dataclass
        class _UsageWithoutCacheFields:
            input_tokens: int = 5
            output_tokens: int = 5

        log_path = tmp_path / "usage.jsonl"
        monkeypatch.setattr(llm_usage_logger, "LOG_PATH", log_path)

        log_usage(_FakeResponse(usage=_UsageWithoutCacheFields()), model="m")

        entry = _read_entries(log_path)[0]
        assert entry["cache_creation_input_tokens"] == 0
        assert entry["cache_read_input_tokens"] == 0
