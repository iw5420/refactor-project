# translator_cli/scaffold.py
"""④ 骨架生成模式，對應 07a 四章全節。同步、確定性，不呼叫本地模型
（見四章「決策：不呼叫本地模型，純機械產生」）——`build_files()` 全程
在記憶體中組裝並驗證，`write_files()` 才真正落地寫入磁碟，兩者分開
是為了讓 `client.generate_scaffold()` 能在「全部驗證通過後才一次性
寫入」（四章「寫入時機」）。
"""
from __future__ import annotations

import ast
import builtins
import logging
import re
from pathlib import Path

from common.jpa_base_repository import BASE_REPOSITORY_CLASS, BASE_REPOSITORY_FILE
from translator_cli.exceptions import TranslatorCliAssemblyError
from translator_cli.python_adapter import PythonAdapter
from translator_cli.types import InterfaceSpec, PythonStructure

logger = logging.getLogger(__name__)

# `common/jpa_base_repository.py::BASE_REPOSITORY_FILE` 的 import 路徑形式，
# 對應 docs/refactor_bug_trace.md #10／#16：repositories 層的類別若繼承
# Spring Data 基底介面（`InterfaceSpec.jpa_base_entity` 非 None），渲染成
# `class X(BaseRepository[Entity]):` 時固定需要這行 import，跟 routers 層
# 固定需要 `from fastapi import APIRouter` 是同一種「已知固定樣板」處理。
_BASE_REPOSITORY_MODULE = BASE_REPOSITORY_FILE.removesuffix(".py").replace("/", ".")
_BASE_REPOSITORY_IMPORT = f"from {_BASE_REPOSITORY_MODULE} import {BASE_REPOSITORY_CLASS}"

# 05a 三章「全域基礎設施檔案」：內容就是完整檔案內容，直接寫入，不需要
# 像 Schema 定義段那樣合併（見四章「輸入解析：directory_tree 三段格式」
# 「基礎設施段」）。
_INFRA_FILES = {"app/main.py", "app/core/database.py"}

# 05a 三章「檔名規則」的層級判定，`file_path` 開頭字串即可判斷——跟
# `plan_agent/module_index.py` 的 `_LAYER_SUFFIX` 是同一種各自维护機械
# 慣例的既有先例（見該檔案 docstring），這裡不 import design_agent。
# `app/utils/`：08a 二章「Utils 檔案結構——已定案」，一個 Java class 對
# 一個 Python 檔案、不歸屬任何 module，渲染規則跟 routers／global_advice
# 同一種「class_name=None 自由函式」形狀（見下方 _FREE_FUNCTION_LAYERS）。
_LAYER_PREFIX = {
    "app/routers/": "routers",
    "app/services/": "services",
    "app/repositories/": "repositories",
    "app/utils/": "utils",
}

# `_global` 保留模組固定輸出的全域例外處理檔案（見 design_agent/design.py
# `_design_global_advice_module()`、docs/09b_bug_trace.md #11/#12）——
# 單一固定檔案，不是像三層那樣的目錄前綴，因此用精確比對、獨立於
# `_LAYER_PREFIX` 之外判斷。渲染規則比照 routers 層（`class_name=None`
# 自由函式），但**不**包 `APIRouter` 樣板（這不是真正的路由檔案，見
# `_assemble_file_text()`）——這是真實端對端測試才發現的缺口：
# `_render_interface_files()` 原本假設 interfaces 的 file_path 只會落在
# 三層目錄底下，全域例外處理的檔案會被誤判成 unknown layer 整個跳過。
_GLOBAL_ADVICE_FILE = "app/core/exception_handlers.py"
_GLOBAL_ADVICE_LAYER = "global_advice"

# `class_name=None` 自由函式、不包 APIRouter 樣板的層級集合：routers 層
# 本身（一般路由函式）、global_advice（上方）、utils（同上方 _LAYER_PREFIX
# 註解）——三者渲染形狀相同，統一判斷、避免三處各自重複寫同一份 tuple。
_FREE_FUNCTION_LAYERS = frozenset({"routers", _GLOBAL_ADVICE_LAYER, "utils"})

# 四章「已知關鍵字表」，子字串比對，一個檔案內出現多次只加一次。
_KNOWN_KEYWORD_IMPORTS: list[tuple[str, str]] = [
    ("Session", "from sqlalchemy.orm import Session"),
    ("Depends(", "from fastapi import Depends"),
    ("get_db", "from app.core.database import get_db"),
    ("Request", "from fastapi import Request"),
    # ResponseEntity<T> 型別對應（見 common/java_type_mapping.py
    # map_java_type()）與全域例外處理（見 design_agent/design.py
    # _design_global_advice_module()）都用這個做為 return_type，
    # 對應 docs/09b_bug_trace.md #11/#12/#30。
    ("Response", "from fastapi import Response"),
    ("UploadFile", "from fastapi import UploadFile"),
    # 對應 docs/refactor_bug_trace.md #20：MultipartFile 端點的其餘
    # @RequestParam 欄位補 Form(...)（見 design_agent/type_mapping.py
    # ::_classify_params() 新增分支）。
    ("Form(", "from fastapi import Form"),
    ("HTTPException", "from fastapi import HTTPException"),
    ("Decimal", "from decimal import Decimal"),
    # 08a_scaffold_agent_architecture.md 五章新增：common/java_type_mapping.py
    # 的 map_java_type() 補上 java.time／java.util.UUID 對應後，③的方法簽名
    # （非 API 邊界的內部方法，走 map_java_type() 這條路徑）也可能產生這幾個
    # 型別字串，原表沒涵蓋會重演 07a 五章「填空模式：本體 import 解析」修正
    # 前的缺 import 情況。"date"／"datetime"／"time" 三個關鍵字都以英數字元
    # 結尾，走 _keyword_hits() 的 \b 單字邊界比對——"datetime" 內含 "date"／
    # "time" 兩個子字串，但邊界比對不會誤觸發（"date" 與後面的 "time" 之間
    # 沒有字元邊界），三者互不誤判。
    ("date", "from datetime import date"),
    ("datetime", "from datetime import datetime"),
    ("time", "from datetime import time"),
    ("UUID", "from uuid import UUID"),
    ("Callable[", "from typing import Callable"),
    # `Specification<T>` 型別對應（見 common/java_type_mapping.py
    # map_java_type()），對應 docs/refactor_bug_trace.md #14。
    ("ColumnElement", "from sqlalchemy.sql.elements import ColumnElement"),
]

