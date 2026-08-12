# translator-cli 程式碼實作

> 07a 是設計面文件，本文件是實作面文件，一一對應、不重複設計理由——每節開頭註明對應 07a 章節，這裡只講怎麼落地成程式碼。對應 `00_refactor_architecture.md` 十一章文件索引的 `07b_translator_cli_code.md`。

07a 十一章定義的套件結構列了 8 個檔案（`client.py`／`types.py`／`python_adapter.py`／`scaffold.py`／`ollama_client.py`／`prompts.py`／`git_ops.py`／`exceptions.py`）。`client.py`／`types.py` 原本是 stub（見 00 九章、01 七章 stub-first 開發策略），本文件把兩者換成真實實作；其餘 6 個檔案新增。`formatting.py` 是 07a 十一章套件結構之外、實作時新增的第七個模組（比照 04b／05b 對 `exceptions.py` 的既有先例，落地時才產生的基礎設施檔案）。

## 目錄

| 檔案 | 對應章節 | 說明 |
|---|---|---|
| `translator_cli/exceptions.py` | 07a 十三章 | 例外階層 |
| `translator_cli/types.py` | 07a 四、五章 | `FillResult`（既有，改實作）／`ScaffoldResult`／`SkippedInterface`／`SkippedDbModel`／`PythonStructure`／`InterfaceSpec`／`ParamSpec`（結構對齊 `graph/state.py`，不 import） |
| `translator_cli/python_adapter.py` | 07a 六、十章 | `LanguageAdapter` Protocol、`PythonAdapter` 實作、`extract_body_statements()` 共用驗證邏輯 |
| `translator_cli/git_ops.py` | 07a 八、九章 | git snapshot、commit 顆粒度、衝突偵測、rollback |
| `translator_cli/prompts.py` | 07a 七章 | system／user prompt 模板 |
| `translator_cli/ollama_client.py` | 07a 七章 | ollama 連線、delimiter 抽取、網路層與格式錯誤兩種重試 |
| `translator_cli/scaffold.py` | 07a 四、五章 | `directory_tree` 三段解析、Schema 定義段合併、`db_models` 併入、兩層 import 解析、逐 `InterfaceSpec` 隔離驗證、填空模式本體 import 解析（`resolve_body_imports()`／`insert_import_lines()`） |
| `translator_cli/client.py` | 07a 二、四、五章 | 對外唯一入口：`generate_scaffold()`、`fill_function()`（既有，改實作） |
| `translator_cli/formatting.py` | — | 硬性依賴 `ruff check --select I --fix`＋`ruff format`，見八章 |
| `graph/state.py` | 07a 二章 | 新增 `RefactorState.python_project_path` 欄位 |
| `main.py` | 07a 二章 | `initial_state` 新增 `python_project_path` |
| `graph/nodes/scaffold_node.py` | 07a 十二章 | 接上 `translator_cli.generate_scaffold()`（取代 stub） |
| `graph/nodes/implement_node.py` | 07a 十二章 | `_run_one_task()` 接上 `translator_cli.fill_function()` 完整引數（取代 stub） |
| `tests/translator_cli/test_python_adapter.py` | 07a 六、十章 | `PythonAdapter`／`extract_body_statements()` 測試（14 個） |
| `tests/translator_cli/test_scaffold.py` | 07a 四、五章 | 型別正規化、Schema 合併、import 解析、端對端 `build_files()` 測試、填空模式本體 import 解析測試（29 個） |
| `tests/translator_cli/test_git_ops.py` | 07a 八、九章 | 真實 git repo 整合測試，含 rollback 復原（19 個） |
| `tests/translator_cli/test_ollama_client.py` | 07a 七章 | delimiter 抽取、格式錯誤重試、網路層重試、環境變數缺失測試（13 個，`monkeypatch` 假造 httpx／ollama 回應） |
| `tests/translator_cli/test_client.py` | 07a 二、四、五、八、九章 | `generate_scaffold()`／`fill_function()` 端對端測試（19 個，真實 tmp_path git repo＋假造模型回應） |
| `tests/translator_cli/test_formatting.py` | — | `formatting.format_paths()` 硬性依賴行為測試（5 個） |

**已驗證**：`python -m pytest tests/ -q` 全數通過（332 個），見十一章列出尚未能驗證的部分。`translator_cli` 套件在 `graph` 套件完全不可 import 的情況下仍能正常 import（見二章），證實跟 `graph/`（LangGraph 編排層）之間確實沒有 import-time 依賴。**已對真實 ollama／nginx 環境＋真實 Java 專案（93 個檔案）跑過端對端測試**：真實 pipeline（① 解析 → ③ 設計 → [P] 規劃 → ④ 骨架 → ⑤ 實作）70 個 task 中 69 個成功，過程中 delimiter 修正重試、網路層重試都真實觸發並驗證過；填空模式本體 import 解析也已對真實案例（router 層函式呼叫 `UserRepository`、拋出 `HTTPException`）重新驗證修正有效；詳見十一章。

---

## 一、`exceptions.py`——例外階層

對應 07a 十三章「錯誤處理範圍」。translator-cli 本身的呼叫失敗不直接觸發 `retry_count` 迴圈，而是反映成 `FillResult(success=False)` 或 `generate_scaffold()` 的 `{"success": False}`；套件內部一律「深處 raise、邊界（`client.py`）統一 catch 轉換」，不讓例外往 `graph/nodes/` 洩漏。

```python
# translator_cli/exceptions.py
"""translator-cli 例外階層，對應 07a 十三章「錯誤處理範圍」。所有例外
共用 `TranslatorCliError` 基底類別，但呼叫端（`client.py`）一律逐一
`except` 特定子類別、轉成 `FillResult(success=False, ...)` 或
`{"success": False, ...}` 回傳，不讓例外往 `graph/nodes/` 洩漏——
translator-cli 本身的呼叫失敗不直接觸發 `retry_count` 迴圈（見 07a 十三
章），是否要重試、要不要把 task 標記失敗，由呼叫端（`implement_node.py`
／`scaffold_node.py`）決定。
"""
from __future__ import annotations


class TranslatorCliError(Exception):
    """所有 translator-cli 例外的共同基底類別。"""


class TranslatorCliNotGitRepoError(TranslatorCliError):
    """`python_project_path` 不是一個已初始化的 git repo（見 07a 二章
    「一次性前置準備」：目錄存在、`git init` 過、沒有任何 commit）。
    `generate_scaffold()` 執行前檢查，環境沒準備好，直接中止，不是可以
    自動補救的情況（見 07a 二章）。
    """


class TranslatorCliDirtyWorkingTreeError(TranslatorCliError):
    """`git status --porcelain` 非空（見 07a 九章「衝突偵測」）。
    `generate_scaffold()`／`fill_function()` 動筆寫任何檔案之前的
    precondition 檢查沒通過，不寫入任何內容，交由人工核對這份意外的
    變更是什麼——不重試，見 07a 十三章。
    """


class TranslatorCliScaffoldMismatchError(TranslatorCliError):
    """`fill_function()` 定位函式失敗（07a 六章步驟 1／4「scaffold/task
    不一致」）：`target_file` 不存在，或 `(class_name, function_name)`
    在該檔案裡找不到。這是防禦性的最後一道檢查——06a 已在上游用「05a
    對多載的消歧」＋「1:1 涵蓋率」兩道機制保證這個三元組理論上必然
    存在，這裡觸發代表更上游的資料有 bug，不重試，直接回報（見 07a
    六章、十三章）。
    """


class TranslatorCliNetworkError(TranslatorCliError):
    """跟 ollama（經 nginx）的連線失敗，對應 07a 七章「Timeout 與重試
    策略」表格的網路層重試耗盡（`ollama_client._call_ollama_once()`）。
    跟 `TranslatorCliModelOutputError`（模型有回應、但內容違反格式契約）
    是刻意分開的兩種錯誤語意——這裡代表根本沒收到可用回應，成因通常是
    另一台 Mac 的網路／nginx／ollama 服務本身有問題，不是模型輸出品質
    問題，混用會誤導事後排查方向（見 00 十章「錯誤訊息看起來像哪個
    Agent 的問題，根因其實在別處」這類風險）。不重試——網路層重試已經
    在 `_call_ollama_once()` 內部做過，這裡拋出代表重試已經耗盡。
    """


class TranslatorCliModelOutputError(TranslatorCliError):
    """ollama 回應違反格式契約，對應 07a 七章「模型輸出格式錯誤的修正
    重試」四種觸發情況：delimiter 抽取失敗、`body_text` 通不過
    `ast.parse()`、`body_text` 解析出的陳述式清單為空（六章步驟
    6a）、`body_text` 恰好是與目標函式同名的巢狀函式定義（六章步驟
    6b「模型重複輸出函式簽名」）。固定重試一次（`ollama_client.
    get_function_body()`），仍失敗才拋出，不無限重試。
    """


class TranslatorCliAssemblyError(TranslatorCliError):
    """`generate_scaffold()` 整檔組裝完成後的最終 `ast.parse()` 驗證
    失敗（07a 四章「語法驗證與寫入」步驟 4，或 Schema 定義段合併後的
    步驟 5）。代表組裝邏輯本身有 bug（如 import 合併沒去重乾淨），不是
    個別介面的型別字串問題——那種情況走 `skipped_interfaces`／
    `skipped_db_models` 隔離，不會走到這裡。不重試，直接中止（見 07a
    十三章）。
    """


class TranslatorCliConfigError(TranslatorCliError):
    """必要的環境變數缺失（`OLLAMA_BASE_URL`／`OLLAMA_API_KEY`，見 07a
    七章「連線方式」）。跟 `TranslatorCliNotGitRepoError` 是同一類「輸入
    端環境沒準備好，不是可以自動補救的情況」（見 07a 二章），只是分別
    對應 git 環境與 ollama 連線環境兩種前置準備。不重試——遺漏的環境
    變數不會因為重試而自己出現；`.env` 若漏了這兩個變數，這條 pipeline
    run 的第一個 task 就會踩到，需要人工補上環境變數後重跑。
    """
```

`TranslatorCliScaffoldMismatchError` 由 `client.py` 的 `_read_target_file()` 輔助函式與「`locate_function()` 找不到函式」兩處 `raise`，`fill_function()` 用 `except TranslatorCliError` 統一接住轉成 `FillResult(success=False, ...)`（見八章）。

---

## 二、`types.py`——輸出契約

