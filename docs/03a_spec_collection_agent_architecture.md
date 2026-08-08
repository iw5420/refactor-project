# [A] Spec Agent / [B] Collection Agent 詳細設計

> 本文件承接 `00_refactor_architecture.md` 的整體架構（見七、[A] Spec Agent 與 [B] Collection Agent 兩節），是這兩個 Agent 的**設計面**文件：決策、契約、資料結構、流程。實際程式碼實作分兩份文件：[A] 見 `03b_spec_agent_code.md`，[B] 見 `03c_collection_agent_code.md`；本文件不出現可執行的實作邏輯。

---

## 一、本文件範圍與定位

[A]/[B] 兩個 Agent 合併寫在同一份文件，是因為兩者是**同一條資料管線的相鄰兩段**：[A] 產出 `openapi.json`，[B] 直接消費它產出兩份 Postman Collection，中間沒有其他 Agent 介入，拆成兩份文件反而會讓「baseUrl 變數怎麼從 A 傳到 B」這類銜接細節被切成兩半描述。

**本文件涵蓋**：
- [A] 啟動 Java 服務、判定就緒、擷取 `/v3/api-docs` 的流程與決策
- [B] 把 `openapi.json` 轉成兩份 Postman Collection、readonly/mutation 分類規則
- [B] 的 Mutation 頂層 folder 分組規則（沿用 02a 六章的鎖死約定）
- [B] 的人工填值機制（模板產生、`ManualFillEntry` 契約、`Decision.SKIP` 語意）
- [B] 的鏈式依賴偵測與 Postman Environment Variable 注入機制的設計

**本文件不涵蓋**：
- 上述各項的實際程式碼——[A] 見 03b、[B] 見 03c
- Postman Collection 的**執行**（newman 呼叫、測試 DB truncate/reseed、golden output 記錄與比對）——是 Agent ②/⑥ 的職責，見 02a

> Mutation folder 分組與鏈式依賴機制的設計原理見 `02a_harness_architecture.md` 六章，本文件三章沿用、不重新定義。

---

## 二、[A] Spec Agent：啟動 Java 服務並擷取 OpenAPI Spec

[A] 是純程式邏輯，不需要 LLM。整段流程是「啟動一個一次性的 Java 進程 → 判定就緒 → 擷取 spec → 存檔 → 關閉進程」，全程不打開任何 `.java` 檔案。

### 環境變數

| 變數 | 說明 | 現況 |
|---|---|---|
| `JAVA_BASE_URL` | Java 服務的 base URL，如 `http://localhost:8080` | 已在 01 的 `.env` |
| `JAVA_JAR_PATH` | 已打包 jar 的路徑（`mvn package` 後產出，見 00 五） | 本文件新增，已補進 `.env`（見下方「路徑格式」說明） |
| `JAVA_EXECUTABLE_PATH` | 啟動 jar 用的 `java` 執行檔路徑 | 本文件新增，已補進 `.env`（見下方「Java 執行檔路徑」說明） |
| `SPRING_DATASOURCE_URL` / `_USERNAME` / `_PASSWORD` | 覆蓋 Java 服務連到測試 DB（`MOC_MATSUEXAM_TEST`），見 00 五 | 已在 01 的 `.env` |

> **路徑格式**：`JAVA_JAR_PATH` 用**相對於 orchestrator 專案根目錄的相對路徑**（如 `../lang-exam-api-refactor/target/app.jar`，對應 02a 十章 Java 專案與 `refactor-project/` 同層擺放的建議），不寫死絕對路徑——絕對路徑換機器、換使用者就失效，`.env` 沒辦法跨機器共用。`JavaServiceProcess` 內部用 `pathlib.Path(__file__).resolve().parent / relative_path` 解析成絕對路徑再傳給 `subprocess`，不依賴當下 `cwd`。

> **Java 執行檔路徑**：`JAVA_EXECUTABLE_PATH` 預設值 `java`，交給系統 PATH 解析。但一台機器常同時裝有多個 JDK 版本，PATH 解析到的版本不保證跟 Java 專案要求的 `java.version`（見 00 五，目前為 11）相符——版本不符會直接拋 `UnsupportedClassVersionError`。這個值允許填絕對路徑（如 `D:\Ian Work\Java\jdk-11.0.15\bin\java.exe`）指向明確的 JDK 11 執行檔，`JavaServiceProcess` 直接把這個值當可執行檔路徑傳給 `subprocess.Popen`，不做額外解析。

### 服務生命週期管理

**決策**：啟動與關閉的邏輯不能各處各寫一份——00 已經確認 Agent ②（Harness 錄製端）也需要啟動一份自己的 Java 服務來跑測試，兩處若各自實作容易產生行為不一致（例如 timeout 秒數兜不起來）。因此設計一個獨立、無其他模組依賴的生命週期管理元件，供 [A] 與 Agent ② 共用（邊界見下）。

這個元件的職責（介面契約，實作見 03b）：

| 能力 | 說明 |
|---|---|
| 啟動 | 以 `JAVA_JAR_PATH` + 環境變數覆蓋啟動 `java -jar` 進程 |
| 背景抽乾輸出 | 啟動期間持續讀取進程 stdout/stderr、只保留最後約 200 行，避免 Spring Boot 開機日誌把 OS pipe buffer 塞滿導致進程卡死；不即時解析內容 |
| 關閉 | 送出正常終止信號，等待一段寬限時間；逾時未退出則強制關閉 |
| 診斷輸出 | 對外提供「最後 N 行進程輸出」，供啟動失敗時定位原因 |
| 生命週期綁定 | 支援「進入時啟動、離開時保證關閉」的用法，避免啟動失敗或例外中斷時遺留孤兒進程 |

> DB 不需要預先 truncate/reseed：Spring Boot 啟動時會嘗試建立資料庫連線池（因此測試 DB **必須存在且可連線**，schema 需已同步，見 00 五），但 `/v3/api-docs` 是 springdoc 對 controller/schema annotation 做反射掃描的結果，不觸及任何資料列，資料是否已 seed 不影響擷取結果。

### 就緒判定與擷取合一

**決策**：Java 服務沒有掛 `actuator/health`（00 五只要求加 `springdoc-openapi-ui`，沒有要求額外的 actuator 依賴），因此不另外設計健康檢查端點——直接輪詢 `/v3/api-docs` 本身：連線被拒或逾時代表還沒就緒，繼續重試；一旦拿到 200 且是合法 JSON，就緒判定與擷取結果同時到手，不必分「先探活、再擷取」兩階段。

流程（時間預算與間隔為預設值，實作可調）：

