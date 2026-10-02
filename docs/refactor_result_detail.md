# 重構結果（詳細版）：⑤ 從「本地模型讀標記翻譯」改成「Claude API 照呼叫鏈＋Java 原始碼翻譯」

> 這份是給需要查細節的人看的（模組名、逐輪數字、設計規則）。只想先弄懂「改了什麼、結果如何」，請看 `refactor_result.md`。兩份內容結論一致，討論後新增的結論會同時寫進兩份。
>
> 資料截止 2026-09-07 最後一次完整真實 pipeline 重跑（`run_id=20260907_031142_fef1be`）；數字來源是 `logs/reports/*/*.json`、`logs/report_{run_id}.json`、`logs/llm_traces.db`，統計時間 2026-10-02。設計細節見 `00_refactor_architecture.md`、`refactor_plan.md`，bug 追蹤見 `refactor_bug_trace.md`，本文件只記錄「為什麼改、改成什麼、結果如何、還剩什麼」。

---

## 開發時長（預估）

**預估總計約 372 小時（約 46 個 8 小時工作天）**，期間 2026-07-21 ～ 10-02（73 天），共 47 個 Claude Code session。

### 算法（依本人自述工時為準）

| 項目 | 天數 | 每天時數 | 小時 |
|---|---|---|---|
| 約 2 週的密集期（週一到週五 9:00～23:00） | 10 個平日 | 14 | 140 |
| 其餘有作業的平日（一般工時） | 25 個平日 | 8 | 200 |
| 週末（折算為 4 個工作天） | 4 天 | 8 | 32 |
| **合計** | | | **372** |

平日天數取自對話紀錄中有作業的 35 個平日（扣掉 10 個密集期平日後剩 25 天）。其餘平日以一般工時 8 小時估、週末每個折算工作日以 8 小時估，是假設值，實際可由本人調整。

### 各階段分配

各階段時數是按對話紀錄裡各階段所佔的比例，把 372 小時分配下去（對話紀錄顯示 A 約 42.6%、B 約 28.4%、C 約 7.2%、D 約 21.8%）：

| 階段 | 期間 | 對話內作業時數 | 分配後預估時數 | 內容 |
|---|---|---|---|---|
| 設計並做出各個步驟 | 07-21 ～ 08-18 | 96.6 小時 | **約 159 小時** | 00～09 各 Agent 的設計文件（a）與程式實作文件（b）、translator-cli、骨架生成等 |
| 第一版真實驗證與除錯 | 08-19 ～ 08-28 | 64.3 小時 | **約 106 小時** | 端對端真實重跑、⑦ Debug Agent、各項 bug 修正、文件與程式對齊（qwen 時期） |
| 第二版（Claude + 便條） | 08-29 ～ 08-31 | 16.3 小時 | **約 27 小時** | ⑤ 改用 Claude API（`claude_client.py`） |
| 第三版（呼叫鏈＋Java 原始碼） | 09-01 ～ 10-02 | 49.5 小時 | **約 81 小時** | 呼叫鏈範圍查找、三層全域關卡、bug #1～#46、文件逐份同步、成果報告 |

### 對照：純對話紀錄的算法（下限）

讀取專案的 Claude Code 對話紀錄（`~/.claude/projects/` 下 47 個 session），把所有 session 的訊息時間戳合併，相鄰兩則間隔 ≤ 30 分鐘者累計，超過 30 分鐘視為休息，得到約 **226.6 小時**。其中週六、週日 8 天（週六 5 天、週日 3 天）約 45.2 小時（約 20%），平日 35 天約 181.4 小時。

這個數字是下限：不含離線思考、閱讀文件、在別處的工作，也不含沒開 Claude Code 的時間；反過來，它包含 AI 在跑 pipeline、等待測試結果的時間（例如 08-19 ～ 08-21 一個 session 就累計 22 小時，大部分是監看長時間重跑）。

### 其他限制
- 第三版這一段含非重構的附帶工作（成品專案 `exam-platform-api` 的 README、Docker、CORS、安全標頭、上傳參數除錯等）；用文字比對抓到與該專案相關的時段約 19.5 小時，是上限估計，實際較少。
- 07-21 之前若有構想或準備工作，沒有紀錄可算。

---

## 一、結論

