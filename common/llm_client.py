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
prefill**：原本設計是用 assistant message prefill（呼叫時多帶一個「已經
開始的助手回覆」）從結構上避免模型在正式答案前夾帶推理文字，但實測
`claude-sonnet-4-6`（Claude 4.6 系列）已經不支援 prefill，呼叫會直接被
API 拒絕（400 錯誤）——這是 4.6 的破壞性變更，官方遷移指南建議改用
Structured Outputs。查證後改用 `output_config.format`：把 JSON Schema
編譯成文法，在生成階段直接限制 token 選擇，保證輸出結構上符合 schema
（constrained decoding），不是靠事後解析或重試去猜模型有沒有照做。原本
的「prefill＋擷取信心分級＋有界修正重試」三層防禦機制因此整組拿掉，
格式正確性由 API 本身保證，不需要應用層再做這些事（沿革見
`docs/03c_collection_agent_code.md` 1.2 節）。

傳輸層級的暫時性錯誤（連線、429、5xx，含 529 Overloaded）已由
`anthropic` SDK 內建 `max_retries`（預設 2、指數退避）處理，不需要應用層
再做額外重試。
"""
from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path
from typing import Any

import anthropic

from common.llm_usage_logger import log_usage

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
                _client = anthropic.Anthropic()  # 讀 ANTHROPIC_API_KEY 環境變數
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
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Any:
    """呼叫 Claude API，用 `output_config.format`（Structured Outputs）
    要求輸出符合 `schema`，回傳 parse 後的 Python 物件。

    `model`：必填，呼叫端自己的 `llm.py` 算好（讀自己的環境變數＋
    `DEFAULT_MODEL_FALLBACK` 保底）再傳進來，這個函式不猜測任何 Agent
    專屬的預設值（見模組docstring）。

    `schema`：完整的 JSON Schema（不是單純的 Python `dict`/`list` 型別），
    由呼叫端從自己的 `prompts.py` 引入對應的 `*_OUTPUT_SCHEMA` 常數——
    schema 跟它描述的 system prompt 放在同一個檔案，調整其中一個時另一個
    也在旁邊，不容易顧此失彼。

    這個函式是 `common/llm_usage_logger.py` module docstring 講的「共用
    API 封裝函式」例外情況：所有 Agent 的業務函式都透過這裡才打 API，
    `log_usage()` 內部預設的 `inspect.stack()[1]` 會抓到這個函式自己，
    不是真正的業務呼叫端，所以要在這裡（呼叫端的呼叫端）先抓出真正的
    caller，明確傳下去。
    """
    caller_frame = inspect.stack()[1]
    caller = f"{Path(caller_frame.filename).stem}.{caller_frame.function}"

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
    except anthropic.APIError as exc:
        raise LlmJsonError(f"Claude API 呼叫失敗: {exc}") from exc

    log_usage(response, model=model, caller=caller)

    text_parts = [block.text for block in response.content if block.type == "text"]
    raw_text = "".join(text_parts).strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise LlmJsonError(
            f"Claude 回應無法解析為 JSON（output_config 應保證格式合法，"
            f"這是異常情況）: {exc}",
            raw_text=raw_text,
        ) from exc
