# Java → Python 重構計畫：Multi-Agent 協作架構

> 決策版本 v3.3｜以 LangGraph + Claude API + translator-cli + qwen2.5-coder:32b 為核心方向
> 本文件為架構主幹，聚焦「整體流程、Agent 職責邊界、資料流」；各 Agent 的實作細節、演算法、程式碼請見對應細節文件。
> v2.0 變更：改名為 `00_refactor_architecture.md`，作為架構文件的唯一入口；精簡 Agent A/B/③ 的實作細節，改為指向 `02a_harness_architecture.md`；整併 v1.1/v1.2 內容。
> v3.0 變更：以自製 translator-cli 取代 aider，作為 ④/⑤ 呼叫本地模型寫入程式碼的工具；task 粒度統一鎖死為「單一函式」；[P] Plan Agent 職責明確化為「切出 Agent ⑤ 在 Agent ⑥ 執行前必須完成的完整範圍」；局部驗證的觸發時機由「每個 task 完成」改為「整個 module 的 task 全部完成」，避免依賴鏈未接完就誤觸發驗證。
> v3.1 變更：修正 Agent ⑤ 的平行執行認知——本地 M4 Pro Max 跑單一 qwen2.5-coder:32b 已飽和，無法水平擴展，故「排程平行」與「執行併發」需分開設計；「十、待決定事項」除 git commit 顆粒度外全數定案，已併入對應章節，不再重複列出。
> v3.2 變更：補三處平行/依賴邊界的漏洞——(1) 明訂 Agent ③ 的 interface 定義須鎖死到「檔案路徑＋函式簽名」層級，作為 [P] 與 ④ 平行執行時的唯一權威規格；(2) Agent ⑤ 的 module 排程須依 Agent ① 的模組依賴圖決定順序，避免局部驗證 fail 時下游已完成產物一併作廢；(3) [B] Collection Agent 補充鏈式依賴（如先建立資源再用回傳 ID 查詢）不能單純靜態填 seed.sql，機制細節見 `02a_harness_architecture.md`。
> v3.3 變更：補上測試 DB 的具體設計（見五）——測試 DB 需與正式/開發 DB 分開（命名加 `_TEST`）；`TEST_DB_DSN` 與 Java `spring.datasource.url` 格式不同，附對應範例；Java 切換 DB 改用 Spring Boot 環境變數覆蓋；新增 Python 服務統一讀取 `DATABASE_URL` 的規定；測試 DB 的 schema 來源列為待確認事項（見十）。

---

## 一、整體流程概覽

```
[Java 專案]
     ↓
① 解析 Agent         → 輸出：模組清單 + 業務邏輯文件 + API 對應表
     ↓
[A] Spec Agent       → 啟動 Java 服務，取得 OpenAPI 3.0 JSON
     ↓
[B] Collection Agent → 將 OpenAPI 轉換為兩份 Postman Collection
     ↓                 （collection_readonly + collection_mutation）
② 測試 Agent         → 【Harness 錄製端】對 Java 服務執行 Postman，記錄 golden output
     ↓
③ 架構設計 Agent     → 輸出：Python 專案結構（技術棧採用已定案的 FastAPI+SQLAlchemy）+ 模組 interface + route_to_file_mapping
     ↓
  ┌──┴──────────────────┐  ← 可平行執行
[P] Plan Agent          ④ 骨架實作 Agent
 （產出 task list）       （建立目錄與骨架）
  └──────────┬──────────┘
             ↓
⑤ 功能改寫 Agent     → 逐模組將 Java 業務邏輯改寫成 Python（排程可平行，本地模型執行序列化，見七/⑤）
             ↓
⑥ 測試執行 Agent     → 【Harness 驗證端】對 Python 服務執行 Postman，比對 golden output
             ↓
⑦ Debug Agent        → 讀取 Harness report，分析 diff，定位問題，回饋給 ⑤ 修復
             ↓
            ✅ 完成
```

**核心原則**：「測試夾住重構」— 先記錄 Java 服務的現有行為（golden output），重構完成後驗證 Python 服務的行為與之一致。

**Harness 的位置**：Harness 不是一個獨立的 Agent，而是橫跨整個流程的基礎設施層，由 Agent ② 和 Agent ⑥ 共同承擔，兩端共用同一套邏輯（Masker、DiffEngine、Reporter）以確保錄製與驗證標準一致。Agent ⑦ 讀取 Harness 產出的結構化 report 進行 Debug。
→ 完整設計見 `02a_harness_architecture.md`。

