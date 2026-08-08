# [B] Collection Agent 程式碼實作

> 03a 是設計面文件，本文件是實作面文件，一一對應、不重複設計理由——每節開頭註明對應 03a 章節，這裡只講怎麼落地成程式碼。對應 `00_refactor_architecture.md` 十一章文件索引的 `03c_collection_agent_code.md`。[A] Spec Agent 的程式碼、套件總覽、`types.py`／`exceptions.py` 在 `03b_spec_agent_code.md`，本文件直接參照、不重複貼。

---

## 一、[B] 專用共用元件

### 1.1 `postman_tree.py`——Postman item tree 共用走訪工具

03a 沒有點名這個檔案，是 03c 新增的共用模組（理由見下方 module docstring）：item tree 走訪工具供 `collection_converter.py`／`folder_grouper.py`／`value_filler.py`／`chain_dependency_detect.py`／`chain_dependency_inject.py` 共用；`has_nested_key()`／`get_nested_value()`／`set_nested_value()` 三個點號路徑工具供 `value_filler.py`／`chain_dependency_inject.py` 共用。

```python
# spec_collection_agent/postman_tree.py
"""Postman Collection item tree 共用走訪工具，供 `collection_converter`／
`value_filler`／`chain_dependency`／`folder_grouper` 共用，避免各自重寫
一份、尤其是 `:id` ↔ `{id}` 的 path 還原規則兜不起來。
"""
from __future__ import annotations

from typing import Any, Iterator


def item_method(item: dict) -> str | None:
    """取得 item 的 HTTP method；資料夾節點沒有 `request` 欄位，回傳 None。"""
    request = item.get("request")
    if not isinstance(request, dict):
        return None
    method = request.get("method")
    return method.upper() if isinstance(method, str) else None


def is_folder(item: dict) -> bool:
    return "item" in item and item_method(item) is None


def iter_leaf_items(items: list[dict]) -> Iterator[dict]:
    """遞迴走訪 item tree，只 yield 葉節點（實際請求），跳過資料夾節點本身。"""
    for item in items:
        if is_folder(item):
            yield from iter_leaf_items(item["item"])
        else:
            yield item


def _segment_to_str(seg: Any) -> str:
    """把單一 path segment 正規化成字串。Postman v2.1 schema 對
    `url.path` 的定義是 `array of (string | {type, value, ...})`——多數
    工具（含 `openapi-to-postmanv2`）只會輸出純字串，但 schema 本身允許
    帶說明的 variable 物件，這裡做防呆避免遇到 dict 時 `.startswith()`
    直接炸掉。
    """
    if isinstance(seg, dict):
        seg = seg.get("value", "")
    if not isinstance(seg, str):
        return ""
    return "{" + seg[1:] + "}" if seg.startswith(":") else seg


def normalized_path_from_item(item: dict) -> str | None:
    """把 url path segments 還原成 OpenAPI 風格路徑（`:id` -> `{id}`），
    用來對回 `openapi_spec["paths"]` 或跨 Collection 比對同一個 endpoint。
    """
    url = item.get("request", {}).get("url")
    if not isinstance(url, dict):
        return None
    segments = url.get("path")
    if not isinstance(segments, list):
        return None
    normalized = [_segment_to_str(seg) for seg in segments]
    return "/" + "/".join(normalized)


def find_item(items: list[dict], method: str, path: str) -> dict | None:
    """在 item tree 裡找出 method+path 相符的第一個葉節點；找不到回傳 None。"""
    target_method = method.upper()
    for item in iter_leaf_items(items):
        if item_method(item) == target_method and normalized_path_from_item(item) == path:
            return item
    return None


def find_containing_top_level_folder(items: list[dict], target: dict) -> dict | None:
    """找出 `target` item 所屬的頂層 folder。用物件參照（`is`）而非內容比對
    ——tree 裡可能有內容相同但語意不同的 item（如鏈式驗證新增的 GET）。
    """
    for top in items:
        if not is_folder(top):
            continue
        if any(child is target for child in iter_leaf_items(top["item"])):
            return top
    return None


def _split_dotted_key(dotted_key: str) -> list[str]:
    return dotted_key.split(".") if dotted_key else []


def has_nested_key(obj: dict, dotted_key: str) -> bool:
    """檢查 dotted_key（如 `"user.id"`）在巢狀 dict `obj` 裡是否存在對應的
    路徑。只支援 dict 巢狀——中途遇到非 dict 的值（list、純量）一律視為
    路徑不存在，不嘗試往下鑽（`value_filler`／`chain_dependency_inject`
    的填值與注入只需要處理「body 是巢狀 object」這個情境；body 根層級
    本身為 array 的情況在 `_apply_values_to_item()` 已有獨立的失敗路徑，
    不在這個工具函式的範圍內）。
    """
    segments = _split_dotted_key(dotted_key)
    if not segments:
        return False
    current: Any = obj
    for seg in segments[:-1]:
        if not isinstance(current, dict) or seg not in current:
            return False
        current = current[seg]
    return isinstance(current, dict) and segments[-1] in current


def get_nested_value(obj: dict, dotted_key: str) -> Any:
    """讀出 dotted_key 對應的值；路徑不存在時拋 `KeyError`。不在函式內部
    先做 `has_nested_key()` 檢查再靜默回傳預設值，是刻意的設計——讓
    「路徑不存在」這件事的處理方式由呼叫端決定，不要在工具函式裡吞掉。
    """
    segments = _split_dotted_key(dotted_key)
    current: Any = obj
    for seg in segments:
        if not isinstance(current, dict) or seg not in current:
            raise KeyError(dotted_key)
        current = current[seg]
    return current


def set_nested_value(obj: dict, dotted_key: str, value: Any) -> None:
    """把 `value` 寫入 dotted_key（如 `"user.id"`）對應的路徑，中途不存在
    的中繼層會自動建立空 dict（例如 obj 目前完全沒有 `"user"` key，會先
    建立 `obj["user"] = {}` 再寫入 `obj["user"]["id"] = value`）。

    若路徑中途某一層已經存在、但不是 dict（例如已經是 list 或純量），
    視為路徑衝突，拋 `TypeError`——不強制覆寫成 dict，那等於摧毀一段
    既有結構；寧可讓呼叫端把這種情況當成失敗處理，這也是
    `value_filler`／`chain_dependency_inject` 接住這個例外之後的處理
    方式（見各自呼叫端的說明）。
    """
    segments = _split_dotted_key(dotted_key)
    if not segments:
        raise ValueError("dotted_key 不可為空字串")
    current = obj
    for seg in segments[:-1]:
        nxt = current.get(seg)
        if nxt is None:
            nxt = {}
            current[seg] = nxt
        elif not isinstance(nxt, dict):
            raise TypeError(
                f"dotted_key 路徑衝突：{dotted_key!r} 中途的 {seg!r} 已存在"
                f"且不是 dict（型別={type(nxt).__name__}），無法繼續巢狀寫入"
            )
        current = nxt
    current[segments[-1]] = value
```

### 1.2 `llm.py`——[B] 專屬的模型選擇

**Claude API 呼叫的核心邏輯已經搬到 `common/llm_client.py`**：client 初始化、Structured Outputs（`output_config.format`）組裝、`log_usage()` 整合、`LlmJsonError` 錯誤處理，這些在 ①③⑤(⑦ Debug)、[P] Plan、[B] 等所有會呼叫 Claude API 的 Agent 之間幾乎完全相同，集中到 `common/llm_client.py` 一份實作、所有 Agent 共用（見 00 六章「Claude API 呼叫封裝」，完整程式碼與 Structured Outputs 取代 prefill 的沿革見該章，這裡不重複貼一次）。`folder_grouper.py`／`chain_dependency_detect.py` 改成直接 `from common.llm_client import LlmJsonError, call_claude_for_json`，各自的 prompt 與 JSON schema 仍在 1.3 `prompts.py`。

`spec_collection_agent/llm.py` 因此縮到只剩一件事：[B] 用哪個模型，透過 `SPEC_COLLECTION_AGENT_MODEL` 環境變數控制（細節見六章 6.1），沒設定時退回 `common.llm_client.DEFAULT_MODEL_FALLBACK`：

```python
# spec_collection_agent/llm.py
"""[B] Collection Agent 專屬的 Claude API 模型選擇。實際呼叫邏輯（client
初始化、Structured Outputs、log_usage() 整合、錯誤處理）已集中在
`common/llm_client.py`，所有需要呼叫 Claude API 的 Agent 共用同一份，
不再各自重複實作（見 00 六章）。這個檔案只負責一件事：[B] 用哪個模型。
"""
from __future__ import annotations

import os

from common.llm_client import DEFAULT_MODEL_FALLBACK

# 三個呼叫端（singleton 分組、map、reduce）共用這一個環境變數，
# 不寫死模型字面值；下面只是沒設環境變數時的保底 fallback。
DEFAULT_MODEL = os.environ.get("SPEC_COLLECTION_AGENT_MODEL", DEFAULT_MODEL_FALLBACK)
```

各呼叫端把 `DEFAULT_MODEL` 傳進 `call_claude_for_json(..., model=DEFAULT_MODEL)`——`common/llm_client.py` 的 `call_claude_for_json()` 把 `model` 設計成必填參數，不吃任何預設值，因為它不該知道任何 Agent 的環境變數命名慣例，實際呼叫寫法見 2.2、2.3。

### 1.3 `prompts.py`——Claude system prompt 集中管理

[B] 用到的三份 Claude system prompt 集中於此，對應的輸入/輸出契約見 03a 三章各節，這裡只放 prompt 本文。獨立成檔案是為了讓 prompt 調優（見 03a 七章待決定事項）只需要改這一個檔案，不會動到 `folder_grouper.py`／`chain_dependency_detect.py` 的呼叫邏輯。`MAP_SYSTEM_PROMPT`／`REDUCE_SYSTEM_PROMPT` 對「巢狀 body 欄位用點號路徑命名（如 `"user.id"`）」的規則寫法必須保持一致——這個命名約定貫穿鏈式依賴偵測/注入（`chain_dependency_inject.py`，見 `postman_tree.py` 的巢狀路徑工具）與人工填值模板 `values` 欄位（`manual_fill.py`，見 2.6），改了措辭要連帶檢查是否還一致。

> **`FILL_SYSTEM_PROMPT`／`FILL_OUTPUT_SCHEMA` 已移除**——填值改成人工在階段一產生的模板裡完成（見 03a 三章「人工填值機制」），不再有 LLM 填值這個步驟，`value_filler.py` 也已同步不再引用（見 2.3）。

```python
# spec_collection_agent/prompts.py
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
```

### 1.4 `openapi_refs.py`——OpenAPI `$ref` 展開

對應 03a 三章「OpenAPI `$ref` 展開（Claude API payload 組裝的共用前處理）」。人工填值模板產生（2.6 `value_filler._operation_param_schema()`）與鏈式依賴偵測（2.4 `chain_dependency_detect.group_operations_by_tag()`）共用同一個 `resolve_refs()`，組出內容之前先展開，才看得到真正的欄位名稱與型別，不用靠 DTO 類別名稱猜。

```python
# spec_collection_agent/openapi_refs.py
"""OpenAPI `$ref` 展開，對應 03a 三章「OpenAPI `$ref` 展開（Claude API
payload 組裝的共用前處理）」。人工填值模板產生（`value_filler.
_operation_param_schema()`，給人看的參考欄位）與鏈式依賴偵測
（`chain_dependency_detect.py`，送給 Claude API）組出各自需要的內容之前，
都要先用這裡的 `resolve_refs()` 把 operation 片段裡的 `$ref` 展開成實際
schema 定義，才看得到真正的欄位名稱與型別，不用靠 DTO 類別名稱猜。
"""
from __future__ import annotations

from typing import Any

from spec_collection_agent.types import OpenAPISpec


def resolve_refs(obj: Any, spec: OpenAPISpec, *, _seen: frozenset[str] = frozenset()) -> Any:
    """遞迴展開 `obj` 內出現的 `$ref`（JSON Pointer，如
    `"#/components/schemas/SaveScoreRq"`），只展開 `obj` 實際用到的部分，
    不會把整份 `components.schemas` 攤平塞進來（見 03a 該節 Context 控制
    原則）。

    循環參照時停止繼續展開，回傳一個帶 `_circular` 標記的殘留 `$ref`，
    不無限遞迴。找不到指標對應的節點時原樣保留該 `$ref`，不拋例外中止
    ——展開失敗不該讓呼叫端（填值／鏈式依賴偵測）連帶整個失敗，寧可讓
    模型看到一個沒展開的指標，也不要讓這個函式本身變成新的硬性失敗點。
    """
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in _seen:
                return {"$ref": ref, "_circular": True}
            target = _resolve_json_pointer(spec, ref)
            if target is None:
                return obj
            return resolve_refs(target, spec, _seen=_seen | {ref})
        return {k: resolve_refs(v, spec, _seen=_seen) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_refs(v, spec, _seen=_seen) for v in obj]
    return obj


def _resolve_json_pointer(spec: OpenAPISpec, ref: str) -> Any | None:
    """解析 `"#/a/b/c"` 這種 JSON Pointer（RFC 6901），沿路徑走到 `spec`
    裡對應的節點；沿路徑走不下去（key 不存在）就回傳 `None`。
    """
    node: Any = spec
    for segment in ref[2:].split("/"):
        segment = segment.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        else:
            return None
    return node
```

---

## 二、[B] Collection Agent 實作

### 2.1 `collection_converter.py`——轉換流程與 readonly/mutation 分類

對應 03a 三章「轉換流程與 baseUrl 變數化」「Readonly / Mutation 分類規則」，純程式邏輯，規則細節見 03a，這裡只放轉換與分類的落地方式：`split_readonly_mutation()` 遞迴走訪 item tree 依 method 分流，捨棄遞迴後的空資料夾；**不**為鏈式依賴另開例外分支——額外的 GET 驗證 item 是 `chain_dependency_inject.inject_chain_scripts()` 之後才新增的，不在這裡處理。

