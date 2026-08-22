# 全域 Log 機制架構設計：一般 Log／Claude API／本地 Ollama 呼叫紀錄

## 一、背景與動機

09b 端對端驗證期間累積了 `09b_bug_trace.md` 這份缺陷總表，其中多個問題事後能快速定位（如 #20 `response["body"]` 欄位不存在、#33 newman exit code 誤判），都是因為當下能重新跑一次、加印幾行 log 現場重現。但「尚未解決」清單裡的最後一項完全不同：

> `translator_cli` 送給模型的 prompt 內容完全沒有被記錄——`build_user_prompt()` 組出的內容直接送出、呼叫結束就從記憶體消失，沒有任何 log 或落地機制。持續失敗的 task（`exam` 模組 6 支函式）事後無法回溯查證實際送了什麼、context 到底多大。

這暴露的不是單一函式的 bug，而是**整個專案從一開始就沒有規劃「LLM 呼叫內容該怎麼留痕」**——`00_refactor_architecture.md` 六章雖然定案了 Claude API 的**用量**記錄（`common/llm_usage_logger.py`），但那份設計的動機是稽核花費（見 `docs/03_spent_cost_estimate.md`），只記 token 數字，從未打算記 prompt/response 本文；本地 Ollama 呼叫端則連用量都沒記。進一步盤點發現，連「一般執行 log」（`logger.info`/`warning`/`error`）都沒有架構規範，全專案沒有任何地方呼叫過 `logging.basicConfig()`，直到這個 session 才在 `main.py` 土法加了一行暫時解法。

本文件的目標：把「一般 log」「Claude API 呼叫紀錄」「本地 Ollama 呼叫紀錄」三件事**一次定案成統一機制**，而不是各自散落的臨時補丁。三者統一的理由不是巧合——Claude 與 Ollama 呼叫都是「LLM 呼叫」，天生需要記錄 prompt/response 這種大型文字內容；一般 log 則是所有模組（含這兩種 LLM 呼叫端本身）共用的基礎設施，三者天然有依賴關係，分開設計反而會在整合點各自維護一份。

---

## 二、現況缺口盤點

| | 一般執行 log | Claude API 呼叫 | 本地 Ollama 呼叫 |
|---|---|---|---|
| 架構文件規範 | 無 | 有（00 六章，僅涵蓋用量） | 無 |
| 目前落地方式 | 各模組各自 `logger = logging.getLogger(__name__)`，`main.py` 一行暫時性 `basicConfig` | `common/llm_usage_logger.py::log_usage()`，**唯一呼叫端是 `common/llm_client.py::call_claude_for_json()`**（已核對全 repo，無其他呼叫點） | 完全沒有 |
| 記錄內容 | 不一定印得出來（見下） | timestamp／caller／model／token 用量 | 無 |
| Prompt／Response 內容 | N/A | **不記錄**——`log_usage(response, *, model, caller)` 的簽名結構上就拿不到 prompt，`response` 物件本身不含輸入內容 | **不記錄** |
| 斷線／例外時是否留痕 | 視個別模組是否自己 `except` 後 log | **否**——`log_usage()` 只在拿到 `response` 之後才被呼叫，API 呼叫本身失敗時完全不記錄 | 無機制可言 |
| 輸出目的地 | stderr（無 handler 時 Python 預設 WARNING 以上才印，`.info()` 全被吃掉） | `logs/claude_api_usage.jsonl` | 無 |
| 已知副作用 | Windows console 可能落地成 cp950，事後 grep 中文關鍵字會漏；未過濾第三方套件 log 時 INFO 等級會被 `httpx`／`anthropic` 的連線雜訊淹沒 | — | 導致 09b_bug_trace 最後一項無法解決的懸案 |

三者現況不同，但**缺口的性質相同**：都是「沒有從架構層級規劃，只靠各自臨時補丁撐著」。

---

## 三、設計範圍與非目標

**範圍**：
1. 一般執行 log 的正式落地機制（取代 `main.py` 的暫時性 `basicConfig`）
2. Claude API 呼叫的 prompt／response 記錄，與既有的 token 用量記錄整合成同一份儲存，**含呼叫本身失敗時的保證留痕**
3. 本地 Ollama 呼叫的 prompt／response 記錄（含格式修正重試的每次 attempt），**含傳輸層例外時的保證留痕**
4. 一般 log 與單次 LLM 呼叫（trace）之間的精準關聯機制
5. 查詢介面：完成後如何根據「某次測試中失敗的 task」回溯查到當時送出的 prompt

**非目標（刻意不做，留在待決定事項）**：
- 不做人類看的 Web dashboard——已評估並排除 Langfuse／Grafana／純檔案落地等替代方案（見七章「儲存方案評估」），理由是單人單機、Claude Code 才是主要消費者
- 不做 retention 自動清理策略的具體數字（磁碟成長率、保留期間，見十四章）
- 不做跨機器／多人協作的紀錄同步
- **不解決 LangGraph checkpointer／`thread_id` 的接入**——`01_langgraph_architecture.md` 五章「已知限制」已明確記錄這是延後處理的獨立缺口（現階段連 resume 都不是真正的 LangGraph resume，重啟即重頭開始）。本文件六章的 `run_id` 設計會**預留**未來接上 checkpointer 時的掛鉤點，但這次不實作 checkpointer 本身

---

## 四、整體架構總覽

```
main.py 啟動
  │  common/run_context.py::new_run_id() 產生一次
  │  寫入 initial_state["run_id"]（見六章）
  │  configure_logging(run_id=..., file_handler=True)（五章）
  ▼
┌─────────────────────────────────────────────────────────┐
│  一般執行 log（logging 標準庫，五章）                      │
│  各模組沿用既有 logger = logging.getLogger(__name__) 慣例   │
│  Filter 自動附加 [run_id|trace_id]（trace_id 見六章）        │
│  輸出：logs/orchestrator.log（rotating，UTF-8）＋ console      │
│  已過濾 httpx／anthropic 等第三方套件的 INFO 雜訊             │
└─────────────────────────────────────────────────────────┘
                          │
                          │ run_id（同一個 Python process 內顯式傳遞，見六章「執行模型查證」）
                          ▼
┌─────────────────────────────────────────────────────────┐
│  LLM Trace 統一儲存（七章）                                 │
│  common/llm_trace.py :: record_llm_call_start() / record_  │
│  llm_call()——呼叫前寫 running row，finally 再 upsert 補齊    │
│  門檻式混合儲存：< 100KB 進 SQLite 欄位／≥ 100KB 落檔案       │
│  prompt／response 各自獨立判斷門檻（見七章「門檻判斷」）        │
│  呼叫端保證每個 attempt 恰好一筆 row（八、九章）              │
└─────────────────────────────────────────────────────────┘
        ▲                                    ▲
        │ 八章                                │ 九章
┌───────────────────┐              ┌──────────────────────┐
│ common/llm_client.py│              │translator_cli/        │
│ call_claude_for_json│              │ollama_client.py       │
│（同步函式，非 async） │              │（④⑤ 填空模式呼叫）      │
│（①③⑦[P][B] 共用）  │              │ 呼叫前 record_llm_call_ │
│ 呼叫前 record_llm_   │              │ start()，結束 record_  │
│ call_start()，結束    │              │ llm_call()，run_id 為   │
│ record_llm_call()    │              │ 顯式必填參數（見九章）    │
└───────────────────┘              └──────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  llmlog CLI（十一章，實作見 11b，只掛 console handler）        │
│  llmlog task <task_id> / func <file> <function> /          │
│  show <trace_id> / recent / search                         │
└─────────────────────────────────────────────────────────┘
```

**關鍵決策**：Claude 與 Ollama 兩條呼叫路徑**共用同一張 `llm_traces` 表**，不是各自一份儲存——這跟 00 六章「共用工具」一節的既有精神一致（`common/llm_client.py`／`common/concurrency.py` 等都是「同一件事，不是恰好想法一致」）。查詢時不需要知道「這個 task 當初是 Claude 做的還是 Ollama 做的」才能選對地方查，一律 `llmlog` 一個入口。

### 記錄流程總覽

十章用「情境」講清楚了事後怎麼**查**，這裡用同樣的白話方式補一段：一次呼叫**當下**怎麼變成 `llm_traces.db` 裡的一筆紀錄。細節、例外分類、精確順序都在六～九章，這裡只是把時間軸串起來，方便先抓住全貌。

**Claude 路徑**（`call_claude_for_json()`，八章）：

1. **呼叫發生**：Agent（①③⑦、[P]、[B] 任一個）呼叫這個函式——同步函式，可能是主執行緒直接呼叫，也可能被 Map 階段丟進 `ThreadPoolExecutor` 的 worker 執行緒呼叫
2. **產生 `trace_id`**：函式一開始解出 `run_id`（沒傳就用 `adhoc_run_id()`）、產生這次呼叫專屬的 `trace_id` 並 `set` 進 contextvar、記下開始時間
3. **執行呼叫**：實際打 Claude API
4. **（成功或例外）**：成功就解析回應、`json.loads` 出結果；失敗依例外類型（逾時／API 錯誤／JSON 解析失敗／其他意外）分類，記下 `status`／`error_msg`，例外照樣往外拋、不吞掉
5. **組出 prompt／response**：`prompt` 呼叫前就已經組好；`response` 視情況可能是 `None`（例外導致完全沒拿到回應時）
6. **`reset` contextvar**：不論成功或失敗都會執行到收尾，且**必須先 `reset`、才呼叫 `record_llm_call()`**（見八章「為什麼 `reset()` 要排在 `record_llm_call()` 之前」——順序反了的話，`record_llm_call()` 萬一自己拋例外，`reset()` 就永遠不會執行，`trace_id` 會殘留污染這個（可能被重用的）執行緒後續的 log）
7. **門檻判斷 → 寫檔案（如需要）→ INSERT**：把這次呼叫的完整資訊（prompt、response、成功與否、token 用量、耗時……）交給 `record_llm_call()`，內部判斷 prompt／response 個別是否超過大小門檻，小的存進資料庫欄位、大的先落成 payload 檔案再 INSERT——**這一步保證會發生**，不管第 4 步是成功還是失敗

**Ollama 路徑**（`get_function_body()`，九章）：大致同上，但多兩個差異——一是可能重試最多 3 次（模型回應格式不符契約時），**每一次嘗試都各自走一遍上面 2～7 步、各留一筆獨立紀錄**，不是只留最後一次；二是 `run_id` 只在最外層的 `fill_function()` 解析一次，往下傳給每一次重試共用，不是每次重試各自解析（避免同一個 task 的三次嘗試被拆進三個不同的 run）。

兩條路徑最後都匯進同一張 `llm_traces` 表，這也是十章能用同一套 `llmlog` 指令查兩邊的原因。

---

## 五、一般執行 log 機制

### 設計

