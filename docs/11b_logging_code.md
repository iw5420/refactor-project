# 全局 Log 機制 程式碼實作

> 11a 是設計面文件（決策、schema、控制流程），本文件是實作面文件，一一對應、不重複設計理由——每節開頭註明對應 11a 章節，這裡只講怎麼落地成程式碼。對應 `00_refactor_architecture.md` 十一章文件索引的 `11b_logging_code.md`。

## 目錄

| 檔案 | 對應章節 | 說明 |
|---|---|---|
| `common/run_context.py` | 11a 六章 | `new_run_id()`／`adhoc_run_id()`（新增） |
| `common/trace_context.py` | 11a 六章 | `current_trace_id` ContextVar（新增） |
| `common/logging_setup.py` | 11a 五章 | `configure_logging()`、`_ContextFilter`（新增） |
| `common/llm_trace.py` | 11a 七章 | `llm_traces.db` 讀寫唯一權威來源，含 `record_llm_call_start()`／`record_llm_call()` 的 upsert 兩段式寫入（新增） |
| `common/llm_client.py` | 11a 八章 | `call_claude_for_json()` 整合 `record_llm_call_start()`／`record_llm_call()`（改寫） |
| `translator_cli/ollama_client.py` | 11a 九章 | `get_function_body()` 整合 `record_llm_call_start()`／`record_llm_call()`（改寫） |
| `translator_cli/client.py` | 11a 九章 | `fill_function()` 新增 `run_id` 參數並往下傳 |
| `graph/state.py` | 11a 六章 | `RefactorState` 新增 `run_id` 欄位 |
| `main.py` | 11a 五、六章 | 接上 `new_run_id()`／`configure_logging()`，取代 `logging.basicConfig()` |
| `graph/nodes/implement_node.py` | 11a 九章 | `_run_one_task()` 貫穿 `run_id` |
| `llmlog/cli.py`／`llmlog/__main__.py` | 11a 十一章 | 7 個子指令，各自薄包裝 `common/llm_trace.py` 對應函式（新增套件） |
| `common/llm_usage_logger.py` | 11a 八章「`log_usage()` 退場」 | 刪除，功能併入 `llm_trace.py` |
| `tests/common/test_run_context.py` | 11a 六章 | `new_run_id()`／`adhoc_run_id()` 格式與唯一性（3 個） |
| `tests/common/test_logging_setup.py` | 11a 五章 | handler 裝配、`_ContextFilter` 注入、重複呼叫不重複裝 handler（5 個） |
| `tests/common/test_llm_trace.py` | 11a 七章 | 寫入／查詢／門檻落檔／FTS／gc／`record_llm_call_start()` 兩段式 upsert／即時可見性全覆蓋（36 個） |
| `tests/common/test_llm_client.py` | 11a 八章 | 既有測試同步更新：`log_usage` monkeypatch 改為 `record_llm_call`／`record_llm_call_start`，補上失敗路徑也會記錄、start 在 API 呼叫前執行、start／finish 共用同一 `trace_id` 的驗證 |
| `tests/translator_cli/test_ollama_client.py` | 11a 九章 | 既有測試同步更新：`get_function_body()` 呼叫補上必填的 `run_id`；新增 start 呼叫順序、trace_id 共用的驗證 |

**已驗證**：`python -m pytest -q`（排除一個既有、與本次改動無關、缺 `JAVA_BASE_URL` 環境變數才能收集的 E2E 測試檔）546 個全數通過。`llmlog` 7 個子指令已用真實 SQLite 檔案手動跑過一輪（seed 兩筆 trace → `recent`／`task`／`func`／`search`／`show`／`flag`／`gc`），輸出與 11a 十一章表格逐一核對；另外用一個模擬「本地 Ollama 長時間生成」的真實情境（背景執行緒延遲 3 秒才回應，中途實際執行 `python -m llmlog recent --status running`／`llmlog show <trace_id>`）驗證了本次新增的即時可見性機制，細節見十三章「三項驗證」。

---

## 一、`common/run_context.py`（新增）

對應 11a 六章「run_id 的產生與傳遞」。`new_run_id()` 只在 `main.py` 呼叫一次；`adhoc_run_id()` 是旁路情境的防呆 fallback，`adhoc_` 前綴讓漏傳參數的 bug 在查詢結果裡一眼可辨。

```python
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
```

---

## 二、`common/trace_context.py`（新增）

對應 11a 六章「trace_id 與一般 log 的關聯」。`.set()`／`.reset()` 都發生在同一次呼叫、同一個執行緒內，不需要 `contextvars.copy_context()` 跨執行緒複製——這是 11a 八章「B1」修正過的認知：`trace_id` 是在呼叫本身內部 `.set()`，不是從父 context 繼承。

```python
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
```

---

## 三、`common/logging_setup.py`（新增）

對應 11a 五章「一般執行 log 機制」。`configure_logging()` 取代 `main.py` 原本暫時性的 `logging.basicConfig(...)`。`file_handler=False` 是 `llmlog` CLI 專用（見五章「為什麼 llmlog 不掛 file handler」）：Windows 上 `RotatingFileHandler` rollover 用 `os.rename()`，若跟 Orchestrator 主行程搶同一個開著的 `logs/orchestrator.log` 會 `PermissionError`。

```python
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
```

---

## 四、`common/llm_trace.py`（新增）——`llm_traces.db` 讀寫唯一權威來源

對應 11a 七章「LLM Trace 統一儲存設計」／「查詢與維護層設計」／「呼叫開始即寫入 running row」。寫入分兩段：`record_llm_call_start()` 在呼叫發出**之前**立刻寫入一筆 `status="running"` 的 row（只含 prompt），`record_llm_call()` 在呼叫結束的 `finally` 用同一個 `trace_id` upsert 補齊剩餘欄位——這是對「長時間等待本地 Ollama、查不出當時送了什麼」（09b_bug_trace.md 記錄的真實問題）的直接對策：呼叫還沒結束、甚至卡住很久，prompt 都已經可以查到。`get_trace()`／`get_traces_for_task()`／`get_traces_for_function()`／`list_recent()`／`search_traces()`／`latest_run_id()`／`gc_orphan_payloads()` 是查詢與維護入口，`llmlog` CLI（十一章）與未來 ⑦ Debug Agent 共用同一組，不重新實作查詢邏輯。

