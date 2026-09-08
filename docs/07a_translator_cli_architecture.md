# translator-cli 詳細設計

> 本文件承接 `00_refactor_architecture.md` 的整體架構（見七、④骨架實作 Agent／⑤功能改寫 Agent 一節；三章「程式碼執行工具：translator-cli」；六章「Context 控制策略」；十章「待決定事項」），是這個工具的**設計面**文件：決策、契約、資料結構、流程。實際程式碼實作見 `07b_translator_cli_code.md`；本文件不出現可執行的實作邏輯，僅在需要精確釘死「格式契約」時才附固定範本（比照 `05a_design_agent_architecture.md` 三章附 `database.py` 固定範本的既有先例）。

---

## 一、本文件範圍與定位

**本文件涵蓋**：
- 骨架生成模式 `generate_scaffold(python_project_path, python_structure, db_models=None)`（④ 呼叫，見四章）與填空模式 `fill_function(python_project_path, ...)`（⑤ 呼叫，見五章）的完整輸入輸出契約
- AST 插入機制：如何在既有檔案裡精準定位、替換單一函式本體（六章）
- 與 ollama（經 nginx）的連線方式、request/response 格式、delimiter 契約（七章）
- git snapshot 流程、commit 顆粒度（見八章）
- 衝突偵測機制（九章）
- 目標語言 adapter 介面（十章）
- 與既有程式碼（`graph/state.py`／`graph/nodes/scaffold_node.py`／`implement_node.py`）的介面異動需求（見二章末「與既有程式碼的介面異動」）

**本文件不涵蓋**：
- 實際程式碼——見 07b
- ④／⑤ node 本身怎麼組裝 task／怎麼呼叫排程器——這部分已經是既有實作（`graph/nodes/scaffold_node.py`／`implement_node.py`／`graph/scheduler.py`），07a 只定義 translator-cli 被呼叫端的契約，08a／09a 才是 node 內部邏輯的權威文件
- Harness 的局部驗證／全量驗證——見 02a
- [P] Plan Agent 如何產生 `task.description`／`task.context` 的業務語意內容——見 06a

---

## 二、兩種模式總覽

00 七章已定調：`fill_function()`（填空模式）與 `generate_scaffold()`（骨架生成模式）不是同一個契約，呼叫方、輸入輸出、呼叫頻率完全不同：

| | `generate_scaffold()` | `fill_function()` |
|---|---|---|
| 呼叫方 | ④ `scaffold_node.py` | ⑤ `implement_node.py`（qwen 路徑受 translator-cli 內部的 `OLLAMA_MODEL_SEMAPHORE(1)` 限制，Claude 路徑不受限，見七章） |
| 呼叫次數 | 整條 pipeline 一次 | 逐 task 呼叫 |
| 輸入 | `python_project_path`（見下方）＋ `PythonStructure`（整包）＋ `db_models`（④ 自行取得的 DB schema 內容，見四章） | `python_project_path`（見下方）＋單一 task 的 `target_file`／`class_name`／`function_name`／`translator_backend`／`java_source`／`referenced_source`／`context`／`context_files`（見五章，`description` 不在其中——不是模型輸入，純供 log 用） |
| 是否呼叫模型 | **否**（見四章） | 是——依 `translator_backend` 分派 qwen2.5-coder:32b（via ollama）或 Claude API，見七章 |
| 寫入方式 | 從無到有建立檔案（整檔輸出） | 對既有檔案做 AST 精準插入（單一函式本體） |
| git 動作 | 一次性 commit（見八章） | 逐 task commit（見八章） |

`fill_function()` 依 `translator_backend` 分派 qwen（既有 ollama 路徑）或 Claude API（新路徑，見七章）。輸入契約是「`java_source`／`referenced_source`／`context`／`context_files`」，不是「`description`／`context`／`context_files`」——`description` 已降級成純機械模板、只供 log 用途（見 06a 五章），[P] 不再產生語意摘要，改成⑤（`implement_node.py`）用 javalang 抽取 Java 方法原始碼、依 [P] 算好的 `reference_targets` 座標讀出真實原始碼（Java 或已翻譯 Python），組成這兩個新引數傳進來——**這件事發生在 translator-cli 之外**，07a 只定義收到這兩個引數之後怎麼用，不做抽取本身（見五章「為什麼是 java_source／referenced_source」）。Claude 路徑的模型呼叫刻意不序列化（換取雙後端要的平行效能），寫入段因此改用八章「寫入段用細粒度鎖序列化」——只鎖讀檔／AST替換／寫入／commit 這一小段，模型呼叫本身不鎖：同一 phase 內平行執行沒問題，但 qwen 本地模型基於硬體限制仍須序列化。

### 新輸入：`python_project_path`

00 五章「Java 專案端」明訂 Java 專案是與 `refactor-project/` **同層、各自獨立**的資料夾、有自己的 `.git`，理由是避免版控規則互相打架。translator-cli 寫入的 Python 目標專案適用同一個理由，而且更進一步——**每個 task 各自 commit**（見八章）的顆粒度若跟 orchestrator 自己的開發歷史（docs／graph 程式碼的一般開發 commit）混在同一個 repo，會讓 `git log` 沒辦法乾淨地區分「這是我在改 orchestrator」還是「這是 translator-cli 自動產生的第 042 個 task」。因此比照 `JAVA_PROJECT_PATH` 的既有慣例，新增：

```
test/2026/
├── refactor-project/              ← Python orchestrator（本 repo）
├── lang-exam-api-refactor/        ← Java 專案複製版
└── <python-target-project>/       ← translator-cli 寫入目標，獨立 git repo
```

- **環境變數**：`.env` 新增 `PYTHON_PROJECT_PATH`（相對或絕對路徑皆可，慣例同 `JAVA_PROJECT_PATH`）
- **一次性前置準備**（比照 00 五章「Java 專案端」的手動步驟，不是 translator-cli 自動做的事）：目錄存在、`git init` 過、**沒有任何 commit**（乾淨的空 repo）。`generate_scaffold()` 執行前檢查目標目錄是不是一個 git repo，不是則直接中止並回報明確錯誤——這是輸入端環境沒準備好，不是可以自動補救的情況，比照 04a／05a 對輸入端問題「直接往上拋，中止」的既有原則。

**`python_project_path` 是 `generate_scaffold()`／`fill_function()` 的顯式必要引數**：translator-cli 讀寫的每一個相對路徑（`InterfaceSpec.file_path`、`task.target_files`、`db_models` 的 key）都要解析成磁碟上的絕對路徑才能真正 `open()`／`git` 操作，兩個函式的簽名因此明訂：

```python
async def generate_scaffold(
    python_project_path: str,
    python_structure: PythonStructure,
    db_models: dict[str, str] | None = None,
) -> dict: ...

async def fill_function(
    python_project_path: str,
    task_id: str,
    target_file: str,
    class_name: str | None,
    function_name: str,
    translator_backend: Literal["qwen", "claude"],
    java_source: str,
    referenced_source: list[ReferencedSourceItem],
    description: str,
    context: str,
    context_files: list[str],
) -> FillResult: ...
```

`translator_backend`／`java_source`／`referenced_source` 完整設計見五章。

**`task_id` 是必要引數**：對應 06a 八章 `TaskSpec.id`（`task_{:03d}` 格式，穩定、可重現），讓 `git log`（八章 commit 訊息格式）能精確對回 [P] Plan Agent 產出的具體 task，不需要每次靠 `class_name.function_name` 反推——尤其同一個 `(class_name, function_name)` 若因為某種原因被呼叫超過一次（見五章「冪等」），只有 `task_id` 才能唯一區分是哪一次呼叫留下的 commit。

`python_project_path` 是**呼叫端顯式傳入**的引數，不是 translator-cli 自己讀 `os.environ["PYTHON_PROJECT_PATH"]`，也不是靠 import `graph.state` 去拿——這跟 `python_structure`／`task.*` 這些既有引數是同一種模式：`.env` 的值先進 `main.py` 組的 `initial_state`（見下方「與既有程式碼的介面異動」），流進 `RefactorState.python_project_path`，`scaffold_node.py`／`implement_node.py` 從 state 取出後再顯式傳給 translator-cli，translator-cli 本身仍然不 import `graph.state`（見十二章），維持既有的獨立性原則。下方四／五／六／八／九章所有描述「目標目錄」「target_file 現有內容」「git repo」的地方，一律以呼叫端傳入的這個 `python_project_path` 為準（`Path(python_project_path) / target_file` 這類相對路徑解析）。

### 與既有程式碼的介面異動

`python_project_path`／`translator_backend`／`java_source`／`referenced_source`／`context` 這幾項異動（`graph/state.py`／`main.py`／`graph/nodes/scaffold_node.py`／`graph/nodes/implement_node.py` 新增欄位並在對應呼叫點傳遞；`graph/java_source_extraction.py` 負責把 `java_method_id`／`reference_targets` 解析成真正的原始碼文字）已經全部套用完成。實際內容以 `07b_translator_cli_code.md` 十章「與既有程式碼的介面異動」為準，不在這裡重複列一份會跟著實作進度過期的清單。

---

## 三、Python 目標專案的分層與 translator-cli 的角色邊界

translator-cli 不重新決定 Python 專案怎麼分層——03 三章的分層規則（`app/{routers,services,repositories,schemas,models}/`、檔名慣例 `{module}_{layer}.py`）已經是定案，`PythonStructure` 完整攜帶了這份決策。translator-cli 的角色純粹是「把 `PythonStructure`／task 描述轉成磁碟上實際存在、語法正確的 `.py` 檔案」，不重新判斷任何屬於 ③ 職責範圍的事（層級歸屬、型別對應、`db: Session` 要不要加預設值——這些在 `InterfaceSpec` 送到 translator-cli 手上時已經是最終決定，見 `design_agent/design.py` `db_type = "Session = Depends(get_db)" if layer == "routers" else "Session"` 的既有實作：`ParamSpec.type` 本身就已經是完整可以直接拼進函式簽名的字面字串，**translator-cli 渲染參數時只需要 `f"{p['name']}: {p['type']}"` 逐一字串接合，不需要對 `db`／`Depends`／預設值做任何特殊判斷**）。

---

## 四、骨架生成模式：`generate_scaffold(python_project_path, python_structure, db_models=None)`

### 決策：不呼叫本地模型，純機械產生

`generate_scaffold()` 不呼叫本地模型。理由：`PythonStructure` 送到這一步時，`directory_tree`（三段固定格式，見 `05a_design_agent_architecture.md` 三章）＋ `interfaces`（檔案路徑＋完整函式簽名層級）已經是**結構化、無歧義**的資料，骨架階段要做的事——建目錄、把已知的類別/函式簽名渲染成語法正確的 Python 檔案、`pass` 佔位——沒有任何一步需要「判斷」或「創造」，全部是機械字串組裝加 AST 語法驗證。呼應 00 二章「能用程式判斷的，就不要交給 LLM」：這正是可以完全用程式判斷的情況，讓 qwen 生成骨架只會多引入一種全新的失敗模式（模型自己編排的骨架語法錯誤、簽名跟 `InterfaceSpec` 不一致），且沒有對應的好處。

`generate_scaffold()` 因此是**同步、確定性**的函式（不需要 `await` ollama），與 `fill_function()`（必須呼叫模型）在失敗模式上完全不同：`generate_scaffold()` 唯一可能失敗的原因是輸入資料本身有問題（如三章「Python 目標專案的分層」邊界之外的意外情況），不會有「模型亂回答」這種不確定性。

### 輸入解析：`directory_tree` 三段格式

沿用 05a 三章「格式慣例」定義的固定格式，`generate_scaffold()` 逐段處理：

**第一段（目錄結構段）**：純文字樹狀圖，只用來 `mkdir -p` 建立所有子目錄（`app/routers/`、`app/services/`、`app/repositories/`、`app/schemas/`、`app/models/`、`app/core/`），不從這段解析任何檔案內容——檔案內容一律來自下面兩段＋`interfaces`。