0. **啟動前先做一次 DB 連線快篩**：用 `SPRING_DATASOURCE_*` 對測試 DB 做一次輕量 TCP 連線測試（不需要完整握手驗證帳密，能連上 port 即可），連不上就直接判定失敗、不執行 `java -jar`——DB 連不上是 Java 服務啟動失敗最常見的原因（見 00 五「測試 DB 準備」），這個檢查能在幾秒內給出明確錯誤，不必等滿 90 秒逾時預算才知道
1. 設定一個啟動逾時預算（預設 90 秒）
2. 在預算內反覆輪詢：每次先檢查 Java 進程是否已經退出（例如 port 衝突導致進程立即死亡）——若已退出，不等滿逾時預算，直接判定啟動失敗；若進程仍存活，才呼叫 `GET {JAVA_BASE_URL}/v3/api-docs`（fail-fast：多一個存活判斷幾乎零成本，卻能把「port 衝突」這類必然失敗的案例從空等 90 秒縮短到幾秒內回報）
3. 回應 200 且內容為合法 JSON → 視為就緒，該回應本身即為擷取結果，流程結束
4. 連線被拒或逾時 → 服務尚未起來，屬正常現象，等待一段間隔（預設 1.5 秒）後重試
5. 回應非 200 但也非連線層級錯誤（例如 404）→ 記錄為候選錯誤原因，繼續重試直到逾時預算耗盡
6. 逾時預算耗盡仍未成功 → 判定啟動失敗，連同「最後一次候選錯誤」與服務的診斷輸出（見上）一併拋出，方便定位

> **Port 衝突不做自動處理**：不主動偵測並終止佔用 port 的進程——該 port 不保證只被本 pipeline 使用，貿然終止有誤殺風險。衝突時 Spring Boot 會自行啟動失敗，錯誤訊息見診斷輸出（下方錯誤情境表），交由人工排查。

### 錯誤情境與診斷

| 情境 | 判斷依據 | 說明 |
|---|---|---|
| DB 連不上（快篩階段） | 啟動前的 TCP 連線快篩失敗 | 直接失敗，不會進入 90 秒逾時流程；檢查 00 五的測試 DB 是否已建立、連線資訊是否正確 |
| 啟動逾時（90s 內沒 200） | 就緒判定流程逾時 | 快篩已排除 DB 連不上，常見原因改為 port 衝突或其他初始化問題，診斷輸出裡通常看得到具體 log |
| Port 已被佔用 | 進程立即退出，診斷輸出含位址佔用相關訊息 | 輪詢流程偵測到進程已退出即提前結束（見上方流程步驟 2），不必空等滿 90 秒逾時預算；診斷輸出帶出具體錯誤行；不自動清理（見上） |
| `/v3/api-docs` 回 404 | 輪詢過程狀態碼非 200，非連線錯誤 | 通常是 `springdoc-openapi-ui` 依賴版本沒加對（見 00 五），用錯 artifact 會讓進程直接掛掉，屬上一列情境 |
| DB 存在但 schema 未同步 | 快篩能連上 port，但進程啟動後特定初始化失敗 | 診斷輸出會有對應的資料庫初始化錯誤，指向 00 五「schema 來源」章節排查 |

> 這裡的失敗**不進入** `retry_count` 迴圈，[A] 失敗代表環境本身沒準備好，屬於人工介入排查的範疇，直接中止整條 pipeline——邊界的完整定義見六、錯誤處理範圍。

### 與 Agent ② 共用的邊界

[A] 用完即關閉 Java 服務，不會把進程狀態留在 LangGraph State 裡跨 node 傳遞——State 需要保持可序列化，長駐的進程控制代碼不適合放進去，且 LangGraph node 之間沒有「同一進程存活」的保證。

Agent ②（對 Java 服務跑 Postman、記錄 golden output）需要另外啟動一份**自己的** Java 服務實例來執行測試，會重用二、服務生命週期管理小節定義的同一套元件，確保兩處對「啟動、就緒判定、關閉」的行為完全一致。Agent ② 端實際怎麼呼叫、DB truncate/reseed 怎麼接在啟動之後，屬於 02a 的範圍，本文件只界定共用邊界。

→ 實作見：`spec_collection_agent/java_service.py`（`JavaServiceProcess`，`03b_spec_agent_code.md`）；Agent ② 端的重用方式見 `02a_harness_architecture.md`。

---

## 三、[B] Collection Agent：OpenAPI → Postman Collection

[B] 是「程式邏輯 + 少量 LLM」：轉檔、依 HTTP method 分類是純程式邏輯；以下兩件事需要語意判斷，交給 **Claude API**（不是本地模型 qwen2.5-coder，理由見 00 三章 LLM 分工），範圍刻意收窄（各自的輸入輸出契約見下）：

1. **Mutation 的頂層 folder 分組**：依業務情境把有依賴關係的 request 分進同一個頂層 folder（鎖死規則，沿用 02a 六章）
2. **鏈式依賴偵測**：找出哪個 endpoint 的 response 欄位是另一個 endpoint 的參數，並注入對應腳本——此項需要跨 controller 的全局視野，採 map-reduce 方式呼叫，細節見下方對應小節（呼應 00 六章新增的「Claude API 端的大範圍語意判斷：map-reduce 模式」）

**填值不在這個清單裡**：曾經用 Claude API 猜值（依 `seed.sql` 挑一個合理值），後來改成完全不經 LLM 的人工填值機制，見下方「人工填值機制」一節——原因與設計細節都在該節說明，這裡不重複。

### 轉換流程與 baseUrl 變數化

用 `openapi-to-postmanv2`（Node.js CLI，00 三已選定）把 `openapi.json` 轉成一份原始 Collection。呼叫方式比照 01 八的跨平台建議，一律經由 `npx` 而非假設全域指令已在 PATH：

```
npx openapi-to-postmanv2 -s <openapi.json 路徑> -o <輸出路徑> -p
```

非 0 的結束碼視為轉換失敗，錯誤內容取自該指令的 stderr。

**決策：保留 `{{baseUrl}}` 這個變數名稱、不改寫**。`openapi-to-postmanv2` 預設會把 OpenAPI 的 `servers[0].url` 轉成 Postman collection variable `{{baseUrl}}`，請求 URL 用這個變數組裝。同一份 Collection 要對 Java（錄製，Agent ②）跑一次、對 Python（驗證，Agent ⑥）再跑一次，兩邊指向不同服務（`JAVA_BASE_URL` vs `PYTHON_BASE_URL`），靠 newman 執行時各自覆蓋同一個變數名稱來切換服務目標，而不是產生兩份 URL 寫死的 Collection。newman 實際呼叫參數屬於 02a 的範圍。

