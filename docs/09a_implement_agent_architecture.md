# ⑤ 功能改寫 Agent 詳細設計

> 本文件承接 `00_refactor_architecture.md`（七、⑤ 功能改寫 Agent 一節；八、task list 欄位定義）、`01_langgraph_architecture.md`（六、Module 排程器實作）、`02a_harness_architecture.md`（十三、Module 級別的局部驗證）、`07a_translator_cli_architecture.md`（五、填空模式契約）、`08a_scaffold_agent_architecture.md`（八、九、十二章）。是這個 Agent 的**設計面**文件：決策、契約、資料結構、流程。實際程式碼實作見 `09b_implement_agent_code.md`（待建立）；本文件不出現完整可執行的實作邏輯，僅在需要精確釘死修正內容時附關鍵片段。

---

## 一、本文件範圍與定位

`ModuleScheduler`（`graph/scheduler.py`）與 `implement_node.py` 的骨架早在 `01_langgraph_architecture.md` 六章就已經設計並落地，`translator_cli.fill_function()` 的呼叫契約已在 `07a_translator_cli_architecture.md` 定案、`07b_translator_cli_code.md` 完成真實環境端對端驗證（70 個 task 中 69 個成功，見 07b 十一章）。**本文件不是從零設計 ⑤，而是把既有文件明確留給 09a 的收尾缺口定案**：

**本文件涵蓋**：
- Harness 局部驗證的真正串接，含熱重載期間的競態防護：`implement_node.py` 目前 `_partial_verify()` 仍是固定回傳 `pass` 的 stub（`db`／`verifier` 為 `None`），本文件定案如何接上真正的 `DbEnvironment`／`GoldenVerifier`，以及為什麼每次局部驗證前都需要等待 Python 服務重啟完成（三章）
- task／module 兩層驗證觸發時機的最終確認，對齊 `02a_harness_architecture.md` 十三章（四章）
- `context`／`context_files` 補強：ORM `relationship()` 缺失的提示、共用 Enum 定義檔 `_enums.py` 的引入，這是 `08a_scaffold_agent_architecture.md` 八章明確交棒給 09a、但過去沒有被任何文件實際接手的兩個 prompt 內容缺口（五章）
- task 失敗根因追蹤與提前排除：`RefactorState` 新增 `task_failures`，讓「④ 骨架缺口」與「翻譯品質問題」兩種不同根因在資料層面就能分辨，且 scaffold 缺口一經確認就從排程中永久排除，不再浪費本地模型呼叫（六章）
- Debug 迴圈重試語意的一處既有缺陷修正：`already_failed` 若直接取用 `state["failed_tasks"]`（跨整條 graph run 累積的歷史清單），會讓「可能修得好」的翻譯品質失敗被誤判成永久排除；反過來若完全不排除，scaffold 缺口這種永久修不好的失敗又會被無謂地重新嘗試——七章定案兩者該用什麼資料來源區分
- 與 `RefactorState`／既有程式碼的介面異動（九章）

**本文件不涵蓋**：
- `ModuleScheduler` 排程演算法本身（拓樸排序、regression 偵測、防環、`module_owned_files`）——已在 `01_langgraph_architecture.md` 六章定案並落地為 `graph/scheduler.py`，本文件不重新設計，只在七章指出並修正呼叫端（`implement_node.py`）對它的一處誤用
- 兩層驗證「比對什麼、report 格式、Masker／DiffEngine 邏輯」——見 `02a_harness_architecture.md` 三～九章，本文件只確認觸發時機與呼叫參數
- translator-cli 的填空契約、AST 插入、delimiter 重試、git snapshot——見 `07a_translator_cli_architecture.md`／`07b_translator_cli_code.md`，已完成真實驗證，本文件視為黑盒直接呼叫
- ⑦ Debug Agent 如何讀取 `task_failures`／`test_results` 分析根因、產生修正指令，以及是否要讓 `scaffold_skipped` 造成整條 pipeline 提早結束——見 `10a_debug_agent_architecture.md`（待建立），本文件只保證資料有進 `RefactorState`、且不再浪費本地模型呼叫，更激進的「提早止損」路由留給 10a 評估（見十一章）

---

## 二、現況：已完成與待收尾

| 項目 | 狀態 | 說明 |
|---|---|---|
| `ModuleScheduler`（`graph/scheduler.py`） | ✅ 已完成 | 拓樸排程、regression 偵測、防環，見 01 六章 |
| `implement_node._run_one_task()` | ✅ 已完成 | 已接上真實 `translator_cli.fill_function()`，含 `python_project_path`／`task_id`／`class_name`／`function_name`／`context` 全部引數，見 07b 九章第 4 項 |
| `should_run_tests_or_give_up()`、`scaffold_done=False` 短路 | ✅ 已完成 | 見 01 五章「scaffold 失敗時的收尾路徑」 |
| `graph/builder.py` 的 conditional edges | ✅ 已完成 | 見 01 五章 |
| `_partial_verify()` 的 `db`／`verifier`、熱重載等待 | ❌ stub | `db = None`／`verifier = None`，固定回傳 `{"status": "pass", "details": {}}`，且完全沒有處理 Python 服務重啟期間的競態，見三章 |
| `context`／`context_files` 補強（relationship 提示、`_enums.py`） | ❌ 未實作 | 見五章 |
| task 失敗根因（`skipped_interfaces` 比對、`FillResult.error` 留存） | ❌ 未實作 | `failed_tasks` 只有 task id，錯誤訊息與根因分類完全遺失，見六章 |
| Debug 迴圈重試 | ⚠️ 有缺陷 | `already_failed=set(state.get("failed_tasks", []))` 把歷史累積清單當成排除清單，見七章 |

---

## 三、Harness 局部驗證串接

### `DbEnvironment`／`GoldenVerifier` 的建構時機與參數

比照 `refactor_harness/langgraph_nodes/test_nodes.py` 既有的 `run_postman_tests()` 建構模式，`run()` 開頭各建構一次 `DbEnvironment`（`test_dsn` 取 `state["test_dsn"]`）與 `GoldenVerifier`（`python_base_url` 取 `state["python_base_url"]`、`golden_dir` 固定 `"fixtures/golden"`），整個 while 迴圈重複使用同一個物件參照，不是每次局部驗證都重新建構——`GoldenVerifier` 內部只持有設定與共用工具，不持有連線狀態，重複使用沒有副作用；`DbEnvironment` 同理，每次 `apply_seed()` 呼叫都是獨立的 `psycopg2.connect()`。

### `tables_to_truncate`：沿用 `config/harness.yaml` 的同一份 `TABLES`，不是 `verifier.tables`

01 六章文件當初的概念性草稿寫的是 `db.apply_seed("fixtures/seed.sql", tables_to_truncate=verifier.tables)`——**這個屬性在真實 `GoldenVerifier` 實作裡不存在**（見 `refactor_harness/verifier/comparator.py`）。真正的 `tables_to_truncate` 來源是 `config/harness.yaml` 的 `databases.test.tables_to_truncate`，`test_nodes.py` 已經在模組層級讀出這份設定：`TABLES = HARNESS_CONFIG["databases"]["test"]["tables_to_truncate"]`。`implement_node.py` 比照同一種讀法，不新增第二份設定來源。**局部驗證截斷／重灌的是全部表，不是只有這個 module 名下的表**：`config/harness.yaml` 沒有「module → tables」的對應關係，全庫級 `apply_seed()` 才能保證外鍵約束不會因為只清一部分表而出問題，這與 ⑥ 全量驗證使用同一份 `TABLES`、同一份 `seed.sql`，行為完全對稱。

### 熱重載競態：`_partial_verify()` 前必須確認 Python 服務真正換過新程式碼

**問題**：每次局部驗證都是「translator-cli 剛寫入磁碟 → 立刻打 newman」，中間完全沒有等待。若 Python 服務是以 `uvicorn main:app --reload` 這類熱重載模式常駐（本章下方「運行前提」說明為什麼必須是這個模式），reload 機制在偵測到檔案異動後，會先關掉舊 worker、再啟動帶新程式碼的新 worker，中間有一段連線會被拒絕（`Connection refused`）的空窗期。若 `_partial_verify()` 剛好在這段空窗期打 newman，02a 五章「Newman 共用執行器」的既有設計（服務沒起來直接拋例外，不吞）會讓這次例外被誤判成「Python 服務整個掛了」，進而讓整條 `graph.ainvoke()` 崩潰，但實際上只是重啟中的暫時狀態，不是真正的環境錯誤。

**單純「輪詢直到能連線成功」不夠**：檔案監控本身有 debounce，連線成功只代表「有某個 worker 在回應」，分不清是還沒被淘汰的舊 worker、還是真的換上新程式碼的新 worker。**解法**：需要一個極輕量的探測端點（`GET /__reload_probe__`），但不是比對「這個值有沒有變」——那個做法（見下方「沒有採用的替代方案」）自己會製造新的競態；改成比對「這個值是不是我要的那個」：探測端點回傳的 token 由 Orchestrator 自己控制、寫入前就先決定好，不是由 worker 自己隨機產生後被動觀察。

**這個端點不寫進 `app/main.py`，改用外掛的 ASGI wrapper 掛載真正的 app**：若直接把探測端點塞進骨架生成的 `app/main.py`，會讓一段純粹服務重構 pipeline 自己需要的探測端點，永久留在目標專案的正式原始碼裡——重構完成、`app/main.py` 進入正式維護後，這個端點依然掛著，沒有人會記得回頭移除，變成一個在 production 環境長期曝露的多餘 API，違反「測試基礎設施不進正式程式碼」的分際，也會讓 09a 背上一個需要異動 05a／05b 已驗證內容的跨文件依賴。改為在 `python_project_path` 底下另外放一個獨立的 wrapper 檔案（例如 `_reload_probe_wrapper.py`，命名上就清楚標示這是工具檔，不是業務程式碼的一部分），`state["python_base_url"]` 不需要改變任何指向（既有路由原封不動維持在原本的路徑）。組裝順序**必須**是：

1. 先註冊 `/__reload_probe__` 這個明確路徑的路由
2. 再 `mount("/", app)` 掛載真正的 `app.main.app`

**這個順序不能顛倒，顛倒會讓探測端點永遠打不到**：Starlette／FastAPI 的路由是依註冊順序逐一比對，`Mount("/", app=...)` 掛在根路徑，任何路徑都會先符合它的前綴——若 `mount("/", app)` 排在 `/__reload_probe__` 之前註冊，之後任何對 `/__reload_probe__` 的請求都會先被這個 catch-all mount 攔截、轉發進 `app.main.app` 內部找不到對應路由，回傳 404，而不是 wrapper 自己要回的 token；`_wait_for_service_reload()` 因此永遠等不到預期的回應值，每一輪都會逾時。

**探測端點讀取 token 檔案時要容忍檔案還不存在**：模組載入當下（每次 uvicorn `--reload` 產生新 worker 時）`from _reload_token import TOKEN`（下方「決定性的同步屏障」說明這個檔案與這個 import 寫法）——但 pipeline 第一次啟動 Python 服務時，這個檔案根本還沒被任何一輪 `_wait_for_service_reload()` 寫過，若沒有容錯，這行 import 會在 wrapper 自己的模組載入階段直接拋出 `ModuleNotFoundError`，讓整個 wrapper（連同掛載在裡面的真正 app）完全起不來。因此這一行要包在 `try/except ModuleNotFoundError`（不是 `FileNotFoundError`——`import` 找不到模組時拋的是 `ModuleNotFoundError`，這是 `ImportError` 的子類別，跟直接 `open()` 一個不存在的檔案拋出的例外不是同一種）裡，退回一個不可能等於任何真實 token 的預設值（例如空字串——真實 token 一律是 Orchestrator 產生的 UUID，不可能是空字串）。這樣即使一次性前置準備沒有額外去建立一份初始 token 檔案，wrapper 本身也能正常啟動；`run()` 開頭的初始探測（下方）只需要「有回應」，不在乎內容是不是這個預設值，第一輪 `_wait_for_service_reload()` 寫入第一個真正的 token 後，一切照常運作。

