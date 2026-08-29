# Harness 架構設計：Java → Python Multi-Agent 重構情境

> 為「Java → Python Multi-Agent 重構」設計的 Test Harness 方案，與 `00_refactor_architecture.md`、`01_langgraph_architecture.md` 的架構決策保持一致。程式碼實作見 `02b_harness_code.md`。

---

## 一、本情境的 Harness 角色定位

Multi-Agent 架構中，Harness **不是一個單獨的 Agent**，而是**橫跨整個流程的基礎設施層**，由兩個現有 Agent 共同承擔：

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

兩端共用同一套 Harness 邏輯，確保「錄製」和「驗證」的標準完全一致，不會因為兩端用不同邏輯而產生偽 fail。

---

## 二、Harness 整體架構

```
refactor_harness/                  ← Python 套件（程式碼）
├── recorder/
│   └── golden_writer.py           （錄製 golden output，Agent ② 使用）
├── verifier/
│   ├── comparator.py              （載入 golden output，執行比對，Agent ⑥ 使用）
│   └── mutation_verifier.py       （比對寫入操作的 response）
├── core/                          ← 共用核心，兩端都用
│   ├── postman_runner.py          （執行 newman + 頂層 folder 列舉）
│   ├── masker.py                  （動態欄位遮罩，readonly / mutation 兩種情境）
│   ├── diff_engine.py             （diff 計算與格式化）
│   ├── route_mapper.py            （route → related_files 解析，兩個 Verifier 共用）
│   └── reporter.py                （產生 JSON report，供 Debug Agent 使用）
├── fixtures/
│   └── db_env.py                  （測試 DB 環境管理，見十四）
└── langgraph_nodes/
    └── test_nodes.py              （LangGraph node 整合，見十二）

（專案根目錄，套件外——完整專案樹見 01 二章）
├── fixtures/
│   ├── seed.sql                   ← 測試 DB 初始資料
│   └── golden/                    ← golden output 存放位置
│       ├── users/
│       ├── orders/
│       └── _metadata.json
└── config/
    ├── harness.yaml                ← harness 設定
    └── mask_rules.yaml             ← 遮罩規則設定
```

> **設計說明**：`postman_runner.py` 統一放在 `core/`，兩端共用同一份，避免日後改了一份漏改另一份造成行為不一致。

> **匯入慣例**：套件內部模組互相引用一律用帶 `refactor_harness.` 前綴的絕對匯入（如 `from refactor_harness.core.masker import ResponseMasker`），與套件外部呼叫方（`graph/builder.py`、`graph/nodes/implement_node.py`、本套件自己的 `langgraph_nodes/test_nodes.py`）的匯入方式一致。

> **額外 Python 依賴**：`01_langgraph_architecture.md` 的核心 `requirements.txt`（`langgraph`／`pyyaml`／`python-dotenv`／`httpx`／`anthropic`，不含 `langchain-anthropic`，見 01 二章）不含 Harness 自己用到的套件，需另外加上 `psycopg2-binary`（`fixtures/db_env.py` 直接連 PostgreSQL 執行 seed）與 `deepdiff`（`core/diff_engine.py` 的比對核心，見八）。

---

## 三、Agent ② — 錄製端（Harness Recorder）

### 職責

對**正在運行的 Java 服務**執行**兩份** Postman collection（readonly ＋ mutation），把每一支 API 的 response 標準化後存成 golden output。兩份都要錄製：若只有 readonly 有 golden，寫入操作（POST/PUT/DELETE）就只能靠 status code 把關，測不出 body 內容是否正確。

### 執行流程

```
① 確認 Java 服務健康（health check）
② 錄製 readonly：apply_seed 一次 → newman 跑完整份 collection_readonly.json
③ 錄製 mutation：逐「頂層 folder」循環——每個 folder 開跑前 apply_seed
   → newman run --folder（reset 粒度與驗證端一致，見六）
④ 捕獲每支 API 的 response
⑤ 標準化（masking）：readonly 用 context="readonly"，mutation 用 context="mutation"（見七）
⑥ 寫入 golden output 檔案（fixtures/golden/{module}/{case_id}.json）
⑦ 兩份 collection 都錄完後，合併寫入一份 _metadata.json（錄製摘要）
```

### case_id 命名規則

golden output 的檔案名稱即 case_id，格式為：

```
{item_name}_{method}_{path}
範例：create_order_POST_api_v1_orders
```

加入 Postman Item Name 作為前綴，確保同一個 endpoint 的多個測試情境（成功/失敗/邊界）不會互相覆蓋。

item name 與 URL path 可能含有檔名非法字元（`:`、`?`、`*`、`/`、空白等），而 case_id 直接作為檔名使用，因此產生時統一將「字母數字、底線、連字號」以外的字元替換為底線。此演算法抽出為 `core/postman_runner.py` 的 `make_case_id()` 單一共用函式，從結構上保證錄製端寫檔、驗證端讀檔用同一套規則——任一端演算法漂移，golden 就永遠找不到。

### 非 JSON Response 與空 Body 的處理

只錄製 `Content-Type: application/json` 的 response。二進位、HTML error page 一律跳過並記錄到 `_metadata.json` 的 `skipped` 清單，供人工確認是否需要額外處理。

Content-Type 是 JSON 但 body 為空（如 `204 No Content` 或部分 DELETE 回應）的情況，golden 明確存成 `body: null`，**不會**被 fallback 成空物件 `{}`——「沒有 body」和「回傳了 `{}`」是兩種不同語意，混為一談會掩蓋「Java 回 204 無 body、Python 誤回 200＋`{}`」這類真實差異。驗證端讀取 actual response 時採用相同規則，兩端對稱。

### Mutation 錄製異常偵測（非預期 Status Code 與鏈式牽連）

`collection_mutation.json` 設計上是「業務情境的快樂路徑」（見六章設計約定），不是拿來測錯誤路徑用的。但 [B] 的 LLM 填值邏輯不保證填出的值一定通過 Java 端的業務規則檢查（唯一鍵衝突、狀態機不允許的操作等）。若不做任何處理，這種情況下 Java 回傳的 4xx/5xx 會被**原樣錄成 golden output**——之後只要 Python 端剛好對同一份輸入也回傳類似的錯誤（不論是正確重現了業務規則，還是恰好用不相干的理由觸發了同一種錯誤），這個 case 就會「通過」，但從未真正測試過原本想驗證的行為。這是與 `03a_spec_collection_agent_architecture.md` 三章「LLM 填值邏輯」同性質的 False Pass 風險，只是發生的位置在錄製端而非填值端。Recorder 因此對 mutation collection 套用以下規則：

