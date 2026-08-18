# 09b 端對端驗證：bug 追蹤表

> 09a/09b 端對端驗證過程中獵到的問題記錄，含歸屬章節與狀態。狀態欄：**1 未解決／2 待驗證／3 已解決**。詳細重現方式與修正內容見 `09b_implement_agent_code.md` 九、十章。
>
> **「3 已解決」保留給已經用完整重新跑過受影響流程確認、且沒有殘留副作用的項目。** 本文件記錄三輪完整重跑：第一輪找到 `batch_reload_timeout`（#18）但未解出根因；第二輪（見「第二次完整重跑」）確認根因、修正、並**第一次讓一個模組（`school`）真正跑完 Newman、對真實 golden output 比對、完全通過**；第三輪（見「第三次完整重跑」）驗證①③新增的四項架構修正（`_global` 全域例外處理、`ResponseEntity` 型別對應、schema 遞迴收集、`blocked_reasons` 診斷）在真實環境中全數確認有效，剩餘失敗全部明確歸類為翻譯品質問題。

---

## 已修正並在第二次完整重跑中被真實觸發驗證的缺陷

| # | 問題 | 檔案 | 歸屬 | 狀態 | 備註 |
|---|---|---|---|---|---|
| 1 | `newman` 在 Windows 上無法被 `subprocess.run(["newman"], shell=False)` 找到（需要 `.cmd`） | `refactor_harness/core/postman_runner.py` | 02a/02b | 3 已解決 | 第二次完整重跑中，`school` 模組的錄製＋驗證皆真實跑過 newman 多次，全數成功 |
| 2 | `golden_writer.py` 讀錯 newman JSON report 的 header 欄位（`headers.members` vs 真實的 `header`） | `refactor_harness/recorder/golden_writer.py` | 02a/02b | 3 已解決 | 第二次完整重跑重新錄製 golden，`recorded_count` 正確 |
| 3 | `--env-var base_url=` 跟 collection 實際變數名稱 `baseUrl` 對不上 | `refactor_harness/core/postman_runner.py` | 02a/02b | 3 已解決 | `school` 模組驗證確認真的打中容器化 Python 服務並拿到正確比對結果 |
| 20 | **（最關鍵，第二次完整重跑才發現）`response["body"]` 這個欄位在真實 newman 6.2.2 根本不存在**，body 內容序列化在 `response["stream"]`（Node.js Buffer 的 JSON 表示 `{"type": "Buffer", "data": [位元組陣列]}`）。舊寫法 `response.get("body")` 永遠回傳 `None`，代表**這次端對端驗證之前錄到的每一筆 golden output body 都是空的**——不只 school 模組，全部 9 筆都是。body diff 從未真正比對過任何內容，只有 status code 比對還有意義 | `refactor_harness/core/postman_runner.py`（`golden_writer.py`／`comparator.py`／`mutation_verifier.py` 三處呼叫點） | 02a/02b | 3 已解決 | 新增共用函式 `extract_response_body()`，三處呼叫點統一改用；重新錄製 golden 後，body 內容正確含真實中文資料（如「操作成功」「馬祖語」）；`school` 模組完整比對（含 body 內容）通過 |
| 21 | `GoldenVerifier` 不區分「Recorder 錄製時就判定非 JSON 而主動跳過」與「golden 真的遺失／route_to_file_mapping 設定錯誤」，把前者也誤判成 `golden_not_found` 失敗 | `refactor_harness/verifier/comparator.py`、`refactor_harness/core/reporter.py` | 02a（設計缺口，`excluded_folders` 機制原本只涵蓋 mutation tainted folder，readonly 沒有對應機制） | 3 已解決 | 新增 `GoldenVerifier._load_skipped_case_ids()` 讀 `_metadata.json` 的 `skipped` 清單，比照既有 `excluded_folders` 模式新增 `excluded_cases` 欄位；`school` 模組的 `get_version`（text/plain，本來就該跳過）正確被排除，不計入失敗 |
| 5 | 填空階段 `resolve_body_imports()` 把「同一檔案自己定義的 class／函式」誤判成要從自己匯入 | `translator_cli/scaffold.py` | 07a/07b | 3 已解決 | 兩輪完整重跑共 132 次真實 `fill_function()` 呼叫，沒有再產生這種循環 import |
| 16 | 骨架生成階段 `_resolve_imports()`（`resolve_body_imports()` 的簽名層級版本，另一個獨立實作）有同一種自我引用誤判 | `translator_cli/scaffold.py` | 09b（第一次完整重跑執行 `generate_scaffold()` 時發現，舊 fixture 早於這個機制存在） | 3 已解決 | 新增 `same_file_class_names` 排除邏輯；兩輪 `generate_scaffold()` 重新產生的 `common_service.py` 均未再誤加，服務能在容器內成功 `import` |
| 17 | implement 期間容器透過 bind mount 常駐執行 uvicorn --reload，CPython import 時寫出的 `__pycache__/` 是未追蹤檔案，讓 working tree 變髒，導致同一輪 `implement_node.run()` 裡幾乎每個後續 task 都被 07a 九章的 precondition 檢查擋下 | `python_service/reload_probe.py` | 09b | 3 已解決 | `.gitignore` 新增 `__pycache__/`；第二次完整重跑 60-61/72 task 穩定成功完成 |
| 18 | `implement_node.run()` 對 `file`／`school` 模組局部驗證 `_wait_for_service_reload()` 逾時（`batch_reload_timeout`） | `graph/nodes/implement_node.py`（`SERVICE_READY_TIMEOUT_SECONDS` 預設值） | 09a 三章 | 3 已解決（根因已確認） | 把逾時預算從 120 秒調到 300 秒後，同一批次規模下**完全不再逾時**，`file`／`school` 都真正跑完 Newman 拿到（非逾時的）真實比對結果——證明根因是「120 秒在真實批次規模下不夠」，不是機制卡死。**待決定**：09a 三章／`implement_node.py` 的預設值是否要正式改成更大的數字，見下方「待決定」 |
| 31 | `design_agent/type_mapping.py::_simple_type_name()` 呼叫 `_GENERIC_RE.match(...)`，但這個檔案從未 import `_GENERIC_RE`（只在 `common/java_type_mapping.py` 定義），有 requestBody 且需要依型別比對 body 參數的 POST/PUT 端點一律 `NameError` | `design_agent/type_mapping.py` | 05a（既有缺陷，不是這輪四項架構修正的一部分，是這次真實重測才第一次真正跑到這條路徑而暴露） | 3 已解決 | 這次真實端對端重測①③時第一次真正跑到 `_classify_params()` 的 requestBody 型別比對分支（過去的單元測試與端對端驗證都沒有覆蓋到帶 requestBody 的 POST 端點走到這一行）而觸發。修正：`from common.java_type_mapping import _GENERIC_RE, camel_to_snake, map_java_type`，直接重用同一顆共用正則，不重新定義。新增回歸測試 `tests/design_agent/test_type_mapping.py::TestResolveApiBoundarySignatureRequestBody` |

