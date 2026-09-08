# 重構新方案：分層分階段翻譯（Phase 1 基礎零件 / Phase 2 業務邏輯）

> 本文件承接 `refactor_call_chain_implement_prompt.md`（上一輪 session 的交接文件，核心結論：⑤ 不該吃 `[P] plan_agent` 產生的 LLM 語意摘要，該吃「呼叫鏈 + 完整 Java 原始碼」）。這份文件記錄後續定案的**分層分階段**設計、Agent 職責分工、雙後端分工，以及支撐這些決定的實測依據。**仍是討論階段的工作文件，尚未動任何既有程式碼**——`06a`／`07a`／`08a`／`09a`／`01_langgraph_architecture.md` 尚未依此更新，見六章待決定事項。

---

## 一、核心設計：分層分階段 + 全域關卡

把要翻譯的 Java 程式碼分兩個 **phase**（Phase 1／Phase 2，逐一對應 `InterfaceSpec.phase`／`translator_backend` 分派），但 Phase 2 內部**再疊加一層全域關卡**，把 service 與 controller/router 拆成先後兩批釋放——三批合起來才是實際的排程順序，全部照 Java 原始碼直接翻譯，全程不經過 LLM 生成的語意摘要：

```
Phase 1：基礎零件（entity / dto / repository / utils，全專案、跨所有 module）
  → 全部照 Java 原始碼直接翻，無摘要層
  → 全專案的 Phase 1 全部翻完、語法驗證過關，才釋放任何 module 的 Phase 2
       ↓  ← 全域硬性關卡一（barrier），不是逐 module 各自判斷
Phase 2 之一：service（全專案、跨所有 module）
  → 全部照 Java 原始碼直接翻，無摘要層
  → 呼叫鏈遇到 Phase 1 的 callee → 直接讀「已翻好的真實 Python 原始碼」（function-level 抽取，不是整檔案），
    不繼續往下展開（保證存在：Phase 1 全域已做完，不需要查任何 runtime 完成狀態）
  → 呼叫鏈遇到同層呼叫（service 呼叫 service）→ 直接展開 Java 原始碼，遞迴下去（見下方「呼叫鏈規則」）
  → 全專案的 service 全部翻完、語法驗證過關，才釋放任何 module 的 controller/router
       ↓  ← 全域硬性關卡二（barrier），同樣是全域而非逐 module
Phase 2 之二：controller / router（全專案、跨所有 module）
  → 全部照 Java 原始碼直接翻，無摘要層
  → 呼叫鏈遇到 service（此時全域已保證翻完）→ 直接讀真實 Python 原始碼，不繼續往下展開
  → controller 互相呼叫 controller 極少見（Spring MVC 慣例上 controller 是入口，不是被其他 controller 呼叫的對象），
    理論上不太會觸發「同層遞迴」這條路徑，但機制上仍比照 service 同層規則處理，不假設它絕對不會發生
```

**這不是發明新的中間表示法**：後面的關卡讀前面關卡的產物時，讀到的是**真實存在的 `.py` 檔案內容**（翻譯完成、寫進 `python_project_path` 的實體檔案），不是任何 Agent 生成的描述或摘要。呼叫鏈「停」只代表「不再往下展開一層 Java 原始碼」，不引入一層可能失真的抽象。

### 為什麼是三層全域關卡，不是逐 module、也不是用呼叫圖排序

不管哪個 module，全部 module 的前一關都做完、通過語法驗證，才有任何 module 開始下一關。選全域而非逐 module，是因為規則簡單——不用處理「B module 的某一關還沒做完、A module 的下一關卻呼叫到它」這種跨 module 邊界情況；代價是犧牲一些排程彈性，但 qwen 那條路徑（repository）本來就序列化，Phase 1 的總耗時瓶頸主要在那裡，全域關卡不會讓損失顯著變大。

**把 Phase 2 再拆成 service／controller 兩關，而不是用呼叫圖做真實依賴排程（如偵測 service 之間的呼叫關係、算拓樸順序）**：曾經評估過用呼叫圖分析＋環偵測做更精細的排程（讓更多同層呼叫也能命中「讀已翻譯 Python」），但那套機制的複雜度（SCC／環偵測、跨 module 依賴閉包比對）換來的好處是邊際的——多拆一層全域關卡就已經把最大宗的「controller 呼叫 service 呼叫 repository」這種常見的深呼叫鏈整個消掉，剩下「service 呼叫 service」這個範圍小很多的殘餘情況維持原本最簡單的規則（讀 Java、遞迴、上限保底）就夠了；而環偵測寫錯的代價（排程器真正死結）遠比「這裡多讀了幾層 Java 原始碼、或極端情況被截斷」嚴重，複雜度不值得換這個邊際收益。

