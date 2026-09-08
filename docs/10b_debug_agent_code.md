# ⑦ Debug Agent 程式碼實作

> 本文件承接 `10a_debug_agent_architecture.md`（設計面：決策、契約、資料結構、流程；⑦ 的核心任務——把專案改到能通過驗證，不是分析完就結束——見 10a 一章），是這個 Agent 的**實作面**文件：對應每一項 10a 決策實際落地的程式碼。本文件描述的程式碼已套用進 repo，通過完整單元測試與多輪真實環境端對端驗證（含真實 pipeline 節點跑完整 `debug ↔ implement ↔ run_tests` 迴圈）。已知限制見第十一章。

---

## 目錄

1. `refactor_harness/`——`module`／`related_files` 欄位補丁（`comparator.py`／`mutation_verifier.py`／`reporter.py`）＋ `run_postman_tests()` 前置健康檢查＋ `should_debug_or_done()` 修正（`langgraph_nodes/test_nodes.py`）
2. `graph/state.py`——新增 `TaskFix`／`DebugRound`／`FileFix`／六個 `RefactorState` 欄位
3. `debug_agent/`——新增套件（`triage.py`／`llm.py`／`prompts.py`／`analysis.py`／`__init__.py`）
4. `graph/nodes/debug_node.py`——全面改寫
5. `graph/scheduler.py`／`python_service/manager.py`（新增）／`graph/nodes/implement_node.py`／`translator_cli/`——`force_reschedule()` 新增方法、`_python_service` 單例下沉、`_augment_task_io()`／`_run_one_task()` 擴充、`apply_file_fix()`（phase 2）
6. `graph/builder.py`——`debug` 出邊改條件邊；`graph/nodes/give_up_node.py`——新增 `give_up_early` 訊息分支
7. `main.py`——`initial_state` 新增欄位
8. `common/run_report.py`——pipeline 執行完的制式報告（JSON＋人類可讀 Markdown）
9. `tests/`——新增與既有測試同步更新
10. 模組結構總覽
11. 已知限制與待驗證事項

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

`tests/refactor_harness/test_comparator.py`／`tests/refactor_harness/test_mutation_verifier.py`／既有 `test_reporter.py`（若無則新增）各補一個斷言：`results[i]["module"]`／`report["failures"][i]["module"]` 等於預期的 module 名稱。**`test_comparator.py` 另外補兩個案例專門鎖住 `related_files` 缺口**：構造一個 golden 檔案不存在的 case、一個回傳非 JSON body 的 case，斷言 `results[i]["related_files"]` 等於 `route_mapper.resolve_related_files(method, url_parts)` 的預期值（不是空清單）——這兩個測試案例專門防回歸。不修改任何既有斷言（純新增欄位，既有斷言只檢查特定欄位，不會因為字典多一個 key 而失敗）。

### `refactor_harness/core/postman_runner.py`（`run_newman()` 逾時時的報表完整性檢查）

對應 10a 二章「前置修補之二」。`NewmanTimeoutError`（`RuntimeError` 子類別，讓呼叫端能精確只攔這一種暫時性逾時、不誤吞 newman 找不到／collection 路徑錯這類真正的設定錯誤）：

```python
class NewmanTimeoutError(RuntimeError):
    """newman 執行逾時，目標服務可能處於「socket 還開著但沒有真的回應」
    的異常狀態，也可能只是 newman 本身沒退出但工作早就做完了（見下方
    raise 處的完整判斷）。"""
```

`run_newman()` 原本 `subprocess.TimeoutExpired` 分支砍完行程樹之後直接 `raise`；新增報表完整性檢查：

```python
        _kill_process_tree(process.pid)
        process.communicate()  # 收尾：確認管線真的關閉，避免留下殭屍行程

        # newman（Node.js）已知會在收到最後一筆回應、報表檔案已經完整
        # 寫出之後，行程本身卻不結束（很可能是 keep-alive socket 沒有
        # 乾淨關閉，殘留的 handle 讓 Node 事件迴圈不會自然退出）——跟
        # `returncode != 0` 那條既有規則是同一種情況的延伸：newman 這個
        # 行程「有沒有正常結束」，不等於「這次執行到底有沒有成功產生
        # 報表」。逾時當下先檢查報表檔案是否已經是完整合法 JSON，是的
        # 話直接當成功回傳，不誤判成執行失敗。
        try:
            with open(output_path, encoding="utf-8") as f:
                result = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            raise NewmanTimeoutError(
                f"newman 執行逾時（timeout={NEWMAN_TIMEOUT_SECONDS}s）且報表檔案未完整寫出：..."
            ) from None
        logger.warning("newman 逾時但報表檔案已完整寫出，視為執行成功——...")
        return result
```

