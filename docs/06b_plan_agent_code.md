# [P] Plan Agent 程式碼實作

> 06a 是設計面文件，本文件是實作面文件，一一對應、不重複設計理由——每節開頭註明對應 06a 章節，這裡只講怎麼落地成程式碼。對應 `00_refactor_architecture.md` 十一章文件索引的 `06b_plan_agent_code.md`。

06a 九章定義的套件結構列了 5 個檔案（`module_index.py`／`prompts.py`／`llm.py`／`planning.py`／`__init__.py`）。比照 04b／05b 補上 `exceptions.py` 的既有先例（05a／06a 十章的套件結構都沒有列出這個檔案，是落地時必然需要、但不屬於設計文件決策範圍的基礎設施檔案），本文件同樣補上這一個檔案。

## 目錄

| 檔案 | 對應章節 | 說明 |
|---|---|---|
| `plan_agent/exceptions.py` | 06a 十一章 | 例外階層 |
| `plan_agent/module_index.py` | 06a 四章、六章、八章 | `file_path` → `(module, layer)` 反查、三元組字串編碼、六/八章共用的固定全序 |
| `plan_agent/llm.py` | 06a 五章 | [P] 專屬的模型選擇 |
| `plan_agent/prompts.py` | 06a 五章 | Claude system prompt 與 output schema |
| `plan_agent/planning.py` | 06a 五、六、七、八章 | LLM 呼叫、`depends_on`／`target_files` 組裝、`task_list` 組裝與涵蓋率驗證 |
| `plan_agent/__init__.py` | 06a 九章 | 對外唯一入口 `run_plan_agent()` |
| `graph/nodes/plan_node.py` | 06a 十章 | LangGraph node（取代原本的 stub） |
| `tests/plan_agent/test_module_index.py` | 06a 四章、六章、八章 | `classify()`／`interface_id()`／`parse_interface_id()`／`full_order_key()` 的自動化測試（15 個） |
| `tests/plan_agent/test_planning.py` | 06a 五、六、七、八章 | happy path、`referenced_interfaces` 過濾、防環規則、`referenced_functions` 同檔案排除、失敗處理、涵蓋率 defense-in-depth 的自動化測試（8 個），見五章「已驗證」 |

---

## 一、`exceptions.py`——例外階層

對應 06a 十一章「錯誤處理範圍」。三種硬性失敗：`file_path` 反查不出 module（四章）、五章 LLM 呼叫重試仍失敗、八章涵蓋率驗證失敗。比照 `design_agent/exceptions.py` 的既有先例，`PlanAgentCoverageError` 明確定位成 defense-in-depth（正常路徑下五章的核對規則已經先擋過一次，這裡不該是唯一防線，但仍然保留，理由見類別 docstring）。

```python
# plan_agent/exceptions.py
"""[P] Plan Agent 例外階層，對應 06a 十一章「錯誤處理範圍」。比照
design_agent/exceptions.py 的既有先例——05a 十章／06a 九章的套件結構都
沒有列出這個檔案，是落地時必然需要、但不屬於設計文件決策範圍的基礎
設施檔案（見 05b 開頭「補上四個檔案」的同一種說明）。
"""
from __future__ import annotations


class PlanAgentModuleLookupError(Exception):
    """`InterfaceSpec.file_path` 反查不出 module（06a 四章
    `module_index.classify()`）時拋出——代表③輸出違反自己承諾的
    `{module}_{layer}.py` 檔名格式。直接中止整條 plan run，交由人工
    核對③的輸出，不是可以重試化解的暫時性錯誤（見 06a 十一章）。
    """


class PlanAgentModuleError(Exception):
    """單一 module 的五章 Claude API 呼叫，在 `planning.py` 的重試佇列
    機制（待重試清單、5 分鐘後統一重試一次，比照 05a 六章）跑完仍失敗
    時拋出，中止整條 plan run（見 06a 五章：`task_list` 是⑤唯一輸入，
    任一 module 的 task 缺失會讓涵蓋率保證失效，風險遠高於重新執行
    一次）。
    """


class PlanAgentCoverageError(Exception):
    """八章涵蓋率驗證失敗時拋出——`python_structure.interfaces` 沒有被
    恰好一個 task 認領（缺漏或重複）。這是 `planning.py` 自己組裝邏輯
    該保證但沒保證到的不變量，不是需要人工判斷的模糊情況，也不進五章
    的 LLM 重試佇列（見 06a 八章、十一章）。

    正常執行路徑下理論上不會觸發：五章對每個 module 的 LLM 回應已經
    做過「回應的三元組集合必須與輸入完全一致」的核對（缺漏視同呼叫
    失敗、多餘的直接略過，見 `planning._plan_module()`），八章這裡是
    最後一道 defense-in-depth，不是用來擋一個已知會發生的情況（比照
    06a 十二章對 05a 多載消歧的同一種定位）。
    """
```

