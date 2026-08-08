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

from common.openapi_ref_resolver import resolve_refs
from spec_collection_agent import manual_fill
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