**第二、三段（Schema 定義段／基礎設施段）**：格式固定為 `### {file_path}` 標題 + ` ```python ` fenced code block，用正則逐段擷取 `(file_path, code_block_text)`。兩種處理方式：

- **基礎設施段**（`app/core/database.py`／`app/main.py`）：code block 內容就是完整檔案內容，直接寫入，不需要合併。
- **Schema 定義段**：**同一個 `file_path`（典型是 `app/schemas/{module}.py`）可能出現多個獨立的 code block**——05a 三章明講「API 邊界 schema、建構子占位、資料容器占位」三種來源各自是獨立的 `### {file_path}` 段落，「不是三選一」。`generate_scaffold()` 因此依 `file_path` 分組，把命中同一個檔案的所有 code block **合併成一個檔案**：
  1. 逐 block 解析出 import 陳述式（`from X import Y` 這一行）與其餘內容（class 定義／註解）
  2. 所有 block 的 import 陳述式去重、合併成單一行集合（如三個 block 各自有 `from pydantic import BaseModel`、`from dataclasses import dataclass`、`from pydantic import BaseModel, Field`，合併後是 `from pydantic import BaseModel, Field` ＋ `from dataclasses import dataclass` 兩行，`BaseModel` 只留一次）
  3. `from __future__ import annotations` 這行只保留一次、放最前面（05a 三章已規定每個 Schema 定義段固定以這行起頭，多個 block 各自都有，需去重）
  4. import 段落之後，依原本三個 block 各自的順序依序接上各自的 class／註解內容
  5. 合併後的完整文字丟一次 `ast.parse()` 驗證語法（見六章），失敗代表 05a 這端渲染的 pseudocode 本身有問題，直接中止並回報，不嘗試自動修正

### `db_models`：④ 自行取得的 DB schema 內容如何併入

**背景**：00 七章、05a 三章／九章都明訂 DB schema 的欄位層級規格不是③的職責，由④直接從既有 Postgres 測試 DB 或 Java entity 原始碼取得。但 `python_structure.interfaces` 完全不含 `models/{module}.py` 的欄位內容（`InterfaceSpec` 只能表達函式簽名），05a 三章的 `directory_tree` 也只涵蓋 `schemas/{module}.py`／基礎設施檔案兩類文字內容——`models/{module}.py` 從未出現在 `PythonStructure` 能表達的任何一個欄位裡。若④在 `generate_scaffold()` 之外自己另開一次寫入＋commit 來放這批檔案，時序上必須嚴格排在 `generate_scaffold()` 前後其中一邊、且自己先 commit 乾淨，否則會撞上九章的 working tree 衝突偵測；即使排對順序，也會讓「一次性 commit」的骨架基礎（八章）被拆成兩次，徒增復雜度。

**決策：`generate_scaffold()` 新增第三個輸入 `db_models: dict[str, str] | None = None`**（key 為檔案相對路徑，如 `app/models/user.py`；value 是④已經生成好的完整 SQLAlchemy model 檔案內容字串）。④仍舊負責「怎麼從 DB／Java entity 取得欄位、生成程式碼」這件事本身（00／05a 既有分工不變），只是**不直接寫入磁碟**，改把產出封裝成這個字典、當作呼叫 `generate_scaffold()` 的引數之一。translator-cli 內部把 `db_models` 的每一項併入跟 `interfaces`／`directory_tree` 渲染結果**同一個記憶體中的寫入佇列**，一起走過驗證與寫入流程，不是額外的寫入／commit 動作：

1. 每一項 `db_models[file_path]` 的內容各自 `ast.parse()` 一次（跟下方「語法驗證與寫入」對 `InterfaceSpec` 的隔離失敗處理同一種精神）：失敗 → 這個檔案不寫入，記進回傳值新增的 `skipped_db_models` 欄位（`{file_path, error}`），不影響其他 `db_models` 檔案，也不影響 `interfaces`／`directory_tree` 渲染出的檔案——一個模組的 DB 內省失敗不該拖垮其餘模組的骨架產出，理由跟「語法驗證與寫入」全域 all-or-nothing 的既有論證一致
2. `db_models` 的檔案路徑天然落在 `app/models/` 底下，由第一段（目錄結構段）的 `mkdir -p` 保證目錄已存在，不需要額外建目錄邏輯
3. 沒有出現在 `db_models` 裡的模組（純外部 API 串接、沒有對應 DB 表的模組），`models/{module}.py` 就不會被建立——這是合法情況，不是錯誤，下游讀取端的容錯機制見五章「`context_files` 讀取容錯」
4. 所有檔案（`interfaces`／`directory_tree`／`db_models` 三個來源合起來）都通過各自驗證後，才進入「語法驗證與寫入」步驟 4 的全域最終檢查與一次性寫入＋commit——`db_models` 不獨立 commit，天然併入八章「`generate_scaffold()` 一次性 commit」的既有顆粒度，這也是選擇「傳字典給 `generate_scaffold()` 合併寫入」而不是「④自己寫、自己 commit」的核心理由：從根本上消除排序風險，不需要協調兩個各自獨立的 git 操作

### 型別字串正規化（Java 泛型符號 → Python subscript 語法）

**型別字串的語法合法性由 `design_agent/type_mapping.map_java_type()` 保證**（見 `05a_design_agent_architecture.md` 五章「未知泛型包裝類別的處理」）：已知的 JDK functional interface 查表轉成 `Callable[...]`，其餘未知泛型遞迴正規化內層型別參數＋符號轉換 `Foo<Bar>` → `Foo[Bar]`，`python_structure.interfaces` 送到這一步時型別字串已保證語法合法。

**這裡仍保留同一套符號轉換邏輯，但只作為 defense-in-depth**：對 ③ 輸出而言恆為 no-op 的最後防線（防禦其他呼叫路徑萬一繞過 ③，或未來 `InterfaceSpec` 出現這裡沒預期到的殘餘寫法）：

```
normalized = raw_type.replace("<", "[").replace(">", "]")
```

**已知殘留限制（正規化後仍可能不完美，但不阻擋語法驗證）**：
- 轉換只保證**語法合法**，不保證**語意正確**——`ResponseResult[T]` 語法上合法，但 `T`（Java 泛型型別變數）在 Python 端沒有對應的 `TypeVar` 定義，`ResponseResult` 本身也不一定支援 subscript（除非它是 `Generic[T]` 子類別）。這個問題由「`interfaces`：函式簽名渲染」一節的渲染樣板頂端強制加入 `from __future__ import annotations` 承接（機制細節見該節），解決的是「會不會直接炸」，不是完整語意正確：若 ⑤ 之後在函式本體真的寫出 `ResponseResult[X]()` 這種在執行期對該型別做 subscript／實例化的程式碼，仍會出錯——但那是函式本體實作品質的問題，屬於 ⑤／⑦ 工作範圍，不是簽名渲染樣板能單獨解決的
- Java 萬用字元泛型（如 `List<? extends Foo>`）轉換後仍含非法字元 `?`，這批資料裡沒有出現這種寫法，暫不特別處理，屬於下方「語法驗證與寫入」失敗隔離機制要接住的殘餘情況

### `interfaces`：函式簽名渲染

`interfaces` 依 `file_path` 分組（`app/routers/`／`app/services/`／`app/repositories/` 三層的實際程式碼都只能來自這裡，`directory_tree` 不含這三層的檔案內容）。同一 `file_path` 群組內，再依 `class_name` 分組。**以下渲染一律使用正規化後的型別字串，不是 `InterfaceSpec` 原始值**：

**檔案開頭一律固定加 `from __future__ import annotations`**（routers／services／repositories 三層無差別套用，比照 05a 三章 Schema 定義段已有的同一行）：這是回應「型別字串正規化」一節「已知殘留限制」的修正——`map_java_type()` 遞迴正規化＋符號轉換（見上方一節）只保證型別字串**語法合法**，不保證其中殘留的裸型別變數（如 `Function<T, String>` 正規化後的 `T`）或未宣告 `Generic[T]` 的自訂泛型類別（如 `ResponseResult[T]`）在 `import` 當下不會因為型別註記被求值而 `NameError`／`TypeError`。加上這一行後（PEP 563），函式簽名的型別註記變成延遲求值的字串，`import` 這個模組本身不會因為註記內容而失敗；API 邊界方法（router 層參數）因為型別全部來自 `openapi_spec`（05a 五章），本來就是可解析的乾淨型別，FastAPI 的 `get_type_hints()` 照常能正確 resolve，不受影響。這一行是**這份文件（渲染樣板契約）本身的一部分，不是留給 07b 實作階段才決定的事**：

- **`class_name is None`（routers 層，05a 七章已定案這層一律為 `None`）**：檔案內是自由函式，不包 class。檔案開頭固定加：
  ```python
  from __future__ import annotations

  from fastapi import APIRouter

  router = APIRouter()
  ```
  **`class_name is None` 不等於「這個函式一定是 API 邊界方法」**：05a 三章的層級判定是依 Java class 的 stereotype（`@RestController`）整批決定，同一個 Controller 底下除了直接對應 endpoint 的方法，也可能有 05a 七章「私有／內部方法命名慣例」講的私有 helper、或其他未直接掛 `@GetMapping` 等 route annotation 的方法——這些方法一樣落在 `routers` 層、`class_name` 一樣是 `None`（05a 七章「routers 層的 `InterfaceSpec.class_name` 一律為 `None`」不分方法種類），但 `http_method`／`route_path` 只在**真正的 API 邊界方法**才非 `None`（05a 五章）。因此每個函式渲染前必須先判斷 `http_method`：
  - `http_method` 非 `None`（API 邊界方法）：
    ```python
    @router.{http_method.lower()}("{route_path}")
    def {function_name}({params}) -> {return_type}:
        pass
    ```
  - `http_method` 為 `None`（routers 層裡的非邊界方法，如私有 helper）：**不加 `@router` 裝飾器**，渲染成一般自由函式：
    ```python
    def {function_name}({params}) -> {return_type}:
        pass
    ```
  
  **這不是理論上的邊角案例**：05a 七章「`interfaces` 涵蓋率規則」要求 `python_structure.interfaces` 涵蓋 `module_list` 每一個 module 的每一個方法（回應 00 八章「internal helper 不能漏」），任何一個 `@RestController` 只要有一個私有 helper 或非直接對應 endpoint 的方法，就會產生一筆 `http_method=None` 的 routers 層 `InterfaceSpec`——若不做這個判斷，無條件套用 `@router.{http_method.lower()}(...)` 樣板，`http_method` 是 `None` 時 `.lower()` 會直接 `AttributeError`，讓 `generate_scaffold()` 對這個真實常見的情況整個掛掉（`params` 直接是 `", ".join(f"{p['name']}: {p['type']}" for p in interface['params'])`，見三章，兩種渲染方式共用同一套 `params` 組裝邏輯，只差要不要加裝飾器）。
- **`class_name` 非 `None`（services／repositories 層）**：檔案開頭同樣固定加 `from __future__ import annotations`（獨立一行，這兩層沒有像 routers 層那樣需要緊接著 `from fastapi import APIRouter` 之類的固定 import）。同一 `file_path` 底下按 `class_name` 分組各自渲染一個 `class`，方法縮排在 class 內：
  ```python
  from __future__ import annotations


  class {class_name}:
      def {function_name}({class_params}) -> {return_type}:
          pass
  ```
  **`{class_params}` 必須帶 `self`，不是直接沿用 `{params}`**：`InterfaceSpec.params` 依 05a 七章定案「不含隱含的 `self`」，這是刻意的邊界（`self` 對 Python 才有意義，Java 沒有對應概念，讓③輸出攜帶它只是徒增雜訊）——但這代表這裡渲染成 `class` 方法時，**必須由這一層自己補上**，不能直接把 `params` 字串塞進括號，否則 `def {function_name}({params})` 會漏掉 `self`，產生出的方法在執行期被實例呼叫時必然 `TypeError`（如 `def get_by_id(user_id: int):`，`repo.get_by_id(5)` 實際呼叫是 `get_by_id(repo, 5)`，兩個位置引數對上一個形式參數）——這不是理論上的邊界案例，是 services／repositories 兩層**每一個**方法都會踩到的必然錯誤，不能只在 routers 層的樣板算對就視為完成。組裝規則：`class_params = f"self, {params}" if params else "self"`（`params` 為空字串時不留下多餘的逗號，讓組出的文字讀起來乾淨；即使留一個 `def foo(self, ):` 這種尾隨逗號寫法本身也是合法 Python 語法，但沒有必要留這種不必要的雜訊）。`routers` 層（`class_name is None`，自由函式、不是 class 方法）不適用這條規則，`{params}` 原樣使用，見上方 routers 樣板。
  
  **刻意不產生 `__init__`**：`InterfaceSpec` 沒有攜帶建構子／欄位依賴資訊（04a／05a 的職責邊界都只到方法簽名層級），這批 class 在骨架階段是無狀態的方法容器；跨 class 呼叫（如 service 呼叫 repository）留給 `fill_function()` 階段依 `context` 決定寫法（直接 import 後實例化，或呼叫端另有慣例），不是骨架階段能機械決定的事，見五章。

