"""Pipeline 執行完成後的制式摘要報告，寫進
`logs/reports/{YYYY-MM-DD}/{run_id}.json`——依日期分資料夾，跟既有
`logs/report_{run_id}.json`（`refactor_harness/langgraph_nodes/
test_nodes.py::run_postman_tests()` 寫的，每輪 debug 迴圈都會覆寫、只
留得住最後一輪）是不同層級的東西：這裡是**整條 run 結束時**寫一次的
總結，`test_results`／`task_failures`／`debug_rounds` 這些欄位在 State
裡本來就有完整資料（`debug_rounds`／`task_failures` 是逐輪累積的歷史，
`operator.add` reducer），但目前沒有 checkpointer，process 一結束就
全部消失，`main.py` 原本的收尾 print 也沒有印出 `debug_rounds`——這個
檔案就是把這些已經存在、但只在記憶體裡活過一次的資料落地成一份人工
事後看得到的紀錄。

依日期分資料夾的「日期」直接從 `run_id` 的時間戳部分取得（見
`common/run_context.py::new_run_id()` 格式
`{YYYYMMDD_HHMMSS}_{uuid4前6碼}`），不是另外呼叫一次
`datetime.now()`——避免長時間執行、跨過午夜時，資料夾日期跟 run_id
本身記錄的開始時間對不上。
"""
from __future__ import annotations

import json
import os

from graph.state import RefactorState

REPORTS_ROOT = os.path.join("logs", "reports")


def _classify_outcome(state: RefactorState) -> str:
    """跟 `graph/nodes/give_up_node.py` 用的是同一套判斷順序（
    `scaffold_done is False` → `give_up_early` → 其餘），不是另外發明
    一套分類——這裡只是把同一個判斷結果轉成一個字串，方便寫進報告。
    """
    if state.get("test_results", {}).get("status") == "pass":
        return "done"
    if state.get("scaffold_done") is False:
        return "give_up_scaffold_failed"
    if state.get("give_up_early"):
        return "give_up_early"
    return "give_up_retry_exhausted"


def _date_folder_from_run_id(run_id: str) -> str:
    date_part = run_id.split("_", 1)[0]  # "YYYYMMDD"（adhoc_ 前綴的 run_id 不會走到這裡，見 write_run_report）
    return f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"


def _compute_summary(state: RefactorState) -> dict:
    """給人看的報告要回答的問題：翻譯了多少函式、多少個曾經出包、
    ⑦ 修正了多少、還剩多少沒解決——這些數字目前沒有任何一個欄位
    直接存在 State 裡，都是從既有欄位彙整出來的衍生資料，只給
    `write_human_readable_report()` 用，不動 `write_run_report()`
    原本的 JSON 形狀。

    「修好了沒」用 module 粒度判斷，不是 task 粒度：`debug_rounds` 的
    `origin`／`fixable` 本來就是逐 module 判斷（見
    docs/10a_debug_agent_architecture.md 三章），沒有一個可靠的訊號能
    回答「這個 task 的 fix_instruction 有沒有讓對應的測試案例通過」
    （related_files → task_id 本來就是反查，不是精確對應，見 10a 五
    章）——曾被 ⑦ 標記為 root_cause、但最後不在 failed_modules／
    blocked_modules 裡的 module，才是有把握說「修好了」的判斷。
    """
    task_list = state.get("task_list", [])
    completed_tasks = set(state.get("completed_tasks", []))
    task_failures = state.get("task_failures", [])
    debug_rounds = state.get("debug_rounds", [])

    tasks_with_translation_issues = sorted({f["task_id"] for f in task_failures})
    permanently_unfillable_tasks = sorted({
        f["task_id"] for f in task_failures if f["reason"] == "scaffold_skipped"
    })

    fix_attempts = [tf for r in debug_rounds for tf in r["task_fixes"]]
    modules_ever_flagged = sorted({r["module"] for r in debug_rounds if r["origin"] == "root_cause"})

    still_broken = set(state.get("failed_modules", [])) | set(state.get("blocked_modules", []))
    modules_fixed = sorted(set(modules_ever_flagged) - still_broken)
    modules_still_broken = sorted(set(modules_ever_flagged) & still_broken)

    return {
        "total_tasks": len(task_list),
        "translated_successfully": len(completed_tasks),
        "tasks_with_translation_issues": tasks_with_translation_issues,
        "permanently_unfillable_tasks": permanently_unfillable_tasks,
        "fix_attempts_issued": len(fix_attempts),
        "modules_ever_flagged": modules_ever_flagged,
        "modules_fixed": modules_fixed,
        "modules_still_broken": modules_still_broken,
        "test_summary": state.get("test_results", {}).get("summary") or {},
    }


