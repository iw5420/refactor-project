# translator-cli 詳細設計

> 本文件承接 `00_refactor_architecture.md` 的整體架構（見七、④骨架實作 Agent／⑤功能改寫 Agent 一節；三章「程式碼執行工具：translator-cli」；六章「Context 控制策略」；十章「待決定事項」），是這個工具的**設計面**文件：決策、契約、資料結構、流程。實際程式碼實作見 `07b_translator_cli_code.md`（待建立）；本文件不出現可執行的實作邏輯，僅在需要精確釘死「格式契約」時才附固定範本（比照 `05a_design_agent_architecture.md` 三章附 `database.py` 固定範本的既有先例）。

---

## 一、本文件範圍與定位

**本文件涵蓋**：
- 骨架生成模式 `generate_scaffold(python_structure)`（④ 呼叫，見四章）與填空模式 `fill_function(...)`（⑤ 呼叫，見五章）的完整輸入輸出契約
- AST 插入機制：如何在既有檔案裡精準定位、替換單一函式本體（六章）
- 與 ollama（經 nginx）的連線方式、request/response 格式、delimiter 契約（七章）
- git snapshot 流程、commit 顆粒度（回應 00 十章待決定事項，見八章）
- 衝突偵測機制（九章）
- 目標語言 adapter 介面（十章）
- 與既有程式碼（`graph/state.py`／`graph/nodes/scaffold_node.py`／`implement_node.py`）的介面異動需求（見二章末「與既有程式碼的介面異動」）

**本文件不涵蓋**：
- 實際程式碼——見 07b
- ④／⑤ node 本身怎麼組裝 task／怎麼呼叫排程器——這部分已經是既有實作（`graph/nodes/scaffold_node.py`／`implement_node.py`／`graph/scheduler.py`），07a 只定義 translator-cli 被呼叫端的契約，08a／09a（皆待建立）才是 node 內部邏輯的權威文件
- Harness 的局部驗證／全量驗證——見 02a
- [P] Plan Agent 如何產生 `task.description`／`task.context` 的業務語意內容——見 06a

---

## 二、兩種模式總覽

00 七章已定調：`fill_function()`（填空模式）與 `generate_scaffold()`（骨架生成模式）不是同一個契約，呼叫方、輸入輸出、呼叫頻率完全不同：

| | `generate_scaffold()` | `fill_function()` |
|---|---|---|
| 呼叫方 | ④ `scaffold_node.py` | ⑤ `implement_node.py`（`MODEL_SEMAPHORE(1)` 內） |
| 呼叫次數 | 整條 pipeline 一次 | 逐 task 呼叫 |
| 輸入 | `PythonStructure`（整包） | 單一 task 的 `target_file`／`class_name`／`function_name`／`description`／`context`／`context_files` |
| 是否呼叫本地模型 | **否**——見四章，這是本文件對既有 stub docstring 的修正 | 是（qwen2.5-coder:32b via ollama，見七章） |
| 寫入方式 | 從無到有建立檔案（整檔輸出） | 對既有檔案做 AST 精準插入（單一函式本體） |
| git 動作 | 一次性 commit（見八章） | 逐 task commit（見八章） |

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

### 與既有程式碼的介面異動

本文件設計出的契約，需要對已實作的部分做以下**小幅**異動，列在這裡供使用者確認是否要一併套用（見十四章「待決定事項」第一項的完整說明）：

1. ~~`graph/state.py`：`RefactorState` 新增 `python_project_path: str` 欄位；`TaskSpec` 新增 `class_name`／`function_name` 兩個欄位~~——**`TaskSpec` 這部分已套用**（`class_name: NotRequired[str | None]`／`function_name: NotRequired[str]`，`plan_agent/planning.py` 對應補上，見 `06a_plan_agent_architecture.md` 八章、`tests/plan_agent/` 新增測試，並已用真實 Claude API 呼叫驗證過）；`RefactorState.python_project_path` 仍待套用
2. ~~`06a_plan_agent_architecture.md` 八章：訂正「不落地進最終輸出」~~——已套用
3. `graph/nodes/implement_node.py`（01 六章「接上真實 translator-cli／Harness 後的目標實作」那段程式碼）：`_run_one_task()` 呼叫 `translator_cli.fill_function()` 時補上 `class_name=task["class_name"]`／`function_name=task["function_name"]`／`context=task["context"]` 三個引數（目前只傳了 `target_file`／`description`／`context_files`）——尚待套用，`implement_node.py` 目前仍是 stub 階段，等 translator-cli 本身（07b）落地時一併處理即可，不急著現在改
4. `main.py`：`initial_state` 新增 `"python_project_path": os.environ["PYTHON_PROJECT_PATH"]`——尚待套用

---

## 三、Python 目標專案的分層與 translator-cli 的角色邊界

translator-cli 不重新決定 Python 專案怎麼分層——03 三章的分層規則（`app/{routers,services,repositories,schemas,models}/`、檔名慣例 `{module}_{layer}.py`）已經是定案，`PythonStructure` 完整攜帶了這份決策。translator-cli 的角色純粹是「把 `PythonStructure`／task 描述轉成磁碟上實際存在、語法正確的 `.py` 檔案」，不重新判斷任何屬於 ③ 職責範圍的事（層級歸屬、型別對應、`db: Session` 要不要加預設值——這些在 `InterfaceSpec` 送到 translator-cli 手上時已經是最終決定，見 `design_agent/design.py` `db_type = "Session = Depends(get_db)" if layer == "routers" else "Session"` 的既有實作：`ParamSpec.type` 本身就已經是完整可以直接拼進函式簽名的字面字串，**translator-cli 渲染參數時只需要 `f"{p['name']}: {p['type']}"` 逐一字串接合，不需要對 `db`／`Depends`／預設值做任何特殊判斷**）。

