# ⑤ 功能改寫 Agent 程式碼實作

> 本文件承接 `09a_implement_agent_architecture.md`（設計面：決策、契約、資料結構、流程），是這個 Agent 的**實作面**文件：對應每一項 09a 決策實際落地的程式碼。本文件同時記錄一項實測發現、以及對 09a「Python 服務啟動機制」待決定事項的實際解法——見五章。

---

## 目錄

1. `graph/state.py`——新增 `TaskFailure`／`task_failures`
2. `graph/nodes/implement_node.py`——全面改寫
3. `main.py`——接上 `ensure_reload_probe_infra()`／`stop_python_service()`
4. `python_service/`——熱重載探測基礎設施與容器化服務啟動器（新增套件）
5. 實測發現：`uvicorn --reload` 在 Windows 上經常無法真正完成重啟
6. 為什麼改用 Docker 容器，以及驗證方式
7. `tests/graph/test_implement_node.py`——既有測試的同步更新
8. 模組結構總覽
9. 端對端驗證：對真實 Java／Python 專案跑一次完整 Harness 鏈路
10. 已知限制與待驗證事項

---

## 一、`graph/state.py`——新增 `TaskFailure`／`task_failures`

對應 09a 六章「決策：新增 `RefactorState.task_failures`」。`TaskFailure` 插入位置在 `TaskSpec` 之前（獨立的 Agent ⑤ 輸出子型別），`task_failures` 欄位插入位置比照 `completed_tasks`／`failed_tasks`／`partial_reports` 同一組「Agent ⑤（逐 task 累積寫入，需要 reducer）」欄位群組：

```python
# ── Agent ⑤ 輸出：task 失敗根因（見 09a 六章）──
class TaskFailure(TypedDict):
    task_id: str
    module: str
    file_path: str
    class_name: str | None
    function_name: str
    reason: Literal["scaffold_skipped", "fill_failed"]
    error: str
```

```python
class RefactorState(TypedDict):
    ...
    # Agent ⑤（逐 task 累積寫入，需要 reducer）
    completed_tasks: Annotated[list[str], operator.add]
    failed_tasks: Annotated[list[str], operator.add]
    partial_reports: Annotated[list[dict], operator.add]
    # task 失敗時的錯誤訊息與根因分類（"scaffold_skipped" vs "fill_failed"），
    # 供 ⑦ Debug Agent（10a，待建立）不需要重新比對 skipped_interfaces 就能
    # 分辨兩種失敗。歷史累積，不因後續重試成功而移除——一筆 task 若第一次
    # 失敗、重試後成功，這筆記錄仍保留（同時 task_id 也會出現在
    # completed_tasks），供除錯追溯。見 09a 六章。
    task_failures: Annotated[list[TaskFailure], operator.add]
```

---

## 二、`graph/nodes/implement_node.py`——全面改寫

對應 09a 三～七章全部內容。原本的 stub（`db = None`／`verifier = None`、`_partial_verify()` 固定回傳 `pass`）整段換成真實實作：

