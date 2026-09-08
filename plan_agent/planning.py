# plan_agent/planning.py
"""[P] Plan Agent 核心邏輯：四章 module／layer 歸屬判定（委派給
`module_index.classify()`）、五章 `phase`／`translator_backend`／
`description`／`context` 機械組裝（純機械，不呼叫 Claude API，見 06a 五章
「為什麼這裡不再是 LLM 設計階段」）、六章呼叫鏈範圍查找
（`reference_targets` 組裝）、七章 `depends_on` 組裝、八章 `target_files`
組裝、九章 `task_list` 組裝與涵蓋率驗證。
"""
from __future__ import annotations

from dataclasses import dataclass

from graph.state import InterfaceSpec, JavaIndexEntry, ModuleInfo, PythonStructure, ReferenceTarget, TaskSpec
from parse_agent.call_graph import parse_java_project
from plan_agent import call_chain, module_index
from plan_agent.exceptions import PlanAgentCoverageError

# 06a 五章「translator_backend：依 layer 機械決定」——只有 repository
# 交給 qwen，其餘一律 claude（entity/dto 不產生 InterfaceSpec，不會走到
# 這條分派規則，見三章「全域基礎設施檔案與資料類別排除」）。
_QWEN_LAYER = "repositories"

_RESERVED_MODULES = frozenset({module_index.UTILS_MODULE_NAME, module_index.GLOBAL_MODULE_NAME})


@dataclass(frozen=True)
class _TaskDraft:
    """單一 `InterfaceSpec` 對應的組裝草稿（尚未算 `depends_on`／`id`），
    `planning.py` 內部使用，不跨模組交接。"""

    file_path: str
    class_name: str | None
    function_name: str
    module: str
    layer: str
    phase: int
    translator_backend: str
    java_method_id: str
    description: str
    context: str
    reference_targets: list[ReferenceTarget]
    reference_targets_truncated: bool
    return_type: str


def plan_all_modules(
    module_list: list[ModuleInfo], python_structure: PythonStructure, java_project_path: str
) -> tuple[list[TaskSpec], list[ModuleInfo]]:
    """對外入口，對應 06a 全文。第一個回傳值直接對應
    `RefactorState.task_list`（見 06a 九章）。[P] 不呼叫 Claude API，
    整個函式是同步、快速完成的機械組裝，不需要重試佇列（見 06a 十一章
    「執行特性的變化」）。

    `java_project_path`：六章「呼叫鏈範圍查找」需要重用①既有的
    `parse_agent.call_graph.parse_java_project()` 取得呼叫圖（純函式，
    當場對 `java_project_path` 重新算一次，見 06a 二章）。

    **第二個回傳值：補回 `_utils` 保留 module 之後的 `module_list`**——
    真實環境發現 `graph/scheduler.py::ModuleScheduler` 用呼叫端傳入的
    `module_list` 建構自己追蹤的 module 集合，`_utils` 這個保留 module
    （見 `module_index.classify()`）只存在於 task 的 `module` 欄位上，
    ①的 `module_list` 從來沒有對應條目——`ModuleScheduler.get_ready_
    tasks()` 因此永遠不會走訪到 `_utils`，這個 module 底下的 task 永遠
    排不進就緒佇列（已用真實資料證實：12 個 `_utils` task，
    `get_ready_tasks()` 從頭到尾不回傳任何一筆）。`_global` 沒有這個
    問題——①（`parse_agent/summarize.py::_assemble_global_advice_
    draft()`）本來就會機械組一筆真正的 `ModuleInfo` 塞進 `module_list`；
    `_utils` 沒有對應的①端機制，因為它是這次 call-chain-implement 重構
    才新增的 [P] 端保留 module 概念，見 `_build_utils_module_entry()`。
    """
    module_names = frozenset(m["module"] for m in module_list)
    module_rank = _build_module_rank(module_list)
    config_field_mappings = python_structure.get("config_field_mappings", {})
    modules_with_schema_file = _modules_with_schema_file(python_structure["directory_tree"], module_names)

    # 六章：呼叫鏈範圍查找需要的兩份共用資料——①的呼叫圖只重新算一次，
    # 所有 task 共用，不逐 task 重算；module 依賴閉包同理。
    call_graph = parse_java_project(java_project_path).call_graph
    java_index = python_structure.get("java_index", {})
    module_closures = call_chain.build_module_closures(module_list)

    # 四章／五章／六章：對每一個 InterfaceSpec 產生恰好一個草稿——這個
    # 1:1 list comprehension 本身就是三章「涵蓋率規則」與五章「不再是
    # LLM 設計階段」兩件事共同的直接後果，沒有 LLM 回應可能漏答，涵蓋率
    # 因結構保證成立，見 `_validate_coverage()`。
    drafts = [
        _build_draft(iface, module_names, config_field_mappings, call_graph, java_index, module_closures)
        for iface in python_structure["interfaces"]
    ]

    _validate_coverage(drafts, python_structure["interfaces"])

    task_list = _assemble_task_list(drafts, module_rank, modules_with_schema_file)
    utils_entry = _build_utils_module_entry(drafts)
    resolved_module_list = module_list + ([utils_entry] if utils_entry else [])
    return task_list, resolved_module_list