```python
# spec_collection_agent/collection_converter.py
"""[B] Collection Agent：轉檔與 readonly/mutation 分類。純程式邏輯，
對應 03a 三章「轉換流程與 baseUrl 變數化」「Readonly / Mutation 分類規則」。
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

from spec_collection_agent.exceptions import CollectionConversionError
from spec_collection_agent.postman_tree import is_folder, item_method
from spec_collection_agent.types import (
    MUTATION_METHODS,
    READONLY_METHODS,
    PostmanCollection,
)


def convert_openapi_to_postman(
    openapi_json_path: Path, output_path: Path
) -> PostmanCollection:
    """呼叫 `openapi-to-postmanv2`（經 `npx`，比照 01 八跨平台建議）把
    `openapi.json` 轉成一份原始 Collection。保留 `{{baseUrl}}` 不改寫
    （見 03a 三章「決策：保留 {{baseUrl}}」）。

    直接把字面字串 `"npx"` 交給 `subprocess.run` 在 Windows 上會失敗
    （`FileNotFoundError`）：Windows 的 npm 全域安裝把 `npx` 裝成
    `npx.cmd`，`CreateProcess` 不像 shell 那樣自動幫你補副檔名。改用
    `shutil.which()` 解析出實際可執行檔的絕對路徑（它會依 `PATHEXT`
    環境變數嘗試 `.cmd`／`.exe` 等副檔名，行為跨平台一致），解析失敗
    直接拋出清楚的錯誤，不要讓 `subprocess.run` 用一個模糊的
    `FileNotFoundError` 去掩蓋「根本沒裝 Node.js/npm」這個更根本的問題。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    npx_path = shutil.which("npx")
    if npx_path is None:
        raise FileNotFoundError(
            "系統 PATH 找不到 npx 指令，請確認 Node.js/npm 已安裝且已加入 PATH"
            "（見 00 五章環境需求）"
        )

    result = subprocess.run(
        [
            npx_path,
            "openapi-to-postmanv2",
            "-s",
            str(openapi_json_path),
            "-o",
            str(output_path),
            "-p",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",  # 不依賴作業系統預設編碼（Windows 常見非 UTF-8
        # locale，openapi-to-postmanv2 的錯誤訊息若含非 ASCII 字元會導致
        # 用系統預設編碼解碼時噴 UnicodeDecodeError）。
        stdin=subprocess.DEVNULL,  # 一次性 CLI 呼叫不需要標準輸入，明確
        # 阻斷而非繼承父進程的 stdin，避免特定終端機環境下意外卡住等輸入。
    )

    if result.returncode != 0:
        raise CollectionConversionError(
            f"openapi-to-postmanv2 轉換失敗（exit code={result.returncode}）",
            stderr=result.stderr,
        )

    with output_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _filter_tree(items: list[dict], allowed_methods: frozenset[str]) -> list[dict]:
    """遞迴保留 method 屬於 `allowed_methods` 的請求，資料夾結構不變；
    遞迴後淨空的資料夾捨棄（見 03a 三章分類規則）。
    """
    kept: list[dict] = []
    for item in items:
        if is_folder(item):
            children = _filter_tree(item["item"], allowed_methods)
            if children:
                new_folder = copy.deepcopy(item)
                new_folder["item"] = children
                kept.append(new_folder)
            continue

        method = item_method(item)
        if method in allowed_methods:
            kept.append(copy.deepcopy(item))

    return kept


def split_readonly_mutation(
    collection: PostmanCollection,
) -> tuple[PostmanCollection, PostmanCollection]:
    """依 HTTP method 把原始 Collection 拆成 readonly（GET/HEAD）/
    mutation（POST/PUT/PATCH/DELETE），見 03a 三章分類表。分類規則不做
    例外——鏈式依賴需要的額外 GET 由 `chain_dependency_inject.inject_chain_scripts()`
    另外新增，不在這裡處理。
    """
    readonly_items = _filter_tree(collection.get("item", []), READONLY_METHODS)
    mutation_items = _filter_tree(collection.get("item", []), MUTATION_METHODS)

    readonly = copy.deepcopy(collection)
    readonly["item"] = readonly_items
    readonly.setdefault("info", {})["name"] = (
        f"{collection.get('info', {}).get('name', 'api')}-readonly"
    )

    mutation = copy.deepcopy(collection)
    mutation["item"] = mutation_items
    mutation.setdefault("info", {})["name"] = (
        f"{collection.get('info', {}).get('name', 'api')}-mutation"
    )

    return readonly, mutation
```

### 2.2 `folder_grouper.py`——Mutation 頂層 folder 分組

對應 03a 三章「Mutation Collection 的頂層 folder 分組」；分組規則（membership／名稱皆機械決定、只有 singleton 才問 LLM、且只在非空時才呼叫）已在 03a 定案，這裡只放落地方式：`_group_by_chain_dependencies()` 用 union-find 做機械分組，`_mechanical_group_name()` 重用 `value_filler.guess_resource_name()` 產生 folder 名稱，`_group_singletons()` 是唯一的 LLM 進入點。`_extract_singleton_folders()` 對 LLM 回傳做防呆，格式不合法一律退回保守預設（每個 endpoint 各自一個 folder）。

```python
# spec_collection_agent/folder_grouper.py
"""[B] Collection Agent：Mutation Collection 的頂層 folder 分組，對應 03a
三章「Mutation Collection 的頂層 folder 分組（設計約定，鎖死）」。

mandatory group（membership + 名稱）機械決定，不呼叫 LLM；只有 singleton
才問 LLM，且只在非空時才呼叫、payload 只放 singleton 清單（設計理由見
03a 該節）。
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any

from common.llm_client import LlmJsonError, call_claude_for_json
from spec_collection_agent.llm import DEFAULT_MODEL
from spec_collection_agent.postman_tree import (
    find_item,
    item_method,
    iter_leaf_items,
    normalized_path_from_item,
)
from spec_collection_agent.prompts import (
    SINGLETON_GROUPING_OUTPUT_SCHEMA,
    SINGLETON_GROUPING_SYSTEM_PROMPT,
)
from spec_collection_agent.types import MUTATION_METHODS, ChainDependency, PostmanCollection
from spec_collection_agent.value_filler import guess_resource_name

logger = logging.getLogger(__name__)


class _UnionFind:
    """以 `id(item)` 為 key 的簡易 union-find，把有鏈式依賴的 request
    合併成同一組（規則 2：membership 是機械式決定，不需要 LLM 判斷）。
    """

    def __init__(self, keys: list[int]) -> None:
        self._parent: dict[int, int] = {k: k for k in keys}

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def _endpoint_of(item: dict) -> str:
    method = item_method(item) or "?"
    path = normalized_path_from_item(item) or "?"
    return f"{method} {path}"


def _group_by_chain_dependencies(
    mutation_items: list[dict], chain_dependencies: list[ChainDependency]
) -> tuple[list[list[dict]], list[dict]]:
    """依鏈式依賴把 mutation item 分成必須同組的群組與剩餘獨立項目。只考慮
    consumer 也是 mutation method 的依賴——GET consumer 此階段還不存在於
    mutation collection（它是 `inject_chain_scripts()` 才新增的驗證 item）。
    """
    uf = _UnionFind([id(item) for item in mutation_items])

    for dep in chain_dependencies:
        if dep.consumer_method not in MUTATION_METHODS:
            continue
        producer = find_item(mutation_items, dep.producer_method, dep.producer_path)
        consumer = find_item(mutation_items, dep.consumer_method, dep.consumer_path)
        if producer is None or consumer is None:
            continue
        uf.union(id(producer), id(consumer))

    groups: dict[int, list[dict]] = {}
    for item in mutation_items:
        root = uf.find(id(item))
        groups.setdefault(root, []).append(item)

    mandatory_groups = [g for g in groups.values() if len(g) > 1]
    singletons = [g[0] for g in groups.values() if len(g) == 1]
    return mandatory_groups, singletons


# --------------------------------------------------------------------------
# mandatory group 命名：機械規則，不呼叫 LLM（見上方模組說明）
# --------------------------------------------------------------------------


def _mechanical_group_name(group: list[dict]) -> str:
    """mandatory group 的 membership 已由鏈式依賴強制決定，這裡只需要一個
    可讀、穩定的 folder 名稱，不追求語意品質，也不呼叫 LLM（對應 03a
    「這部分不需要再問 Claude 一次」）。

    做法：沿用 `value_filler.guess_resource_name()` 對 group 內每個 item
    的 path 猜資源名稱，依出現順序去重，用底線接起來；資源種類太多時
    截斷加 `_etc` 避免名稱過長。猜不出任何資源名稱時退回通用預設。

    **全英文、底線分隔（snake_case）**——跟 `_group_singletons()` 的
    Claude 命名（見 `SINGLETON_GROUPING_SYSTEM_PROMPT`）用同一套風格約定，
    確保同一份 Collection 裡 mandatory／singleton 兩種 folder 的命名不會
    一邊中文一邊英文、風格對不上（見 03a 三章「folder 命名風格」更正）。
    """
    resource_names: list[str] = []
    for item in group:
        path = normalized_path_from_item(item) or ""
        name = guess_resource_name(path)
        if name and name not in resource_names:
            resource_names.append(name)

    if not resource_names:
        return "business_flow"

    label = "_".join(resource_names[:3])
    if len(resource_names) > 3:
        label += "_etc"
    return f"{label}_flow"


# --------------------------------------------------------------------------
# singleton 分組：唯一需要 LLM 判斷的部分，只在有 singleton 時才呼叫
# --------------------------------------------------------------------------


def _default_singleton_folders(
    singleton_endpoints: list[str],
) -> list[tuple[str, list[str]]]:
    return [(endpoint, [endpoint]) for endpoint in singleton_endpoints]


def _extract_singleton_folders(
    llm_output: dict[str, Any] | None, singleton_endpoints: list[str]
) -> list[tuple[str, list[str]]]:
    """`llm_output` 的**結構**已由 `SINGLETON_GROUPING_OUTPUT_SCHEMA`
    （output_config.format）保證——`singleton_folders` 一定存在、一定是
    `{folder_name: str, endpoints: [str, ...]}` 的陣列，不需要再逐一
    isinstance 檢查。但 schema 保證不了**語意**：`endpoints` 合起來是否
    恰好等於輸入的 `singleton_endpoints`（不遺漏、不重複、不多加）——schema
    不知道這次輸入是什麼，這件事仍然要在這裡核對，對不上就退回保守預設。

    `llm_output is None` 代表 LLM 呼叫本身失敗（見 `_group_singletons()`），
    同樣退回保守預設。
    """
    fallback = _default_singleton_folders(singleton_endpoints)

    if llm_output is None:
        return fallback

    result: list[tuple[str, list[str]]] = [
        (entry["folder_name"], entry["endpoints"])
        for entry in llm_output["singleton_folders"]
    ]
    collected = [ep for _, endpoints in result for ep in endpoints]

    if sorted(collected) != sorted(singleton_endpoints):
        logger.warning(
            "Mutation folder 分組 LLM 回傳的 singleton 分組與輸入端點集合不一致"
            "（可能遺漏、重複，或加入不存在的 endpoint），退回保守預設："
            "每個 endpoint 各自一個 folder"
        )
        return fallback

    return result


def _group_singletons(singleton_endpoints: list[str]) -> list[tuple[str, list[str]]]:
    """singleton 分組的唯一 LLM 進入點。**只在有 singleton 時才呼叫**，
    payload 只放 singleton 清單本身（method+path 字串），不夾帶 mandatory
    group 的任何資訊——對應 03a「範圍刻意收窄」「輸入僅為這些剩餘
    endpoint 的摘要，而非整份 openapi_spec」。
    """
    if not singleton_endpoints:
        return []

    llm_output: dict[str, Any] | None = None
    try:
        llm_output = call_claude_for_json(
            system_prompt=SINGLETON_GROUPING_SYSTEM_PROMPT,
            user_prompt=json.dumps(singleton_endpoints, ensure_ascii=False, indent=2),
            schema=SINGLETON_GROUPING_OUTPUT_SCHEMA,
            model=DEFAULT_MODEL,
        )
    except LlmJsonError as exc:
        logger.warning("Singleton folder 分組的 LLM 呼叫失敗，退回保守預設: %s", exc)

    return _extract_singleton_folders(llm_output, singleton_endpoints)


def group_mutation_folders(
    *, mutation: PostmanCollection, chain_dependencies: list[ChainDependency]
) -> PostmanCollection:
    """把 `split_readonly_mutation()` 產出的 mutation collection 重新分組成
    業務情境 folder。mandatory group 完全機械決定（membership + 名稱都
    不問 LLM）；singleton 只在非空時才問 LLM，且問法範圍很窄。LLM 失敗時
    退回保守預設（singleton 各自成一個 folder）——規則 4 本就允許，只損失
    可讀性，不影響 Harness「逐頂層 folder reset」的正確性。
    """
    mutation_items = list(iter_leaf_items(mutation.get("item", [])))
    mandatory_groups, singletons = _group_by_chain_dependencies(
        mutation_items, chain_dependencies
    )

    mandatory_names = [_mechanical_group_name(group) for group in mandatory_groups]

    singleton_endpoints = [_endpoint_of(item) for item in singletons]
    singleton_folders = _group_singletons(singleton_endpoints)

    top_level_items: list[dict] = [
        {"name": mandatory_names[idx], "item": group}
        for idx, group in enumerate(mandatory_groups)
    ]

    endpoint_to_item = {_endpoint_of(item): item for item in singletons}
    for folder_name, endpoints in singleton_folders:
        top_level_items.append(
            {"name": folder_name, "item": [endpoint_to_item[e] for e in endpoints]}
        )

    regrouped = copy.deepcopy(mutation)
    regrouped["item"] = top_level_items
    return regrouped
```

### 2.3 `value_filler.py`——把人工填值套進 Postman Collection

對應 03a 三章「人工填值機制」，設計理由（False Pass 風險、模板不刪除）見 03a 與下方 module docstring，這裡不重複。不呼叫 Claude API。函式：`guess_resource_name()`（僅供 `folder_grouper._mechanical_group_name()` 產生可讀 folder 名稱用，見其 docstring）、`_operation_param_schema()`（判斷 endpoint 是否需要生成模板／套值，人工填值模板產生與套用兩處共用）、`_apply_url_values()`（url variable/query 套用，跟 `fill_mode` 無關，`manual_fill.apply_manual_fill()` 不管哪種模式都會先呼叫，見 03a 三章「url variable/query 一律先套用」）、`_apply_values_to_item()`（`fields` 模式套值，內部沿用 `_apply_url_values()` 處理 url 部分）、`apply_manual_fill_to_collections()`（**只**跑過 mutation Collection，套用人工填值或排除，readonly 原封不動回傳）。

> `apply_manual_fill_to_collections()` 只處理 mutation Collection，readonly 完全不經過「找不到 manual_fill 模板就排除」這段邏輯——`manual_fill.generate_manual_fill_templates()` 只對 `MUTATION_METHODS` 產生模板，若讓這段排除邏輯也套在 readonly 上，readonly 裡帶路徑／查詢參數的 GET（如 `GET /exam/{id}`）會被誤判成「尚未填值」而從 `collection_readonly.json` 剔除，牴觸 03a 三章「Readonly / Mutation 分類規則」的明文保證：readonly 就是只含 GET、零副作用，不會被抽走。

