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
└── process.py            # PythonServiceContainer：docker run 啟動、就緒判定、關閉
```

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
    missing = [name for name in (WRAPPER_FILE_NAME, TOKEN_FILE_NAME) if name not in existing_lines]

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
BASELINE_PACKAGES = ("fastapi", "uvicorn[standard]", "sqlalchemy", "psycopg2-binary")

DOCKER_IMAGE = "python:3.12-slim"


class PythonServiceStartupTimeout(RuntimeError):
    def __init__(self, message: str, *, diagnostics: str = ""):
        super().__init__(message)
        self.diagnostics = diagnostics


class PythonServiceContainer:
    def __init__(
        self, *, python_project_path: str, base_url: str, database_url: str,
        container_name: str = "refactor_python_service",
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self.python_project_path = str(Path(python_project_path).resolve())
        self.base_url = base_url.rstrip("/")
        self.database_url = database_url
        self.container_name = container_name
        self.startup_timeout = startup_timeout
        self.poll_interval = poll_interval
        self.shutdown_grace = shutdown_grace
        self._started = False

    def start(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True, text=True)

        pip_install = " ".join(BASELINE_PACKAGES)
        cmd = [
            "docker", "run", "-d", "--name", self.container_name,
            "-v", f"{self.python_project_path}:/srv", "-w", "/srv",
            "-p", f"{self._port()}:8000",
            "-e", f"DATABASE_URL={self.database_url}",
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
            self.stop()
            raise PythonServiceStartupTimeout(
                f"Python 服務容器在 {self.startup_timeout} 秒內未能就緒",
                diagnostics=diagnostics,
            )

    def _port(self) -> str:
        return str(urlparse(self.base_url).port or 8000)

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

五、六章的 Docker 驗證只證明了「熱重載同步屏障」這一個機制對一個最小 FastAPI 玩具 app 有效。這不足以證明 09b 接上的 `DbEnvironment`／`GoldenVerifier` 真的能對一個真實專案跑出有意義的結果——這兩件事是不同層級的驗證，前者測的是「等待邏輯對不對」，後者測的是「Harness 錄製／驗證這條鏈路本身有沒有埋著別的 bug」。本章記錄對這個 repo 既有的真實環境（`../lang-exam-api-refactor` Java 專案、`../exam-platform-api` Python 目標專案——已有 07b 真實驗證留下的 69 個 `fill_function()` commit、`postman/collection_readonly.json`、可連線的 `MOC_MATSUEXAM_TEST`）實際跑一次「① 啟動 Java → ② 錄製 golden → 啟動容器化 Python 服務 → ⑥ 用 `GoldenVerifier` 驗證」的完整鏈路，過程中發現並修正四個既有（02b）程式碼裡從未被真實環境驗證過的缺陷：

### 1. `newman` 在 Windows 上無法被 `subprocess.run(["newman", ...], shell=False)` 找到

npm 全域安裝的 `newman` 在 Windows 上是 `newman.cmd`（batch wrapper），`subprocess.run` 不帶 `shell=True` 時不會自動嘗試附加副檔名去 PATH 上找——即使命令列打字執行 `newman` 完全正常。修正：`core/postman_runner.py` 改用 `shutil.which("newman")` 解析完整路徑，找不到時直接拋出明確錯誤，不依賴 `shell=True`（避免字串注入風險）。

### 2. `golden_writer.py` 讀錯 newman JSON report 的 header 欄位

`_build_golden()` 原本讀 `response["headers"]["members"]` 來取得 `Content-Type` 判斷是否為 JSON response——但真實 newman 6.2.2 的 JSON reporter 欄位是 `response["header"]`（單數），值是純陣列 `[{"key":...,"value":...}, ...]`，不是包一層物件的形狀。舊寫法永遠讀到空字典，`content_type` 恆為空字串，**每一筆 response 都被誤判成非 JSON 而跳過**——對真實 Java 服務錄製 `collection_readonly.json`（10 個 endpoint）實測，修正前 `recorded_count=0`，修正後 `recorded_count=9`（唯一被跳過的 `get_version` 本來就回傳 `text/plain`，是正確行為）。既有單元測試（`tests/refactor_harness/test_golden_writer.py`）的 mock fixture 沿用同一個錯誤的欄位形狀，跟程式碼「互相印證」了這個 bug，一併修正。

### 3.（最關鍵）`--env-var` 傳的變數名稱跟 collection 實際變數名稱對不上

`run_newman()` 呼叫 `newman run ... --env-var base_url={base_url}`——但 `postman/collection_*.json` 頂層 `variable` 陣列裡實際定義的變數是 `baseUrl`（駝峰式）：

```json
{"variable": [{"key": "baseUrl", "value": "http://localhost:8080"}]}
```

名稱對不上時，`--env-var` 完全不會生效，newman 一律 fallback 回 collection 內建的預設值——**這個預設值剛好是 Java 的網址**，所以錄製對 Java 端「碰巧」正確，長期掩蓋了這個 bug；但驗證對 Python 端時，不論傳入什麼 `base_url`，newman 實際上永遠打的是 `http://localhost:8080`（Java），**`GoldenVerifier`／`MutationVerifier`（⑥ 測試執行 Agent 的核心）自始至終從未真正驗證過 Python 服務**。這是本次端對端驗證裡影響範圍最大的發現，不是 09a/09b 的邏輯設計有問題，是它們共同依賴的底層 `run_newman()` 從一開始就沒有被接上真實 Python 服務測試過。修正：`--env-var` 改傳 `baseUrl={base_url}`；`tests/refactor_harness/test_postman_runner.py` 新增回歸測試鎖住這個變數名稱。

