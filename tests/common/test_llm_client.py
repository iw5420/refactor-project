"""common/llm_client.py 的假資料單元測試（見 docs/00_refactor_architecture.md
六章、docs/03c_collection_agent_code.md 1.2 節）。不呼叫真實 Claude API,
只測 `call_claude_for_json()` 的 `output_config` 組裝與 client 單例的
執行緒安全。這份測試原本放在 tests/spec_collection_agent/test_llm.py,
搬到這裡是因為實作本身已經搬到 common/llm_client.py,供所有 Agent 共用
(見 00 六章),不再是 [B] 專屬。
"""
import threading
from dataclasses import dataclass

import anthropic
import httpx
import pytest

from common import llm_client
from common.llm_client import LlmJsonError, call_claude_for_json


@dataclass
class _FakeBlock:
    type: str
    text: str


@dataclass
class _FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 5
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _FakeResponse:
    content: list
    usage: _FakeUsage = None

    def __post_init__(self):
        if self.usage is None:
            self.usage = _FakeUsage()


class TestLlmJsonErrorRawText:
    def test_str_includes_raw_text_when_present(self):
        exc = LlmJsonError("無法解析為 JSON", raw_text="not json")
        assert "無法解析為 JSON" in str(exc)
        assert "not json" in str(exc)

    def test_str_omits_raw_text_section_when_none(self):
        exc = LlmJsonError("Claude API 呼叫失敗")
        assert str(exc) == "Claude API 呼叫失敗"


class _FakeClient:
    """回傳固定文字內容的 fake client，並記錄呼叫時傳入的 kwargs，供測試
    驗證 `output_config` 有沒有正確組裝、帶給 API。
    """

    def __init__(self, response_text: str):
        self._response_text = response_text
        self.call_count = 0
        self.last_kwargs: dict | None = None

        outer = self

        class _Messages:
            @staticmethod
            def create(**kwargs):
                outer.call_count += 1
                outer.last_kwargs = kwargs
                return _FakeResponse(content=[_FakeBlock(type="text", text=outer._response_text)])

        self.messages = _Messages()


@pytest.fixture(autouse=True)
def _stub_record_llm_call(monkeypatch):
    # 這裡不測 record_llm_call()／record_llm_call_start() 內部寫入邏輯
    # （見 test_llm_trace.py），只確保這份檔案的測試不會因為呼叫記錄而
    # 意外寫真實 DB。個別測試需要驗證實際傳了什麼參數時，在測試本體裡
    # 用自己的 monkeypatch.setattr() 覆寫掉這裡的 no-op（同一個
    # monkeypatch fixture 實例，後設定的覆蓋先設定的）。
    monkeypatch.setattr(llm_client, "record_llm_call", lambda *a, **kw: None)
    monkeypatch.setattr(llm_client, "record_llm_call_start", lambda *a, **kw: None)


class TestCallClaudeForJson:
    _SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}
    _MODEL = "claude-sonnet-4-6"

    def test_returns_parsed_value(self, monkeypatch):
        fake = _FakeClient('{"a": 1}')
        monkeypatch.setattr(llm_client, "_get_client", lambda: fake)

        result = call_claude_for_json(
            system_prompt="x", user_prompt="y", schema=self._SCHEMA, model=self._MODEL
        )
        assert result == {"a": 1}
        assert fake.call_count == 1

    def test_passes_schema_via_output_config(self, monkeypatch):
        fake = _FakeClient('{"a": 1}')
        monkeypatch.setattr(llm_client, "_get_client", lambda: fake)

        call_claude_for_json(system_prompt="x", user_prompt="y", schema=self._SCHEMA, model=self._MODEL)

        assert fake.last_kwargs["output_config"] == {
            "format": {"type": "json_schema", "schema": self._SCHEMA}
        }
        assert fake.last_kwargs["system"] == "x"
        assert fake.last_kwargs["messages"] == [{"role": "user", "content": "y"}]
        assert fake.last_kwargs["model"] == self._MODEL

    def test_array_schema_returns_list(self, monkeypatch):
        fake = _FakeClient("[1, 2, 3]")
        monkeypatch.setattr(llm_client, "_get_client", lambda: fake)

        result = call_claude_for_json(
            system_prompt="x", user_prompt="y", schema={"type": "array"}, model=self._MODEL
        )
        assert result == [1, 2, 3]

    def test_unparseable_response_raises_with_raw_text(self, monkeypatch):
        # 理論上不該發生（output_config 應該保證合法），但還是要能明確
        # 報錯，不能靜默吞掉或崩潰成別的例外。
        fake = _FakeClient("not valid json")
        monkeypatch.setattr(llm_client, "_get_client", lambda: fake)

        with pytest.raises(LlmJsonError) as exc_info:
            call_claude_for_json(system_prompt="x", user_prompt="y", schema=self._SCHEMA, model=self._MODEL)
        assert exc_info.value.raw_text == "not valid json"
        assert fake.call_count == 1  # 不重試

    def test_api_error_raises_llm_json_error(self, monkeypatch):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

        class _FailingClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise anthropic.APIConnectionError(request=request)

        monkeypatch.setattr(llm_client, "_get_client", lambda: _FailingClient())

        with pytest.raises(LlmJsonError):
            call_claude_for_json(system_prompt="x", user_prompt="y", schema=self._SCHEMA, model=self._MODEL)


