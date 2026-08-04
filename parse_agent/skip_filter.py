# parse_agent/skip_filter.py
"""① 解析 Agent：skip 呼叫鏈排除，對應 04a 五章全節。

**「非-skip 全集」的來源是 `openapi_spec["paths"]`，不是
`route_index.keys()`**——`route_index` 是三章從 Java 原始碼機械掃出的
索引，annotation 引用非字面字串常量、或 route 標在 interface 方法上等
情況（見 04b 三章 3.4、十一章已知限制）會讓部分實際存在的 route 完全不
出現在 `route_index` 裡；如果拿 `route_index` 自己當「全集」，這些消失的
route 不會產生任何「查無對應」警告——因為根本沒有外部基準可以比對出
「少了什麼」。這不只是精準度問題：若消失的剛好是一個合法的非-skip
endpoint，而它呼叫到的方法又同時被某個 skip endpoint 的呼叫鏈碰到、且
沒有其他非-skip 路徑能到達，`excluded = skip_reachable − non_skip_
reachable` 會把這個方法**誤判為只服務 skip、實際上仍在被使用**，正是
04a 三章設計原則要防的「不可逆的錯誤」。

`openapi_spec["paths"]`（[A] Spec Agent 對著實際跑起來的 Java 服務取得，
不是靜態分析，因此沒有 `route_index` 的漏掃問題）是「全集」來源，
`route_index` 只當 `endpoint_key → method_id` 的查表工具。任何 route 若
因為三章的解析限制而沒有進 `route_index`，不論它屬於 skip 組還是非-skip
組，都會在查表時觸發同一套「查無對應」warning（見下方
`_endpoints_to_method_ids()`），偵測機制對兩組對稱生效。
"""
from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path

from parse_agent.types import MethodId

logger = logging.getLogger(__name__)

# 跟 spec_collection_agent/chain_dependency_detect.py 的 _HTTP_METHODS
# 同一份 OpenAPI paths 物件列舉邏輯，但不跨套件 import——各 Agent 套件
# 自我封裝、不互相依賴，見 llm.py 的既有慣例。
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def load_skip_endpoints(unfilled_endpoints_path: Path) -> list[str]:
    """讀 `postman/unfilled_endpoints.json`（[B] Collection Agent 已定案
    的輸出，見 04a 二章），取 `category == "skip"` 的項目（見
    `spec_collection_agent/value_filler.py` 的 unfilled 清單格式：
    `{"endpoint": ..., "category": "skip"|"retry", "detail": ...}`）。
    檔案不存在時視為沒有任何 skip（空清單），不拋例外——理論上 ① 排在
    [B] 之後執行（見 04a 二章），這份檔案一定已經存在；防禦性處理只是
    避免單元測試或手動單獨執行 ① 時因為缺這個檔案而整段失敗。
    """
    if not unfilled_endpoints_path.exists():
        logger.warning("找不到 %s，視為沒有任何 skip endpoint", unfilled_endpoints_path)
        return []
    entries = json.loads(unfilled_endpoints_path.read_text(encoding="utf-8"))
    return [e["endpoint"] for e in entries if e.get("category") == "skip"]