**判斷基準**：只在 `record_mutation()`（`context="mutation"`）套用，`record()`（readonly）不受影響——readonly 的 path 參數用的是 `seed.sql` 既有資料（見 `03a_spec_collection_agent_architecture.md` 三章「LLM 填值邏輯」），理論上不會觸發業務規則錯誤，沒有同等風險。若某個 request 的 status code 不落在 `200 <= code < 300`（含 204）範圍內，視為非預期，**不寫入 golden**，改記錄進 `_metadata.json` 的新清單（格式見下）。

> 更精準的判斷基準（依 openapi_spec 宣告的主要成功 response code，而非一律採 2xx）需要 [B] 在 Postman item 上多標註一個欄位才做得到，屬於跨文件的介面擴充，這次不處理，列入十六章待實作清單供未來評估。

**鏈式牽連**：六章的設計約定是「一個頂層 folder＝一條自洽鏈式情境」，folder 內的 request 可能互相依賴（producer 的 response 欄位餵給 consumer）。若 folder 內任何一個 request 觸發上述異常，代表它之後注入的 test script（`pm.environment.set(...)`）很可能沒有正確捕捉到值——error body 通常沒有預期欄位，環境變數最終會被設成字面字串 `"undefined"`——folder 內後續依賴它的 request 即使自己的 status code 正常，也是建立在不可信的前提上。因此**整個 folder 都不寫入 golden**，不細究 folder 內哪些 case 個別正常：這是「頂層 folder＝一條不可分割的情境」這個既有約定的自然延伸，不是新規則。

`_metadata.json` 格式擴充（新增 `tainted_folders`，與既有的 `skipped` 性質相同——都是「Recorder 主動判斷不該當 golden、記錄下來供人工複查」，差別只在 `skipped` 是單一 case、`tainted_folders` 是整個 folder）：

```json
{
  "recorded_at": "...",
  "total_cases": 40,
  "cases": [...],
  "skipped": [...],
  "tainted_folders": [
    {
      "folder": "order_lifecycle",
      "anomalies": [
        {"case_id": "create_order_POST_api_v1_orders", "status_code": 400, "body_preview": "..."}
      ],
      "excluded_case_ids": ["create_order_POST_api_v1_orders", "get_order_GET_api_v1_orders_{id}"]
    }
  ]
}
```

錄製完成後應檢查這份清單是否為空；若非空，通常代表 [B] 的填值邏輯需要調整，而不是 Harness 本身有問題。

→ 實作見：`recorder/golden_writer.py`（`record`／`record_mutation`／`write_metadata`，`02b_harness_code.md`）

---

## 四、Agent ⑥ — 驗證端（Harness Verifier）

### 職責

對**正在運行的 Python 服務**執行相同的兩份 Postman collection（readonly ＋ mutation），把 response 標準化後與 golden output 比對。

### 執行流程（全量驗證）

```
① 確認 Python 服務健康（health check）
② readonly：apply_seed 一次 → newman 跑完整份 collection_readonly.json → 比對
③ mutation：MutationVerifier 逐「頂層 folder」循環（每 folder 前 apply_seed，
   與錄製端 reset 粒度一致，見六）→ 逐 case 比對
④ 標準化（同一份 masking 邏輯；readonly／mutation 各用對應 context，見七）
⑤ 從 fixtures/golden/ 載入對應 golden output
⑥ 執行 diff 比對
⑦ readonly ＋ mutation 的原始結果合併後，輸出單一結構化 report（供 Debug Agent 使用）
   ——只要任一份有 failure，整體 status 即為 fail
```

### 比對入口

`GoldenVerifier`（readonly）：

- `verify(collection_path)`：跑完整 readonly collection 並回傳已分類 report。
- `verify_raw(collection_path)`：同上但回傳未分類的原始結果，供與 mutation 結果合併後統一產 report（見下方注意事項）。
- `verify_module(collection_path, module_filter)`：只比對指定模組的 golden cases。用於 module 級局部驗證（該 module 的所有 task 完成後觸發，見十三）。

`MutationVerifier`（mutation）：

- `verify_all()`／`verify_all_raw()`：逐頂層 folder 執行整份 mutation collection，**自動排除**三章「Mutation 錄製異常偵測」記錄在 `tainted_folders` 的 folder（見下方）。
- `verify_one(folder_name)`：單獨執行某一條鏈式情境（除錯用），**不**自動排除——除錯時可能就是想看某個被標記 tainted 的 folder 實際執行狀況，這裡刻意不套用 `verify_all_raw()` 的排除邏輯。

> **合併時的注意事項**：`build_report()` 只能對「未分類的原始結果」呼叫一次。已分類 report 中 failure 物件的欄位名稱與原始結果不同（如 `status_code_match` vs `status_match`），把已分類物件再丟回 `build_report()` 會因讀不到預期欄位而靜默誤判。因此合併 readonly＋mutation 時一律走 `*_raw()` 收集原始結果、最後統一分類。

> **通過標準**：不論 readonly 或 mutation，單一 case 要同時滿足「status code 相符」與「body diff 為空」才算 `passed`，兩者是 AND 關係，不是只看其中一個——例如 Java 回 404、Python 回 200 但 body 都是空物件的情境，若只看 body diff 會被誤判為通過。

### 排除已知異常的 folder

`MutationVerifier` 初始化時讀取 `fixtures/golden/_metadata.json` 的 `tainted_folders`，取得對應的 folder 名稱清單；`verify_all_raw()` 逐 folder 執行時，若該 folder 在清單內，直接跳過（不執行 newman、不產生任何比對結果）。這是必要的，不是選配：若不跳過，這些 case 會因為找不到 golden，被既有的 `golden_not_found` 分類誤判成失敗——但那不是 Python 端的問題，是這個 case 從錄製當下就沒有被判定為可信的 golden。

`_metadata.json` 不存在時（例如尚未跑過 `record_golden_output`）視為沒有任何 tainted folder，不拋例外——這是正常的初次執行情境，不是異常。

被跳過的 folder 名稱另外收集，交由呼叫端（`run_postman_tests`，見十二章）附加進最終 report 的 `excluded_folders` 欄位（見九章），讓人在看 report 時能一眼看出「這次驗證少測了哪些情境、為什麼」，不會誤以為 `pass_rate` 100% 代表全部涵蓋。

→ 實作見：`verifier/comparator.py`、`verifier/mutation_verifier.py`（`02b_harness_code.md`）

---

## 五、Newman 共用執行器

`core/postman_runner.py` 是兩端共用的 newman 執行入口，確保錄製端和驗證端的執行行為完全一致。