### 4. 容器內的 `127.0.0.1` 不是 host 的 `127.0.0.1`

`PythonServiceContainer` 一開始直接把 `.env` 的 `DATABASE_URL`（從 host 角度寫的，指向 `127.0.0.1:5432`）原樣傳進容器──但容器內的 `127.0.0.1` 指向容器自己，連不到跑在 host 上的測試 DB。修正：新增 `_container_database_url()` 把 `127.0.0.1`／`localhost` 換成 `host.docker.internal`，並在 `docker run` 加上 `--add-host=host.docker.internal:host-gateway`（實測部分 Docker Desktop 版本不會自動提供這個 DNS 名稱，需要顯式指定）。已用真實 `postgres:17-alpine` 容器驗證連線成功，並見下方「測試涵蓋度稽核」補上的自動化測試。

### 5. `resolve_body_imports()` 把「同一檔案自己定義的 class」誤判成要匯入自己

四項修正後第一次對真實 Python 服務跑驗證，10 個 case 全部沒過，錯誤都是 `NameError`／`AttributeError`（見下方「驗證結果」的分類）。追查 `app/routers/school_router.py` 的 `NameError: SchoolRepository` 時，用**目前（修正後）的** `resolve_body_imports()` 直接對這個真實檔案重跑一次填空階段的 import 解析，確認現在的邏輯**能**正確找出缺的 import（`from app.repositories.school_repository import SchoolRepository`）——代表這不是現在的邏輯有問題，是這份目標專案的程式碼比這個機制本身還舊（見下一節「回填與過時 fixture」）。

但用這個結果對整個 `exam-platform-api` 做批次回填時，`app/services/common_service.py` 又重新長出了六章一開始就手動移除過的那個循環 import（`from app.services.common_service import ResponseResult, Result, ValidationUtil`）——這證明**現在的** `resolve_body_imports()` 本身還有一個獨立的既有 bug，不是舊資料的殘留：這批 Java 靜態工具類常見「方法回傳自己所屬的類別」（如 `ResponseResult.error() -> ResponseResult(...)`），`_scan_project_custom_types()` 掃描整個 `app/` 目錄建索引時沒有排除「這個名稱其實就是正在處理的這個檔案自己頂層定義的 class／函式」，導致這種同檔案內自我引用被誤判成跨檔案缺 import，產生一行恆為 True 的循環 import陳述式。修正：`resolve_body_imports()` 新增排除邏輯，把 `tree.body` 自己的頂層 `ClassDef`／`FunctionDef`／`AsyncFunctionDef`／模組層級變數賦值目標，從候選名單裡剔除。`tests/translator_cli/test_scaffold.py` 新增 `test_resolve_body_imports_excludes_class_defined_in_same_file`／`test_resolve_body_imports_excludes_function_defined_in_same_file` 直接重現這個真實案例。

### 回填與過時 fixture：為什麼修完 import 之後 pass_rate 仍是 0.0