新增 `common/logging_setup.py`，提供唯一對外函式 `configure_logging(run_id: str, *, file_handler: bool = True)`，取代 `main.py` 目前的暫時性 `logging.basicConfig(...)` 那一行：

- **兩個 handler（`file_handler=True` 時）**：`RotatingFileHandler`（`logs/orchestrator.log`，明確 `encoding="utf-8"`——理由同 00 六章「檔案讀寫編碼慣例」，Windows 預設 cp950 會讓中文 log 訊息 mangle）＋ console `StreamHandler`；`file_handler=False` 時只掛 console，見下方「`llmlog` 為什麼不掛 file handler」
- **console 的編碼問題**：只把 file handler 設成 `encoding="utf-8"` 只解決一半——開發過程中實際觀察到的痛點是「**console** 輸出落地成 cp950，事後 grep 中文關鍵字會漏」，這發生在開發時用 `python main.py | tee logs/xxx.log` 這類土法留存整段輸出的場景，此時 console 的 stdout 編碼才是實際落地內容的編碼。`configure_logging()` 需要在建立 console handler 之前，嘗試 `sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")`（Python ≥ 3.7；用 `try/except AttributeError` 包住，某些非標準 stream 環境沒有 `reconfigure()`）。這條修好之後，另一個 session 為了同一個編碼問題另外寫的 `watch_signals.py` 土法工具可以退場，見十三章
- **等級**：預設 `INFO`，可用環境變數 `ORCHESTRATOR_LOG_LEVEL` 覆蓋
- **格式**：`%(asctime)s %(levelname)s %(name)s [%(run_id)s|%(trace_id)s] %(message)s`——`run_id` 由 `configure_logging()` 呼叫時的參數固定寫入一個 `logging.Filter`（同一行程全程不變，因此不需要 contextvars，直接閉包捕捉即可）；`trace_id` 則需要 contextvars（見六章「trace_id 與一般 log 的關聯」），同一個 Filter 額外讀取 `common/trace_context.py::current_trace_id`，沒有值時印 `-`
- **不改動既有呼叫端**：03b／04b 等模組既有的 `logger = logging.getLogger(__name__)` 慣例完全不動，`configure_logging()` 只負責裝 handler，不介入各模組怎麼取 logger

### 第三方套件雜訊過濾

若直接在 root logger 掛 handler、等級設 `INFO`，`httpx`（`anthropic` SDK 與 `translator_cli/ollama_client.py` 都用它）、`anthropic` SDK 本身在 `INFO` 等級會印出大量連線層細節（每次 request 的 method/url/headers），把 `logs/orchestrator.log` 淹沒成雜訊，真正的業務流轉訊息反而被稀釋找不到。`configure_logging()` 內必須把 `httpx`／`httpcore`／`anthropic`／`urllib3` 這四個 namespace 的等級明確調到 `WARNING`（實作見 11b）。

這份清單**寫死在程式碼裡，不經環境變數**——跟 00 六章「檔案讀寫編碼慣例」不經環境變數配置是同一種判斷：這是「哪些套件很吵」這個機械事實，不是每個環境會想覆寫的決策。若日後真的需要看到 `httpx` 的 debug 細節（排查連線問題），直接在程式碼臨時改，不需要為此設計環境變數。

### 呼叫時機

`main.py` 在 `load_dotenv()` 之後、`build_graph()` 之前，先產生 `run_id`（見六章）、寫入 `initial_state`，再呼叫 `configure_logging(run_id=run_id, file_handler=True)`。

### `llmlog` 為什麼不掛 file handler

`llmlog` CLI（十一章）是獨立行程，若也對 `logs/orchestrator.log` 呼叫 `configure_logging(run_id="cli", file_handler=True)`，跟 Orchestrator 主行程同時執行時會踩到 Windows 的檔案鎖規則：`RotatingFileHandler` 的 rollover 動作是「關閉檔案 → `os.rename()`」，Windows 上若這個檔案同時被**另一個行程**開著（即使只是 append 模式），`os.rename()` 會直接拋出 `PermissionError: [WinError 32]`。Orchestrator 跑一輪 pipeline 動辄數十分鐘，這段期間 `logs/orchestrator.log` 一直開著；只要這段期間執行了任何一次 `llmlog` 指令（很常見——`llmlog` 存在的目的就是在 pipeline 還在跑或剛失敗時拿來查），就有機會撞上這個問題，且發生時機不可預期（取決於 rollover 剛好落在哪一刻）。

修正：`llmlog` 呼叫 `configure_logging(run_id="cli", file_handler=False)`——只掛 console handler，自己這幾行操作的診斷輸出印到終端機就夠，不需要跟 Orchestrator 的主 log 檔案搶。這**不影響**十一章「查詢預設當前 run」的語意，那是查 `llm_traces.db`（SQLite，本來就有 WAL＋`busy_timeout` 處理多行程併發，見七章），跟這裡討論的 `logs/orchestrator.log`（一般 log 的 rotating file）是兩個完全不同的儲存。

### 為什麼不用 `QueueHandler`（評估過，這次不採用）

`RotatingFileHandler`／`StreamHandler` 的 `emit()` 是同步 I/O，理論上會佔用呼叫當下的協程／執行緒，標準迴避方式是 `QueueHandler` 把寫入丟給背景執行緒。**這次不採用**：容易誤用的前提是「FastAPI 高併發 server 不能塞同步 I/O」，但那是重構後**目標 Python 服務**的技術棧（00 三章），不是 Orchestrator 自己的——Orchestrator 是批次 pipeline script，沒有外部併發請求要服務。實際併發來源（Map 階段 `ThreadPoolExecutor`、已被 `MODEL_SEMAPHORE(1)` 序列化的 Ollama 呼叫）都不佔用主 event loop；一般 log 一行幾百 bytes，磁碟寫入是微秒等級，這個量級的阻塞不會是可感知的瓶頸，不值得換一層背景執行緒生命週期管理的複雜度。若之後 Map 階段規模擴大、實測發現真的卡到 event loop，再引入（見十四章）。

### 不處理範圍

Rotation 策略（`maxBytes`／`backupCount`）給合理預設值即可（如 10MB × 5 份），不做成環境變數——理由同上，操作細節不是每個環境會想覆寫的決策。

---

## 六、識別碼設計：run_id／task_id／函式身分／trace_id

這一章直接對應三個問題：`09b_bug_trace.md` 記錄的「task 編號不是跨次重跑的穩定識別碼」、「單靠一個全域 run_id 無法精準定位是哪一次呼叫」，以及「`run_id` 的產生機制是否經得起實際執行模型的檢驗」。四層識別碼各自解決不同的查詢需求：

| 識別碼 | 產生時機 | 生命週期 | 解決的查詢情境 |
|---|---|---|---|
| `run_id` | `main.py` 啟動時產生一次，寫入 `RefactorState.run_id` | 一次 `python main.py` 執行 | 「這次測試」——同一次執行期間，不論跑了多少個 Agent、多少次 debug 迴圈，都共用同一個 run_id |
| `task_id` | [P] Plan Agent 產生（既有欄位，`TaskSpec.id`） | 同一個 run 內固定；跨 run 不保證穩定 | 在**同一個 run 內**，把某次失敗跟 Plan Agent 排出的具體任務對上 |
| `target_file` + `class_name` + `function_name` | translator-cli 呼叫端從 `TaskSpec` 取得（既有欄位） | 跨 run 穩定 | **跨 run** 比對同一支函式在不同次重跑的表現——task_id 會變、函式身分不會 |
| `trace_id` | 每次 LLM 呼叫（含格式重試的每個 attempt）當場產生一個 uuid4 | 單次呼叫的生命週期 | 精準定位「哪一次特定呼叫」觸發了某行錯誤 log 或某個異常，尤其是 Map 階段平行呼叫、格式重試迴圈這種一般 log 混雜多次呼叫輸出的情境 |

### run_id：執行模型查證

`00_refactor_architecture.md` 四章的環境架構圖原本把 translator-cli 標成「subprocess 呼叫」——若這是字面意義，`run_id` 存在 Orchestrator 行程記憶體裡就沒用，translator-cli 那個獨立行程讀不到，會讓整個「查這次測試的 task」功能失效。已核對實際程式碼（非文件敘述）：`translator_cli/client.py` 的 `fill_function()` 是被 `implement_node.py` 用 `from translator_cli import client as translator_cli` 後 `await translator_cli.fill_function(...)` 直接呼叫的，是同一個 Python process 內的函式呼叫，不是 `subprocess.run()`；打另一台 Mac 的是 `_call_ollama_once()` 內部的 HTTP request，不是「translator-cli 本身是子行程」。

**結論**：translator-cli 與 Orchestrator 跑在**同一個 Python process**，`run_id` 可以用一般函式參數（不是環境變數、不是跨 process IPC）傳遞，七章「同一 process 用 `threading.Lock` 保護 SQLite 寫入」的併發假設同樣成立。**`00_refactor_architecture.md` 四章那張圖的措辭已依查證結果修正**（見十三章）。

### run_id：為什麼是 RefactorState 顯式欄位，不是模組全域變數

- `common/run_context.py::new_run_id() -> str`：純函式，格式 `{YYYYMMDD_HHMMSS}_{uuid4前6碼}`（後綴避免同一秒內啟動兩個行程碰撞），是**唯一的正式產生點**，只在 `main.py` 啟動時呼叫一次
- `main.py` 呼叫一次，寫入 `initial_state["run_id"]`（`01_langgraph_architecture.md` 三章 `RefactorState` 需要新增這個欄位，屬於 11b 落地時要同步修改的既有檔案，見十三章「相容性」）
- 各 node 需要呼叫 `call_claude_for_json()`／`fill_function()` 時，從 `state["run_id"]` 顯式取出傳入——不是全域讀取，這跟 `test_dsn`／`python_base_url` 是同一類「節點邏輯需要用到的資料，該進 State」

### `adhoc_run_id()`：旁路情境的 fallback，與正式產生點區分開來

`translator_cli/` 刻意不 import `graph.state`、維持獨立性（見 07a 十二章），`call_claude_for_json()` 也刻意不知道任何 Agent 的環境變數命名慣例（00 六章）——兩者都可能在沒有 `RefactorState` 可用的情境下被呼叫（單元測試、`01` 五章提到的「直接呼叫 `run_collection_agent()`」人工續跑捷徑）。因此 `run_id` 在對外入口函式的簽名上是選填參數，預設 `None`；若呼叫端沒有傳，需要就地生成一個一次性的值。

**但這個 fallback 值不能跟 `main.py` 正式產生的 `run_id`長得一樣**——若兩者格式相同，`llmlog recent`（預設查最新 run）在「某個 node 忘記傳 `run_id` 參數」這種**純粹是 bug** 的情境下，只會安靜地把資料分裂成一堆單筆 run，看起來像「沒資料」而查不出「其實是漏傳參數」這個真正的根因。因此 `common/run_context.py` 另外提供 `adhoc_run_id() -> str`，回傳值固定是 `f"adhoc_{new_run_id()}"`——單純加一個 `adhoc_` 前綴，讓 `llmlog` 的輸出能一眼認出這不是正常 graph 執行路徑產生的 run_id：不論是刻意的旁路呼叫，還是漏傳參數的 bug，都值得在查詢結果裡顯眼標出來，不是靜默地看起來像另一次正常的測試（實作見 11b）。