**Python 服務只啟動一次，`implement`（⑤）與 `run_tests`（⑥）共用同一個持續運行中的行程，不是各自分別啟動**：改成 `uvicorn _reload_probe_wrapper:wrapper_app --reload`（不是直接 `uvicorn app.main:app --reload`）啟動，這個 `uvicorn` 行程在整條 `implement → run_tests` 期間持續存在、不被 Orchestrator 重新 spawn 或替換——⑤ 執行期間依賴的是這個行程隨 `--reload` 自動更新程式碼；⑥ 執行時單純沿用同一個仍在運行的行程繼續打 API，不需要（也不應該）另外指向一個沒有經過 wrapper 的乾淨服務，否則 ⑥ 驗證的會是一個從未反映過 ⑤ 任何寫入的舊狀態。wrapper 本身內容固定、不依賴 `python_structure`／`task_list` 等任何動態資料，不需要由任何 Agent 產生，是隨 `refactor-project` 自己的工具鏈提供的固定樣板，跟 `python_project_path` 目錄準備（07a 二章「新輸入」的一次性前置準備）性質相同。這樣完全不需要異動 05a／05b，`app/main.py` 從頭到尾維持 `render_main_py()` 產出的乾淨內容。

這個機制不需要求 05a／05b 配合新增任何東西，完全在 09a／⑤ 自己的邊界內就能落地。wrapper 檔案的實際存放位置、由誰在什麼時候寫入 `python_project_path`，跟三章「運行前提」已經指出、目前仍懸而未決的「Python 服務啟動機制沒有文件正式列為 pipeline 步驟」是同一個操作缺口的一部分，一併留給十一章那個既有的待決定事項收斂，不在這裡另開一條。

### 決定性的同步屏障：比對特定 token，不是比對「有沒有變化」

**「記基準 → 觸發重啟 → 等值跟基準不同」這個方向本身有一個無法迴避的競態**：同一輪可能有多個 task 依序寫入不同檔案（本地模型併發數鎖死為 1，見 00 三章，寫入天生是逐一發生、彼此有時間差的），這些寫入各自觸發的 uvicorn 重啟，完成的時間點跟「Orchestrator 讀取基準」「Orchestrator 主動觸發保底重啟」這兩個動作之間，完全可能任意交錯——即使保底觸發的動作被安排在讀基準之後，也無法保證「等到的第一次變化」就是保底觸發那一次，而不是某個較早寫入自然觸發、剛好在這段時間內完成的重啟。萬一等到的是後者，會提早判定「就緒」放行進 Newman，而保底觸發那次重啟其實還沒發生，會在驗證途中才真正發生，把正在進行的 Newman 測試連線腰斬——問題不在「哪個動作先做」，而在於「比對有沒有變化」這個判斷方式本身，只要不知道自己等到的是哪一次變化，就永遠有可能等到不對的那一次。

**解法：不比對「有沒有變化」，比對「是不是我指定的那個值」**：

1. 產生一個全新的、Orchestrator 自己決定的 token（例如一個新的 UUID，不是讀取伺服器回傳的值，是憑空生出來的）
2. 把這個 token 寫進 wrapper 會讀取的那個檔案（`_reload_token.py`，用 `.py` 副檔名是為了確保落在 uvicorn 預設的重載監控範圍內，不需要額外設定監控路徑，這是 09b 落地時需要對照真實環境確認的細節，見十一章）——內容固定是單一一行賦值敘述 `TOKEN = "<uuid>"`，Orchestrator 每次都整份覆寫這一行；wrapper 端用普通的 `from _reload_token import TOKEN`（模組層級 import，不是自己 `open()` 讀檔案文字再解析）取得這個值，因為 uvicorn `--reload` 每次都是全新的 process 重新 import，不是在同一個長駐 process 裡用 `importlib.reload()`，不會有 import cache 導致讀到舊值的疑慮
3. 輪詢 `/__reload_probe__`，直到回傳值等於步驟 1 產生的那個 token

這樣不論這一輪的個別寫入各自觸發了幾次重啟、彼此的完成順序是什麼，都不影響結果——中途觀察到的任何其他值（不論是舊值，還是某次個別寫入觸發的重啟剛好也走到這裡）一律不算數，只有真正讀到「這個 worker 是在 token 檔案更新之後才啟動的」這個值才會通過。因為寫入 token 檔案這個動作本身嚴格排在這一輪所有 `fill_function()` 寫入完成之後，任何回傳這個 token 值的 worker，必然是在那之後才啟動的，天生就包含了這一輪全部的程式碼變更。

**已知限制：這個機制必然讓每一輪局部驗證多付出至少一次額外的完整重啟成本**：這一輪 task 自己的寫入本來就會觸發（至少一次、通常更多次）reload，寫入 token 檔案又保證再觸發一次——換句話說，正確性換來的代價是每一輪局部驗證前，Python 服務至少重啟兩次，每次重啟都要重新 import 整個 app、重建 SQLAlchemy engine 與連線池，不是免費的。這是為了拿到「明確同步屏障」必須付出的代價，不是可以省略的多餘步驟；若真實環境跑起來這個延遲影響太大，屬於效能調校範圍，留待接上真實環境後再評估。

**寫入 token 檔案跟讀取端點回應，都必須容忍暫時連不上，不能是一次性、不重試的請求**：這一輪批次寫入（`asyncio.gather()`）進行的過程中，先完成的 task 本來就可能已經觸發了 uvicorn 的重啟，寫入 token 檔案、或之後輪詢讀取回應的當下，伺服器很可能剛好落在「舊 worker 已經被殺掉、新 worker 還沒起來」的空窗期，遭遇 `Connection refused`——輪詢階段本來就會持續重試直到讀到目標 token 或逾時，天生已經涵蓋了這種情況，不需要另外處理。

**沒有採用的替代方案：touch 這一輪成功寫入的檔案，逼出保底重啟，再等值跟某個基準不同**：這個方向會被本節開頭描述的競態否證——touch 保證「基準之後一定還會再觸發一次重啟」，但不保證「輪詢時第一個觀察到的變化就是那一次」，中間可能被某個較早寫入自然觸發、剛好完成的重啟搶先滿足條件，讓 touch 保底的那次重啟反而落在驗證開始之後才發生。改成比對特定 token 從根本上避開了這個問題：不是「保證還會再變一次」，而是「只認可我指定的那個值」，途中任何提早出現的變化都不會被誤判成就緒。

`run()` 開頭（進入 while 迴圈之前）呼叫一次容忍暫時連不上、重試到任何一次成功回應為止的邏輯，單純確認 Python 服務這時候有在跑，不涉及 token 比對——這裡不需要知道回應的具體內容是什麼，只需要確認服務本身有回應；逾時的處理方式跟迴圈內的 token 等待不同：見下方「逾時不該讓整條 pipeline 崩潰」。

### `_reload_token.py`／`_reload_probe_wrapper.py` 必須排除在 07a 九章的衝突偵測之外

**這兩個檔案若原封不動放在 `python_project_path` 底下，會讓 `translator_cli` 自己的 precondition 檢查連環失敗**：`git_ops.check_clean_working_tree()`（07a 九章「衝突偵測」）在 `generate_scaffold()`／`fill_function()` 動筆寫任何檔案之前，都會先跑一次 `git -C {python_project_path} status --porcelain`，**只要輸出非空就拒絕寫入**——這個判斷不分「已追蹤檔案被改動」還是「未追蹤的新檔案」，兩者都算「不乾淨」。`_reload_probe_wrapper.py`（一次性放入，內容固定）與 `_reload_token.py`（每一輪局部驗證都要覆寫一次新 token，見上方步驟 2）若沒有被 git 追蹤也沒被忽略，會一直以未追蹤檔案的身分出現在 `git status --porcelain` 的輸出裡——不只是後續的 `fill_function()` 呼叫會被這個檢查擋下、被誤判成 `fill_failed`（六章）陷入死鎖，連 `generate_scaffold()` **自己第一次執行**的 precondition 檢查都會先被攔下：若 `_reload_probe_wrapper.py` 在 `scaffold` 階段開始之前就已經放進 `python_project_path`，骨架階段甚至還沒開始寫任何東西就會直接失敗。

**修正**：把這兩個檔名加進 `python_project_path/.gitignore`——`git status --porcelain`（不加 `--ignored`）本來就不會列出被忽略的檔案，這樣兩個檔案不論怎麼覆寫，都不會被 `check_clean_working_tree()` 判定成「不乾淨」。這一步必須在 07a 二章「一次性前置準備」（`python_project_path` 已 `git init`、尚未有任何 commit）的同一個階段完成，且要排在 `generate_scaffold()` 第一次執行之前：建立 `.gitignore`（內容含這兩個檔名）並 `commit` 一次，作為這個 repo 的第一個 commit；`_reload_probe_wrapper.py` 這時候才放進去，因為已經被 `.gitignore` 涵蓋，不會讓 `generate_scaffold()` 隨後的 precondition 檢查看到任何未追蹤或未忽略的異動。這不是要求 07a 的 precondition 檢查程式碼本身做任何改動——`check_clean_working_tree()` 檢查的原本就是「working tree 是否乾淨」，不是「commit 數量是否為零」，多一個記錄 `.gitignore` 的初始 commit 不違反這個檢查的既有邏輯，只是替 09a 自己新增的兩個檔案補齊必要的一次性設定，跟 wrapper 檔案本身「何時放進 `python_project_path`」是同一個操作步驟，一併歸進十一章既有的「Python 服務啟動機制」待決定事項，不獨立另開一項。

**不需要額外調整 uvicorn 的啟動參數**：`watchfiles`（uvicorn `--reload` 底層依賴的檔案監控套件）與 uvicorn 自己的 `WatchFilesReload`／`FileFilter`，判斷要不要監控一個檔案完全是靠寫死的副檔名清單與固定忽略目錄（如 `__pycache__`／`.git`），從原始碼層級確認過兩者都沒有解析 `.gitignore` 的邏輯——把檔案加進 `.gitignore`只影響 git，不影響 uvicorn 的重載判斷。`_reload_token.py` 選 `.py` 副檔名的理由（見上方「決定性的同步屏障」步驟 2）因此依然成立、不需要再加 `--reload-include` 之類的參數去抵銷 `.gitignore` 的影響——這個影響本來就不存在。

