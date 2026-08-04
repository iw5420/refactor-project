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