1. **舊做法的瓶頸是「標記」本身，不是模型大小。** 舊流程由 Claude 管架構／設計，再由 `[P]` 產生 `description`／`context`／`referenced_interfaces` 這層 LLM 語意標記，交給本地模型（qwen）照標記翻譯各函式。標記不穩定、會遺漏或扭曲 Java 原始碼裡的真實資訊，因此最終通過率落在 0%～42%，沒有任何一次進到 50% 以上。
2. **把 qwen 換成 Claude API 但保留標記，沒有解決問題**：通過率變成 12%～64% 大幅震盪（11 輪裡最高 64.0%，7 輪 ≤ 28%；另有 1 輪 88.5% 經人工手動修改，已剔除不計），要靠 3 輪、中位數約 146 次的 ⑦ Debug 修正才撐得起來——換更強的模型只是放大了「標記好不好」的運氣成分。
3. **新做法把「標記」降級成只剩順序與座標**（`phase`／`translator_backend`／`reference_targets`，全部機械產生，`[P]` 不再呼叫任何 LLM），由三層全域關卡（Phase 1 → service → controller/router）決定翻譯順序，Claude API 直接讀「呼叫鏈 + 完整 Java 原始碼」翻譯。通過率最高的三次為 **80.0%／78.6%／69.2%（平均 75.9%）**，階段二最高三次平均為 61.9%，階段三高約 14 個百分點；階段二那三次平均需要約 213 次 ⑦ Debug 修正，階段三只需約 7 次，重試輪數從 3 降到 1。
4. **實務上 Claude API 已經是 100% 的最終產出者**：設計上 qwen 仍保留給 repository 層（20 個 task），但最近 2 輪 20 次 qwen 呼叫 **0 次成功**（錯誤＋逾時），全部退回 Claude 才完成。
5. **還沒解決的**：最新一輪的 5 個失敗 case 全部在 `file` 模組（voice／image 上傳與讀取）；通過率最高 80%，離全部通過仍有差距；Claude 輸入 token 用量約為舊流程的 2.5 倍。見六、七章。

---

## 二、三個階段對照

「標記」指 `[P] plan_agent` 產出的 `description`／`context`／`referenced_interfaces`（LLM 生成的任務語意摘要）。

| | 階段一：qwen + 標記 | 階段二：Claude + 標記（過渡） | 階段三：Claude + 呼叫鏈＋Java 原始碼（現行） |
|---|---|---|---|
| 期間（有完整測試結果的 run） | 2026-08-23 ～ 08-29 | 2026-08-31 ～ 09-01 | 2026-09-04 ～ 09-07 |
| 分支 | master | `claude-api-implement-agent` | `refactor-call-chain-implement` |
| Claude 負責 | 解析、設計、規劃、Debug | 解析、設計、規劃、**⑤ 翻譯**、Debug | 解析、設計、**⑤ 翻譯**、Debug；規劃改為純機械 |
| qwen 負責 | ⑤ 全部函式本體 | 無 | 設計上僅 repository（實際 0 成功，全數退回 Claude） |
| ⑤ 的翻譯依據 | `[P]` 的 `description`／`context`／`referenced_interfaces` | 同左（標記未動） | 呼叫鏈機械展開的 Java／已翻譯 Python 原始碼（`java_method_id`＋`reference_targets`） |
| `[P]` 是否呼叫 LLM | 是 | 是 | **否**（純機械） |
| 翻譯順序控制 | module 內 repository → service → router | 同左 | **全域三層關卡**（`graph/scheduler.py::_global_tier()`） |
| 重試上限（`MAX_RETRY`） | 3 | 3 | 1 |
| 函式總數 | 71 | 71 | 79（新增繼承而來的 repository 讀取方法等） |

### 2.1 三個階段的結構圖（含模組）

三張圖共用的前後段：[A] Spec Agent 取得 OpenAPI → [B] Collection Agent 產生 Postman Collection → ② 錄製 Java 的 golden output。下面只畫差異所在的中段。

**階段一：qwen + 標記**
```
① parse_agent        Claude  解析 Java，產生模組清單
   ↓
③ design_agent       Claude  設計 Python 專案結構與函式簽名
   ↓
[P] plan_agent       Claude  替每個函式產生 description／context／
   ↓                         referenced_interfaces（語意標記）   ← 失真來源
④ scaffold_agent     機械    依 ③ 產生空骨架
   ↓
⑤ implement_node     qwen    讀標記，填入每個函式本體（translator_cli/ollama_client.py）
   ↓                         排序：module 內 repository → service → router
⑥ 測試執行           機械    對 Python 服務跑 Postman，比對 golden
   ↓ 沒通過
⑦ debug_agent        Claude  分析並修補，最多 3 輪（MAX_RETRY=3）
```

**階段二：Claude + 標記（過渡）** —— 只換 ⑤ 的後端，其餘不變
```
① parse_agent → ③ design_agent → [P] plan_agent（仍產生語意標記）→ ④ scaffold_agent
   ↓
⑤ implement_node     Claude  讀同一份標記，填入每個函式本體（translator_cli/claude_client.py）
   ↓
⑥ 測試執行 → 沒通過 → ⑦ debug_agent（Claude，最多 3 輪）
```

