# 03b/03c 開發期間 Bug 記錄

記錄範圍：從開始實作 03b（Spec Agent）、03c（Collection Agent）起，到目前為止在真實整合測試與程式碼審查中發現的問題。只列條目與根本原因，不附完整程式碼（程式碼異動請直接看對應檔案的 git diff）。

## 已修正

1. **JDK 版本不符**
   PATH 預設抓到 JDK 25，但 `pom.xml` 要求 JDK 11，導致 Java 服務啟動失敗。
   修法：新增 `JAVA_EXECUTABLE_PATH` 環境變數，讓 `JavaServiceProcess` 可以指定明確的 JDK 11 執行檔路徑。

2. **JSON 解析被 LLM 推理文字中偶然出現的括號誤導**
   `grading-controller` 這筆真實資料曾經讓 MAP 階段的回應在到達真正的 JSON 答案之前，先在推理文字裡出現 `` `SaveScoreRq[]` `` 這種型別註記，導致 `min(text.find("{"), text.find("["))` 的舊版 JSON 擷取邏輯誤判成 JSON 起點在那個位置，抓出一個空陣列 `[]` 當作整個回應。
   修法：後來隨第 4 項一起，改用 Structured Outputs（`output_config.format`）從 API 端用 constrained decoding 保證輸出格式，徹底不再需要「從自由文字裡找 JSON」這種脆弱的擷取邏輯。

3. **OpenAPI `$ref` 未展開**
   送給 LLM 的 operation 定義裡，`$ref` 指標（如 `#/components/schemas/X`）沒有展開，模型只能靠 DTO 類別名稱猜欄位，填值、鏈式依賴偵測的準確度都受影響。
   修法：新增 `spec_collection_agent/openapi_refs.py`（`resolve_refs()`），遞迴、防循環展開所有 `$ref`，套用到 FILL 與鏈式依賴偵測兩處呼叫點。

4. **Assistant message prefill 與 `claude-sonnet-4-6` 不相容**
   原始設計用 prefill 保證輸出格式，但沒有先查證模型支援度，實測 Claude 4.6 系列已經拿掉 prefill 支援，回 400（"This model does not support assistant message prefill"）。
   修法：查證 Anthropic 官方建議後，改用 Structured Outputs（`output_config.format`，schema-constrained decoding），由 API 端保證格式合法，不再依賴模型自己守規矩。

5. **`FILL_OUTPUT_SCHEMA` 與 Structured Outputs 的 `additionalProperties: false` 限制衝突**
   FILL 原本設計成 `{"type": "object"}`（無 `properties`）想表達「任意動態 key」，但 Structured Outputs 規定所有 object 型別都必須明確列出 `properties` 且 `additionalProperties` 設 `false`，兩者互斥，回 400。
   修法：改用陣列包裝動態 key 集合：`{"values": [{"key": ..., "value": ...}, ...]}`，陣列本身與元素欄位都能寫死 schema，`fill_example_values()` 內部轉回呼叫端原本用的平坦 `{key: value}` 字典。

6. **`fill_all_endpoints()` 重打 LLM 成功後沒清除舊的 manual_fill 模板檔**
   `manual_fill.remove_template()` 只在「人工值套用成功／skip」的路徑會呼叫，重新呼叫 LLM 並成功（不透過人工值）的路徑完全沒有清除機制。導致先前失敗留下的 pending 模板檔案，即使該 endpoint 這次已經填值成功，`manual_fill.list_pending()` 仍然誤報成「尚待人工補值」。
   修法：在 `value_filler.py` 的 `fill_all_endpoints()` 成功分支補上 `manual_fill.remove_template()` 呼叫，並在 `test_value_filler.py` 補上斷言鎖定這個行為。

7. **`chain_dependency_inject.py` 對 array-root consumer body 靜默不注入**
   `_rewrite_param_to_env_var()` 判斷 body 是否可改寫時用 `isinstance(body_obj, dict)`，body 根層若是 array（例如批次送出多筆資料的 endpoint）就直接跳過，**不記錄任何警告**，行為跟同檔案其他「找不到就記警告」的分支不一致，容易讓人誤以為鏈式依賴已經正確注入。
   修法：補上 `else` 分支，明確記錄警告說明「body 根層不是 JSON object，跳過這筆鏈式依賴注入」。**注意：這個修正只解決「靜默」問題，不解決 array-root body 本身無法動態注入值的能力缺口**——這兩個 endpoint 目前仍然只能用人工填的靜態值。

8. **Producer capture script 對陣列型 response 欄位產生不合法 JavaScript**
   MAP 階段的 prompt 沒規定「response 欄位在陣列底下時，`field_path` 該怎麼寫」，實測 LLM 自創 `data.exam[].randomId` 這種裸中括號記法，直接嵌進 capture script 的 `json.{field_path}` 是不合法 JS 語法，Newman 執行到這行會直接拋語法錯誤——而且完全沒有任何警告或例外，是本次除錯過程中影響範圍最廣、也最隱蔽的一個問題。
   修法：兩步一起做——(a) `MAP_SYSTEM_PROMPT` 明確規定陣列型欄位要用 `[0]` 明確索引；(b) `chain_dependency_inject.py` 新增 `_is_valid_js_field_path()`，在主迴圈偵測到裸 `[]` 就整筆依賴跳過（capture script、consumer 改寫、排序都不做），並記警告。(b) 是保證能防住問題的部分，(a) 只是降低觸發頻率，兩者缺一不可。

## 已發現、尚未修正

9. **`postman_runner.py` 的 `base_url` 命名與 collection 實際用的 `{{baseUrl}}` 不一致**
   `run_newman()` 傳給 newman 的是 `--env-var base_url=...`（底線），但 collection 裡所有 request 用的是 `{{baseUrl}}`（駝峰），且 collection 內建一個寫死的 fallback `http://localhost:8080`。結果是 Recorder（錄 Java）跟 Verifier（驗 Python）呼叫時指定的 `base_url` 從未真正生效，兩邊永遠打同一個寫死的網址——這會讓 Test Harness 賴以成立的「錄 Java golden、驗 Python」核心比對機制失效。
   附帶發現：整個 repo 從沒有任何測試或腳本真的拿 newman 執行過 `postman/collection_*.json`，這兩份檔案「能不能被 newman 正確消化」這件事從沒被驗證過。

10. **`_INSERT_TABLE_PATTERN` regex 沒處理 schema 前綴，導致 FILL 幾乎每次都回退塞整份 seed.sql**
    這份專案的 `fixtures/seed.sql` 每筆 `INSERT INTO` 都寫成 `public.exam`、`public.answer` 這種帶 `public.` schema 前綴的形式，但 `value_filler.py` 的 `_INSERT_TABLE_PATTERN` 正規式在遇到 `.` 就停止擷取，永遠只抓到 `"public"`，抓不到真正表名。這讓 `extract_seed_excerpt()` 的比對條件永遠對不上，**幾乎每一個 FILL 呼叫都回退塞整份 68 萬字元的 seed.sql 進 prompt**，是本次真實測試成本異常偏高的主要原因（詳細成本分析見 [03_spent_cost_estimate.md](03_spent_cost_estimate.md)）。

## 附帶的文件層級問題（非程式碼 bug，一併記錄）

- 文件內 `cp .env.env` 指令錯誤，修正為文字說明。
- `docs/00`、`01`、`03a`、`03b`、`03c` 多處把程式碼路徑寫成 `nodes/xxx.py`，實際路徑是 `graph/nodes/xxx.py`，已全部修正對齊。