# 05a 三章「格式慣例」固定格式：`### {file_path}` 標題 + python fenced
# code block，DOTALL 讓 `.` 能跨行比對整個 code block 內容。
_CODE_BLOCK_RE = re.compile(r"### (?P<path>\S+)\n```python\n(?P<code>.*?)```", re.DOTALL)


def _normalize_type(raw_type: str) -> str:
    """對應四章「型別字串正規化」。語法合法性已由
    `design_agent/type_mapping.map_java_type()` 保證（見 05a 五章），
    這裡只是 defense-in-depth：對 ③ 輸出而言恆為 no-op，只防禦其他
    呼叫路徑萬一繞過 ③、或未來 `InterfaceSpec` 出現殘餘寫法。
    """
    return raw_type.replace("<", "[").replace(">", "]")


def _extract_code_blocks(directory_tree: str) -> list[tuple[str, str]]:
    """對應四章「輸入解析：directory_tree 三段格式」第二、三段：逐段
    擷取 `(file_path, code_block_text)`。第一段（目錄結構段）純文字
    樹狀圖不在這裡解析——本實作把「mkdir -p」下放到 `write_files()`
    對每個實際要寫入的檔案各自 `parent.mkdir(parents=True,
    exist_ok=True)`，跟先掃描第一段、預先建好所有子目錄的效果等價，
    因此不需要額外解析第一段。
    """
    return [(m.group("path"), m.group("code")) for m in _CODE_BLOCK_RE.finditer(directory_tree)]