```python
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
見 `python_service/process.py` docstring、本文件五、六章——`uvicorn
--reload` 在 Windows 上經常無法真正完成重啟（Windows 的 `CTRL_C_EVENT`
送達機制不可靠），已用真實環境重現並確認容器化（Linux）能穩定繞開這個
問題。`_python_service`（模組層級單例，比照 `MODEL_SEMAPHORE` 的既有
模式）在整條 graph run 第一次進入 `implement` 時建立，`implement`／
`run_tests`／`debug → implement` 重入期間持續共用同一個容器（見 09a
三章「Python 服務只啟動一次」），由 `main.py` 在 `graph.ainvoke()`
結束後呼叫 `stop_python_service()` 統一關閉。
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

MODEL_SEMAPHORE = asyncio.Semaphore(1)

with open("config/harness.yaml", encoding="utf-8") as f:
    _HARNESS_CONFIG = yaml.safe_load(f)
TABLES = _HARNESS_CONFIG["databases"]["test"]["tables_to_truncate"]

SERVICE_READY_TIMEOUT_SECONDS = float(os.environ.get("SERVICE_READY_TIMEOUT_SECONDS", "120"))
SERVICE_READY_POLL_INTERVAL_SECONDS = float(os.environ.get("SERVICE_READY_POLL_INTERVAL_SECONDS", "2"))

_RELOAD_TOKEN_FILE = "_reload_token.py"
_RELOAD_PROBE_PATH = "/__reload_probe__"

_ENUMS_FILE = "app/models/_enums.py"
_RELATIONSHIP_GAP_NOTICE = (
    "本專案的 SQLAlchemy model（app/models/{module}.py）只有外鍵純量欄位，"
    "沒有 relationship() 物件導覽屬性（見 08a_scaffold_agent_architecture.md "
    "八章）。禁止用「.關聯屬性」的方式取得關聯物件（例如 order.user 這種寫法"
    "一定會在執行期拋出 AttributeError）；需要關聯資料時，改用額外的 "
    "repository 查詢，或在這個函式內手動用外鍵欄位值另外查詢、自行組裝回傳"
    "結構。"
)
# 對應 09a 五章「缺口三」（docs/09b_bug_trace.md「反覆出現的翻譯模式
# 錯誤」）：跟上面的 relationship 提示不同，任何層級的 task 都可能建構
# ResponseResult／Result 回應，不分層級無條件疊加。
_RESULT_FACTORY_INSTANCE_METHOD_NOTICE = (
    "ResponseResult／Result 這類回應包裝類別（app/services/common_service.py）"
    "的 ok()／error()／success() 等方法在 Python 端是 instance method（帶 self"
    "，不是 Java 原始碼裡的 static 工廠方法），呼叫前必須先建立實例，例如 "
    "ResponseResult().ok_2(data)、Result().success(data)——不要直接寫成 "
    "ResponseResult.ok_2(data) 或 Result.success(data)，那樣在 Python 會因為"
    "缺少 self 引數而拋出 TypeError。"
    "如果這個 task 正是在實作 ResponseResult／Result 自己的 ok_2()／error_2()"
    "／success()／failure() 這些方法本體，絕對不要在方法內部呼叫"
    "「ResponseResult().ok_2(...)」或「self.error_2(...)」這種同名／同族方法"
    "呼叫自己（會造成無窮遞迴，永遠不會返回，任何呼叫端都會撞"
    "RecursionError）——正確做法是直接建立一個新實例、把欄位值設好後回傳，"
    "例如 result = ResponseResult(); result.code = code; result.msg = msg; "
    "result.data = data; return result（見 docs/09b_bug_trace.md #53 真實案例）。"
)


def _augment_task_io(task: TaskSpec) -> tuple[str, list[str]]:
    """對應 09a 五章「疊加規則」：缺口一／二只對 services／repositories 層
    疊加，routers 層不動；缺口三（`_RESULT_FACTORY_INSTANCE_METHOD_NOTICE`）
    不分層級，無條件疊加給每一個 task。不修改 task 本身（[P] 的權威輸出），
    只回傳疊加後的 context／context_files 給呼叫端傳給 fill_function()。
    """
    target_file = task["target_files"][0]
    if target_file.startswith("app/repositories/") or target_file.startswith("app/services/"):
        context_files = list(task["target_files"])
        if _ENUMS_FILE not in context_files:
            context_files.append(_ENUMS_FILE)
        existing = task.get("context", "")
        context = f"{existing}\n\n{_RELATIONSHIP_GAP_NOTICE}" if existing else _RELATIONSHIP_GAP_NOTICE
    else:
        context_files = task["target_files"]
        context = task.get("context", "")

    context = f"{context}\n\n{_RESULT_FACTORY_INSTANCE_METHOD_NOTICE}" if context else _RESULT_FACTORY_INSTANCE_METHOD_NOTICE
    return context, context_files


def _target_of(task: TaskSpec) -> tuple[str, str | None, str]:
    return task["target_files"][0], task.get("class_name"), task["function_name"]


def _is_scaffold_skipped(task: TaskSpec, skipped_interfaces: list[dict]) -> bool:
    target = _target_of(task)
    return any(
        (s["file_path"], s["class_name"], s["function_name"]) == target
        for s in skipped_interfaces
    )


def _make_task_failure(task: TaskSpec, reason: str, error: str) -> TaskFailure:
    file_path, class_name, function_name = _target_of(task)
    return TaskFailure(
        task_id=task["id"], module=task["module"], file_path=file_path,
        class_name=class_name, function_name=function_name, reason=reason, error=error,
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
    重複啟動。PythonServiceContainer.start() 內部已經包含輪詢就緒的
    邏輯，逾時會拋出明確例外，不需要再額外呼叫 _get_reload_probe_id()
    確認一次。
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
    return "give_up" if state.get("scaffold_done") is False else "run_tests"


async def run(state: RefactorState) -> RefactorState:
    if state.get("scaffold_done") is False:
        return {
            **state,
            "completed_tasks": [], "failed_tasks": [], "task_failures": [],
            "partial_reports": [], "blocked_modules": [],
            "failed_modules": [m["module"] for m in state["module_list"]],
        }

    latest_module_status = {}
    for r in state.get("partial_reports", []):
        latest_module_status[r["module"]] = r["report"]["status"]
    already_verified_modules = {
        module for module, status in latest_module_status.items() if status == "pass"
    }

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
        state["module_list"], state["task_list"],
        already_completed=set(state.get("completed_tasks", [])),
        already_failed=scaffold_gap_task_ids,
        already_verified_modules=already_verified_modules,
    )

    partial_reports: list[dict] = []

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

    is_first_entry = not state.get("completed_tasks") and not state.get("task_failures")
    if is_first_entry:
        await _ensure_python_service_started(state["python_project_path"], state["python_base_url"])

    completed, failed = [], []
    task_failures: list[TaskFailure] = list(new_scaffold_gap_failures)

    while not scheduler.all_done():
        ready = scheduler.get_ready_tasks()
        if not ready:
            break

        results = await asyncio.gather(*(_run_one_task(t, state["python_project_path"]) for t in ready))

        touched_modules = set()
        for task, result in zip(ready, results):
            scheduler.mark_task_done(task, result.success)
            touched_modules.add(task["module"])

            if result.success:
                completed.append(task["id"])
                for regressed in scheduler.check_upstream_regression(
                    [task["target_files"][0]], skip_module=task["module"]
                ):
                    scheduler.flag_for_reverify(regressed)
            else:
                failed.append(task["id"])
                task_failures.append(_make_task_failure(task, "fill_failed", result.error or ""))

        pending_verify = [m for m in touched_modules if scheduler.module_ready_for_verification(m)]
        pending_reverify = [m for m, s in scheduler.module_status.items() if s == "needs_reverify"]

        for module in pending_verify:
            if _module_has_failed_task(scheduler, module):
                scheduler.mark_module_verified(module, passed=False)

        verify_needs_wait = [m for m in pending_verify if not _module_has_failed_task(scheduler, m)]
        reverify_needs_wait = list(pending_reverify)

        if verify_needs_wait or reverify_needs_wait:
            reload_ok = await _wait_for_service_reload(state["python_project_path"], state["python_base_url"])
            if not reload_ok:
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

    blocked_modules = [m for m, s in scheduler.module_status.items() if s == "pending"]
    failed_modules = [m for m, s in scheduler.module_status.items() if s == "failed"]

    return {
        **state,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "task_failures": task_failures,
        "partial_reports": partial_reports,
        "blocked_modules": blocked_modules,
        "failed_modules": failed_modules,
    }
```