**已驗證**：`tests/plan_agent/test_planning.py::test_duplicate_interfaces_raises_coverage_error` 用人工構造的重複三元組案例，穩定觸發 `PlanAgentCoverageError`——這是目前的權威驗證方式，不依賴任何會隨專案輸出刷新而失效的真實 fixture。

---

## 二、`module_index.py`——反查、三元組編碼、全序（四、六、八章）

對應 06a 四章「module 歸屬判定（機械）」、六章「防環規則」固定全序、八章「id 產生規則」。

**`interface_id()` 用字串編碼三元組，不是 06a 字面描述的三元組物件**：06a 五章「核對規則」要求「回應的 `(file_path, class_name, function_name)` 集合必須與輸入的 interfaces 集合完全一致」，`routers` 層的 `class_name` 恆為 `None`（05a 七章）。若逐欄位要求 LLM 回傳三元組物件，Claude Structured Outputs 的 output schema 需要處理 `class_name` 的 nullable 型別，且 `referenced_interfaces` 會變成「三元組物件陣列」而不是單純字串陣列。這裡改用 `"{file_path}::{class_name or ''}::{function_name}"` 把三元組編碼成單一字串——語意上與 06a 描述的三元組完全等價（`class_name` 為 `None` 時用空字串佔位，不會跟真實 Java class 名稱衝突，因為 Java class 名稱不可能是空字串），但讓 schema 全程只用 `string`／`array of string`，不需要碰 nullable。這是實作層級的簡化，不是對 06a 設計的偏離——比照 04b `method_id()`／05b `JavaMethodSignature.signature_key` 的既有先例（同樣是 `"{a}::{b}::{c}"` 格式的三元組編碼）。