這個定案同時解決了「同層呼叫該不該查排程完成狀態」的問題——答案是不需要：三層關卡各自內部的同層呼叫一律直接展開 Java 原始碼，不依賴任何排程狀態，「讀已翻譯 Python」這個捷徑只在關卡與關卡之間的邊界生效，而這些邊界剛好都是全域關卡保證的。

### 呼叫鏈規則

| 情境 | 規則 |
|---|---|
| 呼叫「更基礎一關」已完成的 callee（如 controller 呼叫 service、service 呼叫 repo/utils） | 讀真實 Python 原始碼（function-level 抽取，只抽被呼叫到的那個函式，不是整份檔案——避免整檔案塞爆 context，`09b_bug_trace.md #37` 已證實這個問題會發生），不繼續往下展開 |
| 同一關內部、跨 module 的同層呼叫（如 module X 的 service 呼叫 module Y 的 service，且 X 依賴 Y） | 讀真實 Python 原始碼，不繼續往下展開——既有的 module 依賴排程（`module_list.depends_on`，`ModuleScheduler` 既有機制）保證 Y 先完成，這條規則不是新增的，只是現在要延伸到每一層內部都生效，不是只在「整個 module 完成」才檢查，見 `06a_plan_agent_architecture.md` 六章「與既有 module 依賴排程的關係」 |
| 同一關內部、**同 module** 的同層呼叫（service 呼叫 service，或理論上 controller 呼叫 controller） | 直接展開 Java 原始碼，遞迴下去，設一個總量上限防止 context 爆量（見 `06a_plan_agent_architecture.md` 六章），超過上限時截斷並在 `TaskSpec` 上標記可見（不是靜默丟棄）——這是真正沒有任何機制能保證順序的唯一殘餘情況 |
| Phase 1 內部呼叫（如 `CollectionUtil` 依賴 `ValidationUtil`） | 同樣直接展開 Java 原始碼，不套用「讀已翻譯 Python」這個捷徑——Phase 1 內部呼叫鏈通常很淺，用捷徑換不到明顯的 context 縮減 |

**同層參照的用途是「讓⑤知道怎麼正確呼叫」，不是要⑤把邏輯合併進來**：⑤翻譯出的 Python 程式碼呼叫到已完成的 callee 或同層 callee 時，都應該是真正呼叫那個函式（import 後呼叫），比照 Java 原始碼本來就是這樣呼叫的——不是把被參照函式的邏輯抄一份貼進來。

### 新增能力與歸屬

- **抽取 Java 方法完整原始碼文字**：給定 `class_name`／`file_path`／`method_name`，用 javalang 找到對應 `MethodDeclaration` 節點取原始碼片段。①③現有職責只做簽名／stereotype／欄位掃描，沒有人抽過完整函式本體文字，這是新能力，歸屬見二章職責分工表。
- **一層呼叫圖查找 → 機械界定每個 task 的參照範圍**：[P] 重用①既有的 `parse_agent.call_graph.parse_java_project(java_project_path) -> ParsedProject`（取 `.call_graph`），不是內部函式 `_build_call_graph()`，不需要新建、不改①的輸出契約——這是純函式，當場對 `java_project_path` 重新算即可，不需要①把呼叫圖當成新的 State 欄位往下傳。細節與既知取捨（多載方法共用 `method_id`）見 `04a_parse_agent_architecture.md` 三章「下游影響」。**「界定範圍」歸 [P]、「實際讀取原始碼」歸⑤**：[P] 用呼叫圖＋③新增的 `java_index` 算出每個 task 該參照哪些 Java 方法（只有座標，見 `06a_plan_agent_architecture.md` 六章 `reference_targets`），⑤ 才把座標實際讀成原始碼文字組進 prompt——這件事不落在 [P] 身上，維持 [P] 不讀 Java 原始碼本體的既有邊界。
- **`InterfaceSpec.phase` 欄位**：標記一個 interface 屬於 Phase 1 還是 Phase 2，供判斷「呼叫鏈遇到這個 callee 該讀 Java 還是讀已翻譯 Python」。Phase 2 內部 service／controller 這一層關卡不新增 `phase` 的第三個值，改用既有的 `layer`（`services`／`routers`，[P] 四章已經算好）判斷，見 `06a_plan_agent_architecture.md` 六章——這個選擇刻意避免動到已經落地、驗證過的 `phase: Literal[1, 2]` 欄位與雙後端分派邏輯。

