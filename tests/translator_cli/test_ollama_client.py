"""translator_cli/ollama_client.py，對應 07a 七章。async 函式用
`asyncio.run()` 包在一般 pytest 函式裡呼叫，不引入 pytest-asyncio
的 marker／設定檔（見 tests/translator_cli/ 目錄說明）。
"""
import asyncio

import httpx
import pytest

from translator_cli import ollama_client
from translator_cli.exceptions import (
    TranslatorCliConfigError,
    TranslatorCliModelOutputError,
    TranslatorCliNetworkError,
)


def test_extract_delimited_body_success():
    raw = "說明文字\n<<<TRANSLATOR_CLI_BODY_START>>>\nreturn 1\n<<<TRANSLATOR_CLI_BODY_END>>>\n多餘的話"
    assert ollama_client._extract_delimited_body(raw) == "return 1"


def test_extract_delimited_body_missing_raises():
    with pytest.raises(TranslatorCliModelOutputError, match="delimiter"):
        ollama_client._extract_delimited_body("模型沒有照格式回應")


def test_get_function_body_success_first_attempt(monkeypatch):
    calls = []

    async def fake_call(system_prompt, user_prompt):
        calls.append(user_prompt)
        return "<<<TRANSLATOR_CLI_BODY_START>>>\nreturn user_id\n<<<TRANSLATOR_CLI_BODY_END>>>"

    monkeypatch.setattr(ollama_client, "_call_ollama_once", fake_call)

    body = asyncio.run(
        ollama_client.get_function_body(
            current_signature="def get_by_id(user_id: int) -> int:",
            description="回傳 user_id",
            context="",
            context_files=[],
            function_name="get_by_id",
        )
    )
    assert body == "return user_id"
    assert len(calls) == 1


def test_get_function_body_retries_once_then_succeeds(monkeypatch):
    responses = [
        "沒有照格式回應",
        "<<<TRANSLATOR_CLI_BODY_START>>>\nreturn 1\n<<<TRANSLATOR_CLI_BODY_END>>>",
    ]
    prompts = []

    async def fake_call(system_prompt, user_prompt):
        prompts.append(user_prompt)
        return responses.pop(0)

    monkeypatch.setattr(ollama_client, "_call_ollama_once", fake_call)

    body = asyncio.run(
        ollama_client.get_function_body(
            current_signature="def f() -> int:",
            description="d",
            context="",
            context_files=[],
            function_name="f",
        )
    )
    assert body == "return 1"
    assert len(prompts) == 2
    assert "上一次回應違反格式" in prompts[1]


def test_get_function_body_retries_on_empty_body_then_succeeds(monkeypatch):
    responses = [
        "<<<TRANSLATOR_CLI_BODY_START>>>\n\n<<<TRANSLATOR_CLI_BODY_END>>>",  # 空 body，見六章步驟 6a
        "<<<TRANSLATOR_CLI_BODY_START>>>\nreturn 1\n<<<TRANSLATOR_CLI_BODY_END>>>",
    ]

    async def fake_call(system_prompt, user_prompt):
        return responses.pop(0)

    monkeypatch.setattr(ollama_client, "_call_ollama_once", fake_call)

    body = asyncio.run(
        ollama_client.get_function_body(
            current_signature="def f() -> int:",
            description="d",
            context="",
            context_files=[],
            function_name="f",
        )
    )
    assert body == "return 1"


def test_get_function_body_retries_twice_then_succeeds(monkeypatch):
    # _FORMAT_RETRY_COUNT=2：第一次、第二次都違反格式，第三次（第二次
    # 重試）才成功——驗證真的用滿了兩次重試預算，不是只有一次。
    responses = [
        "第一次沒有照格式回應",
        "第二次還是沒有照格式回應",
        "<<<TRANSLATOR_CLI_BODY_START>>>\nreturn 1\n<<<TRANSLATOR_CLI_BODY_END>>>",
    ]
    prompts = []

    async def fake_call(system_prompt, user_prompt):
        prompts.append(user_prompt)
        return responses.pop(0)

    monkeypatch.setattr(ollama_client, "_call_ollama_once", fake_call)

    body = asyncio.run(
        ollama_client.get_function_body(
            current_signature="def f() -> int:",
            description="d",
            context="",
            context_files=[],
            function_name="f",
        )
    )
    assert body == "return 1"
    assert len(prompts) == 3


def test_get_function_body_gives_up_after_two_retries(monkeypatch):
    call_count = {"n": 0}

    async def fake_call(system_prompt, user_prompt):
        call_count["n"] += 1
        return "永遠沒有 delimiter"

    monkeypatch.setattr(ollama_client, "_call_ollama_once", fake_call)

    with pytest.raises(TranslatorCliModelOutputError, match="修正重試仍失敗"):
        asyncio.run(
            ollama_client.get_function_body(
                current_signature="def f() -> int:",
                description="d",
                context="",
                context_files=[],
                function_name="f",
            )
        )
    assert call_count["n"] == 3  # 第一次 + 兩次重試