真實案例：mutation collection 全部 executions（含最後一筆）其實都已執行完成、報表也寫好了，只是 newman 行程本身沒退出，`subprocess.communicate(timeout=...)` 因此誤判逾時——用獨立診斷腳本直接檢查逾時當下的報表檔案內容，證實是完整合法 JSON 才確定這個修法方向；同類已知問題見 [uvicorn discussion #1691](https://github.com/encode/uvicorn/discussions/1691)「socket 未乾淨關閉導致行程不退出」。

### 回歸測試

`tests/refactor_harness/test_postman_runner.py` 新增：`subprocess.TimeoutExpired` 時若報表檔案已完整寫出，`run_newman()` 回傳該報表內容而非拋例外；報表檔案不存在或不是合法 JSON 時才拋 `NewmanTimeoutError`；`NewmanTimeoutError` 是 `RuntimeError` 子類別，既有的 `pytest.raises(RuntimeError, ...)` 斷言不用改。

### `refactor_harness/langgraph_nodes/test_nodes.py`（`run_postman_tests()` 前置健康檢查＋逾時安全網）

對應 10a 二章「⑥ 對外呼叫前必須先確認 Python 服務有回應」與「前置修補之二」。兩道防護：呼叫前先做連線層級健康檢查；`run_newman()` 逾時時（`postman_runner.py::NewmanTimeoutError`，見上方）也接住，不當作服務不可達之外的例外任它往外炸穿。

```python
import time
import httpx

from python_service import manager as python_service_manager
from refactor_harness.core.postman_runner import NewmanTimeoutError

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

# 見 docs/09b_bug_trace.md #47：⑦一輪可能一次套用多筆 pending_fixed_
# bodies／pending_file_fixes，各自的檔案寫入被 uvicorn --reload
# （watchfiles）依短暫的 debounce 視窗各自分批觸發，可能連續產生不只
# 一次的 reload 週期；_wait_for_service_reload()（09a 三章）只確認「牠
# 自己最後一次寫入的 token」已經生效，不保證這是這一輪「最後一次」被
# 觸發的 reload——已重現三次的症狀：這裡的健康檢查回報
# service_unreachable，但用獨立診斷腳本立刻探測同一顆容器，服務其實
# 完全正常，是健康檢查時機撞上還沒收斂穩定的 reload 窗口。根因（uvicorn
# 忙碌／reload 批次時機）未 100% 證實，這裡先採用其中一個建議緩解方向：
# 呼叫健康檢查之前先固定等一段緩衝時間，不是同步屏障，只降低機率、不
# 保證根除（見 10a 二章「前置修補之三」）。
RUN_TESTS_PRE_HEALTH_CHECK_BUFFER_SECONDS = float(
    os.environ.get("RUN_TESTS_PRE_HEALTH_CHECK_BUFFER_SECONDS", "5")
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

    **前置健康檢查**（10a 二章）：09a 三章「批次執行」描述的
    batch_reload_timeout 最常見成因（某個 task 寫入的程式碼有模組層級
    匯入／語法錯誤，讓容器內的 uvicorn worker 崩潰）在 implement 結束
    時可能還沒解決，Python 服務容器可能仍處於連不上的狀態（見 09b 六章
    「Python 服務只啟動一次」——⑤／⑥ 共用同一個持續運行的容器，⑤沒有
    機制把它修好）。若不做這個檢查，下面的 run_newman() 會直接拋出
    RuntimeError（02a 五章既有、刻意設計的行為），讓整條 graph.ainvoke()
    崩潰，⑦ Debug Agent 永遠不會被呼叫到。

    同一時間點順手讀取 `service_diagnostics`（10a 二章、八章）：這是
    診斷資料最新鮮的時間點，寫進 State 供 ⑦ 之後讀取——⑦（`debug_agent/`）
    全程不 import 任何 `graph/nodes/*.py`，也不 import
    `python_service.manager` 本身，只讀 `state["service_diagnostics"]`，
    避免 node 對 node 互相依賴（見 10a 八章）。

    **健康檢查前先固定緩衝**（10a 二章「前置修補之三」、
    `RUN_TESTS_PRE_HEALTH_CHECK_BUFFER_SECONDS` docstring）：不分岔判斷
    「這一輪是不是⑦套用了多筆修正」——那需要額外傳遞、判斷跨節點狀態，
    且緩解方向本身是機率性的，對「這一輪其實只有一筆改動」的情況多等
    這幾秒也無害，不值得為了省這幾秒緩衝時間增加狀態判斷的複雜度。
    """
    time.sleep(RUN_TESTS_PRE_HEALTH_CHECK_BUFFER_SECONDS)
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

    try:
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
    except NewmanTimeoutError:
        # newman（Node.js）已知會在收到最後一筆回應、報表檔案已經完整
        # 寫出之後，行程本身卻不退出（keep-alive socket 未乾淨關閉），
        # NewmanTimeoutError 因此不等於服務沒回應——真正的「報表是否
        # 完整」判斷已經下沉到 run_newman() 自己（見上方），這裡
        # 攔到的都是報表也沒寫完的真逾時，落到跟前置健康檢查失敗時
        # 同一份 fallback 結構（見 docs/09b_bug_trace.md #48）。
        return {
            **state,
            "test_results": dict(_UNREACHABLE_TEST_RESULTS),
            "service_diagnostics": python_service_manager.get_diagnostics(),
        }

    report = HarnessReporter().build_report(
        readonly_raw + mutation_raw,
        excluded_folders=mutation_verifier.get_excluded_folders(),
    )

    # 見 docs/09b_bug_trace.md #42：落地成 logs/report_{run_id}.json，
    # 供 ⑦ 讀取這次 run 目前最新的驗證結果，見四章。
    os.makedirs("logs", exist_ok=True)
    report_path = f"logs/report_{state['run_id']}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    return {**state, "test_results": report}
```

`_UNREACHABLE_TEST_RESULTS` 用 `dict(...)` 複製一份再回傳，不是直接回傳模組層級常數本身的參照——避免任何下游程式碼不小心原地修改這個字典，污染到下一次遇到同樣情況時的常數值（同一顆常數會被跨輪次重複使用）。健康檢查失敗與 `NewmanTimeoutError` 兩條路徑回傳完全相同形狀的 fallback，三章的正規化不需要區分是哪一種觸發的。

### 回歸測試

`tests/refactor_harness/test_test_nodes.py`：`_is_service_reachable()` 對真實可連線／連線被拒兩種情境的判斷（mock `httpx.get`）；`run_postman_tests()` 在服務不可達時回傳的 `test_results` 是否正確帶有 `reason="service_unreachable"`、且**沒有**呼叫到 `run_newman()`（mock 掉 `GoldenVerifier`／`MutationVerifier`，斷言它們的建構子／方法完全沒被呼叫，證明真的短路了，不是跑到一半才失敗）；`NewmanTimeoutError` 在 `verify_raw()`／`verify_all_raw()` 執行期間拋出時，一樣落到 `service_unreachable` 的 fallback（`tests/refactor_harness/test_postman_runner.py` 另外鎖住 `run_newman()` 自己「逾時但報表已完整」時視為成功、不誤判成錯誤的行為，見四之一節）；`state["service_diagnostics"]` 正確等於 `python_service_manager.get_diagnostics()` 的回傳值。**前置修補之三**（`#47` 緩衝）：`test_sleeps_pre_health_check_buffer_before_checking_reachability` 鎖住呼叫順序——先 `time.sleep(RUN_TESTS_PRE_HEALTH_CHECK_BUFFER_SECONDS)`，再呼叫 `_is_service_reachable()`；其餘既有測試改成一併 mock `test_nodes.time.sleep`，避免每個測試真的等 5 秒。

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

## 二、`graph/state.py`——新增 `TaskFix`／`DebugRound`／`FileFix`／六個 `RefactorState` 欄位

對應 10a 六章、二章、八章。插入位置：`TaskFix`／`DebugRound` 放在 `TaskFailure` 之後、`TaskSpec` 之前（Agent ⑦ 輸出的子型別）；`RefactorState` 新增欄位放在 `test_results` 之後、`retry_count` 之前（比照既有「依 Agent 順序」排列慣例）。

```python
# ── Agent ⑦ 輸出：task 級修正後程式碼與每輪除錯記錄（見 10a 六章、
# 八章「⑦ 直接產生修正後程式碼」、「phase 2」）──
class FileFix(TypedDict):
    task_id: str
    target_file: str
    old_snippet: str
    new_snippet: str


class TaskFix(TypedDict):
    task_id: str
    diagnosis: str
    # 對應 docs/refactor_bug_trace.md #9：函式本體邏輯有問題時（不論是
    # 從沒成功翻譯過，還是翻過但邏輯錯）優先設 True，交還給⑤用真實 Java
    # 原始碼重新翻譯，diagnosis 當成重新翻譯的提示。NotRequired：既有
    # 建構 TaskFix 的測試 fixture 不用跟著補，預設視為 False。
    retranslate: NotRequired[bool]
    # None：這個 task 的函式本體不需要改（或已由 retranslate=True 處理）。
    # 只在 retranslate 是 False（或缺省）時才可能非 None——兩者互斥。
    fixed_body: str | None
    file_fixes: list[FileFix]


class DebugRound(TypedDict):
    round: int
    module: str
    origin: Literal["blocked", "module_mismatch", "scaffold_gap", "root_cause"]
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
    # ⑦（三章）全程只讀這個欄位，不呼叫任何跨節點函式，見 10a 八章
    # 「診斷資料改走 State」。
    service_diagnostics: str | None

    # Agent ⑦（逐輪累積寫入，需要 reducer）：每一輪 debug 對每個分析過
    # module 的完整記錄，含未呼叫 LLM 的 blocked／scaffold_gap 機械判斷，
    # 供人工事後查看、也是未來評估「要不要讓 LLM 看到上一輪建議」的既有
    # 資料來源（見 10a 十二章）。
    debug_rounds: Annotated[list[DebugRound], operator.add]

    # Agent ⑦：這一輪產出的 task 級修正後程式碼，key 是 task_id、value
    # 是完整函式本體。不掛 reducer，整包覆寫——這是「這一輪的修正」，不是
    # 累加事件，見 10a 六章。implement_node._run_one_task() 讀取這份
    # 資料，有值的 task 直接傳給 fill_function() 的 fixed_body 參數，
    # 完全跳過 ⑤ 本地模型（見 10a 八章）。
    pending_fixed_bodies: dict[str, str]

    # Agent ⑦（對應 docs/refactor_bug_trace.md #9）：這一輪判定「函式
    # 本體邏輯需要重新翻譯」的 task，key 是 task_id、value 是 ⑦ 的
    # diagnosis（作為重新翻譯的提示）。跟 pending_fixed_bodies 同一種
    # 「這一輪的修正」語意，也一樣不掛 reducer；但
    # implement_node._run_one_task() 讀到這裡的 task_id 時**不**跳過
    # 模型呼叫——正常解析 java_source／referenced_source、真的呼叫
    # fill_function()，只是把 diagnosis 疊加進 context（見 10a 八章）。
    # 跟 pending_fixed_bodies 互斥。
    pending_retranslate_tasks: dict[str, str]

    # Agent ⑦：這一輪產出的檔案層級修正（phase 2，見 10a 八章）——
    # fixed_body／fill_function() 的 AST 函式定位機制只能碰函式本體，
    # 函式簽名與模組層級敘述（import 等）碰不到的部分走這裡，由
    # translator_cli.apply_file_fix() 用精確字串替換套用，不經過排程器。
    # 不掛 reducer，整包覆寫，語意同 pending_fixed_bodies。
    pending_file_fixes: list[FileFix]

    # Agent ⑦：這一輪分析後，若確認這一輪所有 root_cause module 都無法
    # 修，設為 True，供 debug_node.should_retry_or_give_up() 路由到
    # give_up，不進 implement 浪費一輪重試（見 10a 七章）。每輪覆寫，
    # 不掛 reducer。
    give_up_early: bool

    # Agent ⑦：這一輪 root_cause module 裡 Claude API 呼叫（含批次重試）
    # 仍然失敗、視為未分析的 module 名稱。每輪覆寫，不掛 reducer。供
    # give_up_node.py 在 retry_count 用盡時分辨「API 打不通」跟「⑦ 判斷
    # 邏輯本身有問題」，見 10a 十章、六章。
    unanalyzed_root_cause_modules: list[str]

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
建議：唯有如此 record_llm_call() 才會被統一呼叫到，API 呼叫才會確實
記進 llm_traces.db，不會因為漏接而算不準（見 11a 七、八章）。

DEBUG_AGENT_MAX_TOKENS 不沿用 common/llm_client.py 的 DEFAULT_MAX_TOKENS
（4096）：10a 八章「⑦ 直接產生修正後程式碼」之後，task_fixes 每一筆的
`fixed_body` 是完整函式本體（不是一句自然語言指令），`file_fixes`（phase
2）甚至可能帶完整函式定義，輸出密度大幅提高，一個 module 有多個
task_fixes 時很容易逼近甚至超過 4096——理由同 06a 五章
PLAN_AGENT_MAX_TOKENS 的既有判斷方式。8192 這個數字目前只有一筆新
schema 的真實測量支持：4 個 task_fixes（其中一筆含 file_fixes）實測
`in=7242／out=1355` token，遠低於上限，暫不需要調整；但 task_fixes／
file_fixes 數量明顯更多的大型 module 是否會逼近或超過這個上限仍未實測，
待接上真實環境累積更多案例後再校準。
"""
from __future__ import annotations

import os

from common.llm_client import DEFAULT_MODEL_FALLBACK

DEFAULT_MODEL = os.environ.get("DEBUG_AGENT_MODEL", DEFAULT_MODEL_FALLBACK)
DEBUG_AGENT_MAX_TOKENS = int(os.environ.get("DEBUG_AGENT_MAX_TOKENS", "8192"))
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
   上失敗訊息 error——這些函式目前極可能還是空骨架或殘缺內容。**這些
   task 一律要求你在 task_fixes 標記 retranslate=true**，見下方任務
   說明第 0 點——本地模型已經證明做不到，不會再有機會重新嘗試，這是
   它們被排入下一次真正翻譯的唯一機會
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
9b. harness_failures 裡若出現 failure_type 為 response_not_json（Python
    服務回傳非 JSON），常見成因不是這個模組自己端點程式碼的業務邏輯
    錯誤，而是全域例外處理器（app/core/exception_handlers.py，一定會
    出現在 source_files 裡，即使它不屬於這個模組）本身在被呼叫時又丟出
    另一個例外，導致連錯誤回應都無法正常序列化成 JSON，被上層框架攔截
    成一頁純文字/HTML 錯誤頁。出現這個 failure_type 時，除了檢查這個
    模組自己的程式碼，務必也核對 app/core/exception_handlers.py 呼叫
    ResponseResult／Result（app/services/common_service.py）相關方法時，
    使用的關鍵字參數名稱是否跟該類別方法的真實簽名（同樣在 source_files
    裡）逐一比對一致；若這個模組自己的程式碼看不出明顯問題，這是優先
    該懷疑、且可以直接在 file_fixes 裡修正的方向（即使
    exception_handlers.py 不是這個模組自己的檔案，file_fixes 的
    target_file 也可以指向它）

你的任務：
0. **對 known_fill_failures 裡的每一個 task，一律要求輸出 task_fixes、
   標記 retranslate=true**（除非確實無法判斷，見下方例外）——這一步跟
   第 1 點的「分析既有程式碼找 bug」是完全不同性質的工作，不要混為
   一談：這些函式從來沒有成功翻譯過，`source_files` 裡對應的內容可能
   是殘缺的骨架、上一次失敗留下的半成品，或完全不相關的內容，**不要
   嘗試在裡面「抓 bug」**，也**不要自己根據 `description` 編出一份
   `fixed_body`**——`description` 是純機械模板文字，本來就不是給模型
   讀的翻譯依據（見 `06a_plan_agent_architecture.md`：「對模型沒有任何
   有效信號」），你手上又沒有這些函式對應的真實 Java 原始碼，自己猜
   寫的實作即使語法合法，也可能整段偏離真正的業務邏輯。正確做法是設
   `retranslate=true`、`fixed_body=null`，讓 ⑤ 用真實 Java 原始碼重新
   翻譯這個函式（見下方第 2 點「retranslate」欄位說明）；`diagnosis`
   只需要說明「這是 known_fill_failure，需要重新翻譯」以及 `error`
   欄位（本地模型當初失敗的訊息，如格式違反、語法錯誤）這段背景，
   不需要、也不應該自己猜測正確的實作邏輯。這一步**每一個
   known_fill_failures 裡的 task 都要嘗試**，不是「你覺得有關聯才
   做」——本地模型已經證明處理不了這些函式，這是它們被排入下一次真正
   翻譯的唯一機會。只有在這個 task_id 因為其他理由（例如同時也在
   known_scaffold_gaps 裡）判斷完全無法修時，才寫進 unfixable_reasons
   誠實說明
1. 逐一分析 harness_failures，對照 source_files 的實際內容，判斷根本
   原因——注意常見的翻譯品質問題模式：用內建關鍵字（如 list、set、dict、
   type）當變數名稱遮蔽 builtin、查詢缺少明確排序（ORDER BY）、必填欄位
   忘記賦值、import 了不存在的模組或類別、業務邏輯分支跟描述不符、
   訊息文字寫死成錯誤語言等。若 service_diagnostics 非空，優先依上面
   第 9 點的指示核對日誌；若 special_note 或 batch_sibling_modules
   顯示這個模組上一輪是服務啟動逾時的一部分，優先檢查 source_files 有
   沒有明顯的 import／語法層級問題，而不是先假設是業務邏輯比對錯誤
2. 對每一個你判斷「能修好」的 task，在 task_fixes 輸出一筆：task_id
   （只能引用 tasks 清單裡的值，且不能是 known_scaffold_gaps 裡的）、
   diagnosis（根本原因，具體到「哪一行/哪個邏輯錯在哪」）、retranslate、
   fixed_body、file_fixes（見下方說明），三者至少要有一個真正生效
   （retranslate=true，或 fixed_body 非 null，或 file_fixes 非空）
   - retranslate：**這個函式本體本身的業務邏輯寫錯、或從沒被成功生成
     過時的預設做法**——設 `true`、`fixed_body` 設 `null`，把這個 task
     交還給⑤用真實 Java 原始碼重新翻譯，不要自己動手寫 `fixed_body`。
     你看得到的 `source_files` 只是 Python 端現有的內容，你手上**沒有**
     這個函式對應的真實 Java 原始碼可以核對——自己憑 `source_files`／
     `harness_failures` 的症狀反推、寫出來的實作，即使能通過這次測試，
     也可能悄悄偏離真正的業務邏輯（例如漏掉一個你看不到的排序條件、
     猜錯一個你看不到的邊界值）；⑤ 那條路徑看得到真正的 Java 原始碼，
     交給它重新翻譯永遠比你自己憑猜測寫更可靠。`diagnosis` 這時候的
     角色是「告訴⑤上一輪／目前這個版本錯在哪」，會被當成重新翻譯時的
     提示一併附上，寫得越具體（哪個條件、哪一行邏輯、對照哪一筆
     harness_failures），重新翻譯就越可能一次就對
   - fixed_body：**只在你能百分之百確定、完全不需要核對 Java 原始碼
     就能判斷對錯的機械性修正才使用**（例如純粹的變數名稱筆誤、明顯
     的語法錯誤）——這類情況應該很少見，多數函式本體的問題都該用上面
     的 retranslate，不要因為手上剛好看得到 source_files 就順手自己
     改寫業務邏輯。真的要用時：這個函式修正後的完整程式碼本體，直接
     會被拿去取代目前的函式本體，不是給人看的建議文字。格式要求：只寫
     函式本體的陳述式，**不要**包含 `def ...():` 那一行簽名，也不要
     縮排（跟目前簽名同一層級，視覺上像函式體整段往左靠齊）；必須是
     語法完整、可以直接執行的 Python 陳述式，不要省略、不要用「...其餘
     不變」這類佔位文字帶過。若這個函式的本體完全不需要改（問題只出在
     檔案層級，見下方 file_fixes；或該用 retranslate 處理），設為 null
   - file_fixes：若根本原因出在函式本體以外的地方（例如檔案開頭的
     import 敘述、模組層級的常數宣告），retranslate／fixed_body 這兩個
     機制都碰不到函式本體以外的內容，改用這個欄位——每一筆是
     {target_file, old_snippet, new_snippet}：target_file 是這份修正要
     套用到的檔案路徑（必須是 source_files 的其中一個 key）；old_snippet
     必須是 source_files 裡那個檔案目前內容的**逐字**子字串（連同縮排、
     換行都要一模一樣），而且必須在整個檔案裡**只出現一次**——這是精確
     字串取代，不是模糊比對，找不到或出現超過一次都會讓這筆修正直接
     失敗；new_snippet 是取代後的內容。只圈出真正需要改的最小範圍
     （例如只圈一行 import 敘述），不要為了「保險」把整個檔案或整個函式
     都當成 old_snippet——範圍越大，之後這個檔案有其他變動時越容易不再
     逐字相符而套用失敗。這個函式本身若同時也需要改本體，retranslate／
     fixed_body 一樣可以填，不衝突——**但如果 file_fixes 的
     `old_snippet`／`new_snippet` 已經涵蓋這個函式完整的簽名行（含
     `def`/`async def`／裝飾器），也就是這筆 file_fixes 本身已經是整個
     函式（簽名＋本體）的完整替換，這種情況 retranslate 必須是
     `false`、`fixed_body` 必須設為 `null`，不要再重複處理一份幾乎一樣
     的內容**：`retranslate`／`fixed_body` 只會被拿去取代「這個函式目前
     的 body」，如果 file_fixes 已經把整個函式（含簽名）換成新的一份，
     這兩個機制其中任一個生效都等於是在剛換好的新函式外面再包一層，會
     被當成一段合法但完全不會被呼叫的巢狀函式定義寫進去——語法合法、
     語意全壞，且下一輪你自己重新讀到這段巢狀死程式碼時很容易誤判成
     「還沒修好的舊 bug」再修一次，陷入自己跟自己打架的迴圈（真實案例
     見 `docs/09b_bug_trace.md #52`）。判斷準則：這次的修正只需要動到
     簽名本身（例如把 `def` 改成 `async def`、加裝飾器），且函式內部
     原有的業務邏輯不變，一律只用 file_fixes 做完整替換、retranslate
     設 `false`、fixed_body 設 `null`；只有當函式簽名不動、只有內部
     陳述式需要修正時，才用 retranslate（或極少數情況用 fixed_body）、
     不需要 file_fixes
   - **根因若確實是檔案層級的問題（缺 import、模組層級常數/enum 沒被
     正確引用等），必須用 file_fixes 精確補上這個缺口，不要為了避開它
     而在 retranslate／fixed_body 裡改寫業務邏輯繞道**——例如某個函式引用了一個未
     import 的 enum／常數，正確做法是用 file_fixes 補上那個 import，
     讓函式繼續依原本的方式引用該 enum／常數；不要因此把函式本體改成
     不再依賴那個 enum／常數（例如原本該回傳 ErrorCode.XXX.value 這種
     具名錯誤碼，被改成直接寫死一個你自己猜的數字或訊息文字）。這種
     繞道即使剛好能通過驗證，也已經悄悄偏離了 source_files／description
     描述的原始業務邏輯，是比「保留 import 缺口、明確回報 unfixable」
     更差的結果——如果你判斷不出正確的 import 路徑，寧可誠實地把這個
     task 的原因寫進 unfixable_reasons，不要用猜的數值掩蓋過去
3. 若某個失敗的根本原因你判斷是 known_scaffold_gaps 裡的函式造成的，
   或是你判斷不出任何函式層級或檔案層級的可行修法（例如需要新增一個
   tasks 清單裡完全沒有的檔案／函式，或問題實際上出在
   batch_sibling_modules），寫進 unfixable_reasons，用一句話說明原因，
   不要勉強塞一個 task_fix
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
                    "retranslate": {"type": "boolean"},
                    "fixed_body": {"type": ["string", "null"]},
                    "file_fixes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target_file": {"type": "string"},
                                "old_snippet": {"type": "string"},
                                "new_snippet": {"type": "string"},
                            },
                            "required": ["target_file", "old_snippet", "new_snippet"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["task_id", "diagnosis", "retranslate", "fixed_body", "file_fixes"],
                "additionalProperties": False,
            },
        },
        "unfixable_reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["root_cause_summary", "fixable", "task_fixes", "unfixable_reasons"],
    "additionalProperties": False,
}
```

**`file_fixes` 完整替換簽名＋本體時 `fixed_body` 必須為 `null`（10a 八章 phase 2「`fixed_body` 與 `file_fixes` 同時作用...」）**：這段新增文字對應缺陷總表 #52——2026-08-28 真實環境重跑撞到 ⑦ 對 `task_066` 同時給了完整替換函式的 `file_fixes` 與違反格式契約（夾帶裝飾器／簽名行）的 `fixed_body`，`translator_cli.client.py::fill_function()` 的 AST body-splice 把後者當成陳述式插進前者剛換好的新函式裡，寫出巢狀死程式碼，且後續輪次 ⑦ 反覆誤判成舊 bug 重修，耗光整輪 retry 預算。單元測試見 `tests/debug_agent/test_prompts.py::TestFileFixesFullReplacementForbidsRedundantFixedBody`。下游同時在 `translator_cli/python_adapter.py::extract_body_statements()` 加固了一道對應防線（見 07b 六章），不只靠 prompt 這一層。

**`known_fill_failures` 一律要求輸出（10a 四章「一旦失敗過一次，改由⑤帶著⑦的診斷重新翻譯」）**：`DEBUG_SYSTEM_PROMPT` 第 0 點是 2026-08-27 真實環境重跑逼出的修正——`analysis.py`／`triage.py` 早就把 `known_fill_failures` 連同 `tasks`（含 `description`）一起送進同一次呼叫，`_validate_task_fixes()` 的合法 task_id 集合也早就涵蓋這些 task，底層機制完全撐得住，缺的只有這段強制性的 prompt 指示。**2026-09-05 再次修正（`docs/refactor_bug_trace.md` #9）**：最初的版本要求 ⑦ 對這些 task「從零生成」`fixed_body`，後來查證發現 ⑦ 手上根本沒有真實 Java 原始碼、只有 `06a_plan_agent_architecture.md` 自己判定「對模型沒有任何有效信號」的 `description` 欄位可用——改成新增的 `retranslate` 欄位，`fixed_body=null`，交還給⑤帶著 ⑦ 的 `diagnosis` 重新翻譯（見五章 `pending_retranslate_tasks`）。單元測試見 `tests/debug_agent/test_prompts.py::TestKnownFillFailuresMandatoryRetranslate`／`TestRetranslatePreferredOverFixedBody`。

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


def compute_zero_endpoint_modules(module_list: list[dict], route_module_mapping: dict[str, str]) -> set[str]:
    """對應 10a 四章「這條規則仍然不夠」（docs/09b_bug_trace.md #49）：
    找出「沒有任何 HTTP endpoint」的模組——這種模組的程式碼（如
    common_service.py）即使被其他模組的 task 當成 referenced_interfaces
    引用，test_results.failures[*].module 也永遠不會出現它，3.2 的模組
    篩選機制永遠不會選中它去分析，只能靠 4.1 疊加 target_files 時被
    「看到」。route_module_mapping 的值（③ 產生、唯一權威來源，見
    02a 十三章、refactor_harness.core.route_mapper.RouteMapper.
    module_mapping）就是「真的擁有 route 的模組」全集，module_list
    裡其餘的都是零 endpoint 模組。純資料轉換，不做檔案 I/O——呼叫端
    負責讀 config/harness.yaml，這裡才好測試。
    """
    modules_with_routes = set(route_module_mapping.values())
    return {m["module"] for m in module_list} - modules_with_routes
```

### `debug_agent/analysis.py`

對應 10a 四、五、七章核心邏輯。

```python
"""⑦ Debug Agent 核心邏輯：四章「Module 級 LLM 分析」（依模組平行呼叫，
比照 05a 六章「依模組取代整包 Map-Reduce」的既有模式，非 map-reduce）、
五章 task_id 反查驗證、七章 give_up_early 判斷、六章 retry_count 遞增。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from pathlib import Path, PurePosixPath

from common.concurrency import default_concurrency
from common.llm_client import LlmJsonError, call_claude_for_json
from debug_agent.llm import DEBUG_AGENT_MAX_TOKENS, DEFAULT_MODEL
from debug_agent.prompts import DEBUG_OUTPUT_SCHEMA, DEBUG_SYSTEM_PROMPT
from debug_agent.triage import ModuleFailureContext, build_module_contexts, compute_zero_endpoint_modules, module_tasks
from graph.state import DebugRound, FileFix, RefactorState, TaskFix
from refactor_harness.core.route_mapper import RouteMapper

logger = logging.getLogger(__name__)

_MAX_MAP_WORKERS = default_concurrency()
# 比照 design_agent/plan_agent 既有的批次重試慣例（同一顆常數
# _RETRY_WAIT_SECONDS=300.0）：SDK 層級的 max_retries（見
# common/llm_client.py）已經處理過短暫的傳輸層錯誤，這裡的等待是給
# 「比 SDK 重試窗口更久」的暫時性外部服務中斷一個恢復機會。跟
# design_agent/plan_agent 不同的地方只在於重試後仍失敗時的處理方式：
# 那兩個 Agent 會中止整條 run，這裡改成把該 module 視為「未分析」
# （見 10a 4.4、七章「give_up_early 的判斷邊界」），不阻斷其餘 module
# 的分析結果，也不讓整個 debug 節點失敗。
_RETRY_WAIT_SECONDS = 300.0

# 固定文字，比照 09a 五章 _RELATIONSHIP_GAP_NOTICE 的既有慣例：機械可
# 確定的結論不需要重新問 LLM 一次措辭（見 10a 3.3）。
_SCAFFOLD_GAP_REASON = "④ 骨架階段未渲染此模組任何函式（100% 骨架缺口），無法透過重新生成修好，需人工介入或等待重新 scaffold"
_MODULE_MISMATCH_REASON = "route_to_module_mapping／task_list 找不到這個 module 對應的 task，需人工核對③/[P]輸出或 skip 呼叫鏈設定，見 02a 十六章"

# 保留模組名稱，跟 design_agent/design.py、parse_agent/summarize.py、
# plan_agent/module_index.py 各自獨立定義的同名常數是同一個字面值（見
# 10a 四章「_global 的檔案永遠不會被任何模組的 referenced_interfaces
# 引用」）——這幾個套件之間沒有共用常數的既有慣例，這裡沿用同一種
# 「各自定義一份」的作法，不新開先例。
_GLOBAL_MODULE_NAME = "_global"


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


def _is_global_infra_file(
    path: str,
    zero_endpoint_modules: frozenset[str] = frozenset(),
    file_to_module: dict[str, str] | None = None,
) -> bool:
    """10a 四章「過濾規則不能只列 schemas/models，還必須涵蓋 core」：
    target_files 除了 schemas/models/core 外還可能帶入
    referenced_interfaces（跨 module 的 router／service／repository
    唯讀參考），無差別聯集會稀釋 context、增加不必要的 token 成本。只取
    父目錄落在 schemas／models／core 任一個的項目——這三者共同的性質是
    「機械產生、不對應任何 TaskSpec，只能透過其他 task 的唯讀 context
    被看到」（05a 三章「全域基礎設施檔案」）——只列 schemas/models 會讓
    透過 app/core/exception_handlers.py 這類共用輔助檔案造成的問題完全
    看不到原始碼，因此一併涵蓋 core。

    **10a 四章「這條規則仍然不夠」、docs/09b_bug_trace.md #49 的修法**：
    純目錄名判斷漏掉「有擁有 task、但擁有它的模組自己沒有 HTTP
    endpoint」這一類檔案（如 app/services/common_service.py）——這種
    module 永遠不會出現在 test_results.failures，3.2 的模組篩選機制
    永遠選不中它，只能靠這裡被其他 module 的 referenced_interfaces 帶進
    context。`zero_endpoint_modules`（`triage.compute_zero_endpoint_modules()`
    算出）、`file_to_module`（`{target_files[0]: module}`，見呼叫端）都是
    可選參數、預設值等同舊行為（只看目錄名）——這是刻意的向後相容設計，
    既有測試以單一參數呼叫這個函式，不該被這次擴充打破。

    `_global` 保留模組（如 app/core/exception_handlers.py）不歸這裡管：
    它從未被任何模組的 referenced_interfaces 引用，連「被聯集進
    target_files」的機會都沒有，必須無條件塞進每個 root_cause module 的
    context，見 run_debug_analysis() 的 global_module_source_files。
    """
    if PurePosixPath(path).parent.name in ("schemas", "models", "core"):
        return True
    if file_to_module is None:
        return False
    owning_module = file_to_module.get(path)
    return owning_module is not None and owning_module in zero_endpoint_modules


def _analyze_root_cause_module(
    ctx: ModuleFailureContext,
    module_summary: str,
    all_tasks: list[dict],
    python_project_path: str,
    service_diagnostics: str | None,
    zero_endpoint_modules: frozenset[str],
    file_to_module: dict[str, str],
    global_module_source_files: dict[str, str],
    run_id: str,
) -> dict:
    """單一 module 的 Claude API 呼叫，回傳 output schema 的原始解析結果。
    呼叫失敗時往上拋 LlmJsonError，由呼叫端的重試佇列機制接手（見
    run_debug_analysis()）。

    `run_id`：對應真實 pipeline 這一輪的 run_id，必填、往下傳給
    `call_claude_for_json()`——原本沒有傳這個參數，`call_claude_for_json()`
    省略時會各自呼叫 `adhoc_run_id()`，導致每一次⑦的分析呼叫都散落在
    自己獨立的 `adhoc_*` run_id 底下，用 `llmlog recent --run <真正的
    run_id>` 完全查不到任何⑦的呼叫紀錄，只能改用時間窗＋caller 名稱
    去撈，違背 11a「查某個 run 到底做了什麼」的查詢設計初衷。

    service_diagnostics：10a 四章「service_diagnostics 要傳給所有
    root_cause module」——這一輪所有 root_cause module 統一傳入，不限於
    special_reason=="batch_reload_timeout" 的 module 才傳（正常情境下是
    None，見呼叫端）。

    zero_endpoint_modules／file_to_module：docs/09b_bug_trace.md #49 修法，
    傳給下面 _is_global_infra_file() 判斷 target_files[1:] 時使用。
    global_module_source_files：`_global` 保留模組的原始碼，見
    run_debug_analysis()，無條件併入 source_files，不受任何過濾規則篩選
    ——`_global` 的檔案從未被任何模組的 referenced_interfaces 引用，沒有
    「被聯集進 related_files」的機會。
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
        # 有 related_files 可用時，「這個 task 自己的主要寫入目標」
        # （target_files[0]）永遠納入——這是 all_tasks 本來就限定「這個
        # module 底下的 task」（見 module_tasks()），target_files[0] 就是
        # 這個 module 自己的程式碼，不是別的 module 引用進來的東西，不該
        # 被下面 schemas/models/core 的過濾規則擋住。
        #
        # 真實環境重跑時發現的缺陷：route_to_file_mapping 只涵蓋 routers／
        # services／repositories 三層，但只針對「這個 endpoint 呼叫鏈上
        # 的檔案」，涵蓋不到 CollectionUtil.find_distinct_field() 這種
        # 真正的根因所在、卻不屬於任何單一 endpoint 呼叫鏈的共用工具檔案
        # （見 docs/09b_bug_trace.md #43）——這種檔案若剛好是這個 module
        # 自己某個 task 的 target_files[0]，過濾前的版本會把它跟「跨
        # module 引用」的 referenced_interfaces 混為一談整個濾掉，讓
        # Debug Agent 看不到真正藏著 bug 的程式碼、給出錯誤診斷（實測
        # 案例：exam-platform-api 的 CollectionUtil.find_distinct_field()
        # 用 list 當參數名遮蔽 builtin，這正是這個 module 自己的 task，
        # 卻因為父目錄是 services 而非 schemas/models/core 被濾掉）。
        #
        # target_files[1:]（referenced_interfaces 帶進來的唯讀參考檔案，
        # 見 06a 七章）才是真正該套用 schemas/models/core 過濾的對象——
        # 這些才是「這個 task 讀但不寫」的其他 module 檔案，過濾理由（見
        # 下方「為什麼要限縮」）只對這部分成立。
        for t in all_tasks:
            if t["target_files"]:
                related_files.add(t["target_files"][0])
            related_files |= {
                tf for tf in t["target_files"][1:]
                if _is_global_infra_file(tf, zero_endpoint_modules, file_to_module)
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
    # `_global` 保留模組無條件併入，不經過上面任何過濾規則——見本函式
    # 開頭 docstring、docs/09b_bug_trace.md #49。source_files 的 key 已經
    # 用 related_files（真正的 target_files 路徑）算過一輪，這裡直接用
    # dict 聯集，被跳過重複讀取也不會產生副作用。
    source_files = {**global_module_source_files, **source_files}

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
        run_id=run_id,
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
    # harness_failures 裡的 body_diff（DeepDiff 格式）可能含 Python type
    # 物件（如 {'old_type': <class 'int'>}），json.dumps 沒有 default=str
    # 兜底會直接 TypeError——這是真實環境重跑時實際發生過的內容形狀，跟
    # refactor_harness/langgraph_nodes/test_nodes.py::run_postman_tests()
    # 寫 logs/report_{run_id}.json 時遇到的同一個坑。
    return json.dumps(payload, ensure_ascii=False, default=str)


def _validate_task_fixes(raw_task_fixes: list[dict], valid_task_ids: set[str]) -> list[TaskFix]:
    """10a 4.3：task_id 反查驗證，捨棄不在合法集合內的 task_fix 並記警告，
    不中止整個 module 的分析。10a 八章「phase 2」：也捨棄
    retranslate／fixed_body／file_fixes 三者都沒有真正生效的 task_fix
    ——這種輸出沒有任何實際修正內容，留著只會在 pending_fixed_bodies／
    pending_retranslate_tasks／pending_file_fixes 裡製造一筆什麼都不做
    的空紀錄。

    對應 `docs/refactor_bug_trace.md` #9：`retranslate=True` 時強制把
    `fixed_body` 視為 `None`（即使 LLM 違反 prompt 指示、兩者都給了值）
    ——`retranslate` 優先，理由見 `debug_agent/prompts.py`：⑦ 沒有真實
    Java 原始碼可以核對，這種情況下自己寫的 `fixed_body` 可靠度低於
    交給⑤重新翻譯，不能讓一次沒遵守指示的回應繞過這個保護。
    """
    validated: list[TaskFix] = []
    for tf in raw_task_fixes:
        if tf["task_id"] not in valid_task_ids:
            logger.warning(
                "Debug Agent 回應引用了不存在或不合法的 task_id=%s，捨棄該筆 task_fix", tf["task_id"]
            )
            continue
        retranslate = bool(tf.get("retranslate", False))
        fixed_body = tf["fixed_body"]
        if retranslate and fixed_body is not None:
            logger.warning(
                "task_id=%s：Debug Agent 同時給了 retranslate=true 與非 null 的 fixed_body（違反 "
                "prompt 指示），優先採用 retranslate、捨棄這次的 fixed_body 內容",
                tf["task_id"],
            )
            fixed_body = None
        if not retranslate and not fixed_body and not tf["file_fixes"]:
            logger.warning(
                "Debug Agent 對 task_id=%s 的回應 retranslate／fixed_body／file_fixes 三者皆空，"
                "捨棄該筆 task_fix",
                tf["task_id"],
            )
            continue
        file_fixes = [
            FileFix(
                task_id=tf["task_id"], target_file=ff["target_file"],
                old_snippet=ff["old_snippet"], new_snippet=ff["new_snippet"],
            )
            for ff in tf["file_fixes"]
        ]
        validated.append(TaskFix(
            task_id=tf["task_id"], diagnosis=tf["diagnosis"], retranslate=retranslate,
            fixed_body=fixed_body, file_fixes=file_fixes,
        ))
    return validated


def _run_batch(
    contexts: list[ModuleFailureContext],
    module_summaries: dict[str, str],
    task_list: list[dict],
    python_project_path: str,
    service_diagnostics: str | None,
    zero_endpoint_modules: frozenset[str],
    file_to_module: dict[str, str],
    global_module_source_files: dict[str, str],
    run_id: str,
) -> tuple[dict[str, dict], list[ModuleFailureContext]]:
    """平行分析一批 root_cause module，回傳
    (module -> 原始回應, 失敗待重試的 ModuleFailureContext 清單)。每個
    module 的失敗互相隔離，比照 05a 六章「依模組取代整包 Map-Reduce」。
    """
    results: dict[str, dict] = {}
    failed: list[ModuleFailureContext] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_MAP_WORKERS, len(contexts))) as pool:
        futures = {
            pool.submit(
                _analyze_root_cause_module,
                ctx,
                module_summaries.get(ctx["module"], ""),
                [dict(t) for t in module_tasks(task_list, ctx["module"])],
                python_project_path,
                service_diagnostics,
                zero_endpoint_modules,
                file_to_module,
                global_module_source_files,
                run_id,
            ): ctx
            for ctx in contexts
        }
        for future in concurrent.futures.as_completed(futures):
            ctx = futures[future]
            try:
                results[ctx["module"]] = future.result()
            except LlmJsonError as exc:
                logger.warning("module %s 的⑦除錯分析呼叫失敗，列入待重試清單: %s", ctx["module"], exc)
                failed.append(ctx)
    return results, failed


def _run_root_cause_analysis(
    root_cause_ctxs: list[ModuleFailureContext],
    module_summaries: dict[str, str],
    task_list: list[dict],
    python_project_path: str,
    service_diagnostics: str | None,
    zero_endpoint_modules: frozenset[str],
    file_to_module: dict[str, str],
    global_module_source_files: dict[str, str],
    run_id: str,
) -> dict[str, dict | None]:
    """對 root_cause_ctxs 逐一呼叫 Claude，失敗的批次重試一次；比照
    design_agent/plan_agent 既有的批次重試慣例，但重試仍失敗時**不中止
    整個 debug 節點**——這裡回傳的 dict 用 None 標記「視為未分析」（見
    10a 4.4），由 run_debug_analysis() 的 give_up_early 判斷正確排除，
    不當作「LLM 判斷不可修」的證據。
    """
    if not root_cause_ctxs:
        return {}

    results, failed = _run_batch(
        root_cause_ctxs, module_summaries, task_list, python_project_path, service_diagnostics,
        zero_endpoint_modules, file_to_module, global_module_source_files, run_id,
    )
    if not failed:
        return results

    logger.warning(
        "%d 個 module 的⑦除錯分析呼叫失敗，等待 %.0f 秒後統一重試一次: %s",
        len(failed), _RETRY_WAIT_SECONDS, [c["module"] for c in failed],
    )
    time.sleep(_RETRY_WAIT_SECONDS)

    retry_results, still_failed = _run_batch(
        failed, module_summaries, task_list, python_project_path, service_diagnostics,
        zero_endpoint_modules, file_to_module, global_module_source_files, run_id,
    )
    results.update(retry_results)
    for ctx in still_failed:
        results[ctx["module"]] = None
    return results


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
    retry_count = state.get("retry_count", 0)  # 這一輪呼叫當下的值，用於 DebugRound.round（遞增前）
    new_retry_count = retry_count + 1

    contexts = build_module_contexts(state)
    # 若 test_results.reason=="service_unreachable" 且 state["failed_modules"]
    # 也是空，contexts 會是空清單——不另外分岔，直接沿用下面一般路徑：
    # root_cause_ctxs 自然也是空，give_up_early 走「root_cause_ctxs 為空」
    # 那條既有規則（見下方），give_up 節點通知人工從 state["service_
    # diagnostics"] 查起，見 10a 三章。

    module_summaries = {m["module"]: m.get("summary", "") for m in state.get("module_list", [])}
    task_list = state.get("task_list", [])
    python_project_path = state["python_project_path"]

    root_cause_ctxs = [c for c in contexts if c["origin"] == "root_cause"]

    # 10a 四章「service_diagnostics 要傳給所有 root_cause module」：這一輪
    # 所有 root_cause module 統一傳入，不限於 special_reason=="batch_reload_
    # timeout" 的 module，理由見該節「為什麼不是只在...才傳」。正常情境
    # 下（⑥ 沒有遇到 service_unreachable）這裡是 None，不影響一般案例。
    service_diagnostics = state.get("service_diagnostics")

    # docs/09b_bug_trace.md #49 修法（10a 四章）：算出「沒有任何 HTTP
    # endpoint」的模組全集，供 _is_global_infra_file() 判斷 target_files
    # [1:] 時使用；順便無條件讀出 `_global` 保留模組的原始碼（它從未被
    # 任何模組的 referenced_interfaces 引用，見 _is_global_infra_file()
    # docstring），等一下塞給每一個 root_cause module，比照
    # service_diagnostics「給所有 root_cause module」的既有模式。全部
    # 條件在 root_cause_ctxs 非空時才算——沒有任何 module 要分析時，連
    # RouteMapper() 這個檔案讀取（route_to_module_mapping 是③寫進
    # config/harness.yaml、唯一的權威來源，不在 RefactorState 裡，見
    # 02a 十三章 RouteMapper）都省下來，理由同 give_up_early 案例二的
    # 既有短路精神：這一輪根本不會呼叫 LLM，不需要為了呼叫準備任何 context。
    if root_cause_ctxs:
        zero_endpoint_modules = frozenset(
            compute_zero_endpoint_modules(state.get("module_list", []), RouteMapper().module_mapping)
        )
        file_to_module = {
            t["target_files"][0]: t["module"] for t in task_list if t["target_files"]
        }
        global_module_files = {
            t["target_files"][0]
            for t in task_list
            if t["module"] == _GLOBAL_MODULE_NAME and t["target_files"]
        }
        global_module_source_files = _read_source_files(python_project_path, global_module_files)
    else:
        zero_endpoint_modules = frozenset()
        file_to_module = {}
        global_module_source_files = {}

    analyzed_results = _run_root_cause_analysis(
        root_cause_ctxs, module_summaries, task_list, python_project_path, service_diagnostics,
        zero_endpoint_modules, file_to_module, global_module_source_files, state["run_id"],
    )

    # ⚠️ this_round_rounds 只裝這一次呼叫產生的 DebugRound，絕對不能跟
    # state.get("debug_rounds", [])（掛 operator.add reducer、逐輪累積
    # 的歷史欄位）混用或誤讀——give_up_early 的判斷必須只看這一輪，見
    # 10a 七章「為什麼一定要是這一輪、不能是累積歷史」：歷史紀錄裡可能
    # 混著更早輪次「fixable=True」的舊記錄，用累積歷史判斷會讓
    # give_up_early 被過期資料卡死、永遠回傳 False。
    this_round_rounds: list[DebugRound] = []
    pending_fixed_bodies: dict[str, str] = {}
    pending_retranslate_tasks: dict[str, str] = {}
    pending_file_fixes: list[FileFix] = []

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
        # task_fixes=[]」這種非法狀態。
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
            if tf.get("retranslate"):
                pending_retranslate_tasks[tf["task_id"]] = tf["diagnosis"]
            elif tf["fixed_body"] is not None:
                pending_fixed_bodies[tf["task_id"]] = tf["fixed_body"]
            pending_file_fixes.extend(tf["file_fixes"])

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
        all_analyzed = all(analyzed_results.get(ctx["module"]) is not None for ctx in root_cause_ctxs)
        root_cause_rounds = [r for r in this_round_rounds if r["origin"] == "root_cause"]
        give_up_early = all_analyzed and all(not r["fixable"] for r in root_cause_rounds)

    # 供 give_up_node.py 在 retry_count 用盡時判斷「這一輪是不是 Claude
    # API 本身打不通」，不是 ⑦ 判斷邏輯錯誤——這兩種情況印出的訊息目前
    # 完全一樣，人工得自己去翻 debug_rounds 或 llmlog 才能分辨。這裡只是
    # 把 analyzed_results 已經算出來的資料原樣往外傳，不是新的判斷邏輯。
    unanalyzed_root_cause_modules = [
        ctx["module"] for ctx in root_cause_ctxs if analyzed_results.get(ctx["module"]) is None
    ]

    return {
        **state,
        "retry_count": new_retry_count,
        "debug_rounds": this_round_rounds,  # reducer（operator.add）在這裡才把這一輪併進歷史
        "pending_fixed_bodies": pending_fixed_bodies,
        "pending_retranslate_tasks": pending_retranslate_tasks,
        "pending_file_fixes": pending_file_fixes,
        "give_up_early": give_up_early,
        "unanalyzed_root_cause_modules": unanalyzed_root_cause_modules,
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

## 五、`graph/scheduler.py`／`python_service/manager.py`／`graph/nodes/implement_node.py`／`translator_cli/`——`force_reschedule()` 新增方法、`_python_service` 單例下沉、`_augment_task_io()`／`_run_one_task()` 擴充、`apply_file_fix()`（phase 2）

對應 10a 八章。

### `graph/scheduler.py`：新增 `ModuleScheduler.force_reschedule()`

只新增這一個方法，`ModuleScheduler` 既有方法（`get_ready_tasks()`／`mark_task_done()`／`check_upstream_regression()`／`flag_for_reverify()` 等，01 六章）完全不動：

```python
    def force_reschedule(self, module: str, task_ids: set[str]):
        """由 ⑦ Debug Agent 產生 pending_fixed_bodies 之後呼叫（見
        10a 八章）：把一個 module 打回 "pending"，並把 task_ids 從
        task_done／task_failed 移除，讓 get_ready_tasks() 重新排到它們。

        只改 module_status 不夠——get_ready_tasks() 對 task_id in
        self.task_done／task_failed 的檢查跟 module_status 是各自獨立
        的兩道關卡（見 get_ready_tasks() 本文），"verified" module 底下
        的 task 一定都已經在 task_done 裡（module_ready_for_verification()
        的前提），只打回 pending 不會讓它們重新被排到。只重開
        task_ids 指名的 task，同一 module 底下其他已完成的 task 維持
        完成狀態，不會被誤重新排程——這跟 flag_for_reverify() 的
        "needs_reverify"（那個狀態只觸發重新跑驗證，本來就不影響
        get_ready_tasks()）是不同的機制。
        """
        self.module_status[module] = "pending"
        self.task_done -= task_ids
        self.task_failed -= task_ids
```

插入位置：緊接在既有的 `flag_for_reverify()` 方法之後即可，兩者性質相近（都是外部呼叫端主動改變某個 module 的排程狀態）。

### `graph/nodes/implement_node.py`：`_augment_task_io()`／`_run_one_task()` 擴充

**⑦ 直接產生修正後程式碼，不是自然語言指令**：完整理由與真實案例見 10a 一章「⑦ 的核心任務」、八章——`_augment_task_io()` 不需要處理 ⑦ 的修正，它只剩 09a 五章「疊加規則」這一件事，`_run_one_task()` 直接把 `fixed_body` 傳給 `fill_function()` 的同名參數，完全跳過 ⑤ 本地模型呼叫：

```python
def _augment_task_io(task: TaskSpec) -> tuple[str, list[str]]:
    """對應 09a 五章「疊加規則」：只對 services／repositories 層疊加
    relationship／enum 固定提示，routers 層原樣返回。⑦ Debug Agent
    上一輪針對這個 task 給的修正不經過這裡（見 10a 八章「⑦ 直接產生
    修正後程式碼」）——`pending_fixed_bodies` 裡的內容是完整程式碼，
    直接傳給 `fill_function()` 的 `fixed_body` 參數取代整個函式本體，
    不是疊加進 context 給 ⑤ 本地模型參考。
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

    return context, context_files


async def _run_one_task(
    task: TaskSpec, python_project_path: str, run_id: str, pending_fixed_bodies: dict[str, str]
) -> FillResult:
    """`pending_fixed_bodies` 裡有這個 task 的 id 時，代表 ⑦ Debug Agent
    已經給出修正後的完整函式本體，直接傳給 fill_function() 的
    fixed_body，完全跳過 ⑤ 本地模型呼叫（見 10a 八章）。
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
```

`_run_one_task()` 既有簽名已經帶 `run_id`／`referenced_functions`（見 09b 一章 run_id 傳遞、docs/09b_bug_trace.md #37 的 `referenced_functions`），這裡只新增最後一個 `pending_fixed_bodies` 參數。`run()` 內唯一呼叫 `_run_one_task()` 的地方改成：

```python
        results = await asyncio.gather(
            *(_run_one_task(t, state["python_project_path"], state["run_id"], pending_fixed_bodies) for t in ready)
        )
```

`pending_fixed_bodies`（`state.get("pending_fixed_bodies", {})`）已經在 `force_reschedule()` 呼叫段（下方）算過一次，`run()` 全程重複使用同一份，不需要在 while 迴圈內重讀——`pending_fixed_bodies` 在整個 `run()` 執行期間不會變（⑦ 只在 `debug` node 寫入，`implement` 執行期間是同一份快照）。

**以上是本章最初的版本，`_run_one_task()` 之後又新增了 `java_source`／`referenced_source` 真實原始碼解析（見 09b 對應章節）——下面這段是 2026-09-05 在那之後、對應 `docs/refactor_bug_trace.md` #9 的追加修正，只描述這次新增的部分，不是重寫整個函式。**

### `_run_one_task()`：新增 `pending_retranslate_tasks` 參數（2026-09-05，對應 #9）

**⑦ 沒有真實 Java 原始碼可以核對，函式本體邏輯有問題時不該自己編寫 `fixed_body`**——完整理由見 10a 八章「`pending_retranslate_tasks`」。這裡跟 `pending_fixed_bodies` 的行為刻意相反：命中時**不**跳過模型呼叫，正常解析 `java_source`／`referenced_source`、真的呼叫 `fill_function()`，只是把 ⑦ 的 `diagnosis` 疊加進 `context`：

```python
def _RETRANSLATE_HINT(diagnosis: str) -> str:
    return (
        "⑦ Debug Agent 診斷這個函式先前的版本（或從沒成功生成過）有以下問題，"
        f"這次翻譯請務必依照下方 java_source 的真實邏輯正確實作，避免重蹈同樣的問題：{diagnosis}"
    )


async def _run_one_task(
    task: TaskSpec, python_project_path: str, java_project_path: str, run_id: str,
    pending_fixed_bodies: dict[str, str],
    pending_retranslate_tasks: dict[str, str] | None = None,
) -> FillResult:
    fixed_body = pending_fixed_bodies.get(task["id"])
    context, context_files = _augment_task_io(task)
    retranslate_diagnosis = (pending_retranslate_tasks or {}).get(task["id"])
    if retranslate_diagnosis:
        hint = _RETRANSLATE_HINT(retranslate_diagnosis)
        context = f"{context}\n\n{hint}" if context else hint
    ...  # 其餘不變：fixed_body 為 None 時照常解析 java_source／referenced_source，
        # 呼叫 fill_function(..., fixed_body=fixed_body)——這個 task 不論是
        # 「⑦ 從未出手」還是「⑦ 標記 retranslate」，都走同一條真實模型呼叫路徑，
        # 差異只在 context 有沒有被疊加 ⑦ 的診斷提示。
```

`run()` 內的呼叫點、`force_reschedule()` 呼叫段都同步多讀一份 `pending_retranslate_tasks`（`state.get("pending_retranslate_tasks", {})`），`tasks_to_reopen_by_module` 改成對 `{*pending_fixed_bodies, *pending_retranslate_tasks}` 取聯集——兩份清單一視同仁地觸發 `force_reschedule()`，只有 `_run_one_task()` 內部「要不要跳過模型呼叫」這一步是分岔點。單元測試見 `tests/graph/test_implement_node.py::TestRunOneTaskRetranslate`、`TestForceRescheduleCallSite::test_pending_retranslate_tasks_also_reschedules_verified_module`。

### 新增檔案：`python_service/manager.py`——`_python_service` 單例從 `implement_node.py` 下沉到這裡

對應 10a 八章「診斷資料改走 State，不是 `debug_agent/` 直接 import `implement_node`」——`debug_agent/analysis.py` 不能直接 `import graph.nodes.implement_node`（node 對 node 互相 import，違反本專案 node 檔案只做薄封裝、彼此不互相 import 的既有慣例，見 10a 八章完整推理）。把 `_python_service` 這個單例本身移出 `implement_node.py`，下沉到 `python_service/` 套件：

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

from python_service.java_properties import resolve_config_env_values
from python_service.process import PythonServiceContainer

_python_service: PythonServiceContainer | None = None


async def ensure_started(
    python_project_path: str,
    python_base_url: str,
    java_project_path: str | None = None,
    config_env_vars: list[dict[str, str]] | None = None,
) -> None:
    """`java_project_path`／`config_env_vars`：對應 docs/09b_bug_trace.md
    #46——`config_env_vars` 是 state["python_structure"].get(
    "config_env_vars")`（③輸出，見 graph/state.py），沒有任何 @Value 欄位
    的專案這個 key 不存在，兩個參數都給 None／預設值時 resolve_config_
    env_values() 直接回傳空字典，行為等同這個機制完全不存在。
    """
    global _python_service
    if _python_service is not None:
        return
    extra_env = (
        resolve_config_env_values(java_project_path, config_env_vars)
        if java_project_path and config_env_vars
        else {}
    )
    service = PythonServiceContainer(
        python_project_path=python_project_path,
        base_url=python_base_url,
        database_url=os.environ["DATABASE_URL"],
        extra_env=extra_env,
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


async def _ensure_python_service_started(state: RefactorState) -> None:
    await python_service_manager.ensure_started(state["python_project_path"], state["python_base_url"])


async def stop_python_service() -> None:
    await python_service_manager.stop()
```

`main.py` 既有呼叫點 `await implement_node.stop_python_service()`（09b 三章）不需要改——`implement_node.stop_python_service()` 還在，只是內部改成委派給 `python_service_manager.stop()`，對外介面不變，`main.py` 不用跟著改動。

**後續異動（`docs/09b_bug_trace.md` #46）**：`_ensure_python_service_started()` 的簽名改成只接 `state` 一個參數（原本兩個位置參數 `python_project_path`／`python_base_url` 改從 `state` 取），額外把 `state["java_project_path"]`／`state["python_structure"].get("config_env_vars")` 一併傳給 `ensure_started()`：

```python
# graph/nodes/implement_node.py（再次異動，對應 #46）
async def _ensure_python_service_started(state: RefactorState) -> None:
    await python_service_manager.ensure_started(
        state["python_project_path"],
        state["python_base_url"],
        java_project_path=state["java_project_path"],
        config_env_vars=state["python_structure"].get("config_env_vars"),
    )
```

呼叫點（`run()` 內 `is_first_entry` 判斷，見 09b 二章）從 `_ensure_python_service_started(state["python_project_path"], state["python_base_url"])` 改成 `_ensure_python_service_started(state)`——改傳整個 `state` 而不是兩個位置參數，是因為這個函式現在需要的 State 欄位變多了（`java_project_path`／`python_structure`），逐一列成位置參數會讓呼叫點的參數列表越來越長，不如直接傳 `state` 讓函式自己取用需要的欄位，跟 `run(state: RefactorState)` 本身的既有簽名風格一致。

新增模組 `python_service/java_properties.py`：讀取 Java 端 `application-{profile}.properties`，解析出 `config_env_vars` 每筆 `property_key` 對應的實際值。**profile 選擇不需要新增決策點**——Java 端 `application.properties`（base）本身用 Spring Boot 標準慣例宣告了 `spring.profiles.active`，機械讀取即可：

```python
# python_service/java_properties.py（新增檔案）
"""讀取 Java 端 application-{profile}.properties，解析 @Value 注入用的
實際屬性值，對應 docs/09b_bug_trace.md #46。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_RESOURCES_DIR = "src/main/resources"
_BASE_PROPERTIES_FILE = "application.properties"
_PROFILE_PROPERTIES_TEMPLATE = "application-{profile}.properties"
_PROFILES_ACTIVE_KEY = "spring.profiles.active"


def _parse_properties_file(path: Path) -> dict[str, str]:
    """最小 Java .properties 格式解析器：跳過空行、#／! 開頭的註解行，
    以第一個 = 或 : 分隔 key/value，解碼 \\uXXXX unicode escape（Java
    .properties 標準格式對非 ASCII 字元的既有寫法）。不引入第三方套件
    ——這裡要解析的格式單純，自己實作比引入依賴划算。檔案不存在時回傳
    空字典，不拋例外。
    """
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        for sep in ("=", ":"):
            if sep in stripped:
                key, _, value = stripped.partition(sep)
                result[key.strip()] = value.strip().encode().decode("unicode_escape")
                break
    return result


def resolve_active_profile(java_project_path: str) -> str | None:
    """讀 base application.properties 裡的 spring.profiles.active。找不到
    檔案或找不到這個 key 都回傳 None（記警告，不拋例外）。
    """
    base_path = Path(java_project_path, _RESOURCES_DIR, _BASE_PROPERTIES_FILE)
    base_props = _parse_properties_file(base_path)
    profile = base_props.get(_PROFILES_ACTIVE_KEY)
    if profile is None:
        logger.warning(
            "%s 找不到 %s，無法判斷要疊加哪個 application-{profile}.properties",
            base_path, _PROFILES_ACTIVE_KEY,
        )
    return profile


def resolve_config_env_values(
    java_project_path: str, config_env_vars: list[dict[str, str]]
) -> dict[str, str]:
    """依 config_env_vars（③輸出，每筆 {"property_key": ..., "constant_
    name": ...}）逐一查出 Java 端實際屬性值，回傳 {constant_name: value}。
    合併順序：profile 專屬蓋過 base（比照 Spring Boot 既有合併語意）。
    查不到的 property_key 記警告、略過，不猜值——容器啟動後
    os.environ[...] 的 KeyError 訊息本身就足夠明確，指向這個常數名稱。
    """
    if not config_env_vars:
        return {}

    base_path = Path(java_project_path, _RESOURCES_DIR, _BASE_PROPERTIES_FILE)
    merged = _parse_properties_file(base_path)

    profile = resolve_active_profile(java_project_path)
    if profile is not None:
        profile_path = Path(
            java_project_path, _RESOURCES_DIR, _PROFILE_PROPERTIES_TEMPLATE.format(profile=profile)
        )
        merged.update(_parse_properties_file(profile_path))

    resolved: dict[str, str] = {}
    for entry in config_env_vars:
        property_key = entry["property_key"]
        constant_name = entry["constant_name"]
        value = merged.get(property_key)
        if value is None:
            logger.warning(
                "Java 端 properties 都找不到 property key %r（對應環境變數 %s），"
                "容器啟動時不會注入這個環境變數",
                property_key, constant_name,
            )
            continue
        resolved[constant_name] = value
    return resolved
```

`PythonServiceContainer`（`python_service/process.py`，09b 四章）新增 `extra_env: dict[str, str] | None = None` 建構參數，`start()` 組 `docker run` 指令時逐一附加成額外 `-e` 旗標——完整程式碼與 `docker run` 組裝細節見 09b 四章「`process.py`」。

### 回歸測試

`tests/python_service/test_java_properties.py`（新增）：`resolve_active_profile()` 讀取／找不到 key／檔案不存在三種情況；`_parse_properties_file()` 透過 `resolve_config_env_values()` 間接驗證（跳過註解／空行、`\uXXXX` unicode escape 解碼、`=`／`:` 兩種分隔符）；`resolve_config_env_values()` 的 profile 覆蓋 base、base-only key、查不到時略過不拋例外、沒有 active profile 時仍用 base 查找、多筆 entry 各自獨立解析。`tests/python_service/test_process.py` 新增 `extra_env` 逐一附加成 `-e` 旗標、`extra_env` 為空時指令不受影響兩個案例。`tests/python_service/test_manager.py` 新增 `ensure_started()` 在有／沒有 `java_project_path`／`config_env_vars` 時 `extra_env` 是否正確解析／保持為空。`tests/graph/test_implement_node.py` 新增 `_ensure_python_service_started()` 正確把 `state` 裡的欄位轉傳給 `ensure_started()`。

### `run()` 新增：`force_reschedule()` 的呼叫點

對應 10a 八章「新增：`ModuleScheduler.force_reschedule()`」——只要 `pending_fixed_bodies` 指向一個當下 `module_status=="verified"` 的 module（四章正常的 `root_cause` 分析可能鎖定一個「⑤局部驗證誤判為`verified`」的 module，見 10a 八章完整說明），`get_ready_tasks()` 永遠不會排到它，`fixed_body` 會被平白浪費。緊接在既有的「100% 由 `scaffold_gap_task_ids` 覆蓋的 module」那段掃描（`for module, tasks in scheduler.tasks_by_module.items(): if tasks and all(...)`）之後，新增：

```python
    pending_fixed_bodies = state.get("pending_fixed_bodies", {})
    tasks_to_reopen_by_module: dict[str, set[str]] = {}
    for task_id in pending_fixed_bodies:
        task = next((t for t in state["task_list"] if t["id"] == task_id), None)
        if task is not None:
            tasks_to_reopen_by_module.setdefault(task["module"], set()).add(task_id)
    for module, task_ids in tasks_to_reopen_by_module.items():
        scheduler.force_reschedule(module, task_ids)
```

依 module 分組後才呼叫，是因為 `force_reschedule()` 現在需要知道「這個 module 底下具體是哪些 task_id 要重開」（見五章 `force_reschedule()` 的簽名），不能只傳 module 名稱。

### `translator_cli/git_ops.py`／`translator_cli/client.py`：新增 `commit_file_fix()`／`apply_file_fix()`（phase 2）

對應 10a 八章「⑤ 端的對應改動：phase 2」。`apply_file_fix()` 完整程式碼與 docstring 見 `07b_translator_cli_code.md`（`fill_function()` 旁邊，同一個檔案）——這裡只列 `git_ops.py` 新增的 commit 函式（跟既有 `commit_fill()` 用不同訊息格式，讓 `git log` 能區分兩種性質不同的變更）：

```python
# translator_cli/git_ops.py（新增函式，緊接在既有 commit_fill() 之後）
def commit_file_fix(python_project_path: str, *, task_id: str, target_file: str) -> None:
    _run_git(python_project_path, "add", target_file)
    result = _run_git(
        python_project_path, "commit", "-m", f"debug: {task_id} apply file-level fix in {target_file}"
    )
    if result.returncode != 0:
        raise TranslatorCliError(f"apply_file_fix commit 失敗（task {task_id}）：{result.stderr.strip()}")
```

`apply_file_fix()`（`translator_cli/client.py`）的呼叫介面：

```python
async def apply_file_fix(
    python_project_path: str,
    task_id: str,
    target_file: str,
    old_snippet: str,
    new_snippet: str,
) -> FillResult: ...
```

`old_snippet` 必須在 `target_file` 目前內容裡逐字出現剛好一次，找不到或出現不只一次都回傳 `FillResult(success=False, ...)`；成功時走跟 `fill_function()` 相同的既有關卡（clean tree 檢查、格式化、diff／commit、commit 失敗時的復原），只是定位方式從 AST 函式節點換成精確字串替換。

### `graph/nodes/implement_node.py`：套用 `pending_file_fixes`

不透過排程器（這些修正不對應任何要重新生成的函式本體），在 `is_first_entry` 之後、`task_failures` 初始化之後、排程器 while 迴圈開始之前，新增一段：

```python
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
        await _wait_for_service_reload(state["python_project_path"], state["python_base_url"])
```

**為什麼要顯式呼叫一次 `_wait_for_service_reload()`，不能只依賴 while 迴圈裡既有的 reload-wait**：while 迴圈的 reload-wait（`verify_needs_wait`／`reverify_needs_wait`）只在「這一輪有 task 被排程執行」時才會觸發——若這一輪只有 `pending_file_fixes`、沒有任何 task 需要透過 `get_ready_tasks()` 排程（`file_fixes` 不對應任何函式本體），檔案寫入磁碟後就不會有任何東西觸發既有的 reload-wait，⑥ 下一輪的全量驗證可能讀到還沒 reload 的舊服務狀態。逾時比照 09a 三章「逾時不該讓整條 pipeline 崩潰」的既有精神接住，不特別處理成這幾個 `file_fix` 失敗——「是否真的修好」交給 ⑥ 下一輪的全量驗證判斷。

**`_make_task_failure()` 的 `reason` 參數新增 `"file_fix_failed"`**：`graph/state.py::TaskFailure.reason` 的 `Literal`（09b 二章原始定義）從 `["scaffold_skipped", "fill_failed"]` 擴充成 `["scaffold_skipped", "fill_failed", "file_fix_failed"]`。

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

`tests/graph/test_implement_node.py`（既有測試檔案）補一個斷言：`partial_reports` 裡每一筆都帶有 `"round"` 鍵，值等於呼叫當下傳入的 `state["retry_count"]`。`tests/debug_agent/test_triage.py`（三章既有測試）新增「模組 A 在 round 0 有 `batch_reload_timeout`、round 1 沒有任何新紀錄（`partial_reports` 裡完全沒有 round=1 的 A）、模組 C 在 round 1 才第一次遇到 `batch_reload_timeout`」的案例——斷言 C 在 round 1 的 `batch_sibling_modules` **不包含** A（A 的紀錄停留在 round 0，不是 round 1 的最新紀錄，屬於過期資料，這個案例專門鎖住不能回歸）。

---

## 六、`graph/builder.py`——`debug` 出邊改條件邊

對應 10a 七章「路由方式」。

```python
    # debug 迴圈：give_up_early（10a 七章：這一輪所有 root_cause module
    # 都判定不可修）路由到 give_up，不進 implement 浪費一輪重試；否則
    # 照舊回 implement 讓 pending_fixed_bodies 生效。
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

### `graph/nodes/give_up_node.py`：新增 `give_up_early` 訊息分支

對應 10a 十章「`give_up_node.py` 依抵達路徑分三種訊息」。既有的 `scaffold_done is False`／其餘（retry_count 用盡）兩種分支中間插入一種：

```python
    if state.get("scaffold_done") is False:
        print("[GIVE_UP] ④ 骨架生成失敗（generate_scaffold() success=False），跳過重試迴圈，等待人工介入")
        print(f"skipped_interfaces: {state.get('skipped_interfaces')}")
        print(f"skipped_db_models: {state.get('skipped_db_models')}")
    elif state.get("give_up_early"):
        print(f"[GIVE_UP] ⑦ Debug Agent 判斷已無可修（非重試次數用盡，目前 retry_count={state['retry_count']}），等待人工介入")
        print(f"Debug rounds: {state.get('debug_rounds')}")
        print(f"Failed modules: {state['failed_modules']}")
    else:
        print(f"[GIVE_UP] 超過重試次數 ({state['retry_count']})，等待人工介入")
        print(f"Failed modules: {state['failed_modules']}")
        print(f"Blocked modules: {state['blocked_modules']}")
        # 見 10a 十章「retry_count 用盡那個分支本身，還要再分辨...」：
        # 「連續三輪 Claude API 都打不通」跟「⑦ 判斷邏輯本身有問題」這兩
        # 種情況，上面兩行印出的訊息完全一樣。unanalyzed_root_cause_modules
        # 非空代表最後一輪至少有 module 完全沒能成功呼叫到 Claude（見
        # debug_agent/analysis.py），提示人工先查 llmlog，不要急著懷疑
        # ⑦ 的判斷邏輯。
        if state.get("unanalyzed_root_cause_modules"):
            print(
                f"[GIVE_UP] 注意：最後一輪這些 module 的 Claude API 呼叫（含重試）仍然失敗，"
                f"完全沒能取得分析結果，可能是 API 本身的問題，不是 ⑦ 判斷錯誤，"
                f"建議先用 llmlog 查對應 trace 再判斷：{state['unanalyzed_root_cause_modules']}"
            )
```

`elif` 順序固定：`give_up_early` 這條新路徑走 `debug → give_up` 直接邊，不經過 `should_debug_or_done()`，`state["retry_count"]` 是正常遞增的真實次數，若判斷順序顛倒、落到最後的 `else` 分支，會誤導成「已經試滿所有次數才放棄」。

### 回歸測試

`tests/graph/test_give_up_node.py`：三種分支各自的訊息斷言，另外鎖住 `unanalyzed_root_cause_modules` 非空時多印出的那行提示（含「非空時才印，空清單不誤觸發」的防回歸案例）。

---

## 七、`main.py`——`initial_state` 新增欄位

```python
    initial_state = {
        ...
        "test_results": {},
        "debug_rounds": [],
        "pending_fixed_bodies": {},
        "pending_retranslate_tasks": {},  # 2026-09-05 新增，對應 docs/refactor_bug_trace.md #9
        "pending_file_fixes": [],
        "give_up_early": False,
        "retry_count": 0,
    }
```

比照既有 `Annotated[list, operator.add]`／一般欄位在 `initial_state` 顯式初始化的既有慣例（01 九章「顯式全部初始化」）。

---

## 八、`common/run_report.py`——pipeline 執行完的制式報告（新增檔案）

對應 10a 十章「與 LangGraph 整合」補充的一小節：整條 graph run 結束後，`main.py` 落地一份人工事後看得到的制式報告。

**動機**：`debug_rounds`／`task_failures`（`graph/state.py` 六、七章）是逐輪累積的完整歷史，但這個專案目前沒有接 LangGraph 的 checkpointer——`graph.astream()` 執行完，process 一結束，這些只活在記憶體裡的 State 就整個消失。`main.py` 原本的收尾只有幾行 `print(final_state.get(...))`，連 `debug_rounds` 都沒印出來——⑦ 這一輪到底診斷過什麼、修法有沒有用，執行完就再也查不到，只能回頭挖 `llmlog`／`logs/orchestrator.log` 湊。這個檔案把這些已經存在、只是留不住的資料落地成兩份檔案。

**跟既有 `logs/report_{run_id}.json`（一章，`run_postman_tests()` 寫的）是不同層級的東西，不是重複建設**：那份是⑥每一輪驗證完整覆寫的最新結果，只留得住最後一輪；這裡是**整條 run 結束時**寫一次的總結，涵蓋所有輪次的 `debug_rounds`／`task_failures` 累積歷史。

```python
# common/run_report.py（新增檔案）
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

from common.llm_trace import list_recent
from graph.state import RefactorState

REPORTS_ROOT = os.path.join("logs", "reports")
```

**`REPORTS_ROOT` 用 `os.path.join()`，不是字面字串 `"logs/reports"`**：Windows 上混用正斜線字面字串跟 `os.path.join()` 組出來的路徑會產生 `logs/reports\...` 這種正反斜線混雜的路徑——這是真實環境測試時實際撞到、讓一個路徑斷言測試失敗的問題，不是預先設想的風險。

### `_classify_outcome()`：跟 `give_up_node.py` 用同一套判斷順序

```python
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
```

**新增 `"done"` 這個 `give_up_node.py` 本身沒有的分類**：`give_up_node.py`（10a 十章、六章）只在真的走到 `give_up` 節點時才執行，不會處理「成功完成」這個結局；這個函式除了報告要用還得涵蓋 `test_results.status == "pass"` 這個 `give_up_node.py` 完全不會遇到的情況，因此在判斷順序最前面多加這一條，其餘三種分類跟 `give_up_node.py` 的既有邏輯逐條對應。

### `_date_folder_from_run_id()`：日期資料夾防同日多次執行互相覆蓋

```python
def _date_folder_from_run_id(run_id: str) -> str:
    date_part = run_id.split("_", 1)[0]  # "YYYYMMDD"（adhoc_ 前綴的 run_id 不會走到這裡，見 write_run_report）
    return f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
```

**檔名本身就帶完整時間戳＋uuid，同一天執行第二次不會互相覆蓋**：`run_id` 格式是 `{YYYYMMDD_HHMMSS}_{uuid4前6碼}`（見 `common/run_context.py::new_run_id()`），檔名直接用完整 `run_id`（見下方 `write_run_report()`／`write_human_readable_report()`）——同一天不論執行幾次，每次的 `run_id` 秒數與 uuid 都不同，檔名天生不會碰撞，不需要額外的序號或覆蓋確認機制。

### `_compute_summary()`：從既有欄位彙整衍生資料，只給人類可讀報告用

```python
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
    章）——曾被 ⑦ 標記為 root_cause、且最後真的落在 `verified_modules`
    裡的 module，才是有把握說「修好了」的判斷。

    **用正面訊號（`verified_modules`），不是「不在 failed_modules／
    blocked_modules 裡」這種負面推論**（對應 docs/refactor_bug_trace.md
    #13 真實案例）：module 卡在 `"in_progress"` 永遠到不了終態時（如
    #12 死結修正前，部分 task 成功、但 router 從沒被排到），既不會落在
    `failed_modules`（沒觸發過失敗判定）、也不會落在 `blocked_modules`
    （不是從沒動過的 `"pending"`）——用「兩個負面集合都沒抓到＝修好了」
    會把這種「其實還卡著、只是卡法比較隱蔽」的情況誤判成已解決。
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

    verified = set(state.get("verified_modules", []))
    modules_fixed = sorted(set(modules_ever_flagged) & verified)
    modules_still_broken = sorted(set(modules_ever_flagged) - verified)

    # 對應 docs/refactor_bug_trace.md #33：「曾經翻譯失敗（含後來補救成功
    # 的）」這行原本只算 State 的 task_failures，但本地模型（qwen）呼叫
    # 失敗、退回 Claude 才成功的情況完全不會進 task_failures（task 整體
    # 是成功的，只有第一次嘗試失敗），這個訊號目前只活在 llm_traces.db
    # 裡，沒有真實案例（`e867f4` 這輪 13 個函式）就沒人會發現——不用另外
    # 起一份新 log，llm_traces.db 本來就記錄了每次呼叫的 vendor／status，
    # 這裡直接查出來、讓它在報告裡看得到，不需要每次都靠 `llmlog` 手動
    # 排查才能注意到。
    local_model_call_failures = sorted({
        t.task_id for t in list_recent(status="error", run_id=state["run_id"])
        if t.vendor == "ollama" and t.task_id
    })

    return {
        "total_tasks": len(task_list),
        "translated_successfully": len(completed_tasks),
        "tasks_with_translation_issues": tasks_with_translation_issues,
        "permanently_unfillable_tasks": permanently_unfillable_tasks,
        "local_model_call_failures": local_model_call_failures,
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

    if summary["local_model_call_failures"]:
        lines.append(
            f"- 本地模型（qwen）呼叫失敗、退回 Claude 才成功：{len(summary['local_model_call_failures'])} 個"
            f" → {', '.join(summary['local_model_call_failures'])}（見 docs/refactor_bug_trace.md #33）"
        )
    else:
        lines.append("- 本地模型（qwen）呼叫失敗、退回 Claude 才成功：0")

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
```

**這是給人看的總覽，不是原始資料的另一種格式**：直接印 `debug_rounds`／`task_failures` 的完整清單只是把同一份資料換個檔案放，不會比翻 State 本身更好讀——`_render_human_readable_report()` 只回答「翻譯了多少、幾個曾出包、⑦ 修了幾個、還剩什麼沒解決」這幾個問題，細節（`body_diff`、完整診斷文字）留在 `write_run_report()` 的 JSON 版本，兩份報告的定位刻意不同，不是同一份內容的重複輸出。

### `write_run_report()`：制式 JSON，程式／未來工具解析用

```python
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
```

`debug_rounds` 逐字帶入完整內容（不像 `test_summary` 只取彙整後的 summary）——這份 JSON 是給程式或未來工具解析用，`task_fixes[*].fixed_body`／`file_fixes` 的完整內容是回溯「⑦ 到底改了什麼」唯一還留得住的地方，不能只留摘要。

### `main.py` 呼叫點

見七章 `initial_state`；graph run 結束後緊接著呼叫（完整位置見 `main.py::main()` 收尾區塊）：

```python
report_path = write_run_report(final_state)
summary_path = write_human_readable_report(final_state)
print(f"Run Report (JSON): {report_path}")
print(f"Run Summary (Markdown): {summary_path}")
```

### 回歸測試

`tests/common/test_run_report.py`（新增檔案）：

- `TestWriteRunReport`：日期資料夾正確從 `run_id` 推導、同一天執行兩次不互相覆蓋（不同 `run_id` 天生不碰撞）、JSON 內容與 State 一致、只帶 `test_summary` 不重複完整 `failures` 清單
- `TestClassifyOutcome`：`pass` → `done`；`scaffold_done is False` 優先權最高；`give_up_early`／`retry_count` 用盡兩種 give up 分開判斷
- `TestWriteHumanReadableReport`：Markdown 跟 JSON 寫進同一個 date 資料夾、寫 Markdown 不影響 JSON 的既有格式、`_compute_summary()` 的翻譯總數／出包數／修好數／仍未解決數正確衍生、`unanalyzed_root_cause_modules` 非空時正確在 Markdown 裡標出來、全部指標為零時的邊界情況、繁體中文的 outcome 標籤正確渲染

---

## 九、`tests/`——新增與既有測試同步更新

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
│   │                                   # task_id；phase 2：fixed_body 為
│   │                                   # None 但 file_fixes 非空仍是有效
│   │                                   # task_fix，兩者都空才捨棄；
│   │                                   # run_debug_analysis() 把
│   │                                   # task_fixes[].file_fixes 攤平成
│   │                                   # state["pending_file_fixes"]、
│   │                                   # 每一筆補上來源 task_id；
│   │                                   # give_up_early 判斷需鎖住
│   │                                   # 四種情境（防回歸）：
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
│   │                                   # 這件事，見 10a 七章）；(5)
│   │                                   # test_results.reason ==
│   │                                   # "service_unreachable" 且
│   │                                   # failed_modules 也為空時，
│   │                                   # build_module_contexts() 回傳空
│   │                                   # 清單，give_up_early 必須是 True
│   │                                   # ——這只是情境 (2) 的一個具體
│   │                                   # 觸發途徑，不需要另外的分支邏輯，
│   │                                   # 見 10a 三章；
│   │                                   # mock call_claude_for_json 驗證
│   │                                   # payload 組裝（含 batch_sibling_modules、
│   │                                   # related_files 與 target_files 聯集
│   │                                   # 只取 schemas／models／core、排除跨
│   │                                   # module 的 referenced_interfaces；
│   │                                   # harness_failures 為空時（見
│   │                                   # service_unreachable 分支）必須
│   │                                   # 改回聯集整份 target_files、不
│   │                                   # 過濾——routers／services／
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
│   │                                   # 診斷內容，不能只限於 special_reason
│   │                                   # 命中時才傳（見 10a 四章）
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
    ├── test_give_up_node.py             # 三種訊息分支（見六章）
    └── test_implement_node.py           # 既有測試同步更新：_augment_task_io()
                                        # 少一個引數（不再處理 ⑦ 的修正）；
                                        # 新增「_run_one_task() 傳
                                        # fixed_body 給 fill_function()」的
                                        # 案例（有命中／沒命中兩種）；新增
                                        # 「pending_fixed_bodies 指向一個
                                        # module_status=="verified" 的
                                        # module 時，force_reschedule() 正確
                                        # 把它打回 pending，get_ready_tasks()
                                        # 下一輪能排到它」的案例（見五章、
                                        # 10a 八章）；phase 2：
                                        # TestPendingFileFixes——套用成功
                                        # 時觸發一次 _wait_for_service_
                                        # reload()、套用失敗時記
                                        # TaskFailure(reason="file_fix_
                                        # failed")、沒有 pending_file_fixes
                                        # 時完全不觸發 reload-wait
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
tests/translator_cli/test_client.py      # phase 2：apply_file_fix() 精確
                                        # 字串替換成功並正確 commit、
                                        # old_snippet 找不到／出現不只一次
                                        # 都回傳失敗、套用後語法不合法回傳
                                        # 失敗（見 07b）
```

`tests/graph/test_implement_node.py` 既有測試（09b 七章）需要同步更新呼叫簽名——凡是直接呼叫 `_run_one_task(task, path)` 的既有測試，補上第四個引數（`pending_fixed_bodies={}` 保持既有行為不變，`fixed_body` 落地後是 `None`，`fill_function()` 走原本 ⑤ 本地模型路徑）。

---

## 十、模組結構總覽

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
│       ├── implement_node.py    # _augment_task_io()／_run_one_task() 擴充、
│       │                          # force_reschedule() 呼叫點、改呼叫
│       │                          # python_service.manager
│       └── give_up_node.py       # 新增 give_up_early 訊息分支
├── python_service/
│   └── manager.py                 # 新增：_python_service 單例（從
│                                  # implement_node.py 下沉）＋
│                                  # ensure_started()／stop()／get_diagnostics()
├── translator_cli/
│   ├── client.py                  # fill_function() 新增 fixed_body 參數；
│                                  # 新增 apply_file_fix()（phase 2）
│   └── git_ops.py                 # 新增 commit_file_fix()（phase 2）
├── common/
│   └── run_report.py              # 新增：write_run_report()／
│                                  # write_human_readable_report()（八章）
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
├── main.py                       # initial_state 新增欄位、graph run
│                                  # 結束後呼叫 write_run_report()／
│                                  # write_human_readable_report()
└── tests/
    ├── debug_agent/             # 新增
    ├── python_service/           # 新增 test_manager.py
    ├── translator_cli/           # apply_file_fix() 測試（phase 2）
    ├── common/                    # 新增 test_run_report.py
    ├── refactor_harness/         # 新增測試
    └── graph/                    # 既有測試同步更新
```

09a 八章「不需要新套件」的判斷準則在這裡不適用——⑦ 有實質領域邏輯需要封裝（module 分組判斷、prompt 設計、task_id 反查驗證、`give_up_early` 判斷），比照 `plan_agent/`／`design_agent/` 獨立成 `debug_agent/`，不是給既有 Agent 加一層不必要的抽象。

---

## 十一、已知限制與待驗證事項

- [ ] **prompt 診斷因果描述的整體準確度**：task_id 選擇至今從未出錯，但同一個真實 bug 連續呼叫給出的因果敘事不一定一致，見 10a 十三章。
- [ ] **`DEBUG_AGENT_MAX_TOKENS`（8192）大型 module 仍未實測**：目前真實呼叫實測 `in≈7000／out≈1400` tokens，遠低於上限；`task_fixes`／`file_fixes` 數量明顯更多的大型 module 是否會逼近上限仍未實測。
- [ ] **`_read_source_files()` 對超大檔案沒有做 context 大小上限控制**：目前假設 module 邊界已經自然收斂檔案數量與大小（見 10a 四章），若真實專案某個 module 的檔案異常龐大，可能需要額外的截斷或摘要策略，第一版不處理。
- [ ] **`RUN_TESTS_HEALTH_CHECK_TIMEOUT_SECONDS`（一章，預設 15 秒）本身仍未實測校準**：真正的風險已由 `run_newman()` 的逾時-報表完整性檢查接住（見一章、10a 二章「前置修補之二」），這個健康檢查本身的逾時預算數字是否要調整，優先級已經降低，非緊急項目。

`give_up_early` 未被真實案例觸發、多輪 debug 之間沒有互相參照這兩項已由多輪真實環境測試分別解決／定案，見 10a 十二章「已定案」。

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