def _build_utils_module_entry(drafts: list[_TaskDraft]) -> ModuleInfo | None:
    """補回 `plan_all_modules()` docstring「第二個回傳值」描述的缺口。
    只在真的有 task 落在 `_utils` module 時才產生（沒有 utils 類別的
    目標專案不需要這筆，維持 `module_list` 乾淨）；`depends_on` 固定為
    空，比照 `_global` 既有先例（`summarize.py::_assemble_global_advice_
    draft()`）——utils 是最基礎的層，不依賴任何業務模組完成才能開始
    實作，也不該讓 `ModuleScheduler._module_deps_satisfied()` 誤判成有
    依賴關係卡住它。`java_files` 從落在這個 module 的 task 的
    `java_method_id`（"{file_path}::{class_name}::{method_name}"）反推
    第一段還原，只是給人工事後核對用的輔助資訊，不影響排程行為。
    `methods` 留空——`ModuleInfo.methods` 是③設計介面邊界時參考的業務
    語境（04a），這個階段（[P] 之後）不會再有任何下游讀這個欄位。
    """
    utils_drafts = [d for d in drafts if d.module == module_index.UTILS_MODULE_NAME]
    if not utils_drafts:
        return None
    java_files = sorted({d.java_method_id.split("::", 1)[0] for d in utils_drafts})
    return ModuleInfo(
        module=module_index.UTILS_MODULE_NAME,
        summary="跨業務 module 共用的工具類別（app/utils/ 保留 module，見 06a 四章特例一）",
        java_files=java_files,
        depends_on=[],
        methods=[],
    )


def _build_module_rank(module_list: list[ModuleInfo]) -> dict[str, int]:
    """對應 06a 八章「id 產生規則」：`module_list` 原始順序當基礎排序，
    `_utils`／`_global` 這兩個不在 `module_list` 裡的保留 module（見 06a
    四章特例一／二）固定排在所有業務 module 之後。
    """
    rank = {m["module"]: i for i, m in enumerate(module_list)}
    rank[module_index.UTILS_MODULE_NAME] = len(module_list)
    rank[module_index.GLOBAL_MODULE_NAME] = len(module_list) + 1
    return rank


# --------------------------------------------------------------------------
# 四章、五章：module／layer 歸屬判定 + phase／translator_backend／
# description／context 機械組裝
# --------------------------------------------------------------------------