## 測試涵蓋度缺口（已補測試）

| # | 缺口 | 狀態 | 備註 |
|---|---|---|---|
| 6 | `--add-host` 旗標只有間接的字串重寫邏輯被測，沒有測試鎖住它真的出現在 `docker run` 指令裡 | 3 已解決 | 補 `test_start_builds_docker_run_command_with_add_host_and_rewritten_database_url` |
| 7 | 「容器內真的能連到 host DB」只手動驗證過一次，沒寫進自動化測試 | 3 已解決 | 補 `test_container_can_reach_host_database_via_host_docker_internal`；且 `school` 模組驗證本身就是「真實服務透過這條路徑查詢真實資料成功」的證據（`locals`／`grades` 兩個 case 都需要真的查到 DB 資料） |
| 22 | `extract_response_body()`、`GoldenVerifier` 的 `excluded_cases` 機制是這次新增的程式碼，原本完全沒有測試 | 3 已解決 | 補 `tests/refactor_harness/test_postman_runner.py`（3 個新測試）、`tests/refactor_harness/test_comparator.py`（新檔案，comparator.py 原本完全沒有專屬測試） |

## 找到但不屬於 09a/09b/02b/07b 範圍、以手動修正方式解決的目標專案問題

這些是 `exam-platform-api`（測試用目標專案）本身的程式碼問題，不是 Orchestrator 的既有程式碼。為了驗證「至少一個模組能真正跑完」，用手動修正的方式處理（性質上等同 ⑦ Debug Agent 未來會做的事），沒有改動 Orchestrator 任何生成邏輯：

| # | 發現 | 根因 | 處理方式 |
|---|---|---|---|
| 9 | `find_distinct_field()` 用 `list` 當參數名稱，遮蔽 builtin `list()`，導致 `return list(distinct_fields)` 變成呼叫一個 list 物件而非 builtin，`TypeError: 'list' object is not callable` | 翻譯品質問題（⑤ 填空結果） | 手動改參數名為 `items` |
| 23 | `find_distinct_field()` 用 `set()` 去重，Java 原始碼是 `.stream().distinct()`（保留 encounter order），兩者語意不同，順序不保證一致，比對出 `array_order_only` | 翻譯品質問題（⑤ 填空結果與 Java 語意不完全對齊） | 手動改用 `dict.fromkeys()` 風格的有序去重 |
| 24 | `school_router.py` 的 `db.query(SchoolEntity).all()` 沒有 `ORDER BY`，PostgreSQL 回傳順序未定義 | 翻譯品質問題 | 手動加 `.order_by(SchoolEntity.id)`（02a 八章建議的「根本解」，不是 `ignore_order_at` 這種暫時緩解） |
| 25 | `language()` 寫死 `msg="Success"`（英文），golden 記錄的 Java 實際回傳是「操作成功」 | 翻譯品質問題 | 手動改成一致的中文訊息 |
| 26 | `locals()`／`grades()` 建構 `ResponseResultXxxRs` 時漏了必填的 `code`／`msg` 欄位（Pydantic v2 的 `int \| None` 沒有 `= None` 預設值時仍是必填），觸發 `ValidationError` | 翻譯品質問題（可能同時是 05a schema 生成時型別註記缺預設值的既有慣例問題，未深入判定） | 手動補上 `code=200, msg="操作成功"` |
| 27 | `language()` 引用 `from app.core.config import settings`，但 `app/core/` 從未生成過這個檔案 | 翻譯品質問題（qwen 假設存在一個未被生成的 config 模組） | 手動建立 `app/core/config.py`，值取自 Java `application-macuhau.properties` 的 `language.code`／`language.displayName` |
| 28 | `app/schemas/registration.py` 的 `ResponseResultGetAllGradeRs` 引用 `GetAllGradeRs`，但這個 class 實際定義在 `app/schemas/school.py`，`registration.py` 沒有匯入，觸發 `PydanticUserError`（"not fully defined"） | 3 已解決 | 手動補上 `from app.schemas.school import GetAllGradeRs` 只是當時的權宜之計；根本修正見下方「#28 已完成的修正」（`collect_named_schemas()` 遞迴收集）。**真實重測確認**：完全重新 scaffold＋fill 後直接讀 `registration.py`，`GetAllGradeRs`／`GetAllClassesRs`／`GetAllSchoolRs` 全部自我完備定義在檔案內，不再依賴 `school.py` 的任何 import，且服務能正常 import 啟動（沒有 `PydanticUserError`）|