**這個 `.gitignore` 修正涵蓋的是「每一輪覆寫」的情況，不是只解決第一次寫入**：`.gitignore` 讓 git 對這兩個檔案「視而不見」是無條件的——一個檔案只要**從未被 `git add`（不論是手動、`fill_function()` 的 `git add {target_file}`、還是 `generate_scaffold()` 的 `git add -A`）**、又符合 `.gitignore` 規則，`git status --porcelain` 就永遠不會提到它，不論這個檔案的內容被覆寫過幾次。`_reload_token.py` 在整條 pipeline 執行期間依設計**從來不會被 git add**（見上方步驟 2，只單純寫入檔案，沒有配任何 git 操作）——因此不存在「round 1 覆寫後 round 2 的 precondition 檢查抓到異動」這回事：round 2 檢查的當下，git 根本不知道有這個檔案存在，跟 round 1 覆寫過幾次無關。`generate_scaffold()` 自己的 `git add -A`（07a 八章）同樣不受影響——`git add -A`／`git add .` 這類萬用字元加檔案的操作，預設就會排除符合 `.gitignore` 的項目，不需要額外處理就不會誤把這兩個工具檔案收進骨架的那次 commit。

### 批次執行，不是每個 module 各自等一次

**若讓每個 module 各自等一次，同一輪內第二個以後的驗證會逾時崩潰**：這一輪批次寫入完成後，可能同時有多個互不相關的 module 達到可以驗證的狀態；第一個 module 產生一個 token、寫入、等到它被回應後，磁碟不會再有新異動、伺服器也不會再重啟——第二個 module 若還各自重新走一次「產生新 token → 寫入 → 等待」，會等到一個新產生、但沒有任何後續寫入會觸發的重啟去回應它，直接逾時。因此「產生 token 並等待重啟」這件事在 `run()` 的 while 迴圈裡整輪只做一次：這一批 `fill_function()`（`asyncio.gather()`）全部完成之後、進入逐 module 驗證之前，統一做一次；`_partial_verify()` 本身因此簡化回單純「seed＋Newman 比對」兩步，不再牽涉重啟偵測。

**要不要驗證某個 module，判斷依據是「這個 module 底下有沒有 task 失敗」，不是「這一輪有沒有 task 成功」**：`translator_cli.fill_function()`（`07b_translator_cli_code.md` 八章 `client.py`）有兩條「先落地、判定失敗後才用 git checkout 復原」的路徑（格式化失敗、commit 失敗）——這種情況磁碟確實動過，但最終 `FillResult.success` 仍是 `False`，代表「有沒有 task 成功」不能拿來當作「磁碟有沒有動」的可靠依據。更根本的是，一個 module 只要有任何 task 最終判定失敗（不論是六章的 `scaffold_skipped`，還是上面這種 `fill_failed`），它的 API 行為本來就必然不完整，Newman 打下去可以預期是 fail，完全不需要真的驗證一次才知道——因此局部驗證前先查排程器已經在維護的 `scheduler.task_failed` 集合：**這個 module 底下有失敗 task → 不呼叫 Newman，直接 `mark_module_verified(module, passed=False)`；沒有 → 才真的驗證**。這個規則同時解決了「該不該等重啟」的判斷：只要接下來真的要驗證某個 module，代表它剛完成的那個 task 必然是成功的（有失敗 task 的 module 已經被排除在外），也就必然對應一次乾淨、真正落地的寫入，兩件事因此變成同一件事，不需要再額外維護一個「這一輪有沒有東西寫入」的旗標。`needs_reverify`（regression 重驗）的觸發前提本來就是「有 task 成功寫入、且波及已驗證 module」，同樣自動滿足。

### 逾時不該讓整條 pipeline 崩潰

模型（qwen）產出的程式碼即使通過 `ast.parse()`（語法合法），仍可能帶有模組層級就會炸開的語意錯誤——最常見的是 import 一個不存在的名稱。`app/main.py`（見九章）在啟動時會 import 每一個 router 檔案，這類錯誤會讓整個 ASGI app 連 import 都過不了，新 worker 直接啟動失敗，`/__reload_probe__` 永遠不會給出新的識別碼——這正是等待重啟這個機制**最終會撞上**的逾時情境，而且是模型生成品質造成的、預期中會發生的失敗模式，不是環境沒準備好。若沿用「逾時就往上拋、中止整條 `graph.ainvoke()`」，這類本該交給 `debug → implement` 迴圈處理的常規失敗，會直接讓整條重構流程崩潰退出，不留下任何診斷資訊。

**修正**：`run()` 迴圈裡「批次等一次重啟」這一步逾時時不再讓例外往外傳，而是就地接住，把這一輪原本要驗證的每一個 module 都判定成沒通過，然後讓 while 迴圈照常往下走——這正是 `retry_count`／`debug` 既有機制原本就該接手的情境，不需要另闢一條路徑。

**`run()` 開頭那一次性的初始探測，只在整條 graph run 真正第一次進入 `implement` 時才會「不吞、直接往上拋」，`debug → implement` 重入時必須跳過**：這條規則不能無條件套用——`fill_function()` 失敗保證不落地（07a 五章「呼叫失敗時不寫入任何內容」），代表 Python 服務唯一會被真的弄壞的路徑，是某個 task **成功**寫入了語法合法、但匯入或語意有問題的程式碼，觸發上方「逾時不該讓整條 pipeline 崩潰」描述的 `batch_reload_timeout`。而 `batch_reload_timeout` 發生後最常見的下一步，正是 `debug → implement` 重入——這代表：這一輪重入時，服務極可能**還在**上一輪弄壞之後的當機狀態（uvicorn 的 reload 監控在 worker crash 之後不會自己重試，只會等下一次偵測到檔案異動才再嘗試），而修復程式碼的那次寫入要等這一輪 while 迴圈跑起來才會發生，此時都還沒發生。若初始探測不分青紅皂白一律「連不上就中止」，等於在 debug 迴圈原本該有的修復機會出現之前，就把好不容易繞過崩潰的機制，繞回同一個崩潰——這不是邊角案例，是這個容錯機制設計出來要接住的那個情境，重入時卻被入口處的探測搶先誤判成致命錯誤。

**修正**：`run()` 開頭檢查 `state.get("completed_tasks")` 與 `state.get("task_failures")` 是否都是空的——兩者皆空才代表這是整條 graph run 第一次進入 `implement`，這時候服務理論上不該連不上，連不上就是操作前提沒滿足（沒有任何程式碼可以歸咎），維持「不吞、直接往上拋」；只要任一個非空，代表先前已經有 task 真正執行過（不論成敗），服務目前連不上完全可能是上一輪某次成功寫入造成的、有機會靠這一輪的寫入自行修復——這種情況下完全跳過初始探測，直接進入 while 迴圈，把「服務現在到底通不通」交給迴圈內已經設計好、能容忍逾時的批次 token 等待機制去處理，不需要在入口再多做一次會誤判的檢查。

**這批因逾時而判定失敗的 module，`partial_reports` 裡的 report 必須跟 Newman 真的跑完、比對出 body diff 的失敗明確分開，不能只是一段人類可讀的說明文字**：逾時發生時，這一輪同時要驗證的 module 可能不只一個（三章「批次執行」），而逾時本身**不知道是哪一個 module 的寫入造成的**——`app/main.py` 在啟動時 import 全部 router，任何一個 module 的程式碼有模組層級錯誤都會讓整個服務起不來，牽連當下要驗證的其他 module，而那些 module 自己的程式碼完全有可能是對的。若這批「陪葬」的 module 跟真正因為 body diff 不符而失敗的 module 用同一種 report 格式呈現，10a（⑦ Debug Agent，待建立）事後分析時無從分辨「這是業務邏輯寫錯」還是「同一批裡有別的檔案把整個服務弄掛，這個 module 自己可能沒問題」，會浪費心力去看幾支其實無辜的程式碼、也定位不到真正的根因。這批 module 的 report 因此用明顯不同於 `HarnessReporter.build_report()`（`summary`／`status`／`failures`／`passed_cases`，見 02a 九章）的最小結構表示，例如只有 `{"status": "fail", "reason": "batch_reload_timeout"}`，不帶 `failures`／`body_diff` 這類欄位——10a 只要先檢查 `report.get("reason") == "batch_reload_timeout"`，就能立刻認出這整批屬於「同一輪逾時連坐」，優先去檢查這一輪寫入的每個檔案本身能不能被正確 import（而不是先假設是業務邏輯錯誤去比對 diff），且這一輪被牽連的 module 應該一起看，因為真正的根因大機率只在其中一個。

**只跑 readonly，不跑 mutation**：比照 02a 十三章「第一層：局部驗證」的既定範圍——局部驗證的目的是 fail-fast、快速縮小 Debug 範圍，mutation 的完整涵蓋是 ⑥ 全量驗證的職責，不重複。

**每次都重新 `apply_seed()` 全庫，即使 readonly collection 理論上零副作用**：這不是遺漏的效能優化點，是刻意的保守選擇——readonly collection 本身不含寫入操作，但這只保證「Java 端」的行為零副作用，不保證這一輪剛被 qwen 填空的 Python 程式碼也一樣乾淨；⑤ 填空的對象正是還沒被驗證過的新程式碼，不能假設它沒有意外寫壞資料的副作用（例如一個本該是純查詢的 GET handler，被寫成順便更新了某個時間戳欄位）。每次都重新 seed，才能保證每一輪局部驗證看到的都是已知乾淨的初始狀態，不會被前一輪某個有問題的 handler 悄悄留下的資料汙染，跟 ⑥ 全量驗證的既有行為對稱，不是自己另外發明一套。

### `db.apply_seed()`／`verifier.verify_module()` 必須包 `asyncio.to_thread()`

**`graph.add_node(name, fn)` 對這兩種函式的處理方式完全不同**（`langgraph._internal._runnable.coerce_to_runnable()`，核心行為自早期版本即穩定存在，不是新引入的特性）：

- **`fn` 是普通 `def`（同步）**：LangGraph 自動把它包成 `afunc=partial(run_in_executor, None, fn)`——`graph.ainvoke()` 呼叫到這個 node 時，會透過 `run_in_executor()` 把這次呼叫丟進 asyncio 預設的執行緒池執行，**不會**佔住主事件迴圈。`run_postman_tests()`（`refactor_harness/langgraph_nodes/test_nodes.py`）正是這種寫法——它不需要包 `asyncio.to_thread()`，不是因為「沒有平行手足節點所以阻塞沒差」，而是因為**它是普通 `def`，LangGraph 已經自動幫它做了這件事**。
- **`fn` 是 `async def`**：LangGraph 直接 `await fn(...)`，在目前的事件迴圈上原地執行這個 coroutine，**沒有任何自動保護**。函式內部若呼叫同步阻塞的 `subprocess.run()`／`psycopg2` 呼叫而不自己包 `asyncio.to_thread()`，這段時間事件迴圈會被真正卡住。

`implement_node.run()`（連帶 `_partial_verify()`）必須是 `async def`——`_wait_for_service_reload()`（三章）內部要 `await` httpx 的非同步請求，`_run_one_task()` 要 `await translator_cli.fill_function()`，整個函式沒有維持純同步的空間。這代表 `implement` 與 `run_tests`（⑥）雖然在 graph 拓樸上處於同一種「沒有平行手足節點」的位置，但**在 LangGraph 的分派機制下走的是兩條完全不同的路**：`run_tests` 靠著保持 `def` 換到自動執行緒分派；`implement` 因為非包成 `async def` 不可，換不到這個保護，必須自己動手把阻塞呼叫包起來——「有沒有平行手足節點」根本不是這裡的判斷依據，判斷依據是「這個 node 是不是 `async def`」。

**修正**：`_partial_verify()` 內呼叫 `db.apply_seed()`（帶 `"fixtures/seed.sql"`、`tables_to_truncate=TABLES`）與 `verifier.verify_module()`（帶 `collection_path="postman/collection_readonly.json"`、`module_filter=module`）時，都要各自包一層 `asyncio.to_thread()`，比照 07b `client.py` 對 `git_ops.py`／磁碟呼叫的既有寫法（見九章）。

