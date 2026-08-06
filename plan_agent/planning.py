# plan_agent/planning.py
"""[P] Plan Agent 核心邏輯：四章 module 歸屬判定（委派給
`module_index.classify()`）、五章 LLM 呼叫（依 module 平行，不需要
拓樸分波）、六章 `depends_on` 組裝、七章 `target_files` 組裝、八章
`task_list` 組裝與涵蓋率驗證。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass, field

from common.concurrency import default_concurrency
from common.llm_client import LlmJsonError, call_claude_for_json
from graph.state import InterfaceSpec, ModuleInfo, PythonStructure, TaskSpec
from plan_agent import module_index
from plan_agent.exceptions import PlanAgentCoverageError, PlanAgentModuleError
from plan_agent.llm import DEFAULT_MODEL
from plan_agent.prompts import PLAN_OUTPUT_SCHEMA, PLAN_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# 併發數＝可用核心數 - 1，跟 design_agent/design.py、parse_agent/summarize.py
# 共用同一份 common/concurrency.py 實作，不各自重新推導（見 00 六章）。
_MAX_WORKERS = default_concurrency()
# 06a 五章「失敗處理」：比照 05a 六章，待重試清單、5 分鐘後統一重試一次。
_RETRY_WAIT_SECONDS = 300.0


@dataclass
class _TaskDraft:
    """單一 `InterfaceSpec` 對應的五章 LLM 輸出（尚未組裝 `depends_on`／
    `target_files`／`id`），`planning.py` 內部使用，不跨模組交接。"""

    file_path: str
    class_name: str | None
    function_name: str
    module: str
    layer: str
    description: str
    context: str
    referenced_interfaces: list[str] = field(default_factory=list)  # interface_id 清單，已過濾非法引用與自我引用


def plan_all_modules(module_list: list[ModuleInfo], python_structure: PythonStructure) -> list[TaskSpec]:
    """對外入口，對應 06a 全文。輸出直接對應 `RefactorState.task_list`
    （見 06a 八章）。
    """
    module_names = frozenset(m["module"] for m in module_list)
    module_rank = {m["module"]: i for i, m in enumerate(module_list)}

    # 四章：module／layer 歸屬判定（機械，見 module_index.classify()）
    interfaces_by_module: dict[str, list[InterfaceSpec]] = {name: [] for name in module_names}
    layer_by_id: dict[str, str] = {}
    module_by_id: dict[str, str] = {}
    for iface in python_structure["interfaces"]:
        module, layer = module_index.classify(iface["file_path"], module_names)
        interfaces_by_module[module].append(iface)
        iid = module_index.interface_id(iface["file_path"], iface["class_name"], iface["function_name"])
        layer_by_id[iid] = layer
        module_by_id[iid] = module

    # 五章：依 module 平行處理，不需要拓樸分波（跟③刻意不同，見 06a 五章
    # 「處理單位：依 module 平行處理，不需要拓樸分波」）。
    drafts_by_id = _plan_all_with_retry(module_list, interfaces_by_module, layer_by_id, module_by_id)

    # 八章「涵蓋率驗證」提前到組裝 task 之前做：後面的排序／depends_on／
    # target_files 組裝都假設 drafts_by_id 跟 python_structure.interfaces
    # 是同一個集合，提前驗證失敗可以更快中止，不用先做完組裝才發現。
    _validate_coverage(drafts_by_id, python_structure["interfaces"])

    # 六／七／八章：機械組裝 task_list
    modules_with_schema_file = _modules_with_schema_file(python_structure["directory_tree"], module_names)
    return _assemble_task_list(module_rank, layer_by_id, module_by_id, drafts_by_id, modules_with_schema_file)


# --------------------------------------------------------------------------
# 五章：LLM 呼叫與重試佇列（比照 05a 六章、04a 四章的既有先例）
# --------------------------------------------------------------------------


def _plan_all_with_retry(
    module_list: list[ModuleInfo],
    interfaces_by_module: dict[str, list[InterfaceSpec]],
    layer_by_id: dict[str, str],
    module_by_id: dict[str, str],
) -> dict[str, _TaskDraft]:
    """對所有 module 一次性平行呼叫 Claude（不像③依 `depends_on` 分波，
    見 06a 五章關鍵差異），失敗的 module 列入待重試清單，等待
    `_RETRY_WAIT_SECONDS` 秒後統一重試一次；重試仍失敗則中止整條 plan
    run（見 06a 五章「失敗處理」：`task_list` 是⑤唯一輸入，任一 module
    的 task 缺失會讓涵蓋率保證失效，風險遠高於重新執行一次）。

    沒有任何 interfaces 的 module（理論上不該發生，見 05a 七章涵蓋率
    規則——但空 module 本身不違反這條規則，只是沒有東西可拆）不需要
    呼叫 LLM，直接跳過。
    """
    pending = [m for m in module_list if interfaces_by_module[m["module"]]]

    drafts, failed = _run_batch(pending, interfaces_by_module, layer_by_id, module_by_id)
    if not failed:
        return drafts

    logger.warning(
        "%d 個 module 的五章 LLM 呼叫失敗，等待 %.0f 秒後統一重試一次: %s",
        len(failed),
        _RETRY_WAIT_SECONDS,
        [m["module"] for m in failed],
    )
    time.sleep(_RETRY_WAIT_SECONDS)

    retry_drafts, still_failed = _run_batch(failed, interfaces_by_module, layer_by_id, module_by_id)
    drafts.update(retry_drafts)

    if still_failed:
        raise PlanAgentModuleError(
            f"{len(still_failed)} 個 module 的五章 LLM 呼叫重試後仍失敗，中止整個 plan run"
            f"（06a 五章的保守預設，見本函式 docstring）: {[m['module'] for m in still_failed]}"
        )
    return drafts


def _run_batch(
    modules: list[ModuleInfo],
    interfaces_by_module: dict[str, list[InterfaceSpec]],
    layer_by_id: dict[str, str],
    module_by_id: dict[str, str],
) -> tuple[dict[str, _TaskDraft], list[ModuleInfo]]:
    """平行處理一批 module，回傳 `(interface_id -> _TaskDraft, 失敗待
    重試的 ModuleInfo 清單)`。每個 module 的失敗互相隔離，不取消其他
    module（module 間彼此本來就互不依賴，見 06a 五章）。
    """
    if not modules:
        return {}, []

    drafts: dict[str, _TaskDraft] = {}
    failed: list[ModuleInfo] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(modules))) as pool:
        futures = {
            pool.submit(_plan_module, m, interfaces_by_module, layer_by_id, module_by_id): m for m in modules
        }
        for future in concurrent.futures.as_completed(futures):
            module = futures[future]
            try:
                for d in future.result():
                    iid = module_index.interface_id(d.file_path, d.class_name, d.function_name)
                    drafts[iid] = d
            except LlmJsonError as exc:
                logger.warning("module %s 的五章 LLM 呼叫失敗，列入待重試清單: %s", module["module"], exc)
                failed.append(module)
    return drafts, failed


def _iface_payload(iface: InterfaceSpec) -> dict:
    return {
        "interface_id": module_index.interface_id(iface["file_path"], iface["class_name"], iface["function_name"]),
        "file_path": iface["file_path"],
        "class_name": iface["class_name"],
        "function_name": iface["function_name"],
        "params": iface["params"],
        "return_type": iface["return_type"],
    }


def _plan_module(
    module: ModuleInfo,
    interfaces_by_module: dict[str, list[InterfaceSpec]],
    layer_by_id: dict[str, str],
    module_by_id: dict[str, str],
) -> list[_TaskDraft]:
    """對應 06a 五章「單一 module 呼叫內容」全表格：組出這個 module 的
    payload、呼叫一次 Claude、核對回應涵蓋率、過濾非法的
    `referenced_interfaces`，回傳這個 module 的 `_TaskDraft` 清單。
    """
    own_interfaces = interfaces_by_module[module["module"]]
    upstream_interfaces = [iface for dep in module["depends_on"] for iface in interfaces_by_module.get(dep, [])]

    own_ids = {
        module_index.interface_id(i["file_path"], i["class_name"], i["function_name"]): i for i in own_interfaces
    }
    visible_ids = set(own_ids) | {
        module_index.interface_id(i["file_path"], i["class_name"], i["function_name"]) for i in upstream_interfaces
    }

    payload = {
        "module": {"module": module["module"], "summary": module["summary"]},
        "java_methods": [
            {
                "class_name": m["class_name"],
                "java_method": m["java_method"],
                "description": m["description"],
                "complexity": m["complexity"],
            }
            for m in module["methods"]
        ],
        "interfaces": [_iface_payload(i) for i in own_interfaces],
        "upstream_interfaces": [_iface_payload(i) for i in upstream_interfaces],
    }
    user_prompt = json.dumps(payload, ensure_ascii=False)

    result = call_claude_for_json(
        system_prompt=PLAN_SYSTEM_PROMPT, user_prompt=user_prompt, schema=PLAN_OUTPUT_SCHEMA, model=DEFAULT_MODEL
    )

    # 06a 五章「核對規則」：回應的三元組（這裡用 interface_id 編碼）集合
    # 必須涵蓋輸入的 interfaces 集合，缺漏視同呼叫失敗，交給上層重試佇列。
    returned_ids = {entry["interface_id"] for entry in result["tasks"]}
    missing = set(own_ids) - returned_ids
    if missing:
        raise LlmJsonError(f"module {module['module']} 的回應遺漏了 interfaces（視同呼叫失敗）: {sorted(missing)}")

    drafts: list[_TaskDraft] = []
    for entry in result["tasks"]:
        iid = entry["interface_id"]
        iface = own_ids.get(iid)
        if iface is None:
            # LLM 對不屬於這次 own_ids 的 interface_id 也生成了一筆（幻覺／
            # 拼錯，或誤把 upstream_interfaces 也當成要輸出的對象）——
            # missing 檢查只保證 own_ids 都有出現，不保證沒有多餘項目，
            # 這裡略過，不計入這個 module 的 task 清單。
            logger.warning("module %s 的五章回應包含非本模組 interface_id，略過: %s", module["module"], iid)
            continue

        referenced = [rid for rid in entry["referenced_interfaces"] if rid in visible_ids and rid != iid]
        invalid = [rid for rid in entry["referenced_interfaces"] if rid not in visible_ids]
        if invalid:
            logger.warning(
                "module %s 的 %s 引用了不存在的 referenced_interfaces，已過濾（見 06a 五章核對規則）: %s",
                module["module"],
                iid,
                invalid,
            )

        drafts.append(
            _TaskDraft(
                file_path=iface["file_path"],
                class_name=iface["class_name"],
                function_name=iface["function_name"],
                module=module["module"],
                layer=layer_by_id[iid],
                description=entry["description"],
                context=entry["context"],
                referenced_interfaces=referenced,
            )
        )
    return drafts


# --------------------------------------------------------------------------
# 八章：涵蓋率驗證（機械，收尾步驟，提前到組裝前執行）
# --------------------------------------------------------------------------


def _validate_coverage(drafts_by_id: dict[str, _TaskDraft], interfaces: list[InterfaceSpec]) -> None:
    """對應 06a 八章「涵蓋率驗證」：驗證每個 `InterfaceSpec` 都被恰好
    一個 task 認領。正常執行路徑下不會觸發——五章對每個 module 的回應
    已經核對過涵蓋率（見 `_plan_module()`），這裡是最後一道
    defense-in-depth（見 `PlanAgentCoverageError` docstring），不是用來
    擋一個已知會發生的情況。
    """
    expected_ids = {
        module_index.interface_id(i["file_path"], i["class_name"], i["function_name"]) for i in interfaces
    }
    actual_ids = set(drafts_by_id)
    if expected_ids != actual_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise PlanAgentCoverageError(
            f"task_list 涵蓋率驗證失敗（見 06a 八章，代表 [P] 自己組裝邏輯有 bug）："
            f"缺漏 {missing}，多餘 {extra}"
        )
    if len(interfaces) != len(expected_ids):
        # python_structure.interfaces 本身就有重複的三元組——③輸出的
        # 正確性缺陷，理論上不該發生（05a 四章已明訂多載消歧規則），
        # 見 06a 十二章「06b 仍值得保留一道機械檢查當 defense-in-depth」。
        raise PlanAgentCoverageError(
            "python_structure.interfaces 存在重複的 (file_path, class_name, function_name) 三元組"
            "（見 06a 十二章 defense-in-depth 說明），無法保證 1:1 涵蓋"
        )


# --------------------------------------------------------------------------
# 七章：本／跨 module 的 schemas／models 判定
# --------------------------------------------------------------------------


def _modules_with_schema_file(directory_tree: str, module_names: frozenset[str]) -> set[str]:
    """`PythonStructure` 沒有另外帶一個「哪些 module 真的產出了
    `schemas/{module}.py`」的欄位（design_agent 內部算過一次，但沒有
    落地進 `python_structure`，見 05a 九章「不重新定義結構」），這裡
    改用機械文字比對重建同一個判斷：directory_tree 的 Schema 定義段
    固定用 `### {file_path}` 起頭（05a 三章「格式慣例」第 2 點），檢查
    這個字串是否出現在 directory_tree 裡即可，不需要解析完整的
    Markdown 結構（見 06a 七章「本 module 的 schemas/{module}.py（若
    存在）」判定方式）。
    """
    return {m for m in module_names if f"### {module_index.schema_file_path(m)}" in directory_tree}


def _build_target_files(
    draft: _TaskDraft,
    module_by_id: dict[str, str],
    layer_by_id: dict[str, str],
    modules_with_schema_file: set[str],
) -> list[str]:
    """對應 06a 七章全表格：`target_files[0]` 固定是自己的 `file_path`；
    依序加入 `referenced_interfaces` 對應的 `file_path`（同／跨 module
    皆可，機械查表，不重新判斷要不要納入）、本 module 的
    schemas／models（依層級判定）、跨 module `referenced_interfaces`
    所屬外部 module 的 schemas／models（依外部介面自身層級判定，同一
    套規則套用在外部 module 身上，見七章新增列）。
    """
    files = [draft.file_path]
    seen = {draft.file_path}

    def _add(path: str) -> None:
        if path not in seen:
            seen.add(path)
            files.append(path)

    for ref_id in draft.referenced_interfaces:
        _add(module_index.file_path_of(ref_id))

    # 本 module 的 schemas／models
    if draft.layer in ("routers", "services") and draft.module in modules_with_schema_file:
        _add(module_index.schema_file_path(draft.module))
    if draft.layer in ("services", "repositories"):
        _add(module_index.model_file_path(draft.module))

    # 跨 module referenced_interfaces 所屬外部 module 的 schemas／models
    # ——依外部介面自身所在層級判定，同一套規則套用在外部 module 身上
    # （見 06a 七章新增列）。
    for ref_id in draft.referenced_interfaces:
        ref_module = module_by_id.get(ref_id)
        if ref_module is None or ref_module == draft.module:
            continue  # 同 module 已由上面「本 module」規則涵蓋
        ref_layer = layer_by_id.get(ref_id)
        if ref_layer in ("routers", "services") and ref_module in modules_with_schema_file:
            _add(module_index.schema_file_path(ref_module))
        if ref_layer in ("services", "repositories"):
            _add(module_index.model_file_path(ref_module))

    return files


# --------------------------------------------------------------------------
# 六／八章：depends_on 組裝、task id 全序編號、最終組裝
# --------------------------------------------------------------------------


def _build_depends_on(draft: _TaskDraft, own_id: str, module_by_id: dict[str, str], order_index: dict[str, int]) -> list[str]:
    """對應 06a 六章：只保留同 module 的 `referenced_interfaces`（跨
    module 的部分只用於七章 `target_files`，不進 `depends_on`），且
    只保留「被依賴 task 的全序索引 < 依賴方 task 的全序索引」的邊——
    索引相等或反向的邊直接捨棄並記 log（多半是同層函式互相引用，或
    LLM 誤判方向，見六章「防環規則」）。這個規則保證最終的
    `depends_on` 圖必為全序的子集，結構上不可能出現環，不需要另外跑
    拓樸排序／環偵測演算法驗證。
    """
    own_rank = order_index[own_id]
    depends_on: list[str] = []
    for ref_id in draft.referenced_interfaces:
        if module_by_id.get(ref_id) != draft.module:
            continue
        ref_rank = order_index.get(ref_id)
        if ref_rank is None or ref_rank >= own_rank:
            logger.info(
                "module %s 的 %s 對 %s 的依賴邊被防環規則捨棄（見 06a 六章）", draft.module, own_id, ref_id
            )
            continue
        depends_on.append(f"task_{ref_rank:03d}")
    return depends_on


def _assemble_task_list(
    module_rank: dict[str, int],
    layer_by_id: dict[str, str],
    module_by_id: dict[str, str],
    drafts_by_id: dict[str, _TaskDraft],
    modules_with_schema_file: set[str],
) -> list[TaskSpec]:
    """對應 06a 八章「id 產生規則」：全部 task 依「module（依
    `module_list` 原始順序）→ 層級 → `function_name` 字母序（
    `class_name` 為 tie-break）」的固定全序依序編號 `task_{:03d}`——
    沿用六章防環規則用的同一套全序（見 `module_index.full_order_key()`），
    編號穩定、可重現。
    """
    ordered_ids = sorted(
        drafts_by_id,
        key=lambda iid: module_index.full_order_key(
            module_rank, drafts_by_id[iid].module, drafts_by_id[iid].layer,
            drafts_by_id[iid].function_name, drafts_by_id[iid].class_name,
        ),
    )
    order_index = {iid: i for i, iid in enumerate(ordered_ids)}

    tasks: list[TaskSpec] = []
    for iid in ordered_ids:
        draft = drafts_by_id[iid]
        tasks.append(
            TaskSpec(
                id=f"task_{order_index[iid]:03d}",
                module=draft.module,
                description=draft.description,
                target_files=_build_target_files(draft, module_by_id, layer_by_id, modules_with_schema_file),
                context=draft.context,
                depends_on=_build_depends_on(draft, iid, module_by_id, order_index),
            )
        )
    return tasks