```python
# spec_collection_agent/value_filler.py
"""[B] Collection Agent：把人工填值套進 Postman Collection，對應 03a 三章
「人工填值機制」全節。不呼叫 Claude API——填值本身是人工在階段一產生的
`postman/manual_fill/<controller>.json` 模板裡完成的（見 `manual_fill.py`），
這裡只負責讀取人工答案、套進 item，以及排除尚未解決的 endpoint。

False Pass 風險（本檔案與 chain_dependency_inject.py 共用的核心考量，
只在此處完整說明，其餘地方只作提示、不重複）：套用失敗或尚未填值時，
若讓 endpoint 帶著預設占位值進 Collection，Java 對爛參數回的錯誤會被
Agent ② 原封不動錄成 golden output；Python 重構後若巧合回同類錯誤，
Agent ⑥ 會判定 PASS，但這是用壞資料驗證出的假結果。因此本檔案任何
「尚未解決」或「套用失敗」的情況一律排除該 endpoint、記入
unfilled_endpoints.json，不留預設值硬撐。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from spec_collection_agent import manual_fill
from spec_collection_agent.openapi_refs import resolve_refs
from spec_collection_agent.postman_tree import (
    iter_leaf_items,
    normalized_path_from_item,
    set_nested_value,
)
from spec_collection_agent.types import OpenAPISpec, PostmanCollection

# 通用 path 前綴（版本號、"api" 這類字眼）不具識別力，猜資源名稱時跳過。
_GENERIC_PATH_SEGMENTS = {"api", "v1", "v2", "v3"}


def guess_resource_name(path: str) -> str:
    """從 endpoint path 猜資源名稱，如 `/api/v1/users/{id}` -> `users`
    （取版本前綴與 path 參數之外最後一段靜態 segment）。

    **僅供 `folder_grouper._mechanical_group_name()` 產生人類可讀的 folder
    名稱用**，猜錯、猜到動詞（這個專案的 endpoint 路徑是動詞式 RPC 風格，
    見 03a 三章「人工填值機制」設計動機）都只影響 folder 顯示名稱好不好
    讀，不影響任何功能正確性——folder membership 由鏈式依賴強制決定，
    這裡的猜測不是唯一依據。舊版本這個函式還兼職「猜資源名稱去比對
    seed.sql 表名、窄化 LLM context」，那個用途已經隨人工填值機制整個
    移除（見 03a 三章「刪除的部分」），不要再往那個方向擴充這個函式。
    """
    segments = [s for s in path.strip("/").split("/") if s]
    static_segments = [
        s
        for s in segments
        if not s.startswith("{") and s.lower() not in _GENERIC_PATH_SEGMENTS
    ]
    return static_segments[-1] if static_segments else ""


# --------------------------------------------------------------------------
# 判斷 endpoint 是否需要填值（供階段一產生模板、階段二套用值兩處共用）
# --------------------------------------------------------------------------


def _operation_param_schema(
    openapi_spec: OpenAPISpec, path: str, method: str
) -> dict[str, Any] | None:
    operation = openapi_spec.get("paths", {}).get(path, {}).get(method.lower())
    if not isinstance(operation, dict):
        return None
    param_schema: dict[str, Any] = {}
    if "parameters" in operation:
        param_schema["parameters"] = operation["parameters"]
    if "requestBody" in operation:
        param_schema["requestBody"] = operation["requestBody"]
    if not param_schema:
        return None
    # 展開 $ref，讓人工填值時看到真正的欄位名稱與型別，不用靠 DTO 類別
    # 名稱猜（見 03a 三章「OpenAPI $ref 展開」）。
    return resolve_refs(param_schema, openapi_spec)


# --------------------------------------------------------------------------
# 套用人工填值（fields 模式），manual_fill.apply_manual_fill() 沿用
# --------------------------------------------------------------------------


def _apply_url_values(item: dict, values: dict[str, Any]) -> set[str]:
    """把 `values` 裡對得上 url variable/query key 的值套進去，回傳已套用
    的 key 集合。url 參數跟 body 是 JSON／raw_body／multipart 哪一種格式
    無關——`multipart/form-data` 的 endpoint 常常同時有 query 參數（如
    `POST /api/file/image` 的 `kind`/`randomId`/`side`），這幾個 query
    參數不該因為 body 走 `file_upload` 模式就沒有管道被填（見 03a 三章
    「檔案上傳」更正）。`_apply_values_to_item()`（`fields` 模式）與
    `manual_fill.apply_manual_fill()`（所有模式套用前）共用這個函式。

    同一個 key 可能對應多個 query item（OpenAPI 陣列型 query 參數展開的
    結果，如 `?status=A&status=B`），全部套用同一個值，不是只改第一筆。
    只接受純量；收到 dict/list（人工填錯格式）視為套用失敗，不轉成 JSON
    字串硬塞。
    """
    used_keys: set[str] = set()
    url = item.get("request", {}).get("url")
    if isinstance(url, dict):
        for var in url.get("variable", []):
            key = var.get("key")
            if key in values:
                var["value"] = _scalar_param_value(values[key], key)
                used_keys.add(key)
        for query in url.get("query", []):
            key = query.get("key")
            if key in values:
                query["value"] = _scalar_param_value(values[key], key)
                used_keys.add(key)
    return used_keys


def _apply_values_to_item(item: dict, values: dict[str, Any]) -> None:
    """把填值套進 item 的 url variable / query / body，比對順序 path
    variable -> query -> 其餘視為 JSON body 欄位（可能是巢狀路徑，見下）。
    失敗一律拋 `ValueError`、不靜默略過，交呼叫端走排除＋記錄路徑
    （False Pass 風險見 module docstring）。

    規則：
    1. url variable/query 用 `_apply_url_values()` 套用（見該函式）。
    2. 巢狀 body 欄位（如 `"user.id"`）用 `postman_tree.set_nested_value()`
       逐層寫入，不對 `body_obj` 做淺層 `dict.update()`——淺層合併會抹除
       同一巢狀物件裡其他未填的手足欄位（如 `name`／`email` 預設值）。
    3. body 根層級不是 dict（array body、body.raw 非合法 JSON、純量）時，
       欄位級填值語意不適用，一律拋 `ValueError`；巢狀路徑寫入衝突
       （`set_nested_value()` 拋出的 `TypeError`）同樣處理。
    """
    used_keys = _apply_url_values(item, values)
    remaining = {k: v for k, v in values.items() if k not in used_keys}

    if not remaining:
        return

    body = item.get("request", {}).get("body")
    if isinstance(body, dict) and body.get("mode") == "raw" and body.get("raw"):
        try:
            body_obj = json.loads(body["raw"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"body.raw 不是合法 JSON，無法套用剩餘的填值 {remaining!r}: {exc}"
            ) from exc
        if isinstance(body_obj, dict):
            for key, value in remaining.items():
                try:
                    set_nested_value(body_obj, key, value)
                except TypeError as exc:
                    raise ValueError(
                        f"填值 {key!r} 與既有 body 結構衝突，無法寫入: {exc}"
                    ) from exc
            body["raw"] = json.dumps(body_obj, ensure_ascii=False, indent=2)
        else:
            raise ValueError(
                f"body 根層級不是 JSON object（實際型別="
                f"{type(body_obj).__name__}），欄位級填值語意不適用，"
                f"剩餘未套用的值: {remaining!r}"
            )


def _scalar_param_value(value: Any, param_name: str) -> str:
    """把 url variable/query 的值轉成字串；dict/list 視為填錯格式，拋
    `ValueError`（見 `_apply_values_to_item()` 說明），不要硬轉成
    Python repr 字串塞進 URL。
    """
    if isinstance(value, (dict, list)):
        raise ValueError(
            f"url 參數 {param_name!r} 收到非純量值: {value!r}；"
            f"url variable/query 只接受純量，視為套用失敗"
        )
    return str(value)


def _prune_excluded(
    items: list[dict], excluded: set[tuple[str, str]]
) -> list[dict]:
    """依 `(method, path)` 排除清單移除 item，遞迴處理並捨棄空資料夾
    （與 `collection_converter._filter_tree` 邏輯對齊，篩選條件相反）。
    """
    kept: list[dict] = []
    for item in items:
        children = item.get("item")
        if isinstance(children, list):
            new_children = _prune_excluded(children, excluded)
            if new_children:
                new_item = {**item, "item": new_children}
                kept.append(new_item)
            continue

        method = item.get("request", {}).get("method", "").upper()
        path = normalized_path_from_item(item)
        if path is not None and (method, path) in excluded:
            continue
        kept.append(item)

    return kept


# --------------------------------------------------------------------------
# 批次套用：走訪 Collection，逐 endpoint 套用人工填值或排除
# --------------------------------------------------------------------------


def apply_manual_fill_to_collections(
    *,
    openapi_spec: OpenAPISpec,
    readonly: PostmanCollection,
    mutation: PostmanCollection,
    manual_fill_dir: Path,
) -> tuple[PostmanCollection, PostmanCollection, list[dict[str, str]]]:
    """對 mutation Collection 裡需要動態值的 endpoint，套用階段一產生、
    人工填好的值；成功者就地更新，標記 `skip` 或尚未解決者移除並記錄。
    回傳更新後的 mutation Collection 與 unfilled 清單
    （`unfilled_endpoints.json` 的內容）。

    readonly Collection 原封不動回傳，不跑這套「找不到模板就排除」的
    邏輯——人工填值機制只涵蓋 mutation endpoint（見 03a 三章「人工填值
    機制」：「所有需要動態值的 mutation endpoint，一律由人工提供真實
    payload」），`manual_fill.generate_manual_fill_templates()` 本來就只對
    `MUTATION_METHODS` 產生模板，readonly 裡帶路徑/查詢參數的 GET 永遠
    找不到對應模板；曾經把這條排除邏輯也套在 readonly 上，會把這些 GET
    誤判成「尚未填值」而從 `collection_readonly.json` 剔除，牴觸 03a
    「readonly 就是只含 GET、零副作用，不會被抽走」的明文保證。

    這個函式假設階段一（`manual_fill.generate_manual_fill_templates()`）
    已經跑過、人工也已經處理完 `postman/manual_fill/` 底下的模板——但
    不強制要求全部解決：尚未解決的 endpoint 一樣走排除＋記錄路徑（見
    module docstring 「False Pass 風險」），不會讓流程中止，只是覆蓋率
    暫時性下降。`unfilled` 清單裡每筆記錄一個 `category`：`skip`（人工
    決定，見 `Decision.SKIP`）或 `retry`（尚未填值，或填了但套用失敗，
    見 03a 三章「排除結果的兩層分類」）——`retry` 的兩種細節原因會回寫
    `last_apply_error`，讓重跑時能被暫停關卡重新抓到（見
    `manual_fill.record_apply_result()`）。
    """
    readonly = copy.deepcopy(readonly)
    mutation = copy.deepcopy(mutation)
    unfilled: list[dict[str, str]] = []
    excluded: set[tuple[str, str]] = set()

    for item in iter_leaf_items(mutation.get("item", [])):
        method = item.get("request", {}).get("method", "").upper()
        path = normalized_path_from_item(item)
        if not method or path is None:
            continue

        param_schema = _operation_param_schema(openapi_spec, path, method)
        if param_schema is None:
            # 這個 operation 沒有 path/query/body 參數需要動態值
            # （例如純粹的 body-less mutation），不需要人工填值。
            continue

        endpoint = f"{method} {path}"
        manual_entry = manual_fill.read_entry(endpoint, manual_fill_dir)

        if manual_entry is None:
            excluded.add((method, path))
            unfilled.append(
                {"endpoint": endpoint, "category": "retry", "detail": "尚未完成人工填值"}
            )
            continue

        if manual_entry.decision == manual_fill.Decision.SKIP:
            excluded.add((method, path))
            unfilled.append(
                {"endpoint": endpoint, "category": "skip", "detail": "人工確認排除"}
            )
            continue

        if not manual_entry.has_value:
            excluded.add((method, path))
            unfilled.append(
                {"endpoint": endpoint, "category": "retry", "detail": "尚未完成人工填值"}
            )
            continue

        # 值已經填了（has_value=True）——即使帶著上一輪的 last_apply_error，
        # 這裡都要重新嘗試套用一次，不能只看 is_resolved 就提早排除，否則
        # 人工修正過的值永遠沒有機會被證實「這次能用」（見 03a 三章
        # 「套用失敗的重填機制」）。
        try:
            manual_fill.apply_manual_fill(item, manual_entry)
        except ValueError as exc:
            excluded.add((method, path))
            unfilled.append(
                {"endpoint": endpoint, "category": "retry", "detail": f"人工補值套用失敗: {exc}"}
            )
            manual_fill.record_apply_result(endpoint, manual_fill_dir, error=str(exc))
        else:
            manual_fill.record_apply_result(endpoint, manual_fill_dir, error=None)

    mutation["item"] = _prune_excluded(mutation.get("item", []), excluded)

    return readonly, mutation, unfilled
```


### 2.4 `chain_dependency_detect.py`——鏈式依賴偵測（map/reduce）

分組依據（依 tag、多 tag 取第一個、無 tag 進 `_untagged`）、併發數（機器核心數 − 1）、`_chunk_operations()` 分批機制、失敗處理粒度，都已在 03a 三章「鏈式依賴偵測與注入」定案，這裡只放程式碼落地方式。

`group_operations_by_tag()` 是公開函式——`manual_fill.generate_manual_fill_templates()`（見 2.6）也用同一套 tag 分組邏輯決定模板要產生進哪個 controller 檔案，避免兩處各自寫一份。

`detect_chain_dependencies()` 的實際執行流程：

1. `_condense_spec_for_chain_detection()`：剝除敘述性欄位，降低 context
2. `_exclude_endpoints()`：從 spec 拿掉人工標記 `Decision.SKIP` 的 endpoint（見 03a 三章「輸入前先過濾 Decision.SKIP 的 endpoint」），呼叫端（`__init__.py` 階段二，見三）傳入 `excluded_endpoints` 參數
3. `group_operations_by_tag()`：依 tag 分組（多 tag 取第一個，無 tag 進 `_untagged`），並在這一步展開每個 operation 的 `$ref`（見 1.4 `openapi_refs.py`、03a 三章「OpenAPI $ref 展開」）
4. `_chunk_operations()`：單一 tag 過大時依字元數切子批次
5. `_map_analyze_group()` × N：平行呼叫 Claude API 取候選，彙整所有候選 producer／consumer；候選 producer 依 `endpoint` 的 method 機械過濾，只保留 `MUTATION_METHODS`（見 03a 三章「候選 producer 僅限 mutation method」）
6. 候選 producer 與 candidate consumer 皆非空？
   - 否 → 省一次 reduce 呼叫，直接回傳空 list
   - 是 → `_reduce_phase()`：單次呼叫 Claude API 做跨 controller 最終配對，回傳 `list[ChainDependency]`

程式碼組織補充兩點：

- 注入邏輯（純程式邏輯，不呼叫 LLM）拆到 2.5 `chain_dependency_inject.py`；兩者只共用 `ChainDependency` 這個資料形狀，呼叫時機、失敗模式都不同，分開檔案。
- 失敗處理粒度延續 03a 定案：`_map_analyze_group()` 呼叫失敗即整條偵測失敗；單筆候選格式不合法只丟棄該筆，其餘照常送進 reduce（細節見函式 docstring）。`_map_analyze_group()`／`_reduce_phase()` 對 `LlmJsonError` 的處理都補上呼叫端才知道的上下文（tag／endpoint 清單，或候選筆數），跟 `LlmJsonError` 本身帶的 `raw_text`（見 1.2 `llm.py`）合起來，讓失敗訊息同時看到「送了什麼」與「模型回了什麼」。