---

## 四、骨架生成模式：`generate_scaffold(python_structure)`

### 決策：不呼叫本地模型，純機械產生

現有 `translator_cli/client.py` stub 的 docstring 寫「呼叫本地模型從零生成整個目錄結構」——**這裡修正這個假設**。理由：`PythonStructure` 送到這一步時，`directory_tree`（三段固定格式，見 `05a_design_agent_architecture.md` 三章）＋ `interfaces`（檔案路徑＋完整函式簽名層級）已經是**結構化、無歧義**的資料，骨架階段要做的事——建目錄、把已知的類別/函式簽名渲染成語法正確的 Python 檔案、`pass` 佔位——沒有任何一步需要「判斷」或「創造」，全部是機械字串組裝加 AST 語法驗證。呼應 00 二章「能用程式判斷的，就不要交給 LLM」：這正是可以完全用程式判斷的情況，讓 qwen 生成骨架只會多引入一種全新的失敗模式（模型自己編排的骨架語法錯誤、簽名跟 `InterfaceSpec` 不一致），且沒有對應的好處。

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

### 型別字串正規化（Java 泛型符號 → Python subscript 語法）

**這一步在渲染任何函式簽名之前先做，套用到每一個 `params[].type`／`return_type` 字串**：對真實 `lang-exam-api-refactor` 專案的 `real_python_structure.json`（72 個 `InterfaceSpec`）逐一用 `ast.parse()` 驗證過，**25 個（35%）的型別字串直接讓 `ast.parse()` 拋 `SyntaxError`**——例如 `ResponseResult<T>`、`ResponseResult<Map<String, Object>>`、`Specification<ExamEntity>`、`Function<T, String>`，橫跨 6 個不同檔案。根因是 05a 五章的型別對應表只硬編了 JDK 集合型別（`List`／`Set`／`Map`／`Optional`／`Collection`）的轉換規則，任何其他泛型包裝類別（專案自訂的 `ResponseResult<T>`、JDK 的 `Function<T,R>`、Spring Data 的 `Specification<T>`）落入「無法辨識的型別 → 原樣保留字串」這條 fallback——**保留的是 Java 語法本身**（角括號 `<...>`），`<` 在 Python 是比較運算子，直接讓函式簽名整個炸出語法錯誤。這不是理論上的邊界案例，是這個真實專案三分之一的介面都會踩到的常態，`generate_scaffold()` 不能假設 `InterfaceSpec` 送來的型別字串已經是合法 Python。

**修正：機械字元替換，不判斷語意**：

```
normalized = raw_type.replace("<", "[").replace(">", "]")
```

單純逐字元替換 `<`→`[`、`>`→`]`，天然保留巢狀結構（`ResponseResult<Map<String, Object>>` → `ResponseResult[Map[String, Object]]`，巢狀泛型的括號配對關係在字元替換下不會亂掉）。這個轉換對上述 25 個真實案例**全部驗證通過、無一失敗**。定位上這仍然是「機械規則做得到，不需要問 LLM」的範疇（00 二章）——跟 05a 型別對應表的性質不同：05a 決定「這個 Java 型別該對應到哪個 Python 型別」是語意層級的決策，這裡只是把殘留在字串裡、還沒被特殊規則處理掉的 Java 泛型記法**符號**轉成 Python 的合法記法，不重新判斷任何型別語意，因此不算越界進 ③ 的職責。

**已知殘留限制（正規化後仍可能不完美，但不阻擋語法驗證）**：
- 轉換只保證**語法合法**，不保證**語意正確**——`ResponseResult[T]` 語法上合法，但 `T`（Java 泛型型別變數）在 Python 端沒有對應的 `TypeVar` 定義，`ResponseResult` 本身也不一定支援 subscript（除非它是 `Generic[T]` 子類別）；這類問題屬於 `NameError`／執行期錯誤，不會被 `ast.parse()` 攔下，留給 Harness 的 module 驗證與 ⑦ Debug Agent 處理，不在骨架生成這一步解決
- Java 萬用字元泛型（如 `List<? extends Foo>`）轉換後仍含非法字元 `?`，這批資料裡沒有出現這種寫法，暫不特別處理，屬於下方「語法驗證與寫入」失敗隔離機制要接住的殘餘情況

### `interfaces`：函式簽名渲染

`interfaces` 依 `file_path` 分組（`app/routers/`／`app/services/`／`app/repositories/` 三層的實際程式碼都只能來自這裡，`directory_tree` 不含這三層的檔案內容）。同一 `file_path` 群組內，再依 `class_name` 分組。**以下渲染一律使用正規化後的型別字串，不是 `InterfaceSpec` 原始值**：

- **`class_name is None`（routers 層，05a 七章已定案這層一律為 `None`）**：檔案內是自由函式，不包 class。檔案開頭固定加：
  ```python
  from fastapi import APIRouter

  router = APIRouter()
  ```
  每個函式渲染成：
  ```python
  @router.{http_method.lower()}("{route_path}")
  def {function_name}({params}) -> {return_type}:
      pass
  ```
  （`http_method`／`route_path` 是 05a 五章新增的 `InterfaceSpec` 欄位，routers 層 API 邊界方法一定非 `None`；`params` 直接是 `", ".join(f"{p['name']}: {p['type']}" for p in interface['params'])`，見三章）
