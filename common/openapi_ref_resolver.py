"""跨 Agent 共用的 OpenAPI `$ref` 展開，見 00 六章「OpenAPI `$ref` 展開
（共用工具）」、05a 五章。[B] Collection Agent（03a/03c）與 ③ 架構設計
Agent（05a 五章）都需要對 openapi_spec 的 operation/schema 片段做同一件
事：遞迴展開 `$ref`，只展開這次任務相關的片段（不整包攤平
`components.schemas`），遞迴展開到底，不處理 `allOf`／`oneOf`／`anyOf`
組合語法（等真的遇到再處理）。

**現況**：③（見 05a 五章「決策：抽為共用工具 common/openapi_ref_resolver.py」）
與 [B] Collection Agent（`spec_collection_agent/chain_dependency_detect.py`／
`value_filler.py`）都已直接呼叫這裡的 `resolve_refs()`；`spec_collection_agent/
openapi_refs.py`（原本各自獨立的重複實作）已刪除，見 05a 十三章已解決事項。
"""
from __future__ import annotations

from typing import Any


def resolve_refs(fragment: dict, full_spec: dict) -> dict:
    """遞迴展開 `fragment` 內出現的 `$ref`（JSON Pointer，如
    `"#/components/schemas/UserCreateRequest"`），只展開 `fragment` 實際
    用到的部分，不會把整份 `full_spec["components"]["schemas"]` 攤平塞
    進來。

    循環參照時停止繼續展開，回傳一個帶 `_circular` 標記的殘留 `$ref`，
    不無限遞迴。找不到指標對應的節點時原樣保留該 `$ref`，不拋例外中止
    ——展開失敗不該讓呼叫端（③ 的型別對應、[B] 的填值/鏈式依賴偵測）
    連帶整個失敗，寧可讓呼叫端看到一個沒展開的指標，也不要讓這個函式
    本身變成新的硬性失敗點。
    """
    return _resolve(fragment, full_spec, seen=frozenset())


def _resolve(obj: Any, full_spec: dict, *, seen: frozenset[str]) -> Any:
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in seen:
                return {"$ref": ref, "_circular": True}
            target = _resolve_json_pointer(full_spec, ref)
            if target is None:
                return obj
            return _resolve(target, full_spec, seen=seen | {ref})
        return {k: _resolve(v, full_spec, seen=seen) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve(v, full_spec, seen=seen) for v in obj]
    return obj


def _resolve_json_pointer(full_spec: dict, ref: str) -> Any | None:
    """解析 `"#/a/b/c"`（RFC 6901 JSON Pointer），沿路徑走到 `full_spec`
    裡對應的節點；沿路徑走不下去（key 不存在）就回傳 `None`。
    """
    node: Any = full_spec
    for segment in ref[2:].split("/"):
        segment = segment.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        else:
            return None
    return node