---

## 二、Orchestrator 設計原則

Orchestrator 是**純 Python 程式邏輯，不是 Agent**。

| 決策類型 | 負責方 |
|---|---|
| 進入下一階段？ | 程式邏輯（有明確完成條件） |
| 這次 fail 要改哪裡？ | LLM Agent 判斷 |
| 要不要重試？重試幾次？ | 程式邏輯（避免無限迴圈） |
| 模組怎麼拆？ | LLM Agent 判斷 |

核心原則：能用程式判斷的，就不要交給 LLM。LLM 只負責「人類才能判斷」的模糊決策。

---

## 三、技術選型

### 框架：LangGraph

- 整個重構流程天然就是有向圖，LangGraph 幾乎 1:1 對應
- 條件分支（測試 fail → 回 Debug Agent）是 LangGraph 的核心強項
- State 型別讓 Agent 間的資料傳遞有型別保護
- 節點（Node）對應 LLM Agent，邊（Edge）對應 Python 條件邏輯

### LLM 分工

| 任務 | 模型 | 位置 | 理由 |
|---|---|---|---|
| 解析、規劃、設計、Debug | Claude API | 雲端 | 擅長理解需求、設計拆分、複雜推理 |
| 程式碼實作 | qwen2.5-coder:32b | 另一台 Mac（本地，M4 Pro Max） | 省費用，程式碼任務重複性高 |

> **硬體限制**：M4 Pro Max 跑單一 qwen2.5-coder:32b 已達飽和，無法水平擴展成多實例。因此 Agent ④/⑤ 對本地模型的**實際生成請求需序列化（併發數=1）**，LangGraph 排程層仍可讓多個 task 同時處於就緒佇列以保留彈性，但不代表平行會讓總耗時變短——詳見七/⑤。
>
> **排程順序**：module 間的依賴關係直接沿用 Agent ① `module_list` 的「依賴的其他模組」欄位，排程時優先讓同一 module 的 task 連續完成並通過局部驗證後，才釋放依賴它的下游 module——避免上游局部驗證 fail 時，下游已完成的產物一併作廢，回滾成本過高。

### Python 目標技術棧（已定案）

- **Web 框架**：FastAPI
- **ORM**：SQLAlchemy

這兩項由 Agent ③ 在設計 Python 專案結構時直接採用，不再由 Agent ③ 於每次執行時重新判斷。

### 程式碼執行工具：translator-cli（自製）

LangGraph 呼叫本地模型完成程式碼填寫的橋接工具，取代通用的 chat 式編輯工具（如 aider）。核心設計是「填空式」輸出契約：給模型已知的函式簽名（骨架已建好）+ Java 原始邏輯，模型只回傳函式本體，由 CLI 用 AST 精準插入，而非依賴模型生成可精確比對原文的 diff。每次呼叫只帶入「當次任務相關」的檔案，控制 context 大小；寫入前後搭配 git snapshot 與語法驗證，確保每個 task 的異動可追蹤、可回滾。

→ 完整設計（adapter 介面、delimiter 契約、git snapshot 流程、衝突偵測）見 `03a_translator_cli_architecture.md`。

### OpenAPI 工具鏈

- **springdoc-openapi**：自動產生 Java 服務的 OpenAPI 3.0 spec
- **openapi-to-postmanv2**：將 OpenAPI JSON 轉換為 Postman Collection
- **newman**：在 LangGraph 中執行 Postman Collection

→ 各工具的具體版本、安裝方式見「五、環境建立」；OpenAPI → Collection 的轉換流程細節見 `02a_harness_architecture.md`。

---

## 四、環境架構

```
你的電腦（RT-AC51U，i7-11700B，32GB RAM）
├── LangGraph Orchestrator（輕量 Python 流程控制）
├── Claude API 呼叫（雲端，解析/設計/Debug Agent）
└── translator-cli（subprocess 呼叫）→ HTTP → 另一台 Mac
                                        └── ollama
                                             └── qwen2.5-coder:32b（程式碼實作）
```

---

## 五、環境建立

在開始執行整個流程前，需要在各機器上準備好以下工具：