主要職責：
- 呼叫 `newman run`，輸出 JSON 格式報告
- 判斷「執行本身有沒有成功」不是只看 return code：newman 的 exit code 只反映 collection 裡的 test script 斷言是否全部通過，不代表這次執行沒有產生報表——鏈式依賴注入的 capture script 斷言失敗時 exit code 也會非 0，但報表仍完整寫出。真正的硬性失敗判準是「report 檔案不存在，或不是合法 JSON」；exit code 非 0 只記警告，斷言失敗與否交給呼叫端（`GoldenVerifier`／`MutationVerifier`／`GoldenRecorder`）自己比對 response 內容判斷，見 `09b_bug_trace.md` #33
- 對目標服務逾時保護：目標服務可能處於「socket 還開著但沒有真的回應」的異常狀態（如容器內 app 載入時就掛掉，reload watcher 仍在監聽），這種情況下 newman 本身也會卡住不結束。逾時後必須連整棵行程樹一起砍乾淨，不能只砍 Python 直接持有 handle 的那個行程——Windows 上 `newman` 實際執行的是 `newman.cmd`（npm batch wrapper），真正在做事的是它底下的 `node.exe` 子行程，只砍掉 wrapper 砍不掉它，會讓收尾等待仍然沒有真正上限。這是端對端驗證才發現的既有缺陷，見 `09b_bug_trace.md` #34／#36
- 提供共用的 `extract_response_body()`，從 newman 單一 execution 的 `response` 物件取出原始 body 文字——**真實 newman（6.2.2）的 JSON reporter 沒有 `response["body"]` 這個欄位**，內容序列化在 `response["stream"]`（Node.js Buffer 的 JSON 表示），這是端對端驗證才發現的既有缺陷，見 `09b_bug_trace.md` #20、`02b_harness_code.md` 該函式的完整說明。Recorder／Verifier 兩端都必須透過這個共用函式讀 body，不能各自直接存取 `response["body"]`，理由跟這裡「兩端共用同一份執行入口」是同一種精神——任一端漂移就會重演這個 bug

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

**Group B（mutation）**：驗證寫入操作的 response（status code 與 body 結構）。每個 mutation folder 之前都重新 `apply_seed`，保證初始狀態一致。寫入操作回傳的動態 ID 欄位（`id`、`order_id` 等）由 Masker 在 **mutation 情境下**額外抹平（見七、Masker 的兩種遮罩情境），因此可以正常比對 body 的非動態欄位，不會漏掉「Python 偷懶只回傳空物件」這類嚴重問題。

### Seed 策略

| Collection | Seed 時機 | 執行方式 |
|---|---|---|
| readonly | 整個 collection 跑前 seed 一次 | `apply_seed` → `newman run` |
| mutation | 每個**頂層 folder**（＝一條完整鏈式情境）跑前都 seed | 逐 folder 循環：`apply_seed` → `newman run --folder` |

**兩端的 reset 粒度必須完全一致**：錄製端（`GoldenRecorder.record_mutation()`）與驗證端（`MutationVerifier`）都以「頂層 folder 開跑前 `apply_seed` 一次」為準。若錄製端整份 collection 連續跑、驗證端逐 folder reset，兩端每個 request 面對的 DB 初始狀態就不同，比對必然出現偽 fail。

→ 實作見：`recorder/golden_writer.py`（`record_mutation`）、`verifier/mutation_verifier.py`（`02b_harness_code.md`）

### 設計約定（鎖死）：頂層 folder ＝ 一條自洽的鏈式情境

`collection_mutation.json` 的**每一個頂層 folder 必須完全自洽（self-contained）**，這是 Agent B 產生 collection 時必須遵守的硬性約定：

- **一個頂層 folder ＝ 一條完整的情境流程**（例如「建立訂單 → 用回傳 ID 查詢 → 修改 → 刪除」），folder 內的 request 依序執行、共用同一次 newman run 的 Postman Environment。
- **有鏈式依賴的 request 必須放進同一個頂層 folder**，不能拆散到不同 folder——因為每個頂層 folder 開跑前 DB 都會被 `apply_seed` 重置，前一個 folder 建立的資料（與其寫入 environment 的動態 ID）在下一個 folder 開跑時已不存在，跨 folder 引用 `{{order_id}}` 必然打到不存在的資源，產生 404/500 偽失敗。
- **禁止單純按 Controller／API 資源類別拆分頂層 folder**：LLM 產生 collection 時習慣性地「User 一個 folder、Order 一個 folder」，但這種拆法會把「建立 user → 用 user_id 建立 order」的隱性跨資源依賴切斷在兩個 folder 之間，觸發上一條的偽失敗。頂層 folder 必須**依業務情境拆分**（如 `user_order_flow`、`order_lifecycle`），一條情境涉及幾個資源就涵蓋幾個資源。
- **獨立無依賴的寫入操作，其請求參數依賴的外鍵 ID 必須直接引用 `seed.sql` 已預先存在的靜態 ID**：例如單發測試 `POST /orders` 的回應格式時，body 中的 `user_id` 必須填 seed 資料裡確實存在的值，**不可**引用其他 folder 產生的動態 Environment Variable——那個變數在本 folder 的 newman run 裡根本不存在（或殘留著上次執行的過期值），且對應的 DB 記錄已被 `apply_seed` 清掉，會直接撞 FK constraint 錯誤。
- **不同 folder 之間不共用任何動態值**：每個 folder 只能依賴（a）`seed.sql` 靜態灌入的資料、（b）自己 folder 內先前 request 產生的動態 ID，除此之外不可假設任何 DB 狀態。
- **無鏈式依賴的單發寫入操作**（如單獨測 `POST /users` 的回應格式）自成一個只有一個 request 的頂層 folder 即可。

這個約定同時解決了「逐 folder reset」與「鏈式依賴」看似衝突的問題：reset 的單位（頂層 folder）正好就是鏈式依賴的封閉邊界，folder 內部鏈得起來、folder 之間互不污染。**這些規則不能只靠 LLM 自行領悟，必須明文寫進 Agent B 的 prompt 指令**（見十、Agent B）。

### 鏈式依賴的動態 ID 傳遞（對應 00 七/[B]）

`seed.sql` 只能提供**靜態存在**的資料。但部分 mutation case 之間存在鏈式依賴——例如先 `POST /orders` 建立一筆訂單，取得回傳的 `order_id`，再用這個 ID 呼叫 `GET /orders/{id}` 或 `POST /orders/{id}/items`。這種 ID 在 `seed.sql` 灌入當下並不存在，不能靜態填死，必須在 newman 執行期間動態產生、動態傳遞。

**機制**：Agent B 在把 OpenAPI 轉成 Postman Collection 時，針對有鏈式依賴的 folder，於「建立資源」的 request 上注入 Postman **test script**，將 response body 中的 ID 寫入 **Postman Environment Variable**（如 `pm.environment.set("order_id", jsonData.id)`）；後續 request 的 URL / body 改用 `{{order_id}}` 引用這個變數，由 newman 在單次執行內自動代入。**依上方設計約定，這整條鏈必須位於同一個頂層 folder 內。**

