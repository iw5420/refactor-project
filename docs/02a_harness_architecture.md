# Harness 架構設計：Java → Python Multi-Agent 重構情境

> 基於「重構多agent架構研究 v1.0」量身設計的 Test Harness 方案
> 程式碼實作請見配套文件：`02b_harness_code.md`

---

## 一、本情境的 Harness 角色定位

你的 Multi-Agent 架構中，Harness **不是一個單獨的 Agent**，而是**橫跨整個流程的基礎設施層**，由兩個現有 Agent 共同承擔：

```
Agent ②（測試 Agent）= Harness 的「錄製端」
Agent ⑥（測試執行 Agent）= Harness 的「驗證端」

[Java 服務]              [Python 服務]
    │                         │
Agent ②                  Agent ⑥
（錄製 golden output）    （比對 golden output）
    │                         │
    └──────── Harness ─────────┘
              （共用基礎設施）
```

這個設計的核心優勢是：**兩端共用同一套 Harness 邏輯**，確保「錄製」和「驗證」的標準完全一致，不會因為兩端用不同邏輯而產生偽 fail。

---

## 二、Harness 整體架構

```
refactor_harness/
├── recorder/               ← Agent ② 使用
│   └── golden_writer.py    （錄製 golden output）
│
├── verifier/               ← Agent ⑥ 使用
│   ├── comparator.py       （載入 golden output，執行比對）
│   └── mutation_verifier.py（比對寫入操作的 response）
│
├── core/                   ← 共用核心（兩端都用）
│   ├── postman_runner.py   （執行 newman，兩端共用同一份）
│   ├── masker.py           （動態欄位遮罩）
│   ├── diff_engine.py      （diff 計算與格式化）
│   └── reporter.py         （產生 JSON report，供 Debug Agent 使用）
│
├── fixtures/
│   ├── seed.sql            （測試 DB 初始資料）
│   └── golden/             （golden output 存放位置）
│       ├── users/
│       ├── orders/
│       └── _metadata.json
│
└── config/
    ├── harness.yaml        （harness 設定）
    └── mask_rules.yaml     （遮罩規則設定）
```

> **設計說明**：`postman_runner.py` 統一放在 `core/`，兩端共用同一份，避免日後改了一份漏改另一份造成行為不一致。

---

## 三、Agent ② — 錄製端（Harness Recorder）

### 職責

對**正在運行的 Java 服務**執行 Postman collection，把每一支 API 的 response 標準化後存成 golden output。

### 執行流程

```
① 確認 Java 服務健康（health check）
② 確認 DB 在已知初始狀態（apply_seed）
③ 執行 newman（Postman CLI）對 Java 服務
④ 捕獲每支 API 的 response
⑤ 標準化（masking）：移除動態欄位
⑥ 寫入 golden output 檔案（fixtures/golden/{module}/{case_id}.json）
⑦ 寫入 _metadata.json（錄製摘要）
```

### case_id 命名規則

golden output 的檔案名稱即 case_id，格式為：

```
{item_name}_{method}_{path}
範例：create_order_POST_api_v1_orders
```

加入 Postman Item Name 作為前綴，確保同一個 endpoint 的多個測試情境（成功/失敗/邊界）不會互相覆蓋。

### 非 JSON Response 的處理

只錄製 `Content-Type: application/json` 的 response。二進位、空回應、HTML error page 一律跳過並記錄到 `_metadata.json` 的 `skipped` 清單，供人工確認是否需要額外處理。

→ 實作見：`recorder/golden_writer.py`（`02b_harness_code.md`）

---

## 四、Agent ⑥ — 驗證端（Harness Verifier）

### 職責

對**正在運行的 Python 服務**執行相同的 Postman collection，把 response 標準化後與 golden output 比對。

### 執行流程

```
① 確認 Python 服務健康（health check）
② 確認 DB 在相同初始狀態（同一份 seed）
③ 執行 newman（對 Python 服務）
④ 捕獲每支 API 的 response
⑤ 標準化（同一份 masking 邏輯）
⑥ 從 fixtures/golden/ 載入對應 golden output
⑦ 執行 diff 比對
⑧ 輸出結構化 report（供 Debug Agent 使用）
```

### 兩種比對模式

`GoldenVerifier` 提供兩個入口：

- `verify(collection_path)`：跑完整 collection，比對所有模組。用於全量驗證（LangGraph 條件邊放行條件）。
- `verify_module(collection_path, module_filter)`：只比對指定模組的 golden cases。用於局部驗證（Task 完成後的快速 fail-fast）。

