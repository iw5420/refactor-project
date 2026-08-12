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
import re
from pathlib import Path

from translator_cli.exceptions import TranslatorCliAssemblyError
from translator_cli.python_adapter import PythonAdapter
from translator_cli.types import InterfaceSpec, PythonStructure

# 05a 三章「全域基礎設施檔案」：內容就是完整檔案內容，直接寫入，不需要
# 像 Schema 定義段那樣合併（見四章「輸入解析：directory_tree 三段格式」
# 「基礎設施段」）。
_INFRA_FILES = {"app/main.py", "app/core/database.py"}

# 05a 三章「檔名規則」的層級判定，`file_path` 開頭字串即可判斷——跟
# `plan_agent/module_index.py` 的 `_LAYER_SUFFIX` 是同一種各自维护機械
# 慣例的既有先例（見該檔案 docstring），這裡不 import design_agent。
_LAYER_PREFIX = {"app/routers/": "routers", "app/services/": "services", "app/repositories/": "repositories"}

# 四章「已知關鍵字表」，子字串比對，一個檔案內出現多次只加一次。
_KNOWN_KEYWORD_IMPORTS: list[tuple[str, str]] = [
    ("Session", "from sqlalchemy.orm import Session"),
    ("Depends(", "from fastapi import Depends"),
    ("get_db", "from app.core.database import get_db"),
    ("Request", "from fastapi import Request"),
    ("UploadFile", "from fastapi import UploadFile"),
    ("HTTPException", "from fastapi import HTTPException"),
    ("Decimal", "from decimal import Decimal"),
    ("Callable[", "from typing import Callable"),
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


def _build_custom_type_index(
    interfaces: list[InterfaceSpec], schema_trees: dict[str, ast.Module], db_models_valid: dict[str, ast.Module]
) -> dict[str, str]:
    """對應四章「import 解析：兩層機制」自訂型別索引，三個來源合併：

    - 來源一：Schema 定義段合併後的每個檔案，AST 掃出頂層 `ClassDef`
    - 來源二：`interfaces` 的 `class_name → file_path`（排除 `None`）
    - 來源三：`db_models` 合併後每個檔案，AST 掃出頂層 `ClassDef`

    優先序三／二＞一，用寫入順序達成（後寫入覆蓋先寫入）——三個來源
    對應的 class 集合理論上不重疊，這裡只是定義明確的 tie-break 規則。

    `schema_trees`／`db_models_valid` 都是呼叫端在 `build_files()` 驗證
    階段已經 `ast.parse()` 過的結果，這裡直接複用同一次解析，不重新
    parse 一次相同的原始碼字串——比照 07a 四章對 `db_models` 這個來源
    「不是新增的驗證負擔，直接複用同一次解析結果」的既有原則，兩個
    來源一致處理。
    """
    index: dict[str, str] = {}
    for file_path, tree in schema_trees.items():
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                index[node.name] = file_path
    for iface in interfaces:
        if iface["class_name"]:
            index[iface["class_name"]] = iface["file_path"]
    for file_path, tree in db_models_valid.items():
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                index[node.name] = file_path
    return index


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


def _resolve_imports(type_strings: list[str], custom_type_index: dict[str, str]) -> list[str]:
    """對應四章「import 解析：兩層機制」：已知關鍵字表（子字串比對，見
    `_keyword_hits()`）＋自訂型別索引（AST `ast.Name` 節點比對，不用
    字串裁切／正則，理由見四章「用 AST 而非字串裁切是必要的」）。兩層
    都比對不到時保守不加、不猜（見四章「兩層都比對不到時保守不加、
    不猜」）。

    型別字串保證是裸名稱（`design_agent.map_java_type()` 對自訂類別
    只產出裸名稱，見 05a 五章型別對應表），不會出現 `models.User` 這種
    帶命名空間前綴的寫法，因此不需要對 `ast.Attribute` 額外排除。
    """
    combined = "\n".join(type_strings)
    lines = [import_line for keyword, import_line in _KNOWN_KEYWORD_IMPORTS if _keyword_hits(keyword, combined)]

    custom_imports: dict[str, list[str]] = {}
    for type_str in type_strings:
        try:
            expr = ast.parse(type_str, mode="eval")
        except SyntaxError:
            continue  # 型別字串已在渲染前個別驗證過，這裡理論上不會發生
        for node in ast.walk(expr):
            if isinstance(node, ast.Name) and node.id in custom_type_index:
                module_path = custom_type_index[node.id].removesuffix(".py").replace("/", ".")
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
) -> str:
    """把單一檔案已驗證通過的函式片段組成完整檔案原始文字（尚未經過
    `PythonAdapter.parse → render` 的最終格式化，見
    `_render_interface_files()`）。

    `all_class_names`（services／repositories 層專用）是這個檔案裡
    **全部** class 名稱，不是只有 `valid` 裡出現過的——四章「語法驗證
    與寫入」步驟 3：同一個 `class_name` 分組若所有方法都被跳過（極端
    情況），該 `class` 仍要渲染出來，body 用 `pass` 佔位，維持檔案
    語法合法。
    """
    if layer == "routers":
        header = ["from __future__ import annotations", "", "from fastapi import APIRouter", *import_lines, "", "router = APIRouter()", ""]
        body = "\n".join(snippet for _, snippet in valid)
        return "\n".join(header) + "\n" + body

    by_class: dict[str, list[str]] = {}
    for iface, snippet in valid:
        by_class.setdefault(iface["class_name"], []).append(snippet)

    lines = ["from __future__ import annotations", "", *import_lines, ""]
    for class_name in all_class_names:
        lines.append(f"class {class_name}:")
        snippets = by_class.get(class_name, [])
        if not snippets:
            lines.append("    pass")
        for snippet in snippets:
            lines.extend(f"    {line}" if line else "" for line in snippet.splitlines())
        lines.append("")
    return "\n".join(lines)


