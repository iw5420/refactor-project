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