對應 07a 四章「回傳契約」、五章「輸出契約」。`FillResult` 是 `fill_function()` 真正建構、回傳的物件（呼叫端用 `result.success` 屬性存取）；`ScaffoldResult`／`SkippedInterface`／`SkippedDbModel` 只是型別提示用的 TypedDict——`generate_scaffold()` 實際回傳相容的 plain dict，呼叫端（`scaffold_node.py`）用 `result["success"]` 字典存取。

**`PythonStructure`／`InterfaceSpec`／`ParamSpec` 是 `generate_scaffold()` 的輸入型別**，也定義在這裡，不從 `graph.state` 匯入——07a 十二章明訂「`translator_cli/` 套件不 import `graph.state`，維持跟 `refactor_harness/`／`spec_collection_agent/` 一致的獨立性」，因此比照 `FillResult`／`ScaffoldResult` 的既有作法，在 `translator_cli/types.py` 自己定義結構對齊 `graph/state.py` 同名 TypedDict 的版本。TypedDict 本質上只是結構化的 dict，呼叫端（`scaffold_node.py`）傳入的實際物件是 `graph.state.PythonStructure`，兩者欄位結構相容，不需要任何轉換就能直接傳入。

```python
# translator_cli/types.py
"""對外輸出契約，對應 07a 四、五章。`ScaffoldResult`／`SkippedInterface`／
`SkippedDbModel` 只是型別提示用的 TypedDict——`generate_scaffold()`
實際回傳相容的 plain dict（見 07a 四章「回傳契約」範例），呼叫端
（`scaffold_node.py`）用 `result["success"]` 這種字典存取，不強制建構
這個型別，這裡的 TypedDict 純粹讓型別檢查工具與閱讀者能對照欄位。

`FillResult` 則是 `fill_function()` 真正建構、回傳的物件（07a 五章
「輸出契約」），呼叫端用 `result.success` 屬性存取。

`ParamSpec`／`InterfaceSpec`／`PythonStructure` 是 `generate_scaffold()`
的輸入型別（07a 四章），結構對齊 `graph/state.py` 的同名 TypedDict，
但刻意在這裡重新定義、不 import `graph.state`（見 07a 十二章
「translator_cli 不依賴 RefactorState 其餘欄位」，維持跟
`refactor_harness/` 一致的獨立性）——TypedDict 只是結構化的 dict，
呼叫端傳入 `graph.state.PythonStructure` 的實際物件時兩者結構相容，
不需要轉換。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NotRequired, TypedDict


@dataclass
class FillResult:
    """對應 07a 五章「輸出契約」。`diff` 是這次 commit 的 git diff 全文，
    供人工／⑦ Debug Agent 事後追溯這次改了什麼（見 07a 八章）——不是
    `graph/scheduler.py` 的 regression 偵測依據，那邊只需要
    `target_files[0]` 這個路徑本身。
    """

    success: bool
    error: str | None = None
    diff: str = ""


class SkippedInterface(TypedDict):
    """對應 07a 四章「語法驗證與寫入」`skipped_interfaces` 單筆項目。"""

    file_path: str
    class_name: str | None
    function_name: str
    error: str


class SkippedDbModel(TypedDict):
    """對應 07a 四章「db_models」一節 `skipped_db_models` 單筆項目。"""

    file_path: str
    error: str


class ParamSpec(TypedDict):
    name: str
    type: str


class InterfaceSpec(TypedDict):
    file_path: str                 # 相對路徑，如 "app/repositories/user_repository.py"
    class_name: str | None
    function_name: str
    params: list[ParamSpec]
    return_type: str
    # 僅 routers 層 API 邊界方法非 None，見 graph/state.py 同名欄位註解。
    http_method: NotRequired[str | None]
    route_path: NotRequired[str | None]


class PythonStructure(TypedDict):
    directory_tree: str
    interfaces: list[InterfaceSpec]


class ScaffoldResult(TypedDict):
    """對應 07a 四章「回傳契約」。`success=True` 但
    `skipped_interfaces`／`skipped_db_models` 非空是預期中會發生、需要
    人工留意但不阻擋 pipeline 的情況，見該章節說明。
    """

    success: bool
    error: str | None
    skipped_interfaces: list[SkippedInterface]
    skipped_db_models: list[SkippedDbModel]
```

**已驗證**：`translator_cli` 整個套件在 `graph` 套件完全不可 import 的情況下（用 `sys.meta_path` 攔截）仍能正常 import 全部模組——證實這是套件層級、不是只有型別檢查層級的獨立性。

---

## 三、`python_adapter.py`——AST 定位／替換／渲染

對應 07a 六章「AST 插入機制」、十章「目標語言 Adapter 介面」。這是 `scaffold.py`／`client.py` 共同依賴的最底層模組，`extract_body_statements()` 是六章步驟 6／6a／6b 的唯一實作，被 `ollama_client.get_function_body()`（判斷是否需要觸發七章「修正重試」）與 `PythonAdapter.splice_body()`（實際替換）共用，避免兩處重複解析／檢查邏輯。

```python
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
```

**已驗證**（`tests/translator_cli/test_python_adapter.py`，14 個測試）：`locate_function()` 涵蓋自由函式／類別方法／找不到 class／找不到方法四種情況；`extract_body_statements()` 涵蓋正常案例、語法錯誤、空 body、簽名重複輸出、以及「巢狀函式但名稱不同不誤觸發」的邊界案例；`splice_body()` 涵蓋成功替換與失敗時不異動原節點兩種情況。

---

## 四、`git_ops.py`——git snapshot、commit、衝突偵測

對應 07a 八、九章。