### Graph 層級的影響

⑤ 這個 node 要拆成**三個**被全域硬性關卡隔開的階段：全部 module 的 Phase 1 task 都完成（且通過語法驗證），才釋放任何 module 的 service task；全部 module的 service task 都完成，才釋放任何 module 的 controller/router task。`graph/scheduler.py::ModuleScheduler` 需要在現有的「跨 module 依賴排程」之上，多一層「全專案 Phase 1 先於 service 先於 controller/router」的全域關卡（原本規劃的「同 module 內依層級排程 repo < service < router」這條同 module 內部排序規則，被這個全域三層關卡取代，不需要同時維護兩套機制）。

---

## 二、Agent 職責分工

| Agent | 現有職責 | 新設計下的角色 |
|---|---|---|
| ① 解析 Agent | 輸出 `module_list` | **不變**。呼叫圖分析程式碼（`parse_agent/call_graph.py`）被 ⑤ 重用（import），①的輸出契約不變 |
| ③ 架構設計 Agent | 輸出 `python_structure.interfaces` | **加一個欄位**：`InterfaceSpec.phase`，依 Java class stereotype 機械判斷。entity/dto 的既有機械產生機制（`@Entity` 等直接跳過、不渲染，見下方「Phase 1 現況表」）不變 |
| ④ 骨架實作 Agent | 機械產生骨架 | **不變** |
| `[P]` Plan Agent | 舊職責：LLM 生成 `description`／`context`／`referenced_interfaces` 語意摘要 | **舊職責整個拿掉**（這正是失真來源）。保留：涵蓋率驗證、`depends_on` 依賴排序、target_files 組裝。**新增**：複製③算好的 `phase`；依「這個 task 屬於哪一層」機械決定 `translator_backend`（repository→qwen，其餘→claude，見三章）。是否還需要 LLM（Phase 1 的 task 拆分本身要不要語意判斷），見六章待決定 |
| ⑤ 功能改寫 Agent（`implement_node.py` + `translator_cli`） | 舊職責：拿 `[P]` 的摘要呼叫 qwen 填空 | **扛下新設計核心**：抽取 Java 方法完整原始碼、一層呼叫圖查找、依 `phase` 決定讀 Java 或 Python、依 `translator_backend` 分派 qwen／Claude API；`graph/scheduler.py` 加全域 Phase 1／Phase 2 關卡 |
| ⑥ 測試執行 Agent | Harness 驗證 | **不變** |
| ⑦ Debug Agent | 直接寫修正 | **不變** |

⑤ 本來就是 `01_langgraph_architecture.md` 明講「非薄封裝、複雜邏輯直接寫在這裡」的例外節點，新設計只是把它原本「呼叫 qwen 填空」的單一職責，擴充成「組 context（呼叫鏈＋原始碼抽取）+ 分派雙後端」；`[P]` 從「產生內容」降級成「打標籤」；③ 多算一個順手的欄位。

### Phase 1 各類型現況與待辦

| 類型 | 現況 | 這次要做的事 |
|---|---|---|
| **Entity** | ③ 遇到 `@Entity`／`@Embeddable`／`@MappedSuperclass` 直接跳過不渲染，欄位層級規格由④直接從既有 Postgres 測試 DB 或 Java 原始碼機械取得，完全不呼叫 LLM（見 [05a_design_agent_architecture.md](05a_design_agent_architecture.md) 三章） | 維持現狀。已經是「呼叫鏈的天然終點」 |
| **DTO** | ③ 從 `openapi_spec` 機械渲染出完整 Pydantic `BaseModel`，④ scaffold 直接寫出完整檔案，不呼叫 LLM（05a 三章「Schema 定義段」） | 維持現狀 |
| **Repository** | 目前跟 service/controller 走同一條 pipeline：① 分類 → ③ 產生介面 → `[P]` LLM 摘要成 task → ⑤ qwen 翻譯本體 | 拆成兩種子情況（見下方） |
| **Utils** | 沒有 Spring stereotype，落在 05a 六章「無 stereotype 的類別」分支，由 LLM 在③階段判斷歸屬（多半併入 services），沒有獨立分類 | 新增機械分類規則（見下方，已定案） |