`_get_reload_probe_id()`／`_wait_for_service_reload()` 本身呼叫的是 `httpx.AsyncClient`，是真正的非同步 I/O，不需要、也不應該包 `asyncio.to_thread()`——那是留給同步呼叫的機制，硬套在已經是 coroutine 的呼叫上型別不合、也沒有意義。

### 為什麼不需要額外的鎖機制：`fill_function()` 寫入與 `_partial_verify()` 驗證在目前的控制流程下不可能重疊

現有（也是本文件延續採用）的 `run()` 是**單一 coroutine 的循序 while 迴圈**，不是多個背景任務各自推進，每一輪固定依序走過四個階段：

```
scheduler.get_ready_tasks() 取得這一輪就緒 task
      ↓
批次 asyncio.gather() 執行所有就緒 task 的 fill_function()      ← 寫入階段，等全部完成才往下走
      ↓
（純記憶體的 mark_task_done() 記帳，不碰磁碟／網路）
      ↓
判斷這一輪要驗證哪些 module（見上方「批次執行」）；有的話，
批次產生一次 token、寫入、等待被回應——整輪只做這一次
      ↓
逐一 for 迴圈跑 module_ready_for_verification()／needs_reverify
兩組驗證（無失敗 task 的 module 才真的呼叫 _partial_verify()）  ← 驗證階段，逐一 await，不是 gather()
      ↓
回到迴圈開頭，下一輪的 get_ready_tasks() 才會發生
```

`asyncio.gather()` 這一步會等這一批就緒 task 的 `fill_function()`**全部**完成才繼續往下走；緊接著的兩個驗證迴圈是逐一 `await`，不是 `asyncio.gather()`，同一時間只有一個 `_partial_verify()` 在跑。下一輪的 `get_ready_tasks()`／新一批 `fill_function()` 呼叫，要等這一輪的兩個驗證迴圈完全跑完才會發生。也就是說「寫入」與「驗證」在這個設計下**結構上不會同時發生**——不是靠鎖擋下來的，是根本沒有任何背景並發：整個 `run()` 只有一條 await 鏈在推進，永遠只處於「正在批次寫入」或「正在逐一驗證」其中一種狀態，兩者不會交錯。

因此**不需要**額外的 Reader-Writer Lock 或「驗證時暫停派發新 task」這類機制——那是在解決一個目前不存在的問題，反而增加一層需要維護、卻沒有對應場景會觸發的複雜度。**這是一條需要刻意維持的不變量，不是巧合**：`09b` 實作或未來任何人「優化」這段迴圈時，若把驗證階段改成 `asyncio.create_task(_partial_verify(...))` 之類不等待就繼續下一輪排程的寫法，或是讓 `asyncio.gather()` 涵蓋驗證呼叫本身，就會破壞這裡描述的序列化保證，届時才需要真的引入鎖機制。

**運行前提**：`_partial_verify()` 要能正確反映 translator-cli 剛寫入的程式碼，Python（FastAPI）服務必須在整個 `implement` node 執行期間持續運行**且能反映磁碟上的最新程式碼**——啟動指令需要帶熱重載，且啟動的目標是上方「單純輪詢不夠」一節描述的 wrapper 模組（`uvicorn _reload_probe_wrapper:wrapper_app --reload`），不是直接指向 `app.main:app`，否則 `/__reload_probe__` 根本不存在。00 五章「環境建立」目前只涵蓋 Java 服務的啟動方式，完全沒有提到 Python 服務何時、如何啟動——**啟動本身**（要不要帶 `--reload`、由誰在什麼時間點 spawn 這個子行程、wrapper 檔案何時寫入 `python_project_path`）仍然是操作前提，留給 00／`main.py` 層級的操作文件補齊（十一章待決定事項）；但**啟動之後、⑤ 執行期間如何應對重啟造成的短暫空窗期**，是 ⑤ 自己排程迴圈裡會反覆踩到的正確性問題（每個 module 完工都要重新面對一次），不能整個丟給操作文件了事，因此上方 `_wait_for_service_reload()` 是本文件的定案範圍，不是待決定事項。

---

## 四、Task／Module 兩層驗證觸發時機：確認，非新設計

02a 十三章已經定案兩層驗證各自的觸發時機與目的，本章只確認現有 `implement_node.py` 的迴圈結構與此對齊，不重新設計：

```
每個 task 完成（translator_cli.fill_function() 回傳）
      ↓
  task 級驗證：ast.parse() 語法驗證，已在 translator-cli 內部完成
  （07a 六章步驟 1/4/10），implement_node 不需要、也不應該重複這件事
      ↓
（同一 module 底下的 task 陸續完成……）
      ↓
scheduler.module_ready_for_verification(module) 為 True
      ↓
  這個 module 底下有沒有 task 失敗（scheduler.task_failed，見三章「批次執行」）？
      ├─ 有 → 不呼叫 Newman，直接 mark_module_verified(module, passed=False)
      │      （行為必然不完整，不需要真的打一次才知道，也避免撞上三章
      │       描述的「失敗 task 復原磁碟」那段還沒穩定的空窗期）
      └─ 沒有 → module 級驗證：_partial_verify()（三章），只跑 readonly golden cases
                 ↓
             Pass → mark_module_verified(module, passed=True)，下游 module 才會被排入就緒佇列
             Fail → mark_module_verified(module, passed=False)
      ↓
  這一輪其餘就緒 task 仍會跑完，不會提早中止 while 迴圈
```

`check_upstream_regression()` 觸發的重驗（`needs_reverify` → 再次呼叫 `_partial_verify()`）沿用同一個函式，不是獨立的第三層——這點 `graph/scheduler.py` 與 `implement_node.py` 現有程式碼已經正確實作，本文件不需要調整。

---

## 五、`context`／`context_files` 補強：ORM `relationship()` 缺失提示與 `_enums.py`

`06a_plan_agent_architecture.md` 七章的 `target_files` 組裝規則、`07a_translator_cli_architecture.md` 五章的 `fill_function()` 輸入契約，都只機械處理「檔案存不存在」層級的問題，沒有涵蓋三個已知、但一直沒有文件真正接手的 prompt 內容缺口。**前兩項補強只發生在 `services`／`repositories` 層**：06a 七章已經定案 `routers` 層方法不納入 `models`（改用 `schemas`），代表 routers 層本來就不該直接碰觸 ORM entity 或其欄位型別，兩項補強對它沒有意義；**第三項（缺口三）不分層級，`routers`／`services`／`repositories` 都要**，見下方。

### 缺口一：ORM `relationship()` 缺失

08a 八章「對下游（⑤／⑥）的連鎖影響，由 09a 承接」明講：③依 `openapi_spec` 產生的 response schema 若期待巢狀結構（如 `Order` 內含完整 `User`），但骨架階段刻意不產生 `relationship()`（只有外鍵純量欄位），⑤填空時若直接假設 ORM 物件會自動帶出關聯屬性（如 `order.user.name`），會在執行期直接 `AttributeError`。這是本專案 ORM model 的結構性限制，不是某個特定任務才會踩到的邊角案例，**必須作為固定提示，不能依賴 [P] 產出的 `task.context` 是否剛好提到這件事**——`task.context` 是 06a 五章 LLM 產出的業務語意補充，不保證涵蓋這種與具體業務邏輯無關、純粹是專案技術棧限制的資訊。

### 缺口二：共用 Enum 定義檔 `app/models/_enums.py`

08a 七章「Enum 欄位」定案 Enum class 一律渲染進單一共用檔案 `app/models/_enums.py`，**不落在任何 `models/{module}.py` 裡**。但 06a 七章 `target_files` 組裝規則表只提到「本 module 的 `models/{module}.py`」，從未提及 `_enums.py`——這代表任何一個 services／repositories 層的 task，只要牽涉到欄位型別是 Enum 的資料（狀態欄位在企業專案極常見，見 08a 四章「同時掃描 `EnumDeclaration`」），context 裡完全看不到這個 Enum 有哪些合法成員（如 `OrderStatus.PENDING`），qwen 只能瞎猜字面值，大幅提高型別與數值幻覺的機率。

### 缺口三：`ResponseResult`／`Result` 這類回應包裝類別的 static／instance 落差（真實環境重跑才發現）

`docs/09b_bug_trace.md`「反覆出現的翻譯模式錯誤」記錄了三個獨立真實案例：`exam_service.py::create_random()` 呼叫 `Result.ok(rs)`（無此方法，應為 `Result().success(rs)`）；`file_router.py::voice()`／`image()` 全部呼叫 `ResponseResult.ok_2(...)`／`ResponseResult.error_4(...)`（缺 `()`）。根因：Java 端 `ResponseResult<T>`／`Result<T>`（`app/services/common_service.py`，`09b_bug_trace.md #49` 的零 endpoint 模組）的 `ok()`／`error()`／`success()` 在 Java 原始碼裡是 `static` 工廠方法（可以直接 `ResponseResult.ok(x)` 呼叫），但④骨架生成階段（05a 四章「多載方法的處理」既有機制）並不區分 Java 的 `static`／instance 修飾詞，一律渲染成帶 `self` 的一般 instance method——⑤ 翻譯時若直接照抄 Java 原始碼的呼叫寫法，在 Python 端會因為缺少 `self` 引數拋出 `TypeError`，必須先實例化才能呼叫（`ResponseResult().ok_2(data)`）。

這不是本文件要修正④骨架生成本身（保留 Java `static`／`@staticmethod` 對應是更完整的根治方案，但範圍已超出本次修正——見 `docs/09b_bug_trace.md`「先不動」的取捨），而是先用跟缺口一同樣的「固定提示」機制緩解：**跟缺口一／二不同，這個提示不分層級**——任何一層（`routers`／`services`／`repositories`）的 task 都可能建構 `ResponseResult`／`Result` 回應（`voice`／`image` 案例正是 `routers` 層），不像 relationship／Enum 缺口只在 `services`／`repositories` 導覽 ORM 資料時才會踩到。

**2026-08-28 延伸出第二種、更嚴重的錯誤模式（見 `docs/09b_bug_trace.md #53`）**：上面談的是「呼叫端」漏打 `()`；真實重跑抓到 `common_service.py::ResponseResult.ok_2()`／`error_2()` 這兩個方法**本體自己**寫成 `return ResponseResult().ok_2(data)`——呼叫自己，無窮遞迴，任何呼叫端都會撞 `RecursionError`（且波及全域例外處理器 `exception_handlers.py::handle_all()`，因為它自己也呼叫 `ResponseResult().error(...)` 組裝錯誤回應，等於「回報錯誤」這個動作也一起崩潰）。這是完全不同的成因（方法本體自己寫錯，不是呼叫方式錯），但同屬「`ResponseResult`／`Result` 怎麼正確實作與使用」這個主題，因此直接延伸 `_RESULT_FACTORY_INSTANCE_METHOD_NOTICE`（不新增獨立常數）：新增一段提醒——若這個 task 正是在實作 `ok_2()`／`error_2()`／`success()`／`failure()` 這些方法本體，禁止在方法內部呼叫自己（或同族方法），必須直接建立新實例、設定欄位後回傳。這個提醒只能防止「未來重新生成」時再犯，這次已經生成、已經卡死整條 pipeline 的 `common_service.py`／`exception_handlers.py` 是直接人工比對 Java 原始碼修正的，不是靠這則提示自動修好。