`exam-platform-api` 的 `scaffold: initial skeleton` commit時間是 **2026-08-11**，`resolve_body_imports()`（07a 五章「填空模式：本體 import 解析」）是隔天 **2026-08-12** 才加進 translator-cli（見 `git log -S resolve_body_imports`）——這個目標專案完整生成於這個機制存在**之前**，且 `app/models/` 整個不存在，代表它也早於④骨架實作 Agent（08a）具備 entity 生成能力的版本。用它驗證「現在的 pipeline 產出的程式碼對不對」，本質上是拿舊版工具的產物測新版工具，不是有效的端對端驗證。

用現在的 `resolve_body_imports()` 對這份舊專案的 `app/routers`／`app/services`／`app/repositories` 全部檔案批次重跑一次填空階段的 import 解析（純粹套用既有邏輯，沒有重新呼叫 qwen），修正 6 個檔案缺的 import 後，重新啟動容器化服務、重新驗證，`pass_rate` 仍是 `0.0`，但失敗原因這次精準地分成三類，且都不是這次（09a/09b/02b）範圍內的既有程式碼造成：

1. **`NameError: SchoolEntity`**——這個舊專案沒有 `app/models/`，entity 從未被生成過，是它比 08a 舊的直接後果，不是 import 解析漏了什麼（`SchoolEntity` 真的不存在於磁碟上任何檔案）
2. **`AttributeError: 'ExamService' object has no attribute 'exam_repository'`**（6 次）——qwen 填空時假設建構子會注入 `self.exam_repository` 這類屬性，但 07a 五章「刻意不產生 `__init__`」的骨架設計是無狀態方法容器，兩者對不上。這是**翻譯品質問題**，不是 import 解析或 Harness 鏈路的 bug——量級上「大部分函式簽名／型別／流程都對，少數幾處假設錯物件狀態」正好符合⑦ Debug Agent 設計要處理的情境（讀 `related_files`、定位、回饋給 ⑤ 重試），不是這次要修的
3. 一個 `ok_2` 被當自由函式呼叫（該寫成 `ResponseResult().ok_2(...)`）、兩個寫死 Windows 路徑（`C:/images/...`）——同樣是這份舊 fixture 殘留的翻譯瑕疵，不是現在程式碼的問題

**結論**：這次「10/10 全掛」不是單一巨大缺陷，也不是現在的鏈路廣泛失效——是舊 fixture 疊了兩層問題（過時工具版本的殘留＋缺少後來才有的 entity 生成能力），拆開來看之後，現在程式碼裡唯一真正的既有 bug（`resolve_body_imports()` 的自我引用誤判）已經修正並補了回歸測試；其餘全部屬於「這份特定 fixture 資料過舊」或「留給 ⑦ Debug Agent 處理的翻譯品質問題」，不需要在 09a/09b/02b 範圍內動任何程式碼。要用現在的 pipeline 做真正對等的端對端驗證，需要重新跑一次 scaffold（帶 08a 的 entity 生成）＋重新對每個 task 呼叫 `fill_function()`（真的呼叫 ollama），這會覆蓋掉 `exam-platform-api` 現有的 69 個驗證 commit，屬於需要另外確認才進行的範圍。

### 測試涵蓋度稽核：這次的修正跟測試是不是真的對得起這次抓到的問題

被問到「這幾項修正跟測試真的涵蓋這次檢測到的問題嗎」之後，逐項重新核對，發現兩個當時沒補齊的缺口：

- **`--add-host=host.docker.internal:host-gateway` 只有字串重寫邏輯（`_container_database_url()`）被單元測試覆蓋，沒有測試鎖住 `start()` 真正組出的 `docker run` 指令有沒有包含這個旗標**——若日後有人不小心刪掉 `process.py` 裡那一行，既有測試完全不會發現，只有跑到真實 Docker 整合測試、且那個整合測試剛好有連 DB 才會暴露。修正：`tests/python_service/test_process.py` 新增 `test_start_builds_docker_run_command_with_add_host_and_rewritten_database_url`（mock `subprocess.run`，斷言實際指令內容）與 `test_start_raises_when_docker_run_fails`。
- **既有的 Docker 整合測試（`test_reload_probe_integration.py`）用的是 `database_url="postgresql://unused/unused"`，從未真的透過容器連過 DB**——「容器內能連到 host DB」這件事，先前只有我手動用一次性的 `docker run postgres:17-alpine psql ...` 驗證過，沒有寫進自動化測試套件，代表這個修正實際上沒有回歸保護。修正：新增 `test_container_can_reach_host_database_via_host_docker_internal`，用真實 `TEST_DB_DSN` 啟動一個會實際對 Postgres 執行 `SELECT 1` 的容器化 app，斷言查詢成功——這個測試需要 `.env` 設定可連線的 `TEST_DB_DSN`，缺少時自動跳過（`_real_test_db_reachable()`），不影響套件其餘部分。已實際跑過確認通過（24 秒）。

