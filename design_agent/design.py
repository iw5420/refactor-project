# design_agent/design.py
"""③ 架構設計 Agent：六章 LLM 設計階段——依 depends_on 拓樸分波，波內
平行呼叫 Claude API，逐模組組裝 InterfaceSpec 與 directory_tree 片段。

**LLM 呼叫契約刻意只問「機械規則判斷不了的部分」**，見 `prompts.py`
module docstring；本檔負責把「機械規則能決定的部分」（層級、檔名、
私有方法命名、Java→Python 型別對應、API 邊界方法的 openapi 覆寫）先
算好，組成 `_MethodContext`，再組出這次呼叫真正需要 LLM 回答的最小
問題集合，收到回應後合併回完整的 `InterfaceSpec`。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import javalang
import javalang.tree

from common.concurrency import default_concurrency
from common.java_annotations import DATA_CLASS_ANNOTATIONS, JPA_ENTITY_ANNOTATIONS
from common.llm_client import LlmJsonError, call_claude_for_json
from design_agent import layout, signature_scan, type_mapping
from design_agent.exceptions import DesignAgentCoverageError, DesignAgentModuleError
from design_agent.llm import DEFAULT_MODEL
from design_agent.prompts import DESIGN_OUTPUT_SCHEMA, DESIGN_SYSTEM_PROMPT
from design_agent.types import JavaClassSignature, JavaMethodSignature, ModuleDesignResult, UncoveredParam
from graph.state import ApiMapping, InterfaceSpec, ModuleInfo, ParamSpec

logger = logging.getLogger(__name__)

# 併發數＝可用核心數 - 1，跟 parse_agent/summarize.py、
# spec_collection_agent/chain_dependency_detect.py 共用同一份
# common/concurrency.py 實作，不各自重新推導（見 00 六章、05a 六章
# 步驟 2）。
_MAX_WAVE_WORKERS = default_concurrency()
# 05a 六章「單一 module 呼叫失敗時」：待重試清單、5 分鐘後統一重試一次，
# 比照 04a 四章的等待秒數。
_RETRY_WAIT_SECONDS = 300.0


@dataclass
class _MethodContext:
    """單一 Java 方法（可能是某個多載）在呼叫 LLM 之前已經機械算好的
    所有事實，design.py 內部使用，不跨模組交接。"""

    signature_key: str
    java_method: str
    class_name: str
    complexity: str
    function_name: str  # 已套用 camelCase→snake_case 與私有方法底線前綴（05a 七章）
    layer: str | None  # None 代表機械規則判斷不了，等 LLM 的 class_layers 決定
    params: list[ParamSpec]  # 已知的業務參數，不含 LLM 決定的框架注入/db session 參數
    return_type: str
    uncovered_params: list[UncoveredParam]
    needs_db_session_decision: bool
    boundary_schemas: list[tuple[str, dict]] = field(default_factory=list)  # (schema_name, resolved_schema)，見五章
    http_method: str | None = None  # 僅 boundary（selected_overload）非 None，見五章「router 層 API 邊界方法額外帶」
    route_path: str | None = None   # 同上，直接是 boundary["endpoint"] 原始字面值，不做轉換


def design_all_modules(
    module_list: list[ModuleInfo],
    api_to_python_target: list[ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
) -> tuple[list[InterfaceSpec], str, set[str]]:
    """對外入口，對應 05a 六章全節。回傳
    `(全部 module 攤平的 InterfaceSpec 清單, 組裝完成的 directory_tree 字串,
    實際產出過 schemas/{module}.py 的 module 名稱集合)`。

    第三個回傳值供 `route_mapping.build_route_mappings()` 判斷
    `related_files` 該不該納入 schema 檔案——不能只憑「這個 module 有沒有
    router 檔案」猜測，同一個 module 的 API 邊界方法若全部只用 inline
    schema（沒有 `$ref`，見 `type_mapping.schema_name_for()`），這個
    module 就不會真的產出 `schemas/{module}.py`，猜測會讓
    `route_to_file_mapping` 指向一個 directory_tree 裡實際上不存在的
    「幽靈檔案」，Debug Agent（⑦）跟著這個路徑去讀檔會撲空。
    """
    waves = layout.build_waves(module_list)
    boundary_index = _build_boundary_index(api_to_python_target)

    all_interfaces: list[InterfaceSpec] = []
    schema_fragments: list[str] = []
    modules_with_schema_file: set[str] = set()
    interfaces_by_module: dict[str, list[InterfaceSpec]] = {}

    for wave in waves:
        wave_results = _design_wave_with_retry(wave, boundary_index, openapi_spec, java_project_path, interfaces_by_module)
        for result in wave_results:
            interfaces_by_module[result.module] = result.interfaces  # type: ignore[assignment]
            all_interfaces.extend(result.interfaces)  # type: ignore[arg-type]
            if result.directory_tree_fragment:
                schema_fragments.append(result.directory_tree_fragment)
                modules_with_schema_file.add(result.module)

    directory_lines = _render_directory_lines(
        all_interfaces, modules_with_schema_file, [m["module"] for m in module_list]
    )
    infra_sections = [
        layout.render_code_section("app/core/database.py", layout.render_database_py()),
        layout.render_code_section("app/main.py", layout.render_main_py(all_interfaces)),
    ]
    directory_tree = layout.render_directory_tree(directory_lines, schema_fragments, infra_sections)

    return all_interfaces, directory_tree, modules_with_schema_file


def _render_directory_lines(
    interfaces: list[InterfaceSpec], modules_with_schema_file: set[str], all_module_names: list[str]
) -> list[str]:
    """05a 三章「格式慣例」第 1 段：純文字樹狀圖，列出所有檔案路徑。
    `interfaces` 是 routers／services／repositories 三層的權威來源；
    `app/schemas/{module}.py` 只在 `modules_with_schema_file`（見
    `design_all_modules()` 「幽靈檔案」說明）裡的 module 才列出，跟
    `route_mapping.build_route_mappings()` 判斷 `related_files`
    用的同一個集合，避免這裡列出一個實際沒有 Schema 定義段的檔案；
    `app/models/{module}.py` 是每個 module 都有的 SQLAlchemy ORM 佔位
    檔案（05a 三章，欄位內容由④生成，見九章），因此對 `all_module_names`
    逐一補上，不像 schemas 需要視內容而定。
    """
    files = {iface["file_path"] for iface in interfaces} | {"app/main.py", "app/core/database.py"}
    files |= {layout.schema_file_path(m) for m in modules_with_schema_file}
    files |= {layout.model_file_path(m) for m in all_module_names}
    return ["app/", *[f"  {f}" for f in sorted(files)]]


# --------------------------------------------------------------------------
# 波次執行與重試佇列（05a 六章「單一 module 呼叫失敗時」，比照 04a 四章）
# --------------------------------------------------------------------------


def _design_wave_with_retry(
    wave: list[ModuleInfo],
    boundary_index: dict[tuple[str, str, str], ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    interfaces_by_module: dict[str, list[InterfaceSpec]],
) -> list[ModuleDesignResult]:
    """對一整波（同一波內彼此不依賴）module 平行呼叫 Claude，失敗的
    module 列入待重試清單，這一波其餘 module 跑完後等待
    `_RETRY_WAIT_SECONDS` 秒統一重試一次；重試仍失敗則中止整條
    design run（見 05a 六章：`python_structure` 是 [P]／④ 唯一的權威
    規格，任何一個 module 的介面缺失風險遠高於重新執行一次）。
    """
    results, failed = _run_wave_batch(wave, boundary_index, openapi_spec, java_project_path, interfaces_by_module)
    if not failed:
        return results

    logger.warning(
        "%d 個 module 的六章設計呼叫失敗，等待 %.0f 秒後統一重試一次: %s",
        len(failed),
        _RETRY_WAIT_SECONDS,
        [m["module"] for m in failed],
    )
    time.sleep(_RETRY_WAIT_SECONDS)

    retry_results, still_failed = _run_wave_batch(failed, boundary_index, openapi_spec, java_project_path, interfaces_by_module)
    results.extend(retry_results)

    if still_failed:
        raise DesignAgentModuleError(
            f"{len(still_failed)} 個 module 的六章設計呼叫重試後仍失敗，中止整個 design run"
            f"（05a 六章的保守預設，見本函式 docstring）: {[m['module'] for m in still_failed]}"
        )
    return results


def _run_wave_batch(
    modules: list[ModuleInfo],
    boundary_index: dict[tuple[str, str, str], ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    interfaces_by_module: dict[str, list[InterfaceSpec]],
) -> tuple[list[ModuleDesignResult], list[ModuleInfo]]:
    """平行處理一批 module（同一波內），回傳
    (成功結果清單, 失敗待重試的 ModuleInfo 清單)。每個 module 的失敗
    互相隔離，不取消其他 module（同一波內彼此本來就互不依賴）。
    """
    results: list[ModuleDesignResult] = []
    failed: list[ModuleInfo] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_WAVE_WORKERS, len(modules))) as pool:
        futures = {
            pool.submit(_design_module, m, boundary_index, openapi_spec, java_project_path, interfaces_by_module): m
            for m in modules
        }
        for future in concurrent.futures.as_completed(futures):
            module = futures[future]
            try:
                results.append(future.result())
            except LlmJsonError as exc:
                logger.warning("module %s 的六章設計呼叫失敗，列入待重試清單: %s", module["module"], exc)
                failed.append(module)
    return results, failed


# --------------------------------------------------------------------------
# 單一 module 處理：機械前置 → （視需要）呼叫 Claude → 合併
# --------------------------------------------------------------------------


def _build_boundary_index(api_to_python_target: list[ApiMapping]) -> dict[tuple[str, str, str], ApiMapping]:
    """`(module, class_name, method_name) -> ApiMapping`，供
    `_build_method_contexts()` 判斷一個方法是不是 API 邊界方法。
    `ApiMapping.java_controller` 是 `"ClassName.method_name"`（見 04a
    六章 `assemble_api_mapping()`），這裡拆開重組成 key。

    **已知限制**：`java_controller` 不含參數簽名，若同一個 class 內有
    多載方法且剛好都是不同 endpoint 的 Controller 方法（比 04a 五章
    `voice`/`image` 那種 skip 案例更少見，但理論上可能發生），這裡的
    索引只保留最後一筆 `ApiMapping`。`_build_method_contexts()` 那邊
    再進一步限定：這唯一一筆 operation 只會套用到 `overloads` 清單裡
    `_select_boundary_overload()` 依參數個數選出的那一個多載，不是宣告
    順序第一個（見該函式 docstring），其餘多載一律退回機械型別對應。
    這仍然是猜測，不是精確消歧，留待接上真實專案規模評估是否需要升級
    成 list（並讓 `ApiMapping` 帶參數簽名徹底消歧）。
    """
    index: dict[tuple[str, str, str], ApiMapping] = {}
    for api in api_to_python_target:
        class_name, _, method_name = api["java_controller"].partition(".")
        index[(api["module"], class_name, method_name)] = api
    return index


def _python_function_name(java_method: str, is_private: bool) -> str:
    """05a 七章「私有／內部方法的命名慣例」：camelCase → snake_case，
    Java `private` 方法加底線前綴，保留在同一個 class／檔案內，不特別
    切出獨立檔案。
    """
    name = type_mapping.camel_to_snake(java_method)
    return f"_{name}" if is_private else name


def _select_boundary_overload(overloads: list[JavaMethodSignature], operation: dict) -> JavaMethodSignature:
    """`boundary_index` 對同名多載只能保留一筆 `ApiMapping`（見
    `_build_boundary_index()` 已知限制），這裡要在 `overloads`
    （javalang 掃描的宣告順序，見 05a 四章「多載方法的處理」）裡挑一個
    最可能對應這個 operation 的多載——不挑宣告順序第一個：Java 原始碼
    裡的宣告順序跟哪個多載才是真正的 Controller 端點無關，單純調整
    程式碼排版就可能悄悄換掉綁定結果。改用「Java 參數個數」跟
    operation 的 `parameters` + `requestBody`（算一個）比對，取參數
    個數差距最小的一個；並列時退回宣告順序，讓結果穩定可重現。這仍然
    是猜測，不是精確消歧（`ApiMapping` 本身不含參數型別，見
    `_build_boundary_index()` docstring），只是比「永遠挑宣告順序第一
    個」更貼近實際簽名形狀。
    """
    expected_count = len(operation.get("parameters") or []) + (1 if operation.get("requestBody") else 0)
    return min(overloads, key=lambda sig: (abs(len(sig.params) - expected_count), overloads.index(sig)))


def _build_method_contexts(
    module: ModuleInfo,
    class_signatures: dict[str, JavaClassSignature],
    boundary_index: dict[tuple[str, str, str], ApiMapping],
    openapi_spec: dict,
) -> list[_MethodContext]:
    """對應 05a 七章「強制規則：interfaces 必須涵蓋 module_list 裡每一
    個 module 的每一個方法」——以 `module["methods"]` 為準逐一處理，
    一個 `MethodInfo` 若在 Java 簽名裡對到多個多載，各自展開成獨立的
    `_MethodContext`（見 05a 四章「多載方法的處理」）。

    **`MethodInfo.class_name` 對不到真實 Java class／method 時立即中止**
    （`DesignAgentCoverageError`，見該類別 docstring「與 05b 原始設計的
    差異」）：這代表①的 Map 階段摘要跟③這次重新掃描 Java 原始碼的結果
    對不上——理論上不該發生（兩者都是對同一份 `java_project_path` 的
    解析結果），實測發生時代表某處有結構性 bug（如過去 `signature_scan.
    scan_java_files()` 漏掃 interface 宣告的案例），不是可以放著跳過的
    暫時性不一致。中止在呼叫任何 Claude API 之前發生，不浪費任何 LLM
    呼叫額度，錯誤訊息帶出精確的 module／class／method，方便直接定位
    是①的輸出問題還是③的掃描邏輯問題。

    **`(class_name, java_method)` 先去重，才逐一展開 overloads**：①對
    同一組多載方法會各自產生一筆獨立的 `MethodInfo`（例如
    `FileController.voice` 的 `@PostMapping`／`@GetMapping` 兩個 overload，
    各自帶不同的 `description`），這是①的正常行為，不是輸出錯誤。但
    `overloads` 是依「方法名稱」查找（javalang 沒有 overload resolution，
    見 04a 三章「決策」），同一組多載的每一筆 `MethodInfo` 查到的都是同
    一份完整 overloads 清單——若不先去重，兩筆 `MethodInfo` 會分別把整組
    overloads 各展開一次，[P] 的涵蓋率驗證會抓到重複的
    `(file_path, class_name, function_name)` 三元組而中止（實測對
    `lang-exam-api-refactor` 的 `FileController.voice`／`image` 兩組
    overload 觸發過）。這裡的去重只影響「同一組 overloads 只展開一次」，
    不影響 05a 四章既有的多載消歧邏輯（第一個保留原名、其餘加 `_2`/`_3`）
    ——那段邏輯本來就是針對「一次展開」設計的，去重後才符合它的前提。
    """
    known_classes = frozenset(class_signatures)
    contexts: list[_MethodContext] = []

    seen_method_keys: set[tuple[str, str]] = set()
    for method_info in module["methods"]:
        method_key = (method_info["class_name"], method_info["java_method"])
        if method_key in seen_method_keys:
            continue
        seen_method_keys.add(method_key)

        class_sig = class_signatures.get(method_info["class_name"])
        if class_sig is None:
            raise DesignAgentCoverageError(
                f"module {module['module']} 的方法 {method_info['class_name']}."
                f"{method_info['java_method']} 在四章重新掃描結果中找不到所屬類別"
                f"（見 DesignAgentCoverageError docstring）"
            )

        overloads = [m for m in class_sig.methods if m.method_name == method_info["java_method"]]
        if not overloads:
            raise DesignAgentCoverageError(
                f"module {module['module']} 的方法 {method_info['class_name']}."
                f"{method_info['java_method']} 在四章重新掃描結果中找不到同名方法"
                f"（見 DesignAgentCoverageError docstring）"
            )

        layer = layout.layer_for_stereotype(class_sig.stereotype)
        boundary = boundary_index.get((module["module"], class_sig.class_name, method_info["java_method"]))
        operation = (
            type_mapping.find_operation(boundary["endpoint"], boundary["http_method"], openapi_spec)
            if boundary is not None
            else None
        )
        # 這個 operation 只屬於「一個」物理方法，boundary_index 對同名
        # 多載本來就只能保留一筆 ApiMapping（見 _build_boundary_index()
        # docstring）。用 _select_boundary_overload() 依參數個數挑出最可能
        # 的那一個多載，而不是宣告順序第一個——不能讓迴圈裡剩下的其他
        # 多載也套用同一份 operation，那會把同一個 endpoint 的參數/回傳
        # 型別錯誤地複製到不相干的多載方法上。
        selected_overload = _select_boundary_overload(overloads, operation) if operation is not None else None

        # 多載消歧（05a 四章「多載方法的處理」）：同一組 overloads 的
        # sig.method_name 相同，_python_function_name() 對每個 sig 都會
        # 算出同一個基礎名稱，必須疊加計數器消歧，否則同一個 class／
        # 檔案會產出兩個同名 InterfaceSpec（Python 不支援多載，屬於非法
        # 輸出）。第一次出現保留原始名稱，第二次起加 `_2`、`_3`……
        seen_names: dict[str, int] = {}

        for sig in overloads:
            if sig is selected_overload:
                params, return_type = type_mapping.resolve_api_boundary_signature(sig, operation, openapi_spec)
                uncovered = type_mapping.find_uncovered_framework_params(sig, operation, openapi_spec)
                # ResponseEntity<T> 方法的 return_type 已被覆寫成
                # "Response"（見 resolve_api_boundary_signature()「見
                # 09b_bug_trace.md #30」），不需要、也不該為它收集 response
                # schema——這種情況下 openapi 的 response schema 不代表
                # 這個方法真正的回傳形狀，硬收集只會產生一個永遠不會被
                # 引用的多餘 Pydantic class。
                boundary_schemas = (
                    []
                    if type_mapping.is_response_entity_return_type(sig.return_type)
                    else type_mapping.collect_named_schemas(operation, openapi_spec)
                )
                # 05a 五章「router 層 API 邊界方法額外帶 http_method／route_path」：
                # 只有真正被選中對應這個 endpoint 的多載才帶這兩個值，其餘多載
                # （下面 else 分支）維持 None，跟 params/return_type 的處理方式一致。
                http_method = boundary["http_method"]
                route_path = boundary["endpoint"]
            else:
                params = [
                    ParamSpec(name=p.name, type=type_mapping.map_java_type(p.java_type, known_classes))
                    for p in sig.params
                ]
                return_type = type_mapping.map_java_type(sig.return_type or "void", known_classes)
                uncovered = []
                boundary_schemas = []
                http_method = None
                route_path = None

            base_name = _python_function_name(sig.method_name, sig.is_private)
            seen_names[base_name] = seen_names.get(base_name, 0) + 1
            function_name = base_name if seen_names[base_name] == 1 else f"{base_name}_{seen_names[base_name]}"

            contexts.append(
                _MethodContext(
                    signature_key=sig.signature_key,
                    java_method=sig.method_name,
                    class_name=class_sig.class_name,
                    complexity=method_info["complexity"],
                    function_name=function_name,
                    layer=layer,
                    params=params,
                    return_type=return_type,
                    uncovered_params=uncovered,
                    # 三層都問，不只 services/repositories：FastAPI+SQLAlchemy 的
                    # db session 慣例上由 router 層的 `Depends(get_db)` 建立，
                    # 再一路傳給它呼叫的 service/repository（見 _design_module()
                    # 合併階段對 layer=="routers" 的特殊處理），router 方法若會
                    # 呼叫到需要 db 的下游，同樣需要宣告這個參數，不能排除在外。
                    needs_db_session_decision=True,
                    boundary_schemas=boundary_schemas,
                    http_method=http_method,
                    route_path=route_path,
                )
            )
    return contexts


def _reorder_params_defaults_last(params: list[ParamSpec]) -> list[ParamSpec]:
    """Python 函式簽名要求「帶預設值的參數必須排在不帶預設值的參數之後」
    （否則 `SyntaxError: parameter without a default follows parameter
    with a default`，已用真實案例驗證：`def f(a: str = 1, b: int): ...`）。
    下方 `_design_module()` 組 `params` 的順序是業務參數（`ctx.params`，
    Java 原始簽名順序，一律不帶預設值）→ `extra_params`（六章 LLM 決定
    的框架注入參數，型別字串可能像 `User = Depends(get_current_user)`
    這樣自帶預設值，順序由 LLM 回應決定，沒有保證）→ `db`（機械附加，
    只有 routers 層帶 `= Depends(get_db)` 預設值，其餘層級不帶）。任何
    一個 `extra_params` 項目若帶預設值、且後面接著沒有預設值的其他項目
    （另一個 `extra_params`，或機械附加的 `db`），組裝出的簽名就會違反
    這條規則——這不是理論邊角案例，只要 LLM 判斷某個框架注入參數該用
    FastAPI 的 `Depends(...)` 慣例（05a 五章「框架注入物件」原文舉的
    例子就是這個），就有機會踩到。

    穩定分割（stable partition）：不帶預設值的參數維持原有相對順序排在
    前面，帶預設值的參數維持原有相對順序排在後面——不改變同一類別內部
    的順序，只調整「有沒有預設值」這一個維度，保證輸出永遠是合法 Python
    函式簽名，不需要要求 LLM 或後續哪個環節自己保證順序正確。用字面
    `"="` 子字串比對判斷「帶不帶預設值」：這個專案目前所有帶預設值的
    型別字串都來自這裡（`db` 的機械附加）或六章 LLM 的 `extra_params`
    （如 `Depends(...)` 慣例），`map_java_type()`／openapi 覆寫產出的
    型別字串從不含 `=`，比對不會誤判。
    """
    no_default = [p for p in params if "=" not in p["type"]]
    with_default = [p for p in params if "=" in p["type"]]
    return no_default + with_default


# 保留模組名稱，承接 parse_agent/summarize.py `_assemble_global_advice_
# draft()` 產出的全域生效類別（`@RestControllerAdvice`／
# `@ControllerAdvice`，見 09b_bug_trace.md #11/#12）。跟 summarize.py 那
# 邊的 `_GLOBAL_MODULE_NAME` 是同一個字面值，兩邊各自定義常數而不共用
# 匯入——`parse_agent`／`design_agent` 是各自獨立套件，不互相 import
# （01 二章「設計原則」），這個字面值本身極不可能變動，重複定義的維護
# 成本遠低於為了共用一個常數在兩個套件之間建立耦合。
_GLOBAL_MODULE_NAME = "_global"


def _exception_handler_targets(java_files: list[str], java_project_path: str) -> dict[str, str]:
    """重新掃 `_global` module 的 `java_files`，逐 method 找
    `@ExceptionHandler(X.class)` 的目標例外類別名稱，回傳
    `{method_name: exception_class_name}`。**只收「單一 class-literal」
    形式**（javalang 把 `X.class`解析成 `ClassReference(type=
    ReferenceType(name="X"))`）——`@ExceptionHandler({A.class, B.class})`
    這種陣列形式的 `element` 不是 `ClassReference`，不會被收進這份
    對照表，呼叫端（`_design_global_advice_module()`）對查不到的方法
    一律記警告略過，不嘗試處理（見 09b_bug_trace.md #11/#12「範圍刻意
    收斂」）。這是獨立於 `signature_scan.scan_java_files()` 之外的小型
    專用掃描——一般業務 module 不需要 annotation 的字面值，只有這個
    特殊模組需要，不值得為了這一種用途替 `JavaMethodSignature` 加欄位。
    """
    targets: dict[str, str] = {}
    for rel_path in java_files:
        source = Path(java_project_path, rel_path).read_text(encoding="utf-8")
        tree = javalang.parse.parse(source)
        for decl in tree.types:
            if not isinstance(decl, javalang.tree.ClassDeclaration):
                continue
            for method_decl in decl.methods:
                for ann in method_decl.annotations:
                    if ann.name != "ExceptionHandler":
                        continue
                    element = ann.element
                    if isinstance(element, javalang.tree.ClassReference):
                        targets[method_decl.name] = element.type.name
    return targets


def _design_global_advice_module(module: ModuleInfo, java_project_path: str) -> ModuleDesignResult:
    """`_global` 保留模組的專屬處理路徑，完全繞過一般模組的
    `_build_method_contexts()`／`_call_design_llm()`（不查
    `_STEREOTYPE_LAYER`，這批方法不屬於 routers/services/repositories
    任何一層的既有分層慣例）。固定輸出 `layout.EXCEPTION_HANDLERS_FILE`、
    `class_name=None` 自由函式，簽名比照 FastAPI `@app.exception_
    handler(...)` 的呼叫慣例（`request: Request, exc: Exception) ->
    Response`），不是 Java 原始簽名的機械轉換——FastAPI 的例外處理器
    協定本來就要求這個固定形狀，跟 Java 端的方法簽名無關。

    **範圍刻意收斂**：只處理 `@ExceptionHandler(Exception.class)` 這種
    全域 catch-all case（見 `docs/09b_bug_trace.md` #11/#12 唯一有真實
    案例佐證的情況）。`module["methods"]` 裡若有方法不是
    `@ExceptionHandler`、或標註了 `Exception` 以外的例外類型（如真實
    案例裡同時存在的 `handleBaseException(BaseException e)`），只記
    警告、不產生任何 `InterfaceSpec`——這些方法因此不會流進 [P] 的
    task list，不會被實作，是已知、刻意接受的限制，不是遺漏。函式
    本體（哪個例外對應什麼 code/msg）不在這裡機械產生——比照一般
    service/repository 方法，經 [P] 產生 task、走⑤既有的
    `fill_function()` 流程翻譯 Java handler 方法本體，見
    `09b_bug_trace.md` #11/#12「① 已完成的前置修正」。
    """
    targets = _exception_handler_targets(module["java_files"], java_project_path)

    interfaces: list[InterfaceSpec] = []
    for method_info in module["methods"]:
        java_method = method_info["java_method"]
        target = targets.get(java_method)
        if target != "Exception":
            logger.warning(
                "module %s 的方法 %s.%s 不是 @ExceptionHandler(Exception.class) 這種全域 "
                "catch-all case（實際目標：%s），範圍刻意收斂只處理 catch-all，略過（見 "
                "docs/09b_bug_trace.md #11/#12「範圍刻意收斂」）",
                module["module"], method_info["class_name"], java_method, target,
            )
            continue
        interfaces.append(
            InterfaceSpec(
                file_path=layout.EXCEPTION_HANDLERS_FILE,
                class_name=None,
                function_name=_python_function_name(java_method, is_private=False),
                params=[ParamSpec(name="request", type="Request"), ParamSpec(name="exc", type="Exception")],
                return_type="Response",
                http_method=None,
                route_path=None,
            )
        )

    return ModuleDesignResult(module=module["module"], interfaces=interfaces, directory_tree_fragment=None)


def _design_module(
    module: ModuleInfo,
    boundary_index: dict[tuple[str, str, str], ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    interfaces_by_module: dict[str, list[InterfaceSpec]],
) -> ModuleDesignResult:
    """對應 05a 六章「單一 module 的 Claude 呼叫內容」全表格：組出這個
    module 的 `_MethodContext` 清單、決定這次呼叫真正需要 LLM 回答的
    問題（無 stereotype 類別的層級、框架注入參數、db session 判斷），
    視需要呼叫一次 Claude，合併回完整的 `InterfaceSpec`。

    **`_GLOBAL_MODULE_NAME` 走完全獨立的專屬路徑**（見
    `_design_global_advice_module()`）：這個保留模組的方法不屬於任何
    業務分層慣例，機械決策即可，不需要（也不該）套用下面一般模組的
    LLM 層級判斷／API 邊界覆寫邏輯。
    """
    if module["module"] == _GLOBAL_MODULE_NAME:
        return _design_global_advice_module(module, java_project_path)

    class_signatures = signature_scan.scan_java_files(module["java_files"], java_project_path)
    contexts = _build_method_contexts(module, class_signatures, boundary_index, openapi_spec)

    # 只問「有一般方法」的無 stereotype 類別要歸哪一層：一個 class 若
    # `methods` 是空清單（常見情況：只有建構子的自訂例外類別，見
    # JavaClassSignature.constructors docstring），永遠不會有任何
    # _MethodContext 引用到它（overloads 永遠篩不到東西），LLM 回答的
    # 層級因此永遠用不到——這批 class 改走下方 orphan class 處理，不需要
    # 也不該浪費一次 LLM 問答在一個沒有答案會被使用的問題上。
    classes_needing_layer = [c for c in class_signatures.values() if c.stereotype is None and c.methods]
    methods_needing_decision = [c for c in contexts if c.uncovered_params or c.needs_db_session_decision]

    if classes_needing_layer or methods_needing_decision:
        layer_by_class, extra_params_by_key, db_session_by_key = _call_design_llm(
            module, classes_needing_layer, methods_needing_decision, interfaces_by_module
        )
    else:
        layer_by_class, extra_params_by_key, db_session_by_key = {}, {}, {}

    interfaces: list[InterfaceSpec] = []
    schema_class_fields: list[tuple[str, list[tuple[str, str]]]] = []
    seen_schema_names: set[str] = set()

    for ctx in contexts:
        layer = ctx.layer or layer_by_class.get(ctx.class_name)
        if layer is None:
            logger.warning(
                "module %s 的類別 %s 沒有 stereotype、LLM 也沒有回答 class_layers，"
                "略過這個方法（05a 六章要求 class_layers 必須逐一回答，理論上走不到"
                "這裡，除非重試佇列已經放行——見 05a 十二章保守中止預設）",
                module["module"], ctx.class_name,
            )
            continue

        params = list(ctx.params)
        params.extend(extra_params_by_key.get(ctx.signature_key, []))
        if db_session_by_key.get(ctx.signature_key, False):
            # FastAPI 的 DI 只在「被 @router 裝飾的端點函式」這一層生效，
            # 只有 routers 層需要 `= Depends(get_db)` 讓 FastAPI 實際建立
            # session；services/repositories 層的 db 是呼叫端（router 或
            # 上一層）以一般引數往下傳，宣告成不帶預設值的 `db: Session`
            # 才不會被 FastAPI 誤判成一般參數硬要求呼叫端另外提供。這裡
            # 用「最終解析出的 layer」（可能來自 LLM 的 class_layers）
            # 判斷，不是 ctx.layer（那個在無 stereotype 類別時還沒定案）。
            db_type = "Session = Depends(get_db)" if layer == "routers" else "Session"
            params.append(ParamSpec(name="db", type=db_type))
        params = _reorder_params_defaults_last(params)

        interfaces.append(
            InterfaceSpec(
                file_path=layout.file_path_for_layer(module["module"], layer),
                class_name=None if layer == "routers" else ctx.class_name,
                function_name=ctx.function_name,
                params=params,
                return_type=ctx.return_type,
                http_method=ctx.http_method,
                route_path=ctx.route_path,
            )
        )

        for schema_name, resolved_schema in ctx.boundary_schemas:
            if schema_name in seen_schema_names:
                continue
            seen_schema_names.add(schema_name)
            schema_class_fields.append((schema_name, type_mapping.extract_schema_fields(resolved_schema)))

    # orphan class：class_signatures 裡沒有任何 InterfaceSpec 的 class_name
    # 指向它——這批 class 完全不會被 contexts 迴圈碰到（見上面
    # classes_needing_layer 的說明），若不另外處理，在 python_structure
    # 裡會完全沒有任何痕跡。依機械事實分五種情況處理（見 05a 三章「孤兒
    # 類別／資料容器占位」判斷優先序，理由見 common/java_annotations.py
    # docstring）：
    #   1. @Entity（含 @Embeddable/@MappedSuperclass）→ 跳過，DB schema
    #      欄位規格是④的職責（05a 九章），不是③要處理的範圍
    #   2. 有 Lombok/JPA 資料標記 → 渲染 dataclass（欄位為主，高信心）
    #   3. 有建構子（現有 AuthException 案例，順序與行為不變）→ 渲染
    #      建構子占位
    #   4. 有欄位、無標記（低信心推斷）→ 渲染 dataclass
    #   5. 什麼都沒有 → 只記警告，不渲染空段落
    # 已經出現在 seen_schema_names（API 邊界 openapi 展開產出的 schema
    # 名稱）的 class 一併排除，避免同一個型別被兩種機制各渲染一次。
    known_classes = frozenset(class_signatures)
    covered_class_names = {iface["class_name"] for iface in interfaces if iface["class_name"]}
    orphan_classes: list[tuple[str, str, list[list[tuple[str, str]]]]] = []
    data_carrier_classes: list[tuple[str, str, bool, list[tuple[str, str]], str]] = []

    def _data_carrier_entry(
        sig: JavaClassSignature, confidence_note: str
    ) -> tuple[str, str, bool, list[tuple[str, str]], str]:
        frozen = bool(sig.fields) and all(f.is_final for f in sig.fields)
        fields = [(f.name, type_mapping.map_java_type(f.java_type, known_classes)) for f in sig.fields]
        return (sig.class_name, sig.file_path, frozen, fields, confidence_note)

    for name, sig in class_signatures.items():
        # 這整棵決策樹的前提是「methods 為空清單」（見本函式上方註解、
        # 05a 三章「孤兒類別與資料容器占位」開頭）。單靠 covered_class_
        # names 判斷「已處理過」不夠：routers 層的 InterfaceSpec.
        # class_name 一律是 None（05a 七章既有規則），covered_class_names
        # 因此永遠不會包含 router 類別的真實 class name，若不額外檢查
        # sig.methods，一個正常、方法齊全的 @RestController（如只是剛好
        # 沒有 constructor／field 的無狀態 Controller）會被誤判成孤兒
        # 類別，嚴重時甚至把它渲染成錯誤的 dataclass 段落。
        if sig.methods or name in covered_class_names or name in seen_schema_names:
            continue
        annotation_set = set(sig.annotations)
        if annotation_set & JPA_ENTITY_ANNOTATIONS:
            continue
        if annotation_set & DATA_CLASS_ANNOTATIONS:
            data_carrier_classes.append(_data_carrier_entry(sig, "偵測到 Lombok/JPA 資料標記"))
        elif sig.constructors:
            orphan_classes.append((
                name,
                sig.file_path,
                [
                    [(p.name, type_mapping.map_java_type(p.java_type, known_classes)) for p in ctor.params]
                    for ctor in sig.constructors
                ],
            ))
        elif sig.fields:
            data_carrier_classes.append(_data_carrier_entry(sig, "無 Lombok 標記，依欄位宣告推斷"))
        else:
            logger.warning(
                "module %s 的類別 %s 沒有方法、建構子、欄位，也沒有被任何 InterfaceSpec 覆蓋，"
                "略過（05a 三章孤兒類別判斷：沒有任何機械事實可渲染）",
                module["module"], name,
            )

    fragments: list[str] = []
    if schema_class_fields:
        fragments.append(layout.render_schema_section(layout.schema_file_path(module["module"]), schema_class_fields))
    if orphan_classes:
        fragments.append(
            layout.render_class_placeholder_section(layout.schema_file_path(module["module"]), orphan_classes)
        )
    if data_carrier_classes:
        fragments.append(
            layout.render_dataclass_section(layout.schema_file_path(module["module"]), data_carrier_classes)
        )
    directory_tree_fragment = "\n".join(fragments) if fragments else None

    return ModuleDesignResult(module=module["module"], interfaces=interfaces, directory_tree_fragment=directory_tree_fragment)


# --------------------------------------------------------------------------
# Claude API 呼叫（05a 六章）
# --------------------------------------------------------------------------


def _call_design_llm(
    module: ModuleInfo,
    classes_needing_layer: list[JavaClassSignature],
    methods_needing_decision: list[_MethodContext],
    interfaces_by_module: dict[str, list[InterfaceSpec]],
) -> tuple[dict[str, str], dict[str, list[ParamSpec]], dict[str, bool]]:
    """呼叫一次 Claude，回傳
    `(class_name -> layer, signature_key -> extra_params, signature_key -> needs_db_session)`。
    未涵蓋的 `classes_needing_layer`／`methods_needing_decision` 視為
    LLM 沒有完整回答，拋出 `LlmJsonError` 交給上層的待重試佇列（見
    `design_all_modules()` 模組 docstring「重試仍失敗則中止整條 design
    run」）。
    """
    descriptions = {(m["class_name"], m["java_method"]): m["description"] for m in module["methods"]}

    payload = {
        "module": {"module": module["module"], "summary": module["summary"]},
        "classes_needing_layer": [
            {
                "class_name": cls.class_name,
                "methods": [
                    {"method_name": m.method_name, "description": descriptions.get((cls.class_name, m.method_name), "")}
                    for m in cls.methods
                ],
            }
            for cls in classes_needing_layer
        ],
        "methods": [
            {
                "signature_key": ctx.signature_key,
                "java_method": ctx.java_method,
                "class_name": ctx.class_name,
                "description": descriptions.get((ctx.class_name, ctx.java_method), ""),
                "uncovered_params": [
                    {"name": u.param.name, "java_type": u.param.java_type} for u in ctx.uncovered_params
                ],
                "needs_db_session_decision": ctx.needs_db_session_decision,
            }
            for ctx in methods_needing_decision
        ],
        "upstream_interfaces": [iface for dep in module["depends_on"] for iface in interfaces_by_module.get(dep, [])],
    }
    user_prompt = json.dumps(payload, ensure_ascii=False)

    result = call_claude_for_json(
        system_prompt=DESIGN_SYSTEM_PROMPT, user_prompt=user_prompt, schema=DESIGN_OUTPUT_SCHEMA, model=DEFAULT_MODEL
    )

    expected_classes = {c.class_name for c in classes_needing_layer}
    returned_classes = {entry["class_name"] for entry in result["class_layers"]}
    missing_classes = expected_classes - returned_classes
    if missing_classes:
        raise LlmJsonError(f"module {module['module']} 的回應遺漏了 class_layers（視同呼叫失敗）: {sorted(missing_classes)}")

    expected_methods = {ctx.signature_key for ctx in methods_needing_decision}
    returned_methods = {entry["signature_key"] for entry in result["method_decisions"]}
    missing_methods = expected_methods - returned_methods
    if missing_methods:
        raise LlmJsonError(f"module {module['module']} 的回應遺漏了 method_decisions（視同呼叫失敗）: {sorted(missing_methods)}")

    layer_by_class = {entry["class_name"]: entry["layer"] for entry in result["class_layers"]}
    extra_params_by_key = {
        entry["signature_key"]: [ParamSpec(name=p["name"], type=p["type"]) for p in entry["extra_params"]]
        for entry in result["method_decisions"]
    }
    db_session_by_key = {entry["signature_key"]: entry["needs_db_session"] for entry in result["method_decisions"]}

    return layer_by_class, extra_params_by_key, db_session_by_key