**階段三：Claude + 呼叫鏈＋Java 原始碼（現行）**
```
① parse_agent        Claude  解析 Java；機械建立呼叫圖
   ↓
③ design_agent       Claude  設計結構；多輸出 java_method_id／java_index
   ↓
[P] plan_agent       機械    不呼叫 LLM：標記 phase／translator_backend，
   ↓                         走呼叫圖算 reference_targets（只有座標，不寫語意標記）
④ scaffold_agent     機械    依 ③ 產生空骨架
   ↓
⑤ implement_node     Claude  graph/java_source_extraction.py 把座標讀成 Java／已翻譯 Python 原始碼
   │                         graph/scheduler.py 三層全域關卡，上一關全部到終態才放行下一關：
   ├─ 第 1 關  entity／dto（機械產生）、repository（設計上 qwen，實際退回 Claude）、utils
   ├─ 第 2 關  service        （可直接讀第 1 關已翻好的 Python）
   └─ 第 3 關  controller／router（可直接讀第 2 關已翻好的 Python）
   ↓
⑥ 測試執行 → 沒通過 → ⑦ debug_agent（Claude，最多 1 輪，MAX_RETRY=1）
```

---

## 三、為什麼要改：問題診斷

### 3.1 標記是失真來源

多次真實 pipeline 重跑並逐一 diff 兩次執行的完整 prompt／response 後確認：`[P]` 產生的語意標記本身不穩定，⑤ 即使模型很強，也會因為「拿到的資訊本身不完整或不準確」而翻錯。正確的錯誤碼數值、API 呼叫慣例、欄位名稱只存在於 Java 原始碼裡，任何摘要步驟都可能遺漏或扭曲它。完整診斷見 `refactor_call_chain_implement_prompt.md`。

### 3.1.1 階段一 → 階段二的原因

階段一（qwen + 標記）的有效 run 共 7 輪，通過率 0%～42.3%（最高三次平均 29.5%），其中 5 輪低於 15%；3.3 的分層實測也顯示 qwen 在 entity／utils 層會出現 fatal 錯誤或隱蔽邏輯 bug。因此第一步是只替換「翻譯者」：⑤ 的後端從 qwen 換成 Claude API（`translator_cli/claude_client.py`，分支 `claude-api-implement-agent`），標記維持不變，用來檢驗瓶頸是不是模型能力。

### 3.1.2 階段二 → 階段三的原因

1. **成績沒有穩定，只是變成賭運氣**（見 3.2）：11 輪裡最高 64.0%、7 輪 ≤ 28%，最高三次平均需約 213 次 ⑦ 修正。
2. **根因定位在標記**：逐一 diff 兩次執行的完整 prompt／response，差異來自 `[P]` 產生的 `description`／`context`／`referenced_interfaces`——LLM 生成的摘要每次不同、會遺漏或扭曲 Java 原始碼細節。
3. **在舊架構上修補沒有收斂**：追查 #74 時對標記過濾規則（`referenced_interfaces` 的 nominal／structural typing）一再調整，同類問題換形式重現；該批修補（`plan_agent/planning.py` 型別相容性過濾、`java_service.py` port 衝突偵測等）全部捨棄，專案退回乾淨的 commit（`3f8c811`）後新開分支 `refactor-call-chain-implement`。
4. **因此改變輸入來源而非繼續調整標記**：⑤ 不再吃摘要，改吃呼叫鏈上的完整 Java 原始碼；並加上三層全域關卡，讓後一層可直接讀前一層已翻好的 Python（見四章）。

### 3.2 階段二證明「換模型救不了標記」

階段二只把 ⑤ 的後端從 qwen 換成 Claude API，標記不動。結果不是穩定變好，而是變成賭運氣：

- 08-31 當天（同一個 commit 基底）的有效 run 裡出現 64.0%、57.1%，也出現 12.5%～27.9% 的 7 輪。
- **剔除一輪**：`20260831_032716_294015`（88.5%）經人工手動修改過，結果不是 pipeline 單獨跑出來的，不列入任何統計（含通過率、⑦ 修正次數）；它的 Claude 花費仍列入 5.5（API 費用實際發生過）。

### 3.3 分層實測：哪一層適合 qwen

`refactor_plan.md` 五章用真實案例逐層比較（驗證方式：`TestClient` 直連測試 DB，對比 golden 或 `psql`／Java 實際執行結果）：