**與 09a 設計的對應關係**：

| 09a 章節 | 落地位置 |
|---|---|
| 三章「`tables_to_truncate`」 | 模組層級 `TABLES`，讀法比照 `test_nodes.py` |
| 三章「決定性的同步屏障」 | `_wait_for_service_reload()` |
| 三章「運行前提」一次性初始探測 | `_ensure_python_service_started()`，`run()` 內 `is_first_entry` 判斷（見五、六章：實際落地時發現需要真的啟動服務，不只是探測，因此比 09a 原文多做了一步） |
| 三章「必須包 `asyncio.to_thread()`」 | `_partial_verify()` |
| 三章「批次執行」「要不要驗證某個 module」 | `run()` 內 `verify_needs_wait`／`reverify_needs_wait` 分流 |
| 三章「逾時不該讓整條 pipeline 崩潰」 | `_wait_for_service_reload()` 回傳 `bool`，`run()` 就地處理逾時 |
| 五章「`context`／`context_files` 補強」 | `_augment_task_io()` |
| 六章「提前排除」「三元組比對」 | `_target_of()`／`_is_scaffold_skipped()`／`_make_task_failure()` |
| 七章「`already_failed` 的正確來源」 | `ModuleScheduler(..., already_failed=scaffold_gap_task_ids, ...)` |
| 七章「100% 由 scaffold_gap_task_ids 覆蓋的 module」 | `run()` 內排程器建構後、while 迴圈前的掃描 |

**與 09a 文字描述的一處刻意簡化**：09a 三章「要不要驗證某個 module」描述成「有失敗 task → 直接 `mark_module_verified`；沒有 → 批次等重啟再驗證」，字面上像是逐 module 各自判斷。實作為了滿足「批次等重啟整輪只做一次」（三章「批次執行」）這個更早的約束，把判斷拆成兩段：先過濾掉本輪已知會失敗的 module（不需要等待），再對剩下**真正需要驗證**的 module 集合（`verify_needs_wait`／`reverify_needs_wait`）只呼叫一次 `_wait_for_service_reload()`——語意上與 09a 一致（同一個 module 的判斷結果不變），只是把「逐 module 判斷」與「批次等待」的迴圈順序對調，避免「這一輪全部 module 都因為失敗 task 被跳過」時還無謂觸發一次重啟等待。

**`_get_reload_probe_id()` 保留但不再是 `run()` 唯一的前置確認手段**：09a 原文設計成「只確認服務有回應，假設服務已由外部某個機制啟動好」。實作階段發現「由誰、何時啟動」這個問題若不解決，整個機制無法真正跑起來（見五、六章），因此改成 `_ensure_python_service_started()` 主動啟動服務——這個函式內部的 `PythonServiceContainer.start()` 已經包含等待就緒的邏輯，語意上涵蓋了 `_get_reload_probe_id()` 原本要做的事。`_get_reload_probe_id()` 本身沒有刪除（仍被 `tests/python_service/test_reload_probe_integration.py` 用來獨立驗證探測端點本身的行為），只是不再是 `run()` 這個時間點呼叫的函式。

---

## 三、`main.py`——接上 `ensure_reload_probe_infra()`／`stop_python_service()`

```python
import asyncio
import os
from dotenv import load_dotenv
from graph.builder import build_graph
from graph.nodes import implement_node
from python_service.reload_probe import ensure_reload_probe_infra


load_dotenv()


async def main():
    graph = build_graph()

    python_project_path = os.environ["PYTHON_PROJECT_PATH"]
    # 一次性前置準備：必須在 scaffold（④）第一次執行之前完成，見
    # python_service/reload_probe.py docstring、本文件四章。冪等，
    # main.py 每次啟動都呼叫。
    ensure_reload_probe_infra(python_project_path)

    initial_state = {
        "java_project_path": os.environ["JAVA_PROJECT_PATH"],
        "python_project_path": python_project_path,
        ...
        "task_failures": [],
        ...
    }

    print("Starting Refactor Orchestrator...")
    try:
        final_state = await graph.ainvoke(initial_state)
        ...
    finally:
        # Docker 容器不會隨 Python process 結束自動清理，不論
        # graph.ainvoke() 成功或拋出例外都要收尾。
        await implement_node.stop_python_service()
```

`initial_state` 補上 `task_failures: []`，比照既有 `Annotated[list, operator.add]` 欄位在 `initial_state` 顯式初始化成空清單的慣例（01 九章「顯式全部初始化」）。

---

## 四、`python_service/`——熱重載探測基礎設施與容器化服務啟動器（新增套件）

09a 十一章原本把「wrapper 檔案的實際存放位置、由誰在什麼時候寫入 `python_project_path`」「Python 服務啟動機制沒有文件正式列為 pipeline 步驟」列為待決定事項。落地時發現這兩個問題若不解決，整個熱重載同步機制無法真正驗證（見五章），因此本次一併定案並實作，獨立成新套件，不塞進 `translator_cli/`（09a 三章已明講這是⑤自己的邊界，不是填空契約的一部分）：

```
python_service/
├── __init__.py          # 對外匯出 PythonServiceContainer、ensure_reload_probe_infra 等
├── reload_probe.py       # WRAPPER_TEMPLATE、ensure_reload_probe_infra()
├── process.py            # PythonServiceContainer：docker run 啟動、就緒判定、關閉
├── java_properties.py    # 新增（10a／10b #46）：解析 Java application-{profile}.properties
└── manager.py             # 新增（10a 八章）：_python_service 單例，見下方說明
```