**Repository 的兩種子情況**：
1. **Spring Data JPA 衍生查詢介面**（如 `findByKindAndRandomId(...)`）——Java 端沒有方法本體，邏輯編碼在方法名稱命名慣例裡。實測（見五章）證實 qwen 對這類方法（含 native SQL `@Query`）翻譯全部正確，是否還要另外做機械規則解析已不是必要項目，見六章。
2. **有真實邏輯的 repository 實作類別**——照原始碼翻。

這個區分（介面方法有沒有 body）在①解析或③重新掃描時可以機械判斷，不需要 LLM。

**Utils 機械分類規則——已定案**：檢查 Java 檔案的 **package 路徑**是不是 `xxx.utils`（實測用到的 `ValidationUtil`／`CollectionUtil`／`CodeUtil`／`ConvertUtil`／`ExamCardUtils` 全部落在 `com.teachLanguage.utils` 下，幾乎所有 Java 專案都遵守這個慣例）。比「無 stereotype + 全靜態方法 + 不依賴注入欄位」這種行為推斷簡單、可靠得多，純機械字串比對，不需要 LLM。

**Utils 檔案結構——已定案：不套用 05a 既有的 `{module}_{layer}.py` 規則，直接複製 Java package 結構**。05a 三章的檔名規則（router／service／repository／schema／model 都是「一個 module 一個檔案」）建立在「這一層依附在某個業務 module 底下」的前提上，但 utils 通常橫跨多個 module（如 `ValidationUtil` 同時被 `ExamController`／`GradingController` 等好幾個 module 使用），套用 `{module}_utils.py` 會出現「這個 util 該算哪個 module」的假問題。正確做法是一個 Java class 對一個 Python 檔案，直接照 package 結構放：`com.teachLanguage.utils.ValidationUtil` → `app/utils/validation_util.py`，不經過 module 分組——這也是這幾輪實測（`validation_util.py`／`collection_util.py`／`code_util.py`／`convert_util.py`／`exam_card_utils.py`）已經在用的做法。

**`InterfaceSpec.phase` 計算規則——已定案**：機械對應，不需要 LLM：
- `@Repository`（或衍生查詢介面）→ Phase 1
- package 為 `xxx.utils` → Phase 1（同上）
- entity／dto → Phase 1（既有機制已處理，不需要新規則）
- `@Service`／`@RestController` → Phase 2
- 極少數完全沒有 stereotype、不在 utils package、卻有實際方法的邊界情況 → 沿用 05a 既有的「無 stereotype 類別」LLM 判斷 fallback，不需要新建機制

---

## 三、雙後端分工（qwen／Claude API）——已定案

**只有 repository 層交給 qwen，其餘（utils／model／entity／service／controller）一律交給 Claude API。**

| 層級 | 測試次數 | 結果 | 決定 |
|---|---|---|---|
| Repository | 8 個方法（JPQL／native SQL `@Query`、`List`／`Optional`／裸可空三種回傳形狀、2～4 欄位 `And` 組合） | **8/8 PASS，零缺陷** | qwen |
| Model／Entity | 4 次獨立翻譯（`AnswerEntity`／`ExamEntity`／`BackUserEntity`／`ExamkindEntity`） | **4/4 都出現 fatal import／reference 錯誤**，每次錯誤類型都不同 | Claude API |
| Utils | 5 個 class、約 10 個方法 | 多數正確（含高難度自製雜湊演算法），但 3 個非 fatal、只在邊界輸入才現形的邏輯 bug | Claude API（風險不對稱：錯得隱蔽，省下的成本換不回這個風險） |
| Controller／Service | 未測（性質與 model/entity 相近，一併歸入） | — | Claude API |

完整實測過程與逐案例證據見五章。

