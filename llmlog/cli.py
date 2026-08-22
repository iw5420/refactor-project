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