**你的電腦（Orchestrator 所在機器）**
- Python 虛擬環境，安裝 `langgraph` 和 `langchain-anthropic`
- Node.js 環境，安裝 `openapi-to-postmanv2` 和 `newman`（全域安裝）
- Claude API 金鑰，設定在環境變數
- PostgreSQL client 工具（`psql`），用於 Harness 的 DB seed 操作

**另一台 Mac（程式碼實作機器）**
- ollama，已下載 `qwen2.5-coder:32b` 模型
- 確認 ollama 的 HTTP API port 可從你的電腦連通

**你的電腦（額外）**
- translator-cli（自製工具）與其依賴（AST 處理套件、git）
- 確認 git 已初始化 Python 專案目錄，作為 translator-cli 寫入前後 snapshot 的版控基礎

→ translator-cli 的安裝與設定細節見 `03a_translator_cli_architecture.md`。

**Java 專案端（一次性準備）**
- 在 `pom.xml` 加入 `springdoc-openapi-starter-webmvc-ui 2.3.0` 依賴（**保留作為長期文件用途，不在產出 Collection 後移除**）
- 啟動方式：**直接 `java -jar` 執行已打包的 jar**，不需 Maven build
- 準備 `fixtures/seed.sql`（由你提供的 PostgreSQL 初始資料）

**測試 DB 準備（獨立於正式/開發 DB，v3.3）**

Harness（Agent ②/⑥）每次驗證前都會 truncate + 重灌 `seed.sql`，**不能指向正式或開發用的資料庫**，需另開一顆測試 DB，命名慣例為原名加 `_TEST`。以目前 Java 連線為例：

```
# Java 原本連線（正式/開發，不動）
spring.datasource.url=jdbc:postgresql://127.0.0.1:5432/MOC_MATSUEXAM
```

對應開一顆 `MOC_MATSUEXAM_TEST`（host/user/password 沿用），`TEST_DB_DSN`（psycopg2/asyncpg 格式）與 `spring.datasource.url`（JDBC 格式）指向同一顆 DB、但寫法不同：

```
TEST_DB_DSN=postgresql://postgres:password@127.0.0.1:5432/MOC_MATSUEXAM_TEST
SPRING_DATASOURCE_URL=jdbc:postgresql://127.0.0.1:5432/MOC_MATSUEXAM_TEST
```

- **Schema 來源（待確認）**：`MOC_MATSUEXAM_TEST` 是空白 DB，`seed.sql` 只灌資料不建表。若 Java 用 Hibernate/JPA `ddl-auto`，[A] Spec Agent 第一次啟動即自動建好 schema；若用 Flyway/Liquibase 或手動 migration，需先 `pg_dump --schema-only` 灌一次。
- **切換方式**：不改 `application.properties`，改用 Spring Boot 環境變數覆蓋（`SPRING_DATASOURCE_URL` 等會自動對應 `spring.datasource.*`）。[A] Spec Agent 啟動 Java 服務固定帶入：

```bash
SPRING_DATASOURCE_URL=jdbc:postgresql://127.0.0.1:5432/MOC_MATSUEXAM_TEST \
SPRING_DATASOURCE_USERNAME=postgres SPRING_DATASOURCE_PASSWORD=password \
java -jar app.jar
```

**Python 服務端 DB 連線（v3.3）**

Python（FastAPI + SQLAlchemy）服務統一讀環境變數 `DATABASE_URL`（值同 `TEST_DB_DSN`）。Agent ③ 設計 `python_structure`、Agent ④ 產生 DB engine 初始化程式碼時固定用這個變數名，避免各自猜測。

---

## 六、Context 控制策略

[P] Plan Agent 產出 task list 時，必須將每個 task 切到**單一函式**的粒度（不保留「或類別」的模糊選項），讓 Agent ⑤ 每次呼叫 translator-cli 時只需傳入少量相關檔案，且輸出契約與 translator-cli 的「填空式」設計（模型只回傳單一函式本體）完全對齊。絕不能把整個專案目錄丟進去，否則 context 爆炸會導致實作品質下降。

例如實作 `UserRepository.get_by_id()` 時，只需傳入 `user_repository.py` 和 `user.py` 兩個檔案，不需要傳入整個 `src/` 目錄。

---

## 七、Agent 職責定義

### ① 解析 Agent（Claude API）