- **`class_name` 非 `None`（services／repositories 層）**：同一 `file_path` 底下按 `class_name` 分組各自渲染一個 `class`，方法縮排在 class 內：
  ```python
  class {class_name}:
      def {function_name}({params}) -> {return_type}:
          pass
  ```
  **刻意不產生 `__init__`**：`InterfaceSpec` 沒有攜帶建構子／欄位依賴資訊（04a／05a 的職責邊界都只到方法簽名層級），這批 class 在骨架階段是無狀態的方法容器；跨 class 呼叫（如 service 呼叫 repository）留給 `fill_function()` 階段依 `context` 決定寫法（直接 import 後實例化，或呼叫端另有慣例），不是骨架階段能機械決定的事，見五章。

**函式本體固定寫 `pass`**：語法合法、明確代表「待填」，也是六章 AST 定位／替換的目標錨點。

### import 解析：兩層機制，不窮舉

每個函式的 `params`／`return_type` 字串裡可能出現需要額外 import 的型別（`Session`、`Depends(...)`、專案內自訂 class 如 `UserCreateRequest`）。`generate_scaffold()` 用兩層規則機械決定每個檔案開頭要加哪些 import：

1. **已知關鍵字表（機械，涵蓋這個專案已知會出現的框架型別）**：型別字串命中以下關鍵字（子字串比對）就加對應 import，一個檔案內出現多次只加一次：

   | 命中關鍵字 | 加入的 import |
   |---|---|
   | `Session` | `from sqlalchemy.orm import Session` |
   | `Depends(` | `from fastapi import Depends` |
   | `get_db` | `from app.core.database import get_db` |
   | `Request` | `from fastapi import Request` |
   | `UploadFile` | `from fastapi import UploadFile` |
   | `HTTPException` | `from fastapi import HTTPException` |
   | `Decimal` | `from decimal import Decimal` |

2. **自訂型別索引，兩個來源合併**：
   - **來源一（既有）**：掃描第二段（Schema 定義段）解析出的所有具名 class（`schemas/{module}.py`／`models/{module}.py` 底下的 Pydantic／dataclass）
   - **來源二（新增，見上方「型別字串正規化」的真實案例）**：直接從 `python_structure.interfaces` 收集每個 `class_name → file_path`（排除 `class_name is None`）。**這一步是必要的，不是加強保險**：真實資料裡有 **44 筆**跨檔案引用（見上方 `ResponseResult`／`Result` 案例）——這些型別是 `services` 層的一般類別（如 `common_service.py` 的 `ResponseResult`），根本不會出現在 Schema 定義段裡（Schema 定義段只收錄 API 邊界 Pydantic model／資料容器 dataclass，不收錄一般 `services`／`repositories` 層的類別），只靠來源一完全抓不到。這個來源不需要額外解析文字——`python_structure.interfaces` 本身就是結構化資料，直接建索引即可，比來源一的正則掃描更直接
   
   兩個來源合併成一份索引（key 衝突時保留來源二，因為它是結構化資料、精確度較高，不是靠文字 pattern 猜出來的）。對每個（已正規化的）`params`／`return_type` 字串，剝掉 `list[...]`／`| None`／泛型包裝（正規化後統一是 `[...]`）取出裸型別名稱，命中這份索引就加 `from {module_path} import {class_name}`（`module_path` 由 `file_path` 去掉 `.py`、`/` 換 `.` 得到）。

**兩層都比對不到時保守不加、不猜**：可能是 Python 內建型別（`int`／`str`／`bool`，本來就不需要 import），也可能是 ③ 的 LLM 步驟自由產生的複合寫法（如 `Annotated[User, Depends(get_current_user)]`，`User` 部分理論上會被第二層抓到，但更複雜的巢狀寫法可能抓不全）。這裡延續 04a／05a 反覆出現的「機械規則解決不了的部分不強行猜測」精神，但方向相反——那邊是「多連少排除」（保守多連結），這裡刻意選「少加不亂猜」，因為錯誤 import 一個不存在的名稱會讓整個檔案在 `import` 階段就炸掉（比缺 import 導致的 `NameError` 更早爆、更難定位、牽連同一個 `app/main.py` 底下的其他 router）。剩餘的 import 缺口交給 Harness 的 module 局部驗證（服務起不來會直接反映在驗證失敗）與 ⑦ Debug Agent 處理，不在骨架階段窮舉。

### 語法驗證與寫入：逐 `InterfaceSpec` 隔離失敗，不是全域 all-or-nothing

**這裡修正本文件先前的設計**：原本設計是「每個組裝完的檔案文字先 `ast.parse()` 驗證，全部檔案都通過才一次性寫入，任一檔案失敗就整包不寫、直接中止」——這個設計沒有考慮到上方「型別字串正規化」發現的真實情況：25/72（35%）介面的原始型別字串本身不合法，即使加了正規化修掉全部已知案例，仍可能有正規化解決不了的殘餘情況（萬用字元泛型等）。全域 all-or-nothing 代表**這一個介面的型別字串有問題，會拖累其餘 71 個完全正常的介面全部無法產出骨架**，讓 `generate_scaffold()` 對這個真實專案直接整個失敗——這不是可以接受的失敗模式，`generate_scaffold()` 一旦失敗，[P]／④／⑤ 全部卡住，風險遠高於「有幾個函式沒骨架」。

**改為逐 `InterfaceSpec` 驗證，個別失敗只跳過那一個函式**：

