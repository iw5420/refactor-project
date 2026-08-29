"""⑦ Debug Agent 的輸入正規化：把 test_results／task_failures／
partial_reports／blocked_modules／blocked_reasons 四份格式、粒度都不同
的資料，收斂成逐 module 的 ModuleFailureContext，並判斷每個 module
值不值得呼叫 LLM（見 10a 三章）。純程式邏輯，不呼叫 Claude API。
"""
from __future__ import annotations

import logging
from typing import Literal, TypedDict

from graph.state import RefactorState, TaskFailure, TaskSpec

logger = logging.getLogger(__name__)


class ModuleFailureContext(TypedDict):
    module: str
    origin: Literal["blocked", "module_mismatch", "scaffold_gap", "root_cause"]
    harness_failures: list[dict]
    task_failures: list[TaskFailure]
    scaffold_gap_task_ids: set[str]
    special_reason: str | None
    batch_sibling_modules: list[str]


def _latest_reasons_by_module(partial_reports: list[dict], current_round: int) -> dict[str, str]:
    """partial_reports 是逐次累加的清單（見 01 三章），同一 module 可能
    有多筆。**只看「這一輪」（round == current_round）的紀錄，不看跨
    輪次的累積歷史**——10a 3.5「過期資料」：若不綁定輪次，一個 module
    這一輪完全沒被 ⑤ 碰過時，會沿用它好幾輪以前的舊 reason，把早已
    無關的 module 誤判成跟這一輪的崩潰同批。`current_round` 傳入
    `state["retry_count"]`（run_debug_analysis() 讀取時尚未被本輪遞增，
    恰好等於剛結束那次 implement() 呼叫看到的值，見 10a 3.5）。

    一次算出「這一輪」全部 module 的最新 reason，供 special_reason 與
    batch_sibling_modules 共用同一份計算，不重複掃描 partial_reports
    兩次。
    """
    latest: dict[str, str] = {}
    for r in partial_reports:
        if r.get("round") != current_round:
            continue
        report = r.get("report", {})
        if "reason" in report:
            latest[r["module"]] = report["reason"]
        elif r["module"] in latest:
            # 這個 module 這一輪後來又有一筆「正常」report（沒有 reason
            # 鍵，走完整 HarnessReporter.build_report() 的格式），代表
            # 這一輪真的跑過 Newman、不再是逾時/骨架缺口的特殊狀態，
            # 蓋掉舊值。
            del latest[r["module"]]
    return latest