```python
# common/llm_trace.py
"""`llm_traces.db` 讀寫的唯一權威來源，對應 11a_logging_architecture.md
七章「LLM Trace 統一儲存設計」／「查詢與維護層設計」。

寫入分兩段：`record_llm_call_start()` 在呼叫實際發出**之前**立刻寫入一筆
`status="running"` 的 row（只含 `prompt`），`record_llm_call()` 在呼叫
結束的 `finally` 用同一個 `trace_id` upsert 補齊 `response`／最終
`status`／耗時等欄位。這樣一次呼叫不論多久（甚至還沒結束、正在長時間
等待中），`prompt` 都已經可以被查到——不必等呼叫完成。兩者都是
`common/llm_client.py::call_claude_for_json()` 與
`translator_cli/ollama_client.py::get_function_body()` 共用的寫入入口；
`get_trace()`／`get_traces_for_task()`／`get_traces_for_function()`／
`list_recent()`／`search_traces()`／`latest_run_id()` 是讀取入口，
`llmlog` CLI 與 ⑦ Debug Agent（`debug_agent/analysis.py`）共用同一組——
`llmlog` 的每個子指令都是對應函式的薄包裝，不重新實作查詢邏輯。

連線是 lazy 建立（第一次呼叫才建立，比照 `common/llm_client.py::_get_client()`
的雙重檢查鎖定風格），避免模組被提早 import 時讀到 `.env` 載入前的環境
變數。單一連線＋單一 `threading.Lock` 保護所有存取（`check_same_thread=False`
只是關掉 Python 自己的執行緒歸屬檢查，SQLite 連線本身仍需要外部序列化）。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common.run_context import adhoc_run_id

logger = logging.getLogger(__name__)

_VALID_ISSUE_TAGS = frozenset(
    {"hallucination", "format_error", "logic_flaw", "refused", "truncated"}
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_traces (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id          TEXT NOT NULL UNIQUE,
  run_id            TEXT NOT NULL,
  ts                TEXT NOT NULL,
  vendor            TEXT NOT NULL,
  model             TEXT NOT NULL,
  caller            TEXT NOT NULL,
  task_id           TEXT,
  attempt           INTEGER NOT NULL DEFAULT 0,
  target_file       TEXT,
  class_name        TEXT,
  function_name     TEXT,
  template_version  TEXT,
  input_tokens      INTEGER,
  output_tokens     INTEGER,
  cache_creation_input_tokens INTEGER,
  cache_read_input_tokens     INTEGER,
  latency_ms        INTEGER,
  status            TEXT NOT NULL,
  http_code         INTEGER,
  error_msg         TEXT,
  prompt                TEXT,
  response               TEXT,
  prompt_payload_key    TEXT,
  response_payload_key  TEXT,
  issue_tag         TEXT
    CHECK (issue_tag IS NULL OR issue_tag IN
      ('hallucination', 'format_error', 'logic_flaw', 'refused', 'truncated')),
  note              TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS llm_fts USING fts5(
  prompt, response, content='llm_traces', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS llm_traces_ai AFTER INSERT ON llm_traces BEGIN
  INSERT INTO llm_fts(rowid, prompt, response) VALUES (new.id, new.prompt, new.response);
END;

CREATE TRIGGER IF NOT EXISTS llm_traces_ad AFTER DELETE ON llm_traces BEGIN
  INSERT INTO llm_fts(llm_fts, rowid, prompt, response) VALUES ('delete', old.id, old.prompt, old.response);
END;

CREATE TRIGGER IF NOT EXISTS llm_traces_au AFTER UPDATE ON llm_traces BEGIN
  INSERT INTO llm_fts(llm_fts, rowid, prompt, response) VALUES ('delete', old.id, old.prompt, old.response);
  INSERT INTO llm_fts(rowid, prompt, response) VALUES (new.id, new.prompt, new.response);
END;

CREATE INDEX IF NOT EXISTS idx_llm_traces_task_id ON llm_traces(task_id);
CREATE INDEX IF NOT EXISTS idx_llm_traces_function ON llm_traces(target_file, class_name, function_name);
CREATE INDEX IF NOT EXISTS idx_llm_traces_run_id ON llm_traces(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_traces_ts ON llm_traces(ts);
"""

_connection: sqlite3.Connection | None = None
_connection_lock = threading.Lock()
# 保護所有對 _connection 的存取（讀＋寫），不是只包 INSERT——SQLite 連線
# 即使 check_same_thread=False 也不是多執行緒同時用安全，需要外部序列化。
_lock = threading.Lock()


@dataclass
class TraceRecord:
    """七章 schema 欄位對應的結構化物件。"""

    id: int
    trace_id: str
    run_id: str
    ts: str
    vendor: str
    model: str
    caller: str
    task_id: str | None
    attempt: int
    target_file: str | None
    class_name: str | None
    function_name: str | None
    template_version: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    latency_ms: int | None
    status: str
    http_code: int | None
    error_msg: str | None
    prompt: str | None
    response: str | None
    prompt_payload_key: str | None
    response_payload_key: str | None
    issue_tag: str | None
    note: str | None


def _get_connection() -> sqlite3.Connection:
    global _connection
    if _connection is None:
        with _connection_lock:
            if _connection is None:  # 鎖內再檢查一次，避免重複初始化
                db_path = os.environ.get("LLM_TRACE_DB_PATH", "logs/llm_traces.db")
                os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
                conn = sqlite3.connect(db_path, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(_SCHEMA_SQL)
                conn.commit()
                _connection = conn
    return _connection


def _row_to_record(row: sqlite3.Row) -> TraceRecord:
    return TraceRecord(**{key: row[key] for key in row.keys()})


def _resolve_since(since: str) -> str:
    """把 '24h'／'7d' 這類簡單時間長度字串轉成 ISO8601 UTC 時間戳下限。"""
    match = re.fullmatch(r"(\d+)([hd])", since.strip())
    if not match:
        raise ValueError(f"since 格式必須是數字加 h 或 d，例如 '24h'／'7d'，收到：{since!r}")
    amount, unit = int(match.group(1)), match.group(2)
    delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
    cutoff = datetime.now(timezone.utc) - delta
    return cutoff.isoformat()


def _write_payload_if_needed(
    run_id: str, trace_id: str, prompt: str | None, response: str | None
) -> tuple[str | None, str | None, str | None, str | None]:
    """七章「門檻判斷」：prompt／response 各自獨立跟門檻比較（UTF-8 byte
    長度）。回傳 (prompt_db, response_db, prompt_payload_key, response_payload_key)。
    兩者都超過門檻時寫進同一個檔案（兩個 Markdown header），只有一個超過
    時只寫那個欄位的內容。
    """
    threshold = int(os.environ.get("LLM_TRACE_PAYLOAD_THRESHOLD_BYTES", "100000"))
    prompt_needs_file = prompt is not None and len(prompt.encode("utf-8")) >= threshold
    response_needs_file = response is not None and len(response.encode("utf-8")) >= threshold

    if not prompt_needs_file and not response_needs_file:
        return prompt, response, None, None

    payload_dir = os.environ.get("LLM_TRACE_PAYLOAD_DIR", "logs/payloads")
    run_dir = Path(payload_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    file_path = run_dir / f"{trace_id}.md"
    relative_key = f"{run_id}/{trace_id}.md"

    sections = []
    if prompt_needs_file:
        sections.append(f"## Prompt\n\n{prompt}\n")
    if response_needs_file:
        sections.append(f"## Response\n\n{response}\n")
    # 先寫檔案，再讓呼叫端 INSERT SQLite——避免 dangling pointer（見七章
    # 「寫入規則」）。
    file_path.write_text("\n".join(sections), encoding="utf-8")

    prompt_db = None if prompt_needs_file else prompt
    response_db = None if response_needs_file else response
    prompt_key = relative_key if prompt_needs_file else None
    response_key = relative_key if response_needs_file else None
    return prompt_db, response_db, prompt_key, response_key


def read_payload_section(payload_key: str, section: str) -> str | None:
    """從落檔的 payload 讀回指定段落（'Prompt' 或 'Response'）的內容，
    供 `llmlog show` 需要看超過門檻的完整內容時使用。找不到對應 header
    時回傳 None（理論上不該發生，門檻判斷保證有寫的欄位一定有對應 key）。
    """
    payload_dir = os.environ.get("LLM_TRACE_PAYLOAD_DIR", "logs/payloads")
    file_path = Path(payload_dir) / payload_key
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        return None
    marker = f"## {section}\n\n"
    start = content.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = content.find("\n## ", start)
    return content[start:end].rstrip("\n") if end != -1 else content[start:].rstrip("\n")


def record_llm_call_start(
    *,
    trace_id: str,
    run_id: str | None,
    vendor: str,
    model: str,
    caller: str,
    attempt: int = 0,
    task_id: str | None = None,
    target_file: str | None = None,
    class_name: str | None = None,
    function_name: str | None = None,
    template_version: str | None = None,
    prompt: str | None,
) -> None:
    """呼叫實際發出**之前**立刻寫入一筆 `status="running"` 的 row，只含
    `prompt`（`response`／`latency_ms`／token 用量等結束才會知道的欄位
    留 `NULL`）。對應 11a_logging_architecture.md 七章「呼叫開始即寫入
    running row」：呼叫還沒結束（甚至逾時前、長時間卡住時）就能查到
    「這次到底送了什麼」，不必等 `record_llm_call()` 在 `finally` 補上
    最終結果才看得到——這是真實環境「長時間等待本地 Ollama、事後查不出
    當下送了什麼」這個缺口（見 09b_bug_trace.md）的直接對策。

    `record_llm_call()` 之後用**同一個 `trace_id`** UPSERT 補齊
    `response`／`status`／`latency_ms` 等欄位，同一筆 row 從 `running`
    轉成 `ok`／`error`／`timeout`，不是兩筆獨立紀錄；`ts` 因此代表這次
    呼叫的**起始時間**，不會被結束時的寫入覆蓋（見 `record_llm_call()`
    的 `ON CONFLICT` 子句刻意不更新 `ts`）。

    寫入失敗只記警告、不拋例外，理由同 `record_llm_call()`。呼叫端若
    因為某種原因跳過這次呼叫（例如程式碼路徑改動、忘記呼叫），
    `record_llm_call()` 之後的 `INSERT ... ON CONFLICT` 在找不到既有
    row 時會直接退化成一般 INSERT，不影響原本的記錄行為。
    """
    resolved_run_id = run_id or adhoc_run_id()
    try:
        conn = _get_connection()
        prompt_db, _response_db, prompt_key, _response_key = _write_payload_if_needed(
            resolved_run_id, trace_id, prompt, None
        )
        ts = datetime.now(timezone.utc).isoformat()
        with _lock:
            conn.execute(
                """
                INSERT INTO llm_traces (
                    trace_id, run_id, ts, vendor, model, caller, task_id, attempt,
                    target_file, class_name, function_name, template_version,
                    status, prompt, prompt_payload_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id, resolved_run_id, ts, vendor, model, caller, task_id, attempt,
                    target_file, class_name, function_name, template_version,
                    "running", prompt_db, prompt_key,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 稽核用旁路紀錄，刻意不讓例外往外傳
        logger.warning("記錄 LLM trace（起始）失敗（不影響主流程）：%s", exc)


def record_llm_call(
    *,
    trace_id: str,
    run_id: str | None,
    vendor: str,
    model: str,
    caller: str,
    attempt: int = 0,
    task_id: str | None = None,
    target_file: str | None = None,
    class_name: str | None = None,
    function_name: str | None = None,
    template_version: str | None = None,
    prompt: str | None,
    response: str | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    latency_ms: int,
    status: str,
    http_code: int | None = None,
    error_msg: str | None = None,
) -> None:
    """呼叫結束（`finally`）唯一寫入入口。`run_id` 為 None 時用
    `adhoc_run_id()`（六章防呆 fallback，正常呼叫鏈裡
    `call_claude_for_json()`／`fill_function()` 已經在最外層解析過，
    這裡不應該收到 None，這是最後一層防線）。

    用 `INSERT ... ON CONFLICT(trace_id) DO UPDATE` upsert：若
    `record_llm_call_start()` 已經寫過同一個 `trace_id`（正常路徑必然
    如此），這裡改成 UPDATE 補齊 `response`／`status`／`latency_ms` 等
    欄位，`ts` 維持原本 `record_llm_call_start()` 寫入的起始時間；若找
    不到既有 row（例如呼叫端跳過了 `record_llm_call_start()`），退化
    成一般 INSERT，行為等同這個函式改版前的既有邏輯。UPSERT 走 UPDATE
    分支時觸發的是 `llm_traces_au` trigger（不是 `llm_traces_ai`），FTS
    索引一樣會正確同步（見七章「外部 content 表需要 trigger 才會同步」）。

    寫入失敗（磁碟問題等）只記警告、不拋例外——這是稽核用的旁路紀錄，
    不該讓記錄本身的問題打斷真正在做的 LLM 呼叫流程（見七章「寫入規則」）。
    """
    resolved_run_id = run_id or adhoc_run_id()
    try:
        conn = _get_connection()
        prompt_db, response_db, prompt_key, response_key = _write_payload_if_needed(
            resolved_run_id, trace_id, prompt, response
        )
        ts = datetime.now(timezone.utc).isoformat()
        with _lock:
            conn.execute(
                """
                INSERT INTO llm_traces (
                    trace_id, run_id, ts, vendor, model, caller, task_id, attempt,
                    target_file, class_name, function_name, template_version,
                    input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens,
                    latency_ms, status, http_code, error_msg,
                    prompt, response, prompt_payload_key, response_payload_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    vendor = excluded.vendor,
                    model = excluded.model,
                    caller = excluded.caller,
                    task_id = excluded.task_id,
                    attempt = excluded.attempt,
                    target_file = excluded.target_file,
                    class_name = excluded.class_name,
                    function_name = excluded.function_name,
                    template_version = excluded.template_version,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    cache_creation_input_tokens = excluded.cache_creation_input_tokens,
                    cache_read_input_tokens = excluded.cache_read_input_tokens,
                    latency_ms = excluded.latency_ms,
                    status = excluded.status,
                    http_code = excluded.http_code,
                    error_msg = excluded.error_msg,
                    prompt = excluded.prompt,
                    response = excluded.response,
                    prompt_payload_key = excluded.prompt_payload_key,
                    response_payload_key = excluded.response_payload_key
                """,
                (
                    trace_id, resolved_run_id, ts, vendor, model, caller, task_id, attempt,
                    target_file, class_name, function_name, template_version,
                    input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens,
                    latency_ms, status, http_code, error_msg,
                    prompt_db, response_db, prompt_key, response_key,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 稽核用旁路紀錄，刻意不讓例外往外傳
        logger.warning("記錄 LLM trace 失敗（不影響主流程）：%s", exc)


def flag_trace(trace_id: str, issue_tag: str, note: str | None = None) -> None:
    """對應 `llmlog flag`。`issue_tag` 必須落在固定列舉內——資料庫層有
    `CHECK` 約束，這裡也檢查一次，給出比原生 SQL 錯誤更好讀的訊息。
    """
    if issue_tag not in _VALID_ISSUE_TAGS:
        raise ValueError(
            f"issue_tag 必須是 {sorted(_VALID_ISSUE_TAGS)} 其中之一，收到：{issue_tag!r}"
        )
    conn = _get_connection()
    with _lock:
        cursor = conn.execute(
            "UPDATE llm_traces SET issue_tag = ?, note = ? WHERE trace_id = ?",
            (issue_tag, note, trace_id),
        )
        conn.commit()
    if cursor.rowcount == 0:
        raise ValueError(f"trace_id 不存在：{trace_id!r}")


def get_trace(trace_id: str) -> TraceRecord | None:
    conn = _get_connection()
    with _lock:
        row = conn.execute(
            "SELECT * FROM llm_traces WHERE trace_id = ?", (trace_id,)
        ).fetchone()
    return _row_to_record(row) if row else None


def get_traces_for_task(task_id: str, run_id: str | None = None) -> list[TraceRecord]:
    """`run_id` 省略時查全部 run——跟 `llmlog` CLI「省略即最新 run」是
    不同語意，這裡是給程式呼叫端（含 Debug Agent）的通用函式。
    """
    conn = _get_connection()
    if run_id is None:
        query, params = "SELECT * FROM llm_traces WHERE task_id = ? ORDER BY id", (task_id,)
    else:
        query = "SELECT * FROM llm_traces WHERE task_id = ? AND run_id = ? ORDER BY id"
        params = (task_id, run_id)
    with _lock:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_record(r) for r in rows]


def get_traces_for_function(
    target_file: str, class_name: str | None, function_name: str
) -> list[TraceRecord]:
    """天生跨 run（六章「函式身分」設計的用途），不接受 run_id 參數。"""
    conn = _get_connection()
    if class_name is None:
        query = (
            "SELECT * FROM llm_traces WHERE target_file = ? AND class_name IS NULL "
            "AND function_name = ? ORDER BY id"
        )
        params = (target_file, function_name)
    else:
        query = (
            "SELECT * FROM llm_traces WHERE target_file = ? AND class_name = ? "
            "AND function_name = ? ORDER BY id"
        )
        params = (target_file, class_name, function_name)
    with _lock:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_record(r) for r in rows]


def list_recent(
    status: str | None = None, since: str | None = None, run_id: str | None = None
) -> list[TraceRecord]:
    """「省略時查什麼」由呼叫端（`llmlog`）自己決定並傳入，這個函式不
    代為決定預設值。
    """
    conditions: list[str] = []
    params: list = []
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if run_id is not None:
        conditions.append("run_id = ?")
        params.append(run_id)
    if since is not None:
        conditions.append("ts >= ?")
        params.append(_resolve_since(since))
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT * FROM llm_traces {where} ORDER BY id DESC"
    conn = _get_connection()
    with _lock:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_record(r) for r in rows]


def _record_matches_filters(
    record: TraceRecord, tag: str | None, vendor: str | None, since: str | None
) -> bool:
    if tag is not None and record.issue_tag != tag:
        return False
    if vendor is not None and record.vendor != vendor:
        return False
    if since is not None and record.ts < _resolve_since(since):
        return False
    return True


def _search_payload_files(payload_dir: str, query: str) -> set[str]:
    """回傳 payload 檔案內容含 query 的 trace_id 集合。有 `rg` 就用 `rg`
    加速，沒有則退回純 Python 逐檔掃描（見十一章「rg 依賴」）。
    """
    if not os.path.isdir(payload_dir):
        return set()

    rg_path = shutil.which("rg")
    matched_paths: list[str] = []
    if rg_path:
        result = subprocess.run(
            [rg_path, "-l", "-F", query, payload_dir],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            matched_paths = result.stdout.splitlines()
    else:
        for root, _dirs, files in os.walk(payload_dir):
            for name in files:
                if not name.endswith(".md"):
                    continue
                path = os.path.join(root, name)
                try:
                    content = Path(path).read_text(encoding="utf-8")
                except OSError:
                    continue
                if query in content:
                    matched_paths.append(path)

    return {Path(p).stem for p in matched_paths}


def search_traces(
    query: str, tag: str | None = None, vendor: str | None = None, since: str | None = None
) -> list[TraceRecord]:
    """對應 `llmlog search`。內部合併兩個來源：FTS5（覆蓋 < 門檻的
    prompt／response）＋ `payloads/` 目錄逐檔掃描（覆蓋 ≥ 門檻、落檔的
    內容）。合併邏輯在這裡，不是 CLI 層。
    """
    conn = _get_connection()
    fts_sql = (
        "SELECT llm_traces.* FROM llm_traces "
        "JOIN llm_fts ON llm_fts.rowid = llm_traces.id "
        "WHERE llm_fts MATCH ?"
    )
    params: list = [query]
    if tag is not None:
        fts_sql += " AND llm_traces.issue_tag = ?"
        params.append(tag)
    if vendor is not None:
        fts_sql += " AND llm_traces.vendor = ?"
        params.append(vendor)
    if since is not None:
        fts_sql += " AND llm_traces.ts >= ?"
        params.append(_resolve_since(since))
    fts_sql += " ORDER BY llm_traces.id DESC"

    with _lock:
        fts_rows = conn.execute(fts_sql, params).fetchall()
    matched_ids = {row["id"] for row in fts_rows}
    records = [_row_to_record(r) for r in fts_rows]

    payload_dir = os.environ.get("LLM_TRACE_PAYLOAD_DIR", "logs/payloads")
    file_trace_ids = _search_payload_files(payload_dir, query)
    if file_trace_ids:
        placeholders = ",".join("?" for _ in file_trace_ids)
        with _lock:
            extra_rows = conn.execute(
                f"SELECT * FROM llm_traces WHERE trace_id IN ({placeholders})",
                list(file_trace_ids),
            ).fetchall()
        for row in extra_rows:
            if row["id"] in matched_ids:
                continue
            record = _row_to_record(row)
            if _record_matches_filters(record, tag, vendor, since):
                records.append(record)
                matched_ids.add(row["id"])

    return records


def latest_run_id() -> str | None:
    """`llmlog` 省略 `--run` 時的「當前 run」語意（十一章）。表是空的
    （尚未有任何呼叫發生）時回傳 None。
    """
    conn = _get_connection()
    with _lock:
        row = conn.execute("SELECT run_id FROM llm_traces ORDER BY id DESC LIMIT 1").fetchone()
    return row["run_id"] if row else None


def gc_orphan_payloads(grace_period_hours: float = 24.0) -> int:
    """對應 `llmlog gc`（只能手動觸發，不掛進 `main.py` 啟動流程）。掃
    `payloads/` 比對 DB 的 payload key，只刪 mtime 早於寬限期之前的孤兒
    檔（見七章「`llmlog gc` 與寫入順序的交互作用」，這是硬性約束，避免
    掃到「payload 檔案已寫完、INSERT 還沒完成」的中間態）。回傳實際刪除
    的檔案數。
    """
    payload_dir = os.environ.get("LLM_TRACE_PAYLOAD_DIR", "logs/payloads")
    if not os.path.isdir(payload_dir):
        return 0

    conn = _get_connection()
    with _lock:
        rows = conn.execute(
            "SELECT prompt_payload_key, response_payload_key FROM llm_traces "
            "WHERE prompt_payload_key IS NOT NULL OR response_payload_key IS NOT NULL"
        ).fetchall()
    referenced_keys: set[str] = set()
    for row in rows:
        if row["prompt_payload_key"]:
            referenced_keys.add(row["prompt_payload_key"])
        if row["response_payload_key"]:
            referenced_keys.add(row["response_payload_key"])

    cutoff = time.time() - grace_period_hours * 3600
    deleted = 0
    for root, _dirs, files in os.walk(payload_dir):
        for name in files:
            if not name.endswith(".md"):
                continue
            full_path = os.path.join(root, name)
            relative_key = os.path.relpath(full_path, payload_dir).replace(os.sep, "/")
            if relative_key in referenced_keys:
                continue
            try:
                mtime = os.path.getmtime(full_path)
            except OSError:
                continue
            if mtime >= cutoff:
                continue  # 寬限期內，可能是寫完檔案、INSERT 還沒完成的中間態
            try:
                os.remove(full_path)
                deleted += 1
            except OSError as exc:
                logger.warning("刪除孤兒 payload 檔案失敗：%s（%s）", full_path, exc)
    return deleted
```