1. 每個 `InterfaceSpec` 對應的函式簽名（正規化＋渲染後）先各自 `ast.parse()` 一次（用「這個簽名＋`pass`」組出的最小片段驗證，不用等整個檔案組完才驗證）
2. 驗證失敗 → 這個函式**不渲染進檔案**，記進 `generate_scaffold()` 回傳值新增的 `skipped_interfaces` 欄位（`{file_path, class_name, function_name, error}`），繼續處理下一個 `InterfaceSpec`，**不影響同檔案其他函式，也不影響其他檔案**
3. 同一個 `class_name` 分組若所有方法都被跳過（極端情況，理論上不該發生），該 `class` 仍要渲染出來，body 用 `pass` 佔位，維持檔案語法合法——不能因為跳過方法就留下一個空殼 `class Foo:` 沒有 body
4. 所有 `InterfaceSpec` 處理完後，每個組裝完的完整檔案文字**再整份 `ast.parse()` 驗證一次**（防禦性：理論上必然成功，因為只由已個別驗證過的合法片段組成，但寫入前的最後一道檢查成本很低）——這一步才是真正的全域關卡，但這時候失敗代表的是「組裝邏輯本身有 bug」（如合併 Schema 定義段落時 import 沒去重乾淨），不是「某個介面型別字串剛好有問題」，性質不同：這種情況才維持中止回報，不吞掉

**寫入時機**：全部檔案（含所有能建出來的骨架）驗證通過後，一次性寫入磁碟＋git commit（見八章）；步驟 4 的全域檢查失敗才整包不寫、直接中止回報。

**回傳契約新增 `skipped_interfaces`**：

```python
{
    "success": bool,       # 步驟 4 的全域檢查通過即為 True，即使 skipped_interfaces 非空
    "error": str | None,   # 只在 success=False 時有值（步驟 4 失敗，或環境前提不滿足，見二、九章）
    "skipped_interfaces": list[dict],  # [{file_path, class_name, function_name, error}, ...]，
                                        # 空清單代表這次沒有任何介面被跳過
}
```

`success=True` 但 `skipped_interfaces` 非空是**預期中會發生、需要人工留意但不阻擋 pipeline** 的情況——呼應 00 十二章／02a「`excluded_folders`」同一種設計精神（非空不代表失敗，是「這次沒完整涵蓋，要留意」的訊號）。`skipped_interfaces` 裡列出的介面，對應的 ⑤ task 之後呼叫 `fill_function()` 時會在六章步驟 4「找不到目標函式」自然失敗、回報明確錯誤（`class_name`／`function_name`／`target_file` 都對得上，不是憑空找不到），不需要 `generate_scaffold()` 自己額外處理這個下游後果。

---

## 五、填空模式：`fill_function(target_file, class_name, function_name, description, context, context_files)`

### 為什麼 `fill_function` 需要 `class_name`／`function_name`

`InterfaceSpec.file_path` 是**每個函式各自的路徑，不是每個檔案各自唯一**——`app/routers/user_router.py` 這一個檔案底下正常會有多個 router 函式，對應多個不同的 `InterfaceSpec`／task。06a 三章「涵蓋率規則」訂為「每一個 `InterfaceSpec` 恰好對應一個 task（1:1）」，代表**多個 task 的 `target_files[0]` 會是同一個檔案路徑**——若 `fill_function()` 只拿到 `target_file`，沒有辦法知道這次要填的是檔案裡的哪一個函式。

06a 八章原本的決定是「`class_name`／`function_name` 不落地進最終 `TaskSpec`，只在 [P] 組裝階段的內部草稿保留」——這個決定在 06a 當時的脈絡下（涵蓋率驗證只需要在組裝階段核對一次，不需要留到執行期）是合理的，但沒有預見到 translator-cli 這一端**執行期**就需要靠這個三元組定位函式。這是本文件發現的一個上游缺口，解法是最小的：06a 在組裝 `TaskSpec` 時本來就手上有這個三元組（八章原文「組裝階段的內部草稿仍保有五章原樣抄回的 `file_path`／`class_name`／`function_name` 三元組」），改成一併寫進最終 `TaskSpec` 即可，不影響 06a 其餘任何演算法（`depends_on`／`target_files` 組裝、涵蓋率驗證都不受影響，純粹多保留兩個欄位）。**這項異動已套用**（`graph/state.py`／`plan_agent/planning.py`／`06a_plan_agent_architecture.md` 八章，見二章「與既有程式碼的介面異動」），並用真實 `lang-exam-api-refactor` 專案輸出＋真實 Claude API 呼叫驗證過：72 個 task 全數帶有正確的 `class_name`／`function_name`，其中 8 個檔案被超過一個 task 共用（最多 22 個，見下方「型別字串正規化」同一份真實資料），確認這不是理論上的邊界情況，而是這個真實專案裡的常態。

### 輸入契約

| 參數 | 來源 | 說明 |
|---|---|---|
| `target_file` | `task.target_files[0]` | 相對路徑，如 `app/repositories/user_repository.py` |
| `class_name` | `task.class_name` | `None` 代表 routers 層自由函式 |
| `function_name` | `task.function_name` | 連同 `class_name` 唯一定位這次要填的函式（見上方說明） |
| `description` | `task.description` | 業務邏輯描述——**不是原始 Java 原始碼**，是 [P] 的 LLM 已經整合過 Java method 業務邏輯後產出的任務描述（見 06a 五章），translator-cli 不需要、也不會拿到逐字的 `.java` 檔案內容。00 三章「Java 原始邏輯」這個措辭在這裡具體落地成這份已經被 Claude 消化過的自然語言描述，不是文字檔案內容——這樣才符合 00 六章「Context 控制策略」控制 context 大小的目的：塞整段原始 Java 方法本文只會放大 context，不會提高正確率，[P] 產生 `description` 時已經把「該做什麼」萃取出來了 |
| `context` | `task.context` | 補充依賴關係／邊界條件文字（06a 五章），現況 `implement_node.py` 尚未傳遞這個欄位，需要補上（見二章「與既有程式碼的介面異動」第 3 項） |
| `context_files` | `task.target_files` | 這次呼叫要讀進 prompt 的檔案清單（含 `target_files[0]` 自己＋06a 七章組裝進來的 referenced 檔案／schemas／models），控制 context 大小的關鍵（00 六章） |