### 缺口四：每端點專屬 Pydantic 回應型別被誤當成 `ResponseResult` 呼叫（真實環境重跑才發現）

`docs/09b_bug_trace.md #55`：③ 依 OpenAPI spec 為每個端點產生專屬回應 schema（`app/schemas/*.py` 的 `ResponseResultXxxRs`／`ResponseResultXxxRq`，純資料欄位的 Pydantic `BaseModel`，沒有任何方法），跟缺口三談的共用 `ResponseResult`／`Result`（`app/services/common_service.py`，有 `ok_2()`／`error_4()` 等工廠方法）是兩種不同性質的類別，但 ⑤ 翻譯時把「Java 端 `ResponseResult<T>` 呼叫慣例（先建實例再呼叫 `.ok_2()`）」無差別套用到這些型別化物件上——`ResponseResultLanguageRq().ok_2(...)` 這種寫法在 Python 端是 `AttributeError`（型別化物件根本沒有 `ok_2` 這個方法），不是缺口三那種「忘記加 `()`」的 `TypeError`。真實案例橫跨 `exam_router.py`（9 處）／`school_router.py`（7 處），只要一個 task 的回傳型別注解是型別化 schema、又需要組裝業務錯誤碼／成功回應，就可能踩到。

跟這個問題同一批出現、但成因完全獨立的兩個相關缺口（同樣記在 #55／#56）：(1) 部分 `.error_4(ErrorCode.XXX)` 引用的錯誤碼名稱本身是幻覺（如 `ErrorCode.NO_DATA_FOUND`），Java 原始碼與 Python `CommonErrorCode`／`ExamErrorCode` 列舉裡都沒有這個名稱——這代表 ⑤ 有時連「這個業務情境該回哪個具體錯誤碼」都是憑感覺編的，不是照 Java 原始碼查出來的，缺口一／二／三的「固定提示」機制對這類問題無能為力（無法預先窮舉每個業務情境該用哪個錯誤碼常數）；(2) `school_router.py::grades()` 從錯誤的模組（`app.schemas.registration`）匯入了同名但不同 class identity 的 `ResponseResultGetAllGradeRs`——`school`／`registration` 兩個模組各自獨立生成了一份同名 schema（因為 Java 端剛好有兩個不同端點都叫「grades」），③ 各自產出本身沒錯，是 ⑤／④ 讓匯入端匯錯了模組，Pydantic v2 對巢狀 `BaseModel` 欄位做嚴格 class identity 檢查，同名不同源的物件會在執行期直接 `ValidationError`。

**這個缺口目前只在已生成的程式碼上直接修正（比對 Java 原始碼逐一還原正確的類別／錯誤碼／匯入來源），沒有比照缺口一／二／三那樣補一段「固定提示」防止未來重新生成時再犯**——待決是否要開發、以及範圍多大：型別化 schema 該不該有工廠方法本身是一個設計選擇（也可以反過來讓④骨架階段替每個 `ResponseResultXxxRs` 產生對應的 `ok`／`error` classmethod，從根本上讓 ⑤ 的既有呼叫慣例直接可用，不需要靠提示糾正呼叫方式）——這條路線改動範圍比疊加提示大，留待之後評估。

### 疊加規則：`_run_one_task()` 呼叫 `fill_function()` 前補強，不修改 `task_list`

`_run_one_task()` 呼叫 `fill_function()` 之前，先依這個 task 的 `target_files[0]` 判斷所屬層級（路徑前綴 `app/repositories/` → repositories、`app/services/` → services、其餘 → routers，跟 06a 四章「module 歸屬判定」用的是同一種路徑前綴判斷方式，只是這裡判斷的是層級不是 module）。只有 services／repositories 層才疊加缺口一／二：`context_files` 若還沒包含 `app/models/_enums.py` 就加入；`context` 附加上缺口一描述的固定提示文字（若 `task.context` 原本非空，接在後面，中間空一行分隔；原本是空字串就直接整段替換）。這一層疊加**不修改 `task` 本身**——`task_list` 是 [P] 的權威輸出，⑤ 不應該就地竄改，只在傳給 `fill_function()` 的 `context`／`context_files` 這兩個引數上疊加。固定提示文字的內容：

> 本專案的 SQLAlchemy model（`app/models/{module}.py`）只有外鍵純量欄位，沒有 `relationship()` 物件導覽屬性（見 `08a_scaffold_agent_architecture.md` 八章）。禁止用「`.關聯屬性`」的方式取得關聯物件（例如 `order.user` 這種寫法一定會在執行期拋出 `AttributeError`）；需要關聯資料時，改用額外的 repository 查詢，或在這個函式內手動用外鍵欄位值另外查詢、自行組裝回傳結構。

**`_enums.py` 無條件加入，不做存在性判斷**：比照 06a 七章「`models/{module}.py` 為什麼是無條件加入」的既有慣例——這個專案是否有任何 Enum 欄位、因此這個檔案存不存在，`_run_one_task()` 這一層無法（也不需要）預先判斷，缺檔案的情況交給 07a 七章「`context_files` 讀取容錯」既有機制（`FileNotFoundError` 記警告、跳過，不中斷任務），不是新規則。

**缺口三的提示不分層級、無條件疊加給每一個 task**（在缺口一／二的 if/else 分支之外另外附加，兩者互不影響）：

> `ResponseResult`／`Result` 這類回應包裝類別（`app/services/common_service.py`）的 `ok()`／`error()`／`success()` 等方法在 Python 端是 instance method（帶 `self`，不是 Java 原始碼裡的 `static` 工廠方法），呼叫前必須先建立實例，例如 `ResponseResult().ok_2(data)`、`Result().success(data)`——不要直接寫成 `ResponseResult.ok_2(data)` 或 `Result.success(data)`，那樣在 Python 會因為缺少 `self` 引數而拋出 `TypeError`。

原本 `task.context` 若已經因為缺口一被替換／附加，這則提示接在後面（同樣空一行分隔）；`task.context` 原本就是空字串、且不屬於 services／repositories 層（因此缺口一／二完全不適用）時，這則提示就是 `context` 唯一的內容。

---

## 六、Task 失敗根因追蹤與提前排除：`skipped_interfaces`

### 問題

`failed_tasks` 目前只存 task id，`FillResult.error` 完全被丟棄，且無法分辨兩種截然不同的失敗原因：

1. **④ 骨架缺口**：`(target_file, class_name, function_name)` 從未被 `generate_scaffold()` 渲染出來（`08a_scaffold_agent_architecture.md` 九章「逐 entity 隔離失敗」或 07a 四章「語法驗證與寫入」跳過了這個 `InterfaceSpec`）。`fill_function()` 在 07a 六章步驟 1／4 會快速失敗（不會呼叫 ollama），回傳 `error="scaffold/task 不一致：..."`。**這種失敗在這一整條 graph run 裡是永久性的**：`skipped_interfaces` 由 `scaffold` node 一次性寫入（見 01 五章，`scaffold` 只執行一次，不在 `retry_count` 迴圈內），不會因為 `debug → implement` 重新進入而改變，⑤ 沒有能力無中生有出一個骨架階段就沒建立的函式
2. **翻譯品質問題**：函式確實存在，但 qwen 生成的內容經 translator-cli 自己的重試後仍然失敗（07b 十一章記錄的真實案例）。這種失敗**可能**在下一輪 `debug → implement` 重試後修好（⑦ Debug Agent 未來或許能調整 `task.context` 或提示人工介入）

08a 十二章已經明講這個判斷邏輯「屬於 09a／10a 的範圍」，本章把「比對」定案在 09a——09a 掌握 task 排程的第一手時機，比 10a 事後回溯更直接。

### 決策：新增 `RefactorState.task_failures`

```python
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
    # Agent ⑤（逐 task 累積寫入，需要 reducer）：task 失敗時的錯誤訊息與
    # 根因分類，供 ⑦ Debug Agent（10a，待建立）不需要重新比對 skipped_
    # interfaces 就能分辨「④骨架缺口」與「翻譯品質問題」。歷史累積，不因
    # 後續重試成功而移除——一筆 task 若第一次失敗、重試後成功，這筆記錄
    # 仍保留（同時 task_id 也會出現在 completed_tasks），供除錯追溯。
    task_failures: Annotated[list[TaskFailure], operator.add]
```

**既有的 `RefactorState.failed_tasks` 不廢棄，但語意跟 `task_failures`不同、範圍更窄**：`failed_tasks`（`Annotated[list[str], operator.add]`，01 三章既有欄位）繼續由 `run()` 寫入，內容維持原樣——只收「這一輪真的被送進 `_run_one_task()`、`fill_function()` 回報失敗」的 task id，不含 `scaffold_skipped` 的 task（那些從一開始就被提前排除、根本沒送進 `_run_one_task()`，見下方）。`task_failures` 是這兩種失敗（`scaffold_skipped`＋`fill_failed`）合併起來、每筆都帶完整脈絡（`module`／`file_path`／`class_name`／`function_name`／`error`）的完整記錄，範圍是 `failed_tasks` 的超集。兩者不是互相替代的關係：`failed_tasks` 保留給 `main.py`（01 九章既有的收尾列印 `Failed Tasks: {final_state.get('failed_tasks')}`）這種只需要「這一輪哪些 task id 沒過」的粗粒度用途，不需要為了新增 `task_failures`而改動這行既有程式碼；需要完整根因（`error` 內容、是不是 scaffold 缺口）的場合一律讀 `task_failures`，不是回頭放大 `failed_tasks` 的欄位。**是否要讓 `main.py` 的收尾列印也一併印出 `task_failures`**，屬於錦上添花的資訊呈現改動，列入十一章待決定事項，不影響本文件其餘設計。

### 提前排除：scaffold 缺口在排程階段就永久跳過，不等 `fill_function()` 失敗才發現

一個 task 的「目標三元組」定義為 `(target_files[0], class_name, function_name)`（`class_name` 用 `task.get("class_name")`，routers 層是 `None`）。判斷某個 task 是不是 scaffold 缺口，就是拿這個三元組去比對 `skipped_interfaces` 清單裡每一筆的 `(file_path, class_name, function_name)`，命中即為是——精確 tuple 相等比對，不做模糊匹配（三元組比對本身的正確性見下一節）。

`run()` 開頭（建構 `ModuleScheduler` 之前）依這個判斷跑一次 `state["task_list"]`，篩出所有命中的 task，收集成 `scaffold_gap_task_ids`（供七章 `already_failed` 使用）。同一批命中的 task，對照 `state.get("task_failures", [])` 裡已經記錄過的 `task_id` 集合，只把**這一次新出現**的（`task_id` 尚未出現在既有 `task_failures` 裡的）組成 `TaskFailure` 記錄（`reason="scaffold_skipped"`、`error` 固定寫「④ 骨架階段未渲染此函式，見 skipped_interfaces」）——避免同一個 scaffold 缺口在每一輪 debug 迴圈重入時被重複記錄：`skipped_interfaces` 整條 run 不會變，`scaffold_gap_task_ids` 每輪重算出來的結果也會是同一批，只需要在第一次發現時記進 `task_failures`。

