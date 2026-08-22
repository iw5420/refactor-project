# 09b 端對端驗證：bug 追蹤表

> 09a/09b 端對端驗證過程中獵到的問題記錄。狀態欄：**1 未解決／2 待驗證／3 已解決**（已解決＝用完整重新跑過受影響流程確認過，非只靠單元測試）。詳細重現步驟與程式碼見 `09b_implement_agent_code.md` 九、十章。

---

## 缺陷總表

| # | 問題 | 檔案 | 歸屬 | 狀態 | 修正摘要 |
|---|---|---|---|---|---|
| 1 | Windows 上 `subprocess.run(["newman"])` 找不到 `.cmd` 執行檔 | `postman_runner.py` | 02a/02b | 3 已解決 | 改用 `shutil.which("newman")` 解析完整路徑 |
| 2 | `golden_writer.py` 讀錯 newman JSON report 的 header 欄位 | `golden_writer.py` | 02a/02b | 3 已解決 | 改讀正確欄位 `header` |
| 3 | `--env-var base_url=` 跟 collection 實際變數名 `baseUrl` 對不上，永遠 fallback 成預設值 | `postman_runner.py` | 02a/02b | 3 已解決 | 改用駝峰式 `baseUrl` |
| 5/16 | `resolve_body_imports()`／`_resolve_imports()` 把同檔案自己定義的 class 誤判成要匯入，造成循環 import | `translator_cli/scaffold.py` | 07a/07b | 3 已解決 | 新增 `same_file_class_names` 排除邏輯 |
| 17 | 容器內 uvicorn --reload 寫出的 `__pycache__/` 弄髒 working tree，擋下後續 task 的 precondition 檢查 | `python_service/reload_probe.py` | 09b | 3 已解決 | `.gitignore` 加 `__pycache__/` |
| 18 | `_wait_for_service_reload()` 在真實批次規模下 120 秒逾時（`batch_reload_timeout`） | `implement_node.py` | 09a | 3 已解決 | 逾時預算調到 300 秒 |
| 20 | **真實 newman 6.2.2 沒有 `response["body"]` 欄位**，內容在 `response["stream"]`（Buffer），舊寫法永遠讀到 `None`，body diff 從未真正比對過內容 | `postman_runner.py` 等三處呼叫點 | 02a/02b | 3 已解決 | 新增共用 `extract_response_body()` |
| 21 | `GoldenVerifier` 把「錄製時就判定非 JSON 而跳過」誤判成 `golden_not_found` 失敗 | `comparator.py`／`reporter.py` | 02a | 3 已解決 | 新增 `excluded_cases` 機制 |
| 31 | `design_agent/type_mapping.py` 呼叫未 import 的 `_GENERIC_RE`，帶 requestBody 的 POST/PUT 端點必 `NameError` | `design_agent/type_mapping.py` | 05a | 3 已解決 | 改用 `common/java_type_mapping.py` 共用正則 |
| 32 | 鏈式依賴偵測把 `POST /api/candidate/search` 自己的回應誤判成能餵給自己參數的來源（producer==consumer，時序上不可能），capture script 對 `null.card` 丟例外 | `chain_dependency_detect.py` | 03a | 3 已解決 | 新增 `_exclude_self_referential()`，過濾 producer_endpoint==consumer_endpoint |
| 33 | `run_newman()` 把 newman exit code 當唯一成敗判準，斷言失敗（exit≠0）就直接 `RuntimeError`，讓 tainted-folder 隔離機制失效 | `postman_runner.py` | 02a/02b | 3 已解決 | 改成優先信任已寫出的報表內容，exit code 只用 log 警告 |
| 34/36 | `run_newman()` 對半死不活的目標服務（socket 開著、app 沒載入）沒有真正的逾時上限——`subprocess.run(timeout=)` 在 Windows 上只砍得掉 `newman.cmd` wrapper，砍不到它底下真正在跑的 `node.exe`，真實卡了 24 分鐘以上 | `postman_runner.py` | 02a 原始設計沒考慮過此情境＋Windows 子行程樹陷阱 | 3 已解決 | 改用 `Popen`＋`_kill_process_tree()`（Windows `taskkill /F /T`、POSIX `killpg`）；真實對著會卡住的容器驗證過，`elapsed=15.1s` 正確逾時且無孤兒行程 |
| 35 | 每個 task 各自重試網路錯誤，沒有跨 task 視角——上游服務本身異常時，連續多個 task 各自重複燒完重試預算才發現是同一個根因 | `ollama_client.py` 等 | 07a 既有設計缺口 | 3 已解決 | 新增跨呼叫連續失敗計數器，達門檻拋 `TranslatorCliUpstreamDegradedError`，`implement_node.py` 提早停止該輪剩餘 task |
| 37 | **prompt 過大時 qwen2.5-coder:32b 高機率跑題／格式違反，且生成耗時暴增 7～20 倍**——已用 11a/11b 新增的 `record_llm_call_start()` 即時記錄機制在真實環境完整量化：09b_bug_trace 記錄的 6 支頑固函式裡，這次被排到的 5 支（`ExamService.create_random`／`get_single_exam`／`save_answer`／`search`／`search_answer`）全數重現失敗，prompt 都落在 16～17KB（同模組其他成功呼叫最大僅 10KB），單次生成耗時從正常的 40～60 秒暴增到 400～900 秒，3 次格式重試預算耗盡後常見連 HTTP 回應都等不到（`TRANSLATOR_CLI_TIMEOUT_SECONDS=300` × 3 次子重試的 906 秒硬上限）。原始回應內容顯示模型會誤解任務範圍（自己寫一篇「如何搭建 FastAPI 專案」教學文，或把 context 裡的 schema／model 定義重新排版後原樣吐回），不是單純的格式小錯 | `translator_cli/client.py`（context 組裝）／`ollama_client.py` | 07a 既有設計缺口，本次用真實環境＋新 log 機制首次量化證實 | 2 待驗證 | 新增 `python_adapter.strip_all_function_bodies()`＋`client.py::_trim_context_files_if_oversized()`：`context_files` 總量超過 `TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES`（預設 13000，取自真實資料：成功呼叫最高 12942 bytes、全失敗呼叫最低 15590 bytes）才裁減，把每個檔案的所有函式本體換成 `pass`，只留簽名／裝飾器／import／class 屬性宣告。已用當次真實失敗案例的實際 prompt 驗證：5 個 context 檔案從 15334 bytes 裁減到 9131 bytes，落回成功呼叫的量級範圍；已補單元測試與 `fill_function()` 整合測試（見 `tests/translator_cli/test_python_adapter.py::TestStripAllFunctionBodies`、`tests/translator_cli/test_client.py::TestTrimContextFilesIfOversized`）。**尚未跑過真實重跑確認能實際避免這 5 支函式失敗**，因此狀態是「待驗證」不是「已解決」 |
| 38 | `run_tests` 節點（`refactor_harness/verifier/comparator.py::verify_raw()` → `run_newman()`）對 Python 服務（Docker 容器，port 8000）逾時 180 秒直接拋出 `RuntimeError`，讓整條 pipeline 崩潰——訊息與 #34/36 描述的「socket 還開著但沒有正確回應」同一類現象，但發生在 `run_tests` 這個呼叫路徑，不是 #34/36 當初驗證過的 harness 局部驗證路徑；容器當時已經連續跑了 3+ 小時（⑤ 填空全程），是否為長時間 bind mount＋反覆 reload 的累積問題尚待查證 | `refactor_harness/verifier/comparator.py`／`postman_runner.py` | 02a／09a，#34/36 同根因的另一個發生點（待查證） | 1 未解決 | 待查證，見下方「尚未解決」 |