→ 實作見：`spec_collection_agent/collection_converter.py`（`convert_openapi_to_postman()`，`03c_collection_agent_code.md`）

### Readonly / Mutation 分類規則

轉換完的原始 Collection 依 HTTP method 拆成兩份，輸出到 00 已定義的路徑：

| 分類 | HTTP Method | 輸出檔案 |
|---|---|---|
| readonly | `GET`、`HEAD` | `postman/collection_readonly.json` |
| mutation | `POST`、`PUT`、`PATCH`、`DELETE` | `postman/collection_mutation.json` |

拆分時保留 `openapi-to-postmanv2` 依 OpenAPI tag 產生的資料夾結構（同一 tag 底下的 item 依 method 分流到兩個輸出，但資料夾層級關係不變），方便日後對照 module。

> **鏈式驗證需要的 GET，用「新增」不用「搬移」**：若某個 mutation 情境需要「查詢剛建立的資源」（例如 `POST /orders` 之後接 `GET /orders/{{order_id}}` 驗證寫入結果，見 02a 六章 `order_lifecycle` 範例），做法是在該筆 `POST` 之後、同一個頂層 folder 裡**新增一個** `GET` request、引用剛寫入的 environment variable——`collection_readonly.json` 裡對應同一個 endpoint、用 seed 靜態 ID 測試的那個 `GET` **原封不動保留**，兩者測的是不同資料，互不排斥。readonly/mutation 分類規則本身維持單純依 HTTP method、不做例外：`collection_readonly.json` 就是「只含 GET，零副作用」，不會因為鏈式依賴而有例外被抽走。

→ 實作見：`spec_collection_agent/collection_converter.py`（`split_readonly_mutation()`，`03c_collection_agent_code.md`）

### Mutation Collection 的頂層 folder 分組（Claude API，設計約定鎖死）

**規則權威版本在 `02a_harness_architecture.md` 六章「設計約定（鎖死）」**：一個頂層 folder＝一條完整業務情境，有依賴關係的 request 必須同組，禁止純依 Controller／資源類別分組。[B] 產出 `collection_mutation.json` 時必須遵守該規則，若與 02a 描述不一致，一律以 02a 為準，本文件不重複列出規則明細與反例。

**執行順序：必須在「鏈式依賴偵測」的 reduce 階段完成後才能執行**——分組不是獨立的第三次全量分析，而是**消費**下方「鏈式依賴偵測與注入」reduce 階段已經彙整好的 producer/consumer 配對清單：偵測到配對關係的兩個 endpoint，直接依規則歸為同一頂層 folder，不需要再問 Claude 一次——**包括這組 folder 的命名**：membership 與名稱都用機械規則決定（例如依 endpoint path 猜資源名稱後拼接），不為了取名字另外呼叫 LLM。只有「未被偵測到依賴、但語意上可能屬於同一業務情境」的剩餘 mutation request，才需要額外一次範圍很小的 Claude API 呼叫（輸入僅為這些剩餘 endpoint 的摘要，不是整份 `openapi_spec`，也不夾帶已確定的 mandatory group 資訊）去判斷情境邊界；沒有剩餘 request 時省略這次呼叫。此順序不可顛倒——顛倒會讓兩次呼叫各自重新掃描全量 spec，可能得出不一致的分組結果。

> **常見疑慮澄清：跨 module 的頂層 folder 會不會撞上 Agent ⑤ 的模組級局部驗證？**——不會。⑤ 的局部驗證只跑該 module 的 readonly golden cases，完全不觸碰 `collection_mutation.json`（見 `02a_harness_architecture.md` 十三章「兩層驗證，觸發時機不同」）。跨 module 的 mutation folder 只在 Agent ②／⑥ 這兩個所有 module 均已存在或已驗證的時間點執行，不會在局部驗證階段產生假失敗。

> **folder 命名風格統一成英文底線（snake_case）**：mandatory group 的機械命名（`_mechanical_group_name()`）與交給 Claude API 判斷的 singleton 命名（`SINGLETON_GROUPING_SYSTEM_PROMPT`）必須用同一套風格，否則同一份 Collection 裡會出現兩種 folder 風格對不上的情況。兩邊統一成英文小寫、底線分隔：`_mechanical_group_name()` 的後綴固定用 `_flow`（猜不出資源名稱時退回 `business_flow`，資源種類過多時用 `_etc` 截斷）；`SINGLETON_GROUPING_SYSTEM_PROMPT` 明確要求 `folder_name` 只能用小寫英文、數字、底線，不用中文、空格、連字號或 Title Case。

### OpenAPI `$ref` 展開（Claude API payload 組裝的共用前處理）

**問題**：springdoc 產生的 `openapi.json`，request/response body 的欄位結構是用 `$ref`（如 `"$ref": "#/components/schemas/SaveScoreRq"`）指向 `components.schemas` 底下的可重用定義，不會內嵌在每個 operation 裡——OpenAPI 標準的正常寫法。下方兩個消費端若只拿單一 endpoint 的 operation 片段直接用，看到的只有一串 `$ref` 指標，看不到真正的欄位名稱與型別：人工填值模板的 `param_schema` 只有 DTO 類別名稱，人工等於白填一個提示；鏈式依賴偵測則可能讓 Claude API 猜錯 `field_path`／`param_name`，注入腳本悄悄存取到不存在的欄位，是比錯誤 HTTP 狀態碼更隱蔽的失敗模式。

**設計**：在組出這兩個消費端各自需要的內容**之前**，把 operation 片段裡出現的 `$ref` 遞迴展開成實際 schema 定義，兩者共用同一套邏輯，不各自寫一份：

- **只展開這次實際要送出去的 operation 片段裡用到的 `$ref`**，不整包攤平 `components.schemas`——延續 00 六章「只帶當次任務相關資料」的既有原則，避免 context 隨用不到的定義一起膨脹
- **遞迴展開到底，不是只展開一層**：schema 之間可能彼此參照，只展開第一層的話，模型還是會在下一層卡到未展開的 `$ref`，等於沒解決問題
- **循環參照防呆**：展開時追蹤「目前正在展開路徑上已經展開過的 `$ref`」，遇到重複就停止繼續展開（保留一個「循環參照，不再展開」的標記）——OpenAPI 規格本身允許循環參照（例如樹狀結構），不能無限遞迴
- **用通用的 JSON Pointer 解析**（沿 `$ref` 字串的 `/` 路徑走到 spec 對應節點），不要硬寫死「只從 `components/schemas` 找」——OpenAPI 規格允許 `$ref` 指向文件內任何位置，通用解析不會比硬寫死複雜多少，涵蓋範圍卻更完整
- **不處理 schema 組合語法**（`allOf`／`oneOf`／`anyOf`）——不為了假設中的情況先做合併邏輯，等真的遇到再處理

