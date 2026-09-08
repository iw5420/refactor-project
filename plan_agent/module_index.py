# plan_agent/module_index.py
"""[P] Plan Agent：`InterfaceSpec.file_path` → (module, layer) 反查、
六／八章共用的固定全序，對應 06a 四章、六章、八章「id 產生規則」。
"""
from __future__ import annotations

from common.jpa_base_repository import BASE_REPOSITORY_FILE
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
BASE_REPOSITORY_MODULE_NAME = "_base_repository"
_EXCEPTION_HANDLERS_FILE = "app/core/exception_handlers.py"
_UTILS_FILE_PREFIX = "app/utils/"


def classify(file_path: str, module_names: frozenset[str]) -> tuple[str, str]:
    """回傳 `(module, layer)`。對應 06a 四章「演算法」：取 `file_path`
    檔名（去掉副檔名），依 `_router`／`_service`／`_repository` 三個固定
    後綴之一去掉後綴，剩餘字串直接比對 `module_names`——檔名本來就是
    ③逐字拼接 `ModuleInfo.module` 產生，這裡是逐字反查，理論上必然精確
    命中。找不到對應後綴、或比對不到任何 module（不應發生，代表③輸出
    違反自己承諾的檔名格式）→ 直接中止，交由人工核對③的輸出（見 06a
    四章、十一章）。

    `app/core/exception_handlers.py`（特例二）、`app/utils/` 開頭的路徑
    （特例一）在一般規則之前直接回傳固定值，見本模組頂部常數說明。
    """
    if file_path == _EXCEPTION_HANDLERS_FILE:
        return GLOBAL_MODULE_NAME, "routers"
    if file_path.startswith(_UTILS_FILE_PREFIX):
        return UTILS_MODULE_NAME, "utils"
    if file_path == BASE_REPOSITORY_FILE:
        # 對應 docs/refactor_bug_trace.md #16：Spring Data 基底介面
        # （JpaRepository/CrudRepository）在 Python 端的對應 BaseRepository
        # 泛型基底類別，見 common/jpa_base_repository.py。歸在
        # "repositories" 層（跟一般 repository 同一階，是全專案最基礎的
        # 一層）——不像 `_utils`／`_global` 需要一筆真正的 ModuleInfo 讓
        # ModuleScheduler 排程，這個檔案從不產生任何 InterfaceSpec、不會
        # 有對應的 task，這裡的分類結果只在 build_reference_targets() 的
        # BFS 判斷「python 還是 java」時用到，不需要接上排程。
        return BASE_REPOSITORY_MODULE_NAME, "repositories"

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
    """06a 六章、八章共用的固定全序：`module`（依 `module_list` 原始
    順序，`_utils`／`_global` 排最後，見 `module_rank` 組裝端）→ `layer`
    （`repositories < services < routers`，utils 排最後）→
    `function_name` 字母序 → `class_name` tie-break。
    """
    return (module_rank[module], LAYER_RANK[layer], function_name, class_name or "")