## 測試涵蓋度缺口（已補測試）

| # | 缺口 | 狀態 |
|---|---|---|
| 6 | `--add-host` 旗標沒測試鎖住真的出現在 `docker run` 指令裡 | 3 已解決 |
| 7 | 「容器內能連到 host DB」只手動驗證過，沒寫進自動化測試 | 3 已解決 |
| 22 | `extract_response_body()`／`excluded_cases` 原本完全沒測試 | 3 已解決 |

## `exam-platform-api`（目標專案）本身的翻譯品質問題，手動修正

不是 Orchestrator 既有程式碼的缺陷，性質上等同⑦ Debug Agent 未來要做的事：

| # | 發現 | 處理方式 |
|---|---|---|
| 9 | `find_distinct_field()` 用 `list` 當參數名遮蔽 builtin | 改參數名為 `items` |
| 23 | 用 `set()` 去重丟失 Java `.distinct()` 的 encounter order | 改用 `dict.fromkeys()` 風格有序去重 |
| 24 | `school_router.py` 查詢沒有 `ORDER BY`，順序不定 | 補 `.order_by(...)` |
| 25 | `language()` 訊息寫死英文，golden 是中文 | 改成一致訊息 |
| 26 | Pydantic v2 必填欄位遺漏，觸發 `ValidationError` | 補上欄位 |
| 27 | 引用了從未生成的 `app/core/config.py` | 手動建立 |
| 28 | schema 巢狀 `$ref` 沒被收集，跨檔案 import 缺漏 | 根因見下方「05a 型別對應修正」，已用完整重跑驗證解決 |

