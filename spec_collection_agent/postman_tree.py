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