---

## 五、`common/llm_client.py`（改寫）——Claude API 呼叫整合 `record_llm_call_start()`／`record_llm_call()`

對應 11a 八章「Claude API 呼叫整合點」。取代原本的 `log_usage()`（`common/llm_usage_logger.py`，本次一併刪除）：那個簽名結構上就拿不到 `system_prompt`／`user_prompt`，改成呼叫發出前先呼叫 `record_llm_call_start()` 寫入 `status="running"` 的 row，`finally` 再呼叫 `record_llm_call()` 用同一個 `trace_id` upsert 補齊最終結果——兩段共用同一份組好的 `prompt_text`，不重新組字串。`status="ok"` 只在 `json.loads()` 成功後才設，`anthropic.APITimeoutError` 排在 `anthropic.APIError` 之前（子類別要放前面），`current_trace_id.reset(token)` 排在 `record_llm_call()` 之前（見八章「為什麼 reset() 要排在 record_llm_call() 之前」）。

```python
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
```

---

## 六、`translator_cli/ollama_client.py`（改寫）——Ollama 呼叫整合 `record_llm_call_start()`／`record_llm_call()`

對應 11a 九章「本地 Ollama 呼叫整合點」。`get_function_body()` 新增 `task_id`／`target_file`／`class_name`／`run_id` 參數；每個格式修正 attempt（不論成功、格式錯誤、還是傳輸層例外）在呼叫發出前先呼叫 `record_llm_call_start()` 寫入 `status="running"` 的 row，`finally` 再呼叫 `record_llm_call()` 用同一個 `trace_id` upsert 補齊最終結果——本地模型單次生成可能耗時數十秒到數分鐘，這段等待期間 prompt 已經可以被 `llmlog` 查到（見 11a 七章「呼叫開始即寫入 running row」，這是對 09b_bug_trace.md 記錄的「長時間等待 Ollama、查不出當時送了什麼」這個真實問題的直接對策）。原本 `_call_ollama_once()` 的傳輸層重試邏輯、`UPSTREAM_DEGRADED_THRESHOLD` 判斷不變，只額外把最終 `raise TranslatorCliNetworkError`／`TranslatorCliUpstreamDegradedError` 補上 `from last_error`，讓 `get_function_body()` 能靠 `exc.__cause__` 正確判斷 `status` 該記 `"timeout"` 還是 `"error"`（原本沒有這個 `from` 子句時，`__cause__` 會是 `None`，判斷永遠落到 `"error"`）。