## 架構層修正（歸屬 ①③05a，已用完整重跑驗證有效）

- **全域例外處理（#11/#12 根因）**：Java `GlobalExceptionHandler`（`@RestControllerAdvice`）從未被①③看見，Python 端沒有對應的全域例外處理器，導致 500＋非 JSON 對不上 golden 的 200＋JSON SERVER_ERROR。修正：`parse_agent/grouping.py` 新增 `collect_global_advice_classes()` 獨立收集路徑（機械歸入保留模組 `_global`，不經 LLM 判斷模組歸屬）；`design_agent/design.py` 新增 `_design_global_advice_module()` 專屬處理路徑，只處理 catch-all `@ExceptionHandler(Exception.class)`（更細的例外類別刻意不處理，記警告略過）；`layout.py` 機械組裝 `app.add_exception_handler()` 註冊。連帶修正 `plan_agent/module_index.py::classify()`／`translator_cli/scaffold.py` 對 `_global` 保留模組的辨識缺口。
- **`ResponseEntity<T>` 型別對應（#30）**：05a 型別對應表從未處理過這個型別，機械轉換產生不合法的 `ResponseEntity[?]`。修正：決策用 `fastapi.Response` 表達動態 HTTP status，`map_java_type()`／`resolve_api_boundary_signature()` 偵測到 `ResponseEntity` 開頭時直接覆寫為 `Response`。已知限制：只處理 catch-all 轉換，不反推每個分支各自的 status/body 語意。
- **schema 遞迴收集（#28）**：`collect_named_schemas()` 原本只收頂層 schema，巢狀 `$ref` 沒收，造成部分 class 完全沒生成或跨檔案 import 缺漏。修正：新增 `_nested_schema_refs()` 遞迴收集，讓每個 module 的 schema 檔案自我完備（同名 class 可能在多個 module 重複定義，換取結構上消除缺 import 問題）。
- **`registration`/`grading` 被卡住（#29）**：排程機制全有全無放行（上游任一 module 沒 `verified` 就永久卡住）是刻意設計，真實資料證實卡住原因是上游 `exam` 模組本身還沒做完（翻譯品質問題），不是排程器的 bug，設計維持不變。新增純附加診斷欄位 `blocked_reasons`，讓卡住原因可以直接讀出，不需要人工反查依賴關係。

## 端對端驗證時間軸

1. **第一輪**：發現 `batch_reload_timeout`（#18）但未解出根因，`file`／`school` 局部驗證從未真正跑到 Newman。
2. **第二輪**：確認 #18 根因（逾時預算不夠）並修正；發現並修正 #20（`response["body"]` 不存在，影響全部 golden body 比對）；`school` 模組第一次真正跑完 Newman、比對真實 golden output，完全通過。
3. **第三輪**：驗證①③四項架構修正（`_global` 全域例外處理、`ResponseEntity` 型別對應、schema 遞迴收集、`blocked_reasons`）全數在真實環境有效；剩餘失敗全部明確歸類為翻譯品質問題，不再有「架構缺陷還是翻譯品質」的模糊地帶。
4. **2026-08-20 第四／五輪**：mutation collection 路徑第一次真正對真實 Java 服務跑通（前三輪只跑過 readonly），直接踩到並修正 #32／#33／#34／#36；`run_tests` 驗證階段從「可能無限期卡住」變成穩定在數十秒內完成。⑤ 填空翻譯反覆卡在 `exam` 模組少數幾支函式（`save_answer`／`search`／`search_answer`／`get_single_exam`／`create_random` 等），三輪 `debug→implement` 重試都沒能讓它們成功——已確認跟今天任何修正無關，是翻譯品質／模型當下狀態問題，且**實際送給模型的 prompt 內容沒有被記錄下來，事後無法回溯分析**（見下方待解決）。
5. **2026-08-21 第六輪**：11a/11b 全局 log 機制（`record_llm_call_start()`／`record_llm_call()`／`llmlog` CLI）落地後首次真實環境端對端重跑，Java 服務／Docker 容器／真實 Ollama 全部真實啟動，全程用 `llmlog` 即時追蹤。結果：確認並量化 #37（prompt 過大導致 qwen 跑題＋生成耗時暴增，見缺陷總表），⑤ 填空全部完成（77 次 Ollama 呼叫，55 成功／18 錯誤／4 逾時）；接著在 ⑥ `run_tests` 撞到 #38 崩潰，pipeline 中止，未能跑到 `debug` 迴圈。