def _render_interface_files(
    interfaces: list[InterfaceSpec], custom_type_index: dict[str, str]
) -> tuple[dict[str, str], list[dict]]:
    """對應四章「interfaces：函式簽名渲染」＋「語法驗證與寫入」：逐
    `InterfaceSpec` 隔離失敗（步驟 1／2），最後用 `PythonAdapter` 的
    `parse → render` 統一格式化，確保骨架與填空兩階段格式化風格一致。
    """
    adapter = PythonAdapter()
    by_file: dict[str, list[InterfaceSpec]] = {}
    for iface in interfaces:
        by_file.setdefault(iface["file_path"], []).append(iface)

    files: dict[str, str] = {}
    skipped: list[dict] = []

    for file_path, ifaces in by_file.items():
        layer = next((v for prefix, v in _LAYER_PREFIX.items() if file_path.startswith(prefix)), None)
        if layer is None:
            # 按照 05a／07a 契約，interfaces 的 file_path 只會落在
            # routers／services／repositories 三層，理論上不會觸發——
            # 但比照本函式其餘失敗路徑（語法錯誤、組裝驗證失敗）一律
            # 顯式記錄成 skipped_interfaces，不靜默丟棄，避免上游一旦
            # 出現非預期 file_path，這批 InterfaceSpec 人不知鬼不覺地
            # 從骨架裡消失、不會被人工發現。
            for iface in ifaces:
                skipped.append(
                    {
                        "file_path": file_path,
                        "class_name": iface["class_name"],
                        "function_name": iface["function_name"],
                        "error": f"{file_path} 不在 routers／services／repositories 三層目錄底下（unknown layer）",
                    }
                )
            continue

        valid: list[tuple[InterfaceSpec, str]] = []
        for iface in ifaces:
            snippet = _render_function_snippet(iface, is_class_method=(layer != "routers"))
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
        if layer != "routers":
            for iface in ifaces:
                if iface["class_name"] not in all_class_names:
                    all_class_names.append(iface["class_name"])

        type_strings = [_normalize_type(p["type"]) for iface, _ in valid for p in iface["params"]]
        type_strings += [_normalize_type(iface["return_type"]) for iface, _ in valid]
        import_lines = _resolve_imports(type_strings, custom_type_index)

        raw_text = _assemble_file_text(layer, import_lines, valid, all_class_names)
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
    custom_type_index = _build_custom_type_index(interfaces, schema_trees, db_models_valid)

    # interfaces：逐 InterfaceSpec 隔離失敗渲染
    interface_files, skipped_interfaces = _render_interface_files(interfaces, custom_type_index)
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


def _scan_project_custom_types(python_project_path: str) -> dict[str, str]:
    """掃描 `python_project_path` 底下 `app/` 目錄所有 `.py` 檔案的頂層
    `ClassDef`，建立 `class_name -> file_path` 索引。供 `resolve_body_
    imports()` 在填空階段解析本體引用的跨檔案自訂類別使用。

    跟 `_build_custom_type_index()` 不同：那個函式在骨架生成階段用
    「還在記憶體裡、尚未寫入磁碟」的三個結構化來源建索引；這裡則是在
    填空階段直接掃描「已經寫到磁碟上」的真實檔案——填空階段沒有
    `python_structure` 可用，掃描磁碟是唯一能拿到完整類別清單的方式，
    而且天然反映最新狀態（含同一次 pipeline 執行中先前 task 已經填入
    的內容）。單一檔案解析失敗（理論上不該發生，骨架階段已驗證過）就
    跳過那個檔案，不拖累整體掃描。
    """
    root = Path(python_project_path) / "app"
    index: dict[str, str] = {}
    if not root.is_dir():
        return index
    for py_file in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        rel_path = py_file.relative_to(Path(python_project_path)).as_posix()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                index[node.name] = rel_path
    return index


def resolve_body_imports(
    python_project_path: str,
    tree: ast.Module,
    body_statements: list[ast.stmt],
    bound_names: set[str],
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

    custom_type_index = _scan_project_custom_types(python_project_path)
    lines.extend(
        f"from {custom_type_index[name].removesuffix('.py').replace('/', '.')} import {name}"
        for name in sorted(candidates)
        if name in custom_type_index
    )

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