`_run_one_task()` 之後、收集這一輪結果時，`result.success is False` 才用同一套目標三元組組出 `TaskFailure`（`reason="fill_failed"`、`error` 帶 `result.error`）——理論上不該在這裡再次命中「這是 scaffold 缺口」的判斷（已經在排程階段被提前排除、根本不會被送進 `_run_one_task()`），但仍然沿用同一個組裝邏輯，只是為了保留一條防線：若 `skipped_interfaces` 與 `task_list` 因為某種上游 bug 出現不一致（如 scaffold 記錄的三元組跟實際 task 對不上），這裡仍然會如實記下 `fill_function()` 自己回報的 `error`，只是 `reason` 會被誤標成 `fill_failed`——這種不一致本身已經是比「分類錯誤」更嚴重的資料問題，不是 ⑤ 這一層該吸收的。

**同一個 task 的 `fill_failed` 紀錄可能在不同輪次各留一筆，這是刻意保留的歷史，不是需要去重的雜訊**：`scaffold_skipped` 有 `already_recorded` 去重，是因為 `skipped_interfaces` 整條 run 不會變，同一個 task 每輪重算出的結果、`error` 文字必然逐字相同——留下第二筆只是在浪費空間複製同一件事。`fill_failed` 不是這種情況：七章已經修正成翻譯品質失敗會在下一輪 `debug → implement` 重新排程，同一個 task 若連續幾輪都失敗，qwen 每次實際生成的內容、`FillResult.error` 的具體訊息完全可能不同（例如第一輪是 delimiter 格式問題、第二輪換成別的語法錯誤）——保留每一輪各自的記錄，才能讓 10a（⑦ Debug Agent）之後回溯「這個 task 到底試過幾次、每次卡在哪裡」，先驗地去重反而會丟掉這個資訊。這裡的資料量本身也有 `retry_count` 的既有上限自然收斂（02b `MAX_RETRY = 3`，見 02b `test_nodes.py`）：同一個 task 在單一 graph run 裡最多只會經歷 `MAX_RETRY` 次 `debug → implement` 重入，`fill_failed` 紀錄不會無限增長，10a 讀取時依 `task_id` 分組看待即可，不需要假設每個 task 只會出現一次。

「提前排除」如何接進排程器，見七章。

### 三元組比對的正確性：`class_name` 不存在 `None`／空字串不一致的風險

上面的判斷用 `(file_path, class_name, function_name) == target` 這種精確 tuple 比對，前提是 `skipped_interfaces` 與 `TaskSpec` 兩邊的 `class_name` 對同一個 routers 層函式，必須是同一種值（Python `None`），不能一邊是 `None`、一邊是空字串 `""`。這個前提在目前的實作鏈路上是成立的，不是假設——追過三個環節的原始碼確認：

1. `design_agent/design.py`（05a）：`class_name=None if layer == "routers" else ctx.class_name`——routers 層的 `InterfaceSpec.class_name` 明確賦值成 Python `None`，不是空字串
2. `translator_cli/scaffold.py`（07a／07b）：`skipped_interfaces` 的每一筆紀錄是 `"class_name": iface["class_name"]`，直接原樣複製上面這個 `InterfaceSpec.class_name` 的值，沒有經過任何字串化或序列化轉換
3. `plan_agent/planning.py`（06a）：`TaskSpec.class_name` 的值來自 `class_name=iface["class_name"]`（第 243 行），這個 `iface` 是 `own_ids.get(iid)`——`own_ids` 是用 `interface_id()` 編碼字串當 key、**原始 `InterfaceSpec` 物件**當 value 的字典（見第 180～182 行）。LLM 回應裡確實只會看到 `interface_id`（`plan_agent/prompts.py` 明講：為了不讓 output schema 處理 nullable 型別，`interface_id()` 把 `class_name` 為 `None` 的情況編碼成空字串——但這個編碼**只存在於 LLM 看到、回傳的那個字串**裡，`planning.py` 事後只拿這個字串當查找鍵，實際寫進 `TaskSpec.class_name` 的值一律是查表查回來的原始 `InterfaceSpec.class_name`，從未對 LLM 回傳的字串做任何解碼還原——不存在「LLM 只能吐出字串、所以被迫把 `None` 存成空字串」這個風險，因為 `class_name` 這個欄位的最終來源根本不經過 LLM 的輸出

`target_files[0]` 是不是可能被誤傳成 context 檔案（而非真正的寫入目標）也一併確認過：06a 七章「`target_files` 組裝規則」明訂「自己的 `file_path`」機械固定放 `target_files[0]`，這是 `graph/scheduler.py` 的 `module_owned_files`、`_run_one_task()` 的 `target_file = task["target_files"][0]` 早已依賴的既有不變量，不是本文件才引入的新假設。

因此這個判斷維持精確 tuple 比對，不需要額外的正規化或防禦性型別斷言——加上這類程式碼在這個具體情境裡屬於「為不會發生的情況寫錯誤處理」，不符合本專案一貫的簡化精神。

### 沒有採用的方案：`skipped_db_models` 的提前排除

**評估過、但刻意不做**的另一個提前排除方向：08a 九章的 `skipped_db_models` 記錄了另一種骨架缺口——某個 `app/models/{module}.py` 底下的 entity class（如 `User`）因為缺 `@Id`、語法驗證失敗等原因整個被跳過，而某個 repository／service task 的函式簽名回傳型別剛好是這個被跳過的 class。理論上這種 task 即使 `fill_function()` 語法上能成功寫入（qwen 只是照著簽名寫程式碼，不會知道 `User` 這個型別其實沒被渲染出來），最終在 Harness 局部驗證階段也大概率因為 `NameError`／import 失敗而過不了，是不是也該比照 `skipped_interfaces` 提前排除？

**沒有採用的原因**：`skipped_interfaces` 與這裡的 `skipped_db_models` 是兩種粒度、可靠度都不同的訊號。`skipped_interfaces` 直接對應 `(file_path, class_name, function_name)` 三元組，跟 `TaskSpec` 的目標**完全等價**——命中就是命中，沒有模糊地帶。`skipped_db_models` 只到「哪個檔案的哪個 class 被跳過」，`TaskSpec` 本身完全不攜帶「這個函式的回傳型別字串裡有沒有出現這個 class 名稱」這項資訊（那是 `python_structure.interfaces[*].return_type`／`params[*].type` 才有的內容，`TaskSpec` 沒有）。要做這個比對，必須另外回頭查 `python_structure.interfaces` 找出對應的 `InterfaceSpec`、再對 `return_type`／`params` 做型別字串比對——07a 五章「自訂型別索引」一節已經明講這類比對「巢狀深度不固定時字串規則無法窮舉」，必須用 AST 而非字串裁切，這已經超出「提前排除」這一步該有的複雜度，且就算做了，也只能判斷「target_files 裡有沒有出現這個檔名」這種粗略關聯，不能保證這個 task 的函式**真的用到**那個被跳過的 class（`target_files` 本來就含大量唯讀 context 檔案，見 06a 七章）——用不精確的訊號提前把一個 task 判定成永久失敗，錯誤的代價（一個原本可能寫得出正確程式碼的 task 被錯誤剝奪重試機會）比「多花一次模型呼叫才發現這個問題」更高。

因此 `skipped_db_models` 不進這個提前排除的判斷範圍，這類失敗會被正常送進 `fill_function()`、如果失敗就走六章既有的 `"fill_failed"` 分類（如果 `fill_function()` 本身成功、只是後續驗證失敗，則完全不會出現在 `task_failures` 裡，只會反映在 `_partial_verify()` 的 report 上）。08a 十二章已經明訂 `state["skipped_db_models"]`「就是唯一能回溯這個 class 當初為什麼沒被渲染出來的地方」，供 10a（⑦ Debug Agent）診斷模組失敗原因時直接讀取、交叉比對 report 的 `related_files`，不需要 09a 額外做一層不可靠的預先分類。

---

## 七、Debug 迴圈重試語意：`already_failed` 的正確來源

### 問題

現有 `implement_node.run()` 這樣建構排程器：

```python
already_failed=set(state.get("failed_tasks", [])),   # ← 問題所在
```

`ModuleScheduler.get_ready_tasks()` 排除任何 `task_id in self.task_failed` 的 task。但 `state["failed_tasks"]` 掛的是 `operator.add` reducer，是**整條 graph run 從頭到尾累積的歷史清單**。`debug → implement` 每次重新進入都會重新建構一個全新的 `ModuleScheduler` 實例，若把這份歷史清單當成建構參數傳入，等於告訴新的排程器「這些 task 永遠不准再被排程」——一個 task 只要失敗過一次（不論是翻譯品質問題還是 scaffold 缺口），之後所有的 `debug → implement` 重入都不會再嘗試它，即使 07a 五章「冪等：允許對已有內容的函式重新填空」明確設計 `fill_function()` 支援重新呼叫。這正是 07b 十一章記錄的「task 永久失敗後如何交給 ⑦ Debug Agent 重試」缺口的根因。

### 修正：`already_failed` 改用六章算出的 `scaffold_gap_task_ids`，不是 `state["failed_tasks"]`

建構 `ModuleScheduler` 時，`already_completed` 引數維持原樣、繼續傳 `set(state.get("completed_tasks", []))`——成功完成的 task **應該**永久排除，跟 `already_failed` 的語意完全相反，不能套用同一種修法。`already_failed` 引數改傳六章算出的 `scaffold_gap_task_ids`，不是 `state["failed_tasks"]`：`already_failed` 只該標記「這一整條 graph run 都不會改變的永久失敗」——`scaffold_gap_task_ids` 每輪都從 `skipped_interfaces`（scaffold 只執行一次，這份清單不會變）重新算出，結果穩定；`state["failed_tasks"]` 是持續累加的歷史清單，混進去會把「這次還沒重試過的普通失敗」也當永久排除。`ModuleScheduler` 自己在這一輪迴圈內透過 `mark_task_done(success=False)` 記錄「這一輪已經試過、先跳過」，足以避免同一輪內對同一個失敗 task 反覆重試，不需要外部歷史狀態補這一塊。

**這個修正同時解決兩件事**：(1) 翻譯品質問題造成的失敗，`debug → implement` 重入後會被重新排程，不再永久卡死；(2) scaffold 缺口造成的失敗，不會在每一輪都被重新送進 `fill_function()`、浪費一次本地模型呼叫——`scaffold_gap_task_ids` 每輪重算的結果必然包含它，`already_failed` 因此持續排除它，直到（理論上不會發生，因為 `scaffold` 不在重試迴圈裡）`skipped_interfaces` 本身改變為止。

**這個修正不影響 `task_done`（成功）的正確性，也不影響 regression 偵測**：`check_upstream_regression()` 只依賴 `module_owned_files`（從 `task_list` 靜態算出，與 task 成敗無關）與當下的 `module_status`，跟 `task_failed` 集合無關。

### 本節後續被 10a 推翻的部分：`fill_failed` 不該「每輪都當新 task 重排」

**這是本文件的正式修正，補記一個真實環境重跑才發現、且橫跨 09a／10a 兩份文件才看得出全貌的落差**：上面「這個修正同時解決兩件事」第 (1) 點——「翻譯品質問題造成的失敗，`debug → implement` 重入後會被重新排程」——在 10a 落地、⑦ Debug Agent 開始接手除錯迴圈之後，**已經不再是正確的行為**，但這一節從未回頭修正。原本的推理成立的前提是：這一節定案時（09a 階段）還沒有 ⑦ 可以接手，「重新排給 ⑤ 本地模型再試一次」是當時唯一可用的重試手段，`scaffold_gap_task_ids`／`fill_failed` 的區分只是為了不要把 scaffold 缺口也白白重排。10a 後來訂下「⑤ 的本地模型一旦進入除錯迴圈就完全退出」（見 10a 一章、八章），這條原則邏輯上必然涵蓋 `fill_failed`——但 10a 落地時只顧著接上「⑦ 產生 `fixed_body` 時如何繞過本地模型」（`force_reschedule()`），沒有回頭檢查「`fill_failed` 沒被 ⑦ 特別處理時，排程器預設還是會怎麼做」，兩份文件的決策因此沒有真正對齊。

