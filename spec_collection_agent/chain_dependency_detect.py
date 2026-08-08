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
from common.openapi_ref_resolver import resolve_refs
from spec_collection_agent.exceptions import ChainDependencyDetectionError
from spec_collection_agent.llm import DEFAULT_MODEL
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
    operation 的字元數怎麼算」與門檻值（見 00 六章「Map 階段併發數
    （共用工具）」——命名雖是「併發數」小節，但併發數與切批次門檻是同一次
    集中整理的產物）。
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
