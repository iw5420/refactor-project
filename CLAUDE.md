# CLAUDE.md

## LLM 呼叫紀錄查詢

排查 prompt 問題、幻覺、LLM API 失敗、pipeline 卡住時，用 `llmlog`，不要手動翻 ./logs。

```bash
python -m llmlog recent --status running        # pipeline 正在長時間等待時，查現在卡在哪個 prompt（顯示已耗時多久）
python -m llmlog recent --status error --since 24h   # 最近的失敗
python -m llmlog search "<關鍵字>" --tag <tag> --since 7d   # 全文搜尋
python -m llmlog show <trace_id> --section prompt --full   # 看完整內容，payload 較大時預設截斷
python -m llmlog task <task_id> --run <run_id>   # 查某個 task 的所有 attempt（省略 --run 查最新一次）
python -m llmlog func --file <path> --class-name <name> --function <name>   # 跨 run 查某支函式的歷史
python -m llmlog flag <trace_id> --tag <tag> --note "<原因>"
```

`--tag` 只能是：`hallucination` / `format_error` / `logic_flaw` / `refused` / `truncated`

- 先用 `recent` 或 `search` 縮小範圍，再對單筆用 `show`；不知道具體是哪次呼叫時用 `task`／`func`；懷疑卡住時先用 `recent --status running`。
- `llm_traces.db`（`common/llm_trace.py`）是 Claude API 與本地 Ollama 兩條呼叫路徑共用的唯一儲存——不需要知道這次呼叫是哪一邊做的，一律用 `llmlog` 查。
- 每次呼叫在**發出之前**就會寫入一筆 `status="running"` 的紀錄（只含 prompt），呼叫結束才補上 `response`／最終狀態——即使呼叫還在進行中、卡住很久，`llmlog show` 也查得到當時送了什麼。
- 完整設計見 `docs/11a_logging_architecture.md`；程式碼實作見 `docs/11b_logging_code.md`。
