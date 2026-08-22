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