```python
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

# `_global` 保留模組（見 parse_agent/summarize.py／design_agent/design.py
# 同一組字面值，這裡比照本檔開頭「各自維護、不 import 其他 Agent 套件」
# 的既有原則自己重複定義，不新增跨套件依賴）固定輸出
# `app/core/exception_handlers.py`，不符合 `{module}_{layer}.py` 命名
# 慣例——真實端對端測試才發現這個缺口（見 docs/09b_bug_trace.md
# #11/#12）：一般規則找不到任何固定後綴，會直接中止整個 [P] 呼叫。
# 這裡在一般規則之前特殊處理；layer 歸類為 "routers"（rank 最高、不影響
# 任何排序正確性——`_global` 目前只有一個 task、模組內無 depends_on，
# 選哪個 layer 對防環規則沒有實質差異，選 "routers" 只是語意上最接近：
# 這批函式跟 routers 層一樣是 class_name=None 的自由函式、在 main.py
# 被機械註冊，不是真正需要區分 service/repository 順序的情況）。
_GLOBAL_MODULE_NAME = "_global"
_EXCEPTION_HANDLERS_FILE = "app/core/exception_handlers.py"


def classify(file_path: str, module_names: frozenset[str]) -> tuple[str, str]:
    """回傳 `(module, layer)`。對應 06a 四章「演算法」：取 `file_path`
    檔名（去掉副檔名），依 `_router`／`_service`／`_repository` 三個固定
    後綴之一去掉後綴，剩餘字串直接比對 `module_names`——檔名本來就是
    ③逐字拼接 `ModuleInfo.module` 產生，這裡是逐字反查，理論上必然精確
    命中。找不到對應後綴、或比對不到任何 module（不應發生，代表③輸出
    違反自己承諾的檔名格式）→ 直接中止，交由人工核對③的輸出（見 06a
    四章、十一章）。

    `app/core/exception_handlers.py`（見上方）是唯一的例外，在一般規則
    之前直接回傳固定值。
    """
    if file_path == _EXCEPTION_HANDLERS_FILE:
        return _GLOBAL_MODULE_NAME, "routers"

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
    轉回檔案路徑，決定要讀哪些檔案。
    """
    return iid.split("::", 1)[0]


def parse_interface_id(iid: str) -> tuple[str, str | None, str]:
    """`interface_id()` 的完整反解，回傳 `(file_path, class_name,
    function_name)`。對應 06a 七章「`referenced_functions`：函式層級
    抽取」：`_build_referenced_functions()` 需要完整三元組，才能讓
    `translator_cli` 精準抽出被引用到的那一個函式，不是整份檔案（見
    `docs/09b_bug_trace.md` #37 根因——`file_path_of()` 只反解檔案路徑，
    正是造成 context 膨脹的直接原因）。`class_name` 編碼時的空字串佔位
    （routers 層，見 05a 七章）在這裡還原成 `None`。
    """
    file_path, class_name, function_name = iid.split("::", 2)
    return file_path, (class_name or None), function_name


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
```

**已驗證**：`tests/plan_agent/test_module_index.py` 的 `test_classify_*` 系列用人工構造案例覆蓋三種層級後綴、兩種失敗情境（找不到對應後綴／module 名稱不存在），是穩定的權威驗證，不受真實 fixture 內容變動影響。另外也對照過真實 `lang-exam-api-refactor` 專案的 `tests/design_agent/fixtures/real_python_structure.json` 跑過 `classify()`，全數正確反查回 `(module, layer)`，沒有觸發過 `PlanAgentModuleLookupError`，間接驗證③實際輸出確實遵守 `{module}_{layer}.py` 檔名格式承諾。`full_order_key()` 用 `tests/plan_agent/test_module_index.py` 的排序測試驗證過，另外也在五章 dry-run 驗證過保留/捨棄兩種方向的排序結果正確。

---

## 三、`llm.py`——模型選擇與輸出長度上限

對應 06a 五章。模型選擇跟 `design_agent/llm.py`、`parse_agent/llm.py` 同一種寫法，只換環境變數名稱；`max_tokens` 則是 [P] 專屬的決策，其他三個 Agent 都沒有覆寫過 `common/llm_client.DEFAULT_MAX_TOKENS`（4096）——[P] 每個 task 都帶完整業務描述／context／依賴清單，輸出密度遠高於其他 Agent 的分類型輸出，對真實 `lang-exam-api-refactor` 專案最大的 module（`exam`，27 個 interfaces）實測跑過，`4096` 會在 JSON 字串中間被截斷，見七章「已驗證」。

