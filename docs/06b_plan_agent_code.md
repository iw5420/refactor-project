# [P] Plan Agent 程式碼實作

> 06a 是設計面文件，本文件是實作面文件，一一對應、不重複設計理由——每節開頭註明對應 06a 章節，這裡只講怎麼落地成程式碼。對應 `00_refactor_architecture.md` 十一章文件索引的 `06b_plan_agent_code.md`。本文件與 `plan_agent/` 實際程式碼逐檔對照過，程式碼區塊即目前的真實內容。
>
> **[P] 不再呼叫 Claude API**（見 06a 五章）：`plan_agent/` 因此沒有 `llm.py`／`prompts.py`，比照 `design_agent/`／`parse_agent/` 需要這兩個檔案的理由是它們要呼叫 Claude API，[P] 現在完全不呼叫。

## 目錄

| 檔案 | 對應章節 | 說明 |
|---|---|---|
| `plan_agent/exceptions.py` | 06a 十二章 | 例外階層 |
| `plan_agent/module_index.py` | 06a 四章、七章、九章 | `file_path` → `(module, layer)` 反查、七/九章共用的固定全序 |
| `plan_agent/call_chain.py` | 06a 六章 | 呼叫鏈範圍查找：重用①呼叫圖 + ③ `java_index`，機械算出 `reference_targets` |
| `plan_agent/planning.py` | 06a 四～九章 | 純機械組裝：`phase`／`translator_backend`／`description`／`context`、`reference_targets`、`depends_on`、`target_files`、`task_list` 組裝與涵蓋率驗證 |
| `plan_agent/__init__.py` | 06a 十章 | 對外唯一入口 `run_plan_agent()` |
| `graph/nodes/plan_node.py` | 06a 十一章 | LangGraph node |

---

## 一、`exceptions.py`——例外階層

對應 06a 十二章「錯誤處理範圍」。[P] 不再呼叫 Claude API，錯誤來源只剩機械組裝本身：`file_path` 反查不出 module（四章），或涵蓋率驗證失敗（九章）。

```python
# plan_agent/exceptions.py
"""[P] Plan Agent 例外階層，對應 06a 十二章「錯誤處理範圍」。比照
design_agent/exceptions.py 的既有先例——套件結構列表都沒有列出這個
檔案，是落地時必然需要、但不屬於設計文件決策範圍的基礎設施檔案。
"""
from __future__ import annotations


class PlanAgentModuleLookupError(Exception):
    """`InterfaceSpec.file_path` 反查不出 module（06a 四章
    `module_index.classify()`）時拋出——代表③輸出違反自己承諾的
    `{module}_{layer}.py` 檔名格式（且不落在 `app/utils/`／
    `app/core/exception_handlers.py` 這兩個已知特例）。直接中止整條
    plan run，交由人工核對③的輸出，不是可以重試化解的暫時性錯誤（見
    06a 十二章）。
    """


class PlanAgentCoverageError(Exception):
    """九章涵蓋率驗證失敗時拋出——`python_structure.interfaces` 沒有被
    恰好一個 task 認領（缺漏或重複）。[P] 不再呼叫 Claude API（見 06a
    五章），涵蓋率單純由「對每個 `InterfaceSpec` 產生恰好一個 task」的
    迴圈結構保證，理論上不會觸發；這裡是最後一道 defense-in-depth，
    專門攔截 `python_structure.interfaces` 本身就存在重複三元組（③輸出
    的正確性缺陷）這種上游輸入問題，不是需要人工判斷的模糊情況（見
    06a 九章、十二章）。
    """
```

**測試**：`tests/plan_agent/test_planning.py::test_duplicate_interfaces_raises_coverage_error`（人工構造重複三元組）、`test_bad_file_path_raises_module_lookup_error`。

---

## 二、`module_index.py`——反查、固定全序（四、七、九章）

對應 06a 四章「module／layer 歸屬判定」、七章「機械規則」固定全序、九章「id 產生規則」。[P] 不再呼叫 LLM，不需要把三元組編碼成字串當 echo-back 核對用的識別鍵。