## 找到但刻意不修、留給未來 ⑦ Debug Agent 的項目

| # | 發現 | 狀態 | 備註 |
|---|---|---|---|
| 8 | 舊版 `exam-platform-api` 沒有 `app/models/`，entity 從未生成 | 3 已解決（已用完整重跑取代） | 兩輪完整重跑都用現在的 pipeline（含 08a）重新生成，`app/models/exam.py`／`school.py` 已存在 |
| 12 | `test-exception` 端點刻意拋 `RuntimeError`，Python 端沒有對應的全域例外處理中介層 | 2 待驗證（**架構層根因已用真實環境確認解決**，剩下的失敗是另一個獨立的翻譯品質問題，見下方「①③ 已完成的修正」與「真實端對端測試最終結果」） | `grading` 模組唯一的 case。已讀真實 Java 原始碼確認：這不是「留給 ⑦ 才能判斷」的未知行為——`GlobalExceptionHandler.handleAll(Exception e)`（`@ExceptionHandler(Exception.class)`）會接住任何未處理例外，回傳 `ResponseResult.error(CommonErrorCode.SERVER_ERROR)`；因為這個 handler 方法回傳的是 POJO 不是 `ResponseEntity`，Spring 預設用 **200**（不是看起來合理的 500）。golden 完全對得上：`{"status_code": 200, "body": {"code": 503, "msg": "伺服器內部錯誤", "data": null}}` |
| 11 | 兩處寫死 Windows 檔案路徑（`file` 模組的 `C:/images/...`、`C:/voice/...`） | 2 待驗證（**架構層根因已用真實環境確認解決**，剩下的失敗是另一個獨立的翻譯品質問題，見下方「①③ 已完成的修正」與「真實端對端測試最終結果」） | **這裡原本的描述是錯的，是這次追查才發現的「測試過程本身的瑕疵」**：golden 不是「200＋空 body」，也不是「檔案不存在」情境——`voice_1`／`image_1` 兩個 case 的 body 其實是 `{"code": 503, "msg": "伺服器內部錯誤", "data": null}`（`data` 是 null，不是整個 body 是空的，先前記錄把兩者混為一談）。真正原因：readonly collection 這兩個 GET request 的 query 參數全部是 openapi 轉換殘留的 Postman schema 佔位字串 `<string>`（未被填入真實值），Java 端 `Paths.get("C:/voice").resolve("<string>")...` 組出含 `<`／`>` 的路徑後，`Files.isDirectory(dir)` 在 Windows 上丟出未被 `catch (IOException e)` 接住的 `InvalidPathException`（非 checked exception），一路往上被同一個 `GlobalExceptionHandler.handleAll()` 接住，回傳跟 #12 一模一樣的 200＋SERVER_ERROR 形狀——不是「檔案不存在」這條分支（那條分支明確回傳 404＋`LOCATION_NOT_EXIST`／`DATA_NOT_EXIST`，跟 golden 對不上）。跟 #12 是同一個根因 |
| 29 | `registration` 模組的 task 因為排程依賴鏈被上游模組擋住，兩輪重跑都停留在 `pass` 骨架（`pass` 本體，從未被 `fill_function()` 真正呼叫過） | 3 已解決（根因調查目標達成：確認是翻譯品質問題，排程機制維持不變的決定成立） | 已讀 `graph/scheduler.py:82-84`（`ModuleScheduler._module_deps_satisfied()`）確認機制：`all(self.module_status.get(d) == "verified" for d in deps)`，全有全無，deps 裡任一 module 停在 `"failed"`（不是 `"verified"`）就永久卡住，沒有部分放行的路徑。**真實重測用 `blocked_reasons` 直接證實**：`{"registration": ["exam"], "grading": ["exam"]}`。追查 `exam` 模組具體卡在哪：5 個 task 失敗，1 個是 ollama 連線暫時失敗（重試 2 次仍失敗），4 個是模型輸出格式違反 delimiter 契約（修正重試仍失敗）——兩者都是翻譯品質／環境暫時性問題，不是程式碼邏輯錯誤，也不是排程設計問題。**結論**：排程機制本身經 `docs/01_langgraph_architecture.md` 六章、`09a` 七章確認是刻意設計（優先讓同一 module 局部驗證通過才釋放下游、已評估過部分放行的風險並刻意不做），這次真實資料進一步證實這個保守設計是對的——`registration`／`grading` 被卡住不是排程器的問題，是上游 `exam` 真的還沒做完，維持全有全無放行邏輯不變。這批 `exam` 失敗 task 屬於 `debug → implement` 重試迴圈該接手的範疇（⑤ 填空失敗本身不需要 Debug Agent 的智能分析，重試就有機會成功），這次只跑一輪 implement 沒有走完整重試迴圈，未進一步確認重試後是否會成功 |
| 30 | Java `ResponseEntity<T>`（可在方法內顯式覆寫 HTTP status／header）完全沒有被 05a 型別對應涵蓋 | 2 待驗證（**架構層根因已用真實環境確認解決**（真實 `voice_2`／`image_2` 正確轉成 `Response` 並產出可執行程式碼），剩下的失敗是另一個獨立的翻譯品質問題，見「真實端對端測試最終結果」） | 直接讀 `common/java_type_mapping.py:107-153`（`map_java_type()`）確認：`_SIMPLE_JAVA_TYPES`／`_UNWRAP_SINGLE_PARAM`／`_FUNCTIONAL_INTERFACE_TEMPLATES` 三個對照表都沒有 `ResponseEntity`，會落入第 148 行「未知的泛型包裝類別」分支，`ResponseEntity<?>` 被機械轉成 `ResponseEntity[?]`——不是合法 Python 型別，且完全遺失「這個方法要能動態回傳 400／404／500 等不同 HTTP status」這個語意。這是比 #11／#12 更底層的根因：即使不考慮 `<string>` 佔位值觸發例外那條路徑，`FileController` 的 `GET /voice`／`GET /image` 本來就用 `ResponseEntity` 明確回傳 404（`LOCATION_NOT_EXIST`／`DATA_NOT_EXIST`）／500，但目前生成的 Python router 函式簽名固定回傳 `ResponseResultXxxRs`（純 Pydantic model），FastAPI 對這種回傳型別預設一律 200，**在目前的型別對應設計下，`file` 模組的這兩個 GET 端點無論如何都無法正確重現 Java 依情境變動 HTTP status 的行為**，不是單一 case 的翻譯品質問題。這是使用者這次追問「HTTP 200 但 code 503」時往回推導出來、之前所有章節都沒有涵蓋的既有缺口，歸屬 05a（型別對應表）+ 07a（`map_java_type()` 的呼叫端如何處理這類回傳型別），需要設計層級決定 Python 端要怎麼表達（`Response`／`JSONResponse`、路由層 `status_code` 參數、或 `raise HTTPException`），不是機械規則能補的空缺 |

