# AI 全自動化重構測試：Java → Python

[English](README.en.md) | **繁體中文**

> **79 個函式，10 個 Agent，自動從 Java 翻成 Python，再用同一份測試驗證——最好一次，25 個測試過 20 個。**
> 三個版本、幾十輪實測，成功率、花費、工時全部公開，包括它做不到的部分。

## 專案亮點

- **多 Agent 分工，各司其職。** 10 個 Agent 各自獨立，由 LangGraph 串成有向圖，解析與錄製、設計與規劃可平行；流程由程式控制，只有需要判斷的環節才交給 LLM，一半的 Agent 完全不呼叫 AI。
- **用測試夾住重構。** 先錄下 Java 服務的回應，Python 服務通過同一份測試，才算翻對。
- **自動規格＋手工 Collection，測試範圍和翻譯範圍對齊。** OpenAPI 規格從運行中的 Java 服務取得，寫入類案例由人工填入真實資料。
- **找到並拿掉真正的瓶頸。** 第三版直接讓 Claude 看 Java 原始碼翻譯，最高三次平均成功率高達 **76%**。
- **三關依序翻譯。** 基礎零件 → 業務邏輯 → 最上層入口，後面的層直接讀前面已翻好的 Python。
- **大批次 Claude 任務用 map-reduce。** 輸入太大、不能整包塞進 prompt 時，依自然邊界（Controller、API 分組）切批平行分析，再彙整做跨邊界判斷；並行數依 CPU 核心數 − 1 動態計算。
- **雲端＋本地模型，失敗自動退回。** repository 層（資料庫查詢）的函式交給本地 `qwen2.5-coder:32b` 翻譯，出錯時自動切到 Claude，流程不中斷。
- **每次 AI 呼叫都可追溯。** 呼叫前先寫入 `running` 紀錄，結束後以同一個 `trace_id` 補上回應、token、耗時，卡死或逾時的呼叫也留得下 prompt。SQLite＋FTS5 全文索引統一記錄 Claude 與本地模型，`llmlog` 可搜尋，標記幻覺、格式錯誤。
- **改完 1～2 分鐘驗證。** Docker 熱重載，`partial_verify.py` 免重跑整條流程。
- **誠實的實驗紀錄。** 約 372 小時、每輪約 NT$100、累計約 NT$3,700，連做不到的部分也寫明。

---

## 專案簡介

把一個 Java Spring Boot 後端，用多個 AI Agent 自動翻成 Python FastAPI 後端，並用「測試夾住重構」確認翻完的行為跟原本一致。

這是一個實驗專案：目的是驗證「AI 全自動化重構」到底做得到什麼程度、要花多少時間與費用。結論在最後一節。

核心原則：**先錄下 Java 服務現有的回應（golden output），翻完之後用同一份測試打 Python 服務，兩邊回應一致才算過。**

---

## 1. 使用套件與技術

### 流程控制與 AI

| 類別 | 技術 | 用途 |
|---|---|---|
| 流程編排 | LangGraph 1.2.6 | 把各 Agent 串成有向圖，含平行分支、重試迴圈、人工填值關卡 |
| 雲端模型 | Claude API（`anthropic` 0.117.0，實際使用 `claude-sonnet-4-6`） | 解析、設計、翻譯（絕大部分函式）、除錯 |
| 本地模型 | ollama + `qwen2.5-coder:32b`（前面掛 nginx 做 token 驗證） | 翻譯 repository 層（資料庫查詢）的函式；出錯時自動退回 Claude |
| Java 解析 | `javalang` | 解析 Java 原始碼、建立呼叫圖、抽取方法原始碼 |

### 測試與驗證

| 類別 | 技術 | 用途 |
|---|---|---|
| API 規格 | springdoc-openapi-ui 1.7.0（Java 端） | 從 Spring annotation 產生 OpenAPI 3.0 |
| 測試腳本 | openapi-to-postmanv2、newman（Node.js） | OpenAPI → Postman Collection，並執行 |
| 回應比對 | `deepdiff`、`PyYAML` | 遮罩動態欄位後比對 golden output |
| 資料庫 | PostgreSQL（獨立的 `_TEST` 測試 DB）、`psycopg2-binary` | 每次驗證前 truncate + 重灌 seed |
| 目標服務執行環境 | Docker（`python:3.12-slim`） | 讓生成的 Python 服務在 Linux 容器內熱重載、供驗證 |

### 其餘

| 類別 | 技術 |
|---|---|
| 執行環境 | Python 3.13（Orchestrator）、`httpx`、`requests`、`python-dotenv` |
| 開發工具 | `pytest`、`ruff` |
| 呼叫紀錄 | SQLite（`logs/llm_traces.db`）＋ `llmlog` CLI，Claude 與本地模型共用 |
| 來源專案 | Java 8+／Spring Boot 2.7.11（`lang-exam-api-refactor`） |
| 目標專案 | Python 3.10+／FastAPI、SQLAlchemy、Pydantic、uvicorn |

---

## 2. 專案整體架構

