# ⑦ Debug Agent 詳細設計

> 本文件承接 `00_refactor_architecture.md`（七、⑦ Debug Agent 一節：「輸入：fail 清單 + diff 報告 + 對應 Python 原始碼；分析根本原因，輸出具體修正指令回饋給 Agent ⑤」）、`02a_harness_architecture.md`（九章 Report 格式、十一章 Route Mapping、十三章 Module 級局部驗證、十五章資料流）、`09a_implement_agent_architecture.md`/`09b_implement_agent_code.md`（`task_failures` 欄位、`already_failed` 修正、09a 七章明確留給本文件的 `scaffold_skipped` 提早止損評估）、`01_langgraph_architecture.md`（State、retry 迴圈）。是這個 Agent 的**設計面**文件：決策、契約、資料結構、流程。實際程式碼實作見 `10b_debug_agent_code.md`。

> 本文件與 `10b_debug_agent_code.md` 描述的程式碼已套用進 repo，通過完整單元測試，並已完成多輪真實環境端對端驗證（含真實 Claude API、真實 Docker、真實 Postgres、多輪 `debug ↔ implement ↔ run_tests` 迴圈）。設計已收斂的關鍵結論：⑦ 對檔案層級問題（`file_fixes`）與極少數可以百分之百確定的機械性修正（`fixed_body`）直接產生可套用的程式碼；但函式本體本身的業務邏輯有問題時（不論是從沒成功翻譯過，還是翻過但邏輯錯），⑦ 沒有真實 Java 原始碼可以核對，改標記 `retranslate=true` 交還給⑤（帶著 ⑦ 的診斷提示）重新翻譯，不是自己編寫（`docs/refactor_bug_trace.md` #9，見八章「`pending_retranslate_tasks`」，取代先前「⑤ 完全退出除錯迴圈」這個已被修正的結論）；`fixed_body`／`retranslate` 都只能替換函式本體，簽名／模組層級敘述改動需要 `file_fixes`（見八章 phase 2）；`retry_count` 上限檢查前移到 `should_debug_or_done()`、對所有分支統一套用（見七章）。已知的結構性限制見十二章。

---

## 一、本文件範圍與定位

**⑦ 的核心任務：把專案改到能通過驗證，不是分析完就結束、把結論丟給人工決定要不要採用。** ⑦ 讀原始碼、定位根因之後，直接產生可以套用的最終修正（`fixed_body`／`file_fixes`／`retranslate`，見六、八章），套用、重新驗證、沒過就再分析一次，直到通過或重試預算用盡。檔案層級問題（`file_fixes`）與極少數機械性修正（`fixed_body`）由 ⑦ 自己產生程式碼；但函式本體邏輯有問題時，⑦ 標記 `retranslate=true`、把 task 交還給⑤重新翻譯（帶著 ⑦ 的診斷當提示）——這不是「⑦ 給建議、⑤ 自己決定」的兩階段分工，是⑤走跟首輪翻譯完全相同的真實模型呼叫，只是多了 ⑦ 的診斷提示（`docs/refactor_bug_trace.md` #9，見八章）。這個「⑦ 自己寫 vs. 交還給⑤」的分界，是兩輪真實環境測試分別逼出來的結論：曾經讓 ⑦ 只給自然語言指令、由 ⑤ 的本地模型根據指令**修改既有程式碼**，實測發現本地模型執行指令會不完整（見八章）——但後續發現讓 ⑦ 自己**編寫函式本體**同樣有問題：⑦ 沒有真實 Java 原始碼可以核對，寫出來的內容一樣可能偏離真正的業務邏輯。兩次教訓收斂出目前的分界：函式本體要不要重寫，該問「誰看得到 Java 原始碼」，答案是⑤，不是⑦。

⑦ Debug Agent 的輸入橫跨兩個既有 Agent 的輸出，不是單一 Agent 自己的延伸——這也是 00 文件把 010a 列成獨立文件、不塞進 09a/09b 的原因：

1. **`task_failures`**（09b 一章，`implement_node.py` 逐 task 累積寫入）——「連程式碼都生不出來」的失敗：`reason` 為 `"scaffold_skipped"`（④ 骨架階段沒渲染出這個函式，永久性，重試無法修好）或 `"fill_failed"`（⑤ 本地模型填空本身失敗）。**`"fill_failed"` 的「可能重試修好」明確指的是「⑦ 這一輪直接接手寫出完整函式本體」，不是「排程器把它排回⑤本地模型重新翻譯一次」**——這條界線見四章「`known_fill_failures`：一旦失敗過一次，不再退回本地模型」，是本文件的正式決策，不是含糊帶過的措辭
2. **`test_results`**（02a 九章 `HarnessReporter.build_report()` 的輸出，⑥ 產生）——「生出來但驗證沒過」的失敗，這才是真正的 bug list：`failures[]` 每筆含 `case_id`／`failure_type`／`body_diff`／`related_files`／`debug_hint`

`test_results` 是**全量**驗證（readonly＋mutation，⑥ 對所有 module 無條件跑一次，見 02a 十二章），不是只驗證 ⑤ 認為有問題的 module——這一點是本文件多處設計決策的基礎（見三章）。

### 已經決定、本文件不重新設計的部分

- Report 格式本身（`summary`／`status`／`failures[]`／`excluded_folders`／`excluded_cases`）——02a 九章已定案，本文件只讀取，不改動既有欄位語意
- `route_to_file_mapping`／`route_to_module_mapping` 的解析演算法——02a 十一章、`RouteMapper` 已定案且通過真實驗證
- `ModuleScheduler` 的排程演算法、`already_failed` 語意——01 六章、09a 七章已定案，本文件只新增一個方法（`force_reschedule()`，見八章），不改動既有方法的行為

### 本文件推翻的一項既有假設：`retry_count` 不能只在 `failed_modules` 非空時遞增

02a 十二章原本的措辭是「`retry_count` 只在 `failed_modules`（確實跑過、驗證過但沒過）非空時才扣減；`blocked_modules`……不消耗重試次數，因為那不是『寫錯了』，只是排程還沒排到」，`graph/nodes/debug_node.py` 現有 stub 也依此實作（`increment = 1 if state.get("failed_modules") else 0`）。**這條規則在交叉比對 09a 三章之後站不住腳，本文件明確推翻它**，理由與修正見六章「`retry_count` 遞增」。

### 本文件要定案的部分（09a/02a 明確留下的缺口）

1. `task_failures`／`test_results`／`partial_reports`／`blocked_reasons` 四份粒度、格式都不同的資料，如何正規化成 Debug Agent 能逐 module 分析的統一輸入（三章）
2. **`test_results.failures[]` 目前沒有 `module` 欄位**——`GoldenVerifier`／`MutationVerifier` 內部都算出了 `module` 這個區域變數，但 `HarnessReporter.build_report()` 從未把它寫進輸出，Debug Agent 因此無法把「哪筆失敗屬於哪個 module」對起來。這是 02a 九章當初設計時沒有預見的缺口（那時候還沒有任何東西真的消費這個 report），本文件定案一個最小、不影響既有欄位與既有測試的修補（二章）
3. `scaffold_skipped` 造成的失敗如何處理——不能重試修好，也不能讓 `debug ↔ implement` 白白耗掉 `retry_count` 卻不可能有結果；09a 七章「沒有採用的方案」明確評估過「在條件邊層級提早繞過整條 debug 迴圈」風險太高、留給本文件用更細的資料重新評估（七章）
4. Debug Agent 的輸出契約——直接產生什麼形狀的程式碼、`implement_node.py` 怎麼在下一輪把它套用進真實檔案（六、八章）
5. task 級粒度的定位——`related_files` 只是檔案清單，不是函式名，一個檔案可能有多個函式，如何精確判斷「哪個 task 該重新填空」（五章）

---

## 二、輸入與前置資料

Debug Agent 讀取的 `RefactorState` 欄位：

| 欄位 | 來源 | 用途 |
|---|---|---|
| `test_results` | ⑥（`run_postman_tests`） | 權威 bug list，見上方 |
| `task_failures` | ⑤（09b） | 區分 `scaffold_skipped`／`fill_failed`，取得 `error` 訊息 |
| `partial_reports` | ⑤（09b） | 取得 `batch_reload_timeout`／`module_entirely_scaffold_skipped` 這類特殊 `reason` 標記，見三章 |
| `failed_modules`／`blocked_modules`／`blocked_reasons` | ⑤（09b/01） | 判斷 module 是不是被上游卡住（origin，見三章），**不是**本文件判斷「這個 module 有沒有真的壞」的依據，理由見三章 |
| `task_list` | [P]（06a） | 每個 task 的 `id`／`module`／`function_name`／`class_name`／`target_files`／`description`／`context`，供 task_id 反查（五章） |
| `module_list` | ①（04a） | `summary`，給 LLM 業務語境 |
| `python_project_path` | main.py（.env） | 讀取 `related_files` 的**目前實際內容**——Debug Agent 是第一個需要讀「已生成的 Python 原始碼」而不是「規格/簽名」的 Agent |
| `retry_count` | Orchestrator | 遞增判斷 |

### 前置修補：`test_results.failures[]` 新增 `module` 欄位，`comparator.py` 兩個早退分支補齊 `related_files`

`GoldenVerifier._process_executions()`（`refactor_harness/verifier/comparator.py`）與 `MutationVerifier._verify_one_raw()`（`refactor_harness/verifier/mutation_verifier.py`）都已經在內部算出 `module = self._get_module(method, url_parts)` 這個區域變數（分別用於 `_load_golden(case_id, module)` 定位 golden 檔案），但兩者組出的 `results.append({...})` 字典都沒有把這個值帶出來；`HarnessReporter.build_report()` 因此也沒有機會把它寫進 `failures[]`。這不是 bug，是因為 02a 九章設計當下還沒有任何消費端需要「這筆失敗屬於哪個 module」——`related_files` 對人工除錯已經夠用，Debug Agent 是第一個需要「把 test_results 的失敗依 module 分組」的消費端。

**`comparator.py::_process_executions()` 還有第二個獨立缺口，這次一併修補**：這個函式有三個 `results.append({...})` 分支——`golden_not_found`（golden 檔案不存在，且不在已知跳過清單裡）、`response_not_json`（回應無法解析成 JSON）、正常比對——**只有最後這個「正常比對」分支帶了 `related_files`**，前兩個早退分支（`continue` 之前）完全沒有帶。`mutation_verifier.py::_verify_one_raw()` 沒有這個問題（它的 `results.append(...)` 只有一處，寫在 `if golden: ... else: ...` 分支**之後**，兩種情況都會經過同一次 append，`related_files` 一定會帶上）——這是 `comparator.py` 特有、`mutation_verifier.py` 沒有的既有落差，兩者的程式碼結構不一樣導致的。

**這個缺口的實際影響，直接連動四章的 Debug Agent context 供給**：`response_not_json`（Python 服務未處理例外，回傳 HTML 錯誤頁而非 JSON，見 02a 九章 `failure_type` 分類表）正是最需要看到 router／service 原始碼才能除錯的情境之一（例如 500 錯誤的堆疊根因）——四章的 `related_files` 聯集若因為這個既有缺口拿到空清單，即使有四章「疊加 `target_files`」的既有修補，也只會補到 `schemas`／`models`（見四章「為什麼要限縮成只取 `schemas`／`models`」），LLM 依然完全看不到真正拋出例外的那段程式碼，除非剛好命中三章「例外」那條「`harness_failures` 為空」的更寬鬆規則——但這裡 `harness_failures` **不是空的**（`response_not_json` 這筆記錄本身就在 `harness_failures` 裡），只是它的 `related_files` 是空清單，不會觸發那條例外規則，是介於兩者之間、原本沒設計到的落差。

**修補範圍（最小、不改變既有欄位語意）**：

1. `comparator.py::_process_executions()`：三個 `results.append({...})`（`golden_not_found`／`response_not_json`／正常比對）都加一行 `"module": module`；**`golden_not_found`／`response_not_json` 這兩個早退分支，同時補上 `"related_files": self.route_mapper.resolve_related_files(method, url_parts)`**——`method`／`url_parts` 在三個分支進入前就已經算好，不需要額外計算，正常比對分支本來就已經這樣寫，這裡只是讓前兩個分支跟第三個分支一致
2. `mutation_verifier.py::_verify_one_raw()`：加 `"module": module`（`related_files` 已經有，見上方說明，不需要補）
3. `reporter.py::build_report()`：`failures[]` 的字典組裝新增 `"module": f.get("module")`

不影響 `passed`／`status`／既有任何欄位的值，也不影響既有測試斷言的欄位（純新增一個 key）。實際 diff 見 10b 一章。

### 前置修補：⑥ 對外呼叫前必須先確認 Python 服務有回應，否則 Debug Agent 永遠無法被觸發

**問題**：09a 三章「逾時不該讓整條 pipeline 崩潰」明訂 ⑤ 遇到 `batch_reload_timeout` 時**就地接住**、不讓例外往外傳——但那只解決了 ⑤ 自己這一關。09a 同一章也說明了 `batch_reload_timeout` 最常見的根因：某個 task 寫入的程式碼有模組層級的匯入或語法錯誤，讓 `app/main.py` 連 import 都過不了，容器內的 uvicorn worker 直接啟動失敗——**這代表 Python 服務極可能在 `implement` 結束、進入 `run_tests`（⑥）的那一刻，仍然處於「連不上」的狀態**（09b 六章：容器化服務在整條 `implement → run_tests` 期間是同一個持續運行的行程，⑤ 沒有機制把它修好，它會帶著壞掉的狀態一路撐到 ⑥）。

`run_postman_tests()`（02b）目前直接呼叫 `run_newman()`，而 `run_newman()` 依 02a 五章「Newman 共用執行器」的既有、刻意設計：「若 newman 失敗（服務沒起來、collection 路徑錯誤）立即拋出明確例外，不讓錯誤靜默流入後續比對」——這個設計本身沒有錯（②錄製端、⑤局部驗證都合理地依賴這個行為在環境真的沒準備好時大聲失敗），但 ⑥ 這個呼叫點是**目前整條 graph 唯一一個「服務没起來是可預期的正常情境，卻沒有任何防護」的地方**：`RuntimeError` 會直接從 `run_postman_tests()` 往外傳，整條 `graph.ainvoke()` 崩潰，`debug_node` 完全不會被呼叫到——三章 3.5「`batch_sibling_modules`」設計的整套機制，在這個最常觸發它的情境下反而永遠執行不到，形同死碼。

**修法**：`run_postman_tests()` 呼叫 `run_newman()` 之前，先做一次**短暫、有界**的連線健康檢查（不是 09a 三章那種要比對特定 token 的同步屏障——⑤ 該做的等待與重試已經做過了，⑥ 這裡只需要知道「現在打不打得到」，不需要再等一輪）：

```python
# refactor_harness/langgraph_nodes/test_nodes.py（新增函式）
def _is_service_reachable(base_url: str, timeout_seconds: float, poll_interval: float) -> bool:
    """只檢查連線層級是否可達，不檢查任何回應內容或狀態碼——即使服務
    回 404，只要連得上就代表這不是 09a 三章描述的「worker 崩潰、連線被
    拒絕」情境，交給 run_newman() 正常執行去發現真正的問題。"""
```

若逾時仍連不上，`run_postman_tests()` **不呼叫** `run_newman()`，改回傳一份格式跟 `HarnessReporter.build_report()` 相容、但 `failures` 為空、帶有明確 `reason` 標記的 `test_results`：

```python
{
    "summary": {"total": 0, "passed": 0, "failed": 0, "pass_rate": 0},
    "status": "fail",
    "reason": "service_unreachable",
    "failures": [], "passed_cases": [],
    "excluded_folders": [], "excluded_cases": [],
}
```

`status` 固定填 `"fail"`（不是 `"pass"`，也不是留空）：`should_debug_or_done()`（02b，本文件不改動）看到 `status != "pass"` 才會繼續往下判斷，而不是誤判成整條驗證通過。`reason` 欄位比照 09b 二章 `partial_reports` 裡 `batch_reload_timeout`／`module_entirely_scaffold_skipped` 的既有寫法，是頂層最小結構標記，供三章的正規化辨識這種情況與正常的 `HarnessReporter` 輸出不同。**完整程式碼、逾時時長的選擇理由見 10b 一章**。

**同一時間點，新增 `RefactorState.service_diagnostics: str | None` 欄位**：`_is_service_reachable()` 判定不可達的當下，順手呼叫 `python_service.manager.get_diagnostics()`（見八章「診斷資料改走 State」，容器崩潰當下的 docker log），連同 `test_results` 一起回傳、寫進這個新欄位——不掛 reducer，每輪覆寫（跟 `test_results` 本身的既有覆寫語意一致）。這是四章「`service_diagnostics` 要傳給所有 `root_cause` module」的資料來源，⑦ 全程只讀 `state.get("service_diagnostics")`，不呼叫任何跨節點函式，理由見八章。