def _fake_async_client(post_impl):
    class _FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def post(self, url, json, headers):
            return await post_impl(url, json, headers)

    return _FakeAsyncClient


def test_call_ollama_once_retries_network_error_then_succeeds(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake-nginx/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "token")
    monkeypatch.setattr(ollama_client, "_NETWORK_RETRY_DELAY_SECONDS", 0)

    call_count = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    async def post_impl(url, json, headers):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise httpx.TransportError("連線暫時失敗")
        return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(post_impl))

    content = asyncio.run(ollama_client._call_ollama_once("sys", "user"))
    assert content == "ok"
    assert call_count["n"] == 2


def test_call_ollama_once_raises_after_exhausting_network_retries(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake-nginx/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "token")
    monkeypatch.setattr(ollama_client, "_NETWORK_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(ollama_client, "TRANSLATOR_CLI_NETWORK_RETRIES", 1)

    async def post_impl(url, json, headers):
        raise httpx.TransportError("一直連不上")

    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(post_impl))

    with pytest.raises(TranslatorCliNetworkError, match="ollama 連線失敗"):
        asyncio.run(ollama_client._call_ollama_once("sys", "user"))


def test_call_ollama_once_http_status_error_fails_immediately_without_retry(monkeypatch):
    # HTTPStatusError（收到回應，但狀態碼是 4xx/5xx）跟傳輸層錯誤不同
    # 性質（07a 七章明確把重試範圍限定在傳輸層錯誤）——例如 nginx token
    # 設錯導致每次都回 401，重試沒有意義，驗證立即失敗、不進重試迴圈
    # （call_count 只會是 1，不會因為 TRANSLATOR_CLI_NETWORK_RETRIES 而
    # 變成 2 或 3）。
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake-nginx/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "token")
    monkeypatch.setattr(ollama_client, "_NETWORK_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(ollama_client, "TRANSLATOR_CLI_NETWORK_RETRIES", 2)

    call_count = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self):
            request = httpx.Request("POST", "http://fake-nginx/v1/chat/completions")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)

    async def post_impl(url, json, headers):
        call_count["n"] += 1
        return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(post_impl))

    with pytest.raises(TranslatorCliNetworkError, match="不重試"):
        asyncio.run(ollama_client._call_ollama_once("sys", "user"))
    assert call_count["n"] == 1


def test_call_ollama_once_sends_stream_false(monkeypatch):
    # nginx／ollama 若對 OpenAI 相容端點預設走 streaming，response.json()
    # 對分段回應會直接解析失敗——確認 payload 明確帶 "stream": False。
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake-nginx/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "token")

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    async def post_impl(url, json, headers):
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(post_impl))

    asyncio.run(ollama_client._call_ollama_once("sys", "user"))
    assert captured["json"]["stream"] is False


def test_call_ollama_once_missing_env_vars_raises_config_error(monkeypatch):
    # os.environ["OLLAMA_BASE_URL"]／["OLLAMA_API_KEY"] 缺失時，過去會讓
    # 原生 KeyError 直接洩漏、擊穿 FillResult 合約——確認現在轉成明確的
    # TranslatorCliConfigError，且訊息點名是哪個變數缺失。
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    with pytest.raises(TranslatorCliConfigError, match="OLLAMA_BASE_URL"):
        asyncio.run(ollama_client._call_ollama_once("sys", "user"))


def test_call_ollama_once_reuses_single_client_across_retries(monkeypatch):
    # httpx.AsyncClient 應該只在整次 _call_ollama_once() 呼叫裡建立一次，
    # 同一次呼叫的多次網路層重試共用同一個連線，不是每次重試都重新建立
    # 一個新的 client（見 _call_ollama_once() docstring）。
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake-nginx/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "token")
    monkeypatch.setattr(ollama_client, "_NETWORK_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(ollama_client, "TRANSLATOR_CLI_NETWORK_RETRIES", 2)

    client_instantiations = {"n": 0}
    post_calls = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class _FakeAsyncClient:
        def __init__(self, timeout):
            client_instantiations["n"] += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def post(self, url, json, headers):
            post_calls["n"] += 1
            if post_calls["n"] < 3:
                raise httpx.TransportError("連線暫時失敗")
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    content = asyncio.run(ollama_client._call_ollama_once("sys", "user"))
    assert content == "ok"
    assert post_calls["n"] == 3  # 重試了兩次才成功
    assert client_instantiations["n"] == 1  # 但只建立了一個 client 實例
