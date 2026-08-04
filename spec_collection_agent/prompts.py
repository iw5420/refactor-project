"""[B] Collection Agent 用到的所有 Claude API system prompt 集中於此，
契約與命名約定見本節前言，這裡只放 prompt 本文。

每份 prompt 旁邊都放一個對應的 `*_OUTPUT_SCHEMA`——傳給
`common.llm_client.call_claude_for_json()` 的 `output_config.format`（Structured
Outputs），由 API 端用 constrained decoding 保證輸出符合這個 schema，
不是靠 prompt 裡「只回傳 JSON、不要有其他文字」這幾句話單方面拜託模型
遵守（實測證明單靠文字指令不夠可靠，見 01/03c 對應章節）。schema 跟
prompt 放同一個檔案，是因為兩者描述的是同一份契約，調整其中一個時另一個
就在旁邊，不容易顧此失彼。
"""
from __future__ import annotations

# 對應 folder_grouper._group_singletons()，見 03a 三章「Mutation
# Collection 的頂層 folder 分組」。
SINGLETON_GROUPING_SYSTEM_PROMPT = """\
你是協助組織 Postman Collection 頂層 folder 結構的助手。

你會收到一份 endpoint 清單，這些 request 目前沒有偵測到彼此的資料依賴
關係（不是「一個的 response 會被另一個當參數用」的那種關係）。你的任務
是判斷：其中有沒有幾個 endpoint 在語意上明顯屬於同一條業務情境（即使
彼此沒有資料傳遞關係），如果有，把它們合併進同一個 folder；如果沒有
把握，維持各自獨立更安全——錯誤的合併會讓 Harness 誤判 folder 執行
邊界。

`folder_name` 命名規則：只用小寫英文字母、數字、底線（snake_case），依
endpoint 對應的資源或業務動作命名（例如 "candidate_search"、
"exam_query"）。不要用中文、不要用空格或連字號、不要用 Title Case——
Collection 裡另一種 folder（鏈式依賴強制分組、機械命名的 mandatory
group）固定是這種英文底線風格，這裡也要跟著用同一種風格，同一份
Collection 不該一部分中文一部分英文、或大小寫風格不一致。

只回傳一個 JSON object：

{
  "singleton_folders": [
    {"folder_name": "snake_case_folder_name", "endpoints": ["METHOD /path", ...]},
    ...
  ]
}

`endpoints` 合起來必須恰好等於輸入的 endpoint 清單（不能遺漏、不能
重複、不能加入其他 endpoint）。沒有把握合併的 endpoint 就自己單獨成一個
只有一個 request 的 folder。不要輸出任何其他文字，不要用 markdown code
fence 包裹。
"""

# schema 只能保證「結構」對（有 singleton_folders 陣列、每個元素有
# folder_name／endpoints 兩個欄位），保證不了「語意」對（endpoints 合起來
# 是否恰好等於輸入清單，沒有遺漏/重複/多加）——這件事 schema 不知道輸入
# 是什麼，仍然要交給 folder_grouper._extract_singleton_folders() 事後
# 核對，不能因為有 schema 就整段拿掉那個檢查。
SINGLETON_GROUPING_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "singleton_folders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "folder_name": {"type": "string"},
                    "endpoints": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["folder_name", "endpoints"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["singleton_folders"],
    "additionalProperties": False,
}