def _build_draft(
    iface: InterfaceSpec,
    module_names: frozenset[str],
    config_field_mappings: dict[str, dict[str, str]],
    call_graph: dict[str, list[str]],
    java_index: dict[str, JavaIndexEntry],
    module_closures: dict[str, set[str]],
) -> _TaskDraft:
    module, layer = module_index.classify(iface["file_path"], module_names)
    # 06a 六章「呼叫鏈範圍查找」——同樣不重新判斷、不需要 fallback，
    # ③保證每一筆 InterfaceSpec 都會設定 java_method_id，理由同 phase。
    reference_targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id=iface["java_method_id"],
        seed_module=module,
        seed_layer=layer,
        call_graph=call_graph,
        java_index=java_index,
        module_names=module_names,
        module_closures=module_closures,
    )
    return _TaskDraft(
        file_path=iface["file_path"],
        class_name=iface["class_name"],
        function_name=iface["function_name"],
        module=module,
        layer=layer,
        # 06a 五章「phase：直接複製 InterfaceSpec.phase」——不重新判斷、
        # 不需要 fallback，③保證每一筆 design_agent 產出的 InterfaceSpec
        # 都會設定這個欄位；缺席代表③違反自己的輸出契約，直接讓
        # KeyError 往上拋，不嘗試靜默補值掩蓋上游問題。
        phase=iface["phase"],
        translator_backend="qwen" if layer == _QWEN_LAYER else "claude",
        java_method_id=iface["java_method_id"],
        description=_mechanical_description(iface["file_path"], iface["class_name"], iface["function_name"]),
        context=_config_hint(iface["file_path"], config_field_mappings),
        reference_targets=reference_targets,
        reference_targets_truncated=truncated,
        # 對應 docs/refactor_bug_trace.md #46：直接複製 InterfaceSpec.
        # return_type，不重新判斷——理由同 phase：③保證每一筆
        # InterfaceSpec 都會設定這個欄位。
        return_type=iface["return_type"],
    )


def _mechanical_description(file_path: str, class_name: str | None, function_name: str) -> str:
    """對應 06a 五章「description：改為機械模板字串」——只用
    `InterfaceSpec` 本身的欄位，不含 Java 方法名稱（①的 `MethodInfo.
    java_method` 跟③的 `InterfaceSpec.function_name` 之間沒有形式化的
    對應欄位，見二章、三章），純供 log／除錯追蹤用，不是餵給 ⑤ 的語意
    輸入。
    """
    target = f"{class_name}.{function_name}()" if class_name else f"{function_name}()"
    return f"填入 {file_path} 的 {target}"


def _config_hint(file_path: str, config_field_mappings: dict[str, dict[str, str]]) -> str:
    """對應 06a 五章「`config_field_mappings` 折進 `context`」——`context`
    唯一已知的內容來源，純機械字串附加，不呼叫 LLM（對應
    `docs/09b_bug_trace.md` #45／#58）。沒有對應項目時回傳空字串，
    `context` 不強行塞入內容（見五章「未來若出現其他...沒有這類事實的
    task，context 維持空字串」）。

    提示文字明講 `app/core/config.py` 只有裸模組層級常數、沒有 `settings`
    物件包裝——⑤過去曾連續三次把點記法（`app.core.config.LANGUAGE_CODE`）
    誤讀成物件屬性存取、幻覺出從未存在的 `settings` 物件（#58 真實案例），
    因此直接把最常見的錯誤點名禁止，不只是給參照路徑。
    """
    field_map = config_field_mappings.get(file_path)
    if not field_map:
        return ""
    field_hints = "；".join(
        f"`{java_field}` 欄位改成 `from app.core.config import {python_ref.rsplit('.', 1)[-1]}` "
        f"後直接使用 `{python_ref.rsplit('.', 1)[-1]}`"
        for java_field, python_ref in field_map.items()
    )
    return (
        f"這個類別有 Spring @Value 屬性注入欄位：{field_hints}"
        "（環境變數注入，已由 ④ 生成，見 app/core/config.py）。"
        "app/core/config.py 裡只有裸模組層級常數（例如 "
        '`LANGUAGE_CODE = os.environ["LANGUAGE_CODE"]`），'
        "沒有 settings 物件、沒有任何 class 包裝——絕對不要寫成 "
        "`settings.LANGUAGE_CODE` 這種物件屬性存取，也不要臆測其他來源（如框架 bean）。"
    )


# --------------------------------------------------------------------------
# 八章：涵蓋率驗證（機械，收尾步驟）
# --------------------------------------------------------------------------