```
[頂層 folder: order_lifecycle]            ← apply_seed 在此 folder 開跑前執行一次
  [POST /orders]                          ← test script: pm.environment.set("order_id", jsonData.id)
        ↓ newman 在同一次 run 中把 order_id 寫入 environment
  [GET /orders/{{order_id}}]              ← 引用剛才寫入的變數，不依賴 seed.sql 事先存在
  [DELETE /orders/{{order_id}}]           ← 同一條鏈，仍在同一個 folder 內
```

**關鍵不變量**：錄製（對 Java）與驗證（對 Python）是**兩次各自獨立**的 newman 執行，各自用自己那次執行實際產生的 ID，環境變數的值不會跨執行、跨服務共用或寫死。這也是為什麼即使 Java／Python 的 DB 自增序列產生的實際數值不同（例如 Java 建出 `order_id=101`，Python 建出 `order_id=1`），鏈式請求依然能正確打到「這次執行實際建立」的資源，不會出現 404。

> 這個機制只發生在 `collection_mutation.json` 內：`collection_readonly.json` 全是唯讀請求，沒有「建立資源」這一步，因此不需要動態變數傳遞，直接用 `seed.sql` 既有的固定 ID 即可。

---

## 七、共用核心：Masker

兩端使用**完全相同**的 masker，這是確保比對公平的最關鍵設計。

### 遮罩機制

Masker 遞迴走訪整個 response body，對符合條件的欄位替換為 `<<MASKED>>`：

- **欄位名稱比對**（`masked_fields`）：timestamp、token、request_id 等動態純量欄位
- **值的 Pattern 比對**（`masked_patterns`）：ISO datetime 字串、UUID、Bearer token，`re.match` 整個字串值，符合就把整個值換成 `<<MASKED>>`
- **值內子字串 Pattern 比對**（`masked_value_substring_patterns_mutation_only`，見下方「第三種遮罩機制」）：只替換字串裡匹配到的一小段，不是整個值

### 兩種遮罩情境

`mask(obj, context)` 依情境套用不同的欄位規則，**同一個 case 的錄製端與驗證端必須用同一個 context**：

| context | 套用規則 | 適用對象 |
|---|---|---|
| `readonly`（預設） | 只套用 `masked_fields` | `collection_readonly.json` 的錄製與比對 |
| `mutation` | `masked_fields` ＋ `masked_fields_mutation_only`（`id`、`order_id`、`user_id`、`randomId` 等主鍵／動態識別碼欄位）＋ `masked_value_substring_patterns_mutation_only` | `collection_mutation.json` 的錄製與比對 |

**為什麼要分情境**：mutation 剛建立的資源，其自增主鍵在 Java／Python 兩端本來就不會相同（見六、關鍵不變量），逐值比對沒有意義，遮掉才能比對其餘欄位。但 readonly 的精確查詢（如 `GET /users/1`）完全相反——回傳的 `user_id` **就應該是 1**，這裡的 ID 是查詢正確性的一部分；若沿用同一套規則把它也遮掉，「Python JOIN 錯欄位、撈到別人的資料」這種最嚴重的 bug 會顯示為 PASS。`randomId` 適用同一套理由：`candidate/generateRandomId` 每次呼叫都用 `CodeUtil.generateRandomCode()` 現生一組隨機碼（真實環境重跑才發現，見 `docs/09b_bug_trace.md`「golden 對隨機欄位無法比對」），golden 錄製當下的值必然跟每次重新驗證時不同，只在 mutation（剛建立、值本來就會變）情境才遮，不是通用地永遠遮罩。

### 第三種遮罩機制：值內子字串替換（`masked_value_substring_patterns_mutation_only`）

`masked_fields`／`masked_patterns` 都假設「動態值本身就是一個完整的欄位值」，但真實案例（`voice` 上傳成功訊息，`docs/09b_bug_trace.md`「golden 對隨機欄位無法比對」）打破了這個假設：隨機碼被包在一段更長的訊息字串裡（如 `"上傳成功: C:\voice\2025\macuhau\TAA\xMpIV\1_1.wav"`），隨機碼既不是獨立欄位（欄位名稱比對擋不住），也不是整個字串值（`masked_patterns` 的 `re.match` 全字串比對若硬套用，會連同前後固定的訊息文字一起吃掉，遮罩範圍過大）。這裡改用 `re.sub()`，只替換 pattern 匹配到的那一小段子字串，保留字串其餘內容。只在 `mutation` 情境套用，理由同 `masked_fields_mutation_only`。

**pattern 設計不能只靠值的長度／字元集判斷**：`voice` 案例的隨機碼定義是 5 個字元、字元集 `[A-Za-z0-9]`（見目標專案 `CodeUtil.CODE_LENGTH`／`CHARACTERS`）——第一版 pattern 只判斷「前後都是路徑分隔符的 5 碼英數字段」，實測發現這樣會連 `"voice"` 這個固定路徑關鍵字本身（剛好也是 5 個字母）一起誤遮，因為它在路徑裡的位置一樣「前後都是分隔符」。**修法**：改成錨定訊息裡更獨特的結構特徵——這個訊息格式裡，隨機碼固定緊接在檔名（`{partNumber}_{questionNumber}.{副檔名}`，如 `1_1.wav`）前面一個路徑分隔符，用零寬 lookahead 只匹配「後面緊接著這個檔名形狀」的那一段，不誤傷路徑裡其他固定關鍵字。這是撰寫這類子字串 pattern 時的通用教訓：優先錨定訊息裡**唯一、不會跟固定文字混淆**的結構特徵，不要只靠隨機值本身的長度或字元集猜測邊界。

### 重要限制