```python
# translator_cli/git_ops.py
"""git snapshot、commit 顆粒度、衝突偵測，對應 07a 八、九章。所有 git
指令一律以呼叫端傳入的 `python_project_path` 為 repo 根目錄
（`git -C {python_project_path} ...`），不是 translator-cli 自己執行時
的 cwd（見 07a 八章）。用列表形式呼叫 `subprocess.run`（不經 shell），
比照 `refactor_harness/core/postman_runner.py` 既有的 `run_newman()`
寫法，跨平台一致（見 01 八章「跨平台注意事項」`shell=False` 建議）。

**不需要鎖機制**：見 07a 八章「決策：每個 task 一個 commit，不需要鎖
機制」——`MODEL_SEMAPHORE(1)`（`implement_node.py`）與 scaffold/implement
的圖結構先後順序，已經從結構上保證任何時刻最多一個呼叫端在寫入這個
git repo，這裡不重複實作一層檔案鎖。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from translator_cli.exceptions import TranslatorCliDirtyWorkingTreeError, TranslatorCliError, TranslatorCliNotGitRepoError


def _run_git(python_project_path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", python_project_path, *args], capture_output=True, text=True, encoding="utf-8"
    )


def ensure_git_repo(python_project_path: str) -> None:
    """對應 07a 二章「一次性前置準備」：`generate_scaffold()` 執行前
    檢查目標目錄是不是一個 git repo，不是則直接中止並回報明確錯誤——
    這是輸入端環境沒準備好，不是可以自動補救的情況。

    不另外檢查目錄是否存在：`git -C {不存在的路徑}` 本身就會乾淨地
    失敗（`fatal: cannot change to '...': No such file or directory`，
    returncode 128），不需要在呼叫 git 之前自己先判斷一次，那只是重複
    git 已經會做的事。
    """
    result = _run_git(python_project_path, "rev-parse", "--is-inside-work-tree")
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise TranslatorCliNotGitRepoError(
            f"{python_project_path} 不是一個 git repo（見 07a 二章「一次性前置準備」，"
            f"需先在目標目錄執行 git init）：{result.stderr.strip()}"
        )


def check_clean_working_tree(python_project_path: str) -> None:
    """對應 07a 九章「衝突偵測」：`generate_scaffold()`／`fill_function()`
    動筆寫任何檔案之前的 precondition 檢查。輸出非空 → 不寫入任何內容，
    直接回傳失敗，交由人工核對這份意外的變更是什麼。
    """
    result = _run_git(python_project_path, "status", "--porcelain")
    if result.returncode != 0:
        # 指令本身跑不動（權限問題、repo 損毀等）跟「不是 git repo」是
        # 不同語意，不套用 TranslatorCliNotGitRepoError 這個字面說法，
        # 避免誤導排查方向（`ensure_git_repo()` 已經在更早的
        # precondition 檢查過一次是不是 git repo，這裡若還失敗代表狀況
        # 更複雜）。
        raise TranslatorCliError(f"git status 執行失敗：{result.stderr.strip()}")

    if result.stdout.strip():
        raise TranslatorCliDirtyWorkingTreeError(
            f"working tree 不乾淨，拒絕寫入（見 07a 九章）：{result.stdout.strip()}"
        )


def diff_for_file(python_project_path: str, target_file: str) -> str:
    """對應 07a 八章：在 `git add` 之前擷取，此時檔案已寫入磁碟但尚未
    進 staging area，working tree 依九章 precondition 保證動筆前是
    乾淨的，這個 diff 精確反映這次 `fill_function()` 造成的變更。
    """
    result = _run_git(python_project_path, "diff", "--", target_file)
    return result.stdout


def commit_scaffold(python_project_path: str) -> None:
    """對應 07a 八章：`generate_scaffold()` 成功寫入所有骨架檔案後的
    一次性 commit，作為後續所有 `fill_function()` commit 的共同基礎。
    """
    _run_git(python_project_path, "add", "-A")
    result = _run_git(python_project_path, "commit", "-m", "scaffold: initial skeleton from python_structure")
    if result.returncode != 0:
        raise TranslatorCliError(f"scaffold commit 失敗：{result.stderr.strip()}")


def commit_fill(
    python_project_path: str,
    *,
    task_id: str,
    target_file: str,
    class_name: str | None,
    function_name: str,
) -> None:
    """對應 07a 八章：只 add 這次實際寫入的那一個檔案，不用 `-A`——即使
    working tree 因為某種原因存在其他未預期的變更（理論上不該發生，見
    九章），也不會被這次 commit 意外一起帶走。`task_id` 是必要引數
    （見 07a 二章），保留 `class_name.function_name` 是為了讓 `git log`
    一眼看出這個 commit 改的是哪個函式。
    """
    label = f"{class_name}.{function_name}" if class_name else function_name
    _run_git(python_project_path, "add", target_file)
    result = _run_git(
        python_project_path, "commit", "-m", f"implement: {task_id} fill {label} in {target_file}"
    )
    if result.returncode != 0:
        raise TranslatorCliError(f"fill_function commit 失敗（task {task_id}）：{result.stderr.strip()}")


def discard_file_changes(python_project_path: str, target_file: str) -> bool:
    """`commit_fill()` 失敗後的復原路徑用（見 `client.fill_function()`）：
    把 `target_file` 還原回目前 HEAD 的內容，撤銷這次失敗的 commit 之前
    寫入磁碟的新內容。`git commit` 若因不可抗力（index lock、權限問題）
    失敗，`write_text()` 已經落地的變更會讓 working tree 卡在「不乾淨」
    狀態，之後每一次 `check_clean_working_tree()` precondition 檢查都會
    連帶失敗——這對這一個 task 而言是可歸因、已知成因的變更（就是我們
    自己剛寫入、但沒能進版控的那份內容），不是九章「衝突偵測」要攔的
    那種「來源不明的意外變更」，因此可以安全地自動撤銷，只把這一個
    task 標記失敗，不讓後續所有 task 被這次 commit 失敗拖累卡住。

    **`git reset` 必須先於 `git checkout`**：`commit_fill()` 失敗前已經
    執行過 `git add {target_file}`，內容已經寫進 index，不是只留在
    working tree——`git checkout -- <path>` 預設是「用 index 內容覆蓋
    working tree」，並不會動 index 本身，若 index 裡已經是這次沒能
    commit 成功的新內容，`checkout` 只會把 working tree 覆蓋回同一份
    「壞」內容，等於什麼都沒還原（已用真實 git 行為驗證過：`add` 後
    只執行 `checkout` 不執行 `reset`，working tree 內容完全不變）。
    `git reset -- <path>` 把 index 還原回目前 HEAD 的狀態（沒有 HEAD
    的情況下也能安全執行），之後 `checkout` 才能真正把 working tree
    也拉回 HEAD 的內容。

    回傳是否成功還原（透過檢查最終 `git status` 是否乾淨，而不是任一
    個別指令的 returncode）。
    """
    _run_git(python_project_path, "reset", "--", target_file)
    _run_git(python_project_path, "checkout", "--", target_file)
    status = _run_git(python_project_path, "status", "--porcelain", "--", target_file)
    return status.returncode == 0 and not status.stdout.strip()


def discard_written_files(python_project_path: str, file_paths: list[str]) -> bool:
    """`commit_scaffold()` 失敗（或 `write_files()` 寫到一半發生
    `OSError`）後的復原路徑用（見 `client.generate_scaffold()`），道理
    跟 `discard_file_changes()` 一致，只是骨架階段一次寫入多個檔案。
    `file_paths` 是 `scaffold.build_files()` 實際算出、這次真正打算
    寫入的檔案清單（`client.py` 呼叫時傳 `list(files.keys())`）——**只**
    對這批路徑執行 `reset`／`checkout`／`clean`，不對整個 repo 做無
    差別還原。

    **範圍刻意限縮到這次寫入的精確檔案清單，不是整個 repo**：
    `python_project_path` 依 07a 二章設計是 translator-cli 專屬 repo，
    但這個前提依賴 `.env` 的 `PYTHON_PROJECT_PATH` 設定正確——萬一設定
    錯誤、誤指到一個帶有其他重要內容的目錄，整個 repo 範圍的 `clean`
    會有無法挽回的風險。既然 `build_files()` 當下就已經精確知道這次要
    寫哪些檔案，只對這批路徑動作，不論 `python_project_path` 實際指向
    哪裡，這個函式的破壞範圍都嚴格限制在「這次呼叫自己打算寫入的
    檔案」，不依賴任何關於目錄內容的外部假設。

    **`git reset` 必須先於 `checkout`／`clean`**：`commit_scaffold()`
    失敗前已經執行過 `git add -A`，這批檔案的內容已經寫進 index——
    `checkout` 只會用 index 內容覆蓋 working tree，`clean -fd` 也只
    處理未追蹤的檔案，兩者都不會動 index 本身。若 index 裡已經是這次
    沒能 commit 成功的新內容，光靠 `checkout`＋`clean` 等於什麼都沒
    還原、index 依然停在「已 add」的髒狀態（已用真實 git 行為驗證
    過）。`git reset -- p1 p2 p3` 先把這批路徑的 index 還原回目前 HEAD
    的狀態——不論這個 repo 有沒有任何 commit，`git reset` 都能安全
    執行（沒有 HEAD 時等同「把這批路徑從 index 整個移除」），之後
    `checkout`／`clean` 才能真正發揮作用。`git reset` 對多個 pathspec
    不是「全有全無」（跟下面 `checkout` 的限制不同，已驗證混合「已
    追蹤＋從未追蹤」的路徑一次呼叫就能正確處理），不需要逐一呼叫。

    **`git checkout` 對多個 pathspec 是「全有全無」，逐一呼叫**：若把
    `file_paths` 一次全部傳給同一個 `git checkout -- p1 p2 p3` 指令，
    只要其中一個路徑是全新、從未被追蹤過的檔案（`git` 眼中「不存在的
    pathspec」），整個指令會直接失敗、**其餘合法路徑也不會被還原**
    （已用真實 git 行為驗證過，多路徑不是逐一嘗試、是整批放棄）。
    因此改成逐一對每個路徑呼叫 `checkout`，個別失敗（代表這個路徑是
    全新檔案、本來就沒有舊版本可還原）直接忽略，交給下一步 `git clean`
    處理；`git clean -fd` 支援多個 pathspec 一次處理、不需要逐一呼叫。

    **判斷成功與否看最終狀態，不是每個指令各自的 returncode**：`git
    status --porcelain -- p1 p2 p3` 把檢查範圍限定在這批路徑，只要
    這批路徑都乾淨（不論是成功還原、或是被 `clean` 移除）就算成功。

    **`clean` 之後額外呼叫 `_prune_empty_parent_dirs()`**：`git clean
    -fd` 只刪未追蹤的檔案本身，不處理它留下的空目錄——`write_files()`
    幫全新檔案建的巢狀目錄（`app/models/` 這類）被清空後會原樣留在
    磁碟上，`git status` 判定乾淨（空目錄本來就不受 git 追蹤），但磁碟
    狀態沒有完全回到執行前，這裡補一步清掉。
    """
    if not file_paths:
        return True

    _run_git(python_project_path, "reset", "--", *file_paths)
    for path in file_paths:
        _run_git(python_project_path, "checkout", "--", path)
    _run_git(python_project_path, "clean", "-fd", "--", *file_paths)
    _prune_empty_parent_dirs(python_project_path, file_paths)
    status = _run_git(python_project_path, "status", "--porcelain", "--", *file_paths)
    return status.returncode == 0 and not status.stdout.strip()


def _prune_empty_parent_dirs(python_project_path: str, file_paths: list[str]) -> None:
    """`write_files()` 用 `parent.mkdir(parents=True, exist_ok=True)`
    幫全新檔案建立巢狀目錄（見 `scaffold.write_files()` docstring）；
    `git clean -fd` 只刪未追蹤的檔案本身，不處理它留下的空目錄（已用
    真實 git 行為驗證過：對單一檔案路徑執行 `clean -fd` 後，該檔案剛
    建立的父目錄若因此變空，會原樣留在磁碟上）。這裡對每個 file_path
    的父目錄逐層往上嘗試 `rmdir()`，只在目錄確實是空的才會成功、遇到
    非空或不存在就直接停止（`OSError` 忽略），並且不會往上超出
    `python_project_path` 這個 repo 根目錄，避免動到 rollback 範圍以外
    的任何東西。
    """
    root = Path(python_project_path).resolve()
    for file_path in file_paths:
        parent = (root / file_path).parent.resolve()
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
```

**rollback 不違反「衝突偵測不自動化解」的原則**：07a 九章要防的是「來源不明的意外變更」（人工手動改了什麼、上一輪執行中途被強制中斷）；`discard_file_changes()`／`discard_written_files()` 撤銷的是**這次呼叫自己造成、成因完全已知**的變更（就是剛寫入但沒 commit 成功的內容），可歸因、可安全撤銷，兩者是不同類別的「dirty」。若不這麼做，一次 `git commit` 失敗（index lock、權限問題）就會讓 working tree 卡在「不乾淨」，之後每一次呼叫的 precondition 檢查都會連帶失敗，等同整條 pipeline 卡死。

**已驗證**（`tests/translator_cli/test_git_ops.py`，19 個測試，全部對真實 `tmp_path` git repo 執行，不 mock `subprocess`）：目錄不存在／非 git repo／乾淨 repo 三種 `ensure_git_repo()` 情況；乾淨／有 untracked 檔案兩種 `check_clean_working_tree()` 情況、**`git status` 指令本身失敗時（用損毀的 `.git/index` 觸發，`rev-parse` 仍會成功）拋出基底 `TranslatorCliError`，不是語意不準確的 `TranslatorCliNotGitRepoError`**；`commit_scaffold()`／`commit_fill()` 的 commit message 與範圍；`discard_file_changes()`／`discard_written_files()` 涵蓋一般還原、初次骨架（repo 尚無任何 commit）、覆蓋既有骨架、空清單 no-op、範圍精準（清單外的「重要未追蹤檔案」完全不受影響）、**全新巢狀目錄（`app/models/`）被清空後空目錄本身也一併清除、一路往上到 repo 根目錄**、**父目錄底下還有清單以外的其他既有檔案時不會被連帶刪除**——以及**檔案已經被 `git add` 進 index 但 commit 本身失敗**這個真實時序下仍能正確還原（`git checkout`／`clean` 都不會動 index，沒有先 `reset` 的話這個情境下的 rollback 會完全失效，見 `discard_file_changes()`／`discard_written_files()` docstring）。

---

## 五、`prompts.py`——system／user prompt 模板

對應 07a 七章「System / User Prompt 組裝」。