**這個套件的單例管理後來從 `implement_node.py` 下沉到獨立的 `manager.py`**（10a 八章「診斷資料改走 State，不是 `debug_agent/` 直接 import `implement_node`」）——本章以下 `_ensure_python_service_started()`／`_python_service` 相關程式碼片段是這個套件**最初**加入時的版本，`manager.py` 誕生後的目前實際架構、`_ensure_python_service_started()` 目前的簽名，以及新增的 `java_properties.py`（解析 Java 端 `application-{profile}.properties`，對應 `docs/09b_bug_trace.md` #46）完整程式碼，見 `10b_debug_agent_code.md` 五章「新增檔案：`python_service/manager.py`」——這裡不重複貼一份會漂移的副本，只維持本章其餘部分（`reload_probe.py`／`process.py`／五、六章的問題根因敘事）作為這個套件最初設計動機的完整記錄。

### `reload_probe.py`

對應 09a 三章「這個端點不寫進 `app/main.py`，改用外掛的 ASGI wrapper 掛載真正的 app」與「必須排除在 07a 九章的衝突偵測之外」定案的完整內容：

```python
"""一次性部署 _reload_probe_wrapper.py／.gitignore 到 python_project_path。

**呼叫時機**：必須在 generate_scaffold()（④ 骨架實作 Agent）第一次執行
之前呼叫——.gitignore 的初始 commit 必須早於 generate_scaffold() 自己的
骨架 commit，否則這兩個檔案會以未追蹤檔案的身分讓
check_clean_working_tree() precondition 檢查失敗（見 07a 九章）。因此由
main.py 在 graph.ainvoke() 之前呼叫一次。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from translator_cli.git_ops import ensure_git_repo

WRAPPER_FILE_NAME = "_reload_probe_wrapper.py"
TOKEN_FILE_NAME = "_reload_token.py"

# __pycache__/ 不是 09a 三章原本列的兩個檔名，是端對端驗證時發現的必要
# 追加：implement 期間容器（process.py）透過 bind mount 常駐執行
# uvicorn --reload，CPython 每次 import 都會在磁碟上寫出
# __pycache__/*.pyc——這些是容器裡的行程寫的，不是任何一次
# fill_function()／generate_scaffold() 自己的動作，但一樣會讓
# check_clean_working_tree()（07a 九章）判定 working tree 不乾淨，導致
# 同一輪 implement 迴圈裡後續每一個 task 都直接被 precondition 檢查擋下。
_IGNORED_ENTRIES = (WRAPPER_FILE_NAME, TOKEN_FILE_NAME, "__pycache__/")

WRAPPER_TEMPLATE = '''"""測試基礎設施用的固定樣板，不含任何業務邏輯，不由任何 Agent 產生。
啟動方式：uvicorn _reload_probe_wrapper:wrapper_app --reload
（不是 uvicorn app.main:app --reload）。
"""
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from app.main import app as _real_app

try:
    from _reload_token import TOKEN
except ModuleNotFoundError:
    TOKEN = ""


async def _probe(request):
    return PlainTextResponse(TOKEN)


wrapper_app = Starlette(routes=[
    Route("/__reload_probe__", _probe),
    Mount("/", app=_real_app),
])
'''


def _run_git(python_project_path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", python_project_path, *args], capture_output=True, text=True, encoding="utf-8"
    )


def ensure_reload_probe_infra(python_project_path: str) -> None:
    """冪等：main.py 每次啟動都可以呼叫。只在 .gitignore 缺少這兩個
    檔名時才新增一次 commit；wrapper 檔案內容固定，每次都覆寫。不自動
    git init：python_project_path 是否已完成 07a 二章「一次性前置準備」
    是輸入端環境問題，比照 generate_scaffold() 對同一個前提的既有
    處理方式（ensure_git_repo() 檢查不通過就直接拋出明確錯誤）。
    """
    ensure_git_repo(python_project_path)

    root = Path(python_project_path)
    gitignore_path = root / ".gitignore"

    existing_lines = (
        gitignore_path.read_text(encoding="utf-8").splitlines() if gitignore_path.exists() else []
    )
    missing = [name for name in _IGNORED_ENTRIES if name not in existing_lines]

    if missing:
        with gitignore_path.open("a", encoding="utf-8") as f:
            for name in missing:
                f.write(f"{name}\n")

        add = _run_git(python_project_path, "add", ".gitignore")
        if add.returncode != 0:
            raise RuntimeError(f"git add .gitignore 失敗：{add.stderr.strip()}")

        commit = _run_git(python_project_path, "commit", "-m", "chore: ignore reload-probe infra files")
        if commit.returncode != 0:
            raise RuntimeError(f"git commit .gitignore 失敗：{commit.stderr.strip()}")

    (root / WRAPPER_FILE_NAME).write_text(WRAPPER_TEMPLATE, encoding="utf-8")
```

### `process.py`——`PythonServiceContainer`

比照 `spec_collection_agent/java_service.py` 的 `JavaServiceProcess` 介面慣例（見 03a 二章）：`start()`／`stop()`／`diagnostics`。與 `JavaServiceProcess` 的關鍵差異：管理的是一個 Docker 容器（見五、六章原因），不是本機 `subprocess.Popen`：