其餘修正（`newman` 路徑解析、`golden_writer.py` header 欄位、`--env-var` 變數名稱、`resolve_body_imports()` 自我引用排除）原本就各自有直接、明確的回歸測試，稽核後判定已足夠，不需要再補。

`fixtures/golden/` 底下留有這次真實錄製的 golden output，未還原；`../exam-platform-api` 的 import 回填（6 個檔案）與 `common_service.py`／`exam_service.py` 的循環 import 移除也留在該專案的 working tree（未 commit，那是使用者自己的獨立 repo，不代為決定是否保留）。

---

## 十、已知限制與待驗證事項

- [x] **`newman`／`golden_writer.py` header 解析／`--env-var` 變數名稱四項既有缺陷**：已在九章發現並修正，附真實環境重現記錄與回歸測試
- [x] **容器內 DB 連線位址（`127.0.0.1` vs `host.docker.internal`）**：已在九章發現並修正，附真實 postgres 容器驗證與自動化測試（`test_container_can_reach_host_database_via_host_docker_internal`）
- [x] **`resolve_body_imports()` 的同檔案自我引用誤判**：已在九章發現並修正，附兩個回歸測試
- [ ] **容器內的依賴集合是刻意簡化，不是完整的依賴管理方案**：`PythonServiceContainer` 目前只安裝 `BASELINE_PACKAGES`（FastAPI／uvicorn／SQLAlchemy／psycopg2-binary，對應 00 三章已定案的技術棧），若目標專案實際還需要更多套件（如 alembic、其他第三方函式庫），容器會在 import 階段直接失敗——目標專案本身的依賴清單（`requirements.txt`）如何產生、如何餵給容器，目前完全沒有文件涵蓋，是本次落地過程中新發現的缺口，需要 05a／08a 或另一份操作文件補齊
- [ ] **Docker 是新的環境前提，尚未寫進 00 五章「環境建立」**：目前只記錄在本文件與程式碼註解裡。00 五章「環境建立」目前完全沒有提到 Python 目標服務的啟動方式（09a 十一章原本就指出這個缺口），這次補上的是「怎麼啟動、為什麼要用容器」，00 文件本身尚未同步更新
- [ ] **`SERVICE_READY_TIMEOUT_SECONDS` 等時間預算調校**：目前預設 `120` 秒／輪詢間隔 `2` 秒（`PythonServiceContainer` 另有自己的 `startup_timeout=90` 秒，涵蓋容器內第一次 `pip install` 的額外時間），屬於數字微調，需要接上真實專案規模校準
- [x] **`_reload_token.py` 是否確實落在 uvicorn `--reload` 預設監控範圍內**：已在 Docker 容器內用真實 reload 週期驗證過會被偵測到（見六章），維持 `.py` 副檔名的既有理由不變
- [ ] **`scaffold_skipped` 是否該讓整條 pipeline 提早 `give_up`**：留給 `10a_debug_agent_architecture.md`（待建立）評估，本次未變動
- [ ] **`app/services/common_service.py` 的循環 import、`school_router.py` 缺 import**：九章發現，屬於 07a/07b 填空後自動補 import 機制或更早期 fill 結果的既有缺陷，範圍外，只做了讓服務能啟動的最小修正，未深入根因

單元測試層級已驗證：`should_run_tests_or_give_up()` 的分流邏輯、`scaffold_done=False` 短路、`scaffold_done=True` 且 `module_list` 為空時不誤觸發短路、`ensure_reload_probe_infra()` 的 git 行為（首次 commit、冪等、precondition 相容性）、`PythonServiceContainer` 的 DB 位址改寫與 port 解析、`run_newman()` 的變數名稱與執行檔解析。**整合測試層級已驗證**（真實 Docker 容器，非 mock）：探測端點連線、token round-trip、修改程式碼觸發真正的容器內 reload 並正確等到新 worker。**端對端層級已驗證**（真實 Java／Python 專案、真實 PostgreSQL、真實 Docker 容器，見九章）：完整的錄製→容器化啟動→驗證鏈路，回傳結構正確、內容真實可信的 report。全部 430 項測試（含既有）通過。

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