```python
# spec_collection_agent/chain_dependency_detect.py
"""[B] Collection Agent：鏈式依賴偵測（map/reduce 兩階段），對應 03a 三章
「鏈式依賴偵測與注入（Claude API，map-reduce）」。注入邏輯在
`chain_dependency_inject.py`（分檔理由見本節前言）。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

from common.chunking import chunk_by_char_budget
from common.concurrency import default_concurrency
from common.llm_client import LlmJsonError, call_claude_for_json
from spec_collection_agent.exceptions import ChainDependencyDetectionError
from spec_collection_agent.llm import DEFAULT_MODEL
from spec_collection_agent.openapi_refs import resolve_refs
from spec_collection_agent.prompts import (
    MAP_OUTPUT_SCHEMA,
    MAP_SYSTEM_PROMPT,
    REDUCE_OUTPUT_SCHEMA,
    REDUCE_SYSTEM_PROMPT,
)
from spec_collection_agent.types import ChainDependency, MUTATION_METHODS, OpenAPISpec

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 偵測（LLM，map-reduce）
# --------------------------------------------------------------------------

# 剝除純敘述性欄位以控制 context 大小，endpoint/參數/schema 結構完整保留
# （為什麼不能整份 spec 丟給 LLM，見 03a 三章）。
_STRIP_KEYS = {"description", "example", "examples", "externalDocs", "summary"}

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}

# 併發數＝執行機器核心數 - 1（見 03a 三章「分組依據與併發數」），
# 動態計算、不寫死數字，換機器會自動跟著調整。跟 ① 解析 Agent 共用同一份
# common/concurrency.py 實作，不各自重新推導（見 00 六章「Map 階段併發數
# （共用工具）」）。
_MAX_MAP_WORKERS = default_concurrency()

# 單一 map 呼叫的軟性上限：防禦「單一 tag（controller）過度肥大」——
# 若某個 controller 底下 endpoint 數量多、schema 又複雜，依 tag 分組後
# 單次呼叫的 payload 可能過大，除了實際 token 上限的風險，更常見的問題
# 是內容太長導致模型注意力渙散、候選判斷失準（即使還在 token 限制內）。
# 用「該批次 endpoints payload 序列化後的字元數」當代理指標，比單純數
# endpoint 數量更能反映實際 context 大小——同樣是 5 個 endpoint，
# schema 複雜度可以差異巨大，只算數量會低估真正肥大的 controller。
# 門檻值是保守估計，不是精確 token 換算，真正的模型 context 限制遠大於
# 這個值；這裡刻意抓一個遠低於實際限制的保守值，留待有真實
# openapi.json 可測時依實際效果調整（呼應 03a 七章「Map 階段平行呼叫的
# 併發數...留待有真實 openapi.json 可測再評估」同樣的待調優精神），
# 因此開放環境變數覆蓋、不寫死在程式碼裡。
_MAX_CHARS_PER_MAP_CHUNK = int(
    os.environ.get("SPEC_COLLECTION_AGENT_MAP_CHUNK_CHARS", "20000")
)


def _condense_spec_for_chain_detection(openapi_spec: OpenAPISpec) -> OpenAPISpec:
    def strip(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items() if k not in _STRIP_KEYS}
        if isinstance(obj, list):
            return [strip(v) for v in obj]
        return obj

    return strip(openapi_spec)


def group_operations_by_tag(
    spec: OpenAPISpec,
) -> dict[str, list[tuple[str, str, dict]]]:
    """依 03a 三章「分組依據與併發數」把 operation 分組：一個 operation
    可能掛多個 tag，取第一個當分組依據；沒有 tag 的一律歸進 `_untagged`。

    每個 operation 在這裡就展開 `$ref`（見 03a 三章「OpenAPI $ref 展開」）
    ——用 `spec` 本身當展開來源，之後 `_chunk_operations()`／
    `_map_analyze_group()` 拿到的都已經是展開完的欄位結構，不需要再各自
    處理一次。

    公用函式，不只給 map-reduce 用：`manual_fill.generate_manual_fill_
    templates()` 也用同一套 tag 分組邏輯決定「這個 endpoint 該產生進哪個
    controller 的模板檔」，避免兩處各自寫一份、日後分組規則跑掉（見 03a
    三章「人工填值機制」階段一）。呼叫端可以傳未剝除敘述性欄位的原始
    spec（人工填值模板需要完整描述），也可以傳
    `_condense_spec_for_chain_detection()` 剝除過的版本（map-reduce 送給
    LLM 用）——本函式不關心傳進來的是哪一種，只依 `paths`／`tags` 分組。
    """
    groups: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            resolved_operation = resolve_refs(operation, spec)
            tags = operation.get("tags") or ["_untagged"]
            groups[tags[0]].append((path, method.upper(), resolved_operation))
    return groups


def _exclude_endpoints(
    spec: OpenAPISpec, excluded: frozenset[tuple[str, str]]
) -> OpenAPISpec:
    """回傳拿掉 `excluded`（`(method, path)` 組成，method 大寫）之後的
    spec 副本，給人工標記 `Decision.SKIP` 的 endpoint 用——在 map 階段
    開始分析之前就先排除，不會被當成候選 producer/consumer（見 03a 三章
    「輸入前先過濾 Decision.SKIP 的 endpoint」）。不修改傳入的 spec 本體。
    """
    if not excluded:
        return spec
    new_paths: dict[str, Any] = {}
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            new_paths[path] = path_item
            continue
        kept_methods = {
            method: operation
            for method, operation in path_item.items()
            if method.lower() not in _HTTP_METHODS
            or (method.upper(), path) not in excluded
        }
        if kept_methods:
            new_paths[path] = kept_methods
    return {**spec, "paths": new_paths}


def _chunk_operations(
    operations: list[tuple[str, str, dict]],
) -> list[list[tuple[str, str, dict]]]:
    """把單一 tag 底下的 operation 依累積 payload 字元數
    （`_MAX_CHARS_PER_MAP_CHUNK`）切成多個子批次，避免單一 controller
    過度肥大時單次 map 呼叫塞入過大的 context。子批次各自呼叫
    `_map_analyze_group()`，候選清單在 `_map_phase()` 彙整時合併，不影響
    reduce 階段正確性——reduce 是跨 endpoint 配對，一次看到所有子批次
    彙整後的完整候選清單，不受切分方式影響。

    至少保留一個 operation 一批（單一 operation 本身就超過門檻時仍自成
    一批，不拆到欄位層級）。切批次演算法本身跟 ① 解析 Agent 的 Map 階段
    共用 `common.chunking.chunk_by_char_budget()`，這裡只決定「一個
    operation 的字元數怎麼算」與門檻值（見 00 六章「Map 階段切批次
    （共用工具）」）。
    """
    return chunk_by_char_budget(
        operations,
        size_of=lambda op: len(json.dumps(op[2], ensure_ascii=False)),
        budget=_MAX_CHARS_PER_MAP_CHUNK,
    )


@dataclass(frozen=True)
class _CandidateProducer:
    """Map 階段的候選 producer 欄位，對應 03a 三章 Map 階段表格。"""

    endpoint: str  # 如 "POST /api/v1/users"
    field_path: str  # 如 "data.id"
    hint: str = ""  # 簡短語意提示，幫助 reduce 階段在多個候選間判斷


@dataclass(frozen=True)
class _CandidateConsumer:
    """Map 階段的候選 consumer 參數，對應 03a 三章 Map 階段表格。"""

    endpoint: str  # 如 "GET /api/v1/users/{id}"
    param_name: str
    param_location: str  # "path" / "query" / "body"
    hint: str = ""


def _map_analyze_group(
    tag: str, operations: list[tuple[str, str, dict]]
) -> tuple[list[_CandidateProducer], list[_CandidateConsumer]]:
    """對單一 tag/controller 呼叫 Claude，取得候選 producer/consumer。

    `MAP_OUTPUT_SCHEMA`（output_config.format）已經保證回傳結構——
    `candidate_producers`／`candidate_consumers` 一定存在、一定是必要欄位
    齊全的物件陣列（`param_location` 也鎖死在三個合法值之一），不需要再
    逐筆 isinstance 檢查、丟棄格式不合法的候選。呼叫失敗（例如傳輸層錯誤）才拋
    `ChainDependencyDetectionError`（整條偵測失敗，不做局部降級——漏偵測
    到的依賴沒有其他機制能發現），並補上呼叫端才知道的資訊——這次送了
    哪個 tag、幾個 endpoint——讓失敗訊息同時看得到「送了什麼」與「模型
    回了什麼」。
    """
    endpoints_payload = [
        {"endpoint": f"{method} {path}", "operation": operation}
        for path, method, operation in operations
    ]
    user_prompt = (
        f"controller (tag): {tag}\n\n"
        f"endpoints:\n{json.dumps(endpoints_payload, ensure_ascii=False)}\n"
    )

    try:
        result = call_claude_for_json(
            system_prompt=MAP_SYSTEM_PROMPT, user_prompt=user_prompt, schema=MAP_OUTPUT_SCHEMA, model=DEFAULT_MODEL
        )
    except LlmJsonError as exc:
        endpoint_names = [f"{method} {path}" for path, method, _ in operations]
        raise ChainDependencyDetectionError(
            f"鏈式依賴偵測 map 階段呼叫失敗（tag={tag}，送出 {len(operations)} 個 "
            f"endpoint: {endpoint_names}）: {exc}"
        ) from exc

    # GET/HEAD 依定義不會建立新資料，其回應要嘛是固定不變的靜態參照資料
    # （該走人工填值，不是鏈式依賴），要嘛就不該被當成「這次測試執行才
    # 產生」的資料來源；這裡不靠語意判斷、只看 method 就能 100% 排除，
    # 在 Reduce 階段看到之前先濾掉，避免被誤配對成 producer。
    producers = [
        _CandidateProducer(
            endpoint=entry["endpoint"],
            field_path=entry["field_path"],
            hint=entry.get("hint", ""),
        )
        for entry in result["candidate_producers"]
        if entry["endpoint"].split(" ", 1)[0] in MUTATION_METHODS
    ]
    consumers = [
        _CandidateConsumer(
            endpoint=entry["endpoint"],
            param_name=entry["param_name"],
            param_location=entry["param_location"],
            hint=entry.get("hint", ""),
        )
        for entry in result["candidate_consumers"]
    ]

    return producers, consumers


def _map_phase(
    condensed_spec: OpenAPISpec,
) -> tuple[list[_CandidateProducer], list[_CandidateConsumer]]:
    """依 tag 拆分、平行呼叫 Claude API 取得候選清單（見 03a 三章
    「Map 階段（依 OpenAPI tag／controller 拆分，可平行呼叫 Claude
    API）」）。過度肥大的 tag 會先被 `_chunk_operations()` 依 payload
    字元數再切成多個子批次，子批次各自送一次 map 呼叫；tag 名稱本身
    不變（子批次只是同一個 controller 分批分析，語意上仍是同一個
    controller，不是新的分組維度），只在候選內容彙整、worker 併發數
    計算時被當成獨立的呼叫單位。任一批次呼叫失敗會讓整個 map 階段失敗
    並往上拋；失敗當下立刻取消其餘**尚未開始**的批次（`cancel_futures=
    True`），避免既然整條偵測都要中止了，還繼續燒已經確定不會被使用的
    API 額度——已經在執行中的呼叫無法中途打斷（Python thread 沒有強制
    終止機制），仍會跑完，但至少排隊中、還沒拿到 worker 的不會再被送
    出去。
    """
    groups = group_operations_by_tag(condensed_spec)
    if not groups:
        return [], []

    call_units: list[tuple[str, list[tuple[str, str, dict]]]] = [
        (tag, chunk) for tag, ops in groups.items() for chunk in _chunk_operations(ops)
    ]

    all_producers: list[_CandidateProducer] = []
    all_consumers: list[_CandidateConsumer] = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_MAX_MAP_WORKERS, len(call_units))
    ) as pool:
        futures = {
            pool.submit(_map_analyze_group, tag, ops): tag for tag, ops in call_units
        }
        try:
            for future in concurrent.futures.as_completed(futures):
                producers, consumers = future.result()  # 失敗時原地重新拋出
                all_producers.extend(producers)
                all_consumers.extend(consumers)
        except Exception:
            pool.shutdown(wait=False, cancel_futures=True)
            raise

    return all_producers, all_consumers


def _reduce_phase(
    producers: list[_CandidateProducer], consumers: list[_CandidateConsumer]
) -> list[ChainDependency]:
    """彙整所有候選、跨 controller 做最終配對（見 03a 三章「Reduce 階段
    （單次呼叫 Claude API，輸入為 map 階段濃縮後的候選清單）」）。輸出
    是最終權威結果。`REDUCE_OUTPUT_SCHEMA`（output_config.format）已經
    保證頂層是陣列、每筆都是五個必要欄位齊全的物件，不需要再逐筆檢查
    型別或補救缺欄位；呼叫失敗才拋 `ChainDependencyDetectionError`。
    """
    payload = {
        "candidate_producers": [asdict(p) for p in producers],
        "candidate_consumers": [asdict(c) for c in consumers],
    }
    user_prompt = json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        result = call_claude_for_json(
            system_prompt=REDUCE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema=REDUCE_OUTPUT_SCHEMA,
            model=DEFAULT_MODEL,
        )
    except LlmJsonError as exc:
        raise ChainDependencyDetectionError(
            f"鏈式依賴偵測 reduce 階段呼叫失敗（candidate_producers={len(producers)} 筆、"
            f"candidate_consumers={len(consumers)} 筆）: {exc}"
        ) from exc

    return [
        ChainDependency(
            producer_endpoint=entry["producer_endpoint"],
            producer_field=entry["producer_field"],
            consumer_endpoint=entry["consumer_endpoint"],
            consumer_param=entry["consumer_param"],
            env_var_name=entry["env_var_name"],
        )
        for entry in result
    ]


def detect_chain_dependencies(
    openapi_spec: OpenAPISpec,
    *,
    excluded_endpoints: frozenset[tuple[str, str]] = frozenset(),
) -> list[ChainDependency]:
    """偵測 openapi_spec 裡的鏈式依賴，拆成 map／reduce 兩階段（見 03a
    三章）。輸出格式見 03a 三章表格；找不到依賴回傳空 list（合法結果）。
    任一階段的 LLM 呼叫失敗或格式不合法拋出 `ChainDependencyDetectionError`
    （硬性失敗，見 exceptions.py）。

    `excluded_endpoints`：人工標記 `Decision.SKIP` 的 `(method, path)`
    集合（method 大寫），在 map 階段開始分析之前先從 spec 拿掉，不會被
    當成候選 producer/consumer（見 03a 三章「人工填值機制」階段二）。
    """
    condensed = _condense_spec_for_chain_detection(openapi_spec)
    condensed = _exclude_endpoints(condensed, excluded_endpoints)
    producers, consumers = _map_phase(condensed)

    if not producers or not consumers:
        # map 階段任一邊是空的，reduce 無論如何都配不出東西，省一次
        # API 呼叫（見 03a 三章 Reduce 階段）。
        logger.info(
            "map 階段候選 producer=%d 筆、candidate consumer=%d 筆，"
            "至少一邊為空，略過 reduce 呼叫",
            len(producers),
            len(consumers),
        )
        return []

    return _reduce_phase(producers, consumers)
```


### 2.5 `chain_dependency_inject.py`——鏈式依賴注入

對應 03a 三章「注入機制」，純程式邏輯，不呼叫 LLM。五個函式：`_is_valid_js_field_path()` 防呆 `producer_field` 裡的裸陣列索引語法；`_append_capture_script()` 在 producer item 加測試腳本；`_rewrite_param_to_env_var()` 改寫 consumer 參數（型別感知、巢狀路徑，見函式 docstring）；`_build_chain_verification_item()` 處理 GET consumer 的新增 item；`_stable_topological_order()` 做單一 folder 內拓樸排序，確保 producer 先執行，循環依賴直接拋例外。**前提**：呼叫前 `mutation` 須已跑過 `folder_grouper.group_mutation_folders()`；找不到 producer/consumer item、所屬 folder、巢狀路徑，或 `producer_field` 含不合法的裸陣列索引，一律記 warning 並跳過該筆依賴，不中止整個流程。

**實測撞到的裸陣列索引問題**：`MAP_SYSTEM_PROMPT` 沒規定 response 欄位在陣列底下時 `field_path` 該怎麼寫，實測 LLM 會生出 `"data.exam[].randomId"` 這種裸中括號記法——這段路徑會被直接組進 `json.{field_path}` 形式的 JavaScript 讀取語句，裸中括號不是合法語法，Newman 執行到這行會直接拋語法錯誤，而且完全沒有任何警告，是實測中影響範圍最廣、也最隱蔽的一個問題。`_is_valid_js_field_path()` 偵測到裸 `[]` 就整筆依賴跳過（不猜測補成 `[0]`），並記警告——不修正 prompt 本身（那只降低觸發頻率，不保證杜絕），這裡才是真正防住問題的一層。