```python
# translator_cli/prompts.py
"""system／user prompt 模板，對應 07a 七章「System / User Prompt 組裝」。
system prompt 固定不變（只定義輸出格式契約，不含業務內容），user prompt
依每次呼叫組裝。
"""
from __future__ import annotations

BODY_START = "<<<TRANSLATOR_CLI_BODY_START>>>"
BODY_END = "<<<TRANSLATOR_CLI_BODY_END>>>"

SYSTEM_PROMPT = f"""你是一個 Python 程式碼填空工具。你會收到一個已經定義好簽名的 Python 函式（骨架已建好，
可能是空的 `pass`，也可能已有既有邏輯），以及這個函式應該做什麼的描述。

你的任務：只回傳這個函式的「本體」陳述式，不要重複函式簽名、不要重複裝飾器、不要加任何
解說文字。也不要在程式碼裡用 `#` 寫任何註解——這個工具用 AST 處理輸出，`#` 註解一律會被
直接丟棄，不會出現在最終寫入的檔案裡，寫了也是白費。回傳格式固定如下，只在這兩個標記之間
寫程式碼，標記本身也要原樣附上：

{BODY_START}
（函式本體陳述式，視為第一層縮排，不要自己加縮排）
{BODY_END}
"""


def build_user_prompt(
    *,
    current_signature: str,
    description: str,
    context: str,
    context_files: list[tuple[str, str]],
    error_feedback: str | None = None,
) -> str:
    """對應 07a 七章「User prompt 組裝內容」：函式目前的簽名（不含
    body）、`description`、`context`（若非空）、`context_files` 逐檔案
    列出「路徑 + 完整內容」。`error_feedback` 非 `None` 時，額外附上
    七章「模型輸出格式錯誤的修正重試」規定的錯誤回饋文字。
    """
    parts = [
        f"函式目前的簽名：\n```python\n{current_signature}\n```",
        f"這個函式該做什麼：\n{description}",
    ]
    if context:
        parts.append(f"補充說明：\n{context}")
    if context_files:
        file_blocks = "\n\n".join(f"### {path}\n```python\n{content}\n```" for path, content in context_files)
        parts.append(f"相關檔案內容：\n\n{file_blocks}")
    if error_feedback:
        parts.append(
            f"上一次回應違反格式／語法錯誤，錯誤訊息是：{error_feedback}，請重新產生，務必遵守 delimiter 格式"
        )
    return "\n\n".join(parts)
```

`BODY_START`／`BODY_END` 定義在這裡、被 `ollama_client.py` import，避免 delimiter 字面字串在兩個檔案裡各自寫一份、日後改格式漏改其中一處。

---

## 六、`ollama_client.py`——ollama 連線與重試

對應 07a 七章全節。

```python
# translator_cli/ollama_client.py
"""ollama 連線與請求契約，對應 07a 七章。translator-cli 不直接打
ollama，而是打 `.env` 的 `OLLAMA_BASE_URL`（指向另一台 Mac 上的
nginx，已含 `/v1` 路徑前綴），帶 `Authorization: Bearer
{OLLAMA_API_KEY}`（見 00 三、四、五章已定案的架構）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

from translator_cli import python_adapter
from translator_cli.exceptions import (
    TranslatorCliConfigError,
    TranslatorCliModelOutputError,
    TranslatorCliNetworkError,
)
from translator_cli.prompts import BODY_END, BODY_START, SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

# 00 三章「硬體限制」已把模型選型釘死為單一選擇，不經環境變數——跟
# common/llm_client.py 對 Claude 模型「呼叫端自己決定用哪個模型」的
# 分工原則刻意不同（見 07a 七章「連線方式」）。
OLLAMA_MODEL = "qwen2.5-coder:32b"

# 07a 七章「Timeout 與重試策略」表格，環境變數可調、有預設值。
TRANSLATOR_CLI_TIMEOUT_SECONDS = float(os.environ.get("TRANSLATOR_CLI_TIMEOUT_SECONDS", "300"))
TRANSLATOR_CLI_NETWORK_RETRIES = int(os.environ.get("TRANSLATOR_CLI_NETWORK_RETRIES", "2"))
_NETWORK_RETRY_DELAY_SECONDS = 3.0
# delimiter／語法驗證失敗重試固定 2 次，跟網路層重試是不同的錯誤類型、
# 不同的重試機制，不共用同一個計數器（見 07a 七章）。原本固定 1 次，
# 真實環境對真實 qwen2.5-coder:32b 實測時發現：對複雜任務（context 檔案
# 多、邏輯複雜），qwen 第一次回應違反 delimiter 格式的機率不低，固定
# 重試 1 次不一定夠——調成 2 次仍是有界重試，不是無限重試。
_FORMAT_RETRY_COUNT = 2

_DELIMITER_RE = re.compile(re.escape(BODY_START) + r"\n?(.*?)\n?" + re.escape(BODY_END), re.DOTALL)


def _extract_delimited_body(raw_text: str) -> str:
    """對應 07a 六章「delimiter 契約：模型回傳格式」抽取邏輯：正則找
    兩個 sentinel 之間的文字。找不到 → 視為「delimiter 契約違反」，
    觸發七章重試策略（見 `get_function_body()`）。
    """
    match = _DELIMITER_RE.search(raw_text)
    if not match:
        raise TranslatorCliModelOutputError(
            f"delimiter 契約違反：模型回應找不到 {BODY_START} / {BODY_END} 標記"
        )
    return match.group(1)


async def _call_ollama_once(system_prompt: str, user_prompt: str) -> str:
    """對應 07a 七章「Timeout 與重試策略」表格：HTTP 連線失敗／timeout
    這類傳輸層錯誤（`httpx.TransportError`／`httpx.TimeoutException`），
    固定間隔 3 秒立即重試 `TRANSLATOR_CLI_NETWORK_RETRIES` 次——這是 LAN
    內部連線的暫時抖動，不是 Claude API 那種需要等配額恢復的限流情境，
    不需要 5 分鐘等待。**`httpx.HTTPStatusError`（收到回應，但狀態碼是
    4xx／5xx）刻意不算進這個重試迴圈**：07a 明確把重試範圍限定在傳輸層
    錯誤，HTTP 狀態碼錯誤是不同性質——例如 nginx token 設錯導致每次都
    回 401，這是必然失敗、重試沒有意義的情況，立即失敗回報，不拖慢
    失敗訊號的傳遞速度。

    `httpx.AsyncClient` 建立在重試迴圈外、只建立一次：同一次呼叫的多次
    網路層重試是短時間內（間隔 `_NETWORK_RETRY_DELAY_SECONDS` 秒）對同
    一個 `base_url` 的連續嘗試，沒有理由每次重試都重新握手 TCP／TLS。
    這個 client 是單一函式呼叫內的區域資源、隨 `async with` 結束自動
    釋放，不跨呼叫、不跨時間共用——`implement_node.py` 的
    `MODEL_SEMAPHORE(1)` 已經把這條路徑序列化，不存在真正的併發連線
    需求，因此不做跨呼叫的 client 重用（module-level singleton）。
    """
    try:
        base_url = os.environ["OLLAMA_BASE_URL"]
        api_key = os.environ["OLLAMA_API_KEY"]
    except KeyError as exc:
        raise TranslatorCliConfigError(
            f"缺少必要的環境變數 {exc}（見 07a 七章「連線方式」，需在 .env 設定 OLLAMA_BASE_URL／OLLAMA_API_KEY）"
        ) from exc

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=TRANSLATOR_CLI_TIMEOUT_SECONDS) as client:
        for attempt in range(TRANSLATOR_CLI_NETWORK_RETRIES + 1):
            try:
                response = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # 有收到回應，只是狀態碼是錯誤——立即失敗，不進重試迴圈。
                raise TranslatorCliNetworkError(
                    f"ollama 回應 HTTP 錯誤狀態碼（非傳輸層錯誤，不重試）：{exc}"
                ) from exc
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt < TRANSLATOR_CLI_NETWORK_RETRIES:
                    logger.warning(
                        "ollama 連線失敗（第 %d/%d 次），%.0f 秒後重試：%s",
                        attempt + 1,
                        TRANSLATOR_CLI_NETWORK_RETRIES,
                        _NETWORK_RETRY_DELAY_SECONDS,
                        exc,
                    )
                    await asyncio.sleep(_NETWORK_RETRY_DELAY_SECONDS)
                continue

            data = response.json()
            return data["choices"][0]["message"]["content"]

    raise TranslatorCliNetworkError(
        f"ollama 連線失敗，已重試 {TRANSLATOR_CLI_NETWORK_RETRIES} 次仍失敗：{last_error}"
    )


async def get_function_body(
    *,
    current_signature: str,
    description: str,
    context: str,
    context_files: list[tuple[str, str]],
    function_name: str,
) -> str:
    """對外唯一入口，對應 07a 七章「模型輸出格式錯誤的修正重試」。

    固定重試 2 次（`_FORMAT_RETRY_COUNT`）：每次失敗後，下一次呼叫的
    user prompt 額外附上「上一次回應違反格式／語法錯誤訊息...請重新
    產生」（見 `prompts.build_user_prompt()` 的 `error_feedback`
    引數）。四種觸發情況統一由 `_extract_delimited_body()`（delimiter
    抽取）與 `python_adapter.extract_body_statements()`（語法／空
    body／簽名重複輸出）覆蓋，這裡只驗證、不使用回傳的陳述式清單——
    真正的替換發生在 `PythonAdapter.splice_body()`，這裡的驗證只是為了
    決定要不要觸發重試，兩處共用同一份 `extract_body_statements()`
    邏輯，避免重複實作。

    回傳已驗證過的 `body_source` 原始文字（未縮排陳述式）；固定重試
    2 次仍失敗，拋出 `TranslatorCliModelOutputError`，交由呼叫端
    （`client.fill_function()`）轉成 `FillResult(success=False, ...)`。
    """
    error_feedback: str | None = None
    last_error: Exception | None = None

    for attempt in range(_FORMAT_RETRY_COUNT + 1):
        user_prompt = build_user_prompt(
            current_signature=current_signature,
            description=description,
            context=context,
            context_files=context_files,
            error_feedback=error_feedback,
        )
        raw_text = await _call_ollama_once(SYSTEM_PROMPT, user_prompt)

        try:
            body_source = _extract_delimited_body(raw_text)
            python_adapter.extract_body_statements(body_source, function_name)
            return body_source
        except TranslatorCliModelOutputError as exc:
            last_error = exc
            error_feedback = str(exc)
            if attempt < _FORMAT_RETRY_COUNT:
                logger.warning("模型輸出格式錯誤（第 %d 次），重新呼叫一次修正：%s", attempt + 1, exc)
                continue

    raise TranslatorCliModelOutputError(f"模型輸出格式錯誤，修正重試仍失敗：{last_error}")