**函式本體固定寫 `pass`**：語法合法、明確代表「待填」，也是六章 AST 定位／替換的目標錨點。

**`from __future__ import annotations` 不經下方「import 解析：兩層機制」判斷**：這一行是每個生成檔案無條件的第一行，不是因為偵測到某個具體型別才觸發的 import，因此獨立於下方兩層機制之外，兩層機制只處理型別觸發的 import（`Session`／`Depends`／自訂型別等）。

### import 解析：兩層機制，不窮舉

每個函式的 `params`／`return_type` 字串裡可能出現需要額外 import 的型別（`Session`、`Depends(...)`、專案內自訂 class 如 `UserCreateRequest`）。`generate_scaffold()` 用兩層規則機械決定每個檔案開頭要加哪些 import：

1. **已知關鍵字表（機械，涵蓋這個專案已知會出現的框架型別）**：型別字串命中以下關鍵字就加對應 import，一個檔案內出現多次只加一次。以英數字元結尾的關鍵字（`Session`／`get_db`／`Request`／`UploadFile`／`HTTPException`／`Decimal`／`date`／`datetime`／`time`／`UUID`——下表除了 `Depends(`／`Callable[` 以外的全部）用單字邊界比對（Python 識別字不能包含非英數字元，邊界即等同識別字邊界），避免命中自訂型別名稱剛好包含這個關鍵字當子字串的情況（Java DTO 常見命名 `LoginRequest`／`UserSession` 這種以 Request／Session 結尾的類別，若用純子字串比對會被誤判成需要 import 標準庫的 `Request`／`Session`）；以標點結尾的關鍵字（`Depends(`／`Callable[`）維持純子字串比對，理由見下方 `Callable` 說明：

   | 命中關鍵字 | 加入的 import |
   |---|---|
   | `Session` | `from sqlalchemy.orm import Session` |
   | `Depends(` | `from fastapi import Depends` |
   | `get_db` | `from app.core.database import get_db` |
   | `Request` | `from fastapi import Request` |
   | `UploadFile` | `from fastapi import UploadFile` |
   | `HTTPException` | `from fastapi import HTTPException` |
   | `Decimal` | `from decimal import Decimal` |
   | `date` | `from datetime import date` |
   | `datetime` | `from datetime import datetime` |
   | `time` | `from datetime import time` |
   | `UUID` | `from uuid import UUID` |
   | `Callable[` | `from typing import Callable` |

   **`Callable` 是這次新增的一項**：05a 五章的型別對應表（Layer 2）把 `Function`／`Supplier`／`Predicate` 等 `java.util.function` 常見型別機械轉成 `Callable[...]`，這些字串會出現在 services／repositories 層、以及 routers 層裡非 API 邊界方法（`http_method` 為 `None` 的私有 helper）的 `params`／`return_type` 裡——這幾種情況都不經過 openapi_spec 覆寫，都會走 `map_java_type()` 那條路徑，因此不是理論上才會出現的型別。`from __future__ import annotations`（見上方）讓型別註記延遲求值，避免了 `import` 當下就 `NameError`，但這解決的是「會不會直接炸」，不是「型別名稱有沒有正確 import」——⑤填空階段若要在函式本體實際使用這個型別（如型別檢查、建構一個符合 `Callable` 簽名的物件），仍然需要這個 import 真的存在，不能只靠註記延遲求值蒙混過去；沒有這一項，`services`／`repositories` 層任何回傳或接收 functional interface 的函式都會缺這個 import。命中比對用 `"Callable["`（含左中括號）而不是單獨的 `"Callable"`，避免不必要地跟其他字串裡剛好出現 `Callable` 這幾個字母的情況混淆（目前這個字串只會由 `map_java_type()` 產出，不會有這種混淆風險，但比對條件寫精確一點沒有壞處）。

   **`date`／`datetime`／`time`／`UUID` 是 08a（④骨架實作 Agent）落地時新增的四項**：`common/java_type_mapping.py`（見 08a 五章「共用邏輯」）補上 `java.time`／`java.util.UUID` 對應後，③的方法簽名（非 API 邊界的內部方法，同樣走 `map_java_type()` 這條路徑）也可能產生這幾個型別字串，原表沒涵蓋會重演上面 `Callable` 那段講的同一種缺 import 情況。`"date"`／`"datetime"`／`"time"` 三個關鍵字都以英數字元結尾，走上面講的單字邊界比對——`"datetime"` 內含 `"date"`／`"time"` 兩個子字串，但邊界比對不會誤觸發（`"date"` 與後面的 `"time"` 之間沒有字元邊界，不構成獨立識別字），三者互不誤判。

2. **自訂型別索引，兩個來源合併**：
   - **來源一（既有）**：掃描第二段（Schema 定義段）解析出的所有具名 class（僅 `schemas/{module}.py` 底下的 Pydantic／dataclass——Schema 定義段不含 `models/{module}.py` 內容，見下方「來源三」）
   - **來源二（新增，見上方「型別字串正規化」的真實案例）**：直接從 `python_structure.interfaces` 收集每個 `class_name → file_path`（排除 `class_name is None`）。**這一步是必要的，不是加強保險**：真實資料裡有 **44 筆**跨檔案引用（見上方 `ResponseResult`／`Result` 案例）——這些型別是 `services` 層的一般類別（如 `common_service.py` 的 `ResponseResult`），根本不會出現在 Schema 定義段裡（Schema 定義段只收錄 API 邊界 Pydantic model／資料容器 dataclass，不收錄一般 `services`／`repositories` 層的類別），只靠來源一完全抓不到。這個來源不需要額外解析文字——`python_structure.interfaces` 本身就是結構化資料，直接建索引即可，比來源一的正則掃描更直接
   - **來源三（新增）**：對 `db_models` 字典（見上方「`db_models`」一節，key 為 `app/models/{module}.py` 這類路徑，value 是④已產出的完整檔案內容字串）逐項 `ast.parse()` 取出頂層 `ClassDef` 名稱，`class_name → file_path`（= `db_models` 的 key）併入索引——這個 `ast.parse()` 不是新增的驗證負擔，上方「`db_models`」一節第 1 點已經對每個 `db_models[file_path]` 各自 `ast.parse()` 一次做語法驗證，這裡直接複用同一次解析結果取 `ClassDef`。**這一步是必要的，不是加強保險**：標註 `@Entity`／`@Embeddable`／`@MappedSuperclass` 的類別依 05a 三章「孤兒類別與資料容器占位」判斷優先序第 1 點，③直接跳過、不渲染任何 `InterfaceSpec`——這些 ORM class 因此從頭到尾不會出現在來源一（不在 Schema 定義段）或來源二（沒有對應 `InterfaceSpec.class_name`）裡，只存在於 `db_models` 的檔案內容字串中。Repository／Service 方法若回傳型別是這類 Model class（如 00 六章 `UserRepository.get_by_id()` 對應 `user.py` 這個 model 檔案，回傳型別依 05a 五章型別對應表就是裸名稱 `User`），沒有這個來源就會落入下方「兩層都比對不到時保守不加」，永遠補不上對應 import——這是每個有 DB 表的 module 都會踩到的常態情況，不是邊角案例
   - **來源三同時掃 `Table(...)` 變數賦值，不是只掃 `ClassDef`**：08a_scaffold_agent_architecture.md 八章「`@ManyToMany`：產生中介表定義」把多對多關聯的中介表渲染成 `user_roles = Table(...)`（`ast.Assign`），不是 class——這種中介表沒有對應的 Java entity，本來就不會有 `ClassDef`。⑤填空時若需要對這張表直接操作（新增／刪除一筆多對多關聯，08a 八章明講的既有動機），生成的程式碼會直接引用 `user_roles` 這個名稱；若只掃 `ClassDef`，這個名稱永遠進不了索引，會被下方「兩層都比對不到時保守不加」吞掉。因此來源三對 `db_models_valid` 的每個 `tree` 額外呼叫 `_table_assignment_names(tree)`（只認「單一目標、RHS 是呼叫 `Table(...)` 或 `xxx.Table(...)`」這個精確形狀，不誤收一般常數賦值），把找到的變數名一併併入索引
   
   三個來源合併成一份索引（key 衝突時優先序為來源三／來源二＞來源一：前兩者是結構化資料或已驗證通過語法的實際檔案內容，精確度高於來源一的正則掃描文字；~~實務上三者對應的 class 集合本來就不重疊……衝突理論上不會發生~~——**這個假設不成立，見下方「來源一內部也會衝突」的真實案例，不是三個來源之間的衝突，是來源一自己內部、同名 class 被多個模組各自獨立定義時的衝突**）。裸型別名稱的擷取用 AST，不用字串裁切／正則：`ast.parse(正規化後的 params／return_type 字串, mode="eval")` 後 `ast.walk()` 遍歷取出所有 `ast.Name` 節點的 `.id`，逐一比對這份索引，命中就加 `from {module_path} import {class_name}`（`module_path` 由 `file_path` 去掉 `.py`、`/` 換 `.` 得到）。用 AST 而非字串裁切是必要的，不是風格選擇——`ResponseResult[User]`、`dict[str, UserCreateRequest]` 這類巢狀泛型若用字串裁切／正則容易只抓到最外層（如 `ResponseResult`），漏掉內層真正需要 import 的型別（`User`），巢狀深度不固定時字串規則無法窮舉；`ast.walk()` 不論巢狀多深都能一次抓齊所有裸名稱。這些型別字串本來就已經在上方「型別字串正規化」一節保證能被 `ast.parse()` 合法解析，這裡不需要額外的容錯處理。

**兩層都比對不到時保守不加、不猜**：可能是 Python 內建型別（`int`／`str`／`bool`，本來就不需要 import），也可能是 ③ 的 LLM 步驟自由產生的複合寫法（如 `Annotated[User, Depends(get_current_user)]`，`User` 部分理論上會被第二層抓到，但更複雜的巢狀寫法可能抓不全）。這裡延續 04a／05a 反覆出現的「機械規則解決不了的部分不強行猜測」精神，但方向相反——那邊是「多連少排除」（保守多連結），這裡刻意選「少加不亂猜」，因為錯誤 import 一個不存在的名稱會讓整個檔案在 `import` 階段就炸掉（比缺 import 導致的 `NameError` 更早爆、更難定位、牽連同一個 `app/main.py` 底下的其他 router）。剩餘的 import 缺口交給 Harness 的 module 局部驗證（服務起不來會直接反映在驗證失敗）與 ⑦ Debug Agent 處理，不在骨架階段窮舉。

**決策：自訂型別索引命中要再排除「正在組裝的這個檔案自己的 class」，否則會產生自我 import**：這是端對端驗證才暴露的既有缺陷，不是設計初期就預見的——真實案例 `exam-platform-api` 的 `app/services/common_service.py` 裡，`ResponseResult.error_4(self, code: ErrorCode)` 這個方法簽名引用了**同一個檔案裡稍後才定義**的另一個 class `ErrorCode`。上方「自訂型別索引」的三個來源都是全專案掃描，本來就會收錄「這個檔案自己定義的 class」，比對時若不排除，會產生一行 `from app.services.common_service import ErrorCode` 寫進 `common_service.py` 自己——這行 import 恆為 True（`ErrorCode` 確實在這個模組裡定義），語法完全合法，`ast.parse()` 抓不出來，但 Python 執行期 import 這個模組時會直接觸發自我引用的 `ImportError`，Harness 啟動服務階段才會炸。這類「同檔案跨 class 互相引用方法簽名」在 Java 靜態工具類（如 `ResponseResult` 這種集中定義多個工廠方法、內部引用同檔案其他 class 的樣式）很常見，不是邊角案例。修法：比對命中之餘，額外排除「正在組裝的這個檔案自己的全部 class 名稱」（`_render_interface_files()` 這一輪 `file_path` 分組下的 class 集合）——這批類別就在同一個檔案裡，本來就不需要 import。

