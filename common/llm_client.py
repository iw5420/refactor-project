"""所有需要呼叫 Claude API 的 Agent（見 00 三章 LLM 分工：解析、規劃、
設計、Debug 等雲端任務）共用的 Claude API 呼叫封裝，走雲端而非本地 qwen。
只做組 prompt、呼叫、parse JSON、拋例外；prompt 內容與 JSON schema 由各
Agent 自己的 `prompts.py` 負責（各 Agent 的 schema 集中定義在自己的
`prompts.py`，跟對應的 system prompt 放一起，不放在這裡）。

這裡不處理任何 Agent 專屬的設定（例如用哪個環境變數選模型）——各 Agent
自己的 `llm.py` 只需要兩三行：讀自己的環境變數（如 `PARSE_AGENT_MODEL`），
沒設定時退回 `DEFAULT_MODEL_FALLBACK`，再把算好的 `model` 字串傳進這裡
的 `call_claude_for_json()`。`model` 在這裡是必填參數，刻意不給預設值
——這個模組不該知道任何 Agent 的環境變數命名慣例，「沒傳 model 時要用
哪個模型」是每個 Agent 自己的決策，不該由共用層代為決定。

**格式保證用 Structured Outputs（`output_config.format`），不是
prefill**：`claude-sonnet-4-6`（Claude 4.6 系列）已經不支援 prefill，
呼叫會直接被 API 拒絕（400 錯誤），改用 `output_config.format` 把 JSON
Schema 編譯成文法，在生成階段直接限制 token 選擇（constrained decoding）。

傳輸層級的暫時性錯誤（連線、429、5xx，含 529 Overloaded）已由
`anthropic` SDK 內建 `max_retries`（預設 2、指數退避）處理，不需要應用層
再做額外重試。

**明確設定 `timeout`，不依賴 SDK 預設值**：2026-08-28 真實環境撞到一次
單一呼叫卡住 30 分鐘以上、`status` 從未變成 `error`／`timeout` 的案例
（見 `docs/09b_bug_trace.md` 對應條目）——沒有留下足夠證據證實 SDK 預設
的逾時機制在這個案例裡確實失效，但既然已知會發生「呼叫卡住不返回」，
就不該讓整條 pipeline 沒有任何保底機制陪著一起卡住。`_get_client()`
明確傳入 `timeout=300.0`（5 分鐘），比這個專案已知最大的 prompt／回應
量級留有餘裕，逾時後交給 SDK 既有的 `max_retries` 重試，重試後仍失敗
才真正冒出例外，讓呼叫端既有的重試／`give_up` 機制接手，不再無限期卡死。

**這個函式是同步函式，不是 `async def`**：Map 階段用
`concurrent.futures.ThreadPoolExecutor.submit()` 呼叫它（見 00 六章），
是被丟進執行緒池、用阻塞呼叫的方式跑的普通函式（見
`11a_logging_architecture.md` 八章）。

**呼叫記錄**：不透過 `common/llm_usage_logger.py::log_usage()`（該模組
已退場，見 11a 八章「`log_usage()` 退場」）——那個簽名結構上就拿不到
`system_prompt`／`user_prompt`／呼叫起訖時間。改成呼叫發出前先呼叫
`common/llm_trace.py::record_llm_call_start()` 寫入 `status="running"`
的 row（讓長時間等待中的呼叫也查得到當時送了什麼），`finally` 再呼叫
`record_llm_call()` 用同一個 `trace_id` 補齊 response、token 用量、耗時、
成功與否，一併記進 `llm_traces.db`，見 11a 八章「控制流程需求」。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import anthropic

from common.llm_trace import record_llm_call, record_llm_call_start
from common.run_context import adhoc_run_id
from common.trace_context import current_trace_id

# 各 Agent 自己的 llm.py 讀不到環境變數時的保底 fallback；不是「選定的
# 模型」，只是沒設定任何環境變數時不至於整個炸掉的最低限度預設值。
DEFAULT_MODEL_FALLBACK = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096

_client: anthropic.Anthropic | None = None
# 多個 Agent 的 Map 階段都用 ThreadPoolExecutor 平行呼叫，多個 thread
# 可能同時看到 `_client is None` 而各自搶著 new 一個 Anthropic()——不會讓
# 程式壞掉，但會浪費資源、產生用不到的孤兒 client 實例。用雙重檢查鎖定
# （double-checked locking）確保只初始化一次：快路徑（已初始化）完全不用
# 碰鎖，只有初始化那個短暫窗口需要排隊。
_client_lock = threading.Lock()


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # 鎖內再檢查一次，避免重複初始化
                _client = anthropic.Anthropic(timeout=300.0)  # 讀 ANTHROPIC_API_KEY 環境變數
    return _client


class LlmJsonError(Exception):
    """LLM 呼叫本身失敗，或回傳內容無法解析成合法 JSON。後者理論上不該
    發生——`output_config.format` 已經用 constrained decoding 保證輸出
    符合 schema——會走到這裡代表 API 端的保證被打破（例如版本行為變化），
    視為硬性失敗，不重試：重試不會讓「API 保證」這件事本身變得更可信。
    一律帶著 `raw_text`（模型的原始回應），讓失敗當下的現場能被還原，
    不用重跑猜測。
    """

    def __init__(self, message: str, *, raw_text: str | None = None) -> None:
        super().__init__(message)
        self.raw_text = raw_text

    def __str__(self) -> str:
        base = super().__str__()
        if self.raw_text is None:
            return base
        return f"{base}\n---- 原始回應 ----\n{self.raw_text}"


def call_claude_for_json(
    *,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    model: str,
    run_id: str | None = None,
    task_id: str | None = None,
    target_file: str | None = None,
    class_name: str | None = None,
    function_name: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Any:
    """呼叫 Claude API，用 `output_config.format`（Structured Outputs）
    要求輸出符合 `schema`，回傳 parse 後的 Python 物件。

    `model`：必填，呼叫端自己的 `llm.py` 算好（讀自己的環境變數＋
    `DEFAULT_MODEL_FALLBACK` 保底）再傳進來，這個函式不猜測任何 Agent
    專屬的預設值（見模組 docstring）。

    `schema`：完整的 JSON Schema，由呼叫端從自己的 `prompts.py` 引入
    對應的 `*_OUTPUT_SCHEMA` 常數。

    `run_id`：選填，省略時就地呼叫 `adhoc_run_id()`（見
    `common/run_context.py`）——這個函式本身就是 Claude 呼叫鏈的最外層，
    fallback 在這裡解析一次即可，不需要再往下傳。

    `task_id`／`target_file`／`class_name`／`function_name`：純記錄用的
    選填參數，讓 `llmlog task`／`llmlog func` 也查得到 Claude 呼叫的歷史
    （見 11a 八章）。不是每個呼叫都填得出來——Map 階段批次呼叫涵蓋多個
    Java class 時，`target_file`／`class_name` 語意上無法化約成單一值，
    維持 `None` 是正確行為，不是遺漏。
    """
    caller_frame = sys._getframe(1)  # noqa: SLF001 - 比 inspect.stack() 便宜一到兩個數量級
    caller = f"{Path(caller_frame.f_code.co_filename).stem}.{caller_frame.f_code.co_name}"

    resolved_run_id = run_id or adhoc_run_id()
    trace_id = str(uuid4())
    token = current_trace_id.set(trace_id)
    start = time.monotonic()
    status: str = "error"  # 預設值：finally 執行時若還是這個值，代表中途有例外
    http_code: int | None = None
    error_msg: str | None = None
    raw_text: str | None = None
    usage = None
    prompt_text = f"=== SYSTEM ===\n{system_prompt}\n\n=== USER ===\n{user_prompt}"

    # 呼叫實際發出「之前」就寫入 status="running" 的 row（見
    # 11a_logging_architecture.md 七章「呼叫開始即寫入 running row」）：
    # 這次呼叫還沒結束、甚至卡住很久時，prompt 已經可以被 llmlog 查到，
    # 不必等下面 finally 的 record_llm_call() 補上最終結果。
    record_llm_call_start(
        trace_id=trace_id, run_id=resolved_run_id, vendor="claude", model=model, caller=caller,
        attempt=0, task_id=task_id, target_file=target_file, class_name=class_name, function_name=function_name,
        prompt=prompt_text,
    )

    try:
        response = _get_client().messages.create(
            model=model,
            max_tokens=max_tokens,
            # 所有呼叫端做的都是分類/抽取，不是需要發散的生成任務，固定
            # temperature=0 收斂同一輸入下不同次呼叫的判斷結果（不影響
            # 計費，token 用量才是計價依據）。
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        usage = response.usage
        raw_text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        parsed = json.loads(raw_text)
        status = "ok"  # 只有走到這裡（json.loads 成功）才標記成功
        return parsed
    except anthropic.APITimeoutError as exc:
        # 必須排在 anthropic.APIError 之前——APITimeoutError 是 APIError
        # 的子類別，except 順序由上而下比對，子類別要放前面，否則永遠會
        # 被下面較泛用的 APIError 分支接住，狀態就分不出是不是逾時。
        status = "timeout"
        error_msg = str(exc)
        raise LlmJsonError(f"Claude API 呼叫逾時: {exc}") from exc
    except anthropic.APIError as exc:
        status = "error"
        http_code = getattr(exc, "status_code", None)
        error_msg = str(exc)
        raise LlmJsonError(f"Claude API 呼叫失敗: {exc}") from exc
    except json.JSONDecodeError as exc:
        status = "error"
        error_msg = str(exc)
        raise LlmJsonError(
            f"Claude 回應無法解析為 JSON（output_config 應保證格式合法，"
            f"這是異常情況）: {exc}",
            raw_text=raw_text,
        ) from exc
    except Exception as exc:
        # 涵蓋以上三種以外、真正意外的例外（SDK 內部錯誤、response 物件
        # 形狀不符預期等）。沒有這個分支，這類例外會留下一筆
        # status='error' 但 error_msg=NULL 的 row——有留痕但查不出原因，
        # 等於沒查。不轉換成 LlmJsonError（不屬於這個函式原本的錯誤契約），
        # 讓原始例外類型原樣往外傳，這裡只負責補上記錄用的 error_msg。
        status = "error"
        error_msg = repr(exc)
        raise
    finally:
        latency_ms = int((time.monotonic() - start) * 1000)
        # current_trace_id.reset() 必須排在 record_llm_call() 之前——
        # 見 11a_logging_architecture.md 八章「為什麼 reset() 要排在
        # record_llm_call() 之前」：record_llm_call() 內部吞掉自己的寫入
        # 例外只是內部契約，不是語言保證，萬一它自己出現非預期的 bug 而
        # 拋出例外，reset() 排在後面就永遠不會執行，trace_id 會殘留污染
        # 這個（可能被 ThreadPoolExecutor 重用的）執行緒後續的 log。
        current_trace_id.reset(token)
        record_llm_call(
            trace_id=trace_id, run_id=resolved_run_id, vendor="claude", model=model, caller=caller,
            attempt=0,  # Claude 路徑沒有格式重試迴圈，固定填 0，不留 NULL（schema NOT NULL）
            task_id=task_id, target_file=target_file, class_name=class_name, function_name=function_name,
            prompt=prompt_text,
            response=raw_text,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) if usage else None,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) if usage else None,
            latency_ms=latency_ms, status=status, http_code=http_code, error_msg=error_msg,
        )
