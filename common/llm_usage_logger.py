"""共用的 Claude API 呼叫用量記錄工具，供所有 Agent 的 LLM 呼叫點使用
（見 00 六章「Claude API 呼叫用量記錄」）。設計動機：`[B]` 的 FILL 曾經
因為 seed.sql 表名比對 bug，讓單次呼叫悄悄帶了 17 萬 token 的 context，
事後只能靠帳戶餘額變化間接猜測，無法精確定位是哪個 Agent、哪個函式
造成的（見 `docs/03_spent_cost_estimate.md`）——只驗證輸出對不對，不驗證
花了多少，是這個缺口的根本原因。

呼叫端規約：必須在呼叫 `messages.create()` 的函式內**直接**呼叫
`log_usage()`，不能包一層中間函式再轉呼叫——不傳 `caller` 參數時，
`log_usage()` 用 `inspect.stack()[1]` 自動抓呼叫端的檔名＋函式名稱，
多包一層會抓到中間層，不是真正打 API 的那個函式。這個設計是為了讓
呼叫端不用手動標記自己是誰，避免日後改名、搬檔案時忘記同步更新標籤
而失準。

**例外**：如果所在專案已經有一個共用的「API 呼叫封裝函式」（例如
`common/llm_client.py` 的 `call_claude_for_json()`，所有需要呼叫
Claude API 的 Agent 都透過它才打 API，見 00 六章），直接呼叫
`log_usage()` 會抓到封裝函式本身、不是真正的業務呼叫端。這種情況下，
封裝函式要在自己的函式內用 `inspect.stack()[1]`（此時抓到的正是它自己
的呼叫端）算出 `caller` 字串，明確傳給
`log_usage(response, model=model, caller=caller)` 覆寫自動偵測。
"""
from __future__ import annotations

import inspect
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOG_PATH = Path(os.environ.get("CLAUDE_API_USAGE_LOG_PATH", "logs/claude_api_usage.jsonl"))


def log_usage(response: Any, *, model: str, caller: str | None = None) -> None:
    """記錄一次 Claude API 呼叫的用量，append 一行 JSON 到 `LOG_PATH`
    （JSON Lines 格式，方便事後寫程式加總算成本，不用人工解析文字 log）。

    `caller`：通常不用傳，預設用 `inspect.stack()[1]` 自動抓直接呼叫端。
    只有共用 API 封裝函式（見上方 module docstring「例外」段落）需要
    自己算好 `caller` 字串明確傳入，覆寫自動偵測。

    記錄失敗（例如磁碟寫入問題）只記警告、不拋例外——這是稽核用的旁路
    紀錄，不該讓記錄本身的問題打斷真正在做的 LLM 呼叫流程。
    """
    if caller is None:
        caller_frame = inspect.stack()[1]
        caller = f"{Path(caller_frame.filename).stem}.{caller_frame.function}"

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "caller": caller,
        "model": model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(
            response.usage, "cache_creation_input_tokens", 0
        ),
        "cache_read_input_tokens": getattr(
            response.usage, "cache_read_input_tokens", 0
        ),
    }

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("寫入 Claude API 用量紀錄失敗（不影響主流程）: %s", exc)