**對既有機制的影響**：展開後的 payload 通常比展開前大（尤其被多個 operation 共用的 schema），鏈式依賴偵測既有的 `_chunk_operations()` 字元數分批機制本來就是在最終 payload 內容上算大小，不需要改動計算邏輯本身，但 `_MAX_CHARS_PER_MAP_CHUNK` 這個門檻值因此更需要拿真實資料重新評估（見七章待決定事項）。

→ 實作見：`spec_collection_agent/openapi_refs.py`（`03c_collection_agent_code.md`），人工填值模板產生與鏈式依賴偵測 map 階段共用。

### 人工填值機制（不經 LLM，一次性前置作業）

**設計定位**：曾經的設計是「Claude API 依 `seed.sql` 猜值，猜失敗才轉人工補救」。實測後改成完全相反的順序：**所有需要動態值的 mutation endpoint，一律由人工提供真實 payload，Claude API 完全不參與填值判斷**。改變原因：

1. 這個 Java 服務的 endpoint 路徑是動詞式 RPC 風格（如 `/api/exam/answer/save`），不是資源式 REST 路徑（`/resource/{id}`）——「從 URL 猜資源名稱、比對 `seed.sql` 表名」這個做法的前提在這個專案不成立，猜出來的字串幾乎不曾對上真正的表名。
2. 人工操作真實網頁取得的 payload 是**實際流量**，正確性是 LLM 猜測不可能達到的等級，也不存在「挑到真實存在但語意不相關的值」這種 False Pass 風險（風險成因見下方 `Decision.SKIP` 段落與「產出與交接」）。
3. 完全省下這部分的 API 呼叫與 token 成本。

**兩階段流程**：

**階段一（[A] Spec Agent 產出 `openapi.json` 後立刻執行，不呼叫 Claude API）**：對每個 mutation endpoint，用既有的 `_operation_param_schema()` 判斷是否有 path/query/body 參數需要動態值；有的話，依 OpenAPI `tags` 分組，每個 controller 產生一份 `postman/manual_fill/<controller>.json`，內含該 controller 底下全部待填 endpoint 的模板（欄位定義見下方表格）。**強制涵蓋全部**待填 endpoint，不是像舊設計那樣「猜失敗才生成」；`openapi.json` 出現新 endpoint 時，重跑只會**新增**新模板，不動既有部分（見下方「模板不刪除」）。這一步結束後流程暫停，等人工把所有 controller 檔案填完（graph 層級怎麼接見 01 對應章節，待後續更新）。

**階段二（人工確認全部檔案已填完，重新觸發後執行）**：
1. 依「尚未就緒」過濾——標記 `skip` 或尚未解決（`is_resolved=False`，涵蓋「還沒填」與「上次套用失敗」兩種情況，見下方「套用失敗的重填機制」）的 endpoint，**在鏈式依賴偵測（下方 map 階段）開始分析之前就先排除**，不會出現在 map 階段的候選分析輸入裡，避免對這次注定不會進最終 Collection 的 endpoint 做無意義的跨 controller 分析，也避免鏈式依賴配對指向一個之後才被排除的 endpoint，讓 folder 分組出現「membership 決定了、其中一端後來卻被拿掉」的殘缺情形。
2. 鏈式依賴偵測（map-reduce，見下方對應小節，設計不變，仍是 Claude API）。
3. Mutation 頂層 folder 分組（見上方對應小節，設計不變，仍是 Claude API）。
4. 套用人工填值（純程式邏輯，不呼叫 API，依 `fill_mode` 分派；套用結果回寫 `last_apply_error`，見下方「套用失敗的重填機制」）。
5. 注入鏈式依賴腳本、寫出最終兩份 Collection。

**`ManualFillEntry` 欄位**（對應 `spec_collection_agent/manual_fill.py`）：

| 欄位 | 說明 |
|---|---|
| `endpoint` | 如 `POST /api/exam/answer/save` |
| `param_schema` | 該 operation 的 path/query/body 參數定義（`$ref` 已展開，見上方「OpenAPI `$ref` 展開」）——純粹給人工填值時參考欄位名稱與型別用，不再是送給 LLM 的 prompt 內容 |
| `note` | 選填，**人工**自己寫的註記（例如為什麼這個 endpoint 要 `skip`）。**取代原本的 `error` 欄位**：舊欄位是記錄「LLM 為什麼填值失敗」的系統訊息，現在不存在 LLM 嘗試這件事，沒有系統自動填的失敗訊息可記錄 |
| `fill_mode` | `fields`／`raw_body`／`file_upload` 三選一，模板產生時依 body 實際形狀決定預設值：body 根層是 object→`fields`、是 array→`raw_body`、`multipart/form-data`→`file_upload`（見下方「檔案上傳」） |
| `values` | `fill_mode="fields"` 時使用，巢狀欄位用點號路徑表示（與下方「鏈式依賴偵測與注入」`producer_field` 的點號路徑約定一致，共用同一套 `postman_tree.py` 巢狀路徑工具） |
| `raw_body` | `fill_mode="raw_body"` 時使用，整包覆蓋 body |
| `file_paths` | `fill_mode="file_upload"` 時使用，見下方「檔案上傳」 |
| `decision` | `fill`（預設，等待人工提供上述其中一種值）／`skip`（人工主動判斷此 endpoint 是特例，不走一般重構驗證流程，見下方說明） |
| `last_apply_error` | **系統**寫入，人工不用填也不用清。記錄上一次嘗試把這個 entry 的值套進 Postman item 時失敗的原因；`None` 代表沒試過，或上次成功。見下方「套用失敗的重填機制」 |

**排除結果的兩層分類**：一個 endpoint 沒進最終 Collection，成因有三種情境，但收斂成**兩種結果**，不是三個並列的原因：

| 情境 | `decision` | 結果分類 | 語意 |
|---|---|---|---|
| 人工主動填 `skip` | `skip` | **skip**（永久排除） | 編輯決定，這個 endpoint 不該走一般重構驗證流程，不會因為重跑而變回來 |
| 尚未填值 | `fill` | **retry**（待重試） | 人工還沒填完，填完重跑即可解決 |
| 填了但套用失敗 | `fill` | **retry**（待重試） | 系統發現填的值套不進 Postman item，修正後重跑即可解決 |