```python
"""管理容器化 Python 服務（uvicorn _reload_probe_wrapper:wrapper_app
--reload，跑在 Docker 容器內）的啟動、就緒判定、關閉。原因見本文件
五、六章：uvicorn --reload 在 Windows 上經常無法真正完成重啟，容器內
的 Linux 環境不受這個限制影響。
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

DEFAULT_STARTUP_TIMEOUT_SECONDS = 90.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.5
DEFAULT_SHUTDOWN_GRACE_SECONDS = 15.0

RELOAD_PROBE_PATH = "/__reload_probe__"

# 目標 Python 服務的技術棧已在 00 三章定案（FastAPI + SQLAlchemy）；
# 容器內只安裝這個已知的最小基線集合。**這是刻意簡化，不是完整的依賴
# 管理方案**——若目標專案還需要更多套件，見九章「已知限制」。
#
# `python-multipart` 是基線的一部分，不是「目標專案依賴」那條未解決的
# 一般缺口：真實環境重跑才發現，任何 endpoint 只要簽名帶 File／Form
# （multipart/form-data），FastAPI 在 import 階段就會直接 RuntimeError，
# 讓整個 app.main 連 import 都失敗，容器完全起不來（不是這一個 endpoint
# 壞掉）——這是 00 三章定案技術棧本身、只要用到檔案上傳就一定需要的
# 套件，跟 psycopg2-binary 同一種「技術棧固定依賴」性質，見
# docs/09b_bug_trace.md。
BASELINE_PACKAGES = ("fastapi", "uvicorn[standard]", "sqlalchemy", "psycopg2-binary", "python-multipart")

DOCKER_IMAGE = "python:3.12-slim"


class PythonServiceStartupTimeout(RuntimeError):
    def __init__(self, message: str, *, diagnostics: str = ""):
        super().__init__(message)
        self.diagnostics = diagnostics


class PythonServiceContainer:
    def __init__(
        self, *, python_project_path: str, base_url: str, database_url: str,
        extra_env: dict[str, str] | None = None,
        container_name: str = "refactor_python_service",
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self.python_project_path = str(Path(python_project_path).resolve())
        self.base_url = base_url.rstrip("/")
        self.database_url = database_url
        # 見 docs/09b_bug_trace.md #46：③ 決定「用環境變數」這個機制、
        # 算出常數名稱（PythonStructure.config_env_vars），呼叫端
        # （python_service/manager.py::ensure_started()）負責解析出實際
        # 值（python_service/java_properties.py），這裡只負責機械把
        # 已經解析好的 {常數名: 值} 對照表轉成額外的 -e 旗標。
        self.extra_env = extra_env or {}
        self.container_name = container_name
        self.startup_timeout = startup_timeout
        self.poll_interval = poll_interval
        self.shutdown_grace = shutdown_grace
        self._started = False

    def start(self) -> None:
        # 啟動前先確保沒有同名的殘留容器（例如上一輪異常中斷留下的），
        # 否則 docker run --name 會直接失敗；容器本來就不存在時 docker
        # rm 回非 0 是正常情況，不是這裡要處理的錯誤。
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True, text=True)

        pip_install = " ".join(BASELINE_PACKAGES)
        # 見 docs/09b_bug_trace.md #51：容器缺套件崩潰時完全沒有 log 線索
        # 能直接看出這次到底裝了哪些套件，事後只能靠 docker logs 反查
        # ——把即將安裝的完整套件清單記下來，之後再缺套件，第一時間就能
        # 從這行 log 核對，不用再重新 docker logs 一次。
        logger.info("PythonServiceContainer 即將安裝的基線套件：%s", pip_install)
        cmd = [
            "docker", "run", "-d", "--name", self.container_name,
            # 容器內的 127.0.0.1／localhost 指向容器自己，不是跑這個
            # Orchestrator 的 host——測試 DB 通常監聽在 host 上（見 00
            # 五章 TEST_DB_DSN／DATABASE_URL 慣例）。--add-host 明確把
            # host.docker.internal 對應到 host-gateway，不依賴 Docker
            # Desktop 版本是否預設就會自動提供這個 DNS 名稱（已實測：
            # 部分版本組合下不加這個旗標會直接解析失敗）。
            "--add-host=host.docker.internal:host-gateway",
            "-v", f"{self.python_project_path}:/srv", "-w", "/srv",
            "-p", f"{self._port()}:8000",
            "-e", f"DATABASE_URL={self._container_database_url()}",
        ]
        # 見 docs/09b_bug_trace.md #46：③ 掃出的 @Value 屬性注入環境變數，
        # 依 self.extra_env 逐一附加成 -e 旗標。extra_env 為空（預設值）
        # 時這裡完全不做事，docker run 指令跟修改前逐字相同。
        for key, value in self.extra_env.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [
            DOCKER_IMAGE, "bash", "-c",
            f"pip install --quiet {pip_install} && "
            "uvicorn _reload_probe_wrapper:wrapper_app --reload --host 0.0.0.0 --port 8000",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise PythonServiceStartupTimeout(f"docker run 啟動失敗：{result.stderr.strip()}")

        self._started = True
        if not self._poll_until_ready():
            diagnostics = self.diagnostics
            # 見 docs/09b_bug_trace.md #51：容器啟動失敗時，docker logs
            # 診斷輸出直接印進 orchestrator.log，不用再手動查容器。
            logger.error(
                "PythonServiceContainer 在 %.0f 秒內未能就緒，容器診斷輸出：\n%s",
                self.startup_timeout, diagnostics,
            )
            self.stop()
            raise PythonServiceStartupTimeout(
                f"Python 服務容器在 {self.startup_timeout} 秒內未能就緒",
                diagnostics=diagnostics,
            )
        logger.info(
            "PythonServiceContainer 就緒（container=%s, base_url=%s, extra_env keys=%s）",
            self.container_name, self.base_url, sorted(self.extra_env),
        )

    def _port(self) -> str:
        return str(urlparse(self.base_url).port or 8000)

    def _container_database_url(self) -> str:
        """把 `database_url` 裡指向 host 自己的位址（`127.0.0.1`／
        `localhost`）換成 `host.docker.internal`——這個字串是從 host
        角度寫的（`.env` 的 `DATABASE_URL`），容器內必須用不同的位址
        才能連到同一顆 DB。只替換 host 部分，不動 port／帳密／DB 名稱。
        """
        for host in ("127.0.0.1", "localhost"):
            if f"@{host}:" in self.database_url or f"@{host}/" in self.database_url:
                return self.database_url.replace(f"@{host}", "@host.docker.internal")
        return self.database_url

    def _poll_until_ready(self) -> bool:
        deadline = time.monotonic() + self.startup_timeout
        url = f"{self.base_url}{RELOAD_PROBE_PATH}"
        while time.monotonic() < deadline:
            try:
                if requests.get(url, timeout=self.poll_interval).status_code == 200:
                    return True
            except requests.exceptions.RequestException:
                pass
            time.sleep(self.poll_interval)
        return False

    def stop(self) -> None:
        if not self._started:
            return
        subprocess.run(
            ["docker", "stop", "-t", str(int(self.shutdown_grace)), self.container_name],
            capture_output=True, text=True,
        )
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True, text=True)
        self._started = False

    @property
    def diagnostics(self) -> str:
        result = subprocess.run(
            ["docker", "logs", "--tail", "200", self.container_name], capture_output=True, text=True,
        )
        return result.stdout + result.stderr
```