- **輸入**：Java 專案原始碼
- **輸出**：模組清單與依賴關係、核心業務邏輯摘要、Java → Python 技術對應表、API 對應表（`java_controller` ↔ `python_target` ↔ 所屬 `module`）

> Agent ① 不解析 request/response schema，這部分由 Agent A 從 springdoc-openapi 自動取得。

---

### [A] Spec Agent（純程式邏輯，不需要 LLM）

啟動 Java 服務（連接測試用 DB，直接 `java -jar` 執行已打包的 jar，不需 Maven build 這一步），呼叫其 `/v3/api-docs` endpoint 取得完整 OpenAPI 3.0 spec，存成 `openapi.json`。全程不解析任何 Java 原始碼。

→ 具體步驟、啟動方式、URL 見 `02a_harness_architecture.md`。

---

### [B] Collection Agent（程式邏輯 + 少量 LLM）

將 `openapi.json` 轉換為兩份 Postman Collection（readonly / mutation），並用 LLM 依 `seed.sql` 填入合理的範例值，避免打出去直接 404。若 API 間存在鏈式依賴（如先建立資源、再用其回傳 ID 查詢），填值與執行順序另有處理機制，不能單純靜態填 `seed.sql`。

> **設計不變量**：鏈式依賴的 ID 一律用 Postman Environment Variable 在單次 newman 執行內動態傳遞，錄製（對 Java）與驗證（對 Python）是兩次各自獨立的 newman 執行，各自用自己那次執行實際產生的 ID，不會跨執行或跨服務把 ID 寫死共用——這也是即使 Java／Python 的 DB 自增序列產生的實際數值不同，鏈式請求也不會因此打到不存在的資源的原因。

- **輸出**：`postman/collection_readonly.json`、`postman/collection_mutation.json`

→ 轉換流程、LLM 填值邏輯、鏈式依賴處理見 `02a_harness_architecture.md`。

---

### ② 測試 Agent（程式邏輯｜Harness 錄製端）

對 Java 服務執行兩份 Collection，記錄每支 API 的 response 作為 golden output。

- **輸出**：`fixtures/golden/` 下的 JSON 檔案集合

→ 錄製流程、非 JSON response 處理見 `02a_harness_architecture.md`。

---

### ③ 架構設計 Agent（Claude API）

- **輸入**：Agent ① 的解析結果 + `openapi.json`
- **輸出**：Python 專案目錄結構、各模組 interface 定義、`route_to_file_mapping`（寫入 `config/harness.yaml`）。技術棧採用已定案的 FastAPI + SQLAlchemy（見三），Agent ③ 不需重新判斷框架選型，聚焦在專案結構與模組拆分上

> **顆粒度要求**：interface 定義須精確到「檔案相對路徑＋函式簽名」層級，不能只到模組層級。這份輸出是 [P] 與 ④ 唯一的權威規格，兩者各自消費、不做二次詮釋，平行執行才不會各自猜出不一致的檔名或函式名。
>
> Agent ③ 完成後，[P] Plan Agent 和 ④ 骨架實作 Agent 可**平行執行**，兩者都只依賴 ③ 的輸出、互不依賴彼此。

→ `route_to_file_mapping` 的 key 格式與比對演算法見 `02a_harness_architecture.md`。

---

### [P] Plan Agent（Claude API）

- **輸入**：Agent ① 的模組清單 + Agent ③ 的架構設計
- **職責**：切出 Agent ⑤ 在進入 Agent ⑥（測試執行）之前必須完成的完整範圍，並拆解成可獨立執行的 task list

拆解時的關鍵原則：
- **覆蓋率**：以 Agent ① 輸出的**完整方法清單**為準，不能只依 API 對應表拆 task——否則沒有直接對應 API 的內部 helper function 容易被漏掉，漏掉的函式不會被 Harness 的 API 級測試直接抓到，而是等到被其他函式呼叫時才爆出不直觀的錯誤
- **粒度**：每個 task 鎖定**單一函式**，與 translator-cli 的輸出契約對齊（見第六節）
- **依賴順序**：`depends_on` 決定同一 module 內的執行順序（例如 repository 先於 service 先於 router）；不同 module 間若無依賴關係，才可平行執行
- **模組歸屬**：每個 task 標記 `module`，供 Agent ⑤ 判斷該 module 的所有 task 是否已全數完成

