# translator_cli/python_adapter.py
"""目標語言 Adapter 介面與唯一實作，對應 07a 六章「AST 插入機制」、
十章「目標語言 Adapter 介面」。`generate_scaffold()`／`fill_function()`
一律透過 `PythonAdapter` 操作 `ast` 模組，不直接 import `ast`（07a
十章）——這個專案目前只有 Python 一種目標語言，`LanguageAdapter` 只是
劃分模組邊界的抽象基底，不是插件系統。

`extract_body_statements()` 是六章步驟 6／6a／6b 的共用實作：同時被
`ollama_client.get_function_body()`（判斷是否需要觸發七章「修正重試」，
只驗證、不使用回傳值）與 `PythonAdapter.splice_body()`（實際替換，見
步驟 7）呼叫，避免兩處各自重複解析／檢查邏輯——見兩個呼叫端各自的
docstring 說明分工。
"""
from __future__ import annotations

import ast
import copy
from typing import Protocol

from translator_cli.exceptions import TranslatorCliModelOutputError


def extract_body_statements(body_source: str, function_name: str) -> list[ast.stmt]:
    """對應 07a 六章步驟 6／6a／6b。`body_source` 是 delimiter 抽取出的
    「未縮排」陳述式文字（07a 六章「delimiter 契約：模型回傳格式」）。

    三種失敗情況，統一拋 `TranslatorCliModelOutputError`（`SyntaxError`
    直接包一層轉成同一種例外，讓呼叫端只需要 catch 一種型別）：
    - `body_source` 通不過 `ast.parse()`（七章條件 2）
    - 解析出的陳述式清單為空（六章步驟 6a、七章條件 3）——空字串本身
      對 `ast.parse()` 是合法輸入（等同空 module，`body=[]`），若不在
      這裡攔下，會一路走到 `splice_body()` 把空清單指定給函式節點的
      `body`，`ast.unparse()` 不會報錯，而是產出「有簽名、沒有本體」
      的殘缺程式碼，要等最終檢查重新 parse 這段輸出時才會炸
      `SyntaxError`，但那時候已經不在這裡的重試觸發範圍內（見 07a
      六章步驟 6a 完整說明）
    - 解析出的陳述式清單恰好只有一筆、且是與 `function_name` 同名的
      `FunctionDef`／`AsyncFunctionDef`（六章步驟 6b「模型重複輸出函式
      簽名」）——模型把整個函式簽名連同本體一起包進 delimiter，這在
      AST 層級是合法的巢狀函式定義，不會被前兩種檢查攔到，替換後目標
      函式的 body 會變成「只宣告一個從未被呼叫的同名巢狀函式」，語法
      合法但語意錯誤，因此需要單獨判斷
    """
    try:
        body_tree = ast.parse(body_source)
    except SyntaxError as exc:
        raise TranslatorCliModelOutputError(f"body_text 通不過 ast.parse()：{exc}") from exc

    if not body_tree.body:
        raise TranslatorCliModelOutputError(
            "body_text 解析出的陳述式清單為空（delimiter 標記之間只有空白／換行，見 07a 六章步驟 6a）"
        )

    if (
        len(body_tree.body) == 1
        and isinstance(body_tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef))
        and body_tree.body[0].name == function_name
    ):
        raise TranslatorCliModelOutputError(
            f"模型重複輸出函式簽名：body_text 只包含一個與目標函式同名的巢狀函式定義 {function_name!r}（見 07a 六章步驟 6b）"
        )

    return body_tree.body


class LanguageAdapter(Protocol):
    """07a 十章：語言相關的機械操作（AST 定位／替換／渲染）跟 git
    snapshot、ollama 呼叫、delimiter 抽取、衝突偵測這類跟目標語言無關
    的通用流程分開，未來若要支援 Python 以外的目標語言，只需要另外
    實作一個 adapter。
    """

    def parse(self, source: str) -> ast.Module: ...

    def locate_function(
        self, tree: ast.Module, class_name: str | None, function_name: str
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None: ...

    def splice_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef, body_source: str) -> None: ...

    def render(self, tree: ast.Module) -> str: ...

    def validate_syntax(self, source: str) -> None: ...  # 失敗拋 SyntaxError


class PythonAdapter:
    """`LanguageAdapter` 的唯一實作，對應 07a 六章描述的 `ast` 模組操作。
    `generate_scaffold()`（`scaffold.py`）與 `fill_function()`
    （`client.py`）都透過這一層，不直接 import `ast`。
    """

    def parse(self, source: str) -> ast.Module:
        return ast.parse(source)

    def locate_function(
        self, tree: ast.Module, class_name: str | None, function_name: str
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        """對應 07a 六章步驟 3：`class_name` 為 `None` 直接在 `tree.body`
        找；非 `None` 先找 `ClassDef`，再到它的 `body` 底下找方法。
        """
        if class_name is None:
            container = tree.body
        else:
            class_node = next(
                (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None
            )
            if class_node is None:
                return None
            container = class_node.body

        return next(
            (
                n
                for n in container
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function_name
            ),
            None,
        )

    def render_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        """07a 七章「User / System Prompt 組裝」：只渲染這個函式的
        `decorator_list`＋簽名列，不含 body，讓模型知道自己在填什麼
        形狀的函式。`ast.unparse()` 沒有「只渲染簽名」的內建選項，這裡
        用一份深拷貝暫時把 body 換成單一 `pass`、`unparse` 之後再把最後
        一行（`pass`）去掉，不動到原始節點。這個方法不在 `LanguageAdapter`
        Protocol 裡——只有 Python 這個目標語言的呼叫端需要它，不影響
        「未來支援其他語言」的抽象邊界。
        """
        stub = copy.deepcopy(node)
        stub.body = [ast.Pass()]
        ast.fix_missing_locations(stub)
        lines = ast.unparse(stub).splitlines()
        return "\n".join(lines[:-1])

    def splice_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef, body_source: str) -> None:
        """對應 07a 六章步驟 6／7：驗證＋替換。驗證邏輯見
        `extract_body_statements()`——這裡是六章描述「步驟 6 之後、
        步驟 7 之前」實際發生的地方，`ollama_client.get_function_body()`
        已經在回傳前用同一個函式驗證過一次（見 07a 七章重試機制），這裡
        是 defense-in-depth，不是唯一防線。
        """
        node.body = extract_body_statements(body_source, node.name)

    def render(self, tree: ast.Module) -> str:
        """對應 07a 六章步驟 8／9：`ast.fix_missing_locations()` ＋
        `ast.unparse()`。`ast.unparse()` 會重新格式化整個檔案，這是
        刻意接受的行為（見 07a 六章「`ast.unparse()` 會重新格式化整個
        檔案」）——`generate_scaffold()` 與 `fill_function()` 一律透過
        這個方法產出最終文字，確保兩階段格式化風格天生一致。
        """
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)

    def validate_syntax(self, source: str) -> None:
        """對應 07a 六章步驟 10、四章步驟 4：寫入磁碟前的最後一道檢查。
        失敗直接讓 `SyntaxError` 往外傳，由呼叫端決定要包成
        `FillResult(success=False, ...)` 還是 `TranslatorCliAssemblyError`
        （兩種情境的處理方式不同，見各自呼叫端）。
        """
        ast.parse(source)