這一道健康檢查只加在 `run_postman_tests()` 這一個呼叫點前面：`run_newman()` 本身、②與⑤局部驗證的呼叫點都不受影響，仍然是遇到服務沒起來就直接拋例外——那些情境下沒有 Debug Agent 這種後續可以接手分析的機制，直接失敗、讓人工介入仍然是對的。

### 前置修補之二：健康檢查只擋得住「開始前就死了」，擋不住「其實沒死只是行程沒退出」

上面的健康檢查只在 `run_postman_tests()` 一開始做一次；readonly＋mutation 合計要打完整個 collection，實際耗時可能遠超過那次檢查的短逾時預算，服務完全可能在健康檢查通過「之後」才變得沒有回應（`docs/09b_bug_trace.md #47`：多次真實重現，症狀是 `run_postman_tests()` 回報 `service_unreachable`，但用獨立診斷腳本直接探測同一顆容器卻完全正常——是健康檢查時機撞上暫時的不穩定視窗，不是真的壞掉，緩解方向是把逾時預算調大，但只能降低機率、不能根除）。

**真正需要動 `run_newman()` 本身的是另一個機制完全不同的問題**（`#48`）：`run_newman()` 逾時不等於「服務沒回應」——newman（Node.js）可能已經把 collection 全部請求跑完、報表也完整寫出了，純粹是**行程本身沒有退出**（已知的 Node.js keep-alive socket 未乾淨關閉問題，同類案例見 [uvicorn discussion #1691](https://github.com/encode/uvicorn/discussions/1691)），`_is_service_reachable()` 這種連線層級的健康檢查從機制上不可能預先擋住它。**修法**：`run_newman()`（`refactor_harness/core/postman_runner.py`）逾時當下，`_kill_process_tree()` 收尾之後不直接判定失敗，先檢查報表檔案是否已完整合法——是的話當成功回傳，只有報表也沒寫完才是真正的逾時錯誤（新增 `NewmanTimeoutError`，`RuntimeError` 子類別，讓 `run_postman_tests()` 精確只攔這一種、不誤吞設定錯誤）。這是對 `run_newman()` 本身的修改，因為問題發生在它內部的逾時處理，`run_postman_tests()` 這一層看不到、也不該重複判斷。

### 前置修補之三：健康檢查前先固定緩衝，緩解「⑦ 一輪套用多筆修正」造成的誤判（`#47` 補充）

`#47` 後續真實環境重跑另外三次重現了一種更具體的成因：⑦ 一輪可能一次開出多筆 `pending_fixed_bodies`／`pending_file_fixes`（真實案例一輪套用 3 筆），`implement_node.py` 各自的檔案寫入被 uvicorn `--reload`（watchfiles）依短暫的 debounce 視窗各自分批觸發，可能連續產生不只一次的 reload 週期；`_wait_for_service_reload()`（09a 三章）只確認「它自己最後一次寫入的 token」已經生效，不保證這就是這一輪「最後一次」被觸發的 reload——`implement_node.run()` 可能在某個 reload 仍在進行中的窗口就已經返回，緊接著 `run_postman_tests()` 的健康檢查（前置修補一）就撞上這個還沒收斂穩定的窗口，回報 `service_unreachable`，但用獨立診斷腳本立刻探測同一顆容器，服務其實完全正常。

根因（uvicorn 忙碌／reload 批次時機）未 100% 證實，這裡採用兩個建議緩解方向之一：健康檢查呼叫之前先固定等一段緩衝時間（`RUN_TESTS_PRE_HEALTH_CHECK_BUFFER_SECONDS`，預設 5 秒），不分岔判斷「這一輪是不是⑦套用了多筆修正」——那需要額外跨節點狀態，且緩解方向本身是機率性的，對「這一輪其實只有一筆改動」的情況多等這幾秒也無害。這不是 `_wait_for_service_reload()` 那種比對特定 token 的同步屏障，純粹是給連續 reload 一點喘息時間，只降低機率、不保證根除——若真正需要更高把握，仍要靠既有的 `retry_count` 迴圈與⑦下一輪重新診斷收斂。完整程式碼見 10b 一章。

---

## 三、輸入正規化：算出「這一輪真正該分析的 module 清單」

這一步是**純程式邏輯**，不呼叫 LLM——先把四份格式、粒度都不同的資料收斂成「這一輪要不要送進 LLM、送的話帶什麼」的判斷。

### 3.1 為什麼用 `test_results.failures` 分組，不是 `state["failed_modules"]`

`failed_modules` 反映的是**⑤ 最後一次 `implement_node.run()` 的局部驗證結果**（09a 三章明訂只跑 readonly、不含 mutation，見 09a 三章「只跑 readonly，不跑 mutation」）。`test_results` 是**⑥ 對全部 module 無條件執行的全量驗證**（readonly＋mutation，見 02a 十二章），是本文件唯一信任的「這個 module 現在到底有沒有壞」的權威來源。兩者可能不一致：

- 一個 module 可能被 ⑤ 標成 `verified`（readonly 通過），但 `test_results` 因為 mutation 或跨 module regression 抓到真正的問題——這種 module **不會**出現在 `failed_modules`，若只依賴 `failed_modules` 分析，會整批漏掉
- 一個 module 可能因為 `batch_reload_timeout`（09a 三章）被 ⑤ 暫時標成 `failed`，但 `test_results` 由 ⑥ 重新乾淨地跑過一次 Newman，问题可能early已經隨著同一輪其他 task 的修正而消失

因此 3.2 的分組**一律以 `test_results["failures"]` 的 `module` 欄位（見二章修補）為準**，`failed_modules`／`blocked_modules`／`blocked_reasons` 只用來判斷「這個有問題的 module 該不該叫 LLM」（3.3），不是「有沒有問題」——**除了一種例外**，見 3.2 的 `service_unreachable` 分支。

### 3.2 依 module 分組

```python
if test_results.get("reason") == "service_unreachable":
    modules_with_failures = {m: [] for m in state.get("failed_modules", [])}
else:
    modules_with_failures: dict[str, list[dict]] = {}
    for f in test_results["failures"]:
        modules_with_failures.setdefault(f["module"], []).append(f)
```

**`service_unreachable` 分支**（對應二章「⑥ 對外呼叫前必須先確認 Python 服務有回應」）：這種情況下 `test_results["failures"]` 本身是空陣列——Newman 根本沒有機會執行，不是「執行了、沒有失敗」。此時退回 `state["failed_modules"]`（⑤ 上一輪自己標記失敗的 module，多半正是造成這次連不上的元凶所在，見 09a 三章「批次執行」），每個 module 先給空的 `harness_failures`（沒有 body-diff 可看）——3.3 的分類規則**完全不變**，繼續套用在這份用不同方式湊出來的 `modules_with_failures` 上；3.5 的 `special_reason`／`batch_sibling_modules` 计算方式也不變（仍然讀 `partial_reports`，跟 `harness_failures` 從哪裡來無關），會自然把這批 module 的 `special_reason` 標成 `batch_reload_timeout`（若 09a 三章的批次判定就是這樣記的）——四章的 LLM 分析因此完全能沿用既有機制，唯一差別只是這次 `harness_failures` 是空的，LLM 只能靠原始碼＋`special_reason`／`batch_sibling_modules` 判斷，這正是四章 prompt 對 `batch_reload_timeout` 情境本來就準備好的分析路徑（「優先檢查 source_files 有沒有明顯的 import／語法層級問題，而不是先假設是業務邏輯比對錯誤」）。

**`service_unreachable` 且 `state["failed_modules"]` 也是空**：`modules_with_failures` 因此是空字典，`contexts`／`root_cause_ctxs`（四、七章）連帶全部是空清單——直接套用七章「`root_cause_ctxs` 為空 → `give_up_early=True`」的一般規則，不另外分岔。

這種情況代表 Newman 完全沒機會執行、⑤ 也沒有標記任何失敗 module，這一輪連「哪個 module 可能有問題」的最基本線索都沒有——沒有真實案例佐證這種情況會發生（`09b_bug_trace.md` 記錄的 `service_unreachable` 案例都伴隨非空的 `failed_modules`，見上方「`service_unreachable` 分支」），發生機率本身就低。即使發生，比起另外設計一套專屬的全專案 crash-log 分析管道（對著 metadata-only 的 task 清單猜測要修哪個 task，準確度本來就沒有保證），直接 give up、讓人工從已經寫進 `state["service_diagnostics"]` 的容器崩潰 log（見二章）著手排查，是更省成本、也不會更差的做法——`give_up` 是通知人工的節點（01 五章），不是直接結束，人工介入之後想怎麼修都可以，不需要 Debug Agent 自動化這一步。

### 3.3 逐 module 分類：值不值得叫 LLM

對 `modules_with_failures` 裡每一個 module，依序判斷（機械規則，不是 LLM 判斷）：

```
module 在 blocked_modules 裡？
  → origin = "blocked"，不呼叫 LLM。
    這個 module 自己的 task 從未被排程器排到（09a 七章：blocked 代表
    「上游沒過，這個 module 的程式碼多半還是骨架 pass」，不是「翻譯錯」），
    對它自己的 task 生成 fixed_body 沒有意義——即使生成了，下一輪
    排程器也不會排到它（依賴的上游仍未 verified）。真正該分析的是
    blocked_reasons[module] 指向的上游 module，那個上游 module 必然也
    在 modules_with_failures 裡（否則它不會是導致下游 blocked 的原因），
    會被獨立分析到，不需要在這裡重複處理。
    → 記一筆 ModuleFailureContext(origin="blocked")，只供除錯記錄用，
      不產生 task_fixes。

module 在 task_list 裡完全找不到任何一個 task？
  → origin = "module_mismatch"，不呼叫 LLM。
    test_results.failures[*].module 來自 RouteMapper.resolve_module()
    （二章修補後才存在的欄位）——查不到 route_to_module_mapping 時會
    fallback 回「URL 第一個非版本路徑段」的字串猜測（見 02a 十三章、
    core/postman_runner.py::get_module()），這個猜出來的字串不保證等於
    task_list 裡任何一個 task["module"] 的值。這種情況代表資料本身有
    落差（多半是③的 route_to_module_mapping 漏收這個 endpoint、或這個
    endpoint 屬於 skip 呼叫鏈已排除但仍留在 collection 裡，見 02a 十六
    章「Module 詞彙一致性」既有待實作清單），不是「不確定哪個 task」，
    是「根本沒有 task 可以指」——送進 LLM 只會讓它面對一份空的 task 清單
    却還要它從 harness_failures 裡的 case 猜出些什麼，白白浪費一次呼叫，
    也不會產生任何可用的 task_fix（4.3 的驗證集合本來就是空的，任何
    task_id 都會被判定非法而捨棄）。
    → 記一筆 ModuleFailureContext(origin="module_mismatch")，
      unfixable_reasons 固定寫「route_to_module_mapping／task_list 找
      不到這個 module 對應的 task，需人工核對③/[P]輸出或 skip 呼叫鏈
      設定，見 02a 十六章」，不嘗試用 related_files 反查回一個「猜測的」
      module——那只是把「資料對不上」的訊號吞掉，換成一個可能同樣不
      準確的猜測，不如直接如實回報這個落差讓人工核對源頭。

module 底下的 task 是否 100% 落在 scaffold_skipped？
  → 用 task_failures 直接判斷（09a 六章既有設計意圖：「供 ⑦ Debug Agent
    不需要重新比對 skipped_interfaces 就能分辨兩種失敗」），不重新掃
    skipped_interfaces：
        scaffold_gap_task_ids = {f["task_id"] for f in task_failures
                                   if f["reason"] == "scaffold_skipped"}
        module_task_ids = {t["id"] for t in task_list if t["module"] == module}
    module_task_ids 非空 且 module_task_ids ⊆ scaffold_gap_task_ids
  → origin = "scaffold_gap"，不呼叫 LLM（100% 骨架缺口，任何 LLM 分析都
    只會建議修正一個不存在的函式，見 09a 六章「不採用」的 skipped_db_models
    提前排除同一種浪費）。直接產出固定格式的 fixable=False 診斷（六章）。

否則
  → origin = "root_cause"，送 LLM 分析（四章）。這個 module 可能混雜
    「部分 task 是 scaffold_skipped、部分是 fill_failed、部分是翻譯
    成功但邏輯錯」——不是上面兩條規則的全有全無，因此不能機械判斷，
    需要 LLM 逐一看程式碼與 diff 才能定位。
```

判斷順序固定：`blocked` → `module_mismatch` → `scaffold_gap` → `root_cause`。`module_mismatch` 排在 `scaffold_gap` 之前是必要的，不是隨意排序——`scaffold_gap` 的判斷式 `module_task_ids 非空 且 ⊆ scaffold_gap_task_ids` 在 `module_task_ids` 為空集合時恆為假（空集合不算「非空」），若不先把「完全找不到 task」獨立出來，這種情況會直接落到 `else` 分支被誤判成 `root_cause`，送進 LLM 時 4.1 的「這個 module 全部 task 的清單」會是空陣列——這正是 `module_mismatch` 這個分支要提前攔截的情況。

`origin="blocked"`、`origin="module_mismatch"`、`origin="scaffold_gap"` 都不消耗 Claude API 呼叫，也是本章「值不值得叫」判斷的直接目的——比照 00 六章「能用程式判斷的，就不要交給 LLM」，這三種情況機械規則已經能百分之百確定結論，不需要 LLM 重新確認一次。

### 3.4 `ModuleFailureContext`：正規化後的單一資料結構

```python
class ModuleFailureContext(TypedDict):
    module: str
    origin: Literal["blocked", "module_mismatch", "scaffold_gap", "root_cause"]
    harness_failures: list[dict]        # test_results.failures 裡這個 module 的子集
    task_failures: list[TaskFailure]     # task_failures 裡這個 module 的子集
    scaffold_gap_task_ids: set[str]      # 這個 module 裡屬於 scaffold_skipped 的 task id（origin="root_cause" 時可能非空但非全部）
    special_reason: str | None           # 見 3.5：這個 module 最新一筆 partial_reports 的 reason（若有）
    batch_sibling_modules: list[str]     # 見 3.5：special_reason 為 "batch_reload_timeout" 時，同一批（最新一次逾時）一起被牽連的其他 module 名稱
```

### 3.5 `special_reason`／`batch_sibling_modules`：`partial_reports` 作為輔助診斷文字，不改變 origin 判斷

`partial_reports` 裡每個 module 最新一筆若帶 `reason`（`"batch_reload_timeout"`／`"module_entirely_scaffold_skipped"`，見 09b 二章），這兩種 report **沒有** `failures[]`／`body_diff` 這些鍵（09a 三章「這批 module 的 report 因此用明顯不同於 `HarnessReporter.build_report()` 的最小結構表示」）——3.1～3.3 的判斷完全不讀 `partial_reports`，只把這個 `reason`（若有）原樣夾帶進 `ModuleFailureContext.special_reason`，作為送進 LLM 時的補充背景文字（四章）：

- `module_entirely_scaffold_skipped` 通常會與 3.3 的 `scaffold_gap` 判斷一致（兩者依據不同但結論應該相同），這裡只是多一句人類可讀的佐證，不是判斷依據本身
- `batch_reload_timeout` 代表這個 module **上一輪**在 ⑤ 那邊沒能真正跑到 Newman；但 3.1 已經說明 `test_results` 是這一輪 ⑥ 重新跑出來的乾淨結果，不受這個歷史狀態影響——`special_reason` 只是讓 LLM 知道「這個 module 之前有一輪環境不穩定，這次的 `harness_failures` 才是目前真正的狀態」，不影響 origin 判斷或要不要呼叫 LLM

**`batch_sibling_modules`：`batch_reload_timeout` 需要跨 module 的脈絡，不能只當一句人類可讀文字帶過**——09a 三章「這批因逾時而判定失敗的 module」一節已經明講這個情境的本質：一輪批次寫入裡，任何一個 module 的程式碼有模組層級匯入／語法錯誤，都會讓 `app/main.py` 整個 import 失敗、牽連當下同一批要驗證的其他 module，而那些被牽連的 module **自己的程式碼完全可能是對的**；09a 同一節也明確指示「10a 只要先檢查 `report.get("reason") == "batch_reload_timeout"`……且這一輪被牽連的 module 應該一起看，因為真正的根因大機率只在其中一個」——這是 09a 寫給本文件的具體待辦，3.5 在此定案：

- **資料形狀（明確釘死，避免誤讀）**：`partial_reports` 的每一筆元素固定是 `{"module": <str>, "report": {...}}` 這個外層包裝——`module` 鍵**在外層**，`report` 鍵底下才是 `{"status", "reason", "regression"}` 這幾個欄位（見 09b 二章 `implement_node.py::run()` 全部 `partial_reports.append({"module": module, "report": {...}})` 呼叫點，逐字皆如此，沒有例外）。09a 三章行文中出現的 `{"status": "fail", "reason": "batch_reload_timeout"}` 只是**內層** `report` 物件的示意，不是整筆元素——三章開頭「`special_reason`」一節與這裡都是對外層 `entry["module"]` 取值、對 `entry["report"].get("reason")` 取值，不存在「`partial_reports` 沒有 module 鍵所以取不到」的問題。

**「最新一筆記錄」必須綁定輪次，否則會抓到跨輪次的過期資料**：`partial_reports` 是 `Annotated[list[dict], operator.add]`，從整條 graph run 第一次進 `implement` 開始逐輪累加，永不清空。若 module A 在第 1 輪遇到 `batch_reload_timeout`，但第 2 輪完全沒有任何 task 動到它（例如它的上游依然被卡住，或它已經完工只是還沒被重新驗證），A 在 `partial_reports` 裡就不會有第 2 輪的新紀錄——這時候若「最新一筆」的判斷不分輪次、單純取「A 在整個累積清單裡最後出現的那一筆」，會直接沿用第 1 輪那筆過期的 `batch_reload_timeout`。假設第 3 輪某個完全無關的 module C 才第一次遇到 `batch_reload_timeout`（跟 A 在第 1 輪的那次崩潰毫無關係），C 的 `batch_sibling_modules` 若照這種「不分輪次的最新一筆」算法，會誤把 A 也算進去（A 的「最新」紀錄剛好還停留在 `batch_reload_timeout`，即使那是兩輪以前的事）——LLM 因此會被誤導去懷疑一個早就跟這次崩潰無關的 module。

**修正：`partial_reports` 每一筆新增 `"round"` 鍵，標記寫入當下的 `retry_count`，`special_reason`／`batch_sibling_modules` 的計算只看「這一輪」的紀錄，不看跨輪次的累積歷史**：

```python
# 09b implement_node.py::run()（異動）：全部 partial_reports.append(...)
# 呼叫都補上這一鍵，值取 state["retry_count"]（呼叫 implement 當下、
# debug 尚未遞增前的值，見 10a 六章）
partial_reports.append({"module": module, "round": state["retry_count"], "report": {...}})
```

```python
# debug_agent/triage.py（異動）
def _latest_reasons_by_module(partial_reports: list[dict], current_round: int) -> dict[str, str]:
    latest: dict[str, str] = {}
    for r in partial_reports:
        if r.get("round") != current_round:
            continue  # 跨輪次的舊紀錄不列入考慮，見 10a 3.5「過期資料」
        report = r.get("report", {})
        if "reason" in report:
            latest[r["module"]] = report["reason"]
        elif r["module"] in latest:
            del latest[r["module"]]
    return latest
```

`current_round` 傳入 `state["retry_count"]`——`run_debug_analysis()` 讀取這個值時，retry_count 尚未被這一輪的六章邏輯遞增，恰好等於「剛結束的那次 `implement()` 呼叫」看到的 `state["retry_count"]`，兩邊用的是同一個數字，不需要額外傳遞或比對其他識別碼。**`special_reason`（3.5 前半段，單一 module 自己的最新狀態）與 `batch_sibling_modules`（同一批的其他 module）共用同一個輪次過濾後的字典，處理方式一致，不是只修 `batch_sibling_modules` 那一半**——一個 module 若這一輪完全沒被 ⑤ 碰過（沒有新的 `partial_reports` 紀錄），`special_reason` 也會正確地變成 `None`（沒有本輪的新訊號），而不是沿用兩輪以前的舊狀態，跟 `batch_sibling_modules` 的推理是同一件事的兩面。

- 計算方式：先用上面的 `_latest_reasons_by_module(partial_reports, state["retry_count"])` 算出「這一輪」每個有新紀錄的 module 對應的 reason，再取 `{module for module, reason in ... if reason == "batch_reload_timeout"}`，即所有「這一輪最新狀態是 batch_reload_timeout」的 module 集合。這仍然是一個**近似值，不是精確的批次分組**——`partial_reports` 本身沒有記錄「哪幾個 module 屬於同一次 `_wait_for_service_reload()` 呼叫」這個批次識別碼（見 09b 二章 `run()`：同一輪迴圈內 `verify_needs_wait`／`reverify_needs_wait` 一起判定逾時，但沒有寫出一個共同的 batch id），只能用「同一輪最新 reason 仍是這個值」做代理——多數情況下這已經足夠精確（同一輪逾時的 module 集合本來就是同一次批次等待造成的），且加上輪次過濾後，不會再被更早輪次的過期資料汙染，比修正前更精確（已知限制見十三章）
- 對每個 `special_reason == "batch_reload_timeout"` 的 module，`batch_sibling_modules` 是上述集合扣掉自己，供四章 prompt 明確告知 LLM「這幾個 module 上一輪是同一批被牽連的，若在自己的 `related_files` 裡找不到明顯的業務邏輯錯誤，根因很可能在這些 sibling module 裡，不要勉強在自己的程式碼裡找一個不存在的 bug」（見四章 prompt 片段）

---

## 四、Module 級 LLM 分析（Claude API，依模組平行呼叫）

比照 05a 六章「依模組取代整包 Map-Reduce」、04a 四章「待重試清單」的既有模式，**不是 map-reduce**（各 module 的分析結果互不需要跨邊界合併），是單純的「依自然邊界（module）拆分、平行呼叫」，併發數共用 `common.concurrency.default_concurrency()`（00 六章「Map 階段併發數（共用工具）」，不重新推導）。

只對 `origin == "root_cause"` 的 `ModuleFailureContext` 呼叫 LLM。

### 4.1 單一 module 的 Claude 呼叫內容

| 輸入 | 說明 |
|---|---|
| `module_list[module].summary` | 業務語境 |
| `harness_failures` | 這個 module 的失敗清單（`case_id`／`failure_type`／`expected_status`／`actual_status`／`body_diff`／`related_files`／`debug_hint`），**逐字轉發**，不做摘要或篩選——DeepDiff 的精確路徑對 LLM 定位問題至關重要 |
| `task_failures`（`reason=="fill_failed"` 的子集） | 已知確定翻譯失敗的 task，附 `error` 訊息——**這些 task 一律要求 ⑦ 標記 `retranslate=true`，不是選擇性的參考資訊**，見下方「`known_fill_failures`：一旦失敗過一次，改由⑤帶著⑦的診斷重新翻譯」（`docs/refactor_bug_trace.md` #9 修正） |
| `scaffold_gap_task_ids`（若非空） | 明確告知這些 task 骨架缺失、不要嘗試建議修正它們（見四章 prompt 片段） |
| 這個 module 全部 task 的清單（`id`／`function_name`／`class_name`／`target_files`／`description`） | 不是只給失敗的 task——LLM 需要**完整**清單才能從 `related_files`／程式碼內容反推「是哪個 task 造成這個 case 失敗」，見五章 |
| 相關原始碼的目前實際內容 | `harness_failures` 裡所有 `related_files`，**與**「這個 module 全部 task 的 `target_files` 裡、父目錄是 `schemas/` 或 `models/` 的檔案」取**聯集**（不是整份 `target_files` 無差別聯集——見下方「為什麼要限縮成只取 `schemas`／`models`」）。**只給 `related_files` 不夠**：見下方「為什麼要疊加 `target_files`，不能只給 `related_files`」。**`harness_failures` 為空時（`service_unreachable`）例外放寬成整份 `target_files` 不過濾**：見下方「例外」 |
| `special_reason`（若非空） | 3.5 的補充背景文字 |
| `batch_sibling_modules`（若非空） | 3.5：只給 module 名稱清單（不附程式碼——附程式碼會讓 context 大小失去 module 邊界的收斂效果，違反上一行的設計精神），prompt 明確指示「若在自己的 related_files 裡找不到明顯錯誤，很可能是這些 sibling module 造成的，寫進 unfixable_reasons 建議一併檢查，不要在自己的程式碼裡勉強找一個不存在的 bug」 |
| `state.get("service_diagnostics")`（若非空） | 見下方「`service_diagnostics` 要傳給所有 `root_cause` module」——這一輪 ⑥ 判定 `service_unreachable` 時讀到的容器崩潰 log 全文，`None`（正常情境）以外的所有 `origin=="root_cause"` module 呼叫都統一傳入 |

### `known_fill_failures`：一旦失敗過一次，改由⑤帶著⑦的診斷重新翻譯

**這是本文件的正式決策，補記一條先前只在口頭討論中確認、卻沒有被準確寫進 10a/10b 的規則**：一個 task 只要在 `task_failures` 裡出現過一筆 `reason=="fill_failed"`（⑤ 本地模型完全沒能產生可用的函式本體），從下一輪 debug 開始，這個 task 就交由 ⑦ 判定「需要重新翻譯」（`retranslate=true`），`implement_node.py` 的排程器**不再把它當成一般 task 排回去、也不會再回落到「永久排除」的狀態**——但也**不是讓 ⑦ 自己頂替寫出完整函式本體**（見下方「2026-09-05 修正」）。

**真實環境重跑才發現這個落差**（2026-08-27，run_id `20260827_015722_639ebd`）：`app/services/exam_service.py` 底下 6 個 task 連續 3 輪、每輪最多 3 次 attempt，9/9 全部失敗，成功率 0%（其中一次真實回應完全答非所問，生出一整段無關的 SQLAlchemy model 定義，耗時近 10 分鐘）——查證發現 `implement_node.py::_run_one_task()` 的既有機制（`pending_fixed_bodies.get(task["id"])`，見八章）只在 ⑦ 這一輪**明確**針對這個 task_id 開出修正時才跳過本地模型；`known_fill_failures` 雖然已經被傳給 ⑦ 當背景資訊（見上表），但 `DEBUG_SYSTEM_PROMPT`（見 `debug_agent/prompts.py`）原本只把它當成「已知確定翻譯失敗的task，附error訊息，是最直接的線索」這種參考性質的背景資料，沒有明確要求 ⑦ 一定要對它輸出 `task_fixes`——這正是 09a 五章 `task_failures` 定案時「`"fill_failed"`（可能重試修好）」這句措辭留下的缺口：沒說清楚「重試」的執行者該是 ⑤ 還是 ⑦，兩者被含糊地當成同一件事。

**最初的修法（已被下方「2026-09-05 修正」取代）**：`DEBUG_SYSTEM_PROMPT` 新增明確的任務指示：對 `known_fill_failures` 裡的每一個 task，⑦ 一律要求輸出 `task_fixes`，且明講這是「從零生成」，只根據 `tasks` 清單裡的 `description`／`class_name`／`target_files` 重新寫一份完整 `fixed_body`。這個做法讓這批 task 確實有機會被排入 `task_fixes`，但埋了一個新問題，見下方。

**2026-09-05 修正（對應 `docs/refactor_bug_trace.md` #9）：⑦ 不該自己憑 `description` 編寫 `fixed_body`**——真實環境重跑逐一查證發現：⑦ 手上從來沒有這些函式對應的真實 Java 原始碼（`_analyze_root_cause_module()` 的 `source_files` 只讀 `python_project_path`，不碰 `java_project_path`），唯一能依賴的 `description` 欄位，`06a_plan_agent_architecture.md` 自己早就判定「對模型沒有任何有效信號」、明確規定不送進 ⑤ 的翻譯 prompt——⑦ 卻被要求靠這個欄位「從零生成」函式本體，可靠度天生比不上⑤（真的看得到 `java_source`／`referenced_source`）。這不只是 `known_fill_failures`（從沒翻譯過）的問題：⑦ 診斷「已翻譯但邏輯寫錯」的一般案例時（4.1 表格「相關原始碼」那一列）同樣沒有 Java 原始碼可以核對，只能憑 `harness_failures` 症狀反推，一樣有這個風險。

修法：`task_fixes` 新增 `retranslate: bool` 欄位（見六章 `TaskFix`）。函式本體邏輯有問題時（不論是從沒成功翻譯過，還是翻過但邏輯錯），⑦ 優先設 `retranslate=true`、`fixed_body=null`，`diagnosis` 這時候的角色是「告訴⑤上一輪錯在哪」；`pending_retranslate_tasks[task_id]`（新的 State 欄位，value 是 `diagnosis`）取代原本會進 `pending_fixed_bodies` 的內容。`implement_node.py::_run_one_task()` 讀到這個 task_id 時**不**像 `pending_fixed_bodies` 命中那樣跳過模型呼叫——正相反，走跟⑤首輪翻譯完全相同的路徑（解析真實 `java_source`／`referenced_source`、真的呼叫 `fill_function()`），只是把 `diagnosis` 疊加進 `context`，讓模型知道上一輪錯在哪。`fixed_body` 機制保留（`retranslate=false` 時的既有行為不變），但 prompt 明確引導只在「完全不需要核對 Java 原始碼就能確定對錯」的機械性修正才使用（見四章 prompt 片段）——這**不是**重新啟用「⑦ 給指令、本地模型執行」這個已經被否決的舊設計（見一章、八章：一句完全正確的指令「參數 `list` 改名為 `items`」交給本地模型執行，結果只套用了一半，把 `TypeError` 換成新的 `NameError`）——那個舊設計是「⑦ 分析完既有程式碼、生出一句修改指令，再讓本地模型照著指令修改**現有程式碼**」；這裡的 `retranslate` 是把 task 交還給⑤走**正常翻譯**（從真實 Java 原始碼重新生成整個函式本體，跟這個 task 第一次被排到 ⑤ 時做的事完全一樣），`diagnosis` 只是附加在 context 裡的提示文字，不是要模型「執行一個修改指令」，兩者的失敗模式因此不同：舊設計的風險是「指令沒有被完整執行」，這裡沒有指令需要執行，⑤ 只是（帶著多一點提示）重新做一次它本來就會做的翻譯工作。

**底層驗證機制原本就撐得住這個決策，不需要新的資料管道或驗證邏輯**——`_validate_task_fixes()` 的 `valid_task_ids` 本來就是整個 module 扣掉 `scaffold_gap_task_ids` 的全部 task（`analysis.py`），`fill_failed` 的 task_id 從來就在合法集合內；`force_reschedule()`（見八章）的呼叫端只需要多納入 `pending_retranslate_tasks` 的 key，跟 `pending_fixed_bodies` 一視同仁。單元測試見 `tests/debug_agent/test_prompts.py::TestKnownFillFailuresMandatoryRetranslate`／`TestRetranslatePreferredOverFixedBody`、`tests/debug_agent/test_analysis.py` 的 retranslate 相關測試、`tests/graph/test_implement_node.py::TestRunOneTaskRetranslate`。**尚未真實環境端對端重跑確認**這個修正能不能真的讓這批 task 的重新翻譯品質提升——只驗證了機制本身正確接線。

**`service_diagnostics` 要傳給所有 `root_cause` module，不能只在 `special_reason=="batch_reload_timeout"` 時才傳**：`service_unreachable` 且 `failed_modules` 非空（`batch_reload_timeout` 情境，⑤ 已經把受牽連的 module 標記進 `failed_modules`）時，這些 module 會被 3.2 的 `service_unreachable` 分支納入 `modules_with_failures`（`harness_failures` 是空清單），分類成 `root_cause`、送進**正常**的 `_analyze_root_cause_module()` 呼叫——但這條路徑原本完全不讀 `service_diagnostics`，只給 `special_reason="batch_reload_timeout"` 這句人類可讀文字，讓 LLM 在完全沒看到 traceback 的情況下對著（放寬過濾後的）整份 module 原始碼做靜態分析、憑空猜測是哪一行造成崩潰。Python 服務因模組層級 `ImportError`／`SyntaxError` 崩潰時，容器 log 幾乎必然含有精確的 traceback 與行號（`python_service/process.py::PythonServiceContainer.diagnostics`，09b 四章既有屬性，`docker logs --tail 200`）——⑥ 判定不可達的當下已經把這份 log 讀出來寫進 `state["service_diagnostics"]`（見二章），這份訊號原本卻只停在 State 裡沒被接到任何 LLM 呼叫，是本末倒置。

**修正**：`run_debug_analysis()`（十章）呼叫每一個 `origin=="root_cause"` module 的 `_analyze_root_cause_module()` 時，一律多傳入 `state.get("service_diagnostics")`（正常情境下是 `None`，不影響非 `service_unreachable` 的一般失敗案例）；`DEBUG_SYSTEM_PROMPT`（四章）新增一個欄位說明，指示 LLM「若這個欄位非空，優先核對 log 裡的 traceback 檔案路徑是不是落在這個 module 的 `target_files` 裡；若是，這比純粹的靜態程式碼比對更精確，直接依 log 內容判斷；若 traceback 指向的檔案不在自己的 `target_files` 裡，根因很可能在 `batch_sibling_modules` 列出的其他 module，比照既有指示處理」。**這不是新開一條資料管道，是把三章已經寫進 State 的既有資料接到另一個原本沒接上的呼叫點**——`service_diagnostics` 本身的產生（⑥ 判定不可達時讀取）完全不用改，只需要在 `_analyze_root_cause_module()` 的呼叫端多傳一個既有的 State 值。

**為什麼不是只在 `special_reason=="batch_reload_timeout"` 的 module 才傳，而是這一輪所有 `root_cause` module 都傳**：`service_diagnostics` 是**全域**的（整個 Python 服務崩潰只有一份 log，不是逐 module 各自一份），而 `special_reason` 只反映**這個 module 自己**上一輪 `partial_reports` 的最新狀態（見 3.5）——一個 module 這一輪若剛好因為其他原因（例如 mutation-only 失敗，跟本輪這次崩潰無關）被歸類成 `root_cause`，但 `service_diagnostics` 剛好非空（代表**這一輪** ⑥ 也遇到了 `service_unreachable`），統一傳入不會有害：log 裡的 traceback 若跟這個 module 完全無關，LLM 依既有 4.1 prompt 設計本來就只會就事論事地看 `harness_failures`／程式碼本身，不會因為多了一段不相關的 log 就產生额外幻覺；反過來若限定只給 `special_reason=="batch_reload_timeout"` 的 module，會漏掉「這個 module 這一輪的 `partial_reports` 剛好沒有新紀錄（見 3.5「過期資料」，`special_reason` 因此是 `None`），但實際上就是這次崩潰的真正元凶」這種情況——統一傳入是更簡單、也更不會漏掉真正相關 module 的做法。

**為什麼要疊加 `target_files`，不能只給 `related_files`**：`related_files` 的來源是 `RouteMapper.resolve_related_files()`，回傳的是 `route_to_file_mapping` 這個 key 底下的檔案清單——而 `route_to_file_mapping` 依 05a 三章「Python 專案分層與命名慣例」的既有設計，**只涵蓋 `routers`／`services`／`repositories` 三層**（02a 十一章的範例就只有這三層檔案），不包含 `schemas/{module}.py`（Pydantic model，定義 API 回應的欄位結構）或 `models/{module}.py`（SQLAlchemy model，定義資料表對應）——這兩類檔案從來就不是任何一個 endpoint 的「route 對應檔案」，`route_to_file_mapping` 從設計上就沒有機會收進它們。02a 九章 `failure_type` 分類表列出的 `missing_fields`（Response 缺少欄位）／`type_mismatch`（欄位型別不符）兩種失敗，根因大機率就落在這兩類檔案裡——若 Debug Agent 看不到 `schemas/{module}.py` 的欄位定義，既無法判斷「這個欄位到底有沒有在 Pydantic model 裡宣告」，也無法對照「型別宣告是不是跟 golden 期待的不一致」，只能憑空猜測，容易產生幻覺或給出語法看似合法但邏輯不對的 `fixed_body`。

**修法**：把 `related_files` 跟這個 module 全部 task 的 `target_files`（不只 `target_files[0]`）取聯集，再一起讀取。這不是新開一條資料管道——`schemas/{module}.py`／`models/{module}.py`（含 09a 五章新增的 `_enums.py`）本身就是 06a 七章「`target_files` 組裝規則」既有設計裡，`routers` 層 task（需要知道回應該長什麼樣）與 `services`／`repositories` 層 task（需要操作 ORM model）的**唯讀 context** 組成部分，早就存在於 `task_list` 裡，只是四章第一版沒有把它們一併讀進來。`schemas`／`models` 檔案本身不對應任何一個 `TaskSpec`（它們是④骨架階段機械渲染的資料結構定義，不是⑤逐函式填空的對象，見 08a／09a），因此不會出現在 `related_files` 或 `task_list.target_files[0]`（各 task 自己的寫入目標）裡——**只能**透過既有 task 的唯讀 context 這條路徑取得。

**為什麼要限縮，不是整份 `target_files` 無差別聯集**：06a 七章「`target_files` 組裝規則」允許 `target_files` 帶入的唯讀 context 不只 `schemas`／`models`，還包含 `referenced_interfaces`——這個 task 業務邏輯上會呼叫到的其他 module 的 router／service／repository 檔案（同 module 內或跨 module 皆可能）。若對「這個 module 全部 task」的 `target_files` 做無差別聯集，一個依賴關係複雜、task 數量多的 module，實際讀進來的檔案集合可能遠遠超出「這個失敗案例牽涉到的範圍」，稀釋 LLM 對真正相關程式碼的注意力，也增加不必要的 token 成本——這正是最初把 `related_files`（Harness 精確算出、只涵蓋這次失敗案例呼叫鏈的檔案）當作主要輸入來源的理由（四章開頭），疊加 `target_files` 的本意只是補上 `route_to_file_mapping` 結構性看不到的缺口，不是要擴大成整個 module 的依賴閉包。

**過濾規則不能只列 `schemas`／`models`，還必須涵蓋 `core`**：05a 三章「全域基礎設施檔案」定案的 `app/core/database.py`／`app/core/exception_handlers.py`，性質上跟 `schemas`／`models`完全一樣——都是機械產生、不對應任何 `TaskSpec`（不是⑤逐函式填空的對象）、只會以「唯讀 context」的形式出現在其他 task 的 `target_files` 裡，因此也不會出現在 `related_files`（`route_to_file_mapping` 只涵蓋 `routers`／`services`／`repositories` 三層，見 05a 三章、02a 十一章）。若過濾規則只列 `schemas`／`models`，任何一個 task 若透過 `app/core/exception_handlers.py`（例如呼叫某個共用的錯誤處理輔助函式）出問題，會因為這個檔案剛好落在既不是 `related_files`、又不是 `schemas`／`models` 的位置而完全看不到，因此聯集時取 `target_files` 裡父目錄落在 `schemas`／`models`／`core` 任一個的項目：

```python
def _is_global_infra_file(path: str) -> bool:
    from pathlib import PurePosixPath
    return PurePosixPath(path).parent.name in ("schemas", "models", "core")
```

**為什麼是「機械產生、無擁有 task 的全域檔案」這個特徵，不是隨機挑幾個目錄名**：`schemas`／`models`／`core` 三者共同的性質才是這條規則真正要抓的東西——它們都不對應任何 `TaskSpec`，只能透過其他 task 的唯讀 context 被看到，見上一段「本身不對應任何 TaskSpec」的既有推理，`core` 只是把同一個推理再套用到 05a 三章原本就已經定案、但四章第一版沒有連帶想到的第三個既有類別，不是新發明一條規則。

**這條規則仍然不夠：純目錄名判斷漏掉了「有擁有 task、但擁有它的模組自己沒有 HTTP endpoint」這一類檔案**（`docs/09b_bug_trace.md #49`，已用真實案例驗證兩次）——`common_service.py`（分派給 `common` 模組）父目錄是 `services`，不在 `schemas`／`models`／`core` 白名單內，即使它確實出現在其他模組（如 `exam`）task 的 `target_files[1:]`（真實業務邏輯明確 `import` 它，不是假設），仍然會被這條規則濾掉。這跟 `schemas`／`models`／`core` 的情況本質相同：`common` 模組自己沒有任何 route，`test_results.failures[*].module` 永遠不會出現 `common`，3.2 的模組篩選機制永遠不會選中它去分析——**唯一能看到它的機會，就是被其他模組當成 `target_files[1:]` 帶進來的這一刻，過濾規則卻恰好把它擋住了**。

**修正**：`_is_global_infra_file()` 除了既有的目錄名判斷，改成「或者這個檔案的擁有模組在 `route_to_module_mapping` 裡查不到任何一個 route」——後面這個條件需要額外傳入 `module` 到 `route` 的反查資訊。`module_list` 是 State 既有欄位，但 `route_to_module_mapping` **不是**：它是③寫進 `config/harness.yaml`、由 `refactor_harness/core/route_mapper.py::RouteMapper` 讀出的既有機制（見 02a 十三章），沒有進 `RefactorState`——這裡直接建構一個 `RouteMapper()` 讀檔案，不經過任何 `graph/nodes/*.py`，維持八章「⑦ 全程不 import 任何 node」的既有邊界：

```python
def _is_global_infra_file(path: str, zero_endpoint_modules: set[str], file_to_module: dict[str, str]) -> bool:
    from pathlib import PurePosixPath
    if PurePosixPath(path).parent.name in ("schemas", "models", "core"):
        return True
    owning_module = file_to_module.get(path)
    return owning_module is not None and owning_module in zero_endpoint_modules
```

`zero_endpoint_modules`（三章正規化時順便算出，不是新的一次性資料）：`{m["module"] for m in module_list} - {route_to_module_mapping 裡出現過的所有 module}`。`file_to_module`：`{t["target_files"][0]: t["module"] for t in task_list if t["target_files"]}`，即每個檔案的擁有模組。這一版仍然無法涵蓋「檔案從未被任何模組的 task 當成 `referenced_interfaces` 引用」的情況（見下方「`_global` 這個保留模組」）——它解決的是「被引用了、但過濾規則本身把它擋掉」，不是「從未被引用過」。

**`_global` 這個保留模組（09b「全域例外處理」）是更極端的情況，需要不同的機制**：`app/core/exception_handlers.py` 分派給 `_global` 模組，但它不是被業務邏輯 `import` 呼叫的（是③機械組裝進 `app.main.py::app.add_exception_handler()`），因此**從未出現在任何模組的 `target_files[1:]`**——不管 `_is_global_infra_file()` 怎麼調整，只要判斷依據是「這個模組的 task 有沒有把它列進 `target_files`」，`_global` 就永遠不會被任何模組的 context 帶到。**修正**：`_global` 這種「機械保留、無業務呼叫關係、檔案數量少」的模組，比照四章「`service_diagnostics` 要傳給所有 `root_cause` module」的既有精神——只要這一輪有任何 `root_cause` module，`_global` 底下的檔案（`app/core/exception_handlers.py` 這類數量很少的全域例外處理檔案）就無條件當成額外唯讀 context 附加給每一個 `root_cause` module，不需要先確認有沒有被引用。這不會顯著增加 token 成本（`_global` 定義上只有機械收集的 catch-all handler，檔案數量少且小），也符合「任何一個 endpoint 的例外都可能經過這裡」的實際語意。

**這個過濾規則的已知取捨**：`referenced_interfaces` 裡若真的有一個跨 module 的 service／repository 檔案是這次失敗案例的真正根因（例如：這個 module 正確呼叫了另一個 module 的函式，但那個函式本身回傳了錯誤的值），這個過濾規則不會把那個檔案的原始碼帶進來——但這種情況下，根因其實**不在這個 module**，應該在對方 module 自己的 `harness_failures`（若對方也失敗）或後續人工排查中被抓到，不是這個 module 這次 LLM 呼叫該負責的範圍；`related_files`（Harness 算出的呼叫鏈）本來就已經涵蓋這個 module 自己的 router／service／repository，這個過濾規則只是刻意不把它再往外擴大到「這個 module 可能呼叫到的所有東西」，維持四章開頭「Debug Agent 要看的是這個 module 自己的程式碼」這個既有邊界。

**例外（這個過濾規則的前提是「已經算出的 `related_files` 聯集」非空，不成立時必須整個放寬，不是照舊套用）**——上面整段推理的地基是「`related_files`（Harness 算出的呼叫鏈）本來就已經涵蓋這個 module 自己的 router／service／repository，疊加 `target_files` 只是為了補 `schemas`／`models`／`core` 的缺口」；這個地基不成立的情況**不只**三章「`service_unreachable` 分支」那一種（`ctx["harness_failures"]` 整個是空陣列），還有一種同樣會發生、範圍更小但一樣真實的情況：**`ctx["harness_failures"]` 本身非空，但這個 module 這一輪全部的失敗案例剛好都是 `golden_not_found`、且對應的 `related_files` 都是空清單**——`RouteMapper.resolve_related_files()`（02a 十一章）查不到 `route_to_file_mapping` 對應的 key 時就回傳空清單（不是 fallback 猜測，`route_to_file_mapping` 沒有 `route_to_module_mapping` 那種 URL 推斷的 fallback 機制），這正是 `golden_not_found` 最常見的成因之一（③ 產出的 `route_to_file_mapping` 漏掉這個 endpoint）。若這個 module 剛好只有這一類失敗、沒有任何其他案例貢獻非空的 `related_files`，`ctx["harness_failures"]` 檢查為真（非空），會誤走「只取 `schemas`／`models`／`core`」那條分支，跟 `service_unreachable` 分支一樣看不到任何一行 `routers`／`services`／`repositories` 的程式碼——但這次的根因往往就在 router 層本身（這個 endpoint 沒有被正確路由或壓根沒實作），偏偏是這個過濾規則會擋住的東西。

**修正**：判斷條件從「`ctx["harness_failures"]` 是否非空」改成「**已經從 `harness_failures` 算出來的 `related_files` 聯集**是否非空」——這是同一個地基的精確表達，`harness_failures` 空陣列只是「這個聯集必然是空的」其中一種、範圍最大的成因，不是唯一成因：

```python
related_files = {rf for f in ctx["harness_failures"] for rf in f.get("related_files", [])}

if related_files:
    # 有 related_files 可用時，「這個 task 自己的主要寫入目標」
    # （target_files[0]）永遠納入，不經過 _is_global_infra_file() 過濾；
    # 只有 target_files[1:]（referenced_interfaces 帶進來的唯讀參考
    # 檔案）才套用 schemas/models/core 過濾規則。
    for t in all_tasks:
        if t["target_files"]:
            related_files.add(t["target_files"][0])
        related_files |= {tf for tf in t["target_files"][1:] if _is_global_infra_file(tf)}
else:
    # 不論是 harness_failures 整個是空的（service_unreachable），還是
    # harness_failures 非空但每一筆的 related_files 都剛好是空的
    # （route_to_file_mapping 漏了這批 golden_not_found case 對應的
    # route）——兩者的共同點都是「沒有已經聚焦的訊號可以依賴」，處理
    # 方式因此相同：整份 target_files 不過濾。
    related_files |= {tf for t in all_tasks for tf in t["target_files"]}
```

這個條件同時涵蓋了三章「`service_unreachable` 分支」（`harness_failures` 整個為空 → `related_files` 聯集必然為空，落入 `else`）與這裡新增的 `golden_not_found` 情境（`harness_failures` 非空，但聯集恰好為空，一樣落入 `else`）——不需要為兩種情況分別寫判斷式，直接檢查最終要用的那個變數本身有沒有內容，比檢查它的其中一個上游來源（`ctx["harness_failures"]`）更精確，也自然涵蓋所有「導致這個聯集為空」的成因，不需要窮舉列出。

**`related_files` 分支裡為什麼 `target_files[0]` 要跳過 `_is_global_infra_file()` 過濾，不能跟 `target_files[1:]` 一視同仁**：這是接上真實環境後才發現的真實缺陷，不是理論推演——第一版曾經對 `all_tasks` 的整份 `target_files`（含 `[0]`）套用同一條過濾規則，實測時剛好有一個 task 自己的主要寫入目標是 `app/services/{module}.py`（父目錄是 `services`，不落在 `schemas`／`models`／`core` 任一類），於是這個 task 自己的程式碼被這條規則整個濾掉，導致該次呼叫完全看不到真正的根因所在檔案、只能就其他無關程式碼做出一個看似合理但錯誤的次要推論。`target_files[0]` 是 `all_tasks`（已經限定「這個 module 底下的 task」）裡每個 task 自己的寫入目標，本來就該無條件納入，跟「這個 module 看不看得到 `schemas`／`models`／`core` 這類無擁有 task 的全域檔案」是兩件不相關的事，`_is_global_infra_file()` 只對 `target_files[1:]`（`referenced_interfaces` 帶進來的唯讀參考檔案）有意義。

**`tasks` 清單本身就是 task_id 的消歧根據，不需要額外編碼成單一字串**：每個 task 條目已經帶 `class_name`／`target_files`，同名函式散落在不同 class／檔案時（如 `UserRepository.get_by_id` 與 `OrderRepository.get_by_id`），LLM 判斷「是哪個 task」時本來就要對照程式碼實際內容（`source_files` 已經是以檔案路徑為 key），`task_id` 本身在 `task_list` 裡就是唯一值（06a：「`id`：task 唯一識別碼」），不存在「同名函式導致 `task_id` 本身有歧義」的問題——真正的風險是「LLM 選錯了一個存在但不是真正根因的 task_id」，這是任何除錯推理都無法用輸出格式消除的風險，4.3 的驗證只能也只需要擋「引用不存在的 task_id」，見 4.3。

**刻意不放的輸入**：Java 原始碼全文。`task.description`／`task.context` 已經是 [P] 從 Java 邏輯萃取出來的業務描述（⑤ 翻譯時依據的就是這份輸入），Debug Agent 站在跟 ⑤ 相同的資訊基礎上判斷「為什麼結果跟這份描述兜不起來」已經足以涵蓋 09b_bug_trace.md 記錄的絕大多數真實案例（`list` 遮蔽 builtin、缺 `ORDER BY`、缺必填欄位、import 不存在的模組等，見七章附錄）——這些都能單獨從 Python 程式碼＋錯誤現象判斷，不需要比對 Java 原始碼。少數需要比對 Java 語意才能發現的案例（如 `#23`：`.stream().distinct()` 保序去重 vs. Python `set()` 不保序）留在十二章「待決定事項」，不在第一版做，避免每個 module 呼叫都額外背上一份 Java 原始碼的 context 成本。

### 4.2 輸出 Schema（Structured Output）

```python
DEBUG_OUTPUT_SCHEMA = {
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

**`fixed_body` 是完整程式碼，不是自然語言指令**：完整理由與真實案例見八章「⑤ 端的對應改動」——簡言之，⑦ 已經讀完整份原始碼、定位到根因，直接產生修正後的函式本體（格式同 `ollama_client.get_function_body()` 既有 delimiter 契約：未縮排陳述式文字，不含 `def` 簽名行），套用時直接取代函式本體、完全跳過 ⑤ 的本地模型呼叫，不是把結論翻譯成指令再讓能力較弱的模型重新生成一次。

**`fixed_body` 可以是 `null`，`file_fixes` 補上函式本體以外的修正（phase 2）**：`fixed_body` 受限於 `fill_function()` 的 AST 函式定位機制——只能取代**函式本體**，碰不到函式簽名（參數名稱／型別標註）或模組層級的敘述（import、常數宣告）。`file_fixes` 是精確字串替換（`target_file`＋`old_snippet`＋`new_snippet`，`old_snippet` 必須在檔案裡逐字出現剛好一次），不透過 AST，因此能碰到 `fixed_body` 碰不到的任何範圍，包括函式簽名本身。兩者不互斥，同一個 task_fix 可以只有其中一個，也可以兩個都有（例如函式本體要改、簽名也要改）；至少要有一個非空，見 4.3。完整設計理由與真實案例見八章「⑤ 端的對應改動：phase 2」。

**prompt 明確要求：根因確實是檔案層級問題時必須用 `file_fixes` 精確補洞，不能在 `fixed_body` 裡繞道**：真實測試發現只描述「`fixed_body` 碰不到的範圍改用 `file_fixes`」不夠——LLM 診斷對了「某個 enum／常數沒被 import」這個檔案層級根因，卻選擇在 `fixed_body` 裡把函式改寫成不再依賴那個 enum（例如原本該用具名錯誤碼的地方，換成猜測的寫死數值），繞過需要修 import 這件事。這種繞道即使剛好通過驗證，也已經悄悄偏離 `source_files`／`description` 描述的原始業務邏輯，比「誠實回報 `unfixable_reasons`」更差。因此 prompt 明確要求：根因確實是檔案層級的問題時必須用 `file_fixes` 補上缺口、讓函式繼續以原本方式引用該 enum／常數，不要改寫本體去閃避它；判斷不出正確 import 路徑時，寧可如實寫進 `unfixable_reasons`，不要用猜的數值掩蓋。

**Prompt 對 `task_id` 的硬性要求**（比照 06a 五章「不要虛構或猜測不存在的 interface_id」同一種寫法）：只能從「這個 module 全部 task 的清單」裡選,不能是 `scaffold_gap_task_ids` 裡的 task（明確告知：這些函式骨架不存在,如果你判斷問題出在其中一個,把它寫進 `unfixable_reasons`,不要放進 `task_fixes`）,不要虛構不存在的 task_id。

**`fixable` 的最終值必須從「4.3 驗證過、已剔除幻覺 task_id 之後」的 `task_fixes` 推導,不能是 LLM 回應裡原始的 `fixable` 欄位本身**——這是執行順序的硬性要求,不是兩件事「順便都做一下」：

```
1. 先做 4.3：task_id 反查驗證，把不合法的 task_fix 從 task_fixes 剔除
2. 用「剔除後」的 task_fixes 重新推導 fixable：fixable = bool(task_fixes)
3. 若 LLM 原始回應的 fixable 是 True，但剔除後 task_fixes 變空（不論是
   LLM 一開始就沒給、還是給了但全部因引用不存在的 task_id 被 4.3 剔除），
   在 unfixable_reasons 補一筆固定文字，說明這是二次校正後的結果
```

**這個順序不能反過來**：若照本節與下一節（4.3）在文件裡出現的先後順序、按字面直覺實作成「先用 LLM 原始的 `fixable`／`task_fixes` 做這裡的一致性檢查，事後才做 4.3 的 task_id 驗證」，會出現一個真實的非法狀態——LLM 回應 `fixable=True` 且給了一筆 `task_fixes`，這裡檢查「`fixable=True` 且 `task_fixes` 非空」通過、沒有觸發二次校正，`fixable` 就這樣定案成 `True`；但這唯一一筆 `task_fixes` 的 `task_id` 其實是幻覺出來的，4.3 事後才把它剔除，剔除後 `task_fixes` 變空——最終狀態就是 `fixable=True` 但 `task_fixes=[]`，正是這一節開頭想要杜絕的非法狀態，只是繞了兩節文件的順序又漏接回來。10b 二章的實際實作把 `fixable` 定義成 `bool(task_fixes)`（`task_fixes` 已經是 4.3 驗證過的結果），不是先讀 `raw["fixable"]` 再視情況覆寫——這樣寫從結構上就不可能出現上述非法狀態，`if raw["fixable"] and not task_fixes` 那一句只負責在 `unfixable_reasons` 補一筆說明文字，不負責「校正」`fixable` 本身（`fixable` 從一開始就沒有機會被設成不一致的值）。

### 4.3 `task_id` 反查驗證（防幻覺，必須先於上一節的 `fixable` 推導執行）

回應解析後,對每一筆 `task_fixes[i]`：

```python
valid_task_ids = {t["id"] for t in task_list if t["module"] == module} - scaffold_gap_task_ids
```

`task_id` 不在 `valid_task_ids` 內的整筆**捨棄並記警告**（不是中止整個 module 的分析——其餘合法的 task_fix 仍然有效）。這比照 09a 六章「三元組比對」與 06a 五章「`referenced_interfaces` 只能引用...不要虛構」同一種防禦性原則：LLM 的自由文字輸出可能幻覺出看起來合理但不存在的 task_id（尤其當它把兩個相似函式名搞混時）,結構化 schema 只保證格式,不保證內容真實,這一步是內容層級的驗證。

同一步也捨棄 `retranslate`／`fixed_body`／`file_fixes` 三者都沒有真正生效的 task_fix（並記警告）——schema 只保證欄位都存在，不保證至少一個真正生效，這種輸出沒有任何實際修正內容，留著只會在 `pending_fixed_bodies`／`pending_retranslate_tasks`／`pending_file_fixes` 裡製造一筆什麼都不做的空紀錄。`retranslate=true` 時強制把 `fixed_body` 視為 `None`（即使 LLM 違反 prompt 指示、兩者都給了值），見 `docs/refactor_bug_trace.md` #9、`debug_agent/analysis.py::_validate_task_fixes()`。

### 4.4 單一 module 呼叫失敗時

比照 04a 四章「待重試清單」機制：失敗的 module 列入待重試清單,這一輪其餘 module 分析完後統一重試一次；仍失敗則這個 module 在這一輪**視為未分析**（不是視為 `fixable=False`）——區分「LLM 判斷這是真的修不好」與「API 暫時故障沒分析到」很重要,見七章「give_up_early 的判斷邊界」,後者不能計入不可修的證據。

---

## 五、找出 task_id 的機制：`related_files` → `task_id` 反查

`harness_failures[*].related_files` 只有檔案清單,不是函式名——同一個檔案內可能有多個函式（如一個 `service.py` 裡有五個方法）,機械對應（比如「有出現在 related_files 裡的檔案就是嫌疑犯」）粒度太粗,無法精確到函式層級,也無法在「這個檔案裡有三個函式,只有一個寫錯」時只建議修那一個。

因此不做機械對應,改成**把 module 內全部 task 的清單（不只失敗的）連同程式碼一起交給 LLM**,由 LLM 自己從「錯誤現象＋程式碼內容＋每個 task 該做什麼（`description`）」反推是哪個函式造成的,直接在 `task_fixes[*].task_id` 指名——這比機械檔案比對準確,因為錯誤往往能從程式碼裡直接讀出「這一行邏輯錯了」,再對照哪個 task 的 `description` 涵蓋這段邏輯。

**這個反查本質上是「給模型完整資訊,讓它做人類除錯者會做的事」,不是新演算法**——類比人類工程師看到一個 API 回傳錯誤,會開啟 router／service／repository 三個檔案,對照 commit log（這裡對應 `task.description`）判斷是哪一段邏輯出錯,Debug Agent 的 LLM 呼叫做的是同一件事,只是輸入結構化了。

四章驗證失敗（4.3）與這裡的反查失敗是同一件事：如果 LLM 精準度不夠、給出的 `task_id` 常常無效,是 prompt／輸入資料的品質問題,不是這個機制本身的設計缺陷,調校方向見十三章「尚待確認事項」。

---

## 六、輸出格式與 State 對應

### 新增型別

```python
class FileFix(TypedDict):
    task_id: str
    target_file: str
    old_snippet: str
    new_snippet: str


class TaskFix(TypedDict):
    task_id: str
    diagnosis: str
    retranslate: NotRequired[bool]  # 對應 docs/refactor_bug_trace.md #9：函式本體邏輯有問題時優先設 True，交還給⑤重新翻譯
    fixed_body: str | None  # None：這個 task 的函式本體不需要改（或已由 retranslate=True 處理）
    file_fixes: list[FileFix]


class DebugRound(TypedDict):
    round: int              # 對應 state["retry_count"]（呼叫當下的值，遞增前）
    module: str
    origin: Literal["blocked", "module_mismatch", "scaffold_gap", "root_cause"]
    fixable: bool            # blocked/module_mismatch/scaffold_gap 固定 False；root_cause 由 LLM 判斷（經 4.2 二次校正）
    root_cause_summary: str  # blocked/module_mismatch/scaffold_gap 用固定文字，不呼叫 LLM
    task_fixes: list[TaskFix]
    unfixable_reasons: list[str]
```

### `RefactorState` 新增欄位

```python
class RefactorState(TypedDict):
    ...
    # Agent ⑦（逐輪累積寫入，需要 reducer）：每一輪 debug 對每個分析過的
    # module 的完整記錄（含 blocked/scaffold_gap 這種未呼叫 LLM 的機械
    # 判斷），供人工事後查看「⑦ 每一輪在想什麼」。
    debug_rounds: Annotated[list[DebugRound], operator.add]

    # Agent ⑦：這一輪產出的 task 級修正後程式碼，key 是 task_id、value
    # 是完整函式本體。**不掛 reducer，整包覆寫**——這是「這一輪的修正」，
    # 不是累加事件：若某個 task 這一輪沒有被任何 DebugRound 提到（module
    # 是 blocked/scaffold_gap，或 LLM 就是沒給它修正），代表沒有新修正，
    # 這個 task 下一輪直接照 task.description 原樣走一般填空流程（見
    # 八章），不該沿用上上輪可能已經過時、甚至已經套用過但沒用的舊程式碼。
    pending_fixed_bodies: dict[str, str]

    # Agent ⑦（對應 docs/refactor_bug_trace.md #9）：這一輪判定「函式
    # 本體邏輯需要重新翻譯」的 task，key 是 task_id、value 是 ⑦ 的
    # diagnosis（作為重新翻譯的提示）。跟 pending_fixed_bodies 同一種
    # 「這一輪的修正」語意，也一樣不掛 reducer；但 implement_node.py 讀
    # 到這裡的 task_id 時**不**跳過模型呼叫——正常解析 java_source／
    # referenced_source、真的呼叫 fill_function()，只是把 diagnosis
    # 疊加進 context。跟 pending_fixed_bodies 互斥。
    pending_retranslate_tasks: dict[str, str]

    # Agent ⑦：這一輪產出的檔案層級修正（phase 2，見八章「⑤ 端的對應
    # 改動：phase 2」）——fixed_body／fill_function() 的 AST 函式定位
    # 機制只能碰函式本體，函式簽名（參數名稱／型別標註）與模組層級的
    # 敘述（import 等）碰不到的部分走這裡，由
    # translator_cli.apply_file_fix() 用精確字串替換套用，不經過排程器
    # （這些修正不對應任何要重新生成的函式本體）。不掛 reducer，整包
    # 覆寫，語意同 pending_fixed_bodies。
    pending_file_fixes: list[FileFix]

    # Agent ⑦：見七章。這一輪分析後，若確認「所有 root_cause module
    # 都無法修」，設為 True，供 debug_node 的條件邊路由到 give_up，
    # 不進 implement 浪費一輪重試。每輪覆寫，不掛 reducer。
    give_up_early: bool

    # Agent ⑦：這一輪 origin=="root_cause" 的 module 裡，Claude API
    # 呼叫（含批次重試）仍然失敗、視為「未分析」的 module 名稱。每輪
    # 覆寫，不掛 reducer。供 give_up_node.py 在 retry_count 用盡時分辨
    # 「這一輪是 Claude API 打不通」還是「⑦ 判斷邏輯本身有問題」——這
    # 兩種情況原本印出的訊息一模一樣，人工只能自己去翻 debug_rounds 或
    # llmlog 才能分辨，見十章。
    unanalyzed_root_cause_modules: list[str]
```

`debug_rounds` 掛 reducer（歷史追溯，比照 `partial_reports` 的既有精神）；`pending_fixed_bodies`／`pending_file_fixes`／`give_up_early` 是「這一輪的判斷快照」，不掛 reducer（比照 `blocked_modules`／`failed_modules` 的既有精神，見 01 三章）。

**`pending_fixed_bodies` 為什麼不需要額外的「清空」步驟**：`implement_node.py::run()` 只用 `.get()` 讀取這份字典（見八章），不修改、也不刪除它，回傳時仍以 `{**state, ...}` 原樣帶回——這代表若把 `implement` 單獨拿出來看，它確實不會主動清掉自己讀過的修正。但這不會造成「⑤ 在後續某一輪套用過時的程式碼」：graph 的邊結構保證 `implement` 永遠只有兩種前驅——初次進入（`plan`／`scaffold` 的 fan-in，此時 `pending_fixed_bodies` 是 `main.py` 初始化的 `{}`，見七章）、或緊接在一次 `debug` 呼叫之後（`debug → implement`，見七章條件邊）。`debug_node` **每一輪都重新跑三章的正規化＋四章的 LLM 分析，整包覆寫**（不是合併）`pending_fixed_bodies`——所以 `implement` 每次真正執行時，讀到的必然是「最近一次 `debug` 分析出的最新修正」，不可能讀到兩輪以前的舊資料。需要小心的地方只在於：`debug_node` 對某個 module 這一輪沒有重新分析到（例如它已經不在 `test_results.failures` 裡，代表已經修好），這個 module 底下的 task 就不會出現在這一輪的 `pending_fixed_bodies` 裡——這是**設計上刻意的**（六章原文：「代表沒有新修正」），不是遺漏的清空邏輯。

### `retry_count` 遞增：推翻既有 stub 邏輯，改成「每進一次 `debug` 就遞增」

**既有 stub 的問題**：`increment = 1 if state.get("failed_modules") else 0` 依據的是 02a 十二章「`blocked_modules` 不是寫錯了，只是排程還沒排到，不該消耗重試預算」——這個理由本身沒錯，但它建立在一個沒有明說的前提上：「`failed_modules` 為空」等於「這一輪真的沒有東西是壞的」。09a 三章已經明訂 ⑤ 的局部驗證**只跑 readonly，不跑 mutation**（「比照 02a 十三章『第一層：局部驗證』的既定範圍……mutation 的完整涵蓋是 ⑥ 全量驗證的職責，不重複」）；換句話說，`failed_modules` 只反映 ⑤ 自己看得到的那一半事實。

**具體的失效場景**：一個 module 的 readonly 全過（⑤ 判定 `verified`，因此不進 `failed_modules`），但它的 mutation 或跨 module regression 有真正的 bug，只有 ⑥ 的 `test_results`（全量驗證）看得到。這個 module 每一輪都被 ⑤ 標成沒問題，`failed_modules` 因此永遠不包含它；`should_debug_or_done()`（02b）看到 `report["status"] == "fail"` 但 `failed_modules` 空，直接回傳 `"debug"`（這條分支完全不檢查 `retry_count`，見七章）；`debug_node` 若沿用舊 stub 邏輯，`increment` 永遠是 `0`——`retry_count` 永遠停在同一個值。若這一輪 LLM 又剛好判斷「這是可修的」（`fixable=True`，這是合理判斷，因為它真的是一個可以修的翻譯品質問題，只是 ⑤ 局部驗證看不到罷了），流程回 `implement`，重複同一個循環，**永久卡在 `implement → run_tests → debug` 三個節點之間，沒有任何機制會讓它停下來**——這不是理論推演，是把 09a 三章「只跑 readonly」與 02a 十二章「只在 failed_modules 非空時遞增」這兩條各自獨立、各自合理的既有規則疊在一起後，直接推導得到的死結。

**修正**：`debug_node` 每次真正執行（也就是真的進入這個節點），就無條件遞增 `retry_count`，不再檢查 `failed_modules`：

```python
new_retry_count = state.get("retry_count", 0) + 1
```

**為什麼可以無條件遞增，不會誤傷 02a 十二章原本想保護的情境**：`debug` 只有一種抵達方式——`should_debug_or_done()` 的 `"pass"` 分支已經先擋掉了 `test_results.status == "pass"` 的情況，能走到 `debug` 就代表 `test_results` **這一輪確實有失敗**，不是「假警報」。02a 十二章原本想避免的是「`blocked_modules` 只是排程還沒排到、什麼都還沒真的錯」這種情況消耗重試預算——但這種情況若要真的發生，必須是「有 module 卡住、但沒有任何東西是真的壞的」，而 `ModuleScheduler` 的拓樸排程在單次 `implement_node.run()` 呼叫內部會把所有可排的都排完才離開 while 迴圈（見 01 六章），會停在 `pending`／`in_progress`／`needs_reverify` 而不繼續前進的唯一原因，是它依賴的上游最終落在 `"failed"`（不是還在排隊，是真的驗證失敗了）——那個真正失敗的上游 module，`test_results` 一定會反映出來，三章的正規化會把它分進 `root_cause`（或極端情況下 `scaffold_gap`／`module_mismatch`，這兩者本身就是「不可能靠重試解決」的機械判斷，不需要 `retry_count` 繼續累積來保護，見七章）。換句話說：**能夠讓 `debug` 被觸發的路徑，追到底一定對應著某個真正的失敗**，「只是排程還沒排到、什麼都沒壞」這種純粹無辜的等待狀態，不會單獨存在到讓 `test_results.status == "fail"` 卻無跡可尋的地步——02a 十二章原本要防的情境，在目前這條 graph 的實際拓樸下不是一個會獨立發生的狀態，用 `failed_modules` 去偵測它因此是防錯了方向、卻意外擋下了三章「mutation-only 失敗」這個真正會發生的場景。

**與七章 `should_debug_or_done()` 修正的關係**：這項修正讓 `retry_count` 在「mutation-only 失敗、`failed_modules` 永遠空」這條路徑上終於會前進，是兩項修正裡的第一步；但只有這一步還不夠——`retry_count` 會前進，不代表有人會去檢查它是否已經超過上限。七章更進一步，直接把 `should_debug_or_done()`（02b）「`failed_modules` 為空即直接 `debug`」這條完全不檢查 `retry_count` 的既有分支修掉，改成對所有分支統一檢查——這兩項修正必須合在一起看：這一步保證 `retry_count` 會前進，七章保證前進到上限後真的會被攔下來，缺一個都不完整。

---

## 七、`give_up_early`：09a 七章留下的決定

09a 七章「沒有採用的方案：在條件邊層級提早繞過整條 debug 迴圈」明確評估過「若某個 module 有 scaffold 缺口就整條放棄」風險太高（可能連帶放棄其他仍可修的 module），把「更完整的判斷」留給本文件——現在有了 module 級的 `ModuleFailureContext`／`DebugRound`，可以做得比 09a 當初評估的更精確。

### 判斷規則

**這裡是最容易寫錯的一步：判斷「有沒有全部分析到」必須以 `origin == "root_cause"` 的 `ModuleFailureContext` 全集為準，不能只看『已經出現在 `debug_rounds` 裡』的子集**——4.4 已定案「API 呼叫失敗、重試仍失敗的 module 視為未分析，不產生 `DebugRound`」，若判斷式只從「已經有 `DebugRound` 記錄」的集合出發，一個 API 失敗、完全沒被分析到的 module 會直接從集合裡**消失**，而不是以「未分析、fixable 未知」的狀態留在集合裡——`all(...)` 對一個少了一筆的集合下判斷，等於默許「沒分析到」跟「分析後判定不可修」有同樣的效果，恰好推翻了 4.4／本章開頭一直在強調的「API 暫時故障不能算不可修的證據」這個原則。因此判斷式必須先固定住「這一輪 `root_cause` 的 module 有哪些」的完整集合，再檢查這個**完整集合**裡是不是每一個都成功分析、且都不可修：

```python
root_cause_ctxs = [ctx for ctx in module_contexts if ctx.origin == "root_cause"]

if not root_cause_ctxs:
    # 案例二（見下方「root_cause_ctxs 為空時」）：機械判斷已經確定這一輪
    # 沒有任何 module 有機會透過重試修好，不需要（也不該）呼叫 LLM 才能
    # 知道這一輪沒救。
    give_up_early = True
else:
    all_analyzed = all(llm_call_succeeded(ctx.module) for ctx in root_cause_ctxs)  # 4.4 定義的成功／失敗
    # ⚠️ this_round_rounds 是「這一輪呼叫剛剛才建構出來的 DebugRound
    # 清單」，絕對不能寫成 state["debug_rounds"] 或任何名稱看起來像是
    # 直接沿用 state 裡那個掛了 operator.add reducer、逐輪累積的
    # 歷史欄位（見六章「debug_rounds 掛 reducer」）——見下方「為什麼
    # 一定要是這一輪、不能是累積歷史」的完整說明。
    this_round_rounds = [r for r in newly_built_debug_rounds if r.origin == "root_cause"]
    give_up_early = (
        all_analyzed                                    # 一個都不能少，見下方說明
        and all(r.fixable is False for r in this_round_rounds)
    )
```

`all_analyzed` 這個條件不能省略、也不能被「`all(r.fixable is False for r in this_round_rounds)` 剛好對非空清單成立」取代——當 `root_cause_ctxs` 有兩個 module A／B、A 分析成功判定 `fixable=False`、B 因為 API 故障未分析時，`this_round_rounds` 只有 A 一筆，`all(r.fixable is False for r in this_round_rounds)` 單獨看會是 `True`（對只含 A 的清單成立），但 `len(root_cause_ctxs)==2` 而 `len(this_round_rounds)==1`，`all_analyzed` 會是 `False`，正確擋下 `give_up_early`。

`all_analyzed` 直接查 `analyzed_results`（LLM 呼叫成敗的權威來源），不是改用「`len(this_round_rounds)` 是否等於 `len(root_cause_ctxs)`」比較筆數：`analyzed_results` 直接回答「LLM 這次呼叫成功了嗎」，語意上精確對應「4.4 定義的成功／失敗」這件事本身，不會被下游組裝 `DebugRound` 的過程（`_validate_task_fixes()`／`unfixable_reasons` 組裝）連帶影響。

**為什麼一定要是這一輪、不能是累積歷史**：`state["debug_rounds"]` 掛的是 `Annotated[list[DebugRound], operator.add]`（六章），代表**整條 graph run 從第一次進 `debug` 開始，每一輪的 `DebugRound` 都會被永久累加進去，不會被覆寫或清除**。若這裡的判斷式誤用累積歷史（例如寫成對 `state.get("debug_rounds", [])` 取 `origin=="root_cause"` 的全部筆數），會直接引入一個新的、比 `all_analyzed` 缺失更隱蔽的 bug：一個 module 若在**第一輪**被 LLM 判定 `fixable=True`（`task_fixes` 非空，因此沒有進一步重試——`fixable=True` 就會直接產生 `pending_fixed_bodies`，理應在下一輪 `implement` 被修好），但那個修法沒有真的成功，**第二輪**這個 module 又出現、被判定 `fixable=False`——這時候累積歷史裡同時有第一輪的 `True` 記錄與第二輪的 `False` 記錄，`all(r.fixable is False for r in ...)` 會因為第一輪那筆 `True` 而**永遠**回傳 `False`，等於 `give_up_early` 從此對這個 module 永久失效，不論之後幾輪它一直被判定不可修，這個判斷式都會被那筆過期的、早已被後續輪次推翻的 `True` 記錄卡死。**因此 `run_debug_analysis()` 內部處理 `give_up_early` 判斷時，`debug_rounds` 必須是每次呼叫時從空清單重新建構、只包含這一輪產出的 `DebugRound`，回傳時才透過 `"debug_rounds"` 這個 key 交給 LangGraph 的 reducer 疊加進歷史——絕不能反過來去讀 `state.get("debug_rounds", [])` 這份歷史清單**，變數命名也要跟 state 欄位明確區分，避免誤用。

**為什麼只看 `root_cause` 的 module，不看 `blocked`／`module_mismatch`／`scaffold_gap`**：這三種 origin 各自的結論（`blocked` 待上游、`module_mismatch` 待人工核對資料、`scaffold_gap` 待重新 scaffold）本身都已經確定，**單獨**存在不足以判斷整條 pipeline 該放棄——09a 七章的顧慮完全成立：可能只有一個 module 卡在骨架缺口，其他 module 都還救得回來。只有當**所有**送進 LLM 分析的 `root_cause` module 都成功分析、且都被判定 `fixable=False`，才代表這一輪已經沒有任何一個「可能透過重試修好」的東西了——這時候不論其餘三種 origin 的 module 有幾個，它們的命運已經跟著 `root_cause` module 一起確定：`blocked` 永遠等不到上游 verified，`module_mismatch`／`scaffold_gap` 本來就不可能靠重試修。

**`root_cause_ctxs` 為空時（案例二）：`give_up_early` 必須是 `True`，不是 `False`**：`root_cause_ctxs` 為空**恰好就是**「三章的機械規則已經證明這一輪沒有任何 module 可能透過重試修好」的直接證據，不是「還不確定」。逐一檢視三章其餘三種 origin 在到達 `debug` 這個時間點時能不能靠回 `implement` 自己解決：

- `scaffold_gap`：定義上就是「重新生成也無用」，回 `implement` 只會被 `already_failed`（09a 七章）擋下，不會被重新嘗試
- `module_mismatch`：`test_results` 的 module 對不上任何 task，`implement` 的排程器根本不認識這個「module」，沒有對應的 task 可以送去重試
- `blocked`：它的命運完全綁定在 `blocked_reasons` 指向的上游 module 身上——若那個上游這一輪也在 `test_results.failures` 裡（它必然在，因為它正是造成下游被卡住的真正原因，`test_results` 對它一定測得到），它自己會被三章分進 `root_cause`／`scaffold_gap`／`module_mismatch` 三者之一；若它被分進後兩者，前面兩點已經證明「重試無用」；若它被分進 `root_cause`，它就會出現在 `root_cause_ctxs` 裡，跟這裡討論的「`root_cause_ctxs` 為空」互相矛盾。也就是說，只要 `root_cause_ctxs` 真的是空的，**不可能**有一個 `blocked` module 的上游最終仍然「有機會被修好」卻沒有出現在 `root_cause_ctxs` 裡——這條路徑上不存在被遺漏的可修母體。

因此 `root_cause_ctxs` 為空這件事本身，已經是「這一輪沒有任何東西能透過重試修好」的完整證明，不需要、也不應該再送回 `implement` 跑一輪注定徒勞的 `implement → run_tests`（`09a` 的排程器會把 `scaffold_gap`／`module_mismatch` 對應的 task 全部當 `already_failed` 跳過，`blocked` 的下游依然卡在原地，這一輪除了浪費一次 Docker 容器重啟＋Newman 執行的時間，不會改變任何 module 的狀態）。

### 路由方式：`retry_count` 上限檢查前移到 `should_debug_or_done()`（02b），不在下游用防禦性補丁妥協

**為什麼不在 `should_retry_or_give_up`（`debug` 之後、`implement` 之前）補一道 `retry_count > MAX_RETRY` 防禦性檢查**：`should_debug_or_done()`（02b）「`failed_modules` 為空即直接回傳 `"debug"`」這條既有分支完全不檢查 `retry_count`，六章「每進一次 `debug` 就無條件遞增」的修正若不接住這條分支就可能無限循環——但補在下游只能在事情已經發生之後（`debug` 已經呼叫了一次本可避免的 Claude API）才攔下來，且因為只能用 `>`（用 `>=` 會沒收 `debug` 剛分析出的最後一輪 `fixed_body`，見下方保留的推演），在「`failed_modules` 永遠空」這個場景下還要多容許一輪才會真正攔下——等於每次觸發這道防線都白白多花一次 LLM 呼叫。

**修正：直接修正 `should_debug_or_done()`（02b）本身，把 `retry_count` 上限檢查移到最前面、對所有分支統一套用**，不再區分 `failed_modules` 是否為空：

```python
# refactor_harness/langgraph_nodes/test_nodes.py（異動，見 10b 一章）
def should_debug_or_done(state: RefactorState) -> str:
    """
    retry_count 上限檢查對所有分支統一套用，不再依 failed_modules 是否
    為空分岔（見 10a 七章「為什麼要統一套用」）。
    """
    report = state["test_results"]
    if report["status"] == "pass":
        return "done"
    if state["retry_count"] >= MAX_RETRY:
        return "give_up"
    return "debug"
```

`debug` 之後的 `should_retry_or_give_up` 因此不再需要重複檢查 `retry_count`，只留 `give_up_early` 這個獨立成立的判斷：

```python
# graph/nodes/debug_node.py
def should_retry_or_give_up(state: RefactorState) -> str:
    # retry_count 上限檢查已經前移到 should_debug_or_done()（02b，見
    # 上方），進入 debug 之前就已經確認過還在預算內——這裡只需要判斷
    # give_up_early（LLM 自己判定這一輪已無可修，見七章「判斷規則」）。
    return "give_up" if state.get("give_up_early") else "implement"
```

```python
# graph/builder.py（異動）
builder.add_conditional_edges(
    "debug",
    debug_node.should_retry_or_give_up,
    {
        "implement": "implement",
        "give_up": "give_up",
    },
)
```

**為什麼要統一套用，不再區分 `failed_modules` 是否為空**：這條分支原本存在的理由（02a 十二章）是「`blocked_modules` 只是排程還沒排到，不算真正失敗，不該消耗重試預算」——但六章已經證明，這個理由在目前 graph 的實際拓樸下不成立：能讓 `debug` 被觸發，代表 `test_results.status == "fail"` 已經是既成事實，`failed_modules` 是否剛好非空只反映 ⑤ 局部驗證（readonly-only）看不看得到問題，不是「有沒有真的壞」的可靠依據。既然這個區分本身站不住腳，用它決定「要不要檢查 `retry_count`」自然也沒有意義——`retry_count` 該不該繼續消耗，只該看「已經試了幾次」，跟 ⑤ 局部驗證那次剛好有沒有看到問題無關。移除這個分支不會丟失任何原本想保護的情境，因為那個情境本來就不曾獨立存在過。

**這個做法為什麼比補在下游更好**：`should_debug_or_done()` 的檢查發生在**進入 `debug` 之前**，用的是這一輪 `debug` 執行前的 `retry_count`（尚未遞增）——這跟原本「`failed_modules` 非空」那條分支的既有邏輯（`state["retry_count"] >= MAX_RETRY: return "give_up"`）完全是同一套語意、同一個比較符號 `>=`，本來就正確（逐輪推演，套用目前 `MAX_RETRY = 1`：第 1 次進入 `debug` 前 `retry_count=0`，`0>=1` 不成立，正確放行；`debug` 遞增到 1 之後回到 `implement`；下一輪 `retry_count=1`，`1>=1` 成立，正確 give up——恰好 1 次重試，不需要另外處理 off-by-one；這套推演對任何 `MAX_RETRY` 值都成立，不是只對特定數字碰巧正確）。統一套用後，「`failed_modules` 永遠空」那個防禦性場景也套用同一條、已經證明正確的邏輯，不再需要下游那道容許多一輪的補丁，也不會再白白多花一次 LLM 呼叫——從源頭修掉，不需要在下游用近似的防線妥協。

不影響 `implement`／`run_tests` 既有的 fan-in／conditional edge 結構（`debug` 只有單一前驅 `run_tests`，改它自己往下的出邊不影響其他任何節點的既有語意，比照 09a 對 `implement→run_tests` 那次修改的判斷方式）；也不影響 `should_debug_or_done()` 的簽名或呼叫端（`graph/builder.py` 既有的 `add_conditional_edges("run_tests", should_debug_or_done, {...})` 完全不用改，只有函式內部邏輯變了）。

---

## 八、⑤ 端的對應改動：`pending_fixed_bodies` 直接取代函式本體，不經過本地模型

**⑦ 直接產生修正後程式碼，不是自然語言指令**：⑦ 已經讀完整份原始碼、精準定位到根因，直接寫出正確的程式碼，比把結論翻譯成一句指令、再丟給能力較弱的本地模型重新生成一次更可靠——真實案例：一句完全正確的指令（"參數 `list` 改名為 `items`，方法體內所有引用一併改名"）交給 ⑤ 的本地模型（qwen）執行，結果只套用了一半（改了一處內部引用，參數宣告跟 `return list(distinct_values)` 沒有跟著改），把原本的 `TypeError` 換成一個新的 `NameError`。重試不會讓本地模型突然變聰明，只是讓它重新賭一次。

**修正**：`task_fixes[].fixed_body`（四章 4.2）直接是修正後的完整函式本體（跟 `ollama_client.get_function_body()` 既有的 delimiter 格式一樣：未縮排陳述式文字，不含 `def` 簽名行），`pending_fixed_bodies[task_id]` 有值時，`implement_node.py` 直接把這份程式碼傳給 `translator_cli.fill_function()` 新增的 `fixed_body` 參數，**完全跳過 ⑤ 本地模型呼叫**：

```
若 state["pending_fixed_bodies"] 裡有這個 task 的 id：
  _run_one_task() 把對應的 fixed_body 傳給 fill_function(..., fixed_body=...)
  fill_function() 內部：fixed_body 非 None 時跳過 ollama_client.get_function_body()，
  直接拿 fixed_body 當本體，其餘既有步驟（定位函式節點、splice_body()、
  import 解析、語法驗證、寫入＋git commit）不變
```

**這是對既有 `fill_function()`／`_run_one_task()` 的擴充，不是重新設計**——`translator_cli.fill_function()` 新增一個選填參數 `fixed_body: str | None = None`，非 `None` 時跳過「讀 `context_files`、呼叫 `ollama_client.get_function_body()`」這兩步，直接進入既有的 `splice_body()` 之後的所有流程；`_run_one_task()` 從 `pending_fixed_bodies` 查出對應值，原樣傳下去（完整 diff 見 10b）。`_augment_task_io()`（09b 二章既有函式）不再需要處理 ⑦ 的修正——它只剩 09a 五章「疊加規則」（services／repositories 層的 relationship／enum 固定提示）這一件事，因為 ⑦ 的修正不再是「疊加進 context 給模型參考」，是「直接取代函式本體」，兩者不是同一層次的機制，不該共用同一個疊加點。

**這個機制不限於 services／repositories 層**——`pending_fixed_bodies` 可能指向 `routers` 層的 task（例如 `#12`／`#11` 那種例外處理相關的失敗，位於 `routers` 或 `app/core/`），任何層級的 task 只要在這份字典裡出現就直接套用，不受 09a 五章 relationship／enum 提示「限定服務／repositories 層」那條規則約束——兩者本來就是各自獨立的機制。

**目前的範圍：函式本體層級的修正**——`fixed_body` 取代的是 `fill_function()` 既有機制能碰到的範圍，即單一函式的本體，不含模組層級的敘述（如 import）。若 ⑦ 判斷的修正需要動到函式本體以外的地方，`file_fixes`（見上方 phase 2）已經能覆蓋這個範圍。

### `pending_retranslate_tasks`：交還給⑤重新翻譯，不是套用 ⑦ 寫的程式碼（對應 `docs/refactor_bug_trace.md` #9）

**⑦ 沒有這個 task 對應的真實 Java 原始碼**——`_analyze_root_cause_module()` 的 `source_files` 只讀 `python_project_path`，這件事讓上面「⑦ 直接產生修正後程式碼」這句話對函式本體層級的修正並不完全成立：⑦ 能讀完整份**現有 Python 程式碼**、能精準定位到「哪裡不對」，但沒辦法核對「Java 原本到底怎麼寫」，這跟真實案例裡本地模型「執行指令不完整」是不同性質的風險（模型能力問題 vs. 資訊本身就不足），但同樣會讓寫出來的程式碼偏離真正的業務邏輯。

**修正**：`task_fixes[].retranslate`（四章 4.2、六章 `TaskFix`）為 `true` 時，`fixed_body` 必須是 `null`，對應 task_id 進 `pending_retranslate_tasks[task_id] = diagnosis`（不進 `pending_fixed_bodies`）：

```
若 state["pending_retranslate_tasks"] 裡有這個 task 的 id：
  _run_one_task() 正常解析 java_source／referenced_source（不跳過！）
  把 diagnosis 疊加進 context（"⑦ Debug Agent 診斷...請務必依照下方
  java_source 的真實邏輯正確實作，避免重蹈同樣的問題：{diagnosis}"）
  照常呼叫 fill_function(..., fixed_body=None)——這個 task 真的被
  重新翻譯一次（qwen／claude，視 task 本身的 translator_backend），
  不是套用 ⑦ 自己寫的程式碼
```

**跟 `pending_fixed_bodies` 共用 `force_reschedule()` 呼叫點，但套用行為完全相反**：`implement_node.py` 打回 pending、把 task_id 從 `task_done`／`task_failed` 移除這一段對兩份清單一視同仁（`{*pending_fixed_bodies, *pending_retranslate_tasks}` 一起算 `tasks_to_reopen_by_module`）；但 `_run_one_task()` 讀到 `pending_fixed_bodies` 命中時跳過模型呼叫，讀到 `pending_retranslate_tasks` 命中時**不**跳過——這是兩個機制唯一的行為分歧點，其餘（打回 pending、寫入段走 `WRITE_LOCK`）完全共用既有邏輯，不是兩套平行的排程機制。

**`fixed_body` 沒有被淘汰，範圍限縮**：prompt 明確引導只在「完全不需要核對 Java 原始碼就能確定對錯」的機械性修正才使用 `fixed_body`（見四章 prompt 片段第 2 點）——多數函式本體層級的問題都該走 `retranslate`。`file_fixes`（phase 2，檔案層級修正，如 import／enum 缺口）完全不受影響，⑦ 對這類問題仍然直接產生精確的字串替換，不需要 Java 原始碼即可判斷對錯。

### 診斷資料改走 State，不是 `debug_agent/` 直接 import `implement_node`

**`debug_agent/analysis.py` 不能直接 `from graph.nodes.implement_node import get_python_service_diagnostics`**——這是節點與節點之間的直接互相 import，違反了本專案從 04a 到 09a 一路維持的既有慣例：node 檔案（`graph/nodes/*.py`）只做薄封裝，彼此之間不互相 import，跨節點需要共用的東西一律經由 `RefactorState` 傳遞，或下沉到雙方都能匯入的共用套件（`refactor_harness/`、`translator_cli/`、`debug_agent/` 皆是如此）。`debug_node.py` 唯一的 import 對象是 `debug_agent`（一個獨立套件），不該有任何一處是「`graph/nodes/X.py` import `graph/nodes/Y.py`」。

**修正：`_python_service` 這個單例本身不該歸屬於 `implement_node.py` 這個「node 檔案」，應該下沉到 `python_service/` 套件裡**——`python_service/`（09b 四章既有套件）本來就是「容器化服務啟動器」的正確歸屬地，`implement_node.py` 只是這個套件的其中一個使用端（⑤ 需要啟動、等待、關閉它），不該同時也是它的**擁有者**。新增 `python_service/manager.py`：

```python
# python_service/manager.py（新增檔案）
"""容器化 Python 服務的模組層級單例與生命週期管理，供 implement_node.py
（⑤，啟動／等待／關閉）與 refactor_harness/langgraph_nodes/test_nodes.py
（⑥，健康檢查失敗時讀取診斷）共用——這是兩個不同 node 都需要碰觸的
共用基礎設施，因此獨立成套件層級的模組，不歸屬於任一個 node 檔案，
比照 refactor_harness/／translator_cli/ 的既有慣例（見 10a 八章）。
"""
from python_service.process import PythonServiceContainer

_python_service: PythonServiceContainer | None = None


async def ensure_started(python_project_path: str, python_base_url: str) -> None:
    global _python_service
    if _python_service is not None:
        return
    import asyncio
    service = PythonServiceContainer(
        python_project_path=python_project_path,
        base_url=python_base_url,
        database_url=__import__("os").environ["DATABASE_URL"],
    )
    await asyncio.to_thread(service.start)
    _python_service = service


async def stop() -> None:
    global _python_service
    if _python_service is None:
        return
    import asyncio
    await asyncio.to_thread(_python_service.stop)
    _python_service = None


def get_diagnostics() -> str | None:
    return _python_service.diagnostics if _python_service is not None else None
```

`implement_node.py`（09b 二章既有的 `_ensure_python_service_started()`／`stop_python_service()`）改成直接呼叫 `python_service.manager.ensure_started(...)`／`python_service.manager.stop()`，不再自己持有 `_python_service` 這個變數；`main.py` 既有的 `implement_node.stop_python_service()` 呼叫點同步改成 `python_service.manager.stop()`（完整 diff 見 10b）。

**⑥ 發現服務不可達時，當下就讀取診斷、寫進 State，⑦ 只從 State 讀，不呼叫任何函式**：`run_postman_tests()`（`refactor_harness/langgraph_nodes/test_nodes.py`，二章既有健康檢查邏輯）在 `_is_service_reachable()` 判定不可達的那一刻，正是診斷資料最新鮮的時間點——這裡直接呼叫 `python_service.manager.get_diagnostics()`，把結果寫進新的 `RefactorState` 欄位 `service_diagnostics: str | None`，跟既有的 `reason="service_unreachable"` 標記一起回傳。`debug_agent/analysis.py` 因此單純讀 `state.get("service_diagnostics")`，完全不需要 import 任何一個 node 檔案，也不需要 import `python_service.manager` 本身——State 已經是完整的資料來源，符合 LangGraph 以 State 為單一真相來源的既有設計精神。

`refactor_harness/langgraph_nodes/test_nodes.py` 因此需要新增一行 `from python_service.manager import get_diagnostics`——這不是「node import node」，是「⑥ 的 State 整合層（`langgraph_nodes/` 本來就是 Harness 與 LangGraph State 之間的既有整合層，見 02b）import 一個雙方共用的基礎設施套件」，跟 `implement_node.py` import `python_service.process` 是同一種、本專案從 09b 就有的既有依賴方向，不是新的耦合模式。

### 新增：`ModuleScheduler.force_reschedule()`

三章 3.1 已經明講「一個 module 可能被 ⑤ 標成 `verified`（readonly 通過），但 `test_results` 因為 mutation 或跨 module regression 抓到真正的問題」——這正是本文件從六章開始反覆處理的核心場景。這代表四章的正常 `root_cause` 分析路徑，只要它鎖定的 module 剛好是這種「⑤ 局部驗證誤判為 `verified`、但 ⑥ 全量驗證抓到真正問題」的情況，產生的 `pending_fixed_bodies` 會指向一個 `module_status=="verified"` 的 module——但 `ModuleScheduler.get_ready_tasks()`（01 六章既有邏輯）只回傳 `"pending"`／`"in_progress"` 的 module 底下的 task，`"verified"` 的 module 永遠不會再被排進就緒佇列，`fixed_body` 因此會被平白浪費掉：下一輪 `implement()` 對這個 module 完全不會有任何動作，直到重試預算耗盡才停下來。

`ModuleScheduler` 目前只有 `flag_for_reverify()`（01 六章既有方法）能讓一個 `"verified"` 的 module 重新進入排程考量，但它打回的 `"needs_reverify"` 狀態本身**不會**讓 task 重新被 `get_ready_tasks()` 排入（`get_ready_tasks()` 只認 `"pending"`／`"in_progress"`，`"needs_reverify"` 是給「驗證」用的狀態，不是給「排程 task」用的狀態）。這裡需要更直接的「把這個 module 重新打回 `pending`」——`ModuleScheduler` 目前沒有這個方法，這是對 `graph/scheduler.py`（01 六章既有、已通過驗證的程式碼）新增的一個方法，不是修改既有方法的行為。

**只改 `module_status` 不夠，還必須清掉被指名 task 的完成紀錄**：`get_ready_tasks()` 對一個 task 是否就緒，同時檢查 `module_status` 與 `task_id in self.task_done`／`task_failed` 兩道獨立的關卡（見 `get_ready_tasks()` 本文）——只把 `module_status` 打回 `"pending"`，這個 module 底下的 task 若已經在 `task_done`（`"verified"` module 的定義就是底下所有 task 都在 `task_done`）仍然會被 `get_ready_tasks()` 跳過，`fixed_body` 一樣不會有機會生效。因此 `force_reschedule()` 需要同時接收「要重開的 task_id」，只清掉這些被明確指名的 task，同一 module 底下其他已完成的 task 維持完成狀態，不被誤重新排程：

```python
# graph/scheduler.py（新增方法，不改動既有方法）
def force_reschedule(self, module: str, task_ids: set[str]):
    """由 ⑦ Debug Agent 產生的 pending_fixed_bodies 指向一個
    "verified"／"failed" module 底下的 task 時呼叫（見 10a 八章）：把該
    module 打回 "pending"，並把 task_ids 從 task_done／task_failed
    移除，讓 get_ready_tasks() 重新排到它們——這跟 flag_for_reverify()
    的 needs_reverify 不同，needs_reverify 只觸發重新跑驗證，不會讓
    task 重新進 get_ready_tasks()；這裡要解決的問題是 task 本身要重新
    被排入佇列、直接套用新的 fixed_body。
    """
    self.module_status[module] = "pending"
    self.task_done -= task_ids
    self.task_failed -= task_ids
```

`implement_node.run()`（09b 二章既有邏輯）需要在 `is_first_entry` 之後、建構完 `scheduler` 之後，新增一小段：讀取 `state.get("pending_fixed_bodies", {})` 裡每個 task_id 對應的 module（透過 `task_list` 反查），依 module 分組後對每組呼叫 `scheduler.force_reschedule(module, task_ids)`——緊接在既有的「100% 由 `scaffold_gap_task_ids` 覆蓋的 module」那段掃描之後：

```python
tasks_to_reopen_by_module: dict[str, set[str]] = {}
for task_id in state.get("pending_fixed_bodies", {}):
    task = next((t for t in state["task_list"] if t["id"] == task_id), None)
    if task is not None:
        tasks_to_reopen_by_module.setdefault(task["module"], set()).add(task_id)
for module, task_ids in tasks_to_reopen_by_module.items():
    scheduler.force_reschedule(module, task_ids)
```

這一段對所有 `pending_fixed_bodies` 一視同仁，不區分是哪一種 `origin` 產生的建議。對已經是 `"pending"`／`"in_progress"`／`"failed"` 狀態的 module，這個呼叫只是把狀態原樣覆寫（無害）；只有原本是 `"verified"` 的 module，這個呼叫才會真正改變行為——但這正是需要被修正的那個缺口。

### phase 2：`file_fixes`——`fixed_body` 碰不到的範圍

**動機，來自真實案例**：`fixed_body` 只能取代 `fill_function()` 用 AST 定位到的**函式本體**，碰不到函式簽名（參數名稱、型別標註）或模組層級的敘述（import、常數宣告）——第一輪真實測試的根因（`CollectionUtil.find_distinct_field` 的參數名稱 `list` 遮蔽 builtin）剛好就需要改**簽名**（把參數名稱從 `list` 改成 `items`），純函式本體替換從機制上就無法觸及這一行,只能在本體內部繞道（例如把 `for item in list:` 改成別的寫法但保留參數名不動,或者像 phase 1 真實測試裡 ⑤ 本地模型那樣改了一半)。第二輪真實測試（真實 Claude API，隔離 clone,詳見下方）證實 `file_fixes` 能真正解決這個問題。