## #11／#12 根因：Python 端缺少對應 Java `GlobalExceptionHandler` 的全域例外處理

這次繼續追查 `file`／`grading` 兩個模組時發現，#11 與 #12 不是兩個獨立問題，是**同一個缺口在兩個不同 controller 各自的表現**：

- Java 側 `lang-exam-api-refactor/src/main/java/com/teachLanguage/exception/GlobalExceptionHandler.java` 有 `@RestControllerAdvice`＋`@ExceptionHandler(Exception.class)` 的 `handleAll(Exception e)`，接住任何沒有被更早的 handler／controller 自己 catch 住的例外，統一回傳 `ResponseResult.error(CommonErrorCode.SERVER_ERROR)`（`code=503`，`msg="伺服器內部錯誤"`），且因為回傳型別是 POJO 不是 `ResponseEntity`，HTTP status 維持 Spring 預設的 **200**——語意上的錯誤跟 HTTP 狀態碼是脫鉤的，這點容易讓人直覺誤判（先前 #11 的記錄就是誤判的例子）。
- Python 側 `app/main.py` 目前只有 `app.include_router(...)` 五行，完全沒有註冊任何 exception handler；FastAPI 對未攔截例外的預設行為是回傳 500＋非 JSON 內容，跟 golden 的「200＋JSON SERVER_ERROR」形狀完全不同。
- 兩個 golden 案例（`grading` 的 `test-exception`、`file` 的 `voice_1`／`image_1`）的 body 都是逐位元組相同的 `{"code": 503, "msg": "伺服器內部錯誤", "data": null}`，佐證這是同一個機制，不是巧合。

**精確歸屬（使用者追問「這應該是架構 Agent 的職責」後確認）：③ 架構設計 Agent（`05a_design_agent_architecture.md`/`05b_design_agent_code.md`）**。05a 三章「全域基礎設施檔案」這個分類本來就存在（機械產生、不需 LLM，目前涵蓋 `app/core/database.py`／`app/main.py` 的 router 掛載，見 05a 三章 103-148 行）——「不屬於任何 module、全域生效」這類東西本來就該歸在這裡，`GlobalExceptionHandler.java` 性質上跟這兩個既有案例是同一類，但目前這個分類完全沒有涵蓋這一種。

**前置依賴：① 解析 Agent 也要先補一塊，③ 才看得到這個檔案**：③ 的輸入是 `module_list.java_files`（① 按 module 分組後的產物，見 05a 二章），`GlobalExceptionHandler.java` 因為 `parse_agent/call_graph.py:21` 的 `_STEREOTYPES`（`{"RestController", "Controller", "Service", "Component", "Repository"}`）不含 `RestControllerAdvice`，且 `parse_agent/grouping.py` 只沿 Controller 的欄位依賴閉包收類別（`@RestControllerAdvice` 是 Spring component-scan 自動生效，不會被任何 Controller 用 `@Autowired` 欄位注入），從未被分進任何 module 的 `java_files`——③ 現在連這個檔案存在都看不到。要補 05a 三章的規則之前，得先讓 ① 把這類「全域生效、不屬於任何 module」的 Java 檔案傳遞出來。

### ①③ 已完成的修正

**①**：`parse_agent/grouping.py` 新增 `collect_global_advice_classes()`：完全獨立於 `controller_dependency_closure()` 的第二收集路徑，直接掃 `project.classes` 比對 `@RestControllerAdvice`／`@ControllerAdvice`，不透過 BFS（原本 `GlobalExceptionHandler` 這類 class 永遠進不了任何 Controller 的依賴閉包，見上方根因說明）。`parse_agent/summarize.py::run_map_reduce()` 送這批類別的方法摘要進 Map（沿用既有重試機制），但**不經過 Reduce 的模組歸屬 LLM 判斷**——機械組成一筆保留模組 `"_global"`（`_assemble_global_advice_draft()`），附加進 `module_list`。已用真實 `../lang-exam-api-refactor` 驗證：`module_list` 現在包含 `_global` 模組、`java_files=["src/main/java/com/teachLanguage/exception/GlobalExceptionHandler.java"]`、`handleAll` 方法摘要正確產出。單元測試見 `tests/parse_agent/test_grouping.py`。