```python
# spec_collection_agent/chain_dependency_inject.py
"""[B] Collection Agent：把鏈式依賴偵測結果（`list[ChainDependency]`）套用
到 Postman Collection 上，對應 03a 三章「注入機制」。純程式邏輯，不呼叫
LLM；偵測邏輯在 `chain_dependency_detect.py`。

前提：呼叫 `inject_chain_scripts()` 前 `mutation` 須已跑過
`folder_grouper.group_mutation_folders()`（分組依據就是這裡要注入的
依賴關係本身）。
"""
from __future__ import annotations

import copy
import json
import logging
import re

from spec_collection_agent.postman_tree import (
    find_containing_top_level_folder,
    find_item,
    get_nested_value,
    has_nested_key,
    set_nested_value,
)
from spec_collection_agent.types import MUTATION_METHODS, ChainDependency, PostmanCollection

logger = logging.getLogger(__name__)

# MAP_SYSTEM_PROMPT 沒規定 response 欄位在陣列內時 field_path 該怎麼寫，
# 實測 LLM 會生出 "data.exam[].randomId" 這種裸中括號記法——直接嵌進
# capture script 的 `json.{producer_field}` 會是不合法 JavaScript，
# Newman 執行到這行會直接拋語法錯誤。裸 `[]`（不含索引）在任何情境下都
# 不是合法的屬性存取語法，偵測到就整筆依賴跳過，不猜測補成 `[0]`（跟
# value_filler.py「填不出安全值就排除，不硬撐」是同一個判斷：猜錯索引
# 比不注入更難察覺）。
_BARE_ARRAY_INDEX_PATTERN = re.compile(r"\[\s*\]")


def _is_valid_js_field_path(field_path: str) -> bool:
    return not _BARE_ARRAY_INDEX_PATTERN.search(field_path)


# --------------------------------------------------------------------------
# 注入（純程式邏輯）
# --------------------------------------------------------------------------


def _append_capture_script(item: dict, dep: ChainDependency) -> None:
    """在 producer item 加測試腳本，把 response 欄位存進 environment
    variable（格式見 03a 三章「注入機制」範例）。
    """
    script_lines = [
        f'pm.test("capture {dep.env_var_name}", function () {{',
        "    const json = pm.response.json();",
        f'    pm.environment.set("{dep.env_var_name}", json.{dep.producer_field});',
        "});",
    ]
    events = item.setdefault("event", [])
    events.append(
        {"listen": "test", "script": {"type": "text/javascript", "exec": script_lines}}
    )


def _rewrite_param_to_env_var(item: dict, param_name: str, env_var_name: str) -> None:
    """把 consumer item 裡對應參數的值改成引用 environment variable。

    body 需要型別感知處理，url variable／query 不需要（後者本來就是
    字串 schema）：body 走 `json.dumps()` 序列化，若原值是數值／布林，
    直接換成字串 placeholder 會讓該欄位被序列化成帶引號的字串，newman
    只做逐字串替換不會脫引號，Java 端型別嚴格時可能因此回 400——這個
    錯誤狀態碼一旦被 Agent ② 錄成 golden output 就是 False Pass（見
    value_filler.py module docstring）。做法：只在原值是數值/布林時，
    把 `json.dumps()` 產生的帶引號 placeholder 脫成不帶引號；字串型原值
    （如 UUID）維持帶引號，不無條件脫引號。

    `param_name` 若是巢狀 body 路徑（如 `"user.id"`），用
    `postman_tree.has_nested_key()`／`get_nested_value()`／
    `set_nested_value()` 定位，不能用淺層 `in` 存在性檢查（對巢狀路徑
    永遠 False，會讓函式靜默跳過、consumer 留著過期靜態值，是比拋例外
    更隱蔽的 False Pass 風險）。找不到路徑或路徑衝突時記 warning 並跳過
    這筆依賴，不中止整個注入流程（與 `inject_chain_scripts()` 對找不到
    item 的處理方式一致，見 2.5 節前言）。

    `param_name` 若同時出現在多個 url variable／query item（OpenAPI
    陣列型 query 參數展開的結果，如 `?status=A&status=B`），全部改寫成
    同一個 environment variable 引用，不是只改第一筆找到的——跟
    `value_filler._apply_values_to_item()` 對同一種資料形狀的處理方式
    保持一致，避免部分項目引用了變數、部分項目留著舊的靜態值。
    """
    placeholder = f"{{{{{env_var_name}}}}}"

    url = item.get("request", {}).get("url")
    matched_in_url = False
    if isinstance(url, dict):
        for var in url.get("variable", []):
            if var.get("key") == param_name:
                var["value"] = placeholder
                matched_in_url = True
        for query in url.get("query", []):
            if query.get("key") == param_name:
                query["value"] = placeholder
                matched_in_url = True

    if matched_in_url:
        return

    body = item.get("request", {}).get("body")
    if isinstance(body, dict) and body.get("mode") == "raw" and body.get("raw"):
        try:
            body_obj = json.loads(body["raw"])
        except (TypeError, ValueError):
            return
        if not isinstance(body_obj, dict):
            # body 根層是 array（如 manual_fill 的 raw_body 模式）：現有
            # 巢狀路徑改寫機制以 object 為前提，無法定位陣列內對應元素的
            # 欄位，不猜測要 broadcast 到所有元素還是只改第一筆——兩種
            # endpoint 的實際資料形狀不同構（有的整批共用同一個值、有的
            # 每筆都不同），猜錯比不注入更難察覺。記警告讓這個限制看得
            # 見，不要靜默留著過期的靜態值。
            logger.warning(
                "consumer body 根層不是 JSON object（實際型別=%s），"
                "無法用巢狀路徑定位 %r，跳過這筆鏈式依賴注入",
                type(body_obj).__name__, param_name,
            )
            return
        if has_nested_key(body_obj, param_name):
            try:
                original_value = get_nested_value(body_obj, param_name)
                set_nested_value(body_obj, param_name, placeholder)
            except (KeyError, TypeError):
                logger.warning(
                    "consumer_param %r 與 body 結構不符，跳過這筆鏈式依賴注入",
                    param_name,
                )
                return
            raw = json.dumps(body_obj, ensure_ascii=False, indent=2)
            if isinstance(original_value, (int, float, bool)):
                # 原值是數值/布林：脫掉 json.dumps() 加上的引號，讓
                # placeholder 以未加引號的形式出現在 raw text 裡，newman
                # 替換後保持數值/布林型別。
                raw = raw.replace(f'"{placeholder}"', placeholder)
            body["raw"] = raw


def _build_chain_verification_item(
    dep: ChainDependency, template_item: dict | None
) -> dict:
    """組出鏈式驗證用的 GET item，優先複製 readonly 裡既有版本（保留
    header／auth）；找不到範本才退回最小可用版本（理論上不應發生，見
    03a 三章分類規則保證每個 GET 都在 readonly）。
    """
    if template_item is not None:
        item = copy.deepcopy(template_item)
    else:
        logger.warning(
            "readonly collection 裡找不到 %s，改用最小可用版本組出鏈式驗證 item",
            dep.consumer_endpoint,
        )
        path_segments = [s for s in dep.consumer_path.strip("/").split("/") if s]
        item = {
            "name": dep.consumer_endpoint,
            "request": {
                "method": dep.consumer_method,
                "url": {
                    "raw": f"{{{{baseUrl}}}}/{'/'.join(path_segments)}",
                    "host": ["{{baseUrl}}"],
                    "path": path_segments,
                },
            },
        }

    item["name"] = f'{item.get("name", dep.consumer_endpoint)}（鏈式依賴驗證）'
    _rewrite_param_to_env_var(item, dep.consumer_param, dep.env_var_name)
    return item


def _stable_topological_order(n: int, edges: list[tuple[int, int]]) -> list[int]:
    """對 0..n-1 節點做拓樸排序（邊代表 u 須排在 v 之前），無限制的節點
    盡量維持原始相對順序。循環依賴直接拋出，不默默吃掉。
    """
    import heapq

    graph: list[list[int]] = [[] for _ in range(n)]
    indegree = [0] * n
    for u, v in edges:
        graph[u].append(v)
        indegree[v] += 1

    heap = [i for i in range(n) if indegree[i] == 0]
    heapq.heapify(heap)
    order: list[int] = []

    while heap:
        u = heapq.heappop(heap)
        order.append(u)
        for v in graph[u]:
            indegree[v] -= 1
            if indegree[v] == 0:
                heapq.heappush(heap, v)

    if len(order) != n:
        raise ValueError("鏈式依賴之間出現循環，folder 內無法排出合法執行順序")

    return order


def _reorder_top_level_folders(
    top_level_items: list[dict],
    ordering_constraints: list[tuple[dict, dict, dict]],
) -> None:
    """依 producer/consumer 關係對受影響的頂層 folder 做拓樸排序，範圍
    限定單一 folder 內（見 03a 三章「folder 內排序」）。就地修改
    `folder["item"]`。
    """
    edges_by_folder: dict[int, tuple[dict, list[tuple[dict, dict]]]] = {}
    for folder, producer_item, consumer_item in ordering_constraints:
        key = id(folder)
        if key not in edges_by_folder:
            edges_by_folder[key] = (folder, [])
        edges_by_folder[key][1].append((producer_item, consumer_item))

    for folder, pairs in edges_by_folder.values():
        items: list[dict] = folder.get("item", [])
        index_of = {id(item): idx for idx, item in enumerate(items)}

        edges: list[tuple[int, int]] = []
        for producer_item, consumer_item in pairs:
            p_idx = index_of.get(id(producer_item))
            c_idx = index_of.get(id(consumer_item))
            if p_idx is None or c_idx is None:
                continue
            if p_idx == c_idx:
                # producer_endpoint 與 consumer_endpoint 是同一個 item
                # （method+path 相同）時，find_item() 會回傳同一個物件，
                # 產生自環。同一個 item 本來就只執行一次，沒有「排在自己
                # 之前」的排序意義，過濾掉即可，不視為循環依賴。
                continue
            edges.append((p_idx, c_idx))

        order = _stable_topological_order(len(items), edges)
        folder["item"] = [items[i] for i in order]


def inject_chain_scripts(
    *,
    mutation: PostmanCollection,
    readonly: PostmanCollection,
    chain_dependencies: list[ChainDependency],
) -> tuple[PostmanCollection, PostmanCollection]:
    """套用鏈式依賴偵測結果：注入 capture 腳本、改寫 consumer 參數、視情況
    新增鏈式驗證用 GET item、對受影響 folder 做拓樸排序（見 03a 三章
    「注入機制」步驟 1、2）。

    前提：`mutation` 已完成 folder_grouper 分組，producer/consumer 已在
    同一頂層 folder（分組依據即鏈式依賴偵測結果）。`readonly` 不會被
    修改，回傳只是與 `apply_manual_fill_to_collections()` 簽章對稱。
    """
    mutation = copy.deepcopy(mutation)
    readonly = copy.deepcopy(readonly)

    ordering_constraints: list[tuple[dict, dict, dict]] = []

    for dep in chain_dependencies:
        if not _is_valid_js_field_path(dep.producer_field):
            logger.warning(
                "producer_field 含不合法的裸陣列索引語法（%r），"
                "capture script 會是不合法 JavaScript，跳過這筆鏈式依賴: %s -> %s",
                dep.producer_field, dep.producer_endpoint, dep.consumer_endpoint,
            )
            continue

        producer_item = find_item(
            mutation.get("item", []), dep.producer_method, dep.producer_path
        )
        if producer_item is None:
            # producer 理論上必在 mutation collection；找不到通常是該
            # endpoint 已被 value_filler 排除，記警告並跳過，不中止整個 [B]。
            logger.warning("找不到鏈式依賴的 producer item: %s", dep.producer_endpoint)
            continue

        _append_capture_script(producer_item, dep)

        folder = find_containing_top_level_folder(mutation.get("item", []), producer_item)
        if folder is None:
            logger.warning(
                "producer item 不屬於任何頂層 folder（folder_grouper 尚未分組？）: %s",
                dep.producer_endpoint,
            )
            continue

        if dep.consumer_method in MUTATION_METHODS:
            consumer_item = find_item(
                mutation.get("item", []), dep.consumer_method, dep.consumer_path
            )
            if consumer_item is None:
                logger.warning(
                    "找不到鏈式依賴的 consumer item: %s", dep.consumer_endpoint
                )
                continue
            _rewrite_param_to_env_var(consumer_item, dep.consumer_param, dep.env_var_name)
        else:
            template_item = find_item(
                readonly.get("item", []), dep.consumer_method, dep.consumer_path
            )
            consumer_item = _build_chain_verification_item(dep, template_item)
            folder.setdefault("item", []).append(consumer_item)

        ordering_constraints.append((folder, producer_item, consumer_item))

    _reorder_top_level_folders(mutation.get("item", []), ordering_constraints)

    return mutation, readonly
```

### 2.6 `manual_fill.py`——人工填值機制

對應 03a 三章「人工填值機制」全節。階段一（`generate_manual_fill_templates()`）在 `openapi.json` 落地後立刻主動產生全部模板，依 controller（OpenAPI `tags`）分檔案，不是每個 endpoint 一份，強制涵蓋全部待填 endpoint：

- `postman/manual_fill/<controller>.json`，一個 controller 一份檔案，內含該 controller 底下全部待填 endpoint 的 `ManualFillEntry` 陣列（見 `_entry_to_dict()`／`_entry_from_dict()`）
- **模板永久保留、不刪除**——沒有刪除模板的函式；`list_pending()`／`list_skipped()` 逐筆讀 `ManualFillEntry.is_resolved`／`decision` 判斷，不靠「檔案存不存在」判斷（見 03a 三章「模板不刪除」）
- 三種填值模式（`fill_mode`）：`fields`（body 是 object，點號路徑 `values`）／`raw_body`（body 根層級是 array，整包覆蓋）／`file_upload`（`multipart/form-data`，見 `_apply_file_upload()`）——`_default_fill_mode()` 依 `param_schema` 的 body 形狀自動決定預設模式
- `decision`（`fill`／`skip`）：`skip` 是人工主動判斷「這個 endpoint 不該走一般重構驗證流程」的編輯決定，不是「填不出值、放棄」（見 03a 三章「Decision.SKIP 的語意」）
- `note` 欄位是選填的人工註記——沒有 LLM 填值這個步驟，不需要記錄系統自動填的失敗訊息
- `last_apply_error`：系統寫入的欄位，記錄上一次套用失敗的原因，`record_apply_result()` 在每次套用嘗試後回寫（成功清空、失敗記錄）；`has_value`（值填了沒，不看 `last_apply_error`）與 `is_resolved`（這一輪算不算解決，`decision=skip` 或「值填了且上次沒失敗」）是兩個不同的屬性，見下方 `ManualFillEntry` 定義與 03a 三章「套用失敗的重填機制」

**`fill_mode` 分派用 `match`，不是 `if/elif`**：三種模式處理的資料形狀完全不同，彼此不共用邏輯，`match-case`（Python 3.10+，本專案執行環境為 3.13）讓分支邊界更明確，之後要加第四種模式也只需要多一個 `case`。

**刻意不引入 Pydantic 做 `raw_body`／`file_paths` 的 schema 驗證**：人工填的值是當下手動確認過的資料，只使用一次，過度驗證只是徒增依賴——跟 01 三章「用 TypedDict 而非 Pydantic」判斷一致。`_apply_raw_body()`／`_apply_file_upload()` 只做最基本檢查（序列化得出來、檔案路徑真的存在），格式對不對由人工自己負責，跟 `_apply_values_to_item()`（2.3）的 `ValueError` 風格一致。

