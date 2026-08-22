# common/logging_setup.py
"""一般執行 log 機制，對應 11a_logging_architecture.md 五章。取代
`main.py` 原本暫時性的 `logging.basicConfig(...)` 那一行。

`configure_logging()` 是唯一對外函式，`main.py` 在產生 `run_id`
（`common/run_context.py`）之後、`build_graph()` 之前呼叫一次；`llmlog`
CLI 是獨立行程，自己的進入點也呼叫一次，但帶 `file_handler=False`（見
下方「為什麼 llmlog 不掛 file handler」）。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from common.trace_context import current_trace_id

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(run_id)s|%(trace_id)s] %(message)s"

# 這幾個 namespace 在 INFO 等級會印出大量連線層細節（method/url/headers），
# 把 logs/orchestrator.log 淹沒成雜訊。寫死在程式碼裡，不經環境變數——
# 「哪些套件很吵」是機械事實，不是每個環境會想覆寫的決策（見 11a 五章）。
_NOISY_THIRD_PARTY_LOGGERS = ("httpx", "httpcore", "anthropic", "urllib3")

# Rotation 策略給合理預設值即可，不做成環境變數，理由同上——操作細節不是
# 每個環境會想覆寫的決策。
_ROTATE_MAX_BYTES = 10 * 1024 * 1024  # 10MB
_ROTATE_BACKUP_COUNT = 5

_configured = False


class _ContextFilter(logging.Filter):
    """把 run_id（同一行程全程不變，建構時閉包捕捉）與 trace_id（每次
    LLM 呼叫都不同，從 contextvars 動態讀取）注入每一筆 log record。
    """

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self._run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self._run_id
        record.trace_id = current_trace_id.get() or "-"
        return True


def _reconfigure_console_encoding() -> None:
    """Windows 預設 console 編碼是 cp950，中文 log 訊息 mangle、事後
    grep 會漏（見 11a 五章「console 的編碼問題」）。某些非標準 stream
    環境（例如 stdout 被重新導向成不支援 reconfigure() 的物件）沒有這個
    方法，容錯跳過，不讓一般 log 機制本身的初始化因為這個環境差異而炸掉。
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (ValueError, OSError):
        pass


def configure_logging(run_id: str, *, file_handler: bool = True) -> None:
    """裝好 root logger 的 handler。可重複呼叫（例如測試情境），第二次
    呼叫會先清掉舊 handler 再重新裝，避免重複輸出。
    """
    global _configured

    level_name = os.environ.get("ORCHESTRATOR_LOG_LEVEL", "INFO")
    level = getattr(logging, level_name.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    context_filter = _ContextFilter(run_id)
    formatter = logging.Formatter(_LOG_FORMAT)

    if file_handler:
        log_path = os.environ.get("ORCHESTRATOR_LOG_PATH", "logs/orchestrator.log")
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        rotating_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=_ROTATE_MAX_BYTES, backupCount=_ROTATE_BACKUP_COUNT,
            encoding="utf-8",
        )
        rotating_handler.setFormatter(formatter)
        rotating_handler.addFilter(context_filter)
        root.addHandler(rotating_handler)

    _reconfigure_console_encoding()
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)
    root.addHandler(console_handler)

    for name in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