def _split_block_imports(
    block: str,
) -> tuple[bool, list[tuple[str, str | None]], dict[str, list[str]], str]:
    """用 AST 定位一個 block 裡的 import 陳述式所在行號範圍，而不是逐行
    正則比對——正則版本漏抓 `import xxx` 裸匯入、無法處理 PEP 8 多行
    `from typing import (\\n    List,\\n)` 括號跨行寫法。用 AST 找出
    `ast.Import`／`ast.ImportFrom` 節點的 `lineno`／`end_lineno`，兩種
    形式與跨行寫法都能正確定位。

    **body 保留原始逐行文字，刻意不整段丟進 `ast.unparse()` 重新序列化**：
    `render_class_placeholder_section()`（05a 三章）產出的機械註解
    （Java 原始檔路徑、建構子簽名）是這個佔位機制存在的核心資訊，
    `ast.unparse()` 不保留註解，會把這些資訊整個丟掉——因此這裡只用
    AST 定位 import 陳述式的行號範圍，從原始文字裡精準挖掉這幾行，
    其餘文字（含註解、原始排版）逐字保留，不重新格式化。

    回傳 `(是否含 from __future__ import annotations,
    [(module, alias 或 None), ...] 裸 import 清單,
    {module: [name（含 "X as Y" 別名寫法）, ...]} from-import 清單,
    去掉 import 陳述式後的原始 body 文字)`。
    """
    tree = ast.parse(block)
    lines = block.splitlines()
    future_seen = False
    plain_imports: list[tuple[str, str | None]] = []
    from_imports: dict[str, list[str]] = {}
    excluded_lines: set[int] = set()

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            future_seen = True
            excluded_lines.update(range(node.lineno - 1, node.end_lineno))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = from_imports.setdefault(module, [])
            for alias in node.names:
                name = alias.name if not alias.asname else f"{alias.name} as {alias.asname}"
                if name not in names:
                    names.append(name)
            excluded_lines.update(range(node.lineno - 1, node.end_lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                entry = (alias.name, alias.asname)
                if entry not in plain_imports:
                    plain_imports.append(entry)
            excluded_lines.update(range(node.lineno - 1, node.end_lineno))

    body_lines = [line for i, line in enumerate(lines) if i not in excluded_lines]
    return future_seen, plain_imports, from_imports, "\n".join(body_lines)


def _merge_schema_blocks(blocks: list[str]) -> str:
    """對應四章「Schema 定義段」合併演算法：同一個 `file_path` 可能有
    多個獨立 code block（API 邊界 schema、建構子占位、資料容器占位，
    見 05a 三章），依序合併成一個檔案：

    1. 逐 block 用 `_split_block_imports()` 解析出 import 陳述式與其餘
       內容
    2. 所有 block 的 import 陳述式去重、合併（保留各自 module 內第一次
       出現的名稱順序）
    3. `from __future__ import annotations` 只保留一次、放最前面
    4. import 段落之後，依原本各 block 的順序依序接上各自的內容
    """
    future_seen = False
    plain_import_order: list[tuple[str, str | None]] = []
    from_import_order: list[str] = []
    from_import_names: dict[str, list[str]] = {}
    bodies: list[str] = []

    for block in blocks:
        block_future, block_plain, block_from, body = _split_block_imports(block)
        future_seen = future_seen or block_future
        for entry in block_plain:
            if entry not in plain_import_order:
                plain_import_order.append(entry)
        for module, names in block_from.items():
            existing = from_import_names.setdefault(module, [])
            if module not in from_import_order:
                from_import_order.append(module)
            for name in names:
                if name not in existing:
                    existing.append(name)
        bodies.append(body.strip("\n"))

    header = ["from __future__ import annotations"] if future_seen else []
    header.extend(
        f"import {module}" if alias is None else f"import {module} as {alias}"
        for module, alias in plain_import_order
    )
    header.extend(f"from {module} import {', '.join(from_import_names[module])}" for module in from_import_order)

    parts = (["\n".join(header)] if header else []) + [b for b in bodies if b]
    return "\n\n\n".join(parts) + "\n"


def _table_assignment_names(tree: ast.Module) -> list[str]:
    """找出模組層級「`name = Table(...)`」這種 SQLAlchemy Core `Table`
    變數賦值的名稱——對應 08a_scaffold_agent_architecture.md 八章
    「`@ManyToMany`：產生中介表定義」：`@ManyToMany` 的中介表沒有對應
    的 Java entity class，`scaffold_agent` 渲染成純 `Table(...)` 賦值
    （`ast.Assign`），不是 `ast.ClassDef`——若自訂型別索引只掃
    `ClassDef`（見下方 `_build_custom_type_index()`／
    `_scan_project_custom_types()`），這種名稱永遠進不了索引，⑤填空時
    只要在函式本體引用這個中介表（如 `insert(user_roles).values(...)`，
    見 08a 八章「⑤填空時若需要對這張表直接操作」的既有動機），兩層
    import 解析都比對不到，會被四章「兩層都比對不到時保守不加、不猜」
    這條既有原則吞掉，產出缺 import 的程式碼。

    只認「單一目標、RHS 是呼叫 `Table(...)` 或 `xxx.Table(...)`」這個
    精確形狀，不是任意模組層級賦值——避免誤把一般常數賦值（如
    `DEFAULT_TIMEOUT = 30`）也當成自訂型別收進索引。`func` 可能是
    `ast.Name("Table")`（`from sqlalchemy import Table` 之後直接呼叫）
    或 `ast.Attribute(attr="Table")`（`sqlalchemy.Table(...)` 這種完整
    路徑呼叫），08a 八章的渲染範例走前者，這裡兩種都認，不假設呼叫端
    一定用哪一種匯入風格。
    """
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if func_name == "Table":
            names.append(node.targets[0].id)
    return names


def _build_custom_type_index(
    interfaces: list[InterfaceSpec], schema_trees: dict[str, ast.Module], db_models_valid: dict[str, ast.Module]
) -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    """對應四章「import 解析：兩層機制」自訂型別索引，三個來源合併：

    - 來源一：Schema 定義段合併後的每個檔案，AST 掃出頂層 `ClassDef`
    - 來源二：`interfaces` 的 `class_name → file_path`（排除 `None`）
    - 來源三：`db_models` 合併後每個檔案，AST 掃出頂層 `ClassDef`，
      **以及** `_table_assignment_names()` 找到的 `Table(...)` 賦值
      （08a 八章 `@ManyToMany` 中介表，見該函式 docstring）——這兩種
      頂層宣告形狀只會出現在 `db_models`，來源一／二不需要一併掃

    優先序三／二＞一，用寫入順序達成（後寫入覆蓋先寫入）——三個來源
    對應的 class 集合理論上不重疊，這裡只是定義明確的 tie-break 規則。

    `schema_trees`／`db_models_valid` 都是呼叫端在 `build_files()` 驗證
    階段已經 `ast.parse()` 過的結果，這裡直接複用同一次解析，不重新
    parse 一次相同的原始碼字串——比照 07a 四章對 `db_models` 這個來源
    「不是新增的驗證負擔，直接複用同一次解析結果」的既有原則，兩個
    來源一致處理。

    第二個回傳值 `schema_classes_by_module`（對應
    `docs/refactor_bug_trace.md` #8 真實案例）：`ResponseResult<T>` 這類
    專案自訂泛型的具名包裝類別（如 `ResponseResultString`）常常被多個
    模組各自獨立定義一份——③ 逐模組產生 schema，模組之間互不知道彼此
    定義了同名的東西。上面 `index` 是全域、只認 class 名稱的扁平結構，
    同名 class 在多個 `schema_trees` 檔案裡都出現時，只會留住迭代到
    最後的那一份（字典覆寫），導致其他模組明明自己也定義了同名 class，
    `_resolve_imports()` 查到的卻是別的模組的檔案路徑。這裡額外記錄
    「每個模組（`app/schemas/{module}.py` 的 `module` 段）自己的 schema
    檔案定義了哪些 class 名稱」，供 `_resolve_imports()` 優先查「這個
    檔案自己所屬的模組」，查得到就用同一個模組的 schema 檔案，不必依賴
    可能被覆寫過的全域 `index`。
    """
    index: dict[str, str] = {}
    schema_classes_by_module: dict[str, set[str]] = {}
    for file_path, tree in schema_trees.items():
        module = (
            file_path.removeprefix("app/schemas/").removesuffix(".py")
            if file_path.startswith("app/schemas/") and file_path.endswith(".py")
            else None
        )
        names = schema_classes_by_module.setdefault(module, set()) if module is not None else None
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                index[node.name] = file_path
                if names is not None:
                    names.add(node.name)
    for iface in interfaces:
        if iface["class_name"]:
            index[iface["class_name"]] = iface["file_path"]
    for file_path, tree in db_models_valid.items():
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                index[node.name] = file_path
        for name in _table_assignment_names(tree):
            index[name] = file_path
    return index, {module: frozenset(names) for module, names in schema_classes_by_module.items()}


def _module_for_file_path(file_path: str) -> str | None:
    """從 routers／services／repositories 層的 `file_path` 反推 module
    名稱，對應 `design_agent/layout.py::file_path_for_layer()`「檔名規則：
    `{module}_{layer_singular}.py`」的反向操作——`translator_cli` 各自
    獨立維護同一套機械慣例、不 import `design_agent`（見本檔案既有
    `_LAYER_PREFIX` 旁的說明）。`utils`（橫跨多個 module，見
    `file_path_for_utils()`）與 `_global` 保留模組的固定檔案都沒有
    module 概念，回傳 `None`——呼叫端遇到 `None` 時退回既有的全域索引
    行為，不特別處理。
    """
    for prefix, suffix in (
        ("app/routers/", "_router.py"),
        ("app/services/", "_service.py"),
        ("app/repositories/", "_repository.py"),
    ):
        if file_path.startswith(prefix) and file_path.endswith(suffix):
            return file_path.removeprefix(prefix).removesuffix(suffix)
    return None


def _keyword_hits(keyword: str, combined: str) -> bool:
    """`_KNOWN_KEYWORD_IMPORTS` 命中判斷。以英數字元結尾的關鍵字
    （`Session`／`get_db`／`Request`／`UploadFile`／`HTTPException`／
    `Decimal`——`_KNOWN_KEYWORD_IMPORTS` 裡除了 `Depends(`／`Callable[`
    以外的全部）用 `\\b` 單字邊界比對，避免命中自訂型別名稱裡剛好包含
    這個關鍵字當子字串的情況（如 Java DTO 常見命名 `LoginRequest`／
    `UserSession` 這種以 Request／Session 結尾的類別）——這種名稱在
    `combined` 裡只會是 `custom_type_index` 已知的裸名稱（見本函式
    docstring），單字邊界比對不會誤傷。以標點結尾的關鍵字（`Depends(`／
    `Callable[`）維持原本的純子字串比對：Python 識別字不能包含
    `(`／`[`，這兩個字元本身就已經是足夠精確的錨點（比照 07a 四章
    「命中比對用 `Callable[`（含左中括號）而不是單獨的 `Callable`」的
    既有理由），改用 `\\b` 反而會誤判 `Callable[[int, str], bool]`
    （多引數 functional interface 常見的雙層中括號）這種合法情況。
    """
    if keyword[-1].isalnum():
        return re.search(rf"\b{re.escape(keyword)}\b", combined) is not None
    return keyword in combined


def _resolve_imports(
    type_strings: list[str],
    custom_type_index: dict[str, str],
    same_file_class_names: frozenset[str] = frozenset(),
    schema_classes_by_module: dict[str, frozenset[str]] | None = None,
    own_module: str | None = None,
    is_repository_layer: bool = False,
) -> list[str]:
    """對應四章「import 解析：兩層機制」：已知關鍵字表（子字串比對，見
    `_keyword_hits()`）＋自訂型別索引（AST `ast.Name` 節點比對，不用
    字串裁切／正則，理由見四章「用 AST 而非字串裁切是必要的」）。兩層
    都比對不到時保守不加、不猜（見四章「兩層都比對不到時保守不加、
    不猜」）。

    型別字串保證是裸名稱（`design_agent.map_java_type()` 對自訂類別
    只產出裸名稱，見 05a 五章型別對應表），不會出現 `models.User` 這種
    帶命名空間前綴的寫法，因此不需要對 `ast.Attribute` 額外排除。

    `same_file_class_names` 是正在組裝的這個檔案自己（`_render_interface_
    files()` 這一輪 `file_path` 分組下）的全部 class 名稱——這批類別即使
    命中 `custom_type_index`（該索引涵蓋全專案，本來就會收錄這個檔案
    自己定義的 class），也不需要 import，因為它們就在同一個檔案裡。
    這批 Java 靜態工具類常見「方法簽名引用同檔案另一個 class」（如
    `ResponseResult.error_4(self, code: ErrorCode)`，`ErrorCode` 是同一個
    檔案稍後才定義的另一個 class）——不排除的話會產生一行恆為 True 的
    循環 import，讓這個模組 import 階段直接 ImportError（已用真實案例
    重現：exam-platform-api 的 app/services/common_service.py，見
    09b_implement_agent_code.md 十章「已知限制」）。填空階段的對應
    排除見 `resolve_body_imports()`——這裡是骨架生成階段的簽名層級
    版本，同一個問題、兩個不同時機點的兩份獨立實作，各自要排除。

    `schema_classes_by_module`／`own_module`（對應 `docs/refactor_bug_
    trace.md` #8 真實案例）：同名的具名回應包裝 class（如
    `ResponseResultString`）常常被多個模組各自獨立定義一份，`custom_
    type_index` 這個全域扁平索引在這種情況下只留得住其中一個模組的
    檔案路徑（見 `_build_custom_type_index()` docstring）。這裡查到
    `own_module`（這個檔案自己所屬的模組，`None` 代表 utils／`_global`
    這種沒有 module 概念的檔案）自己的 schema 檔案剛好也定義了同名
    class 時，**優先**直接用同一個模組的 schema 檔案，不查可能已經被
    覆寫過的全域索引——這樣同一個模組的檔案永遠 import 自己的 schema
    定義，不會意外指向別的模組。查不到（`own_module` 是 `None`，或
    這個模組自己沒有定義這個 class 名稱）才退回既有的全域索引行為。

    `is_repository_layer`（對應 `docs/refactor_bug_trace.md` #17 真實
    案例）：repositories 層需要的是 SQLAlchemy ORM model（`app/models/
    {module}.py`），但 `ExamEntity`／`AnswerEntity`／`ExamkindEntity` 這類
    entity 名稱常常剛好也被同一個模組的 schema 檔案（Pydantic DTO）定義
    一份——上面這條 `own_module_schema_classes` 優先序原本是為了 #8
    （`ResponseResultString` 這類跨模組同名回應包裝類別）設計，沒有分
    「這個檔案是哪一層」，repositories 層的檔案因此被這條規則誤導，優先
    選到同名的 schema 定義，把 Pydantic `BaseModel` 子類別傳給 SQLAlchemy
    查詢，在真正執行查詢時才炸 `sqlalchemy.exc.ArgumentError`（本體程式碼
    本身完全正確，純粹被錯誤的 import 拖累）。`is_repository_layer=True`
    時完全跳過這條「優先查同模組 schema」的規則，直接退回下面的全域
    `custom_type_index`——`_build_custom_type_index()` 組裝索引時
    `db_models_valid`（來源三）是最後寫入、覆寫掉同名的 schema 條目（來源
    一），全域索引本來就已經是「同名時 model 贏」，不需要另外查一次
    `app/models/`。
    """
    combined = "\n".join(type_strings)
    lines = [import_line for keyword, import_line in _KNOWN_KEYWORD_IMPORTS if _keyword_hits(keyword, combined)]

    own_module_schema_classes = (
        frozenset()
        if is_repository_layer
        else (schema_classes_by_module or {}).get(own_module, frozenset()) if own_module else frozenset()
    )

    custom_imports: dict[str, list[str]] = {}
    for type_str in type_strings:
        try:
            expr = ast.parse(type_str, mode="eval")
        except SyntaxError:
            continue  # 型別字串已在渲染前個別驗證過，這裡理論上不會發生
        for node in ast.walk(expr):
            if not (isinstance(node, ast.Name) and node.id not in same_file_class_names):
                continue
            if node.id in own_module_schema_classes:
                module_path = f"app.schemas.{own_module}"
            elif node.id in custom_type_index:
                module_path = custom_type_index[node.id].removesuffix(".py").replace("/", ".")
            else:
                continue
            names = custom_imports.setdefault(module_path, [])
            if node.id not in names:
                names.append(node.id)

    lines.extend(f"from {module_path} import {', '.join(sorted(names))}" for module_path, names in sorted(custom_imports.items()))
    return lines


def _render_function_snippet(iface: InterfaceSpec, *, is_class_method: bool) -> str:
    """對應四章「interfaces：函式簽名渲染」。routers 層 API 邊界方法
    （`http_method` 非 `None`）加 `@router.{method}("{route}")` 裝飾器；
    routers 層非邊界方法（私有 helper，`http_method` 為 `None`）不加
    裝飾器；services／repositories 層一律不加裝飾器、加 `self` 前綴。
    """
    params = ", ".join(f"{p['name']}: {_normalize_type(p['type'])}" for p in iface["params"])
    if is_class_method:
        params = f"self, {params}" if params else "self"
    return_type = _normalize_type(iface["return_type"])

    lines = []
    http_method = iface.get("http_method")
    if http_method:
        lines.append(f'@router.{http_method.lower()}("{iface["route_path"]}")')
    lines.append(f"def {iface['function_name']}({params}) -> {return_type}:")
    lines.append("    pass")
    return "\n".join(lines) + "\n"


def _assemble_file_text(
    layer: str,
    import_lines: list[str],
    valid: list[tuple[InterfaceSpec, str]],
    all_class_names: list[str],
    class_bases: dict[str, str] | None = None,
) -> str:
    """把單一檔案已驗證通過的函式片段組成完整檔案原始文字（尚未經過
    `PythonAdapter.parse → render` 的最終格式化，見
    `_render_interface_files()`）。

    `all_class_names`（services／repositories 層專用）是這個檔案裡
    **全部** class 名稱，不是只有 `valid` 裡出現過的——四章「語法驗證
    與寫入」步驟 3：同一個 `class_name` 分組若所有方法都被跳過（極端
    情況），該 `class` 仍要渲染出來，body 用 `pass` 佔位，維持檔案
    語法合法。

    `class_bases`（repositories 層專用，對應 docs/refactor_bug_trace.md
    #10／#16）：`class_name → entity 型別簡單名稱`，來自這個類別任一
    `InterfaceSpec.jpa_base_entity`（見 `_render_interface_files()` 組裝
    端）。有值代表這個 interface 繼承了 Spring Data 基底介面，渲染成
    `class X(BaseRepository[Entity]):` 並注入 `model = Entity` 當第一行
    body——讓 `find_all`/`find_by_id` 等內建方法透過繼承「真的」存在，
    不需要像 `_synthesize_inherited_repository_reads()` 舊版那樣逐一
    合成方法（那個函式仍保留，只是現在合成出的 InterfaceSpec 一樣帶
    `jpa_base_entity`，走同一條繼承渲染路徑，不再是唯一手段）。
    """
    if layer == "routers":
        header = ["from __future__ import annotations", "", "from fastapi import APIRouter", *import_lines, "", "router = APIRouter()", ""]
        body = "\n".join(snippet for _, snippet in valid)
        return "\n".join(header) + "\n" + body

    if layer in (_GLOBAL_ADVICE_LAYER, "utils"):
        # class_name=None 自由函式（跟 routers 層同一種形狀），但不是
        # 真正的路由檔案，不需要 APIRouter 樣板。utils 同一套理由：
        # Java 靜態工具方法對應 Python module-level 函式，不是路由。
        header = ["from __future__ import annotations", "", *import_lines, ""]
        body = "\n".join(snippet for _, snippet in valid)
        return "\n".join(header) + "\n" + body

    by_class: dict[str, list[str]] = {}
    for iface, snippet in valid:
        by_class.setdefault(iface["class_name"], []).append(snippet)

    class_bases = class_bases or {}
    header_import_lines = [*import_lines, _BASE_REPOSITORY_IMPORT] if class_bases else import_lines
    lines = ["from __future__ import annotations", "", *header_import_lines, ""]
    for class_name in all_class_names:
        entity = class_bases.get(class_name)
        if entity is not None:
            lines.append(f"class {class_name}({BASE_REPOSITORY_CLASS}[{entity}]):")
            lines.append(f"    model = {entity}")
        else:
            lines.append(f"class {class_name}:")
        snippets = by_class.get(class_name, [])
        if not snippets and entity is None:
            lines.append("    pass")
        for snippet in snippets:
            lines.extend(f"    {line}" if line else "" for line in snippet.splitlines())
        lines.append("")
    return "\n".join(lines)


def _render_interface_files(
    interfaces: list[InterfaceSpec],
    custom_type_index: dict[str, str],
    schema_classes_by_module: dict[str, frozenset[str]],
) -> tuple[dict[str, str], list[dict]]:
    """對應四章「interfaces：函式簽名渲染」＋「語法驗證與寫入」：逐
    `InterfaceSpec` 隔離失敗（步驟 1／2），最後用 `PythonAdapter` 的
    `parse → render` 統一格式化，確保骨架與填空兩階段格式化風格一致。

    `schema_classes_by_module`：對應 `docs/refactor_bug_trace.md` #8，
    逐檔案用 `_module_for_file_path()` 反推這個檔案自己所屬的模組，
    傳給 `_resolve_imports()` 優先查同一個模組自己的 schema 定義，避免
    同名的具名回應包裝 class（如 `ResponseResultString`）被誤導到別的
    模組（見該函式 docstring）。
    """
    adapter = PythonAdapter()
    by_file: dict[str, list[InterfaceSpec]] = {}
    for iface in interfaces:
        by_file.setdefault(iface["file_path"], []).append(iface)

    files: dict[str, str] = {}
    skipped: list[dict] = []

    for file_path, ifaces in by_file.items():
        if file_path == _GLOBAL_ADVICE_FILE:
            layer = _GLOBAL_ADVICE_LAYER
        else:
            layer = next((v for prefix, v in _LAYER_PREFIX.items() if file_path.startswith(prefix)), None)
        if layer is None:
            # 按照 05a／07a 契約，interfaces 的 file_path 只會落在
            # routers／services／repositories／utils（或上面明確處理過的
            # _GLOBAL_ADVICE_FILE），理論上不會再觸發這裡——但比照本函式
            # 其餘失敗路徑（語法錯誤、組裝驗證失敗）一律顯式記錄成
            # skipped_interfaces，不靜默丟棄，避免上游一旦出現非預期
            # file_path，這批 InterfaceSpec 人不知鬼不覺地從骨架裡消失、
            # 不會被人工發現。
            for iface in ifaces:
                skipped.append(
                    {
                        "file_path": file_path,
                        "class_name": iface["class_name"],
                        "function_name": iface["function_name"],
                        "error": f"{file_path} 不在 routers／services／repositories／utils 目錄底下（unknown layer）",
                    }
                )
            continue

        valid: list[tuple[InterfaceSpec, str]] = []
        for iface in ifaces:
            snippet = _render_function_snippet(iface, is_class_method=(layer not in _FREE_FUNCTION_LAYERS))
            try:
                ast.parse(snippet)
            except SyntaxError as exc:
                skipped.append(
                    {
                        "file_path": file_path,
                        "class_name": iface["class_name"],
                        "function_name": iface["function_name"],
                        "error": str(exc),
                    }
                )
                continue
            valid.append((iface, snippet))

        all_class_names: list[str] = []
        if layer not in _FREE_FUNCTION_LAYERS:
            for iface in ifaces:
                if iface["class_name"] not in all_class_names:
                    all_class_names.append(iface["class_name"])

        # 對應 docs/refactor_bug_trace.md #10／#16：repositories 層才會有
        # `jpa_base_entity`（見 design_agent/design.py 組裝端），逐 iface
        # 找第一個非 None 的值即可——同一個 class 的全部 InterfaceSpec
        # 理論上要嘛全部帶同一個 entity（都繼承自同一個 Java interface），
        # 要嘛全部是 None，不會同一個 class 出現互相矛盾的兩種 entity。
        class_bases: dict[str, str] = {}
        for iface in ifaces:
            entity = iface.get("jpa_base_entity")
            if entity is not None and iface["class_name"] not in class_bases:
                class_bases[iface["class_name"]] = entity

        type_strings = [_normalize_type(p["type"]) for iface, _ in valid for p in iface["params"]]
        type_strings += [_normalize_type(iface["return_type"]) for iface, _ in valid]
        type_strings += [_normalize_type(entity) for entity in class_bases.values()]
        import_lines = _resolve_imports(
            type_strings, custom_type_index, frozenset(all_class_names),
            schema_classes_by_module, _module_for_file_path(file_path),
            is_repository_layer=(layer == "repositories"),
        )

        raw_text = _assemble_file_text(layer, import_lines, valid, all_class_names, class_bases)
        try:
            tree = adapter.parse(raw_text)
        except SyntaxError as exc:
            raise TranslatorCliAssemblyError(
                f"{file_path} 組裝完成後的最終 ast.parse() 驗證失敗（代表組裝邏輯本身有 bug，見四章步驟 4）：{exc}"
            ) from exc
        files[file_path] = adapter.render(tree)

    return files, skipped


def build_files(
    python_structure: PythonStructure,
    db_models: dict[str, str],
) -> tuple[dict[str, str], list[dict], list[dict]]:
    """對應四章全節，回傳 `(file_path -> 最終檔案內容, skipped_interfaces,
    skipped_db_models)`。純記憶體組裝，不碰磁碟——寫入交給
    `write_files()`，對應四章「寫入時機：全部檔案...驗證通過後，一次性
    寫入磁碟」。
    """
    directory_tree = python_structure["directory_tree"]
    interfaces = python_structure["interfaces"]
    code_blocks = _extract_code_blocks(directory_tree)

    files: dict[str, str] = {}

    # 第二、三段：infra 段直接寫入，schema 段依 file_path 分組合併
    grouped_schema_blocks: dict[str, list[str]] = {}
    infra_sources: dict[str, str] = {}
    for file_path, code in code_blocks:
        if file_path in _INFRA_FILES:
            infra_sources[file_path] = code
        else:
            grouped_schema_blocks.setdefault(file_path, []).append(code)

    schema_sources: dict[str, str] = {}
    schema_trees: dict[str, ast.Module] = {}
    for file_path, blocks in grouped_schema_blocks.items():
        # try/except 涵蓋 _merge_schema_blocks() 本身——它內部對每個
        # block 各自呼叫 ast.parse()，單一 block 語法有誤會在合併
        # 「之前」就拋出 SyntaxError，不能只包住合併後的最終驗證。
        try:
            merged = _merge_schema_blocks(blocks)
            tree = ast.parse(merged)
        except SyntaxError as exc:
            raise TranslatorCliAssemblyError(
                f"{file_path} 合併後的 Schema 定義段無法通過 ast.parse()"
                f"（05a 渲染的 pseudocode 本身有問題，見四章）：{exc}"
            ) from exc
        schema_sources[file_path] = merged
        schema_trees[file_path] = tree
    files.update(schema_sources)

    for file_path, code in infra_sources.items():
        try:
            ast.parse(code)
        except SyntaxError as exc:
            raise TranslatorCliAssemblyError(f"{file_path}（基礎設施段）無法通過 ast.parse()：{exc}") from exc
        files[file_path] = code

    # db_models 併入（四章「db_models」一節）：逐項驗證，個別失敗只跳過那一個檔案
    skipped_db_models: list[dict] = []
    db_models_valid: dict[str, ast.Module] = {}
    for file_path, content in (db_models or {}).items():
        try:
            db_models_valid[file_path] = ast.parse(content)
        except SyntaxError as exc:
            skipped_db_models.append({"file_path": file_path, "error": str(exc)})
            continue
        files[file_path] = content

    # 自訂型別索引（四章「import 解析」來源一／二／三）
    custom_type_index, schema_classes_by_module = _build_custom_type_index(interfaces, schema_trees, db_models_valid)

    # interfaces：逐 InterfaceSpec 隔離失敗渲染
    interface_files, skipped_interfaces = _render_interface_files(
        interfaces, custom_type_index, schema_classes_by_module,
    )
    files.update(interface_files)

    # 步驟 4：全域最終檢查（防禦性，見四章「語法驗證與寫入」）
    for file_path, content in files.items():
        try:
            ast.parse(content)
        except SyntaxError as exc:
            raise TranslatorCliAssemblyError(
                f"{file_path} 組裝完成後的最終 ast.parse() 驗證失敗（代表組裝邏輯本身有 bug，見四章步驟 4）：{exc}"
            ) from exc

    return files, skipped_interfaces, skipped_db_models


def write_files(python_project_path: str, files: dict[str, str]) -> None:
    """對應四章「寫入時機」：`build_files()` 全部驗證通過後才呼叫這個
    函式一次性寫入磁碟。每個檔案各自 `parent.mkdir(parents=True,
    exist_ok=True)`，等同於先掃描 directory_tree 第一段預建所有子目錄。
    """
    root = Path(python_project_path)
    for file_path, content in files.items():
        full_path = root / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")


def _scan_project_custom_types(python_project_path: str) -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    """掃描 `python_project_path` 底下 `app/` 目錄所有 `.py` 檔案的頂層
    `ClassDef`（以及 `_table_assignment_names()` 找到的 `Table(...)`
    賦值，見該函式 docstring），建立 `class_name -> file_path` 索引。供
    `resolve_body_imports()` 在填空階段解析本體引用的跨檔案自訂類別
    （及 08a 八章 `@ManyToMany` 中介表變數）使用。

    跟 `_build_custom_type_index()` 不同：那個函式在骨架生成階段用
    「還在記憶體裡、尚未寫入磁碟」的三個結構化來源建索引；這裡則是在
    填空階段直接掃描「已經寫到磁碟上」的真實檔案——填空階段沒有
    `python_structure` 可用，掃描磁碟是唯一能拿到完整類別清單的方式，
    而且天然反映最新狀態（含同一次 pipeline 執行中先前 task 已經填入
    的內容）。單一檔案解析失敗（理論上不該發生，骨架階段已驗證過）就
    跳過那個檔案，不拖累整體掃描。

    第二個回傳值 `schema_classes_by_module`（對應 docs/refactor_bug_
    trace.md #45 真實案例）：跟 `_build_custom_type_index()` 的同名回傳
    值一樣的道理，同名 schema class 若被多個模組各自獨立定義一份，這裡
    的 `index` 是全域扁平索引，`sorted(root.rglob("*.py"))` 掃描順序
    （字母序）決定了同名 class 最後留誰的路徑，跟這個名稱實際是哪個
    模組要用完全無關（真實案例：`GetAllGradeRs` 同時在 `app/schemas/
    exam.py`／`app/schemas/school.py` 各自定義一份，字母序 "exam" <
    "school"，"school" 版本覆寫掉 "exam" 版本，導致 `exam_router.py::
    grades()` 這個明明該用自己模組 schema 的函式，填空階段補 import 時
    被指到 `school.py` 去）。這裡額外記錄「每個模組（`app/schemas/
    {module}.py` 的 `module` 段）自己的 schema 檔案定義了哪些 class
    名稱」，供 `resolve_body_imports()` 依樣比照 `_resolve_imports()`
    （#8）優先查「這個函式自己所屬的模組」，不必依賴可能被覆寫過的
    全域 `index`——這是 #8 當初就記錄下來、暫緩處理的分層盲區（見
    docs/refactor_bug_trace.md #17「附帶風險」），#45 是它第一次真的
    被踩到的真實案例。
    """
    root = Path(python_project_path) / "app"
    index: dict[str, str] = {}
    schema_classes_by_module: dict[str, set[str]] = {}
    if not root.is_dir():
        return index, {}
    for py_file in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        rel_path = py_file.relative_to(Path(python_project_path)).as_posix()
        module = (
            rel_path.removeprefix("app/schemas/").removesuffix(".py")
            if rel_path.startswith("app/schemas/") and rel_path.endswith(".py")
            else None
        )
        names = schema_classes_by_module.setdefault(module, set()) if module is not None else None
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                index[node.name] = rel_path
                if names is not None:
                    names.add(node.name)
        for name in _table_assignment_names(tree):
            index[name] = rel_path
    return index, {module: frozenset(names) for module, names in schema_classes_by_module.items()}


def resolve_body_imports(
    python_project_path: str,
    tree: ast.Module,
    body_statements: list[ast.stmt],
    bound_names: set[str],
    own_module: str | None = None,
) -> list[str]:
    """對應 07a 五章「填空模式：本體 import 解析」。`fill_function()`
    填入的函式本體是 qwen 自由生成的內容，可能引用簽名以外的名稱（跨
    檔案自訂類別、框架例外）——骨架階段的兩層 import 解析（見
    `_resolve_imports()`）只分析函式簽名，看不到這些，因為當時函式
    本體還是空的 `pass`。這裡在填空完成、`splice_body()` 已經把新本體
    接上 `tree` 之後，針對新插入的本體內容重新掃一次，找出遺漏的
    import。

    `bound_names` 是這個函式作用域內已知會被綁定的名稱（函式參數，
    含 `self`）——出現在這裡的名稱即使剛好命中已知關鍵字表／自訂型別
    索引，也不當成需要 import 的對象，因為它們本來就已經可用。

    比照四章「兩層都比對不到時保守不加、不猜」：只對已知關鍵字表跟
    真實掃描出的自訂型別索引（`_scan_project_custom_types()`）命中的
    名稱補 import，其餘一律不猜——本體裡任何其他未定義名稱，維持現狀
    （不主動修正），留給 Harness 的 module 局部驗證與 ⑦ Debug Agent
    處理，不在這裡窮舉。

    `own_module`（對應 docs/refactor_bug_trace.md #45 真實案例，補齊
    #8／#17 當初留下的分層盲區）：這個函式所屬的模組（呼叫端用既有的
    `_module_for_file_path(target_file)` 算出，`None` 代表 utils／
    `_global` 這種沒有 module 概念的檔案）。`_scan_project_custom_types()`
    回傳的 `index` 是全域扁平索引，同名 schema class 若被多個模組各自
    獨立定義一份，掃描順序（字母序）決定留誰的路徑，跟這個名稱實際該
    屬於哪個模組無關（真實案例：`GetAllGradeRs` 同時在 `exam.py`／
    `school.py` 定義，`exam_router.py::grades()` 卻被指到 `school.py`）
    ——這裡比照 `_resolve_imports()`（#8）優先查同一個模組自己的
    schema 定義，查不到才退回全域索引。
    """
    referenced: set[str] = set()
    assigned: set[str] = set(bound_names)
    for stmt in body_statements:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    referenced.add(node.id)
                else:
                    assigned.add(node.id)

    candidates = referenced - assigned - set(dir(builtins))
    if not candidates:
        return []

    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom):
            candidates -= {alias.asname or alias.name for alias in stmt.names}
        elif isinstance(stmt, ast.Import):
            candidates -= {alias.asname or alias.name.split(".")[0] for alias in stmt.names}

    # 這個名稱若本來就是「這個檔案自己」頂層定義的 class／函式／模組
    # 層級變數，同一模組內引用不需要 import——這裡的 candidates 只知道
    # 「在函式本體被引用、且不是參數/區域變數」，無法分辨它是跨檔案的
    # 自訂型別，還是同檔案另一個頂層符號（常見於這批 Java 靜態工具類
    # 翻譯結果：class 自己的方法回傳自己的型別，如
    # `ResponseResult.error() -> ResponseResult(...)`）。`_scan_project_
    # custom_types()` 掃描整個 app/ 目錄（含這個檔案自己），若不排除，
    # 會誤判成「要從自己匯入自己」，產生一行恆為 True 的循環 import，
    # `import` 這個模組時直接 ImportError（已用真實案例重現：
    # exam-platform-api 的 app/services/common_service.py 曾因此整個
    # 服務起不來，見 09b_implement_agent_code.md 十章「已知限制」）。
    same_file_top_level = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in tree.body:
        if isinstance(node, ast.Assign):
            same_file_top_level |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    candidates -= same_file_top_level

    if not candidates:
        return []

    # 已知關鍵字表比對用精確集合成員判斷，不是子字串／單字邊界比對
    # （見 `_keyword_hits()`）——這裡的 candidates 已經是逐一拆開的裸
    # 識別字（AST Name 節點），不是要在一段文字裡搜尋子字串，不會有
    # `_keyword_hits()` 要防的「自訂名稱剛好包含關鍵字子字串」問題，
    # 精確比對本身就是最精確的判斷方式。`Depends(`／`Callable[` 這兩個
    # 帶標點的關鍵字，比對前先用 `rstrip("([")` 去掉標點，因為 AST
    # `Name` 節點的 `.id` 不會帶括號（`Depends(get_db)` 這個呼叫式的
    # 名稱本身是 `"Depends"`，標點是呼叫語法的一部分，不是名稱的一部分）。
    lines = [
        import_line for keyword, import_line in _KNOWN_KEYWORD_IMPORTS if keyword.rstrip("([") in candidates
    ]

    custom_type_index, schema_classes_by_module = _scan_project_custom_types(python_project_path)
    own_module_schema_classes = schema_classes_by_module.get(own_module, frozenset()) if own_module else frozenset()

    for name in sorted(candidates):
        if name in own_module_schema_classes:
            module_path = f"app.schemas.{own_module}"
            if name in custom_type_index and custom_type_index[name] != f"app/schemas/{own_module}.py":
                logger.info(
                    "resolve_body_imports()：本體引用的 %s 在全域索引裡指向 %s，"
                    "但這個函式自己所屬的模組 %s 也定義了同名 class，優先用自己"
                    "模組的定義（見 docs/refactor_bug_trace.md #45／#8）：改成 "
                    "from %s import %s",
                    name, custom_type_index[name], own_module, module_path, name,
                )
        elif name in custom_type_index:
            module_path = custom_type_index[name].removesuffix(".py").replace("/", ".")
        else:
            continue
        lines.append(f"from {module_path} import {name}")

    return lines


def insert_import_lines(tree: ast.Module, import_lines: list[str]) -> None:
    """把 `resolve_body_imports()` 算出的 import 陳述式插入 `tree`，
    放在既有 import 區塊（含開頭可能有的模組 docstring）之後、其餘
    程式碼之前——就地修改 `tree.body`，呼叫端接著跑
    `PythonAdapter.render()` 即可反映這批新 import。
    """
    if not import_lines:
        return
    insert_at = 0
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            insert_at += 1
        elif (
            insert_at == 0
            and isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            insert_at += 1  # 模組 docstring，算在 import 區塊之前
        else:
            break
    new_nodes = [ast.parse(line).body[0] for line in import_lines]
    tree.body[insert_at:insert_at] = new_nodes
