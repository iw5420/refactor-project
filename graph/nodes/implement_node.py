"""
⑤ 功能改寫 Agent（translator-cli + scheduler）
依 task list 逐一呼叫 translator-cli 實作業務邏輯
見 01_langgraph_architecture.md 六、07a_translator_cli_architecture.md、07b_translator_cli_code.md

translator-cli 串接已完成（見 07b）；Harness 局部驗證（_partial_verify()
的 db／verifier）仍是 stub，非 07a/07b 範圍，見 02a/02b、09a（待建立）。
"""
import asyncio

from graph.scheduler import ModuleScheduler
from graph.state import RefactorState
from translator_cli import client as translator_cli
from translator_cli.types import FillResult

# 對應硬體限制：本地模型併發數＝1（見 00 三章）。排程層 get_ready_tasks()
# 可一次回傳多個就緒 task，但實際呼叫 translator_cli.fill_function()
# 永遠被這個 Semaphore 收斂成一個個跑（見 07a 八章「不需要鎖機制」）。
MODEL_SEMAPHORE = asyncio.Semaphore(1)


async def _run_one_task(task: dict, python_project_path: str) -> FillResult:
    async with MODEL_SEMAPHORE:
        return await translator_cli.fill_function(
            python_project_path=python_project_path,
            task_id=task["id"],
            target_file=task["target_files"][0],
            class_name=task.get("class_name"),
            function_name=task["function_name"],
            description=task["description"],
            context=task.get("context", ""),
            context_files=task["target_files"],
        )


def _partial_verify(module: str, db, verifier) -> dict:
    """stub: 模擬局部驗證（固定回傳 pass）

    TODO: 實作 db.apply_seed() 與 verifier.verify_module()
    見 02a/02b（非 07a/07b 範圍）
    """
    # db.apply_seed("fixtures/seed.sql", tables_to_truncate=verifier.tables)
    # report = verifier.verify_module(
    #     collection_path="postman/collection_readonly.json",
    #     module_filter=module,
    # )
    # return report

    # stub：固定回傳 pass
    return {
        "status": "pass",
        "details": {},
    }


async def run(state: RefactorState) -> RefactorState:
    # 恢復前一輪執行（debug 迴圈重入）的進度
    latest_module_status = {}
    for r in state.get("partial_reports", []):
        latest_module_status[r["module"]] = r["report"]["status"]
    already_verified_modules = {
        module for module, status in latest_module_status.items() if status == "pass"
    }

    scheduler = ModuleScheduler(
        state["module_list"],
        state["task_list"],
        already_completed=set(state.get("completed_tasks", [])),
        already_failed=set(state.get("failed_tasks", [])),
        already_verified_modules=already_verified_modules,
    )

    # TODO: 引入 DbEnvironment 與 GoldenVerifier（見 02a/02b，非 07a/07b 範圍）
    # from refactor_harness.db_env import DbEnvironment
    # from refactor_harness.verifier import GoldenVerifier
    # 暫時用 None stub，實際實作時替換
    db = None
    verifier = None

    completed, failed, partial_reports = [], [], []

    while not scheduler.all_done():
        ready = scheduler.get_ready_tasks()
        if not ready:
            break

        # 排程層可同時把多個就緒 task 丟進 gather，但 MODEL_SEMAPHORE(1) 保證序列化
        results = await asyncio.gather(*(_run_one_task(t, state["python_project_path"]) for t in ready))

        touched_modules = set()
        for task, result in zip(ready, results):
            scheduler.mark_task_done(task, result.success)
            touched_modules.add(task["module"])
            (completed if result.success else failed).append(task["id"])

            # regression 偵測：只傳 target_files[0]（實際寫入目標），不是整份 target_files——
            # 其餘元素是唯讀 context，task 並沒有真的寫入那些檔案，理由見 graph/scheduler.py
            # module_owned_files 註解。
            if result.success:
                for regressed in scheduler.check_upstream_regression([task["target_files"][0]], skip_module=task["module"]):
                    scheduler.flag_for_reverify(regressed)

        # 一般完工觸發的局部驗證
        for module in touched_modules:
            if scheduler.module_ready_for_verification(module):
                report = _partial_verify(module, db, verifier)
                report["regression"] = False
                partial_reports.append({"module": module, "report": report})
                scheduler.mark_module_verified(module, passed=report["status"] == "pass")

        # regression 觸發的重驗
        for module, status in list(scheduler.module_status.items()):
            if status == "needs_reverify":
                report = _partial_verify(module, db, verifier)
                report["regression"] = True
                partial_reports.append({"module": module, "report": report})
                scheduler.mark_module_verified(module, passed=report["status"] == "pass")

    blocked_modules = [m for m, s in scheduler.module_status.items() if s == "pending"]
    failed_modules = [m for m, s in scheduler.module_status.items() if s == "failed"]

    return {
        **state,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "partial_reports": partial_reports,
        "blocked_modules": blocked_modules,
        "failed_modules": failed_modules,
    }