---

## 五、實測發現：`uvicorn --reload` 在 Windows 上經常無法真正完成重啟

09a 三章「決定性的同步屏障」整個機制的前提是「`uvicorn --reload` 最終會真的重啟、只是需要正確判斷等到的是哪一次重啟」。第一版落地時直接照這個前提實作（`_wait_for_service_reload()`／`_get_reload_probe_id()` 假設有一個外部啟動、正在跑 `uvicorn --reload` 的服務），但寫了針對真實服務的整合測試（`tests/python_service/test_reload_probe_integration.py`）後發現**這個前提在 Windows 上經常不成立**。

**重現方式**：啟動一個最小的 `uvicorn app:app --reload`，修改被監控的 `.py` 檔案，觀察是否真的重啟：

```
WARNING:  WatchFiles detected changes in 'app.py'. Reloading...
INFO:     127.0.0.1:xxxxx - "GET /v HTTP/1.1" 200 OK   ← 之後所有回應都還是舊版內容
```

`WatchFiles` 正確偵測到檔案變更、`Reloading...` 訊息也印出來了，但**新的 worker process 從未真正啟動**（`tasklist` 確認舊 PID 一路存活），舊 worker 持續回應舊內容，永遠卡住——不是等待時機判斷錯誤，是重啟這件事本身沒有發生。

**根因**（`uvicorn/supervisors/basereload.py` `BaseReload.restart()`）：

```python
def restart(self) -> None:
    if sys.platform == "win32":
        self.is_restarting = True
        os.kill(self.process.pid, signal.CTRL_C_EVENT)   # ← 問題點
        sys.stdout.write(" ")
        sys.stdout.flush()
    else:
        self.process.terminate()
    self.process.join()   # Windows 上常常永遠等不到
```

Windows 沒有 POSIX signal，uvicorn 用 `CTRL_C_EVENT` 模擬 Ctrl+C 要求 worker 自我終止——但 `CTRL_C_EVENT` 只有在目標行程與發送端共享同一個 console process group 時才能送達。Orchestrator 自己 spawn 這個子行程（不論是否重導向 stdout/stderr、是否用 `creationflags=subprocess.CREATE_NEW_CONSOLE`，兩者都實測試過仍然卡住）時，這個條件經常不滿足，`os.kill()` 呼叫本身不會報錯，但事件從未真正送達，`self.process.join()` 因此永遠卡住，reloader 的主迴圈被鎖死，永遠不會走到「產生新 worker」那一步。

---

## 六、為什麼改用 Docker 容器，以及驗證方式

由於「什麼時候該讓服務換上新程式碼」（每輪 `fill_function()` 批次寫完之後）完全由 Orchestrator 自己掌握，理論上不一定需要依賴 `--reload` 的檔案監控機制；但改成「Orchestrator 自己 kill＋重啟整個行程」一樣會撞上同一個 Windows 子行程管理的不確定性，而且會讓 09a 三章已經設計好、已核准的 token 同步機制整套作廢。改用 Docker（Linux 容器）保留了 09a 的原始設計：容器內的 uvicorn 走的是穩定的 POSIX `self.process.terminate()`（SIGTERM）路徑，不受 Windows 限制影響。

**驗證方式**：先手動用 `docker run` + bind mount 重現整個場景確認可行，再寫成 `tests/python_service/test_reload_probe_integration.py`（真實 Docker 容器，非 mock）：

1. 建立最小 FastAPI app，`ensure_reload_probe_infra()` 部署 wrapper，`PythonServiceContainer` 啟動容器
2. `_get_reload_probe_id()` 對容器送出真實 HTTP 請求，確認能連上
3. `_wait_for_service_reload()` 寫入第一個 token，確認容器內的 wrapper 正確回應這個值
4. **修改 `app/main.py`（透過 host 端的 bind mount），觸發一次「真正的」容器內 reload**，再呼叫一次 `_wait_for_service_reload()` 帶新 token——這是整個測試的核心：若函式錯誤地把舊 worker 仍在回應的舊值當成就緒，這裡的斷言會失敗；只有真的等到新 worker 才會通過
5. 額外打 `/ping` 確認容器回傳的內容確實是新版程式碼，佐證剛才等到的不只是「連線成功」，是「新程式碼生效」

三個測試全數通過（見 `tests/python_service/test_reload_probe_integration.py`），手動重現（`docker run` + 修改掛載目錄下的檔案）也確認容器內日誌會出現 `Finished server process [N]` → `Started server process [N+1]`——這是本機 Windows 環境從未觀察到的一行日誌，直接證明重啟真的發生了。

---

## 七、`tests/graph/test_implement_node.py`——既有測試的同步更新

`run()` 現在一開始就會建構真正的 `DbEnvironment`／`GoldenVerifier`，並在「真正第一次進入 `implement`」時呼叫 `_ensure_python_service_started()`（會嘗試啟動 Docker 容器）。既有的 `test_scaffold_done_true_does_not_short_circuit`（`module_list` 為空，只驗證短路分支沒有被誤觸發）因此需要 mock 掉 `_ensure_python_service_started()`，避免單元測試依賴 Docker：

