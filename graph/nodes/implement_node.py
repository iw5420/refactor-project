"""
⑤ 功能改寫 Agent（translator-cli + scheduler + Harness 局部驗證）
依 task list 逐一呼叫 translator-cli 實作業務邏輯，並在 module 完工後
呼叫 Harness 局部驗證。見 01_langgraph_architecture.md 六、
02a_harness_architecture.md 十三、07a/07b_translator_cli、
09a_implement_agent_architecture.md。

translator-cli 串接見 07b；Harness 局部驗證串接（DbEnvironment／
GoldenVerifier、熱重載同步屏障、task 失敗根因追蹤、already_failed 修正、
context/context_files 疊加）見本檔案，對應 09a 三～七章。

**Python 服務跑在 Docker 容器內，不直接在 Orchestrator 所在機器上跑**：
見 `python_service/process.py` docstring、`09b_implement_agent_code.md`
十章「已知限制」——`uvicorn --reload` 在 Windows 上經常無法真正完成
重啟（Windows 的 `CTRL_C_EVENT` 送達機制不可靠），已用真實環境重現並
確認容器化（Linux）能穩定繞開這個問題。`_python_service`（模組層級
單例，比照 `MODEL_SEMAPHORE` 的既有模式）在整條 graph run 第一次進入
`implement` 時建立，`implement`／`run_tests`／`debug → implement`
重入期間持續共用同一個容器（見 09a 三章「Python 服務只啟動一次」），
由 `main.py` 在 `graph.ainvoke()` 結束後呼叫 `stop_python_service()`
統一關閉。
"""
import asyncio
import os
import time
import uuid
from pathlib import Path

import httpx
import yaml

from graph.scheduler import ModuleScheduler
from graph.state import RefactorState, TaskFailure, TaskSpec
from python_service.process import PythonServiceContainer
from refactor_harness.fixtures.db_env import DbEnvironment
from refactor_harness.verifier.comparator import GoldenVerifier
from translator_cli import client as translator_cli
from translator_cli.types import FillResult

# 對應硬體限制：本地模型併發數＝1（見 00 三章）。排程層 get_ready_tasks()
# 可一次回傳多個就緒 task，但實際呼叫 translator_cli.fill_function()
# 永遠被這個 Semaphore 收斂成一個個跑（見 07a 八章「不需要鎖機制」）。
MODEL_SEMAPHORE = asyncio.Semaphore(1)

# 比照 refactor_harness/langgraph_nodes/test_nodes.py 既有讀法，不新增
# 第二份設定來源（見 09a 三章「tables_to_truncate」）。
with open("config/harness.yaml", encoding="utf-8") as f:
    _HARNESS_CONFIG = yaml.safe_load(f)
TABLES = _HARNESS_CONFIG["databases"]["test"]["tables_to_truncate"]

# 見 09a 三章「決定性的同步屏障」／「運行前提」，環境變數可調。
SERVICE_READY_TIMEOUT_SECONDS = float(os.environ.get("SERVICE_READY_TIMEOUT_SECONDS", "120"))
SERVICE_READY_POLL_INTERVAL_SECONDS = float(os.environ.get("SERVICE_READY_POLL_INTERVAL_SECONDS", "2"))

_RELOAD_TOKEN_FILE = "_reload_token.py"
_RELOAD_PROBE_PATH = "/__reload_probe__"

# 見 09a 五章「缺口二：共用 Enum 定義檔」。
_ENUMS_FILE = "app/models/_enums.py"
# 見 09a 五章「缺口一：ORM relationship() 缺失」固定提示文字，逐字沿用設計文件內容。
_RELATIONSHIP_GAP_NOTICE = (
    "本專案的 SQLAlchemy model（app/models/{module}.py）只有外鍵純量欄位，"
    "沒有 relationship() 物件導覽屬性（見 08a_scaffold_agent_architecture.md "
    "八章）。禁止用「.關聯屬性」的方式取得關聯物件（例如 order.user 這種寫法"
    "一定會在執行期拋出 AttributeError）；需要關聯資料時，改用額外的 "
    "repository 查詢，或在這個函式內手動用外鍵欄位值另外查詢、自行組裝回傳"
    "結構。"
)


