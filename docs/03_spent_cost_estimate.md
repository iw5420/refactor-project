# 03b/03c 開發期間 Claude API 成本估算

## 前言：這是估算，不是帳單

這份文件**不是**從 Anthropic 後台帳務資料回推的精確數字——我沒有讀取實際用量/帳務 API 的權限，測試腳本裡也從沒印過 `response.usage`（實際 input/output token 數）。以下數字是根據：(1) 目前使用的模型 `claude-sonnet-4-6` 官方定價、(2) 這次除錯過程中**實際觀察到**的 prompt 內容大小（尤其是 seed.sql 全檔回退這件事，是用真實資料量測出來的，不是猜的）、(3) 各次真實整合測試 log 裡記錄的 API 呼叫次數，反推出來的合理範圍估計。目的是讓你看懂「錢花在哪」跟「量級對不對得上」，不是逐筆對帳。

帳戶餘額從 $100 掉到 -$2，總花費約 **$102**。

## 定價基準

`claude-sonnet-4-6`：輸入 $3.00 / 百萬 token，輸出 $15.00 / 百萬 token。Structured Outputs（`output_config.format`）本身**不額外收費**，用量還是照一般輸入/輸出 token 計價——第一次用到某個新 schema 時，官方文件提到會有一次性的 schema 編譯延遲，但那是延遲（latency），不是額外費用。

## 四個會呼叫 Claude API 的地方，各自的目的與規模

| 呼叫點 | 檔案 | 目的 | 每次真實跑會呼叫幾次 | 單次 prompt 規模（正常情況） |
|---|---|---|---|---|
| MAP 階段 | `chain_dependency_detect.py` `_map_analyze_group()` | 逐 controller tag 分析，找出候選 producer/consumer 欄位 | 每個 tag 一次，這份 spec 約 6 個 tag | 該 tag 底下所有 operation 的精簡 schema，數 KB 到十幾 KB |
| REDUCE 階段 | `chain_dependency_detect.py` `_reduce_phase()` | 彙整所有 MAP 候選，做跨 controller 最終配對 | 1 次 | 全部候選清單，數 KB |
| Folder 分組 | `folder_grouper.py` `_group_singletons()` | 判斷沒偵測到依賴關係的 endpoint 要不要合併進同一個 folder | 1 次 | 該批 singleton endpoint 清單，數 KB |
| FILL 填值 | `value_filler.py` `fill_example_values()` | 逐 endpoint 判斷該填什麼值，需要帶 seed.sql 相關片段當 context | 每個需要填值的 endpoint 一次，這份 spec 約 30 個 | **正常情況**應該只帶該 endpoint 對應資源的 INSERT 片段，數百到數千字元 |

MAP、REDUCE、folder 分組三個呼叫點加起來（~8 次）在正常情況下都是小 prompt，每次花費落在幾分錢等級，不是這次燒錢的主因。**FILL 才是主因**，而且是因為一個尚未修正的 bug（見下段），不是設計本身注定要這麼貴。

## 主要成本放大因素：seed.sql 全檔回退 bug

`value_filler.py` 的 `_INSERT_TABLE_PATTERN` 正規式沒處理 `public.` schema 前綴（詳見 [03_spent_bug_log.md](03_spent_bug_log.md) 第 10 項），導致 `extract_seed_excerpt()` 永遠比對不到真正的表名。我實測 8 個真實 endpoint，**全部 8 個**都回退塞整份 seed.sql 進 FILL prompt，沒有一個抓到正確的片段。

實測數字（直接讀真實 `fixtures/seed.sql` 得到，不是估的）：

- 檔案大小：823,934 bytes
- Python 讀成字串後長度：**676,946 字元**（這就是每次回退塞進 prompt 的量）

用英文/SQL 語法常見的「約 4 字元 ≈ 1 token」概算，這份 seed_excerpt 單獨就佔約 **17 萬 token**；如果內容裡有相當比例的中文資料（考生姓名、地址、考試內容之類），中文在 Claude 的 tokenizer 下通常是 1.5–2 字元 ≈ 1 token，比英文密集，實際 token 數可能落在 **17 萬到 25 萬**之間。我沒有實際跑 `count_tokens` 對這份檔案量測，所以用這個區間，不用單一數字假裝精確。

單次「回退全檔」的 FILL 呼叫成本（只算輸入，輸出通常只是一小段 JSON，可忽略）：

```
170,000 ~ 250,000 tokens × $3 / 1,000,000 ≈ $0.51 ~ $0.75 / 次
```

一次完整 pipeline 大約有 30 個 endpoint 要跑 FILL，假設多數都踩到這個回退（實測 8/8 全中，合理假設整體比例也很高，抓 25–30 個）：

```
25 ~ 30 次 × $0.51 ~ $0.75 ≈ $13 ~ $22 / 次完整跑
```

加上 MAP/REDUCE/folder 分組那 ~8 次小 prompt 呼叫（合計約 $0.2–0.4），**一次完整跑（03c 全流程）粗估落在 $13 ~ $23**，量級對得上你說的「一次 25 美元」。

## 這個 session 花了多少次「完整跑」

從各次真實整合測試的 log 直接數 API 呼叫次數（每一行 `HTTP Request... 200 OK` 對應一次真實呼叫）：

| 階段 | 完整跑次數（粗估） | 備註 |
|---|---|---|
| 03b/03c 開發前期（壓縮前的對話） | 約 2–3 次 | 含初次驗證、prefill 失敗重試、Structured Outputs 遷移後確認跑 |
| 這次對話可見部分 | 3 次完整跑 + 1 次幾乎零成本 | 15:44 確認 FILL 修正、16:41 清空重現問題、16:45 人工補值續跑驗證；17:21 那次因為額度已經用完，7 次呼叫全部 400（帳務層直接擋下，沒有真的處理 token，成本趨近於 0） |

合計約 **5–6 次真正有花錢的完整跑**，用上面 $13–23/次的區間反推：

```
5 ~ 6 次 × $13 ~ $23 ≈ $65 ~ $138
```

跟實際花費 $102 的量級吻合，可以合理確認：**seed.sql 回退這個 bug，就是這次成本異常偏高的主要原因**，不是模型本身用量設計就該這麼貴，也不是 Structured Outputs 帶來額外費用。

## 修好之後的預期效果

`_INSERT_TABLE_PATTERN` regex 一旦修正（讓它正確跳過 `public.` 前綴、抓到真正表名），`extract_seed_excerpt()` 應該能正確比對到單一資源相關的 INSERT 片段——從實測資料看，單一資源的相關片段通常是幾百到幾千字元，而不是 67 萬字元。單次 FILL 呼叫成本會從 $0.5–0.75 降到約 $0.001–0.01 等級，**FILL 部分整體成本估計會降到只剩原本的 1% 以下**，一次完整跑的總成本應該會落在 $0.5–2 之間，而不是 $13–23。這個修正還沒做（見 bug log 第 10 項），是目前最值得優先處理、CP 值最高的一項。