def build_module_contexts(state: RefactorState) -> list[ModuleFailureContext]:
    """對應 10a 3.1～3.5：以 test_results.failures 的 module 欄位分組
    ——不是 state["failed_modules"]，後者只反映 ⑤ 最後一次局部驗證
    （readonly-only）的結果，test_results 才是 ⑥ 全量驗證（readonly＋
    mutation）算出的權威真相，兩者可能不一致（見 10a 3.1）。

    若 test_results.reason=="service_unreachable" 且
    state["failed_modules"] 也是空，回傳空清單——不另外分岔，交給
    analysis.py 的一般規則處理（root_cause_ctxs 為空 → give_up_early=True，
    見 10a 三章）。
    """
    test_results = state.get("test_results", {})

    if test_results.get("reason") == "service_unreachable":
        # 10a 3.2「service_unreachable 分支」：⑥ 這一輪連 Newman 都沒能
        # 執行（見 refactor_harness/langgraph_nodes/test_nodes.py 的前置
        # 健康檢查），test_results["failures"] 因此是空陣列——不是
        # 「跑過、沒有失敗」，是「根本沒跑」。退回 state["failed_modules"]
        # （⑤ 上一輪自己標記失敗的 module，多半正是造成這次連不上的元凶
        # 所在，見 09a 三章「批次執行」），harness_failures 留空，下面
        # 3.3 的機械分類規則完全不變，繼續套用在這份用不同方式湊出來的
        # modules_with_failures 上。
        modules_with_failures: dict[str, list[dict]] = {
            m: [] for m in state.get("failed_modules", [])
        }
    else:
        failures = test_results.get("failures", [])
        modules_with_failures = {}
        for f in failures:
            module = f.get("module")
            if module is None:
                # 理論上不應發生（見 02a 九章 module 欄位）；防禦性略過
                # 並記警告，這裡不中止整個分析。
                logger.warning("test_results.failures 有一筆缺少 module 欄位，忽略：%s", f.get("case_id"))
                continue
            modules_with_failures.setdefault(module, []).append(f)

    task_failures = state.get("task_failures", [])
    task_list = state.get("task_list", [])
    blocked_modules = set(state.get("blocked_modules", []))
    latest_reasons = _latest_reasons_by_module(state.get("partial_reports", []), state.get("retry_count", 0))
    timeout_modules = {m for m, reason in latest_reasons.items() if reason == "batch_reload_timeout"}

    scaffold_gap_task_ids_all = {
        f["task_id"] for f in task_failures if f["reason"] == "scaffold_skipped"
    }

    contexts: list[ModuleFailureContext] = []
    for module, module_failures in modules_with_failures.items():
        module_task_failures = [f for f in task_failures if f["module"] == module]
        module_task_ids = {t["id"] for t in task_list if t["module"] == module}
        module_scaffold_gap_ids = module_task_ids & scaffold_gap_task_ids_all

        # 判斷順序固定：blocked → module_mismatch → scaffold_gap →
        # root_cause（見 10a 3.3「判斷順序固定」）。module_mismatch 必須
        # 排在 scaffold_gap 之前：module_task_ids 為空集合時，下面
        # scaffold_gap 判斷式的 `module_task_ids and ...` 恆為假，若不
        # 先攔截會直接落到 root_cause，讓 LLM 面對一份空的 tasks 清單。
        if module in blocked_modules:
            origin: Literal["blocked", "module_mismatch", "scaffold_gap", "root_cause"] = "blocked"
        elif not module_task_ids:
            origin = "module_mismatch"
        elif module_task_ids <= scaffold_gap_task_ids_all:
            origin = "scaffold_gap"
        else:
            origin = "root_cause"

        special_reason = latest_reasons.get(module)
        batch_siblings = sorted(timeout_modules - {module}) if special_reason == "batch_reload_timeout" else []

        contexts.append(
            ModuleFailureContext(
                module=module,
                origin=origin,
                harness_failures=module_failures,
                task_failures=module_task_failures,
                scaffold_gap_task_ids=module_scaffold_gap_ids,
                special_reason=special_reason,
                batch_sibling_modules=batch_siblings,
            )
        )
    return contexts


def module_tasks(task_list: list[TaskSpec], module: str) -> list[TaskSpec]:
    """這個 module 全部 task 的清單（不只失敗的），供 prompt 組裝時反查
    task_id 用（見 10a 五章）。"""
    return [t for t in task_list if t["module"] == module]


def compute_zero_endpoint_modules(module_list: list[dict], route_module_mapping: dict[str, str]) -> set[str]:
    """對應 10a 四章「這條規則仍然不夠」（docs/09b_bug_trace.md #49）：
    找出「沒有任何 HTTP endpoint」的模組——這種模組的程式碼（如
    common_service.py）即使被其他模組的 task 當成 referenced_interfaces
    引用，test_results.failures[*].module 也永遠不會出現它，3.2 的模組
    篩選機制永遠不會選中它去分析，只能靠 4.1 疊加 target_files 時被
    「看到」。`route_module_mapping` 的值（③ 產生、唯一權威來源，見
    02a 十三章、`refactor_harness.core.route_mapper.RouteMapper.
    module_mapping`）就是「真的擁有 route 的模組」全集，`module_list`
    裡其餘的都是零 endpoint 模組。純資料轉換，不做檔案 I/O——呼叫端
    負責讀 `config/harness.yaml`，這裡才好測試。
    """
    modules_with_routes = set(route_module_mapping.values())
    return {m["module"] for m in module_list} - modules_with_routes