def _augment_task_io(task: TaskSpec) -> tuple[str, list[str]]:
    """對應 09a 五章「疊加規則」：只對 services／repositories 層疊加，
    routers 層原樣返回。不修改 task 本身（[P] 的權威輸出），只回傳疊加
    後的 context／context_files 給呼叫端傳給 fill_function()。
    """
    target_file = task["target_files"][0]
    if not (target_file.startswith("app/repositories/") or target_file.startswith("app/services/")):
        return task.get("context", ""), task["target_files"]

    context_files = list(task["target_files"])
    if _ENUMS_FILE not in context_files:
        context_files.append(_ENUMS_FILE)

    existing = task.get("context", "")
    context = f"{existing}\n\n{_RELATIONSHIP_GAP_NOTICE}" if existing else _RELATIONSHIP_GAP_NOTICE
    return context, context_files


def _target_of(task: TaskSpec) -> tuple[str, str | None, str]:
    """task 的「目標三元組」，見 09a 六章「提前排除」。"""
    return task["target_files"][0], task.get("class_name"), task["function_name"]


def _is_scaffold_skipped(task: TaskSpec, skipped_interfaces: list[dict]) -> bool:
    """精確 tuple 相等比對，不做模糊匹配，見 09a 六章「三元組比對的正確性」。"""
    target = _target_of(task)
    return any(
        (s["file_path"], s["class_name"], s["function_name"]) == target
        for s in skipped_interfaces
    )


def _make_task_failure(task: TaskSpec, reason: str, error: str) -> TaskFailure:
    file_path, class_name, function_name = _target_of(task)
    return TaskFailure(
        task_id=task["id"],
        module=task["module"],
        file_path=file_path,
        class_name=class_name,
        function_name=function_name,
        reason=reason,
        error=error,
    )


async def _run_one_task(task: TaskSpec, python_project_path: str) -> FillResult:
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
        )


async def _partial_verify(module: str, db: DbEnvironment, verifier: GoldenVerifier) -> dict:
    """對應 09a 三章「批次執行」／「必須包 asyncio.to_thread()」：db／
    verifier 都是同步 API，`run()` 是 async def（LangGraph 對 async def
    node 不會自動丟執行緒池，見 09a 三章「為什麼不需要額外的鎖機制」上方
    段落），這裡自己包一層，避免佔住主事件迴圈。
    """
    await asyncio.to_thread(db.apply_seed, "fixtures/seed.sql", tables_to_truncate=TABLES)
    return await asyncio.to_thread(
        verifier.verify_module,
        collection_path="postman/collection_readonly.json",
        module_filter=module,
    )


def _module_has_failed_task(scheduler: ModuleScheduler, module: str) -> bool:
    module_task_ids = {t["id"] for t in scheduler.tasks_by_module.get(module, [])}
    return bool(module_task_ids & scheduler.task_failed)


async def _get_reload_probe_id(python_base_url: str) -> None:
    """對應 09a 三章「運行前提」的一次性初始探測：容忍連線暫時被拒絕，
    重試到任何一次成功回應為止，只確認服務目前有在跑，不比對回應內容。
    只在整條 graph run 真正第一次進入 implement 時才由 run() 觸發。
    """
    deadline = time.monotonic() + SERVICE_READY_TIMEOUT_SECONDS
    async with httpx.AsyncClient() as client:
        while True:
            try:
                resp = await client.get(f"{python_base_url}{_RELOAD_PROBE_PATH}", timeout=5.0)
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Python 服務初始探測逾時（{SERVICE_READY_TIMEOUT_SECONDS}s）："
                    f"{python_base_url}{_RELOAD_PROBE_PATH} 未回應，見 09a 三章「運行前提」——"
                    "服務可能沒有以 uvicorn _reload_probe_wrapper:wrapper_app --reload 啟動"
                )
            await asyncio.sleep(SERVICE_READY_POLL_INTERVAL_SECONDS)