`call_claude_for_json()`／`fill_function()` 的 fallback 一律呼叫 `run_id or adhoc_run_id()`，不是 `new_run_id()`。`common/llm_trace.py::record_llm_call()` 內部**也**做一次同樣的防禦性 fallback（`run_id or adhoc_run_id()`）——這不是期待正常路徑會用到，而是防呆：即使八、九章最外層的解析邏輯以後被改壞、忘記解析就直接傳 `None` 下來，`record_llm_call()` 這一層依然保證不會因為 `run_id` 是 `NULL` 而讓整筆 INSERT 失敗、trace 靜默遺失（schema 是 `run_id TEXT NOT NULL`，見七章）。

### fallback 只能在每條呼叫鏈的最外層解析一次，不能在內層重複判斷

`translator_cli` 這條呼叫鏈有兩層（`fill_function()` → `get_function_body()` 的格式重試迴圈），若「沒有值就生一個」這個判斷放在**內層**的 `get_function_body()`，同一個 task 的三次格式修正 attempt（`_FORMAT_RETRY_COUNT=2`，見九章）在旁路情境下會各自呼叫一次 `adhoc_run_id()`、拿到三個**不同**的 run_id，直接破壞七章「同一次測試的 payload 天然聚在 `{run_id}` 資料夾」這個實體路徑設計——三次 attempt 會被分散寫進三個不同的 `payloads/{run_id}/` 目錄。正確做法：`run_id` 的解析**只在 `fill_function()` 這個最外層函式做一次**，得到的具體值原樣往下傳給 `get_function_body()`；`get_function_body()` 自己收到的 `run_id` 是**必填參數**（不是 `str | None`），不會是 `None`，不需要（也不應該）再自己判斷一次。對 Claude 這條鏈，`call_claude_for_json()` 本身就是最外層（沒有更深的、需要共用 run_id 的重試迴圈），沒有這個風險，維持原設計即可。

**範圍界定**：這個設計解決的是「不要用隱藏全域 singleton、資料流向要顯式」，不解決「行程重啟後 run_id 保持不變」（那需要 checkpointer／`thread_id`，見三章「非目標」）。把 `run_id` 放進 State 順帶預留了接上 checkpointer 時的掛鉤點——屆時 `run_id` 可以直接定義為 `thread_id`，不需要重新設計這一層。

### trace_id 與一般 log 的關聯

`run_id` 只能告訴你「這是同一次執行」，無法回答「Map 階段五個平行呼叫裡，是哪一個觸發了這行 WARNING」。新增 `common/trace_context.py`，提供模組層級的 `current_trace_id`（`contextvars.ContextVar[str | None]`，預設 `None`）。

**使用方式**（八、九章的呼叫點都遵守同一套模式）：

1. 呼叫實際 LLM API 前，先產生 `trace_id = str(uuid4())`
2. `token = current_trace_id.set(trace_id)`
3. 執行實際呼叫（`try/except/finally`，見八、九章）
4. `finally` 區塊裡呼叫 `current_trace_id.reset(token)`，恢復成呼叫前的狀態（不是設回 `None`——若這次呼叫本身是巢狀在另一個已有 `trace_id` 的 context 裡，`reset` 才能正確還原，而不是粗暴清空）；**這一行必須排在 `record_llm_call()` 之前執行，見八章「為什麼 `reset()` 要排在 `record_llm_call()` 之前」**

`configure_logging()`（五章）裝的 `logging.Filter` 在每一行 log 產生時讀取 `current_trace_id.get()`，有值就附加在格式字串的 `[run_id|trace_id]` 區段。這樣一來，`_call_ollama_once()` 重試迴圈裡 `logger.warning("ollama 連線失敗...")` 這類既有的 log 呼叫（程式碼完全不用改）就會自動帶上當次呼叫的 `trace_id`，可以直接跟 `llmlog show <trace_id>` 的內容對照。

### `ThreadPoolExecutor` 下的 contextvars

`trace_id` 是在 worker 執行緒**內部**（`call_claude_for_json()` 本體，就是被 `executor.submit()` 丟進去跑的那個 callable）呼叫 `.set()` 產生的，不是外層先設好、期待 worker 執行緒「繼承」——同一個函式呼叫、同一個執行緒內設完就讀得到，不需要 `contextvars.copy_context()` 包住 `executor.submit()`。

真正需要小心的是 `ThreadPoolExecutor` **重複使用**同一批 worker 執行緒：若 `current_trace_id.reset(token)` 沒執行到（例如「為什麼 `reset()` 要排在 `record_llm_call()` 之前」提到的順序顛倒），殘留的 `trace_id` 會污染同一個執行緒後續、下一個 task 呼叫前印的其他 log。正確解法是保證 `reset()` 一定執行到（見八章），`copy_context()` 對這個風險無效。

---

## 七、LLM Trace 統一儲存設計

儲存方式定案為「門檻式混合儲存 + CLI 封裝」：payload 小於門檻進 SQLite 欄位，大於門檻落成檔案，統一透過 `llmlog` CLI（十一章）查詢，不是原始 log 檔案讓人直接翻。這是針對本專案執行環境（單人單機、Windows、Claude Code 是主要消費者、需要排查「呼叫成功但回答錯誤」）評估過其他方案後的結論：

### 儲存方案評估

| 方案 | 排除理由 |
|---|---|
| Langfuse 自架 | v3 起綁 ClickHouse，需 4 核心／16GB、六個服務，會在開發機上跟 IDE 搶資源 |
| Langfuse Cloud | 未排除，是備案——日後有團隊協作、prompt 版本管理、eval 需求時可回頭考慮 |
| PLG／ALG（Loki + Grafana） | 為人類的 dashboard 設計，不是給 Claude Code 用的：查一筆紀錄要組 LogQL → curl → 解析巢狀 JSON → 再 gunzip blob，且 Docker 沒開就斷線（若日後重新考慮：Promtail 已於 2026-03-02 EOL，須改用 Grafana Alloy） |
| 純檔案落地（無 DB） | 失去混合條件查詢的能力——「上週失敗的呼叫裡哪些 prompt 提到某個關鍵字」會變成先掃檔名再逐檔開的兩步驟＋膠水程式碼，SQLite＋FTS5 一條查詢就夠 |

本節列出**整合進本專案既有兩條呼叫路徑後，需要擴充或修正的設計細節**：

### 儲存位置

```
logs/
├── orchestrator.log            # 五章，一般執行 log
├── llm_traces.db               # 本節，SQLite，WAL 模式
└── payloads/
    └── {run_id}/
        └── {trace_id}.md       # prompt／response 任一項 ≥ 門檻時落檔，見下方「門檻判斷」
```

payload 檔案路徑用 `{run_id}/{trace_id}.md`，不是單純用 `{YYYY-MM}/{task_id}.md` 這種按月分目錄的方式——原因見六章：`task_id` 不是穩定識別碼、且同一 task 可能有多次 attempt（格式修正重試），用 `trace_id`（uuid4）才能保證檔名不互撞；用 `run_id` 分目錄則是讓「這次測試」的所有 payload 天然聚在同一個資料夾，符合本文件標題「完成後如何查哪次測試的 task 的 prompts」這個核心查詢情境。

### Schema 設計

`llm_traces` 一筆記錄一次 LLM 呼叫（Ollama 每個格式重試 attempt 各算一筆），欄位設計：

| 欄位 | 型別／約束 | Nullable | 說明 |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | 否 | 內部主鍵，FTS5 `content_rowid` 綁這個，不綁 `trace_id`（見下方「為什麼不能用 trace_id 當 FTS 主鍵」） |
| `trace_id` | `TEXT UNIQUE` | 否 | uuid4，對外查詢識別碼（`llmlog show <trace_id>`） |
| `run_id` | `TEXT` | 否 | 六章 |
| `ts` | `TEXT`（ISO8601 UTC） | 否 | |
| `vendor` | `TEXT`（`'claude'` \| `'ollama'`） | 否 | |
| `model` | `TEXT` | 否 | |
| `caller` | `TEXT` | 否 | 兩條路徑語意不同：Claude 是 inspect 抓到的真正業務呼叫端；Ollama 固定寫死 `"ollama_client.get_function_body"`（記錄點自己，不是 `implement_node`）。之後 `GROUP BY caller` 做統計時，Ollama 這條分不出是哪個 task 呼叫的，要另外用 `task_id`／`target_file` 分——這是設計決定，不是疏漏 |
| `task_id` | `TEXT` | 是 | [P] Plan Agent task id；⑤ 填空模式必填，⑦ Debug Agent 可選填（見八章），其餘為 `NULL` |
| `attempt` | `INTEGER`，預設 `0` | 否 | Claude 固定填 `0`（見八章；SDK 內建 `max_retries` 的重試對這裡不可見，`latency_ms` 會把那些隱藏重試的時間也含進去，分析延遲時留意）；Ollama 為當前重試次數（0-based，見九章） |
| `target_file` / `class_name` / `function_name` | `TEXT` | 是 | 六章「函式身分」 |
| `template_version` | `TEXT` | 是 | 十四章待決定 |
| `input_tokens` / `output_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` | `INTEGER` | 是 | ollama 呼叫的 `cache_*` 固定 `NULL` |
| `latency_ms` | `INTEGER` | 是 | |
| `status` | `TEXT`（`'running'` \| `'ok'` \| `'error'` \| `'timeout'`） | 否 | `record_llm_call_start()` 寫入時固定 `'running'`（見下方「呼叫開始即寫入 running row」），`record_llm_call()` 之後 upsert 成最終值；沒有 `CHECK` 約束（跟 `issue_tag` 不同），因為寫入端只有兩個、都在自己控制範圍內 |
| `http_code` | `INTEGER` | 是 | |
| `error_msg` | `TEXT` | 是 | |
| `prompt` / `response` | `TEXT` | 是 | 低於門檻時存這；門檻判斷見下節 |
| `prompt_payload_key` / `response_payload_key` | `TEXT` | 是 | 超過門檻時存 `'{run_id}/{trace_id}.md'` |
| `issue_tag` | `TEXT`，固定列舉 | 是 | `hallucination` / `format_error` / `logic_flaw` / `refused` / `truncated`，需要 `CHECK` 約束（見下） |
| `note` | `TEXT` | 是 | |

另需一張 FTS5 virtual table（`llm_fts`，索引 `prompt`／`response`）供全文搜尋，以及四個一般 index（`task_id`、`(target_file, class_name, function_name)`、`run_id`、`ts`——最後一個是給 `llmlog recent --since` 用，避免全表掃，目前量級無所謂但成本趨近於零，一併加上）。完整 `CREATE TABLE`／trigger／index 語句見 11b。