情境二、三本質是同一種結果——都是「這一輪還不能用，但修正後重跑就會好」，統稱 **retry**，不能沿用 `skip` 這個字（`skip` 專指人工的編輯決定，兩者語意不同、後續動作也不同：`skip` 不會因為任何後續動作變回來，`retry` 的目標就是被修正掉）。`postman/unfilled_endpoints.json`（見「產出與交接」）與 `manual_fill.py` 的內部判斷都依這個兩層分類組織，不是三個獨立字串。

**`Decision.SKIP` 的語意**：**不是**「填不出值、放棄」，是人工主動判斷「這個 endpoint 不該走一般重構驗證流程」的編輯決定——例如依賴 OCR、語音辨識這類需要真實內容才有意義的處理，本階段暫不自動化測試。標記後：不進人工填值套用邏輯、不進鏈式依賴偵測候選分析、不進最終 Collection，只記錄進 `postman/unfilled_endpoints.json`（`category="skip"`）。

**套用失敗的重填機制（retry 分類之一）**：套用失敗跟 `Decision.SKIP` 不同——套用失敗是系統發現「填的值套進 Postman item 時對不上結構」（例如巢狀路徑衝突），只有實際套用才會知道，不代表人工想放棄這個 endpoint，跟「尚未填值」一樣歸在 retry。

- **`is_resolved`**：`decision=skip` 一律已解決；`decision=fill` 時除了「值已填」還要 `last_apply_error is None` 才算已解決——套用失敗會讓已填值的 entry 變回未解決，跟「尚未填值」是同一種未解決狀態，`is_resolved` 不區分兩者的細節原因。
- **`has_value` 與 `is_resolved` 是兩個不同的問題**：`has_value` 只回答「值填了沒」（不看 `last_apply_error`）；`is_resolved` 回答「這一輪算不算解決了」。階段二套用階段判斷「要不要嘗試套用」用的是 `has_value`，不是 `is_resolved`——一個帶著上一輪 `last_apply_error` 的 entry，`has_value=True` 但 `is_resolved=False`，如果套用階段誤用 `is_resolved` 判斷要不要嘗試，會讓已經修正好的值永遠沒有機會被重新套用、永遠卡在「尚未解決」，重填機制形同虛設。`list_pending()`／暫停關卡才用 `is_resolved`。
- **寫回**：階段二步驟 4 套用失敗時，錯誤訊息寫回該 entry 的 `last_apply_error`；套用成功則清空。人工不用碰這個欄位，照錯誤訊息修正 `values`／`raw_body`／`file_paths` 即可。
- **重跑自動生效**：`is_resolved` 一改，五章兩道既有暫停關卡（都靠 `list_pending()` 判斷）就會自然抓到這個 entry 並暫停等重填，不需要新的重試迴圈或 graph 節點——原本形同防禦性擺設的第二道關卡因此有了實質用途。
- **已知限制**：鏈式依賴偵測發生在套用人工填值**之前**，過濾用的是這一輪開始時的 `is_resolved` 狀態。一個帶著上一輪 `last_apply_error` 的 endpoint，就算這一輪套用已經成功，這一輪的鏈式依賴偵測仍會排除它——它會正常進最終 Collection，但牽涉的鏈式依賴要等下一輪才補上。要消除這個落差得在偵測前先做一次「乾跑套用」，複雜度不成比例（套用失敗本身是少數情況，多半是路徑打錯字），故不處理。

**模板不刪除**：舊設計的 `remove_template()` 會在 endpoint 解決後刪掉模板檔，這次改成**永久保留**：(1) 後續流程（鏈式依賴注入、newman 執行）萬一發現問題，人工填過的原始資料要找得回來；(2) 保留本身就是可稽核的紀錄。**連帶影響**：`manual_fill.list_pending()` 不能再用「檔案存在＝pending」判斷（模板不刪除後每個 endpoint 都會永遠有檔案），改成逐一讀取模板內容，依 `ManualFillEntry.is_resolved` 判斷。

**檔案上傳（`fill_mode="file_upload"`）**：對應 `multipart/form-data` 的 body（目前有 `POST /api/file/image`；`POST /api/file/voice` 語意上也是檔案上傳，但實測發現 openapi.json 對它的宣告有誤——見下方「更正：openapi.json 宣告錯誤時的處理」）。`file_paths` 是「formdata 欄位名稱 → 相對於專案根目錄的檔案路徑」的對應，套用時把路徑寫進 Postman formdata 項目的 `src` 欄位。**模板產生時 `file_paths` 留空，不自動預填假圖檔**——若 Java 端對檔案內容有實質業務邏輯（OCR、語音辨識），塞一張空白假圖可能讓 Java/Python 因「都辨識不出東西」巧合走到同一種降級分支、產生沒有意義的驗證結果，是跟填值本身相同的 False Pass 風險，必須由人工依實際業務邏輯決定。倉庫提供一個最小合法 PNG fixture（`fixtures/file_upload_samples/`，目錄命名刻意不用 `manual_fill` 字根，避免跟 `postman/manual_fill/` 這個流程主幹搞混——兩者關係是「主幹」與「主幹裡某個欄位可選引用的旁支素材庫」，不是同一套機制的兩個部分）供選用，不會自動套用。

> **真實檔案放 `local_uploads/`，不是 `fixtures/file_upload_samples/`**：人工若選擇不用假圖測試、改用真實業務內容（例如真的考生錄音、真的考生照片）驗證有實質業務邏輯的 endpoint，這些檔案**不能**放進 `fixtures/file_upload_samples/`——那個目錄沒有被 `.gitignore` 排除，會跟著 `git commit` 一起進版本歷史，而錄音／人臉照片是真實個資，這個系統又是教育考試場景（`school`/`grade`/`class` 這類欄位），考生很可能是未成年人，個資一旦寫進 git history 就不是刪檔案能清乾淨的事。改放專案根目錄下的 `local_uploads/`（已加進 `.gitignore`，見該處註解），`file_paths` 一樣填相對於專案根目錄的路徑（如 `"local_uploads/candidate_voice.mp3"`）即可，套用邏輯完全不用區分檔案放哪裡。兩個目錄的區隔：`fixtures/file_upload_samples/` 是可重複使用、無敏感內容、進 git 的通用範例檔；`local_uploads/` 是這次測試實際要用、可能含個資、刻意不進 git 的真實檔案，兩者不互相替代。