```python
# plan_agent/module_index.py
"""[P] Plan Agent：`InterfaceSpec.file_path` → (module, layer) 反查、
七／九章共用的固定全序，對應 06a 四章、七章、九章「id 產生規則」。
"""
from __future__ import annotations

from plan_agent.exceptions import PlanAgentModuleLookupError

# 05a 三章「檔名規則：{module}_{layer_singular}.py」的逆運算，跟
# design_agent/layout.py 的 _LAYER_SUFFIX／schema_file_path／
# model_file_path 是同一個機械慣例——這裡不 import design_agent，因為
# 這幾行本身就是純字串規則（05a 三章訂死的格式），不是 design_agent
# 「擁有」的邏輯，兩邊各自維護這幾行，比引入跨 Agent 套件依賴更乾淨。
_LAYER_SUFFIX = {"routers": "_router", "services": "_service", "repositories": "_repository"}

# 06a 六章／八章固定全序的層級排序：repositories／utils（Phase 1，見
# design_agent/layout.py::_PHASE_1_LAYERS）並列最基礎，< services < routers
# （Phase 2）。這個 dict 有兩個消費端，數值意義不同：
#
# 1. `full_order_key()`（本檔）：只在同一個 module 內部比較時才會用到，
#    `_utils`／`_global` 保留 module 內部都只有單一 layer，這個數值本身
#    對它們的排序不構成影響。
#
# 2. `plan_agent/call_chain.py::build_reference_targets()`：拿
#    `LAYER_RANK[callee_layer] < LAYER_RANK[current_layer]` 判斷「callee
#    是不是比呼叫者更基礎、保證已經是翻譯完成的 Python」——這裡數值
#    **會**造成實質差異。對應 docs/refactor_bug_trace.md #23 真實案例：
#    utils 曾經被排在最後（`3`，比 routers 的 `2` 還大），導致 services／
#    routers 呼叫 utils 函式時這個判斷式恆為假，reference_targets 一律
#    退回顯示 Java 原始碼，即使 utils 函式早在 Phase 1 就已經翻譯完成。
#    utils 跟 repositories 同屬 Phase 1，數值訂成跟 repositories 一樣的
#    `0`，才能讓這個判斷式正確反映「utils 比 services／routers 更早
#    完成」這個事實。
LAYER_RANK = {"repositories": 0, "utils": 0, "services": 1, "routers": 2}

# 兩個保留 module（不是真實業務 module，見 06a 四章「特例一／特例二」）：
# `_global` 承接 04a 十一章／05a 十四章的全域例外處理保留模組，固定輸出
# `app/core/exception_handlers.py`；`_utils` 承接 utils package 特例
# （05a 三章、refactor_plan.md 二章）——utils 檔案路徑刻意不帶 module
# 前綴（一個 utils class 常橫跨多個業務 module），一般規則反查不出
# module，因此比照 `_global` 的既有先例，固定回傳一個保留 module 名稱。
GLOBAL_MODULE_NAME = "_global"
UTILS_MODULE_NAME = "_utils"
_EXCEPTION_HANDLERS_FILE = "app/core/exception_handlers.py"
_UTILS_FILE_PREFIX = "app/utils/"


def classify(file_path: str, module_names: frozenset[str]) -> tuple[str, str]:
    """回傳 `(module, layer)`。對應 06a 四章「演算法」：取 `file_path`
    檔名（去掉副檔名），依 `_router`／`_service`／`_repository` 三個固定
    後綴之一去掉後綴，剩餘字串直接比對 `module_names`——檔名本來就是
    ③逐字拼接 `ModuleInfo.module` 產生，這裡是逐字反查，理論上必然精確
    命中。找不到對應後綴、或比對不到任何 module（不應發生，代表③輸出
    違反自己承諾的檔名格式）→ 直接中止，交由人工核對③的輸出（見 06a
    四章、十二章）。

    `app/core/exception_handlers.py`（特例二）、`app/utils/` 開頭的路徑
    （特例一）在一般規則之前直接回傳固定值，見本模組頂部常數說明。
    **這個函式同時是六章「呼叫鏈範圍查找」查 callee 所屬 module／layer
    的入口**，不是只有 `planning.py` 用。
    """
    if file_path == _EXCEPTION_HANDLERS_FILE:
        return GLOBAL_MODULE_NAME, "routers"
    if file_path.startswith(_UTILS_FILE_PREFIX):
        return UTILS_MODULE_NAME, "utils"

    stem = file_path.rsplit("/", 1)[-1].removesuffix(".py")
    for layer, suffix in _LAYER_SUFFIX.items():
        if stem.endswith(suffix):
            module = stem[: -len(suffix)]
            if module in module_names:
                return module, layer
            raise PlanAgentModuleLookupError(
                f"{file_path} 反查出的 module 名稱 {module!r} 不存在於 module_list（見 06a 四章）"
            )
    raise PlanAgentModuleLookupError(
        f"{file_path} 不符合 {{module}}_{{layer}}.py 檔名規則，無法反查 module／layer（見 06a 四章、05a 三章）"
    )


def schema_file_path(module: str) -> str:
    """對應 05a 三章「檔名規則」，跟 design_agent/layout.py 同名函式
    格式一致（見本檔開頭模組 docstring 說明為何不共用 import）。"""
    return f"app/schemas/{module}.py"


def model_file_path(module: str) -> str:
    return f"app/models/{module}.py"


def full_order_key(
    module_rank: dict[str, int], module: str, layer: str, function_name: str, class_name: str | None
) -> tuple[int, int, str, str]:
    """06a 七章、九章共用的固定全序：`module`（依 `module_list` 原始
    順序，`_utils`／`_global` 排最後，見 `module_rank` 組裝端）→ `layer`
    （`repositories < services < routers`，utils 排最後）→
    `function_name` 字母序 → `class_name` tie-break。
    """
    return (module_rank[module], LAYER_RANK[layer], function_name, class_name or "")
```