**`id` 用 `INTEGER PRIMARY KEY AUTOINCREMENT`，不是 `trace_id TEXT` 當主鍵**：FTS5 的 `content_rowid` 必須綁在一個真正的、單調不重複的整數 rowid 上。若讓 `trace_id`（`TEXT`）當主鍵，SQLite 底層仍會分配一個隱含 rowid，這個隱含 rowid 在 `VACUUM` 之後可能被重新編號，導致 FTS 索引跟實際資料錯位。`AUTOINCREMENT` 保證即使有資料被刪除，同一個 `id` 也不會被重新分配給新資料，從結構上避免這個錯位風險；`trace_id` 降級成 `UNIQUE`，對外查詢一樣走得到索引，不影響查詢效能。

**外部 content 表需要 trigger 才會同步**：`llm_fts` 用 `content='llm_traces'` 的 external content 模式，SQLite **不會**自動跟著主表更新——這是最危險的一類錯誤，不會報錯，只是 `search` 永遠回傳 0 筆，容易被誤以為是關鍵字沒打對。必須自己掛三個 trigger：`AFTER INSERT`、`AFTER DELETE`、**以及 `AFTER UPDATE`**——最後這個不是理論情境，`llmlog flag` 會 `UPDATE issue_tag/note`，不處理的話 FTS 索引在被 flag 過的 row 上會跟實際內容脫鉤。

`issue_tag` 加 `CHECK` 約束：這份設計最主要的寫入者是 Claude Code 本身，若不做硬性檢查，同一種問題類型會被寫成不同的詞（下次可能不是 `format_error` 而是 `wrong_format`），三個月後這個欄位就沒辦法聚合統計。只在 CLI 層檢查（十一章 `flag` 指令）不夠保險，資料庫層再加一道 `CHECK` 約束成本近乎零，雙重防線比單靠應用層自律可靠。

### SQLite 連線的執行緒歸屬

`sqlite3.connect()` 預設 `check_same_thread=True`：同一個連線物件只能在建立它的那個執行緒使用，若模組層級建一個連線、卻被 `ThreadPoolExecutor` 的 worker 執行緒（Claude Map 階段）拿去用，會直接拋 `sqlite3.ProgrammingError`，跟七章討論的 `threading.Lock` 完全無關——鎖只解決「兩個執行緒同時寫」的競態，解決不了「這個連線物件根本不屬於這個執行緒」的問題。

**設計決定**：`common/llm_trace.py` 用**單一**模組層級連線，建立時明確 `sqlite3.connect(db_path, check_same_thread=False)`，並讓現有的模組層級 `threading.Lock`（見下方「寫入規則」）保護**所有**存取這個連線的程式碼路徑，不是只包 INSERT 那一行——`check_same_thread=False` 只是關掉 Python 自己的執行緒歸屬檢查，SQLite 連線本身仍然不是「多執行緒同時用也安全」，外部序列化（就是這把 Lock）是必要條件，不是可有可無的加強。這決定了 `common/llm_trace.py` 的模組結構（單一連線 + 單一鎖，不是連線池、不是 per-thread `threading.local()`），11b 直接照這個結構實作，不需要重新評估。

**連線必須是 lazy（第一次呼叫才建立），不是模組匯入時的頂層陳述式**：比照 `common/llm_client.py::_get_client()` 既有的雙重檢查鎖定風格（`if _client is None: with _lock: if _client is None: ...`）——理由相同：`common/llm_trace.py` 可能被其他模組在 `main.py` 呼叫 `load_dotenv()` 之前就 import 到（例如某個套件在檔案頂層 `from common.llm_trace import record_llm_call`），若連線是頂層陳述式，建立當下讀到的 `LLM_TRACE_DB_PATH` 可能還是環境變數載入前的狀態（未設定、或讀到不對的預設值）。連線物件跟保護它的 `threading.Lock` 都用模組層級變數＋lazy 初始化這一套既有慣例，不另外發明新模式。

### 查詢與維護層設計：`llmlog` 與 Debug Agent 共用同一組 Python 函式

`llm_traces.db` 有兩種完全不同的消費情境，但要的是同一份資料：

1. **互動式排查**：開發時本地用 Claude Code 查「這支函式過去送過什麼 prompt」——這是十、十一章設計的 `llmlog` CLI 服務的對象，人／Claude Code 敲指令、讀終端機輸出
2. **⑦ Debug Agent 系統內排查**：`debug_agent/`（見 `10a_debug_agent_architecture.md` 九章模組結構）分析某個 module 失敗原因時，若能知道「⑤ 上一次幫這個 task 填空實際送了什麼 context、qwen 回了什麼」，能直接對齊 10a 自己的既有設計哲學（10a：「給模型完整資訊，讓它做人類除錯者會做的事」）——這是 10a 十二章待決定事項已經點名、但當時還沒有機制可用的「`debug_rounds` 歷史是否要餵回下一輪 LLM 呼叫」

這兩者要的資料是同一份（task_id／函式身分對應的歷史 prompt／response），差別只在**呼叫情境**：`llmlog` 是獨立行程被人／Claude Code 敲指令觸發；Debug Agent 是 LangGraph node，跑在 Orchestrator 同一個 Python process 裡（六章已查證 translator-cli／整個 pipeline 都是 in-process 執行模型），分析時直接呼叫 Python 函式即可，沒有理由讓它去 shell out 呼叫 `llmlog` CLI、再解析文字輸出——那是不必要的迂迴，還多一層字串解析可能出錯的風險。

**設計決定：`common/llm_trace.py` 是 `llm_traces.db` 讀寫的唯一權威來源，不是只有查詢**——`llmlog` 有 7 個子指令（十一章），其中 5 個要碰資料庫（`recent`／`search`／`show`／`task`／`func`／`flag`／`gc`，`show` 併入 `get_trace`），每一個都必須對應一個具名函式，不能讓 CLI 自己在 argparse handler 裡直接寫 SQL——不然「`llmlog` 與 Debug Agent 共用同一套機制」這個原則只對其中幾個指令成立，其餘的等於各自為政，日後 Debug Agent 想用「搜尋」這類能力時也沒有函式可呼叫。

九個函式，對外簽名見下表（`TraceRecord` 是七章 schema 欄位對應的結構化物件，`dict` 或 `dataclass`，型別細節留給 11b；完整參數列表與型別標註同樣留給 11b，這裡只列必須在架構層釘死的行為）：

| 函式 | 對應 | 需要釘死的行為 |
|---|---|---|
| `record_llm_call_start(...)` | 八、九章呼叫端（呼叫發出前） | 只收 `prompt` 相關參數，不收 `response`／`latency_ms`／token 用量——這些欄位這個時間點還不存在；寫入 `status="running"`，見下方「呼叫開始即寫入 running row」 |
| `record_llm_call(...)` | 八、九章呼叫端（呼叫結束，UPSERT 唯一入口） | `run_id=None` 時內部呼叫 `adhoc_run_id()`（六章防呆，正常呼叫鏈不會觸發）；`prompt_payload_key`／`response_payload_key`／`issue_tag`／`note`／`id`／`ts` 不是參數——前兩者依門檻判斷內部計算，後三者是寫入當下不需要或 DB 自動產生的欄位；`trace_id` 跟 `record_llm_call_start()` 相同時走 UPSERT 更新既有 row，不同或沒有既有 row 時退化成一般 INSERT |
| `flag_trace(trace_id, issue_tag, note=None)` | `llmlog flag` | `issue_tag` 需落在 schema 的 `CHECK` 列舉內，這裡也要檢查一次、給出比原生 SQL 錯誤更好讀的訊息；`UPDATE` 會觸發 `llm_traces_au` trigger 同步 FTS |
| `get_trace(trace_id)` | `llmlog show` | — |
| `get_traces_for_task(task_id, run_id=None)` | `llmlog task`、Debug Agent | `run_id` 省略時查**全部** run——跟 `llmlog` CLI「省略即最新 run」是不同語意，這裡是給程式呼叫端的通用函式，呼叫端要篩最新 run 自己傳 |
| `get_traces_for_function(target_file, class_name, function_name)` | `llmlog func` | 天生跨 run（六章「函式身分」設計的用途） |
| `list_recent(status=None, since=None, run_id=None)` | `llmlog recent` | 「省略時查什麼」由呼叫端（`llmlog`）自己決定並傳入，理由同上 |
| `search_traces(query, tag=None, vendor=None, since=None)` | `llmlog search` | 內部合併 FTS5（< 門檻的內容）與 `payloads/` 目錄逐檔掃描（≥ 門檻的內容，見十一章「`rg` 依賴」）——合併邏輯在這裡，不是 CLI 層，Debug Agent 未來要用全文搜尋直接呼叫就有完整能力 |
| `latest_run_id()` | 十一章「當前 run」語意 | `SELECT run_id FROM llm_traces ORDER BY id DESC LIMIT 1`；表是空的時回傳 `None` |
| `gc_orphan_payloads(grace_period_hours=24.0)` | `llmlog gc` | 只刪 mtime 早於寬限期的孤兒檔（見下方「`llmlog gc` 與寫入順序的交互作用」）；只能手動觸發；回傳刪除數供 CLI 印出 |

Debug Agent 拿到 `TraceRecord` 後自己決定怎麼摘要進 prompt（例如只取字數、`status`、`error_msg`），控制 context 大小的判斷權在呼叫端，`common/llm_trace.py` 不代為決定。

**共用機制**：`llmlog` CLI 的每個子指令**都是對應函式的薄包裝**，只負責參數解析與終端機輸出格式化（截斷、表格排版），不重新實作一次邏輯——這樣才是真的共用同一套機制，而不是「部分指令共用、部分指令各自為政」。`debug_agent/analysis.py` 需要時直接 `from common.llm_trace import get_traces_for_task`（或未來若用得到，`search_traces`），不經過 `llmlog`。

**範圍界線**：11a 只負責提供這些讀寫能力本身；要不要在 Debug Agent 的哪個分析路徑呼叫、餵進 prompt 的哪個位置、怎麼避免讓 context 過度膨脹，屬於 `10a_debug_agent_architecture.md` 自己的設計範圍，不在這裡展開（10a 十二章對應項目已同步更新為指向這裡，見下方相容性影響）。

### Prompt／Response 序列化格式

`prompt` 欄位（或落檔時 payload 檔案的對應段落）統一存成：

```
=== SYSTEM ===
{system_prompt}

=== USER ===
{user_prompt}
```

`response` 欄位存模型的原始回應文字；**呼叫失敗導致完全沒拿到回應時，這個欄位是 `NULL`，`error_msg` 才是有內容的欄位**。`prompt` 欄位則相反——**內容一定會被留痕，只是落地位置二擇一**（SQLite 欄位或 payload 檔案，看七章「門檻判斷」，欄位本身可能是 `NULL`、但那時 payload 檔案裡有），不會出現「完全查不到 prompt 內容」的情況：`record_llm_call_start()` 在呼叫發出前就把 prompt 寫進去（見上方「呼叫開始即寫入 running row」），不是等 `finally` 才組——即使呼叫還沒結束、正在長時間等待、甚至最終連線失敗，prompt 內容從一開始就查得到。