**③**：`design_agent/design.py` 對 `module["module"] == "_global"` 新增專屬處理路徑（`_design_global_advice_module()`），完全繞過一般模組的 `_build_method_contexts()`／`_call_design_llm()`（這批方法不屬於 routers/services/repositories 任何一層的既有分層慣例，機械決策即可，不需要 LLM）。新增 `_exception_handler_targets()`：重新掃該模組的 `java_files`，逐 method 抽取 `@ExceptionHandler(X.class)` 的目標例外類別名稱（javalang 把 `X.class` 解析成 `ClassReference(type=ReferenceType(name="X"))`）。**範圍刻意收斂**：只有 `target == "Exception"`（全域 catch-all）才產生 `InterfaceSpec`（固定輸出 `app/core/exception_handlers.py`、`class_name=None`、簽名比照 FastAPI `@app.exception_handler` 慣例 `(request: Request, exc: Exception) -> Response`）；真實案例裡同時存在的 `handleBaseException(BaseException e)`（`@ExceptionHandler(BaseException.class)`）不在這次範圍內，記警告略過，不產生 `InterfaceSpec`，不會被 [P] 排進 task list，是刻意接受的限制。`design_agent/layout.py::render_main_py()` 新增組裝邏輯：偵測到 `app/core/exception_handlers.py` 的 interface 時，機械產生 `from app.core.exception_handlers import {fn}` 與 `app.add_exception_handler(Exception, {fn})`。`translator_cli/scaffold.py` 已知關鍵字表新增 `Response` → `from fastapi import Response`（`_design_global_advice_module()`／B2 的 `ResponseEntity` 對應共用同一個 return_type 字面值）。單元測試見 `tests/design_agent/test_global_advice_module.py`。

**函式本體不在③機械產生**——`handleAll` 這個 InterfaceSpec 走既有 [P]→⑤ 的 task pipeline，由 qwen 翻譯 Java 原始邏輯，跟一般 service/repository 方法完全相同的流程，不需要③新增第四類 LLM 問題。

**真實端對端測試進度**：①③ 已對真實 `../lang-exam-api-refactor` 跑過，確認 `module_list` 含 `_global`（`java_files=["...GlobalExceptionHandler.java"]`），③ 正確產出 `app/core/exception_handlers.py::handle_all`（`params=[request: Request, exc: Exception]`、`return_type=Response`、`http_method=None`），且**同一批 71 個 InterfaceSpec 裡，`return_type=="Response"` 的另外兩筆是真實的 `file_router.py::voice_2`／`image_2`**——證實 B2（ResponseEntity → Response）在真實資料上也正確觸發，不只是我自己構造的測試案例。

**額外發現並修正**：[P] Plan Agent 的 `plan_agent/module_index.py::classify()` 只認得 `{module}_{layer}.py` 三種固定後綴，`app/core/exception_handlers.py` 不符合這個命名慣例，第一次跑到真實 `_global` 資料時直接 `PlanAgentModuleLookupError` 中止。這是 B1 改動的直接連帶後果（③ 新增了一種前所未有的 file_path 形狀，[P] 沒有跟著更新），不是獨立缺陷——修正：`classify()` 在一般規則之前新增 `app/core/exception_handlers.py` → `("_global", "routers")` 的特殊處理（`layer` 選 `"routers"` 純粹因為語意最接近，`_global` 目前只有一個 task、無 intra-module depends_on，選哪個 layer 對防環規則排序沒有實質差異）。單元測試見 `tests/plan_agent/test_module_index.py::test_classify_exception_handlers_file_maps_to_global_module`。

**追問「HTTP 200 但 code 503 是否代表更早的章節有被忽略的錯誤」後，往下多挖一層找到 #30**：這個 200／503 脫鉤的現象本身不是 bug，是這個 Java 專案全專案一致的既有慣例（`ResponseResult` 直接回傳一律 200，錯誤語意都放在 body 的 `code` 欄位，不靠 HTTP status 表達；`FileController` 的 GET 端點是少數改用 `ResponseEntity` 顯式設定 400/404/500 的例外）。真正被忽略的是 05a 型別對應表**從來沒有處理過 `ResponseEntity<T>` 這個型別**，見下方 #30——同樣歸屬③。

### #30 已完成的修正

決策：Python 端用 `fastapi.Response` 表達「動態控制 HTTP status」，函式本體自行組裝 `JSONResponse(content=..., status_code=...)`，不嘗試在型別系統層面窮舉每個 status 各自的 body schema（openapi_spec 本來就只能表達一個代表性 status，這個資訊損失是刻意接受的，具體要回什麼交給⑤翻譯 Java 原始邏輯決定）。

`common/java_type_mapping.py::map_java_type()` 新增分支：`outer == "ResponseEntity"` 時直接回傳 `"Response"`，丟棄內層泛型參數，不落入「未知的泛型包裝類別」fallback（原本會產生不合法的 `ResponseEntity[?]`）。`design_agent/type_mapping.py` 新增 `is_response_entity_return_type()`，`resolve_api_boundary_signature()` 偵測到 Java 原始簽名以 `ResponseEntity` 開頭時，直接覆寫 `return_type="Response"`、不查 openapi 的 response schema（openapi 只能表達一個代表性 status，對這種方法沒有代表性）。`design_agent/design.py::_build_method_contexts()` 對應跳過 `collect_named_schemas()`，不為這類方法產生任何多餘的 Pydantic class。`translator_cli/scaffold.py` 已知關鍵字表新增 `Response` → `from fastapi import Response`。單元測試見 `tests/design_agent/test_type_mapping.py`（`TestResponseEntity`／`TestIsResponseEntityReturnType`／`TestResolveApiBoundarySignatureResponseEntity`）、`tests/design_agent/test_design.py`（端對端案例，比照真實 `FileController.voice` 案例）、`tests/translator_cli/test_scaffold.py`。