**測試**：`tests/plan_agent/test_module_index.py`（`classify()` 三種層級後綴、`_utils`／`_global` 兩個特例、兩種失敗情境、`full_order_key()` 排序）。

---

## 三、`call_chain.py`——呼叫鏈範圍查找（六章）

對應 06a 六章全節。純機械圖走訪，不呼叫任何 LLM，也不讀取 Java 原始碼本體——只用①的呼叫圖＋③的 `java_index`＋既有的 module 依賴排程決定每個 task 該參照哪些其他方法，只界定範圍，不含原始碼文字。

```python
# plan_agent/call_chain.py
"""[P] Plan Agent：呼叫鏈範圍查找，對應 06a 六章「reference_targets 組裝」。
純機械圖走訪，不呼叫任何 LLM，也不讀取 Java 原始碼本體——只用①的呼叫圖
（誰呼叫誰的事實）＋ ③的 java_index（Java method_id → Python 對應資訊）
＋既有的 module 依賴排程（`module_list.depends_on`）決定每個 task 該
參照哪些其他方法，只界定範圍，不含原始碼文字（見六章「為什麼是 [P]，
不是⑤」）。
"""
from __future__ import annotations

import logging
import os
from collections import deque

from graph.state import JavaIndexEntry, ModuleInfo, ReferenceTarget
from plan_agent import module_index

logger = logging.getLogger(__name__)

# 06a 六章「上限」：這是 service 同 module 內互相呼叫（唯一沒有任何機制
# 保證完成順序的殘餘情況）的保底，不是常態路徑，見六章「與既有 module
# 依賴排程的關係」。
MAX_REFERENCE_TARGETS = int(os.environ.get("PLAN_AGENT_MAX_REFERENCE_TARGETS", "20"))


def build_module_closures(module_list: list[ModuleInfo]) -> dict[str, set[str]]:
    """對每個 module 算出遞移依賴閉包（module → 這個 module 遞移依賴的
    所有 module 集合），供 `build_reference_targets()` 判斷「跨 module
    同層呼叫，能不能安全假設對方已經做完」——`ModuleScheduler` 既有機制
    保證 X 依賴的 module 會先完成，這裡只是把 `module_list.depends_on`
    展開成遞移閉包，供查表用，不是新的排程邏輯，見 06a 六章「與既有
    module 依賴排程的關係」。
    """
    deps_by_module = {m["module"]: m["depends_on"] for m in module_list}
    closures: dict[str, set[str]] = {}
    for name in deps_by_module:
        visited: set[str] = set()
        queue: deque[str] = deque(deps_by_module[name])
        while queue:
            dep = queue.popleft()
            if dep in visited:
                continue
            visited.add(dep)
            queue.extend(deps_by_module.get(dep, []))
        closures[name] = visited
    return closures


def build_reference_targets(
    *,
    seed_java_method_id: str,
    seed_module: str,
    seed_layer: str,
    call_graph: dict[str, list[str]],
    java_index: dict[str, JavaIndexEntry],
    module_names: frozenset[str],
    module_closures: dict[str, set[str]],
) -> tuple[list[ReferenceTarget], bool]:
    """對應 06a 六章「演算法」：從 `seed_java_method_id` 出發 BFS 走訪①
    的呼叫圖，回傳 `(reference_targets, truncated)`。

    每個彈出節點的直接呼叫對象查 `java_index`：
    - 查不到（entity 欄位存取、無法解析的呼叫等）→ 略過，不繼續展開。
    - `layer` 比目前節點更基礎 → `language="python"`，不繼續展開（三層
      全域關卡保證，見六章「呼叫鏈規則」）。
    - `layer` 與目前節點相同（或比目前節點更後面，保守視為同層處理）：
      - 同一個 module → `language="java"`，繼續展開（沒有任何機制保證
        完成順序，見六章「與既有 module 依賴排程的關係」）。
      - 不同 module，且目前節點的 module 依賴 callee 的 module（遞移）
        → `language="python"`，不繼續展開（既有 module 依賴排程保證）。
      - 不同 module，且沒有這層依賴關係 → `language="java"`，繼續展開。

    達到 `MAX_REFERENCE_TARGETS` 上限時立即停止整個 BFS（不是這一輪
    展開完才停），回傳 `truncated=True`；呼叫端負責把這個訊號落地成
    `TaskSpec.reference_targets_truncated`（見 06a 六章「截斷可見化」），
    這裡只記一筆警告供開發時查 log 用，警告不是這個訊號的權威落地位置。
    """
    targets: list[ReferenceTarget] = []
    truncated = False
    visited: set[str] = {seed_java_method_id}
    queue: deque[tuple[str, str, str]] = deque([(seed_java_method_id, seed_module, seed_layer)])

    while queue and not truncated:
        current_id, current_module, current_layer = queue.popleft()
        for callee_id in call_graph.get(current_id, []):
            if callee_id == current_id or callee_id in visited:
                continue  # 直接遞迴（方法呼叫自己）或已造訪過，見六章
            visited.add(callee_id)

            entry = java_index.get(callee_id)
            if entry is None:
                continue  # 查不到：entity 欄位存取等，見六章「查不到」說明

            callee_module, callee_layer = module_index.classify(entry["file_path"], module_names)

            if module_index.LAYER_RANK[callee_layer] < module_index.LAYER_RANK[current_layer]:
                language = "python"
            elif callee_module == current_module:
                language = "java"
            elif callee_module in module_closures.get(current_module, set()):
                language = "python"
            else:
                language = "java"

            if len(targets) >= MAX_REFERENCE_TARGETS:
                truncated = True
                break

            if language == "java":
                # java_index 的 value 是③投影過的 Python 側事實，不是 Java
                # 原始名稱——language="java" 的項目要給⑤解析真實 .java
                # 檔案，必須直接拆 callee_id（①的 method_id() 格式）取得
                # Java 原始座標，見 06a 六章「輸出」小節。
                java_file_path, java_class_name, java_method_name = callee_id.split("::", 2)
                targets.append(
                    ReferenceTarget(
                        file_path=java_file_path,
                        class_name=java_class_name,
                        function_name=java_method_name,
                        language=language,
                    )
                )
            else:
                targets.append(
                    ReferenceTarget(
                        file_path=entry["file_path"],
                        class_name=entry["class_name"],
                        function_name=entry["function_name"],
                        language=language,
                    )
                )

            if language == "java":
                queue.append((callee_id, callee_module, callee_layer))

    if truncated:
        logger.warning(
            "%s 的呼叫鏈參照數量達到上限（%d），已截斷（見 06a 六章「上限被觸發時：截斷可見化」）",
            seed_java_method_id, MAX_REFERENCE_TARGETS,
        )

    return targets, truncated
```