**設計**：`task_fixes[i]` 新增 `file_fixes: list[FileFix]`，`fixed_body` 允許為 `None`（見四章 4.2 schema）：

```python
class FileFix(TypedDict):
    task_id: str
    target_file: str
    old_snippet: str
    new_snippet: str
```

套用機制不透過 `fill_function()` 的 AST 函式定位（那個機制的前提是「先找到一個函式節點」，簽名／模組層級敘述不屬於任何函式節點內部，AST 定位從一開始就用不上），改用**精確字串搜尋替換**：`old_snippet` 必須在 `target_file` 目前內容裡逐字出現剛好一次（連同縮排、換行都要一致），成功找到唯一一次時原地替換成 `new_snippet`；找不到或出現不只一次都視為失敗——安全起見不做模糊匹配、不猜測該替換哪一處。寫入前後的既有關卡（clean tree 檢查、格式化、diff／commit、commit 失敗時的復原）比照 `fill_function()`，見 `translator_cli/client.py::apply_file_fix()`（10b 五章）。

**`file_fixes` 不透過排程器**：跟 `fixed_body` 不同，`file_fixes` 不對應任何要重新生成的函式本體，不需要 `get_ready_tasks()` 排到任何 task，也不需要 `force_reschedule()`——`implement_node.run()` 在 `is_first_entry` 之後、排程器的 while 迴圈開始之前，直接逐筆呼叫 `apply_file_fix()`；套用失敗的記一筆 `TaskFailure(reason="file_fix_failed")`（新增到既有 `reason` 列舉）。套用成功後（只要有任何一筆成功），顯式呼叫一次 `_wait_for_service_reload()`，不依賴 while 迴圈裡「有其他 task 在跑」才會觸發的既有 reload-wait 時機——若這一輪剛好沒有其他 task 可以當「順風車」，`file_fix` 寫入磁碟後就不會有任何東西觸發 reload-wait，⑥ 下一輪的全量驗證可能讀到還沒 reload 的舊服務狀態。至於「⑥ 下一輪重新驗證這個 module 時會不會發現還沒修好」完全交給 ⑥ 的全量驗證本身判斷——不需要 `scheduler.module_status` 特地打回 `pending`：`module_status` 只是 `implement_node.run()` 這一次呼叫內部的排程簿記，`debug ↔ implement` 迴圈實際跨輪依賴的是 `test_results`（⑥ 產生）與 `partial_reports`，兩者都不依賴 `module_status` 在 `run()` 呼叫之間存活。

