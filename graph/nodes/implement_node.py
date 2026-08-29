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
確認容器化（Linux）能穩定繞開這個問題。單例本身（模組層級，比照
`MODEL_SEMAPHORE` 的既有模式）下沉在 `python_service/manager.py`（見
10a 八章），這裡只是啟動／關閉的呼叫端：在整條 graph run 第一次進入
`implement` 時建立，`implement`／`run_tests`／`debug → implement`
重入期間持續共用同一個容器（見 09a 三章「Python 服務只啟動一次」），
由 `main.py` 在 `graph.ainvoke()` 結束後呼叫 `stop_python_service()`
統一關閉。
"""
import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

import httpx
import yaml

from graph.scheduler import ModuleScheduler
from graph.state import RefactorState, TaskFailure, TaskSpec
from python_service import manager as python_service_manager
from refactor_harness.fixtures.db_env import DbEnvironment
from refactor_harness.verifier.comparator import GoldenVerifier
from translator_cli import client as translator_cli
from translator_cli.types import FillResult

logger = logging.getLogger(__name__)

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
# 對應 docs/09b_bug_trace.md：_RESULT_FACTORY_INSTANCE_METHOD_NOTICE
# 只用文字描述 ResponseResult／Result 的正確呼叫慣例（如「範例是
# result.msg = msg」），但這個 session 三次真實完整 pipeline 重跑，
# `app/core/exception_handlers.py` 三次都用錯 ResponseResult.error() 的
# 關鍵字參數名稱（如寫成 message= 而不是 msg=）——追查發現這個檔案屬於
# 「全域」模組，不落在 _ENUMS_FILE 那個 if 分支涵蓋的 services／
# repositories 層，context_files 裡從來沒有真的帶上 common_service.py
# 本身，⑤ 只看得到文字提示、看不到真實原始碼可以核對確切的參數名稱，
# 只能用常見英文詞猜（"message" 比 "msg" 更像自然語言，但這個專案的
# 既有慣例是 "msg"）。跟 _RESULT_FACTORY_INSTANCE_METHOD_NOTICE 一樣
# 無條件套用，不看 target_file 落在哪一層——任何層級的 task 都可能
# 呼叫 ResponseResult／Result，提示文字既然無條件疊加，真實原始碼也該
# 無條件一起帶，不能只顧著讓 ⑤「看得到規則」卻看不到「規則描述的對象
# 長什麼樣子」。
_COMMON_SERVICE_FILE = "app/services/common_service.py"
# 見 09a 五章「缺口一：ORM relationship() 缺失」固定提示文字，逐字沿用設計文件內容。
_RELATIONSHIP_GAP_NOTICE = (
    "本專案的 SQLAlchemy model（app/models/{module}.py）只有外鍵純量欄位，"
    "沒有 relationship() 物件導覽屬性（見 08a_scaffold_agent_architecture.md "
    "八章）。禁止用「.關聯屬性」的方式取得關聯物件（例如 order.user 這種寫法"
    "一定會在執行期拋出 AttributeError）；需要關聯資料時，改用額外的 "
    "repository 查詢，或在這個函式內手動用外鍵欄位值另外查詢、自行組裝回傳"
    "結構。"
)
# 見 docs/09b_bug_trace.md「反覆出現的翻譯模式錯誤」：Java 端
# ResponseResult<T>/Result<T> 的 ok()／error()／success() 這些方法在
# Java 原始碼裡是 static 工廠方法（可以直接 ResponseResult.ok(x) 呼叫），
# 但④骨架生成階段（05a 四章「多載方法的處理」既有機制）並不區分 Java
# 的 static／instance 修飾詞，一律渲染成帶 self 的一般 instance method
# （見 app/services/common_service.py::ResponseResult／Result 的實際
# 產出）——直接照 Java 原始碼的寫法呼叫 ResponseResult.ok_2(data) 在
# Python 端會是 TypeError（缺 self），必須先實例化再呼叫，例如
# ResponseResult().ok_2(data)。已重複出現三次獨立案例（exam_service.py
# ::create_random() 呼叫 Result.ok(rs)；file_router.py::voice()／
# image() 呼叫 ResponseResult.ok_2(...)／error_4(...)），跟 relationship
# 缺口不同的是：任何層級（routers／services／repositories）的 task 都
# 可能建構這類回應物件，不像 relationship 只在 services／repositories
# 導覽關聯資料時才會踩到，因此下面 _augment_task_io() 對所有 task 一律
# 疊加，不像 _RELATIONSHIP_GAP_NOTICE 只在 services／repositories 層才加。
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
# 見 docs/09b_bug_trace.md 新增條目：Java 端 FileController 的 voice／image
# 上傳下載方法寫死 Windows 磁碟機代號絕對路徑 Paths.get("C:/voice")／
# Paths.get("C:/images")，⑤ 忠實翻譯成 Python 字面字串後，在 Linux 容器
# 內 "C:" 不是磁碟機代號、會被當成一般相對路徑目錄名稱，容器啟動時的
# bind-mount 專案根目錄底下因此真的生出一個叫 "C:" 的資料夾，讓 git
# 工作目錄變髒，拖垮同一輪後續所有 ⑤／⑦ 寫回動作（07a 設計的「乾淨工作
# 樹」前置檢查正確攔下，但攔下的時機已經太晚）。只限定在這一個目標檔案
# 才疊加——這是這個 Java 專案這兩個 method 特有的寫死路徑，不是普遍規則
# （見決策：只修目標專案本身，不在 global_infra.py 新增通用掃描機制）。
_FILE_ROUTER_FILE = "app/routers/file_router.py"
_HARDCODED_UPLOAD_PATH_NOTICE = (
    "Java 原始碼的 voice／image 上傳下載方法把儲存目錄寫死成 Windows 磁碟機"
    "代號絕對路徑（Paths.get(\"C:/voice\")、Paths.get(\"C:/images\")）——"
    "不要在 Python 端逐字翻譯成 \"C:/voice\"、\"C:/images\" 這種字面字串。"
    "這個專案跑在 Linux 容器內，\"C:\" 不是磁碟機代號，會被當成一般相對"
    "路徑目錄名稱，在容器 bind-mount 的專案根目錄下真的生出一個叫 \"C:\" "
    "的資料夾。改成在函式內 local import（跟這個檔案裡其他讀取 app.core."
    "config 的既有寫法一致，例如 from app.core.config import "
    "VOICE_UPLOAD_DIR），用 VOICE_UPLOAD_DIR／IMAGE_UPLOAD_DIR 取代對應的"
    "字面路徑字串——不要放在檔案頂層 import，那會讓 app/main.py 啟動時"
    "就強制載入 app.core.config，任何沒有這兩個環境變數的啟動情境（例如"
    "局部驗證工具）都會直接啟動失敗。"
)


def _augment_task_io(task: TaskSpec) -> tuple[str, list[str]]:
    """對應 09a 五章「疊加規則」：只對 services／repositories 層疊加
    relationship／enum 固定提示，routers 層原樣返回。不修改 task 本身
    （[P] 的權威輸出），只回傳疊加後的 context／context_files 給呼叫端
    傳給 fill_function()。

    ⑦ Debug Agent 上一輪針對這個 task 給的修正不經過這裡——10a 八章
    「⑦ 直接產生修正後程式碼」之後，`pending_fixed_bodies` 裡的內容是
    完整程式碼，直接傳給 `fill_function()` 的 `fixed_body` 參數取代整個
    函式本體，不是疊加進 context 給 ⑤ 本地模型參考（見 `_run_one_task()`）。
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

    # 見 _RESULT_FACTORY_INSTANCE_METHOD_NOTICE：跟上面的 relationship
    # 提示不同，任何層級的 task 都可能建構 ResponseResult／Result 回應，
    # 無條件疊加，不看 target_file 落在哪一層。
    context = f"{context}\n\n{_RESULT_FACTORY_INSTANCE_METHOD_NOTICE}" if context else _RESULT_FACTORY_INSTANCE_METHOD_NOTICE

    # 見 _COMMON_SERVICE_FILE：跟上面的提示一樣無條件疊加，不看
    # target_file 落在哪一層——任何 task 都可能呼叫 ResponseResult／
    # Result，只給文字規則描述參數慣例不夠，⑤ 需要看到真實原始碼才能
    # 核對確切的參數名稱，不能只能用猜的。這一層 services／repositories
    # 已經把自己複製過 context_files（見上面的 if 分支），所以這裡才能
    # 安全地直接 append，不會動到 task 本身的 target_files。
    if target_file != _COMMON_SERVICE_FILE and _COMMON_SERVICE_FILE not in context_files:
        context_files = list(context_files)
        context_files.append(_COMMON_SERVICE_FILE)

    # 見 _HARDCODED_UPLOAD_PATH_NOTICE：只限定 file_router.py 才疊加。
    if target_file == _FILE_ROUTER_FILE:
        context = f"{context}\n\n{_HARDCODED_UPLOAD_PATH_NOTICE}"

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