```python
def test_scaffold_done_true_does_not_short_circuit(self, monkeypatch):
    import graph.nodes.implement_node as implement_node

    async def _skip_start(*args, **kwargs):
        return None

    monkeypatch.setattr(implement_node, "_ensure_python_service_started", _skip_start)

    state = {
        "scaffold_done": True,
        "module_list": [],
        "task_list": [],
        "completed_tasks": [],
        "failed_tasks": [],
        "task_failures": [],
        "skipped_interfaces": [],
        "partial_reports": [],
        "python_project_path": "/unused",
        "test_dsn": "postgresql://unused",
        "python_base_url": "http://unused",
    }

    result = asyncio.run(run(state))

    assert result["failed_modules"] == []
    assert result["blocked_modules"] == []
```

`test_scaffold_failure_skips_scheduler_and_marks_all_modules_failed`（`scaffold_done=False` 短路案例）不受影響——那條路徑在任何 Docker／DB 相關程式碼執行之前就已經 `return`。

---

## 八、模組結構總覽

```
refactor-project/
├── graph/
│   ├── state.py               # 新增 TaskFailure、RefactorState.task_failures
│   └── nodes/
│       └── implement_node.py  # 全面改寫：真實 Harness 串接、熱重載同步屏障、
│                               # context 疊加、task 失敗根因追蹤、already_failed 修正、
│                               # 容器化 Python 服務生命週期管理
├── python_service/            # 新增套件：熱重載探測基礎設施 + 容器化服務啟動器
│   ├── __init__.py
│   ├── reload_probe.py        # WRAPPER_TEMPLATE、ensure_reload_probe_infra()
│   └── process.py             # PythonServiceContainer
├── main.py                    # 接上 ensure_reload_probe_infra()／stop_python_service()
├── requirements.txt           # 新增 fastapi／uvicorn／starlette（測試依賴，見九章）
└── tests/
    ├── graph/test_implement_node.py                    # 同步更新既有測試
    └── python_service/
        ├── test_reload_probe.py                        # ensure_reload_probe_infra() 單元測試（真實 git repo）
        └── test_reload_probe_integration.py             # 真實 Docker 容器整合測試
```

09a 八章「不新增 `implement_agent/` 套件」的決策維持不變——`python_service/` 是新增套件，但這是回應實測發現、屬於「Python 服務啟動機制」這個原本就懸而未決的操作缺口的實作，不是給 ⑤ 本身的業務邏輯新增抽象層。`graph/scheduler.py`、`translator_cli/`、`refactor_harness/` 完全未變動。

---

## 九、端對端驗證：對真實 Java／Python 專案跑一次完整 Harness 鏈路

五、六章的 Docker 驗證只證明「熱重載同步屏障」這個機制對一個最小 FastAPI 玩具 app 有效，不足以證明 09b 接上的 `DbEnvironment`／`GoldenVerifier` 真的能對一個真實專案跑出有意義的結果。本章記錄對這個 repo 既有的真實環境（`../lang-exam-api-refactor` Java 專案、`../exam-platform-api` Python 目標專案、可連線的測試 DB）實際跑「① 啟動 Java → ② 錄製 golden → 啟動容器化 Python 服務 → ⑥ 用 `GoldenVerifier` 驗證」完整鏈路的過程，共跑了三輪。

**第一輪**用的是這個 repo 既有的舊版 `exam-platform-api`（早於 07a 五章「填空模式本體 import 解析」機制存在、也早於 08a 具備 entity 生成能力），過程中發現並修正 `refactor_harness`（`newman` 找不到執行檔、`golden_writer.py` 讀錯 header 欄位、`--env-var` 變數名稱對不上、容器內 DB 連線位址）與 translator-cli（`resolve_body_imports()` 同檔案自我引用誤判）共五項既有缺陷——完整重現過程、程式碼與回歸測試已同步進 `02a_harness_architecture.md`／`02b_harness_code.md`／`07a_translator_cli_architecture.md`／`07b_translator_cli_code.md`，不在本文件重複。修完這五項後，用當時的舊 fixture 驗證仍是 10 個 case 全掛，追查確認純粹是舊 fixture 缺 `app/models/`、且早於上述 import 修正存在，不是這五項修正的問題——需要重新跑一次完整 scaffold＋fill 才能做真正對等的驗證。

**第二輪**改用當前 pipeline 對 `exam-platform-api` 重新跑一次 scaffold＋fill（含 08a entity 生成，覆蓋掉舊的驗證 commit），過程中依序發現並解決：

- **`SERVICE_READY_TIMEOUT_SECONDS` 逾時**：72 個 task 的真實批次規模下，預設 120 秒不足以讓 `file`／`school` 模組的局部驗證跑到 Newman；診斷用 300 秒後完全不再逾時，確認根因是「批次規模下 120 秒不夠」，不是同步機制卡死（正式預設值是否要調整見十章）。
- **`__pycache__/` 讓 working tree 變髒**：容器透過 bind mount 常駐執行 `uvicorn --reload`，寫回 host 的 `__pycache__/*.pyc` 是未追蹤檔案，讓後續每個 task 的 precondition 檢查失敗——已修正並補進 `reload_probe.py` 的 `_IGNORED_ENTRIES`（見四章）。
- **`response["body"]` 在真實 newman 6.2.2 根本不存在**：body 內容序列化在 `response["stream"]`，舊寫法永遠讀到 `None`——代表這之前錄到的每一筆 golden body 都是空的，body diff 從未真正比對過任何內容。這項修正同樣同步進 `02a`/`02b`。

修正後重新錄製 golden、逐一排除目標專案本身的翻譯品質問題（`find_distinct_field()` 遮蔽 builtin、`set()` 去重順序不對、缺 `ORDER BY`、寫死訊息文字、Pydantic 必填欄位、缺 import 等——這些是 `exam-platform-api` 自己的程式碼問題，不是 Orchestrator 生成邏輯的 bug，手動修正，未改動任何生成邏輯），最終：