**`fixed_body` 與 `file_fixes` 同時作用在同一段程式碼，不是無條件安全的——這條結論被 2026-08 的真實案例推翻，見 `docs/09b_bug_trace.md #52`**：早期真實測試裡 Claude 對同一個 task（`task_svc1`）同時給了兩者，`file_fixes` 整個取代了 `find_distinct_field` 的函式定義（含簽名），`fixed_body` 又給了本體的修正版本，兩者描述的最終內容剛好一致，第二次套用（`fill_function()` 的 AST body-splice）寫入的內容跟磁碟上已經 commit 的版本完全相同，`diff` 為空，觸發既有的冪等短路——**當時把這個結果解讀成「機制本身安全」，其實只是「這次剛好兩者算出一致結果」的巧合，不是結構性保證**。`task_066`（`file_router.py::image`）真實撞到兩者**不一致**的情況：`file_fixes` 已經把整個函式（含新簽名 `async def image(...)`）完整替換掉，`fixed_body` 卻又違反自己的格式契約（見四章 4.2「不要包含 `def` 簽名行」），塞了一份包含裝飾器＋`async def` 簽名行的內容——第二次套用（AST body-splice）把這段內容當成陳述式插進「`file_fixes` 剛換好的新函式」body 裡，寫出巢狀死程式碼，不是冪等短路，也不是任何一種既有的失敗檢測會攔到的情況（語法合法，只是語意錯）。**修正後的規則（`DEBUG_SYSTEM_PROMPT` 已更新，見四章）**：`file_fixes` 若已經完整替換掉這個函式的簽名＋本體，`fixed_body` 必須設為 `null`，不要重複；只有當函式簽名不變、只有內部陳述式需要修正時，才單獨用 `fixed_body`。下游同時新增了一道防線（`translator_cli/python_adapter.py::extract_body_statements()` 的巢狀同名函式檢查，見 07a 六章步驟 6b）：即使 prompt 這邊沒擋住，這段檢查也會擋下巢狀污染、記一筆 `fill_failed`，不會靜默寫入。