**測試**：`tests/plan_agent/test_call_chain.py`——涵蓋跨層（`python`，不展開）、同 module 同層（`java`，展開）、跨 module 同層有依賴閉包（`python`）、跨 module 同層無依賴閉包（`java`）、查不到略過、自我遞迴略過、環（`visited` 集合防無窮迴圈）、上限截斷（`monkeypatch` 降低 `MAX_REFERENCE_TARGETS` 驗證）共 10 個案例。

---

## 四、`planning.py`——四～九章核心邏輯

對應 06a 全文核心。[P] 不再依 module 平行呼叫 LLM、不需要重試佇列——對每個 `InterfaceSpec` 做一次 1:1 的機械組裝，純同步函式。

```python
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
```

**測試**：`tests/plan_agent/test_planning.py`（happy path 覆蓋 `phase`／`translator_backend`／`description`／`context`／`reference_targets`／`depends_on`／`target_files`／`id` 排序；`_utils`／`_global` 專屬規則；`phase` 缺席拋 `KeyError`；`config_field_mappings` 提示附加；涵蓋率／module 反查兩種 defense-in-depth；空 `interfaces` 邊界情況；`_build_utils_module_entry()` 補回 `_utils` 條目、沒有 utils task 時 `module_list` 原樣傳回兩種情況；`test_utils_module_list_entry_makes_utils_tasks_schedulable` 直接拿真實 `ModuleScheduler` 驗證補完後的 `module_list` 真的能讓 `_utils` task 出現在 `get_ready_tasks()`）。