_OUTCOME_LABELS = {
    "done": "成功完成",
    "give_up_scaffold_failed": "放棄（④ 骨架生成失敗）",
    "give_up_early": "⑦ 提早判斷放棄（非重試次數用盡）",
    "give_up_retry_exhausted": "放棄（重試次數用盡）",
}


def _render_human_readable_report(state: RefactorState, summary: dict) -> str:
    outcome = _classify_outcome(state)
    lines = [
        f"# Pipeline 執行報告 — {state['run_id']}",
        "",
        f"**結果**：{_OUTCOME_LABELS[outcome]}",
        f"**重試輪數**：{state.get('retry_count')}",
        "",
        "## 翻譯總覽",
        f"- 總共需要翻譯的函式數：{summary['total_tasks']}",
        f"- 成功產生程式碼：{summary['translated_successfully']}",
        f"- 曾經翻譯失敗（含後來補救成功的）：{len(summary['tasks_with_translation_issues'])}",
    ]
    if summary["permanently_unfillable_tasks"]:
        lines.append(
            f"- 永久無法生成（骨架缺口，需人工介入）：{len(summary['permanently_unfillable_tasks'])} 個"
            f" → {', '.join(summary['permanently_unfillable_tasks'])}"
        )
    else:
        lines.append("- 永久無法生成（骨架缺口）：0")

    ts = summary["test_summary"]
    lines += ["", "## API 測試總覽（最後一輪）"]
    if ts:
        lines.append(f"- 總測試案例：{ts.get('total')}")
        lines.append(f"- 通過：{ts.get('passed')}")
        lines.append(f"- 失敗：{ts.get('failed')}")
        lines.append(f"- 通過率：{ts.get('pass_rate')}")
    else:
        lines.append("- （無資料）")

    lines += ["", "## Debug Agent 診斷總覽"]
    flagged = summary["modules_ever_flagged"]
    lines.append(f"- 曾被標記需要修正的模組數：{len(flagged)}" + (f" → {', '.join(flagged)}" if flagged else ""))
    lines.append(f"- 開出的修正嘗試次數：{summary['fix_attempts_issued']}")
    fixed = summary["modules_fixed"]
    lines.append(f"- 最終確認修好：{len(fixed)}" + (f" → {', '.join(fixed)}" if fixed else ""))
    broken = summary["modules_still_broken"]
    if broken:
        lines.append(f"- **仍未解決：{len(broken)} → {', '.join(broken)}**")
    else:
        lines.append("- 仍未解決：0")

    if state.get("unanalyzed_root_cause_modules"):
        lines += [
            "",
            "## 注意",
            "以下模組最後一輪 Claude API 呼叫（含重試）仍然失敗，完全沒能取得分析結果，"
            f"可能是 API 本身的問題，不是 ⑦ 判斷錯誤，建議查 `llmlog`："
            f"{', '.join(state['unanalyzed_root_cause_modules'])}",
        ]

    return "\n".join(lines) + "\n"


def write_human_readable_report(state: RefactorState) -> str:
    """對應 `write_run_report()` 那份制式 JSON 的人類可讀版本——同一個
    date 資料夾、同一個檔名主體，副檔名換成 `.md`，供人直接打開看，不
    用自己解析 JSON 或去翻 debug_rounds 原始清單湊出「這一趟到底做了
    什麼」。兩份報告各自獨立寫入，互不影響彼此的格式。
    """
    run_id = state["run_id"]
    summary = _compute_summary(state)
    content = _render_human_readable_report(state, summary)

    date_folder = _date_folder_from_run_id(run_id)
    report_dir = os.path.join(REPORTS_ROOT, date_folder)
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, f"{run_id}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)
    return report_path


def write_run_report(state: RefactorState) -> str:
    """寫入摘要報告，回傳寫入的檔案路徑（供呼叫端印出來，讓人知道去
    哪裡看）。

    `test_summary` 只取 `test_results["summary"]`（total／passed／failed／
    pass_rate），不含完整 `failures[*].body_diff`——詳細的比對差異已經
    在 `logs/report_{run_id}.json` 裡，這份報告要回答的是「整條 run
    最後怎麼樣、⑦ 診斷過什麼、修法有沒有用」，不是重複一份完整的
    Harness 報告。
    """
    run_id = state["run_id"]
    report = {
        "run_id": run_id,
        "outcome": _classify_outcome(state),
        "retry_count": state.get("retry_count"),
        "test_summary": state.get("test_results", {}).get("summary"),
        "completed_tasks_count": len(state.get("completed_tasks", [])),
        "failed_tasks_count": len(state.get("failed_tasks", [])),
        "task_failures": state.get("task_failures", []),
        "debug_rounds": state.get("debug_rounds", []),
        "unanalyzed_root_cause_modules": state.get("unanalyzed_root_cause_modules", []),
    }

    date_folder = _date_folder_from_run_id(run_id)
    report_dir = os.path.join(REPORTS_ROOT, date_folder)
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, f"{run_id}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    return report_path