| 層級 | 實測 | 結果 | 決定 |
|---|---|---|---|
| repository | 8 個方法（JPQL／native SQL `@Query`、三種回傳形狀、2～4 欄位 `And`） | qwen **8/8 零缺陷** | 留給 qwen |
| entity／model | 4 次獨立翻譯 | qwen **4/4 出現 fatal 錯誤**（幻覺 import、覆蓋 `Base` 等，每次不同） | 不交給模型（機械產生） |
| utils | 5 個 class、約 10 個方法 | qwen 多數正確，但 3 個隱蔽邏輯 bug（`chr('A'+int)`、regex 雙反斜線、`set()` 不保序），語法驗證抓不到 | Claude API |
| service／controller | 性質同 entity，未單獨測 | — | Claude API |

但這個分層結論是單點實驗，沒有在完整流程裡維持：同樣 20 個 repository task，qwen 呼叫成功數從 09-05 14:31 的 19/20 一路降到 09-07 的 0/20（見 5.3），原因沒有查證。

---

## 四、新做法

### 4.1 標記只剩順序與座標

| 舊標記（LLM 生成） | 新標記（機械產生） |
|---|---|
| `description`：業務語意摘要 | `description`：機械組出的翻譯目標描述 |
| `context`：補充說明 | `context`：僅 `@Value` 設定注入提示 |
| `referenced_interfaces`：LLM 判斷該看哪些檔案 | `reference_targets`：BFS 走①的呼叫圖＋③的 `java_index` 算出的座標，上限 20，截斷時標 `reference_targets_truncated` |
| —— | `phase`（1／2）、`translator_backend`（qwen／claude）、`java_method_id` |

`[P]`（`plan_agent/call_chain.py`）只界定「看哪裡」，⑤（`graph/java_source_extraction.py`）才把座標讀成原始碼文字：task 自己的 Java 方法讀整個檔案（真實專案一檔一 class），`reference_targets` 以整檔為主、超過 13000 bytes 才裁成單方法；Python 側用 `ast.get_source_segment()` 精確抽取。

### 4.2 三層全域關卡取代 module 內分層排序

```
Phase 1：entity / dto / repository / utils（全專案）   ← 全部到終態才釋放下一關
service（全專案）                                      ← 全部到終態才釋放下一關
controller / router（全專案）
```

呼叫鏈遇到「更基礎一關」的 callee 直接讀已翻譯的真實 Python，不再往下展開 Java；同 module 同層才展開 Java 並設上限。因此不需要同層排程順序保證機制，也不需要呼叫圖環偵測。

### 4.3 後端分工

- Claude API：解析、設計、Debug、⑤ 的 utils／service／controller。
- qwen2.5-coder:32b：設計上只負責 repository；`OLLAMA_MODEL_SEMAPHORE(1)` 序列化，Claude task 可平行，寫入段共用 `WRITE_LOCK`。
- entity／dto：完全不呼叫模型。
- qwen 呼叫失敗（錯誤或逾時）會自動退回 Claude，報告的「本地模型呼叫失敗、退回 Claude 才成功」欄位記錄 task 清單。

---

## 五、量化結果

### 5.1 通過率與 Debug 依賴度

通過率＝最後一輪 ⑥ 全量驗證（readonly＋mutation）通過 case 數／總 case 數。只統計有跑到 ⑥ 的 run（排除 scaffold 失敗、未進測試的 run）。

| | 階段一 | 階段二 | 階段三（09-05 14:31 起） |
|---|---|---|---|
| 有效 run 數 | 7 | 11（已剔除人工修改的 1 輪） | 7 |
| 通過率範圍 | 0% ～ 42.3% | 12.5% ～ 64.0% | 36.0% ～ 80.0% |
| **最高三次通過率** | 42.3%／38.5%／7.7% | 64.0%／64.0%／57.7% | 80.0%／78.6%／69.2% |
| **最高三次平均** | **29.5%** | **61.9%** | **75.9%** |
| 這三次各自的 ⑦ 修正次數 | 6／276／104（平均 129） | 192／188／260（平均 213） | 4／6／10（平均 6.7） |
| 通過率 ≥ 60% 的 run | 0 / 7 | 2 / 11 | 6 / 7 |
| 通過率 ≥ 80% 的 run | 0 / 7 | 0 / 11 | 1 / 7 |
| 重試輪數 | 1～3 | 3 | 1 |
| ⑦ 修正次數（所有有效 run 的中位數） | 104 | 146 | 14 |

**成功率的統計方式**：每個階段只取通過率最高的三次再平均（不統計全部 run 的平均與中位數），反映「該做法最好能做到多少」。階段二已剔除經人工手動修改的 88.5% 那一輪（`20260831_032716_294015`）。階段三最高三次平均 75.9%，比階段二的 61.9% 高約 14 個百分點，而且階段二那三次平均需要約 213 次 ⑦ 修正、3 輪重試，階段三約 7 次、1 輪重試。