```python
# plan_agent/llm.py
"""[P] Plan Agent 專屬的 Claude API 模型選擇與輸出長度上限。實際呼叫
邏輯（client 初始化、Structured Outputs、log_usage() 整合、錯誤處理）在
common/llm_client.py，所有需要呼叫 Claude API 的 Agent 共用同一份（見
00 六章）。這個檔案只負責 [P] 自己的決策：用哪個模型、`max_tokens` 要
多少，不跟其他 Agent 共用同一個環境變數或同一個數字。

`PLAN_AGENT_MAX_TOKENS` 不用 `common/llm_client.DEFAULT_MAX_TOKENS`
（4096）：[P] 每個 task 都帶完整業務描述／context／依賴清單，輸出密度
遠高於其他 Agent 的分類型輸出。實測對真實 lang-exam-api-refactor 專案
最大的 module（27 個 interfaces）跑過，完整回應約需 5,591 token（用
Anthropic `count_tokens()` 對截斷前的部分回應實測換算，不是猜的），
4096 會被截斷成不合法的 JSON。8192 約為實測值的 1.5 倍，留有餘裕應付
同一 module 不同次呼叫的回應長度波動，以及比這次目標專案更大的 module。
"""
from __future__ import annotations

import os

from common.llm_client import DEFAULT_MODEL_FALLBACK

DEFAULT_MODEL = os.environ.get("PLAN_AGENT_MODEL", DEFAULT_MODEL_FALLBACK)
PLAN_AGENT_MAX_TOKENS = int(os.environ.get("PLAN_AGENT_MAX_TOKENS", "8192"))
```

---

## 四、`prompts.py`——LLM 契約（五章）

對應 06a 五章「單一 module 呼叫內容」全表格。輸出 schema 用二章說明的 `interface_id` 字串編碼取代三元組物件，`referenced_interfaces` 因此是單純的字串陣列。

```python
# plan_agent/prompts.py
"""[P] Plan Agent 用到的 Claude API system prompt 與對應 output schema，
對應 06a 五章「LLM 設計階段」。schema 跟 prompt 放同一個檔案的理由，
比照 design_agent/prompts.py 的說明——調整其中一個時另一個就在旁邊，
不容易顧此失彼。

**輸出契約用 `interface_id` 字串代替三元組物件**：06a 五章要求「回應的
`(file_path, class_name, function_name)` 集合必須與輸入的 interfaces
集合完全一致」，`referenced_interfaces` 也是同一種三元組。但 `routers`
層的 `class_name` 恆為 `None`（05a 七章），若逐欄位要求 LLM 回傳，
output schema 需要處理 `class_name` 的 nullable 型別，且
`referenced_interfaces` 會變成「三元組物件陣列」而不是單純字串陣列，
複雜度沒有必要地增加。`plan_agent/module_index.py` 的 `interface_id()`
把三元組編碼成單一字串（`"file_path::class_name::function_name"`，
`class_name` 為 `None` 時用空字串），語意上完全等價，但讓這裡的 schema
全程只用 `string`／`array of string`，不需要碰 nullable。
"""
from __future__ import annotations

PLAN_SYSTEM_PROMPT = """\
你是協助把 Java 方法的業務邏輯描述，銜接到已經設計好的 Python 函式
介面（interface）的助手。你會收到一個模組（module）的資料，任務是為
這個模組的每一個 Python interface，整合出對應的 Java 業務邏輯描述、
補充 context，並判斷它在業務邏輯上會呼叫到哪些其他 interface。

輸入包含：
1. module：模組名稱與業務摘要
2. java_methods：這個模組所有 Java 方法的清單，每筆含 class_name／
   java_method／description／complexity——這是原始業務邏輯的來源
3. interfaces：這個模組所有 Python interface 的清單，每筆含
   interface_id（唯一識別碼，原樣抄回即可，不要更動任何字元）、
   file_path、class_name（routers 層為 null，代表是自由函式、不屬於
   任何類別）、function_name、params、return_type——這是你要逐一填寫
   描述的對象
4. upstream_interfaces：這個模組依賴的上游模組已經產出的 interface
   清單（同樣格式，含 interface_id），純參考用，讓你知道有哪些函式
   已經存在，可以被 referenced_interfaces 引用

你的任務：對 interfaces 陣列裡**每一個**元素，透過函式名稱轉寫關係
（Java camelCase 方法名跟 Python snake_case 函式名的對應）、參數個數、
業務描述語意，找出它對應的 Java 方法（interfaces 與 java_methods 之間
沒有現成的對照表，需要你自己配對），輸出：

1. interface_id：原樣抄回輸入的 interface_id，一個字元都不要更動
2. description：整合對應 Java 方法業務邏輯後、給後續實作者的任務
   描述——具體描述這個函式該做什麼、遵循什麼業務規則，不要只是重複
   函式簽名本身
3. context：補充依賴關係／邊界條件文字，沒有特別要補充時給空字串
   （不要省略這個欄位）
4. referenced_interfaces：這個函式業務邏輯上會呼叫到的其他 interface
   的 interface_id 清單——只能引用 interfaces 或 upstream_interfaces
   這兩份清單裡出現過的 interface_id，不要虛構或猜測不存在的
   interface_id，也不要引用自己。沒有業務關聯時給空陣列。

**interfaces 陣列裡的每一個元素都必須在你的輸出裡出現恰好一次，不能
省略、不能重複、也不要為 interfaces／upstream_interfaces 以外的東西
生成項目。**

不要輸出任何其他文字，不要用 markdown code fence 包裹。
"""

PLAN_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "interface_id": {"type": "string"},
                    "description": {"type": "string"},
                    "context": {"type": "string"},
                    "referenced_interfaces": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["interface_id", "description", "context", "referenced_interfaces"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tasks"],
    "additionalProperties": False,
}
```