### 輸出契約

```python
class FillResult:
    success: bool
    error: str | None
    diff: str   # 這次 commit 的 git diff 全文，供人工／⑦ Debug Agent 事後追溯這次改了什麼（見八章），
                # 不是 scheduler 的 regression 偵測依據——那邊只需要 target_files[0] 這個路徑本身
                # （見 graph/scheduler.py module_owned_files 註解），不需要讀 diff 內容
```

`diff` 欄位的用途是**修正既有 stub 註解裡「用於 regression 偵測」這個不精確的說法**：`graph/scheduler.py` 的 `check_upstream_regression()` 只用檔案路徑判斷，完全不看 `diff` 內容，`diff` 純粹是留給人工或未來 ⑦ Debug Agent 想深入某個 task 實際改了什麼時的追溯資訊。

### 呼叫失敗時不寫入任何內容

比照四章「語法驗證與寫入」的原則：`fill_function()` 全程在記憶體中組出新的檔案內容、驗證通過才落地寫入＋commit；任何一步失敗（模型呼叫失敗、delimiter 契約違反、AST 解析失敗，見六、七章）都**不碰磁碟**，直接回傳 `FillResult(success=False, error=...)`。這個保證是九章「衝突偵測」成立的前提——只要失敗路徑保證不寫入，working tree 在失敗後仍然乾淨，下一次呼叫（不論是同一個 task 重跑，或另一個 task）看到的都是最後一次成功 commit 的狀態，不需要額外清理步驟。

### 冪等：允許對已有內容的函式重新填空

`fill_function()` 定位到的函式節點，body 可能是骨架階段的 `pass`（第一次填），也可能已經是先前一次成功呼叫留下的真實邏輯（同一個 task 因為某種原因被重新呼叫）。AST 定位＋替換（六章）不對 body 現有內容做任何假設，兩種情況處理方式完全一樣——直接整個替換。這不是為了目前已知的某個重試流程設計（⑤／⑦ 目前的重試語意留給 09a／10a 待建立文件定案），而是讓 translator-cli 自己的契約不論上游怎麼重跑都行為一致、不需要額外狀態判斷。

---

## 六、AST 插入機制

`generate_scaffold()`／`fill_function()` 都經由同一層 Python adapter（見十章）操作 `ast` 模組，不做任何文字層級的 diff／patch——00 三章「核心設計是『填空式』輸出契約...由 CLI 用 AST 精準插入，而非依賴模型生成可精確比對原文的 diff」在這裡具體落地：

### 定位

```
1. source = target_file 現有內容（Path.read_text(encoding="utf-8")，見 00 六章「檔案讀寫編碼慣例」）
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
5. 呼叫 ollama（七章），取得 body_text（已通過 delimiter 抽取＋語法驗證，見七章）
6. body_tree = ast.parse(body_text)   # body_text 是模型回傳的「未縮排」陳述式集合，見七章 delimiter 契約
7. 目標函式節點.body = body_tree.body
8. ast.fix_missing_locations(tree)
9. new_source = ast.unparse(tree)
10. ast.parse(new_source) 再驗證一次（防禦性：理論上必然成功，因為是從合法 AST 反渲染回來的，
    但寫入磁碟前的最後一道檢查成本很低，值得留著）
11. 驗證通過 → 寫入 target_file，見八章 commit
```

**`ast.unparse()` 會重新格式化整個檔案，不是只改動被替換的那個函式**：這是刻意接受的行為，不是需要避免的副作用——`generate_scaffold()` 產生的初始骨架也一律經過同一個 `ast.parse → ast.unparse` 流程產生（而不是像 `design_agent/layout.py` 那樣手動組字串），確保骨架階段與填空階段的格式化風格**天生一致**，同一顆 Python 版本下 `ast.unparse()` 對同一棵 AST 的輸出是確定性的，因此除了這次真正改動的函式，檔案其餘部分每次重新渲染都會得到一模一樣的文字，`git diff` 實際看到的改動範圍仍然乾淨、只集中在被填的那個函式。

### delimiter 契約：模型回傳格式

