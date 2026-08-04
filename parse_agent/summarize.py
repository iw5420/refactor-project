# parse_agent/summarize.py
"""① 解析 Agent：Map/Reduce 呼叫與輸出組裝，對應 04a 四章（Map/Reduce
執行與失敗重試）、六章（輸出組裝）。

**跟 `chain_dependency_detect.py` 的失敗處理策略刻意不同**：[B] 的
`_map_phase()` 任一批次失敗就整體中止、取消其餘尚未開始的批次（鏈式
依賴偵測的 map 候選彼此依賴 reduce 階段才有意義，任一批次缺失就會讓
reduce 的配對不完整，語意上等於整體失敗）。① 的 Map 分組彼此獨立（每組
摘要各自的 class，不互相依賴），所以 04a 四章刻意設計成「失敗的組列入
待重試清單，不立即中止」——`_run_map_batch()` 讓每個批次的呼叫互相隔離，
一個失敗不影響其他批次繼續執行。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from common.concurrency import default_concurrency
from common.llm_client import LlmJsonError, call_claude_for_json
from graph.state import ApiMapping, MethodInfo, ModuleInfo
from parse_agent.exceptions import ParseAgentMapReduceError
from parse_agent.grouping import (
    MapUnit,
    build_controller_units,
    build_shared_class_units,
    classify_trivial_classes,
    controller_dependency_closure,
    find_shared_classes,
)
from parse_agent.llm import DEFAULT_MODEL
from parse_agent.prompts import (
    MAP_OUTPUT_SCHEMA,
    MAP_SYSTEM_PROMPT,
    REDUCE_OUTPUT_SCHEMA,
    REDUCE_SYSTEM_PROMPT,
)
from parse_agent.types import ClassInfo, MapClassResult, MapMethodResult, MethodId, ParsedProject, method_id

logger = logging.getLogger(__name__)

# 併發數＝可用核心數 - 1，跟 spec_collection_agent/chain_dependency_
# detect.py 共用同一份 common/concurrency.py 實作，不各自重新推導（見
# 04a 四章「併發數」、00 六章「Map 階段併發數（共用工具）」）。4a 通常
# 只有一批，這個上限對 4a 沒有實際影響，兩個子階段共用同一個常數不重複
# 定義。
_MAX_MAP_WORKERS = default_concurrency()

# 04a 四章：「等待 5 分鐘，對待重試清單裡的組統一重試一次」。
_RETRY_WAIT_SECONDS = float(os.environ.get("PARSE_AGENT_MAP_RETRY_WAIT_SECONDS", "300"))


# --------------------------------------------------------------------------
# 7.1 Map 呼叫與重試佇列
# --------------------------------------------------------------------------


def _class_source_payload(project_root: str, class_info: ClassInfo) -> dict:
    """組單一 class 送給 Map 的 payload：直接讀整個檔案原始碼。假設
    Java 慣例「每檔一個 top-level class」（04a 三章驗證過的目標專案
    90 個 .java 檔案對應 90 個 class，符合這個假設）——不做 AST 節點到
    原始碼片段的精確切割，模型看到整個檔案（含 import、其他非
    top-level 的巢狀類別）多花一點 token，換取不用處理「怎麼從 AST 節點
    精確還原原始碼區間」這個額外複雜度。

    `class_info.file_path` 只相對 `project_root`（見
    `ParsedProject.project_root` 說明），這裡組回絕對路徑才能實際讀到
    檔案。
    """
    return {
        "class_name": class_info.class_name,
        "source": Path(project_root, class_info.file_path).read_text(encoding="utf-8"),
    }


def _map_analyze_batch(unit: MapUnit) -> list[MapClassResult]:
    """對單一 MapUnit（4a 或 4b 的一個分組）呼叫 Claude，回傳這批
    class 各自的摘要結果。`MAP_OUTPUT_SCHEMA`（output_config.format）已
    保證輸出結構，不需要再逐筆檢查型別——但「classes 陣列的 class_name
    是否恰好對應輸入」是語意層面的事，schema 保證不了，這裡仍要核對。

    **核對到遺漏時視同呼叫失敗，直接拋 `LlmJsonError`，交給
    `run_map_phase_with_retry()` 的重試佇列**——只記 warning、照樣把不
    完整的結果傳下去，會跟 `run_map_phase_with_retry()` 自己的
    docstring 立場矛盾：那裡明講「留空繼續會讓 ③ 在不知情的狀況下對著
    不完整的模組清單做設計決策，風險更高」，這句話對「整批呼叫失敗」和
    「呼叫成功但內容漏答」這兩種情況都成立，沒有理由只在前者中止、後者
    卻放行。Structured Outputs（`output_config.format`）保證的是**每一筆
    輸出項目**符合 schema，不保證陣列筆數等於輸入筆數，LLM 漏答完全是
    schema-valid 的合法輸出，不會被 `call_claude_for_json()` 攔下。拋出
    後整個 unit（不只是遺漏的那幾個 class）會被丟回待重試清單，等 5 分鐘
    後重新送出同一批 payload。
    """
    payload = {
        "classes": [_class_source_payload(unit.project_root, c) for c in unit.classes],
        "known_shared_class_summaries": unit.known_shared_summaries,
    }
    user_prompt = json.dumps(payload, ensure_ascii=False)

    result = call_claude_for_json(
        system_prompt=MAP_SYSTEM_PROMPT, user_prompt=user_prompt, schema=MAP_OUTPUT_SCHEMA, model=DEFAULT_MODEL
    )

    expected_names = {c.class_name for c in unit.classes}
    returned_names = {entry["class_name"] for entry in result["classes"]}
    missing = expected_names - returned_names
    if missing:
        raise LlmJsonError(
            f"Map 分組 {unit.label} 的回應遺漏了部分輸入 class 的摘要"
            f"（視同呼叫失敗，交給待重試清單處理）: {sorted(missing)}"
        )

    known_methods_by_class = {c.class_name: {m.name for m in c.methods} for c in unit.classes}

    return [
        MapClassResult(
            class_name=entry["class_name"],
            summary=entry["summary"],
            methods=[
                MapMethodResult(
                    _normalize_method_name(m["method_name"], known_methods_by_class.get(entry["class_name"], set())),
                    m["description"],
                    m["complexity"],
                )
                for m in entry["methods"]
            ],
            cross_group_dependency_hints=entry["cross_group_dependency_hints"],
        )
        for entry in result["classes"]
    ]


_METHOD_NAME_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _normalize_method_name(raw_name: str, known_names: set[str]) -> str:
    """Map 階段回傳的 `method_name` 理論上應該原樣抄自輸入原始碼（見
    `prompts.MAP_SYSTEM_PROMPT`「method_name：原樣抄方法名稱」），但一個
    class 內有同名多載方法時（如兩個 `voice`，一個 `@PostMapping`、一個
    `@GetMapping`），即使 prompt 明確要求原樣抄，模型仍可能自行加註
    `"(POST)"`／`"(GET)"` 這類後綴消歧同名方法。這個後綴一旦留著，
    `filter_excluded_methods()`／`assemble_api_mapping()` 用字串完全比對
    method_id 時就會對不上 `route_index`／`call_graph` 裡 javalang 解析
    出的真實方法名稱（沒有這個後綴），導致該方法對應的 endpoint 整批從
    `api_to_python_target` 消失——`assemble_api_mapping()` 會把這誤判成
    「方法已被排除」的合法情況，不會產生任何 warning。

    這裡在合併回 `MapMethodResult` 之前正規化：原樣名稱若不在這個 class
    實際宣告的方法名稱集合（`known_names`，來自 javalang 掃描結果，權威
    來源）裡，嘗試剝掉結尾的括號後綴再比對一次；還是對不上就保留原樣，
    只記警告，不強行猜測（呼應 04a 三章「多連、少排除」同一種保守精神：
    正規化不到就不動，讓下游該排除的排除，不偽造一個可能是錯的名稱）。
    """
    if raw_name in known_names:
        return raw_name
    stripped = _METHOD_NAME_SUFFIX_RE.sub("", raw_name).strip()
    if stripped != raw_name and stripped in known_names:
        logger.info(
            "Map 回應的 method_name %r 正規化為 %r（多載方法消歧後綴，見 "
            "_normalize_method_name() docstring）",
            raw_name,
            stripped,
        )
        return stripped
    logger.warning(
        "Map 回應的 method_name %r 在對應 class 的實際方法清單中找不到"
        "（剝掉消歧後綴後仍對不上），可能導致對應的 endpoint 從 "
        "api_to_python_target 消失，請人工核對: 已知方法清單=%s",
        raw_name,
        sorted(known_names),
    )
    return raw_name


def _run_map_batch(units: list[MapUnit]) -> tuple[list[MapClassResult], list[MapUnit]]:
    """平行執行一批 MapUnit（4a 或 4b 各自呼叫一次這個函式），回傳
    (成功結果攤平清單, 失敗待重試的 MapUnit 清單)。每個 unit 的失敗
    互相隔離，不取消其他 unit（見本節前言，跟 chain_dependency_detect.py
    的策略刻意不同）。
    """
    results: list[MapClassResult] = []
    failed: list[MapUnit] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_MAP_WORKERS, len(units))) as pool:
        futures = {pool.submit(_map_analyze_batch, u): u for u in units}
        for future in concurrent.futures.as_completed(futures):
            unit = futures[future]
            try:
                results.extend(future.result())
            except LlmJsonError as exc:
                logger.warning("Map 分組呼叫失敗，列入待重試清單: %s（%s）", unit.label, exc)
                failed.append(unit)
    return results, failed


def run_map_phase_with_retry(units: list[MapUnit]) -> list[MapClassResult]:
    """對應 04a 四章「單一分組（4a 或 4b 任一組）呼叫 Claude API 失敗
    時」的三步驟：先跑一輪、失敗的列入待重試清單；全部跑完後等待
    `_RETRY_WAIT_SECONDS` 秒，統一重試一次；重試仍失敗的視為硬性失敗。

    **重試仍失敗後採「中止整條 run」的保守預設（見 04a 十章）**——理由：
    留空繼續代表這批 class 的方法完全不會出現在 module_list，讓 ③ 架構
    設計 Agent 在完全不知情的狀況下對著不完整的模組清單做設計決策，比起
    直接中止讓人工介入，風險更高、更難事後追查根因。這個理由對「整批
    呼叫失敗」（如網路逾時、API 限流）和「呼叫成功但 LLM 漏答部分 class」
    （見 7.1 `_map_analyze_batch()`，一律拋 `LlmJsonError` 統一走這裡的
    重試佇列）同樣成立，因此兩者共用同一套重試／中止機制，不分開處理。
    日後若要改成「留空繼續」，只需要把下面的 `raise ParseAgentMapReduce
    Error` 改成 log warning＋回傳目前已有的 `results`，不影響本函式的
    呼叫端介面。

    **`time.sleep()` 為什麼不會拖住整個 Orchestrator**：這個函式（連同
    整個 `run_parse_agent()`）是被 `parse_node.py` 用 `asyncio.to_thread()`
    丟到獨立 worker thread 執行的，`sleep` 只擋住那一條 thread，不會擋住
    asyncio 事件迴圈本身。這裡是單次批次流程（單一 Orchestrator 跑單一
    Java 專案，`parse` 在圖上是單點執行），等待的這幾分鐘裡沒有其他工作
    在跟這條 thread 搶執行緒池資源。
    """
    if not units:
        return []

    results, failed = _run_map_batch(units)
    if not failed:
        return results

    logger.warning(
        "%d 組 Map 呼叫失敗，等待 %.0f 秒後統一重試一次: %s",
        len(failed),
        _RETRY_WAIT_SECONDS,
        [u.label for u in failed],
    )
    time.sleep(_RETRY_WAIT_SECONDS)

    retry_results, still_failed = _run_map_batch(failed)
    results.extend(retry_results)

    if still_failed:
        raise ParseAgentMapReduceError(
            f"{len(still_failed)} 組 Map 呼叫重試後仍失敗，中止整個 parse run"
            f"（04a 十章的保守預設，見本函式 docstring）: "
            f"{[u.label for u in still_failed]}"
        )
    return results


# --------------------------------------------------------------------------
# 7.2 Reduce 呼叫
# --------------------------------------------------------------------------


def _reduce_phase(map_results: list[MapClassResult], controller_deps: dict[str, set[str]]) -> dict:
    """單次呼叫，輸入是 4a+4b 全部 Map 結果的彙整（濃縮後的候選結果，
    非原始碼全量，見 04a 四章 Reduce 階段）+ 程式算好的
    controller_dependencies 事實（見 04a 四章「這也是程式算出的精確
    事實」）。回傳 REDUCE_OUTPUT_SCHEMA 的原始 dict，組裝成 ModuleInfo
    是 7.3 的事，這裡只負責呼叫。
    """
    payload = {
        "classes": [
            {
                "class_name": r.class_name,
                "summary": r.summary,
                "methods": [
                    {"method_name": m.method_name, "description": m.description, "complexity": m.complexity}
                    for m in r.methods
                ],
                "cross_group_dependency_hints": r.cross_group_dependency_hints,
            }
            for r in map_results
        ],
        "controller_dependencies": {
            controller: sorted(deps) for controller, deps in controller_deps.items()
        },
    }
    user_prompt = json.dumps(payload, ensure_ascii=False)

    try:
        return call_claude_for_json(
            system_prompt=REDUCE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema=REDUCE_OUTPUT_SCHEMA,
            model=DEFAULT_MODEL,
        )
    except LlmJsonError as exc:
        # Reduce 只有單次呼叫，沒有「多組互相隔離」的概念，失敗直接視為
        # 硬性失敗（跟 Map 階段的重試佇列語意不同，這裡不重試）——重試一次
        # 能解決的通常是網路抖動這類 anthropic SDK 內建 max_retries 已經
        # 處理過的暫時性錯誤，這裡再重試意義不大。
        raise ParseAgentMapReduceError(f"Reduce 階段呼叫失敗（{len(map_results)} 筆 class 摘要）: {exc}") from exc


# --------------------------------------------------------------------------
# 7.3 輸出組裝
# --------------------------------------------------------------------------
#
# 為什麼需要 `_ModuleDraft` 這個中繼結構：`graph/state.py` 的 `MethodInfo`
# 只有 `java_method`／`description`／`complexity` 三個欄位，沒有
# `class_name`——這是刻意的，因為 ③ 架構設計 Agent 之後只需要方法層級的
# 資訊（見 04a 六章）。但 `filter_excluded_methods()`（下方）需要用
# `method_id`（含 `class_name`／`file_path`）判斷是否落在 `skip_filter`
# 算出的排除集合裡，如果直接組成 `MethodInfo` 就把 `class_name` 弄丟了，
# 之後就無法正確比對。因此組裝分兩步：先組出保留 `class_name` 的內部
# 草稿 `_ModuleDraft`，排除計算跟 `assemble_api_mapping()`（同樣需要
# `class_name`）都在草稿階段完成，最後才用 `finalize_module_list()`
# 剝除 `class_name`、產出真正符合 `ModuleInfo` 型別的公開輸出。


@dataclass(frozen=True)
class _DraftMethod:
    class_name: str
    file_path: str
    method: MethodInfo


@dataclass
class _ModuleDraft:
    module: str
    summary: str
    java_files: list[str]
    depends_on: list[str]
    methods: list[_DraftMethod]


def _assemble_module_drafts(
    map_results: list[MapClassResult], reduce_result: dict, project: ParsedProject
) -> tuple[list[_ModuleDraft], dict[str, str]]:
    """把 Reduce 的模組拆分決策（哪些 class 歸在同一個 module），跟 Map
    階段已經產出的 methods（description／complexity）機械合併，組成
    `_ModuleDraft` 清單。Reduce 只決定 class 層級的歸屬，方法清單完全
    沿用 Map 的輸出（見本節前言），不需要 Reduce 重新謄寫或猜測 Python
    命名。同時回傳 `class_name -> module` 對照表，供
    `assemble_api_mapping()` 使用（04a 六章：ApiMapping.module 要對回這個
    class 最終被分進哪個模組）。這裡產出的是**排除 skip 呼叫鏈之前**的
    完整版本，method 層級的排除交給 `filter_excluded_methods()`（下一個
    函式），對應 04a 五章「排除發生在 Reduce 階段輸出 module_list...之前」。

    **`missing_classes` 只檢查一個方向**：Reduce 輸出的 `java_classes`
    引用了不存在的 class（模型虛構／拼錯名稱）。這裡另外反向檢查
    `map_class_names - class_to_module.keys()`——Map 階段已經成功摘要、
    但 Reduce 完全沒有把它分進任何 module 的 class：這種情況不會觸發
    `missing_classes`，卻會讓一個真實存在、已經花錢摘要過的 class
    整個從 `module_list` 消失、沒有任何訊號。只記警告，不代為指派
    module——指派哪個 module 是需要業務判斷的事，不該由這裡的程式邏輯
    瞎猜一個。

    另外也驗證 `depends_on`（見本函式最後一段）：比照 `missing_classes`
    對 `java_classes` 的處理方式，過濾掉引用不存在 module 名稱的項目並
    記警告，不讓虛構的依賴關係原樣流入最終輸出。
    """
    # class_name -> Map 階段對這個 class 的摘要結果（含 methods），供機械合併用
    class_to_map_result: dict[str, MapClassResult] = {r.class_name: r for r in map_results}

    drafts: list[_ModuleDraft] = []
    class_to_module: dict[str, str] = {}

    for module_entry in reduce_result["modules"]:
        valid_classes = [cls for cls in module_entry["java_classes"] if cls in project.classes]
        missing_classes = [cls for cls in module_entry["java_classes"] if cls not in project.classes]
        if missing_classes:
            logger.warning(
                "Reduce 產出的 module %s 引用了不存在於呼叫圖掃描結果的 class"
                "（模型可能虛構或拼錯名稱），已略過: %s",
                module_entry["module"],
                missing_classes,
            )

        usable_classes: list[str] = []
        draft_methods: list[_DraftMethod] = []
        for cls in valid_classes:
            map_result = class_to_map_result.get(cls)
            if map_result is None:
                # 7.1 _map_analyze_batch() 遺漏任何一個輸入 class 就會
                # 直接拋例外、交給重試佇列，重試仍失敗會中止整條 run，
                # 所以能執行到這裡代表 map_results 對所有送進過 Map 階段
                # 的 class 都是完整的——這裡會觸發只可能是 Reduce 自己
                # 虛構了一個「存在於專案、但沒被送進過 Map 階段」的 class
                # 名稱。
                logger.warning(
                    "Reduce 產出的 module %s 引用了 Map 階段沒有摘要過的 class"
                    "（模型可能虛構），已略過: %s",
                    module_entry["module"],
                    cls,
                )
                continue
            usable_classes.append(cls)
            class_to_module[cls] = module_entry["module"]
            for m in map_result.methods:
                draft_methods.append(
                    _DraftMethod(
                        class_name=cls,
                        file_path=project.classes[cls].file_path,
                        method=MethodInfo(
                            java_method=m.method_name,
                            description=m.description,
                            complexity=m.complexity,  # type: ignore[typeddict-item]
                        ),
                    )
                )

        drafts.append(
            _ModuleDraft(
                module=module_entry["module"],
                summary=module_entry["summary"],
                java_files=sorted({project.classes[cls].file_path for cls in usable_classes}),
                depends_on=module_entry["depends_on"],
                methods=draft_methods,
            )
        )

    map_class_names = {r.class_name for r in map_results}
    unassigned = map_class_names - set(class_to_module)
    if unassigned:
        logger.warning(
            "Map 階段已摘要、但 Reduce 沒有把它分進任何 module 的 class"
            "（模型可能漏看，這些 class 的方法不會出現在 module_list，見"
            "本函式 docstring「missing_classes 只檢查一個方向」）: %s",
            sorted(unassigned),
        )

    # depends_on 引用的是其他 module 的名稱，不是 class，只有等所有
    # module_entry 都跑完、drafts 收集齊全才知道「合法的 module 名稱」有
    # 哪些，所以放在迴圈外面單獨一輪檢查。
    valid_module_names = {d.module for d in drafts}
    for d in drafts:
        invalid_deps = [dep for dep in d.depends_on if dep not in valid_module_names]
        if invalid_deps:
            logger.warning(
                "module %s 的 depends_on 引用了不存在於本次輸出的 module 名稱"
                "（模型可能虛構或拼錯名稱），已從 depends_on 移除: %s",
                d.module,
                invalid_deps,
            )
            d.depends_on = [dep for dep in d.depends_on if dep in valid_module_names]

    return drafts, class_to_module


def filter_excluded_methods(drafts: list[_ModuleDraft], excluded: set[MethodId]) -> list[_ModuleDraft]:
    """對應 04a 五章「排除發生在 Reduce 階段輸出 module_list...之前，
    被排除的方法從一開始就不會出現在最終輸出裡」——這裡是實際套用排除
    的地方：`excluded` 來自 `skip_filter.compute_excluded_methods()`，
    只排除 method 層級，不影響 module／其餘方法。排除後方法清單為空的
    module 予以保留（module 底下所有方法都只服務 skip endpoint 的極端
    情況）——是否要連 module 一起拿掉不在 04a 五章的排除定義範圍內，
    交給下游 ③ 架構設計 Agent 自行判斷是否需要一個空模組，這裡不做額外
    決策。
    """
    return [
        _ModuleDraft(
            module=d.module,
            summary=d.summary,
            java_files=d.java_files,
            depends_on=d.depends_on,
            methods=[
                dm
                for dm in d.methods
                if method_id(dm.file_path, dm.class_name, dm.method["java_method"]) not in excluded
            ],
        )
        for d in drafts
    ]


def assemble_api_mapping(
    project: ParsedProject,
    class_to_module: dict[str, str],
    drafts: list[_ModuleDraft],
    skip_endpoints: set[str],
) -> list[ApiMapping]:
    """對應 04a 六章 ApiMapping：把三章機械建構的 `route_index`（Java
    原始碼實際擁有的 endpoint，見三章步驟 5～6）跟 Reduce 決定的模組
    歸屬、method 排除結果串起來。呼叫端必須傳入**已經 `filter_excluded_
    methods()` 過的** `drafts`——route_index 裡指向的 method_id 若已經
    被排除，這筆 endpoint 直接不進最終 api_to_python_target：不只是
    「該 method 被排除」，而是這個 endpoint 本身的處理方法都不會被下游
    改寫，繼續留著這筆映射沒有意義。

    `skip_endpoints`：`skip_filter.load_skip_endpoints()` 的原始輸出
    （人工明確標記 `category="skip"` 的 endpoint_key 集合）。**這裡對
    `skip_endpoints` 做無條件、endpoint 層級的排除，跟下面 method 層級的
    `known_methods` 檢查是兩道各自獨立生效的關卡**——理由見 04a 五章
    「skip endpoint 的絕對排除保證」：`method_id` 是 `{file}::{class}::
    {method_name}`，不含參數簽名（見 04a 三章「決策」：javalang 不做
    overload resolution），若同一個 class 內有兩個同名多載方法各自掛
    不同 HTTP method 的 route（如 `FileController` 的 `voice`／`image`，
    一個 `@PostMapping`、一個 `@GetMapping`），且其中一個是 skip、另一個
    不是，兩者會共用同一個 `method_id`。`compute_excluded_methods()` 的
    「共用方法會被保護」規則（見 04a 五章）這時會誤判成「這個方法也被
    非-skip endpoint 使用，不該排除」，導致人工明確排除的那個 endpoint
    透過 `known_methods` 檢查悄悄復活。

    人工標記 skip 這個事實是對**這一個 endpoint**下的絕對判斷，不該因為
    底層 `method_id` 共用而被覆蓋——因此這裡直接、無條件排除
    `skip_endpoints` 裡的每一個 endpoint_key，不透過 `method_id` 這層
    間接關係，兩個機制哪個先觸發都能正確擋下：這道排除只影響
    `api_to_python_target` 這個 endpoint 級輸出，不影響 `module_list`
    ——共用 `method_id` 的另一個非-skip 分支（如 `GET /api/file/image`）
    的方法描述，仍會依既有規則正常留在 `module_list`（`compute_excluded_
    methods()` 現在會對這類碰撞記警告，供人工核對描述是否混雜了 skip
    分支的行為，見 `skip_filter.py`）。

    `ApiMapping` 不含 Java→Python 的檔案/函式對應——那唯一權威來源是
    ③ 的 `python_structure.interfaces`，① 沒有可靠依據能替每個方法猜出
    實際落點，這裡只負責「這個 Java controller method 最終歸屬哪個
    module」（見 04a 六章）。
    """
    # (class_name, java_method) 有沒有出現在 drafts 裡，用來判斷這個方法
    # 是否留在最終 module_list（沒被 skip 呼叫鏈排除、也確實被 Reduce 分進某個 module）
    known_methods: set[tuple[str, str]] = {
        (dm.class_name, dm.method["java_method"])
        for d in drafts
        for dm in d.methods
    }

    result: list[ApiMapping] = []
    for endpoint_key, method_ids in project.route_index.items():
        if endpoint_key in skip_endpoints:
            continue  # 人工明確排除，endpoint 層級絕對排除，見本函式 docstring
        http_method, _, path = endpoint_key.partition(" ")
        for mid in method_ids:
            _, class_name, method_name = mid.split("::")
            module = class_to_module.get(class_name)
            if module is None or (class_name, method_name) not in known_methods:
                continue  # 未歸屬任何模組，或方法已被排除（見本函式 docstring）
            result.append(
                ApiMapping(
                    endpoint=path,
                    http_method=http_method,
                    java_controller=f"{class_name}.{method_name}",
                    module=module,
                )
            )
    return result


def finalize_module_list(drafts: list[_ModuleDraft]) -> list[ModuleInfo]:
    """剝除 `_ModuleDraft` 只有內部組裝過程需要的 `class_name`／
    `file_path`，產出符合 `graph/state.py` `ModuleInfo` 型別的最終輸出。
    必須在 `filter_excluded_methods()`／`assemble_api_mapping()` 都跑完
    之後才呼叫——這兩者都依賴草稿階段保留的 `class_name`。
    """
    return [
        ModuleInfo(
            module=d.module,
            summary=d.summary,
            java_files=d.java_files,
            depends_on=d.depends_on,
            methods=[dm.method for dm in d.methods],
        )
        for d in drafts
    ]


# --------------------------------------------------------------------------
# 7.4 對外入口
# --------------------------------------------------------------------------


def _mechanical_summary(class_info: ClassInfo) -> MapClassResult:
    """對 `grouping.classify_trivial_classes()` 判定不需要送 Map 的類別
    （純資料類別，見該函式 docstring），機械組出一份佔位摘要，不呼叫
    Claude API。目的單純是完整性：讓這個類別仍然能透過 Reduce 被分進
    某個 module、出現在 `module_list.java_files`，不是真的做了語意摘要，
    `summary` 內容本身要老實反映這件事，不偽裝成 LLM 產出的摘要。

    `methods=[]`：這些方法多半是 getter/setter，個別拆成 [P] 的 task 去
    翻譯沒有意義——Python 端通常用 dataclass／SQLAlchemy model 的屬性
    表達，不需要逐一翻譯存取器方法；真的有非存取器方法而被判定需要
    摘要的類別，本來就不會走到這裡（見 `needs_llm_summary()`）。
    """
    annotation_note = "、".join(class_info.annotations) or "無 Lombok/JPA 標記"
    return MapClassResult(
        class_name=class_info.class_name,
        summary=(
            f"純資料類別（{annotation_note}），僅有存取器方法，機械判定無自訂業務邏輯，"
            "未呼叫 Claude API 摘要（見 grouping.needs_llm_summary()）。"
        ),
        methods=[],
        cross_group_dependency_hints=[],
    )


def run_map_reduce(project: ParsedProject) -> tuple[list[_ModuleDraft], dict[str, str]]:
    """對應 04a 四章全節：串接分組（grouping.py）、4a→4b 兩階段 Map（見
    「4a 在 4b 之前完成」）、Reduce、輸出組裝。回傳排除 skip 呼叫鏈之前
    的 `(module_drafts, class_to_module)`——skip 排除交給呼叫端（見
    `parse_agent/__init__.py`）在這之後才做，因為排除計算（五章）跟
    Map/Reduce（四章）是兩條互不相依的資料流，分開呼叫比較清楚；回傳
    草稿型別而非 `ModuleInfo`，讓呼叫端能先做 `filter_excluded_methods()`
    ／`assemble_api_mapping()`，最後才呼叫 `finalize_module_list()`。

    `trivial_class_names`（`grouping.classify_trivial_classes()`）判定
    不需要送 Map 的類別，從 4a／4b 實際送出的批次裡剔除，省下對應的
    API 呼叫；但這些類別若真的被某個 Controller 依賴到（`reachable_
    classes`），仍要透過 `_mechanical_summary()` 補一份機械摘要、混進
    `all_map_results` 一起送進 Reduce——不這樣做的話，這些類別會連
    Reduce 都看不到，`module_list.java_files` 又會漏掉它們，等於繞了
    一圈把四章一開始想解決的完整性問題重新引入。
    """
    controller_deps = controller_dependency_closure(project)
    shared_class_names = find_shared_classes(controller_deps)
    trivial_class_names = classify_trivial_classes(project)

    shared_units = build_shared_class_units(project, shared_class_names - trivial_class_names)
    map_results_4a = run_map_phase_with_retry(shared_units)
    shared_summaries = {r.class_name: r.summary for r in map_results_4a}

    controller_units = build_controller_units(
        project, controller_deps, shared_class_names, shared_summaries, trivial_class_names
    )
    map_results_4b = run_map_phase_with_retry(controller_units)

    reachable_classes = {c for deps in controller_deps.values() for c in deps}
    mechanical_results = [
        _mechanical_summary(project.classes[name])
        for name in sorted(trivial_class_names & reachable_classes)
        if name in project.classes
    ]

    all_map_results = map_results_4a + map_results_4b + mechanical_results
    reduce_result = _reduce_phase(all_map_results, controller_deps)

    return _assemble_module_drafts(all_map_results, reduce_result, project)
