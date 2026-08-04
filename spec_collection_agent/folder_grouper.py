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
    一邊中文一邊英文、風格對不上（見 03a 三章「folder 命名風格」）。
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
    恰好等於輸入的 `singleton_endpoints`（不遺漏、不重複、不多加）—— schema
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