**來源一內部也會衝突：同名的具名回應包裝 class 被多個模組各自獨立定義（對應 `docs/refactor_bug_trace.md` #8）**：上面「三個來源合併」那段的假設是「三個來源對應的 class 集合本來就不重疊」，但這個假設只保證**跨來源**不衝突，沒有保證**來源一自己內部**不衝突。真實案例：`ResponseResult<String>` 這種專案自訂泛型，③ Design Agent 逐模組產生具名包裝 class（如 `ResponseResultString`）時，模組之間互不知道彼此定義了同名的東西——`exam`／`file` 兩個模組的 `schemas/{module}.py` 各自都定義了一份完全獨立、內容相同的 `ResponseResultString(BaseModel)`（`ResponseResultGetAllGradeRs` 在 `general`／`registration` 也是同一個模式）。原本的自訂型別索引是全域、只認 class 名稱的扁平 `dict[str, str]`，逐一掃描每個模組的 schema 檔案時對同名 class 直接覆寫（`index[node.name] = file_path`），最後只留得住迭代到最後那個模組的檔案路徑——`file_router.py` 引用自己模組定義的 `ResponseResultString` 時，查到的卻可能是 `exam.py` 的路徑，生出 `from app.schemas.exam import ResponseResultString`，即使 `file.py` 明明自己也定義了一份一模一樣的 class。

修法：額外記錄「每個模組（`app/schemas/{module}.py` 的 `module` 段）自己的 schema 檔案定義了哪些 class 名稱」（`schema_classes_by_module`），並反推「正在組裝的這個檔案自己屬於哪個模組」（`_module_for_file_path()`，`app/{routers|services|repositories}/{module}_{layer_singular}.py` 這個既有檔名規則的反向操作，`utils`／`_global` 沒有 module 概念、回傳 `None`）。解析 import 時**優先**查「這個檔案自己所屬模組」的 schema 定義，命中就直接用同一個模組的路徑，不查可能已經被覆寫過的全域索引；查不到（`own_module` 是 `None`，或這個模組自己沒有定義這個 class 名稱）才退回既有的全域索引行為——這保留了「模組 A 的檔案合法引用模組 B 專屬 class」這種既有情境不受影響，只修正「兩個模組各自都有一份同名定義，卻被誤導到別的模組」這個特定衝突。

**填空階段（⑤）有同一個問題的獨立實作，需要各自排除**：`fill_function()` 填入的函式本體是 qwen 自由生成的內容，可能引用簽名以外的名稱，因此有另一段「填空模式：本體 import 解析」（見下方）在骨架階段的兩層規則之外，針對填入的本體重新掃一次遺漏的 import——這段獨立邏輯一樣是全專案自訂型別掃描，一樣需要排除「正在解析的這個檔案自己的頂層符號」，否則重演同一種自我 import。兩處各自維護排除邏輯，是因為兩者的比對時機、資料來源（骨架階段用記憶體中的結構化資料；填空階段用已寫入磁碟的完整檔案 AST）都不同，沒有共用的中間狀態可以合併成一份實作。

### 語法驗證與寫入：逐 `InterfaceSpec` 隔離失敗，不是全域 all-or-nothing

**決策：逐 `InterfaceSpec` 隔離失敗，不是全域 all-or-nothing**：型別字串即使經過正規化，仍可能有殘留的不合法情況（見上方「已知殘留限制」，如萬用字元泛型）。若採全域 all-or-nothing，**任何一個介面的型別字串有問題，會拖累其餘所有正常的介面全部無法產出骨架**，讓 `generate_scaffold()` 對整個專案直接失敗——這不是可以接受的失敗模式，`generate_scaffold()` 一旦失敗，[P]／④／⑤ 全部卡住，風險遠高於「有幾個函式沒骨架」。

**改為逐 `InterfaceSpec` 驗證，個別失敗只跳過那一個函式**：

1. 每個 `InterfaceSpec` 對應的函式簽名（正規化＋渲染後）先各自 `ast.parse()` 一次（用「這個簽名＋`pass`」組出的最小片段驗證，不用等整個檔案組完才驗證）
2. 驗證失敗 → 這個函式**不渲染進檔案**，記進 `generate_scaffold()` 回傳值新增的 `skipped_interfaces` 欄位（`{file_path, class_name, function_name, error}`），繼續處理下一個 `InterfaceSpec`，**不影響同檔案其他函式，也不影響其他檔案**
3. 同一個 `class_name` 分組若所有方法都被跳過（極端情況，理論上不該發生），該 `class` 仍要渲染出來，body 用 `pass` 佔位，維持檔案語法合法——不能因為跳過方法就留下一個空殼 `class Foo:` 沒有 body
4. 所有 `InterfaceSpec`（以及上方「`db_models` 併入」一節描述的每一項 `db_models` 檔案）處理完後，每個組裝完的完整檔案文字**再整份 `ast.parse()` 驗證一次**（防禦性：理論上必然成功，因為只由已個別驗證過的合法片段組成，但寫入前的最後一道檢查成本很低）——這一步才是真正的全域關卡，但這時候失敗代表的是「組裝邏輯本身有 bug」（如合併 Schema 定義段落時 import 沒去重乾淨），不是「某個介面型別字串剛好有問題」，性質不同：這種情況才維持中止回報，不吞掉

**「帶預設值的參數排序錯誤」不算在這裡的隔離範圍內，根因在 05a**：`db: Session = Depends(get_db)`（05a 七章「框架慣例參數」）、或 LLM 決定的框架注入參數（05a 五章「框架注入物件」，可能同樣是 `Depends(...)` 慣例）若排在其他不帶預設值的參數之前，會是 `SyntaxError: parameter without a default follows parameter with a default`。這類錯誤如果真的發生，一樣會被上面步驟 1／4 的 `ast.parse()` 攔下、走 `skipped_interfaces` 隔離流程——**但正常情況下不會發生**，因為 `design_agent/design.py` 的 `_reorder_params_defaults_last()` 在③組出 `InterfaceSpec.params` 的當下就保證帶預設值的參數一律排最後（見 `05a_design_agent_architecture.md` 七章「帶預設值的參數必須排在不帶預設值的參數之後」）。這裡的隔離機制因此對這一類問題而言，跟上方「型別字串正規化」的符號轉換一樣是 defense-in-depth，不是實際承接這個問題的那一層。

**寫入時機**：全部檔案（含所有能建出來的骨架、`db_models` 併入的模型檔案）驗證通過後，一次性寫入磁碟＋git commit（見八章）；步驟 4 的全域檢查失敗才整包不寫、直接中止回報。

**回傳契約新增 `skipped_interfaces`／`skipped_db_models`**：

```python
{
    "success": bool,       # 步驟 4 的全域檢查通過即為 True，即使 skipped_interfaces／skipped_db_models 非空
    "error": str | None,   # 只在 success=False 時有值（步驟 4 失敗，或環境前提不滿足，見二、九章）
    "skipped_interfaces": list[dict],  # [{file_path, class_name, function_name, error}, ...]，
                                        # 空清單代表這次沒有任何介面被跳過
    "skipped_db_models": list[dict],   # [{file_path, error}, ...]，見「db_models：④ 自行取得的 DB
                                        # schema 內容如何併入」一節，空清單代表沒有任何 db_models 項目被跳過
}
```

`success=True` 但 `skipped_interfaces`／`skipped_db_models` 非空是**預期中會發生、需要人工留意但不阻擋 pipeline** 的情況——呼應 00 十二章／02a「`excluded_folders`」同一種設計精神（非空不代表失敗，是「這次沒完整涵蓋，要留意」的訊號）。`skipped_interfaces` 裡列出的介面，對應的 ⑤ task 之後呼叫 `fill_function()` 時會在六章步驟 4「找不到目標函式」自然失敗、回報明確錯誤（`class_name`／`function_name`／`target_file` 都對得上，不是憑空找不到），不需要 `generate_scaffold()` 自己額外處理這個下游後果。

---

## 五、填空模式：`fill_function(python_project_path, task_id, target_file, class_name, function_name, translator_backend, java_source, referenced_source, description, context, context_files)`

### 為什麼 `fill_function` 需要 `class_name`／`function_name`

`InterfaceSpec.file_path` 是**每個函式各自的路徑，不是每個檔案各自唯一**——`app/routers/user_router.py` 這一個檔案底下正常會有多個 router 函式，對應多個不同的 `InterfaceSpec`／task。06a 三章「涵蓋率規則」訂為「每一個 `InterfaceSpec` 恰好對應一個 task（1:1）」，代表**多個 task 的 `target_files[0]` 會是同一個檔案路徑**——若 `fill_function()` 只拿到 `target_file`，沒有辦法知道這次要填的是檔案裡的哪一個函式。

`TaskSpec` 因此攜帶 `class_name`／`function_name` 兩個欄位（見 06a 八章），translator-cli 執行期靠這個三元組定位函式；`depends_on`／`target_files` 組裝、涵蓋率驗證等 06a 其餘演算法不受影響，純粹多帶兩個欄位。

### 為什麼是 `java_source`／`referenced_source`，不是 `description`

00 六章、06a 一章已定調：舊版由 [P] 呼叫 Claude API 把 Java 業務邏輯整合成 `description` 語意摘要、⑤ 只吃這份摘要的設計已淘汰——摘要本身就是失真來源（傳值有各種可能失誤，看真實原始碼比較準）。新設計下 ⑤ 直接讀「呼叫鏈 + 完整真實原始碼」：

- `java_source`：這個 task 自己對應的 Java 方法**所在的整個檔案**（未經任何 LLM 摘要，也不是只抽這一個方法）。
- `referenced_source`：06a 六章 `reference_targets` 界定出的呼叫鏈範圍，逐項補上真實原始碼文字——`language="java"` 的項目讀真實 `.java` 原始碼，`language="python"` 的項目讀已翻譯完成的真實 `.py` 原始碼（該檔案在全域關卡保證下已由前一段翻譯完畢，見 06a 六章）。

**這兩個欄位的組裝不屬於 translator-cli 的範圍**：把 `TaskSpec.java_method_id`／`reference_targets` 這兩個座標（06a 六章）解析成實際原始碼文字，需要讀取 Java 原始碼、或讀取目標 Python 檔案，這件事由呼叫方（⑤ `implement_node.py`，經 `graph/java_source_extraction.py`，見 09a）在呼叫 `fill_function()` 之前做完，`fill_function()` 收到的已經是組好的文字內容，不做任何抽取——維持二章「translator-cli 只定義被呼叫端契約」的邊界，也維持 `translator_cli/` 不 import `graph.state` 的既有獨立性（見十二章）。

**`java_source` 是整個檔案，不是只抽這一個方法**：真實 `lang-exam-api-refactor` 專案的 90 個 `.java` 檔案，每個檔案恰好一個頂層 class，0 個例外——「整個 class」在這個專案裡就是「整個檔案」。`graph/java_source_extraction.py` 因此直接 `Path.read_text()` 讀整份，不用 javalang 定位起始行＋手動配對大括號去切出單一方法——後者是自訂解析邏輯，錯誤率無法保證是零；讀整個檔案不需要任何解析或配對，錯誤率趨近於零。附帶效果：⑤ 翻譯某個方法時，天然看得到同一個 class 內其他方法的真實原始碼，不依賴呼叫鏈有沒有正確解析出同 class 內的呼叫邊。`referenced_source`（可能有好幾筆，上限 `PLAN_AGENT_MAX_REFERENCE_TARGETS`）維持單方法抽取為主，總量超過門檻（`REFERENCED_SOURCE_TRIM_THRESHOLD_BYTES`，預設 13000 bytes，環境變數可調）才個別項目退回單方法抽取——這裡才是真的需要控制大小的地方，真實資料目前遠低於門檻（單一 task 最多 4 筆 reference_targets，最大 Java 檔案 16KB）。

### `translator_backend`：依此分派 qwen 或 Claude API

`TaskSpec.translator_backend`（06a 五章機械決定：repository 層固定 `"qwen"`，其餘固定 `"claude"`）原樣透傳進來，`fill_function()` 依這個值決定走哪一條模型呼叫路徑（見七章），下游的 AST 定位／替換／驗證／commit（六、八、九章）完全共用，跟 backend 無關。