### 執行流程

```
Java 專案
   ↓
[A] Spec Agent         啟動 Java 服務，取得 OpenAPI 規格
   ↓
[B] Collection Agent   轉成 Postman Collection（有人工填值關卡，未填完會暫停）
   ↓
① 解析 Agent           解析 Java，輸出模組清單、呼叫圖
   ↓
 ┌─┴────────────────────────┐   可平行
② 測試 Agent（錄製）      ③ 架構設計 Agent
  對 Java 服務錄下 golden     設計 Python 專案結構與函式簽名
 └─┬────────────────────────┘
   ↓
 ┌─┴─────────────────┐   可平行
[P] Plan Agent        ④ 骨架實作 Agent
  排順序、找出相關程式    建立空骨架
 └─┬─────────────────┘
   ↓
⑤ 功能改寫 Agent       Claude 直接看 Java 原始碼翻成 Python，分三關依序進行
   ↑                      第 1 關 基礎零件 → 第 2 關 業務邏輯 → 第 3 關 最上層入口
   │      ↓
   │   ⑥ 測試執行 Agent（驗證）  對 Python 服務跑同一份測試，比對 golden
   │      ↓
   │    全部通過 ─────────────▶ ✅ 完成
   │      ↓ 有失敗
   └── ⑦ Debug Agent      分析差異，直接寫出並套用修正，回到 ⑤ 重驗
          ↓ 超過重試上限或判定無法修
       give_up（通知人工）
```

### 目錄

| 目錄／檔案 | 內容 |
|---|---|
| `main.py` | 進入點：組裝並執行整張圖、備份上一輪產物、寫執行報告 |
| `partial_verify.py` | 局部驗證：不跑整條流程，只對已生成的 Python 服務跑挑選的測試案例 |
| `graph/` | LangGraph 的狀態、節點、排程器（三層關卡）、原始碼抽取 |
| `spec_collection_agent/` | [A]、[B] 的實作 |
| `parse_agent/`、`design_agent/`、`plan_agent/`、`scaffold_agent/`、`debug_agent/` | ①③[P]④⑦ 各自的實作 |
| `translator_cli/` | 寫入程式碼的工具：骨架生成模式、填空模式（可選 Claude 或本地模型） |
| `refactor_harness/` | 測試 Harness：錄製、驗證、遮罩、比對、報告（②⑥ 共用） |
| `python_service/` | 生成的 Python 服務的 Docker 容器管理 |
| `common/` | 跨 Agent 共用工具（Claude client、呼叫紀錄、型別對應等） |
| `llmlog/` | 查詢 LLM 呼叫紀錄的 CLI |
| `config/`、`specs/`、`postman/`、`fixtures/` | 設定、OpenAPI 規格、Postman Collection、seed 與 golden output |
| `docs/` | 各 Agent 的設計文件（`*a`）與程式碼文件（`*b`）、成果報告 |
| `tests/` | 單元測試 |

---

## 3. 各 Agent 功能簡介

| Agent | 做什麼 | 用不用 AI |
|---|---|---|
| **[A] Spec Agent** | 啟動 Java 服務，抓下完整的 OpenAPI 規格 | 否 |
| **[B] Collection Agent** | 把規格轉成 Postman 測試腳本；偵測 API 之間的依賴（先建立、再查詢）；需要真實資料的案例交給人工填值 | 少量（Claude） |
| **① 解析 Agent** | 讀 Java 原始碼，切成模組、建立呼叫圖、產出 API 對應表 | 是（Claude） |
| **② 測試 Agent** | 對 Java 服務跑測試，把每支 API 的回應存成 golden output | 否 |
| **③ 架構設計 Agent** | 設計 Python 專案的目錄、檔案、每個函式的簽名 | 是（Claude） |
| **[P] Plan Agent** | 排好翻譯順序，替每個函式找出它會用到的其他程式 | 否（純機械） |
| **④ 骨架實作 Agent** | 依設計建立目錄和空函式，並從 Java entity 產生資料表模型 | 否（純機械） |
| **⑤ 功能改寫 Agent** | 照 Java 原始碼把每個函式翻成 Python；分三關，上一關全完成才進下一關 | 是（Claude；repository 層用本地模型，失敗退回 Claude） |
| **⑥ 測試執行 Agent** | 對 Python 服務跑同一份測試，比對 golden output，產出報告 | 否 |
| **⑦ Debug Agent** | 看失敗報告與程式碼，找出原因，直接寫出並套用修正 | 是（Claude） |

---

## 4. 開發時長與花費

### 開發時長（預估）

**約 372 小時（約 46 個 8 小時工作天）**，期間 2026-07-21 ～ 10-02。

| 階段 | 預估 |
|---|---|
| 設計並做出各個步驟 | 約 159 小時 |
| 第一版實際跑起來驗證、除錯 | 約 106 小時 |
| 第二版（Claude + 便條） | 約 27 小時 |
| 第三版（Claude 直接看 Java 原始碼、分三關） | 約 81 小時 |