async def _wait_for_service_reload(python_project_path: str, python_base_url: str) -> bool:
    """對應 09a 三章「決定性的同步屏障」：產生新 token、寫入
    _reload_token.py、輪詢 /__reload_probe__ 直到回應等於這個 token。
    逾時回傳 False（不拋例外，見「逾時不該讓整條 pipeline 崩潰」），由
    呼叫端就地把這一輪要驗證的 module 全部標記失敗。
    """
    token = str(uuid.uuid4())
    token_path = Path(python_project_path) / _RELOAD_TOKEN_FILE
    await asyncio.to_thread(token_path.write_text, f'TOKEN = "{token}"\n', encoding="utf-8")

    deadline = time.monotonic() + SERVICE_READY_TIMEOUT_SECONDS
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{python_base_url}{_RELOAD_PROBE_PATH}", timeout=5.0)
                if resp.status_code == 200 and resp.text == token:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(SERVICE_READY_POLL_INTERVAL_SECONDS)
    return False


# 模組層級單例，比照 MODEL_SEMAPHORE 的既有模式：整條 graph run 只跑在
# 單一 Python process 裡（main.py 的 asyncio.run(main())），這個變數在
# implement／run_tests／debug → implement 重入之間持續存在，對應 09a
# 三章「Python 服務只啟動一次」。
_python_service: PythonServiceContainer | None = None


async def _ensure_python_service_started(python_project_path: str, python_base_url: str) -> None:
    """真正第一次進入 implement 時呼叫：容器還沒起來就建立並啟動，已經
    起來（同一個 process 內的後續呼叫，或 debug → implement 重入）就不
    重複啟動。`PythonServiceContainer.start()` 內部已經包含輪詢就緒的
    邏輯，逾時會拋出明確例外（見 python_service/process.py），不需要
    再額外呼叫 _get_reload_probe_id() 確認一次。
    """
    global _python_service
    if _python_service is not None:
        return
    service = PythonServiceContainer(
        python_project_path=python_project_path,
        base_url=python_base_url,
        database_url=os.environ["DATABASE_URL"],
    )
    await asyncio.to_thread(service.start)
    _python_service = service


async def stop_python_service() -> None:
    """main.py 在整條 graph run 結束（不論成功或失敗）時呼叫一次，關閉
    並移除容器——Docker 容器不會隨 Python process 結束自動清理。
    """
    global _python_service
    if _python_service is None:
        return
    await asyncio.to_thread(_python_service.stop)
    _python_service = None


def should_run_tests_or_give_up(state: RefactorState) -> str:
    """對應 01 五章「scaffold 失敗時的收尾路徑」：④ 骨架生成失敗時，
    run_tests（⑥）打的是一個從未被正確產出程式碼的 Python 服務——02a
    五章「Newman 共用執行器」明訂服務沒起來時 newman 執行器會直接拋出
    例外，不是回傳一筆失敗的比對結果。若放任 `implement` 之後無條件接
    `run_tests`，這個例外會讓整條 `graph.ainvoke()` 崩潰，根本走不到
    `should_debug_or_done()` 這個既有的 conditional edge。

    這裡在 `implement → run_tests` 之間插入這道判斷：`implement` 只有
    單一前驅（`scaffold → implement` 才是 fan-in，這裡改的不是那個合流
    點，不影響 01 五章已經驗證過的平行分支語意）。`debug → implement`
    的重試迴圈對 `generate_scaffold()` 的失敗無能為力（07a 十三章：
    working tree 不乾淨等錯誤「需要人工介入核對，不是可以自動化解的
    暫時性錯誤」），因此直接跳 `give_up`，不浪費重試次數在注定不會
    改變結果的迴圈上。
    """
    return "give_up" if state.get("scaffold_done") is False else "run_tests"