> **url variable/query 一律先套用，不受 `fill_mode` 限制**：`multipart/form-data` 或 `raw_body` 的 endpoint 常常同時帶著 query 參數（例如 `POST /api/file/image` 的 `kind`／`randomId`／`side` 是 query 參數，只有 `file` 是 multipart body）。若依 `fill_mode` 三選一分派、只呼叫對應那一種套用函式，旁邊的 query 參數會被忽略，永遠停留在 `openapi-to-postmanv2` 產生的預設佔位值而非人工填的真值——而且是**靜默**發生，不會有任何警告，是本節「False Pass 風險」的具體案例。因此 `apply_manual_fill()` 不管 `fill_mode` 是什麼，一律先用 `entry.values` 套用 url variable/query（`value_filler._apply_url_values()`），再依 `fill_mode` 處理 body 本身——url 參數跟 body 是什麼格式無關，不該綁在一起判斷。

> **openapi.json 宣告錯誤時的處理**：`POST /api/file/voice` 是實例——openapi.json 把它宣告成 `application/json` 裡一個 `format: binary` 字串欄位（走 `fields` 模式），但瀏覽器實際流量是 `multipart/form-data`，而且欄位（`typeNumber`）根本沒被宣告出來——這是 springdoc 對 `MultipartFile` 參數混搭其他 `@RequestParam` 時常見的標註缺陷，不是 [B] 這邊的問題。**只要來源 openapi.json 是錯的，`openapi-to-postmanv2` 轉出來的 Postman item 結構就是錯的，任何 `fill_mode` 都無法在 manual_fill 這層補救**——例如硬把 `fill_mode` 改成 `file_upload`，`_apply_file_upload()` 會發現 item 的 body 根本不是 formdata 結構而直接拋例外。這類 endpoint 應標記 `Decision.SKIP`（`/api/file/voice` 本身也符合 SKIP 語意舉例的「語音辨識這類需要真實內容才有意義的處理」），`note` 寫明原因；真正要修得回頭改 Java 端的 OpenAPI 標註，超出 [A]/[B] 範圍。日後若 Java 端修正，模板不會自動更新（見「模板不刪除」——已存在的 endpoint 不覆寫），需要人工刪除舊 entry 讓它被當成新 endpoint 重新產生模板。

**刪除的部分**：舊的 LLM 填值路徑（`value_filler.py` 的 `fill_example_values()`）整個移除，連帶清掉沒有其他呼叫端的孤兒程式碼：`prompts.py` 的 `FILL_SYSTEM_PROMPT`／`FILL_OUTPUT_SCHEMA`；`value_filler.py` 的 `extract_seed_excerpt()`／`_split_sql_statements()`／`_INSERT_TABLE_PATTERN`（這組原本是為了幫 LLM 填值窄化 `seed.sql` context，現在沒有 LLM 呼叫需要窄化，一併移除）。`value_filler.py` 保留：`_apply_values_to_item()`（`fields` 模式套值，manual_fill 沿用）、`_scalar_param_value()`、`_prune_excluded()`、`_operation_param_schema()`（判斷 endpoint 是否需要生成模板）、`guess_resource_name()`（`folder_grouper.py` 的 `_mechanical_group_name()` 用它猜 mandatory group 的人類可讀 folder 名稱，跟 seed.sql 無關的另一個用途）。

→ 實作見：`spec_collection_agent/manual_fill.py`（`03c_collection_agent_code.md`）

### 鏈式依賴偵測與注入（Claude API，map-reduce）

**設計不變量**：鏈式依賴的 ID 一律用 Postman Environment Variable 動態傳遞、不跨執行共用（原文見 00 七/[B]，此處不重複）。

**輸入前先過濾尚未就緒的 endpoint**：階段二第一步——`skip` 或 `is_resolved=False`（還沒填、或帶著上一輪 `last_apply_error`）的 endpoint，送進 map 階段前先從 operation 集合拿掉，不會被分析成候選 producer／consumer。避免對這一輪注定不會進最終 Collection 的 endpoint 做無意義分析，也避免鏈式依賴配對指向一個之後才被排除的 endpoint，讓 folder 分組出現殘缺情形。

**為什麼不能一次把整份 openapi_spec 丟給 Claude API**：鏈式依賴的判斷需要跨 endpoint（甚至跨 controller）的全局視野，屬於 00 六章「Claude API 端的大範圍語意判斷：map-reduce 模式」明列的適用場景——單次呼叫塞入完整 `openapi_spec` 會讓 context 隨 API 數量線性膨脹，也容易在 endpoint 數量多時漏配對或誤配對。因此偵測拆成 map／reduce 兩階段：

**Map 階段（依 OpenAPI tag／controller 拆分，可平行呼叫 Claude API）**：每個 controller 各自分析自己底下的 endpoint，只做兩件事，**不做跨 controller 配對判斷**：

| 候選類型 | 判斷內容 | 範例 |
|---|---|---|
| 候選 producer 欄位 | 哪些 response 欄位「看起來像」可被其他 endpoint 引用的識別碼（如 `id`、`code`、唯一值） | `POST /api/v1/users` 的 `data.id` |
| 候選 consumer 參數 | 哪些 path/query/body 參數「看起來像」需要動態值，而非 seed 靜態值就能滿足 | `GET /api/v1/users/{id}` 的 path 參數 `id` |

Map 階段輸出的是「候選清單」，不是最終配對結果——同一個 controller 看不到其他 controller 的候選，所以無法、也不需要在這一步下結論。

**候選 producer 僅限 mutation method（POST/PUT/PATCH/DELETE）**：GET/HEAD 依分類規則不會建立新資料，回應內容要嘛是固定不變的靜態參照資料（該用人工填值機制解決，不是鏈式依賴），要嘛不該被當成「這次測試執行才產生」的資料來源。這個限制不靠 Claude 判斷語意，Map 階段回傳後由程式碼依 `endpoint` 的 method 機械過濾，在 Reduce 階段開始前就排除，不會被誤配對成 producer（見 `03c_collection_agent_code.md` `_map_analyze_group()`）。候選 consumer 不受此限——GET 端點的 path/query 參數一樣可以是消費端。