```

**已驗證**（`tests/translator_cli/test_ollama_client.py`，13 個測試）：`_extract_delimited_body()` 成功／找不到 delimiter；`get_function_body()` 首次成功、格式錯誤重試一次後成功（含空 body）、**兩次重試都失敗、第三次（第二次重試）才成功**、兩次重試都用盡仍失敗放棄；`_call_ollama_once()` 網路層重試成功、耗盡重試次數仍失敗時拋出 `TranslatorCliNetworkError`（不是 `TranslatorCliModelOutputError`——見一章「為什麼分開」）、`httpx.HTTPStatusError`（模擬 401）立即失敗、不進重試迴圈（`call_count` 驗證只呼叫一次）、payload 明確帶 `"stream": False`、環境變數缺失轉成 `TranslatorCliConfigError`、同一次呼叫的多次重試只建立一個 `httpx.AsyncClient` 實例（`monkeypatch` 假造 `httpx.AsyncClient`／`_call_ollama_once`，不需要真的連線）。

---

## 七、`scaffold.py`——骨架生成模式

對應 07a 四章全節。

```python
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
```

**已驗證**（`tests/translator_cli/test_scaffold.py`，29 個測試）：型別正規化、code block 擷取、Schema 定義段合併（import 去重、`from __future__` 只留一次且放最前面、正確處理裸 `import xxx`／PEP 8 多行 import、機械註解不被 `ast.unparse()` 弄丟）、函式片段渲染（routers 邊界方法的裝飾器、class 方法的 `self` 前綴）、兩層 import 解析（已知關鍵字、AST 自訂型別、都比對不到時不加、自訂型別名稱剛好包含關鍵字子字串時不誤觸發（`_keyword_hits()` 單字邊界比對）、`Callable[[int, str], bool]` 雙層中括號仍正確命中）；`build_files()` 端對端測試涵蓋正常情況、單一介面型別字串非法時被隔離跳過、`file_path` 不在三層目錄底下時記進 `skipped_interfaces`（unknown layer，不靜默丟棄）、Schema 定義段本身損毀時整體中止；`write_files()` 驗證會自動建立巢狀目錄。**`_scan_project_custom_types()`／`resolve_body_imports()`／`insert_import_lines()`（填空模式本體 import 解析）**：掃描結果跨 routers／services／repositories 層都找得到、單一檔案語法錯誤不拖累其他檔案、`app/` 目錄不存在時回傳空索引；本體引用已知關鍵字（`HTTPException`）與跨檔案自訂類別（`UserRepository`）都正確解析、函式參數與 `self` 不誤判成缺 import、Python builtin 不誤判、本體內部區域變數賦值不誤判、已經 import 過的名稱不重複加、兩層都比對不到時保守不加（不猜）；插入位置正確接在既有 import 區塊（含模組 docstring）之後、空清單是 no-op。

---

## 八、`client.py`——對外唯一入口

對應 07a 二、四、五章，串接以上模組。`generate_scaffold()`／`fill_function()` 內部所有子行程、磁碟呼叫都用 `asyncio.to_thread()` 包裝，理由見下方模組 docstring。

```python
# translator_cli/client.py
"""translator-cli 對外唯一入口，對應 07a 二章全節：`generate_scaffold()`
（骨架生成模式，④呼叫）與 `fill_function()`（填空模式，⑤呼叫）。兩者
都是 `python_project_path` 顯式引數（見 07a 二章「python_project_path
是顯式引數」）——不讀 `os.environ["PYTHON_PROJECT_PATH"]`，也不 import
`graph.state`，維持跟 `refactor_harness/`／`spec_collection_agent/`
一致的獨立性（見 07a 十二章）。`PythonStructure` 型別定義在
`translator_cli/types.py`（結構對齊 `graph/state.py` 同名 TypedDict，
呼叫端傳入的實際物件兩邊相容，見該檔案 docstring）。

**所有會碰觸子行程或磁碟的呼叫都包在 `asyncio.to_thread()` 裡**：
`git_ops.py`（`subprocess.run`）與 `Path.read_text()`／`write_text()`
本身是純同步 API，直接在 `async def` 函式裡呼叫會佔住 event loop。
`asyncio.to_thread()` 把這些呼叫丟到執行緒池執行，不需要把
`git_ops.py`／`scaffold.py` 本身改寫成 async（那兩個模組維持純同步、
易於獨立測試，只有這裡的呼叫端需要調整）。純 AST 記憶體操作
（`PythonAdapter` 的 `parse`／`locate_function`／`splice_body`／`render`／
`validate_syntax`）不在這個範圍——那些是對單一函式的操作，速度是微秒
等級，不是 I/O，包一層 `to_thread` 只會增加雜訊、換不到實質好處。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from translator_cli import formatting, git_ops, ollama_client, scaffold
from translator_cli.exceptions import (
    TranslatorCliConfigError,
    TranslatorCliError,
    TranslatorCliModelOutputError,
    TranslatorCliNetworkError,
    TranslatorCliScaffoldMismatchError,
)
from translator_cli.python_adapter import PythonAdapter
from translator_cli.types import FillResult, PythonStructure, ScaffoldResult

logger = logging.getLogger(__name__)


async def generate_scaffold(
    python_project_path: str,
    python_structure: PythonStructure,
    db_models: dict[str, str] | None = None,
) -> ScaffoldResult:
    """對應 07a 四章全節。同步、確定性（不呼叫本地模型，見四章
    「決策」）——`async def` 不是因為函式本身需要非同步邏輯，而是每一步
    子行程／磁碟操作都經 `asyncio.to_thread()` 丟到執行緒池（見本模組
    docstring），對外仍是 `await translator_cli.generate_scaffold(...)`
    這個跟 `fill_function()`／LangGraph node 一致的呼叫慣例。

    流程：
    1. precondition 檢查（git repo 存在＋working tree 乾淨，見二、九章）
    2. `scaffold.build_files()` 在記憶體中組裝並驗證全部檔案
    3. 全部驗證通過才呼叫 `scaffold.write_files()` 一次性寫入磁碟
    4. `git_ops.commit_scaffold()` 一次性 commit（見八章）——寫入或
       commit 任一步失敗都會嘗試 `git_ops.discard_written_files()` 只
       還原這次打算寫入的檔案清單（見下方「寫入／commit 失敗時的復原」）
    """
    try:
        await asyncio.to_thread(git_ops.ensure_git_repo, python_project_path)
        await asyncio.to_thread(git_ops.check_clean_working_tree, python_project_path)
    except TranslatorCliError as exc:
        return {"success": False, "error": str(exc), "skipped_interfaces": [], "skipped_db_models": []}

    try:
        files, skipped_interfaces, skipped_db_models = await asyncio.to_thread(
            scaffold.build_files, python_structure, db_models or {}
        )
    except TranslatorCliError as exc:
        return {"success": False, "error": str(exc), "skipped_interfaces": [], "skipped_db_models": []}

    # 寫入／commit 失敗時的復原：write_files() 可能因 OSError（磁碟空間
    # 不足、權限問題）中途失敗，commit_scaffold() 可能因 git 本身的問題
    # （index lock 等）失敗——不論哪一種，這一批檔案已經有部分或全部
    # 落地磁碟但沒能進版控，working tree 會卡在「不乾淨」狀態，讓下一次
    # 呼叫的 precondition 檢查連帶失敗。這批變更是這次呼叫自己造成、
    # 成因已知（就是我們剛寫入但沒 commit 成功的內容），不是九章「衝突
    # 偵測」要攔的「來源不明」情況，因此可以安全地自動撤銷，只把這次
    # 呼叫標記失敗，不讓 working tree 卡住之後所有呼叫。
    try:
        await asyncio.to_thread(scaffold.write_files, python_project_path, files)
        # 格式化（見 formatting.py）是硬性依賴：在 commit 之前跑，讓 commit
        # 進去的內容就是格式化後的版本，不是格式化前後兩次改動。格式化
        # 失敗（含找不到 ruff 執行檔）跟下面 write_files()／commit_scaffold()
        # 失敗走同一條 rollback 路徑——都已經落地磁碟但沒能進版控。
        await asyncio.to_thread(formatting.format_paths, python_project_path, *files.keys())
        await asyncio.to_thread(git_ops.commit_scaffold, python_project_path)
    except (OSError, TranslatorCliError) as exc:
        restored = await asyncio.to_thread(git_ops.discard_written_files, python_project_path, list(files.keys()))
        detail = "已還原這次寫入的檔案" if restored else "還原也失敗，working tree 可能仍不乾淨，需要人工介入核對"
        return {
            "success": False,
            "error": f"骨架寫入或 commit 失敗（{detail}）：{exc}",
            "skipped_interfaces": [],
            "skipped_db_models": [],
        }

    return {
        "success": True,
        "error": None,
        "skipped_interfaces": skipped_interfaces,
        "skipped_db_models": skipped_db_models,
    }


def _read_target_file(root: Path, target_file: str) -> str:
    """對應 07a 六章步驟 1：讀取 `fill_function()` 的目標檔案。
    `FileNotFoundError` 是「scaffold/task 不一致」這個特定語意（見六章
    步驟 4 同一類錯誤）；其餘 `OSError`（權限問題等）是另一類環境層級
    的失敗，訊息不套用「scaffold/task 不一致」這個字面說法，避免誤導
    排查方向——兩者都是 `TranslatorCliError` 子類別，呼叫端可以用同一個
    `except TranslatorCliError` 一併接住。
    """
    try:
        return (root / target_file).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise TranslatorCliScaffoldMismatchError(f"scaffold/task 不一致：{target_file} 不存在") from exc
    except OSError as exc:
        raise TranslatorCliError(f"讀取 {target_file} 失敗：{exc}") from exc


def _read_context_files(root: Path, context_files: list[str], *, task_id: str) -> list[tuple[str, str]]:
    """對應 07a 七章「`context_files` 讀取容錯」：`context_files[0]`
    （＝`target_file`）已在呼叫端（六章步驟 1）確認存在，這裡不會再
    踩到；其餘項目遇到 `FileNotFoundError` 記警告並跳過，不中斷整個
    `fill_function()` 呼叫——常見於沒有對應 DB 表的模組（純外部 API
    串接）或 `db_models` 因型別驗證失敗被跳過的情況。刻意只收斂到
    `FileNotFoundError`（比照 `_read_target_file()` 對這兩類錯誤的區分）：
    其餘 `OSError`（權限問題等）是環境層級的真實錯誤，不屬於 07a
    原文界定的「檔案不存在」容錯範圍，讓它往外傳、中止這次呼叫，避免
    靜默吞掉磁碟權限這類需要人工排查的問題。
    """
    resolved: list[tuple[str, str]] = []
    for rel_path in context_files:
        try:
            content = (root / rel_path).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            logger.warning(
                "task %s：context_files 讀取 %s 失敗，跳過（見 07a 七章「context_files 讀取容錯」）：%s",
                task_id,
                rel_path,
                exc,
            )
            continue
        resolved.append((rel_path, content))
    return resolved


async def fill_function(
    python_project_path: str,
    task_id: str,
    target_file: str,
    class_name: str | None,
    function_name: str,
    description: str,
    context: str,
    context_files: list[str],
) -> FillResult:
    """對應 07a 五、六、七章。呼叫失敗時不寫入任何內容（見五章「呼叫
    失敗時不寫入任何內容」）——每個失敗分支都在寫入磁碟之前 return，
    是九章「衝突偵測」成立的前提。
    """
    root = Path(python_project_path)

    try:
        await asyncio.to_thread(git_ops.check_clean_working_tree, python_project_path)
    except TranslatorCliError as exc:
        return FillResult(success=False, error=str(exc), diff="")

    adapter = PythonAdapter()
    try:
        source = await asyncio.to_thread(_read_target_file, root, target_file)
        tree = adapter.parse(source)
        node = adapter.locate_function(tree, class_name, function_name)
        if node is None:
            label = f"{class_name}.{function_name}" if class_name else function_name
            raise TranslatorCliScaffoldMismatchError(f"scaffold/task 不一致：{label} 在 {target_file} 找不到")
    except TranslatorCliError as exc:
        return FillResult(success=False, error=str(exc), diff="")

    current_signature = adapter.render_signature(node)
    try:
        resolved_context_files = await asyncio.to_thread(_read_context_files, root, context_files, task_id=task_id)
    except OSError as exc:
        # _read_context_files() 只吞 FileNotFoundError（見該函式 docstring）；
        # 其餘 OSError（權限問題等）是真實環境錯誤，這裡轉成
        # FillResult(success=False)，不讓原生例外洩漏擊穿合約。
        return FillResult(success=False, error=f"讀取 context_files 失敗：{exc}", diff="")

    try:
        body_source = await ollama_client.get_function_body(
            current_signature=current_signature,
            description=description,
            context=context,
            context_files=resolved_context_files,
            function_name=function_name,
        )
    except (TranslatorCliModelOutputError, TranslatorCliNetworkError, TranslatorCliConfigError) as exc:
        # TranslatorCliNetworkError（網路層重試耗盡）／TranslatorCliConfigError
        # （缺 OLLAMA_BASE_URL／OLLAMA_API_KEY，見
        # ollama_client._call_ollama_once()）都不會被 get_function_body()
        # 的格式重試迴圈攔截、會直接往外傳到這裡——這是刻意的：網路層
        # 錯誤與環境變數缺失都不該進「模型輸出格式錯誤」的重試邏輯，那
        # 救不了連線失敗或缺環境變數這兩件事，一樣轉成
        # FillResult(success=False) 讓這個 task 明確失敗、不讓例外洩漏
        # 擊穿合約。
        return FillResult(success=False, error=str(exc), diff="")

    try:
        adapter.splice_body(node, body_source)
    except TranslatorCliModelOutputError as exc:
        return FillResult(success=False, error=str(exc), diff="")

    # 填空模式的本體 import 解析（見 07a 五章）：node.body 現在是 qwen
    # 生成的本體，可能引用簽名以外的名稱（跨檔案自訂類別、框架例外），
    # 骨架階段的 import 解析看不到這些，這裡針對新本體重新掃一次補上。
    # 掃描專案磁碟找自訂型別索引是 I/O，包 to_thread；純 AST 插入不是。
    bound_names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
    if node.args.vararg:
        bound_names.add(node.args.vararg.arg)
    if node.args.kwarg:
        bound_names.add(node.args.kwarg.arg)
    missing_imports = await asyncio.to_thread(
        scaffold.resolve_body_imports, python_project_path, tree, node.body, bound_names
    )
    scaffold.insert_import_lines(tree, missing_imports)

    new_source = adapter.render(tree)
    try:
        adapter.validate_syntax(new_source)
    except SyntaxError as exc:
        return FillResult(success=False, error=f"寫入前最終語法驗證失敗（理論上不應發生）：{exc}", diff="")

    # 動筆寫入前重新檢查一次 working tree（見九章「衝突偵測」）：本地
    # 模型單次生成可能耗時數十秒到數分鐘（見 07a 七章），函式最開頭那次
    # check_clean_working_tree() 檢查的是「開始等模型回應之前」的狀態，
    # 這段漫長的 I/O 等待期間人工完全可能手動修改 target_file——若不
    # 在真正落筆前再檢查一次，這裡的 write_text() 會在人不知鬼不覺的
    # 情況下覆蓋掉那份人工修正，正是九章「衝突偵測」要防的事，不能只
    # 在函式入口做一次就視為全程有效。
    try:
        await asyncio.to_thread(git_ops.check_clean_working_tree, python_project_path)
    except TranslatorCliError as exc:
        return FillResult(success=False, error=str(exc), diff="")

    try:
        await asyncio.to_thread((root / target_file).write_text, new_source, encoding="utf-8")
    except OSError as exc:
        return FillResult(success=False, error=f"寫入 {target_file} 失敗：{exc}", diff="")

    # 格式化（見 formatting.py）是硬性依賴：在擷取 diff／commit 之前跑，
    # 讓 diff 與 commit 反映的都是格式化後的最終內容。格式化失敗（含
    # 找不到 ruff 執行檔）不允許半格式化的內容流入 commit，還原這次
    # 寫入——理由跟下方 commit 失敗時的復原邏輯一致。
    try:
        await asyncio.to_thread(formatting.format_paths, python_project_path, target_file)
    except TranslatorCliError as exc:
        restored = await asyncio.to_thread(git_ops.discard_file_changes, python_project_path, target_file)
        detail = "已還原該檔案" if restored else "還原也失敗，working tree 可能仍不乾淨，需要人工介入核對"
        return FillResult(success=False, error=f"格式化失敗（{detail}）：{exc}", diff="")

    diff = await asyncio.to_thread(git_ops.diff_for_file, python_project_path, target_file)

    if not diff:
        # 冪等（見 07a 五章「允許對已有內容的函式重新填空」）：LLM 這次
        # 生成的內容跟磁碟上已經 commit 的版本完全相同（例如 task 被
        # 重複排程），write_text() 寫入後 working tree 其實沒有任何
        # 變更——`git commit`（不帶 --allow-empty）遇到 staging area
        # 是空的會直接失敗（exit code 1），若照下面正常流程呼叫
        # commit_fill() 會把這個「實質上成功」的操作誤判成失敗、觸發不
        # 必要的 rollback。這裡提前偵測 diff 為空就直接視為成功，不必
        # 也不能 commit 一個空變更。
        logger.info("task %s：%s 內容與現有版本相同，視為冪等成功，不建立新 commit", task_id, target_file)
        return FillResult(success=True, error=None, diff="")

    # commit 失敗時的復原：write_text() 已經落地，但沒能進版控會讓
    # working tree 卡在「不乾淨」，之後每個 task 的 precondition 檢查
    # 都會連帶失敗。這是這次呼叫自己造成、成因已知的變更，可以安全地
    # 自動撤銷這一個檔案，只把這一個 task 標記失敗，不讓其餘 task 被
    # 這次 commit 失敗拖累卡住。
    try:
        await asyncio.to_thread(
            git_ops.commit_fill,
            python_project_path,
            task_id=task_id,
            target_file=target_file,
            class_name=class_name,
            function_name=function_name,
        )
    except TranslatorCliError as exc:
        restored = await asyncio.to_thread(git_ops.discard_file_changes, python_project_path, target_file)
        detail = "已還原該檔案" if restored else "還原也失敗，working tree 可能仍不乾淨，需要人工介入核對"
        return FillResult(success=False, error=f"commit 失敗（{detail}）：{exc}", diff="")

    return FillResult(success=True, error=None, diff=diff)
```

**已驗證**（`tests/translator_cli/test_client.py`，19 個測試，真實 `tmp_path` git repo＋`monkeypatch` 假造 ollama／git／磁碟呼叫）：`generate_scaffold()` 涵蓋非 git repo／working tree 不乾淨兩種 precondition 失敗、正常寫入＋commit、commit 失敗時自動 rollback、`write_files()` 中途 `OSError` 時同樣自動 rollback；`fill_function()` 端對端涵蓋成功填空並正確 commit、**填入的本體引用簽名以外的名稱（跨檔案自訂類別＋框架例外）時正確補上 import、寫入內容語法合法**、**同一個 task 重複呼叫、LLM 生成內容與現有版本完全相同時視為冪等成功、不建立新 commit（見 07a 五章「冪等」，`git commit` 不帶 `--allow-empty` 遇到空 staging area 原本會誤判成失敗）**、`target_file` 不存在、函式在骨架裡找不到、`context_files` 缺少非首項檔案不中斷、`context_files` 遇到 `PermissionError` 等真實 `OSError`（非 `FileNotFoundError`）時轉成 `FillResult(success=False)` 而不是往外洩漏、working tree 不乾淨時拒絕寫入、模型生成期間 working tree 才被人工改動的競態情境、commit 失敗時自動 rollback、格式化失敗時自動 rollback（走 `discard_file_changes()`，不是 commit 失敗那條路徑）、`write_text()` 拋出 `OSError`、環境變數缺失轉成 `FillResult(success=False)` 而不是原生例外洩漏。整個檔案用 autouse fixture 把 `formatting.format_paths()` stub 成 no-op（測試環境本身沒裝 `ruff`）——`ruff` 本身找不到／執行失敗這條路徑由 `test_formatting.py` 獨立驗證，這裡只測 `client.py` 收到格式化失敗訊號後的串接邏輯。

### `formatting.py`

`PythonAdapter.render()`（見三章）的 `ast.unparse()` 輸出語法合法，但不保證符合 PEP8／專案 lint 規則。`formatting.py` 在寫入完成、commit 之前依序呼叫 `ruff check --select I --fix`（import 排序）與 `ruff format`（其餘排版）把輸出正規化——`ruff` 是硬性依賴：任一步找不到執行檔或本身失敗都直接拋出，不吞掉。

**為什麼不是 best-effort**：07a 六章「`ast.unparse()` 會重新格式化整個檔案」這個承諾（風格天生一致，`git diff` 只集中在被填的那個函式）建立在「每次寫入都經過同一條格式化流程」的前提上。若格式化只在部分環境／部分次執行才生效，這個前提就不成立——`scaffold` 若在有裝 `ruff` 的機器上先跑過一次，之後任何一次 `fill_function()` 在沒有 `ruff` 的環境執行，那次的 diff 就會整檔改形（引號慣例、空行數量等），07a 承諾的「diff 範圍必須乾淨」隨之失效。因此格式化失敗一律中止該次呼叫、觸發 rollback（見八章 `generate_scaffold()`／`fill_function()`），寧可讓 task 失敗交給排程器重試，也不讓不一致的格式風格污染 git 歷史。

**`ruff format` 不處理 import 排序**：`ruff format`（格式器）只管排版（引號、空行、縮排），不含 isort 功能——`scaffold._merge_schema_blocks()`（四章）合併多個 Schema 定義段的 import 陳述式時只用清單／字典去重，只保證語法合法與不重複，不保證排序符合慣例。因此格式化流程先跑 `ruff check --select I --fix`（isort 對應的 lint 規則，開 `--fix` 自動排序整理），再跑 `ruff format` 正規化其餘排版。

**只解決格式風格，不解決註解流失**：`ast.parse()`／`ast.unparse()` 這個 translator-cli 核心機制本身不保留任何註解（Python 標準庫 `ast` 模組不把註解當成語法樹的一部分）——LLM 生成的函式本體若含有註解，在 `ollama_client.get_function_body()` 回傳的 `body_source` 被 `extract_body_statements()`（`python_adapter.py`）解析成陳述式清單的那一刻就已經遺失，這是比「寫入之後才格式化」更早的步驟，`ruff format` 救不回已經不存在的東西，它整理的是「已經沒有註解的程式碼」的排版。`prompts.py` 的 `SYSTEM_PROMPT` 明確告知模型不要寫 `#` 註解（見五章），從源頭省下模型生成註解的 token，但這是成本優化，不是解法——根本限制仍在。

```python
# translator_cli/formatting.py
"""寫入後的格式化，統一用 `ruff check --select I --fix`＋`ruff format`
正規化 `PythonAdapter.render()`（見 `python_adapter.py`）產出的內容。
`ast.unparse()` 保證語法合法，但不保證符合 PEP8／專案 lint 規則（引號
慣例、空行數量、import 排序等）。