async def _run_one_task(
    task: TaskSpec, python_project_path: str, run_id: str, pending_fixed_bodies: dict[str, str]
) -> FillResult:
    """`pending_fixed_bodies` 裡有這個 task 的 id 時，代表 ⑦ Debug Agent
    已經給出修正後的完整函式本體（見 10a 八章「⑦ 直接產生修正後程式
    碼」），直接傳給 `fill_function()` 的 `fixed_body`，完全跳過 ⑤ 本地
    模型呼叫——不需要再組 context／context_files 給模型參考。
    """
    fixed_body = pending_fixed_bodies.get(task["id"])
    context, context_files = _augment_task_io(task)
    referenced_functions = [
        (ref["file_path"], ref["class_name"], ref["function_name"])
        for ref in task.get("referenced_functions", [])
    ]
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
            run_id=run_id,
            referenced_functions=referenced_functions,
            fixed_body=fixed_body,
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


# _python_service 單例本身下沉到 python_service/manager.py（見 10a 八章
# 「診斷資料改走 State，不是 debug_agent/ 直接 import implement_node」）
# ——這裡只是委派呼叫，不再自己持有這個變數；`refactor_harness/
# langgraph_nodes/test_nodes.py`（⑥）健康檢查失敗時也需要讀取同一個
# 容器的診斷資料，因此不能讓這個單例只屬於這個 node 檔案。
async def _ensure_python_service_started(state: RefactorState) -> None:
    # 對應 docs/09b_bug_trace.md #46：把③輸出的 config_env_vars（見
    # graph/state.py PythonStructure.config_env_vars）跟 java_project_path
    # 一併傳給 manager，讓它在真正啟動容器前解析出 Java 端 application-
    # {profile}.properties 的實際值，當額外 -e 環境變數注入。沒有任何
    # @Value 欄位的專案 state["python_structure"] 不會有這個 key，
    # .get(...) 回傳 None，manager.ensure_started() 據此不做任何事，
    # 行為等同這個機制不存在。
    await python_service_manager.ensure_started(
        state["python_project_path"],
        state["python_base_url"],
        java_project_path=state["java_project_path"],
        config_env_vars=state["python_structure"].get("config_env_vars"),
    )