### 輸入契約

| 參數 | 來源 | 說明 |
|---|---|---|
| `python_project_path` | `state["python_project_path"]` | translator-cli 寫入目標的 repo 根目錄，見二章「新輸入：`python_project_path`」 |
| `task_id` | `task.id` | 06a 八章 `TaskSpec.id`（`task_{:03d}` 格式），供八章 commit 訊息使用，見八章「`task_id` 是必要引數」 |
| `target_file` | `task.target_files[0]` | 相對路徑，如 `app/repositories/user_repository.py` |
| `class_name` | `task.class_name` | `None` 代表 routers 層自由函式 |
| `function_name` | `task.function_name` | 連同 `class_name` 唯一定位這次要填的函式（見上方說明） |
| `translator_backend` | `task.translator_backend` | `"qwen"` 或 `"claude"`，見上方「依此分派」，決定七章走哪條模型呼叫路徑 |
| `java_source` | 呼叫方（⑤／`graph/java_source_extraction.py`）依 `task.java_method_id` 讀出的整個 Java 檔案內容 | 見上方「為什麼是 `java_source`／`referenced_source`」「`java_source` 是整個檔案」 |
| `referenced_source` | 呼叫方（⑤／`graph/java_source_extraction.py`）依 `task.reference_targets` 逐項讀出的真實原始碼 | `list[ReferencedSourceItem]`（`translator_cli/types.py` 新增型別，見十一章），每項含 `file_path`／`class_name`／`function_name`／`language`／`source` |
| `description` | `task.description` | 純機械模板字串（06a 五章：`"填入 {file_path} 的 {class_name}.{function_name}()"`），只供 log／除錯人眼辨識用途，**不送進模型 prompt**（見七章「Prompt 組裝」） |
| `context` | `task.context` | 呼叫鏈原始碼展開涵蓋不到、但 [P] 能機械算出的補充事實（06a 五章，目前唯一內容：`config_field_mappings` 環境變數對應提示），現況 `implement_node.py` 尚未傳遞這個欄位，需要補上（見二章「與既有程式碼的介面異動」第 2 項） |
| `context_files` | `task.target_files` | 這次呼叫要讀進 prompt 的檔案清單（`target_files[0]` 自己＋06a 八章組裝進來的 schema／model 檔案），控制 context 大小的關鍵（00 六章）。**不再包含「因引用而拉進來的跨檔案函式」**——那是 `referenced_source` 的職責，`context_files` 縮小回「這個函式所在檔案本身需要的結構性依賴」。**讀取容錯見七章「User / System Prompt 組裝」** |

### 輸出契約

```python
class FillResult:
    success: bool
    error: str | None
    diff: str   # 這次 commit 的 git diff 全文，供人工／⑦ Debug Agent 事後追溯這次改了什麼（見八章），
                # 不是 scheduler 的 regression 偵測依據——那邊只需要 target_files[0] 這個路徑本身
                # （見 graph/scheduler.py module_owned_files 註解），不需要讀 diff 內容
```

`graph/scheduler.py` 的 `check_upstream_regression()` 只用檔案路徑判斷 regression，不看 `diff` 內容——`diff` 純粹是留給人工或未來 ⑦ Debug Agent 事後追溯某個 task 實際改了什麼的資訊。

### 呼叫失敗時不寫入任何內容

比照四章「語法驗證與寫入」的原則：`fill_function()` 全程在記憶體中組出新的檔案內容、驗證通過才落地寫入＋commit；任何一步失敗（模型呼叫失敗、delimiter 契約違反、AST 解析失敗，見六、七章）都**不碰磁碟**，直接回傳 `FillResult(success=False, error=...)`。這個保證是九章「衝突偵測」成立的前提——只要失敗路徑保證不寫入，working tree 在失敗後仍然乾淨，下一次呼叫（不論是同一個 task 重跑，或另一個 task）看到的都是最後一次成功 commit 的狀態，不需要額外清理步驟。

### 冪等：允許對已有內容的函式重新填空

`fill_function()` 定位到的函式節點，body 可能是骨架階段的 `pass`（第一次填），也可能已經是先前一次成功呼叫留下的真實邏輯（同一個 task 因為某種原因被重新呼叫）。AST 定位＋替換（六章）不對 body 現有內容做任何假設，兩種情況處理方式完全一樣——直接整個替換。這不是為了目前已知的某個重試流程設計（⑤／⑦ 的重試語意見 09a／10a），而是讓 translator-cli 自己的契約不論上游怎麼重跑都行為一致、不需要額外狀態判斷。

### 填空模式：本體 import 解析

`fill_function()` 填入的函式本體是 qwen 自由生成的內容，可能引用四章「已知關鍵字表」／「自訂型別索引」在骨架階段從未見過的名稱——那兩層機制分析的對象是函式**簽名**（`params`／`return_type`），骨架生成當下函式本體還是空的 `pass`，沒有內容可以分析；真正會用到哪些名稱，要等 qwen 自由決定寫法之後才知道。常見情況：呼叫跨檔案的自訂類別（如 `UserRepository`）、拋出框架例外（如 `HTTPException`）——這兩種名稱都不會出現在函式簽名裡，只會出現在 qwen 自己決定的實作寫法中。

**決策：填空完成後（AST 替換之後、`render()` 之前）重新掃一次新插入的本體，補上遺漏的 import**：

1. 對新本體的 AST 走一遍，收集所有 Load context 的裸名稱，扣掉這個函式本身已經綁定的名稱（參數、`self`）與 Python builtin
2. 對照檔案現有的 import 陳述式，扣掉已經 import 過的名稱
3. 剩下的候選名稱，比照四章的兩層機制比對：
   - **已知關鍵字表**：精確集合成員判斷，不是子字串比對——候選名稱已經是逐一拆開的裸識別字（AST `Name` 節點），不需要在一段文字裡搜尋子字串，精確比對本身就是最精確的判斷方式
   - **自訂型別索引**：改成直接掃描磁碟上 `app/` 底下所有 `.py` 檔案的頂層 class 定義**，以及模組層級的 `Table(...)` 變數賦值**（同四章來源三「`Table(...)` 變數賦值」，08a 八章 `@ManyToMany` 中介表沒有對應 class，qwen 生成的本體若直接操作這張表，引用的是這個變數名，不是 class 名稱），不是沿用骨架階段記憶體裡的三來源索引——填空階段沒有 `python_structure` 可用（`fill_function()` 刻意不依賴這份輸入，跟二章「`python_project_path` 是顯式引數」的獨立性原則同構），掃描磁碟是唯一能拿到完整類別清單的方式，而且天然反映最新狀態（含同一次 pipeline 執行中先前 task 已經填入的內容）
4. 兩層都比對不到的名稱，維持四章「保守不加、不猜」的既有原則——不主動修正，留給 Harness 的 module 局部驗證與 ⑦ Debug Agent 處理

這是接上真實 qwen2.5-coder:32b、對真實 Java 專案跑過完整 pipeline 才發現的缺口，不是設計初期就預見的——已對真實案例驗證修正有效（router 層函式呼叫 `UserRepository`／拋出 `HTTPException`，兩者都正確補上 import，語法驗證通過）。記錄在這裡是因為它改變了 `fill_function()` 的既有流程（骨架階段的 import 解析只跑一次，填空階段現在還會再跑一次），不只是實作細節。

**決策：候選名稱要再排除「正在解析的這個檔案自己的頂層符號」，理由跟四章「自訂型別索引命中要再排除同檔案 class」同一種問題**：真實端對端驗證發現，qwen 生成的本體同樣可能引用「同一個檔案裡稍後才定義的另一個 class 或函式」——磁碟掃描出的自訂型別索引一樣涵蓋全專案，一樣會收錄這個檔案自己定義的符號，不排除會產生 `from X import X` 這種恆為 True 的自我 import，一樣導致 `ImportError`。這裡的排除範圍比四章的骨架階段更廣：除了頂層 `class`／`def`（含 `async def`），還額外排除頂層變數賦值（`x = ...` 這種 `ast.Assign`）——填空階段是對已寫入磁碟的完整檔案操作，檔案裡本來就可能有骨架階段就存在、或先前 task 已填入的模組層級變數，範圍比骨架階段（當時檔案裡只有函式簽名與 `pass`，不會有頂層變數賦值）更廣。跟四章的 `same_file_class_names` 排除是同一個問題、兩個不同時機點的獨立實作，因為兩者的比對時機與資料來源不同（見四章末段說明），沒有共用的中間狀態可以合併。

---

## 六、AST 插入機制

`generate_scaffold()`／`fill_function()` 都經由同一層 Python adapter（見十章）操作 `ast` 模組，不做任何文字層級的 diff／patch——00 三章「核心設計是『填空式』輸出契約...由 CLI 用 AST 精準插入，而非依賴模型生成可精確比對原文的 diff」在這裡具體落地：

### 定位

```
1. source = target_file 現有內容（Path.read_text(encoding="utf-8")，見 00 六章「檔案讀寫編碼慣例」）
   ——路徑先解析成 Path(python_project_path) / target_file（見二章「python_project_path 是顯式引數」）；
   讀取遇到 FileNotFoundError（如該檔案因④「語法驗證與寫入」的 skipped_interfaces／skipped_db_models
   隔離失敗而從未被建立）→ 直接視為與步驟 4 同一類「scaffold/task 不一致」，回傳
   FillResult(success=False, error="scaffold/task 不一致：{target_file} 不存在")，不是任由
   FileNotFoundError 這種作業系統例外原樣往外拋、繞過 FillResult 的錯誤回報契約
2. tree = ast.parse(source)
3. 若 class_name 為 None：在 tree.body 找 FunctionDef／AsyncFunctionDef，name == function_name
   若 class_name 非 None：先在 tree.body 找 ClassDef，name == class_name；
                          再在該 ClassDef.body 找 FunctionDef／AsyncFunctionDef，name == function_name
4. 找不到 → FillResult(success=False, error="scaffold/task 不一致：{class_name}.{function_name} 在
   {target_file} 找不到")——這是防禦性的最後一道檢查（06a 已在上游用「05a 對多載的消歧」＋「1:1 涵蓋率」
   兩道機制保證這個三元組理論上必然存在，這裡觸發代表更上游的資料有 bug，不是可以重試化解的情況）
```

### 替換

```
5. 依 `translator_backend` 呼叫 qwen（經 ollama）或 Claude API（七章），取得 body_text
6. body_tree = ast.parse(body_text)   # body_text 是模型回傳的「未縮排」陳述式集合，見七章「取得 body_text」
6a. 若 not body_tree.body（回傳內容只有空白／換行，沒有任何陳述式）→ 視為與 body_text 格式
    錯誤同一類，觸發七章「兩條路徑共用：body_text 格式修正重試」，不進入步驟 7——
    空 body 對 ast.parse(body_text) 本身是合法輸入（空字串／純空白等同一個空 module，body=[]），
    這裡不特別攔下的話，會一路走到步驟 7 把空清單指定給函式節點的 body，Python 函式節點不允許空
    body（至少要有一個陳述式），ast.unparse() 在這個情況下不會報錯，而是直接產出「有簽名、沒有
    本體」的殘缺程式碼（如 `def foo(x: int) -> int:` 後面什麼都沒有），要等步驟 10 重新 parse
    這段輸出時才會炸 SyntaxError——但那時候已經不在七章既有的重試觸發條件（「body_text 通不過
    ast.parse()」）涵蓋範圍內，因為 body_text 自己是能被 parse 的。因此必須在步驟 6 之後、
    步驟 7 之前就攔下，不能依賴步驟 10 的最終檢查
6b. 若 body_tree.body 裡**任何一筆**陳述式是 FunctionDef／AsyncFunctionDef，且其 .name ==
    function_name → 視為「模型重複輸出函式簽名」（模型把整個函式簽名連同本體一起包進 delimiter，
    而不是只回傳本體陳述式）。這在 AST 層級是合法的巢狀函式定義，不會被步驟 6a 或後續的
    ast.parse() 驗證攔到——替換後目標函式的 body 會變成「宣告一個從未被呼叫的同名巢狀函式」，
    語法完全合法但語意錯誤，這個錯誤能通過步驟 10 的驗證，卻不是正確的實作。
    命中 → 視為與 delimiter 抽取失敗同一類「模型輸出格式錯誤」，觸發七章修正重試，不進入步驟 7、
    不嘗試自動剝離取 .body[0].body——保持跟 6a 一致的處理方式：格式錯誤一律回饋給模型自己修正，
    不靜默接受再加工過的內容。**2026-08：判斷式原本寫成「body_tree.body 恰好只有一筆陳述式、
    且是同名巢狀函式」，只在整段 body 只有這一筆時才觸發**——真實案例（`docs/09b_bug_trace.md
    #52`）撞到 ⑦ Debug Agent 的 `fixed_body` 開頭夾帶了 5 行 import，同名巢狀函式定義只是
    第 6 筆陳述式，`len(body)==1` 不成立，這道防線沒攔住，巢狀污染直接寫入磁碟。放寬成「不論
    混在多少其他陳述式之間都算」才是目前這裡描述的版本；#52 也連帶暴露 `known_fill_failures`
    以外的呼叫路徑（⑦ 的 `fixed_body`，見 10a 八章）跟 ⑤ 本地模型走的是同一段
    `extract_body_statements()` 檢查，不是各自獨立的防線
