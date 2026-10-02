# Java → Python 重構計畫：Multi-Agent 協作架構

> 以 LangGraph + Claude API + translator-cli + qwen2.5-coder:32b 為核心方向；⑤ 功能改寫 Agent 的 context 組裝已改為「呼叫鏈 + 完整 Java 原始碼」，qwen 僅用於 repository 層翻譯，其餘一律 Claude API（見六、三章，完整實測依據見 `refactor_plan.md`）。
> 本文件為架構主幹，聚焦「整體流程、Agent 職責邊界、資料流」；各 Agent 的實作細節、演算法、程式碼請見對應細節文件（見十、文件索引）。

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
 （產出 task list，       （建立目錄與骨架）
  含分層與翻譯後端標記）
  └──────────┬──────────┘
             ↓
⑤ 功能改寫 Agent     → 直接餵「呼叫鏈 + 完整 Java 原始碼」翻譯，不經 LLM 語意摘要（見六章、`refactor_plan.md`），
                        分三層全域關卡執行：
                        Phase 1（entity/dto 機械產生不需模型；repository 用 qwen；utils 用 Claude API）
                          全部完成、通過語法驗證後才釋放 service
                        service（Claude API；呼叫鏈遇到 Phase 1 已完成層直接讀真實 Python 原始碼）
                          全部完成後才釋放 controller/router
                        controller/router（Claude API；呼叫鏈遇到 service 直接讀真實 Python 原始碼）
             ↓
⑥ 測試執行 Agent     → 【Harness 驗證端】對 Python 服務執行 Postman，比對 golden output
             ↓
⑦ Debug Agent        → 讀取 Harness report，分析 diff，定位問題，直接寫出並套用修正後的程式碼（⑤ 完全退出這個迴圈，見 10a）
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
| [P] Plan Agent | 產出 Agent ⑤ 的 task list，含分層（`phase`／`translator_backend`）與呼叫鏈參照座標（`reference_targets`） | `06a_plan_agent_architecture.md` / `06b_plan_agent_code.md` |
| ④ 骨架實作 Agent | 建立目錄與骨架（呼叫 translator-cli「骨架生成模式」），並從 Java entity 原始碼組出 `db_models` | `08a_scaffold_agent_architecture.md` / `08b_scaffold_agent_code.md` |
| ⑤ 功能改寫 Agent | 依三層全域關卡（Phase 1／service／controller-router）改寫業務邏輯，直接餵呼叫鏈＋完整 Java 原始碼（不經 LLM 摘要），依層級切換 qwen／Claude API（呼叫 translator-cli「填空模式」） | `09a_implement_agent_architecture.md` / `09b_implement_agent_code.md` |
| ⑥ 測試執行 Agent（Harness 驗證端） | 對 Python 服務執行 Postman，比對 golden output | `02a_harness_architecture.md` / `02b_harness_code.md` |
| ⑦ Debug Agent | 分析 diff、定位問題，直接寫出並套用修正後的程式碼（不回饋給 ⑤ 重新翻譯，⑤ 不論 qwen 還是 Claude API 那條路徑都完全退出這個迴圈） | `10a_debug_agent_architecture.md` / `10b_debug_agent_code.md` |

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

**整個 pipeline 只有一種情況用本地 qwen2.5-coder:32b：⑤ 對 repository 層的翻譯。其餘所有需要模型的地方——①③⑦、[B]，以及⑤對 entity/dto 以外其他層的翻譯——一律 Claude API；entity／dto 則完全不呼叫任何模型（純機械產生）。**

| 層級／任務 | 所屬全域關卡（見六章三層關卡） | 模型 | 位置 | 理由 |
|---|---|---|---|---|
| ①③⑦、[B]（解析、規劃、設計、Debug、填值） | — | Claude API | 雲端 | 擅長理解需求、設計拆分、複雜推理 |
| ⑤：entity／dto | Phase 1 | — | — | 純機械產生（③④既有機制），不呼叫任何模型 |
| ⑤：repository | Phase 1 | **qwen2.5-coder:32b** | 另一台 Mac（本地） | 省費用；實測 8/8 案例零缺陷（含 JPQL／native SQL `@Query` 困難案例），見 `refactor_plan.md` 5.2 |
| ⑤：utils | Phase 1 | Claude API | 雲端 | 實測 5 個 class 裡有 3 個非 fatal 但隱蔽的邏輯 bug（只在特定邊界輸入才會現形，語法驗證抓不到），風險不對稱，不值得為了省成本冒險，見 `refactor_plan.md` 5.4 |
| ⑤：service | service | Claude API | 雲端 | 業務邏輯所在，且 model/entity 層（性質相近）實測 4/4 都出現 fatal 錯誤，一律不交給 qwen |
| ⑤：controller／router | controller/router | Claude API | 雲端 | 同上 |

這是實測結果，不是憑印象假設：`refactor_plan.md` 用真實案例（repository 8/8、model/entity 4 次全出 fatal 錯誤、utils 5 個 class 混合但有隱蔽 bug）比較 qwen／Claude API 在不同層級的翻譯正確率後定案。