階段三之前還有 4 輪 bring-up run（09-04 ～ 09-05 中午：34.6%、0%、0%、34.6%），當時新機制剛接上、仍在修基礎設施 bug（見 `refactor_bug_trace.md`），不列入上表統計。

### 5.2 階段三逐輪紀錄

| run_id | 通過 / 總數 | 通過率 | ⑦ 修正次數 | 退回 Claude 的 qwen task |
|---|---|---|---|---|
| 20260905_143141_a2adf3 | 9 / 25 | 36.0% | 46 | — |
| 20260905_161715_4b2116 | 15 / 25 | 60.0% | 26 | — |
| 20260906_060838_e867f4 | 18 / 26 | 69.2% | 10 | — |
| 20260906_093519_f564a7 | 11 / 14 | 78.6% | 6 | 11 |
| 20260906_124553_c898a9 | 15 / 25 | 60.0% | 18 | 12 |
| 20260906_154606_b00447 | 15 / 25 | 60.0% | 14 | 14 |
| 20260907_031142_fef1be | 20 / 25 | 80.0% | 4 | 15 |

### 5.3 qwen 在完整流程裡的實際表現

最近 5 輪的 qwen（`ollama_client.get_function_body`）呼叫：

| run | qwen 呼叫 | 成功 | Claude 翻譯呼叫 |
|---|---|---|---|
| 20260906_060838_e867f4 | 20 | 3 | 81 |
| 20260906_093519_f564a7 | 20 | 3 | 78 |
| 20260906_124553_c898a9 | 20 | 1 | 78 |
| 20260906_154606_b00447 | 20 | 0 | 80 |
| 20260907_031142_fef1be | 20（15 error + 5 timeout） | **0** | 80 |

79 個函式在最後一輪全部由 Claude 產出。qwen 失敗的根因（模型輸出格式、連線、逾時）沒有逐一查證，報告只記錄「退回 Claude」這個事實。

### 5.4 Claude token 用量（⑤ 翻譯＋⑦ 分析，同一 run_id 內的 `claude` 呼叫）

| run | Claude 呼叫 | 輸入 token | 輸出 token |
|---|---|---|---|
| 20260831_032716_294015（階段二最佳） | 72 | 261,272 | 10,099 |
| 20260901_034859_eaa00f（階段二） | 71 | 256,575 | 9,183 |
| 20260906_060838_e867f4（階段三） | 84 | 628,521 | 18,544 |
| 20260907_031142_fef1be（階段三最新） | 81 | 654,214 | 16,085 |

階段三輸入 token 約為階段二的 2.5 倍（每個 task 都帶完整 Java 原始碼與呼叫鏈參照），輸出約 1.6～1.8 倍。階段二 run 的 parse／design／plan 呼叫沒有統一記錄在同一個 `run_id` 下，所以這個表只比較 ⑤＋⑦ 的部分，不是整條 pipeline 的總成本；整條的金額見 5.5。

### 5.5 Claude API 花費（美金與台幣）

**計價依據**：`llm_traces.db` 顯示全部 4,181 筆 Claude 呼叫都是 `claude-sonnet-4-6`，單價 **輸入 $3.00／輸出 $15.00 每百萬 token**，所有呼叫的 prompt cache 讀寫 token 皆為 0（沒有用到 prompt caching，所以不打折）。台幣以 **1 USD ≈ 32 TWD** 概算。

**累計（所有已記錄的 Claude 呼叫，2026-08-21 ～ 09-07）**：輸入 26,483,973 token、輸出 2,333,279 token，合計 **US$114.45 ≈ NT$3,662**。其中 `adhoc_*` 的 run_id（解析、設計、規劃、除錯等階段在 run_id 串接完成前的呼叫，以及驗證腳本與實驗）佔 US$79.11。

| 階段（依 `caller` 歸類） | 呼叫數 | 美金 | 台幣 |
|---|---|---|---|
| ⑤ 翻譯（`claude_client.get_function_body`／`thread.run`） | 1,890 | $31.34 | NT$1,003 |
| ⑦ Debug 分析（`analysis.*`） | 305 | $32.96 | NT$1,055 |
| ① 解析（`summarize.*`） | 668 | $20.10 | NT$643 |
| ③ 設計（`design.*`／`global_infra`） | 515 | $12.17 | NT$389 |
| [P] 規劃（`planning.*`，階段三已不呼叫 LLM） | 254 | $11.60 | NT$371 |
| [B] 鏈式依賴偵測等（`chain_dependency_detect`／`folder_grouper`） | 543 | $5.97 | NT$191 |
| 其他（驗證腳本） | 6 | 約 $0.3 | 約 NT$10 |

