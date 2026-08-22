# common/run_context.py
"""run_id 的產生點，對應 11a_logging_architecture.md 六章。

`new_run_id()` 是唯一的正式產生點，只在 `main.py` 啟動時呼叫一次，寫進
`RefactorState.run_id`，之後由呼叫端顯式往下傳（`call_claude_for_json()`／
`fill_function()`），不是全域讀取。

`adhoc_run_id()` 是旁路情境（沒有 RefactorState 可用時，如單元測試、
`01_langgraph_architecture.md` 五章提到的人工續跑捷徑）的防呆 fallback，
`adhoc_` 前綴讓 `llmlog` 的輸出能一眼認出這不是正常 graph 執行路徑產生的
run_id——不論是刻意的旁路呼叫還是漏傳參數的 bug，都值得在查詢結果裡顯眼
標出來。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def new_run_id() -> str:
    """格式 `{YYYYMMDD_HHMMSS}_{uuid4前6碼}`——後綴避免同一秒內啟動兩個
    行程碰撞，單人單機情境下機率極低，但產生成本幾乎為零。
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{timestamp}_{suffix}"


def adhoc_run_id() -> str:
    """旁路情境專用的一次性 fallback，回傳值固定加 `adhoc_` 前綴。"""
    return f"adhoc_{new_run_id()}"