模型（qwen）透過 chat completion 回傳的是自然語言夾雜程式碼的文字，不能直接假設回傳內容剛好等於一段可以直接 `ast.parse()` 的函式本體（可能有解說文字、可能用 ` ```python ` fence 包、可能兩者都有或都沒有）。System prompt 固定要求模型把函式本體包在兩個字面 sentinel 之間，且**只**回傳這個區間內的內容作為程式碼：

```
<<<TRANSLATOR_CLI_BODY_START>>>
statement_1
statement_2
<<<TRANSLATOR_CLI_BODY_END>>>
```

抽取邏輯：正則找兩個 sentinel 之間的文字。找不到 sentinel（模型沒有遵守格式）→ 視為「delimiter 契約違反」，見七章重試策略。

**body 內容要求「未縮排」**：模型回傳的陳述式視為函式本體的第一層陳述式（如同它們是模組頂層程式碼），不要求模型自己判斷「這是被塞進一層 class 方法還是自由函式，該縮 4 格還是 8 格」——這個判斷模型容易出錯（尤其 routers／services/repositories 三層縮排深度不同）。抽取出來的文字直接 `ast.parse()`（等同解析成一個模組），取得的 `.body` 就是這個函式本體要用的陳述式清單，縮排完全由 `ast.unparse()` 在第 9 步依實際巢狀深度重新計算，不依賴模型自己的縮排是否正確。

---

## 七、Ollama 連線與請求契約

### 連線方式

沿用 00 三、四、五章已定案的架構：translator-cli 不直接打 ollama，而是打 `.env` 的 `OLLAMA_BASE_URL`（指向另一台 Mac 上的 nginx，已含 `/v1` 路徑前綴），帶 `Authorization: Bearer {OLLAMA_API_KEY}`，nginx 驗證通過後才轉發給 ollama 本機的 `qwen2.5-coder:32b`。使用 `httpx.AsyncClient`（`requirements.txt` 已有此依賴，不需新增）呼叫 OpenAI 相容的 `POST {OLLAMA_BASE_URL}/chat/completions`。

`model` 欄位固定填 `"qwen2.5-coder:32b"`（Python 常數，不經環境變數——00 三章「硬體限制」已經把模型選型釘死為單一選擇，不是每個 Agent 各自可調的東西，跟 `common/llm_client.py` 對 Claude 模型「呼叫端自己決定用哪個模型」的分工原則刻意不同：Claude 那邊有多個模型可選、多個 Agent 各自決定；qwen 這邊實體機器只跑得動一顆模型，沒有「選哪個」的問題）。

### Timeout 與重試策略

本地模型單次生成可能需要數十秒到數分鐘（視函式複雜度），且 fill_function 每次呼叫都是單一函式、體積遠小於 [P]/[③] 那種一次處理整個 module 的 Claude 呼叫，重試策略因此比 04a/05a 的「5 分鐘後重試一次」更輕量、更快：

| 常數 | 預設值 | 說明 |
|---|---|---|
| `TRANSLATOR_CLI_TIMEOUT_SECONDS` | `300`（環境變數可調） | 單次 HTTP 呼叫的 timeout，本地模型生成時間比雲端 API 長，需要比 Claude 呼叫更寬鬆的預設值 |
| `TRANSLATOR_CLI_NETWORK_RETRIES` | `2`（環境變數可調） | HTTP 連線失敗／timeout 這類**傳輸層**錯誤的立即重試次數，固定間隔 `3` 秒——這是 LAN 內部連線的暫時抖動，不是 Claude API 那種需要等配額恢復的限流情境，不需要 5 分鐘等待 |
| delimiter／語法驗證失敗重試 | 固定 `1` 次 | 見下方「模型輸出格式錯誤的修正重試」，跟網路層重試是不同的錯誤類型、不同的重試機制，不共用同一個計數器 |

**模型輸出格式錯誤的修正重試**（跟 04a/05a 的「批次重試」不同性質，這裡是「針對這次錯誤，把錯誤內容回饋給模型，讓它自己修正」）：六章 delimiter 抽取失敗，或抽取出來的 `body_text` 通不過 `ast.parse()`（`SyntaxError`），**重新呼叫一次模型**，這次的 user prompt 額外附上「上一次回應違反格式／語法錯誤訊息是 ...，請重新產生，務必遵守 delimiter 格式」。這一次呼叫仍然失敗（不論是同一種錯誤還是不同錯誤）→ 放棄，`FillResult(success=False, error=...)`，不無限重試——固定重試一次的理由跟 04a 的「一次性的固定延遲已經夠用」同構：這是扛過模型單次偶發輸出品質不穩的緩衝，不是要取代 ⑦ Debug Agent 那種「業務邏輯寫錯了」層級的修正機制。

### System / User Prompt 組裝

System prompt 固定模板（不含業務內容，只定義輸出格式契約）：

```
你是一個 Python 程式碼填空工具。你會收到一個已經定義好簽名的 Python 函式（骨架已建好，
可能是空的 `pass`，也可能已有既有邏輯），以及這個函式應該做什麼的描述。

你的任務：只回傳這個函式的「本體」陳述式，不要重複函式簽名、不要重複裝飾器、不要加任何
解說文字。回傳格式固定如下，只在這兩個標記之間寫程式碼，標記本身也要原樣附上：