### 門檻判斷：prompt 與 response 各自獨立

若對 `prompt+response` 合併判斷是否超過 100KB 門檻，一個 5KB 的 prompt 配一個 150KB 的 response，會因為總量超標，連 prompt 也被踢出 SQLite 欄位、進而**踢出 FTS5 索引範圍**——小的那份內容原本完全有能力被全文搜尋，卻因為跟大的那份綁在一起判斷而白白失去這個能力，因此設計上兩個欄位必須各自獨立判斷。

因此 `record_llm_call()` 對 `prompt`／`response` 兩個欄位**各自獨立**跟 `LLM_TRACE_PAYLOAD_THRESHOLD_BYTES` 比較（用 UTF-8 編碼後的 byte 長度，不是字元數）：

- 都小於門檻：兩個都存 SQLite 欄位，`prompt_payload_key`／`response_payload_key` 皆為 `NULL`
- 只有一個超過門檻：超過的那個寫進 `{run_id}/{trace_id}.md`（單一 Markdown header，如只有 `## Response`），對應的 `_payload_key` 記檔案路徑，SQLite 欄位為 `NULL`；沒超過的那個維持存 SQLite 欄位，繼續享有 FTS5 索引
- 兩者都超過門檻：兩個都寫進**同一個** `{run_id}/{trace_id}.md`（`## Prompt`／`## Response` 兩個 header），兩個 `_payload_key` 指向同一個檔案路徑

`llmlog show <trace_id>`（十一章）讀取時，對每個欄位分別判斷該讀 SQLite 欄位還是讀檔案對應段落。

### 呼叫開始即寫入 running row

**寫入分兩段，不是只在呼叫結束後寫一次**：`record_llm_call_start()` 在呼叫實際發出**之前**立刻寫入一筆 `status="running"` 的 row（只含 `prompt`，`response`／`latency_ms`／token 用量等結束才知道的欄位留 `NULL`）；`record_llm_call()` 在呼叫結束的 `finally` 用**同一個 `trace_id`** upsert（`INSERT ... ON CONFLICT(trace_id) DO UPDATE`）補齊剩餘欄位，同一筆 row 從 `running` 轉成 `ok`／`error`／`timeout`，不是兩筆獨立紀錄；`ts` 因此代表**呼叫起始時間**，upsert 時刻意不覆寫。

**動機**：真實環境曾發生「長時間等待本地 Ollama、期間或事後都查不出當時送了什麼 prompt」的情況（見 09b_bug_trace.md）——若只在 `finally` 寫入，一次還沒結束（甚至卡住很久）的呼叫在 `llm_traces.db` 裡完全不存在，`llmlog` 查不到任何東西。分兩段寫入後，`llmlog recent --status running` 或 `llmlog show <trace_id>` 在呼叫進行中就能看到完整 prompt、以及已經耗時多久，不必等呼叫結束、也不必依賴事後回溯 git 歷史猜測。

**UPSERT 對 FTS 同步沒有額外風險**：SQLite 的 `ON CONFLICT DO UPDATE` 走到 UPDATE 分支時，觸發的是既有的 `llm_traces_au`（`AFTER UPDATE`）trigger，不是 `llm_traces_ai`（`AFTER INSERT`）——七章既有的三個 trigger 已經涵蓋這個路徑，不需要新增第四個 trigger。

**`record_llm_call_start()` 寫入失敗（或呼叫端因故跳過）時的退化行為**：跟 `record_llm_call()` 一樣只記警告、不拋例外；`record_llm_call()` 之後的 `INSERT ... ON CONFLICT` 在找不到既有 row 時會直接退化成一般 INSERT，行為等同這個機制加入之前的既有邏輯——不會因為 start 這一段沒寫成功就讓最終結果也記錄不到。

### 寫入規則

- **先寫檔案，再 INSERT SQLite**——避免 dangling pointer：若順序反過來，INSERT 先成功但檔案寫入失敗，`prompt_payload_key`／`response_payload_key` 會指向一個不存在的檔案
- `PRAGMA journal_mode=WAL; busy_timeout=5000; synchronous=NORMAL;`
- **同步寫入＋一個模組層級 `threading.Lock`，不是獨立背景執行緒＋佇列**：本專案已查證兩條呼叫路徑的實際併發情況——Claude 端 Map 階段用 `ThreadPoolExecutor`（見 00 六章）會有多執行緒同時呼叫，Ollama 端已被 `MODEL_SEMAPHORE(1)` 序列化——加上六章已查證兩者跑在同一個 Python process，用同步寫入＋鎖（比照 `common/llm_client.py::_get_client()` 現有的雙重檢查鎖定風格）就足夠：本專案量級（500 次/天）下，鎖爭用時間是毫秒級，不需要額外背景執行緒管理生命週期的複雜度
- **已知取捨**：九章 Ollama 這條呼叫鏈是 `async def`，`finally` 區塊呼叫同步的 `record_llm_call()` 會短暫佔用當下的 event loop（一次 SQLite INSERT，毫秒級）；`MODEL_SEMAPHORE(1)` 已把這條路徑序列化，同一時間不會有其他協程搶 event loop，接受這個取捨
- `PRAGMA busy_timeout` 是另一層防禦，服務不同情境：`llmlog` CLI（獨立行程）在 Orchestrator 還在跑的時候執行 `flag`／`gc` 這類會寫入的指令時，才是真正的跨 process 併發寫入，這時才需要靠 WAL＋`busy_timeout`；`threading.Lock` 只在同一個 process 內有效，不處理跨 process 情境，兩層防禦分工不同、缺一不可
- 寫入失敗（磁碟問題等）只記警告、不拋例外——這是稽核用的旁路紀錄，不該讓記錄本身的問題打斷真正在做的 LLM 呼叫流程。**這條規則只適用於「trace 落地本身失敗」，跟八、九章的「LLM 呼叫本身失敗仍要保證寫入 trace」是兩層不同的保證**

### `llmlog gc` 與寫入順序的交互作用

「先寫檔案、再 INSERT SQLite」這個順序本身沒問題，但若 `llmlog gc`（掃 `payloads/` 清孤兒檔）在 Orchestrator 還在跑的時候執行，可能掃到「payload 檔案已經寫完、但對應那筆 INSERT 還沒真的完成」的中間態——若不處理，`gc` 會把這個檔案當孤兒刪掉，接著 INSERT 完成，`prompt_payload_key`／`response_payload_key` 就指向一個已經被刪除的檔案，變成新的 dangling pointer，正是「先寫檔案再 INSERT」這個順序原本要防的事，被 `gc` 自己繞了回去。

**設計約束**：`gc` 只能刪除 mtime 早於寬限期之前的孤兒檔案，不能無條件刪除「DB 裡查無對應的所有檔案」——這是正確性要求，不是效能優化。

**執行方式與寬限期定案**：`gc_orphan_payloads()` 只由 `llmlog gc` 手動觸發，不掛進 `main.py` 的啟動流程——自動在 Orchestrator 啟動時清理，會讓 pipeline 啟動時間耦合到 `payloads/` 目錄大小，且清理本來就不是每次執行都需要的操作，手動觸發更簡單、不引入這層耦合。

寬限期預設 **24 小時**（`gc_orphan_payloads(grace_period_hours=24.0)`），對齊 `docker-gc` 等第三方清理工具對這類「先寫檔案、後寫 metadata」孤兒清理的常見預設（write-then-record 模式的建議範圍通常落在 24～72 小時）。我們的實際競態視窗遠小於這個範圍（同一函式內「寫檔案→緊接著 INSERT」，中間沒有網路、沒有跨行程協調，正常是毫秒級），但拉長寬限期在我們的情境幾乎零成本——`gc` 是手動觸發，不是排程清理，量級（一天頂多幾百筆）下多留 24 小時的磁碟成本可忽略。之後磁碟成長率（十四章）實測比預期快很多時，這個預設值可以直接調整，不影響「必須有寬限期」這個結構性設計本身。

---

## 八、Claude API 呼叫整合點

`common/llm_client.py::call_claude_for_json()` 是所有 Claude API 呼叫的唯一入口（①③⑦、[P]、[B] 共用，見 00 六章）。它不透過 `log_usage(response, *, model, caller)` 記錄——那個簽名結構上就拿不到 `system_prompt`／`user_prompt`／呼叫起訖時間，硬要沿用只會讓 `prompt` 欄位在 Claude 路徑上永遠是 `NULL`，等於沒解決本文件要解決的核心問題。

### `log_usage()` 退場

已核對全 repo，`log_usage()` 目前唯一呼叫端就是 `call_claude_for_json()`（見二章），沒有其他生產程式碼直接呼叫它。**`common/llm_usage_logger.py` 整個模組退場**，`call_claude_for_json()` 改成直接呼叫 `common/llm_trace.py::record_llm_call()`。

### 控制流程需求（完整程式碼見 11b）

**這個函式是同步函式（`def`，不是 `async def`）**——00 六章、六章「`ThreadPoolExecutor` 下的 contextvars」都指向同一件事：Map 階段用 `concurrent.futures.ThreadPoolExecutor.submit()` 呼叫它，`call_claude_for_json()` 是被丟進執行緒池、用**阻塞呼叫**的方式跑的普通函式，不是 `await` 出來的協程；若寫成 `async def`，既有呼叫端會拿到一個 coroutine 物件而不是解析好的 dict，直接破壞相容性。

**簽名**（既有必填參數不變，新增全部是選填）：

`call_claude_for_json(*, system_prompt, user_prompt, schema, model, run_id=None, task_id=None, target_file=None, class_name=None, function_name=None, max_tokens=DEFAULT_MAX_TOKENS) -> Any`

**11b 實作時必須滿足的控制流程需求**（順序即約束，不是建議）：