def _validate_coverage(drafts: list[_TaskDraft], interfaces: list[InterfaceSpec]) -> None:
    """對應 06a 八章「涵蓋率驗證」。`drafts` 是對 `interfaces` 做 1:1
    list comprehension 的直接結果，長度天生相等，理論上不會觸發——這裡
    是最後一道 defense-in-depth，專門攔截 `interfaces` 本身就存在重複
    `(file_path, class_name, function_name)` 三元組的情況（③輸出的
    正確性缺陷，理論上不該發生），不是需要人工判斷的模糊情況。
    """
    ids = [(d.file_path, d.class_name, d.function_name) for d in drafts]
    if len(ids) != len(set(ids)):
        raise PlanAgentCoverageError(
            "python_structure.interfaces 存在重複的 (file_path, class_name, function_name) 三元組"
            "（見 06a 八章 defense-in-depth 說明），無法保證 1:1 涵蓋"
        )
    if len(drafts) != len(interfaces):
        # 理論上不可能發生（drafts 直接由 interfaces 做 1:1 list
        # comprehension 產生），防禦性保留，真的觸發代表 _build_draft()
        # 內部邏輯有 bug（如中途過濾掉了某些項目）。
        raise PlanAgentCoverageError(
            f"drafts 數量（{len(drafts)}）與 interfaces 數量（{len(interfaces)}）不一致"
            "（見 06a 八章 defense-in-depth 說明），代表 planning.py 組裝邏輯本身有 bug"
        )


# --------------------------------------------------------------------------
# 七章：本 module 的 schemas／models 判定、target_files 組裝
# --------------------------------------------------------------------------


def _modules_with_schema_file(directory_tree: str, module_names: frozenset[str]) -> set[str]:
    """`PythonStructure` 沒有另外帶一個「哪些 module 真的產出了
    `schemas/{module}.py`」的欄位（design_agent 內部算過一次，但沒有
    落地進 `python_structure`，見 05a 九章「不重新定義結構」），這裡
    改用機械文字比對重建同一個判斷：directory_tree 的 Schema 定義段
    固定用 `### {file_path}` 起頭（05a 三章「格式慣例」），檢查這個
    字串是否出現在 directory_tree 裡即可，不需要解析完整的 Markdown
    結構（見 06a 七章「本 module 的 schemas/{module}.py（若存在）」
    判定方式）。
    """
    return {m for m in module_names if f"### {module_index.schema_file_path(m)}" in directory_tree}


def _build_target_files(draft: _TaskDraft, modules_with_schema_file: set[str]) -> list[str]:
    """對應 06a 七章全表格：自己的 `file_path` 固定放
    `target_files[0]`；`routers`／`services` 層且該 module 真的產出
    `schemas/{module}.py` 才加入 schema 檔案；`services`／`repositories`
    層無條件加入 model 檔案。

    `utils`／`_global` 層不需要額外特殊判斷就會被這兩條規則自然排除
    （見 06a 七章「utils／_global 層」）：utils 的 `layer == "utils"`
    不落在 `("routers", "services")` 或 `("services", "repositories")`
    任一集合裡；`_global` 的 `layer == "routers"` 雖然落在 schema 檢查
    的集合裡，但 `_global` 這個 module 名稱從不會出現在
    `modules_with_schema_file`（③從不為它產生 `schemas/_global.py`），
    第二個條件天然擋下。
    """
    files = [draft.file_path]
    if draft.layer in ("routers", "services") and draft.module in modules_with_schema_file:
        files.append(module_index.schema_file_path(draft.module))
    if draft.layer in ("services", "repositories"):
        files.append(module_index.model_file_path(draft.module))
    return files


# --------------------------------------------------------------------------
# 六章、八章：depends_on 組裝、task id 全序編號、最終組裝
# --------------------------------------------------------------------------