**多輪真實環境驗證確認的設計結論**：`file_fixes` 確實能修好 `fixed_body` 結構上做不到的事（函式簽名改動）；prompt「根因是檔案層級問題時必須用 `file_fixes`，不能在 `fixed_body` 裡繞道」這條規則有效抑制了猜測寫死值的傾向；`origin="module_mismatch"` 對 `task_list` 未涵蓋的模組正確攔下，不浪費 Claude 呼叫。「⑦ 看不到某個檔案」在實測中幾乎都是輸入資料本身缺漏（如某個 task 該帶的 `target_files` 沒帶到，屬於 06a／[P] 的既有規則本來就該涵蓋），不是 ⑦ 機制本身的缺陷；真正因為程式碼從未被生成、補再多 context 也沒用的情況，屬於 ④／⑤ 階段的既有缺口，不在本文件職責範圍。

---

## 九、模組結構規劃

`debug_agent/` 獨立套件，比照 `plan_agent/`／`design_agent/` 的既有慣例（有實質領域邏輯需要封裝：module 分組與值不值得叫 LLM 的判斷、prompt 設計、task_id 反查驗證、`give_up_early` 判斷）：

```
debug_agent/
├── __init__.py       # 對外唯一入口：run_debug_analysis(state) -> dict
├── triage.py          # 三章：ModuleFailureContext 正規化與分類（機械）
├── llm.py              # 模型選擇／max_tokens，比照既有 Agent 慣例——**只決定
                        # 用哪個模型／token 上限，實際呼叫一律經
                        # common.llm_client.call_claude_for_json()（00 六章
                        # 既有硬性規定），不自行初始化 Anthropic client，
                        # 理由同 03c 一章對 spec_collection_agent/llm.py
                        # 的既有說明：這樣用量與 prompt/response 才會被統一
                        # 記進 llm_traces.db（見 11a 七、八章 record_llm_call()），
                        # 不會因為某個 Agent 自己另開一條呼叫路徑而漏記**
├── prompts.py           # 四章 system prompt + DEBUG_OUTPUT_SCHEMA
└── analysis.py            # 四、五、七章核心：逐 module 平行呼叫、
                           # task_id 反查驗證、give_up_early 判斷

graph/nodes/debug_node.py  # 薄封裝：async def run() 呼叫
                           # debug_agent.run_debug_analysis()（見十章
                           # 為什麼要包 asyncio.to_thread）；
                           # should_retry_or_give_up() 條件邊函式（七章）

graph/scheduler.py           # 新增 ModuleScheduler.force_reschedule()（八章）

python_service/manager.py    # 新增：_python_service 單例與生命週期管理，
                             # 從 implement_node.py 下沉到這裡（八章）

graph/nodes/implement_node.py  # 改呼叫 python_service.manager.ensure_started()／
                               # .stop()；force_reschedule() 呼叫點（八章）

refactor_harness/langgraph_nodes/test_nodes.py  # 健康檢查失敗時讀取
                                                 # python_service.manager
                                                 # .get_diagnostics()，寫進
                                                 # state["service_diagnostics"]
                                                 # （二章、八章）
```