```python
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
from translator_cli.prompts import BODY_END, BODY_START, SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

OLLAMA_MODEL = "qwen2.5-coder:32b"

TRANSLATOR_CLI_TIMEOUT_SECONDS = float(os.environ.get("TRANSLATOR_CLI_TIMEOUT_SECONDS", "300"))
TRANSLATOR_CLI_NETWORK_RETRIES = int(os.environ.get("TRANSLATOR_CLI_NETWORK_RETRIES", "2"))
_NETWORK_RETRY_DELAY_SECONDS = 3.0
_FORMAT_RETRY_COUNT = 2

UPSTREAM_DEGRADED_THRESHOLD = int(
    os.environ.get("TRANSLATOR_CLI_UPSTREAM_DEGRADED_THRESHOLD", "3")
)
_consecutive_transport_failures = 0

_DELIMITER_RE = re.compile(re.escape(BODY_START) + r"\n?(.*?)\n?" + re.escape(BODY_END), re.DOTALL)


def _extract_delimited_body(raw_text: str) -> str:
    match = _DELIMITER_RE.search(raw_text)
    if not match:
        raise TranslatorCliModelOutputError(
            f"delimiter 契約違反：模型回應找不到 {BODY_START} / {BODY_END} 標記"
        )
    return match.group(1)


async def _call_ollama_once(system_prompt: str, user_prompt: str) -> str:
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
                _consecutive_transport_failures = 0
                raise TranslatorCliNetworkError(
                    f"ollama 回應 HTTP 錯誤狀態碼（非傳輸層錯誤，不重試）：{exc}"
                ) from exc
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt < TRANSLATOR_CLI_NETWORK_RETRIES:
                    logger.warning(
                        "ollama 連線失敗（第 %d/%d 次），%.0f 秒後重試：%s",
                        attempt + 1, TRANSLATOR_CLI_NETWORK_RETRIES,
                        _NETWORK_RETRY_DELAY_SECONDS, exc,
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


async def get_function_body(
    *,
    current_signature: str,
    description: str,
    context: str,
    context_files: list[tuple[str, str]],
    function_name: str,
    task_id: str | None = None,
    target_file: str | None = None,
    class_name: str | None = None,
    run_id: str,
) -> str:
    """對外唯一入口，對應 07a 七章「模型輸出格式錯誤的修正重試」。

    `run_id` 是必填參數：`fill_function()` 已經在最外層解析成具體值
    （見 11a 九章「run_id 的解析只在 fill_function() 做一次」），這裡不
    再自己判斷，避免同一個 task 的三次格式修正 attempt 拿到不同 run_id。
    `task_id`／`target_file`／`class_name` 是純記錄用的選填參數。

    **每個 attempt（不論成功、格式錯誤、還是傳輸層例外）恰好產生一筆
    trace row，寫入時機統一在該次 attempt 的 `finally` 區塊**（見 11a
    九章），不是只記最後一次。
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

        trace_id = str(uuid4())
        token = current_trace_id.set(trace_id)
        start = time.monotonic()
        status, error_msg, raw_text = "error", None, None
        prompt_text = f"=== SYSTEM ===\n{SYSTEM_PROMPT}\n\n=== USER ===\n{user_prompt}"

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
            raw_text = await _call_ollama_once(SYSTEM_PROMPT, user_prompt)
            body_source = _extract_delimited_body(raw_text)
            python_adapter.extract_body_statements(body_source, function_name)
            status = "ok"
            return body_source
        except (TranslatorCliNetworkError, TranslatorCliConfigError) as exc:
            error_msg = str(exc)
            status = "timeout" if isinstance(exc.__cause__, httpx.TimeoutException) else "error"
            raise
        except TranslatorCliModelOutputError as exc:
            error_msg = str(exc)
            last_error = exc
            error_feedback = str(exc)
            if attempt < _FORMAT_RETRY_COUNT:
                logger.warning("模型輸出格式錯誤（第 %d 次），重新呼叫一次修正：%s", attempt + 1, exc)
                continue
        except Exception as exc:
            status = "error"
            error_msg = repr(exc)
            raise
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
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
```

