# Java → Python 重構計畫：Multi-Agent 協作架構

> 以 LangGraph + Claude API + translator-cli + qwen2.5-coder:32b 為核心方向。
> 本文件為架構主幹，聚焦「整體流程、Agent 職責邊界、資料流」；各 Agent 的實作細節、演算法、程式碼請見對應細節文件（見十一、文件索引）。

---

## 一、整體流程概覽

```
[Java 專案]
     ↓
[A] Spec Agent       → 啟動 Java 服務，取得 OpenAPI 3.0 JSON
     ↓
[B] Collection Agent → 將 OpenAPI 轉換為兩份 Postman Collection
     ↓                 （collection_readonly + collection_mutation；人工填值/skip 關卡在此定案，見 03a）
① 解析 Agent         → 輸出：模組清單 + 業務邏輯文件 + API 對應表（skip 呼叫鏈排除需讀取上一步已定案的 skip 清單，見 04a）
     ↓
  ┌──┴──────────────────────────┐  ← 可平行執行（② 不依賴③的輸出，③ 不依賴 golden_output，兩者互不相依，見 05a 十一章）
② 測試 Agent                   ③ 架構設計 Agent
（Harness 錄製端，對 Java       （輸出：Python 專案結構，技術棧採用
 服務執行 Postman，記錄          已定案的 FastAPI+SQLAlchemy，
 golden output）                 + 模組 interface + route_to_file_mapping）
  └──────────┬───────────────────┘
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

**各階段對應文件**：

| 階段 | 功能簡述 | 對應文件 |
|---|---|---|
| [A] Spec Agent | 啟動 Java 服務，取得 OpenAPI 3.0 JSON | `03a_spec_collection_agent_architecture.md` |
| [B] Collection Agent | 將 OpenAPI 轉換為兩份 Postman Collection（含人工填值/skip 關卡，skip 清單在此定案） | `03a_spec_collection_agent_architecture.md` |
| ① 解析 Agent | 解析 Java 專案，輸出模組清單、業務邏輯摘要、API 對應表；skip 呼叫鏈排除消費上一步的 skip 清單 | `04a_parse_agent_architecture.md` / `04b_parse_agent_code.md` |
| ② 測試 Agent（Harness 錄製端） | 對 Java 服務執行 Postman，記錄 golden output | `02a_harness_architecture.md` / `02b_harness_code.md` |
| ③ 架構設計 Agent | 輸出 Python 專案結構、模組 interface、route_to_file_mapping | `05a_design_agent_architecture.md` / `05b_design_agent_code.md` |
| [P] Plan Agent | 產出 Agent ⑤ 的 task list | `06a_plan_agent_architecture.md` / `06b_plan_agent_code.md` |
| ④ 骨架實作 Agent | 建立目錄與骨架（呼叫 translator-cli「骨架生成模式」），並從 Java entity 原始碼組出 `db_models` | `08a_scaffold_agent_architecture.md` / `08b_scaffold_agent_code.md` |
| ⑤ 功能改寫 Agent | 逐模組改寫業務邏輯（呼叫 translator-cli「填空模式」） | `09a_implement_agent_architecture.md` / `09b_implement_agent_code.md`（待建立，介面定義見 `07a_translator_cli_architecture.md`） |
| ⑥ 測試執行 Agent（Harness 驗證端） | 對 Python 服務執行 Postman，比對 golden output | `02a_harness_architecture.md` / `02b_harness_code.md` |
| ⑦ Debug Agent | 分析 diff、定位問題，回饋給 ⑤ | `10a_debug_agent_architecture.md` / `10b_debug_agent_code.md`（待建立） |

### 預計開發順序

上面的流程圖是**執行期**的順序（跑起來之後 pipeline 怎麼走），但**開發期**不是照這個順序從頭做到尾——Agent ⑤ 會直接呼叫 Harness（`refactor_harness` 套件），所以 Harness 必須比 ⑤ 早準備好；Harness 的驗證端又需要真實的 Postman Collection 與 Python 服務才能完整跑通，這兩者本質上要等 pipeline 後段才會出現。實際規劃的開發順序：

1. **Harness（02）**：程式碼先寫完，其中不依賴外部服務的純邏輯模組（Masker、DiffEngine、RouteMapper 等）可以先用假資料單元測試
2. **[A] Spec Agent / [B] Collection Agent（03）**：儘快接上，讓 Harness 的錄製端（Agent ②）能吃到真實 Postman Collection、對真實 Java 服務做完整驗證，不用一直依賴手動準備的替代資料
3. **① 解析 Agent（04）**：輸出模組清單，是 ③ 的必要輸入，緊接著 1、2 做。① 的 skip 呼叫鏈排除（見 04a 五章）需要讀取 [B] Collection Agent 產出的 `postman/unfilled_endpoints.json`（skip 清單在人工填值關卡定案，見 03a），這是硬性輸入依賴，因此執行期 graph 也把 ① 排在 [A]/[B] 之後（見一章流程圖）
4. **③ 架構設計 Agent（05）**：依賴 ① 的模組清單，輸出「檔案相對路徑＋函式簽名」層級的 interface 定義——這是 ④／⑤ 唯一的權威規格，必須盡早產出，不能拖到最後才做，否則 ④／⑤ 即使工具都備妥也無事可做。③ 不依賴 ② 的 `golden_output`（見 05a 十一章），執行期 graph 讓 ②／③ 在 ① 完成後平行執行（見一章流程圖），縮短關鍵路徑，開發順序上兩者也可各自獨立推進，不互相卡進度
5. **[P] Plan Agent（06）**：依賴 ① ＋ ③ 的輸出，拆解 Agent ⑤ 的 task list
6. **translator-cli（07）**：與 3～5 沒有直接資料相依（介面契約不需要真實函式簽名就能設計），可平行開發；但完整驗證填空契約是否設計對，要等 ③ 的真實輸出穩定後才能做，建議排在 ④／⑤ 開始前的最後一步收斂
7. **④ 骨架實作 Agent（08）＋ ⑤ 功能改寫 Agent（09）**：兩者都直接呼叫 translator-cli，且 ⑤ 的輸入是 [P] 的 task list，要等 3～6 都就緒才能真正跑起來
8. **⑦ Debug Agent（10）**：放最後做，這塊目前完全沒有 prompt 雛型，需要最多來回調整與測試——④／⑤ 的呼叫介面（translator-cli）已在步驟 6 穩定，不會出現「一邊調 Debug prompt 一邊還在改底層工具介面」的互相干擾
9. **LangGraph 整合測試**：`01` 七章的 stub-first 策略，確認整張圖的節點、平行分支、retry 迴圈都接對，再逐一把 stub 換成真實實作

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
| 程式碼填空實作（⑤，見七/⑤） | qwen2.5-coder:32b | 另一台 Mac（本地） | 省費用，程式碼任務重複性高。④ 的骨架生成純機械產生、不呼叫本地模型（見七/④、`07a_translator_cli_architecture.md` 四章），這一列只涵蓋⑤的「填空模式」 |

> **硬體限制**：該機器跑單一 qwen2.5-coder:32b 已達飽和，無法水平擴展成多實例。因此 Agent ⑤ 對本地模型的**實際生成請求需序列化（併發數=1）**，LangGraph 排程層仍可讓多個 task 同時處於就緒佇列以保留彈性，但不代表平行會讓總耗時變短——詳見七/⑤。
>
> **排程順序**：module 間的依賴關係直接沿用 Agent ① `module_list` 的「依賴的其他模組」欄位，排程時優先讓同一 module 的 task 連續完成並通過局部驗證後，才釋放依賴它的下游 module——避免上游局部驗證 fail 時，下游已完成的產物一併作廢，回滾成本過高。

### Python 目標技術棧（已定案）

- **Web 框架**：FastAPI
- **ORM**：SQLAlchemy
- **Python 版本**：≥ 3.10——③ 的型別對應（Java `Optional<T>` → `T | None`、OpenAPI schema 非必填欄位 → `X | None`，見 `05a_design_agent_architecture.md` 五章）用的是 PEP 604 union 語法，這個語法需要執行環境 ≥ 3.10 才能在執行期直接求值成功（Pydantic 建立 model class 時會需要真的求值型別註記，不是只在型別檢查工具裡才用得到）。Orchestrator 本身已經跑在 3.13（見 `03c_collection_agent_code.md` 對 `match`／`case` 的備註），這裡是另外對**目標 Python 服務**執行環境的明確要求，不能只憑 Orchestrator 的版本推論。

這三項由 Agent ③ 在設計 Python 專案結構時直接採用，不再由 Agent ③ 於每次執行時重新判斷。

### 程式碼執行工具：translator-cli（自製）

Agent ④／⑤ 共用的自製工具，取代通用的 chat 式編輯工具（如 aider），對外提供**兩種各自獨立的模式**：

- **骨架生成模式**（④ 呼叫）：輸入是 Agent ③ 已經決定好的 `python_structure`（目錄結構＋每個檔案路徑、class、函式簽名，見七/③）與④自行取得的 `db_models`。這一步**不呼叫任何模型**——③ 送到這一步的資料已經結構化、無歧義，translator-cli 只是把它機械組裝、渲染成語法正確的空殼 `.py` 檔案（函式本體固定 `pass`），純粹是同步、確定性的字串／AST 組裝，不需要模型判斷或創造。
- **填空模式**（⑤ 呼叫）：輸入是骨架生成模式已建好的函式簽名 + [P] 拆出的單一 task（Java 業務邏輯描述）。這一步才真的呼叫本地模型（qwen2.5-coder:32b）——核心設計是「填空式」輸出契約：給模型已知的函式簽名 + Java 原始邏輯，模型只回傳函式本體，由 CLI 用 AST 精準插入，而非依賴模型生成可精確比對原文的 diff。每次呼叫只帶入「當次任務相關」的檔案，控制 context 大小。

兩種模式都在寫入前後搭配 git snapshot 與語法驗證，確保每個 task 的異動可追蹤、可回滾。

> **連線方式（僅填空模式適用）**：translator-cli 不是直接打 ollama，中間多掛一層 **nginx 反向代理**做 token 驗證——本地模型機器對外只開放 nginx 的 port，nginx 驗證 `Authorization: Bearer <token>` 通過後才轉發給後面的 ollama；ollama 本身沒有變、還是同一顆 `qwen2.5-coder:32b`。骨架生成模式完全不會觸發這條連線，全程留在 Orchestrator 所在機器內完成。詳見四、環境架構。

→ 完整設計（adapter 介面、delimiter 契約、git snapshot 流程、衝突偵測）見 `07a_translator_cli_architecture.md`。

### OpenAPI 工具鏈

- **springdoc-openapi**：自動產生 Java 服務的 OpenAPI 3.0 spec
- **openapi-to-postmanv2**：將 OpenAPI JSON 轉換為 Postman Collection
- **newman**：在 LangGraph 中執行 Postman Collection

→ 各工具的具體版本、安裝方式見「五、環境建立」；OpenAPI → Collection 的轉換流程細節見 `03a_spec_collection_agent_architecture.md`。

---

## 四、環境架構

```
你的電腦（Orchestrator 所在機器）
├── LangGraph Orchestrator（輕量 Python 流程控制）
├── Claude API 呼叫（雲端，解析/設計/Debug Agent）
├── Java／Python 服務（本機執行：[A] 啟動的 java -jar、日後 Python 服務同樣跑在這台機器）
└── translator-cli（subprocess 呼叫）
      ├── 骨架生成模式（④）：純機械組裝，全程留在這台機器，不發出任何請求
      └── 填空模式（⑤）：HTTP（帶 Authorization: Bearer <token>）→ 另一台 Mac
                            └── nginx（反向代理，驗證 token）
                                 └── ollama（僅接受來自 nginx 的本機轉發）
                                      └── qwen2.5-coder:32b（程式碼實作）
