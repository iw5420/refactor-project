# translator_cli/ollama_client.py
"""ollama 連線與請求契約，對應 07a 七章。translator-cli 不直接打
ollama，而是打 `.env` 的 `OLLAMA_BASE_URL`（指向另一台 Mac 上的
nginx，已含 `/v1` 路徑前綴），帶 `Authorization: Bearer
{OLLAMA_API_KEY}`（見 00 三、四、五章已定案的架構）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

from translator_cli import python_adapter
from translator_cli.exceptions import (
    TranslatorCliConfigError,
    TranslatorCliModelOutputError,
    TranslatorCliNetworkError,
)
from translator_cli.prompts import BODY_END, BODY_START, SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

# 00 三章「硬體限制」已把模型選型釘死為單一選擇，不經環境變數——跟
# common/llm_client.py 對 Claude 模型「呼叫端自己決定用哪個模型」的
# 分工原則刻意不同（見 07a 七章「連線方式」）。
OLLAMA_MODEL = "qwen2.5-coder:32b"

# 07a 七章「Timeout 與重試策略」表格，環境變數可調、有預設值。
TRANSLATOR_CLI_TIMEOUT_SECONDS = float(os.environ.get("TRANSLATOR_CLI_TIMEOUT_SECONDS", "300"))
TRANSLATOR_CLI_NETWORK_RETRIES = int(os.environ.get("TRANSLATOR_CLI_NETWORK_RETRIES", "2"))
_NETWORK_RETRY_DELAY_SECONDS = 3.0
# delimiter／語法驗證失敗重試固定 2 次，跟網路層重試是不同的錯誤類型、
# 不同的重試機制，不共用同一個計數器（見 07a 七章）。原本固定 1 次，
# 真實環境對真實 qwen2.5-coder:32b 實測時發現：對複雜任務（context 檔案
# 多、邏輯複雜），qwen 第一次回應違反 delimiter 格式的機率不低，固定
# 重試 1 次不一定夠——調成 2 次仍是有界重試，不是無限重試。
_FORMAT_RETRY_COUNT = 2

_DELIMITER_RE = re.compile(re.escape(BODY_START) + r"\n?(.*?)\n?" + re.escape(BODY_END), re.DOTALL)


def _extract_delimited_body(raw_text: str) -> str:
    """對應 07a 六章「delimiter 契約：模型回傳格式」抽取邏輯：正則找
    兩個 sentinel 之間的文字。找不到 → 視為「delimiter 契約違反」，
    觸發七章重試策略（見 `get_function_body()`）。
    """
    match = _DELIMITER_RE.search(raw_text)
    if not match:
        raise TranslatorCliModelOutputError(
            f"delimiter 契約違反：模型回應找不到 {BODY_START} / {BODY_END} 標記"
        )
    return match.group(1)


async def _call_ollama_once(system_prompt: str, user_prompt: str) -> str:
    """對應 07a 七章「Timeout 與重試策略」表格：HTTP 連線失敗／timeout
    這類傳輸層錯誤（`httpx.TransportError`／`httpx.TimeoutException`），
    固定間隔 3 秒立即重試 `TRANSLATOR_CLI_NETWORK_RETRIES` 次——這是 LAN
    內部連線的暫時抖動，不是 Claude API 那種需要等配額恢復的限流情境，
    不需要 5 分鐘等待。**`httpx.HTTPStatusError`（收到回應，但狀態碼是
    4xx／5xx）刻意不算進這個重試迴圈**：07a 明確把重試範圍限定在傳輸層
    錯誤，HTTP 狀態碼錯誤是不同性質——例如 nginx token 設錯導致每次都
    回 401，這是必然失敗、重試沒有意義的情況，立即失敗回報，不拖慢
    失敗訊號的傳遞速度。

    `httpx.AsyncClient` 建立在重試迴圈外、只建立一次：同一次呼叫的多次
    網路層重試是短時間內（間隔 `_NETWORK_RETRY_DELAY_SECONDS` 秒）對同
    一個 `base_url` 的連續嘗試，沒有理由每次重試都重新握手 TCP／TLS。
    這個 client 是單一函式呼叫內的區域資源、隨 `async with` 結束自動
    釋放，不跨呼叫、不跨時間共用——`implement_node.py` 的
    `MODEL_SEMAPHORE(1)` 已經把這條路徑序列化，不存在真正的併發連線
    需求，因此不做跨呼叫的 client 重用（module-level singleton）。
    """
    try:
        base_url = os.environ["OLLAMA_BASE_URL"]
        api_key = os.environ["OLLAMA_API_KEY"]
    except KeyError as exc:
        raise TranslatorCliConfigError(
            f"缺少必要的環境變數 {exc}（見 07a 七章「連線方式」，需在 .env 設定 OLLAMA_BASE_URL／OLLAMA_API_KEY）"
        ) from exc

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=TRANSLATOR_CLI_TIMEOUT_SECONDS) as client:
        for attempt in range(TRANSLATOR_CLI_NETWORK_RETRIES + 1):
            try:
                response = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # 有收到回應，只是狀態碼是錯誤——立即失敗，不進重試迴圈。
                raise TranslatorCliNetworkError(
                    f"ollama 回應 HTTP 錯誤狀態碼（非傳輸層錯誤，不重試）：{exc}"
                ) from exc
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt < TRANSLATOR_CLI_NETWORK_RETRIES:
                    logger.warning(
                        "ollama 連線失敗（第 %d/%d 次），%.0f 秒後重試：%s",
                        attempt + 1,
                        TRANSLATOR_CLI_NETWORK_RETRIES,
                        _NETWORK_RETRY_DELAY_SECONDS,
                        exc,
                    )
                    await asyncio.sleep(_NETWORK_RETRY_DELAY_SECONDS)
                continue

            data = response.json()
            return data["choices"][0]["message"]["content"]

    raise TranslatorCliNetworkError(
        f"ollama 連線失敗，已重試 {TRANSLATOR_CLI_NETWORK_RETRIES} 次仍失敗：{last_error}"
    )


async def get_function_body(
    *,
    current_signature: str,
    description: str,
    context: str,
    context_files: list[tuple[str, str]],
    function_name: str,
) -> str:
    """對外唯一入口，對應 07a 七章「模型輸出格式錯誤的修正重試」。

    固定重試 2 次（`_FORMAT_RETRY_COUNT`）：每次失敗後，下一次呼叫的
    user prompt 額外附上「上一次回應違反格式／語法錯誤訊息...請重新
    產生」（見 `prompts.build_user_prompt()` 的 `error_feedback`
    引數）。四種觸發情況統一由 `_extract_delimited_body()`（delimiter
    抽取）與 `python_adapter.extract_body_statements()`（語法／空
    body／簽名重複輸出）覆蓋，這裡只驗證、不使用回傳的陳述式清單——
    真正的替換發生在 `PythonAdapter.splice_body()`，這裡的驗證只是為了
    決定要不要觸發重試，兩處共用同一份 `extract_body_statements()`
    邏輯，避免重複實作。

    回傳已驗證過的 `body_source` 原始文字（未縮排陳述式）；固定重試
    2 次仍失敗，拋出 `TranslatorCliModelOutputError`，交由呼叫端
    （`client.fill_function()`）轉成 `FillResult(success=False, ...)`。
    """
    error_feedback: str | None = None
    last_error: Exception | None = None

    for attempt in range(_FORMAT_RETRY_COUNT + 1):
        user_prompt = build_user_prompt(
            current_signature=current_signature,
            description=description,
            context=context,
            context_files=context_files,
            error_feedback=error_feedback,
        )
        raw_text = await _call_ollama_once(SYSTEM_PROMPT, user_prompt)

        try:
            body_source = _extract_delimited_body(raw_text)
            python_adapter.extract_body_statements(body_source, function_name)
            return body_source
        except TranslatorCliModelOutputError as exc:
            last_error = exc
            error_feedback = str(exc)
            if attempt < _FORMAT_RETRY_COUNT:
                logger.warning("模型輸出格式錯誤（第 %d 次），重新呼叫一次修正：%s", attempt + 1, exc)
                continue

    raise TranslatorCliModelOutputError(f"模型輸出格式錯誤，修正重試仍失敗：{last_error}")