`masked_fields`／`masked_fields_mutation_only` **只放純量型態（string / number）的動態欄位**。若把值為陣列或物件的欄位放進來，Java 和 Python 兩端無論回傳何種型態都會被打成 `<<MASKED>>`，型態不一致的 bug 就會被掩蓋。結構性欄位內若含有動態子欄位，讓 Masker 遞迴處理子欄位即可，不需要在父層欄位遮罩。

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
      module           ← 所屬 module，供 Debug Agent 依 module 分組（見 core/reporter.py、10a_debug_agent_architecture.md 二章）
      failure_type     ← 失敗分類（見下表）
      status_code_match
      expected_status / actual_status
      body_diff        ← DeepDiff 格式的詳細差異
      related_files    ← 對應的 Python 原始碼路徑（Debug Agent 直接開這些檔案）
      debug_hint       ← 人類可讀的修正方向
    }
  ]
  passed_cases: [case_id, ...]
  excluded_folders: [folder_name, ...]   ← 見「excluded_folders 欄位」一節，預設空陣列
  excluded_cases: [case_id, ...]         ← 見「excluded_cases 欄位」一節，預設空陣列
}
```

### failure_type 分類

| failure_type | 含意 | debug_hint 方向 |
|---|---|---|
| `status_code_mismatch` | HTTP 狀態碼不符 | 檢查 exception handler |
| `missing_fields` | Response 缺少欄位 | 檢查 Pydantic schema |
| `type_mismatch` | 欄位型別不符 | 檢查 ORM model 型別 |
| `value_mismatch` | 值不符但結構正確 | 檢查業務邏輯 |
| `body_presence_mismatch` | 一端沒有 body（null）另一端有 | 檢查 204 vs 200+body 的回傳設計 |
| `array_order_only` | 僅陣列排序不同 | 加 ORDER BY |
| `golden_not_found` | 找不到 golden 檔案 | 檢查 route_to_file_mapping |
| `response_not_json` | 回傳非 JSON | 檢查未處理例外 |

### 合併範圍

全量驗證的 report **同時涵蓋 readonly 與 mutation 兩份 collection 的 case**（合併機制見四）。`summary`／`status`／`failures` 的結構不變，Debug Agent 不需要區分來源即可處理。route 解析邏輯統一放在 `core/route_mapper.py`（`RouteMapper`），`GoldenVerifier` 與 `MutationVerifier` 共用同一份，mutation failure 的 `related_files` 能正常解析。

### excluded_folders 欄位

Report 頂層另有一個 `excluded_folders` 欄位（預設空陣列），列出因三章「Mutation 錄製異常偵測」被判定為 tainted、整個 folder 未參與這次驗證的情境（見四章「排除已知異常的 folder」）。這些 case **不計入** `summary` 的統計，也不出現在 `failures`——它們既不是通過也不是失敗，是「這次沒測」。`excluded_folders` 非空時代表 [B] 的填值邏輯有 case 需要人工複查，通常應優先處理，而不是放著等它自己消失。

→ 實作見：`core/reporter.py`（`02b_harness_code.md`）

### excluded_cases 欄位

**這是端對端驗證（見 `09b_bug_trace.md` #21）才發現、原設計沒有涵蓋的缺口，這裡補上定案**：三章「非 JSON Response 與空 Body 的處理」已經定案 Recorder 只錄製 `Content-Type: application/json` 的 response，非 JSON（如 `/version` 這類回傳 `text/plain` 的端點）一律跳過、記進 `_metadata.json` 的 `skipped` 清單，不寫入 golden——但原設計沒有交代**驗證端**該怎麼處理這批端點：這些端點仍然在 Postman Collection 裡，Newman 照樣會打，`GoldenVerifier` 找不到對應 golden 檔案，若不特殊處理，會被 `_process_executions()` 判定成 `golden_not_found` 失敗——但那不是「golden 遺失」或「route_to_file_mapping 設定錯誤」，是這個端點從一開始就不在 JSON body diff 這種比對契約的適用範圍內，不該算失敗。

**解法**：`GoldenVerifier` 初始化時讀取 `_metadata.json` 的 `skipped` 清單（跟 `MutationVerifier` 讀 `tainted_folders` 是同一種機制、同一份檔案），比對時命中這份清單的 case 直接排除，不產生 `golden_not_found` 結果，改記進 report 頂層新增的 `excluded_cases` 欄位（預設空陣列）——跟 `excluded_folders` 是同一種精神（"這次沒測"，不是失敗，也不計入 `summary`），差別只在 `excluded_folders` 是 mutation 端以「整個頂層 folder」為單位排除，`excluded_cases` 是 readonly 端以「單一 case」為單位排除。

→ 實作見：`verifier/comparator.py`、`core/reporter.py`（`02b_harness_code.md`）

---

## 十、Postman Collection 的來源

Harness 依賴兩份 Collection 才能運作，這兩份檔案**由 Agent A + Agent B 自動產生**，不需手動維護。

### Java 專案的本地擺放位置

Agent ①（讀取原始碼）與 Agent A（啟動服務、打 `/v3/api-docs`）都只需要一個**檔案系統路徑**（對應 `01_langgraph_architecture.md` 三章 State 的 `java_project_path`），Java 專案不需要在 `refactor-project/` 這個 Python repo 內部。建議：

- **與 `refactor-project/` 同層、各自獨立的資料夾**，不要巢狀塞進 `refactor-project/` 裡面——Java 專案是另一個完整的 Maven repo（自己的 `.git`、`.gitignore`、`target/`），混在一起容易讓兩邊版控規則互相打架。
- **複製一份專門給這次重構用，不要直接指向你正在維護的正式專案原始位置**：下方 Agent A 一節會修改 Java 專案的 `pom.xml`，這個改動依 00 五章定案是**永久保留**、不是產完 Collection 就還原，直接對正式專案動手風險較高。

```
test/2026/
├── refactor-project/          ← Python orchestrator（01）
└── lang-exam-api-refactor/    ← Java 專案的複製版，專門給這次重構用
```

`java_project_path` 可以填相對路徑（如 `"../lang-exam-api-refactor"`）或絕對路徑；`main.py` 組裝 `initial_state` 時讀 `.env` 的 `JAVA_PROJECT_PATH`，不寫死在程式碼裡，見 00 五章「環境建立」。

### Agent A：springdoc-openapi → OpenAPI 3.0

在 Java 專案的 `pom.xml` 加入 springdoc-openapi 依賴（依 00 五章定案**永久保留**，不是產出 Collection 後就移除），啟動 Java 服務後呼叫 `/v3/api-docs`，取得完整的 OpenAPI 3.0 JSON。springdoc-openapi 自動從 Spring annotation 讀取所有資訊，包括 request/response schema、路徑參數、必填規則等，不需要額外解析原始碼。

Java 專案目前是 **Spring Boot 2.7.11**，對應加入：

```xml
<dependency>
    <groupId>org.springdoc</groupId>
    <artifactId>springdoc-openapi-ui</artifactId>
    <version>1.7.0</version>
</dependency>
```

> ⚠️ **artifact id／版本要對應 Spring Boot 大版本，兩者不可混用**：Spring Boot 3.x（Jakarta EE 9、Java 17+）要用 `springdoc-openapi-starter-webmvc-ui` 2.x；Spring Boot 2.x／1.x 要用上面這個 `springdoc-openapi-ui`，v1.7.0 是最後一版支援 2.x 的 OSS 版本。用錯版本不只是「裝不上」，而是能安裝成功、啟動時才炸——症狀是 `UnsupportedClassVersionError`（依賴的 class file 版本高於目前 JRE 能解析的版本），因為 3.x 系列是用 Java 17 編譯的。若之後這個 Java 專案升級到 Spring Boot 3.x，要記得把這個依賴也換回 `springdoc-openapi-starter-webmvc-ui` 2.x 系列。
>
> 加完依賴後需要重新 `mvn package` 打包出新的 jar：00 五章定案的啟動方式是直接 `java -jar` 執行已打包的 jar、不含 Maven build 這一步，舊 jar 裡不會有這個新加的依賴。打包完啟動服務，先手動 `curl http://localhost:8080/v3/api-docs` 確認能拿到完整 JSON，這是 Agent A/B 能不能運作的先決條件，建議在讓 Claude Code 開始跑 02 之前就先驗證過一次。