→ 實作見：`verifier/comparator.py`（`02b_harness_code.md`）

---

## 五、Newman 共用執行器

`core/postman_runner.py` 是兩端共用的 newman 執行入口，確保錄製端和驗證端的執行行為完全一致。

主要職責：
- 呼叫 `newman run`，輸出 JSON 格式報告
- 檢查 return code：若 newman 失敗（服務沒起來、collection 路徑錯誤）立即拋出明確例外，不讓錯誤靜默流入後續比對

→ 實作見：`core/postman_runner.py`（`02b_harness_code.md`）

---

## 六、Newman Collection 的狀態污染防護

### 問題

Newman 是循序執行整個 Collection 的。如果 Collection 裡混有寫入操作（`POST /orders`、`DELETE /users/1`），第一個 request 執行後 DB 狀態就改變了，後續的 GET 拿到的資料會與「乾淨 seed 狀態」不同。

Java 和 Python 跑同一份 Collection，只要兩邊處理副作用的順序或行為有任何微小差異，中後段的比對就會產生偽 fail。

### 解法：Collection 按副作用程度分成兩組

```
postman/
├── collection_readonly.json    ← 只含 GET，零副作用，主要驗證來源
└── collection_mutation.json    ← 含 POST/PUT/DELETE，每次跑前重新 seed
```

**Group A（readonly）**：主要的 golden output 錄製與比對都用這份。每次跑前只需 `apply_seed` 一次，Collection 執行過程中 DB 不會被改變，比對結果完全可信。

**Group B（mutation）**：驗證寫入操作的 response（status code 與 body 結構）。每個 mutation case 之前都重新 `apply_seed`，保證初始狀態一致。動態 ID 欄位（`id`、`order_id` 等）由 Masker 統一抹平，因此可以正常比對 body 的非動態欄位，不會漏掉「Python 偷懶只回傳空物件」這類嚴重問題。

### Seed 策略

| Collection | Seed 時機 | 執行方式 |
|---|---|---|
| readonly | 整個 collection 跑前 seed 一次 | `apply_seed` → `newman run` |
| mutation | 每個 folder（case）跑前都 seed | 逐 folder 循環：`apply_seed` → `newman run --folder` |

→ 實作見：`verifier/mutation_verifier.py`（`02b_harness_code.md`）

---

## 七、共用核心：Masker

兩端使用**完全相同**的 masker，這是確保比對公平的最關鍵設計。

### 遮罩機制

Masker 遞迴走訪整個 response body，對符合條件的欄位替換為 `<<MASKED>>`：

- **欄位名稱比對**（`masked_fields`）：timestamp、token、request_id 等動態純量欄位
- **值的 Pattern 比對**（`masked_patterns`）：ISO datetime 字串、UUID、Bearer token

### 重要限制

`masked_fields` **只放純量型態（string / number）的動態欄位**。若把值為陣列或物件的欄位放進來，Java 和 Python 兩端無論回傳何種型態都會被打成 `<<MASKED>>`，型態不一致的 bug 就會被掩蓋。結構性欄位內若含有動態子欄位，讓 Masker 遞迴處理子欄位即可，不需要在父層欄位遮罩。

→ 設定見：`config/mask_rules.yaml`；實作見：`core/masker.py`（`02b_harness_code.md`）

---

## 八、共用核心：DiffEngine

DiffEngine 封裝所有比對邏輯，決定了「什麼叫做測試通過」。

### 陣列順序問題

PostgreSQL 在沒有明確 `ORDER BY` 的情況下，回傳順序是未定義的，Java 和 Python 的 ORM 在相同查詢下可能產生不同排序，直接比對陣列會出現偽 fail：

```
expected: [{"id": 1}, {"id": 2}, {"id": 3}]
actual:   [{"id": 2}, {"id": 1}, {"id": 3}]  ← 內容相同但順序不同
```

### 兩層解法

**根本解（推薦）**：確保 Java 和 Python 的 SQL / ORM 都有明確 `ORDER BY`，從源頭消除不確定性。Debug Agent 在遇到 `array_order_only` 類型的 diff 時應優先建議此方向。

**防禦解（Harness 層）**：在 `harness.yaml` 的 `diff_rules.ignore_order_at` 列出允許忽略排序的 JSONPath，DiffEngine 使用 DeepDiff 的 `ignore_order_func` 做精準的路徑級無序比對。