7. 目標函式節點.body = body_tree.body
8. ast.fix_missing_locations(tree)
9. new_source = ast.unparse(tree)
10. ast.parse(new_source) 再驗證一次（防禦性：理論上必然成功，因為是從合法 AST 反渲染回來的，
    且步驟 6a／6b 已經擋掉空 body、簽名重複輸出這兩個已知的例外，但寫入磁碟前的最後一道檢查成本
    很低，值得留著）
11. 驗證通過 → 寫入 target_file，見八章 commit
```

**`ast.unparse()` 會重新格式化整個檔案，不是只改動被替換的那個函式**：這是刻意接受的行為，不是需要避免的副作用——`generate_scaffold()` 產生的初始骨架也一律經過同一個 `ast.parse → ast.unparse` 流程產生（而不是像 `design_agent/layout.py` 那樣手動組字串），確保骨架階段與填空階段的格式化風格**天生一致**，同一顆 Python 版本下 `ast.unparse()` 對同一棵 AST 的輸出是確定性的，因此除了這次真正改動的函式，檔案其餘部分每次重新渲染都會得到一模一樣的文字，`git diff` 實際看到的改動範圍仍然乾淨、只集中在被填的那個函式。

### 取得 `body_text`：qwen delimiter 契約／Claude Structured Outputs

**qwen 路徑**：透過 chat completion 回傳的是自然語言夾雜程式碼的文字，不能直接假設回傳內容剛好等於一段可以直接 `ast.parse()` 的函式本體（可能有解說文字、可能用 ` ```python ` fence 包、可能兩者都有或都沒有）。System prompt 固定要求模型把函式本體包在兩個字面 sentinel 之間，且**只**回傳這個區間內的內容作為程式碼：

```
<<<TRANSLATOR_CLI_BODY_START>>>
statement_1
statement_2
<<<TRANSLATOR_CLI_BODY_END>>>
```

抽取邏輯：正則找兩個 sentinel 之間的文字，得到 `body_text`。找不到 sentinel（模型沒有遵守格式）→ 視為「delimiter 契約違反」，見七章重試策略。

**Claude 路徑**：不需要 delimiter——七章「Claude 路徑：連線方式」的 JSON Schema 已經把輸出形狀鎖死成 `{"body_statements": "..."}`，`call_claude_for_json()` 回傳的物件直接取 `response["body_statements"]` 就是 `body_text`，沒有正則抽取、也沒有「找不到 sentinel」這種失敗模式。

兩條路徑取得的 `body_text` 之後走同一套流程（步驟 6 起）。

**body 內容要求「未縮排」**（兩條路徑的 system prompt 都這樣要求，見七章 System prompt 內容）：模型回傳的陳述式視為函式本體的第一層陳述式（如同它們是模組頂層程式碼），不要求模型自己判斷「這是被塞進一層 class 方法還是自由函式，該縮 4 格還是 8 格」——這個判斷模型容易出錯（尤其 routers／services/repositories 三層縮排深度不同）。`body_text` 直接 `ast.parse()`（等同解析成一個模組），取得的 `.body` 就是這個函式本體要用的陳述式清單，縮排完全由 `ast.unparse()` 在第 9 步依實際巢狀深度重新計算，不依賴模型自己的縮排是否正確。

---

## 七、模型呼叫：qwen（Ollama）／Claude API 雙後端

### 分派方式

`fill_function()` 收到的 `translator_backend`（05a 沿用、06a 五章機械決定：repository 層固定 `"qwen"`，其餘固定 `"claude"`）決定走以下哪一條路徑取得 `body_text`（未縮排的函式本體陳述式文字）；取得 `body_text` 之後，六章步驟 6～11（AST 替換／6a 空本體檢查／6b 簽名重複輸出檢查／驗證／寫入）**完全共用同一套邏輯，不分 backend**——兩條路徑的差異只在「怎麼把 prompt 送出去、怎麼把回應抽成 `body_text`」，不影響下游。

### qwen 路徑：Ollama 連線方式

沿用 00 三、四、五章已定案的架構：translator-cli 不直接打 ollama，而是打 `.env` 的 `OLLAMA_BASE_URL`（指向另一台 Mac 上的 nginx，已含 `/v1` 路徑前綴），帶 `Authorization: Bearer {OLLAMA_API_KEY}`，nginx 驗證通過後才轉發給 ollama 本機的 `qwen2.5-coder:32b`。使用 `httpx.AsyncClient`（`requirements.txt` 已有此依賴，不需新增）呼叫 OpenAI 相容的 `POST {OLLAMA_BASE_URL}/chat/completions`。

`model` 欄位固定填 `"qwen2.5-coder:32b"`（Python 常數，不經環境變數——00 三章「硬體限制」已經把模型選型釘死為單一選擇，不是每個 Agent 各自可調的東西，跟 `common/llm_client.py` 對 Claude 模型「呼叫端自己決定用哪個模型」的分工原則刻意不同：Claude 那邊有多個模型可選、多個 Agent 各自決定；qwen 這邊實體機器只跑得動一顆模型，沒有「選哪個」的問題）。回應是自然語言夾雜程式碼的文字，靠 delimiter sentinel 抽取出 `body_text`（見下方「delimiter 契約」）。

`translator_cli/ollama_client.py` 內部的 `OLLAMA_MODEL_SEMAPHORE(1)` 只包住 qwen 這條路徑——實體機器只跑得動一顆 qwen 模型，同時間只能有一個 qwen 請求在飛（見八章「寫入段序列化」，這個號誌不是外部呼叫端包的，而是收在 translator-cli 內部自己管理）；Claude 路徑不受這個號誌限制，理由見下方「Claude 路徑」。

### Claude 路徑：連線方式

`translator_backend == "claude"` 時，直接呼叫既有的 `common/llm_client.py::call_claude_for_json()`（[③]／[P] 舊版已在用的同一支共用 wrapper，含 `llm_trace.py` 呼叫紀錄，見專案 `CLAUDE.md` 的 `llmlog` 查詢說明），不新增另一套 Claude 連線邏輯。使用 Structured Outputs（`output_config.format` 帶 JSON Schema constrained decoding），schema 固定為：

```json
{
  "type": "object",
  "properties": {
    "body_statements": {"type": "string"}
  },
  "required": ["body_statements"],
  "additionalProperties": false
}
```

`body_statements` 就是六章要拿去 `ast.parse()` 的 `body_text`——不需要 qwen 路徑的 delimiter sentinel 正則抽取，schema constrained decoding 已經保證回應是這個形狀的合法 JSON，`response["body_statements"]` 直接可用（六章步驟 6a／6b 的空本體／簽名重複輸出檢查仍然套用，這兩項檢查的對象是 `body_text` 的內容本身，不是抽取機制）。

Claude 路徑的**模型呼叫本身**不受 qwen 那種單模型限制——Claude API 是雲端服務，多個 task 平行呼叫模型不衝突，也是導入雙後端本來就要換到的效能收益（真實資料 67/78 task 走 Claude，若整體仍序列化，等於白導入雙後端）。**寫入磁碟／commit 這一小段仍然跨 backend 序列化**，理由與鎖機制設計見八章「寫入段序列化」——「模型呼叫平行、寫入序列化」是刻意分開的兩件事：平行的是慢、不碰共用狀態的部分（生成通常數秒到數十秒），序列化的是快、會動到同一個 git repo 的部分（AST 替換＋寫檔＋commit 通常次秒等級），不會因為序列化寫入段而抵銷平行呼叫模型帶來的效能收益。

### qwen 路徑：Timeout 與重試策略

本地模型單次生成可能需要數十秒到數分鐘（視函式複雜度），且 fill_function 每次呼叫都是單一函式、體積遠小於 [P]/[③] 那種一次處理整個 module 的 Claude 呼叫，重試策略因此比 04a/05a 的「5 分鐘後重試一次」更輕量、更快：

| 常數 | 預設值 | 說明 |
|---|---|---|
| `TRANSLATOR_CLI_TIMEOUT_SECONDS` | `300`（環境變數可調） | 單次 HTTP 呼叫的 timeout，本地模型生成時間比雲端 API 長，需要比 Claude 呼叫更寬鬆的預設值 |
| `TRANSLATOR_CLI_NETWORK_RETRIES` | `2`（環境變數可調） | HTTP 連線失敗／timeout 這類**傳輸層**錯誤的立即重試次數，固定間隔 `3` 秒——這是 LAN 內部連線的暫時抖動，不是 Claude API 那種需要等配額恢復的限流情境，不需要 5 分鐘等待 |
| delimiter／語法驗證失敗重試 | 固定 `2` 次 | 見下方「兩條路徑共用：body_text 格式修正重試」，跟網路層重試是不同的錯誤類型、不同的重試機制，不共用同一個計數器——對 context 檔案多、邏輯複雜的任務，第一次回應違反格式的機率不低，固定重試 1 次不夠 |

**qwen 路徑特有：delimiter 抽取失敗觸發重試**——找不到 sentinel（見下方「delimiter 契約：qwen 回應格式」）時，重新呼叫一次 qwen，user prompt 額外附上「上一次回應違反格式，請重新產生，務必遵守 delimiter 格式」。Claude 路徑不會遇到這種錯誤——Structured Outputs 的 schema constrained decoding 保證回應必然是合法 JSON、必然帶 `body_statements` 欄位，沒有「抽不到」這種失敗模式。

### 兩條路徑共用：`body_text` 格式修正重試

不論 `body_text` 是怎麼取得的（qwen 的 delimiter 抽取，或 Claude 的 `response["body_statements"]`），六章對 `body_text` 內容本身的三項檢查完全共用，任一項命中都**重新呼叫一次模型**（同一個 backend，這次的 prompt 額外附上上一次的錯誤訊息，要求重新產生）：

1. `body_text` 通不過 `ast.parse()`（`SyntaxError`）
2. `body_text` 能 `ast.parse()`，但解析出的陳述式清單是空的（六章步驟 6a「空本體」）——這個情況單獨列成觸發條件，不能只看「`body_text` 通不過 `ast.parse()`」就以為涵蓋了它：空字串本身是合法的 Python（等同空 module），`ast.parse()` 不會報錯，只有把這個空 body 塞進函式節點、重新 `ast.unparse()` 再 `ast.parse()` 之後才會炸，若不單獨攔，這個錯誤會晚兩步才被發現、而且繞過了這裡的重試機制，見六章步驟 6a 完整說明
3. `body_text` 解析出的陳述式清單裡任何一筆是與 `function_name` 同名的 FunctionDef／AsyncFunctionDef（六章步驟 6b「簽名重複輸出」——模型把整個函式簽名連同本體一起包進回應，而不是只回傳本體陳述式；不限於「整段清單恰好只有這一筆」才算，見 `docs/09b_bug_trace.md #52` 放寬理由）——這在 AST 層級是合法的巢狀函式定義，不會被上一項檢查攔到，需單獨判斷，見六章步驟 6b 完整說明