**真實後果**：2026-08-27 真實環境重跑（run_id `20260827_015722_639ebd`）證實，`app/services/exam_service.py` 底下 6 個 task 連續 3 輪、每輪最多 3 次 attempt，9/9 全部透過 Ollama 重試、全部失敗——不是因為 ⑦ 沒被要求處理它們（10a 四章另有一條獨立修正處理這一半），是因為**排程器每一輪重建時，只要這個 task_id 不在 `scaffold_gap_task_ids` 裡，就會被當成全新、可以再排給 ⑤ 的 task**，跟 ⑦ 那一輪到底有沒有分析到它完全無關——即使 10a 那條修正生效、⑦ 完全不管這個 task，排程器照樣會把它送回 Ollama。

**修正**：`already_failed` 引數改傳 `scaffold_gap_task_ids | fill_failed_task_ids`（`fill_failed_task_ids` 是 `state["task_failures"]` 裡 `reason=="fill_failed"` 的 task_id 集合，逐輪重算，反映到目前為止累積的翻譯失敗歷史）——`fill_failed` 從此比照 `scaffold_gap` 同一種「機械永久排除、只由更高權限機制解除」的處理方式；唯一能讓它重新被排到的路徑是 `force_reschedule()`（10a 八章，只由 ⑦ 產生的 `pending_fixed_bodies` 觸發）。**這不是走回頭路、回到本節開頭「`state["failed_tasks"]` 永久排除」那個被推翻的舊版本**：差別在於解除排除的權限——舊版本沒有任何機制能讓一個失敗的 task 重新被排到；這次的修正把「解除排除」的權限從「排程器每輪自動重試」收斂成「只有 ⑦ 明確給出修正時才解除」，跟 `scaffold_gap_task_ids` 目前的既有處理方式（永久排除、無解除機制，因為 scaffold 缺口本來就不會自己變好）是同一個光譜上的兩個點，不是同一個 bug 重演。

**這條修正在時序上天然只影響「已經進入除錯迴圈」的輪次，不需要額外的條件判斷**：`fill_failed_task_ids` 直接從 `state["task_failures"]` 算出——一個 task 在還沒被嘗試過的第一輪，`task_failures` 裡不會有它的記錄，`fill_failed_task_ids` 自然不包含它，排程行為與修正前完全相同（正常送 ⑤ 本地模型）；只有在它**已經**失敗過、留下記錄之後，才會被這條規則排除，不需要另外判斷「這是不是第一輪」。

完整設計見 `10a_debug_agent_architecture.md` 四章「`known_fill_failures`：一旦失敗過一次，不再退回本地模型」；程式碼見 `graph/nodes/implement_node.py::run()`（`fill_failed_task_ids` 計算）；單元測試見 `tests/graph/test_implement_node.py::TestFillFailedTasksExcludedFromLocalModelRetry`。

**`already_failed` 目前只餵 `scaffold_gap_task_ids`，不代表這是唯一能餵給它的東西**：`ModuleScheduler.__init__` 收的是一個普通 `set[str]`，不是專屬於「scaffold 缺口」這個概念的型別——未來若 10a（⑦ Debug Agent）判斷某個 task 屬於另一種永久性、重試也無法修復的失敗（如需要一個目前拿不到的外部依賴），要讓排程器同樣永久跳過它，只需要在建構 `already_failed` 時把 `scaffold_gap_task_ids` 跟 10a 那時候定義的任何一個 task id 集合取聯集即可，`ModuleScheduler` 這一層完全不需要改動。這裡不預先新增一個 `permanently_failed_tasks` 之類的 `RefactorState` 欄位——10a 目前還沒建立，這種永久失敗判斷該用什麼資料形狀（單一 task 粒度？附原因？跟 `task_failures` 合併還是分開？）都還沒有答案，09a 猜一個形狀出來，等 10a 真正設計時多半要重改，不如等 10a 定案時再由那份文件決定怎麼併入這個既有的聯集點。

### 100% 由 `scaffold_gap_task_ids` 覆蓋的 module，必須提前標記，否則永遠不會被驗證、甚至讓整條 pipeline 無限迴圈

**這是上面那個修正（`already_failed` 提前排除 scaffold 缺口）帶出的連帶情況**：`ModuleScheduler.get_ready_tasks()` 只從 `tasks_by_module` 裡挑出「不在 `task_done`、也不在 `task_failed`」的 task；若某個 module 底下**所有** task 一開始就已經全部落在 `scaffold_gap_task_ids`（08a 九章「逐 `InterfaceSpec` 隔離失敗」明確允許整批介面失敗這種情況發生，不是理論上不可能），這個 module 的 task 就永遠不會出現在任何一輪的 `ready` 清單裡——連帶它也永遠不會出現在 `touched_modules` 裡（`touched_modules` 只從這一輪真的執行過的 task 反推），`scheduler.mark_module_verified()` 因此永遠不會被呼叫到它身上，它的 `module_status` 永遠停在初始值 `"pending"`。

**後果不只是「這個 module 沒被驗證」，而是可能讓整條 pipeline 卡進無限迴圈**：`run()` 結尾把 `module_status == "pending"` 的 module 歸進 `blocked_modules`，不是 `failed_modules`（見 `run()` 收尾那兩行既有邏輯）。`should_debug_or_done()` 的既有規則是「只有 `blocked_modules` 非空、`failed_modules` 為空時，不消耗 `retry_count`」——如果這個 100% scaffold 缺口的 module 剛好是這一輪唯一有問題的 module（其餘 module 都正常通過），`failed_modules` 會是空的，`retry_count` 永遠不會增加，`debug → implement` 會無止盡地循環下去，`give_up` 永遠不會被觸發。這不是「下游被卡住」這種局部影響，是整條 graph run 真正的死結。

**修正**：`run()` 建構 `scheduler` 之後、進入 while 迴圈之前，額外掃一次每個 module：若這個 module 底下的 task 全部都在 `scaffold_gap_task_ids` 裡，直接呼叫 `scheduler.mark_module_verified(module, passed=False)`，不等它自然出現在某一輪的 `touched_modules` 裡。這樣它的 `module_status` 會正確落在 `"failed"`，`run()` 收尾時會正確歸進 `failed_modules`（不是 `blocked_modules`），`should_debug_or_done()` 就能正確判斷「有東西真的壞了」，`retry_count` 會照常遞增，最終在 `MAX_RETRY` 用盡後正常走到 `give_up`，不會無限循環。這個掃描只需要在 while 迴圈開始前做一次——「一個 module 的 task 是否 100% 落在 `scaffold_gap_task_ids`」是靜態事實，`scaffold_gap_task_ids` 整條 run 不會變、module 的 task 組成也不會變，不需要每一輪重新檢查。這個判定同時也要記一筆 `partial_reports`（`{"status": "fail", "reason": "module_entirely_scaffold_skipped"}`，比照三章 `batch_reload_timeout` 那筆的做法，用明顯的 `reason` 跟真正跑過 Newman 的結果區分開來），不能因為沒有真的呼叫 `_partial_verify()` 就完全不留紀錄——不然 10a 事後回溯會看到一個「狀態是 failed，卻沒有任何說明」的 module，一樣難以判讀。

### 沒有採用的方案：在條件邊層級提早繞過整條 debug 迴圈

**評估過、但刻意不做**的更激進版本：`scaffold_skipped` 造成的模組失敗，理論上不可能透過 `debug → implement` 重試修好（要修好必須重新跑 `scaffold`，但目前的 graph 結構沒有 `debug → scaffold` 這條回頭邊）。若能精確判斷「這一輪 `failed_modules` 全部都是由 scaffold 缺口造成、沒有任何一個 module 還有翻譯品質問題待修」，理論上可以讓 `should_debug_or_done()` 提早路由到 `give_up`，省下剩餘的 `retry_count` 預算與 ⑦ Debug Agent 的 API 呼叫成本。

**沒有採用的原因**：`should_debug_or_done()` 是**全域**路由決策——若只因為某一個 module 有 scaffold 缺口就讓整條 pipeline 提早棄守，會連帶放棄其他仍然可能修好的 module。要做對這件事，必須精確到「每一個 `failed_modules` 都完全由 scaffold_skipped 造成，沒有任何一個 module 混雜了未被 `task_failures` 記錄到的業務邏輯問題」——但業務邏輯問題往往是「`fill_function()` 回報成功、`task_failures` 完全沒有記錄，卻在 Harness 局部驗證階段才發現輸出不對」，這種失敗不會出現在 `task_failures` 裡，用「這個 module 名下的 `task_failures` 是不是全部 `scaffold_skipped`」當判斷依據，容易在最常見的情境（成功填空但業務邏輯寫錯）誤判成「已經沒有可修的東西了」，錯誤地提早放棄一個其實仍然可修的 module。這個判斷需要更完整的資料（例如逐 module 追蹤「這次局部驗證的 failure 有沒有命中含 scaffold 缺口的那支函式」），而 10a（⑦ Debug Agent）目前還沒建立、沒有真實案例可以驗證這個判斷邏輯的正確性，貿然實作風險高於效益，因此只做了「不再浪費 ollama 呼叫」這個確定安全的部分（上方），完整的路由優化留給 10a 一併評估（見十一章）。

---

## 八、模組結構：不需要新套件

`plan_agent/`／`design_agent/`／`scaffold_agent/` 各自是獨立套件，因為它們各自有實質的領域邏輯需要封裝。⑤ 沒有對應的領域邏輯——`ModuleScheduler` 是純排程演算法，已經封裝在 `graph/scheduler.py`；`fill_function()` 的填空邏輯屬於 `translator_cli/`；`verify_module()` 的比對邏輯屬於 `refactor_harness/`。`implement_node.py` 剩下的工作是「串接排程器、呼叫兩個既有套件、組裝 state 回傳值、疊加 context」，這是**膠水邏輯**，比照 04a／05a／06a／08a 對「node 本身盡量薄」的一貫原則（見 01 二章「設計原則」），維持現狀（`graph/nodes/implement_node.py` ＋ `graph/scheduler.py`），不新增 `implement_agent/` 套件。

---

## 九、與 `RefactorState`／既有程式碼的介面異動

### `graph/state.py`：新增 `TaskFailure`／`task_failures`

見六章完整型別定義，插入位置比照 `completed_tasks`／`failed_tasks`／`partial_reports` 同一組「Agent ⑤（逐 task 累積寫入，需要 reducer）」欄位群組。

### `graph/nodes/implement_node.py`：七處異動