> ⚠️ `ignore_order_at` 的路徑格式必須用 bracket 記法（`root['data']['items']`），不能用 dot 記法（`root.data.items`），否則不會被 DeepDiff 正確匹配。

### 比對結果分類

| 結果 | 含意 |
|---|---|
| `None` | 完全一致，測試通過 |
| `{"type": "array_order_only"}` | 內容相同但有陣列排序差異，需加 `ORDER BY` 或加入 `ignore_order_at` |
| `dict`（deepdiff 格式） | 有實質差異，送入 Reporter 分類後給 Debug Agent |

→ 設定見：`config/harness.yaml`（`diff_rules` 段）；實作見：`core/diff_engine.py`（`02b_harness_code.md`）

---

## 九、Report 格式（供 Debug Agent 使用）

Agent ⑦（Debug Agent）的輸入是結構化的 JSON report，格式設計讓 Claude 能精準定位問題。

### Report 結構

```
{
  summary: { total, passed, failed, pass_rate }
  status: "pass" | "fail"
  failures: [
    {
      case_id          ← 唯一識別失敗的 case
      failure_type     ← 失敗分類（見下表）
      status_code_match
      expected_status / actual_status
      body_diff        ← DeepDiff 格式的詳細差異
      related_files    ← 對應的 Python 原始碼路徑（Debug Agent 直接開這些檔案）
      debug_hint       ← 人類可讀的修正方向
    }
  ]
  passed_cases: [case_id, ...]
}
```

### failure_type 分類

| failure_type | 含意 | debug_hint 方向 |
|---|---|---|
| `status_code_mismatch` | HTTP 狀態碼不符 | 檢查 exception handler |
| `missing_fields` | Response 缺少欄位 | 檢查 Pydantic schema |
| `type_mismatch` | 欄位型別不符 | 檢查 ORM model 型別 |
| `value_mismatch` | 值不符但結構正確 | 檢查業務邏輯 |
| `array_order_only` | 僅陣列排序不同 | 加 ORDER BY |
| `golden_not_found` | 找不到 golden 檔案 | 檢查 route_to_file_mapping |
| `response_not_json` | 回傳非 JSON | 檢查未處理例外 |

→ 實作見：`core/reporter.py`（`02b_harness_code.md`）

---

## 十、Postman Collection 的來源

Harness 依賴兩份 Collection 才能運作，這兩份檔案**由 Agent A + Agent B 自動產生**，不需手動維護。

### Agent A：springdoc-openapi → OpenAPI 3.0

在 Java 專案的 `pom.xml` 暫時加入 springdoc-openapi 依賴，啟動 Java 服務後呼叫 `/v3/api-docs`，取得完整的 OpenAPI 3.0 JSON。springdoc-openapi 自動從 Spring annotation 讀取所有資訊，包括 request/response schema、路徑參數、必填規則等，不需要額外解析原始碼。

### Agent B：OpenAPI → 兩份 Postman Collection

```
openapi.json
    ↓
openapi-to-postmanv2（npm 工具）
    ↓
[LLM] 填入 path parameter 的真實 ID（從 seed.sql 取得存在的 ID）
[LLM] 填入 mutation request body 的範例值
    ↓
按 HTTP method 拆分
    ↓
collection_readonly.json    （只含 GET，零副作用）
collection_mutation.json    （含 POST / PUT / DELETE）
```

Collection 是根據 OpenAPI Spec 自動產生的。若 Java API 有變動，只需重跑 Agent A + Agent B 即可更新，不需手動改 Postman。

---

## 十一、Route Mapping：related_files 的解析機制

`related_files` 讓 Debug Agent 知道要去看哪幾個 Python 檔案，由 `config/harness.yaml` 的 `route_to_file_mapping` 驅動。

**這份 mapping 由 Agent ③（架構設計 Agent）自動產生**，在設計 Python 專案結構的同時一併輸出，寫入 `config/harness.yaml`，不需人工填寫。

### Key 格式

```
{METHOD}_{path_normalized}
```

URL 中的動態段（純數字或 UUID）統一替換為 `{id}`：

```
GET /api/v1/users/123          → GET_api_v1_users_{id}
GET /api/v1/users/123/profiles → GET_api_v1_users_{id}_profiles
```