> **硬體限制**：該機器跑單一 qwen2.5-coder:32b 已達飽和，無法水平擴展成多實例。因此 Agent ⑤ 對 qwen 的**實際生成請求（僅限 repository 層）需序列化（併發數=1）**，LangGraph 排程層仍可讓多個 task 同時處於就緒佇列以保留彈性，但不代表平行會讓總耗時變短——詳見七/⑤。Claude API 呼叫（utils／service／controller-router）不受這個序列化限制，走一般的 Claude API 併發策略。
>
> **排程順序**：module 間的依賴關係直接沿用 Agent ① `module_list` 的「依賴的其他模組」欄位，排程時優先讓同一 module 的 task 連續完成並通過局部驗證後，才釋放依賴它的下游 module——避免上游局部驗證 fail 時，下游已完成的產物一併作廢，回滾成本過高。
>
> **三層全域關卡（barrier）是新增的一層排程約束，在既有的 module 依賴排程之上**：不是逐 module 各自判斷，是**全專案**所有 module 的 Phase 1（entity/dto/repository/utils）task 都完成、通過語法驗證，才釋放任何 module 的 service task；全專案所有 module 的 service task 都完成，才釋放任何 module 的 controller/router task——選全域而非逐 module，是因為規則簡單，不用處理「B module 的上一關還沒完成、A module 的下一關卻呼叫到它」這種跨 module 邊界情況；代價是犧牲一些排程彈性，但 qwen 那條路徑（repository）本來就序列化，全域關卡不會讓損失顯著變大。完整設計與呼叫鏈邊界規則見 `refactor_plan.md` 一章、`graph/scheduler.py::_global_tier()`/`_tier_barrier_satisfied()`。

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
├── Java 服務（本機直接執行：[A] Spec Agent 啟動的 java -jar）
├── Python 服務（Docker 容器內執行，非本機直接跑——見下方備註）
└── translator-cli（Python 套件，in-process 呼叫，非獨立子行程——見 11a 六章「run_id：執行模型查證」對實際程式碼的核對）
      ├── 骨架生成模式（④）：純機械組裝，全程留在這台機器，不發出任何請求
      └── 填空模式（⑤）：HTTP（帶 Authorization: Bearer <token>）→ 另一台 Mac
                            └── nginx（反向代理，驗證 token）
                                 └── ollama（僅接受來自 nginx 的本機轉發）
                                      └── qwen2.5-coder:32b（程式碼實作）