def _all_endpoints_from_openapi(openapi_spec: dict) -> list[str]:
    """把 `openapi_spec["paths"]` 展開成 `{HTTP_METHOD} {path}` 字串清單，
    當作 04a 五章「非-skip 全集」的來源（見本節前言）。`path` 直接沿用
    OpenAPI 的樣板格式（如 `/api/users/{id}`），跟 `unfilled_endpoints.json`
    的 `endpoint` 欄位、三章 `route_index` 的 key 是同一種格式。非 HTTP
    method 的 path item 欄位（如 `parameters`／`summary`）直接過濾掉，
    不當成 route。
    """
    endpoints: list[str] = []
    for path, path_item in (openapi_spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in path_item:
            if method.lower() not in _HTTP_METHODS:
                continue
            endpoints.append(f"{method.upper()} {path}")
    return endpoints


def _endpoints_to_method_ids(
    endpoints: list[str], route_index: dict[str, list[MethodId]], *, group: str
) -> set[MethodId]:
    """對應 04a 五章步驟 1：把 endpoint 字串查表轉成 method_id 起點集合。
    查到多個 method_id（route_index 值本身就是清單）全部視為起點，沿用
    「多連、少排除」原則。`group`（`"skip"` 或 `"非-skip"`）只供下面
    warning log 標明是哪一組查無對應，不影響排除邏輯本身——兩組都會走
    這裡，查無對應時的警告因此對稱生效（見本節前言）。
    """
    method_ids: set[MethodId] = set()
    unmatched: list[str] = []
    for ep in endpoints:
        ids = route_index.get(ep)
        if not ids:
            unmatched.append(ep)
            continue
        method_ids.update(ids)
    if unmatched:
        logger.warning(
            "%s endpoint 在 Controller Route 索引中查無對應 method_id，"
            "略過（不貢獻任何排除起點，見 04a 五章步驟 1 第三點）: %s",
            group,
            unmatched,
        )
    return method_ids


def _bfs_reachable(starts: set[MethodId], call_graph: dict[MethodId, set[MethodId]]) -> set[MethodId]:
    """對應 04a 五章步驟 2：從一組起點沿呼叫圖做可達性分析（BFS，圖可能
    有環——同一個方法互相遞迴呼叫的情況，`visited` 集合天然防止重複
    展開，不需要額外處理環偵測）。
    """
    visited: set[MethodId] = set()
    queue: deque[MethodId] = deque(starts)
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for callee in call_graph.get(current, ()):
            if callee not in visited:
                queue.append(callee)
    return visited


def compute_excluded_methods(
    *,
    skip_endpoints: list[str],
    openapi_spec: dict,
    route_index: dict[str, list[MethodId]],
    call_graph: dict[MethodId, set[MethodId]],
) -> set[MethodId]:
    """對應 04a 五章步驟 1～4 全流程，回傳最終要排除的 method_id 集合
    （= skip 可達集合 − 非-skip 可達集合）。孤立方法（兩個集合都沒碰到
    的方法）不會出現在回傳集合裡——`excluded` 只從 `skip_reachable` 扣
    掉 `nonskip_reachable`，孤立方法從未進過 `skip_reachable`，因此
    「預設保留」（04a 五章步驟 4）是這個計算方式的自然結果，不需要
    額外的特判分支。

    `openapi_spec`：`RefactorState.openapi_spec`，[A] Spec Agent 產出，
    這裡是「非-skip 全集」的來源，不是 `route_index`（見本節前言）。
    """
    skip_set = set(skip_endpoints)
    non_skip_endpoints = [ep for ep in _all_endpoints_from_openapi(openapi_spec) if ep not in skip_set]

    skip_starts = _endpoints_to_method_ids(skip_endpoints, route_index, group="skip")
    non_skip_starts = _endpoints_to_method_ids(non_skip_endpoints, route_index, group="非-skip")

    skip_reachable = _bfs_reachable(skip_starts, call_graph)
    non_skip_reachable = _bfs_reachable(non_skip_starts, call_graph)

    excluded = skip_reachable - non_skip_reachable
    if excluded:
        logger.info("skip 呼叫鏈排除：%d 個方法只能從 skip endpoint 到達，將不會出現在 module_list", len(excluded))

    # skip_starts 與 non_skip_starts 的交集：某個 method_id 同時是 skip
    # endpoint 與非-skip endpoint 的直接繫結目標。這只可能發生在同一個
    # class 內有多個同名多載方法、各自掛不同 HTTP method 的 route（如
    # FileController 的 voice/image，一個 @PostMapping、一個
    # @GetMapping）——method_id 不含參數簽名（javalang 不做 overload
    # resolution，見 04a 三章「決策」），兩個不同的物理方法被誤判成同一個
    # 方法，讓「共用方法會被保護」規則把 skip 的那個分支也保護下來。這個
    # 方法不會進 excluded（module_list 仍會保留它的描述），但對應的
    # skip endpoint 本身在 assemble_api_mapping() 有獨立的絕對排除保證
    # （見該函式 docstring），不受這裡的判斷影響——這裡只負責記警告，讓
    # module_list 裡這個方法的描述（可能混雜了 skip 分支的行為）能被人工
    # 核對到。
    protected_by_sharing = skip_starts & non_skip_starts
    if protected_by_sharing:
        logger.warning(
            "%d 個方法同時是 skip 與非-skip endpoint 的直接繫結目標（多載"
            "方法同名導致 method_id 共用，見 04a 三章「決策」）：這個方法"
            "在 module_list 的描述可能混雜了 skip 分支的行為，對應的 skip "
            "endpoint 本身已由 assemble_api_mapping() 絕對排除，但方法"
            "描述本身建議人工核對: %s",
            len(protected_by_sharing),
            sorted(protected_by_sharing),
        )

    return excluded