**落地方式**：`TaskSpec` 新增欄位 `translator_backend: Literal["qwen", "claude"]`，由 task 產生階段依「這個 task 屬於哪一層」機械決定，不需要 LLM 判斷。`translator_cli` 填空模式現在打 `OLLAMA_BASE_URL`；Claude API 這條路直接複用既有的 `common/llm_client.py`，不需要重新實作 client 初始化。

---

## 四、驗證閘門——已定案：先上輕量閘門

現有 Harness（02a）完全是 HTTP／Postman 導向，repository／utils 沒有獨立於 API 之外的 golden 可比對。

- **輕量閘門（採用）**：Phase 1 task 完成只做語法驗證（AST parse），不做功能驗證；功能對不對，等 Phase 2 對應 module 的 controller/service 也翻完、觸發既有的 module 級 API 驗證時一併驗到。不需要新建任何 Harness 機制。
- **重量閘門（先不做，視 bug 率評估）**：repository／utils 專屬 unit-level 測試，能更早定位問題，但需要額外設計「預期值從哪來」。

**已知的反證資料點**：五章 8.3 發現「值層級」邏輯 bug（型別轉換沒判斷 `None`、正規表示式跳脫規則錯誤）語法驗證完全接不住，只有真的執行、帶邊界值輸入的測試才驗得出來。是否因此提早評估重量閘門，見六章。

---

## 五、實測依據

第一個驗證案例選 `ExamController.searchAnswer`——不經過 Service 層（直接呼叫 `answerRepository`），讓測試聚焦在「Phase 1 產物能否被 Phase 2 直接正確呼叫」，同時涵蓋 Phase 1 全部四種類型（utils／衍生查詢 repository／entity／dto）。之後陸續擴大到 `CollectionUtil`／`ExamCardUtils`／`ConvertUtil`／`CodeUtil`（utils）、`ExamEntity`／`BackUserEntity`／`ExamkindEntity`（model）、`CandidateController`／`GradingController` 牽涉到的 repository（8 個衍生查詢／`@Query` 方法，涵蓋三種回傳形狀）。

驗證方式：`FastAPI TestClient` 直接連測試 DB（`MOC_MATSUEXAM_TEST`），對比既有 golden 或 `psql`／編譯執行 Java 原始碼算出的 ground truth（`javaref/GroundTruth.java`），不是憑印象判斷。

### 5.1 `searchAnswer` 端到端驗證

| 版本 | 案例 | 結果 |
|---|---|---|
| Claude（一般 prompt） | 空值驗證錯誤路徑 + happy path（30 筆） | **兩者皆 PASS**，一次翻對 utils／衍生查詢 repository／entity／dto／controller 全部五個部分，含容易出錯的細節（錯誤路徑 `data` 必須 `null`、未設定欄位維持 `None`、camelCase↔snake_case 對應） |
| qwen（一般 prompt） | 同上 | 原始輸出因 1 個 import typo（`sqlalcheampy`）無法執行；修掉後空值驗證 PASS，happy path 因 `answer.__dict__` 帶出 SQLAlchemy 內部狀態導致序列化崩潰；另外查出：`result` 欄位被塞入 Java 從未賦值的幻覺值、必填欄位導致請求綁定行為與 Java 不一致、未重用既有 `Base`、`created`/`updated` 型別與 default 錯誤、命名未轉 snake_case、repository 用了 class-based 設計 |
| qwen（加 4 條規則：禁止捏造值／巢狀物件用 Pydantic model／重用既有 Base／snake_case 命名） | 同上 | **兩案例完全沒手動修正，直接 PASS**，上一版問題全部消失；但抓到新殘留 bug：`created=str(answer.created)` 在值為 `None` 時產生字面字串 `"None"` 而非 JSON `null`（不在 4 條規則涵蓋範圍內，需要帶 `NULL` 資料的測試才驗得出來） |

**結論**：prompt 工程能解決「具名、明確」的問題，但無法涵蓋所有情況——真正的兜底是測試本身，不是把 prompt 寫得更完整；這也是四章「輕量閘門接不住值層級 bug」的第一手證據。

### 5.2 Repository（qwen）——累計 8/8 PASS