```

> 本地模型機器對外只曝露 nginx 的 port，ollama 自己的 port（預設 `11434`）不對外開放，只接受 nginx 轉發進來的請求；translator-cli 端的 `OLLAMA_BASE_URL` 因此指向的是 nginx，而不是 ollama 本身。這條連線只有填空模式會用到，骨架生成模式不涉及。

---

## 五、環境建立

在開始執行整個流程前，需要在各機器上準備好以下工具：

**你的電腦（Orchestrator 所在機器）**
- Python 虛擬環境，安裝 `langgraph` 和 `anthropic`（Claude API 呼叫走 `anthropic` SDK 直接呼叫，不經 `langchain`，見 `01_langgraph_architecture.md` 二章）
- Node.js 環境，安裝 `openapi-to-postmanv2` 和 `newman`（全域安裝）
- Claude API 金鑰，設定在環境變數
- PostgreSQL client 工具（`psql`），用於 Harness 的 DB seed 操作

**另一台 Mac（程式碼實作機器）**
- ollama，已下載 `qwen2.5-coder:32b` 模型
- nginx，設定反向代理將對外 port 轉發到 ollama 的本機 port（預設 `11434`），並加上 token 驗證（例如比對 `Authorization: Bearer <token>` header，不符則回 401）
- 建議 ollama 只綁定 `127.0.0.1`（不直接對外開放），對外連線一律經過 nginx，避免繞過驗證直接打 ollama
- 確認 nginx 對外的 port 可從你的電腦連通（防火牆規則見八，`01_langgraph_architecture.md`）

**你的電腦（額外）**
- translator-cli（自製工具）與其依賴（AST 處理套件、git）
- Python 目標專案（translator-cli 寫入目標）：與 `refactor-project/`、Java 專案複製版同層、獨立的第三個資料夾，有自己的 `.git`（`git init` 過、沒有任何 commit），`.env` 新增 `PYTHON_PROJECT_PATH` 指向這個目錄——完整目錄配置與一次性準備步驟見 `07a_translator_cli_architecture.md` 二章「新輸入：python_project_path」
- 確認 `.env` 的 `OLLAMA_API_KEY` 與另一台 Mac 上 nginx 設定的 token 一致

→ translator-cli 的安裝與設定細節見 `07a_translator_cli_architecture.md`。

**Java 專案端（一次性準備）**
- **本地擺放位置**：與 `refactor-project/` 同層、各自獨立的資料夾（不要巢狀塞進 `refactor-project/` 內部——Java 專案是另一個完整的 Maven repo，有自己的 `.git`／`target/`，混在一起容易讓兩邊版控規則互相打架），複製一份專門給這次重構用，不要直接指向正式維護的專案原始位置（下方會永久修改 `pom.xml`）：
  ```
  test/2026/
  ├── refactor-project/          ← Python orchestrator
  └── lang-exam-api-refactor/    ← Java 專案的複製版，專門給這次重構用
  ```
  對應 `RefactorState.java_project_path`（① 解析 Agent、Agent A、③ 架構設計 Agent 都只需要這一個檔案系統路徑——③ 對 `module_list.java_files` 做輕量簽名再掃描時需要直接讀取，見 `05a_design_agent_architecture.md` 四章），`main.py` 組裝 `initial_state` 時讀 `.env` 的 `JAVA_PROJECT_PATH`（相對或絕對路徑皆可，如 `../lang-exam-api-refactor`），不寫死在程式碼裡——完整理由與目錄慣例見 `02a_harness_architecture.md` 十章。
- 在 `pom.xml` 加入 `springdoc-openapi-ui 1.7.0` 依賴（**保留作為長期文件用途，不在產出 Collection 後移除**）——目前 Java 專案是 **Spring Boot 2.7.11**，springdoc-openapi v1.7.0 是最後一版支援 Spring Boot 2.x／1.x 的 OSS 版本；`springdoc-openapi-starter-webmvc-ui` 這個 artifact 是給 Spring Boot 3.x（Jakarta EE 9、Java 17+）用的，兩者不可互換，用錯會導致 `UnsupportedClassVersionError`（class file 版本不符）
- 啟動方式：**直接 `java -jar` 執行已打包的 jar**，不需 Maven build（加入上述依賴後需先重新 `mvn package` 一次，之後才是單純 `java -jar`）
- `fixtures/seed.sql`：**已完成**，手動撰寫的可重複套用 INSERT 腳本（非 `pg_dump` 匯出格式），供 Harness 每次驗證前 truncate + 重灌用

**測試 DB 準備（獨立於正式/開發 DB）**

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

- **Schema 來源（已確認）**：`MOC_MATSUEXAM_TEST` 已建立。Java 專案的資料表是手動 SQL 建的，不是 Hibernate/JPA `ddl-auto` 自動建表，因此 schema 需要手動同步（例如整顆複製正式 DB：`pg_dump MOC_MATSUEXAM | psql MOC_MATSUEXAM_TEST`，schema 與資料一併到位）；資料則交給 `fixtures/seed.sql`（見上方「Java 專案端」）處理，機制同前，不重複。
- **切換方式**：不改 `application.properties`，改用 Spring Boot 環境變數覆蓋（`SPRING_DATASOURCE_URL` 等會自動對應 `spring.datasource.*`）。[A] Spec Agent 啟動 Java 服務固定帶入：

```bash
SPRING_DATASOURCE_URL=jdbc:postgresql://127.0.0.1:5432/MOC_MATSUEXAM_TEST \
SPRING_DATASOURCE_USERNAME=postgres SPRING_DATASOURCE_PASSWORD=password \
java -jar app.jar
```

**Python 服務端 DB 連線**

Python（FastAPI + SQLAlchemy）服務統一讀環境變數 `DATABASE_URL`（值同 `TEST_DB_DSN`）。Agent ③ 設計 `python_structure`、Agent ④ 產生 DB engine 初始化程式碼時固定用這個變數名，避免各自猜測。

---

## 六、Context 控制策略

[P] Plan Agent 產出 task list 時，必須將每個 task 切到**單一函式**的粒度（不保留「或類別」的模糊選項），讓 Agent ⑤ 每次呼叫 translator-cli 時只需傳入少量相關檔案，且輸出契約與 translator-cli 的「填空式」設計（模型只回傳單一函式本體）完全對齊。絕不能把整個專案目錄丟進去，否則 context 爆炸會導致實作品質下降。

例如實作 `UserRepository.get_by_id()` 時，只需傳入 `user_repository.py` 和 `user.py` 兩個檔案，不需要傳入整個 `src/` 目錄。

### Claude API 端的大範圍語意判斷：map-reduce 模式

上述「單一函式粒度」是針對本地模型（qwen）填空任務的 context 控制。但 Claude API 這側也有「輸入規模大、不能整包塞入」的任務——差別在於這類任務**需要跨邊界的全局視野**才能得出正確結論（例如判斷哪個 endpoint 的輸出是另一個 endpoint 的輸入，或是整個 Java 專案的模組拆分），不能像 translator-cli 那樣單純切到最小單位、互不相干地各自處理。

對這類任務，設計方向是 **map-reduce**，而非單純「拆分後平行、各自獨立產出」：

1. **Map**：依自然邊界（如 controller、Java 檔案）拆分，各自平行產出**局部候選結果**（不下最終判斷，只標記「看起來相關」的線索）
2. **Reduce**：彙整所有局部候選結果（此時輸入已大幅濃縮，而非原始全量資料），做跨邊界的最終判斷與合併

適用場景舉例：① 解析 Agent 對 Java 專案依 controller 拆分後的合併分析、[B] Collection Agent 的鏈式依賴偵測（跨 controller 的 producer/consumer 配對）。若合併階段只是機械拼接、不重新做跨邊界判斷，會系統性漏掉邊界之間的關聯，因此 reduce 階段不可省略。

具體怎麼拆、候選結果的資料形狀、reduce 階段的輸入輸出契約，由各自的細節文件定案。

### Claude API 呼叫用量記錄（成本稽核）

任何 Agent（不限於 ①③⑦、[P]、[B] 現有這幾個）只要呼叫 Claude API，都必須記錄用量，不能只驗證輸出對不對，不驗證花了多少。

- **共用工具**：`common/llm_usage_logger.py` 提供 `log_usage(response, *, model)`，所有 Claude API 呼叫點（各 Agent 各自的 LLM 呼叫函式）呼叫 API 之後、回傳結果之前，直接呼叫這個函式一次，不需要自己組 log 格式。
- **呼叫端不必手動標記自己是誰**：`log_usage()` 內部用 `inspect.stack()[1]` 抓呼叫端所在的檔名＋函式名稱（例如 `value_filler.fill_example_values`），自動組出「哪個 Agent、哪個 function」，避免每個呼叫點手動填標籤、日後改名或搬檔案時忘記同步更新而失準。前提：`log_usage()` 必須在實際呼叫 `messages.create()` 的函式內**直接**呼叫，不能包一層中間函式再轉呼叫，否則抓到的會是中間層、不是真正的呼叫端。
- **輸出格式**：固定寫到 `logs/claude_api_usage.jsonl`（JSON Lines，一行一筆，方便事後用程式加總算成本，不用人工去解析文字 log），每筆記錄：`timestamp`（UTC ISO 8601）、`caller`（自動抓到的「檔名.函式名」）、`model`、`input_tokens`、`output_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens`。
- **不覆寫**：每次啟動都是 append，不清空舊紀錄，讓一次完整 pipeline 執行的所有呼叫可以在同一份檔案裡依時間戳串起來看。

### Claude API 呼叫封裝（共用 client）

`log_usage()` 只負責記錄用量，不負責「怎麼打 API」；「怎麼打 API」這件事本身（client 初始化、Structured Outputs 的 `output_config.format` 組裝、JSON parse、錯誤處理）在 ①③⑤(⑦ Debug)、[P] Plan、[B] 這些會呼叫 Claude API 的 Agent 之間幾乎完全相同——差別只在「用哪個模型」（各 Agent 自己的環境變數，如 `SPEC_COLLECTION_AGENT_MODEL`／`PARSE_AGENT_MODEL`）。這部分因此集中在 `common/llm_client.py`，跟 `common/llm_usage_logger.py` 放同一層級，不讓每個 Agent 各自維護一份幾乎相同的實作：

- `common/llm_client.call_claude_for_json(*, system_prompt, user_prompt, schema, model, max_tokens=4096)`：唯一對外函式，`model` 是必填參數——這個模組不知道任何 Agent 的環境變數命名慣例，「沒指定要用哪個模型時退回什麼」是每個 Agent 自己的決策，不由共用層代為決定。
- 各 Agent 只需要自己的 `llm.py` 留幾行：讀自己的環境變數，沒設定時退回 `common.llm_client.DEFAULT_MODEL_FALLBACK`，算出 `DEFAULT_MODEL` 常數，呼叫端把這個值傳進 `call_claude_for_json(..., model=DEFAULT_MODEL)`。
- `max_tokens` 比照同一個原則，是 Agent 自己的決策，不是共用層該猜的事——差別在於「多少 token 夠用」取決於這個 Agent 輸出內容的密度：`call_claude_for_json()` 的 `max_tokens` 有預設值（`DEFAULT_MAX_TOKENS=4096`），給輸出精簡的分類／抽取型 Agent（①③[B]）直接沿用即可，不需要每個 Agent 都覆寫；但輸出密度高的 Agent（如 [P] Plan Agent，每個 task 都帶完整業務描述／context／依賴清單）需要在自己的 `llm.py` 另外算一個 `PLAN_AGENT_MAX_TOKENS` 常數、呼叫時明確覆寫，同樣經環境變數可調，不寫死在程式碼裡——實測案例見 `06a_plan_agent_architecture.md` 五章。
- `common/llm_client.py` 內部呼叫 `log_usage()` 時，屬於 `common/llm_usage_logger.py` module docstring「例外」段落講的「共用 API 封裝函式」情況——它自己先用 `inspect.stack()[1]` 抓出真正的業務呼叫端，明確傳給 `log_usage(response, model=model, caller=caller)`，不靠 `log_usage()` 內部的自動偵測（那會抓到 `call_claude_for_json` 自己）。

`spec_collection_agent/llm.py` 是第一個接上這個共用 client 的 Agent（見 `03c_collection_agent_code.md` 一、1.2 節）；之後任何新 Agent（如 ①③⑦、[P] Plan Agent）需要呼叫 Claude API 時，一律直接呼叫 `common.llm_client.call_claude_for_json()`，不要各自重新實作 client 初始化、Structured Outputs 組裝這些邏輯——只需要在自己的 `llm.py` 決定「用哪個模型」，輸出密度高時一併決定「`max_tokens` 要多少」。

### Map 階段併發數（共用工具）

「Claude API 端的大範圍語意判斷：map-reduce 模式」一節提到的 Map 階段（依自然邊界拆分、平行呼叫 Claude API），[B] Collection Agent 的鏈式依賴偵測與 ① 解析 Agent 的 Controller 批次摘要都採同一條政策：**併發數＝可用核心數 − 1，執行期動態計算，不寫死**（避免佔滿本機其餘資源——同一台機器通常還跑著 translator-cli 的本地模型）。這條政策不是各 Agent 各自決定、恰好想法一致，而是同一件事，因此比照 `common/llm_client.py` 的做法集中到 `common/concurrency.py`：

- `common/concurrency.default_concurrency() -> int`：回傳 `max(1, os.cpu_count() - 1)`，唯一對外函式
- [B]、① 呼叫平行 Map 階段時直接呼叫這個函式決定併發數，不各自重新推導；之後任何 Agent 有平行呼叫 Claude API 的併發數需求，同樣直接共用，不重新實作

### Map 階段切批次（共用工具）

跟上一節同一個時機點發現的重複：[B] 的鏈式依賴偵測（依 tag 切 operation 子批次）與 ① 的 Map 階段（依字元預算切 class 批次）各自维护一份「依累積字元數切批次」的演算法——單一項目的字元數加總超過門檻就先切一批，單一項目本身超過門檻仍自成一批。兩份實作除了處理的項目型別（operation payload vs. class 原始碼）不同，演算法完全一致，因此同樣集中到 `common/chunking.py`：

- `common/chunking.chunk_by_char_budget(items, size_of, budget) -> list[list[items 的型別]]`：唯一對外函式，`size_of` 是呼叫端提供的「單一項目怎麼算字元數」函式，`budget` 是門檻值
- 門檻值本身（多少字元、用哪個環境變數）仍由各 Agent 自己決定（如 `SPEC_COLLECTION_AGENT_MAP_CHUNK_CHARS`／`PARSE_AGENT_MAP_CHUNK_CHARS`），`common/chunking.py` 不知道、也不需要知道這些環境變數命名慣例——跟 `common/llm_client.py` 不代為決定「沒指定模型時退回什麼」是同一種分工原則
- [B]、① 需要依字元預算切批次時直接呼叫這個函式，不各自重新寫迴圈

### OpenAPI `$ref` 展開（共用工具）

`[B] Collection Agent`（見 03a）與 `③ 架構設計 Agent`（見 05a 五章）都需要對 `openapi_spec` 的 operation/schema 片段做同一件事：遞迴展開 `$ref`，只展開這次任務相關的片段（不整包攤平 `components.schemas`），遞迴展開到底，不處理 `allOf`／`oneOf`／`anyOf` 組合語法。跟前兩節同一種情況——兩個 Agent 需要的是同一份機械邏輯，不是恰好想法一致，因此集中到 `common/openapi_ref_resolver.py`：

- `common/openapi_ref_resolver.resolve_refs(fragment: dict, full_spec: dict) -> dict`：唯一對外函式，`fragment` 是呼叫端已取出的 operation/schema 片段，`full_spec` 是完整 `openapi_spec`（供 JSON Pointer 解析用）
- `③`／`[B]`（03a/03c）皆直接呼叫這個共用函式，不各自維護獨立的展開實作

### Java 型別／命名對應（共用工具）

③ 架構設計 Agent（方法簽名的型別／命名轉換，見 `05a_design_agent_architecture.md` 五章）與④骨架實作 Agent（JPA entity 欄位的型別／命名轉換，見 `08a_scaffold_agent_architecture.md` 五章）都需要「Java 型別字面字串 → Python 型別字串」（如 `BigDecimal` → `Decimal`、`Optional<String>` → `str | None`）與「Java camelCase 識別字 → Python snake_case 識別字」這兩件事，規則完全相同，不是恰好想法一致，因此集中到 `common/java_type_mapping.py`：

- `common/java_type_mapping.map_java_type(java_type: str, known_classes: frozenset[str] = frozenset()) -> str`：遞迴處理泛型容器，未知型別原樣沿用
- `common/java_type_mapping.camel_to_snake(name: str) -> str`：camelCase 轉 snake_case，連續大寫（縮寫）視為單一詞界
- `design_agent/type_mapping.py` 從這裡 `import camel_to_snake, map_java_type`，既有呼叫端（`type_mapping.map_java_type(...)` 這種模組屬性存取寫法）不需要改動，見 05a 五章、08a 五章
- `map_java_type()` 額外涵蓋 `java.time`／`java.util` 日期時間型別對應（`LocalDate`／`LocalDateTime`／`LocalTime`／`Date`／`Instant`），見 08a 五章

### Java class annotation 判斷（共用工具）

① 解析 Agent（04a 四章 `needs_llm_summary()`，判斷 class 值不值得花一次 Claude API 摘要）與 ③ 架構設計 Agent（05a 三章「孤兒類別／資料容器占位」，判斷沒有一般方法的 class 該渲染成什麼）都需要判斷「這個 class 是不是 Lombok／JPA 標記的資料容器」，是同一份機械 annotation 清單，不是恰好想法一致，因此集中到 `common/java_annotations.py`：

- `common/java_annotations.DATA_CLASS_ANNOTATIONS`：`@Entity`／`@Embeddable`／`@MappedSuperclass`／`@Data`／`@Value`／`@Getter`／`@Setter`／`@Builder`／`@NoArgsConstructor`／`@AllArgsConstructor`／`@RequiredArgsConstructor` 的聯集，只是「大概率是資料容器」的觸發訊號，不是最終判斷依據——各自呼叫端仍需要自己的方法清單／欄位／`InterfaceSpec` 覆蓋範圍等資訊才能下最終判斷
- `common/java_annotations.JPA_ENTITY_ANNOTATIONS`：`DATA_CLASS_ANNOTATIONS` 的子集（`@Entity`／`@Embeddable`／`@MappedSuperclass`），只有 ③ 需要單獨判斷——DB schema 欄位層級規格不是③的職責（見 05a 九章），偵測到這個子集時③直接跳過、不渲染，交由④直接從 DB 取得；① 不需要這個區分（04a 只需要「大概率是資料容器」這個粗粒度判斷）

### 檔案讀寫編碼慣例

全專案的 config／JSON／原始碼檔案都含中文內容，任何 `open()`／`Path.read_text()`／`Path.write_text()` 呼叫都必須明確帶 `encoding="utf-8"`，不依賴平台預設編碼——Windows 預設用 cp950，讀寫這些檔案會直接 mangle 或丟出 `UnicodeDecodeError`。這是寫死的程式撰寫慣例，不是每個環境可能想覆寫的值，因此不經環境變數／`.env` 配置。

---

## 七、Agent 職責定義

### ① 解析 Agent（Claude API）

- **輸入**：Java 專案原始碼
- **輸出**：模組清單與依賴關係、核心業務邏輯摘要、API 對應表（`java_controller` ↔ 所屬 `module`）——Java → Python 的檔案/函式對應由 Agent ③ 的 `python_structure` 決定，① 不產出這份對應（見 04a 六章）

> Agent ① 不解析 request/response schema，這部分由 Agent A 從 springdoc-openapi 自動取得。

---

### [A] Spec Agent（純程式邏輯，不需要 LLM）

啟動 Java 服務（連接測試用 DB，直接 `java -jar` 執行已打包的 jar，不需 Maven build 這一步），呼叫其 `/v3/api-docs` endpoint 取得完整 OpenAPI 3.0 spec，存成 `openapi.json`。全程不解析任何 Java 原始碼。

→ 具體步驟、啟動方式、URL 見 `03a_spec_collection_agent_architecture.md`。

---

### [B] Collection Agent（程式邏輯 + 少量 LLM）

將 `openapi.json` 轉換為兩份 Postman Collection（readonly / mutation），並用 LLM 依 `seed.sql` 填入合理的範例值，避免打出去直接 404。若 API 間存在鏈式依賴（如先建立資源、再用其回傳 ID 查詢），填值與執行順序另有處理機制，不能單純靜態填 `seed.sql`。

> **設計不變量**：鏈式依賴的 ID 一律用 Postman Environment Variable 在單次 newman 執行內動態傳遞，錄製（對 Java）與驗證（對 Python）是兩次各自獨立的 newman 執行，各自用自己那次執行實際產生的 ID，不會跨執行或跨服務把 ID 寫死共用——這也是即使 Java／Python 的 DB 自增序列產生的實際數值不同，鏈式請求也不會因此打到不存在的資源的原因。

- **輸出**：`postman/collection_readonly.json`、`postman/collection_mutation.json`

→ 轉換流程、LLM 填值邏輯、鏈式依賴處理見 `03a_spec_collection_agent_architecture.md`。

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

→ task 拆解演算法、涵蓋率驗證、依賴排序細節見 `06a_plan_agent_architecture.md`。

---

### ④ 骨架實作 Agent（translator-cli）

依 Agent ③ 已經決定好的 `python_structure`（目錄結構＋interface 定義，見三/translator-cli），透過 translator-cli 骨架生成模式機械組裝出目錄、base class、router 骨架、config、DB schema，不含業務邏輯、不呼叫本地模型。骨架階段產出的函式簽名，是 Agent ⑤ 呼叫 translator-cli 填空模式時的目標。

> DB schema 內容由④自行從既有 DB 或 Java entity 取得、封裝成 `db_models` 傳給 `generate_scaffold()`（見 07a 四章），這一步同樣是機械讀取，不需要模型。
>
> 與 [P] Plan Agent 平行執行，兩者都完成後才進入 Agent ⑤。

---

### ⑤ 功能改寫 Agent（translator-cli + qwen2.5-coder:32b）

依 task list 逐一呼叫 translator-cli 實作業務邏輯，每次 task 鎖定單一函式。

**排程 vs. 執行併發**：多個 module 的 task 可以同時處於「就緒可排程」狀態（同一 module 內仍依 `depends_on` 序列執行），但對本地模型的**實際生成請求序列化，併發數固定為 1**（硬體限制見三/LLM 分工）。也就是說平行帶來的效益是「排程更有彈性、模組完成順序不死板卡住」，而不是「總耗時等比例縮短」；總耗時大致等於所有 task 的模型生成時間總和。排程順序須依 module 間依賴圖決定，優先完成同一 module 再釋放下游。

驗證分兩個層級，觸發時機不同：
- **task 完成**：僅觸發 translator-cli 內建的語法驗證（AST parse），確認寫入沒有破壞語法，不觸發 API 級測試
- **module 完成**（該 module 底下所有 task 都已完成）：才觸發該 module 的局部驗證，跑此 module 的 golden cases——因為 API 呼叫鏈往往橫跨 repository/service/router 多個函式，過早以單一 task 觸發 API 測試會產生大量「依賴鏈未接完」的假失敗

→ translator-cli 的填空契約、AST 插入機制、request queue 序列化設計見 `07a_translator_cli_architecture.md`；局部驗證與全量驗證的兩層架構見 `02a_harness_architecture.md`。

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

每個模組記錄 Java 原始檔路徑、所屬 module 名稱（對應 `fixtures/golden/` 子目錄）、依賴的其他模組、方法清單（Java 方法名、所屬 class、描述、複雜度）。API 對應表另記錄 endpoint、HTTP method、Java Controller、所屬 module；schema 資訊不在此處，由 Agent A 提供。**不含 Python 目標檔路徑／Python 方法名**——Java → Python 的檔案/函式對應由 Agent ③ 的 `python_structure.interfaces` 決定，① 不產出這份對應，見 04a 六章、05a 二～九章。

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

## 九、LangGraph 狀態與圖結構

Agent 之間的資料透過 LangGraph 的 State 傳遞：從 ① 讀取 `java_project_path` 開始解析，一路累積到 ⑥ 產出 `test_results` 結束。State 的完整型別定義（`RefactorState`，含每個欄位由誰產出、型別是什麼）、圖的節點/邊實際建構（[P]/④ 平行分支、⑤ 的 module 排程器、retry 迴圈的 conditional edge）都屬於實作細節，不在此重複列表，一律以 01 為準——避免兩邊各自維護同一份欄位清單、日後漏同步。

→ 完整設計見 `01_langgraph_architecture.md`。

---

## 十、待決定事項

- [ ] **06a 是否要為「[P]／④ 平行執行、彼此看不到對方輸出」這個結構性限制補一條「已知例外」文字說明**：資料面已經有 `RefactorState.skipped_interfaces`／`skipped_db_models`（`scaffold_node.py` 寫入，見 08a 十二章）供之後的 09a／⑦ Debug Agent 在遇到「AST 定位失敗：scaffold/task 不一致」時比對，判斷根因是④的骨架缺口、不是⑤的實作問題；這裡懸而未決的只是 06a 文件本身要不要額外補一段對應說明，不影響資料落地

---

## 十一、文件索引

| 文件 | 內容 |
|---|---|
| `00_refactor_architecture.md`（本文件） | 整體流程、Agent 職責邊界、資料流 |
| `01_langgraph_architecture.md` | LangGraph 實作細節：State schema 的實際型別定義、graph 的 node/edge 建構、[P]/④ 平行分支與 ⑤ module 排程器實作、conditional edge（retry 迴圈）、stub-first 開發策略、跨平台（含 Windows）注意事項、專案初始化 |
| `02a_harness_architecture.md` | Harness 詳細設計：Recorder / Verifier、Masker、DiffEngine、Report 格式、DB 環境、Route Mapping 演算法 |
| `02b_harness_code.md` | Harness 各模組的實際程式碼實作 |
| `03a_spec_collection_agent_architecture.md` | [A] Spec Agent / [B] Collection Agent 詳細設計：Java 服務啟動與 `/v3/api-docs` 擷取步驟、OpenAPI → Postman Collection 轉換流程、LLM 填值邏輯、鏈式依賴處理 |
| `03b_spec_agent_code.md` | [A] Spec Agent / [B] Collection Agent 的實際程式碼實作 |
| `04a_parse_agent_architecture.md` | ① 解析 Agent 詳細設計：模組拆分邏輯、業務邏輯摘要產出方式、依賴關係判定 |
| `04b_parse_agent_code.md` | ① 解析 Agent 的實際程式碼實作 |
| `05a_design_agent_architecture.md` | ③ 架構設計 Agent 詳細設計：Python 專案結構、interface 定義規格（檔案相對路徑＋函式簽名層級）、route_to_file_mapping 產出邏輯 |
| `05b_design_agent_code.md` | ③ 架構設計 Agent 的實際程式碼實作 |
| `06a_plan_agent_architecture.md` | [P] Plan Agent 詳細設計：task list 拆解演算法、方法清單覆蓋率檢查、依賴排序 |
| `06b_plan_agent_code.md` | [P] Plan Agent 的實際程式碼實作 |
| `07a_translator_cli_architecture.md` | translator-cli 詳細設計：填空契約、骨架生成介面、AST 插入機制、git snapshot 流程、衝突偵測、目標語言 adapter 介面 |
| `07b_translator_cli_code.md` | translator-cli 的實際程式碼實作 |
| `08a_scaffold_agent_architecture.md` | ④ 骨架實作 Agent 詳細設計：如何依 ③ 的輸出呼叫 translator-cli 骨架生成介面、`db_models` 如何從 Java entity 原始碼組出（JPA entity 掃描、table／欄位對應、`RefactorState` 介面異動） |
| `08b_scaffold_agent_code.md` | ④ 骨架實作 Agent 的實際程式碼實作 |
| `09a_implement_agent_architecture.md` | ⑤ 功能改寫 Agent 詳細設計：task list 消費邏輯、module 排程、task／module 兩層驗證觸發時機 |
| `09b_implement_agent_code.md` | ⑤ 功能改寫 Agent 的實際程式碼實作 |
| `10a_debug_agent_architecture.md` | ⑦ Debug Agent 詳細設計：diff 分析邏輯、修正指令產出格式 |
| `10b_debug_agent_code.md` | ⑦ Debug Agent 的實際程式碼實作 |

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*