---

## 十、與 LangGraph 整合

```
run_tests → should_debug_or_done()（02b，七章已修正：retry_count 上限
             檢查對所有分支統一套用，不再分 failed_modules 是否為空）
      ├── done       → END
      ├── give_up    → give_up node（retry_count 已達上限）
      └── debug      → debug node
                            ↓
                    should_retry_or_give_up()（七章新增，只判斷 give_up_early：
                         所有 root_cause module 都成功分析且判定不可修，
                         或這一輪機械判斷已經沒有任何可分析的 module，見七章）
                         ├── give_up
                         └── implement
```

`debug_node.run()` 是 `async def`（延續現有 stub 的簽名）。但內部真正的工作——ThreadPoolExecutor 平行呼叫 Claude API（04a/05a 既有模式，同步 API）、讀取 `python_project_path` 下的原始碼（同步檔案 I/O）——全部是阻塞呼叫。比照 09a 三章「必須包 `asyncio.to_thread()`」那個關鍵洞察（LangGraph 對 `async def` node **不會**自動丟進執行緒池，只有普通 `def` node 才會）：`debug_node.run()` 把整個同步分析流程包一層 `asyncio.to_thread()`，而不是把 `debug_agent/analysis.py` 本身寫成 `async def`——這樣 `debug_agent/` 套件可以維持跟 `plan_agent/`／`design_agent/` 一致的同步 `ThreadPoolExecutor` 寫法，不需要為了配合 LangGraph 的 async 節點就把整個套件改寫成 asyncio 風格，兩者的關注點（套件內部怎麼平行、node 怎麼不卡住事件迴圈）清楚分開。

