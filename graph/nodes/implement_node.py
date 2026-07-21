"""
⑤ 功能改寫 Agent（translator-cli + scheduler）
依 task list 逐一呼叫 translator-cli 實作業務邏輯
見 01_langgraph_architecture.md 六
"""
import asyncio
from graph.state import RefactorState
from graph.scheduler import ModuleScheduler

# TODO: 引入來自 translator-cli 與 Harness 的正式介面
# from translator_cli import client as translator_cli
# from translator_cli.types import FillResult
# from refactor_harness.verifier import GoldenVerifier
# from refactor_harness.db_env import DbEnvironment

# 暫時用 stub 表示硬體限制：本地模型併發數 = 1
MODEL_SEMAPHORE = asyncio.Semaphore(1)


async def _run_one_task(task: dict) -> dict:
    """stub: 模擬呼叫 translator-cli.fill_function()"""
    async with MODEL_SEMAPHORE:
        # TODO: 呼叫 await translator_cli.fill_function(...)
        # TODO: 改成 result.success 屬性存取（目前用字典寫法是 stub，會改）
        # 見 03a/03b
        return {
            "success": True,
            "error": None,
            "diff": "...",
        }


def _partial_verify(module: str, db, verifier) -> dict:
    """stub: 模擬局部驗證（固定回傳 pass）

    TODO: 實作 db.apply_seed() 與 verifier.verify_module()
    見 02a/02b
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

    # TODO: 引入 DbEnvironment 與 GoldenVerifier（見 02a/02b）
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
        results = await asyncio.gather(*(_run_one_task(t) for t in ready))

        touched_modules = set()
        for task, result in zip(ready, results):
            scheduler.mark_task_done(task, result["success"])
            touched_modules.add(task["module"])
            (completed if result["success"] else failed).append(task["id"])

            # regression 偵測
            if result["success"]:
                for regressed in scheduler.check_upstream_regression(task["target_files"], skip_module=task["module"]):
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
