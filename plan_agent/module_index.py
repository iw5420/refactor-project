# plan_agent/module_index.py
"""[P] Plan Agent：`InterfaceSpec.file_path` → (module, layer) 反查、
`(file_path, class_name, function_name)` 三元組的字串編碼、六／八章共用
的固定全序，對應 06a 四章、六章「防環規則」、八章「id 產生規則」。
"""
from __future__ import annotations

from graph.state import InterfaceSpec

from plan_agent.exceptions import PlanAgentModuleLookupError

# 05a 三章「檔名規則：{module}_{layer_singular}.py」的逆運算，跟
# design_agent/layout.py 的 _LAYER_SUFFIX／schema_file_path／
# model_file_path 是同一個機械慣例——這裡不 import design_agent，因為
# 這幾行本身就是純字串規則（05a 三章訂死的格式），不是 design_agent
# 「擁有」的邏輯，兩邊各自维护這幾行，比引入跨 Agent 套件依賴更乾淨。
_LAYER_SUFFIX = {"routers": "_router", "services": "_service", "repositories": "_repository"}
# 06a 六章「防環規則」固定全序第一層：repositories < services < routers。
LAYER_RANK = {"repositories": 0, "services": 1, "routers": 2}


def classify(file_path: str, module_names: frozenset[str]) -> tuple[str, str]:
    """回傳 `(module, layer)`。對應 06a 四章「演算法」：取 `file_path`
    檔名（去掉副檔名），依 `_router`／`_service`／`_repository` 三個固定
    後綴之一去掉後綴，剩餘字串直接比對 `module_names`——檔名本來就是
    ③逐字拼接 `ModuleInfo.module` 產生，這裡是逐字反查，理論上必然精確
    命中。找不到對應後綴、或比對不到任何 module（不應發生，代表③輸出
    違反自己承諾的檔名格式）→ 直接中止，交由人工核對③的輸出（見 06a
    四章、十一章）。
    """
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


def interface_id(file_path: str, class_name: str | None, function_name: str) -> str:
    """`(file_path, class_name, function_name)` 三元組的字串編碼，供五章
    LLM 呼叫（echo back 核對用）與跨函式傳遞時當唯一識別鍵——比照 04b
    `method_id()`／05b `JavaMethodSignature.signature_key` 的既有先例
    （`"{a}::{b}::{c}"` 格式）。`class_name` 為 `None`（routers 層，見
    05a 七章）時用空字串佔位：這是刻意的實作選擇，讓五章的 output schema
    全程只有 `string` 型別，不需要為 `class_name` 額外處理 nullable
    schema（06a 五章要求「原樣抄回三元組」，字串編碼與拆開傳三個欄位
    在語意上等價，但前者讓 schema 更簡單、也順便讓 `referenced_interfaces`
    從「三元組物件陣列」簡化成「字串陣列」）。
    """
    return f"{file_path}::{class_name or ''}::{function_name}"


def file_path_of(iid: str) -> str:
    """`interface_id()` 的部分反解——只取 `file_path` 這一段，供七章
    `target_files` 組裝時把 `referenced_interfaces`（interface_id 清單）
    轉回檔案路徑。`class_name`／`function_name` 這兩段在 06a 的設計裡
    不需要反解回來（七章只需要檔案路徑），因此不提供完整反解函式。
    """
    return iid.split("::", 1)[0]


def schema_file_path(module: str) -> str:
    """對應 05a 三章「檔名規則」，跟 design_agent/layout.py 同名函式
    格式一致（見本檔開頭模組 docstring 說明為何不共用 import）。"""
    return f"app/schemas/{module}.py"


def model_file_path(module: str) -> str:
    return f"app/models/{module}.py"


def full_order_key(
    module_rank: dict[str, int], module: str, layer: str, function_name: str, class_name: str | None
) -> tuple[int, int, str, str]:
    """06a 六章「防環規則」固定全序（`repositories < services < routers`，
    同層再按 `function_name` 字母序、`class_name` 為 tie-break），
    延伸至 06a 八章「id 產生規則」跨 module 的情況——外加 `module_rank`
    （`module_list` 原始順序）當最外層排序鍵。六章本身的全序只在單一
    module 內比較（不含 `module_rank` 這一層，因為六章的防環規則只
    處理同 module 內的依賴邊），但排序演算法完全相同，這裡合併成一個
    函式，六章呼叫時傳入的 `module_rank` 對同一 module 內的所有 task
    都是同一個值，不影響同 module 內的相對順序。
    """
    return (module_rank[module], LAYER_RANK[layer], function_name, class_name or "")