async def run(state: RefactorState) -> RefactorState:
    # ④ 骨架生成若失敗，這個 pipeline run 底下不會有任何一個
    # target_file 真的存在，逐一呼叫 fill_function() 只會各自用
    # FileNotFoundError 快速失敗（見 07a 六章步驟 1「scaffold/task 不
    # 一致」），不需要真的跑一輪排程器才知道結果。提前在這裡短路：
    #
    # 全部標記成 failed_modules，不是 blocked_modules——blocked_modules
    # 不消耗 retry_count（見 debug_node.py），若這裡誤用 blocked 會讓
    # debug → implement 永遠原地打轉。標記成 failed_modules 才能讓既有的
    # should_debug_or_done() 正確判斷，但實際路由改由上面的
    # should_run_tests_or_give_up() 接手，不會真的再跑一次 run_tests。
    if state.get("scaffold_done") is False:
        return {
            **state,
            "completed_tasks": [],
            "failed_tasks": [],
            "task_failures": [],
            "partial_reports": [],
            "blocked_modules": [],
            "failed_modules": [m["module"] for m in state["module_list"]],
            "blocked_reasons": {},
        }

    # 恢復前一輪執行（debug 迴圈重入）的進度
    latest_module_status = {}
    for r in state.get("partial_reports", []):
        latest_module_status[r["module"]] = r["report"]["status"]
    already_verified_modules = {
        module for module, status in latest_module_status.items() if status == "pass"
    }

    # 提前排除：scaffold 缺口在排程階段就永久跳過，不等 fill_function()
    # 失敗才發現，見 09a 六章「提前排除」。
    skipped_interfaces = state.get("skipped_interfaces", [])
    existing_failure_ids = {f["task_id"] for f in state.get("task_failures", [])}
    scaffold_gap_task_ids: set[str] = set()
    new_scaffold_gap_failures: list[TaskFailure] = []
    for task in state["task_list"]:
        if _is_scaffold_skipped(task, skipped_interfaces):
            scaffold_gap_task_ids.add(task["id"])
            if task["id"] not in existing_failure_ids:
                new_scaffold_gap_failures.append(
                    _make_task_failure(
                        task, "scaffold_skipped", "④ 骨架階段未渲染此函式，見 skipped_interfaces"
                    )
                )

    scheduler = ModuleScheduler(
        state["module_list"],
        state["task_list"],
        already_completed=set(state.get("completed_tasks", [])),
        # 見 09a 七章「修正」：改傳 scaffold_gap_task_ids，不是
        # state["failed_tasks"]——後者是跨整條 graph run 累積的歷史清單，
        # 混進去會把「這次還沒重試過的翻譯品質失敗」也當永久排除。
        already_failed=scaffold_gap_task_ids,
        already_verified_modules=already_verified_modules,
    )

    partial_reports: list[dict] = []

    # 見 09a 七章「100% 由 scaffold_gap_task_ids 覆蓋的 module」：這種
    # module 永遠不會出現在 touched_modules 裡，必須在這裡提前標記，
    # 否則停在 pending、被歸進 blocked_modules，在沒有其他 failed_modules
    # 時會讓 debug ↔ implement 無限循環（retry_count 永遠不遞增）。
    for module, tasks in scheduler.tasks_by_module.items():
        if tasks and all(t["id"] in scaffold_gap_task_ids for t in tasks):
            scheduler.mark_module_verified(module, passed=False)
            partial_reports.append({
                "module": module,
                "report": {
                    "status": "fail",
                    "reason": "module_entirely_scaffold_skipped",
                    "regression": False,
                },
            })

    db = DbEnvironment(test_dsn=state["test_dsn"])
    verifier = GoldenVerifier(python_base_url=state["python_base_url"], golden_dir="fixtures/golden")

    # 見 09a 三章「debug 重入時必須跳過」：只在整條 graph run 真正第一次
    # 進入 implement 時才啟動 Python 服務容器，debug → implement 重入時
    # 完全跳過（_ensure_python_service_started() 本身也是冪等的，容器
    # 已在跑就直接 return，這裡額外用 is_first_entry 判斷純粹對齊 09a
    # 三章原文「只在真正第一次進入時才做這次前置確認」的語意）。
    is_first_entry = not state.get("completed_tasks") and not state.get("task_failures")
    if is_first_entry:
        await _ensure_python_service_started(state["python_project_path"], state["python_base_url"])

    completed, failed = [], []
    task_failures: list[TaskFailure] = list(new_scaffold_gap_failures)

    while not scheduler.all_done():
        ready = scheduler.get_ready_tasks()
        if not ready:
            break  # 沒有可執行的 task：全部做完、卡在失敗的上游 module，或還有 needs_reverify 待處理

        # 排程層可以同時把多個就緒 task 丟進 gather，
        # 但 MODEL_SEMAPHORE(1) 保證同一時間只有一個真的在呼叫本地模型。
        results = await asyncio.gather(*(_run_one_task(t, state["python_project_path"]) for t in ready))

        touched_modules = set()
        for task, result in zip(ready, results):
            scheduler.mark_task_done(task, result.success)
            touched_modules.add(task["module"])

            if result.success:
                completed.append(task["id"])
                # regression 偵測：只傳 target_files[0]（實際寫入目標），
                # 見 graph/scheduler.py module_owned_files 註解。
                for regressed in scheduler.check_upstream_regression(
                    [task["target_files"][0]], skip_module=task["module"]
                ):
                    scheduler.flag_for_reverify(regressed)
            else:
                failed.append(task["id"])
                task_failures.append(_make_task_failure(task, "fill_failed", result.error or ""))

        pending_verify = [m for m in touched_modules if scheduler.module_ready_for_verification(m)]
        pending_reverify = [m for m, s in scheduler.module_status.items() if s == "needs_reverify"]

        # 見 09a 三章「要不要驗證某個 module」：這個 module 底下有失敗
        # task → 不呼叫 Newman，直接判定沒過，不需要真的等重啟。
        for module in pending_verify:
            if _module_has_failed_task(scheduler, module):
                scheduler.mark_module_verified(module, passed=False)

        verify_needs_wait = [m for m in pending_verify if not _module_has_failed_task(scheduler, m)]
        reverify_needs_wait = list(pending_reverify)

        # 見 09a 三章「批次執行」：整輪只呼叫一次，不是每個 module 各自呼叫。
        if verify_needs_wait or reverify_needs_wait:
            reload_ok = await _wait_for_service_reload(state["python_project_path"], state["python_base_url"])
            if not reload_ok:
                # 見 09a 三章「逾時不該讓整條 pipeline 崩潰」：就地接住，
                # 不讓例外往外傳，這一輪要驗證的每個 module 都判定沒過。
                for module in verify_needs_wait:
                    partial_reports.append({
                        "module": module,
                        "report": {"status": "fail", "reason": "batch_reload_timeout", "regression": False},
                    })
                    scheduler.mark_module_verified(module, passed=False)
                for module in reverify_needs_wait:
                    partial_reports.append({
                        "module": module,
                        "report": {"status": "fail", "reason": "batch_reload_timeout", "regression": True},
                    })
                    scheduler.mark_module_verified(module, passed=False)
            else:
                for module in verify_needs_wait:
                    report = await _partial_verify(module, db, verifier)
                    report["regression"] = False
                    partial_reports.append({"module": module, "report": report})
                    scheduler.mark_module_verified(module, passed=report["status"] == "pass")
                for module in reverify_needs_wait:
                    report = await _partial_verify(module, db, verifier)
                    report["regression"] = True
                    partial_reports.append({"module": module, "report": report})
                    scheduler.mark_module_verified(module, passed=report["status"] == "pass")

    # while 迴圈跳出的兩種可能，對 run_tests/debug 的意義完全不同：
    # - blocked：從未被排到（上游從沒驗證過，屬於「程式碼不存在」）
    # - failed：曾經驗證過但沒過（不論是原生失敗還是 regression 造成的失敗，屬於「程式碼寫錯」）
    blocked_modules = [m for m, s in scheduler.module_status.items() if s == "pending"]
    failed_modules = [m for m, s in scheduler.module_status.items() if s == "failed"]

    # 純附加診斷資訊，不改變 ModuleScheduler 任何放行邏輯（見
    # docs/09b_bug_trace.md #29「先查證根因，不假設、不改排程邏輯」）：
    # 對每個 blocked module，列出它 depends_on 裡狀態還不是 "verified"
    # 的直接上游 module 名稱，讓「這個 module 到底被誰卡住」不需要人工
    # 反查 module_list.depends_on 就能直接讀出來。
    blocked_reasons = {
        m: [d for d in scheduler.modules[m]["depends_on"] if scheduler.module_status.get(d) != "verified"]
        for m in blocked_modules
    }

    return {
        **state,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "task_failures": task_failures,
        "partial_reports": partial_reports,
        "blocked_modules": blocked_modules,
        "failed_modules": failed_modules,
        "blocked_reasons": blocked_reasons,
    }