**已知限制（記錄不解決）**：只處理 catch-all `ResponseEntity`→`Response` 轉換，不嘗試從 Java 原始碼反推每個分支各自的 status/body 語意——這部分翻譯品質依賴⑤，是預期中留給未來人工或 Debug Agent 校正的範疇。

## #28 已完成的修正

**根因確認**：`design_agent/type_mapping.py::collect_named_schemas()` 只收集 requestBody + 第一個 2xx response 兩個**頂層**具名 schema，不遞迴走訪欄位內的巢狀 `$ref`。`GetAllGradeRs` 之所以曾經存在於 `school.py`，是因為它「剛好」也是 `school` 模組某個端點的頂層 response schema——若它只曾經作為某個 schema 的巢狀欄位（從未當過任何端點的頂層 schema），舊邏輯下這個 class 會完全不會被任何 module 渲染出來（比缺 import 更嚴重的「完全沒生成」）。

**決策（採簡化方案，非跨模組全域註冊表）**：讓每個 module 的 schema 檔案**自我完備**（self-contained）——`collect_named_schemas()` 新增 `_nested_schema_refs()`，遞迴走訪已收集 schema 的 `properties`，把巢狀 `$ref`（含陣列包裝）指向的具名 schema 也一併收進來，一路收到底（`visited` 集合防止 `User`↔`Order` 這類互相引用造成無窮遞迴）。**不建立跨模組的 schema 擁有權登記表**——同一個具名 schema 可能在多個 module 各自的 `schemas/{module}.py` 都渲染一份同名 class，因為 Pydantic model 只在各自檔案內部使用，重複定義不影響正確性，只是多一點產出檔案體積，換取結構上直接消除「缺 import」這整類 bug，不需要額外的跨波次資料流。

單元測試見 `tests/design_agent/test_type_mapping.py`（`TestCollectNamedSchemasRecursion`，涵蓋巢狀 `$ref`、陣列包裝巢狀 `$ref`、互相引用防無窮迴圈、同一 schema 被多欄位引用不重複收集、requestBody 與 response 各自遞迴、無巢狀時行為不變六種情境）。

## #29 根因查證：輕量重跑①＋比對既有 commit 記錄（尚未達到最終確認）

**目的**：#29 先前的「合理推測是 exam」是未經驗證的猜測，不是根因；使用者要求先查證、不要用猜測當基礎去改任何排程設計。這裡先做成本最低的查證（不需要 Docker／DB／Newman），把猜測換成有依據的證據，若仍不足以完全確認，才等後面架構修正完成後的真實重測。

**步驟 1：輕量重跑①，取得目前版本 `registration` 的真實 `depends_on`**——對真實 `../lang-exam-api-refactor` 只重跑①解析 Agent（純 Claude API 呼叫），確認 `registration.depends_on = ["common", "exam"]`。這是先前完全未知、只能猜測的資訊，現在有實際輸出佐證：`registration` 確實直接依賴 `exam`，不是憑空猜的。

**步驟 2：比對 `../exam-platform-api` 既有 commit 記錄，找失敗 task 的痕跡**——`fill_function()` 只在成功時才 commit（07a 五章「呼叫失敗時不寫入任何內容」），因此 `task_{:03d}` 編號序列中的缺口，就是失敗、從未成功寫入的 task。實際檢查：

```
git log --oneline --all | grep -oE 'task_[0-9]+' | sed 's/task_//' | sort -nu
```

範圍 0-64（共 65 個編號），實際存在 61 筆，缺 `task_028`／`task_029`／`task_030`／`task_060` 四筆。`task_027`（`school_router.py` 的 `schools`）與 `task_031`（`exam_repository.py` 的 `AnswerRepository.count_distinct_random_id_by_kind`）之間的 `028/029/030` 三個編號恰好夾在一整批 `exam_repository.py` 相關 task（`031`／`033`／`035`／`036`／`038`／`040`）中間，強烈指向這三筆缺失 task 同樣屬於 `exam` 模組的 repository／service 層（不是巧合的隨機分佈）。`task_060` 位於 `task_059`（`exam_router.py` 的 `search`）與 `task_061`（`file_router.py` 的 `image`）之間，同樣可能是 `exam` 模組的收尾 task。

**結論（誠實記錄目前確認的程度）**：步驟 1／2 把「合理推測是 exam」提升為「有具體依賴關係＋commit 缺口證據支持 exam 模組」，但**還不是最終確認**——commit 缺口只能證明「這些 task 沒有成功 commit」，不能證明失敗原因（可能是翻譯品質問題，理論上也可能是其他原因，如 harness 局部驗證逾時導致整個 module 被標記失敗而部分 task 從未真正送進 `fill_function()`），也無法排除 `common` 模組同時或另外造成阻塞。**最終確認留給下方項目 D 新增的 `blocked_reasons` 診斷欄位，在架構修正（項目 A/B）完成後的真實完整重測中直接讀出**，不在這裡憑 commit 缺口的間接證據就下定論或修改任何排程邏輯。

### 項目 D 已完成：`blocked_reasons` 純附加診斷欄位

`graph/state.py` 新增 `blocked_reasons: dict[str, list[str]]`；`graph/nodes/implement_node.py::run()` 收尾計算 `blocked_modules` 的同時，一併對每個 blocked module 算出它 `depends_on` 裡狀態還不是 `"verified"` 的直接上游 module 名稱清單。**沒有修改 `ModuleScheduler`／`_module_deps_satisfied()` 任何放行邏輯**——這只是把既有資料（`scheduler.modules[m]["depends_on"]`、`scheduler.module_status`）以更直接的形式暴露出來，不改變任何排程行為。單元測試見 `tests/graph/test_implement_node.py::TestBlockedReasons`：構造一個「upstream 翻譯失敗 → downstream 被卡住」的最小案例，驗證 `blocked_reasons == {"downstream": ["upstream"]}`。

