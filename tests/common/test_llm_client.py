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


class TestCallClaudeForJson:
    _SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}
    _MODEL = "claude-sonnet-4-6"

    @pytest.fixture(autouse=True)
    def _stub_log_usage(self, monkeypatch):
        # 這裡不測 log_usage() 內部寫檔邏輯（見 test_llm_usage_logger.py），
        # 只確保這些既有測試不會因為新增的用量記錄而意外寫真實檔案。
        monkeypatch.setattr(llm_client, "log_usage", lambda *a, **kw: None)

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


class TestCallClaudeForJsonUsageLogging:
    def test_logs_usage_with_real_caller_not_wrapper(self, monkeypatch):
        # call_claude_for_json() 是所有 Agent 共用的封裝——log_usage()
        # 不傳 caller 時的預設行為（inspect.stack()[1]）會抓到
        # call_claude_for_json 自己，不是真正的業務呼叫端。
        # call_claude_for_json() 必須自己先算出真正的呼叫端（這裡測試函式
        # 本身），明確傳給 log_usage() 覆寫掉這個預設行為。
        fake = _FakeClient('{"a": 1}')
        monkeypatch.setattr(llm_client, "_get_client", lambda: fake)

        captured = {}

        def _fake_log_usage(response, *, model, caller=None):
            captured["response"] = response
            captured["model"] = model
            captured["caller"] = caller

        monkeypatch.setattr(llm_client, "log_usage", _fake_log_usage)

        call_claude_for_json(
            system_prompt="x", user_prompt="y", schema={"type": "object"}, model="claude-sonnet-4-6"
        )

        assert captured["caller"] == "test_llm_client.test_logs_usage_with_real_caller_not_wrapper"
        assert captured["model"] == "claude-sonnet-4-6"
        assert captured["response"].usage.input_tokens == 10

    def test_not_called_when_api_call_fails(self, monkeypatch):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

        class _FailingClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise anthropic.APIConnectionError(request=request)

        monkeypatch.setattr(llm_client, "_get_client", lambda: _FailingClient())
        called = {"n": 0}
        monkeypatch.setattr(llm_client, "log_usage", lambda *a, **kw: called.__setitem__("n", called["n"] + 1))

        with pytest.raises(LlmJsonError):
            call_claude_for_json(system_prompt="x", user_prompt="y", schema={"type": "object"}, model="claude-sonnet-4-6")

        assert called["n"] == 0  # 沒有 response 可記錄，不該被呼叫


class TestGetClientThreadSafety:
    def test_only_constructed_once_under_concurrency(self, monkeypatch):
        monkeypatch.setattr(llm_client, "_client", None)
        construction_count = {"n": 0}
        barrier = threading.Barrier(8)

        class _FakeClientForThreadTest:
            def __init__(self):
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