<<<TRANSLATOR_CLI_BODY_START>>>
（函式本體陳述式，視為第一層縮排，不要自己加縮排）
<<<TRANSLATOR_CLI_BODY_END>>>
```

User prompt 組裝內容：函式目前的簽名（第六章第 3 步定位到的節點，取 `ast.unparse()` 只渲染這個函式的 `decorator_list`＋簽名列，不含 body，讓模型知道自己在填什麼形狀的函式）、`description`、`context`（若非空）、`context_files` 逐檔案列出「路徑 + 完整內容」。

---

## 八、Git Snapshot 與 Commit 顆粒度

回應 00 十章待決定事項：「translator-cli 的 git commit 顆粒度與平行寫入的鎖機制，細節待本文件定案」。

### 決策：每個 task 一個 commit，不需要鎖機制

**顆粒度**：
- `generate_scaffold()` 成功寫入所有骨架檔案後，`git add -A && git commit -m "scaffold: initial skeleton from python_structure"` 一次性 commit，作為後續所有 `fill_function()` commit 的共同基礎
- `fill_function()` 每次成功寫入後，`git add {target_file} && git commit -m "implement: {task_id 或 class_name.function_name} fill {target_file}"`——**只 add 這次實際寫入的那一個檔案**，不用 `-A`：即使working tree因為某種原因存在其他未預期的變更（理論上不該發生，見九章），也不會被這次 commit 意外一起帶走

**為什麼不需要鎖機制**：00 十章把這件事列為待決定，隱含假設「可能有平行寫入」；但實際檢視現有排程設計，**這個系統結構上不存在並行寫入的可能**：

1. `graph/nodes/implement_node.py`（01 六章已實作）用 `MODEL_SEMAPHORE = asyncio.Semaphore(1)` 包住每一次 `translator_cli.fill_function()` 呼叫——排程層 `get_ready_tasks()` 可以一次回傳多個就緒 task，但 `asyncio.gather()` 底下真正執行到 `fill_function()` 內部（含寫入磁碟＋commit）這一段，永遠只有一個 coroutine 在跑，其餘在等 semaphore
2. `generate_scaffold()`（④）與 `fill_function()`（⑤）不會同時執行：01 五章「平行分支：① → (② ∥ ③) → ([P] ∥ ④) → ⑤」的圖結構保證 `scaffold` 必須完成（連同 `plan`）才會進入 `implement`，兩者是先後關係，不是並行關係

因此 translator-cli **不需要**自己再實作一層檔案鎖／分散式鎖——上游（LangGraph 排程層）已經從結構上保證任何時刻最多一個呼叫端在寫入這個 git repo。這不是「暫時先不做，之後再補」的取捨，而是這個情境下鎖機制本來就沒有對應的併發場景需要保護，硬加一層只會是不會被觸發的死代碼，違反「不要為不可能發生的情境寫錯誤處理」的既有原則（呼應 00 二章）。

若未來排程設計改變（例如真的要讓多個本地模型實例平行跑，見 00 三章「硬體限制」目前明確排除這個可能性），到時候才需要重新評估這一節的假設是否還成立——這個決策的前提條件（`MODEL_SEMAPHORE(1)` 與 scaffold/implement 的先後順序）寫在這裡，方便日後如果真的要改，能一眼看到這裡的結論是奠基在哪兩個具體保證上。

---

## 九、衝突偵測

00 三章原文承諾「衝突偵測」，八章已經說明這不是併發鎖的情境；這裡的衝突偵測指的是另一種風險：**translator-cli 對 working tree 目前狀態的假設，跟磁碟上實際狀態不一致**。可能成因：人工不小心手動編輯了目標專案裡的檔案、上一輪執行中途被強制中斷、留下未 commit 的殘留寫入（理論上五章「呼叫失敗時不寫入任何內容」已經保證正常失敗路徑不會留殘留，但例如程序被 kill -9 這種非正常中斷仍可能留下部分寫入）。

**機制：每次寫入前的 precondition 檢查**——`generate_scaffold()`／`fill_function()` 動筆寫任何檔案之前，先跑 `git status --porcelain`：

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
    ├── client.py           # 對外唯一入口：generate_scaffold()、fill_function()
    ├── types.py            # FillResult（既有）
    ├── python_adapter.py   # 十章 LanguageAdapter 的 Python 實作：ast 定位／替換／渲染／語法驗證
    ├── scaffold.py         # 四章：directory_tree 解析、interfaces 分組渲染、import 解析
    ├── ollama_client.py    # 七章：httpx 呼叫 ollama（經 nginx）、delimiter 抽取、修正重試
    ├── prompts.py          # 七章 system/user prompt 模板
    ├── git_ops.py          # 八、九章：commit、git status 衝突偵測
    └── exceptions.py       # 內部例外型別（比照 design_agent/exceptions.py 既有慣例）
```

| 職責 | 說明 |
|---|---|
| 骨架生成（無模型呼叫） | 對應四章，`scaffold.py` |
| 填空（呼叫 ollama） | 對應五、七章，`client.py` 串接 `ollama_client.py` |
| AST 定位／替換／渲染 | 對應六、十章，`python_adapter.py` |
| git snapshot／commit／衝突偵測 | 對應八、九章，`git_ops.py` |
| 對外唯一入口 | 供 `graph/nodes/scaffold_node.py`／`graph/nodes/implement_node.py` 呼叫，兩個 node 本身不直接碰觸上述任何細節 |

---

## 十二、與 LangGraph 整合

沿用 01 四章已經定案的 node 對應，不需要新增或調整 node／edge 結構：

- `scaffold`（④）：`await translator_cli.generate_scaffold(state["python_structure"])`，回傳 `{"scaffold_done": result["success"]}`（若失敗，`scaffold_done=False` 目前的 graph 結構不會因此特別分流——這屬於既有 `scaffold_node.py`／01 文件的既知限制，不在本文件範圍內解決，07a 只保證 `generate_scaffold()` 本身在失敗時清楚回報 `error`）。**`result["skipped_interfaces"]`（見四章）目前也還沒有管道往 `RefactorState` 寫**——`scaffold_node.py` 現在的回傳只有 `scaffold_done` 這個布林值，沒有欄位可以承接這份清單；短期內 `skipped_interfaces` 至少要記進日誌供人工事後查閱，State 是否需要新增對應欄位讓 ⑦ Debug Agent 之類的下游也讀得到，留給 08a（骨架實作 Agent 詳細設計，待建立）評估，不在本文件範圍內決定
- `implement`（⑤）：`_run_one_task()` 在 `MODEL_SEMAPHORE(1)` 內呼叫 `translator_cli.fill_function(...)`，見二章「與既有程式碼的介面異動」需要補上的三個引數