```python
# spec_collection_agent/manual_fill.py
"""[B] Collection Agent：人工填值機制，對應 03a 三章「人工填值機制」全節。

階段一（`generate_manual_fill_templates()`）在 [A] Spec Agent 產出
`openapi.json` 後立刻執行，對每個需要動態值的 mutation endpoint 主動產生
模板，依 OpenAPI `tags` 分 controller、一個 controller 一份檔案，強制涵蓋
全部、不是「LLM 猜失敗才生成」。人工填完後才進入階段二（見
`spec_collection_agent/__init__.py`）：鏈式依賴偵測（先過濾標記 `skip`
的 endpoint）、folder 分組、套用人工填值（`value_filler.
apply_manual_fill_to_collections()`）、注入、寫出。

模板檔案**永久保留、不刪除**——鏈式依賴注入、newman 執行等後續流程萬一
發現問題，人工填過的原始資料要能找得回來；保留下來本身也是一份可稽核
的紀錄。連帶影響：`list_pending()` 不能再用「檔案存不存在」判斷 pending，
要逐筆讀模板內容依 `ManualFillEntry.is_resolved` 判斷。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# spec_collection_agent/ 相對於 orchestrator 專案根目錄的深度是 1 層
# （沿用 java_service.py 的既有慣例），用來把 file_upload 的相對路徑
# 解析成不依賴 cwd 的絕對路徑。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FillMode(str, Enum):
    FIELDS = "fields"  # body 是 object：填 values（點號路徑）
    RAW_BODY = "raw_body"  # body 不是 object（如陣列）：填 raw_body，整包覆蓋
    FILE_UPLOAD = "file_upload"  # multipart/form-data：填 file_paths


class Decision(str, Enum):
    FILL = "fill"  # 預設：等待人工填 values／raw_body／file_paths
    # 人工主動判斷這個 endpoint 是特例，不走一般重構驗證流程（例如依賴
    # OCR、語音辨識這類需要真實內容才有意義的處理）——不是「填不出值、
    # 放棄」，是編輯決定，見 03a 三章「Decision.SKIP 的語意」。
    SKIP = "skip"


@dataclass
class ManualFillEntry:
    """對應 controller 模板檔案裡 `endpoints` 陣列的單筆項目。"""

    endpoint: str
    param_schema: dict[str, Any]
    # 選填，人工自己寫的註記（例如為什麼這個 endpoint 要 skip）。取代
    # 舊版的 `error` 欄位——舊欄位記錄「LLM 為什麼填值失敗」，現在沒有
    # LLM 嘗試這件事，沒有系統自動填的失敗訊息可記錄。
    note: str | None = None
    fill_mode: FillMode = FillMode.FIELDS
    values: dict[str, Any] = field(default_factory=dict)
    raw_body: Any = None
    file_paths: dict[str, str] = field(default_factory=dict)
    decision: Decision = Decision.FILL
    # 系統寫入，人工不用填也不用清（見 record_apply_result()）。記錄上一次
    # 嘗試把這個 entry 的值套進 Postman item 時失敗的原因；`None` 代表沒
    # 試過，或上次成功。跟 `decision=skip` 是兩種不同的排除分類——`skip`
    # 是人工的編輯決定，這個欄位代表「填了但套不進去，屬於 retry」（見
    # 03a 三章「排除結果的兩層分類」「套用失敗的重填機制」）。
    last_apply_error: str | None = None

    @property
    def has_value(self) -> bool:
        """依 `fill_mode` 判斷人工是否已經提供對應的值，**不考慮**
        `last_apply_error`——單純回答「值填了沒」。跟 `is_resolved` 是
        不同的問題：一個帶著上一輪 `last_apply_error` 的 entry 可能
        `has_value=True`（值填了）但 `is_resolved=False`（上次套用失敗，
        還沒證實這次能用）。`value_filler.apply_manual_fill_to_collections()`
        用這個屬性判斷「值填了沒有」以決定要不要嘗試套用，`is_resolved`
        則用在 `list_pending()`/暫停關卡這類「這一輪算不算完成」的判斷。
        """
        match self.fill_mode:
            case FillMode.FIELDS:
                return bool(self.values)
            case FillMode.RAW_BODY:
                return self.raw_body is not None
            case FillMode.FILE_UPLOAD:
                return bool(self.file_paths)
        return False

    @property
    def is_resolved(self) -> bool:
        """人工是否已經給出可用的答案（填值或明確跳過）。`decision=skip`
        一律視為已解決；`decision=fill` 時除了值已填（`has_value`），還要
        上一次套用沒有失敗（`last_apply_error is None`）才算——套用失敗
        會讓已填值的 entry 變回未解決，跟「尚未填值」是同一種 retry 狀態。
        """
        if self.decision == Decision.SKIP:
            return True
        return self.has_value and self.last_apply_error is None


def _sanitize_controller(controller: str) -> str:
    """把 controller tag（如 "exam-controller"）轉成安全的檔名。"""
    return re.sub(r"[^A-Za-z0-9]+", "_", controller).strip("_")


def _controller_file_path(controller: str, manual_fill_dir: Path) -> Path:
    return manual_fill_dir / f"{_sanitize_controller(controller)}.json"


def _entry_to_dict(entry: ManualFillEntry) -> dict[str, Any]:
    return {
        "endpoint": entry.endpoint,
        "param_schema": entry.param_schema,
        "note": entry.note,
        "fill_mode": entry.fill_mode.value,
        "values": entry.values,
        "raw_body": entry.raw_body,
        "file_paths": entry.file_paths,
        "decision": entry.decision.value,
        "last_apply_error": entry.last_apply_error,
    }


def _entry_from_dict(raw: dict[str, Any]) -> ManualFillEntry:
    return ManualFillEntry(
        endpoint=raw["endpoint"],
        param_schema=raw.get("param_schema", {}),
        note=raw.get("note"),
        fill_mode=FillMode(raw.get("fill_mode", "fields")),
        values=raw.get("values") or {},
        raw_body=raw.get("raw_body"),
        file_paths=raw.get("file_paths") or {},
        decision=Decision(raw.get("decision", "fill")),
        last_apply_error=raw.get("last_apply_error"),
    )


def _default_fill_mode(param_schema: dict[str, Any]) -> FillMode:
    """依 body 實際形狀決定模板產生時的預設 fill_mode（見 03a 三章
    「ManualFillEntry 欄位」）：`multipart/form-data` → `file_upload`；
    body 根層是 array → `raw_body`；其餘（object 或沒有 body、只有
    path/query 參數）→ `fields`。
    """
    request_body = param_schema.get("requestBody")
    if not isinstance(request_body, dict):
        return FillMode.FIELDS
    content = request_body.get("content")
    if not isinstance(content, dict):
        return FillMode.FIELDS
    if "multipart/form-data" in content:
        return FillMode.FILE_UPLOAD
    for media_schema in content.values():
        schema = media_schema.get("schema") if isinstance(media_schema, dict) else None
        if isinstance(schema, dict) and schema.get("type") == "array":
            return FillMode.RAW_BODY
    return FillMode.FIELDS


def generate_manual_fill_templates(
    *, openapi_spec: dict[str, Any], manual_fill_dir: Path
) -> list[str]:
    """階段一：對每個需要填值的 mutation endpoint，依 controller 產生／
    更新人工填值模板（見 03a 三章「人工填值機制」階段一）。

    **已存在的 endpoint 項目不覆寫**，只新增這次才出現的——`openapi.json`
    有變動、重跑這一步時是安全的，不會動到既有、已經處理過的部分（不管
    已解決還是尚未解決）。回傳這次新增的 endpoint 清單，給呼叫端知道有
    沒有新東西要填。
    """
    # 延遲 import 避免模組載入順序的循環依賴（chain_dependency_detect.py
    # 沒有 import 這個模組，value_filler.py 也沒有，方向是單向的，但延遲
    # import 讓相依關係在讀程式碼時更明確：這兩個函式只在產生模板時才
    # 需要）。
    from spec_collection_agent.chain_dependency_detect import group_operations_by_tag
    from spec_collection_agent.types import MUTATION_METHODS
    from spec_collection_agent.value_filler import _operation_param_schema

    manual_fill_dir.mkdir(parents=True, exist_ok=True)
    groups = group_operations_by_tag(openapi_spec)

    added: list[str] = []
    for controller, operations in groups.items():
        path = _controller_file_path(controller, manual_fill_dir)
        entries: dict[str, ManualFillEntry] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            for raw_entry in raw.get("endpoints", []):
                entry = _entry_from_dict(raw_entry)
                entries[entry.endpoint] = entry

        for op_path, method, _operation in operations:
            if method not in MUTATION_METHODS:
                continue
            param_schema = _operation_param_schema(openapi_spec, op_path, method)
            if param_schema is None:
                continue  # 沒有 path/query/body 參數，不需要人工填值

            endpoint = f"{method} {op_path}"
            if endpoint in entries:
                continue  # 已存在（不管解不解決），不覆寫

            entries[endpoint] = ManualFillEntry(
                endpoint=endpoint,
                param_schema=param_schema,
                fill_mode=_default_fill_mode(param_schema),
            )
            added.append(endpoint)

        if not entries:
            continue  # 這個 controller 沒有任何需要填值的 endpoint，不產生空檔案

        with path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "controller": controller,
                    "endpoints": [_entry_to_dict(e) for e in entries.values()],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    return added


def read_entry(endpoint: str, manual_fill_dir: Path) -> ManualFillEntry | None:
    """跨所有 controller 檔案找這個 endpoint 的模板項目。呼叫端不一定知道
    這個 endpoint 屬於哪個 controller，掃描檔案數量少（依 controller 分組，
    通常只有幾個到十幾個檔案），直接掃描比額外維護一份
    endpoint→controller 索引簡單。
    """
    if not manual_fill_dir.exists():
        return None
    for path in manual_fill_dir.glob("*.json"):
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        for raw_entry in raw.get("endpoints", []):
            if raw_entry.get("endpoint") == endpoint:
                return _entry_from_dict(raw_entry)
    return None


def list_pending(manual_fill_dir: Path) -> list[str]:
    """回傳尚未解決（既沒填值、也沒標記 skip）的 endpoint 清單，見 01 五
    章 conditional edge 判斷依據。模板不刪除，不能再用「檔案存不存在」
    判斷，逐筆讀模板內容依 `ManualFillEntry.is_resolved` 判斷。
    """
    if not manual_fill_dir.exists():
        return []
    pending: list[str] = []
    for path in sorted(manual_fill_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        for raw_entry in raw.get("endpoints", []):
            entry = _entry_from_dict(raw_entry)
            if not entry.is_resolved:
                pending.append(entry.endpoint)
    return pending


def list_skipped(manual_fill_dir: Path) -> list[str]:
    """回傳已標記 `Decision.SKIP` 的 endpoint 清單。呼叫端（`__init__.py`
    階段二）把這份清單跟 `list_pending()` 合併，一起在跑鏈式依賴偵測之前
    先過濾掉（見 03a 三章「輸入前先過濾尚未就緒的 endpoint」）——skip 與
    pending 是兩種不同的排除分類（見「排除結果的兩層分類」），各自用
    對應的函式回傳，不在這裡合併，合併留給呼叫端依用途決定。
    """
    if not manual_fill_dir.exists():
        return []
    skipped: list[str] = []
    for path in sorted(manual_fill_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        for raw_entry in raw.get("endpoints", []):
            entry = _entry_from_dict(raw_entry)
            if entry.decision == Decision.SKIP:
                skipped.append(entry.endpoint)
    return skipped


def record_apply_result(
    endpoint: str, manual_fill_dir: Path, *, error: str | None
) -> None:
    """套用人工填值後回寫這次嘗試的結果到對應模板檔案的
    `last_apply_error`——失敗記錄原因，成功則清空（見 03a 三章「套用失敗
    的重填機制」）。只在值真的改變時才寫檔，避免每次套用成功都重寫一份
    沒有變化的模板檔案。找不到對應 entry（理論上不該發生，呼叫端一定是
    先 `read_entry()` 讀到了才會走到套用這一步）時靜默略過，不拋例外。
    """
    if not manual_fill_dir.exists():
        return
    for path in manual_fill_dir.glob("*.json"):
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        target = next(
            (e for e in raw.get("endpoints", []) if e.get("endpoint") == endpoint),
            None,
        )
        if target is None:
            continue
        if target.get("last_apply_error") != error:
            target["last_apply_error"] = error
            with path.open("w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
        return


def apply_manual_fill(item: dict, entry: ManualFillEntry) -> None:
    """把人工補值套進 Postman item。

    url variable/query 跟 `fill_mode` 無關，一律先套用 `entry.values`
    裡對得上的部分（`_apply_url_values()`）——`multipart/form-data` 或
    `raw_body` 的 endpoint 常常同時帶著 query 參數（例如
    `POST /api/file/image` 的 `kind`/`randomId`/`side` 是 query 參數，
    `file` 才是 multipart body），這幾個參數不該因為 body 走
    `file_upload`／`raw_body` 模式就沒有管道被填、只能拿到
    `openapi-to-postmanv2` 產生的預設佔位值——那正是 module docstring
    講的 False Pass 風險本尊（見 03a 三章「檔案上傳」更正）。

    body 本身才依 `fill_mode` 分派——三種模式處理的資料形狀完全不同
    （逐欄位／整包覆蓋／檔案路徑），match-case 讓分支邊界更明確，之後要
    加第四種模式也只需要多一個 case。`FillMode.FIELDS` 內部會重複呼叫一次
    `_apply_url_values()`（`_apply_values_to_item()` 自己也需要用它決定
    哪些 key 剩下要寫進 body），是有意的重複、不是遺漏——兩次呼叫都是
    冪等操作，成本可忽略。
    """
    from spec_collection_agent.value_filler import _apply_url_values, _apply_values_to_item

    _apply_url_values(item, entry.values)

    match entry.fill_mode:
        case FillMode.FIELDS:
            _apply_values_to_item(item, entry.values)
        case FillMode.RAW_BODY:
            _apply_raw_body(item, entry.raw_body)
        case FillMode.FILE_UPLOAD:
            _apply_file_upload(item, entry.file_paths)
        case _:
            raise ValueError(f"不支援的 fill_mode: {entry.fill_mode!r}")


def _apply_raw_body(item: dict, raw_body: Any) -> None:
    """整包覆蓋 body.raw，供 body 根層級不是 object（如陣列）的 endpoint
    使用。不引入 Pydantic 之類的 schema 驗證套件——人工填的值是當下手動
    確認過的資料、只使用一次，不是長期需要防禦的外部輸入，過度驗證只是
    徒增依賴（跟 01 三章「用 TypedDict 而非 Pydantic」判斷一致）。
    `json.dumps()` 失敗（如值本身不可序列化）會自然拋 `TypeError`，交由
    呼叫端（`value_filler.apply_manual_fill_to_collections()`）當成套用
    失敗處理。
    """
    body = item.setdefault("request", {}).setdefault("body", {})
    body["mode"] = "raw"
    body["raw"] = json.dumps(raw_body, ensure_ascii=False, indent=2)


def _apply_file_upload(item: dict, file_paths: dict[str, str]) -> None:
    """把人工指定的檔案路徑套進 formdata 的 file 欄位（見 03a 三章
    「檔案上傳」）。`file_paths` 是「formdata 欄位名稱 → 相對於專案根
    目錄的檔案路徑」的對應。找不到對應 formdata 項目、或專案根目錄下
    找不到指定的檔案，都直接拋 `ValueError`——不要讓一個指向不存在檔案
    的 src 悄悄進 Collection，newman 執行時才用「檔案讀不到」的方式
    失敗，現場會比在這裡直接拋錯更難追。
    """
    body = item.get("request", {}).get("body")
    if not isinstance(body, dict) or body.get("mode") != "formdata":
        raise ValueError("item 的 body 不是 formdata 模式，無法套用 file_upload")

    entries_by_key = {
        entry["key"]: entry
        for entry in body.get("formdata", [])
        if entry.get("type") == "file"
    }
    for key, path_str in file_paths.items():
        formdata_entry = entries_by_key.get(key)
        if formdata_entry is None:
            raise ValueError(f"formdata 裡找不到 file 型別的欄位 {key!r}")
        file_path = _PROJECT_ROOT / path_str
        if not file_path.is_file():
            raise ValueError(f"file_paths[{key!r}] 指向的檔案不存在: {path_str}")
        formdata_entry["src"] = path_str
```

---

## 三、對外唯一入口：`__init__.py`

對應 03a 四章「對外唯一入口」：`graph/nodes/spec_node.py`、`graph/nodes/collection_node.py` 只呼叫這裡的函式，不直接 import 套件內其他模組。這是 [A]/[B] 共用的**同一個檔案**，[B] 對外分**兩個**函式，對應 03a 三章「人工填值機制」的兩階段：

- `generate_manual_fill_templates()`——階段一，落地 `openapi.json`、產生／更新人工填值模板，不呼叫 Claude API
- `run_collection_agent()`——階段二，假設模板已經人工填完，跑完剩下的 pipeline

graph 層怎麼在兩階段之間暫停等人工填值，見 `01_langgraph_architecture.md` 五章「人工填值關卡」，本節只涵蓋這兩個函式本身，不涵蓋 graph node 層的暫停機制。