```python
# graph/nodes/debug_node.py
import asyncio
from graph.state import RefactorState
from debug_agent import run_debug_analysis


async def run(state: RefactorState) -> RefactorState:
    return await asyncio.to_thread(run_debug_analysis, state)


def should_retry_or_give_up(state: RefactorState) -> str:
    # retry_count 上限檢查已經前移到 should_debug_or_done()（02b，見
    # 七章「路由方式」），這裡只需要判斷 give_up_early。
    return "give_up" if state.get("give_up_early") else "implement"
```

`run_debug_analysis()`（`debug_agent/__init__.py`）本身是同步函式，內部負責：`retry_count` 遞增（六章，無條件遞增，非既有 stub 那套邏輯）＋三章正規化＋四章 LLM 分析＋七章 `give_up_early` 判斷＋組裝完整回傳字典。

**`give_up_node.py` 依抵達路徑分三種訊息**：01 五章既有的「④ 骨架生成失敗」／「超過重試次數」兩種之外，`debug → give_up` 這條新邊（`give_up_early=True`）需要第三種——這條路徑不經過 `should_debug_or_done()`，`state["retry_count"]` 是正常遞增的真實次數，不是被強制設成上限，沿用「超過重試次數」的訊息會讓人工誤以為已經試滿所有次數，實際上是 ⑦ 主動判斷這一輪已無可修才提早放棄。判斷順序：`scaffold_done is False` → `give_up_early` → 其餘（retry_count 用盡）。