兩次重試仍然失敗（不論是同一種錯誤還是不同錯誤，qwen 路徑的話連同「delimiter 抽取失敗」共用同一個計數器）→ 放棄，`FillResult(success=False, error=...)`，不無限重試——固定重試次數（而非無限重試）的理由跟 04a 的「一次性的固定延遲已經夠用」同構：這是扛過模型偶發輸出品質不穩的緩衝，不是要取代 ⑦ Debug Agent 那種「業務邏輯寫錯了」層級的修正機制。次數從最初的 1 次調整為 2 次，是接上真實 qwen2.5-coder:32b 後的實測校準（見上方表格），Claude 路徑沿用同一個上限，尚未有真實資料顯示需要另外校準。

### System / User Prompt 組裝

**核心指示「呼叫，不要內嵌」必須同時出現在兩條路徑的 system prompt**——06a 六章已定案：`referenced_source`（含 `java_source` 自身呼叫到的、以及呼叫鏈展開出的同層／已完成函式）給模型看是為了「知道該怎麼正確呼叫」，不是給模型「拿去合併改寫」；模型看了真實原始碼卻整段複製貼上而不是呼叫，一樣達不到「基底穩、上層調用」的目的，因此明確寫進兩條路徑各自的 system prompt，不能只在 06a 文件裡交代、不落地到實際 prompt 內容。

**qwen 路徑** system prompt 固定模板：

```
你是一個 Python 程式碼填空工具。你會收到一個已經定義好簽名的 Python 函式（骨架已建好，
可能是空的 `pass`，也可能已有既有邏輯），以及這個函式對應的真實原始碼、以及它呼叫到的其他
函式的真實原始碼（Java 或已翻譯完成的 Python）。

你的任務：把對應的 Java 邏輯改寫成正確的 Python 實作。你收到的其他函式原始碼是給你參考
「該怎麼呼叫它」（參數、回傳型別、行為），不是要你把那些函式的邏輯合併或複製進來——你的
實作應該真正呼叫（import + invoke）那些函式，就像原始 Java 程式碼本來就是這樣呼叫的一樣。

只回傳這個函式的「本體」陳述式，不要重複函式簽名、不要重複裝飾器、不要加任何解說文字。
回傳格式固定如下，只在這兩個標記之間寫程式碼，標記本身也要原樣附上：

<<<TRANSLATOR_CLI_BODY_START>>>
（函式本體陳述式，視為第一層縮排，不要自己加縮排）
<<<TRANSLATOR_CLI_BODY_END>>>
```

**Claude 路徑** system prompt 固定模板（不需要 delimiter 段落，Structured Outputs 已保證輸出形狀）：

```
你是一個 Python 程式碼填空工具。你會收到一個已經定義好簽名的 Python 函式（骨架已建好，
可能是空的 `pass`，也可能已有既有邏輯），以及這個函式對應的真實原始碼、以及它呼叫到的其他
函式的真實原始碼（Java 或已翻譯完成的 Python）。

你的任務：把對應的 Java 邏輯改寫成正確的 Python 實作。你收到的其他函式原始碼是給你參考
「該怎麼呼叫它」（參數、回傳型別、行為），不是要你把那些函式的邏輯合併或複製進來——你的
實作應該真正呼叫（import + invoke）那些函式，就像原始 Java 程式碼本來就是這樣呼叫的一樣。

只回傳這個函式的「本體」陳述式，不要重複函式簽名、不要重複裝飾器、不要加任何解說文字。
```

兩條路徑的 User prompt 組裝內容相同：函式目前的簽名（第六章第 3 步定位到的節點，取 `ast.unparse()` 只渲染這個函式的 `decorator_list`＋簽名列，不含 body，讓模型知道自己在填什麼形狀的函式）、`java_source`（標明「這是這個函式對應的原始 Java 方法」）、`referenced_source` 逐項列出「`file_path` / `class_name` / `function_name` / `language` + 原始碼內容」（標明每項是 Java 還是已翻譯 Python）、`context`（若非空）、`context_files` 逐檔案列出「路徑 + 完整內容」。**`description` 不放進 prompt**（見五章，純 log 用途）。

**`context_files` 讀取容錯**：`context_files`（=`task.target_files` 全部清單）逐項 `open()` 讀取時，除了 `target_file` 本身（= `context_files[0]`，六章第 1 步已經確認存在，不會在這裡才踩到）以外，其餘項目**不保證檔案一定存在**——06a 八章對 services／repositories 層的 task 無條件把該 module 的 `models/{module}.py` 加進 `target_files`，但這個檔案只在④透過上方「`db_models` 併入」機制實際產出時才存在；沒有對應 DB 表的模組（純外部 API 串接）、或該筆 `db_models` 因型別驗證失敗被跳過（見四章 `skipped_db_models`），都會讓這個路徑合法地不存在。因此組裝這段內容時，逐項 `open()` 遇到 `FileNotFoundError` → **記一筆警告（含 task 識別資訊與該路徑），跳過這個檔案，繼續組裝其餘 `context_files`**，不是讓作業系統例外直接中斷整個 `fill_function()` 呼叫——這不是「隱藏錯誤」，被跳過的檔案本來就不該存在於這次任務的合理輸入裡；真正需要修 bug 的情況（`target_file` 本身不存在）由六章第 1 步的既有檢查把關，不會被這條容錯規則吞掉。

---

## 八、Git Snapshot 與 Commit 顆粒度

translator-cli 對 git 的使用須滿足 00 三章的要求：「寫入前後搭配 git snapshot 與語法驗證，確保每個 task 的異動可追蹤、可回滾。」本章（顆粒度）負責「可追蹤、可回滾」，九章（衝突偵測）負責防止 working tree 假設跟磁碟實際狀態不一致時靜默覆蓋掉別人的東西——這是 Harness 的功能驗證完全不涵蓋的另一種風險（改了什麼、何時改的、有沒有意外覆蓋人工修正）。

### 決策：每個 task 一個 commit，寫入段用細粒度鎖序列化

**顆粒度**（所有 `git` 指令一律以呼叫端傳入的 `python_project_path` 為 repo 根目錄，如 `git -C {python_project_path} add -A`，不是 translator-cli 自己執行時的 cwd）：
- `generate_scaffold()` 成功寫入所有骨架檔案（含 `db_models` 併入的內容，見四章）後，`git add -A && git commit -m "scaffold: initial skeleton from python_structure"` 一次性 commit，作為後續所有 `fill_function()` commit 的共同基礎
- `fill_function()` 每次成功寫入後，`git add {target_file} && git commit -m "implement: {task_id} fill {class_name}.{function_name} in {target_file}"`（`class_name` 為 `None` 時省略該段，寫成 `fill {function_name} in {target_file}`）——`task_id` 現在是必要引數（見五章「輸入契約」），不再是「有就用、沒有退回 class_name.function_name」的 fallback；同時保留 `class_name.function_name` 是為了讓 `git log` 一眼看出這個 commit 改的是哪個函式，不需要另外反查 `task_id` 對應什麼。**只 add 這次實際寫入的那一個檔案**，不用 `-A`：即使working tree因為某種原因存在其他未預期的變更（理論上不該發生，見九章），也不會被這次 commit 意外一起帶走
- `FillResult.diff`（見五章輸出契約）在 `git add` 之前擷取：`git -C {python_project_path} diff -- {target_file}`——此時檔案已寫入磁碟但尚未進 staging area，working tree 依九章 precondition 保證動筆前是乾淨的，這個 diff 精確反映這次 `fill_function()` 造成的變更。固定順序為：寫入 target_file → 擷取 diff → `git add` → `git commit`；顛倒成先 commit 再擷取，working tree 會回到與 HEAD 一致，`git diff` 只會拿到空字串

**為什麼不能只靠「結構上不存在並行寫入」這個論證**：Claude 路徑刻意讓模型呼叫可以平行（見七章「Claude 路徑：連線方式」，這是雙後端要換到的效能收益——同一 phase 內平行執行沒問題，但 qwen 本地模型不可以平行，實體機器硬體撐不住），代表**同一 phase 內多個 Claude task 可能同時執行到 `fill_function()`**——雖然大多數情況下同一 phase 內的 task 落在不同檔案（`target_file` 不同），彼此寫入不衝突，但 06a 三章「涵蓋率規則」明訂同一個檔案可以對應多個 task（如同一個 router 檔案有多個端點函式），這種情況下確實存在「兩個 task 同時讀到同一份舊內容、各自 AST 替換後寫回、其中一個的改動被覆蓋」的真實 race condition，不能只靠「系統結構上任何時刻最多一個 `fill_function()` 呼叫在跑」帶過。

**決策：鎖機制縮小到只包住寫入段，模型呼叫段不鎖**：

1. `translator_cli` 內部有一個模組層級的寫入鎖（`git_ops.WRITE_LOCK = asyncio.Lock()`），`fill_function()` 依序執行：(a) 組 prompt、呼叫模型取得 `body_text`（六、七章，**不持鎖**——這段是慢、不碰共用磁碟狀態的部分，qwen／Claude 皆然）→ (b) `await` 取得寫入鎖 → (c) 六章步驟 1～11（讀檔、AST 定位／替換、`ast.unparse()`、驗證、寫入、八章 commit）全程持鎖，做完才釋放。**不分 backend**，qwen／Claude 呼叫都走同一把鎖——這段快（次秒等級），序列化不影響整體吞吐量，換來的是「同一時刻只有一個呼叫在動這個 git repo」這件事。
2. qwen 實體機器單模型的序列化收在 `translator_cli/ollama_client.py` 內部自己管理（`OLLAMA_MODEL_SEMAPHORE`，只包住 qwen 路徑實際打 ollama 的那段呼叫），不是外部呼叫端包一層號誌——`implement_node.py` 呼叫 `fill_function()` 不需要知道背後有這個限制，序列化責任完全收進 translator-cli 內部（1. 的寫入鎖＋這裡的模型號誌）。
3. `generate_scaffold()`（④）與 `fill_function()`（⑤）不會同時執行：01 五章「平行分支：① → (② ∥ ③) → ([P] ∥ ④) → ⑤」的圖結構保證 `scaffold` 必須完成（連同 `plan`）才會進入 `implement`，兩者是先後關係。

`generate_scaffold()` 本身不需要鎖——04a／08a 既有邊界下它是 pipeline 唯一一次性呼叫（見二章），不存在多個 coroutine 同時呼叫 `generate_scaffold()` 的情境。

若未來排程設計改變（例如讓多個本地模型實例平行跑，見 00 三章「硬體限制」目前排除這個可能性），qwen 模型號誌需要重新評估是否還要固定 `1`；寫入鎖本身不受這個假設影響——不論 qwen 是否平行，寫入段一律序列化的決策都成立。

---

## 九、衝突偵測

00 三章原文承諾「衝突偵測」，八章已經說明這不是併發鎖的情境；這裡的衝突偵測指的是另一種風險：**translator-cli 對 working tree 目前狀態的假設，跟磁碟上實際狀態不一致**。可能成因：人工不小心手動編輯了目標專案裡的檔案、上一輪執行中途被強制中斷、留下未 commit 的殘留寫入（理論上五章「呼叫失敗時不寫入任何內容」已經保證正常失敗路徑不會留殘留，但例如程序被 kill -9 這種非正常中斷仍可能留下部分寫入）。

**機制：每次寫入前的 precondition 檢查**——`generate_scaffold()`／`fill_function()` 動筆寫任何檔案之前，先跑 `git -C {python_project_path} status --porcelain`（同八章，一律以呼叫端傳入的 `python_project_path` 為 repo 根目錄）：**`fill_function()` 這個檢查要在取得八章「寫入段」的鎖之後才執行**，不能在拿到鎖之前就先查——鎖之前查完、鎖之後才寫，中間仍有另一個 coroutine 插隊寫入的空隙，這個檢查存在的意義就是「確保開始寫的當下 working tree 真的跟預期一致」，必須跟寫入動作在同一段critical section 裡才有效。

- 輸出為空（working tree 乾淨，跟最後一次 commit 完全一致）→ 正常繼續
- 輸出非空 → **不寫入任何內容**，直接回傳失敗（`generate_scaffold()` 回傳 `{"success": False, "error": "..."}`；`fill_function()` 回傳 `FillResult(success=False, error="working tree 不乾淨，拒絕寫入：{git status 輸出}")`），交由人工核對這份意外的變更是什麼