**真實環境驗證**：對真實 `lang-exam-api-refactor` 專案完整跑過 ①→③→[P]（沿用既有 `specs/openapi.json`，不重跑 [A]/[B]）：78 個 `InterfaceSpec` → 78 個 task，`translator_backend` 分佈 `{claude: 67, qwen: 11}`，`java_index` 65 筆，13 個 task 有非空 `reference_targets`（語言分佈 `{python: 15, java: 4}`），`reference_targets_truncated` 的 task 為 0（上限 20 從未被逼近，符合六章「三層全域關卡＋既有 module 依賴排程已消掉絕大多數情況」的預期），`depends_on` 全數指向已存在且全序索引更小的 task id，未觸發任何例外。

---

## 五、`__init__.py`——對外唯一入口

```python
# plan_agent/__init__.py
"""[P] Plan Agent 對外唯一入口，`graph/nodes/plan_node.py` 只呼叫這裡
的函式（見 06a 九章）。"""
from __future__ import annotations

from graph.state import ModuleInfo, PythonStructure, TaskSpec
from plan_agent import planning


def run_plan_agent(
    *, module_list: list[ModuleInfo], python_structure: PythonStructure, java_project_path: str
) -> tuple[list[TaskSpec], list[ModuleInfo]]:
    """對應 06a 全文：輸出 `(task_list, module_list)`，`task_list` 直接
    對應 `RefactorState.task_list`（見 06a 九章）；`module_list` 是補回
    `_utils` 保留 module 之後的版本，直接覆寫 `RefactorState.module_list`
    （見 `planning.plan_all_modules()` docstring「第二個回傳值」）。
    `java_project_path`：六章「呼叫鏈範圍查找」需要。"""
    return planning.plan_all_modules(module_list, python_structure, java_project_path)
```

---

## 六、`graph/nodes/plan_node.py`——LangGraph node

`plan` 是平行分支 node，與 `scaffold`（④）同以 `design`（③）為前驅，只回傳自己實際更動的 `task_list`／`module_list` 兩個 key，不整包展開 state（見 06a 十一章）。`module_list` 這裡也是 [P] 實際更動的 key（見九章「補回 `_utils` 保留 module 之後的版本」）——④不讀也不寫這個 key，兩個平行 node 不會對同一個 key 各自寫入。