下次真實完整重測時，若 `registration` 仍被卡住，可直接讀 `state["blocked_reasons"]["registration"]` 確認卡在哪個 module，不需要再手動反查 `module_list.depends_on`——屆時再依實際資料決定要不要進一步處理 #29（本文件維持不臆測的立場）。

---

## 第一次完整重跑：找到 #18 但未解出根因

用當前 pipeline（含 08a entity 生成、修正後的 translator-cli）對 `exam-platform-api` 重新跑一次 scaffold＋fill：Plan Agent 產出 72 個 task，`completed_tasks: 60`／`failed_tasks: 2`，但 `file`／`school` 模組的局部驗證都逾時（`batch_reload_timeout`，`SERVICE_READY_TIMEOUT_SECONDS` 預設 120 秒），從未真正跑到 Newman，容器結束時已關閉，沒留下 log 可回溯。

## 第二次完整重跑：確認根因、逐一修正、`school` 模組真正 100% 通過

把逾時預算調到 300 秒、保留容器＋持續串流 log 做診斷，重新跑一次：**不再逾時**，`file`／`school`／`exam` 都拿到真實 Newman 比對結果（不是逾時空結果）。追查 `school` 模組 4 個 case 的真實失敗原因，發現 #20（`response.body` 不存在）這個影響全部 9 筆 golden 的根本性 bug，修正後重新錄製 golden、逐一修正 #9／#23／#24／#25／#26／#27／#28（多為目標專案本身的翻譯品質問題，手動修正），最終：

```
school 模組：total=3, passed=3, failed=0, pass_rate=1.0, status="pass"
excluded_cases: ["get_version_GET_api_general_version"]（正確排除，非失敗）
```

**這是本次追查達成的目標**：至少一個模組（`school`）真正跑過完整的錄製 → 容器化啟動 → 熱重載同步 → Newman 驗證 → 與真實 golden output 內容比對的完整鏈路，且完全通過，不是「因為沒有測試案例」或「逾時未測」這種假通過。

## 第三次完整重跑：驗證①③新增的四項架構修正（`_global`／`ResponseEntity`／schema 遞迴收集／`blocked_reasons`）

**目的**：使用者要求把「架構性、不該丟給 Debug Agent 去猜」的缺口（#11/#12/#28/#29/#30）先修好，讓未來 ⑦ Debug Agent 只需要處理真正的翻譯品質問題。設計、實作、單元測試完成後，對真實 `../lang-exam-api-refactor`／`../exam-platform-api` 跑一次完整 ①③[P]④⑤⑥（`../exam-platform-api` 先把上一輪殘留的手動修正 commit 掉，作為這次重新 scaffold+fill 的乾淨基礎）。

**過程中連環發現並修正的 3 個額外缺口**（都是這次才第一次真正跑到對應程式碼路徑才暴露，不是設計階段能預見的）：

1. `design_agent/type_mapping.py::_simple_type_name()` 缺 `_GENERIC_RE` import（見上方 #31）——①③首次處理真實帶 requestBody 的 POST 端點才觸發。
2. `plan_agent/module_index.py::classify()` 不認得 `app/core/exception_handlers.py`（`_global` 保留模組固定輸出）——[P] 首次處理 `_global` 模組的 task 才觸發，`PlanAgentModuleLookupError` 中止整個 [P] 呼叫。修正：`classify()` 新增這個路徑的精確比對特例，回傳 `("_global", "routers")`。
3. `translator_cli/scaffold.py::_render_interface_files()` 同樣不認得這個檔案，判定成 unknown layer 整個跳過——④首次處理 `_global` 模組的 interface 才觸發。連鎖後果：③正確產出的 `app/main.py` 仍會 `import` 這個從未寫入磁碟的模組，容器內 import 階段直接 `ModuleNotFoundError`，服務整個起不來（一開始誤判成 `PythonServiceStartupTimeout`，手動重現容器啟動過程才挖出真正原因）。修正：新增精確比對，渲染成不含 `APIRouter` 樣板的自由函式檔案。

**最終驗證結果**（①③[P]④⑤⑥ 全部真實跑過，不是分段模擬）：

```
① module_list: 7 個模組（含 _global，java_files 正確指向 GlobalExceptionHandler.java）
③ 71 個 InterfaceSpec（含 exception_handlers.py::handle_all；
  file_router.py::voice_2／image_2 正確標成 return_type="Response"）
[P] 71 個 task（_global 模組 1 個：task_070）
④ success=True，skipped_interfaces=0，skipped_db_models=0
⑤ completed_tasks=55，failed_tasks=5（全部集中在 exam 模組，
  1 個 ollama 連線暫時失敗＋4 個 delimiter 格式違反，見 #29）
  blocked_modules=['registration', 'grading']
  blocked_reasons={'registration': ['exam'], 'grading': ['exam']}（見 #29，D 項目診斷欄位真實驗證通過）
⑥ readonly 9 個 case：0 passed（根因見下方，全部是翻譯品質問題，非架構問題）
```

**容器內直接驗證架構層根因確實解決**（這是本次追查真正的目標，比 readonly pass/fail 數字更關鍵）：