**依階段（日期）累計**：

| 階段 | 期間 | Claude 花費 | 說明 |
|---|---|---|---|
| 階段一 | 08-21 ～ 08-29 | $28.85 ≈ NT$923 | ⑤ 由 qwen 翻譯（不計費），Claude 只負責解析、設計、規劃、除錯 |
| 階段二 | 08-31 ～ 09-01 | $44.07 ≈ NT$1,410 | ⑤ 改用 Claude；一天內跑了約 10 輪完整流程加上大量除錯，是花費最高的階段 |
| 開發空窗 | 09-02 ～ 09-03 | $2.35 ≈ NT$75 | 新設計開發中的零星呼叫 |
| 階段三 | 09-04 ～ 09-07 | $39.18 ≈ NT$1,254 | 含 bring-up 期間多輪失敗重跑 |

**三個階段「跑一次完整流程」的預估花費**：

算法：階段期間內所有 Claude 呼叫的總花費 ÷ 該期間「完整跑完翻譯」的次數（同一 `run_id` 內翻譯呼叫 ≥ 40 筆者）。總花費包含少量實驗與驗證腳本，因此略偏高。

| | 階段一 | 階段二 | 階段三 |
|---|---|---|---|
| 期間 | 08-21 ～ 08-29 | 08-31 ～ 09-01 | 09-04 ～ 09-07 |
| 期間 Claude 總花費 | $28.84 | $44.08 | $39.17 |
| 完整輪數 | 14 | 14 | 13 |
| **一輪預估（美金）** | **約 $2.06** | **約 $3.15** | **約 $3.01** |
| **一輪預估（台幣，×32）** | **約 NT$66** | **約 NT$101** | **約 NT$96** |
| 　⑤ 翻譯 | $0（qwen，不計費） | $0.95 | $1.39（含 bring-up 中途失敗的輪；最近 5 輪穩定值約 $2.2） |
| 　⑦ Debug | $0.73 | $1.25 | $0.40 |
| 　①③[P][B] 上游 | $1.31 | $0.95 | $1.22（最近 2 天降到約 $0.7，因為 `[P]` 不再呼叫 LLM） |
| 最高三次平均通過率 | 29.5% | 61.9% | 75.9% |

階段三若只看最近穩定的幾輪，⑤ $2.2 + ⑦ 約 $0.35 + 上游約 $0.7 ≈ **$3.25（約 NT$104）**，與整段平均 $3.01 差不多，所以對外統一說「約 NT$100」。

**判讀**：階段一最便宜（約 NT$66），因為翻譯交給免費的本地模型，但最高三次平均通過率只有約 30%。階段三把 ⑤ 的單輪成本抬高約 2 倍，但 ⑦ 除錯的呼叫大幅減少，兩者抵銷後一輪的花費與階段二相近（約 NT$100）；同樣約 NT$100，最高三次平均通過率從約 62% 提高到約 76%，取得該成績所需的 ⑦ 修正也從約 213 次降到約 7 次。

**這份估算的限制**：
- 上游階段與 ⑦ 的呼叫在 run_id 串接完成前記在 `adhoc_*` 底下，無法精確歸屬到單一輪，因此各項拆分是「期間總額 ÷ 輪數」的均攤值。
- 失敗的呼叫（錯誤、逾時）沒有回傳 token 數，以 0 計，實際可能略低估。
- 2026-08-21 之前沒有呼叫記錄；`llm_traces.db` 之外的用量（例如平常使用 Claude Code 工具、此外的手動實驗）不在內。
- 匯率為概算假設，實際金額依當時匯率與帳單為準。

---

## 六、剩餘落差

### 6.1 最新一輪的 5 個失敗 case

全部在 `file` 模組；其餘 module 20 / 20 通過。

| case | 失敗類型 | 預期 / 實際 |
|---|---|---|
| `voice_1_GET_api_file_voice` | status_code_mismatch | 200 / 404 |
| `image_1_GET_api_file_image` | status_code_mismatch | 200 / 404 |
| `voice_POST_api_file_voice` | value_mismatch | 200 / 200 |
| `voice_1_鏈式依賴驗證__GET_api_file_voice` | status_code_mismatch | 200 / 404 |
| `image_1_鏈式依賴驗證__GET_api_file_image` | status_code_mismatch | 200 / 404 |

這批 case 依賴檔案系統狀態（上傳後讀取），尚未逐一確認每一個是翻譯錯誤還是容器內路徑／環境差異。