**待驗證**：這份 prompt 的實際判斷品質（LLM 能否穩定從 `function_name`／描述語意配對回正確的 Java method）尚未接上真實 Claude API 呼叫校準，只驗證過 schema／核對邏輯本身能正確運作（見七章「已驗證」用 mock 呼叫做的 dry-run）——延續 06a 十二章「待接上真實③輸出後校準」的既有待辦，見十章「已知限制」。

---

## 五、`planning.py`——五、六、七、八章核心邏輯

對應 06a 全文核心：五章 LLM 呼叫與重試佇列（不需拓樸分波，跟 `design_agent/design.py` 的關鍵差異見 06a 五章）、六章 `depends_on` 組裝（固定全序防環）、七章 `target_files` 組裝（含跨 module schemas／models）、八章 `task_list` 組裝與涵蓋率驗證。

**`referenced_functions`：函式層級抽取**（對應 06a 七章同名小節、`docs/09b_bug_trace.md` #37 根因）：下方 `_build_target_files()` 只決定「要讀哪些檔案」，檔案內容怎麼帶是另一件事——`_build_referenced_functions()` 另外算出每個因引用而拉進來的檔案裡，具體是哪個函式被引用到，交給 `translator_cli` 只抽取那幾個函式（`python_adapter.extract_specific_functions()`），細節見七章。

**涵蓋率驗證提前到組裝前執行**：06a 八章把涵蓋率驗證放在「輸出格式與 State 對應」一節、字面順序上像是最後一步，但這裡選擇在五章 LLM 呼叫全部完成、六／七／八章機械組裝**之前**就先驗證——理由：後面的排序／`depends_on`／`target_files` 組裝都假設 `drafts_by_id` 跟 `python_structure.interfaces` 是同一個集合，提前驗證失敗可以更快中止、錯誤訊息也更單純（不會混雜組裝階段可能產生的其他例外），不影響驗證邏輯本身。