class TestCallClaudeForJsonStartRecording:
    def test_record_llm_call_start_called_before_api_call_with_full_prompt(self, monkeypatch):
        # 呼叫還沒結束就要能查到 prompt（見 11a 七章「呼叫開始即寫入
        # running row」）：record_llm_call_start() 必須在真正打 API 之前
        # 就被呼叫，且帶著跟 finally 那筆完全一致的組好的 prompt 內容。
        call_order = []
        start_kwargs = {}

        def _fake_record_start(**kw):
            call_order.append("start")
            start_kwargs.update(kw)

        class _OrderTrackingClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    call_order.append("api_call")
                    return _FakeResponse(content=[_FakeBlock(type="text", text='{"a": 1}')])

        monkeypatch.setattr(llm_client, "_get_client", lambda: _OrderTrackingClient())
        monkeypatch.setattr(llm_client, "record_llm_call_start", _fake_record_start)

        call_claude_for_json(
            system_prompt="sys 內容", user_prompt="user 內容",
            schema={"type": "object"}, model="claude-sonnet-4-6",
        )

        assert call_order == ["start", "api_call"]
        assert "sys 內容" in start_kwargs["prompt"]
        assert "user 內容" in start_kwargs["prompt"]
        assert start_kwargs["vendor"] == "claude"
        assert start_kwargs["model"] == "claude-sonnet-4-6"

    def test_record_llm_call_start_uses_same_trace_id_as_finish(self, monkeypatch):
        # record_llm_call() 靠比對同一個 trace_id 才能把 running row
        # upsert 成最終結果，不是兩筆獨立紀錄——這裡驗證兩次呼叫確實共用
        # 同一個 trace_id。
        start_kwargs = {}
        finish_kwargs = {}
        monkeypatch.setattr(llm_client, "_get_client", lambda: _FakeClient('{"a": 1}'))
        monkeypatch.setattr(llm_client, "record_llm_call_start", lambda **kw: start_kwargs.update(kw))
        monkeypatch.setattr(llm_client, "record_llm_call", lambda **kw: finish_kwargs.update(kw))

        call_claude_for_json(
            system_prompt="x", user_prompt="y", schema={"type": "object"}, model="claude-sonnet-4-6"
        )

        assert start_kwargs["trace_id"] == finish_kwargs["trace_id"]


class TestCallClaudeForJsonTraceRecording:
    def test_records_with_real_caller_not_wrapper(self, monkeypatch):
        # call_claude_for_json() 是所有 Agent 共用的封裝，用 sys._getframe(1)
        # 抓真正的業務呼叫端（這裡測試函式本身），不是自己這層 wrapper。
        fake = _FakeClient('{"a": 1}')
        monkeypatch.setattr(llm_client, "_get_client", lambda: fake)

        captured = {}
        monkeypatch.setattr(
            llm_client, "record_llm_call", lambda **kw: captured.update(kw)
        )

        call_claude_for_json(
            system_prompt="x", user_prompt="y", schema={"type": "object"}, model="claude-sonnet-4-6"
        )

        assert captured["caller"] == "test_llm_client.test_records_with_real_caller_not_wrapper"
        assert captured["model"] == "claude-sonnet-4-6"
        assert captured["status"] == "ok"
        assert captured["input_tokens"] == 10

    def test_records_with_status_error_when_api_call_fails(self, monkeypatch):
        # record_llm_call() 在 finally 區塊呼叫，失敗時也要留下一筆
        # status='error' 的 trace，不能因為呼叫失敗就完全沒有記錄。
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

        class _FailingClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise anthropic.APIConnectionError(request=request)

        monkeypatch.setattr(llm_client, "_get_client", lambda: _FailingClient())
        captured = {}
        monkeypatch.setattr(
            llm_client, "record_llm_call", lambda **kw: captured.update(kw)
        )

        with pytest.raises(LlmJsonError):
            call_claude_for_json(system_prompt="x", user_prompt="y", schema={"type": "object"}, model="claude-sonnet-4-6")

        assert captured["status"] == "error"
        assert captured["error_msg"] is not None


class TestGetClientThreadSafety:
    def test_only_constructed_once_under_concurrency(self, monkeypatch):
        monkeypatch.setattr(llm_client, "_client", None)
        construction_count = {"n": 0}
        barrier = threading.Barrier(8)

        class _FakeClientForThreadTest:
            def __init__(self, **kwargs):
                construction_count["n"] += 1

        monkeypatch.setattr(llm_client.anthropic, "Anthropic", _FakeClientForThreadTest)

        def _worker():
            barrier.wait()
            llm_client._get_client()

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert construction_count["n"] == 1

    def test_client_constructed_with_explicit_timeout(self, monkeypatch):
        """對應 docs/09b_bug_trace.md：2026-08-28 真實環境撞到一次呼叫卡住
        30 分鐘以上、狀態從未變成 error／timeout 的案例——不能再依賴 SDK
        預設值，`_get_client()` 必須明確傳 `timeout`，才有保底機制讓卡住
        的呼叫最終失敗、交給既有重試／give_up 機制接手，不會無限期卡死。"""
        monkeypatch.setattr(llm_client, "_client", None)
        captured_kwargs = {}

        class _FakeClientCapturingKwargs:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

        monkeypatch.setattr(llm_client.anthropic, "Anthropic", _FakeClientCapturingKwargs)

        llm_client._get_client()

        assert captured_kwargs.get("timeout") == 300.0