**`retry_count` 用盡那個分支本身，還要再分辨「Claude API 打不通」跟「⑦ 判斷邏輯本身有問題」**：這兩種情況目前印出的訊息完全一樣——連續 `MAX_RETRY` 輪（目前 `MAX_RETRY = 1`）都沒能真的解決問題，可能是因為每一輪 Claude API 呼叫都失敗（`analyzed_results` 全是 `None`，見四章 4.4），也可能是 API 正常、但 ⑦ 給的 `fixed_body` 一直沒用（見八章「⑦ 直接產生修正後程式碼」——`fixed_body` 本身仍要通過語法驗證等既有關卡才會真的寫入，語法合法但邏輯依然不對的情況一樣可能發生）。人工光看 `retry_count` 用盡的訊息分辨不出來，得自己去翻 `debug_rounds` 或用 `llmlog` 查對應 trace。`run_debug_analysis()` 已經算出 `analyzed_results`（哪些 `root_cause` module 這一輪完全沒能成功呼叫到 Claude），這裡只是把這個既有資料原樣往外傳，不是新的判斷邏輯：新增 `RefactorState.unanalyzed_root_cause_modules: list[str]`（這一輪快照，不掛 reducer），`give_up_node.py` 在 `retry_count` 用盡的分支若看到這個欄位非空，額外印一行提示「這幾個 module 這一輪 Claude API 呼叫（含重試）仍然失敗，建議先用 `llmlog` 查對應 trace，不要急著懷疑 ⑦ 的判斷邏輯」。

**整條 graph run 結束後的制式報告**：`give_up_node.py`／`END` 只是 graph 執行流程的收尾，`debug_rounds`／`task_failures` 這些逐輪累積的完整歷史沒有 checkpointer 就留不住，process 一結束就消失。`main.py` 因此在 `graph.astream()` 跑完後呼叫 `common/run_report.py::write_run_report()`（制式 JSON）／`write_human_readable_report()`（人類可讀 Markdown），依日期分資料夾寫進 `logs/reports/{YYYY-MM-DD}/{run_id}.{json,md}`，回答「這一趟翻譯了多少函式、⑦ 修了多少、還剩什麼沒解決」。完整設計與程式碼見 10b 八章。

---

## 十一、錯誤處理範圍

比照 04a／05a／06a／07a／08a／09a 的既有邊界：`retry_count` 迴圈只包住 `implement → run_tests → debug`，本文件不新增額外的重試機制。

- 讀取 `related_files` 原始碼失敗（檔案不存在等）：記警告，略過該檔案內容，不中斷整個 module 的分析——理由同 07a「`context_files` 讀取容錯」既有機制
- 單一 module 的 Claude API 呼叫失敗：見 4.4，重試一次，仍失敗則這個 module 視為「未分析」，不計入 `give_up_early` 判斷的不可修證據（七章）
- `task_fixes[*].task_id` 驗證失敗：見 4.3，捨棄該筆、記警告，不中止整個 module 分析
- `test_results.failures[*].module` 對不上 `task_list` 裡任何一個 task（三章 `module_mismatch`）：不呼叫 LLM、不嘗試用 `related_files` 反查猜測，直接記固定診斷文字，留給人工核對③／[P]輸出或 skip 呼叫鏈設定——這是刻意選擇「明確回報資料落差」而非「用不保證準確的猜測掩蓋它」
- 若某個 module 同時符合 `blocked` 與 `module_mismatch`／`scaffold_gap` 的判斷條件（理論上少見：一個被上游卡住、自己完全沒被排程過的 module，不可能同時「底下 task 100% 已知 scaffold_skipped」——因為 `task_failures` 只有真正被送進排程器判斷過的 task 才會出現在裡面，`blocked` module 的 task 從未被排程器處理過。三章固定的判斷順序（`blocked` → `module_mismatch` → `scaffold_gap` → `root_cause`）已經隱含這個優先序，不需要額外處理）
- ⑥ 對 Python 服務的健康檢查逾時（二章「⑥ 對外呼叫前必須先確認 Python 服務有回應」）：不拋例外，`run_postman_tests()` 直接回傳 `reason="service_unreachable"` 的結構化 `test_results`，交由三章的 `service_unreachable` 分支正常走完整個分析流程——這條路徑不消耗額外的重試預算判斷，一樣走 `should_debug_or_done()`（七章已修正）／`give_up_early` 機制

---

## 十二、已定案（多輪真實環境驗證後收斂的決定）

- **Java 原始碼不放進 LLM context**：多輪真實案例裡，即使根因涉及 Java／Python 語意差異（如去重是否保序），⑦ 只靠 Python 側的事實（golden 回應順序固定、目前實作用了會打亂順序的結構）就能正確診斷，不需要比對 Java 原始碼。維持四章的決定。
- **`debug_rounds` 歷史不餵回下一輪 LLM 呼叫**：機制面可行（`common/llm_trace.py::get_traces_for_task()` 查得到上一輪的 prompt/回應），但多輪真實迴圈測試中沒有觀察到「連續幾輪給出雷同或矛盾建議」的情況，加這個機制的成本目前不划算，不實作。
- **`give_up_early` 已被真實案例驗證**：多輪真實迴圈測試中，`root_cause_ctxs` 為空、以及所有 `root_cause` module 分析後一致判定不可修這兩種情況都真實觸發過，行為符合七章設計。
- **mutation／readonly 用同一套分析策略，不拆分**：多輪真實測試涵蓋大量 mutation 案例，沒有出現需要額外 prompt 引導的 mutation 特有失敗模式。
- **`_is_global_infra_file()` 的目錄判斷擴充為「或擁有模組沒有 HTTP endpoint」，`_global` 保留模組改為無條件附加**：見四章設計，已用 `common_service.py`／`exception_handlers.py` 兩個真實案例驗證問題存在，修法已回寫進 `debug_agent/triage.py`／`analysis.py` 並補齊單元測試，尚待真實環境端對端重跑確認（見 `docs/09b_bug_trace.md #49`）。
- **⑥ 健康檢查逾時的真正修法不是調大數字，是讓 `run_newman()` 本身能分辨「服務沒回應」與「工作做完但行程沒退出」**：見二章「前置修補之二」，已修正並回寫程式碼（`docs/09b_bug_trace.md #48`）。健康檢查本身的逾時預算（預設 15 秒）維持原值，因為真正的風險已經被這個修法接住，不再需要靠調大逾時數字硬頂。

## 十三、尚待確認事項

- [ ] **task_id 反查（五章）的準確度**：4.3 的驗證只能擋住「幻覈出不存在的 task_id」，擋不住「選對 task_id 但因果推理錯誤」。真實案例裡 task_id 本身從未選錯，但曾經對同一個真實壞掉的端點連續兩次給出不同診斷敘事，其中一次因果關係經查證是錯的。真實案例仍不夠多，暫不調整 prompt。
- [ ] **`batch_sibling_modules`（3.5）仍是近似值，不是精確的批次分組**：`partial_reports` 沒有記錄「哪幾個 module 屬於同一次 `_wait_for_service_reload()` 呼叫」這個精確的批次識別碼，只能用「同一輪最新 reason 仍是這個值」代理。

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