依本人自述工時估算：約 2 週平日 9:00～23:00（140 小時）＋其餘 25 個平日 8 小時（200 小時）＋週末折算 4 天（32 小時）；各階段按對話紀錄的比例分配。

### Claude API 花費（以 1 美元 ≈ 32 台幣概算）

| 版本 | 跑一次完整流程 | 最高三次平均成功率 |
|---|---|---|
| 第一版：本地模型 + 說明便條 | 約 US$2.1（約 **NT$66**） | 約 30% |
| 第二版：Claude + 說明便條 | 約 US$3.15（約 **NT$100**） | 約 62% |
| 第三版（現在）：Claude 直接看 Java 原始碼 | 約 US$3.0～3.3（約 **NT$100**） | 約 76% |

整個重構過程（含所有實驗與重跑）累計約 US$114，**約 NT$3,700**。本地模型不計費；這些是估算，不是帳單。

---

## 5. 心得與結論

- **版本演進：** 第一版請本地模型照「說明便條」翻，成功率最好只有約 30%；第二版只換成 Claude，仍然忽高忽低；問題出在便條會漏掉、講歪 Java 細節，所以第三版拿掉便條，讓 Claude 直接看原始碼、照固定順序翻，成功率提高到約 76%，而且不用靠大量事後修補。
- **AI 容易「改 A 壞 B」：** 專案越大，文件越容易落後；中間一次誤改，後面的修改就建立在錯誤上，引發別種問題。重複的邏輯要抽成共用區塊，不要複製多份各自維護。
- **工程師的角色不可少：** AI 很會做小區塊，但大架構要工程師指方向；方向走錯時要靠工程師拉回來，工程師的直覺也常比 AI 判斷準，能防止 AI 亂猜。
- **全自動做不到 100%：** AI 自己重複嘗試不會自己找出正確答案，方向歪了只會越試越歪；由工程師質疑方向、從根本解決才切得中核心。
- **自動化的適用場景：** 需要大量作業、格式相對固定時，自動化才能發揮規模經濟；只為單一專案的重構就做一整套自動化，是殺雞用牛刀。

**結論：資深工程師和 AI 協作才能發揮最大效益。與其追求完美的全自動，不如一步一步導正，讓每一步的地基都是最穩的，長起來才不會歪。**

---

## 環境設定（`.env` 範本）

`.env` 含金鑰與連線資訊，不會上傳到 git（已列在 `.gitignore`）。請在專案根目錄自行建立 `.env`，把尖括號的值換成自己的：

```ini
# Claude API
ANTHROPIC_API_KEY=<你的 Anthropic API Key>
SPEC_COLLECTION_AGENT_MODEL=claude-sonnet-4-6

# 專案路徑（相對於本專案資料夾；Java 專案、Python 目標專案都與本專案同層、各自獨立）
JAVA_PROJECT_PATH=../lang-exam-api-refactor
PYTHON_PROJECT_PATH=../exam-platform-api

# Java 服務（[A]、② 會用 java -jar 啟動）
JAVA_BASE_URL=http://localhost:8080
JAVA_JAR_PATH=../lang-exam-api-refactor/target/lang-exam-api-1.0.1.jar
JAVA_EXECUTABLE_PATH=<java 執行檔完整路徑，PATH 上的版本正確的話直接寫 java>

# Python 目標服務（Docker 容器對外的位址）
PYTHON_BASE_URL=http://localhost:8000

# 本地模型：ollama 前面掛的 nginx 位址與 token（只有用到 qwen 時才需要）
OLLAMA_BASE_URL=http://<本地模型機器 IP>:<nginx port>/v1
OLLAMA_API_KEY=<nginx 設定的 token>

# 測試用 DB（獨立於正式／開發 DB，名稱加 _TEST；Harness 每次驗證前會 truncate + 重灌 seed）
TEST_DB_DSN=postgresql://postgres:<DB 密碼>@127.0.0.1:5432/MOC_MATSUEXAM_TEST
SPRING_DATASOURCE_URL=jdbc:postgresql://127.0.0.1:5432/MOC_MATSUEXAM_TEST
SPRING_DATASOURCE_USERNAME=postgres
SPRING_DATASOURCE_PASSWORD=<DB 密碼>

# Python 服務自己的 DB 連線，值同 TEST_DB_DSN
DATABASE_URL=postgresql://postgres:<DB 密碼>@127.0.0.1:5432/MOC_MATSUEXAM_TEST
```

> 其他可調參數（逾時、log 路徑等）都有程式內建預設值，不用寫進 `.env`，完整清單見 [`docs/01_langgraph_architecture.md`](docs/01_langgraph_architecture.md) 二章。

---

## 更多文件

- 完整成果報告：[`docs/refactor_result.md`](docs/refactor_result.md)（白話版）、[`docs/refactor_result_detail.md`](docs/refactor_result_detail.md)（細節版）
- 整體架構設計：[`docs/00_refactor_architecture.md`](docs/00_refactor_architecture.md)
- 各 Agent 設計與程式碼文件：`docs/01`～`docs/11`