1. 進入函式立即解析：`caller`（用 `sys._getframe(1)`，不是既有 `log_usage()` 沿用的 `inspect.stack()[1]`——後者會建完整呼叫堆疊並讀取原始碼檔案，`sys._getframe(1)`／`inspect.currentframe().f_back` 拿到同樣的 frame 資訊但便宜一到兩個數量級，這次順手一併換掉）、`resolved_run_id = run_id or adhoc_run_id()`（六章：這個函式本身就是呼叫鏈最外層，fallback 在這裡解析一次即可）、`trace_id = uuid4()`、`current_trace_id.set(trace_id)`、呼叫起始時間（`time.monotonic()`）；`status` 預設值必須是 `"error"`，只有真正成功時才改
2. **呼叫 API 之前**先呼叫 `record_llm_call_start()`（見七章「呼叫開始即寫入 running row」），帶入這次組好的 prompt——這一步失敗（或被跳過）不影響後續流程，純粹是讓長時間等待中的呼叫也查得到 prompt
3. 主要邏輯依序：呼叫 API → 取出 `usage`／`raw_text` → `json.loads(raw_text)` → **只有這裡** `status = "ok"` → `return`
4. 例外分支必須按這個順序排列（由具體到泛用，Python `except` 由上而下比對，子類別必須放在對應父類別之前）：
   - `anthropic.APITimeoutError`（`APIError` 的子類別，必須排在下一項之前，否則永遠被泛用分支接住）→ `status="timeout"`
   - `anthropic.APIError` → `status="error"`，順帶記 `http_code`
   - `json.JSONDecodeError` → `status="error"`（`raw_text` 這時已有值，附進 `LlmJsonError`）
   - `Exception` 兜底（涵蓋以上三種以外真正意外的例外，如 SDK 內部錯誤、`response` 物件形狀不符預期）→ `status="error"`，`error_msg=repr(exc)`，**原樣 `raise`**（不轉換成 `LlmJsonError`，那不屬於這個函式的錯誤契約）；沒有這個分支，這類例外會留下一筆 `status='error'` 但 `error_msg=NULL` 的 row，有留痕但查不出原因，等於沒查
5. `finally` 區塊：**`current_trace_id.reset(token)` 必須排在 `record_llm_call()` 之前**，見下方「為什麼 `reset()` 要排在 `record_llm_call()` 之前」；`latency_ms` 用 `time.monotonic()` 差值；把 `trace_id`／`resolved_run_id`／`vendor="claude"`／`attempt=0`（固定值，見七章 schema `NOT NULL`）／`task_id`／`target_file`／`class_name`／`function_name`／組好的 `prompt`（跟步驟 2 傳給 `record_llm_call_start()` 的同一份字串，不重新組一次）／`raw_text`／`usage` 的四個 token 欄位／`latency_ms`／`status`／`http_code`／`error_msg` 一併傳進 `record_llm_call()`（`trace_id` 跟步驟 2 相同，UPSERT 靠這個對到同一筆 row，見七章）

**為什麼 `reset()` 要排在 `record_llm_call()` 之前**：七章「寫入規則」要求 `record_llm_call()` 內部吞掉自己的寫入例外（只記警告），但那是 `record_llm_call()` 自己的內部契約，不是 Python 語言保證——萬一它自己出現非預期的 bug 而拋出例外，若 `reset()` 排在它後面，`reset()` 就永遠不會執行，`current_trace_id` 會卡在這次呼叫的 `trace_id` 上，殘留到這個（可能被 `ThreadPoolExecutor` 重用的）執行緒後續印的所有 log，而且這個新拋出的例外會取代掉 `try` 區塊裡原本正在傳遞的例外（Python `finally` 的固有語意），讓真正的錯誤原因被蓋掉。`record_llm_call()` 不需要讀 contextvar（`trace_id` 是用參數傳進去的），兩行對調完全安全，不影響任何行為。

### `target_file`／`class_name`／`function_name` 的填寫範圍

七章 schema 這三個欄位原本是為 Ollama 填空模式（九章）設計的，但十一章的 `llmlog func` 查詢本來就不該只服務 Ollama——① 解析 Agent、③ 架構設計 Agent 對單一 Java 檔案／class 做語意分析時，一樣有「這支 Claude 呼叫是針對哪個檔案」的問題，值得留痕。因此把這三個參數提升成 `call_claude_for_json()` 的通用選填參數。

**但不是每個呼叫都填得出來，這是欄位定義決定的範圍，不是遺漏**：00 六章「Map 階段切批次」訂了 `chunk_by_char_budget()`——① 的 Map 階段依累積字元數切批次，一次 Claude 呼叫常常涵蓋**多個** Java class，這種批次呼叫語意上無法化約成單一 `target_file`／`class_name`，維持 `NULL` 是正確行為。實際會填入這幾個欄位的情境，是呼叫本身**天然對應單一檔案／類別**的時候（例如某個 class 單獨自成一批）。

### `task_id` 選填參數

Ollama 填空模式（⑤）之外，⑦ Debug Agent 的呼叫也天然對應到一個具體的 task——它的輸入本來就是「fail 清單＋diff 報告＋對應 Python 原始碼」（見 00 七章），分析的對象往往就是某個 task 寫出來的程式碼。若 `call_claude_for_json()` 不開放 `task_id`，`llmlog task <id>` 就只能查到 Ollama 那次「怎麼寫的」，查不到 Debug Agent 事後「怎麼分析這次失敗的」——這兩個問題在排查同一個持續失敗的函式時，通常是一起想知道的。`debug_node.py` 呼叫 `call_claude_for_json()` 時，若能定位到具體 task（例如 `failed_tasks` 清單裡有明確的 task id），就帶入這個參數；分析範圍橫跨多個 task／整個 module 時維持 `NULL`，理由同上一節。

### 與既有用量記錄的整合關係

`input_tokens`／`output_tokens`／`cache_creation_input_tokens`／`cache_read_input_tokens` 這四個欄位定義沿用 00 六章「Claude API 呼叫用量記錄」的既有規格，**不是重新設計一套 token 估算邏輯**，只是儲存位置從 `logs/claude_api_usage.jsonl` 換成 `llm_traces.db`。`logs/claude_api_usage.jsonl` 視為**淘汰**，不做雙寫、不做資料遷移。

---

## 九、本地 Ollama 呼叫整合點

`translator_cli/ollama_client.py::_call_ollama_once()` 是唯一真正打 HTTP 請求的地方，記錄的觸發點放在 `get_function_body()` 的重試迴圈內——理由：`_call_ollama_once()` 只知道單次 HTTP 請求的結果，不知道這是第幾次格式修正重試（`attempt`，見七章 schema）；`get_function_body()` 的重試迴圈才握有這個資訊，且它是 `translator_cli/client.py::fill_function()` 唯一呼叫的入口，改動範圍最小。這條呼叫鏈在既有程式碼裡本來就是 `async def`（`httpx.AsyncClient`、`asyncio.sleep`），跟八章 Claude 那條（同步）不同，維持原樣即可。

### 需要新增的參數傳遞路徑

目前 `fill_function()` 呼叫 `ollama_client.get_function_body()` 時只傳了 prompt 組裝需要的欄位，**沒有傳 `task_id`／`target_file`／`class_name`／`run_id`**——這正是 09b_bug_trace #35 的根本原因。

### run_id 的解析只在 fill_function() 做一次

`get_function_body()` 是一個帶格式修正重試迴圈的函式（最多 3 次呼叫），若「`run_id` 沒傳就生一個」這個 fallback 判斷放在這一層，旁路情境下三次 attempt 會各自呼叫 `adhoc_run_id()`、各自拿到不同的 run_id——直接破壞七章「同一次測試的 payload 天然聚在 `{run_id}` 資料夾」的設計。正確做法：fallback 只在呼叫鏈最外層的 `fill_function()` 解析一次（`resolved_run_id = run_id or adhoc_run_id()`），得到具體值後原樣往下傳給 `get_function_body()`；`get_function_body()` 的 `run_id` 參數因此是**必填**（`str`，不是 `str | None`），這一層不再自己判斷。完整程式碼見 11b。

### 記錄語意：一個 attempt 恰好一筆，寫在 finally（完整程式碼見 11b）

**簽名**：`get_function_body(*, current_signature, description, context, context_files, function_name, task_id=None, target_file=None, class_name=None, run_id: str) -> str`

**每個 attempt（不論成功、格式錯誤、還是傳輸層例外）恰好產生一筆 trace row，寫入時機統一在該次 attempt 的 `finally` 區塊**——不是「呼叫前一筆、呼叫後又一筆」。11b 實作時，重試迴圈（`for attempt in range(_FORMAT_RETRY_COUNT + 1)`）每一輪必須滿足：

1. 迴圈一開始（組完 `user_prompt` 之後）就產生這次 attempt 的 `trace_id`、`current_trace_id.set(trace_id)`、記起始時間，`status`／`error_msg`／`raw_text` 預設 `"error"`／`None`／`None`
2. **呼叫 `_call_ollama_once()` 之前**先呼叫 `record_llm_call_start()`（見七章「呼叫開始即寫入 running row」）——本地模型單次生成可能耗時數十秒到數分鐘，這是 09b_bug_trace.md 記錄的真實痛點（長時間等待 Ollama、查不出當時送了什麼），這一步是直接對策
3. `try` 主體依序：呼叫 `_call_ollama_once()` → `_extract_delimited_body()` → `python_adapter.extract_body_statements()` → `status="ok"` → `return`
4. 例外分支：
   - `(TranslatorCliNetworkError, TranslatorCliConfigError)`——傳輸層／環境變數缺失，既有設計本來就不進格式重試迴圈、直接往外拋（見 07a 七章，這裡不改變這個既有邏輯）；`status` 依例外是否為逾時類（`exc.__cause__` 是 `httpx.TimeoutException`）分成 `"timeout"`／`"error"`
   - `TranslatorCliModelOutputError`——delimiter／語法驗證失敗，`raw_text` 依然有值（模型確實回應了，只是內容不符契約）；`attempt` 未達 `_FORMAT_RETRY_COUNT` 時不 `raise`，讓迴圈自然進下一次 attempt，但這次 attempt 仍要透過 `finally` 留痕；達到上限才 `raise`
   - `Exception` 兜底（涵蓋以上兩類以外的意外例外，如 `extract_body_statements()` 內部真正非預期的錯誤，理由同八章「例外分支」）→ `status="error"`，`error_msg=repr(exc)`，原樣 `raise`
5. `finally` 區塊：**`current_trace_id.reset(token)` 必須排在 `record_llm_call()` 之前**（同八章「為什麼 `reset()` 要排在 `record_llm_call()` 之前」）；把 `trace_id`（跟步驟 2 相同，UPSERT 靠這個對到同一筆 row）／`run_id`／`vendor="ollama"`／`model=OLLAMA_MODEL`／固定 `caller="ollama_client.get_function_body"`／`task_id`／`attempt`／`target_file`／`class_name`／`function_name`／組好的 `prompt`（跟步驟 2 相同的字串，不重新組一次）／`raw_text`／`latency_ms`／`status`／`error_msg` 一併傳進 `record_llm_call()`

（`httpx.TimeoutException` 的判斷方式、`TranslatorCliNetworkError.__cause__` 是否真的攜帶原始例外等細節留給 11b 核對 `ollama_client.py` 現有的例外包裝邏輯。）

**與既有重試邏輯的相容性**：`TranslatorCliNetworkError`／`TranslatorCliConfigError` 目前的行為是「不重試，直接往外拋」（07a 七章已定案），這個既有邏輯完全不變，這裡只是保證留痕，不是新增一層重試判斷。

### token 用量

