"""LLM 呼叫卡死觀察機制：每 60 秒查一次 llm_traces.db 有沒有 status="running"
超過門檻秒數（預設 300 秒）的呼叫，一旦偵測到，把當下能拿到的所有現場證據
——完整 prompt（含大小）、netstat 連線狀態、python.exe 行程列表——一次寫進
診斷檔，且只在「這個 trace_id 第一次跨過門檻」時寫一次（不重複灌爆檔案）。

背景：2026-08-28 一次真實 pipeline run 卡在單一 Claude API 呼叫超過 30
分鐘，砍掉行程之前沒有先擷取 netstat／prompt 現場，事後完全無法回頭判斷
真正卡住的原因——這支腳本就是為了不再重演這個疏漏而寫的，用法見
docs/09b_bug_trace.md 對應條目。
"""
from __future__ import annotations

import datetime
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DB_PATH = Path("logs/llm_traces.db")
DIAG_DIR = Path("logs/stall_diagnostics")
STALL_THRESHOLD_SECONDS = 300
POLL_INTERVAL_SECONDS = 60


def _current_running_call() -> tuple[str, str, float] | None:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT trace_id, ts FROM llm_traces WHERE status='running' "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    trace_id, ts = row
    started = datetime.datetime.fromisoformat(ts)
    now = datetime.datetime.now(datetime.timezone.utc)
    elapsed = (now - started).total_seconds()
    return trace_id, ts, elapsed


def _capture(trace_id: str, ts: str, elapsed: float) -> Path:
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DIAG_DIR / f"{trace_id}.txt"

    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT run_id, vendor, model, caller, task_id, target_file, "
            "class_name, function_name, prompt FROM llm_traces WHERE trace_id=?",
            (trace_id,),
        ).fetchone()
    finally:
        conn.close()

    lines: list[str] = []
    lines.append(f"=== stall detected {datetime.datetime.now().isoformat()} ===")
    lines.append(f"trace_id={trace_id} started={ts} elapsed_seconds={elapsed:.0f}")
    if row:
        run_id, vendor, model, caller, task_id, target_file, class_name, function_name, prompt = row
        lines.append(
            f"run_id={run_id} vendor={vendor} model={model} caller={caller} "
            f"task_id={task_id} target_file={target_file} class_name={class_name} "
            f"function_name={function_name}"
        )
        prompt = prompt or ""
        lines.append(f"prompt_size_chars={len(prompt)}")
        lines.append("--- full prompt ---")
        lines.append(prompt)
    else:
        lines.append("(trace_id not found in llm_traces.db — unexpected)")

    lines.append("\n--- python.exe processes (tasklist) ---")
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/V"],
            capture_output=True, text=True, timeout=15,
        )
        lines.append(r.stdout)
    except Exception as e:  # noqa: BLE001 — 診斷腳本本身不能因為這裡失敗而中止
        lines.append(f"(tasklist failed: {e!r})")

    lines.append("\n--- netstat -ano (all established/listening) ---")
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=15)
        lines.append(r.stdout)
    except Exception as e:  # noqa: BLE001
        lines.append(f"(netstat failed: {e!r})")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def main() -> None:
    already_captured: set[str] = {p.stem for p in DIAG_DIR.glob("*.txt")} if DIAG_DIR.exists() else set()
    while True:
        current = _current_running_call()
        if current is not None:
            trace_id, ts, elapsed = current
            if elapsed >= STALL_THRESHOLD_SECONDS and trace_id not in already_captured:
                out_path = _capture(trace_id, ts, elapsed)
                print(
                    f"STALL DETECTED: trace={trace_id} elapsed={elapsed:.0f}s "
                    f"diagnostics written to {out_path}",
                    flush=True,
                )
                already_captured.add(trace_id)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