## 發現：task 編號不是跨次重跑的穩定識別碼（會誤導問題比對）

`exam-platform-api` 沒有 checkpointer，每次重啟都會用 scaffold_agent 重新生成骨架，而**同一支函式在不同次生成裡，被分配到的 task 編號不保證相同**（用 git 歷史直接驗證：`ExamSpecification.with_status` 08-18 早上那次是 task_045，08-18 傍晚／08-20 這次都變成 task_043，三次都成功）。用「這次的 task_045 對比上次的 task_045」判斷退步是錯的比較方式，必須改用函式本體（檔案＋class＋函式名）逐一核對。

改用函式本體比對後，`exam-platform-api` 的 `exam` 模組裡真正「08-18 早上那次成功、但 08-18 傍晚（= `pre-09b-rerun-20260820` 備份基準）與 08-20 這次都持續失敗」的只有 6 支：`exam_router.py` 的 `save_answer`／`search`／`search_answer`／`get_single_exam`／`create_random`，以及 `exam_service.py` 的 `ExamService.create_random`——這批問題從備份基準那次就已經卡住，不是這次才退步。

比對這 6 支的 scaffold 發現：函式簽章三次都完全一致，依賴的輔助類別（`ExamRepository`、`CodeUtil` 等）三次也都存在，不是缺元件。但 [translator_cli/client.py:117](../translator_cli/client.py) 的 `_read_target_file()` 送給模型的 context 是**目標檔案當下的即時完整內容**，不是原始空白 scaffold——這 6 支在批次裡排序較後面，等它們被嘗試時，同檔案裡的姊妹函式大多已經被填成十幾行的真實邏輯，實際送進去的檔案內容遠比 08-18 早上那次同一支函式被嘗試時大得多（`exam_router.py` 光是 scaffold 階段就從 79 行長到 103 行，行為好的姊妹函式又填進更多程式碼）。

**2026-08-21 已用 11a/11b 新增的 log 機制證實**：這不再是假說。這 6 支裡這次被排到的 5 支，prompt 全部落在 16～17KB（同模組其他成功呼叫最大僅 10KB），單次生成耗時從正常的 40～60 秒暴增到 400～900 秒，模型的回應內容也證實會明顯跑題（見缺陷總表 #37）。「同檔案內容變大導致 context 膨脹」這個假說已經直接證實成立。

## 尚未解決

- [ ] **`SERVICE_READY_TIMEOUT_SECONDS` 正式預設值**：目前程式碼仍是 120 秒，診斷用 300 秒才穩定，需決定要不要正式調高。
- [ ] **`_design_global_advice_module()` 只認 catch-all `@ExceptionHandler(Exception.class)`**：更細的例外類別（如 `handleBaseException`）刻意不處理，日後若需要要重新評估範圍。
- [ ] **翻譯品質問題**（留給未來⑦ Debug Agent 或人工校正）：`handle_all` import 幻覺模組；`voice`／`image` 業務邏輯分支跟 Java 不完全一致；`list` 遮蔽 builtin 在新一輪生成中又出現過一次；`registration_router.py::schools` 回傳 `None` 觸發 `ResponseValidationError`。
- [ ] **#37 修正方向：large context 裁減**：目前規劃是先比對「會出問題」與「不會出問題」的 prompt 差異，找出實際是哪部分內容造成 context 膨脹（同檔案姊妹函式的已填內容？`context_files` 疊加？），再設計裁減策略（例如姊妹函式只留簽名、不帶本體），最後把裁減邏輯寫進 `translator_cli`，補上測試涵蓋門檻判斷與裁減後的行為。處理中，見 09b_implement_agent_code.md 後續章節。
- [ ] **#38 `run_tests` newman 逾時崩潰**：待查證是 #34/36 同根因在新呼叫路徑的重現，還是容器長時間運行後的新問題；晚點處理。

**已解決**：~~`translator_cli` 送給模型的 prompt 內容完全沒有被記錄~~——2026-08-21 已用 11a/11b（`common/llm_trace.py::record_llm_call_start()`／`record_llm_call()`、`llmlog` CLI）解決，見 `docs/11a_logging_architecture.md`／`docs/11b_logging_code.md`。

---

*完整重現步驟與程式碼修正內容見 `09b_implement_agent_code.md`。*