**`ruff format` 不處理 import 排序**：`ruff format`（格式器）只管排版
（引號、空行、縮排），不含 isort 功能——`scaffold._merge_schema_blocks()`
合併多個 Schema 定義段的 import 陳述式時用清單／字典去重，只保證語法
合法與不重複，不保證排序符合慣例。因此先跑一次
`ruff check --select I --fix`（isort 對應的 lint 規則，開 `--fix` 自動
排序整理），再跑 `ruff format` 正規化其餘排版——兩者職責不同，缺一個
都無法達到「風格完全一致」的目標。

**`ruff` 是硬性依賴，不是 best-effort**：07a 六章「`ast.unparse()` 會
重新格式化整個檔案」承諾「格式風格天生一致」，讓 `git diff` 只集中在
被填的那個函式——這個承諾建立在「每次寫入都經過同一條格式化流程」
這個前提上。若格式化只在部分環境／部分次執行才生效，這個前提就不成
立：`fill_function()` 每次都會先用 `ast.unparse()` 把整個目標檔案重新
序列化（例如把雙引號改成 `ast.unparse()` 固定使用的單引號、壓縮多餘
空行），若這次執行剛好沒有 `ruff` 可以把格式排回去，這個「整檔改形」
會原樣寫入 diff／commit，讓 07a 承諾的「diff 範圍必須乾淨」失效——
`scaffold` 若在有裝 `ruff` 的機器上先跑過一次，之後只要有任何一次
`fill_function()` 在沒有 `ruff` 的環境執行，那次的 diff 就會整檔改形。

