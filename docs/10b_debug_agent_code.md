# ⑦ Debug Agent 程式碼實作

> 本文件承接 `10a_debug_agent_architecture.md`（設計面：決策、契約、資料結構、流程），是這個 Agent 的**實作面**文件：對應每一項 10a 決策實際落地的程式碼。所有異動都對照目前 repo 的真實檔案內容（`graph/state.py`、`graph/builder.py`、`graph/nodes/implement_node.py`、`graph/nodes/debug_node.py`、`refactor_harness/` 現有程式碼）撰寫，比照真實原始碼寫出對應的 diff，不是憑空重寫——但**這些 diff 目前只存在於這份文件裡，尚未實際套用進 repo**（`git status` 可查：`comparator.py`／`test_nodes.py`／`implement_node.py`／`scheduler.py` 等既有檔案目前都還是修改前的原始內容，`debug_agent/`／`python_service/manager.py` 等新檔案也都還不存在）。這是刻意的兩階段做法，比照 09a（設計）→09b（09a 定案後，另一輪對真實檔案的實際套用＋真實環境驗證＋`09b_bug_trace.md` 記錄的落差）——本文件對應的是 09a 那個階段，還沒有走到 09b 那個「真正把 diff 套用進 repo、跑過真實測試」的階段。文件名稱雖然是「程式碼實作」，指的是「這一輪設計時已經連帶想清楚要怎麼實作」，不是「已經是 repo 的一部分」。

---

## 目錄

1. `refactor_harness/`——`module`／`related_files` 欄位補丁（`comparator.py`／`mutation_verifier.py`／`reporter.py`）＋ `run_postman_tests()` 前置健康檢查＋ `should_debug_or_done()` 修正（`langgraph_nodes/test_nodes.py`）
2. `graph/state.py`——新增 `TaskFix`／`DebugRound`／三個 `RefactorState` 欄位
3. `debug_agent/`——新增套件（`triage.py`／`llm.py`／`prompts.py`／`analysis.py`／`__init__.py`）
4. `graph/nodes/debug_node.py`——全面改寫
5. `graph/scheduler.py`／`python_service/manager.py`（新增）／`graph/nodes/implement_node.py`——`force_reschedule()` 新增方法、`_python_service` 單例下沉、`_augment_task_io()`／`_run_one_task()` 擴充
6. `graph/builder.py`——`debug` 出邊改條件邊
7. `main.py`——`initial_state` 新增欄位
8. `tests/`——新增與既有測試同步更新
9. 模組結構總覽
10. 已知限制與待驗證事項

---

## 一、`refactor_harness/`——`module` 欄位補丁

對應 10a 二章「前置修補」。三處異動都是新增一個 key，不改變既有欄位的值或既有測試的斷言。

### `refactor_harness/verifier/comparator.py`（`GoldenVerifier._process_executions()`）

```python
    def _process_executions(self, executions: list[dict]) -> list[dict]:
        results = []
        excluded: list[str] = []
        for execution in executions:
            item = execution["item"]
            actual_response = execution["response"]

            case_id = make_case_id(item)
            url_parts = item["request"]["url"]["path"]
            method = item["request"]["method"]
            module = self._get_module(method, url_parts)

            golden = self._load_golden(case_id, module)
            if golden is None:
                if case_id in self._skipped_case_ids:
                    excluded.append(case_id)
                    continue
                results.append({
                    "case_id": case_id,
                    "module": module,          # ← 新增，見 10a 二章
                    "passed": False,
                    "error": "golden_not_found",
                    # ← 新增，見 10a 二章「comparator.py 兩個早退分支補齊
                    # related_files」：這兩個早退分支原本完全沒有
                    # related_files，10a 四章的 Debug Agent context 供給
                    # 因此會拿到空清單，對 response_not_json（未處理例外
                    # 導致的 500）這種最需要看程式碼的情境影響最大。
                    "related_files": self.route_mapper.resolve_related_files(method, url_parts),
                })
                continue

            raw_body = extract_response_body(actual_response)
            if raw_body is None or raw_body.strip() == "":
                actual_body = None
            else:
                try:
                    actual_body = json.loads(raw_body)
                except (json.JSONDecodeError, TypeError):
                    results.append({
                        "case_id": case_id,
                        "module": module,      # ← 新增
                        "passed": False,
                        "error": "response_not_json",
                        "related_files": self.route_mapper.resolve_related_files(method, url_parts),  # ← 新增，見 10a 二章
                    })
                    continue
            actual_masked = self.masker.mask(actual_body, context="readonly")

            status_match = golden["response"]["status_code"] == actual_response["code"]
            diff = self.diff_engine.compare(
                expected=golden["response"]["body"],
                actual=actual_masked
            )

            results.append({
                "case_id": case_id,
                "module": module,              # ← 新增
                "passed": status_match and (diff is None),
                "expected_status": golden["response"]["status_code"],
                "actual_status": actual_response["code"],
                "status_match": status_match,
                "body_diff": diff,
                "related_files": self.route_mapper.resolve_related_files(method, url_parts)
            })

        self._last_excluded_cases = excluded
        return results
```

其餘程式碼（`__init__`／`verify`／`verify_raw`／`verify_module`／`_get_module`／`_load_golden`）不變。

### `refactor_harness/verifier/mutation_verifier.py`（`MutationVerifier._verify_one_raw()`）

同樣在 `results.append({...})` 加一行 `"module": module`——`module` 這個變數在 `_verify_one_raw()` 裡已經在迴圈開頭算好（`module = self._get_module(method, url_parts)`），有 golden／沒 golden 兩個分支都要加：

```python
            results.append({
                "case_id": case_id,
                "module": module,              # ← 新增，見 10a 二章
                "passed": status_match and (body_diff is None),
                "expected_status": expected_status,
                "actual_status": actual_response["code"],
                "status_match": status_match,
                "body_diff": body_diff,
                "related_files": self.route_mapper.resolve_related_files(method, url_parts),
            })
```

其餘程式碼不變。

### `refactor_harness/core/reporter.py`（`HarnessReporter.build_report()`）

```python
            "failures": [
                {
                    "case_id": f["case_id"],
                    "module": f.get("module"),         # ← 新增，見 10a 二章
                    "failure_type": self._classify_failure(f),
                    "status_code_match": f.get("status_match", True),
                    "expected_status": f.get("expected_status"),
                    "actual_status": f.get("actual_status"),
                    "body_diff": f.get("body_diff"),
                    "related_files": f.get("related_files", []),
                    "debug_hint": self._generate_hint(f)
                }
                for f in failed
            ],
```

用 `.get("module")` 而非 `["module"]`：`build_report()` 是通用方法，理論上呼叫端可能傳入沒有 `module` 鍵的舊格式 `results`（例如未來新增的呼叫端、或既有測試用手寫 fixture 沒補這個欄位）——沒有就回傳 `None`，不因為缺一個新增的可選欄位就整個拋例外，其餘欄位的既有容錯風格（`f.get("status_match", True)` 等）也是同一種寫法。

### 回歸測試

`tests/refactor_harness/test_comparator.py`／`tests/refactor_harness/test_mutation_verifier.py`／既有 `test_reporter.py`（若無則新增）各補一個斷言：`results[i]["module"]`／`report["failures"][i]["module"]` 等於預期的 module 名稱。**`test_comparator.py` 另外補兩個案例專門鎖住 `related_files` 缺口**：構造一個 golden 檔案不存在的 case、一個回傳非 JSON body 的 case，斷言 `results[i]["related_files"]` 等於 `route_mapper.resolve_related_files(method, url_parts)` 的預期值（不是空清單）——這是校對時抓到的既有缺口，這兩個測試案例專門防回歸。不修改任何既有斷言（純新增欄位，既有斷言只檢查特定欄位，不會因為字典多一個 key 而失敗）。

### `refactor_harness/langgraph_nodes/test_nodes.py`（`run_postman_tests()` 前置健康檢查）

對應 10a 二章「⑥ 對外呼叫前必須先確認 Python 服務有回應」。`run_newman()` 本身不動；`run_postman_tests()` 前面加一道防護：