```

> 本地模型機器對外只曝露 nginx 的 port，ollama 自己的 port（預設 `11434`）不對外開放，只接受 nginx 轉發進來的請求；translator-cli 端的 `OLLAMA_BASE_URL` 因此指向的是 nginx，而不是 ollama 本身。這條連線只有填空模式會用到，骨架生成模式不涉及。
>
> **Python 服務改跑在 Docker 容器內，不是「日後同樣跑在這台機器」**：這是 09a／09b 落地 ⑤ 時才定案、比最初規劃更晚確定的環境依賴。原因是 `uvicorn --reload` 在 Windows 上經常無法真正完成重啟（Windows 的 `CTRL_C_EVENT` 送達機制不可靠，已用真實環境重現），⑤ 局部驗證依賴的熱重載同步屏障因此在 Windows 上不穩定；改成容器內的 Linux 環境後，uvicorn 走穩定的 POSIX `SIGTERM` 重啟路徑，不受這個限制影響。`python_service/process.py` 的 `PythonServiceContainer` 用 `docker run` 啟動，把 `python_project_path` bind mount 進容器（容器內固定路徑 `/srv`），由 `implement_node.run()` 在整條 graph run 第一次進入 `implement` 時啟動、`main.py` 收尾時統一關閉——不需要人工手動啟停。完整設計見 `09a_implement_agent_architecture.md` 三章「熱重載競態」、`09b_implement_agent_code.md`。

---

## 五、環境建立

在開始執行整個流程前，需要在各機器上準備好以下工具：

**你的電腦（Orchestrator 所在機器）**
- Python 虛擬環境，安裝 `langgraph` 和 `anthropic`（Claude API 呼叫走 `anthropic` SDK 直接呼叫，不經 `langchain`，見 `01_langgraph_architecture.md` 二章）
- Node.js 環境，安裝 `openapi-to-postmanv2` 和 `newman`（全域安裝）
- Claude API 金鑰，設定在環境變數
- PostgreSQL client 工具（`psql`），用於 Harness 的 DB seed 操作
- **Docker**（Docker Desktop 或等價的 Docker Engine）：⑤ 執行期間目標 Python 服務跑在 Docker 容器內，不是本機直接 `uvicorn`，見四章備註、下方「Python 服務端」、`09a_implement_agent_architecture.md` 三章

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

**Python 服務端（一次性準備，Docker 容器）**
- 不需要人工手動啟動 Python 服務——`python_service/process.py`（`PythonServiceContainer`）由 `implement_node.run()` 在整條 graph run 第一次進入 `implement` 時自動 `docker run` 啟動，`main.py` 收尾時統一關閉，見四章備註、`09a_implement_agent_architecture.md` 三章
- `python_project_path` 底下需要先完成一次性準備，且必須排在 `generate_scaffold()`（④）第一次執行之前：新增 `.gitignore`（至少含 `_reload_probe_wrapper.py`／`_reload_token.py` 兩個檔名）並 `commit` 一次，再放入 `_reload_probe_wrapper.py`（固定樣板，掛載真正的 `app.main.app` 並額外提供 `/__reload_probe__` 熱重載同步探測端點，不需要人工編寫內容）——順序顛倒會讓 translator-cli 的 precondition 檢查（`git status --porcelain` 非空即拒絕寫入）在骨架階段就直接失敗；完整機制見 09a 三章「熱重載競態」「這兩個檔案必須排除在衝突偵測之外」
- 容器內只安裝已知的最小基線套件集合（`fastapi`／`uvicorn[standard]`／`sqlalchemy`／`psycopg2-binary`，見 `python_service/process.py::BASELINE_PACKAGES`）——**目前仍是待解決缺口**：若目標專案實際還需要更多依賴（如 `alembic`），還沒有機制能自動偵測、安裝進容器，見 `09b_implement_agent_code.md` 九章
- 容器啟動指令固定 `uvicorn _reload_probe_wrapper:wrapper_app --reload`，不是直接 `uvicorn app.main:app --reload`——見上一項的 wrapper 檔案

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

### ⑤ 的 context 來源：呼叫鏈 + 完整原始碼

⑤ 不吃 `[P]` 生成的語意摘要，直接餵「完整 Java 原始碼」；`[P]` 只負責用機械圖走訪界定「這個函式該參照哪些其他函式」的座標範圍（`reference_targets`），實際把座標讀成原始碼文字是⑤的工作——「規劃範圍」與「執行讀取」是兩個各自單純的職責，不疊在同一步做。分**三層全域關卡**執行（不是逐 module 各自判斷）：全部 module 的 Phase 1 都完成、通過語法驗證，才釋放任何 module 的 service task；全部 module 的 service 都完成，才釋放任何 module 的 controller/router task。舊設計曾讓 `[P]` 產生 LLM 語意摘要（`description`／`context`／`referenced_interfaces`）供⑤翻譯依據，因為這層轉譯本身不穩定、會遺漏或扭曲 Java 原始碼裡的真實資訊而被取代；完整診斷過程見 `refactor_call_chain_implement_prompt.md`，定案設計與實測數據見 `refactor_plan.md`。

- **Phase 1（entity／dto／repository／utils，全專案）**：全部直接照 Java 原始碼翻譯，不經摘要層。entity／dto 沿用③④既有機制機械產生（不呼叫模型）；repository（Spring Data 衍生查詢介面與有真實邏輯本體的類別，皆照原始碼翻）交給 qwen；utils 交給 Claude API（見三章「LLM 分工」的實測依據）。Phase 1 內部若有互相呼叫（如某個 utils 依賴另一個 utils），直接展開 Java 原始碼，不套用下面的「讀已翻譯 Python」捷徑——這類呼叫鏈通常很淺，不值得為此另外處理排程保證。
- **service（全專案，Claude API）**：呼叫鏈遇到 Phase 1 的 callee（entity/dto/repository/utils）時，**不再遞迴展開 Java 原始碼**，改讀「已翻譯完成的真實 Python 原始碼」——因為 Phase 1 是全域先做完才開始這一關，這份 Python 原始碼在讀取當下保證已經存在，不需要查任何排程完成狀態；遇到同 module 的同層呼叫（service 呼叫 service）則直接展開 Java 原始碼並設上限防止 context 爆量，跨 module 的同層呼叫則沿用既有的 module 依賴排程保證，讀已翻譯 Python（見 `refactor_plan.md` 一章「呼叫鏈規則」）。
- **controller／router（全專案，Claude API）**：全部照 Java 原始碼翻譯；呼叫鏈遇到 service（此時全域已保證翻完）直接讀真實 Python 原始碼，不繼續往下展開。

`[P]`（`plan_agent/call_chain.py`）重用①既有機械化的呼叫圖分析（`parse_agent/call_graph.py`）＋③新增的 `python_structure.java_index`，做一層 BFS 圖走訪算出每個 task 的 `reference_targets`（只有座標，不含原始碼文字，設有數量上限防止 context 爆量）；⑤（`graph/java_source_extraction.py`）才把座標實際解析成原始碼文字：task 自己對應的 Java 方法讀**整個 Java 檔案**（真實專案裡一個檔案恰好一個頂層 class，等同整個 class，不需要額外定位單一方法），`reference_targets` 則以單方法抽取為主、總量超過門檻才對 Java 項目裁減成單方法（Python 側用 `ast.get_source_segment()` 精確抽取）。呼叫鏈本身不是 LLM 生成，不會有摘要失真的問題。

**驗證閘門**：Phase 1 task 完成只做語法驗證（AST parse），功能驗證延續既有的 module 級 API 驗證機制（見 `02a_harness_architecture.md` 十三章「Module 級別的局部驗證」），不新建 Phase 1 專屬驗證——這是目前的決定，但實測發現「值層級」邏輯 bug（型別轉換沒判斷 `None`、正規表示式跳脫規則錯誤）語法驗證完全接不住，只有真的執行、帶邊界值輸入的測試才驗得出來，若之後真實 bug 率偏高需要重新評估，見 `refactor_plan.md` 四章。

**Graph 層級的影響**：⑤ 這個 node 拆成三個被全域硬性關卡隔開的階段，`graph/scheduler.py::ModuleScheduler` 在現有的跨 module 依賴排程之上，多一層「全專案 Phase 1 先於 service 先於 controller/router」的全域關卡（`_global_tier()`／`_tier_barrier_satisfied()`），取代原本規劃的「同 module 內依層級排程」——這條同 module 內部排序規則已被全域三層關卡取代，不需要同時維護兩套機制。完整設計見 `01_langgraph_architecture.md`。

### 「單一函式粒度」原則

「單一函式粒度」「檔案內容不能整份帶入」原則適用於 context 組裝，抽取來源是「Java 原始碼或已翻譯完成的 Python 原始碼」（依上面呼叫鏈邊界規則而定）：

[P] Plan Agent 產出 task list 時，必須將每個 task 切到**單一函式**的粒度（不保留「或類別」的模糊選項），讓 Agent ⑤ 每次呼叫 translator-cli 時只需傳入少量相關檔案，且輸出契約與 translator-cli 的「填空式」設計（模型只回傳單一函式本體）完全對齊。絕不能把整個專案目錄丟進去，否則 context 爆炸會導致實作品質下降。

例如實作 `UserRepository.get_by_id()` 時，只需傳入 `user_repository.py` 和 `user.py` 兩個檔案，不需要傳入整個 `src/` 目錄。

**「傳入哪些檔案」不等於「檔案內容整份帶入」**：真實環境端對端測試量化證實，即使已經照上述原則把 task 切到單一函式，若相關檔案本身塞滿其他無關的姊妹函式／方法，一樣會讓 context 暴增到本地模型無法穩定處理的量級（`09b_bug_trace.md` #37）。現行機制（`reference_targets` 函式層級抽取＋總量上限，見上方）已把這個原則落實到呼叫鏈的每一層，細節見 `06a_plan_agent_architecture.md` 六章。

### Claude API 端的大範圍語意判斷：map-reduce 模式

上述「單一函式粒度」原本是針對本地模型（qwen）填空任務的 context 控制，現在不論哪個後端都沿用同一套原則。但 Claude API 這側另外還有「輸入規模大、不能整包塞入」的任務——差別在於這類任務**需要跨邊界的全局視野**才能得出正確結論（例如判斷哪個 endpoint 的輸出是另一個 endpoint 的輸入，或是整個 Java 專案的模組拆分），不能像 translator-cli 那樣單純切到最小單位、互不相干地各自處理。

對這類任務，設計方向是 **map-reduce**，而非單純「拆分後平行、各自獨立產出」：

1. **Map**：依自然邊界（如 controller、Java 檔案）拆分，各自平行產出**局部候選結果**（不下最終判斷，只標記「看起來相關」的線索）
2. **Reduce**：彙整所有局部候選結果（此時輸入已大幅濃縮，而非原始全量資料），做跨邊界的最終判斷與合併

適用場景舉例：① 解析 Agent 對 Java 專案依 controller 拆分後的合併分析、[B] Collection Agent 的鏈式依賴偵測（跨 controller 的 producer/consumer 配對）。若合併階段只是機械拼接、不重新做跨邊界判斷，會系統性漏掉邊界之間的關聯，因此 reduce 階段不可省略。

具體怎麼拆、候選結果的資料形狀、reduce 階段的輸入輸出契約，由各自的細節文件定案。

### Claude API 呼叫紀錄（成本稽核與內容留痕）

任何 Agent（不限於 ①③⑦、[P]、[B] 現有這幾個）只要呼叫 Claude API，都必須記錄用量與呼叫內容，不能只驗證輸出對不對，不驗證花了多少、送了什麼。

完整機制——一般執行 log、Claude／本地 Ollama 呼叫的 prompt／response 記錄、`run_id`／`trace_id` 等識別碼設計、`llm_traces.db` 查詢介面（`llmlog` CLI 與 ⑦ Debug Agent 共用同一組函式）——見 `11a_logging_architecture.md`／`11b_logging_code.md`。`common/llm_client.py::call_claude_for_json()` 是所有 Agent 呼叫 Claude API 的唯一入口，內部直接呼叫 `common/llm_trace.py::record_llm_call()` 記錄進 `llm_traces.db`，呼叫端不需要自己組 log 格式。

### Claude API 呼叫封裝（共用 client）

「怎麼打 API」這件事本身（client 初始化、Structured Outputs 的 `output_config.format` 組裝、JSON parse、錯誤處理、呼叫紀錄）在 ①③⑤(⑦ Debug)、[P] Plan、[B] 這些會呼叫 Claude API 的 Agent 之間幾乎完全相同——差別只在「用哪個模型」（各 Agent 自己的環境變數，如 `SPEC_COLLECTION_AGENT_MODEL`／`PARSE_AGENT_MODEL`）。這部分因此集中在 `common/llm_client.py`：

- `common/llm_client.call_claude_for_json(*, system_prompt, user_prompt, schema, model, max_tokens=4096, ...)`：唯一對外函式，`model` 是必填參數——這個模組不知道任何 Agent 的環境變數命名慣例，「沒指定要用哪個模型時退回什麼」是每個 Agent 自己的決策，不由共用層代為決定。完整參數（含 `run_id`／`task_id`／`target_file`／`class_name`／`function_name` 等純記錄用選填參數）見 `11a_logging_architecture.md` 八章。
- 各 Agent 只需要自己的 `llm.py` 留幾行：讀自己的環境變數，沒設定時退回 `common.llm_client.DEFAULT_MODEL_FALLBACK`，算出 `DEFAULT_MODEL` 常數，呼叫端把這個值傳進 `call_claude_for_json(..., model=DEFAULT_MODEL)`。
- `max_tokens` 比照同一個原則，是 Agent 自己的決策，不是共用層該猜的事——差別在於「多少 token 夠用」取決於這個 Agent 輸出內容的密度：`call_claude_for_json()` 的 `max_tokens` 有預設值（`DEFAULT_MAX_TOKENS=4096`），給輸出精簡的分類／抽取型 Agent（①③[B]）直接沿用即可，不需要每個 Agent 都覆寫；但輸出密度高的 Agent（如 [P] Plan Agent，每個 task 都帶完整業務描述／context／依賴清單）需要在自己的 `llm.py` 另外算一個 `PLAN_AGENT_MAX_TOKENS` 常數、呼叫時明確覆寫，同樣經環境變數可調，不寫死在程式碼裡——實測案例見 `06a_plan_agent_architecture.md` 五章。

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
>
> **`InterfaceSpec.phase` 欄位**（承接六章「呼叫鏈 + 完整原始碼」設計）：標記這個 interface 屬於 Phase 1（entity/dto/repository/utils）還是 Phase 2（controller/service），供 `[P]` 排程與呼叫鏈範圍查找判斷「這個 callee 該讀 Java 還是讀已翻譯的 Python」使用（見六章）。**計算規則**：`@Repository` 或 package 路徑為 `xxx.utils` → Phase 1，entity/dto 已有既有機制，`@Service`／`@RestController` → Phase 2；極少數無 stereotype 又不在 utils package 的邊界情況沿用 05a 既有的 LLM fallback，見 `refactor_plan.md` 二章。另外新增 `java_method_id`（這個 interface 對應的 Java 方法識別碼）與 `python_structure.java_index`（Java method_id → Python 對應資訊的全表），供 `[P]` 做呼叫鏈範圍查找時查表用，不需要重新實作③既有的 camelCase／多載消歧邏輯，見 `05a_design_agent_architecture.md` 對應章節。

→ `route_to_file_mapping` 的 key 格式與比對演算法見 `02a_harness_architecture.md`。

---

### [P] Plan Agent（純機械邏輯，不呼叫 LLM）

不產出 `description`／`context` 這種 LLM 語意摘要（那正是六章要拿掉的失真來源），改成純機械打標籤＋組裝：複製③算好的 `phase`，依「這個 task 屬於哪一層」機械決定 `translator_backend`；用①的呼叫圖 + ③的 `java_index` 做 BFS 圖走訪，機械算出每個 task 的呼叫鏈參照範圍（`reference_targets`，只有座標，不含原始碼文字）。**不呼叫 Claude API**——Phase 1 的 task 切分（entity/dto/repository/utils，依 method 對應的 Java class stereotype／衍生查詢介面／package 路徑判斷）與呼叫鏈範圍查找皆已定案為純程式邏輯即可決定，不需要 LLM（見 00 二章「能用程式判斷的，就不要交給 LLM」）。`description`／`context` 兩欄仍保留在輸出裡，但角色改為純機械內容：`description` 是機械組出的翻譯目標描述，`context` 是 `@Value` 設定值注入提示（見八章）。

- **輸入**：Agent ① 的模組清單 + Agent ③ 的架構設計
- **職責**：切出 Agent ⑤ 在進入 Agent ⑥（測試執行）之前必須完成的完整範圍，拆解成可獨立執行的 task list，並標記每個 task 的 `phase`、`translator_backend`、`reference_targets`

拆解時的關鍵原則：
- **覆蓋率**：以 Agent ① 輸出的**完整方法清單**為準，不能只依 API 對應表拆 task——否則沒有直接對應 API 的內部 helper function 容易被漏掉，漏掉的函式不會被 Harness 的 API 級測試直接抓到，而是等到被其他函式呼叫時才爆出不直觀的錯誤
- **粒度**：每個 task 鎖定**單一函式**，與 translator-cli 的輸出契約對齊（見第六節）
- **依賴順序**：`depends_on` 只表達同一 module 內真正的呼叫依賴，不用來編碼「repository 先於 service 先於 router」這種跨層順序——那由 `graph/scheduler.py` 的全域三層關卡負責（見六章、`refactor_plan.md` 一章）；不同 module 間若無依賴關係，才可平行執行
- **模組歸屬**：每個 task 標記 `module`，供 Agent ⑤ 判斷該 module 的所有 task 是否已全數完成
- **翻譯後端**：每個 task 標記 `translator_backend`，依「這個 task 屬於哪一層」機械決定（repository → qwen，其餘 → claude），不需要 LLM 判斷（見八章 `TaskSpec`）

- **輸出**：task list（欄位格式見第八節）

→ task 拆解演算法、涵蓋率驗證、呼叫鏈範圍查找細節見 `06a_plan_agent_architecture.md`。

---

### ④ 骨架實作 Agent（translator-cli）

依 Agent ③ 已經決定好的 `python_structure`（目錄結構＋interface 定義，見三/translator-cli），透過 translator-cli 骨架生成模式機械組裝出目錄、base class、router 骨架、config、DB schema，不含業務邏輯、不呼叫本地模型。骨架階段產出的函式簽名，是 Agent ⑤ 呼叫 translator-cli 填空模式時的目標。

> DB schema 內容由④自行從既有 DB 或 Java entity 取得、封裝成 `db_models` 傳給 `generate_scaffold()`（見 07a 四章），這一步同樣是機械讀取，不需要模型。
>
> 與 [P] Plan Agent 平行執行，兩者都完成後才進入 Agent ⑤。

---

### ⑤ 功能改寫 Agent（translator-cli，依層級切換 qwen / Claude API）

依 task list 逐一呼叫 translator-cli 實作業務邏輯，每次 task 鎖定單一函式；每個 task 帶 `translator_backend` 標記，決定這次呼叫打 qwen 還是 Claude API（見三章「LLM 分工」、六章「⑤ 的 context 來源」）。**依三層全域關卡執行**（不是逐 module 判斷，見六章）：全部 module 的 Phase 1（entity/dto 機械產生、repository 用 qwen、utils 用 Claude API）都完成、通過語法驗證後，才釋放任何 module 的 service task；全部 module 的 service 都完成，才釋放任何 module 的 controller/router task；context 一律是「呼叫鏈 + 完整原始碼」，不是 `[P]` 產生的語意摘要。

**⑤ 的職責**：把 `[P]` 算好的座標（`java_method_id`／`reference_targets`）解析成真正的原始碼文字（`graph/java_source_extraction.py`）——task 自己對應的方法讀整個 Java 檔案，`reference_targets` 依 `[P]` 判斷的 Phase／層級標記，是「更基礎一關」的 callee 就讀已翻譯完成的真實 Python 原始碼（保證已存在，不需查排程狀態），是同層呼叫就展開 Java 原始碼（設上限防止 context 爆量）；範圍界定本身在 `[P]` 那一步已經做完，⑤ 不重新查呼叫圖。

**排程 vs. 執行併發**：多個 module 的 task 可以同時處於「就緒可排程」狀態（同一 module 內仍依 `depends_on` 序列執行）。qwen 這條路徑（僅 repository 層）**實際生成請求序列化，併發數固定為 1**（硬體限制見三/LLM 分工）——平行帶來的效益是「排程更有彈性、模組完成順序不死板卡住」，不是「總耗時等比例縮短」；Claude API 這條路徑（entity/dto 除外的其餘各層）不受這個序列化限制，走一般 Claude API 併發策略。排程順序須依 module 間依賴圖決定，優先完成同一 module 再釋放下游；**另外疊加全專案三層全域關卡**（見上方）。

驗證分兩個層級，觸發時機不同：
- **task 完成**：僅觸發 translator-cli 內建的語法驗證（AST parse），確認寫入沒有破壞語法，不觸發 API 級測試
- **module 完成**（該 module 底下所有 task 都已完成）：才觸發該 module 的局部驗證，跑此 module 的 golden cases——因為 API 呼叫鏈往往橫跨 repository/service/router 多個函式，過早以單一 task 觸發 API 測試會產生大量「依賴鏈未接完」的假失敗

→ translator-cli 的填空契約、AST 插入機制、request queue 序列化設計見 `07a_translator_cli_architecture.md`；局部驗證與全量驗證的兩層架構見 `02a_harness_architecture.md`；三層全域關卡排程、雙後端切換、context 組裝的完整設計與實測依據見 `09a_implement_agent_architecture.md`、`refactor_plan.md`。

---

### ⑥ 測試執行 Agent（程式邏輯｜Harness 驗證端）

對 Python 服務執行 Postman collection，對比 golden output。

- **輸出**：pass/fail 清單 + diff 報告

→ 比對邏輯（Masking、陣列順序處理）與 report 格式見 `02a_harness_architecture.md`。

---

### ⑦ Debug Agent（Claude API）

- **輸入**：fail 清單 + diff 報告 + 對應 Python 原始碼（`related_files`）
- 分析根本原因，**直接寫出並套用修正後的程式碼**（`fixed_body`／`file_fixes`），不是輸出指令交給 Agent ⑤ 重新翻譯——⑤（不論 qwen 還是 Claude API 那條路徑）完全退出這個除錯迴圈，即使是「⑤ 翻譯完全失敗」的函式，也由 ⑦ 直接從業務描述寫出完整實作，不會被排回 ⑤ 重試（見 `10a_debug_agent_architecture.md` 四章「一旦失敗過一次，不再退回本地模型」）

---

## 八、Agent 間的資料格式

Agent 間使用結構化 JSON 傳遞，不使用自然語言，避免資訊失真。

### Agent ① 輸出：模組清單

每個模組記錄 Java 原始檔路徑、所屬 module 名稱（對應 `fixtures/golden/` 子目錄）、依賴的其他模組、方法清單（Java 方法名、所屬 class、描述、複雜度）。API 對應表另記錄 endpoint、HTTP method、Java Controller、所屬 module；schema 資訊不在此處，由 Agent A 提供。**不含 Python 目標檔路徑／Python 方法名**——Java → Python 的檔案/函式對應由 Agent ③ 的 `python_structure.interfaces` 決定，① 不產出這份對應，見 04a 六章、05a 二～九章。

### [P] Plan Agent 的 task list

`description`／`context` 不再是 `[P]` 產出的 LLM 語意摘要（那正是六章要拿掉的失真來源）：`description` 是機械組出的翻譯目標描述，`context` 是 `@Value` 設定值注入提示（有的話）——⑤ 翻譯時的主要依據是呼叫鏈解析出的 Java／已翻譯 Python 原始碼（`java_method_id`／`reference_targets`），不是這兩欄。

| 欄位 | 說明 |
|---|---|
| `id` | task 唯一識別碼，如 `task_001` |
| `module` | 所屬模組名稱，對應 `fixtures/golden/` 子目錄；供 Agent ⑤ 判斷該 module 是否所有 task 都已完成，以觸發局部驗證 |
| `phase` | `1`／`2`，逐字複製對應 `InterfaceSpec.phase`；六章三層全域關卡進一步依 `target_files` 落在 `services`／`routers` 哪一層，把 `phase == 2` 再細分成 service／controller-router 兩批 |
| `translator_backend` | `"qwen"` / `"claude"`，依 task 所屬層級機械決定（repository → qwen，其餘 → claude，見三章「LLM 分工」），不需要 LLM 判斷 |
| `java_method_id` | 這個 task 自己對應的 Java 方法識別碼，⑤ 讀取其原始碼文字（整個檔案）的座標 |
| `reference_targets` | 六章「呼叫鏈範圍查找」機械算出的參照座標清單（`file_path`／`class_name`／`function_name`／`language`），只有座標不含原始碼文字，⑤ 據此讀出真正的參考原始碼 |
| `reference_targets_truncated` | `reference_targets` 因觸及數量上限而被截斷時設為 `True`，供事後追查翻譯品質是否受影響 |
| `description` | 機械組出的翻譯目標描述 |
| `target_files` | 這次 task 需要讀寫的檔案清單（`target_files[0]` 是實際寫入目標） |
| `context` | `@Value` 設定值注入提示（無則為空字串） |
| `depends_on` | 同一 module 內真正的呼叫依賴 id 清單；不編碼跨層順序，那由全域三層關卡負責 |

> Plan Agent 必須確保：同一個 `module` 底下的 task，合起來涵蓋 Agent ① 輸出的**完整方法清單**（不只是 API 對應表列出的方法），否則局部驗證觸發時會因缺函式而失敗，且錯誤不易與「邏輯寫錯」區分。
>
> 完整型別定義見 `graph/state.py::TaskSpec`，欄位語意與呼叫鏈範圍查找演算法見 `06a_plan_agent_architecture.md`。

### Agent ③ 的 route_to_file_mapping

產出後直接寫入 `config/harness.yaml`，不需人工填寫，格式細節見 `02a_harness_architecture.md`。

---

## 九、LangGraph 狀態與圖結構

Agent 之間的資料透過 LangGraph 的 State 傳遞：從 ① 讀取 `java_project_path` 開始解析，一路累積到 ⑥ 產出 `test_results` 結束。State 的完整型別定義（`RefactorState`，含每個欄位由誰產出、型別是什麼）、圖的節點/邊實際建構（[P]/④ 平行分支、⑤ 的 module 排程器、retry 迴圈的 conditional edge）都屬於實作細節，不在此重複列表，一律以 01 為準——避免兩邊各自維護同一份欄位清單、日後漏同步。

→ 完整設計見 `01_langgraph_architecture.md`。

---

## 十、文件索引

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
| `11a_logging_architecture.md` | 全域 log 機制詳細設計：一般執行 log、Claude API／本地 Ollama 呼叫的 prompt/response 記錄、run_id／trace_id 等識別碼設計、`llm_traces.db` 查詢層（`llmlog` CLI 與 ⑦ Debug Agent 共用） |
| `11b_logging_code.md` | 全域 log 機制的實際程式碼實作，含 `llmlog` CLI |
| `refactor_call_chain_implement_prompt.md` | ⑤ 改用「呼叫鏈＋完整原始碼」取代 `[P]` LLM 摘要的原始交接文件：問題診斷、新方案構想、既有基礎設施盤點 |
| `refactor_plan.md` | 承接上一份文件的定案設計：三層全域關卡分階段翻譯、呼叫鏈邊界規則、Agent 職責分工、qwen／Claude API 雙後端分工（含真實案例實測數據）、驗證閘門取捨。已落地進 `06a`／`07a`／`09a`／`01`（`08a` 因④職責不變而未動），剩餘未收斂項目見該文件六章 |

---

## 十一、局部真實測試 vs 完整真實測試

2026-08-28 新增：先前每次要驗證一批修正（不論是 Orchestrator 自己的程式碼，還是直接修正目標專案已生成的檔案）是否真的生效，唯一的辦法是重新跑一次「完整真實測試」——`python main.py`，從 ① 解析一路跑到 ⑦ debug 迴圈，實測耗時約 2.5 小時（絕大多數時間花在 ⑤ 呼叫 Ollama 翻譯全部 task）。這個成本讓「改一行、等 2.5 小時才知道有沒有效」的回饋循環太慢，因此新增一個更快的驗證手段。

### 兩種模式的定位

| | 完整真實測試 | 局部真實測試 |
|---|---|---|
| 進入點 | `python main.py` | `python partial_verify.py` |
| 涵蓋範圍 | ①～⑦ 全部 Agent，`postman/collection_readonly.json`／`collection_mutation.json` 全量 endpoint | 只有 ⑥（Harness 驗證），跳過 ①～⑤／⑦；只驗證從完整 collection 動態挑出的子集（`--list`／`--items`／`--batch`／`--collection`，見下方「怎麼挑案例」） |
| 驗證的是什麼 | 整條 pipeline（含這次 ⑤／⑦ 的生成／修正品質）是否正確 | `python_project_path` 底下**目前已經存在**的程式碼，對「挑選出來的這幾個 case」是否正確——不驗證程式碼是怎麼來的 |
| 耗時 | 約 2.5 小時（真實量測） | 約 1～2 分鐘（容器啟動 + 少量 golden 比對） |
| 能不能驗證 Orchestrator 自己的修正（如 #52／#54 這類排程器／`test_nodes.py` 層級的修正） | 能——這些修正只在**重新生成／重新跑一輪 debug 迴圈**時才會被真正執行到 | **不能**——這類修正只有在重新跑 ①～⑤／⑦ 時才會被觸發，局部真實測試完全跳過這幾步，程式碼是舊的就是舊的 |
| 能不能驗證直接手動修正目標專案檔案（如 #53／#55 這類） | 能，但要等整條 pipeline 跑完 | 能，而且快——這正是它存在的目的 |

**結論：局部真實測試只能用來快速確認「這幾個已經手動修正／已經生成的檔案，這次是不是真的對了」，不能取代完整真實測試對整條 pipeline（尤其是 ⑤／⑦ 生成品質、排程器行為）的驗證。** 兩者互補：局部真實測試用來快速迭代「改檔案 → 驗證 → 改檔案」這個小循環，完整真實測試才是最終確認整條 pipeline 沒問題的權威手段。

### 局部真實測試的運作方式

`partial_verify.py`（repo 根目錄，跟 `main.py`同一層）：
1. 呼叫 `python_service.manager.ensure_started()` 啟動 Python 服務容器——跟完整真實測試共用同一套容器基礎設施（`python_service/process.py`），差別是這裡**不傳 `java_project_path`／`config_env_vars`**，容器啟動時不會有 ③ 產出的 `@Value` 注入資料（見 09b_bug_trace.md #46）。**如果挑選的 case 依賴某個 `@Value` 設定值，這裡驗證不到，這種 case 不適合放進局部測試的 partial collection**（真實案例：`school_router.py::language()` 依賴 `LANGUAGE_CODE`／`LANGUAGE_DISPLAY_NAME` 環境變數，第一次挑錯就踩到這個限制，後來換成不依賴 `@Value` 的 `locals()`）
2. `DbEnvironment.apply_seed()` 清空並重新灌入測試資料——跟完整真實測試同一份 `fixtures/seed.sql`
3. `GoldenVerifier.verify_raw()` 對挑選出來的子集跑（不含 mutation，這是刻意簡化，讓「先確認服務起得來、簡單案例過不過」這件事盡量快，不驗證需要真正寫入資料的 mutation case）——子集怎麼挑，見下方「怎麼挑案例」
4. `HarnessReporter().build_report()` 輸出跟完整真實測試同一種格式的 summary／failures，印在終端機（不落地成檔案，這點跟完整真實測試的 `logs/report_{run_id}.json` 不同——局部真實測試是互動式快速檢查，不是要留存的正式紀錄）
5. `finally` 區塊確保容器一定會關閉，跟 `main.py` 既有的清理邏輯同構

### 怎麼挑案例：`--list`／`--items`／`--batch`

`postman/collection_readonly_full_backup.json` 是 `collection_readonly.json` 的完整備份（2026-08-28 建立，動 partial collection 之前的安全副本）——`collection_readonly.json`／`collection_mutation.json` 本身維持原樣，完整真實測試不受影響，`partial_verify.py` 的所有選案例模式都是從這份完整 collection 動態挑選，不需要另外維護一份固定的 partial 檔案。

```bash
python partial_verify.py --list          # 列出完整 collection 全部 case 的編號與名稱，不執行驗證
python partial_verify.py --items 1,2     # 用編號挑子集，這次測 1、2
python partial_verify.py --items locals,language   # 也可以用名稱，逗號分隔、可跟編號混用
python partial_verify.py --batch 1/5     # 依原始順序切成 5 等分，跑第 1 份；測完抓到 bug 就去修，
                                          # 修完再跑 --batch 2/5，依序把 TOTAL 份都跑過一輪