### 執行順序

`generate_manual_fill_templates()`（階段一）：

```
1. 落地 openapi.json
2. manual_fill.generate_manual_fill_templates()   ── 依 controller 產生／更新模板，只新增沒出現過的 endpoint
```

`run_collection_agent()`（階段二，假設階段一已完成、人工已填值）：

```
1. 從 specs_dir/openapi.json 讀回階段一落地的 spec（不接受 openapi_spec 參數，見下方 docstring 說明）
2. manual_fill.list_skipped() ∪ list_pending()    ── 找出這一輪不會進最終 Collection 的 endpoint（skip 或尚未解決）
3. detect_chain_dependencies(excluded_endpoints=…) ── 鏈式依賴偵測，先過濾掉上一步的 endpoint
4. convert_openapi_to_postman()                  ── 轉換流程
5. split_readonly_mutation()                     ── 分類規則
6. group_mutation_folders()                      ── folder 分組（依賴步驟 3 的結果）
7. apply_manual_fill_to_collections()             ── 套用人工填值（純程式邏輯，不呼叫 API；只處理 mutation，readonly 原封不動，見 2.3）
8. inject_chain_scripts()                         ── 鏈式依賴注入（依賴步驟 6 已完成分組、步驟 7 已完成填值）
9. 寫出兩份 Collection、unfilled_endpoints.json，並回報 manual_fill_pending（見 2.6）
```

**`run_collection_agent()` 不強制要求全部填值完成才能跑**：尚未解決的 endpoint 一樣走排除＋記錄路徑（見 2.3 `apply_manual_fill_to_collections()`），只是額外回報 `manual_fill_pending` 清單，交給 graph 層（01 對應章節）決定要不要暫停等人工。重跑本身是冪等的——同一份 `openapi.json`、同一份 `manual_fill/` 內容跑出同樣結果，差別只在有沒有新填的答案。

兩項順序限制是實作層級的細節，03a 沒有明講：

- **步驟 6 先於步驟 8**：folder 分組依據來自鏈式依賴偵測結果，而注入步驟需要 folder 已存在，才知道鏈式驗證用的 GET item 該加進哪裡。
- **步驟 7 先於步驟 8**：鏈式依賴涉及的參數先由人工填值給一個值，再被注入步驟覆蓋成 environment variable 引用，兩者不衝突。反過來的話，套用人工填值時會看到已是 `{{env_var}}` 格式的參數值，容易誤判成需要覆寫的既有值。

```python
# spec_collection_agent/__init__.py
"""[A] Spec Agent / [B] Collection Agent 對外唯一入口，`graph/nodes/spec_node.py`
`graph/nodes/collection_node.py` 只呼叫這裡的函式（見 03a 四章「對外唯一
入口」）。[B] 分兩階段：`generate_manual_fill_templates()`（階段一，落地
openapi.json、產生人工填值模板）與 `run_collection_agent()`（階段二，假設
模板已經人工填完，跑完剩下的 pipeline）——見 03a 三章「人工填值機制」。
graph 層怎麼在兩階段之間暫停等人工填值，見 01_langgraph_architecture.md
五章「人工填值關卡」，本檔案只提供這兩個函式本身。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from spec_collection_agent import manual_fill
from spec_collection_agent.chain_dependency_detect import detect_chain_dependencies
from spec_collection_agent.chain_dependency_inject import inject_chain_scripts
from spec_collection_agent.collection_converter import (
    convert_openapi_to_postman,
    split_readonly_mutation,
)
from spec_collection_agent.folder_grouper import group_mutation_folders
from spec_collection_agent.java_service import JavaServiceProcess, resolve_jar_path
from spec_collection_agent.types import CollectionAgentResult, OpenAPISpec
from spec_collection_agent.value_filler import apply_manual_fill_to_collections

logger = logging.getLogger(__name__)


def run_spec_agent(
    *,
    java_jar_path: str,
    java_base_url: str,
    java_executable_path: str,
    spring_datasource_url: str,
    spring_datasource_username: str,
    spring_datasource_password: str,
) -> OpenAPISpec:
    """[A] Spec Agent：啟動 Java 服務、擷取 `/v3/api-docs`、關閉服務（對應
    03a 二章）。`spring_datasource_*`／`java_executable_path` 由呼叫端從
    環境變數讀出傳入，本函式不直接讀 `os.environ`（見 03a 五章備註）。
    """
    with JavaServiceProcess(
        jar_path=resolve_jar_path(java_jar_path),
        base_url=java_base_url,
        java_executable=java_executable_path,
        env_overrides={
            "SPRING_DATASOURCE_URL": spring_datasource_url,
            "SPRING_DATASOURCE_USERNAME": spring_datasource_username,
            "SPRING_DATASOURCE_PASSWORD": spring_datasource_password,
        },
    ) as svc:
        return svc.openapi_spec  # start() 已在 __enter__ 內呼叫，此時必不為 None


def generate_manual_fill_templates(
    *, openapi_spec: OpenAPISpec, specs_dir: Path, postman_dir: Path
) -> list[str]:
    """[B] Collection Agent 階段一：落地 `openapi.json`，依 controller 產生
    ／更新人工填值模板（見 03a 三章「人工填值機制」階段一）。不呼叫
    Claude API。

    回傳**全部**尚未解決的 endpoint 清單（不是只有這次新增的）——跟
    `run_collection_agent()` 的 `CollectionAgentResult.manual_fill_pending`
    是同一種契約，讓 graph node 層（`graph/nodes/collection_node.py`）能
    直接拿這個回傳值判斷要不要暫停等人工，不需要另外 import `manual_fill`
    模組自己再查一次（見 03a 四章「對外唯一入口」：node 只呼叫套件對外
    函式）。
    """
    specs_dir.mkdir(parents=True, exist_ok=True)
    openapi_json_path = specs_dir / "openapi.json"
    with openapi_json_path.open("w", encoding="utf-8") as f:
        json.dump(openapi_spec, f, ensure_ascii=False, indent=2)

    manual_fill_dir = postman_dir / "manual_fill"
    added = manual_fill.generate_manual_fill_templates(
        openapi_spec=openapi_spec, manual_fill_dir=manual_fill_dir
    )
    if added:
        logger.info(
            "%d 個 endpoint 新增人工填值模板，見 %s: %s",
            len(added),
            manual_fill_dir,
            added,
        )

    pending = manual_fill.list_pending(manual_fill_dir)
    if pending:
        logger.warning(
            "%d 個 endpoint 尚待人工補值，見 %s: %s",
            len(pending),
            manual_fill_dir,
            pending,
        )
    return pending


def run_collection_agent(
    *, specs_dir: Path, postman_dir: Path
) -> CollectionAgentResult:
    """[B] Collection Agent 階段二：假設 `generate_manual_fill_templates()`
    已經跑過、人工也已經處理完 `postman/manual_fill/` 底下的模板，跑完
    剩下的 pipeline（見 03a 三章「人工填值機制」階段二）：過濾尚未就緒的
    endpoint（`skip` 或尚未解決）→ 鏈式依賴偵測 → folder 分組 → 套用
    人工填值 → 注入 → 寫出。不強制要求全部解決——尚未解決的 endpoint
    一樣走排除＋記錄路徑（見 `value_filler.apply_manual_fill_to_collections()`）。

    從 `specs_dir/openapi.json` 讀回階段一落地的 spec，不再接受
    `openapi_spec` 參數——階段一、階段二可能是兩次分開的呼叫（人工填值
    期間可能間隔任意長時間），讓兩階段共用同一份已經落地的檔案當唯一
    真相來源，不依賴呼叫端每次都傳入「同一份」spec 物件這種脆弱的隱性
    假設。
    """
    specs_dir.mkdir(parents=True, exist_ok=True)
    postman_dir.mkdir(parents=True, exist_ok=True)
    manual_fill_dir = postman_dir / "manual_fill"

    openapi_json_path = specs_dir / "openapi.json"
    with openapi_json_path.open("r", encoding="utf-8") as f:
        openapi_spec: OpenAPISpec = json.load(f)

    # skip（永久排除）與 pending（尚未解決，涵蓋「還沒填」與「上次套用
    # 失敗」）是兩種不同的排除分類，但對這裡的過濾目的一樣：這一輪都不
    # 會進最終 Collection，不該浪費一次候選分析（見 03a 三章「輸入前先
    # 過濾尚未就緒的 endpoint」）。
    not_ready = set(manual_fill.list_skipped(manual_fill_dir)) | set(
        manual_fill.list_pending(manual_fill_dir)
    )
    excluded_endpoints = frozenset(
        (endpoint.split(" ", 1)[0], endpoint.split(" ", 1)[1]) for endpoint in not_ready
    )
    chain_dependencies = detect_chain_dependencies(
        openapi_spec, excluded_endpoints=excluded_endpoints
    )
    logger.info("偵測到 %d 筆鏈式依賴", len(chain_dependencies))

    raw_collection_path = postman_dir / "_raw_collection.json"
    raw_collection = convert_openapi_to_postman(openapi_json_path, raw_collection_path)

    readonly, mutation = split_readonly_mutation(raw_collection)
    mutation = group_mutation_folders(
        mutation=mutation, chain_dependencies=chain_dependencies
    )

    readonly, mutation, unfilled = apply_manual_fill_to_collections(
        openapi_spec=openapi_spec,
        readonly=readonly,
        mutation=mutation,
        manual_fill_dir=manual_fill_dir,
    )
    if unfilled:
        logger.warning(
            "%d 個 endpoint 未納入 Collection（尚未填值或人工排除），"
            "已記錄進 unfilled_endpoints.json: %s",
            len(unfilled),
            [entry["endpoint"] for entry in unfilled],
        )

    mutation, readonly = inject_chain_scripts(
        mutation=mutation, readonly=readonly, chain_dependencies=chain_dependencies
    )

    readonly_path = postman_dir / "collection_readonly.json"
    mutation_path = postman_dir / "collection_mutation.json"
    unfilled_path = postman_dir / "unfilled_endpoints.json"

    with readonly_path.open("w", encoding="utf-8") as f:
        json.dump(readonly, f, ensure_ascii=False, indent=2)
    with mutation_path.open("w", encoding="utf-8") as f:
        json.dump(mutation, f, ensure_ascii=False, indent=2)
    with unfilled_path.open("w", encoding="utf-8") as f:
        json.dump(unfilled, f, ensure_ascii=False, indent=2)

    raw_collection_path.unlink(missing_ok=True)  # 只是轉換中間產物，不列入交接範圍

    manual_fill_pending = manual_fill.list_pending(manual_fill_dir)
    if manual_fill_pending:
        logger.warning(
            "%d 個 endpoint 尚待人工補值，見 %s: %s",
            len(manual_fill_pending),
            manual_fill_dir,
            manual_fill_pending,
        )

    return CollectionAgentResult(
        collection_readonly_path=str(readonly_path),
        collection_mutation_path=str(mutation_path),
        unfilled_endpoints_path=str(unfilled_path),
        manual_fill_pending=manual_fill_pending,
    )
```


---

## 四、LangGraph node 封裝

對應 03a 五章，拆成**兩個** node，對應 03a 三章「人工填值機制」的兩階段：

| graph node id | 函式 | 對應階段 |
|---|---|---|
| `gen_manual_fill_templates` | `collection_node.run_generate_templates` | 階段一：落地 openapi.json、產生人工填值模板，不呼叫 Claude API |
| `gen_collection` | `collection_node.run`（沿用舊 node id） | 階段二：假設模板已填完，跑完剩下的 pipeline |

兩者都是線性 node，比照 01 七 stub 慣例整包展開 state；node 檔案放在專案既有的 `graph/nodes/` 目錄（非文件字面的頂層 `nodes/`），並用 `asyncio.to_thread` 包住阻塞呼叫（`npx` 子進程、Claude API 同步呼叫），避免卡住事件迴圈——與 `spec_node.py` 對 `run_spec_agent()` 的處理方式一致。

`graph/builder.py` 的佈線：`extract_spec → gen_manual_fill_templates →`（conditional：有待填 endpoint 就到 `await_manual_fill` 結束這次 run，否則）`→ gen_collection →`（conditional：仍有待填 endpoint——防禦性的第二道關卡，正常情況下階段一已經擋下——就到 `await_manual_fill`，否則）`→ record_tests`。兩個 conditional edge 都指向同一個 `await_manual_fill` 終止節點，語意上是同一件事。

### `graph/nodes/collection_node.py`

```python
# graph/nodes/collection_node.py
"""
[B] Collection Agent（程式邏輯 + 少量 LLM）
將 OpenAPI JSON 轉換為 Postman Collection（readonly / mutation）
見 03a_spec_collection_agent_architecture.md 三、03c_collection_agent_code.md

分兩個 node，對應 03a 三章「人工填值機制」的兩階段：

- `run_generate_templates`（graph node id：`gen_manual_fill_templates`）
  ——階段一，落地 openapi.json、產生人工填值模板，不呼叫 Claude API
- `run`（graph node id：`gen_collection`，沿用舊名）——階段二，假設模板
  已經人工填完，跑完剩下的 pipeline（鏈式依賴偵測、folder 分組、套用
  人工填值、注入）

兩個 node 各自搭配一個 conditional edge 判斷函式（`should_await_manual_
fill_templates_or_continue`／`should_await_manual_fill_or_continue`），
跟對應的 node 放同一個檔案（比照 refactor_harness 的
`should_debug_or_done` 跟 run_tests 節點放一起的慣例），見
01_langgraph_architecture.md 五「人工填值關卡」。兩個判斷函式都指向
同一個 `await_manual_fill` 終止節點——不管是「階段一產生完模板」還是
「階段二跑完後仍有缺口」，語意上都是同一件事：`postman/manual_fill/`
底下還有沒填完的 endpoint，交給人工處理。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from graph.state import RefactorState
from spec_collection_agent import generate_manual_fill_templates, run_collection_agent


async def run_generate_templates(state: RefactorState) -> RefactorState:
    """階段一：落地 openapi.json、產生人工填值模板。丟到執行緒跑純粹是
    延續 spec_node.py／本檔案 `run` 的一貫做法，這一步本身不呼叫 Claude
    API、不是真的阻塞很久，但檔案 I/O 仍然是同步呼叫，跟其他 node 保持
    一致的包法比較不容易日後漏包。
    """
    pending = await asyncio.to_thread(
        generate_manual_fill_templates,
        openapi_spec=state["openapi_spec"],
        specs_dir=Path("specs"),
        postman_dir=Path("postman"),
    )

    return {
        **state,
        "collection_manual_fill_pending": pending,
    }


def should_await_manual_fill_templates_or_continue(state: RefactorState) -> str:
    return "await_manual_fill" if state.get("collection_manual_fill_pending") else "continue"


async def run(state: RefactorState) -> RefactorState:
    """階段二：假設 `run_generate_templates` 已經跑過、人工也已經處理完
    `postman/manual_fill/` 底下的模板。run_collection_agent() 內部有
    阻塞呼叫（npx 子進程、Claude API 同步呼叫），丟到執行緒跑，避免卡住
    事件迴圈（與 spec_node.py 做法一致）。
    """
    result = await asyncio.to_thread(
        run_collection_agent,
        specs_dir=Path("specs"),
        postman_dir=Path("postman"),
    )

    return {
        **state,
        "collection_readonly_path": result.collection_readonly_path,
        "collection_mutation_path": result.collection_mutation_path,
        "collection_manual_fill_pending": result.manual_fill_pending,
    }


def should_await_manual_fill_or_continue(state: RefactorState) -> str:
    return "await_manual_fill" if state.get("collection_manual_fill_pending") else "continue"
```

---

## 五、驗證方式與測試建議（[B] 部分）

呼應 00 一章「不依賴外部服務的純邏輯模組可先用假資料單元測試」的精神。開發時已用假資料驗證以下行為，建議整理成 `tests/spec_collection_agent/` 底下的正式單元測試：