因此找不到 `ruff` 執行檔、或 `ruff format` 本身執行失敗，都直接拋出
`TranslatorCliError` 中止這次呼叫（由 `client.py` 轉成
`FillResult(success=False)`／`ScaffoldResult` 失敗並觸發 rollback），
不允許「這次沒排版」的檔案流入 commit——寧可讓這個 task 失敗、交給
排程器記一次失敗重試，也不要讓不一致的格式風格污染 git 歷史。
"""
from __future__ import annotations

import subprocess

from translator_cli.exceptions import TranslatorCliError


def _run_ruff(python_project_path: str, args: list[str]) -> None:
    """執行單一 ruff 指令，找不到執行檔或指令本身失敗都拋出
    `TranslatorCliError`，不吞掉。
    """
    try:
        result = subprocess.run(
            ["ruff", *args],
            cwd=python_project_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise TranslatorCliError(
            "找不到 ruff 執行檔——ruff 是硬性依賴（見 07b 文件「formatting.py」），"
            "請先 `pip install ruff`（或 `pip install -r requirements.txt`）"
        ) from exc

    if result.returncode != 0:
        raise TranslatorCliError(f"ruff {' '.join(args)} 執行失敗：{result.stderr.strip()}")


def format_paths(python_project_path: str, *relative_paths: str) -> None:
    """對 `python_project_path` 底下的一或多個相對路徑（檔案，通常是
    `scaffold.write_files()`／`fill_function()` 剛寫入的那批路徑）依序
    呼叫 `ruff check --select I --fix`（import 排序）與 `ruff format`
    （其餘排版），就地格式化。任一步找不到 `ruff` 執行檔、或本身失敗
    都拋出 `TranslatorCliError`，不吞掉——呼叫端需要這個訊號才能觸發
    rollback，避免半格式化的內容進入 commit。
    """
    if not relative_paths:
        return
    _run_ruff(python_project_path, ["check", "--select", "I", "--fix", *relative_paths])
    _run_ruff(python_project_path, ["format", *relative_paths])
```

`requirements.txt` 新增一行 `ruff>=0.8.0`——執行環境沒有安裝 `ruff` 時，`generate_scaffold()`／`fill_function()` 每次呼叫都會在格式化步驟失敗並觸發 rollback，不會有「跑起來但格式沒套用」的中間狀態。

**已驗證**（`tests/translator_cli/test_formatting.py`，5 個測試）：空路徑清單是 no-op、不呼叫子行程；`ruff` 執行檔不存在時拋出 `TranslatorCliError`（測試環境本身沒裝 `ruff`，天然驗證這條路徑）；`ruff check --select I --fix`（import 排序）本身失敗時拋出；`ruff format` 本身失敗時拋出；成功時兩步依序都執行、不拋出，且組出正確的指令（`["ruff", "check", "--select", "I", "--fix", ...]` 接著 `["ruff", "format", ...]`）。`client.py` 收到這個例外後的 rollback 串接邏輯另見八章 `test_client.py` 的 `test_fill_function_rolls_back_on_format_failure`。

---

## 九、與既有程式碼的介面異動

對應 07a 二章「與既有程式碼的介面異動」列出的四項，全部已套用：

### 1. `graph/state.py`：新增 `python_project_path` 欄位

```python
class RefactorState(TypedDict):
    # 進入點輸入（main.py 組裝 initial_state 時填入，見九）
    java_project_path: str
    # translator-cli 寫入目標的 Python 專案根目錄，對應 .env 的
    # PYTHON_PROJECT_PATH（見 07a_translator_cli_architecture.md 二章
    # 「新輸入：python_project_path」）——與 refactor-project/、
    # java_project_path 同層、各自獨立的 git repo，scaffold_node.py／
    # implement_node.py 呼叫 translator_cli.generate_scaffold()／
    # fill_function() 時顯式傳入，translator_cli 本身不 import
    # graph.state（見 07a 十二章）。
    python_project_path: str
    ...
```

### 2. `main.py`：`initial_state` 新增對應欄位

```python
initial_state = {
    "java_project_path": os.environ["JAVA_PROJECT_PATH"],  # ① 解析 Agent 讀取用，見 00 五章「環境建立」
    "python_project_path": os.environ["PYTHON_PROJECT_PATH"],  # translator-cli 寫入目標，見 07a 二章
    ...
}
```

**需要人工在 `.env` 補上 `PYTHON_PROJECT_PATH`**：`.env` 屬於敏感設定檔，本次實作不代為修改（見 CLAUDE.md 紅線規則）。比照既有 `JAVA_PROJECT_PATH` 的既有慣例，在 `.env` 新增一行（相對或絕對路徑皆可，見 07a 二章）：

```
PYTHON_PROJECT_PATH=../python-target-project
```

且目標目錄需完成 07a 二章「一次性前置準備」：目錄存在、`git init` 過、沒有任何 commit（`generate_scaffold()` 執行前會用 `git_ops.ensure_git_repo()` 檢查，沒準備好會直接回報明確錯誤）。

### 3. `graph/nodes/scaffold_node.py`：接上 `generate_scaffold()`

```python
"""
④ 骨架實作 Agent（translator-cli，骨架生成模式）
依 Agent ③ 的目錄結構與 interface 定義，建立目錄、base class、router 骨架、config
見 07a_translator_cli_architecture.md、07b_translator_cli_code.md

