# common/trace_context.py
"""單次 LLM 呼叫的 trace_id，對應 11a_logging_architecture.md 六章
「trace_id 與一般 log 的關聯」。

`common/logging_setup.py` 裝的 `logging.Filter` 讀這個 ContextVar，讓一般
log 的每一行自動帶上當次呼叫的 trace_id；`common/llm_client.py`／
`translator_cli/ollama_client.py` 在每次實際呼叫前 `.set()`、`finally`
區塊裡 `.reset()`。

`.set()`／`.reset()` 都發生在同一個函式呼叫、同一個執行緒內，不依賴
`contextvars.copy_context()` 跨執行緒複製（見 11a 六章「ThreadPoolExecutor
下的 contextvars」）。
"""
from __future__ import annotations

import contextvars

current_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_trace_id", default=None
)
