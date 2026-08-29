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


def strip_all_function_bodies(source: str) -> str:
    """把 `source` 裡「所有」函式／方法本體替換成單一 `pass`，只保留
    簽名、裝飾器、import、class 定義與 class 層級屬性宣告（SQLAlchemy
    Column／Pydantic 欄位這類定義資料形狀的陳述式，不是函式本體，不受
    影響）。對應 09b_bug_trace.md #37：`context_files` 純粹是參考用途，
    模型只需要知道「這裡有哪些函式／類別可用、簽名長怎樣」就能正確
    呼叫，不需要看到其他（非目前要填的）函式的完整實作細節——真實環境
    量化證實，prompt 越大，本地模型跑題與生成耗時暴增的機率越高（見
    `translator_cli/client.py` 的 `_trim_context_files_if_oversized()`）。

    只在呼叫端判斷 context 過大時才會被呼叫，不是無條件套用（見
    `client.py`）。用 `ast.walk()` 找出所有 `FunctionDef`／
    `AsyncFunctionDef`（不分是否巢狀、是否為 class 方法）逐一替換
    `body`，其餘節點原封不動。輸入若通不過 `ast.parse()`，讓
    `SyntaxError` 原樣往外傳，由呼叫端決定要不要退回原始內容（裁減本身
    不該變成新的失敗來源）。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body = [ast.Pass()]
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def extract_referenced_classes(source: str, seed_names: set[str]) -> str:
    """對應 `docs/09b_bug_trace.md #45`：`strip_all_function_bodies()`
    （見上方）對「純資料宣告、沒有函式本體」的檔案（`app/schemas/`／
    `app/models/` 這類 Pydantic／SQLAlchemy 資料類別檔）是完全無效的
    空操作——這些檔案的膨脹來源是「一堆無關的 class 定義」，不是「函式
    本體寫太長」，剝函式本體剝不到東西。受控實驗證實：把這類檔案裁到
    只留這個 task 真的用得到的 class，跟裁到完整未裁剪版本比，成功率
    與耗時差異巨大（12-25s vs 逾時 21 分鐘），不是裁不裁都差不多。

    `seed_names`：呼叫端已經算出、這個 task 可能用到的 class 名稱（通常
    來自函式簽名的型別標註／description 文字裡出現的名稱，見
    `client.py::_referenced_class_names()`）。這裡在種子集合之上做**一層
    遞迴閉包**：被留下的 class，若它自己的欄位型別標註又指到另一個這個
    檔案裡定義的 class（最常見的模式：`ResponseResultXxxRs.data: XxxRs`，
    `XxxRs` 不會出現在目標函式簽名或 description 裡，卻是理解
    `ResponseResultXxxRs` 結構的必要資訊），也一併留下，直到不再有新
    class 被加入為止（`defined_names` 是有限集合，保證會收斂）。

    `seed_names` 為空、或跟這個檔案實際定義的 class 完全沒有交集
    （代表呼叫端的文字比對機制沒抓到任何線索，不是「這個檔案真的用不到
    任何東西」）時，**原樣回傳整份原始內容，不做任何過濾**——寧可裁不動
    也不要錯砍模型真正需要的資訊，這是跟 `strip_all_function_bodies()`
    失敗時「退回原始內容」一致的保守處理方式。

    只處理模組頂層 `ClassDef`，不處理巢狀 class；import／模組層級的
    非 class 陳述式（常數指派等）維持不變，跟 `extract_specific_functions()`
    對非目標函式陳述式的處理方式一致。
    """
    tree = ast.parse(source)

    class_nodes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    if not seed_names & class_nodes.keys():
        return source

    kept = set(seed_names) & class_nodes.keys()
    changed = True
    while changed:
        changed = False
        for name in list(kept):
            for referenced in _referenced_names_in_class_body(class_nodes[name]):
                if referenced in class_nodes and referenced not in kept:
                    kept.add(referenced)
                    changed = True

    new_body = [
        node for node in tree.body
        if not isinstance(node, ast.ClassDef) or node.name in kept
    ]
    tree.body = new_body
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _referenced_names_in_class_body(node: ast.ClassDef) -> set[str]:
    """掃這個 class 定義本身（欄位型別標註、base class 等）裡出現過的
    識別字——不下鑽進巢狀 class／函式，只看這個 class 自己的直接內容，
    足以涵蓋 Pydantic／SQLAlchemy 欄位型別標註這個目標情境。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
    names.discard(node.name)
    return names