`_extract_delimited_body()`／`_call_ollama_once()` 其餘內容（HTTP 重試迴圈、`UPSTREAM_DEGRADED_THRESHOLD` 判斷、環境變數讀取）維持原樣，僅補上 `from last_error`；完整檔案見程式碼庫。

---

## 七、`translator_cli/client.py`——`fill_function()` 新增 `run_id` 參數

對應 11a 九章「run_id 的解析只在 fill_function() 做一次」：這是 Ollama 呼叫鏈的最外層，`run_id` 只在這裡解析一次（`resolved_run_id = run_id or adhoc_run_id()`），往下傳給 `get_function_body()`，同一個 task 的多次格式修正 attempt 才會落在同一個 run_id 底下。

```python
async def fill_function(
    python_project_path: str,
    task_id: str,
    target_file: str,
    class_name: str | None,
    function_name: str,
    description: str,
    context: str,
    context_files: list[str],
    run_id: str | None = None,
) -> FillResult:
    resolved_run_id = run_id or adhoc_run_id()
    root = Path(python_project_path)
    ...
    body_source = await ollama_client.get_function_body(
        current_signature=current_signature,
        description=description,
        context=context,
        context_files=resolved_context_files,
        function_name=function_name,
        task_id=task_id,
        target_file=target_file,
        class_name=class_name,
        run_id=resolved_run_id,
    )
    ...
```