translator-cli 本身**不**依賴 `RefactorState` 其餘欄位，只需要呼叫端組好的顯式引數——這是刻意的邊界：`translator_cli/` 套件不 import `graph.state`，維持跟 `refactor_harness/`／`spec_collection_agent/` 一致的獨立性（01 二章「設計原則」）。

---

## 十三、錯誤處理範圍

比照 04a 九章／05a 十二章的邊界：`retry_count` 迴圈只包住 `implement → run_tests → debug`，translator-cli 本身的呼叫失敗不直接觸發這個迴圈，而是反映成 `FillResult(success=False)` 或 `generate_scaffold()` 的 `{"success": False}`，由呼叫端（`implement_node.py`／`scaffold_node.py`）決定後續（task 標記失敗、module 局部驗證會抓到、進而觸發 `debug`）。translator-cli 自己只負責：

- 網路層錯誤：七章「Timeout 與重試策略」的固定次數重試，重試仍失敗才回報 `success=False`
- 模型輸出格式／語法錯誤：七章「修正重試」固定重試一次，仍失敗才回報 `success=False`
- AST 定位失敗（scaffold/task 資料不一致）：不重試，直接回報——這是輸入端資料問題，見六章
- working tree 不乾淨（九章）：不重試，直接回報——需要人工介入核對，不是可以自動化解的暫時性錯誤
- `generate_scaffold()` 單一 `InterfaceSpec` 的型別字串驗證失敗（正規化後仍不合法）：**不中止、不重試**，跳過這一個函式並記進 `skipped_interfaces`，其餘介面正常繼續（見四章「語法驗證與寫入」的修正）——這點跟本文件先前版本、以及跟 05a／06a「單一 module／單一批次失敗就中止整條 run」的既有先例刻意不同：05a／06a 中止是因為那些失敗會讓**下游對著不完整規格工作**（`python_structure`／`task_list` 本身就是缺角的），但 `generate_scaffold()` 這裡失敗的粒度是單一函式，其餘 71 個介面的骨架完全不受影響，沒有理由讓一個函式的問題拖垮整個 `python_structure`（已知涵蓋 72 個介面）的骨架產出
- `generate_scaffold()` 整檔組裝完成後的最終 `ast.parse()` 驗證失敗（四章步驟 4）：不重試，直接中止——這代表組裝邏輯本身有 bug（如 import 合併沒去重乾淨），不是個別介面的型別字串問題，性質同 05a／06a 的「中止」情境

---

## 十四、待決定事項

- [x] ~~`TaskSpec` 新增 `class_name`／`function_name` 欄位（見二章「與既有程式碼的介面異動」）~~——已套用並補上 `tests/plan_agent/` 測試、真實 Claude API 呼叫驗證過（見二章）；`RefactorState.python_project_path` 仍待套用
- [ ] `implement_node.py` 補上 `context`／`class_name`／`function_name` 三個引數（見二章第 3 項）——待 translator-cli 本體（07b）落地時一併處理
- [ ] 六章「已知關鍵字表」目前只列這個專案已知會用到的 FastAPI／SQLAlchemy／`Decimal` 型別，實際跑過真實專案後可能要補充其他框架型別（如 `BackgroundTasks`、`Header`），比照 05a 十三章「待接上真實專案輸出後校準」的既有模式
- [ ] 七章 `TRANSLATOR_CLI_TIMEOUT_SECONDS`／`TRANSLATOR_CLI_NETWORK_RETRIES` 的預設值是估計值，需要接上真實 ollama／nginx 環境後依實測生成耗時調整
- [ ] delimiter 抽取失敗／語法錯誤的「修正重試」prompt 具體措辭，需要用 qwen2.5-coder:32b 實測校準——不同模型對「請修正上次的格式錯誤」這類指令的遵從度不同，可能需要調整措辭甚至加入少量 few-shot 範例
- [ ] `generate_scaffold()` 失敗時 `scaffold_node.py` 目前不會特別分流（見十二章）——這屬於 08a（骨架實作 Agent 詳細設計，待建立）該補的既知限制，07a 只確保失敗訊息清楚，不越界處理 node 層級的分流邏輯
- [ ] **建議修正 05a 型別對應表（根因，不屬於 07a 範圍，07a 只做防禦）**：四章「型別字串正規化」發現的問題根因在 05a 五章的型別對應表——只涵蓋 JDK 集合型別，任何其他泛型包裝類別（專案自訂的 `ResponseResult<T>`、JDK `Function<T,R>`、Spring Data `Specification<T>`）落入「原樣保留字串」fallback，保留的是**未轉換的 Java 語法**。05a 文件字面上寫「交由六章 LLM 設計階段判斷」，但實測 06 章 `method_decisions` 的 LLM 呼叫只被問 `uncovered_params`／`needs_db_session`，從未被要求修正 return type——這個承諾沒有真的被兌現，`type_mapping.py` 也沒有對應的通用泛型轉換規則。07a 這裡的「型別字串正規化」是防禦性修正（保護 `generate_scaffold()` 不被上游的原始字串炸掉），**不是**取代 05a 該做的事——05a 若能在型別對應表就近直接加一條「非清單裡的泛型包裝類別，機械轉 `Foo<Bar>` → `Foo[Bar]`」規則，07a 這裡的正規化就變成單純的 defense-in-depth（永遠是 no-op，因為輸入早就已經合法），品質也會更好（05a 那層還能一併決定要不要順便處理殘留的裸型別變數 `T`／`String` 這類語意問題，07a 這裡只能做純符號轉換）。是否要回頭修 05a／05b，留給使用者評估，07a 不因此假設它一定會被修，仍然保留自己的正規化與失敗隔離機制

---

*各 Agent／工具的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