| 驗證項目 | 對應函式 | 驗證內容 |
|---|---|---|
| readonly/mutation 分類 | `split_readonly_mutation()` | 混合 method 的假 Collection，拆分後 GET 進 readonly、POST/DELETE 進 mutation，資料夾結構保留 |
| 資源名稱猜測（僅供 folder 命名參考） | `guess_resource_name()` | `/api/v1/users/{id}` → `users`，版本前綴與 path 參數被正確跳過；動詞式路徑（如 `/api/exam/answer/save`）猜出動詞也是預期行為，不影響功能正確性（見函式 docstring） |
| 填值套用 | `_apply_values_to_item()` | 填值結果正確寫回 body JSON（改用 `set_nested_value()` 逐層寫入，不是淺層 `dict.update()`）；url variable/query 的值不論 LLM 回傳 int 或 str 都寫成字串；url variable/query 收到 dict/list（LLM 幻覺出複雜結構）時拋出 `ValueError`；body 根層級為 array 或純量時、以及 `body.raw` 本身不是合法 JSON 時皆拋出 `ValueError`（不再有分支靜默吞掉剩餘值）；body 裡的巢狀 dict/list 不受影響，正常寫入 |
| 填值套用（巢狀 body 路徑） | `_apply_values_to_item()` / `postman_tree.set_nested_value()` | 巢狀 key（如 `"user.id"`）正確寫入對應的巢狀路徑；body 已有 `{"user": {"id": 0, "name": "placeholder"}}`、只填 `user.id` 時 `name` 維持不變（證明不是淺層覆蓋整個 `"user"` 物件）；路徑中途不存在時自動建立中繼 dict；路徑與既有結構衝突（中途某層不是 dict）時拋出 `ValueError`，走既有的排除路徑 |
| 填值套用（重複 query key） | `_apply_values_to_item()` | 同一個 key 出現在多個 `url.query` item（模擬 OpenAPI 陣列型 query 參數展開，如 `?status=A&status=B`）時，全部都被套用到值，而不是只有第一個；`url.variable`／`url.query` 兩個迴圈都跑完才批次清理 `remaining`，確認 body 階段看到的剩餘 key 正確排除已套用的 |
| 排除失敗 endpoint | `_prune_excluded()` | 指定 `(method, path)` 的 item 被移除，其餘保留 |
| 人工填值批次套用 | `apply_manual_fill_to_collections()` | 只處理 mutation：已解決（`fields`）的 endpoint 套用值並保留，`category="skip"`/`"retry"` 兩種分類正確標記；未解決（無模板或 `has_value=False`）記 `category="retry"`、`detail="尚未完成人工填值"` 並排除；`decision=skip` 記 `category="skip"` 並排除；`apply_manual_fill()` 拋出的 `ValueError`（如 array-body）記 `category="retry"`、`detail` 含「人工補值套用失敗」並排除，同時回寫 `last_apply_error`（迴歸測試：`test_apply_failure_excluded_with_reason` 驗證回寫、`test_apply_success_clears_previous_apply_error` 驗證帶著上一輪 `last_apply_error` 的 entry 這一輪套用成功會清空並正常進 Collection）；沒有 param_schema 的 endpoint 完全不受影響；模板檔案全程不被刪除；readonly Collection 完全不經過這段邏輯，原封不動回傳（迴歸測試：`test_readonly_get_with_path_param_never_excluded`） |
| 人工填值模板產生（階段一） | `manual_fill.generate_manual_fill_templates()` | 依 controller 產生對應檔案；沒有參數的 endpoint 不產生；readonly method 不產生；重跑不覆寫既有項目、只新增新出現的 endpoint；依 body 形狀（object/array/multipart）決定預設 `fill_mode` |
| 人工填值查詢／狀態判斷 | `read_entry()` / `list_pending()` / `list_skipped()` | 跨多個 controller 檔案正確找到目標 endpoint；已解決（含 `skip`）不算 pending；`skip` 正確列進 `list_skipped()`；帶著 `last_apply_error` 的已填值 entry 仍算 pending（迴歸測試：`test_last_apply_error_counts_as_pending`）；模板不刪除，解決後檔案與其餘未解決項目仍在 |
| `has_value`／`is_resolved`／`last_apply_error` | `ManualFillEntry` | `decision=skip` 不論 `last_apply_error` 為何一律 `is_resolved=True`；`has_value=True` 但 `last_apply_error` 有值時 `is_resolved=False`（`has_value` 不受 `last_apply_error` 影響，兩者是不同判斷） |
| 套用結果回寫 | `manual_fill.record_apply_result()` | 記錄錯誤訊息；成功（`error=None`）清空前一次的錯誤；值沒有變化時（如預設本就是 `None`、又記錄一次成功）不寫檔；找不到對應 endpoint 或 `manual_fill_dir` 不存在時靜默略過，不拋例外 |
| 檔案上傳套用 | `manual_fill._apply_file_upload()` | 提供的路徑正確寫進 formdata 對應欄位的 `src`；找不到對應 formdata 欄位、指定檔案不存在、body 不是 formdata 模式時皆拋出 `ValueError` |
| url 參數不受 fill_mode 限制 | `manual_fill.apply_manual_fill()` | 迴歸測試：`fill_mode="raw_body"`／`fill_mode="file_upload"` 時，`entry.values` 裡對得上 url variable/query key 的部分仍會被套用，不會因為走 body 特殊模式就被忽略（`test_raw_body_mode_also_fills_url_query`／`test_file_upload_mode_also_fills_url_query`，對應真實案例 `POST /api/file/image` 的 `kind`/`randomId`/`side` 是 query 參數、`file` 才是 multipart body） |
| 拓樸排序 | `_stable_topological_order()` | producer 排在 consumer 之前；循環依賴正確拋出 `ValueError` |
| 拓樸排序自環過濾 | `_reorder_top_level_folders()` | producer_endpoint 與 consumer_endpoint 為同一個 item（method+path 相同）時不建自環邊、不誤判為循環依賴 |
| path segment 型別防呆 | `_segment_to_str()` / `normalized_path_from_item()` | segment 為 dict（含 `value` 欄位）時正確取值；非預期型別時回傳空字串而非拋例外 |
| folder 分組（機械規則） | `_group_by_chain_dependencies()` | 有鏈式依賴的兩個 mutation item 被分進同一組，其餘落入 singleton |
| mandatory group 機械命名 | `_mechanical_group_name()` | 猜得到資源名稱時組出可讀名稱（英文底線 snake_case）、去重、超過 3 個時截斷加 `_etc`；猜不到任何資源名稱時退回 `business_flow` 預設，且**不呼叫 LLM**（mock 一個一呼叫就失敗的 LLM，驗證這個函式不會觸發它） |
| folder 分組防呆 | `_extract_singleton_folders()` | LLM 回傳分組與輸入端點集合不一致時，正確退回保守預設並記錄 warning |
| singleton 分組只在非空時呼叫 | `_group_singletons()`（mock LLM） | 傳入空清單時完全不呼叫 LLM、直接回傳空清單；傳入非空清單時 payload 只含 singleton endpoint 字串，不含 mandatory group 資訊 |
| 分組 + 注入協同（GET consumer） | `group_mutation_folders()` + `inject_chain_scripts()`（mock LLM） | producer 加上正確的 capture 腳本；GET consumer 在同一頂層 folder 內新增獨立 item、參數改寫成 `{{env_var}}`；readonly 原本的 GET item 不受影響 |
| 鏈式依賴注入（body 型別感知／巢狀路徑） | `_rewrite_param_to_env_var()` | body 欄位原值為 int/float/bool 時，改寫後的 raw text 裡 placeholder 不帶引號（如 `{"user_id": {{created_user_id}}}`）；原值為 str 時 placeholder 維持帶引號（如 `{"code": "{{created_code}}"}`）；url variable／query 一律維持帶引號寫法（本就是字串 schema，不受此邏輯影響）；巢狀路徑（如 `"user.id"`）能正確定位並改寫，且不影響同一層其他手足欄位；`param_name` 找不到對應巢狀路徑時記 warning 並跳過，不靜默無動作（原本的淺層 `in` 檢查對巢狀路徑一律回傳 `False`，會導致完全靜默不改寫，是本輪修正的重點案例） |
| 鏈式依賴注入（重複 query key） | `_rewrite_param_to_env_var()` | 同一個 `param_name` 出現在多個 url query item（模擬 OpenAPI 陣列型 query 參數展開，如 `?status=A&status=B`）時，全部改寫成同一個 environment variable 引用，不是只改第一筆找到的就提早 return（迴歸測試：`test_duplicate_query_key_all_rewritten`） |
| 鏈式依賴注入（body 根層非 object） | `_rewrite_param_to_env_var()` | body 根層是 array（如 `raw_body` 模式的 body）時記 warning 並跳過這筆依賴，不靜默留著過期靜態值（`test_array_root_body_logs_warning_and_skips`） |
| 裸陣列索引防呆 | `_is_valid_js_field_path()` / `inject_chain_scripts()` | `"data.id"`／`"data.list[0].id"` 這類明確索引視為合法；`"data.exam[].randomId"` 這種裸 `[]` 視為不合法；`producer_field` 含裸陣列索引時整筆依賴（capture script、consumer 改寫、排序）都跳過，並記 warning（`test_invalid_producer_field_syntax_skips_whole_dependency`） |
| tag 分組 | `group_operations_by_tag()` | 單一 tag 的 operation 正確歸組；多 tag operation 取第一個；沒有 tag 的歸進 `_untagged` |
| map 階段候選解析 | `_map_analyze_group()`（mock LLM） | `output_config.format` 已保證結構合法，不需要再逐筆檢查格式——測試涵蓋正常情況能正確轉成 `_CandidateProducer`/`_CandidateConsumer`（含缺省 `hint` 補空字串）；LLM 呼叫失敗時拋出 `ChainDependencyDetectionError` |
| map 階段平行彙整 | `_map_phase()`（mock LLM，多個 tag） | 多個 group 的候選正確彙整成單一清單；任一 group 失敗時整個 map 階段拋出例外（其餘已完成的 group 結果不會被誤用） |
| 省略 reduce 呼叫 | `detect_chain_dependencies()`（mock LLM） | map 階段候選 producer 或 candidate consumer 任一邊為空時，直接回傳空 list、確認 `_reduce_phase` 完全沒被呼叫到 |
| map 階段失敗時取消排隊中的 group | `_map_phase()`（mock LLM，worker 數設 1、多個 tag） | 其中一個 group 拋例外時，尚未開始執行的其餘 group 不會被呼叫（用呼叫紀錄確認呼叫次數少於 tag 數量） |
| Map 階段分塊 | `_chunk_operations()` | 單一 tag 累積 payload 超過 `_MAX_CHARS_PER_MAP_CHUNK` 時正確切成多個子批次；單一 operation 本身就超過門檻時仍自成一批，不會被拆到欄位層級；未超過門檻的正常情況仍回傳單一批次（不多分）；`_map_phase()` 用 mock LLM 驗證同一個 tag 被拆成多批呼叫後，候選清單仍正確彙整成單一清單（與未分塊時的結果等價） |
| `call_claude_for_json()` schema 組裝 | `call_claude_for_json()`（fake client） | `schema` 正確組進 `output_config`（`{"format": {"type": "json_schema", "schema": ...}}`）；回應解析成對應的 dict／array；API 呼叫失敗（`anthropic.APIError`）轉成 `LlmJsonError`；`output_config` 保證仍失效這種異常情況（回應不是合法 JSON）時 `LlmJsonError` 帶 `raw_text` 且不重試 |
| 用量記錄的 caller 歸屬 | `call_claude_for_json()`（fake `log_usage`） | `log_usage()` 收到的 `caller` 是真正呼叫 `call_claude_for_json()` 的業務函式，不是 `call_claude_for_json` 自己；API 呼叫失敗時完全不呼叫 `log_usage()` |
| Claude client 單例的執行緒安全 | `_get_client()` | 多執行緒同時搶第一次呼叫（用 `threading.Barrier` 模擬），`anthropic.Anthropic()` 只被建構一次 |
| npx 路徑解析失敗時的錯誤訊息 | `convert_openapi_to_postman()`（mock `shutil.which` 回傳 `None`） | 拋出訊息清楚的 `FileNotFoundError`，不是讓 `subprocess.run` 自己噴一個模糊的錯誤 |

**沒有**驗證的部分（需要真實外部服務，留給 00 一章第 2 步接上 Harness 錄製端時整合驗證）：

- `convert_openapi_to_postman()` 對真實 `npx openapi-to-postmanv2` 的呼叫
- `call_claude_for_json()` 對真實 Claude API 的呼叫（prompt 品質也要等真實 `openapi.json`／`seed.sql` 才能評估，見 03a 七章）

[A] 相關的測試項目見 03b 六章。

---

## 六、環境需求與待落實事項（[B] 部分）

### 6.1 安裝

`anthropic` 套件由 `requirements.txt` 統一控制版本，不再用 `pip install` 手動裝（`requests` 也是同樣做法，見 03b 七章）——理由同 01 二章「鎖死版本而非用 `>=`」：orchestrator 長時間無人值守運行，依賴版本要能被 `pip install -r requirements.txt` 一次到位重現。

`ANTHROPIC_API_KEY` 沿用 00 五章已要求設定的環境變數。模型選擇統一透過 `SPEC_COLLECTION_AGENT_MODEL` 環境變數控制（`llm.py` 的 `DEFAULT_MODEL` 只是沒設定時的保底 fallback），已補進 `.env`：

```bash
SPEC_COLLECTION_AGENT_MODEL=claude-sonnet-4-6
```

換模型、比較填值/分組/偵測品質，改這一行即可，不需要動到程式碼或重新部署；03a 七章「模型選擇」定案後也維持這個做法，不回頭把選定的模型寫死進 `llm.py`。

### 6.2 實作時需要留意的點

這裡只列「03a 沒定義、且日後可能真的要重新檢視或調整」的重點；單純描述現有程式碼行為的說明不列在這裡，完整理由都在對應函式的 docstring／行內註解裡。

- `_condense_spec_for_chain_detection()` 目前只剝除 `description`／`example`／`examples`／`externalDocs`／`summary` 這幾個純敘述性欄位；若日後發現某些依賴關係得靠 description 才能判斷，需要重新檢視剝除範圍。
- `group_operations_by_tag()` 用「取第一個 tag」決定 map 階段的分組（03a 三章「分組依據與併發數」定案的規則本身，這裡留意的是它的邊界情況）：若同一個 operation 因為掛了多個 tag、被歸進錯的一個而漏掉候選，需要重新檢視這條規則（例如改成同一個 operation 出現在它掛的每個 tag group 裡，代價是候選清單可能重複、reduce 階段要去重）。
- `postman_tree` 的 `has_nested_key()`／`get_nested_value()`／`set_nested_value()` 只支援點號路徑對應到 dict 巢狀（如 `"user.id"`），刻意不支援陣列索引（如 `"items[0].id"`）或 JSONPath 語法；若日後出現「人工填值/鏈式依賴的參數位於 body 巢狀陣列內」的情況，需要擴充這三個工具函式（以及 `MAP_SYSTEM_PROMPT`、`manual_fill.py` `values` 欄位的命名約定）。
- `_chunk_operations()` 的 `_MAX_CHARS_PER_MAP_CHUNK`（預設 20,000 字元，可用 `SPEC_COLLECTION_AGENT_MAP_CHUNK_CHARS` 環境變數覆蓋）是保守估計出來的軟性門檻，留待有真實 `openapi.json` 可測時再依實際效果調整——這是 03a 沒涵蓋到的新參數（Map 階段併發數本身的調優，03a 七章已列為待決定事項，不在此重複）。

[A] 相關的待落實事項見 03b 七章。