---

## 八、`graph/state.py`——新增 `run_id`

對應 11a 六章。放在 `RefactorState` 最前面（進入點輸入群組）：

```python
class RefactorState(TypedDict):
    # 進入點輸入（main.py 組裝 initial_state 時填入，見九）
    java_project_path: str
    # 這次 pipeline 執行的唯一識別碼，main.py 用 common/run_context.py::
    # new_run_id() 產生一次，貫穿一般 log 與 llm_traces.db 兩邊（見
    # 11a_logging_architecture.md 六章）。implement_node.py 透過
    # translator_cli.fill_function(run_id=...) 往下傳。
    run_id: str
    ...
```

---

## 九、`main.py`——接上 `new_run_id()`／`configure_logging()`

對應 11a 五、六章。取代原本的 `logging.basicConfig(...)`：`run_id` 必須在 `configure_logging()` 之前產生，才能被閉包進 `_ContextFilter`。

```python
import asyncio
import logging
import os
from dotenv import load_dotenv
from common.logging_setup import configure_logging
from common.run_context import new_run_id
from graph.builder import build_graph
from graph.nodes import implement_node
from graph.stream_watchdog import STUCK_REPORT_SECONDS, dump_self_stack, pump_graph_stream
from python_service.reload_probe import ensure_reload_probe_infra


load_dotenv()

logger = logging.getLogger(__name__)


async def main():
    graph = build_graph()

    # 一般 log 與 llm_traces.db 共用同一個 run_id（見
    # 11a_logging_architecture.md 六章）：必須在 configure_logging() 之前
    # 產生，才能把它閉包進 _ContextFilter，讓這條 pipeline 執行期間每一行
    # log 都能對回這次 run。
    run_id = new_run_id()
    configure_logging(run_id=run_id, file_handler=True)

    python_project_path = os.environ["PYTHON_PROJECT_PATH"]
    ...
    initial_state = {
        "run_id": run_id,
        "java_project_path": os.environ["JAVA_PROJECT_PATH"],
        ...
    }
```

---

## 十、`graph/nodes/implement_node.py`——`run_id` 貫穿 `_run_one_task()`

```python
async def _run_one_task(task: TaskSpec, python_project_path: str, run_id: str) -> FillResult:
    context, context_files = _augment_task_io(task)
    async with MODEL_SEMAPHORE:
        return await translator_cli.fill_function(
            python_project_path=python_project_path,
            task_id=task["id"],
            target_file=task["target_files"][0],
            class_name=task.get("class_name"),
            function_name=task["function_name"],
            description=task["description"],
            context=context,
            context_files=context_files,
            run_id=run_id,
        )
```

呼叫端一併改成傳入 `state["run_id"]`：

```python
        results = await asyncio.gather(
            *(_run_one_task(t, state["python_project_path"], state["run_id"]) for t in ready)
        )
```

---

## 十一、`llmlog/` CLI 套件（新增）

對應 11a 十一章「CLI 概覽（`llmlog`）」。7 個子指令**都是 `common/llm_trace.py` 對應函式的薄包裝**：只做參數解析與終端機輸出格式化（截斷、表格排版），不重新實作查詢邏輯——這樣才是真的跟 ⑦ Debug Agent 共用同一套機制。`main()` 呼叫 `configure_logging(run_id="cli", file_handler=False)`，理由見五章「為什麼 `llmlog` 不掛 file handler」。`recent`／`task` 省略 `--run` 時呼叫 `latest_run_id()` 決定查哪個 run，不重複一份邏輯。`recent --status running` 對應 11a 十章「情境六」：列出目前還沒結束的呼叫；`show` 對 `status="running"` 的 row 額外算出已耗時多久。