Ollama（OpenAI 相容端點）的回應理論上有 `usage` 欄位，若上游 nginx／ollama 版本回傳了就記錄，沒有就留 `NULL`——這條路徑的核心價值是 prompt／response 內容而非 token 稽核（見八章「與既有用量記錄的整合關係」，token 估算的稽核動機本來就只針對 Claude API）。若需要對 context 大小有感，可選擇用 `len(prompt)` 字元數當替代指標存進 `note` 欄位，不強行換算成 token 數（qwen 的 tokenizer 跟 Claude 不同，換算不準確）。

---

## 十、查詢工作流程：如何找到「某次測試的 task」的 prompts

這是本文件的核心交付目標，走一遍實際案例（沿用 09b_bug_trace 記錄的真實情境：`exam` 模組的 `ExamService.create_random` 持續翻譯失敗）。

### 情境一：這次測試剛跑完，想看某個失敗 task 當時送了什麼 prompt

1. `run_tests` 產出的 `test_results` report 裡，`failures[].related_files` 能定位到 Python 檔案，但要找到「當初填空時送的 prompt」，需要先知道這次 run 的 `task_id`——`partial_reports`（module 局部驗證結果）或 `implement` node 的 `failed_tasks` 清單裡就有
2. `llmlog task task_043`（預設查最新一次 `run_id`，語意見十一章；不確定時可加 `--run <run_id>`）→ 列出這個 task 在該次 run 底下的所有 attempt（含格式重試的每一次；八章新增後，也可能列出 ⑦ Debug Agent 針對這個 task 的分析呼叫，若當時有帶 `task_id`）
3. `llmlog show <trace_id>` 看完整 prompt／response；payload 較大時預設截斷，加 `--full` 看全文

### 情境二：想確認某支函式是不是「這次才開始壞」，還是每次重跑都壞（09b_bug_trace 的真實需求）

`task_id` 在不同次重跑不保證相同（六章），此時改用函式身分查詢，跨 run 一次看到所有歷史：

```bash
llmlog func --file app/services/exam_service.py --class-name ExamService --function create_random
```

這個查詢**不帶 `--run`**，天然回傳所有 run 的紀錄，依時間排序。

### 情境三：不知道 task_id，只知道「某個 case 測試失敗」

Harness report 的 `failures[].related_files` 給的是 Python 檔案路徑，不是函式層級。若要收斂到具體函式，先用 `route_to_file_mapping`／`task_list` 反查該檔案對應哪些 task（人工核對），再套情境一或二。

### 情境四：不確定問題出在哪，先找「最近失敗的呼叫」

```bash
llmlog recent --status error --since 24h
llmlog search "search_answer" --tag format_error
```

### 情境五：從一般 log 裡的一行錯誤，反查是哪次 LLM 呼叫

`logs/orchestrator.log` 裡看到一行 `ERROR ... [20260821_140203_a1b2c3|f47ac10b-...] ollama 連線失敗`，方括號裡第二段就是 `trace_id`，直接 `llmlog show f47ac10b-...`。

若看到的是 `[adhoc_20260821_...|...]`（`run_id` 帶 `adhoc_` 前綴，見六章），代表這次呼叫不是走正常的 graph 執行路徑產生的 run_id——先確認是不是刻意的旁路呼叫（單元測試、人工續跑捷徑），若不是，很可能是某個 node 忘記把 `state["run_id"]` 傳下去，值得回頭檢查呼叫端。

### 情境六：pipeline 正在長時間等待，想知道現在卡在哪個 prompt（09b_bug_trace 的真實需求）

這是七章「呼叫開始即寫入 running row」要解決的場景：不必等呼叫結束，也不必看 git 歷史猜測。

```bash
llmlog recent --status running        # 列出目前還沒結束的呼叫
llmlog show <trace_id>                # 看完整 prompt，並顯示已耗時多久
```

`show` 對 `status="running"` 的 row 會額外算出「目前已耗時約 N 秒」（用 `ts`——呼叫起始時間——跟現在時間相減），不需要自己心算。若這個 trace 遲遲停在 `running` 沒有變化，且耗時已經明顯超過 `TRANSLATOR_CLI_TIMEOUT_SECONDS`（預設 300 秒）加上重試預算，代表這次呼叫本身有異常（例如網路層卡住但沒有正確逾時），而不是模型單純算得慢——這個判斷依據前提有兩者的 log/trace 都要交叉比對，只看 `llmlog` 不夠時，回頭比對五章一般 log 裡同一個 `trace_id` 有沒有出現重試相關的 WARNING。

---

## 十一、CLI 概覽（`llmlog`）

實作細節、參數完整清單見 11b。這裡列出對外命令集，作為十章查詢工作流程的依據。

### 「當前 run」的查詢語意

`llmlog` 是獨立行程，不是 Orchestrator 的一部分，**不能用 `common/run_context.py::new_run_id()` 生一個新的**——那樣產生的 run_id 在 `llm_traces.db` 裡不存在任何資料，查詢永遠是空的。明確定義：省略 `--run` 時，查詢範圍是**資料庫裡最新一筆 trace 所屬的 run_id**，對應七章「查詢與維護層設計」的 `latest_run_id()`（`SELECT run_id FROM llm_traces ORDER BY id DESC LIMIT 1`——用 `id` 不用 `ts`，避免同一秒多筆時無法決勝負，這正是七章改用 `INTEGER PRIMARY KEY AUTOINCREMENT` 額外換來的好處）。`llmlog recent`／`llmlog task` 省略 `--run` 時，先呼叫 `latest_run_id()` 解出具體值再查，不是自己另外重複一份邏輯。

這跟五章提到的 `configure_logging(run_id="cli", file_handler=False)`（`llmlog` 自己這幾行操作在一般 log 裡的標籤）是完全不同的兩件事，不要混用。

| 命令 | 對應函式（七章「查詢與維護層設計」） | 用途 |
|---|---|---|
| `llmlog recent [--status running\|ok\|error\|timeout] [--since <duration>] [--run <run_id>]` | `list_recent()` | 最近的呼叫，省略 `--run` 時先呼叫 `latest_run_id()` 決定查哪個 run；`--status running` 對應情境六，找出目前還沒結束的呼叫 |
| `llmlog search <關鍵字> [--tag <issue_tag>] [--vendor claude\|ollama] [--since <duration>]` | `search_traces()` | 全文搜尋 prompt／response（FTS5 查小 payload；大 payload 落在 `payloads/` 目錄，見下方「`rg` 依賴」，兩個來源的合併邏輯在 `search_traces()` 內部，不是 CLI 層） |
| `llmlog show <trace_id> [--section prompt\|response] [--full]` | `get_trace()` | 看單筆完整內容，預設截斷；情境五、六的落點；內部依 `prompt_payload_key`／`response_payload_key` 分別判斷該讀 SQLite 欄位還是讀檔案；`status="running"` 時額外顯示已耗時多久 |
| `llmlog task <task_id> [--run <run_id>]` | `get_traces_for_task()` | 十章情境一：查某個 task 在（指定或最新）run 底下的所有 attempt |
| `llmlog func --file <path> [--class-name <name>] --function <name>` | `get_traces_for_function()` | 十章情境二：跨 run 查某支函式的所有歷史紀錄 |
| `llmlog flag <trace_id> --tag <issue_tag> --note "<原因>"` | `flag_trace()` | 人工／Claude Code 標記問題類型；`--tag` 值須落在七章 schema 的 `CHECK` 列舉內，CLI 端也要做一次同樣的檢查、給出比 SQL constraint 錯誤更好讀的訊息，不是只靠資料庫層那道防線 |
| `llmlog gc` | `gc_orphan_payloads()` | 掃 `payloads/` 比對 DB 的 `prompt_payload_key`／`response_payload_key`，清孤兒檔——**只刪 mtime 早於寬限期（預設 24 小時）的檔案**（見七章「`llmlog gc` 與寫入順序的交互作用」，這是硬性約束，不是可選項）；只能手動觸發，不掛進 `main.py` 啟動流程 |

### `rg` 依賴

`search_traces()` 補查大 payload 的機制依賴 `ripgrep`（`rg`），但 Windows 開發機不一定預裝。設計上不把它當硬性依賴：啟動時用 `shutil.which("rg")` 探測（沿用本專案 `core/postman_runner.py::run_newman()` 對 `newman` 執行檔的既有作法，見 02b），探測到就用 `rg` 加速；探測不到則退回純 Python 的 `pathlib.Path.rglob()` + 逐檔字串比對——功能上一樣找得到，只是量大時速度較慢，不會因為環境沒裝 `rg` 就直接不能用。

### 建議實作順序

刻意分階段，不要一起寫，跟十四章「100KB 門檻要用真實 payload 分布驗證」是同一個邏輯——沒有真實資料前，`show`／`recent` 的輸出格式、`search` 的查詢體驗都只能憑空猜：

1. **寫入端**：`record_llm_call_start()`／`record_llm_call()` + 建表（含 trigger），接上 `call_claude_for_json()`／`get_function_body()`，實際跑幾天，累積真實資料
2. **`show` 和 `recent`**：最常用，等有真實資料後再決定輸出格式怎麼排最好讀
3. **`search`／`flag`／`gc`／`task`／`func`**：最後補，`task`／`func` 是本文件新增的查詢維度（原始的 CLI 設計沒有這兩個，六章「函式身分」的識別碼設計才讓這種跨 run 查詢變得有意義），設計已在十章驗證過查詢邏輯，實作順序上仍歸在「最後補」這一批

### `CLAUDE.md` 引導內容

11b 落地時，專案的 `CLAUDE.md`（或等同的說明檔）需要補一段，讓 Claude Code 知道排查 LLM 呼叫問題時該用 `llmlog`、不要手動翻 `logs/`：

```markdown
## LLM 呼叫紀錄查詢

排查 prompt 問題、幻覺、LLM API 失敗、pipeline 卡住時，用 `llmlog`，不要手動翻 ./logs。

- `llmlog recent --status running`   pipeline 正在長時間等待時，查現在卡在哪個 prompt（顯示已耗時多久）
- `llmlog recent --status error --since 24h`   最近的失敗
- `llmlog search <關鍵字> [--tag <tag>] [--since 7d]`   全文搜尋
- `llmlog show <trace_id> [--section prompt|response] [--full]`
- `llmlog task <task_id> [--run <run_id>]`   查某個 task 的所有 attempt
- `llmlog func --file <path> [--class-name <name>] --function <name>`   跨 run 查某支函式的歷史
- `llmlog flag <trace_id> --tag <tag> --note "<原因>"`

tag 只能是：hallucination / format_error / logic_flaw / refused / truncated

先用 recent 或 search 縮小範圍，再對單筆 show；不知道具體是哪次呼叫時用 task／func；懷疑卡住時先用 recent --status running。
payload 可能很大，show 預設截斷，需要全文才加 --full。
```

---

## 十二、環境變數與設定

新增到 `.env`（比照 01 二章既有 `.env` 範例的段落慣例）：