平行分支 node：與 [P] Plan Agent 同以 record_tests／design 為共同前驅
（見 01 五章），只回傳自己的 key，不展開 state。
"""
import logging

from graph.state import RefactorState
from translator_cli import client as translator_cli

logger = logging.getLogger(__name__)


async def run(state: RefactorState) -> dict:
    # db_models 由④自行從既有 DB 或 Java entity 原始碼取得（見 07a 四章
    # 「db_models」一節），這部分屬於 08a（骨架實作 Agent 詳細設計，待
    # 建立）的範圍——07b 只接上 generate_scaffold() 本身，db_models 暫時
    # 固定傳 None，等 08a 落地時再補上真正的字典（見 07a 十二章）。
    result = await translator_cli.generate_scaffold(
        python_project_path=state["python_project_path"],
        python_structure=state["python_structure"],
        db_models=None,
    )

    # result["skipped_interfaces"]／["skipped_db_models"]（見 07a 四章）
    # 目前還沒有管道寫進 RefactorState——這是 07a 十二章、00 十章已知的
    # 結構性缺口，State 是否需要新增對應欄位留給 08a 評估，這裡先記警告
    # 供人工事後查閱，不阻擋 pipeline。
    if result["skipped_interfaces"] or result["skipped_db_models"]:
        logger.warning(
            "generate_scaffold() 有 %d 個 interface、%d 個 db_model 被跳過，"
            "見回傳值 skipped_interfaces／skipped_db_models（07a 四章、十二章）",
            len(result["skipped_interfaces"]),
            len(result["skipped_db_models"]),
        )
    if not result["success"]:
        logger.error("generate_scaffold() 失敗：%s", result["error"])

    # 注意：scaffold 是平行分支 node，只回傳自己的 key，不展開 state
    return {"scaffold_done": result["success"]}
```

`db_models=None` 是刻意的暫時狀態：07a 十二章明訂「`db_models` 這個字典本身怎麼組出來...屬於 08a（骨架實作 Agent 詳細設計，待建立）的範圍」，07b 只負責接上 `generate_scaffold()` 本身的呼叫契約。

### 4. `graph/nodes/implement_node.py`：`_run_one_task()` 補齊完整引數

```python
"""
⑤ 功能改寫 Agent（translator-cli + scheduler）
依 task list 逐一呼叫 translator-cli 實作業務邏輯
見 01_langgraph_architecture.md 六、07a_translator_cli_architecture.md、07b_translator_cli_code.md

translator-cli 串接已完成（見 07b）；Harness 局部驗證（_partial_verify()
的 db／verifier）仍是 stub，非 07a/07b 範圍，見 02a/02b、09a（待建立）。
"""
import asyncio

from graph.scheduler import ModuleScheduler
from graph.state import RefactorState
from translator_cli import client as translator_cli
from translator_cli.types import FillResult

MODEL_SEMAPHORE = asyncio.Semaphore(1)


async def _run_one_task(task: dict, python_project_path: str) -> FillResult:
    async with MODEL_SEMAPHORE:
        return await translator_cli.fill_function(
            python_project_path=python_project_path,
            task_id=task["id"],
            target_file=task["target_files"][0],
            class_name=task.get("class_name"),
            function_name=task["function_name"],
            description=task["description"],
            context=task.get("context", ""),
            context_files=task["target_files"],
        )
```

`run()` 內對應改動：`_run_one_task()` 呼叫改傳 `state["python_project_path"]`；`result["success"]` 全部改成 `result.success`（`FillResult` 是 dataclass，不是字典）——`_partial_verify()`／Harness 局部驗證串接維持既有 stub，非本次範圍。

**未變動的部分**：`ModuleScheduler`、regression 偵測、局部驗證觸發時機等排程邏輯完全不受影響——07a／07b 只定義 translator-cli 被呼叫端的契約，node 內部怎麼組 task／怎麼呼叫排程器早已是既有實作。

---

## 十、模組結構總覽

```
refactor-project/
└── translator_cli/
    ├── client.py           # 對外唯一入口：generate_scaffold()、fill_function()
    ├── types.py            # FillResult／ScaffoldResult／PythonStructure／InterfaceSpec 等輸出入契約
    ├── python_adapter.py   # LanguageAdapter Protocol、PythonAdapter：ast 定位／替換／渲染／語法驗證
    ├── scaffold.py          # directory_tree 解析、interfaces 分組渲染、import 解析、db_models 併入
    ├── ollama_client.py     # httpx 呼叫 ollama（經 nginx）、delimiter 抽取、修正重試
    ├── prompts.py           # 七章 system/user prompt 模板
    ├── git_ops.py            # commit、git status 衝突偵測、rollback
    ├── formatting.py         # ruff format（硬性依賴）
    └── exceptions.py         # 內部例外型別
```

跟 07a 十一章規劃的結構一致，`client.py`／`types.py` 從 stub 換成真實實作，`formatting.py` 是規劃之外新增的模組，其餘 6 個檔案對應原訂結構。

---

## 十一、已知限制與待驗證事項

- **已接上真實 ollama／nginx 環境＋真實 Java 專案（93 個檔案）跑過端對端測試**：真實 pipeline（① 解析 → ③ 設計 → [P] 規劃 → ④ 骨架 → ⑤ 實作）70 個 task 中 69 個成功，delimiter 修正重試、網路層重試都真實觸發並驗證過。`_FORMAT_RETRY_COUNT` 依實測結果從 1 調整為 2（見七章）；`TRANSLATOR_CLI_TIMEOUT_SECONDS`（300 秒）依實測判斷足夠（簡單任務單次生成 15～19 秒）；`TRANSLATOR_CLI_NETWORK_RETRIES` 維持 2 不調整，理由見 07a 十四章。唯一失敗的真實案例（複雜查詢邏輯、6 個 target_files）已定位根因：qwen 對這個複雜度的任務，第一次回應違反 delimiter 格式的機率偏高，不是偶發——這超出 translator-cli 自己重試機制能保證解決的範圍，屬於 09a（⑤ Agent 詳細設計，待建立）該補的「task 永久失敗後如何交給 ⑦ Debug Agent 重試」機制，07a 十四章已記錄這個分工。
- **填空模式本體 import 解析（`resolve_body_imports()`）已對真實案例驗證**：真實 pipeline 執行中發現 qwen 生成的函式本體確實會引用簽名以外的名稱（如呼叫 `UserRepository`、拋出 `HTTPException`），已實作修正並對同一個真實案例重新對真實 ollama 驗證，import 正確補上、寫入內容語法合法，見七章。
- **`db_models` 目前固定傳 `None`**：`scaffold_node.py` 還沒有能力從 DB 或 Java entity 取得 `db_models` 字典，這是 07a 十二章明訂的 08a（骨架實作 Agent 詳細設計，待建立）範圍——這代表目前跑 `generate_scaffold()`，`app/models/{module}.py` 這批檔案都不會被產出。等 08a 落地、`scaffold_node.py` 補上真正的 `db_models` 字典後，`build_files()`／`write_files()` 不需要改動。
- **`skipped_interfaces`／`skipped_db_models` 沒有管道進 `RefactorState`**：見九章「3. `scaffold_node.py`」——這是 07a 十二章、00 十章已經記錄的已知結構性缺口（`[P]`／④ 是平行分支，`[P]` 結構上看不到④實際跳過了哪些 `InterfaceSpec`），07b 只確保這兩份清單被正確計算並記警告，留給 08a 評估是否需要新增 State 欄位。
- **`generate_scaffold()` 失敗時 `scaffold_node.py` 不會特別分流**：`result["success"] = False` 時，目前只記一筆 `logger.error()`，LangGraph 圖結構本身不會因此走不同的邊——屬於 08a 該補的範圍，07b 只確保失敗訊息清楚。
- **`_normalize_type()`／型別正規化的「已知殘留限制」未特別測試**：07a 四章提到的萬用字元泛型（如 `List<? extends Foo>`）轉換後仍含非法字元 `?`，測試涵蓋了「隔離失敗、不拖累其餘介面」的行為，但沒有涵蓋其他潛在殘留寫法——這批資料目前的目標專案沒有出現過，維持 07a 原文「暫不特別處理」的既有結論。
- **rollback 本身失敗時，只回報清楚的錯誤訊息，不會再重試或進一步自動處理**：這是刻意的邊界——rollback 本身失敗代表 git 環境有更根本的問題，不是 translator-cli 這一層能安全猜測、自動修復的情況，維持 07a 十三章「需要人工介入的錯誤不重試」的既有原則。
- **`ast.parse()`／`ast.unparse()` 不保留任何註解，是 `ast` 模組本身的根本限制**：LLM 在 `fill_function()` 生成的函式本體若含有解釋性註解，在 `extract_body_statements()` 把 `body_source` 解析成陳述式清單的那一刻就已經遺失（Python 標準庫 `ast` 不把註解視為語法樹的一部分），`formatting.format_paths()` 面對的已經是「沒有註解」的程式碼，救不回來。真正要保留 LLM 生成程式碼裡的註解，需要換掉 `ast.parse`／`ast.unparse` 這個核心機制（例如改用保留具體語法樹的 `libcst`），這是 07a 六章「AST 插入機制」設計決策本身的範圍，記錄在這裡供未來若確定需要保留生成程式碼裡的註解時，回頭重新評估。**`prompts.py` 的 `SYSTEM_PROMPT` 明確告知模型「不要寫 `#` 註解，反正會被丟掉」**——這不解決根本限制，但能省下模型生成註解的 output token（既然一定會被丟掉），也讓模型不會誤以為留了有意義的說明。
- **巢狀空 body（如 `if True: pass`）不在 CLI 層攔截，維持現狀**：`extract_body_statements()`（三章）只擋「陳述式清單完全為空」與「模型重複輸出函式簽名」兩種情況（07a 六章步驟 6a／6b），`if True: pass` 這種「語法合法、陳述式非空，但邏輯上等同沒做事」的退化輸出不會被攔下。這跟 07a 六章步驟 6b 明確點出的同一類問題（「隱含 return None，語法完全合法但語意錯誤，這個錯誤能通過步驟 10 的驗證，卻不是正確的實作」）性質相同——07a 只選擇攔「模型重複輸出函式簽名」這一種**可以無歧義機械判定**的模式，其餘語意層級的錯誤（`if True: pass`、`while False: ...`、算了但沒回傳等）刻意不窮舉，留給 Harness 實際執行測試／⑦ Debug Agent 處理，不是 CLI 層「格式契約」該負責的範圍。額外針對 `if True: pass` 這一種寫法加特判，既不能涵蓋這整類問題（`for _ in []: pass`／`try: pass except: pass` 等都是同樣性質但不同寫法），也違背 07a 這裡已經明確定案的邊界，因此維持現狀，不在 CLI 層新增這類語意檢查。
- **`ruff` 是硬性依賴**：`requirements.txt` 已加入 `ruff>=0.8.0`，執行環境沒裝的話 `generate_scaffold()`／`fill_function()` 每次呼叫都會在格式化步驟失敗並觸發 rollback（見八章 `formatting.py`）——這是刻意的取捨，用「沒裝 ruff 就整條流程都不能跑」換取 07a「diff 範圍必須乾淨」的承諾不因環境差異而破功，而不是讓格式化變成錦上添花的 best-effort。連帶地，`_split_block_imports()`（見四章）合併 Schema 定義段時可能殘留的多餘空行，也因為 `ruff format` 現在保證每次都會跑而自然被壓平，不需要另外處理。