| 方法 | 回傳形狀 | 結果 |
|---|---|---|
| `AnswerRepository.countDistinctRandomIdByKind`（JPQL `@Query` 聚合） | `long` | PASS，與 `psql` 查證一致 |
| `AnswerRepository.findLatestTypeWithOrderByRandomId`（native SQL `@Query`，subquery+join） | `List` | PASS，含容易忽略的字串排序語意（varchar 欄位排序不是數字排序），30 筆順序逐一相符 |
| `BackUserRepository.findByUserNameAndPassword` | 裸型別（可能 `null`） | PASS，正確用 `.first()`，沒有誤用會拋例外的 `.one()` |
| `ExamkindRepository.findByKind` | `List` | PASS，77 筆與 `psql` 一致 |
| `ExamkindRepository.findByTypeNumberAndPartNumberAndQuestionNumberAndKind`（4 欄位 `And`） | `Optional` | PASS |
| `ExamRepository.findByKind` / `findByRandomIdAndKind`（2 欄位 `And`） | `List` / `Optional` | PASS |
| `AnswerRepository.findAllByTypeNumberAndPartNumberAndQuestionNumber`（3 欄位 `And`，在既有檔案上擴充） | `List` | PASS，且正確保留既有 3 個方法，沒有規模退化 |

**結論**：原本最擔心的困難案例（native SQL、複雜 `@Query`）反而零缺陷，「repository 翻譯高機率準確」有足夠案例支撐三章的定案。

### 5.3 Model／Entity（qwen）——累計 4/4 出現 fatal 錯誤

| 案例 | 結果 |
|---|---|
| `AnswerEntity` | `from sqlalcheampy.orm import mapped_column`——拼字錯誤，模組不存在 |
| `ExamEntity`（型別覆蓋更廣：`LocalDate`／`Boolean`） | `from datetime import LocalDateTime`——把 Java 型別名稱直接當 Python 名稱用，Python `datetime` 模組沒有這個東西。修掉後功能正確，但違反明確給的規則：沒有重用已提供的 `Base`，且自行捏造 DB schema 未提供的 varchar 長度限制 |
| `BackUserEntity`／`ExamkindEntity`（第一次，未提供 `BaseEntity.java`） | 完全省略繼承來的 `id`／`created`／`updated`，SQLAlchemy 連 mapping 都做不起來（此次未提供父類別原始碼是測試方法論疏漏，非 qwen 問題） |
| `BackUserEntity`／`ExamkindEntity`（補測，提供 `BaseEntity.java`） | `id`/`created`/`updated` 正確加回，但冒出兩個新 fatal 問題：`from pydantic.v2 import BaseModel`（路徑不存在的幻覺 import）；`from app.core.database import Base` 後緊接著下一行又寫 `Base = declarative_base()`（先做對、再自己蓋掉） |

**結論**：4 次嘗試、4 次不同類型的 fatal 錯誤，且第二次仍在「同一份請求裡對不同檔案的規則遵守不一致」（`answer_repository.py` 正確重用 `Base`、`exam.py` 卻沒有）——不是規則沒講到，是規則的**執行可靠度**問題，比「還有沒設想到的新問題」更嚴重。model/entity 層 fatal 錯誤率 4/4，repository 層 0/8，支撐三章的定案。

### 5.4 Utils（qwen）——5 個 class，多數正確但有隱蔽 bug

| Class／方法 | 結果 |
|---|---|
| `ValidationUtil`（2 方法） | PASS |
| `CodeUtil.halfWidth2FullWidth`（字元碼位移） | PASS，含 `null`／空字串邊界，逐 Unicode code point 比對相符 |
| `ConvertUtil.convertCy`／`convertCy3`（西元⇄民國年） | 邏輯 PASS，但自行加了 Java 沒有的 `if fy is None: return None` 防禦判斷——Java 版傳 `null` 會直接丟例外，Python 版靜默回傳 `None`，行為不一致 |
| `CollectionUtil.findDistinctField`（依賴另一個已翻譯的 `ValidationUtil`） | 用 `set()` 做去重，不保留原始出現順序，但 Java `Stream.distinct()` 會保留（已用構造案例驗證）。正確重用了已翻譯的 `is_valid_field`，沒有重新實作——驗證了一章「呼叫鏈停在已翻譯層」設計對 qwen 有效 |
| `ExamCardUtils`（含自製 Java `hashCode()` 等價邏輯，最高難度案例） | 原始輸出 `chr('A' + int)`——Python 不能字串加整數（`TypeError`），出現兩次。修掉後：自製的 32-bit 有號整數溢位雜湊邏輯完全正確（逐字元對上 Java 實際執行結果，含業務規則邊界案例）；但另一個沒被規則涵蓋的新 bug：`re.sub(r"\\D", "", year)` 雙反斜線，regex 語意變成「比對字面反斜線+D」而非「比對非數字」——乾淨輸入（`"2025"`）測不出來，用 Java 註解自己舉的例子（`"2025年"`）重測才證實 |