- **輸出**：task list（欄位格式見第八節）

---

### ④ 骨架實作 Agent（translator-cli + qwen2.5-coder:32b）

依 Agent ③ 的目錄結構與 interface 定義，建立目錄、base class、router 骨架、config、DB schema，不含業務邏輯。骨架階段產出的函式簽名，是 Agent ⑤ 呼叫 translator-cli 時「填空」的目標。

> 與 [P] Plan Agent 平行執行，兩者都完成後才進入 Agent ⑤。

---

### ⑤ 功能改寫 Agent（translator-cli + qwen2.5-coder:32b）

依 task list 逐一呼叫 translator-cli 實作業務邏輯，每次 task 鎖定單一函式。

**排程 vs. 執行併發**：多個 module 的 task 可以同時處於「就緒可排程」狀態（同一 module 內仍依 `depends_on` 序列執行），但對本地模型的**實際生成請求序列化，併發數固定為 1**——因為 M4 Pro Max 跑單一 qwen2.5-coder:32b 已飽和，無法水平擴展。也就是說平行帶來的效益是「排程更有彈性、模組完成順序不死板卡住」，而不是「總耗時等比例縮短」；總耗時大致等於所有 task 的模型生成時間總和。排程順序須依 module 間依賴圖決定，優先完成同一 module 再釋放下游，見三/LLM 分工。

驗證分兩個層級，觸發時機不同：
- **task 完成**：僅觸發 translator-cli 內建的語法驗證（AST parse），確認寫入沒有破壞語法，不觸發 API 級測試
- **module 完成**（該 module 底下所有 task 都已完成）：才觸發該 module 的局部驗證，跑此 module 的 golden cases——因為 API 呼叫鏈往往橫跨 repository/service/router 多個函式，過早以單一 task 觸發 API 測試會產生大量「依賴鏈未接完」的假失敗

→ translator-cli 的填空契約、AST 插入機制、request queue 序列化設計見 `03a_translator_cli_architecture.md`；局部驗證與全量驗證的兩層架構見 `02a_harness_architecture.md`。

---

### ⑥ 測試執行 Agent（程式邏輯｜Harness 驗證端）

對 Python 服務執行 Postman collection，對比 golden output。

- **輸出**：pass/fail 清單 + diff 報告

→ 比對邏輯（Masking、陣列順序處理）與 report 格式見 `02a_harness_architecture.md`。

---

### ⑦ Debug Agent（Claude API）

- **輸入**：fail 清單 + diff 報告 + 對應 Python 原始碼（`related_files`）
- 分析根本原因，輸出具體修正指令回饋給 Agent ⑤

---

## 八、Agent 間的資料格式

Agent 間使用結構化 JSON 傳遞，不使用自然語言，避免資訊失真。

### Agent ① 輸出：模組清單

每個模組記錄 Java 原始檔路徑、對應的 Python 目標檔路徑、所屬 module 名稱（對應 `fixtures/golden/` 子目錄）、依賴的其他模組、方法清單（Java 方法名、Python 方法名、描述、複雜度）。API 對應表另記錄 endpoint、HTTP method、Java Controller、Python 目標檔、所屬 module；schema 資訊不在此處，由 Agent A 提供。

### [P] Plan Agent 的 task list

| 欄位 | 說明 |
|---|---|
| `id` | task 唯一識別碼，如 `task_001` |
| `module` | 所屬模組名稱，對應 `fixtures/golden/` 子目錄；供 Agent ⑤ 判斷該 module 是否所有 task 都已完成，以觸發局部驗證 |
| `description` | 任務描述，供 translator-cli 組裝 prompt 用 |
| `target_files` | 這次 task 需要讀寫的檔案清單（控制 translator-cli 呼叫模型時的 context） |
| `context` | 補充說明，如依賴關係、邊界條件 |
| `depends_on` | 前置 task 的 id 清單，決定同一 module 內的執行順序 |

> Plan Agent 必須確保：同一個 `module` 底下的 task，合起來涵蓋 Agent ① 輸出的**完整方法清單**（不只是 API 對應表列出的方法），否則局部驗證觸發時會因缺函式而失敗，且錯誤不易與「邏輯寫錯」區分。

### Agent ③ 的 route_to_file_mapping