```python
# llmlog/__main__.py
import sys

from llmlog.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

```python
# llmlog/cli.py
"""`llmlog` CLI，對應 11a_logging_architecture.md 十一章。7 個子指令
（`recent`／`search`／`show`／`task`／`func`／`flag`／`gc`）**都是
`common/llm_trace.py` 對應函式的薄包裝**：只做參數解析與終端機輸出格式化
（截斷、表格排版），不重新實作查詢邏輯——這樣才是真的跟 ⑦ Debug Agent
（`debug_agent/analysis.py`，直接呼叫同一組函式）共用同一套機制，見 11a
七章「查詢與維護層設計」。

呼叫 `configure_logging(run_id="cli", file_handler=False)`——只掛 console
handler，不掛 `RotatingFileHandler`：這是獨立行程，若跟長時間執行的
Orchestrator 主行程搶 `logs/orchestrator.log` 的 rollover，Windows 上會
因為檔案被另一個行程開著而 `PermissionError`（見 11a 五章「`llmlog` 為
什麼不掛 file handler」）。`run_id="cli"` 只是這幾行操作自己在一般 log
裡的標籤，跟下面「省略 `--run` 時查最新 run」是完全不同的兩件事。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from common.llm_trace import (
    TraceRecord,
    flag_trace,
    gc_orphan_payloads,
    get_trace,
    get_traces_for_function,
    get_traces_for_task,
    list_recent,
    read_payload_section,
    search_traces,
)
from common.logging_setup import configure_logging

_VALID_ISSUE_TAGS = ("hallucination", "format_error", "logic_flaw", "refused", "truncated")
_SHOW_TRUNCATE_CHARS = 2000


def _resolve_run_id(explicit_run_id: str | None) -> str | None:
    """`--run` 省略時查最新 run（十一章「省略 `--run` 時的語意」），不是
    另外重複一份 `latest_run_id()` 邏輯。找不到任何資料時回傳 None，讓
    呼叫端印出明確訊息，不是靜默查出空結果。
    """
    if explicit_run_id is not None:
        return explicit_run_id
    from common.llm_trace import latest_run_id

    return latest_run_id()


def _fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    return dt.astimezone(timezone.utc).strftime("%m-%d %H:%M:%S")


def _target_label(record: TraceRecord) -> str:
    if not record.target_file:
        return "-"
    if record.class_name:
        return f"{record.target_file}::{record.class_name}.{record.function_name}"
    return f"{record.target_file}::{record.function_name}"


def _print_table(records: list[TraceRecord]) -> None:
    if not records:
        print("（沒有符合條件的紀錄）")
        return
    header = f"{'trace_id':<36} {'ts':<14} {'status':<7} {'vendor':<7} {'attempt':<7} {'task_id':<12} target"
    print(header)
    print("-" * len(header))
    for r in records:
        tag = f" [{r.issue_tag}]" if r.issue_tag else ""
        print(
            f"{r.trace_id:<36} {_fmt_ts(r.ts):<14} {r.status:<7} {r.vendor:<7} "
            f"{r.attempt:<7} {(r.task_id or '-'):<12} {_target_label(r)}{tag}"
        )


def _resolve_payload(record: TraceRecord, section: str) -> str | None:
    """`show` 依 `prompt_payload_key`／`response_payload_key` 判斷該讀
    SQLite 欄位還是讀落檔的段落（11a 七章「門檻式混合儲存」）。
    """
    if section == "Prompt":
        if record.prompt_payload_key:
            return read_payload_section(record.prompt_payload_key, "Prompt")
        return record.prompt
    if record.response_payload_key:
        return read_payload_section(record.response_payload_key, "Response")
    return record.response


def _print_section(title: str, content: str | None, *, full: bool) -> None:
    print(f"\n=== {title} ===")
    if content is None:
        print("（無內容）")
        return
    if full or len(content) <= _SHOW_TRUNCATE_CHARS:
        print(content)
    else:
        print(content[:_SHOW_TRUNCATE_CHARS])
        print(f"...（已截斷，共 {len(content)} 字元，加 --full 看全文）")


def _cmd_recent(args: argparse.Namespace) -> int:
    run_id = _resolve_run_id(args.run)
    if run_id is None:
        print("（llm_traces.db 目前沒有任何紀錄）")
        return 0
    records = list_recent(status=args.status, since=args.since, run_id=run_id)
    print(f"run_id={run_id}")
    _print_table(records)
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    records = search_traces(args.query, tag=args.tag, vendor=args.vendor, since=args.since)
    _print_table(records)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    record = get_trace(args.trace_id)
    if record is None:
        print(f"找不到 trace_id：{args.trace_id}", file=sys.stderr)
        return 1

    print(f"trace_id     {record.trace_id}")
    print(f"run_id       {record.run_id}")
    print(f"ts           {record.ts}")
    print(f"vendor/model {record.vendor}/{record.model}")
    print(f"caller       {record.caller}")
    print(f"task_id      {record.task_id or '-'}   attempt={record.attempt}")
    print(f"target       {_target_label(record)}")
    if record.status == "running":
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(record.ts)).total_seconds()
        print(f"status       running（呼叫尚未結束，目前已耗時約 {elapsed:.0f} 秒）")
    else:
        print(f"status       {record.status}" + (f"（http={record.http_code}）" if record.http_code else ""))
    if record.error_msg:
        print(f"error_msg    {record.error_msg}")
    if record.issue_tag:
        print(f"issue_tag    {record.issue_tag}" + (f"（{record.note}）" if record.note else ""))
    tokens = (
        f"in={record.input_tokens} out={record.output_tokens} "
        f"cache_creation={record.cache_creation_input_tokens} cache_read={record.cache_read_input_tokens}"
    )
    print(f"tokens       {tokens}   latency_ms={record.latency_ms}")

    sections = [("Prompt", "prompt")] if args.section == "prompt" else (
        [("Response", "response")] if args.section == "response" else [("Prompt", "prompt"), ("Response", "response")]
    )
    for title, key in sections:
        content = _resolve_payload(record, "Prompt" if key == "prompt" else "Response")
        _print_section(title, content, full=args.full)
    return 0


def _cmd_task(args: argparse.Namespace) -> int:
    run_id = _resolve_run_id(args.run)
    records = get_traces_for_task(args.task_id, run_id=run_id)
    if run_id is not None:
        print(f"run_id={run_id}")
    _print_table(records)
    return 0


def _cmd_func(args: argparse.Namespace) -> int:
    records = get_traces_for_function(args.file, args.class_name, args.function)
    _print_table(records)
    return 0


def _cmd_flag(args: argparse.Namespace) -> int:
    if args.tag not in _VALID_ISSUE_TAGS:
        print(f"--tag 必須是 {list(_VALID_ISSUE_TAGS)} 其中之一，收到：{args.tag!r}", file=sys.stderr)
        return 1
    try:
        flag_trace(args.trace_id, args.tag, note=args.note)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"已標記 {args.trace_id} → {args.tag}")
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    deleted = gc_orphan_payloads(grace_period_hours=args.grace_period_hours)
    print(f"已刪除 {deleted} 個孤兒 payload 檔案")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llmlog", description="查詢 llm_traces.db（見 11a 十一章）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("recent", help="最近的呼叫（預設查最新 run）")
    p.add_argument("--status", choices=("running", "ok", "error", "timeout"))
    p.add_argument("--since", help="例如 24h／7d")
    p.add_argument("--run", help="省略時查最新一次 run_id")
    p.set_defaults(func=_cmd_recent)

    p = sub.add_parser("search", help="全文搜尋 prompt／response")
    p.add_argument("query")
    p.add_argument("--tag", choices=_VALID_ISSUE_TAGS)
    p.add_argument("--vendor", choices=("claude", "ollama"))
    p.add_argument("--since", help="例如 24h／7d")
    p.set_defaults(func=_cmd_search)

    p = sub.add_parser("show", help="看單筆完整內容")
    p.add_argument("trace_id")
    p.add_argument("--section", choices=("prompt", "response"))
    p.add_argument("--full", action="store_true", help="不截斷，看全文")
    p.set_defaults(func=_cmd_show)

    p = sub.add_parser("task", help="查某個 task 的所有 attempt")
    p.add_argument("task_id")
    p.add_argument("--run", help="省略時查最新一次 run_id")
    p.set_defaults(func=_cmd_task)

    p = sub.add_parser("func", help="跨 run 查某支函式的所有歷史紀錄")
    p.add_argument("--file", required=True, dest="file")
    p.add_argument("--class-name", dest="class_name", default=None)
    p.add_argument("--function", required=True, dest="function")
    p.set_defaults(func=_cmd_func)

    p = sub.add_parser("flag", help="標記問題類型")
    p.add_argument("trace_id")
    p.add_argument("--tag", required=True, choices=_VALID_ISSUE_TAGS)
    p.add_argument("--note", default=None)
    p.set_defaults(func=_cmd_flag)

    p = sub.add_parser("gc", help="清理孤兒 payload 檔案（只刪寬限期外的檔案，手動觸發）")
    p.add_argument("--grace-period-hours", type=float, default=24.0, dest="grace_period_hours")
    p.set_defaults(func=_cmd_gc)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging(run_id="cli", file_handler=False)
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

呼叫方式：`python -m llmlog <子指令> ...`（例如 `python -m llmlog recent --status error --since 24h`）。

---

## 十二、既有測試同步更新

`common/llm_usage_logger.py` 退場後，`tests/common/test_llm_client.py` 裡對 `log_usage` 的 monkeypatch 全部改為 `record_llm_call`／`record_llm_call_start`（移到模組層級 autouse fixture，讓整份檔案的測試都不會意外寫真實 DB）；原本 `test_not_called_when_api_call_fails`（「失敗時不記錄」）的斷言方向是**錯的**——新設計裡 `record_llm_call()` 在 `finally` 呼叫，失敗時也要留下一筆 `status='error'` 的 trace，因此改寫成 `test_records_with_status_error_when_api_call_fails`，驗證失敗路徑確實記錄且 `error_msg` 有值。新增 `TestCallClaudeForJsonStartRecording`，驗證 `record_llm_call_start()` 在真正打 API **之前**就被呼叫、帶著完整 prompt，且跟 `finally` 那筆共用同一個 `trace_id`。

`tests/translator_cli/test_ollama_client.py` 的既有 `get_function_body()` 呼叫補上必填的 `run_id="test_run"`；autouse fixture 同時把 `record_llm_call`／`record_llm_call_start` monkeypatch 成 no-op，避免這份測試對真實 SQLite 檔案產生副作用。新增兩個測試驗證 start 呼叫順序（在 `_call_ollama_once()` 之前）與 trace_id 共用。

`tests/graph/test_implement_node.py` 裡會真正跑到 `_run_one_task()`（進 `asyncio.gather`）的測試案例，`state` 字典補上 `"run_id": "test_run"`。

`tests/translator_cli/test_client.py` 不需要改動——`fill_function()` 呼叫端沒有明確傳 `run_id` 時走 `adhoc_run_id()` fallback，`monkeypatch.setattr(ollama_client, "get_function_body", fake)` 用 `**kwargs` 接收，新增的 `task_id`／`target_file`／`class_name`／`run_id` 參數不影響既有測試。

`tests/common/test_llm_trace.py` 新增 `TestRecordLlmCallStart`（9 個測試），除了驗證 `record_llm_call_start()` 寫入 `running` row、`record_llm_call()` upsert 到同一筆 row、`ts` 不被覆蓋、FTS 對 start-only row 與 upsert 後的 row 都正確同步，還包含一個**用真實執行緒模擬「呼叫進行中」的端對端測試**（`test_live_visibility_during_a_slow_in_flight_call`）：背景執行緒呼叫 `record_llm_call_start()` 後 `sleep(0.3)` 才呼叫 `record_llm_call()`，主執行緒在這段期間輪詢 `get_trace()`，驗證確實觀察到 `status="running"` 且 `prompt` 完整可讀、`response` 仍是 `None`；執行緒結束後同一筆 row 轉成最終結果。這是本次設計要解決的核心場景的直接驗證。

---

## 十三、模組結構總覽與三項驗證

```
common/
  run_context.py       # new_run_id() / adhoc_run_id()
  trace_context.py     # current_trace_id ContextVar
  logging_setup.py     # configure_logging()
  llm_trace.py          # llm_traces.db 讀寫唯一權威來源（record_llm_call_start + record_llm_call upsert）
  llm_client.py          # call_claude_for_json()（呼叫前 start，finally upsert）
llmlog/
  __main__.py
  cli.py                # 7 個子指令，recent/show 支援 status=running
translator_cli/
  ollama_client.py      # get_function_body()（每個 attempt：呼叫前 start，finally upsert）
  client.py              # fill_function() 新增 run_id 參數
graph/
  state.py               # RefactorState.run_id
  nodes/implement_node.py  # run_id 貫穿 _run_one_task()
main.py                  # new_run_id() + configure_logging()
```

`python -m pytest -q`（排除既有、與本次無關、缺 `JAVA_BASE_URL` 才能收集的 `refactor_harness/langgraph_nodes/test_nodes.py`）546 個全數通過。

**三項驗證**（對應使用者要求，逐項核對）：

1. **全局 log 機制**：`main.py` 產生 `run_id` 後呼叫 `configure_logging(run_id=run_id, file_handler=True)`，取代原本的 `logging.basicConfig(...)`；`_ContextFilter` 把 `run_id`／`trace_id` 注入每一行 log（`tests/common/test_logging_setup.py` 驗證 handler 裝配、注入邏輯、`file_handler=False` 不掛 `RotatingFileHandler`、重複呼叫不重複裝 handler）。Windows console 編碼問題已用 `_reconfigure_console_encoding()` 處理，噪音第三方 logger（`httpx`／`httpcore`／`anthropic`／`urllib3`）已降到 WARNING。✅

2. **Claude API prompts 紀錄機制**：`common/llm_client.py::call_claude_for_json()` 呼叫發出前先呼叫 `record_llm_call_start()` 寫入 `status="running"` 的 row（prompt 立即可查），呼叫結束（不論成功、逾時、API 錯誤、JSON 解析失敗、還是其他未預期例外）都在 `finally` 呼叫 `record_llm_call()` 用同一個 `trace_id` upsert 補齊 `response`／最終 `status`；`status` 只在 `json.loads()` 成功後才標記 `"ok"`。`tests/common/test_llm_client.py::TestCallClaudeForJsonStartRecording`／`TestCallClaudeForJsonTraceRecording` 驗證 start 先於 API 呼叫、正常路徑與失敗路徑都有最終記錄、`caller` 抓到真正呼叫端。✅

3. **本地 Ollama prompts 紀錄機制（含延遲期間的即時可查）**：`translator_cli/ollama_client.py::get_function_body()` 每個格式修正 attempt（含觸發重試的失敗 attempt）在呼叫發出前先呼叫 `record_llm_call_start()`，`finally` 再 upsert 補齊最終結果，`attempt` 欄位標記第幾次；`translator_cli/client.py::fill_function()` 新增 `run_id` 參數並在最外層解析一次、往下傳，確保同一個 task 的所有 attempt 落在同一個 run_id。`tests/common/test_llm_trace.py::TestRecordLlmCallStart::test_live_visibility_during_a_slow_in_flight_call` 用真實執行緒證實：呼叫進行中（尚未取得模型回應）就能查到完整 prompt 與 `status="running"`；`tests/common/test_llm_trace.py` 其餘測試驗證 `get_traces_for_task()` 能依序列出同一 task 的多筆 attempt；`tests/translator_cli/test_ollama_client.py` 既有的格式重試測試在補上 `run_id` 後全數通過，確認新增的 start/upsert 記錄邏輯沒有破壞原本的重試控制流程。✅

**端對端 CLI 驗證**（模擬使用者實際場景：本地 Ollama 長時間生成）：用一支腳本呼叫 `record_llm_call_start()` 後 `sleep(1.5)` 才呼叫 `record_llm_call()`，在 sleep 期間實際執行 `python -m llmlog recent --status running` 與 `python -m llmlog show <trace_id>`——兩者都正確顯示 `status="running"`、完整 prompt、「呼叫尚未結束，目前已耗時約 2 秒」，`Response` 顯示「（無內容）」；`sleep` 結束後再次 `show` 顯示 `status="ok"`、完整 `response`、`latency_ms=3000`；`llmlog task task_smoke` 正確列出這筆記錄。另外用 seed 過兩筆 trace（一筆 Claude、一筆 Ollama）的真實 SQLite 檔案驗證其餘子指令：`recent`／`task`／`func` 正確列出記錄；`search "retry"` 命中 Ollama 那筆的 prompt 內容；`flag ... --tag format_error` 成功標記，`recent --status error` 的輸出正確顯示 `[format_error]`；`gc --grace-period-hours 0` 在沒有孤兒檔案時回報刪除 0 個。