```python
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
from graph.state import InterfaceSpec, ModuleInfo, PythonStructure, ReferencedFunctionRef, TaskSpec
from plan_agent import module_index
from plan_agent.exceptions import PlanAgentCoverageError, PlanAgentModuleError
from plan_agent.llm import DEFAULT_MODEL, PLAN_AGENT_MAX_TOKENS
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
        system_prompt=PLAN_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema=PLAN_OUTPUT_SCHEMA,
        model=DEFAULT_MODEL,
        max_tokens=PLAN_AGENT_MAX_TOKENS,
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


def _build_referenced_functions(draft: _TaskDraft) -> list[ReferencedFunctionRef]:
    """對應 06a 七章「`referenced_functions`：函式層級抽取」：把
    `draft.referenced_interfaces` 逐一反解成 `(file_path, class_name,
    function_name)`，供 `translator_cli` 只抽取被引用到的那個函式，不是
    整份檔案帶入（見 `docs/09b_bug_trace.md` #37 根因）。

    **同檔案引用（`file_path == draft.file_path`）刻意排除**：這種情況
    指向的是這個 task 自己的檔案，`target_files[0]` 本來就整份帶入（見
    七章表格第一列），不需要、也不能對它做函式層級抽取——`_read_context_files()`
    若對 `context_files[0]`（＝`target_files[0]`）套用這份清單，會把
    「這次要填的目標函式本身」也一併篩掉（因為目標函式不在
    `referenced_interfaces` 裡，那是「這個函式引用別人」的清單，不包含
    自己），等於損毀目標檔案的 context。
    """
    result: list[ReferencedFunctionRef] = []
    for ref_id in draft.referenced_interfaces:
        file_path, class_name, function_name = module_index.parse_interface_id(ref_id)
        if file_path == draft.file_path:
            continue
        result.append({"file_path": file_path, "class_name": class_name, "function_name": function_name})
    return result


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
                class_name=draft.class_name,
                function_name=draft.function_name,
                description=draft.description,
                target_files=_build_target_files(draft, module_by_id, layer_by_id, modules_with_schema_file),
                context=draft.context,
                depends_on=_build_depends_on(draft, iid, module_by_id, order_index),
                referenced_functions=_build_referenced_functions(draft),
            )
        )
    return tasks
```

**`class_name`／`function_name` 落地進 `TaskSpec`（訂正 06a 八章原文）**：`_TaskDraft` 從五章 `_plan_module()` 開始就一路帶著這兩個欄位（供 `module_index.interface_id()` 編碼、`_build_target_files()`／`_build_depends_on()` 查表用），只有這裡組裝最終 `TaskSpec` 時原本沒有寫出來。06a 八章原訂「`class_name`／`function_name` 不落地進最終輸出」，理由是涵蓋率驗證只需要在組裝階段核對一次，不需要留到執行期——但沒有預見到 translator-cli 的 `fill_function()`（見 `07a_translator_cli_architecture.md` 五章）執行期同樣需要這兩個值：同一個檔案（尤其三層檔案）正常有多個函式對應多個 task，光靠 `target_files[0]` 這個路徑無法定位要填哪一個。這是 07a 設計階段發現的缺口，修法是最小的：`_assemble_task_list()` 補這兩行，`_TaskDraft`／`module_index.py`／`_build_target_files`／`_build_depends_on`／涵蓋率驗證／`prompts.py` 的 schema 全部不需要改。

**已驗證**：

`tests/plan_agent/test_module_index.py`（15 個）／`test_planning.py`（8 個）是現在的權威、可重複執行的自動化測試，全程 monkeypatch `call_claude_for_json`，不呼叫真實 API，涵蓋：happy path（`depends_on`／`target_files`／`referenced_functions` 組裝、`id` 固定全序編號）、`referenced_interfaces` 的非法引用／自我引用過濾、`referenced_functions` 同檔案引用排除、六章防環規則（反向／同層邊捨棄）、五章失敗處理（LLM 回應遺漏 interface → 重試 → 仍失敗中止）、涵蓋率 defense-in-depth（人工構造重複三元組）、四章 module 反查失敗。

**接上真實 Claude API 呼叫的驗證紀錄**：

**2026-08-08，`real_module_list.json`（6 個 module）＋ `real_python_structure.json`（72 個 `InterfaceSpec`）**：6 個 module 平行呼叫 `claude-sonnet-4-6`（`PLAN_AGENT_MODEL` 未設，退回 `DEFAULT_MODEL_FALLBACK`），65.6 秒內全數成功；72 個 task 與 72 個 interface **精確** 1:1 對應（用完整三元組核對，不只是數量）；所有 task 都帶有正確的 `class_name`／`function_name`，其中 8 個檔案被超過一個 task 共用（最多 `common_service.py` 22 個，橫跨 6 個不同 class）——證實這不是理論上的邊界情況，是這個真實專案的常態；抽查依賴鏈（`task_049` `ExamService.get_candidate_by_card` 正確 `depends_on` → `task_033` `ExamRepository.find_by_card`）與業務描述品質皆正確。花費：36,304 input ＋ 13,172 output tokens，約 US$0.31（`claude-sonnet-4-6` 費率 $3/$15 每百萬 token）。**這次驗證的範圍是「機制跑得通、結構正確」，不含**逐筆核對 LLM 判斷的 Java↔Python 配對與 `referenced_interfaces` 語意是否準確——那是十二章仍列為待辦的 prompt 品質校準，屬於另一件事。