### 6.2 golden 測試測不到、在實際輸出專案才暴露的落差

把產出的 `exam-platform-api` 接上真實前端後，手動發現並補上以下項目（只修在輸出專案，**pipeline 本身尚未涵蓋**）：

| 落差 | Java 端來源 | 狀態 |
|---|---|---|
| 缺 CORS | `SecurityConfig.java` | 已手動補 |
| 缺安全 response header（CSP、`X-Frame-Options`、`X-Content-Type-Options`） | `GlobalSecurityHeaderFilter.java` | 已手動補 |
| 未處理例外沒有 log | `GlobalExceptionHandler.java` | 已手動補 |
| `voice()` 上傳參數只接受 query string，前端用 multipart form 就失敗 | Spring `@RequestParam` 同時接受 query 與 form | 已手動補（Query 優先、fallback 讀 form） |

這四項說明 Postman golden 比對的盲區：它只驗證「被錄製的那種呼叫方式」的 response，驗證不到前端實際可能用的其他呼叫方式，以及 filter／middleware 這類不在 controller 方法裡的行為。重構原則是「重構前前端傳什麼能接，重構後也要能接」，目前沒有機制自動檢查這點。

---

## 七、取捨與未解項目

| 項目 | 現況 |
|---|---|
| 通過率上限 | 階段三最高 80%（階段二最高 64.0%，不含人工修改的那一輪），離全部通過仍有差距；階段二最高三次平均需約 213 次修正、3 輪重試，階段三最高三次平均只需約 7 次修正、1 輪重試 |
| 重試輪數 | `MAX_RETRY` 從 3 降為 1（能修就該在第一輪修出來，不靠多跑幾輪碰運氣）；階段三的數字是在較少 Debug 機會下取得的 |
| 成本 | ⑤ 輸入 token 約 2.5 倍、翻譯段單輪約貴 2 倍；換來的是 ⑦ 修正次數與重試輪數大幅下降，一輪預估：階段一約 NT$66、階段二約 NT$101、階段三約 NT$96～104，階段二與三相近（見 5.5）；累計約 NT$3,700 |
| qwen 的定位 | 設計上保留給 repository，實測近兩輪 0/20；是否乾脆拿掉 qwen 路徑（`refactor_plan.md` 六章待評估項）尚未決定 |
| 測試案例數不一致 | 各 run 的總 case 數從 14 到 44 不等（08-31 有 7 輪為 43～44、09-06 有 1 輪為 14），通過率只適合看趨勢，不適合跨 run 比較絕對值 |
| 程式碼版本不一致 | 各階段的 run 之間 pipeline 本身持續在修 bug，統計是「各階段期間的實際成績」，不是控制變因的 A/B 實驗 |
| 輕量驗證閘門 | Phase 1 task 只做語法驗證，「值層級」邏輯 bug 要等 module 級 API 驗證才看得到；是否升級重量閘門待評估 |
| give_up 通知 | 仍只 `print()`，沒有 Slack／email |

---

## 八、心得（附本專案的實例）

### 8.1 關於 AI 改程式會遇到的問題

1. **專案越大，文件越容易落後。** 程式已經改過，文件還停在舊的樣子；再照落後的文件去改就會改錯。
   - 實例：本次逐份核對 `00`／`01`／`02a` 時發現，`01` 內嵌的 `graph/state.py` 快照缺了 `phase`、`translator_backend`、`reference_targets`、`java_index`、`verified_modules` 等十多個欄位；內嵌的 `graph/scheduler.py` 缺了 `_global_tier()`，展示的程式碼根本編不過；內嵌的 `main.py` 沒有 #28／#29／#31 的備份還原包裝；`02a` 還把 ⑦ 畫成「輸出修正指令給 ⑤」（已被取代的舊設計）。
2. **一次誤改，會長出下一個 bug（改 A 壞 B）。** 中間修 bug 時改錯，後面的修改又建立在這個錯誤上，引發別種問題。
   - 實例（`refactor_bug_trace.md`、`09b_bug_trace.md`）：#1 新加的全域 Phase 關卡與既有的 module 依賴排程各自合理，合在一起卻互相等待、死結；#2 `_backfill_missing_task_deps()` 自動串鏈不分後端，一個 qwen task 失敗就連坐拖垮同 module 的 Claude task；#54 `run_postman_tests()` 用 `**state` 回傳，使掛 reducer 的欄位每次完成就翻倍（一筆失敗記錄膨脹成 128 筆，report 肥到 941KB）；#41 `debug ↔ implement` 無法終止，累積到 `MemoryError`。