python partial_verify.py --collection postman/my_custom.json   # 直接指定一份現成的 collection 檔案
python partial_verify.py                 # 不帶任何旗標，退回 postman/collection_readonly_partial.json
                                          # （目前內容是 locals／test Exception 兩個 case，當作預設快速檢查用）
```

`--items`／`--batch` 底層都是讀 `postman/collection_readonly.json`、用 `_flatten_items()` 攤平資料夾巢狀結構後依編號/名稱/切分挑出子集，寫成一份暫存 collection 檔案（`postman/partial_verify_*.json`）跑完即刪除，不會留下垃圾檔案，也不會動到 `collection_readonly.json` 本身。

挑選案例時要注意：

- **case 必須在 `fixtures/golden/{module}/` 已經有對應的 golden 檔案，否則會被判定 `golden_not_found` 失敗**（`GoldenVerifier` 靠 `case_id` 比對 golden）——唯一的例外是 Recorder 錄製當下就因為回應不是 JSON 而主動跳過、記進 `_skipped_case_ids` 的 case（見 02a 既有的 `excluded_cases` 機制），這種會被靜默排除、不計入 `total`，不是驗證工具的 bug（真實案例：`get Version` 這個 case 屬於這種情況）
- **避免依賴 `@Value` 注入的 case**（見上方限制）——挑 DB 查詢或純邏輯運算的 case，不要挑讀 `os.getenv()` 的（真實案例：`school_router.py::language()` 依賴 `LANGUAGE_CODE`／`LANGUAGE_DISPLAY_NAME` 環境變數，第一次挑錯就踩到這個限制，後來換成不依賴 `@Value` 的 `locals()`）
- `make_case_id()` 的規則是 `{item_name}_{method}_{path}`，只依賴 collection item 本身的欄位，挑出來的子集不需要保留原本的資料夾巢狀結構（`GoldenVerifier`／newman 都是攤平處理 item，不看 folder 階層）

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*