產出後直接寫入 `config/harness.yaml`，不需人工填寫，格式細節見 `02a_harness_architecture.md`。

---

## 九、LangGraph 狀態（State）設計

| 欄位 | 產出者 | 說明 |
|---|---|---|
| `module_list` | Agent ① | 模組清單與依賴關係 |
| `api_to_python_target` | Agent ① | API ↔ Python 檔案對應表 |
| `openapi_spec` | Agent A | `/v3/api-docs` 的完整 OpenAPI JSON |
| `collection_readonly_path` | Agent B | readonly collection 的檔案路徑 |
| `collection_mutation_path` | Agent B | mutation collection 的檔案路徑 |
| `golden_output` | Agent ② | golden output 的摘要（詳細內容在檔案系統） |
| `python_structure` | Agent ③ | Python 專案目錄結構與 interface 定義（精確到檔案路徑＋函式簽名，為 [P] 與 ④ 的權威規格，見七/③） |
| `route_to_file_mapping` | Agent ③ | API → Python 檔案 mapping，寫入 harness.yaml |
| `task_list` | [P] Plan Agent | 含 module 欄位的 task 清單 |
| `completed_tasks` | Agent ⑤ | 已完成的 task id |
| `failed_tasks` | Agent ⑤ | 失敗的 task id |
| `test_results` | Agent ⑥ | Harness report（pass/fail + diff） |
| `retry_count` | Orchestrator | 目前重試次數，上限設為 3，超過則通知人工 |

### LangGraph 圖的節點與邊

主流程：parse（①）→ extract_spec（A）→ gen_collection（B）→ record_tests（②）→ design（③）。

③ 完成後，plan（[P]）和 scaffold（④）**平行執行**，兩者都完成後才進入 implement（⑤）。

⑤ 完成後進入 run_tests（⑥），接條件邊：測試全過 → 結束；有失敗且未超過重試次數 → debug（⑦）→ implement（⑤）；超過重試次數 → 結束並通知人工。

---

## 十、待決定事項

以下事項已定案，內容已併入對應章節，不再於此重複列出：Python 技術選型（見三）、ORM 選擇（見三）、平行 Agent 的執行策略（見七/⑤）、最大重試次數設 3（見九 `retry_count`）、translator-cli 連接另一台 Mac 沿用原 aider 的 HTTP 連線方式（見四）、springdoc-openapi 保留（見五）、Java 專案啟動方式為 `java -jar`（見五、七/[A]）、測試 DB 命名與切換方式（見五，v3.3 新增）、Python 服務 DB 連線變數統一為 `DATABASE_URL`（見五，v3.3 新增）。

尚待定案：

- [ ] translator-cli 的 git commit 顆粒度與平行寫入的鎖機制，細節待 `03a_translator_cli_architecture.md` 定案
- [ ] 測試 DB（`MOC_MATSUEXAM_TEST`）的 schema 來源：Java 是否用 `ddl-auto` 自動建表？若否，需確認手動 dump schema 的具體指令，補進五、環境建立

---

## 十一、文件索引

| 文件 | 內容 |
|---|---|
| `00_refactor_architecture.md`（本文件） | 整體流程、Agent 職責邊界、資料流、State 設計 |
| `01_langgraph_architecture.md` | LangGraph 實作細節：State schema 的實際型別定義、graph 的 node/edge 建構、[P]/④ 平行分支與 ⑤ module 排程器實作、conditional edge（retry 迴圈）、stub-first 開發策略、跨平台（含 Windows）注意事項、專案初始化 |
| `02a_harness_architecture.md` | Harness 詳細設計：Recorder / Verifier、Masker、DiffEngine、Report 格式、DB 環境、Route Mapping 演算法 |
| `02b_harness_code.md` | Harness 各模組的實際程式碼實作 |
| `03a_translator_cli_architecture.md` | translator-cli 詳細設計：填空契約、AST 插入機制、git snapshot 流程、衝突偵測、目標語言 adapter 介面 |
| `03b_translator_cli_code.md` | translator-cli 的實際程式碼實作 |

---

*本文件為整體架構概覽，只保留「架構、流程、基礎設定」，各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護。*
*v3.2 補上三處平行/依賴邊界的規則缺口（③ 輸出顆粒度、⑤ 排程依賴圖、[B] 鏈式依賴），隨實作推進持續更新。*