---

## 六、`__init__.py`——對外唯一入口

對應 06a 九章。

```python
# plan_agent/__init__.py
"""[P] Plan Agent 對外唯一入口，`graph/nodes/plan_node.py` 只呼叫這裡
的函式（見 06a 九章）。"""
from __future__ import annotations

from graph.state import ModuleInfo, PythonStructure, TaskSpec
from plan_agent import planning


def run_plan_agent(*, module_list: list[ModuleInfo], python_structure: PythonStructure) -> list[TaskSpec]:
    """對應 06a 全文：輸出 `task_list`，直接對應 `RefactorState.task_list`
    （見 06a 八章）。"""
    return planning.plan_all_modules(module_list, python_structure)
```

---

## 七、`graph/nodes/plan_node.py`——LangGraph node 實作

對應 06a 十章「與 LangGraph 整合」。取代原本回傳固定 stub task 的版本，做法比照 `design_node.py`——同步阻塞呼叫丟到執行緒跑，避免卡住事件迴圈；只回傳自己實際更動的 `task_list` key，不整包展開 state（`plan` 是平行分支 node，與 `scaffold` 同以 `design` 為前驅，見 06a 十章）。

```python
"""
[P] Plan Agent（Claude API）
輸入：Agent ① 的模組清單 + Agent ③ 的架構設計
輸出：task_list
見 00_refactor_architecture.md 七/[P]、06a_plan_agent_architecture.md、06b_plan_agent_code.md

平行分支 node：③（design）完成後與 scaffold（④）同時觸發（見 00 一章流程圖、
01 五章、06a 十章——[P] 不依賴④的輸出），因此只回傳自己實際更動的
key，不能用 `{**state, ...}` 整包展開，避免跟 scaffold 同一個 superstep
對同一個 key 各自寫入。
"""
from __future__ import annotations

import asyncio

from graph.state import RefactorState
from plan_agent import run_plan_agent


async def run(state: RefactorState) -> dict:
    # run_plan_agent() 內部是同步阻塞呼叫（多次 Claude API 呼叫，且五章
    # 重試佇列的 5 分鐘等待，見 06a 五章），丟到執行緒跑，避免卡住事件
    # 迴圈（與 design_node.py／parse_node.py 做法一致）。
    task_list = await asyncio.to_thread(
        run_plan_agent,
        module_list=state["module_list"],
        python_structure=state["python_structure"],
    )

    return {"task_list": task_list}
```

---

## 八、已知限制與待驗證事項