def extract_specific_functions(source: str, targets: list[tuple[str | None, str]]) -> str:
    """對應 06a 七章新設計「`referenced_interfaces` 函式層級抽取」：從
    `source` 裡只抽出 `targets` 指定的 `(class_name, function_name)` 組合
    ——完整保留這些函式的簽名與本體，其餘函式／方法／完全沒被引用到的
    class 一律捨棄；import、模組層級的非函式陳述式（常數指派等）維持
    不變。對應 `docs/09b_bug_trace.md` #37 根因：這類參考檔案不需要看到
    無關函式的實作細節，也不需要它們的簽名——只需要 `targets` 指定的那
    幾個函式的完整定義，不多不少。

    跟 `strip_all_function_bodies()`（門檻式安全網，見
    `translator_cli/client.py::_trim_context_files_if_oversized()`）方向
    相反：那個是「全部保留簽名、砍掉本體」的粗略裁減；這個是「精準只留
    被引用到的函式，其餘整個不出現」，是結構上更精確的做法，優先套用；
    裁減只在精準抽取後 context 仍然過大時才當最後一道安全網介入。

    `targets` 裡指定但在 `source` 找不到的組合（理論上不該發生，
    `interface_id` 來自③的真實輸出，見 06a 五章核對規則）不視為錯誤，
    單純不出現在結果裡——找不到的原因交由呼叫端既有的「`context_files`
    讀取容錯」機制處理，這裡不重複那層責任。
    """
    tree = ast.parse(source)
    wanted = set(targets)

    new_body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            kept_methods = [
                n
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (node.name, n.name) in wanted
            ]
            if kept_methods:
                node.body = kept_methods
                new_body.append(node)
            # 沒有任何方法被引用到的 class 整個捨棄，不留空殼。
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if (None, node.name) in wanted:
                new_body.append(node)
            # 沒被引用的自由函式捨棄。
        else:
            new_body.append(node)

    tree.body = new_body
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


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
    - 解析出的陳述式清單裡，任何一筆是與 `function_name` 同名的
      `FunctionDef`／`AsyncFunctionDef`（六章步驟 6b「模型重複輸出函式
      簽名」，2026-08 因 `docs/09b_bug_trace.md #52` 從「只有一筆時才算」
      放寬成「不論混在多少其他陳述式之間都算」）——模型把整個函式簽名
      連同本體一起包進 delimiter，這在 AST 層級是合法的巢狀函式定義，
      不會被前兩種檢查攔到，替換後目標函式的 body 會變成「宣告一個從
      未被呼叫的同名巢狀函式」，語法合法但語意錯誤，因此需要單獨判斷。
      原本只檢查「body 恰好只有一筆陳述式」的版本，攔不住 ⑦
      Debug Agent 的 `fixed_body` 夾帶 import／裝飾器等其他陳述式、
      同名巢狀函式只是其中一筆的情況（真實案例：`fixed_body` 開頭帶了
      5 行 import，最後一筆才是同名巢狀 `async def`，`len(body)==1`
      判斷不成立，巢狀污染沒被攔下，見 #52 完整重現）
    """
    try:
        body_tree = ast.parse(body_source)
    except SyntaxError as exc:
        raise TranslatorCliModelOutputError(f"body_text 通不過 ast.parse()：{exc}") from exc

    if not body_tree.body:
        raise TranslatorCliModelOutputError(
            "body_text 解析出的陳述式清單為空（delimiter 標記之間只有空白／換行，見 07a 六章步驟 6a）"
        )

    nested_same_name_defs = [
        stmt.name
        for stmt in body_tree.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == function_name
    ]
    if nested_same_name_defs:
        raise TranslatorCliModelOutputError(
            f"模型重複輸出函式簽名：body_text 裡包含一個與目標函式同名的巢狀函式定義 "
            f"{function_name!r}（不限於整段 body 只有這一筆才算，見 09b_bug_trace.md #52）"
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