```python
"""
[P] Plan Agent（Claude API）
輸入：Agent ① 的模組清單 + Agent ③ 的架構設計
輸出：task_list、module_list（補回 `_utils` 保留 module 之後的版本）
見 00_refactor_architecture.md 七/[P]、06a_plan_agent_architecture.md、06b_plan_agent_code.md

平行分支 node：③（design）完成後與 scaffold（④）同時觸發（見 00 一章流程圖、
01 五章、06a 十章——[P] 不依賴④的輸出），因此只回傳自己實際更動的
key，不能用 `{**state, ...}` 整包展開，避免跟 scaffold 同一個 superstep
對同一個 key 各自寫入。`module_list` 這裡也是[P]實際更動的 key（見下方
`run_plan_agent()` 呼叫說明）——④（scaffold_node.py）不讀也不寫這個
key，兩個平行 node 不會對同一個 key 各自寫入。
"""
from __future__ import annotations

import asyncio

from graph.state import RefactorState
from plan_agent import run_plan_agent


async def run(state: RefactorState) -> dict:
    # run_plan_agent() 內部是同步函式（純機械組裝，不再呼叫 Claude API，
    # 見 06a 五章、十章「執行特性的變化」），仍丟到執行緒跑，避免這個
    # 同步呼叫卡住事件迴圈（與 design_node.py／parse_node.py 做法一致，
    # 維持同一種呼叫慣例，不因為變快了就特殊處理）。
    #
    # module_list 一併覆寫：真實環境發現 `_utils` 保留 module（utils 檔案
    # 路徑不帶 module 前綴，見 06a 四章特例一）只存在於 task.module 上，
    # ①的 module_list 從來沒有對應條目，導致 graph/scheduler.py::
    # ModuleScheduler 永遠排不到 utils 的 task（見 plan_agent/planning.py::
    # plan_all_modules() docstring 完整說明）。[P] 補回這筆缺的 ModuleInfo，
    # 這裡把補完的版本寫回 state，取代①原始版本，下游（⑤／⑦）一律讀
    # 這個補完後的版本。
    task_list, module_list = await asyncio.to_thread(
        run_plan_agent,
        module_list=state["module_list"],
        python_structure=state["python_structure"],
        java_project_path=state["java_project_path"],
    )

    return {"task_list": task_list, "module_list": module_list}
```

---

## 七、已知限制

- **`_utils`／`_global` 保留 module 尚未接上 `ModuleScheduler`／局部驗證機制**：這兩個保留 module 沒有對應的 API golden case，局部驗證該略過只做語法驗證還是需要別的觸發條件，待 09a／`graph/scheduler.py` 更新時定案（見 06a 十三章）。
- **`graph/scheduler.py` 尚未實作三層全域關卡（Phase 1 → service → controller/router）**：六章「呼叫鏈規則」的正確性直接依賴這三層真的照順序釋放，且既有的 module 依賴排程要在每一層內部都生效，不能只在「整個 module 完成」才檢查——這是 09a 落地時的必要前提，`graph/scheduler.py` 目前還是舊的兩層 Phase 1／Phase 2 關卡。
- **同一個檔案的多個 task 在 Claude API 這條較高併發路徑下的併發寫入安全性未定案**：qwen 走 `MODEL_SEMAPHORE = asyncio.Semaphore(1)` 全域序列化天然安全，Claude API 這條路徑是否需要檔案級鎖，待 09a／`graph/scheduler.py` 定案雙後端併發模型時一併處理（見 06a 七章「待銜接」）。
- **`PLAN_AGENT_MAX_REFERENCE_TARGETS`（預設 20）只用真實專案驗證過「不會被逼近」，沒有驗證過「真的被截斷時，⑤ 拿到的殘缺參照是否足夠」**：真實 `lang-exam-api-refactor` 專案這次跑下來 `reference_targets_truncated` 的 task 數為 0，這個數字本身合不合理仍待接上真實 ⑤ 呼叫、遇到真正的深呼叫鏈之後才能校準，見 06a 十三章。
- **`referenced_functions` 型別定義暫時保留、[P] 不再填值**：`translator_cli/client.py`／`graph/nodes/implement_node.py` 尚未依本輪設計更新前仍會讀取這個欄位（`task.get("referenced_functions", [])` 安全處理缺席），貿然從 `TaskSpec` 刪除型別定義會影響這兩個目前仍在正常運作的既有模組，待 07a／09a 完成更新後才是真正刪除的時機（見 06a 九章）。

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
