# translator_cli/ollama_client.py
"""ollama 連線與請求契約，對應 07a 七章。translator-cli 不直接打
ollama，而是打 `.env` 的 `OLLAMA_BASE_URL`（指向另一台 Mac 上的
nginx，已含 `/v1` 路徑前綴），帶 `Authorization: Bearer
{OLLAMA_API_KEY}`（見 00 三、四、五章已定案的架構）。

**呼叫記錄**：對應 `11a_logging_architecture.md` 九章。每個格式修正
attempt 各自記一筆 trace（見 `get_function_body()`），不是只記最後一次；
`_call_ollama_once()` 本身不記錄，因為它只知道單次 HTTP 請求結果，不知道
這是第幾次格式修正重試。每個 attempt 在呼叫發出「之前」先寫入
`status="running"` 的 row（只含 prompt），呼叫結束才 upsert 補齊
response／status／latency_ms——本地模型單次生成可能耗時數十秒到數分鐘，
這段等待期間 prompt 已經可以被 `llmlog` 查到，不必等呼叫結束。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from uuid import uuid4

import httpx

from common.llm_trace import record_llm_call, record_llm_call_start
from common.trace_context import current_trace_id
from translator_cli import python_adapter
from translator_cli.exceptions import (
    TranslatorCliConfigError,
    TranslatorCliModelOutputError,
    TranslatorCliNetworkError,
    TranslatorCliUpstreamDegradedError,
)
from translator_cli.prompts import BODY_END, BODY_START, SYSTEM_PROMPT_QWEN, build_user_prompt
from translator_cli.types import ReferencedSourceItem

logger = logging.getLogger(__name__)

# 00 三章「硬體限制」已把模型選型釘死為單一選擇，不經環境變數——跟
# common/llm_client.py 對 Claude 模型「呼叫端自己決定用哪個模型」的
# 分工原則刻意不同（見 07a 七章「連線方式」）。
OLLAMA_MODEL = "qwen2.5-coder:32b"

# 07a 七章「Timeout 與重試策略」表格，環境變數可調、有預設值。
TRANSLATOR_CLI_TIMEOUT_SECONDS = float(os.environ.get("TRANSLATOR_CLI_TIMEOUT_SECONDS", "300"))
# 對應 docs/refactor_bug_trace.md：qwen 這一路原本網路層重試 2 次、
# 格式修正重試再 2 次，兩層疊加、每層每次最長可等到
# TRANSLATOR_CLI_TIMEOUT_SECONDS（300 秒），真實環境對真實
# qwen2.5-coder:32b 實測，單一個 task 卡在這兩層重試裡最長可以耗掉
# 20~45 分鐘才等到失敗訊號、才輪到 client.py::fill_function() 的
# Claude fallback 接手。qwen 本身已經在掙扎的呼叫，重試不會讓它突然
# 變聰明（重試解決的是「暫時性」問題，不是「這個模型這次就是做不到」的
# 問題）——與其在同一顆會失敗的模型上反覆等待，不如失敗一次就立刻換
# Claude API 重試同一個 task（見 client.py 的 fallback），兩層重試次數
# 都改成 0：只試一次，失敗就馬上交棒。
TRANSLATOR_CLI_NETWORK_RETRIES = int(os.environ.get("TRANSLATOR_CLI_NETWORK_RETRIES", "0"))
_NETWORK_RETRY_DELAY_SECONDS = 3.0
# qwen 專屬的格式修正重試次數——刻意跟 Claude 路徑（claude_client.py）
# 的 FORMAT_RETRY_COUNT 分開成兩個獨立常數，不能共用：上面的理由（重試
# 不會讓一個正在掙扎的模型變聰明）只對 qwen 這條路徑成立，Claude 呼叫
# 快、便宜，沒有「等待時間過長」這個痛點，Claude 路徑仍沿用原本的
# FORMAT_RETRY_COUNT=2，不受這次調整影響。
OLLAMA_FORMAT_RETRY_COUNT = int(os.environ.get("OLLAMA_FORMAT_RETRY_COUNT", "0"))
# Claude 路徑（claude_client.py）自己的格式修正重試上限，從這裡 import，
# 不重複定義；qwen 路徑改用上面的 OLLAMA_FORMAT_RETRY_COUNT，不再共用
# 這個常數（見 07a 七章「兩條路徑共用：body_text 格式修正重試」）。
FORMAT_RETRY_COUNT = 2

# 對應 07a 七章「分派方式」／八章「寫入段序列化」：實體機器單一 qwen
# 模型的併發限制收在這裡自己管理——只序列化「真正打 ollama」這一段，
# 不連帶序列化 Claude 路徑（見 client.py::fill_function()「依 backend
# 決定要不要 acquire 這個號誌」）。
OLLAMA_MODEL_SEMAPHORE = asyncio.Semaphore(1)

# 見 docs/09b_bug_trace.md #35：單一 task 自己的網路層重試（見
# `_call_ollama_once()`）解決不了「上游服務本身異常」這種系統性問題，只
# 是各自獨立地把自己的重試預算燒完。這裡加一個跨 task、模組層級的連續
# 失敗計數器——`MODEL_SEMAPHORE(1)` 本來就把所有呼叫序列化成一個接一個，
# 「連續」在這裡天然對應「最近這幾次呼叫，不管是哪個 task 的，是不是都
# 在傳輸層失敗」，不需要額外的鎖或跨 task 協調機制。任何一次成功拿到
# 回應（含收到錯誤狀態碼的 HTTPStatusError，那也證明連線本身是通的）就
# 歸零；連續失敗次數達到門檻，代表這不太可能是單一 task 的暫時性問題，
# 改拋出 `TranslatorCliUpstreamDegradedError`，讓呼叫端（見
# `client.py::fill_function()`）能提早停下來，不要繼續逐一重試。
UPSTREAM_DEGRADED_THRESHOLD = int(
    os.environ.get("TRANSLATOR_CLI_UPSTREAM_DEGRADED_THRESHOLD", "3")
)
_consecutive_transport_failures = 0

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
    固定間隔 3 秒立即重試 `TRANSLATOR_CLI_NETWORK_RETRIES` 次（預設 0，
    見上方常數定義：對一個正在掙扎的本地模型，重試不會讓它突然變聰明，
    失敗一次就直接交給 client.py 的 Claude fallback 更快更可靠；環境
    變數調大時才會真的重試）——這是 LAN 內部連線的暫時抖動，不是 Claude
    API 那種需要等配額恢復的限流情境，不需要 5 分鐘等待。
    **`httpx.HTTPStatusError`（收到回應，但狀態碼是
    4xx／5xx）刻意不算進這個重試迴圈**：07a 明確把重試範圍限定在傳輸層
    錯誤，HTTP 狀態碼錯誤是不同性質——例如 nginx token 設錯導致每次都
    回 401，這是必然失敗、重試沒有意義的情況，立即失敗回報，不拖慢
    失敗訊號的傳遞速度。

    `httpx.AsyncClient` 建立在重試迴圈外、只建立一次：同一次呼叫的多次
    網路層重試是短時間內（間隔 `_NETWORK_RETRY_DELAY_SECONDS` 秒）對同
    一個 `base_url` 的連續嘗試，沒有理由每次重試都重新握手 TCP／TLS。
    這個 client 是單一函式呼叫內的區域資源、隨 `async with` 結束自動
    釋放，不跨呼叫、不跨時間共用——`OLLAMA_MODEL_SEMAPHORE(1)`（見上方
    常數）已經把這條路徑序列化，不存在真正的併發連線需求，因此不做跨
    呼叫的 client 重用（module-level singleton）。

    整段函式（含網路層重試迴圈）都在 `OLLAMA_MODEL_SEMAPHORE` 底下執行
    ——見呼叫端 `get_function_body()`，這裡本身不 acquire，避免重試迴圈
    內部重入造成死鎖。
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

    global _consecutive_transport_failures

    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=TRANSLATOR_CLI_TIMEOUT_SECONDS) as client:
        for attempt in range(TRANSLATOR_CLI_NETWORK_RETRIES + 1):
            try:
                response = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # 有收到回應，只是狀態碼是錯誤——連線本身是通的，歸零連續
                # 失敗計數器（見模組層級 _consecutive_transport_failures
                # 說明），立即失敗，不進重試迴圈。
                _consecutive_transport_failures = 0
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

            _consecutive_transport_failures = 0
            data = response.json()
            return data["choices"][0]["message"]["content"]

    _consecutive_transport_failures += 1
    message = (
        f"ollama 連線失敗，已重試 {TRANSLATOR_CLI_NETWORK_RETRIES} 次仍失敗：{last_error}"
    )
    # `from last_error` 讓 __cause__ 帶著真正的傳輸層例外——get_function_body()
    # 靠這個判斷 status 該記 "timeout" 還是 "error"（見 11a 九章），也讓
    # traceback 保留原始成因，不只是這裡重新組的訊息字串。
    if _consecutive_transport_failures >= UPSTREAM_DEGRADED_THRESHOLD:
        raise TranslatorCliUpstreamDegradedError(
            f"連續 {_consecutive_transport_failures} 次（跨不同呼叫）都在傳輸層失敗"
            f"（門檻 {UPSTREAM_DEGRADED_THRESHOLD}），懷疑是上游 ollama／nginx 服務本身"
            f"異常（掛了／重啟中／過載），不是單一暫時性問題：{message}"
        ) from last_error
    raise TranslatorCliNetworkError(message) from last_error


# 對應 docs/refactor_bug_trace.md #7：`_call_ollama_once()` 的連續失敗
# 偵測（上方 UPSTREAM_DEGRADED_THRESHOLD）是被動的——要等真的排到
# repository task、燒完門檻次數才會發現。這裡補一個主動預檢，讓
# implement_node.py 在真正第一次進入時（見該檔案 run()）就先確認網路
# 通不通，不用等浪費掉幾次真正的模型呼叫預算才知道連不上。
OLLAMA_PREFLIGHT_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_PREFLIGHT_TIMEOUT_SECONDS", "10"))


async def check_ollama_reachable() -> None:
    """比照 `graph/nodes/implement_node.py::_get_reload_probe_id()` 對
    Python 服務的初始探測同一種「操作前提沒滿足，不吞、直接往上拋」原則
    ——只確認連線本身通不通（收到任何 HTTP 回應，不論狀態碼），不呼叫
    真正的 chat completion（探測本身不該浪費一次模型呼叫的時間與成本）。
    只在呼叫端確認這次 run 確實有 qwen／repository task 時才需要呼叫這個
    函式（見呼叫端），沒有 qwen task 的 run 不需要 ollama 連線，不該因為
    這個預檢而被誤擋。
    """
    try:
        base_url = os.environ["OLLAMA_BASE_URL"]
    except KeyError as exc:
        raise TranslatorCliConfigError(
            f"缺少必要的環境變數 {exc}（見 07a 七章「連線方式」，需在 .env 設定 OLLAMA_BASE_URL／OLLAMA_API_KEY）"
        ) from exc

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_PREFLIGHT_TIMEOUT_SECONDS) as client:
            await client.get(base_url)
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        raise TranslatorCliUpstreamDegradedError(
            f"啟動前預檢：連不到 ollama（{base_url}），懷疑是網路連線或 "
            "ollama／nginx 服務本身異常，不是可以透過 debug 重試修好的翻譯"
            f"品質問題——請確認 OLLAMA_BASE_URL 是否正確、這台機器是否連得到"
            f"該服務後再重跑：{exc}"
        ) from exc
    # 收到任何 HTTP 回應（含 4xx/5xx，例如 base_url 本身不接受 GET）都
    # 代表連線本身是通的，視為預檢通過——這裡刻意不呼叫 raise_for_status()。


async def get_function_body(
    *,
    current_signature: str,
    java_source: str,
    referenced_source: list[ReferencedSourceItem],
    context: str,
    context_files: list[tuple[str, str]],
    function_name: str,
    task_id: str | None = None,
    target_file: str | None = None,
    class_name: str | None = None,
    run_id: str,
) -> str:
    """對外唯一入口，對應 07a 七章「模型輸出格式錯誤的修正重試」。

    整段函式（含網路層重試、格式修正重試）都在 `OLLAMA_MODEL_SEMAPHORE`
    底下執行——見下方迴圈開頭 `async with OLLAMA_MODEL_SEMAPHORE`，這是
    實體機器單一 qwen 模型的硬體限制，收在本模組自己管理（見 07a 七章
    「分派方式」）。

    `java_source`／`referenced_source` 是模型輸入，`description` 不是
    （06a 五章已定調改為純機械模板、只供 log 用途，見 07a 五章）。

    格式修正重試次數由 `OLLAMA_FORMAT_RETRY_COUNT` 決定（預設 0，即
    「失敗一次就交棒」，見上方常數定義的理由）；環境變數調大時，每次
    失敗後下一次呼叫的 user prompt 會額外附上「上一次回應違反格式／
    語法錯誤訊息...請重新產生」（見 `prompts.build_user_prompt()` 的
    `error_feedback` 引數）。四種觸發情況統一由 `_extract_delimited_body()`
    （delimiter 抽取）與 `python_adapter.extract_body_statements()`
    （語法／空 body／簽名重複輸出）覆蓋，這裡只驗證、不使用回傳的陳述式
    清單——真正的替換發生在 `PythonAdapter.splice_body()`，這裡的驗證
    只是為了決定要不要觸發重試，兩處共用同一份 `extract_body_statements()`
    邏輯，避免重複實作。

    回傳已驗證過的 `body_source` 原始文字（未縮排陳述式）；格式修正重試
    次數耗盡仍失敗，拋出 `TranslatorCliModelOutputError`，交由呼叫端
    （`client.fill_function()`）轉成 Claude API fallback 或
    `FillResult(success=False, ...)`。

    `run_id` 是必填參數：`fill_function()` 已經在最外層解析成具體值
    （見 11a 九章「run_id 的解析只在 fill_function() 做一次」），這裡不
    再自己判斷，避免同一個 task 的多次格式修正 attempt 拿到不同 run_id。
    `task_id`／`target_file`／`class_name` 是純記錄用的選填參數。

    **每個 attempt（不論成功、格式錯誤、還是傳輸層例外）恰好產生一筆
    trace row，寫入時機統一在該次 attempt 的 `finally` 區塊**（見 11a
    九章），不是只記最後一次。
    """
    error_feedback: str | None = None
    last_error: Exception | None = None

    # 整段格式修正重試迴圈都在號誌底下——實體機器單一 qwen 模型，即使
    # OLLAMA_FORMAT_RETRY_COUNT 調大、真的有第二、三次修正 attempt，也
    # 不能跟其他 task 的 qwen 呼叫交錯執行（見上方 OLLAMA_MODEL_SEMAPHORE
    # 常數說明）。
    async with OLLAMA_MODEL_SEMAPHORE:
        for attempt in range(OLLAMA_FORMAT_RETRY_COUNT + 1):
            user_prompt = build_user_prompt(
                current_signature=current_signature,
                java_source=java_source,
                referenced_source=referenced_source,
                context=context,
                context_files=context_files,
                error_feedback=error_feedback,
            )

            trace_id = str(uuid4())
            token = current_trace_id.set(trace_id)
            start = time.monotonic()
            status, error_msg, raw_text = "error", None, None
            prompt_text = f"=== SYSTEM ===\n{SYSTEM_PROMPT_QWEN}\n\n=== USER ===\n{user_prompt}"

            # 呼叫實際發出「之前」就寫入 status="running" 的 row（見
            # 11a_logging_architecture.md 七章「呼叫開始即寫入 running row」）：
            # 本地模型單次生成可能耗時數十秒到數分鐘，這段等待期間 prompt
            # 已經可以被 llmlog 查到，不必等下面 finally 補上最終結果——這是
            # 真實環境「長時間等待本地 Ollama、事後查不出當時送了什麼」這個
            # 缺口的直接對策。
            record_llm_call_start(
                trace_id=trace_id, run_id=run_id, vendor="ollama", model=OLLAMA_MODEL,
                caller="ollama_client.get_function_body",
                task_id=task_id, attempt=attempt,
                target_file=target_file, class_name=class_name, function_name=function_name,
                prompt=prompt_text,
            )

            try:
                raw_text = await _call_ollama_once(SYSTEM_PROMPT_QWEN, user_prompt)
                body_source = _extract_delimited_body(raw_text)
                python_adapter.extract_body_statements(body_source, function_name)
                status = "ok"
                return body_source
            except (TranslatorCliNetworkError, TranslatorCliConfigError) as exc:
                # 傳輸層／環境變數缺失：既有設計本來就不進格式重試迴圈，直接
                # 往外拋（見 07a 七章），這裡只是在拋出之前，先讓 finally
                # 保證這次 attempt 留痕。
                error_msg = str(exc)
                status = "timeout" if isinstance(exc.__cause__, httpx.TimeoutException) else "error"
                raise
            except TranslatorCliModelOutputError as exc:
                # delimiter／語法驗證失敗：進格式修正重試迴圈，raw_text 依然
                # 有值（模型確實回應了，只是內容不符契約），留在下面正常記錄。
                error_msg = str(exc)
                last_error = exc
                error_feedback = str(exc)
                if attempt < OLLAMA_FORMAT_RETRY_COUNT:
                    logger.warning("模型輸出格式錯誤（第 %d 次），重新呼叫一次修正：%s", attempt + 1, exc)
                    continue
                # 未達上限時已經 continue；這裡是最後一次 attempt 也失敗，
                # 落到迴圈自然結束，finally 仍會先記錄這次 attempt，然後在
                # 迴圈外統一拋出（見下方）。
            except Exception as exc:
                # 涵蓋以上兩類以外的意外例外（如 extract_body_statements()
                # 內部真正非預期的錯誤），理由同 common/llm_client.py 的同類
                # 兜底分支——確保 error_msg 一定有內容，不留一筆查不出原因的
                # status='error' row。
                status = "error"
                error_msg = repr(exc)
                raise
            finally:
                latency_ms = int((time.monotonic() - start) * 1000)
                # current_trace_id.reset() 必須排在 record_llm_call() 之前，
                # 理由同 common/llm_client.py（見 11a 八章「為什麼 reset()
                # 要排在 record_llm_call() 之前」）。
                current_trace_id.reset(token)
                record_llm_call(
                    trace_id=trace_id, run_id=run_id, vendor="ollama", model=OLLAMA_MODEL,
                    caller="ollama_client.get_function_body",
                    task_id=task_id, attempt=attempt,
                    target_file=target_file, class_name=class_name, function_name=function_name,
                    prompt=prompt_text,
                    response=raw_text,
                    latency_ms=latency_ms, status=status, error_msg=error_msg,
                )

    raise TranslatorCliModelOutputError(f"模型輸出格式錯誤，修正重試仍失敗：{last_error}")