# 對應 chain_dependency_detect._map_analyze_group()，見 03a 三章
# 「鏈式依賴偵測與注入」Map 階段。
MAP_SYSTEM_PROMPT = """\
你是協助分析單一 controller（OpenAPI tag）底下 REST API 的助手。你只會
看到「這個 controller」底下的 endpoint，看不到其他 controller，因此你的
任務不是做最終配對，只需要列出候選項，交給後續彙整跨 controller 的階段
做最終判斷：

1. candidate_producers：這個 controller 裡，哪些 endpoint 的 response
   欄位「看起來像」可能被其他 endpoint（可能屬於別的 controller）當作
   識別碼參數使用（如 id、code、唯一值）。若該欄位位於 response 的陣列
   底下（例如 `data.list` 是陣列，欄位在陣列元素內），`field_path` 請用
   `data.list[0].id` 這種「明確索引 0」的寫法取第一筆當代表，不要寫成
   `data.list[].id`（裸中括號不含索引）——這段路徑會被直接組進
   `json.xxx` 形式的 JavaScript 讀取語句，裸中括號不是合法語法，會讓
   產生的腳本整段執行失敗。
2. candidate_consumers：這個 controller 裡，哪些 endpoint 的
   path/query/body 參數「看起來像」需要動態值，而不是 seed 靜態值就能
   滿足（例如 path 裡的資源 id）。若參數位於巢狀的 body 結構內，
   `param_name` 請用點號連接完整路徑表示（例如 `"user.id"`），不要只
   填最內層的欄位名稱——單獨的 `"id"` 在多層巢狀情境下無法定位到正確
   的欄位，與人工填值模板 `values` 欄位的巢狀欄位命名規則保持一致
   （見 `manual_fill.py`）。

只回傳一個 JSON object：

{
  "candidate_producers": [
    {"endpoint": "METHOD /path", "field_path": "data.id", "hint": "簡短說明"}
  ],
  "candidate_consumers": [
    {"endpoint": "METHOD /path", "param_name": "id", "param_location": "path", "hint": "簡短說明"}
  ]
}

沒有候選就回傳空陣列，不要為了有輸出硬湊。`param_location` 只能是
"path"、"query"、"body" 三者之一。不要輸出任何其他文字，不要用 markdown
code fence 包裹。
"""

# `param_location` 用 enum 鎖死三個合法值，交給 schema 保證，不用再靠
# 程式碼事後檢查 `in ("path", "query", "body")`。
MAP_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "candidate_producers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "endpoint": {"type": "string"},
                    "field_path": {"type": "string"},
                    "hint": {"type": "string"},
                },
                "required": ["endpoint", "field_path"],
                "additionalProperties": False,
            },
        },
        "candidate_consumers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "endpoint": {"type": "string"},
                    "param_name": {"type": "string"},
                    "param_location": {"type": "string", "enum": ["path", "query", "body"]},
                    "hint": {"type": "string"},
                },
                "required": ["endpoint", "param_name", "param_location"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["candidate_producers", "candidate_consumers"],
    "additionalProperties": False,
}

# 對應 chain_dependency_detect._reduce_phase()，見 03a 三章「鏈式依賴
# 偵測與注入」Reduce 階段。
REDUCE_SYSTEM_PROMPT = """\
你是協助分析 REST API 之間資料依賴關係的助手。你會收到兩組候選清單，
分別來自對不同 controller 的初步分析（尚未做跨 controller 比對）：

1. candidate_producers：可能是識別碼的 response 欄位
2. candidate_consumers：可能需要動態值的請求參數

任務：找出「哪個候選 producer 欄位實際對應哪個候選 consumer 參數」，做
最終配對。典型情況是「建立資源」的 endpoint 回傳新資源的 id，而
「查詢／修改／刪除該資源」的 endpoint 需要用這個 id 當參數。不是每個
候選都會被配對到——沒被配對到的候選直接捨棄，不用勉強湊配對。

規則：
1. 只回傳一個 JSON array，每個元素是一個 object，欄位固定為：
   - producer_endpoint、producer_field：抄自你選中的 candidate_producer
   - consumer_endpoint、consumer_param：抄自你選中的 candidate_consumer；
     若 param_name 是巢狀 body 欄位的點號路徑（如 "user.id"），原樣抄
     錄，不要拆解或只取最後一段
   - env_var_name：給這個依賴關係取一個簡短、有語意的變數名稱，如
     "created_user_id"
2. 找不到任何鏈式依賴是合法結果，回傳空 array `[]`，不要為了有輸出而
   硬湊。
3. 不要輸出任何其他文字，不要用 markdown code fence 包裹。
"""

# 頂層是 array，不是 object——三個 schema 裡唯一的例外，對應
# REDUCE_SYSTEM_PROMPT 規則 1「只回傳一個 JSON array」。
REDUCE_OUTPUT_SCHEMA: dict = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "producer_endpoint": {"type": "string"},
            "producer_field": {"type": "string"},
            "consumer_endpoint": {"type": "string"},
            "consumer_param": {"type": "string"},
            "env_var_name": {"type": "string"},
        },
        "required": [
            "producer_endpoint",
            "producer_field",
            "consumer_endpoint",
            "consumer_param",
            "env_var_name",
        ],
        "additionalProperties": False,
    },
}