```python
import time
import httpx

from python_service import manager as python_service_manager

# 10a 二章：只確認連線層級可達，不是 09a 三章 _wait_for_service_reload()
# 那種要比對特定 token 的同步屏障——⑤ 該做的等待已經做過，這裡只需要
# 知道「現在」連不連得上。逾時預算刻意比 09a 的 SERVICE_READY_TIMEOUT_
# SECONDS（預設 120s）短很多：這裡的目的不是「等它恢復」，是「快速
# 判斷這一輪要不要跳過 Newman」，服務若真的當機，多等也不會自己好。
RUN_TESTS_HEALTH_CHECK_TIMEOUT_SECONDS = float(
    os.environ.get("RUN_TESTS_HEALTH_CHECK_TIMEOUT_SECONDS", "15")
)
RUN_TESTS_HEALTH_CHECK_POLL_INTERVAL_SECONDS = float(
    os.environ.get("RUN_TESTS_HEALTH_CHECK_POLL_INTERVAL_SECONDS", "3")
)


def _is_service_reachable(base_url: str, timeout_seconds: float, poll_interval: float) -> bool:
    """只檢查連線層級是否可達，不檢查回應內容或狀態碼——即使服務回
    404／500，只要連得上就代表這不是「worker 崩潰、連線被拒絕」的情境，
    交給 run_newman() 正常執行、讓既有的比對邏輯去發現真正的問題。
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            httpx.get(base_url, timeout=5.0)
            return True
        except httpx.HTTPError:
            pass
        time.sleep(poll_interval)
    return False


_UNREACHABLE_TEST_RESULTS = {
    "summary": {"total": 0, "passed": 0, "failed": 0, "pass_rate": 0},
    "status": "fail",
    "reason": "service_unreachable",
    "failures": [],
    "passed_cases": [],
    "excluded_folders": [],
    "excluded_cases": [],
}


def run_postman_tests(state: RefactorState) -> RefactorState:
    """
    readonly 與 mutation 的原始結果合併後只呼叫一次 build_report()。

    **新增的前置健康檢查**（10a 二章）：09a 三章「批次執行」描述的
    batch_reload_timeout 最常見成因（某個 task 寫入的程式碼有模組層級
    匯入／語法錯誤，讓容器內的 uvicorn worker 崩潰）在 implement 結束
    時可能還沒解決，Python 服務容器可能仍處於連不上的狀態（見 09b 六章
    「Python 服務只啟動一次」——⑤／⑥ 共用同一個持續運行的容器，⑤沒有
    機制把它修好）。若不做這個檢查，下面的 run_newman() 會直接拋出
    RuntimeError（02a 五章既有、刻意設計的行為），讓整條 graph.ainvoke()
    崩潰，⑦ Debug Agent 永遠不會被呼叫到。

    **同一時間點順手讀取 `service_diagnostics`**（10a 二章、八章）：
    這是診斷資料最新鮮的時間點，寫進 State 供 ⑦ 之後讀取——⑦
    （`debug_agent/`）全程不 import 任何 `graph/nodes/*.py`，也不 import
    `python_service.manager` 本身，只讀 `state["service_diagnostics"]`，
    避免 node 對 node 互相依賴（見 10a 八章）。
    """
    if not _is_service_reachable(
        state["python_base_url"],
        RUN_TESTS_HEALTH_CHECK_TIMEOUT_SECONDS,
        RUN_TESTS_HEALTH_CHECK_POLL_INTERVAL_SECONDS,
    ):
        return {
            **state,
            "test_results": dict(_UNREACHABLE_TEST_RESULTS),
            "service_diagnostics": python_service_manager.get_diagnostics(),
        }

    db = DbEnvironment(test_dsn=state["test_dsn"])
    db.apply_seed("fixtures/seed.sql", tables_to_truncate=TABLES)

    verifier = GoldenVerifier(
        python_base_url=state["python_base_url"],
        golden_dir="fixtures/golden"
    )
    readonly_raw = verifier.verify_raw("postman/collection_readonly.json")

    mutation_verifier = MutationVerifier(
        python_base_url=state["python_base_url"],
        golden_dir="fixtures/golden",
        test_dsn=state["test_dsn"],
    )
    mutation_raw = mutation_verifier.verify_all_raw()

    report = HarnessReporter().build_report(
        readonly_raw + mutation_raw,
        excluded_folders=mutation_verifier.get_excluded_folders(),
    )

    return {**state, "test_results": report}
```

`_UNREACHABLE_TEST_RESULTS` 用 `dict(...)` 複製一份再回傳，不是直接回傳模組層級常數本身的參照——避免任何下游程式碼不小心原地修改這個字典，污染到下一次遇到同樣情況時的常數值（同一顆常數會被跨輪次重複使用）。

**與既有 `run_postman_tests()` 的差異只有開頭這一段短路判斷**：`db.apply_seed()` 之後的邏輯完全不變，`import time`／`import httpx` 是新增的模組層級 import（`httpx` 已經是既有依賴，09b `implement_node.py` 已經在用）。

### 回歸測試

`tests/refactor_harness/test_test_nodes.py`（若無則新增，這個檔案原本沒有專屬測試）：`_is_service_reachable()` 對真實可連線／連線被拒兩種情境的判斷（mock `httpx.get`）；`run_postman_tests()` 在服務不可達時回傳的 `test_results` 是否正確帶有 `reason="service_unreachable"`、且**沒有**呼叫到 `run_newman()`（mock 掉 `GoldenVerifier`／`MutationVerifier`，斷言它們的建構子／方法完全沒被呼叫，證明真的短路了，不是跑到一半才失敗）；`state["service_diagnostics"]` 正確等於 `python_service_manager.get_diagnostics()` 的回傳值（mock 掉，斷言真的有被呼叫且值有寫進回傳的 state）。

### `refactor_harness/langgraph_nodes/test_nodes.py`（`should_debug_or_done()` 修正）

對應 10a 七章「路由方式：`retry_count` 上限檢查前移到 `should_debug_or_done()`」。取代既有函式：

```python
def should_debug_or_done(state: RefactorState) -> str:
    """
    retry_count 上限檢查對所有分支統一套用，不再依 failed_modules 是否
    為空分岔（見 10a 七章「為什麼要統一套用」）——這條分支原本存在的
    理由是「blocked_modules 只是排程還沒排到，不算真正失敗」，但 10a
    六章已經證明這個理由在目前 graph 的實際拓樸下不成立：能讓 debug
    被觸發，test_results.status == "fail" 已經是既成事實，failed_modules
    是否非空只反映 ⑤ 局部驗證（readonly-only）看不看得到問題，不是
    「有沒有真的壞」的可靠依據，不該拿來決定要不要检查 retry_count 上限。
    """
    report = state["test_results"]
    if report["status"] == "pass":
        return "done"
    if state["retry_count"] >= MAX_RETRY:
        return "give_up"
    return "debug"
```

**這是對既有函式的簡化，不是新增邏輯**：拿掉 `failed_modules = state.get("failed_modules", [])` 與 `if not failed_modules: return "debug"` 這兩行，`retry_count >= MAX_RETRY` 這行的比較符號跟位置完全沿用既有（本來就在 `failed_modules` 非空分支使用、已經證明正確），只是移到最前面、對所有情況一視同仁。函式簽名、`MAX_RETRY` 常數本身、`graph/builder.py` 呼叫這個函式的既有寫法（`add_conditional_edges("run_tests", should_debug_or_done, {...})`）完全不用改。

### 回歸測試

`tests/refactor_harness/test_test_nodes.py`（同上方檔案）新增／更新 `should_debug_or_done()` 的測試：`test_results.status=="fail"` 且 `failed_modules` 為空、`retry_count` 已達 `MAX_RETRY` 時必須回傳 `"give_up"`（既有測試若原本只涵蓋 `failed_modules` 非空的情境，需要補上這個 `failed_modules` 為空的案例，鎖住這次修正的行為）。

---

## 二、`graph/state.py`——新增 `TaskFix`／`DebugRound`／四個 `RefactorState` 欄位

對應 10a 六章、二章、八章。插入位置：`TaskFix`／`DebugRound` 放在 `TaskFailure` 之後、`TaskSpec` 之前（Agent ⑦ 輸出的子型別）；`RefactorState` 新增欄位放在 `test_results` 之後、`retry_count` 之前（比照既有「依 Agent 順序」排列慣例）。

```python
# ── Agent ⑦ 輸出：task 級修正指令與每輪除錯記錄（見 10a 六章）──
class TaskFix(TypedDict):
    task_id: str
    diagnosis: str
    fix_instruction: str


class DebugRound(TypedDict):
    round: int
    module: str
    origin: Literal["blocked", "module_mismatch", "scaffold_gap", "root_cause", "no_signal"]
    fixable: bool
    root_cause_summary: str
    task_fixes: list[TaskFix]
    unfixable_reasons: list[str]
```

```python
class RefactorState(TypedDict):
    ...
    # Agent ⑥
    test_results: dict

    # Agent ⑥：⑥ 判定 Python 服務不可達時（見一章 run_postman_tests()
    # 前置健康檢查），連同 test_results 一起寫入這個欄位——容器崩潰
    # 當下的 docker log（見 python_service/manager.py::get_diagnostics()）。
    # 不掛 reducer，每輪覆寫，跟 test_results 本身的既有覆寫語意一致。
    # ⑦ 的 no_signal／crash-log 分析路徑（三章）全程只讀這個欄位，不呼叫
    # 任何跨節點函式，見 10a 八章「診斷資料改走 State」。
    service_diagnostics: str | None

    # Agent ⑦（逐輪累積寫入，需要 reducer）：每一輪 debug 對每個分析過
    # module 的完整記錄，含未呼叫 LLM 的 blocked／scaffold_gap 機械判斷，
    # 供人工事後查看、也是未來評估「要不要讓 LLM 看到上一輪建議」的既有
    # 資料來源（見 10a 十二章）。
    debug_rounds: Annotated[list[DebugRound], operator.add]

    # Agent ⑦：這一輪產出的 task 級修正指令，key 是 task_id。不掛
    # reducer，整包覆寫——這是「這一輪的建議」，不是累加事件，見 10a
    # 六章。implement_node._augment_task_io() 讀取這份資料疊加進
    # fill_function() 的 context（見 10a 八章）。
    pending_fix_instructions: dict[str, str]

    # Agent ⑦：這一輪分析後，若確認這一輪所有 root_cause module 都無法
    # 修，設為 True，供 debug_node.should_retry_or_give_up() 路由到
    # give_up，不進 implement 浪費一輪重試（見 10a 七章）。每輪覆寫，
    # 不掛 reducer。
    give_up_early: bool

    # Orchestrator
    retry_count: int
```

---

## 三、`debug_agent/`——新增套件

比照 `plan_agent/`／`design_agent/` 的既有結構慣例。

### `debug_agent/llm.py`

```python
"""⑦ Debug Agent 專屬的 Claude API 模型選擇與輸出長度上限。實際呼叫邏輯
（client 初始化、Structured Outputs、log_usage() 整合、錯誤處理）在
common/llm_client.py，所有需要呼叫 Claude API 的 Agent 共用同一份（見
00 六章）。debug_agent/ 全套件（這個檔案、analysis.py）嚴禁自行初始化
Anthropic client 或另開一條呼叫路徑——所有 Claude API 呼叫一律經
common.llm_client.call_claude_for_json()，這是 00 六章的硬性規定，不是
建議：唯有如此 log_usage() 才會被統一呼叫到，API 成本才會確實寫進
logs/claude_api_usage.jsonl，不會因為漏接而算不準（見 10a 九章）。

DEBUG_AGENT_MAX_TOKENS 比照 06a 五章 PLAN_AGENT_MAX_TOKENS 的判斷方式：
單一 module 的輸出包含 root_cause_summary、每個 task_fix 的 diagnosis／
fix_instruction（可能多筆）、unfixable_reasons，密度介於 ①③ 的分類型
輸出與 [P] 的完整 task 描述之間。第一版沿用 common/llm_client.py 的
DEFAULT_MAX_TOKENS（4096），待接上真實環境、對真實失敗案例實測回應長度
後再校準（見 10a 十二章、十章「已知限制」）——這裡先不假設某個更大的
數字，理由同 06a 五章「不是憑空猜測」的既有原則：沒有實測依據前，猜一個
比 4096 更大的數字並不比沿用預設值更有根據。
"""
from __future__ import annotations

import os

from common.llm_client import DEFAULT_MODEL_FALLBACK

DEFAULT_MODEL = os.environ.get("DEBUG_AGENT_MODEL", DEFAULT_MODEL_FALLBACK)
DEBUG_AGENT_MAX_TOKENS = int(os.environ.get("DEBUG_AGENT_MAX_TOKENS", "4096"))
```

### `debug_agent/prompts.py`

對應 10a 四章「單一 module 的 Claude 呼叫內容」全表格、4.2 輸出 schema。

```python
"""⑦ Debug Agent 用到的 Claude API system prompt 與對應 output schema，
對應 10a 四章「Module 級 LLM 分析」。schema 跟 prompt 放同一個檔案的
理由，比照 design_agent/prompts.py／plan_agent/prompts.py 的既有說明。
"""
from __future__ import annotations

DEBUG_SYSTEM_PROMPT = """\
你是協助定位「Java 重構成 Python 後，某個功能模組驗證失敗」根本原因的
除錯助手。你會收到一個模組（module）的資料：這個模組目前有哪些 API
呼叫結果跟預期（golden output）不符、這個模組所有函式的清單與各自該做
什麼、以及這些函式目前的實際 Python 原始碼。

輸入包含：
1. module_summary：這個模組的業務語境
2. harness_failures：目前驗證失敗的清單，每筆含 case_id、failure_type
   （狀態碼不符／缺欄位／型別不符／值不符／陣列排序問題等）、
   expected_status/actual_status、body_diff（DeepDiff 格式的詳細差異）、
   related_files（推測相關的原始碼檔案）、debug_hint（機械產生的粗略
   方向提示，僅供參考，不一定準確）
3. known_fill_failures：已知確定「連程式碼都沒能成功生成」的函式，附
   上失敗訊息 error——這些函式目前極可能還是空骨架或殘缺內容
4. known_scaffold_gaps：已知這些函式在骨架階段就沒有被建立、目前根本
   不存在於任何檔案裡——**這些函式無法透過重新生成修好**，若你判斷某個
   失敗是這些函式造成的，把原因寫進 unfixable_reasons，絕對不要把它們
   的 task_id 放進 task_fixes
5. tasks：這個模組全部函式的清單，每筆含 task_id、function_name、
   class_name（None 代表是 routers 層的自由函式）、target_files、
   description（這個函式該做什麼的業務描述）——這是你判斷「是哪個函式
   造成失敗」的唯一合法 task_id 來源，task_fixes 裡的 task_id 只能是
   這份清單裡出現過、且不在 known_scaffold_gaps 裡的值。同名函式可能
   分屬不同 class（如 UserRepository.get_by_id 與
   OrderRepository.get_by_id），選擇時務必核對 class_name／target_files
   與 source_files 裡實際看到的程式碼是否一致，不要只憑 function_name
   字面比對
6. source_files：related_files 涉及到的檔案目前的實際內容（相對路徑 →
   原始碼字串）
7. special_note：這個模組上一輪的額外背景資訊（可能為空字串）
8. batch_sibling_modules：若非空，代表這個模組上一輪跟這些模組一起因為
   服務啟動逾時而無法驗證（可能是某個模組的程式碼有模組層級匯入或語法
   錯誤，牽連整個服務起不來，導致同一批全部被判定失敗，不代表這些模組
   自己的程式碼都有問題）——若你在這個模組的 source_files 裡找不到能
   解釋 harness_failures 的明顯錯誤，很可能問題出在這些 sibling 模組，
   不要在這個模組裡勉強找一個不存在的 bug，改在 unfixable_reasons 裡
   如實建議「檢查這批 sibling 模組」
9. service_diagnostics：若非空，代表這一輪 Python 服務整體連不上時擷取
   到的容器崩潰日誌（可能包含完整或不完整的 Python traceback）——這是
   比對 source_files 做純靜態分析更精確的訊號：優先核對日誌裡的
   traceback 檔案路徑是否落在這個模組的 target_files 裡；若是，直接
   依日誌內容判斷根本原因（不需要再靠猜的）；若日誌指向的檔案不在這個
   模組的 target_files 裡，根因很可能在 batch_sibling_modules 列出的
   其他模組，在 unfixable_reasons 如實說明，不要勉強在這個模組裡找一個
   不存在的 bug

你的任務：
1. 逐一分析 harness_failures，對照 source_files 的實際內容，判斷根本
   原因——注意常見的翻譯品質問題模式：用內建關鍵字（如 list、set、dict、
   type）當變數名稱遮蔽 builtin、查詢缺少明確排序（ORDER BY）、必填欄位
   忘記賦值、import 了不存在的模組或類別、業務邏輯分支跟描述不符、
   訊息文字寫死成錯誤語言等。若 service_diagnostics 非空，優先依上面
   第 9 點的指示核對日誌；若 special_note 或 batch_sibling_modules
   顯示這個模組上一輪是服務啟動逾時的一部分，優先檢查 source_files 有
   沒有明顯的 import／語法層級問題，而不是先假設是業務邏輯比對錯誤
2. 對每一個你判斷「重新生成這個函式有機會修好」的 task，在 task_fixes
   輸出一筆：task_id（只能引用 tasks 清單裡的值，且不能是
   known_scaffold_gaps 裡的）、diagnosis（根本原因，具體到「哪一行/
   哪個邏輯錯在哪」）、fix_instruction（給下一次重新生成這個函式時的
   具體指令，用祈使句直接說明該怎麼改，不要重複 diagnosis 的分析過程）
3. 若某個失敗的根本原因你判斷是 known_scaffold_gaps 裡的函式造成的，
   或是你判斷不出任何函式層級的可行修法（例如需要新增一個 tasks 清單
   裡完全沒有的檔案／函式，或問題實際上出在 batch_sibling_modules），
   寫進 unfixable_reasons，用一句話說明原因，不要勉強塞一個 task_fix
4. fixable：這個模組是否至少有一個 task_fixes——true 若且唯若
   task_fixes 非空
5. root_cause_summary：兩三句話總結這個模組目前主要的問題

**只能引用 tasks 清單裡出現過的 task_id，不要虛構或猜測不存在的
task_id，也不要引用 known_scaffold_gaps 裡的 task_id。**

不要輸出任何其他文字，不要用 markdown code fence 包裹。
"""

DEBUG_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "root_cause_summary": {"type": "string"},
        "fixable": {"type": "boolean"},
        "task_fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "diagnosis": {"type": "string"},
                    "fix_instruction": {"type": "string"},
                },
                "required": ["task_id", "diagnosis", "fix_instruction"],
                "additionalProperties": False,
            },
        },
        "unfixable_reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["root_cause_summary", "fixable", "task_fixes", "unfixable_reasons"],
    "additionalProperties": False,
}


# 對應 10a 三章「子情境：service_unreachable 且 failed_modules 也是空」
# 的 crash-log 分析路徑——輸出契約沿用 DEBUG_OUTPUT_SCHEMA（不另外定義
# 新 schema），只有輸入內容（system prompt）不同：這裡給的是容器崩潰
# log 全文＋全專案 task 清單，不是單一 module 的 harness_failures。
CRASH_LOG_SYSTEM_PROMPT = """\
你是協助定位「Python 服務容器崩潰、完全無法啟動」根本原因的除錯助手。
你會收到容器崩潰當下的完整日誌（通常包含一段 Python traceback），以及
整個專案所有函式的清單。

輸入包含：
1. container_log：容器崩潰當下的最後一段輸出（docker logs），可能包含
   完整或不完整的 Python traceback，也可能只有應用程式啟動失敗的訊息
2. tasks：整個專案（不限單一模組）所有函式的清單，每筆含 task_id、
   module、function_name、class_name（None 代表是 routers 層的自由
   函式）、target_files、description——這是你判斷「是哪個函式造成
   崩潰」的唯一合法 task_id 來源
3. known_scaffold_gaps：已知這些函式在骨架階段就沒有被建立、目前根本
   不存在於任何檔案裡，無法透過重新生成修好，若你判斷是這些函式造成
   崩潰，把原因寫進 unfixable_reasons，不要放進 task_fixes

你的任務：
1. 從 container_log 裡找出崩潰的直接原因——最常見的是模組層級的匯入
   錯誤（ImportError／ModuleNotFoundError／NameError，通常是 qwen 生成
   程式碼時 import 了一個不存在的名稱）或語法錯誤（SyntaxError）。
   Python traceback 通常會明確列出檔案路徑與行號（例如
   `File "/srv/app/services/order_service.py", line 12, in <module>`）
   ——把這個檔案路徑對照 tasks 清單裡 target_files 命中的項目，找出
   對應的 task_id
2. 若能明確定位到一個或多個 task，對每一個在 task_fixes 輸出一筆：
   task_id（只能引用 tasks 清單裡的值，且不能是 known_scaffold_gaps
   裡的）、diagnosis（根本原因，具體引用 log 裡的錯誤訊息）、
   fix_instruction（給下一次重新生成這個函式時的具體指令）
3. 若 log 內容不足以定位（例如只有「服務啟動逾時」這類沒有具體錯誤
   訊息的輸出，或 traceback 指向的檔案不在 tasks 清單任何一個
   target_files 裡），如實在 unfixable_reasons 說明「無法從日誌定位
   具體函式」，不要勉強猜一個 task_id
4. fixable：是否至少有一個 task_fixes——true 若且唯若 task_fixes 非空
5. root_cause_summary：兩三句話總結崩潰原因

**只能引用 tasks 清單裡出現過的 task_id，不要虛構或猜測不存在的
task_id，也不要引用 known_scaffold_gaps 裡的 task_id。**

不要輸出任何其他文字，不要用 markdown code fence 包裹。
"""
```

### `debug_agent/triage.py`

對應 10a 三章：機械正規化，不呼叫 LLM。

```python
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

_ORIGIN_ORDER = ("blocked", "module_mismatch", "scaffold_gap", "root_cause")


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
    （見 10a 二章前置修補），不是 state["failed_modules"]——後者只反映
    ⑤ 最後一次局部驗證（readonly-only）的結果，test_results 才是 ⑥
    全量驗證（readonly+mutation）算出的權威真相，兩者可能不一致（見
    10a 3.1）。
    """
    test_results = state.get("test_results", {})

    if test_results.get("reason") == "service_unreachable":
        # 10a 3.2「service_unreachable 分支」：⑥ 這一輪連 Newman 都沒能
        # 執行（見 refactor_harness/langgraph_nodes/test_nodes.py 的前置
        # 健康檢查，一章），test_results["failures"] 因此是空陣列——不是
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
                # 理論上不應發生（見一章補丁），防禦性略過並記警告；
                # 這裡不中止整個分析。
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
```

### `debug_agent/analysis.py`

對應 10a 四、五、七章核心邏輯。

```python
"""⑦ Debug Agent 核心邏輯：四章「Module 級 LLM 分析」（依模組平行呼叫，
比照 05a 六章「依模組取代整包 Map-Reduce」的既有模式，非 map-reduce）、
五章 task_id 反查驗證、七章 give_up_early 判斷。
"""
from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path, PurePosixPath

from common.concurrency import default_concurrency
from common.llm_client import LlmJsonError, call_claude_for_json
from debug_agent.llm import DEBUG_AGENT_MAX_TOKENS, DEFAULT_MODEL
from debug_agent.prompts import CRASH_LOG_SYSTEM_PROMPT, DEBUG_OUTPUT_SCHEMA, DEBUG_SYSTEM_PROMPT
from debug_agent.triage import ModuleFailureContext, module_tasks
from graph.state import DebugRound, RefactorState, TaskFix

logger = logging.getLogger(__name__)

_MAX_MAP_WORKERS = default_concurrency()

# 固定文字，比照 09a 五章 _RELATIONSHIP_GAP_NOTICE 的既有慣例：機械可
# 確定的結論不需要重新問 LLM 一次措辭（見 10a 3.3）。
_SCAFFOLD_GAP_REASON = "④ 骨架階段未渲染此模組任何函式（100% 骨架缺口），無法透過重新生成修好，需人工介入或等待重新 scaffold"
_MODULE_MISMATCH_REASON = "route_to_module_mapping／task_list 找不到這個 module 對應的 task，需人工核對③/[P]輸出或 skip 呼叫鏈設定，見 02a 十六章"


def _read_source_files(python_project_path: str, related_files: set[str]) -> dict[str, str]:
    sources: dict[str, str] = {}
    root = Path(python_project_path)
    for rel_path in related_files:
        path = root / rel_path
        try:
            sources[rel_path] = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # 見 10a 十一章「錯誤處理範圍」：略過該檔案，不中斷整個 module
            # 的分析，理由同 07a「context_files 讀取容錯」既有機制。
            logger.warning("Debug Agent 讀取 related_file 失敗（不存在）：%s", path)
    return sources


def _is_global_infra_file(path: str) -> bool:
    """10a 四章「過濾規則不能只列 schemas/models，還必須涵蓋 core」：
    target_files 除了 schemas/models/core 外還可能帶入
    referenced_interfaces（跨 module 的 router／service／repository
    唯讀參考），無差別聯集會稀釋 context、增加不必要的 token 成本。只取
    父目錄落在 schemas／models／core 任一個的項目——這三者共同的性質是
    「機械產生、不對應任何 TaskSpec，只能透過其他 task 的唯讀 context
    被看到」（05a 三章「全域基礎設施檔案」），依賴這個既有目錄命名慣例
    （見 10a 十二章「已知限制」）。校對時發現原本只列 schemas/models 會
    讓透過 app/core/exception_handlers.py 這類共用輔助檔案造成的問題
    完全看不到原始碼，因此補上 core。
    """
    return PurePosixPath(path).parent.name in ("schemas", "models", "core")


def _analyze_root_cause_module(
    ctx: ModuleFailureContext,
    module_summary: str,
    all_tasks: list[dict],
    python_project_path: str,
    service_diagnostics: str | None,
) -> dict:
    """單一 module 的 Claude API 呼叫，回傳 output schema 的原始解析結果。
    呼叫失敗時往上拋 LlmJsonError，由呼叫端的重試佇列機制接手（見
    run_debug_analysis()）。

    service_diagnostics：10a 四章「service_diagnostics 不能只留給
    no_signal 用」——這一輪 state["service_diagnostics"] 的值，不限於
    special_reason=="batch_reload_timeout" 的 module 才傳，這一輪所有
    root_cause module 統一傳入（正常情境下是 None，見呼叫端）。
    """
    # 10a 四章「為什麼要疊加 target_files，不能只給 related_files」：
    # route_to_file_mapping（related_files 的來源）只涵蓋 routers／
    # services／repositories 三層，不含 schemas/{module}.py／
    # models/{module}.py——missing_fields／type_mismatch 這兩種
    # failure_type 的根因常常就落在這兩類檔案。這兩類檔案不對應任何一個
    # TaskSpec（④機械渲染，不是⑤逐函式填空的對象），因此只能透過 06a
    # 七章既有設計裡、各 task 的唯讀 context（target_files 除了自己的
    # 寫入目標外，還包含這類參考檔案）取得。
    related_files = {rf for f in ctx["harness_failures"] for rf in f.get("related_files", [])}

    # 10a 四章「例外」：判斷條件是「已經算出的 related_files 聯集是否
    # 非空」，不是「ctx["harness_failures"] 是否非空」——後者只涵蓋
    # service_unreachable（harness_failures 整個是空陣列）這一種成因，
    # 漏掉了另一種一樣真實的情況：harness_failures 非空，但這一輪全部
    # 案例剛好都是 golden_not_found、且 route_to_file_mapping 查不到
    # 對應的 route，related_files 因此逐筆都是空清單，聯集起來一樣是
    # 空的。用「聯集本身」當判斷條件，兩種成因用同一個條件自然涵蓋，
    # 不需要窮舉列出。
    if related_files:
        # 有 related_files 可用，只取 schemas／models／core 補缺口（10a
        # 四章「過濾規則不能只列 schemas/models，還必須涵蓋 core」）：
        # target_files 也可能帶入跨 module 的 referenced_interfaces，
        # 無差別聯集會稀釋 context，這裡刻意過濾掉那部分，只保留補齊
        # 原本缺口所需要的檔案。
        related_files |= {
            tf for t in all_tasks for tf in t["target_files"] if _is_global_infra_file(tf)
        }
    else:
        # 沒有任何已聚焦的訊號可用（不論成因），schemas／models／core-only
        # 的過濾規則在這裡完全不適用：若仍然只給這三類，Debug Agent 會
        # 完全看不到 routers／services／repositories 的原始碼，但
        # batch_reload_timeout 最常見的成因（模組層級匯入／語法錯誤，見
        # 09a 三章）、以及 golden_not_found 對應的路由實作，都正好在
        # 那些檔案裡。這裡改成整份 target_files 不過濾，寧可 context
        # 寬一點，也不能讓 LLM 完全看不到可能藏著問題的程式碼。
        related_files |= {tf for t in all_tasks for tf in t["target_files"]}

    source_files = _read_source_files(python_project_path, related_files)

    known_fill_failures = [
        {"task_id": f["task_id"], "function_name": f["function_name"], "error": f["error"]}
        for f in ctx["task_failures"]
        if f["reason"] == "fill_failed"
    ]
    known_scaffold_gaps = [
        {"task_id": t["id"], "function_name": t["function_name"]}
        for t in all_tasks
        if t["id"] in ctx["scaffold_gap_task_ids"]
    ]

    user_prompt = _build_user_prompt(
        module_summary=module_summary,
        harness_failures=ctx["harness_failures"],
        known_fill_failures=known_fill_failures,
        known_scaffold_gaps=known_scaffold_gaps,
        tasks=all_tasks,
        source_files=source_files,
        special_reason=ctx["special_reason"] or "",
        batch_sibling_modules=ctx["batch_sibling_modules"],
        service_diagnostics=service_diagnostics or "",
    )

    return call_claude_for_json(
        system_prompt=DEBUG_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema=DEBUG_OUTPUT_SCHEMA,
        model=DEFAULT_MODEL,
        max_tokens=DEBUG_AGENT_MAX_TOKENS,
    )


def _build_user_prompt(
    *,
    module_summary: str,
    harness_failures: list[dict],
    known_fill_failures: list[dict],
    known_scaffold_gaps: list[dict],
    tasks: list[dict],
    source_files: dict[str, str],
    special_reason: str,
    batch_sibling_modules: list[str],
    service_diagnostics: str,
) -> str:
    import json as _json

    payload = {
        "module_summary": module_summary,
        "harness_failures": harness_failures,
        "known_fill_failures": known_fill_failures,
        "known_scaffold_gaps": known_scaffold_gaps,
        "tasks": [
            {
                "task_id": t["id"],
                "function_name": t["function_name"],
                "class_name": t.get("class_name"),
                "target_files": t["target_files"],
                "description": t["description"],
            }
            for t in tasks
        ],
        "source_files": source_files,
        "special_note": special_reason,
        "batch_sibling_modules": batch_sibling_modules,
        "service_diagnostics": service_diagnostics,
    }
    return _json.dumps(payload, ensure_ascii=False)


def _validate_task_fixes(raw_task_fixes: list[dict], valid_task_ids: set[str]) -> list[TaskFix]:
    """10a 4.3：task_id 反查驗證，捨棄不在合法集合內的 task_fix 並記警告，
    不中止整個 module 的分析。"""
    validated: list[TaskFix] = []
    for tf in raw_task_fixes:
        if tf["task_id"] not in valid_task_ids:
            logger.warning(
                "Debug Agent 回應引用了不存在或不合法的 task_id=%s，捨棄該筆 task_fix", tf["task_id"]
            )
            continue
        validated.append(TaskFix(task_id=tf["task_id"], diagnosis=tf["diagnosis"], fix_instruction=tf["fix_instruction"]))
    return validated


def _run_llm_analysis_with_retry(
    ctx: ModuleFailureContext,
    module_summary: str,
    all_tasks: list[dict],
    python_project_path: str,
    service_diagnostics: str | None,
) -> dict | None:
    """比照 04a 四章「待重試清單」：呼叫失敗重試一次；仍失敗回傳 None，
    由呼叫端視為「未分析」（見 10a 4.4、七章 give_up_early 判斷邊界）。
    """
    for attempt in range(2):
        try:
            return _analyze_root_cause_module(
                ctx, module_summary, all_tasks, python_project_path, service_diagnostics
            )
        except LlmJsonError as exc:
            logger.warning(
                "Debug Agent 對 module=%s 的 Claude API 呼叫失敗（第 %d 次）：%s",
                ctx["module"], attempt + 1, exc,
            )
    return None


def _analyze_from_crash_log(diagnostics: str, all_tasks: list[dict], scaffold_gap_task_ids: set[str]) -> dict:
    """10a 三章「修正：用容器崩潰當下的 log 當作訊號」：全專案範圍（不是
    單一 module）的 Claude API 呼叫，輸入是容器崩潰 log 全文＋全專案
    task 清單（僅 metadata，不含原始碼——log 裡的 traceback 通常已足夠
    精確）。輸出契約沿用 DEBUG_OUTPUT_SCHEMA。呼叫失敗時往上拋
    LlmJsonError，由 _run_crash_log_analysis_with_retry() 接手。
    """
    import json as _json

    payload = {
        "container_log": diagnostics,
        "tasks": [
            {
                "task_id": t["id"],
                "module": t["module"],
                "function_name": t["function_name"],
                "class_name": t.get("class_name"),
                "target_files": t["target_files"],
                "description": t["description"],
            }
            for t in all_tasks
        ],
        "known_scaffold_gaps": [
            {"task_id": t["id"], "function_name": t["function_name"]}
            for t in all_tasks if t["id"] in scaffold_gap_task_ids
        ],
    }
    return call_claude_for_json(
        system_prompt=CRASH_LOG_SYSTEM_PROMPT,
        user_prompt=_json.dumps(payload, ensure_ascii=False),
        schema=DEBUG_OUTPUT_SCHEMA,
        model=DEFAULT_MODEL,
        max_tokens=DEBUG_AGENT_MAX_TOKENS,
    )


def _run_crash_log_analysis_with_retry(
    diagnostics: str, all_tasks: list[dict], scaffold_gap_task_ids: set[str]
) -> dict | None:
    """比照 _run_llm_analysis_with_retry()：呼叫失敗重試一次，仍失敗回傳 None。"""
    for attempt in range(2):
        try:
            return _analyze_from_crash_log(diagnostics, all_tasks, scaffold_gap_task_ids)
        except LlmJsonError as exc:
            logger.warning("Debug Agent 的 crash-log 分析呼叫失敗（第 %d 次）：%s", attempt + 1, exc)
    return None


def run_debug_analysis(state: RefactorState) -> dict:
    """⑦ Debug Agent 的唯一對外入口，同步函式（見 10a 十章：由
    graph/nodes/debug_node.py 包一層 asyncio.to_thread() 呼叫）。

    對應 10a 六章「retry_count 遞增：推翻既有 stub 邏輯」：既有 stub
    （`increment = 1 if state.get("failed_modules") else 0`）依賴的
    `failed_modules` 只反映 ⑤ 的 readonly 局部驗證，⑤ 依 09a 三章刻意
    不驗證 mutation——一個 module 若只在 mutation 或跨模組 regression上
    有真正的 bug，會被 ⑤ 永遠判定「沒問題」，`failed_modules` 因此永遠
    不包含它，`retry_count` 若沿用舊邏輯會永遠停在原地，形成
    `implement → run_tests → debug` 無限迴圈（見 10a 六章完整論證）。
    這裡只要真的進入這個函式（等同於 `should_debug_or_done()` 已經確認
    `test_results.status == "fail"`），就無條件遞增，不再檢查
    `failed_modules`。
    """
    from debug_agent.triage import build_module_contexts

    retry_count = state.get("retry_count", 0)  # 這一輪呼叫當下的值，用於 DebugRound.round（遞增前）
    new_retry_count = retry_count + 1

    contexts = build_module_contexts(state)

    if not contexts and state.get("test_results", {}).get("reason") == "service_unreachable":
        # 10a 三章「子情境：service_unreachable 且 failed_modules 也是
        # 空」：完全沒有任何 module 可以鎖定，不代表沒有問題（服務仍然
        # 連不上）——不能因此 give_up_early=True，那等於在零次 LLM 呼叫
        # 的情況下直接放棄。優先嘗試從容器崩潰 log 找線索（見 10a 三章
        # 「修正：用容器崩潰當下的 log 當作訊號」）；log 讀不到、或分析
        # 不出結果，才退回被動記錄，當作最後手段。
        #
        # diagnostics 完全從 State 讀取，不 import 任何 graph/nodes/*.py
        # ——⑥（run_postman_tests()）判定服務不可達的當下就已經寫進
        # state["service_diagnostics"]，見 10a 八章「診斷資料改走
        # State」：debug_agent/ 不對任何 node 檔案有依賴。
        diagnostics = state.get("service_diagnostics")
        no_signal_rounds: list[DebugRound] = []
        no_signal_pending_fix_instructions: dict[str, str] = {}

        if diagnostics:
            all_project_tasks = [dict(t) for t in state.get("task_list", [])]
            project_scaffold_gap_ids = {
                f["task_id"] for f in state.get("task_failures", []) if f["reason"] == "scaffold_skipped"
            }
            raw = _run_crash_log_analysis_with_retry(diagnostics, all_project_tasks, project_scaffold_gap_ids)
            if raw is not None:
                valid_task_ids = {t["id"] for t in all_project_tasks} - project_scaffold_gap_ids
                task_fixes = _validate_task_fixes(raw["task_fixes"], valid_task_ids)
                if task_fixes:
                    involved_modules = sorted({
                        next(t["module"] for t in all_project_tasks if t["id"] == tf["task_id"])
                        for tf in task_fixes
                    })
                    no_signal_rounds.append(DebugRound(
                        round=retry_count, module=", ".join(involved_modules), origin="no_signal",
                        fixable=True, root_cause_summary=raw["root_cause_summary"],
                        task_fixes=task_fixes, unfixable_reasons=list(raw["unfixable_reasons"]),
                    ))
                    no_signal_pending_fix_instructions = {
                        tf["task_id"]: tf["fix_instruction"] for tf in task_fixes
                    }

        fell_back_to_passive_record = not no_signal_rounds
        if fell_back_to_passive_record:
            # 被動記錄，當作最後手段：log 讀不到、Claude API 呼叫失敗，
            # 或分析後沒有任何合法 task_fixes。
            no_signal_rounds = [DebugRound(
                round=retry_count, module="<none>", origin="no_signal", fixable=False,
                root_cause_summary="服務不可達，且 failed_modules 亦為空，無法從容器 log 或 failed_modules 鎖定任何可疑 module",
                task_fixes=[],
                unfixable_reasons=[
                    "無法定位可疑 module，可能是 mutation-only 或跨模組問題導致的服務崩潰，"
                    "⑤ 的 readonly 局部驗證未曾偵測到，容器 log 也沒能提供足夠線索；"
                    "需人工直接檢查最近一輪寫入的程式碼"
                ],
            )]

        # 10a 三章「Fail-fast」：連續兩輪都退回被動記錄，代表這不是這套
        # 機制能處理的問題（多半是純基礎設施異常，不是程式碼崩潰），不必
        # 等 MAX_RETRY 用盡才停下來。這是本文件唯一一處讀取
        # state["debug_rounds"]（累積歷史）的地方，跟七章「give_up_early
        # 判斷絕不能讀累積歷史」的規則性質不同——七章那條規則是針對「同一
        # 個 module 的 fixable 判斷」（同一個 module 不同輪次可能因不同
        # 原因被判定不同結論，用歷史會被過期資料卡死），這裡判斷的是「這
        # 整套 crash-log 機制這一次到底管不管用」，不是針對特定 module 的
        # 結論，兩者不衝突。
        previous_rounds = state.get("debug_rounds", [])
        previously_also_unfixable_no_signal = (
            bool(previous_rounds)
            and previous_rounds[-1]["origin"] == "no_signal"
            and not previous_rounds[-1]["fixable"]
        )
        give_up_early = fell_back_to_passive_record and previously_also_unfixable_no_signal

        return {
            **state,
            "retry_count": new_retry_count,
            "debug_rounds": no_signal_rounds,
            "pending_fix_instructions": no_signal_pending_fix_instructions,
            "give_up_early": give_up_early,
        }

    module_summaries = {m["module"]: m.get("summary", "") for m in state.get("module_list", [])}
    task_list = state.get("task_list", [])
    python_project_path = state["python_project_path"]

    # ⚠️ this_round_rounds 只裝這一次呼叫產生的 DebugRound，絕對不能跟
    # state.get("debug_rounds", [])（掛 operator.add reducer、逐輪累積
    # 的歷史欄位，見二章）混用或誤讀——下面 give_up_early 的判斷必須只
    # 看這一輪，見 10a 七章「為什麼一定要是這一輪、不能是累積歷史」的
    # 完整說明：歷史紀錄裡可能混著更早輪次「fixable=True」的舊記錄，用
    # 累積歷史判斷會讓 give_up_early 被過期資料卡死、永遠回傳 False。
    this_round_rounds: list[DebugRound] = []
    pending_fix_instructions: dict[str, str] = {}

    root_cause_ctxs = [c for c in contexts if c["origin"] == "root_cause"]
    analyzed_results: dict[str, dict | None] = {}

    # 10a 四章「service_diagnostics 不能只留給 no_signal 用」：這一輪所有
    # root_cause module 統一傳入，不限於 special_reason=="batch_reload_
    # timeout" 的 module，理由見該節「為什麼不是只在...才傳」。正常情境
    # 下（⑥ 沒有遇到 service_unreachable）這裡是 None，不影響一般案例。
    service_diagnostics = state.get("service_diagnostics")

    if root_cause_ctxs:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(_MAX_MAP_WORKERS, len(root_cause_ctxs))
        ) as pool:
            futures = {
                pool.submit(
                    _run_llm_analysis_with_retry,
                    ctx,
                    module_summaries.get(ctx["module"], ""),
                    [dict(t) for t in module_tasks(task_list, ctx["module"])],
                    python_project_path,
                    service_diagnostics,
                ): ctx
                for ctx in root_cause_ctxs
            }
            for future in concurrent.futures.as_completed(futures):
                ctx = futures[future]
                analyzed_results[ctx["module"]] = future.result()

    for ctx in contexts:
        module = ctx["module"]

        if ctx["origin"] == "blocked":
            this_round_rounds.append(DebugRound(
                round=retry_count, module=module, origin="blocked", fixable=False,
                root_cause_summary="此模組被上游卡住（見 blocked_reasons），非此模組自身問題",
                task_fixes=[], unfixable_reasons=[],
            ))
            continue

        if ctx["origin"] == "module_mismatch":
            # 10a 3.3：test_results.failures 的 module 對不上 task_list
            # 裡任何一個 task，不呼叫 LLM、不嘗試用 related_files 反查
            # 猜測，直接記固定診斷文字。
            this_round_rounds.append(DebugRound(
                round=retry_count, module=module, origin="module_mismatch", fixable=False,
                root_cause_summary=_MODULE_MISMATCH_REASON,
                task_fixes=[], unfixable_reasons=[_MODULE_MISMATCH_REASON],
            ))
            continue

        if ctx["origin"] == "scaffold_gap":
            this_round_rounds.append(DebugRound(
                round=retry_count, module=module, origin="scaffold_gap", fixable=False,
                root_cause_summary=_SCAFFOLD_GAP_REASON,
                task_fixes=[], unfixable_reasons=[_SCAFFOLD_GAP_REASON],
            ))
            continue

        raw = analyzed_results.get(module)
        if raw is None:
            # API 呼叫失敗、重試仍失敗：視為「未分析」，不產生 DebugRound
            # （不能算進 fixable=False 的證據，見 10a 七章「為什麼排除
            # API 失敗未分析到的 module」）。root_cause_ctxs 本身（下面
            # give_up_early 判斷用的集合）仍然完整保留這個 module，不會
            # 因為這裡沒有 append DebugRound 就從集合裡消失。
            continue

        # ⚠️ 順序固定：4.3（task_id 反查驗證）必須先跑，fixable 才能從
        # 驗證過的 task_fixes 推導——見 10a 4.2「這個順序不能反過來」。
        # fixable 直接定義成 bool(task_fixes)，不是讀 raw["fixable"] 再
        # 視情況覆寫：這樣寫從結構上就不可能出現「fixable=True 但
        # task_fixes=[]」這種非法狀態（唯一一筆 task_fix 若因為幻覺
        # task_id 被下面這行剔除，task_fixes 變空，fixable 自然也是
        # False，不需要額外一步「校正」）。
        valid_task_ids = {t["id"] for t in module_tasks(task_list, module)} - ctx["scaffold_gap_task_ids"]
        task_fixes = _validate_task_fixes(raw["task_fixes"], valid_task_ids)

        fixable = bool(task_fixes)
        unfixable_reasons = list(raw["unfixable_reasons"])
        if raw["fixable"] and not task_fixes:
            # 10a 4.2：這裡只是補一筆說明文字，不是「校正」fixable 本身
            # ——fixable 從上面 bool(task_fixes) 那行起就已經是對的。
            unfixable_reasons.append("LLM 回應宣稱可修但未提供任何有效 task_fix，視為分析失敗")

        this_round_rounds.append(DebugRound(
            round=retry_count, module=module, origin="root_cause", fixable=fixable,
            root_cause_summary=raw["root_cause_summary"],
            task_fixes=task_fixes, unfixable_reasons=unfixable_reasons,
        ))
        for tf in task_fixes:
            pending_fix_instructions[tf["task_id"]] = tf["fix_instruction"]

    # 10a 七章「判斷規則」：root_cause_ctxs 為空（案例二）時直接
    # give_up_early=True——三章的機械分類已經證明這一輪沒有任何 module
    # 有機會透過重試修好（blocked 待的上游若真有救必然自己也在
    # root_cause_ctxs 裡，module_mismatch／scaffold_gap 定義上就是不可能
    # 靠重試修，見 10a 七章完整論證），不需要呼叫 LLM 才能知道這一輪
    # 沒救，回 implement 只會被排程器的 already_failed 跳過，白白浪費一輪。
    if not root_cause_ctxs:
        give_up_early = True
    else:
        # 案例一：root_cause_ctxs 非空時，give_up_early 必須以這個
        # **完整集合**為準，不能只看「已經出現在 this_round_rounds 裡」
        # 的子集——API 呼叫失敗、重試仍失敗的 module 不會產生 DebugRound
        # （見上方 `if raw is None: continue`），若判斷式只從
        # this_round_rounds 反推，這種 module 會直接從集合裡消失，讓
        # all_analyzed 這一關形同虛設。
        #
        # 這裡也刻意只看 this_round_rounds（這一輪剛建構出來的清單），
        # 不是 state.get("debug_rounds", [])（累積歷史，見上方
        # this_round_rounds 宣告處的警告註解）——用歷史判斷會讓某個
        # module 若曾在更早輪次被判定 fixable=True，之後永遠卡死
        # give_up_early 判斷成 False，見 10a 七章完整說明。
        #
        # 這裡刻意不用「this_round_rounds 裡 origin=="root_cause" 的
        # 筆數」跟 len(root_cause_ctxs) 比較筆數是否相等這種寫法——用
        # all() 逐一比對 analyzed_results 更直接表達「一個都不能少」的
        # 意圖，避免未來有人加了别的原因导致 this_round_rounds 缺筆時
        # （例如 4.3 驗證失敗但仍應計入已分析）被筆數比對誤傷。
        all_analyzed = all(analyzed_results.get(ctx["module"]) is not None for ctx in root_cause_ctxs)
        root_cause_rounds = [r for r in this_round_rounds if r["origin"] == "root_cause"]
        # 防禦性斷言：analyzed_results（LLM 呼叫是否成功的唯一權威來源）
        # 與 this_round_rounds（實際產出的 DebugRound）目前必然一致——
        # 每個 root_cause module 在上面的迴圈裡，raw is None 就 continue
        # （不產生 DebugRound），否則一定會 append 恰好一筆。這個斷言不是
        # 「改用筆數比對」，是確保兩者不會在未來的修改中悄悄脫鉤：若有
        # 人在 raw is not None 的路徑裡新增一個會 continue、不 append 的
        # 分支，這裡會立刻炸開，而不是讓 give_up_early 默默算錯。
        assert len(root_cause_rounds) == sum(1 for m in analyzed_results.values() if m is not None), (
            "analyzed_results 與 this_round_rounds 不一致，give_up_early 判斷不可信"
        )
        give_up_early = all_analyzed and all(not r["fixable"] for r in root_cause_rounds)

    return {
        **state,
        "retry_count": new_retry_count,
        "debug_rounds": this_round_rounds,  # reducer（operator.add）在這裡才把這一輪併進歷史
        "pending_fix_instructions": pending_fix_instructions,
        "give_up_early": give_up_early,
    }
```

### `debug_agent/__init__.py`

```python
"""⑦ Debug Agent 對外唯一入口。"""
from debug_agent.analysis import run_debug_analysis

__all__ = ["run_debug_analysis"]
```

---

## 四、`graph/nodes/debug_node.py`——全面改寫

對應 10a 十章。取代現有 stub：

```python
"""
⑦ Debug Agent（Claude API）
輸入：test_results（⑥ 全量驗證，權威 bug list）+ task_failures（⑤ 的
task 級失敗根因）+ 對應 Python 原始碼。分析根本原因，輸出具體修正指令
回饋給 Agent ⑤（見 10a_debug_agent_architecture.md）。

真正的分析邏輯在 debug_agent/ 套件（同步、依模組用 ThreadPoolExecutor
平行呼叫 Claude API，比照 plan_agent/design_agent 既有模式）。這裡只是
薄封裝：把同步工作包一層 asyncio.to_thread()，理由見 09a 三章「必須包
asyncio.to_thread()」的既有洞察——LangGraph 對 async def node 不會自動
丟進執行緒池，只有普通 def node 才會，這裡選擇讓 debug_agent/ 維持同步
寫法、由這一層節點負責不卡住事件迴圈，而不是把整個套件改寫成 asyncio
風格（見 10a 十章）。
"""
import asyncio

from debug_agent import run_debug_analysis
from graph.state import RefactorState


async def run(state: RefactorState) -> RefactorState:
    return await asyncio.to_thread(run_debug_analysis, state)


def should_retry_or_give_up(state: RefactorState) -> str:
    """10a 七章「路由方式」：retry_count 上限檢查已經前移到
    should_debug_or_done()（02b，見一章），進入 debug 之前就已經確認過
    還在預算內——這裡只需要判斷 give_up_early（由 run_debug_analysis()
    判斷並寫入 state）。不再需要重複檢查 retry_count，也因此不再有
    should_debug_or_done() 與這裡各自用不同比較符號、產生 off-by-one
    的風險（見 10a 七章「為什麼要統一套用」）。
    """
    return "give_up" if state.get("give_up_early") else "implement"
```

---

## 五、`graph/scheduler.py`／`python_service/manager.py`／`graph/nodes/implement_node.py`——`force_reschedule()` 新增方法、`_python_service` 單例下沉、`_augment_task_io()`／`_run_one_task()` 擴充

對應 10a 八章。

### `graph/scheduler.py`：新增 `ModuleScheduler.force_reschedule()`

只新增這一個方法，`ModuleScheduler` 既有方法（`get_ready_tasks()`／`mark_task_done()`／`check_upstream_regression()`／`flag_for_reverify()` 等，01 六章）完全不動：

```python
    def force_reschedule(self, module: str):
        """由 ⑦ Debug Agent 產生 pending_fix_instructions 之後呼叫（見
        10a 八章）：把一個 module 強制打回 "pending"，讓它底下已完成的
        task 重新變成可以被 get_ready_tasks() 排到——這跟
        flag_for_reverify() 的 "needs_reverify" 不同，needs_reverify
        只觸發重新跑驗證，不會讓 task 重新進 get_ready_tasks()（見
        get_ready_tasks() 只認 "pending"／"in_progress" 這兩種狀態）；
        這裡要解決的問題是 task 本身要重新被排入佇列、用新的
        fix_instruction 重新填空。
        """
        self.module_status[module] = "pending"
```

插入位置：緊接在既有的 `flag_for_reverify()` 方法之後即可，兩者性質相近（都是外部呼叫端主動改變某個 module 的 `module_status`）。

### `graph/nodes/implement_node.py`：`_augment_task_io()`／`_run_one_task()` 擴充

```python
def _augment_task_io(task: TaskSpec, pending_fix_instructions: dict[str, str]) -> tuple[str, list[str]]:
    """對應 09a 五章「疊加規則」＋ 10a 八章「⑤ 端的對應改動」：新增第三種
    疊加來源——⑦ Debug Agent 上一輪針對這個 task 的修正指令。這個疊加
    不限於 services／repositories 層（09a 五章的 relationship／enum
    提示才有這個限制），任何層級的 task 只要在 pending_fix_instructions
    裡出現就疊加。
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

    fix_instruction = pending_fix_instructions.get(task["id"])
    if fix_instruction:
        context = f"{context}\n\n【上一輪除錯建議】\n{fix_instruction}" if context else f"【上一輪除錯建議】\n{fix_instruction}"

    return context, context_files


async def _run_one_task(
    task: TaskSpec, python_project_path: str, pending_fix_instructions: dict[str, str]
) -> FillResult:
    context, context_files = _augment_task_io(task, pending_fix_instructions)
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
```

`run()` 內唯一呼叫 `_run_one_task()` 的地方（`asyncio.gather(*(_run_one_task(t, state["python_project_path"]) for t in ready))`）改成：

```python
        pending_fix_instructions = state.get("pending_fix_instructions", {})
        results = await asyncio.gather(
            *(_run_one_task(t, state["python_project_path"], pending_fix_instructions) for t in ready)
        )
```

**不需要移到迴圈外**：`pending_fix_instructions` 在整個 `run()` 執行期間不會變（⑦ 只在 `debug` node 寫入，`implement` 執行期間是同一份快照），放在 while 迴圈內每次重讀 `state.get(...)` 或迴圈外算一次結果相同，這裡就近取值只是避免額外一層變數，不影響正確性。

### 新增檔案：`python_service/manager.py`——`_python_service` 單例從 `implement_node.py` 下沉到這裡

對應 10a 八章「診斷資料改走 State，不是 `debug_agent/` 直接 import `implement_node`」——`debug_agent/analysis.py` 原本設計成直接 `import graph.nodes.implement_node`（node 對 node 互相 import），違反本專案 node 檔案只做薄封裝、彼此不互相 import 的既有慣例（見 10a 八章完整推理）。修正方式是把 `_python_service` 這個單例本身移出 `implement_node.py`，下沉到 `python_service/` 套件：

```python
# python_service/manager.py（新增檔案）
"""容器化 Python 服務的模組層級單例與生命週期管理，供 implement_node.py
（⑤，啟動／等待／關閉）與 refactor_harness/langgraph_nodes/test_nodes.py
（⑥，健康檢查失敗時讀取診斷）共用。獨立成套件層級模組，不歸屬任一個
node 檔案，比照 refactor_harness/／translator_cli/ 的既有慣例。
"""
from __future__ import annotations

import asyncio
import os

from python_service.process import PythonServiceContainer

_python_service: PythonServiceContainer | None = None


async def ensure_started(python_project_path: str, python_base_url: str) -> None:
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


async def stop() -> None:
    global _python_service
    if _python_service is None:
        return
    await asyncio.to_thread(_python_service.stop)
    _python_service = None


def get_diagnostics() -> str | None:
    """⑥ 健康檢查失敗時讀取（見一章 `run_postman_tests()` 修正），寫進
    `state["service_diagnostics"]`；⑦ 全程只讀 State，不呼叫這個函式，
    見 10a 八章。"""
    return _python_service.diagnostics if _python_service is not None else None
```

`implement_node.py` 既有的 `_ensure_python_service_started()`／`stop_python_service()`（09b 二章）改成呼叫這個模組，不再自己持有 `_python_service`：

```python
# graph/nodes/implement_node.py（異動）
from python_service import manager as python_service_manager

# _python_service 這個模組層級變數整個刪除，不再需要


async def _ensure_python_service_started(python_project_path: str, python_base_url: str) -> None:
    await python_service_manager.ensure_started(python_project_path, python_base_url)


async def stop_python_service() -> None:
    await python_service_manager.stop()
```

`main.py` 既有呼叫點 `await implement_node.stop_python_service()`（09b 三章）不需要改——`implement_node.stop_python_service()` 還在，只是內部改成委派給 `python_service_manager.stop()`，對外介面不變，`main.py` 不用跟著改動。

### `run()` 新增：`force_reschedule()` 的呼叫點

對應 10a 八章「新增：`ModuleScheduler.force_reschedule()` 的呼叫點」——這是這次校對才發現的既有缺口的修正，不是只給 `no_signal` 分析路徑專用：只要 `pending_fix_instructions` 指向一個當下 `module_status=="verified"` 的 module（不限於哪條分析路徑產生的——四章正常的 `root_cause` 分析同樣可能鎖定一個「⑤局部驗證誤判為`verified`」的 module，見 10a 八章完整說明），`get_ready_tasks()` 永遠不會排到它，`fix_instruction` 會被平白浪費。緊接在既有的「100% 由 `scaffold_gap_task_ids` 覆蓋的 module」那段掃描（`for module, tasks in scheduler.tasks_by_module.items(): if tasks and all(...)`）之後，新增：

```python
    for task_id in state.get("pending_fix_instructions", {}):
        task = next((t for t in state["task_list"] if t["id"] == task_id), None)
        if task is not None:
            scheduler.force_reschedule(task["module"])
```

### `partial_reports.append(...)` 新增 `"round"` 鍵

對應 10a 3.5「過期資料」。09b 二章 `run()` 全部四處 `partial_reports.append({"module": module, "report": {...}})` 呼叫點，一律補上 `"round": state["retry_count"]`：

```python
            partial_reports.append({
                "module": module, "round": state["retry_count"],
                "report": {
                    "status": "fail",
                    "reason": "module_entirely_scaffold_skipped",
                    "regression": False,
                },
            })
```

```python
                    partial_reports.append({
                        "module": module, "round": state["retry_count"],
                        "report": {"status": "fail", "reason": "batch_reload_timeout", "regression": False},
                    })
                    scheduler.mark_module_verified(module, passed=False)
                for module in reverify_needs_wait:
                    partial_reports.append({
                        "module": module, "round": state["retry_count"],
                        "report": {"status": "fail", "reason": "batch_reload_timeout", "regression": True},
                    })
```

```python
                    partial_reports.append({"module": module, "round": state["retry_count"], "report": report})
                    scheduler.mark_module_verified(module, passed=report["status"] == "pass")
                for module in reverify_needs_wait:
                    report = await _partial_verify(module, db, verifier)
                    report["regression"] = True
                    partial_reports.append({"module": module, "round": state["retry_count"], "report": report})
```

**這是純新增一個鍵，不改變既有欄位語意**——比照二章 `module` 欄位補丁同一種手法：既有讀取 `partial_reports` 的程式碼（`implement_node.run()` 開頭恢復 `already_verified_modules` 那段 `latest_module_status` 邏輯）只用 `.get("module")`／`["report"]`，不受影響；新增的 `debug_agent/triage.py::_latest_reasons_by_module()`（三章）才會讀這個新鍵。`state["retry_count"]`：`implement_node.run()` 執行當下，`retry_count` 還沒有被 `debug_node`（六章）遞增，讀到的值恰好等於「這一輪 `implement()` 呼叫」對應的輪次標記，跟 `debug_agent` 之後用 `state.get("retry_count", 0)` 讀取的值是同一個數字，兩邊不需要另外同步。

其餘 `implement_node.py` 程式碼不變（`run()` 其餘排程邏輯、`_partial_verify()`、`_wait_for_service_reload()` 等完全不受影響）。

### 回歸測試

`tests/graph/test_implement_node.py`（既有測試檔案）補一個斷言：`partial_reports` 裡每一筆都帶有 `"round"` 鍵，值等於呼叫當下傳入的 `state["retry_count"]`。`tests/debug_agent/test_triage.py`（三章既有測試）新增「模組 A 在 round 0 有 `batch_reload_timeout`、round 1 沒有任何新紀錄（`partial_reports` 裡完全沒有 round=1 的 A）、模組 C 在 round 1 才第一次遇到 `batch_reload_timeout`」的案例——斷言 C 在 round 1 的 `batch_sibling_modules` **不包含** A（A 的紀錄停留在 round 0，不是 round 1 的最新紀錄，屬於過期資料，見校對時抓到的缺陷，這個案例專門鎖住不能回歸）。

---

## 六、`graph/builder.py`——`debug` 出邊改條件邊

對應 10a 七章「路由方式」。

```python
    # debug 迴圈：give_up_early（10a 七章：這一輪所有 root_cause module
    # 都判定不可修）路由到 give_up，不進 implement 浪費一輪重試；否則
    # 照舊回 implement 讓 pending_fix_instructions 生效。
    builder.add_conditional_edges(
        "debug",
        debug_node.should_retry_or_give_up,
        {
            "implement": "implement",
            "give_up": "give_up",
        },
    )
```

取代原本的 `builder.add_edge("debug", "implement")`。不影響其餘邊的結構（`debug` 只有單一前驅 `run_tests`，改它自己往下的出邊不牽動任何 fan-in／fan-out）。

---

## 七、`main.py`——`initial_state` 新增欄位

```python
    initial_state = {
        ...
        "test_results": {},
        "debug_rounds": [],
        "pending_fix_instructions": {},
        "give_up_early": False,
        "retry_count": 0,
    }
```

比照既有 `Annotated[list, operator.add]`／一般欄位在 `initial_state` 顯式初始化的既有慣例（01 九章「顯式全部初始化」）。

---

## 八、`tests/`——新增與既有測試同步更新

```
tests/
├── refactor_harness/
│   ├── test_comparator.py           # 新增：results/failures 帶 module 欄位
│   └── test_mutation_verifier.py     # 新增：同上
├── debug_agent/
│   ├── test_triage.py                 # build_module_contexts()：blocked／
│   │                                   # module_mismatch／scaffold_gap／
│   │                                   # root_cause 四種分類，含「同一
│   │                                   # module 部分 scaffold_skipped 部分
│   │                                   # 正常」的混合情境、「module 完全
│   │                                   # 找不到對應 task」情境、
│   │                                   # batch_sibling_modules 計算（含
│   │                                   # 「module 後來有一筆正常 report
│   │                                   # 蓋掉舊的 timeout reason」情境）；
│   │                                   # test_results.reason ==
│   │                                   # "service_unreachable" 時改從
│   │                                   # failed_modules 湊出
│   │                                   # modules_with_failures（harness_
│   │                                   # failures 全部是空陣列），三章的
│   │                                   # 分類規則不變仍要正確套用
│   ├── test_analysis.py                # _validate_task_fixes() 過濾非法
│   │                                   # task_id；give_up_early 判斷需鎖住
│   │                                   # 四種情境（皆為校對時抓到並修正的
│   │                                   # 實作缺陷，專門用來防回歸）：
│   │                                   # (1) 兩個 root_cause module，一個
│   │                                   # 分析成功判定不可修、另一個 API
│   │                                   # 失敗未分析到 → give_up_early 必須
│   │                                   # 是 False；(2) root_cause_ctxs 為
│   │                                   # 空（全部落在 blocked／
│   │                                   # module_mismatch／scaffold_gap）
│   │                                   # → give_up_early 必須是 True；
│   │                                   # (3) retry_count 遞增不看
│   │                                   # failed_modules，只要進了這個函式
│   │                                   # 就一定 +1；(4) 同一個 module 在
│   │                                   # state["debug_rounds"]（傳入的歷史
│   │                                   # 累積清單）裡已經有一筆更早輪次的
│   │                                   # fixable=True 記錄，這一輪重新分析
│   │                                   # 判定 fixable=False → give_up_early
│   │                                   # 必須正確反映這一輪（True），不能
│   │                                   # 被歷史裡的舊 True 記錄卡住（鎖住
│   │                                   # this_round_rounds 不誤用累積歷史
│   │                                   # 這件事，見 10a 七章）；(5) **"no_signal"
│   │                                   # 專門回歸測試（三種情境）**：
│   │                                   # test_results.reason ==
│   │                                   # "service_unreachable" 且
│   │                                   # failed_modules 為空時——(a) 有
│   │                                   # diagnostics 且分析成功定位到
│   │                                   # task：give_up_early 必須是
│   │                                   # False，pending_fix_instructions
│   │                                   # 非空且對應 task_id，debug_rounds
│   │                                   # 一筆 origin="no_signal"、
│   │                                   # fixable=True；(b) diagnostics 為
│   │                                   # None（容器從未啟動）：完全不
│   │                                   # 呼叫 call_claude_for_json（mock
│   │                                   # 掉斷言零呼叫），落到被動記錄
│   │                                   # 分支，這是**第一次**出現（
│   │                                   # state["debug_rounds"] 為空或
│   │                                   # 最後一筆不是 no_signal/
│   │                                   # unfixable）時 give_up_early
│   │                                   # 必須是 False；(c) 有 diagnostics
│   │                                   # 但分析不出任何合法 task_fixes：
│   │                                   # 同樣落到被動記錄分支，第一次
│   │                                   # 出現時 give_up_early 仍是
│   │                                   # False；(d) **Fail-fast 專門
│   │                                   # 回歸測試**：state["debug_rounds"]
│   │                                   # 最後一筆已經是
│   │                                   # origin="no_signal"、
│   │                                   # fixable=False（模擬上一輪也
│   │                                   # 退回了被動記錄），這一輪又落到
│   │                                   # 被動記錄分支時，give_up_early
│   │                                   # 必須是 True——四種情境皆為校對
│   │                                   # 時抓到並修正的缺陷或新增機制，
│   │                                   # 專門鎖住不能回歸（見 10a 三章
│   │                                   # 「子情境」「Fail-fast」）；
│   │                                   # mock call_claude_for_json 驗證
│   │                                   # payload 組裝（含 batch_sibling_modules、
│   │                                   # related_files 與 target_files 聯集
│   │                                   # 只取 schemas／models／core、排除跨
│   │                                   # module 的 referenced_interfaces；
│   │                                   # harness_failures 為空時（見
│   │                                   # service_unreachable 分支）必須
│   │                                   # 改回聯集整份 target_files、不
│   │                                   # 過濾——這是校對時抓到並修正的
│   │                                   # 缺陷，routers／services／
│   │                                   # repositories 的原始碼在這個
│   │                                   # 情境下必須進到 source_files，
│   │                                   # 這個案例專門鎖住不能回歸）；
│   │                                   # **service_diagnostics 專門回歸
│   │                                   # 測試**：state["service_diagnostics"]
│   │                                   # 非空時，即使該 module 的
│   │                                   # special_reason 不是
│   │                                   # "batch_reload_timeout"（甚至是
│   │                                   # None），_analyze_root_cause_module()
│   │                                   # 送出的 payload 仍必須包含這份
│   │                                   # 診斷內容，不能只限於 no_signal
│   │                                   # 分支或 special_reason 命中時才傳
│   │                                   # （這是校對時抓到並修正的核心
│   │                                   # 設計缺陷，見 10a 四章）
│   └── test_prompts.py                  # DEBUG_OUTPUT_SCHEMA 本身是合法
│                                       # JSON Schema（比照既有 Agent 慣例）
└── graph/
    ├── test_debug_node.py               # should_retry_or_give_up() 現在
    │                                   # 只判斷 give_up_early（True→give_up，
    │                                   # False→implement），retry_count
    │                                   # 上限的判斷已經前移到
    │                                   # should_debug_or_done()，不在這裡
    │                                   # 測；run() 對 asyncio.to_thread()
    │                                   # 的呼叫（mock run_debug_analysis）
    └── test_implement_node.py           # 既有測試同步更新：_augment_task_io()
                                        # 多一個必填引數，_run_one_task() 同理；
                                        # 新增「pending_fix_instructions 命中
                                        # 時正確疊加進 context」的案例；
                                        # 新增「pending_fix_instructions 指向
                                        # 一個 module_status=="verified" 的
                                        # module 時，force_reschedule() 正確
                                        # 把它打回 pending，get_ready_tasks()
                                        # 下一輪能排到它」的案例（見五章、
                                        # 10a 八章——這是這次校對發現的既有
                                        # 缺口，不限於 no_signal 路徑）
tests/python_service/test_manager.py     # ensure_started()／stop()／
                                        # get_diagnostics() 對
                                        # _python_service 為 None／非 None
                                        # 兩種情境的行為（新檔案，見五章）
tests/graph/test_scheduler.py            # ModuleScheduler.force_reschedule()：
                                        # 把 "verified" 的 module 打回
                                        # "pending" 後，get_ready_tasks()
                                        # 正確回傳它底下的 task（既有
                                        # test_scheduler.py 若無則新增，見
                                        # 01 六章既有測試檔案位置）
```

`tests/graph/test_implement_node.py` 既有測試（09b 七章）需要同步更新呼叫簽名——凡是直接呼叫 `_augment_task_io(task)` 或 `_run_one_task(task, path)` 的既有測試，補上第三個引數（`pending_fix_instructions={}` 保持既有行為不變）。

---

## 九、模組結構總覽

```
refactor-project/
├── debug_agent/                 # 新增套件
│   ├── __init__.py
│   ├── triage.py                 # 三章：ModuleFailureContext 正規化
│   ├── llm.py                     # 模型/max_tokens
│   ├── prompts.py                  # system prompt + schema
│   └── analysis.py                  # 核心：平行分析、task_id 驗證、give_up_early
├── graph/
│   ├── state.py                 # 新增 TaskFix／DebugRound／四個欄位（含 service_diagnostics）
│   ├── scheduler.py               # 新增 ModuleScheduler.force_reschedule()
│   ├── builder.py                # debug 出邊改條件邊
│   └── nodes/
│       ├── debug_node.py        # 全面改寫
│       └── implement_node.py    # _augment_task_io()／_run_one_task() 擴充、
│                                  # force_reschedule() 呼叫點、改呼叫
│                                  # python_service.manager
├── python_service/
│   └── manager.py                 # 新增：_python_service 單例（從
│                                  # implement_node.py 下沉）＋
│                                  # ensure_started()／stop()／get_diagnostics()
├── refactor_harness/
│   ├── verifier/
│   │   ├── comparator.py        # 補 module／related_files 欄位
│   │   └── mutation_verifier.py # 補 module 欄位
│   ├── core/
│   │   └── reporter.py           # 補 module 欄位
│   └── langgraph_nodes/
│       └── test_nodes.py          # run_postman_tests() 前置健康檢查、
│                                  # 寫入 state["service_diagnostics"]、
│                                  # should_debug_or_done() 修正
├── main.py                       # initial_state 新增欄位
└── tests/
    ├── debug_agent/             # 新增
    ├── python_service/           # 新增 test_manager.py
    ├── refactor_harness/         # 新增測試
    └── graph/                    # 既有測試同步更新
```

09a 八章「不需要新套件」的判斷準則在這裡不適用——⑦ 有實質領域邏輯需要封裝（module 分組判斷、prompt 設計、task_id 反查驗證、`give_up_early` 判斷），比照 `plan_agent/`／`design_agent/` 獨立成 `debug_agent/`，不是給既有 Agent 加一層不必要的抽象。

---

## 十、已知限制與待驗證事項

- [ ] **尚未接上真實 Claude API 校準 prompt 品質**：`DEBUG_SYSTEM_PROMPT`／`DEBUG_OUTPUT_SCHEMA` 目前只驗證過 schema 本身合法、`_validate_task_fixes()` 等純邏輯函式的單元測試，沒有對真實失敗案例（如 `09b_bug_trace.md` 記錄的 `#9`／`#23`～`#28`）實際跑過 Claude API，判斷品質（能否準確定位 task_id、`fix_instruction` 是否真的有效）完全未知，比照 06a 十二章「待接上真實③輸出後校準」的既有先例
- [ ] **`DEBUG_AGENT_MAX_TOKENS` 沿用預設值 4096，未實測**：見 `debug_agent/llm.py` 說明，尚無真實回應長度數據
- [ ] **`give_up_early` 機制尚未經過真實多輪 `debug ↔ implement` 循環驗證**：見 10a 十二章
- [ ] **`_read_source_files()` 對超大檔案沒有做 context 大小上限控制**：目前假設 module 邊界已經自然收斂檔案數量與大小（見 10a 四章），若真實專案某個 module 的檔案異常龐大，可能需要額外的截斷或摘要策略，第一版不處理
- [ ] **`give_up_node.py` 尚未區分 `give_up_early` 與「`retry_count` 用盡」兩種訊息**：目前會落入既有的「超過重試次數」訊息分支（`state["retry_count"]` 已被本文件的路由邏輯正常遞增，不是強制設成上限，所以訊息裡印出的次數是真實的，不會誤導，只是沒有额外標註「這是 ⑦ 主動判斷放棄，不是次數用盡」），屬於 10a 十二章列出的資訊呈現層面待辦，不影響核心機制正確性
- [ ] **多輪 debug 之間沒有互相參照（`no_signal` 的 Fail-fast 判斷是唯一例外）**：正常的 per-module `root_cause` 分析每輪呼叫互不知道彼此，`debug_rounds` 只累積記錄供人工查看，見 10a 十二章；`no_signal` 分支（三章）為了 Fail-fast 判斷會讀取 `state["debug_rounds"]` 最後一筆——這是刻意的例外，判斷對象是「整套機制這次管不管用」而非特定 module 的結論，見 10a 三章「Fail-fast」完整說明
- [x] **`_latest_reasons_by_module()` 原本不分輪次取「最新一筆」，會抓到跨輪次的過期 `partial_reports` 資料，把早已無關的 module 誤判成同批**：已解決，見五章 `partial_reports.append(...)` 新增 `"round"` 鍵、三章 `_latest_reasons_by_module()` 加上輪次過濾。「不是精確的批次分組」這個更根本的近似仍然存在（`partial_reports` 沒有精確的批次識別碼，只能用「同一輪」代理），見 10a 十二章
- [x] **`should_debug_or_done()`（02b）「`failed_modules` 為空即直接 `"debug"`」分支疊加 09a「⑤ 只做 readonly 局部驗證」導致 `retry_count` 長期不遞增**：已解決，`run_debug_analysis()`（二章）改成每次執行無條件 `retry_count + 1`，不再依賴 `failed_modules`，見 10a 六章完整論證
- [ ] **`origin="module_mismatch"`（三章）尚未有真實案例驗證**：第一版沒有接上真實 ③ 輸出、真實 `route_to_module_mapping` 觸發過這條分支，`unfixable_reasons` 的固定文字是否對人工核對有實際幫助待確認
- [x] **疊加 `target_files` 後的 context 大小**：已解決，見 10a 四章「過濾規則不能只列 schemas/models，還必須涵蓋 core」與三章 `_is_global_infra_file()`——不再無差別聯集整份 `target_files`
- [x] **過濾規則原本只列 `schemas`／`models`，遺漏 `app/core/*.py` 這類同一性質的全域基礎設施檔案，透過 `core` 共用輔助函式造成的問題完全看不到原始碼**：已解決，過濾規則擴大為 `schemas`／`models`／`core` 三者聯集，函式改名 `_is_global_infra_file()`，見三章
- [x] **`give_up_early` 誤讀累積歷史的風險**：已解決，`run_debug_analysis()` 內部改用 `this_round_rounds`（每次呼叫從空清單重新建構）取代直接沿用名稱容易誤讀成 `state["debug_rounds"]` 的變數，見二章程式碼與 10a 七章「為什麼一定要是這一輪、不能是累積歷史」
- [x] **`run_postman_tests()` 在服務不可達時會讓整條 `graph.ainvoke()` 崩潰，⑦ 永遠無法被呼叫到**：已解決，見一章「`run_postman_tests()` 前置健康檢查」——新增 `_is_service_reachable()`，不可達時回傳 `reason="service_unreachable"` 的結構化 `test_results`；`debug_agent/triage.py::build_module_contexts()` 對應新增分支（從 `state["failed_modules"]` 湊出待分析 module，見三章）
- [x] **`service_unreachable` 分支與 `schemas`／`models`-only 過濾規則互相矛盾，導致 LLM 完全看不到 routers／services／repositories 原始碼**：已解決，見三章 `_analyze_root_cause_module()`——`harness_failures` 為空時例外放寬成整份 `target_files` 不過濾，10a 四章「例外」記錄完整理由；這是同一輪（第三輪外部審查加的 `service_unreachable` 機制、`schemas`／`models` 過濾規則）兩個各自合理的修正互相打架的例子，兩者都是這次校對加的，第一次組合起來就出現矛盾，值得記錄：修正一個既有問題時，要連帶檢查它跟同一輪其他修正的交互，不能只獨立驗證
- [x] **`comparator.py::_process_executions()` 的 `golden_not_found`／`response_not_json` 兩個早退分支缺 `related_files`**：已解決，見一章——這是 02b 既有程式碼原本就有的落差（不是 10a/10b 新增），只有「正常比對」分支帶了 `related_files`；對 `response_not_json`（未處理例外導致 500）這種最需要看程式碼的情境影響最大，已補齊
- [x] **`should_retry_or_give_up()` 原本補一道 `retry_count >= MAX_RETRY` 防禦性檢查，會沒收 `MAX_RETRY` 那一輪剛分析出的 fix_instruction（off-by-one），且用 `>` 修正後在防禦性場景仍會多浪費一次 LLM 呼叫**：已用更根本的方式解決——直接修正 `should_debug_or_done()`（02b，見一章），把 `retry_count` 上限檢查移到最前面統一套用，不再需要 `should_retry_or_give_up` 額外檢查 `retry_count`（見四章、10a 七章「路由方式」）。這是校對時發現「下游補丁」不如「修正源頭」的例子：兩者都能防止無限循環，但源頭修正沒有 off-by-one 疑慮，也不會多花那一次 LLM 呼叫
- [ ] **`RUN_TESTS_HEALTH_CHECK_TIMEOUT_SECONDS`（一章，預設 15 秒）未實測**：跟 09a 三章 `SERVICE_READY_TIMEOUT_SECONDS` 一樣，這個數字目前是憑經驗猜的初始值，需要接上真實環境校準——尤其若容器崩潰後的重啟（下一輪 fix_instruction 命中時）耗時比預期久，這個短逾時可能太早放棄、把「服務其實正在重啟中」誤判成「不可達」
- [x] **4.2（`fixable`／`task_fixes` 一致性校正）與 4.3（`task_id` 反查驗證）的執行順序沒有明確釘死，文件字面順序容易被誤讀成先做 4.2 再做 4.3**：已解決，見二章程式碼註解與 10a 4.2「這個順序不能反過來」——`fixable` 直接定義成 `bool(task_fixes)`（`task_fixes` 已經是 4.3 驗證過的結果），不是讀 `raw["fixable"]` 再視情況覆寫，從結構上排除「`fixable=True` 但 `task_fixes=[]`」這種非法狀態
- [x] **`service_unreachable` 且 `failed_modules` 也是空時，會在零次 LLM 呼叫的情況下直接 `give_up_early=True`**：已解決，見三章 `run_debug_analysis()` 開頭的短路判斷——`contexts` 為空且 `test_results.reason=="service_unreachable"` 時，第一輪固定 `give_up_early=False`（之後可能因 Fail-fast 機制變 `True`，見下方另一項），不套用「`root_cause_ctxs` 為空即代表沒救」的一般規則（10a 三章「子情境」有完整推演，說明兩者的證據強度不對等）
- [x] **`no_signal` 分支跟七章「`root_cause_ctxs` 為空 → `give_up_early=True`」的一般規則之間的關係，文件沒有明講兩者是互斥分支、不是循序執行，容易誤讀成後者會覆寫前者**：已解決，見二章程式碼（`no_signal` 分支內有明確的 `return`，函式在此結束，不會執行到七章那段邏輯）與 10a 七章「判斷規則」開頭新增的說明、10a 三章「子情境」的對應補充——兩節描述的是互斥的兩條分支，不是同一段循序執行的邏輯
- [x] **`no_signal` 情境沒有區分「純基礎設施異常」與「真正的程式碼崩潰」，即使根本不是程式碼問題也要熬滿 `MAX_RETRY` 輪才放棄**：已解決，見三章——連續兩輪都退回被動記錄（crash-log 分析定位不到任何 task）時，直接 `give_up_early=True`，不必等到 `MAX_RETRY` 用盡；第一輪給一次容錯空間（容器 log 可能還沒寫完），見 10a 三章「Fail-fast」
- [x] **上一項的第一版修法（單純記錄 `origin="no_signal"`、`pending_fix_instructions={}`）本身也不夠：`ModuleScheduler.get_ready_tasks()` 不會排到已標記 `"verified"` 的 module，什麼都不做等於確定燒光整個 `retry_count` 預算、沒有任何一輪真正嘗試修復**：已解決，見三章 `_analyze_from_crash_log()`——改用 `PythonServiceContainer.diagnostics`（09b 四章既有屬性，先前只在啟動逾時失敗時讀取過一次）讀取容器崩潰當下的 docker log，送進一次全專案範圍的 Claude API 呼叫定位具體 task；新增 `ModuleScheduler.force_reschedule()`（五章）把對應 module 打回 `"pending"`，讓它真的能被重新排程——log 讀不到或分析不出結果才退回被動記錄，當作最後手段，不是主要機制
- [x] **`force_reschedule()` 的必要性其實不限於 `no_signal` 路徑**：實作 `no_signal` 修法時發現，四章正常的 `root_cause` 分析路徑，只要鎖定的 module 剛好是「⑤ 局部驗證誤判為 `verified`，但 ⑥ 全量驗證抓到真正問題」（10a 3.1 已經明講的核心場景，不需要同時符合 `failed_modules` 整體為空），一樣會遇到同樣的排程器問題——`force_reschedule()` 的呼叫點（五章）因此覆蓋**全部** `pending_fix_instructions`，不只是 `no_signal` 產生的那些，見 10a 八章完整說明

- [ ] **crash-log 分析（三章 `_analyze_from_crash_log()`）的準確度尚未接上真實環境驗證**：見 10a 十二章——需要真實崩潰案例才能校準 log 是否足夠明確、LLM 能否正確反查回 `task_list`
- [x] **`debug_agent/analysis.py` 原本直接 `import graph.nodes.implement_node`（node 對 node 互相 import），違反本專案 node 檔案彼此不互相 import、跨節點資料一律經 State 傳遞的既有慣例**：已解決，見五章「新增檔案：`python_service/manager.py`」——`_python_service` 單例下沉到不歸屬任何 node 的共用模組，⑥（`run_postman_tests()`，一章）判定服務不可達時直接讀取診斷、寫進新欄位 `RefactorState.service_diagnostics`（二章），⑦（三章）全程只讀這個 State 欄位
- [x] **`related_files` 為空的放寬規則原本只涵蓋 `harness_failures` 整個為空（`service_unreachable`）的情況，遺漏「`harness_failures` 非空、但全部案例的 `related_files` 都剛好是空清單」（`golden_not_found` 因 `route_to_file_mapping` 缺漏該路由造成，最常見的成因）**：已解決，見三章 `_analyze_root_cause_module()`——判斷條件從 `ctx["harness_failures"]` 是否非空改成已算出的 `related_files` 聯集是否非空，同一個條件自然涵蓋兩種成因
- [x] **`service_diagnostics`（容器崩潰 log）原本只在 `no_signal` 子情境使用，`service_unreachable` 且 `failed_modules` 非空（更常見的 `batch_reload_timeout` 情境）反而看不到，只能對放寬過濾後的原始碼做純靜態猜測**：已解決，見三章——`_analyze_root_cause_module()`／`_run_llm_analysis_with_retry()` 新增 `service_diagnostics` 參數，`run_debug_analysis()` 對這一輪所有 `root_cause` module 統一傳入 `state.get("service_diagnostics")`（不限 `special_reason` 是否命中），`DEBUG_SYSTEM_PROMPT` 對應新增第 9 點輸入說明與分析指示
- [x] **`debug_agent/llm.py` 的檔案層級文件雖然已經正確描述要用 `common.llm_client`，但沒有用「嚴禁自行初始化」這麼直白的措辭明講這是硬性規定**：已解決，見上方 `debug_agent/llm.py` docstring 補上的明確宣告，呼應 10a 九章新增的同一條約束

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
