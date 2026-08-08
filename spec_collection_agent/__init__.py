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