- 手動重現容器啟動＋直接 curl `/api/grading/test-exception`，從 500 錯誤的 **完整 traceback 直接看到** Starlette 例外處理中介層呼叫到 `/srv/app/core/exception_handlers.py` 的 `handle_all`——證實「這個方法會不會被框架呼叫到」這個架構問題已解決。實際 500 的原因是 ⑤ 翻譯出的 `handle_all` 函式本體 `import` 了一個不存在的 `app.exceptions` 模組（`AppException`／`BaseException` 兩個幻覺類別）——這是翻譯品質問題，不是架構問題。
- 直接讀真實生成的 `app/schemas/registration.py`：`GetAllGradeRs`／`GetAllClassesRs`／`GetAllSchoolRs` 全部自我完備定義在檔案內，確認 #28 徹底解決。
- 真實 `voice_2`／`image_2` 兩個 `ResponseEntity<FileRs>` 方法正確轉成 `return_type="Response"` 並產出可執行程式碼；`voice_1`／`image_1` 兩個 golden case 仍然失敗，但失敗原因是 `status_code_mismatch`（實際回 404 "Directory not found"，golden 預期 200＋503 body）——這是 ⑤ 翻譯的業務邏輯分支跟 Java 原始行為不完全一致，不是型別系統或路由層面的架構問題（如果架構沒修好，連可執行的程式碼都不會產生，只會是 `ResponseEntity[?]` 這種語法錯誤）。
- readonly 其餘 6 個失敗案例（`schools`／`grades`／`classes`／`locals`／`language` 回傳 `None` 或 `TypeError: 'list' object is not callable`）跟這輪四項修正完全無關，是全新一輪 qwen 生成的翻譯品質問題（`list` 遮蔽 builtin 這個問題屬於 #9 同一類，機率性地在這次全新生成中又出現一次）。

**結論**：①③新增的四項架構修正（`_global` 全域類別收集、全域例外處理生成與正確註冊、`ResponseEntity → Response` 型別對應、schema 遞迴收集）**在真實環境中全數確認有效**——不只是通過自建的單元測試，而是對真實 Java 專案重新跑過完整 pipeline、在真實 Docker 容器內觀察到預期的框架行為。剩餘的所有失敗（無論是 ⑤ 填空失敗還是 ⑥ readonly 驗證失敗）都能明確歸類為翻譯品質問題，不再有「不確定這是架構缺陷還是翻譯品質」的模糊地帶——這正是這輪修正的目標。

## 待決定事項

以下四項已在第三輪完整重跑中解決，保留刪除線供追溯：

- ~~**①（04a/04b）要不要新增「全域生效、不屬於任何 module」的 Java 檔案辨識機制**~~——已解決，見 04a 十一章、`parse_agent/grouping.py::collect_global_advice_classes()`
- ~~**③（05a/05b）三章「全域基礎設施檔案」要不要新增「Java 全域例外處理...」規則**~~——已解決，見 05a 十四章、`design_agent/design.py::_design_global_advice_module()`
- ~~**#30：③（05a）五章型別對應表要不要新增 `ResponseEntity<T>` 的處理規則**~~——已解決，決定用 `Response`／`JSONResponse` 回傳型別表達，見 05a 十四章
- ~~**#28（05a schema 跨檔案參照缺口）是否需要系統性修正**~~——已解決，`collect_named_schemas()` 改為遞迴收集、每個 module 自我完備，見 05a 十四章

**尚未解決／新發現**：

- [ ] **`SERVICE_READY_TIMEOUT_SECONDS` 的正式預設值**：目前 09a/09b 程式碼裡預設仍是 120 秒，這次診斷用 300 秒才穩定不逾時，需要決定要不要把預設值正式調高（或改成依批次 task 數量動態計算），這個決定會影響 `graph/nodes/implement_node.py` 的既有預設值
- [ ] **mutation collection 路徑完全沒有被三輪完整重跑觸及**：#1／#2／#3／#20 的修正對 mutation 端（`record_mutation()`／`MutationVerifier`）的影響仍是推論，不是實測；第三輪重跑改成單獨跑 readonly（見「第三次完整重跑」），沒有解決這個既有缺口
- [ ] **`_design_global_advice_module()` 目前只認 `@ExceptionHandler(Exception.class)` 這種全域 catch-all**：真實案例裡的 `handleBaseException(BaseException e)`（`@ExceptionHandler(BaseException.class)`）刻意不處理，只記警告——若日後有真實案例需要這類更細的例外處理，需要重新評估範圍是否要擴大，以及擴大後要不要走跟 catch-all 相同的 task pipeline
- [ ] **⑤ 翻譯品質問題：`handle_all` 函式本體 import 了不存在的 `app.exceptions` 模組**（`AppException`／`BaseException` 兩個幻覺類別）——第三輪重跑真實觸發，架構層（例外處理器有沒有被正確呼叫到）已確認沒問題，這是 qwen 生成內容本身的品質問題，留給未來 ⑦ Debug Agent 或人工校正，不在這輪處理範圍
- [ ] **⑤ 翻譯品質問題：`voice`／`image` 的業務邏輯分支跟 Java 原始行為不完全一致**（回 404 "Directory not found" 而非 golden 預期的 200＋503 body）——架構層（`ResponseEntity → Response` 型別轉換、動態 status 程式碼是否可執行）已確認沒問題，這是函式本體邏輯翻譯品質問題
- [ ] **⑤ 翻譯品質問題：`list` 遮蔽 builtin（#9 同一類問題）在全新一輪生成中又出現**——證實這類問題有一定機率重演，不是一次性修好就不會再犯，未來 ⑦ Debug Agent 若要處理這類問題，可能需要考慮某種形式的靜態檢查或 lint 規則預防，而不只是逐案修正
- [ ] **`registration_router.py::schools` 回傳 `None` 觸發 `ResponseValidationError`**：這次重跑才發現的新翻譯品質問題，還沒深入追查根因（可能是查詢邏輯本身寫錯、或某個條件分支忘記回傳），留給未來 ⑦ Debug Agent 或人工校正

---

*本文件記錄 09a/09b 端對端驗證過程中的發現，完整重現步驟與程式碼修正內容見 `09b_implement_agent_code.md`。*