**分組依據與併發數**：以 OpenAPI operation 的 `tags` 欄位當 controller 邊界——一個 operation 可能掛多個 tag，取第一個決定分組；完全沒有 tag 的歸進同一個 `_untagged` 桶。分組的同時，每個 operation 的 `$ref` 也在這一步展開（見上方「OpenAPI `$ref` 展開」），送進 Map 階段時已是展開完的欄位結構。平行呼叫的併發數呼叫 `common/concurrency.py` 的 `default_concurrency()` 決定（可用核心數 − 1，執行期動態計算，不寫死，避免佔滿本機其餘資源——同一台機器通常還跑著 translator-cli 的本地模型），跟 ① 解析 Agent 共用同一份實作，不各自重新推導（見 00 六章「Map 階段併發數（共用工具）」）。單一 tag 的 endpoint 數量或 schema 複雜度過大時，依 payload 大小把該 tag 拆成多個子批次分別呼叫（見 `03c_collection_agent_code.md` `_chunk_operations()`）——tag 仍是分組邊界，子批次只是同一 controller 的候選分析分幾次呼叫完成，下一段「失敗處理粒度」的判斷單位對齊到子批次。

**失敗處理粒度**：延續本文件六章「LLM 呼叫失敗即硬性失敗」的邊界，但分兩層看：任一 controller 的 map 呼叫本身失敗（逾時、回傳格式整體不合法），視為整個偵測失敗，直接中止，因為漏掉的候選不會被任何後續機制發現；但單一 controller 候選清單裡，個別一筆格式不合法（例如缺欄位），只丟棄那一筆、其餘照常送進 reduce 階段——候選清單是 best-effort 中間產物，reduce 階段的最終配對輸出才是需要嚴格把關的權威結果。

**Reduce 階段（單次呼叫 Claude API，輸入為 map 階段濃縮後的候選清單，非原始 schema 全量）**：彙整所有 controller 的候選 producer／consumer，做跨 controller 最終配對，找出「哪個候選 producer 欄位對應哪個候選 consumer 參數」。找不到任何鏈式依賴是合法結果；map 階段任一邊整體為空時，reduce 無論如何配不出東西，直接省略這次呼叫。輸出資料形狀：

| 欄位 | 說明 |
|---|---|
| `producer_endpoint` | 產生資料的 endpoint，如 `POST /api/v1/users` |
| `producer_field` | response 中的欄位路徑，點號分隔，如 `data.id` |
| `consumer_endpoint` | 需要用到該資料的 endpoint，如 `GET /api/v1/users/{id}` |
| `consumer_param` | 對應的參數名稱（path/query/body 皆可）；巢狀 body 欄位比照 `producer_field` 用點號路徑表示（如 `"user.id"`） |
| `env_var_name` | producer/consumer 共用的 environment variable 名稱，如 `created_user_id` |

這份 reduce 輸出即為上方「Mutation Collection 的頂層 folder 分組」直接消費的配對清單，分組階段不需要重新呼叫 map 或 reduce。

**注入機制**：偵測結果（reduce 階段輸出）套用到 Collection 上分兩步。

1. **注入腳本**：`producer_endpoint` 對應的 item 加一段測試腳本，把 response 欄位存進 environment variable。`consumer_endpoint` 依它原本的分類做不同處理：
   - 若是 `POST`／`PUT`／`PATCH`／`DELETE`（依分類規則本來就只存在於 mutation），直接把該 item 在 mutation 裡的 URL/參數改用這個 environment variable。
   - 若是 `GET`（依分類規則已經在 readonly 裡有一份用 seed 靜態值測試的版本），則在同一個頂層 folder 內**新增一個獨立 item**、引用這個 environment variable，`collection_readonly.json` 裡原本那份維持不動（見上方「Readonly / Mutation 分類規則」的說明）。

   產出格式範例（Postman 測試腳本，屬於**產出檔案**的內容，非本專案程式碼）：

    ```javascript
    pm.test("capture created_user_id", function () {
        const json = pm.response.json();
        pm.environment.set("created_user_id", json.data.id);
    });
    ```

    對應的 consumer 請求 URL 會是 `{{baseUrl}}/api/v1/users/{{created_user_id}}`。

2. **folder 內排序**：`openapi-to-postmanv2` 產生的 item 順序依 OpenAPI 文件裡的宣告順序，不保證 producer 排在 consumer 前面。套用注入前，先依偵測結果建一個小型依賴圖，對**同一個頂層 folder 內**的 item 做拓樸排序，確保 producer 先執行——排序範圍限定在單一 folder 內，folder 之間本來就彼此獨立（分組規則見上）。

> **不需要額外維護 `postman/environment.json`**：Postman 的環境讀寫在 newman 執行時作用於記憶體內的 environment context，即使沒有帶入既有 environment 檔案，腳本仍能在單次執行內正常讀寫。00 的設計不變量本來就要求「不跨執行共用 ID」，沒有檔案反而更直接保證這個不變量不會被意外破壞（例如忘記清空舊檔案導致下次執行讀到過期 ID）。

→ 實作見：`spec_collection_agent/chain_dependency_detect.py`（map／reduce 兩階段函式）、`chain_dependency_inject.py`（`inject_chain_scripts()`），`03c_collection_agent_code.md`；兩個 system prompt 集中在 `spec_collection_agent/prompts.py`，不散落在邏輯檔案裡。

### 產出與交接

- `postman/collection_readonly.json`
- `postman/collection_mutation.json`
- `postman/unfilled_endpoints.json`（這一輪沒能進最終 Collection 的 endpoint 清單，見上方「排除結果的兩層分類」：每筆記錄一個 `category`，只有 `skip`／`retry` 兩種值；`retry` 底下用細節文字區分「尚未填值」或「套用失敗」，兩者都會回寫進對應模板的 `last_apply_error`／保持 `is_resolved=False`，讓重跑時能被暫停關卡重新抓到）
- `postman/manual_fill/<controller>.json`（人工填值模板，永久保留，見上方「模板不刪除」）

兩份 Collection 是 [B] 的最終產出，State 裡對應 `collection_readonly_path`／`collection_mutation_path`（見 01 三）。交接給 Agent ②：Agent ② 直接讀這兩份檔案跑 newman，不會回頭呼叫 [B]；[B] 之後也不再參與 pipeline（不像 Harness／translator-cli 會被 ⑤/⑥/⑦ 迴圈重複呼叫）。`unfilled_endpoints.json` 不進 State，是給人看的產出，不影響 pipeline 後續走向。

---

## 四、模組結構規劃

比照 `refactor_harness/`、`translator_cli/` 的組織方式（見 01 二），[A]/[B] 的邏輯規劃為獨立套件 `spec_collection_agent/`，`graph/nodes/spec_node.py`、`graph/nodes/collection_node.py` 維持薄封裝、只負責從 State 取值、呼叫套件、把結果寫回 State（沿用專案既有的 `graph/nodes/` 目錄，非新建頂層 `nodes/`）：