async def stop_python_service() -> None:
    """main.py 在整條 graph run 結束（不論成功或失敗）時呼叫一次，關閉
    並移除容器——Docker 容器不會隨 Python process 結束自動清理。
    """
    await python_service_manager.stop()


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
    # 見 docs/09b_bug_trace.md #41：module 一旦所有 task 都已完成，重建
    # scheduler 後不會再被排進 touched_modules，"failed" 狀態必須跟
    # "verified" 一樣明確恢復，否則會預設落回 "pending" 被誤判成
    # blocked_modules，讓 debug ↔ implement 陷入不會終止的迴圈。
    already_failed_modules = {
        module for module, status in latest_module_status.items() if status == "fail"
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

    # 見 docs/09b_bug_trace.md：09a 七章原本只把 scaffold_gap_task_ids
    # 當成 already_failed，理由是「這次還沒重試過的翻譯品質失敗」不該被
    # 永久排除——這條規則在 10a 落地、⑦ Debug Agent 開始接手除錯迴圈之後
    # 沒有跟著修正，導致排程器每一輪重建時，仍然把所有 fill_failed 的
    # task 當成全新、可以再排給 ⑤ 本地模型的 task，即使 ⑦ 這一輪完全沒
    # 分析到它。真實環境重跑證實：這正是同一批 task 連續 3 輪、9 次
    # attempt 全部透過 Ollama 重試、卻從未真正交給 ⑦ 的機制性原因——
    # 不是 prompt 沒要求 ⑦ 出手，是排程器本身就會把它排回 ⑤，跟 ⑦ 有沒有
    # 出手無關。
    #
    # 10a 的既有決策（⑤ 的本地模型一旦進入除錯迴圈就完全退出）優先於
    # 09a 這條更早、範圍更窄的規則：一個 task 只要曾經在 task_failures
    # 留下一筆 reason=="fill_failed"，就永久排除在排程器的「可排給 ⑤」
    # 池之外，唯一能讓它重新被排到的路徑是 force_reschedule()（見下方，
    # 只由 ⑦ 產生的 pending_fixed_bodies 觸發）——跟 scaffold_gap 同一種
    # 「機械永久排除、只由更高權限的機制解除」的處理方式，不需要另外的
    # 資料結構，直接併入同一個 already_failed 集合。
    fill_failed_task_ids = {
        f["task_id"] for f in state.get("task_failures", []) if f["reason"] == "fill_failed"
    }

    scheduler = ModuleScheduler(
        state["module_list"],
        state["task_list"],
        already_completed=set(state.get("completed_tasks", [])),
        already_failed=scaffold_gap_task_ids | fill_failed_task_ids,
        already_verified_modules=already_verified_modules,
        already_failed_modules=already_failed_modules,
    )

    # 見 10a 八章「新增：ModuleScheduler.force_reschedule()」：
    # pending_fixed_bodies 可能指向一個 module_status=="verified"／
    # "failed" 的 module（⑤ 局部驗證誤判為 verified、但 ⑥ 全量驗證抓到
    # 真正問題；或模組已經被判定 failed，⑦ 給出可修的程式碼）——這兩種
    # 狀態的模組，get_ready_tasks() 都不會再排到它們的 task。這裡把對應
    # module 打回 pending，並把被指名的 task_id 從 task_done／task_failed
    # 移除，讓它真的能被重新排程、直接套用 ⑦ 給的 fixed_body。這一段對
    # 所有 pending_fixed_bodies 一視同仁，不區分是哪一種 origin 產生的
    # 修正。
    pending_fixed_bodies = state.get("pending_fixed_bodies", {})
    tasks_to_reopen_by_module: dict[str, set[str]] = {}
    for task_id in pending_fixed_bodies:
        task = next((t for t in state["task_list"] if t["id"] == task_id), None)
        if task is not None:
            tasks_to_reopen_by_module.setdefault(task["module"], set()).add(task_id)
    for module, task_ids in tasks_to_reopen_by_module.items():
        scheduler.force_reschedule(module, task_ids)

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
                "round": state.get("retry_count", 0),
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
        await _ensure_python_service_started(state)

    completed, failed = [], []
    task_failures: list[TaskFailure] = list(new_scaffold_gap_failures)

    # 見 10a 八章「⑦ 直接產生程式碼機制的 phase 2」：pending_file_fixes
    # 是檔案層級的修正（如 import 敘述），fill_function() 的 AST 函式
    # 定位機制碰不到，改用 translator_cli.apply_file_fix() 的精確字串
    # 替換。這些修正不對應任何要重新生成的函式本體，不透過排程器（不會
    # 出現在 get_ready_tasks() 裡）；套用完直接顯式等一次服務重啟，不
    # 依賴下面 while 迴圈「有其他 task 在跑」才會觸發的既有
    # reload-wait 時機——若這裡沒有其他 task 可以當「順風車」，file_fix
    # 寫入磁碟後就不會有任何東西觸發 reload-wait，⑥ 下一輪的全量驗證
    # 可能讀到還沒 reload 的舊服務狀態。
    file_fix_applied = False
    for file_fix in state.get("pending_file_fixes", []):
        result = await translator_cli.apply_file_fix(
            python_project_path=state["python_project_path"],
            task_id=file_fix["task_id"],
            target_file=file_fix["target_file"],
            old_snippet=file_fix["old_snippet"],
            new_snippet=file_fix["new_snippet"],
        )
        if result.success:
            file_fix_applied = True
        else:
            task = next((t for t in state["task_list"] if t["id"] == file_fix["task_id"]), None)
            if task is not None:
                task_failures.append(_make_task_failure(task, "file_fix_failed", result.error or ""))
    if file_fix_applied:
        # 逾時不視為這幾個 file_fix 失敗（比照 09a 三章「逾時不該讓整條
        # pipeline 崩潰」的既有精神）——這裡只是盡量給服務重啟的時間，
        # 「是否真的修好」交給 ⑥ 下一輪的全量驗證判斷，不是這裡的職責。
        await _wait_for_service_reload(state["python_project_path"], state["python_base_url"])

    while not scheduler.all_done():
        ready = scheduler.get_ready_tasks()
        if not ready:
            break  # 沒有可執行的 task：全部做完、卡在失敗的上游 module，或還有 needs_reverify 待處理

        # 排程層可以同時把多個就緒 task 丟進 gather，
        # 但 MODEL_SEMAPHORE(1) 保證同一時間只有一個真的在呼叫本地模型
        # （pending_fixed_bodies 命中的 task 不呼叫本地模型，直接套用
        # ⑦ 給的程式碼，但仍然共用同一個 semaphore、同一個 gather，不需要
        # 另外分流）。
        results = await asyncio.gather(
            *(_run_one_task(t, state["python_project_path"], state["run_id"], pending_fixed_bodies) for t in ready)
        )

        touched_modules = set()
        upstream_degraded = False
        for task, result in zip(ready, results):
            scheduler.mark_task_done(task, result.success)
            touched_modules.add(task["module"])

            # 心跳 log：對應 docs/09b_bug_trace.md #34——這次真實重跑卡住時，
            # log 完全安靜超過一小時，因為 task 成功時原本什麼都不印（只有
            # 失敗／重試才有 log），沒辦法只靠「log 有沒有新東西」判斷是
            # 正常在跑還是卡死。每個 task 做完（不論成功失敗）都固定印一行，
            # 讓「長時間沒有這行」變成一個對 implement 階段也有效的卡住訊號。
            logger.info(
                "task %s %s（module=%s）", task["id"],
                "成功" if result.success else "失敗", task["module"],
            )

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
                if result.upstream_degraded:
                    upstream_degraded = True

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
                        "round": state.get("retry_count", 0),
                        "report": {"status": "fail", "reason": "batch_reload_timeout", "regression": False},
                    })
                    scheduler.mark_module_verified(module, passed=False)
                for module in reverify_needs_wait:
                    partial_reports.append({
                        "module": module,
                        "round": state.get("retry_count", 0),
                        "report": {"status": "fail", "reason": "batch_reload_timeout", "regression": True},
                    })
                    scheduler.mark_module_verified(module, passed=False)
            else:
                for module in verify_needs_wait:
                    report = await _partial_verify(module, db, verifier)
                    report["regression"] = False
                    partial_reports.append({"module": module, "round": state.get("retry_count", 0), "report": report})
                    scheduler.mark_module_verified(module, passed=report["status"] == "pass")
                for module in reverify_needs_wait:
                    report = await _partial_verify(module, db, verifier)
                    report["regression"] = True
                    partial_reports.append({"module": module, "round": state.get("retry_count", 0), "report": report})
                    scheduler.mark_module_verified(module, passed=report["status"] == "pass")

        if upstream_degraded:
            # 見 translator_cli/exceptions.py::TranslatorCliUpstreamDegradedError、
            # docs/09b_bug_trace.md #35：連續多個 task 各自獨立地在傳輸層
            # 失敗，懷疑是上游 ollama／nginx 服務本身異常，不是個別 task
            # 的暫時性問題——這一輪已經觸發的驗證（跟 ollama 無關，是打
            # 本地 Python 服務的 harness 驗證）照常做完、真正完成的
            # module 不會卡在 in_progress，但不再開始下一輪
            # get_ready_tasks()，不要繼續逐一燒重試預算。還沒被排到的
            # task 維持在排程器的 pending 狀態，對應 module 會自然落在
            # 下方的 blocked_modules 分類（見下方「while 迴圈跳出的兩種
            # 可能」說明），不需要額外處理。
            logger.error(
                "偵測到疑似上游模型服務異常（連續多個 task 在傳輸層失敗），"
                "提早停止後續 task，建議先確認 ollama／nginx 服務健康狀態"
            )
            break

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