這個檢查成本很低（一次 subprocess 呼叫），但能防止 translator-cli 在一個「假設錯誤」的基礎上動手——例如若 working tree 其實帶著一段人工手動修正、還沒 commit，`ast.parse()` 讀到的 `source` 就已經跟六章假設的「上一次 commit 留下的乾淨骨架／已填函式」不同，AST 定位跟替換仍然「能跑」，但等於在不知情的狀況下覆蓋掉那段人工修正，是比「明確報錯要求人工確認」危險得多的結果。

---

## 十、目標語言 Adapter 介面

00 文件索引已將「目標語言 adapter 介面」列為 07a 的既定內容（見 `00_refactor_architecture.md` 十一章）——這代表把「AST 定位／替換／渲染」這類語言相關的機械操作，跟「git snapshot、ollama 呼叫、delimiter 抽取、衝突偵測」這類跟目標語言無關的通用流程分開，讓未來若要支援 Python 以外的目標語言時，只需要另外實作一個 adapter，不需要碰通用流程。**這個專案目前只有 Python 一種目標語言，不需要也不會建立第二個 adapter 或任何動態選擇機制**——介面本身刻意維持最小，只是一個劃分模組邊界的抽象基底類別，不是插件系統：

```python
class LanguageAdapter(Protocol):
    def parse(self, source: str) -> ast.Module: ...
    def locate_function(
        self, tree: ast.Module, class_name: str | None, function_name: str
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None: ...
    def splice_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef, body_source: str) -> None: ...
    def render(self, tree: ast.Module) -> str: ...
    def validate_syntax(self, source: str) -> None:  # 失敗拋 SyntaxError
        ...
```

`PythonAdapter` 是唯一實作，內部就是六章描述的 `ast` 模組操作。`generate_scaffold()`／`fill_function()` 一律透過這層介面操作，不直接 import `ast`——這樣六章「AST 插入機制」的內容實際上是 `PythonAdapter` 的實作細節，07b 落地時兩者應該對應到同一個模組。

---

## 十一、模組結構規劃

比照 `parse_agent/`／`design_agent/`／`plan_agent/` 的組織方式，translator-cli 的邏輯規劃為既有的 `translator_cli/` 套件，擴充現有的 `client.py`／`types.py`：

```
refactor-project/
└── translator_cli/
    ├── client.py           # 對外唯一入口：generate_scaffold()、fill_function()，依 translator_backend 分派
    ├── types.py            # FillResult（既有）、ReferencedSourceItem（新增，見五章）
    ├── python_adapter.py   # 十章 LanguageAdapter 的 Python 實作：ast 定位／替換／渲染／語法驗證
    ├── scaffold.py         # 四章：directory_tree 解析、interfaces 分組渲染、import 解析、db_models 併入
    ├── ollama_client.py    # 七章 qwen 路徑：httpx 呼叫 ollama（經 nginx）、delimiter 抽取、修正重試、
    │                       # 模組層級 asyncio.Semaphore(1)（實體機器單模型限制，見八章）
    ├── claude_client.py    # 七章 Claude 路徑（新增）：串接 common/llm_client.py::call_claude_for_json()
    ├── prompts.py          # 七章 system/user prompt 模板（兩條路徑共用「呼叫，不要內嵌」核心指示）
    ├── git_ops.py          # 八、九章：commit、git status 衝突偵測、模組層級 asyncio.Lock()（寫入段序列化，見八章）
    └── exceptions.py       # 內部例外型別（比照 design_agent/exceptions.py 既有慣例）
```

| 職責 | 說明 |
|---|---|
| 骨架生成（無模型呼叫） | 對應四章，`scaffold.py` |
| 填空（依 `translator_backend` 分派 qwen／Claude） | 對應五、七章，`client.py` 依 backend 分別串接 `ollama_client.py` 或 `claude_client.py` |
| AST 定位／替換／渲染 | 對應六、十章，`python_adapter.py` |
| git snapshot／commit／衝突偵測 | 對應八、九章，`git_ops.py` |
| 對外唯一入口 | 供 `graph/nodes/scaffold_node.py`／`graph/nodes/implement_node.py` 呼叫，兩個 node 本身不直接碰觸上述任何細節 |

---

## 十二、與 LangGraph 整合

沿用 01 四章已經定案的 node 對應，不需要新增或調整 node／edge 結構：

- `scaffold`（④）：`await translator_cli.generate_scaffold(state["python_project_path"], state["python_structure"], db_models=db_models)`，回傳 `{"scaffold_done": result["success"]}`（`scaffold_done=False` 時 `implement`→`run_tests` 之間的 conditional edge 會直接分流到 `give_up`，不進 `debug` 重試迴圈，見 01 五章「scaffold 失敗時的收尾路徑」——07a 只保證 `generate_scaffold()` 本身在失敗時清楚回報 `error`，分流邏輯屬於 01／`graph/nodes/implement_node.py` 的範圍）。`db_models` 這個字典本身怎麼組出來（查詢 Postgres 測試 DB 或解析 Java entity 原始碼，見四章「`db_models`：④ 自行取得的 DB schema 內容如何併入」）屬於 08a（骨架實作 Agent 詳細設計）的範圍，07a 只定義這個參數送進 `generate_scaffold()` 之後的處理契約。`result["skipped_interfaces"]`／`result["skipped_db_models"]`（見四章）的 State 寫入管道由 08a 補上（`RefactorState` 新增對應欄位，`scaffold_node.py` 合併寫入，見 08b 八章）。
- `implement`（⑤）：`_run_one_task()` 呼叫 `translator_cli.fill_function(python_project_path=state["python_project_path"], task_id=task["id"], translator_backend=task["translator_backend"], java_source=..., referenced_source=..., ...)`——`java_source`／`referenced_source` 由呼叫端（⑤，經 `graph/java_source_extraction.py`）解析 `task["java_method_id"]`／`task["reference_targets"]` 組出，見五章「為什麼是 `java_source`／`referenced_source`」。呼叫端不需要自己包一層 `MODEL_SEMAPHORE` 號誌——qwen 的實體機器序列化、寫入磁碟的序列化都收進 `translator_cli` 內部管理，見八章「寫入段序列化」，`implement_node.py` 只需要單純呼叫 `fill_function()`，可以用 `asyncio.gather()` 對同一 phase 內多個就緒 task 平行發起呼叫，不需要自己判斷 backend 或包號誌

translator-cli 本身**不**依賴 `RefactorState` 其餘欄位，只需要呼叫端組好的顯式引數——這是刻意的邊界：`translator_cli/` 套件不 import `graph.state`，維持跟 `refactor_harness/`／`spec_collection_agent/` 一致的獨立性（01 二章「設計原則」）。

---

## 十三、錯誤處理範圍

比照 04a 九章／05a 十二章的邊界：`retry_count` 迴圈只包住 `implement → run_tests → debug`，translator-cli 本身的呼叫失敗不直接觸發這個迴圈，而是反映成 `FillResult(success=False)` 或 `generate_scaffold()` 的 `{"success": False}`，由呼叫端（`implement_node.py`／`scaffold_node.py`）決定後續（task 標記失敗、module 局部驗證會抓到、進而觸發 `debug`）。translator-cli 自己只負責：

- 網路層錯誤（僅 qwen 路徑）：七章「qwen 路徑：Timeout 與重試策略」的固定次數重試，重試仍失敗才回報 `success=False`
- 模型輸出格式／語法錯誤（兩條路徑共用）：七章「兩條路徑共用：body_text 格式修正重試」固定重試 2 次，仍失敗才回報 `success=False`
- AST 定位失敗（scaffold/task 資料不一致）：不重試，直接回報——這是輸入端資料問題，見六章
- working tree 不乾淨（九章）：不重試，直接回報——需要人工介入核對，不是可以自動化解的暫時性錯誤
- `generate_scaffold()` 單一 `InterfaceSpec` 的型別字串驗證失敗（正規化後仍不合法）：**不中止、不重試**，跳過這一個函式並記進 `skipped_interfaces`，其餘介面正常繼續（見四章「語法驗證與寫入」）——這點跟 05a／06a「單一 module／單一批次失敗就中止整條 run」的既有先例刻意不同：05a／06a 中止是因為那些失敗會讓**下游對著不完整規格工作**（`python_structure`／`task_list` 本身就是缺角的），但 `generate_scaffold()` 這裡失敗的粒度是單一函式，其餘介面的骨架完全不受影響，沒有理由讓一個函式的問題拖垮整個 `python_structure` 的骨架產出
- `generate_scaffold()` 整檔組裝完成後的最終 `ast.parse()` 驗證失敗（四章步驟 4）：不重試，直接中止——這代表組裝邏輯本身有 bug（如 import 合併沒去重乾淨），不是個別介面的型別字串問題，性質同 05a／06a 的「中止」情境

---

## 十四、待決定事項

- [ ] **`MultipartFile`（Java 型別）沒有被 `map_java_type()` 轉換成 `UploadFile`，導致四章「已知關鍵字表」比對不到**：真實 Java 專案（93 個檔案、70 個 interfaces）實測時發現，其餘型別關鍵字表命中正常（`skipped_interfaces` 全部為空）；這是 05a 型別對應範圍的缺口，不是這裡的關鍵字表本身缺項，留給 05a 之後處理
- [ ] **Claude 路徑的併發上限尚未定案**：qwen 路徑有 `OLLAMA_MODEL_SEMAPHORE(1)`（實體機器單模型的硬限制），Claude 路徑目前只是「不受這個號誌限制」，沒有另外設計自己的併發控制——`common/llm_client.py` 本身是否已有跨呼叫方共用的速率限制/併發上限（[③]／舊版 [P] 既有呼叫是否曾經撞過 429）需要查證，若沒有，⑤ 一次可能同時對多個 phase 2 task 發起 Claude 呼叫，要不要另外加一層併發上限（如另一個較大的 semaphore）留給實作階段依真實 API 限流表現決定，不在本文件先驗定案
- [ ] **Claude 路徑的格式修正重試次數（固定 2 次，見七章）沿用 qwen 校準值，尚未有真實資料驗證是否適用**：qwen 的 2 次是接上真實 qwen2.5-coder:32b 才校準出來的（見七章），Claude 走 Structured Outputs、天生不會有 delimiter／JSON 格式錯誤，剩下的 `ast.parse()` 語法錯誤／空本體／簽名重複輸出三類錯誤發生機率是否與 qwen 相近，需要接上真實 Claude API 端到端跑過才能確認，屆時視實測結果再調整

---

## 十五、`app/core/exception_handlers.py`（`_global` 保留模組）的骨架生成

**背景**：09b 端對端整合測試發現（見 `docs/09b_bug_trace.md` #11/#12），四章「interfaces：函式簽名渲染」原本假設 `interfaces` 的 `file_path` 只會落在 `app/routers/`／`app/services/`／`app/repositories/` 三層目錄底下——`_global` 保留模組（見 04a 十一章、05a 十四章）固定輸出的 `app/core/exception_handlers.py` 不符合這個假設，會被判定成 unknown layer，整個 `InterfaceSpec` 被跳過、記進 `skipped_interfaces`，這個檔案因此從未被 `generate_scaffold()` 建立。連鎖後果：③正確輸出的 `app/main.py` 仍然會 import 這個從未存在的模組，Python 服務在 import 階段直接 `ModuleNotFoundError` 掛掉（容器啟動時的行為，因為 import 錯誤發生在啟動當下，不是某次 API 呼叫才觸發）。

**決策**：`app/core/exception_handlers.py` 用精確比對（不是前綴），獨立於三層目錄的前綴判斷之外處理。渲染規則比照 routers 層（`class_name=None` 自由函式），但**不**包 `APIRouter` 樣板（`from fastapi import APIRouter`／`router = APIRouter()`）——這不是真正的路由檔案，函式不會被 `@router.xxx` 裝飾（`http_method` 恆為 `None`，這批函式本來就不是 API 端點）。

實作細節見 `07b_translator_cli_code.md` 八章（`scaffold.py`）。單元測試見 `tests/translator_cli/test_scaffold.py::test_build_files_renders_global_advice_file_without_api_router_boilerplate`。

**真實環境驗證**：對真實 `../lang-exam-api-refactor` 完整跑過 ①③[P]④⑤⑥，`app/core/exception_handlers.py` 正確產生（不再進 `skipped_interfaces`），容器內確認 Python 服務能正常 import 啟動，且 Starlette 的例外處理中介層確實會呼叫到這個檔案裡⑤翻譯出的函式。

---

*各 Agent／工具的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