這樣 `GET_api_v1_users_{id}_profiles` 不會被 `GET_api_v1_users_{id}` 前綴假匹配，巢狀資源能精確對應到正確的 router 檔案。

### 匹配流程

```
實際 URL → normalize（數字/UUID → {id}）
         → 精確匹配 route_to_file_mapping key
         → fallback：前綴匹配
         → 找不到：回傳空清單，Reporter 標記警告
```

→ 設定見：`config/harness.yaml`（`route_to_file_mapping` 段）；實作見：`verifier/comparator.py`（`_normalize_path_key`、`_resolve_related_files`）

---

## 十二、與 LangGraph 的整合

Harness 在 LangGraph 中以兩個 Node 的形式存在，共用底層 `refactor_harness/` 套件。

### Node 分工

```
record_golden_output（Agent ②）
  apply_seed → GoldenRecorder.record(readonly collection)
  → state["golden_output"]

run_postman_tests（Agent ⑥）
  apply_seed → GoldenVerifier.verify(readonly collection)
  → state["test_results"]
```

### 條件邊邏輯

```
run_postman_tests
      ↓
should_debug_or_done()
      ├── status == "pass"          → END（done）
      ├── retry_count >= MAX_RETRY  → END（give_up，通知人工）
      └── 否則                      → debug node → implement node（retry_count + 1）
```

→ 實作見：`langgraph_nodes/test_nodes.py`（`02b_harness_code.md`）

---

## 十三、Task 級別的局部驗證（縮小 Debug 範圍）

Agent ⑤ 是可平行的模組改寫，建議每個 task 完成後只跑「與該模組相關」的 golden cases，而不是等全部完成才跑完整 collection。

> **前提**：每個 task 必須含有 `module` 欄位（對應 `fixtures/golden/` 的子目錄名稱），才能觸發正確模組的局部驗證。`module` 欄位由 Plan Agent 在產生 task list 時填入，格式見主架構文件（`01_refactor_architecture.md`）第八節。

### 兩層驗證架構

```
每個 task 完成
      ↓
【第一層：局部驗證（Node 內部快速循環）】
  只跑此模組的 readonly golden cases
  目的：確認「自己這個 task 沒寫錯」，快速 fail-fast
      ↓
  Fail → 立即回 Agent ⑤ 修正，只帶入此模組 diff（Debug 範圍小）
  Pass ↓
      ↓
【第二層：全量驗證（LangGraph 條件邊放行條件）】
  跑完整 collection_readonly.json 所有模組
  目的：偵測跨模組的 regression（Order 改動是否破壞 User）
      ↓
  Fail → Debug Agent 處理跨模組問題，帶入完整 diff
  Pass → task 真正標記 done，進入下一個 task 或結束
```

**關鍵原則**：局部驗證 pass 只代表「自己沒壞」，全量驗證 pass 才代表「沒有連帶傷害」，兩個條件都滿足才能放行。

→ 實作見：`langgraph_nodes/test_nodes.py`（`partial_verify`）

---

## 十四、測試用 DB 設計（保護原始資料）

### 為什麼不能直接 truncate 原始 DB

既有的 PostgreSQL 裡有初始資料。Harness 在測試前必須清除殘留資料並重新注入 seed，如果直接對原始 DB 操作，會把既有資料洗掉。

Transaction Rollback 在這個情境也不可行：Harness 和 Java / Python 服務是**不同的 process**，Harness 無法介入服務自己的 connection 和 commit。

### 解法：同一台 PostgreSQL，開第二個 Database

```
PostgreSQL Server（同一台）
├── database: myapp        ← 原始資料，Harness 完全不碰
└── database: myapp_test   ← 測試專用，隨便 truncate / seed
```

**一次性建立**（只需執行一次）：

```bash
createdb myapp_test
pg_dump --schema-only myapp | psql myapp_test
```

**讓服務在測試時連到 myapp_test**，只需啟動時覆蓋環境變數，不需要改任何程式碼：

```bash
# Agent ② 啟動 Java 服務時
DB_URL=postgresql://localhost/myapp_test java -jar app.jar

# Agent ⑥ 啟動 Python 服務時
DB_URL=postgresql://localhost/myapp_test uvicorn main:app
```

### DbEnvironment 的職責

- `apply_seed(seed_file, tables_to_truncate)`：先以單一 TRUNCATE CASCADE 清除資料表（避免多次鎖表），再用 `psql -f` 注入 seed（正確支援多條 SQL 語句）
- `sync_schema(source_dsn)`：當 myapp schema 有變動時同步到 myapp_test，migration 後執行一次