- **已接上真實 Claude API 呼叫（含完整 04a→05a→06a 串接），但只驗證「機制」不驗證「判斷品質」**：完整驗證紀錄見五章「已驗證」（2026-08-08，對照 07a 新增 `class_name`／`function_name` 後的程式碼跑的）。**這次驗證只證明「呼叫得通、輸出格式對、容量上限夠」，沒有逐筆核對 `prompts.py` 的 system prompt 實際判斷品質**（`referenced_interfaces` 的業務關聯判斷整體準確率）——抽查過的少數 task（如 `SchoolRepository.find_by_grade_and_local`、`routers` 層 `class_name=None` 的 `ExamController.search`／`saveAnswer`、`ExamService.get_candidate_by_card`）配對正確，但沒有逐筆核對，延續 06a 十二章「待接上真實③輸出後校準」的既有待辦，範圍縮小為「品質校準」而非「有沒有跑過、容量夠不夠」。
- [x] ~~驗證涵蓋率 defense-in-depth 用的 fixture 已刷新，`PlanAgentCoverageError` 的多載重複測試案例目前沒有天然來源~~——**已解決**：`tests/plan_agent/test_planning.py::test_duplicate_interfaces_raises_coverage_error` 改用人工構造的重複三元組案例，不再依賴真實 fixture 是否剛好含 bug，見一章「已驗證」。
- **`referenced_functions` 函式層級抽取——已實作，待真實環境重跑驗證**：`docs/09b_bug_trace.md` #37 根因的修正，`module_index.parse_interface_id()`／`_build_referenced_functions()`／`translator_cli.python_adapter.extract_specific_functions()` 均已完成，單元測試與真實失敗案例的 prompt 資料重放皆已驗證（15334 bytes → 7327 bytes，見七章）。尚未跑過真實環境端對端重跑確認能讓 #37 記錄的 5 支函式實際成功，見 06a 十二章。
- **`interface_id()` 的字串編碼是實作層級的簡化，未在 06a 明文出現**：06a 五章字面描述「回應的三元組」，這裡改用單一字串編碼（理由見二、四章）。語意等價，但若未來有人依照 06a 字面描述另外實作一份消費 `plan_agent` 內部資料的程式碼，需要知道實際的識別鍵格式是這個編碼字串，不是三個獨立欄位。
- **`_TaskDraft.referenced_interfaces` 的「過濾非法引用」只做一次，發生在五章 LLM 回應剛收到時**：06a 五章原文只說「六／七章組裝時一律過濾並記警告」，沒有明講是「組裝時各自過濾」還是「統一先過濾一次、組裝時直接消費乾淨的結果」。這裡選擇後者（`_plan_module()` 內一次過濾），單一根據點，六、七章不需要重複同一段過濾邏輯——語意上滿足 06a 的要求（六／七章確實用的是已過濾的結果），但實作路徑跟字面描述「六／七章組裝時」不完全一樣，記錄在此避免日後對照時誤以為是漏做。

---

## `config_field_mappings` 折進 `task.context`（對應 06a 十一之一章、`docs/09b_bug_trace.md` #45）

`plan_all_modules()` 讀 `python_structure.get("config_field_mappings", {})`（`NotRequired` 欄位，沒有 `@Value` 欄位的專案完全不會有這個 key），往下傳給 `_assemble_task_list()`，新增 `_augment_context_with_config_hint(context, file_path, config_field_mappings) -> str`：`config_field_mappings.get(file_path)` 查無對應項目時原樣回傳 `context`；有對應項目時把 `{java_field: python_reference}` 逐一組成一段固定格式的中文提示，附加到 `context` 尾端（`context` 本身若為空字串，直接用提示文字，不留多餘的空白行）。`TaskSpec` 的 `context=draft.context` 這一行改成 `context=_augment_context_with_config_hint(draft.context, draft.file_path, config_field_mappings)`，其餘八章組裝邏輯不變。

比對 key 用的是 `draft.file_path`（即 `TaskSpec.target_files[0]`，Python 檔案路徑），跟 `config_field_mappings` 的 key（`design_agent.global_infra.scan_value_injected_fields()` 算出的 `python_file_path`）是同一個路徑空間——這是實作時特別留意的一點：`ValueInjectedField` 原本另外記了一份 Java 原始碼的 `file_path`（純記錄用），兩者容易混淆，命名上刻意用 `python_file_path` 這個更明確的欄位名稱區分，避免 [P] 這一側不小心拿 Java 路徑去比對 Python 路徑（兩者字串格式完全不同，比對永遠落空，且不會拋錯——`dict.get()` 查無此 key 只是安靜地不附加提示，很容易在真實環境驗證前都不會被發現）。

單元測試見 `tests/plan_agent/test_planning.py::test_config_field_mappings_appended_to_matching_task_context`／`test_no_config_field_mappings_leaves_context_untouched`。真實環境驗證見 06a 十一之一章。

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