### Agent B：OpenAPI → 兩份 Postman Collection

```
openapi.json
    ↓
openapi-to-postmanv2（npm 工具）
    ↓
[LLM] 填入 path parameter 的真實 ID（從 seed.sql 取得存在的 ID）
[LLM] 填入 mutation request body 的範例值
[LLM] 依「業務情境」組織 mutation 的頂層 folder（見下方 prompt 必要指令）
    ↓
按 HTTP method 拆分
    ↓
collection_readonly.json    （只含 GET，零副作用）
collection_mutation.json    （含 POST / PUT / DELETE）
```

**Agent B prompt 的必要指令**（對應六章設計約定）——這些規則違反時 Harness 會產生大量偽失敗且難以歸因，必須明文寫進 prompt，不能期待 LLM 自行推導：

1. `collection_mutation.json` 的頂層 folder **禁止**單純按 Controller／資源類別拆分（不可「User 一個 folder、Order 一個 folder」），必須依業務情境拆分（如 `user_order_flow`）；一條情境內所有有依賴關係的 request（含跨資源者，例如先 `POST /users` 再用 `{{user_id}}` `POST /orders`）必須放在**同一個**頂層 folder。
2. 獨立無依賴的寫入操作，request body／path 中的外鍵 ID 必須引用 `seed.sql` 中確實存在的靜態 ID，**禁止**引用其他 folder 的 Environment Variable。
3. 有鏈式依賴的 folder，在「建立資源」的 request 注入 test script（`pm.environment.set(...)`），後續 request 用 `{{變數}}` 引用（機制見六章）。
4. 動態 ID 不可寫死進 Collection JSON 檔案本身——錄製（Java）與驗證（Python）是兩次獨立的 newman 執行，各自產生各自的 ID（關鍵不變量見六章）。

Collection 是根據 OpenAPI Spec 自動產生的。若 Java API 有變動，只需重跑 Agent A + Agent B 即可更新，不需手動改 Postman。

### 開發順序上的先後矛盾：Recorder/Verifier 的輸入天生比自己晚完成

`GoldenRecorder`／`GoldenVerifier`／`MutationVerifier` 都需要吃 Agent A/B 產出的 Collection 才能執行，但 Agent A/B 通常不會比 Harness 本身早完成——這不是特例，是這條依賴鏈結構上的必然。實作／驗證 Harness 時不需要等 Agent A/B 就緒：手刻一份只含 1-2 個 endpoint 的最小 Collection（照本章的格式即可，不需要 Agent B 的 LLM 填值與鏈式依賴邏輯），就足以驗證 `apply_seed` → `run_newman` → mask → 寫入/比對 golden output 這條路徑是否正確；等 Agent A/B 完成後，換上正式產生的完整 Collection 即可，不需要改動 Harness 任何程式碼。

---

## 十一、Route Mapping：related_files 的解析機制

`related_files` 讓 Debug Agent 知道要去看哪幾個 Python 檔案，由 `config/harness.yaml` 的 `route_to_file_mapping` 驅動。

**這份 mapping 由 Agent ③（架構設計 Agent）自動產生**，在設計 Python 專案結構的同時一併輸出，寫入 `config/harness.yaml`，不需人工填寫。

> `config/harness.yaml` 同一次由③寫入時，還有一個**共用下面同一套 key 格式**的姊妹表 `route_to_module_mapping`，供 `get_module()`（十三章）判斷 module 分區用——差別只在值：`route_to_file_mapping` 存 `related_files` 清單，`route_to_module_mapping` 存單一 `module` 字串（即 `ApiMapping.module`）。兩者是同一次機械組裝的兩個輸出，不是各自獨立的產出流程，細節見 `05a_design_agent_architecture.md` 八章。

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
         → fallback：前綴匹配（取「最長（最精確）」的 pattern，見下方說明）
         → 找不到：回傳空清單，Reporter 標記警告
```

> **fallback 前綴匹配須取最長 pattern**：若某條路由沒有被 Agent ③ 精確列在 `route_to_file_mapping` 裡（fallback 才會被觸發的前提），可能同時有多個既有 key 都是該 URL 的前綴（例如 `GET_api_v1_users` 和 `GET_api_v1_users_{id}` 都是 `GET_api_v1_users_{id}_profiles_photos` 的前綴）。此時必須取字串最長、也就是最精確的那個 pattern，不能依「哪個先出現在 `harness.yaml`」決定，否則命中結果會隨 yaml 撰寫順序而變、可能挑到較不精確的 mapping，跟本節「避免巢狀資源被前綴假匹配」的目的矛盾。

→ 設定見：`config/harness.yaml`（`route_to_file_mapping` 段）；實作見：`core/route_mapper.py`（`RouteMapper.normalize_path_key`、`RouteMapper.resolve_related_files`，`02b_harness_code.md`）

---

## 十二、與 LangGraph 的整合

Harness 的底層套件 `refactor_harness/` 在 LangGraph 圖中被三處呼叫：`record_tests`／`run_tests` 兩個獨立 node，以及 `implement` node **內部**呼叫的 module 級局部驗證（見十三章）。三處共用同一套 `GoldenVerifier`／`Masker`／`DiffEngine`，差別只在「跑哪份 collection、比對哪些 module」。

### Node 分工

```
record_tests（Agent ②，對應 record_golden_output）
  GoldenRecorder.record(collection_readonly.json)          ← 內部 apply_seed 一次
  GoldenRecorder.record_mutation(collection_mutation.json) ← 內部逐頂層 folder apply_seed
  write_metadata(兩份結果合併)
  → state["golden_output"]（含 readonly / mutation 兩份摘要）

implement（Agent ⑤，內部呼叫，非獨立 node，見十三章 + 01 六章）
  module 底下所有 task 完成 → apply_seed → GoldenVerifier.verify_module(module_filter)
  → 累加進 state["partial_reports"]（含 regression 重驗結果）

run_tests（Agent ⑥，對應 run_postman_tests，全量驗證）
  health check：先確認 Python 服務連線層級可達（見四章「執行流程」步驟①、
    10a_debug_agent_architecture.md 二章）
    不可達 → 直接回傳 status="fail" 的空 report（不執行下面的 apply_seed／Newman），
             並把 python_service 容器的診斷 log 存進 state["service_diagnostics"]
             供 Debug Agent 使用
  apply_seed → GoldenVerifier.verify_raw(collection_readonly.json)
  MutationVerifier.verify_all_raw()                        ← 內部逐頂層 folder apply_seed，自動排除 tainted_folders（見四、九）
  build_report(readonly_raw + mutation_raw, excluded=mutation_verifier 的排除清單)
  → state["test_results"]（含 excluded_folders／excluded_cases 欄位），
    並落地寫入 logs/report_{run_id}.json 供 Debug Agent 讀取（見 02b test_nodes.py）