**設計原則**：`DbEnvironment` 的建構子只接受 `test_dsn`，無法傳入 production DSN，從程式層面杜絕誤操作。

→ 實作見：`fixtures/db_env.py`（`02b_harness_code.md`）

---

## 十五、Harness 在整體架構中的資料流

```
PostgreSQL Server
├── myapp        （原始 DB，Harness 完全不碰）
└── myapp_test   （測試專用）
        │
        ├── Agent ②（Harness Recorder）
        │       │ apply_seed → truncate + psql seed
        │       │ 啟動 Java 服務（DB_URL=myapp_test）
        │       │ run_newman → capture responses
        │       │ mask dynamic fields
        │       └──→ 寫入 fixtures/golden/
        │
        ├── Agent ⑥（Harness Verifier）
        │       │ apply_seed → truncate + psql seed  ← 同一份 seed
        │       │ 啟動 Python 服務（DB_URL=myapp_test）
        │       │ run_newman → capture responses      ← 同一份 postman_runner
        │       │ mask dynamic fields                 ← 同一份 masker
        │       │ load fixtures/golden/
        │       │ diff compare
        │       └──→ 輸出 report.json
        │
        └── Agent ⑦（Debug Agent）
                │ 輸入：report.json + related_files 的程式碼
                │ 分析 failure_type + debug_hint
                └──→ 輸出修正指令給 Agent ⑤
```

---

## 十六、待實作清單

**OpenAPI → Collection 工具鏈（優先執行）**
- [ ] Java 專案 pom.xml 加入 `springdoc-openapi-starter-webmvc-ui 2.3.0`
- [ ] 啟動 Java 服務，確認 `GET /v3/api-docs` 能正常回傳完整 OpenAPI JSON
- [ ] 安裝 `openapi-to-postmanv2`（npm），確認能將 openapi.json 轉成 Postman Collection
- [ ] 確認 Agent B 填入的 path parameter ID 在 seed.sql 中確實存在（避免 404）
- [ ] 確認 collection_readonly.json 和 collection_mutation.json 都能被 newman 正常執行

**測試 DB 建立**
- [ ] 執行 `createdb myapp_test` 建立測試用 DB
- [ ] 執行 `pg_dump --schema-only myapp | psql myapp_test` 複製 schema
- [ ] 確認 Java 服務可以用 `DB_URL=postgresql://localhost/myapp_test` 正常啟動
- [ ] 確認 Python 服務可以用 `DB_URL=postgresql://localhost/myapp_test` 正常啟動
- [ ] 在 `config/harness.yaml` 填入正確的 `test.dsn`

**Harness 核心**
- [ ] mask_rules.yaml 初版：把 Java 回傳格式中所有動態欄位列出來（只列純量欄位）
- [x] ~~fixtures/seed.sql~~：由你直接提供 PostgreSQL 初始資料
- [ ] newman 安裝與 collection 執行驗證：確認 newman run 可正常輸出 JSON report

**Collection 分組（狀態污染防護）**
- [ ] 確認 Agent B 正確拆分 readonly / mutation 兩份 collection
- [ ] 確認 readonly collection 跑完後 DB 狀態不變
- [ ] Agent ② 的錄製流程補上對 collection_mutation.json 的 golden 錄製
- [ ] 確認 mask_rules.yaml 的 masked_fields 已涵蓋所有寫入回傳的動態 ID 欄位（id、order_id 等）

**Route Mapping（Agent ③ 自動產生）**
- [ ] 確認 Agent ③ 輸出的 route_to_file_mapping 已正確寫入 config/harness.yaml
- [ ] key 格式確認為 `{METHOD}_{path_normalized}`，動態段用 `{id}` 佔位
- [ ] 確認巢狀資源路由有獨立 key，不會被父路由假匹配
- [ ] 確認沒有 API 對應到空的 related_files

**Schema 同步機制**
- [ ] 確認 Python 服務的 Alembic migration 可以直接套用到 myapp_test，或改用 db.sync_schema() 手動同步

**流程整合**
- [ ] MAX_RETRY 設定：建議初期設為 3，後期視情況調整
- [ ] give_up 通知機制：超過重試次數時，Slack / email 通知人工介入

---

*本文件針對「Java → Python Multi-Agent 重構」情境設計，隨實作推進持續更新。*