```bash
# 一般執行 log（見 11a 五章）
ORCHESTRATOR_LOG_PATH=logs/orchestrator.log
ORCHESTRATOR_LOG_LEVEL=INFO

# LLM Trace 統一儲存（見 11a 七章）
LLM_TRACE_DB_PATH=logs/llm_traces.db
LLM_TRACE_PAYLOAD_DIR=logs/payloads
LLM_TRACE_PAYLOAD_THRESHOLD_BYTES=100000
```

`CLAUDE_API_USAGE_LOG_PATH`（既有，`common/llm_usage_logger.py`）隨本模組退場一併移除。第三方套件 log 等級（五章）刻意不給環境變數，理由見該章節。`rg`（十一章）不是必要環境依賴，沒裝時自動退回 Python 內建掃描，不需要在這裡列成必要條件。

---

## 十三、對既有模組的相容性與遷移

| 模組 | 變動範圍 | 相容性 |
|---|---|---|
| `01_langgraph_architecture.md` / `graph/state.py` | `RefactorState` 新增 `run_id: str` 欄位（六章） | 需要 `main.py` 九章的 `initial_state` 同步補上這個 key；其餘既有欄位不變 |
| `main.py` | 產生 `run_id`（`common/run_context.py::new_run_id()`）、寫入 `initial_state`，`logging.basicConfig(...)` 換成 `configure_logging(run_id=run_id, file_handler=True)` | 行為等價，是既有暫時性修補的正式化 |
| `common/llm_usage_logger.py` | **整個模組退場**（八章），改由 `common/llm_trace.py::record_llm_call()` 取代 | `call_claude_for_json()` 不再 import 這個模組；`tests/common/test_llm_usage_logger.py` 需要在 11b 一併移除或改寫成測試 `common/llm_trace.py`；`tests/common/test_llm_client.py` 目前 `monkeypatch.setattr(llm_client, "log_usage", ...)` 那幾處（見該檔案 81/156/182 行）改成 `monkeypatch.setattr(llm_client, "record_llm_call", ...)` |
| `common/llm_trace.py`（**新模組**） | `llm_traces.db` 讀寫的唯一權威來源，十個函式見七章「查詢與維護層設計」表格（含 `record_llm_call_start()`）；連線 lazy 建立（比照 `_get_client()`），單一連線＋一把模組層級 `threading.Lock` | 全新檔案，無相容性負擔 |
| `debug_agent/analysis.py`（見 `10a_debug_agent_architecture.md` 九章） | 可直接呼叫 `common.llm_trace.get_traces_for_task()` 取得歷史 prompt／response 作為分析輸入（見七章「查詢與維護層設計」） | 純新增用法，10a 自己的 12 章待決定事項已同步指向這裡（見下一行） |
| `10a_debug_agent_architecture.md` 九章、十二章 | 九章 `debug_agent/llm.py` 的模組結構註解仍引用即將退場的 `log_usage()`／`logs/claude_api_usage.jsonl`；十二章「`debug_rounds` 歷史是否要餵回下一輪 LLM 呼叫」待決定事項在本文件之前沒有對應機制可用 | 兩處都已同步修正，指向本文件（`common/llm_client.py` 的新記錄機制、`common/llm_trace.py` 的查詢層），見該文件對應章節 |
| `common/run_context.py`（**新模組**） | 提供 `new_run_id()`（正式產生點，僅 `main.py` 呼叫）與 `adhoc_run_id()`（旁路 fallback，見六章） | 全新檔案，無相容性負擔 |
| `common/trace_context.py`（**新模組**） | 提供 `current_trace_id: ContextVar`（見六章） | 全新檔案，無相容性負擔 |
| `common/logging_setup.py`（**新模組**） | 提供 `configure_logging(run_id, *, file_handler=True)`（五章，含 console 編碼修正、第三方套件過濾） | 全新檔案，無相容性負擔 |
| `common/llm_client.py` | `call_claude_for_json()` 是明確的**同步函式**（不是 `async def`，見八章）；新增選填參數 `run_id`／`task_id`／`target_file`／`class_name`／`function_name`；新增 `anthropic.APITimeoutError`／泛用 `Exception` 例外分支；呼叫 API 之前先呼叫 `record_llm_call_start()`，`try/except/finally` 包住整段呼叫，`reset()` 排在 `record_llm_call()` 之前（八章） | 對外既有必填參數與回傳值、例外類型（`LlmJsonError`）全部不變，新增的都是選填參數，舊呼叫端不傳也能動作 |
| `parse_agent`／`design_agent` 的 Map 階段呼叫端 | 呼叫 `call_claude_for_json()` 時，語意對應單一 Java 檔案／class 的呼叫可選擇性帶入 `target_file`／`class_name` | 純新增選填引數，不影響既有呼叫 |
| `debug_node.py` | 呼叫 `call_claude_for_json()` 時，能定位到具體 task 時帶入 `task_id`（八章） | 純新增選填引數 |
| `translator_cli/ollama_client.py` | `get_function_body()` 新增選填參數 `task_id`／`target_file`／`class_name`，`run_id` 改為**必填**；重試迴圈內每個 attempt 加 `try/except/finally`（含泛用 `Exception` 分支），`reset()` 排在 `record_llm_call()` 之前（九章） | 舊呼叫（不帶 `task_id`/`target_file`/`class_name`）仍可運作；`run_id` 改必填是破壞性變更，但 `translator_cli/client.py::fill_function()` 是唯一呼叫端，會同步更新帶入解析好的值 |
| `translator_cli/client.py` | `fill_function()` 新增選填參數 `run_id`，在函式最開頭解析 `resolved_run_id = run_id or adhoc_run_id()` | 不影響 `fill_function()` 既有必填參數與回傳型別 |
| `graph/nodes/implement_node.py` | 呼叫 `translator_cli.fill_function()` 時補上 `run_id=state["run_id"]` | 純新增引數 |
| `llmlog`（新 CLI，11b 落地） | 呼叫 `configure_logging(run_id="cli", file_handler=False)`，只掛 console handler（五章「`llmlog` 為什麼不掛 file handler」） | 避免 Windows 上與 Orchestrator 主行程搶 `logs/orchestrator.log` 的 rollover |
| `00_refactor_architecture.md` | 四章環境架構圖原把 translator-cli 標成「subprocess 呼叫」，與六章查證的實際 in-process 執行模型不符 | **已修正**：改為「Python 套件，in-process 呼叫，非獨立子行程」並註明依據見本文件六章 |
| `00_refactor_architecture.md` 六章 | 「Claude API 呼叫用量記錄」整節（`log_usage()` 用法、`logs/claude_api_usage.jsonl` 格式）描述的機制已被本文件取代 | **已修正**：改為指向本文件與 `common/llm_trace.py::record_llm_call()`，不再重複描述已退場的機制 |
| `logs/claude_api_usage.jsonl` | 停止寫入，視為淘汰 | 不遷移舊資料 |
| `watch_signals.py`（另一個 session 為了 console cp950 編碼問題寫的土法修補腳本） | 五章補上 console handler 的 UTF-8 reconfigure 後，這個腳本存在的理由消失 | 是否要一併移除留給 11b／使用者決定，這裡只記錄「不再需要」這個事實，不代為刪除既有檔案 |

---

## 十四、待決定事項

- [ ] **`template_version` 的實際填法**：目前各 Agent 的 `prompts.py` 沒有版本標記機制，這欄位先允許填 `NULL`，等哪個 Agent 的 prompt 開始需要 A/B 比較時再回頭定案怎麼標版本
- [ ] **100KB 門檻是否合適**：目前是估算值，需要接上真實流量、看實際 payload 大小分布後才能驗證這個切點是否恰當
- [ ] **磁碟成長率驗證**：估算 500 次/天 × 20KB ≈ 3.6GB/年，是否符合本專案實際呼叫頻率——這是需要真實流量才能驗證的數字，設計上無法先行拍板
- [ ] **`issue_tag` 標記動線**：目前設計是開發者／Claude Code 排查後手動呼叫 `llmlog flag`；要不要讓 Debug Agent（⑦）分析出結論後自動呼叫 `flag_trace()` 標記，還是維持人工決定要不要標，留待接上真實環境、觀察標記頻率後再評估——自動標記的風險是準確度不夠時會把 `issue_tag` 弄髒，比沒有標記更難清理
- [ ] **LangGraph checkpointer／`thread_id` 整合**：三章已明確排除在這次範圍外；一旦落地，`run_id` 的產生方式需要從「`main.py` 每次啟動產生新值」改為「優先沿用 `thread_id` 對應的既有 `run_id`」，屆時回頭修 `common/run_context.py::new_run_id()`
- [ ] **`QueueHandler`／`QueueListener`**：五章已評估過，這次不採用。若之後 Map 階段規模擴大、實測發現一般 log 寫入真的影響到 event loop 的反應速度，回頭在 `configure_logging()` 這一層加上，不需要改動任何呼叫端
- [ ] **`rg` 是否要列進 `00_refactor_architecture.md` 五章環境建置清單**：十一章已設計 `search` 在偵測不到 `rg` 時自動退回 Python 掃描，功能上不受影響；是否仍要為了效能把 `rg` 列成建議安裝項，留待使用者決定
- [ ] **行程異常終止時，`status="running"` 的孤兒 row 永遠留在資料庫**：正常路徑一定會走到 `finally` 把 row upsert 成最終狀態（見七章「呼叫開始即寫入 running row」），但若整個 Python process 被強制終止（`Ctrl+C` 沒被正確攔截、OOM kill、斷電），已經寫入的 `running` row 不會有任何後續更新，永遠停在 `running`。目前沒有處理機制——`llmlog recent --status running` 之後若看到一筆 `run_id` 明顯不是當前這次執行的舊資料，代表是這種孤兒 row，人工可以靠 `run_id`／`ts` 判斷這不是「現在正卡住」，只是「上次意外中斷留下的殘骸」；要不要讓 `llmlog gc` 或另一個機制把停留過久（例如超過 `TRANSLATOR_CLI_TIMEOUT_SECONDS` 加重試預算的合理上限）的 `running` row 自動轉成 `error`，留待接上真實環境、觀察這種孤兒 row 出現頻率後再評估

---

## 十五、文件索引

| 文件 | 內容 |
|---|---|
| `11a_logging_architecture.md`（本文件） | 全域 log 機制架構設計：一般 log／Claude API／本地 Ollama 呼叫紀錄／run_id／trace_id 識別碼設計 |
| `11b_logging_code.md` | 上述機制的實際程式碼實作，含 `llmlog` CLI |
| `09b_bug_trace.md` | #35 記錄了本文件要解決的缺口；「task 編號不是跨次重跑穩定識別碼」的發現是六章三層識別碼設計的直接依據 |

---

*本文件是全域 log 機制（一般 log／Claude API／本地 Ollama 呼叫紀錄）的定案版本，經多輪交叉審查修正後定案，隨實作推進持續更新。*