```

> `record_golden_output`／`run_postman_tests` 是 01 四章沿用的 node 名稱（`record_tests`／`run_tests`）；module 級局部驗證不是獨立 node，是 `implement` node 內部依排程觸發的呼叫，不出現在圖的節點清單中。

### 條件邊邏輯

```
run_tests
      ↓
should_debug_or_done()
      ├── test_results.status == "pass"   → END（done）
      ├── retry_count >= MAX_RETRY          → give_up node（通知人工，非直接 END）
      └── 其餘情況                          → debug node → implement node（retry_count + 1）
```

**retry_count 上限對所有分支統一套用，不再依 `failed_modules` 是否非空分岔**：`failed_modules` 只反映 ⑤ 局部驗證（僅 readonly，見 `09a_implement_agent_architecture.md` 三章）看不看得到問題，不是「這一輪有沒有真的壞」的可靠依據；能讓 debug 被觸發，`test_results.status == "fail"` 已經是 ⑥ 全量驗證（readonly＋mutation）確認過的事實，`retry_count` 上限必須照樣檢查——否則只有 mutation 或跨模組 regression 才會出現的 bug，會因為 `failed_modules` 永遠是空的而讓 `debug ↔ implement` 迴圈沒有上限地跑下去。這條規則推翻了本章原本「`blocked_modules` 不消耗重試次數」的設計，完整失效場景與理由見 `10a_debug_agent_architecture.md` 一章「本文件推翻的一項既有假設」、七章。`give_up` 是獨立節點（見 01 五章），保留通知人工的落點，不是直接跳 `END`。

→ 完整條件邊判斷邏輯與 `blocked_modules`／`failed_modules` 的區分見 `01_langgraph_architecture.md` 五、六章；node 對應的實作程式碼見 `02b_harness_code.md`。

---

## 十三、Module 級別的局部驗證（縮小 Debug 範圍）

Agent ⑤ 逐 task 呼叫 translator-cli 寫入單一函式，但**驗證不是逐 task 觸發**，而是在「該 module 底下所有 task 都已完成（成功或失敗）」時才跑一次該模組的 golden cases——驗證觸發時機是「整個 module 全部完成」而非「每個 task 完成」，避免依賴鏈未接完（例如 repository 已完成但 service／router 還沒寫）就誤觸發驗證、產生大量假失敗。實作對應 `01_langgraph_architecture.md` 六章的 `ModuleScheduler`。

> **前提**：每個 task 必須含有 `module` 欄位（對應 `fixtures/golden/` 的子目錄名稱）；`module` 欄位由 [P] Plan Agent 在產生 task list 時填入，逐字沿用 `ModuleInfo.module`（原因見 `06a_plan_agent_architecture.md` 四章），格式見主架構文件 `00_refactor_architecture.md` 第八節。
>
> **module 詞彙表的權威來源**：golden 子目錄名稱由 `core/postman_runner.py` 的 `get_module()` 決定——優先查 `config/harness.yaml` 的 `route_to_module_mapping` 段（③ 產出，見 `05a_design_agent_architecture.md` 八章，key 正規化方式與 `route_to_file_mapping` 相同，值即 `ApiMapping.module`），查得到就採用；查不到（該 endpoint 落在①③解析範圍外，如 04a 十一章列出的已知限制，或屬於 skip 呼叫鏈已排除的端點）才 fallback 回「URL 第一個非版本路徑段」的字串猜測，並記警告。fallback 路徑保留，只是不再是唯一機制（十六章「Module 詞彙一致性」待實作清單已同步更新）。

### 兩層驗證，觸發時機不同

```
每個 task 完成
      ↓
  僅觸發 translator-cli 內建的語法驗證（AST parse）
  確認寫入沒有破壞語法——不觸發任何 API 級測試
      ↓
（同一 module 底下的 task 陸續完成……）
      ↓
該 module 的所有 task 都完成
      ↓
【第一層：局部驗證（Module 完成後的快速循環）】
  只跑此模組的 readonly golden cases
  目的：確認「這個 module 沒寫錯」，快速 fail-fast，Debug 範圍限縮在單一模組
      ↓
  Fail → 標記該 module 為 failed，帶著此模組 diff 進入 `debug ↔ implement` 迴圈，
  由 ⑦ Debug Agent 分析並直接寫出修正（⑤ 本地模型不再參與這類已驗證失敗
  task 的重試，見 10a「known_fill_failures」一節）
  Pass → 標記該 module 為 verified，其依賴此 module 的下游 module 才會被排入就緒佇列
      ↓
【第二層：全量驗證（LangGraph 條件邊放行條件）】
  跑完整 collection_readonly.json 所有模組
  目的：偵測跨模組的 regression（例如 Order 模組的改動是否波及 User）
      ↓
  Fail → Debug Agent 處理跨模組問題，帶入完整 diff
  Pass → 整體流程放行、結束
```

### Regression 重驗：不是只有「第一次驗證」這一種局部驗證

module 被標記 `verified` 之後，若後續其他 task 寫入的檔案剛好落在這個已驗證 module 名下（如 debug 迴圈回頭修 repository，而該檔案同時被 Order／User 共用），該 module 會被打回 `needs_reverify`，強制重新跑一次局部驗證，而不是放著等到全量驗證才發現。這讓「跨模組 regression」不必等到最後一步才被抓到，能更早、更精確地定位是哪次改動造成的。

**關鍵原則**：局部驗證 pass 只代表「這個 module 目前沒壞」；一旦有新的改動觸及已驗證 module 的檔案，該 module 需要重新過局部驗證才能維持 `verified` 狀態；全量驗證 pass 才代表「整體沒有連帶傷害」。三個條件都滿足才能放行進入下一階段。

→ 排程與觸發邏輯的具體實作（`ModuleScheduler`、`MODEL_SEMAPHORE`、`check_upstream_regression`）見 `01_langgraph_architecture.md` 六章；Report 與比對邏輯本身見本文件三～九章。

---

## 十四、測試用 DB 設計（保護原始資料）

> 以下範例使用實際專案 `MOC_MATSUEXAM`（測試 DB 為 `MOC_MATSUEXAM_TEST`），環境變數命名對齊 `00_refactor_architecture.md` 五章的定案（`TEST_DB_DSN`／`SPRING_DATASOURCE_URL`）。

### 為什麼不能直接 truncate 原始 DB

既有的 PostgreSQL 裡有初始資料。Harness 在測試前必須清除殘留資料並重新注入 seed，如果直接對原始 DB 操作，會把既有資料洗掉。

Transaction Rollback 在這個情境也不可行：Harness 和 Java / Python 服務是**不同的 process**，Harness 無法介入服務自己的 connection 和 commit。

### 解法：同一台 PostgreSQL，開第二個 Database

```
PostgreSQL Server（同一台）
├── database: MOC_MATSUEXAM        ← 原始資料，Harness 完全不碰
└── database: MOC_MATSUEXAM_TEST   ← 測試專用，隨便 truncate / seed
```

**一次性建立（前提是 Java 不是用 `ddl-auto` 自動建表——見下方「Schema 來源」）**：

```bash
createdb MOC_MATSUEXAM_TEST
pg_dump --schema-only MOC_MATSUEXAM | psql MOC_MATSUEXAM_TEST
```

**讓服務在測試時連到 `MOC_MATSUEXAM_TEST`**，只需啟動時覆蓋環境變數，不需要改任何程式碼，兩服務用的變數名與格式不同：

```bash
# Agent ② 啟動 Java 服務時：Spring Boot 環境變數覆蓋，JDBC 格式，不改 application.properties
SPRING_DATASOURCE_URL=jdbc:postgresql://127.0.0.1:5432/MOC_MATSUEXAM_TEST \
SPRING_DATASOURCE_USERNAME=postgres SPRING_DATASOURCE_PASSWORD=password \
java -jar app.jar