1. 模組層級新增 `TABLES` 讀取（比照 `refactor_harness/langgraph_nodes/test_nodes.py` 既有寫法）、`SERVICE_READY_TIMEOUT_SECONDS`／`SERVICE_READY_POLL_INTERVAL_SECONDS` 兩個環境變數常數
2. 新增 `_get_reload_probe_id()`（容忍連線暫時被拒絕、重試到任何一次成功回應為止，`run()` 開頭的前置確認用，見三章）、`_wait_for_service_reload()`（三章「決定性的同步屏障」，內部依序「產生新 token → 寫入 `_reload_token.py` → 輪詢直到端點回應等於這個 token」，不需要呼叫端傳入任何基準值）、`_augment_task_io()`（五章）、`_target_of()`／`_is_scaffold_skipped()`／`_make_task_failure()`（六章）
3. `_run_one_task()` 呼叫 `fill_function()` 的 `context`／`context_files` 改用 `_augment_task_io(task)` 的回傳值
4. `run()` 開頭建構真正的 `DbEnvironment`／`GoldenVerifier`；只有在 `state.get("completed_tasks")` 與 `state.get("task_failures")` 都是空的（真正第一次進入，見三章「debug 重入時必須跳過」）才呼叫 `_get_reload_probe_id()` 確認服務這時候有在跑，失敗即不吞、往上拋（見十章）——`debug → implement` 重入時整段跳過，不呼叫這個函式；建構 `ModuleScheduler` 前算出 `scaffold_gap_task_ids`／`new_scaffold_gap_failures`；`already_failed` 引數改傳 `scaffold_gap_task_ids`（七章）
5. `ModuleScheduler` 建構完成後、進入 while 迴圈之前，新增一段掃描：module 底下所有 task 若 100% 落在 `scaffold_gap_task_ids`，直接呼叫 `scheduler.mark_module_verified(module, passed=False)` 並記一筆 `partial_reports`（`reason="module_entirely_scaffold_skipped"`），不等它自然出現在 `touched_modules` 裡（七章「100% 由 `scaffold_gap_task_ids` 覆蓋的 module」——不做這一步會讓這個 module 永遠停在 `"pending"`、被歸進 `blocked_modules`，在沒有其他 `failed_modules` 的情況下讓 `debug → implement` 無限循環）
6. 兩個驗證迴圈（一般完工觸發、regression 重驗觸發）改成：先查 `scheduler.task_failed` 判斷這個 module 底下有沒有失敗 task——有就直接 `mark_module_verified(module, passed=False)`、跳過 Newman；沒有才批次呼叫一次 `_wait_for_service_reload()`（整輪只呼叫一次，不是每個 module 各自呼叫）確認伺服器已載入這一輪的寫入，再呼叫 `_partial_verify()`（三章「批次執行」）；`_wait_for_service_reload()` 逾時時就地把這一輪要驗證的每個 module 都標記失敗，report 帶明顯區別於 Newman 比對結果的 `reason` 標記（三章「逾時不該讓整條 pipeline 崩潰」），不讓例外往外傳；`_partial_verify()` 簡化成三章的最終實作（`async def`，內部 `db.apply_seed()`／`verifier.verify_module()` 都包一層 `asyncio.to_thread()`，理由見三章「必須包 `asyncio.to_thread()`」）
7. `run()` 迴圈內收集 `task_failures`（六章），最終回傳值新增 `"task_failures": new_scaffold_gap_failures + [這一輪 fill_failed 的記錄]`

### `main.py`：`initial_state` 新增 `task_failures: []`

比照其餘 `Annotated[list, operator.add]` 欄位在 `initial_state` 顯式初始化成空清單的既有慣例（01 九章「顯式全部初始化」）。

### 新增：`_reload_probe_wrapper.py`（不改動任何既有文件）

三章「熱重載競態」的探測端點是獨立的 ASGI wrapper，**不需要**異動 05a／05b（`design_agent/layout.py` 的 `render_main_py()` 維持原樣）。新增的 `_reload_probe_wrapper.py` 是固定內容、不依賴任何 Agent 輸出的樣板檔案，性質上比較接近 07a 二章「一次性前置準備」（`python_project_path` 需要先 `git init` 過的空 repo）這類環境準備步驟，不是某個 Agent 動態產生的內容——實際寫入 `python_project_path` 的時機、由誰負責，留給十一章「Python 服務啟動機制」那個既有待決定事項一併收斂，不獨立另開一項。

**這項一次性準備還必須包含 `python_project_path/.gitignore`**（見三章「必須排除在 07a 九章的衝突偵測之外」）：內容至少含 `_reload_probe_wrapper.py`／`_reload_token.py` 兩個檔名，建立後先 `commit` 一次，才能放進 wrapper 檔案，否則會讓 `generate_scaffold()` 自己第一次執行的 precondition 檢查（07a 九章）就失敗。這不需要改動 `translator_cli/git_ops.py` 的 precondition 檢查邏輯本身——`check_clean_working_tree()` 檢查的是「working tree 是否乾淨」，不是「commit 數量是否為零」，多一個記錄 `.gitignore` 的初始 commit 完全相容。

---

## 十、錯誤處理範圍

比照 04a／05a／06a／07a／08a 的既有邊界：`retry_count` 迴圈只包住 `implement → run_tests → debug`。

- `translator_cli.fill_function()` 呼叫失敗（含其自身網路層／delimiter 重試耗盡後）：不在 `implement_node` 這一層重試，直接反映成 `FillResult(success=False)` → task 標記失敗、記入 `task_failures`（六章）→ 該 module 因為 `scheduler.task_failed` 非空直接判定沒過，不再打 Newman（三章）→ 交由既有的 `should_debug_or_done()` 判斷是否進入 `debug` 消耗 `retry_count`，本文件不新增另一層重試機制
- scaffold 缺口造成的失敗：一次性記入 `task_failures`（`reason="scaffold_skipped"`），之後每一輪都被 `already_failed` 排除，不再嘗試呼叫 `fill_function()`（七章）——但仍會走完整個 `retry_count` 預算才 `give_up`（沒有提早短路，理由見七章「沒有採用的方案」）
- `run()` 開頭 `_get_reload_probe_id()` 的前置確認逾時（已經容忍過暫時連不上、重試一段時間仍然沒有任何成功回應）：**只在真正第一次進入 `implement`（`completed_tasks`／`task_failures` 皆空，見三章）時才會執行到這個檢查**，此時不吞、直接往上拋，中止整條 `graph.ainvoke()`——這個時間點還沒有任何一次 `fill_function()` 寫入發生過，重試過後仍連不上代表服務打從一開始就沒被正確啟動，是操作前提沒滿足，不是可以透過 `retry_count` 迴圈自動解決的情況。`debug → implement` 重入時完全跳過這個檢查（不會執行到、也就不會拋出這個例外），服務目前連不上與否交給迴圈內的批次 token 等待機制處理
- `run()` 迴圈內批次呼叫的 `_wait_for_service_reload()`（三章「批次執行」）逾時：**不吞，但也不往外拋**——就地把這一輪原本要驗證的每個 module 都標記失敗（附上區別於 body diff 失敗的說明文字），讓 while 迴圈照常往下走，交由既有的 `should_debug_or_done()`／`retry_count`／`debug` 機制接手（三章「逾時不該讓整條 pipeline 崩潰」）——這類逾時通常代表模型生成的程式碼有模組層級的匯入或語意錯誤，是預期中會發生、該交給重試迴圈的常規失敗，不是環境問題
- `_partial_verify()` 內部 `run_newman()` 拋出其餘例外（服務確實連不上，不是 reload 逾時這種已知情境）：不吞、直接往上拋，中止整條 `graph.ainvoke()`——這種情況已經先通過本輪的 `_wait_for_service_reload()` 確認過服務有回應，若這裡還連不上，代表服務在驗證的瞬間又整個掛了，性質上跟「服務打從一開始就沒起來」是同一類環境問題

---

## 十一、待決定事項

- [x] **`_reload_token.py` 是否確實落在 uvicorn `--reload` 預設監控的範圍內**：已解決。09b 落地時用真實 Docker 容器內的 reload 週期驗證過會被正確偵測到（見 `09b_implement_agent_code.md` 六章），`.py` 副檔名維持原方案，不需要改用 `--reload-include`
- [x] **`_reload_probe_wrapper.py`（九章）需要有測試涵蓋**：已解決，見 `tests/python_service/test_reload_probe_integration.py`（真實 Docker 容器，非 mock），涵蓋探測端點連線、token round-trip、修改程式碼後正確等到新 worker 三種情境
- [ ] **`_wait_for_service_reload()` 的實際時間預算調校**：目前輪詢 token 用的是 `SERVICE_READY_TIMEOUT_SECONDS`（三章），實際跑起來這個預算夠不夠用（尤其模型生成耗時本來就長，見 07a 七章，且每輪至少兩次完整重啟的成本也要算進去，見三章「已知限制」），需要接上真實環境才能校準，屬於數字微調，不影響本文件已定案的機制設計
- [x] **Python 服務的啟動機制目前沒有任何文件正式列為 pipeline 步驟**：09b 落地時發現這個機制若不解決就無法真正驗證，一併定案並實作：`uvicorn --reload` 在 Windows 上經常無法真正完成重啟（Windows `CTRL_C_EVENT` 送達機制不可靠，已用真實環境重現，見 `09b_implement_agent_code.md` 五章），改在 Docker 容器內執行目標 Python 服務（`python_service/process.py` 的 `PythonServiceContainer`，六章），由 `implement_node.run()` 在真正第一次進入時啟動、`main.py` 收尾時關閉。**新衍生的待決定事項**：容器內只安裝已知的最小基線套件集合（FastAPI／SQLAlchemy 等），目標專案完整依賴清單如何產生、如何餵給容器仍未解決；Docker 這項新環境前提也還沒寫進 00 五章「環境建立」——皆見 `09b_implement_agent_code.md` 九章
- [ ] **`scaffold_skipped` 是否該讓整條 pipeline 提早 `give_up`，不必走完 `retry_count`**：七章「沒有採用的方案」已完整記錄評估過程與擱置理由（風險是誤判成「已無可修的東西」、連帶放棄其他仍可修的 module）。九章的修正已經確保這類失敗至少不會重複浪費本地模型呼叫，只是還沒有做到「提早結束整條 pipeline」這一步。留給 `10a_debug_agent_architecture.md`（待建立）評估，需要更完整的「這次局部驗證失敗是否命中 scaffold 缺口對應的函式」資料才能安全判斷
- [ ] **`main.py`（01 九章）收尾列印是否要一併印出 `task_failures`**：見六章「既有的 `RefactorState.failed_tasks` 不廢棄」——目前收尾只印粗粒度的 `failed_tasks`（task id 清單），`task_failures` 能提供根因分類與 `error` 內容，對人工事後查看更有幫助，但屬於資訊呈現層面的錦上添花，不影響任何功能，優先度低於其餘項目
- [ ] **`07a_translator_cli_architecture.md` 十四章記錄的 `MultipartFile` 型別缺口**：若因這類型別對應缺口導致的失敗被歸類成 `"fill_failed"`（不是 `skipped_interfaces` 命中），⑦ Debug Agent 判讀時仍需要自行讀懂 `error` 文字內容才能定位根因；是否需要在 `task_failures` 再新增更細緻的錯誤分類，待 10a 建立、累積更多真實失敗案例後再評估
- [ ] **五章的 `_RELATIONSHIP_GAP_NOTICE`／`_enums.py` 補強目前是固定套用在所有 services／repositories 層 task，沒有依實際是否用到關聯／Enum 欄位做精算**：比照 06a 十二章「`routers` 層一律納入 `schemas/{module}.py` 是否過度保守」同一種取捨——這是保守、多連不排除的既有精神，若真實專案 `_enums.py` 體積偏大或 relationship 提示造成 context 雜訊過多，可再評估是否需要精算，待接上真實專案規模驗證

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