3. **設計模式的核心想法仍然適用：重複的邏輯要抽成共用區塊、用參數化處理。** 多份各自維護，遲早漏改其中一份。
   - 正面：`00` 六章的共用工具（`llm_client`、`concurrency`、`chunking`、`openapi_ref_resolver`、`java_type_mapping`、`java_annotations`、`jpa_base_repository`）都是「兩個以上 Agent 需要同一份邏輯」才抽出來的。
   - 反面：`design_agent/route_mapping.py::normalize_path_key()` 必須與 `RouteMapper.normalize_path_key()` 逐位元一致，`graph/scheduler.py` 與 `translator_cli/scaffold.py` 各自維護一份 `_LAYER_PREFIX`——這類「靠人記得同步」的重複，是下一個改 A 壞 B 的候選。

### 8.2 工程師在這裡面的角色

4. **AI 走錯方向時，要靠工程師拉回來。** 及早發現可以在更大問題發生前修正，或直接協助 AI 找出根源。
   - 實例：階段二到階段三。追查 #74 時 AI 一直在標記過濾規則上補（nominal／structural typing），同類問題換形式重現；工程師判斷「方向本身有問題」，整批修補捨棄、改成不用標記（見 3.1.2）。
5. **AI 很會做「小區塊」，「大架構」仍要工程師指方向。** 
   - 實例：階段三最新一輪 ⑤ 的單函式翻譯，非 `file` 模組的 20 個 case 全數通過，區塊級的能力很穩；但全域三層關卡、「`[P]` 界定範圍／⑤ 讀取原始碼」的分工、哪一層給 qwen 哪一層給 Claude，這些架構決定都是討論後由人拍板，且跨機制的衝突（如上面的 #1）是真實重跑才現形。
6. **工程師的直覺常比 AI 準，能防止 AI 亂猜，讓問題被準確定位與呈現。**
   - 實例：`voice()` 上傳 API，AI 依既有的 bug 紀錄推論「後端只收 query string 是對的」；工程師拿出真實前端的 Network 截圖，並提出「重構前前端傳什麼能接，重構後也要能接」的原則，推翻了這個推論，最後改成 query 與 form 兩種都接。另一例是階段二那次 88.5%：它是人工手動修改過的結果，這件事只有工程師知道，pipeline 紀錄看不出來——沒人指出的話，會被誤當成該做法的真實能力。

### 8.3 結論

7. **全自動 AI 重構，時間成本太高，也做不到 100%。** AI 會在某些點缺乏相關知識或判斷錯誤，導致後面連貫都做不成功。
   - 參考：一次完整真實測試（舊流程）實測約 2.5 小時；階段二最高三次平均 62%，仍需約 213 次 ⑦ 修正與 3 輪重試。
8. **AI 無法自己 loop 出完全正確的答案；方向歪了，只會越 loop 失敗率越高。** 工程師介入、質疑方向與方法、從根本解決，才切得中更核心的問題。
   - 實例：修正次數最多的一輪（`20260905_071212_ff9298`，456 次）通過率 0%；另一輪（`20260827_104547_f29491`，216 次）也是 0%。次數多不等於收斂，方向錯時更多次嘗試只是在錯的地方打轉。

**目前的結論：資深工程師與 AI 協作才能發揮最大效益。與其追求完美的全自動，不如逐步導正，讓每一步的地基都是最穩的，長起來才不會歪。**

---

## 九、如何重現這些數字

```bash
# 每輪 run 的摘要（通過率、⑦ 修正次數、qwen 退回清單）
ls logs/reports/*/*.md
# 最後一輪 harness 失敗明細
python -c "import json;d=json.load(open('logs/report_20260907_031142_fef1be.json',encoding='utf-8'));print(d['summary']);[print(f['case_id'],f['failure_type']) for f in d['failures']]"
# 某一輪 qwen／Claude 翻譯呼叫分佈：查 logs/llm_traces.db 的 llm_traces 表（vendor、caller、status、run_id）
python -m llmlog recent --status error --since 7d
```

---

## 十、相關文件

| 文件 | 內容 |
|---|---|
| `refactor_call_chain_implement_prompt.md` | 為什麼放棄 `[P]` 語意標記的原始診斷與任務交接 |
| `refactor_plan.md` | 三層全域關卡設計、呼叫鏈規則、qwen／Claude 分層實測 |
| `refactor_bug_trace.md` | 新設計真實重跑後的 bug 追蹤表（#1～#46） |
| `00_refactor_architecture.md` | 整體架構與 LLM 分工 |
| `06a_plan_agent_architecture.md`／`09a_implement_agent_architecture.md` | `[P]` 呼叫鏈範圍查找、⑤ 讀取與排程 |