# Agent ⑥ 啟動 Python 服務時：FastAPI + SQLAlchemy 統一讀 DATABASE_URL（psycopg2/asyncpg 格式）
DATABASE_URL=postgresql://postgres:password@127.0.0.1:5432/MOC_MATSUEXAM_TEST uvicorn main:app
```

> Harness（`DbEnvironment`）本身連線用 `TEST_DB_DSN`，值與 `DATABASE_URL` 相同、但用途不同——`TEST_DB_DSN` 是 Orchestrator／Harness 自己拿來 truncate／seed 用的，`DATABASE_URL` 是 Python 服務進程自己讀的環境變數，兩者只是剛好指向同一顆 DB（見 01 三章 State 設計的註記）。
>
> **`TEST_DB_DSN` 的實際傳遞路徑**：`config/harness.yaml` 裡 `databases.test.dsn` 只是 fallback 用的靜態預設值。正常執行路徑上，`TEST_DB_DSN` 由 `01` 九章 `main.py` 讀進 `state["test_dsn"]`，Recorder（`GoldenRecorder`）與 Verifier（`DbEnvironment`、`MutationVerifier`）建構時都由呼叫端傳入這個值（見 `langgraph_nodes/test_nodes.py`），不會各自去讀 yaml 的靜態值——這樣才能保證錄製與驗證兩端、以及 readonly／mutation 兩條驗證路徑，全程連的是同一顆測試 DB。

### Schema 來源（已確認，見 00 五章）

`MOC_MATSUEXAM_TEST` 建立時是一顆空白 DB，`seed.sql` 只灌資料、不建表，schema 必須先準備好。目標專案的資料表是**手動 SQL 建的，不是 Hibernate/JPA `ddl-auto` 自動建表**（00 五章「環境建立」已確認），因此固定走「需要手動同步」這條路徑：

```bash
pg_dump --schema-only MOC_MATSUEXAM | psql MOC_MATSUEXAM_TEST
```

（若日後接上其他用 `ddl-auto` 建表的 Java 專案，[A] Spec Agent 第一次啟動服務、連到空白的 `_TEST` DB 時會自動建好 schema，不需要上面這個步驟——但這不是目前目標專案的情況，這裡先記錄成通用備註，不代表這個專案還要判斷。）

### DbEnvironment 的職責

- `apply_seed(seed_file, tables_to_truncate)`：以**單一交易**執行 TRUNCATE CASCADE（單一指令避免多次鎖表）＋ seed 注入（純 psycopg2，不依賴 psql 命令列工具；seed 失敗時整體 rollback，不會留下「已清空但沒灌資料」的中間態）。**限制**：`seed.sql` 必須是標準 SQL，不能含 psql meta-command（`\copy`、`\i` 等）或 `COPY FROM stdin` 格式
- `sync_schema(source_dsn)`：當 `MOC_MATSUEXAM` schema 有變動（或屬於上表「Flyway/Liquibase／手動 migration」情況需要初次同步）時同步到 `MOC_MATSUEXAM_TEST`，migration 後執行一次（此方法仍依賴 `pg_dump`／`psql` 外部工具）

**設計原則**：`DbEnvironment` 的建構子只接受 `test_dsn`（對應 `TEST_DB_DSN`），無法傳入 production DSN，從程式層面杜絕誤操作。

→ 實作見：`fixtures/db_env.py`（`02b_harness_code.md`）

---

## 十五、Harness 在整體架構中的資料流

```
PostgreSQL Server
├── MOC_MATSUEXAM        （原始 DB，Harness 完全不碰）
└── MOC_MATSUEXAM_TEST   （測試專用，TEST_DB_DSN 指向這裡）
        │
        ├── Agent ②（Harness Recorder）
        │       │ 啟動 Java 服務（SPRING_DATASOURCE_URL=jdbc:postgresql://.../MOC_MATSUEXAM_TEST）
        │       │ readonly：apply_seed 一次 → run_newman（整份 collection）
        │       │ mutation：逐頂層 folder（apply_seed → run_newman --folder）
        │       │ mask dynamic fields（readonly / mutation 各用對應 context）
        │       └──→ 寫入 fixtures/golden/ ＋ 合併寫一份 _metadata.json
        │
        ├── Agent ⑥（Harness Verifier）
        │       │ 啟動 Python 服務（DATABASE_URL=postgresql://.../MOC_MATSUEXAM_TEST）
        │       │ readonly：apply_seed 一次 → run_newman   ← 同一份 seed / postman_runner
        │       │ mutation：逐頂層 folder（apply_seed → run_newman --folder）
        │       │ mask dynamic fields                       ← 同一份 masker、同一個 context
        │       │ load fixtures/golden/
        │       │ diff compare
        │       └──→ readonly + mutation 原始結果合併 → 輸出單一 report.json
        │
        └── Agent ⑦（Debug Agent）
                │ 輸入：report.json + related_files 的程式碼
                │ 分析 failure_type + debug_hint
                └──→ 輸出修正指令給 Agent ⑤
```

---

## 十六、待實作清單

> 以下項目已隨 ①～⑦ 各 Agent 的實作與真實環境端對端驗證陸續完成，本節僅保留仍未落地的項目；已完成項目的細節見對應章節與程式碼本身，不在此重複列出佐證。

**Mutation 錄製異常偵測（見三章、四章、九章）**
- [ ]（優先度較低）更精準的預期 status 判斷基準：需 [B] 在 Postman item 標註 openapi 宣告的成功 response code，屬 `03a`/`03b` 與本文件的介面擴充，待評估

**流程整合**
- [ ] give_up 通知機制：超過重試次數時，Slack / email 通知人工介入（目前 `graph/nodes/give_up_node.py` 僅印出訊息，見該檔案內的 TODO）

---

*本文件針對「Java → Python Multi-Agent 重構」情境設計，隨實作推進持續更新。*