```
school 模組：total=3, passed=3, failed=0, pass_rate=1.0, status="pass"
excluded_cases: ["get_version_GET_api_general_version"]（正確排除，非失敗）
```

`school` 模組完整跑過錄製 → 容器化啟動 → 熱重載同步 → Newman 驗證 → 與真實 golden output（含 body 內容）比對的完整鏈路，且完全通過，是第二輪追查達成的目標。

**第三輪**針對 #11/#12/#28/#29/#30 這批「架構性、不該丟給 ⑦ Debug Agent 去猜」的缺口（①解析 Agent 漏收 `@RestControllerAdvice` 這類全域生效類別、③架構設計 Agent 沒有 `ResponseEntity<T>` 型別對應、05a schema 生成的跨檔案參照缺口、`registration`/`grading` 排程卡住的真正原因），在①③（Parse／Design Agent，05a/05b 範圍）新增對應機制後，對兩個真實專案重新跑一次完整 ①③[P]④⑤⑥。四項修正（`_global` 全域類別收集、全域例外處理生成與正確註冊、`ResponseEntity → Response` 型別對應、`blocked_reasons` 診斷欄位）**在真實環境中全數確認有效**——容器內直接觀察到 Starlette 正確呼叫到生成的全域例外處理函式、`registration.py` 的 schema 自我完備、`voice_2`／`image_2` 正確產出可執行的 `Response` 型別程式碼。這輪重跑之後，`file`／`grading`／`registration` 剩餘的失敗已能明確歸類為⑤翻譯品質問題（不再是架構層的不確定地帶），不屬於 09a/09b 範圍。完整重現過程、四項修正的程式碼位置與回歸測試見 `09b_bug_trace.md`「第三次完整重跑」一節（04a/05a/06a 範圍，不在本文件重複）。

**兩輪之間補的兩個回歸測試**（稽核發現原本沒鎖住）：`tests/python_service/test_process.py` 的 `test_start_builds_docker_run_command_with_add_host_and_rewritten_database_url`（鎖住 `--add-host` 旗標與 DB 位址改寫真的出現在 `docker run` 指令裡）、`test_container_can_reach_host_database_via_host_docker_internal`（真實容器對 `TEST_DB_DSN` 執行 `SELECT 1`，缺少時自動跳過）。

`fixtures/golden/` 底下留有兩輪真實錄製的 golden output；`exam-platform-api` 的手動修正留在該專案自己的 working tree（未 commit，使用者自己的獨立 repo，不代為決定是否保留）。

---

## 十、已知限制與待驗證事項

- [ ] **容器內的依賴集合是刻意簡化，不是完整的依賴管理方案**：`PythonServiceContainer` 目前只安裝 `BASELINE_PACKAGES`（FastAPI／uvicorn／SQLAlchemy／psycopg2-binary／`python-multipart`，見四章，`python-multipart` 是 2026-08-26 真實環境重跑才發現的必要基線套件，見 `docs/09b_bug_trace.md`），若目標專案實際還需要更多套件（如 alembic），容器會在 import 階段直接失敗——目標專案本身的依賴清單（`requirements.txt`）如何產生、如何餵給容器，目前完全沒有文件涵蓋，需要 05a／08a 或另一份操作文件補齊
- [ ] **Docker 是新的環境前提，尚未寫進 00 五章「環境建立」**：目前只記錄在本文件與程式碼註解裡，00 五章目前完全沒有提到 Python 目標服務的啟動方式
- [ ] **`SERVICE_READY_TIMEOUT_SECONDS` 正式預設值未定案**：程式碼目前仍是 `120` 秒／輪詢間隔 `2` 秒，第二輪重跑診斷用 300 秒才穩定不逾時（見九章），是否要把預設值正式調高、或改成依批次 task 數量動態計算，尚未決定
- [ ] **`scaffold_skipped` 是否該讓整條 pipeline 提早 `give_up`**：留給 `10a_debug_agent_architecture.md`（待建立）評估，本次未變動
- [ ] **`file`／`grading`／`registration` 三個模組尚未達到完整通過**：第三輪重跑已確認架構層根因（①③的既有缺口）全數解決，剩餘失敗明確歸類為⑤翻譯品質問題（如 `handle_all` 函式本體 import 幻覺模組、`voice`／`image` 業務邏輯分支跟 Java 不完全一致），細節見 `09b_bug_trace.md`「第三次完整重跑」，不屬於 09a/09b 範圍，這裡只記錄「尚未通過」這個事實
- [ ] **mutation collection 路徑完全沒有被三輪完整重跑觸及**：#1／#2／#3／#20 的修正對 mutation 端的影響仍是推論，不是實測，見 `09b_bug_trace.md`「待決定事項」

單元測試層級已驗證：`should_run_tests_or_give_up()` 的分流邏輯、`scaffold_done=False` 短路、`scaffold_done=True` 且 `module_list` 為空時不誤觸發短路、`ensure_reload_probe_infra()` 的 git 行為（首次 commit、冪等、precondition 相容性）、`PythonServiceContainer` 的 DB 位址改寫與 port 解析、`run_newman()` 的變數名稱與執行檔解析。**整合測試層級已驗證**（真實 Docker 容器，非 mock）：探測端點連線、token round-trip、修改程式碼觸發真正的容器內 reload 並正確等到新 worker。**端對端層級已驗證**（真實 Java／Python 專案、真實 PostgreSQL、真實 Docker 容器，見九章）：三輪完整的錄製→容器化啟動→驗證鏈路，`school` 模組完整通過，第三輪確認①③架構修正全數有效。全部 478 項測試（含既有）通過，另有 4 項需要真實 `TEST_DB_DSN` 才會執行，缺少時自動跳過。

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