def _build_depends_on_map(ordered_drafts: list[_TaskDraft], task_ids: list[str]) -> dict[str, list[str]]:
    """對應 06a 六章「機械規則」：同 module 內、且**同一個
    `translator_backend`** 的 task 依全序（`layer` → `function_name` →
    `class_name`）串成一條鏈，每個 task 的 `depends_on` 只放全序中緊接
    在前一名的 task id；module 邊界（跟前一名不同 module）、後端邊界
    （跟前一名不同 `translator_backend`）都沒有同 module 前一名，
    `depends_on` 留空。`_utils`／`_global` 這兩個保留 module 內部沒有
    排程順序要求（見六章），固定留空，即使它們在全序裡確實彼此相鄰。

    **後端邊界檢查對應 docs/refactor_bug_trace.md：真實環境重現過這條
    鏈跨後端造成的連坐**——序列鏈原始理由（qwen 併發數鎖死為 1，見
    `graph/scheduler.py::_backfill_missing_task_deps()` 同一段理由）只
    對 qwen 成立；repository 層（qwen）跟 services／routers 層（Claude）
    在全序裡恰好相鄰（`LAYER_RANK`：repositories < services < routers），
    若不分後端一律串鏈，一個孤立的 qwen 格式錯誤／逾時失敗會讓同 module
    底下毫無關聯、原本可以正常跑的 Claude service／router task 全部卡
    死等一個永遠不會完成的上游——這是這份 `depends_on` 真正被消費的
    地方（`graph/scheduler.py::_task_deps_satisfied()` 只認 `task_done`
    不認終態），比 `graph/scheduler.py` 那邊的同名防禦更早、更根本：
    [P] 這裡本來就無條件幫每個 module 排出一條完整全序鏈，
    `_backfill_missing_task_deps()` 只補「沒有 depends_on」的殘餘情況，
    實務上幾乎不會觸發，真正吃到全序鏈的正是這裡。

    `ordered_drafts`／`task_ids` 必須是同一個全序、逐一對應的兩個清單
    （見呼叫端 `_assemble_task_list()`）。
    """
    depends_on: dict[str, list[str]] = {task_ids[0]: []} if ordered_drafts else {}
    for i in range(1, len(ordered_drafts)):
        cur, prev = ordered_drafts[i], ordered_drafts[i - 1]
        if (
            cur.module != prev.module
            or cur.module in _RESERVED_MODULES
            or cur.translator_backend != prev.translator_backend
        ):
            depends_on[task_ids[i]] = []
        else:
            depends_on[task_ids[i]] = [task_ids[i - 1]]
    return depends_on


def _assemble_task_list(
    drafts: list[_TaskDraft], module_rank: dict[str, int], modules_with_schema_file: set[str]
) -> list[TaskSpec]:
    """對應 06a 九章「id 產生規則」：全部 task 依固定全序（見
    `module_index.full_order_key()`）依序編號 `task_{:03d}`，編號穩定、
    可重現；同一趟排序結果也拿去算七章的 `depends_on`（見
    `_build_depends_on_map()`），兩者共用同一份全序，不重複排序。

    `referenced_functions` 刻意不設值（`TaskSpec` 這個欄位仍是
    `NotRequired`，見 06a 九章「暫時保留在 TypedDict 定義裡」的說明）——
    這個舊機制的功能已被 `reference_targets`（六章）取代，[P] 不再產生
    `referenced_functions` 這份資料，07a／09a 既有程式碼讀不到這個 key
    時已有安全的預設行為（`task.get("referenced_functions", [])`）。
    """
    ordered = sorted(
        drafts,
        key=lambda d: module_index.full_order_key(module_rank, d.module, d.layer, d.function_name, d.class_name),
    )
    task_ids = [f"task_{i:03d}" for i in range(len(ordered))]
    depends_on_map = _build_depends_on_map(ordered, task_ids)

    tasks: list[TaskSpec] = []
    for task_id, draft in zip(task_ids, ordered):
        task = TaskSpec(
            id=task_id,
            module=draft.module,
            phase=draft.phase,
            translator_backend=draft.translator_backend,
            java_method_id=draft.java_method_id,
            class_name=draft.class_name,
            function_name=draft.function_name,
            description=draft.description,
            target_files=_build_target_files(draft, modules_with_schema_file),
            reference_targets=draft.reference_targets,
            context=draft.context,
            depends_on=depends_on_map[task_id],
            return_type=draft.return_type,
        )
        # 06a 六章「截斷可見化」：只有真的被截斷才設這個 key，缺席即代表
        # 「沒有這回事」，不留一個永遠是 False 的多餘欄位。
        if draft.reference_targets_truncated:
            task["reference_targets_truncated"] = True
        tasks.append(task)
    return tasks
