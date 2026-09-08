"""translator_cli/claude_client.py，對應 07a 七章「Claude 路徑：連線
方式」。跟 `ollama_client.get_function_body()` 是同一份契約（見該模組
docstring），這裡不重複驗證 delimiter 抽取（Claude 路徑靠
Structured Outputs 保證 JSON 格式，沒有 delimiter 這個環節），只驗證
`body_statements` 內容本身的格式修正重試、以及 API 呼叫失敗直接轉成
`TranslatorCliNetworkError`（不重試）這兩件事。async 函式用
`asyncio.run()` 包在一般 pytest 函式裡呼叫，不引入 pytest-asyncio 的
marker／設定檔（見 tests/translator_cli/ 目錄說明）。

`call_claude_for_json` 是直接 import 進 `claude_client.py` 模組（
`from common.llm_client import ... call_claude_for_json`），因此
monkeypatch 的對象是 `claude_client.call_claude_for_json`，不是
`common.llm_client.call_claude_for_json`（見 07a 七章 docstring「不新增
另一套 Claude 連線邏輯」）。這個模組不像 `ollama_client.py` 自己管
`record_llm_call`／`record_llm_call_start`（`call_claude_for_json()`
內部已經做好），這裡不需要對應的 stub fixture。
"""
import asyncio

import pytest

from translator_cli import claude_client
from translator_cli.exceptions import TranslatorCliModelOutputError, TranslatorCliNetworkError


def test_get_function_body_success_first_attempt(monkeypatch):
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return {"body_statements": "return user_id\n"}

    monkeypatch.setattr(claude_client, "call_claude_for_json", fake_call)

    body = asyncio.run(
        claude_client.get_function_body(
            current_signature="def get_by_id(user_id: int) -> int:",
            java_source="public int getById(int userId) { return userId; }",
            referenced_source=[],
            context="",
            context_files=[],
            function_name="get_by_id",
            run_id="test_run",
        )
    )
    assert body == "return user_id\n"
    assert len(calls) == 1
    assert calls[0]["schema"] is claude_client.BODY_STATEMENTS_SCHEMA
    assert calls[0]["system_prompt"] == claude_client.SYSTEM_PROMPT_CLAUDE
    assert calls[0]["model"] == claude_client.CLAUDE_MODEL
    assert calls[0]["run_id"] == "test_run"


def test_get_function_body_format_retry_then_succeeds(monkeypatch):
    # body_statements 通不過 extract_body_statements()（空陳述式清單）
    # 第一次觸發格式修正重試，第二次改回合法內容才成功——跟 qwen 路徑共用
    # 同一套 extract_body_statements() 驗證，見模組 docstring。
    responses = [
        {"body_statements": ""},  # 空 body，見 07a 六章步驟 6a
        {"body_statements": "return 1\n"},
    ]
    prompts = []

    def fake_call(**kwargs):
        prompts.append(kwargs["user_prompt"])
        return responses.pop(0)

    monkeypatch.setattr(claude_client, "call_claude_for_json", fake_call)

    body = asyncio.run(
        claude_client.get_function_body(
            current_signature="def f() -> int:",
            java_source="d",
            referenced_source=[],
            context="",
            context_files=[],
            function_name="f",
            run_id="test_run",
        )
    )
    assert body == "return 1\n"
    assert len(prompts) == 2
    assert "上一次回應違反格式" in prompts[1]


def test_get_function_body_gives_up_after_retries_exhausted(monkeypatch):
    call_count = {"n": 0}

    def fake_call(**kwargs):
        call_count["n"] += 1
        return {"body_statements": ""}  # 永遠是空 body，格式修正重試救不回來

    monkeypatch.setattr(claude_client, "call_claude_for_json", fake_call)

    with pytest.raises(TranslatorCliModelOutputError, match="修正重試仍失敗"):
        asyncio.run(
            claude_client.get_function_body(
                current_signature="def f() -> int:",
                java_source="d",
                referenced_source=[],
                context="",
                context_files=[],
                function_name="f",
                run_id="test_run",
            )
        )
    # FORMAT_RETRY_COUNT=2：第一次 + 兩次重試 = 3 次呼叫。
    assert call_count["n"] == claude_client.FORMAT_RETRY_COUNT + 1


def test_get_function_body_call_failure_converts_to_network_error_without_retry(monkeypatch):
    # call_claude_for_json() 失敗（API／網路層錯誤）不進格式修正重試迴圈
    # ——見模組 docstring「API／網路層錯誤不重試」，一次失敗立即轉成
    # TranslatorCliNetworkError 往外拋。
    call_count = {"n": 0}

    def fake_call(**kwargs):
        call_count["n"] += 1
        raise RuntimeError("Claude API 逾時")

    monkeypatch.setattr(claude_client, "call_claude_for_json", fake_call)

    with pytest.raises(TranslatorCliNetworkError, match="Claude API 呼叫失敗"):
        asyncio.run(
            claude_client.get_function_body(
                current_signature="def f() -> int:",
                java_source="d",
                referenced_source=[],
                context="",
                context_files=[],
                function_name="f",
                run_id="test_run",
            )
        )
    assert call_count["n"] == 1  # 不重試