```
refactor-project/
├── spec_collection_agent/       # 本文件規劃的新套件，實作分兩份：[A] 見 03b、[B] 見 03c
├── specs/
│   └── openapi.json             # [A] 落地檔案（本文件新增目錄，供 [B] 呼叫轉換 CLI 用）
├── fixtures/
│   └── file_upload_samples/     # 檔案上傳 endpoint 可選用的通用範例檔，進 git（見三章「檔案上傳」）
├── local_uploads/                # 真實檔案（可能含個資），.gitignore 排除，不進 git（見三章「檔案上傳」）
├── postman/
│   ├── collection_readonly.json
│   ├── collection_mutation.json
│   ├── unfilled_endpoints.json  # 這一輪未進最終 Collection 的清單，category=skip／retry 兩種，正常情況下為空
│   └── manual_fill/
│       └── <controller>.json    # 依 OpenAPI tags 分檔案，永久保留
```

| 職責 | 說明 |
|---|---|
| 啟動並擷取 Java 服務的 OpenAPI spec | 對應二的服務生命週期管理與就緒判定 |
| 落地 `openapi.json` | State 中的 `openapi_spec` 是主要傳遞路徑，落地檔案是給 `openapi-to-postmanv2` 這類吃檔案路徑的 CLI 工具用 |
| OpenAPI → Postman 轉換與分類 | 對應三的轉換流程與 readonly/mutation 分類 |
| OpenAPI `$ref` 展開 | 對應三的共用前處理，人工填值模板與鏈式依賴偵測共用 |
| Mutation 頂層 folder 分組 | 對應三的分組鎖死規則 |
| 人工填值模板產生與套用 | 對應三的「人工填值機制」，不呼叫 Claude API |
| 鏈式依賴偵測與注入 | 對應三的鏈式依賴偵測與注入 |
| 對外唯一入口 | 供 `graph/nodes/spec_node.py`、`graph/nodes/collection_node.py` 呼叫，兩個 node 本身不直接碰觸上述任何細節 |

> `specs/`、`spec_collection_agent/` 已補進 01 二的專案目錄樹。

---

## 五、與 LangGraph 的整合

對應 01 四的節點表格，`extract_spec`／`gen_manual_fill_templates`／`gen_collection` 是主流程的線性 node（01 五：`parse → extract_spec → gen_manual_fill_templates → gen_collection → record_tests → design`，`gen_manual_fill_templates`／`gen_collection` 之間各接一道條件邊，見下方說明），三者只做「從 State 取值、呼叫四的對外入口、把結果寫回 State」，不包含任何四所述的內部邏輯。三者讀寫哪些 State Keys 以 01 三章 State Schema 為權威定義，本文件不重複列表；三者都是線性 node，可比照 01 七 stub 慣例整包展開 state（`{**state, ...}`），不像 `plan`／`scaffold` 是平行分支、必須只回傳自己更動的 key。

`extract_spec` 另外直接讀取 `JAVA_JAR_PATH`、`JAVA_BASE_URL`、`SPRING_DATASOURCE_*` 這幾個環境變數（見下方備註），這部分 01 未涵蓋。

> 三章「人工填值機制」把暫停點從「`gen_collection` 之後」提早到「`extract_spec` 之後、`gen_collection` 真正開始跑鏈式依賴偵測之前」——`collection_node.py` 拆成 `run_generate_templates`（graph node id：`gen_manual_fill_templates`，階段一）與 `run`（沿用舊 node id `gen_collection`，階段二），中間的暫停判斷是 `should_await_manual_fill_templates_or_continue`，跟階段二既有的 `should_await_manual_fill_or_continue` 一樣指向同一個 `await_manual_fill` 終止節點。`graph/builder.py` 佈線細節見 `03c_collection_agent_code.md` 三、四章；兩階段拆分已同步進 `01_langgraph_architecture.md` 四、五章的節點表格與流程圖。

> **備註**：`JAVA_JAR_PATH`、`SPRING_DATASOURCE_*` 這幾個啟動 Java 服務用的設定，**不放進 `RefactorState`**，`spec_node.py` 內部直接讀 `os.environ`。理由：`RefactorState` 應該只放 pipeline 執行期間動態產生、需要跨 node 傳遞的資料（如 `openapi_spec`），這幾個值是啟動當下就固定、只有 `spec_node.py` 一個 node 用得到的靜態設定，不需要經過 State 傳遞。這跟 01 三章 `test_dsn`／`python_base_url` 放進 State 不完全一致——那兩個值被 `implement_node` 內部的 `DbEnvironment`／`GoldenVerifier` 跨模組共用；`JAVA_JAR_PATH` 只有單一 node 用，直接讀環境變數更省事。

---

## 六、錯誤處理範圍與 `retry_count` 的邊界

`retry_count` 迴圈只包住 `implement → run_tests → debug` 這一段（定義見 01 五的 conditional edge）。`parse → extract_spec → gen_collection → record_tests → design` 是純線性流程，**不在**這個迴圈裡：

- [A]/[B] 任何一步失敗（Java 啟動不了、轉檔失敗、鏈式依賴偵測/folder 分組的 LLM API 掛掉且無法降級），錯誤直接往上拋，整條 LangGraph run 中止
- 這類失敗多半是環境沒準備好（00 五的一次性設定沒做對）或外部服務問題，重跑 `implement`/`debug` 也解決不了，因此不適合塞進自動重試迴圈，交給人工排查後重新從頭跑
- 例外：`unfilled_endpoints.json` 的兩種結果分類——`skip`（人工決定，永久排除）與 `retry`（尚未填值或套用失敗，見三「排除結果的兩層分類」「套用失敗的重填機制」）——都**不算硬性失敗**：endpoint 被排除、記入 `unfilled_endpoints.json`，[B] 其餘部分照常跑完，不中止。兩者都是人工填值機制本身的正常狀態，不是外部服務層級的失敗

---

## 七、待決定事項

- [ ] Prompt 本文與模型選擇留待 03c（依三章各節的輸入/輸出契約定案，prompt 本文集中在 `spec_collection_agent/prompts.py`）；初步方向：先求一版堪用 prompt 讓 [A]/[B] 儘快接上（呼應 00 一開發順序第 2 步），精修（措辭、few-shot、模型比較）留到後續與 ①③⑤⑦／[P] 一起處理。
- [ ] Map 階段平行呼叫的併發數，目前以「執行機器核心數 − 1」為預設（見三章「分組依據與併發數」），實際跑起來的延遲/成本表現若不理想，可能需要改成固定值或獨立環境變數，留待有真實 `openapi.json` 可測再評估。