**結論**：utils 層跟 model 層的失敗模式性質不同——model 層是「幻覺出根本不存在的東西」，utils 層是「用了語意跟 Java 不完全等價的 Python 慣用寫法」（`char+int`、regex 跳脫規則、`set()` 不保序），且複雜度更高的雜湊演算法反而翻對了。這類 bug 有「乾淨輸入測不出來、需要邊界值輸入才會現形」的特性，語法驗證完全接不住，支撐三章「utils 交給 Claude API」與四章「測試才是真正兜底機制」的判斷。

---

## 六、待決定事項

- [x] Utils 機械分類規則、`InterfaceSpec.phase` 計算規則——已定案，見二章（package 路徑判斷 utils，stereotype／package 機械對應 phase，極少數邊界情況沿用既有 LLM fallback）
- [x] `[P] Plan Agent` 對 Phase 1 的 task 產生邏輯，是否完全不需要 LLM（純機械）——**已定案：不需要**，`06a_plan_agent_architecture.md` 已完成同步更新，`phase`／`translator_backend` 都是機械查表，[P] 整個不再呼叫 Claude API，見 06a 五章
- [x] **⑤ 如何把「Java callee」對回③的 `InterfaceSpec`——已定案**：③新增輸出 `InterfaceSpec.java_method_id`／`python_structure.java_index`（Java method_id → Python 對應資訊，見 `05a_design_agent_architecture.md` 對應章節），把③內部本來就算過、跑完即丟的對應關係正式落地保留，不需要⑤重新實作一次camelCase／多載消歧邏輯。**「範圍界定」與「實際讀取」拆成兩個職責**：[P] 用①的呼叫圖 + ③的 `java_index` 做機械圖走訪（BFS，含大小上限），算出每個 task 的 `reference_targets`（只有座標，見 `06a_plan_agent_architecture.md` 六章）；⑤（09a，待落地）才把座標實際讀成原始碼文字。這也連帶回答了「切多大」的問題——上限設在 [P] 這一層統一控制（`PLAN_AGENT_MAX_REFERENCE_TARGETS`），不是⑤逐 task 各自決定
- [ ] Repository 衍生查詢介面原本設想的「規則化翻譯」是否還有必要——5.2 顯示 qwen 對衍生查詢（含 native SQL）全部翻對，整個 repository 層已定案交給 qwen 且可靠，是否還要另外維護一套機械規則解析器，收益（省一點 API 成本）是否值得多一條路徑，待評估
- [ ] 輕量閘門若之後發現 bug 率偏高，要不要升級成重量閘門（見四章）
- [ ] 09a 新增的「抽取 Java 方法完整原始碼文字」工具函式，實作細節（javalang 定位 `MethodDeclaration` 節點、取原始碼片段的具體做法）待落地時設計，目前只有能力歸屬與做法方向
- [ ] `07a_translator_cli_architecture.md`／`08a_scaffold_agent_architecture.md`／`09a_implement_agent_architecture.md`／`01_langgraph_architecture.md`（`graph/state.py::TaskSpec`、`graph/scheduler.py::ModuleScheduler` 全域 Phase 關卡）需要依本文件同步更新——`06a`／`06b_plan_agent_code.md` 已完成，`plan_agent/` 真實程式碼已依新設計重寫（不再呼叫 Claude API），對真實 `lang-exam-api-refactor` 專案跑過完整 ①→③→[P]（78 個 InterfaceSpec → 78 個 task，涵蓋率／`translator_backend`／`_utils`／`_global` 分佈皆正確）；`04a_parse_agent_architecture.md` 校對過，內容本身不需要改，只在三章補了一段「下游影響」小節記錄⑤重用 `parse_java_project()` 這件事

---

*本文件是討論階段的工作文件，不是定案的架構文件——待六章待決定事項逐一收斂、`06a`／`07a`／`08a`／`09a`／`01` 同步更新後，再視需要調整本文件的定位（併入正式文件或保留作為設計紀錄）。*
