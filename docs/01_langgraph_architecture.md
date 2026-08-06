# LangGraph 實作細節：Java → Python 重構 Orchestrator

---

## 一、本文件的定位

00 的一、整體流程概覽（流程圖＋各階段對應文件表）只勾勒整體流程與 Agent 職責邊界，State 完整定義與 Graph 節點/邊建構整段都在本文件（00 九章目前只留一段指向本文件的摘要，不重複列表）。本文件補三個更深一層的實作問題：

1. State 的實際型別（尤其 `python_structure` 需精確到檔案路徑＋函式簽名）
2. ③ 完成後的平行分支、⑤ 的 module 排程在 LangGraph 裡具體怎麼寫
3. 專案怎麼從零建立、圖怎麼組裝、跨平台會踩到什麼坑

---

## 二、專案目錄結構

```
refactor-project/
├── main.py                     # 進入點：組裝並執行 graph
├── graph/
│   ├── __init__.py
│   ├── state.py                 # RefactorState 與所有子型別定義
│   ├── builder.py                # StateGraph 組裝（node 註冊、edge 連接）
│   ├── scheduler.py              # ⑤ 的 module 依賴圖排程器（見六）
│   └── nodes/
│       ├── __init__.py
│       ├── parse_node.py         # ① 解析 Agent（Claude API）
│       ├── spec_node.py          # [A] Spec Agent（程式邏輯，見 03a/03b）
│       ├── collection_node.py    # [B] Collection Agent（程式邏輯 + LLM，見 03a/03b）
│       ├── design_node.py        # ③ 架構設計 Agent（Claude API）
│       ├── plan_node.py          # [P] Plan Agent（Claude API）
│       ├── scaffold_node.py      # ④ 骨架實作 Agent（translator-cli，骨架生成模式，非填空模式，見四備註）
│       ├── implement_node.py     # ⑤ 功能改寫 Agent（translator-cli + scheduler）
│       ├── debug_node.py         # ⑦ Debug Agent（Claude API）
│       └── give_up_node.py       # 超過重試次數的收尾（通知人工）
│
├── refactor_harness/            # 見 02a / 02b（Agent ②/⑥ 與共用核心）
├── spec_collection_agent/       # 見 03a / 03b（[A]/[B] Agent 核心邏輯）
├── translator_cli/              # 見 04a / 04b（Agent ④/⑤ 呼叫本地模型）
│
├── config/
│   ├── harness.yaml
│   └── mask_rules.yaml
├── specs/
│   └── openapi.json             # [A] 落地檔案，見 03a 二
├── postman/                     # [B] 產出，見 03a 三
│   ├── collection_readonly.json
│   ├── collection_mutation.json
│   └── unfilled_endpoints.json
├── fixtures/
│   ├── seed.sql
│   └── golden/
│
├── requirements.txt
├── .env
└── .gitignore
```

**設計原則**：`graph/` 只放 LangGraph 相關的組裝邏輯與 node 定義；每個 Agent 實際呼叫的「重活」（Harness 比對、[A]/[B] 的 OpenAPI/Collection 處理、translator-cli 呼叫）都委派給 `refactor_harness/`、`spec_collection_agent/`、`translator_cli/` 這三個獨立套件，node 函式本身盡量薄，方便 stub-first 開發（見七）。

### requirements.txt（核心依賴，鎖定版本）

```
langgraph==1.2.6
pyyaml==6.0.3
python-dotenv==1.2.2
httpx==0.28.1
```

> **鎖死版本而非用 `>=`**：orchestrator 長時間無人值守運行，`langgraph` 仍在 1.x 早期，版本間可能有 breaking change（`Send` API、reducer 行為、checkpointer 介面）。用 `==` 精確鎖定，升級時主動跑 `pip install -U langgraph` 並重跑 stub-first 驗證（見七）。上面版本號僅供參考，建立專案時用 `pip index versions langgraph` 確認最新版即可。
>
> **不含 `langchain-anthropic`**：Claude API 呼叫統一走 `anthropic` SDK 直接呼叫（見 03b 一章「相依套件」、00 六章「Claude API 呼叫封裝」），全 repo 沒有任何地方 import `langchain`，不需要這個依賴。
>
> `newman`、`openapi-to-postmanv2` 是 Node.js 工具，見 00 五；建議 `package.json` 同樣鎖版本，用 `npm ci`。

### .env

```
ANTHROPIC_API_KEY=
JAVA_BASE_URL=http://localhost:8080
JAVA_JAR_PATH=../lang-exam-api-refactor/target/app.jar
JAVA_EXECUTABLE_PATH=java
PYTHON_BASE_URL=http://localhost:8000

# 本地模型：另一台 Mac 上 ollama 前面掛了 nginx 做 token 驗證，
# ollama 本身不對外開放，OLLAMA_BASE_URL 指向的是 nginx 的 port，不是 ollama 原生的 11434。
OLLAMA_BASE_URL=http://<另一台Mac的IP>:<nginx對外port>/v1
OLLAMA_API_KEY=<與另一台 Mac 上 nginx 設定的 token 一致>

# 測試 DB（見 00 五）：獨立於正式/開發 DB，命名加 _TEST；
# 下面三個變數指向同一顆測試 DB，格式依各自工具而定。
TEST_DB_DSN=postgresql://postgres:password@127.0.0.1:5432/MOC_MATSUEXAM_TEST
SPRING_DATASOURCE_URL=jdbc:postgresql://127.0.0.1:5432/MOC_MATSUEXAM_TEST
SPRING_DATASOURCE_USERNAME=postgres
SPRING_DATASOURCE_PASSWORD=password

# Python（FastAPI + SQLAlchemy）服務自己的 DB 連線，值同 TEST_DB_DSN
DATABASE_URL=postgresql://postgres:password@127.0.0.1:5432/MOC_MATSUEXAM_TEST
```

> `JAVA_JAR_PATH` 用相對於 `refactor-project/` 的相對路徑（見 03a 二「路徑格式」），不是絕對路徑，避免 `.env` 換機器/換使用者就失效。
>
> `JAVA_EXECUTABLE_PATH`（見 03a 二「Java 執行檔路徑」）：預設值 `java`，交給系統 PATH 解析；PATH 上有多個 JDK 版本、且預設解析到的版本與 Java 專案要求版本不符時，改填該 JDK 的 `java`／`java.exe` 完整路徑，避免啟動時因位元組碼版本不符而失敗。這個值機器規格相關、不強求可攜，性質同 `OLLAMA_BASE_URL` 的 IP。
>
> `SPRING_DATASOURCE_*` 是 [A] Spec Agent／② 測試 Agent 啟動 Java 服務（`subprocess.Popen`，見八）時帶入的環境變數，Spring Boot 會自動覆蓋 `spring.datasource.*`，不需改 `application.properties`（⑥ 測試執行 Agent 操作的是 Python 服務，用不到這組 JDBC 格式的變數）。
>
> `OLLAMA_API_KEY`：translator-cli 呼叫 `OLLAMA_BASE_URL` 時，須在 request header 帶上 `Authorization: Bearer {OLLAMA_API_KEY}`，讓另一台 Mac 上的 nginx 驗證通過後才轉發給 ollama。這個變數只在 translator-cli 內部使用，不放進 `RefactorState`（與 `OLLAMA_BASE_URL` 同一類——性質同 `DATABASE_URL`，是外部工具自己讀的環境變數，不是 Orchestrator 決策要用的資料，見三章 State 設計）。實際 header 組裝與 request schema 見 `07a_translator_cli_architecture.md`。

### 初始化步驟

```bash
mkdir refactor-project && cd refactor-project
python3 -m venv .venv
source .venv/bin/activate          # Windows 見八
pip install -r requirements.txt
# 建立 .env 並依上方範例填入實際值（.env 已被 .gitignore 排除，不進版控）
git init
```

---

## 三、State Schema 設計

State 欄位對應到 00 一章「各階段對應文件表」列出的各個 Agent，這裡用 `TypedDict` 定義實際結構，是欄位定義的唯一來源（00 九章不重複列表）。關鍵決策：

- 用 `TypedDict` 而非 Pydantic——State 是普通 dict，不需要額外的 serialize/validate 開銷
- `completed_tasks`、`failed_tasks`、`partial_reports` 是逐 task 累積寫入的（見六），掛 `Annotated[list, operator.add]` reducer
- `python_structure` 鎖死到「檔案路徑＋函式簽名」層級，而非自由格式字串，讓 [P] 和 ④ 消費同一份有結構保證的資料

```python
# graph/state.py
from typing import TypedDict, Annotated, Literal, NotRequired
import operator


# ── Agent ① 輸出的子型別 ──────────────────────────────
# 不含 python_method／python_files／python_target：Java→Python 的檔案/函式
# 對應唯一權威來源是 Agent ③ 的 python_structure.interfaces，見 04a 六章。
class MethodInfo(TypedDict):
    java_method: str
    class_name: str        # 所屬 Java class，供 ③ 重新掃描簽名時比對回正確的類別
                            # （同一 module 內跨層同名方法會歧義，見 05a 二章）
    description: str
    complexity: Literal["low", "medium", "high"]


class ModuleInfo(TypedDict):
    module: str                    # 對應 fixtures/golden/{module}/ 子目錄
    summary: str                   # 模組業務邏輯摘要，供 Agent ③ 設計 Python 結構/interface 邊界時參考（見 04a）
    java_files: list[str]
    depends_on: list[str]          # 依賴的其他 module 名稱（⑤ 排程依此做 topological sort）
    methods: list[MethodInfo]


class ApiMapping(TypedDict):
    endpoint: str
    http_method: str
    java_controller: str
    module: str


# ── Agent ③ 輸出：python_structure ──
class ParamSpec(TypedDict):
    name: str
    type: str


class InterfaceSpec(TypedDict):
    file_path: str                 # 相對路徑，如 "app/repositories/user_repository.py"
    class_name: str | None
    function_name: str
    params: list[ParamSpec]
    return_type: str
    # 僅 routers 層 API 邊界方法非 None，值沿用 ApiMapping.http_method／
    # .endpoint（見 05a 五章、九章）；NotRequired 讓既有所有建構 InterfaceSpec
    # 的地方（含本章七節 design_node.py stub）不需要跟著補這兩個欄位。
    http_method: NotRequired[str | None]
    route_path: NotRequired[str | None]


class PythonStructure(TypedDict):
    directory_tree: str             # 目錄結構的文字表示，供 ④ 建立骨架時參照
    interfaces: list[InterfaceSpec] # [P] target_files 與 ④ 骨架簽名的唯一權威來源


# ── [P] Plan Agent 輸出：task list ────────────────────
class TaskSpec(TypedDict):
    id: str
    module: str
    description: str
    target_files: list[str]        # 必須是 python_structure.interfaces 中已存在的 file_path
    context: str
    depends_on: list[str]


# ── 整體 State ─────────────────────────────────────────
class RefactorState(TypedDict):
    # 進入點輸入（main.py 組裝 initial_state 時填入，見九）
    java_project_path: str

    # 環境設定（main.py 從 .env 讀入，見九；implement node 需要）
    test_dsn: str          # 對應 .env 的 TEST_DB_DSN，Orchestrator 直接連 DB 用
    python_base_url: str   # 對應 .env 的 PYTHON_BASE_URL，Orchestrator 打 API 用
    # 註：.env 的 DATABASE_URL 不放進這個 State——那是 Python 服務進程自己讀的環境變數，
    # Orchestrator 不會用到，只需確保啟動 Python 服務的 subprocess 有繼承 .env。

    # Agent ①
    module_list: list[ModuleInfo]
    api_to_python_target: list[ApiMapping]

    # Agent A / B
    openapi_spec: dict
    collection_readonly_path: str
    collection_mutation_path: str
    # [B] 尚未被人工解決（既沒填值、也沒標記 skip）的 endpoint 清單，
    # 供 gen_manual_fill_templates／gen_collection 兩個節點後的條件邊
    # 判斷要不要暫停等人工處理（見五「人工填值關卡」、03a 三章「人工
    # 填值機制」）。空清單＝全部解決。
    collection_manual_fill_pending: list[str]

    # Agent ②
    golden_output: dict

    # Agent ③
    python_structure: PythonStructure
    route_to_file_mapping: dict

    # [P] Plan Agent
    task_list: list[TaskSpec]

    # Agent ④：平行分支旗標，scaffold 節點回傳「做完了」用，僅供除錯觀察，
    # LangGraph 的 fan-in（見五）靠邊結構完成，不依賴讀取這個值
    scaffold_done: bool

    # Agent ⑤（逐 task 累積寫入，需要 reducer）
    completed_tasks: Annotated[list[str], operator.add]
    failed_tasks: Annotated[list[str], operator.add]
    partial_reports: Annotated[list[dict], operator.add]   # 每個 module 局部驗證結果（含 regression 重驗），逐次累加供 ⑦ 回溯

    # Agent ⑤：每次呼叫重新計算的「當下完整快照」，不是累加事件，故不掛 reducer
    blocked_modules: list[str]    # 因上游 module 未驗證通過而從未進入就緒佇列的 module
    failed_modules: list[str]     # 確實執行過、驗證過、但沒通過的 module（含 regression 造成的失敗）

    # Agent ⑥
    test_results: dict

    # Orchestrator
    retry_count: int
```

> `partial_reports`／`blocked_modules`／`failed_modules` 是排程實作（見六）新增的內部欄位，00 的流程圖層級不會細到列出這幾個欄位（00 九章已不重複列 State 欄位，見一）。`partial_reports` 是逐次累加的事件記錄，掛 reducer 正確；`blocked_modules`／`failed_modules` 是每次結束當下的狀態快照，不掛 reducer、由 `implement_node.run()` 整包覆蓋。兩者讓 `run_tests`／`debug` 能區分「程式碼根本沒被排到」和「程式碼確實跑過但驗證沒過」，決定要不要消耗 `retry_count`（見六）。

---

## 四、Graph 節點與 Agent 對應

| Node 名稱 | 對應 Agent | 型態 | 檔案 |
|---|---|---|---|
| `extract_spec` | [A] Spec Agent | 程式邏輯 | `graph/nodes/spec_node.py`（見 03a/03b） |
| `gen_manual_fill_templates` | [B] Collection Agent（階段一） | 程式邏輯，不呼叫 LLM | `graph/nodes/collection_node.py`（`run_generate_templates`，見 03a 三章「人工填值機制」、03c） |
| `gen_collection` | [B] Collection Agent（階段二） | 程式邏輯 + LLM | `graph/nodes/collection_node.py`（`run`，見 03a/03c） |
| `await_manual_fill` | — | 程式邏輯 | `graph/nodes/await_manual_fill_node.py`（見五「人工填值關卡」，`gen_manual_fill_templates`／`gen_collection` 共用同一個終止節點） |
| `parse` | ① 解析 Agent | Claude API | `graph/nodes/parse_node.py`（見 04a：排在 `gen_collection` 之後，因為 skip 呼叫鏈排除需要讀 [B] 已定案的 `unfilled_endpoints.json`） |
| `record_tests` | ② 測試 Agent | 程式邏輯（Harness） | `refactor_harness/langgraph_nodes/test_nodes.py`（見 02b；與 `design` 平行執行，見五章「平行分支」） |
| `design` | ③ 架構設計 Agent | Claude API | `graph/nodes/design_node.py`（不依賴 `golden_output`，與 `record_tests` 平行執行，見 05a 十一章、五章「平行分支」） |
| `plan` | [P] Plan Agent | Claude API | `graph/nodes/plan_node.py` |
| `scaffold` | ④ 骨架實作 Agent | translator-cli（骨架生成模式） | `graph/nodes/scaffold_node.py` |
| `implement` | ⑤ 功能改寫 Agent | translator-cli（填空模式）＋ 排程器 | `graph/nodes/implement_node.py` |
| `run_tests` | ⑥ 測試執行 Agent | 程式邏輯（Harness） | `refactor_harness/langgraph_nodes/test_nodes.py`（見 02b） |
| `debug` | ⑦ Debug Agent | Claude API | `graph/nodes/debug_node.py` |
| `give_up` | — | 程式邏輯 | `graph/nodes/give_up_node.py` |

> **`scaffold` 與 `implement` 呼叫 translator-cli 的兩種不同模式，不是同一支 API**：`implement`（⑤）用「填空模式」`fill_function()`——目標檔案與空函式簽名已存在，模型只回傳單一函式本體，用 AST 插入。`scaffold`（④）從無到有建立目錄、檔案、class、空函式簽名，沒有既有結構可插入，因此呼叫另一個「骨架生成模式」介面（如 `translator_cli.generate_scaffold(python_structure)`，整檔輸出）。精確介面定義見 `07a_translator_cli_architecture.md`，這裡先釘死「不是同一個契約」，避免誤用 `fill_function()` 處理不存在的檔案。

---

## 五、Graph 建構：主流程、平行分支、Retry 迴圈

### 主流程（線性）

```python
# graph/builder.py
from langgraph.graph import StateGraph, END
from graph.state import RefactorState
from graph.nodes import (
    parse_node, spec_node, collection_node, await_manual_fill_node, design_node,
    plan_node, scaffold_node, implement_node, debug_node, give_up_node,
)
from refactor_harness.langgraph_nodes.test_nodes import (
    record_golden_output, run_postman_tests, should_debug_or_done,
)

def build_graph():
    builder = StateGraph(RefactorState)

    builder.add_node("extract_spec", spec_node.run)
    builder.add_node("gen_manual_fill_templates", collection_node.run_generate_templates)
    builder.add_node("gen_collection", collection_node.run)
    builder.add_node("await_manual_fill", await_manual_fill_node.run)
    builder.add_node("parse", parse_node.run)
    builder.add_node("record_tests", record_golden_output)
    builder.add_node("design", design_node.run)
    builder.add_node("plan", plan_node.run)
    builder.add_node("scaffold", scaffold_node.run)
    builder.add_node("implement", implement_node.run)
    builder.add_node("run_tests", run_postman_tests)
    builder.add_node("debug", debug_node.run)
    builder.add_node("give_up", give_up_node.run)

    builder.set_entry_point("extract_spec")
    builder.add_edge("extract_spec", "gen_manual_fill_templates")

    # 階段一（產生人工填值模板）後的人工填值關卡（Conditional Edge，見
    # 本節下方說明）：有待填 endpoint 就暫停，否則直接進階段二
    builder.add_conditional_edges(
        "gen_manual_fill_templates",
        collection_node.should_await_manual_fill_templates_or_continue,
        {
            "continue": "gen_collection",
            "await_manual_fill": "await_manual_fill",
        },
    )

    # 階段二（跑完剩下的 pipeline）後的人工填值關卡：防禦性的第二道
    # 關卡，正常情況下階段一已經擋下所有待填 endpoint
    builder.add_conditional_edges(
        "gen_collection",
        collection_node.should_await_manual_fill_or_continue,
        {
            "continue": "parse",
            "await_manual_fill": "await_manual_fill",
        },
    )
    builder.add_edge("await_manual_fill", END)

    # parse（① 解析 Agent）排在 [B] 之後：skip 呼叫鏈排除（見 04a 五章）
    # 要讀 [B] 已定案的 postman/unfilled_endpoints.json，排更前面這份輸入不存在
    #
    # 平行分支：parse 完成後，record_tests（②）與 design（③）同時進入就緒
    # 狀態——② 不依賴③的輸出，③ 不依賴 golden_output，兩者互不相依，見
    # 00 一章流程圖、05a 十一章
    builder.add_edge("parse", "record_tests")
    builder.add_edge("parse", "design")
```

### 平行分支：① → (② ∥ ③) → ([P] ∥ ④) → ⑤

00 的流程圖有兩處 fan-out/fan-in：`parse` 完成後 `record_tests` 和 `design` 平行執行；`design` 完成後 `plan` 和 `scaffold` 平行執行，四者都完成才進 `implement`。在 LangGraph 的 `StateGraph` 裡，這個 fan-out / fan-in 不需要額外的 API：**只要兩個節點都以同一個節點為前驅、又都指向同一個後繼節點，LangGraph 執行時會在同一個 superstep 平行呼叫兩者，並等兩者都完成後才觸發後繼節點。**

```python
    # fan-in：plan／scaffold 的前驅是 record_tests 與 design 兩者都完成才
    # 觸發——plan／scaffold 本身不讀 golden_output，但仍等 record_tests 一併
    # 完成才進入下一階段，維持圖上單一明確的合流點（比每個節點各自判斷
    # 「我依賴的東西是否就緒」更容易推理，也跟 00 一章流程圖的單一菱形
    # 合流點一致）；若日後 record_tests 明顯拖慢整體關鍵路徑、且 plan／
    # scaffold／implement 都已穩定，可再評估讓 record_tests 直接接到
    # run_tests（⑥）前，不強制在此合流，見九章「已知限制」同類型的
    # 「先求正確、非阻塞優化留待穩定後再做」原則
    builder.add_edge("record_tests", "plan")
    builder.add_edge("design", "plan")
    builder.add_edge("record_tests", "scaffold")
    builder.add_edge("design", "scaffold")

    # fan-in：implement 的兩個前驅都完成後才觸發一次
    builder.add_edge("plan", "implement")
    builder.add_edge("scaffold", "implement")
```

> fan-in 不需要 reducer 的**前提**是：`record_tests`／`design`／`plan`／`scaffold` 的回傳值只包含各自實際更動的 key（互不相交），**絕對不能用 `{**state, ...}` 展開整包 state**——一旦多個分支在同一個 superstep 對同一個 key 各自寫入，就違反「無 reducer 時每個 key 只能被一個節點寫入」的前提，是未定義行為。`design` 雖然只有單一前驅（`parse`），但因為它同時也是 `record_tests` 的平行分支，仍需遵守「只回傳自己實際更動的 key」——不能再套用七章對純線性 node 的 `{**state, ...}` stub 慣例，這是本次改成平行分支後 `design_node.py` 的 stub 也要跟著調整的地方（見七章）。`implement` 節點**內部**對 module/task 的平行處理才真的需要 reducer（見六）。

### Retry 迴圈（Conditional Edge）

```python
    builder.add_edge("implement", "run_tests")

    builder.add_conditional_edges(
        "run_tests",
        should_debug_or_done,
        {
            "done": END,
            "debug": "debug",
            "give_up": "give_up",
        },
    )
    builder.add_edge("debug", "implement")
    builder.add_edge("give_up", END)

    return builder.compile()
```

`should_debug_or_done`（定義在 `02b_harness_code.md` 的 `test_nodes.py`）依 `test_results.status` 與 `retry_count` 三分流：全過結束、失敗但未超重試上限進 `debug`、超過上限進 `give_up`。`give_up` 不是 END 本身，而是一個獨立節點——保留這個節點是為了讓「通知人工」這個動作有明確落點（Slack/email，見 02a 十六待實作清單），而不是讓圖靜默結束。

### 人工填值關卡（Conditional Edge，兩道）

**設計**：[B] Collection Agent 的填值機制是「所有需要動態值的 mutation endpoint，一律由人工在階段一主動產生的模板裡提供真實 payload，Claude API 完全不參與填值」（見 03a 三章「人工填值機制」，設計動機與細節不在此重複）。對應到 graph 層，`gen_collection` 拆成兩個 node：`gen_manual_fill_templates`（階段一，落地 `openapi.json`、產生模板，不呼叫 Claude API）與 `gen_collection`（階段二，假設模板已填完，跑鏈式依賴偵測、folder 分組、套用人工填值、注入）。暫停點因此提早到「連鏈式依賴偵測這種真的要花錢的 Claude API 呼叫都還沒開始，就先讓人工把值填好」，不會白白燒掉 API 額度等人工。

兩個 node 之後各接一道 conditional edge，判斷依據、`await_manual_fill` 節點都共用同一套機制：

**判斷依據**：兩個 node 執行完後，都把回傳值（`gen_manual_fill_templates()`／`CollectionAgentResult.manual_fill_pending`，見 03c 三章）寫進 `state["collection_manual_fill_pending"]`（見三章 State Schema）。這份清單只列**尚未被人工解決**（既沒填值、也沒標記 `skip`）的 endpoint；已經 `skip` 的視為「人工確認這個 endpoint 是特例、不走一般重構驗證流程」，不算 pending。

```python
# graph/nodes/collection_node.py
def should_await_manual_fill_templates_or_continue(state: RefactorState) -> str:
    return "await_manual_fill" if state.get("collection_manual_fill_pending") else "continue"


def should_await_manual_fill_or_continue(state: RefactorState) -> str:
    return "await_manual_fill" if state.get("collection_manual_fill_pending") else "continue"
```

- **`gen_manual_fill_templates` 後**：空清單 → `"continue"` → 直接進 `gen_collection`（階段二），跳過等待（例如重跑時模板早已填完）；非空 → `"await_manual_fill"`，這是**預期中的常態路徑**——第一次跑一定會停在這裡，等人工填完 `postman/manual_fill/` 底下的模板。
- **`gen_collection` 後**：空清單 → `"continue"` → 走原本的 `record_tests`；非空 → `"await_manual_fill"`，這是**防禦性的第二道關卡**，正常情況下第一道已經擋下所有待填 endpoint，只有「階段一之後 `openapi.json` 又變動」「人工填的值套用失敗」這類邊界情況才會走到這裡。

**`await_manual_fill` 節點**（`graph/nodes/await_manual_fill_node.py`，比照 `give_up_node` 的「終止節點」寫法，不是 `async def run` 的線性展開 state 慣例，因為它不需要再往下傳遞任何新資訊）：只做一件事——把 `state["collection_manual_fill_pending"]` 印出來，附上 `postman/manual_fill/` 路徑提示，然後這條 graph run 結束（`add_edge("await_manual_fill", END)`）。**不是**跟 `give_up` 一樣代表失敗，只是「暫停等人工」，語意上更接近一個特殊的正常結束狀態。兩道關卡共用同一個節點，因為對人工來說是同一件事：去 `postman/manual_fill/` 把值填完。

**已知限制：目前的「續跑」不是真正的 LangGraph resume**——`main.py` 目前沒有配置 checkpointer（見九），`await_manual_fill` 之後的 END 是這次 `graph.ainvoke()` 呼叫的終點，狀態不會保留。人工補完 `postman/manual_fill/` 底下的值後，理論上「重新整個跑 `python main.py`」可以繼續，但那會從 `extract_spec` 重頭開始，連帶重新呼叫 Claude API 做一次 `parse`／`design`（這兩步跟 collection 填值完全無關，純屬浪費）。

現階段（stub-first、尚未接上 checkpointer）的務實做法：**直接呼叫 `run_collection_agent()`**（`specs_dir`/`postman_dir` 帶跟階段一同一組路徑，不透過整個 graph、也不需要重跑 `generate_manual_fill_templates()`——`specs/openapi.json` 跟 `postman/manual_fill/` 都已經是階段一落地的檔案，`run_collection_agent()` 直接讀就好）。**要注意這個「續跑」的實際成本**：每次呼叫 `run_collection_agent()` 都會重新完整跑一次鏈式依賴偵測（MAP/REDUCE，真實 Claude API 呼叫），不是只處理「這次新填的值」——這在實測中是真實成本的主要來源之一（見 `docs/03_spent_cost_estimate.md`），如果卡在第二道防禦性關卡、需要多次補值才能填完，每補一次都要重新付一次鏈式依賴偵測的成本，不是免費的。真正讓 `python main.py` 能從 `gen_manual_fill_templates`／`gen_collection` 斷點續跑（而不必重跑 `parse`／`design`，也不必每次重付鏈式依賴偵測的成本），需要接上 LangGraph 的 checkpointer（如 `MemorySaver` 或持久化版本）＋固定 `thread_id`，留待專案脫離 stub-first 階段、main.py 需要支援長時間、可中斷續跑的執行模式時再一併處理，不在本次範圍。

---

## 六、⑤ 功能改寫 Agent：Module 排程器實作

對應 00 三章的排程原則：**module 間依賴關係沿用 Agent ① `module_list` 的 `depends_on`，優先讓同一 module 的 task 連續完成並通過局部驗證，才釋放依賴它的下游 module；本地模型的實際生成請求全域序列化（併發數＝1）。**

### 設計決策：為什麼不用 LangGraph 的 `Send` API 做 task 級平行節點

LangGraph 的 `Send` API 可把一個 node 動態展開成多個平行子節點，但這裡刻意不用：本地模型併發數鎖死為 1，`Send` 的「多個子節點同時跑」在生成階段仍會被同一個資源瓶頸收斂成序列，只增加圖的複雜度（reducer、子節點失敗回報）換不到平行效益。因此 `implement` 維持**單一 node**，內部用輕量排程器＋`asyncio.Semaphore(1)` 分離「就緒佇列」與「實際呼叫序列化」。

### 排程器

```python
# graph/scheduler.py
from graph.state import ModuleInfo, TaskSpec


class ModuleScheduler:
    """
    依 module_list 的 depends_on 做 topological 排程：
    - 只有「所有依賴 module 都已通過局部驗證」的 module，其 task 才會進入就緒佇列
    - 同一 module 內，task 依 depends_on 序列化（repository → service → router）

    task 級 depends_on 由 [P] Plan Agent 產出，若同 module 內漏填，順序會退化成
    依賴 asyncio.gather 的排程細節（未定義行為）。初始化時做防呆：沒有被引用、
    也沒填 depends_on 的 task，按 task_list 原始順序自動串成序列依賴。
    """

    def __init__(
        self,
        module_list: list[ModuleInfo],
        task_list: list[TaskSpec],
        already_completed: set[str] | None = None,
        already_failed: set[str] | None = None,
        already_verified_modules: set[str] | None = None,
    ):
        """
        `already_*` 讓 scheduler 從前一輪 implement 的執行結果恢復狀態——debug → implement
        是回頭呼叫同一個 node，若每次從零建立 scheduler，module_status 會被重置成全部
        pending，regression 偵測（見 check_upstream_regression）就抓不到已驗證過的 module。
        """
        self.modules = {m["module"]: m for m in module_list}
        self.tasks_by_module: dict[str, list[TaskSpec]] = {}
        for task in task_list:
            self.tasks_by_module.setdefault(task["module"], []).append(task)

        # module 名下擁有哪些檔案，用來偵測「後續寫入是否波及已驗證過的上游 module」（見下方 check_upstream_regression）。
        # 從 task_list 的 target_files 彙整，而不是 module_list 的檔名——① 的檔名只是猜測，
        # ③ 可能整個改寫；task_list.target_files 必須是 python_structure.interfaces 中已存在的
        # file_path，才是這個時間點真正權威的檔案路徑來源（見 04a 六章）。
        # 只取 target_files[0]：這是 translator-cli 實際寫入的唯一目標檔案，target_files 其餘
        # 元素只是唯讀 context（referenced_interfaces、跨 module 的 schemas/models，見 06a 七章），
        # 若整份 target_files 都算「擁有」，會把只是讀取過的其他 module 檔案誤判成這個 module 名下，
        # 造成不相干 module 的偽 regression。
        self.module_owned_files: dict[str, set[str]] = {}
        for module, tasks in self.tasks_by_module.items():
            self.module_owned_files[module] = {t["target_files"][0] for t in tasks}

        self._backfill_missing_task_deps()

        # module 狀態：pending → in_progress → verified / needs_reverify / failed
        self.module_status = {m: "pending" for m in self.modules}
        for m in (already_verified_modules or ()):
            self.module_status[m] = "verified"
        self.task_done: set[str] = set(already_completed or ())
        self.task_failed: set[str] = set(already_failed or ())

    def check_upstream_regression(self, touched_files: list[str], skip_module: str) -> list[str]:
        """回傳已 verified、但這次寫入的檔案剛好落在其名下的 module 清單（排除自己）。
        只在真的觸及已驗證 module 範圍時才觸發，避免每次都全量重驗。
        """
        hit = set()
        for module, owned in self.module_owned_files.items():
            if module == skip_module or self.module_status.get(module) != "verified":
                continue
            if owned & set(touched_files):
                hit.add(module)
        return list(hit)

    def flag_for_reverify(self, module: str):
        """把已驗證 module 打回 needs_reverify——下游依賴它的 module 在重驗通過前不會被釋放。"""
        self.module_status[module] = "needs_reverify"

    def _backfill_missing_task_deps(self):
        """同 module 內沒有 depends_on、也未被引用的 task，依原始順序自動串成序列依賴。
        本地模型併發數鎖死為 1，這些 task 本來就得排隊，強制序列化不拉長總耗時，
        只是換取可預期性與可除錯性。
        """
        for tasks in self.tasks_by_module.values():
            referenced = {dep for t in tasks for dep in t["depends_on"]}
            unordered = [t for t in tasks if not t["depends_on"] and t["id"] not in referenced]
            for prev, curr in zip(unordered, unordered[1:]):
                curr["depends_on"] = curr["depends_on"] + [prev["id"]]

    def _module_deps_satisfied(self, module: str) -> bool:
        deps = self.modules[module]["depends_on"]
        return all(self.module_status.get(d) == "verified" for d in deps)

    def _task_deps_satisfied(self, task: TaskSpec) -> bool:
        return all(dep in self.task_done for dep in task["depends_on"])

    def get_ready_tasks(self) -> list[TaskSpec]:
        """回傳目前可以送去 translator-cli 的 task（尚未考慮模型併發限制）。"""
        ready = []
        for module, status in self.module_status.items():
            if status not in ("pending", "in_progress"):
                continue
            if not self._module_deps_satisfied(module):
                continue
            for task in self.tasks_by_module.get(module, []):
                if task["id"] in self.task_done or task["id"] in self.task_failed:
                    continue
                if self._task_deps_satisfied(task):
                    ready.append(task)
        return ready

    def mark_task_done(self, task: TaskSpec, success: bool):
        if success:
            self.task_done.add(task["id"])
        else:
            self.task_failed.add(task["id"])
        self.module_status[task["module"]] = "in_progress"

    def module_ready_for_verification(self, module: str) -> bool:
        """該 module 底下所有 task 都已完成（成功或失敗）才算齊，觸發局部驗證。"""
        all_tasks = self.tasks_by_module.get(module, [])
        return all(
            t["id"] in self.task_done or t["id"] in self.task_failed
            for t in all_tasks
        ) and len(all_tasks) > 0

    def mark_module_verified(self, module: str, passed: bool):
        self.module_status[module] = "verified" if passed else "failed"

    def all_done(self) -> bool:
        return all(s in ("verified", "failed") for s in self.module_status.values())
```

### `implement` node：串接排程器、Semaphore(1)、局部驗證

```python
# graph/nodes/implement_node.py
import asyncio
from graph.state import RefactorState
from graph.scheduler import ModuleScheduler
from translator_cli import client as translator_cli
from translator_cli.types import FillResult                  # 見 04a/04b：{success, error, diff, ...}
from refactor_harness.verifier.comparator import GoldenVerifier
from refactor_harness.fixtures.db_env import DbEnvironment

MODEL_SEMAPHORE = asyncio.Semaphore(1)  # 對應硬體限制：本地模型併發數＝1（見 00 三）


async def _run_one_task(task: dict) -> FillResult:
    async with MODEL_SEMAPHORE:
        return await translator_cli.fill_function(
            target_file=task["target_files"][0],
            description=task["description"],
            context_files=task["target_files"],
        )


def _partial_verify(module: str, db: DbEnvironment, verifier: GoldenVerifier) -> dict:
    db.apply_seed("fixtures/seed.sql", tables_to_truncate=verifier.tables)
    return verifier.verify_module(
        collection_path="postman/collection_readonly.json",
        module_filter=module,
    )


async def run(state: RefactorState) -> RefactorState:
    # partial_reports 是累加清單，同一 module 可能有多筆紀錄（初次驗證、regression 重驗），
    # 需取「最後一次」結果，避免 module 先 pass、後被 regression 打回 fail 卻仍被誤判為 verified。
    latest_module_status = {}
    for r in state.get("partial_reports", []):
        latest_module_status[r["module"]] = r["report"]["status"]
    already_verified_modules = {
        module for module, status in latest_module_status.items() if status == "pass"
    }

    scheduler = ModuleScheduler(
        state["module_list"],
        state["task_list"],
        # 從 state 恢復上一輪（debug 回圈重入）的進度，否則 module_status 會被重置成
        # 全部 pending，regression 偵測就抓不到這輪改動波及了哪個已驗證的舊 module。
        already_completed=set(state.get("completed_tasks", [])),
        already_failed=set(state.get("failed_tasks", [])),
        already_verified_modules=already_verified_modules,
    )
    db = DbEnvironment(test_dsn=state.get("test_dsn"))
    verifier = GoldenVerifier(
        python_base_url=state.get("python_base_url"),
        golden_dir="fixtures/golden",
    )

    completed, failed, partial_reports = [], [], []

    while not scheduler.all_done():
        ready = scheduler.get_ready_tasks()
        if not ready:
            break  # 沒有可執行的 task：全部做完、卡在失敗的上游 module，或還有 needs_reverify 待處理

        # 排程層可以同時把多個就緒 task 丟進 gather，
        # 但 MODEL_SEMAPHORE(1) 保證同一時間只有一個真的在呼叫本地模型。
        results = await asyncio.gather(*(_run_one_task(t) for t in ready))

        touched_modules = set()
        for task, result in zip(ready, results):
            scheduler.mark_task_done(task, result.success)
            touched_modules.add(task["module"])
            (completed if result.success else failed).append(task["id"])

            # regression 偵測：這次寫入的檔案若落在別的已驗證 module 名下，
            # 把該 module 打回 needs_reverify，強制重驗，而不是等到全量測試才發現。
            # 只傳 target_files[0]（實際寫入目標），不是整份 target_files——
            # 其餘元素是唯讀 context，task 並沒有真的寫入那些檔案，理由同上方 module_owned_files 註解。
            if result.success:
                for regressed in scheduler.check_upstream_regression([task["target_files"][0]], skip_module=task["module"]):
                    scheduler.flag_for_reverify(regressed)

        # 一般完工觸發的局部驗證
        for module in touched_modules:
            if scheduler.module_ready_for_verification(module):
                report = _partial_verify(module, db, verifier)
                report["regression"] = False
                partial_reports.append({"module": module, "report": report})
                scheduler.mark_module_verified(module, passed=report["status"] == "pass")

        # regression 觸發的重驗：只重驗被波及的那個 module，不是「當前 + 全部上游」
        for module, status in list(scheduler.module_status.items()):
            if status == "needs_reverify":
                report = _partial_verify(module, db, verifier)
                report["regression"] = True
                partial_reports.append({"module": module, "report": report})
                scheduler.mark_module_verified(module, passed=report["status"] == "pass")

    # while 迴圈跳出的三種可能，對 run_tests/debug 的意義完全不同：
    # - all_done()：全部做完（不論成敗）
    # - blocked：從未被排到（上游從沒驗證過，屬於「程式碼不存在」）
    # - failed：曾經驗證過但沒過（不論是原生失敗還是 regression 造成的失敗，屬於「程式碼寫錯」）
    blocked_modules = [m for m, s in scheduler.module_status.items() if s == "pending"]
    failed_modules = [m for m, s in scheduler.module_status.items() if s == "failed"]

    # ⚠️ 回傳時兩種語意不能混：completed_tasks/failed_tasks/partial_reports 掛了
    # operator.add reducer，只能回傳這次新增的 delta（函式最上面就是從空陣列收集的）；
    # blocked_modules/failed_modules 沒掛 reducer，必須回傳這次結束當下的完整快照
    # （上面兩行本來就是對 module_status 全量重算的結果，直接回傳即可）。
    # `**state` 放最前面、後面五個 key 放後面——dict literal 後面的 key 會覆蓋前面的。
    return {
        **state,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "partial_reports": partial_reports,
        "blocked_modules": blocked_modules,
        "failed_modules": failed_modules,
    }
```

> **`blocked_modules` 與 `failed_modules` 的下游路由差異**：`should_debug_or_done`（見 02b）須分開判斷，不能只看 `test_results` 整體成敗——`retry_count` **只在 `failed_modules` 非空時才扣減**（代表 module 確實跑過、驗證過但沒通過，屬於「程式碼寫錯」）。兩者都非空時通常是因果關係（`failed_modules` 是 root cause，`blocked_modules` 是被牽連的下游），`debug_node` 只需針對 `failed_modules`（尤其 `regression: true` 的項目）分析，`blocked_modules` 待對應的 `failed_modules` 修好、重驗通過後排程器會自然釋放，不額外消耗 `retry_count`。

**對應 00 三章的兩條規則**：
- `_module_deps_satisfied` 保證下游 module 不會在上游通過局部驗證前被排進就緒佇列，避免疊在錯誤程式碼上的產物
- `MODEL_SEMAPHORE = asyncio.Semaphore(1)` 是「排程可平行、執行序列化」的具體實作：`get_ready_tasks()` 可一次回傳多個就緒 task，但實際呼叫仍被 Semaphore 收斂成一個個跑

---

## 七、Stub-First 開發策略

在接上真正的 Claude API / translator-cli / Harness 之前，先用固定回傳值的 stub 節點把整張圖的**邊、conditional edge、reducer** 跑通，理由：graph 的路由邏輯（該平行的有沒有平行、retry 迴圈會不會無限迴圈）和每個 Agent 的實作邏輯是兩個獨立會出錯的地方，混在一起 debug 很難分辨錯誤來源。

### Stub 範例

```python
# nodes/design_node.py（stub 版本，design 現在是平行分支 node——與 record_tests
# 共用同一個前驅，只回傳自己實際更動的 key，不能再展開 state，見五章）
async def run(state: RefactorState) -> dict:
    return {
        "python_structure": {
            "directory_tree": "app/\n  repositories/\n  services/\n  routers/",
            "interfaces": [
                {
                    "file_path": "app/repositories/user_repository.py",
                    "class_name": "UserRepository",
                    "function_name": "get_by_id",
                    "params": [{"name": "user_id", "type": "int"}],
                    "return_type": "User | None",
                }
            ],
        },
        "route_to_file_mapping": {"GET_api_v1_users_{id}": ["app/routers/user_router.py"]},
    }
```

```python
# refactor_harness/langgraph_nodes/test_nodes.py 的 record_golden_output（stub 版本，
# 平行分支 node，只回傳自己的 key，不展開 state）
def record_golden_output(state: RefactorState) -> dict:
    return {"golden_output": {"readonly": {}, "mutation": {}}}
```

```python
# nodes/plan_node.py（stub 版本，平行分支 node，只回傳自己的 key，不展開 state）
async def run(state: RefactorState) -> dict:
    return {"task_list": [{"id": "t1", "module": "user", "description": "stub task",
                            "target_files": ["app/repositories/user_repository.py"],
                            "context": "", "depends_on": []}]}
```

```python
# nodes/scaffold_node.py（stub 版本，平行分支 node，只回傳自己的 key，不展開 state）
async def run(state: RefactorState) -> dict:
    return {"scaffold_done": True}
```

其餘**線性** node（`parse`、`extract_spec`、`implement`……，前驅只有一個、後繼也只有一個的 node）比照七章開頭「線性 node」的既有寫法，回傳 `{**state, ...}`；`record_tests`／`design`（見五章新增的 `parse → (record_tests ∥ design)` 分支）與 `plan`／`scaffold` 這兩組**平行分支** node，一律比照上面四個範例，只回傳自己實際更動的 key（原因見五、平行分支段落——`design` 雖然邏輯上仍是「一個前驅、直接產出下一階段的權威規格」，但因為跟 `record_tests` 是同一個前驅的平行分支，一樣不能展開 state）。

### 替換順序

1. 先用 stub 跑通 `extract_spec → gen_collection → parse`，確認純線性段落沒問題。
2. 換上 `record_tests`／`design` 的 stub 後，驗證兩者確實同時觸發、`plan`／`scaffold` 確實等兩者都完成才進入下一階段（可在 stub 裡印 timestamp 觀察）；再驗證 `plan`／`scaffold` 自己那組平行分支、`implement` 等兩者都完成才跑一次。
3. 用假的 `test_results`（先 fail 後 pass）驗證 retry 迴圈：`debug → implement → run_tests` 是否正確迴圈、`retry_count` 超過上限是否正確走到 `give_up`。
4. 圖的路由確認無誤後，才逐一把 stub 換成真正呼叫 Claude API / translator-cli / Harness 的實作，一次換一個 node，換完立刻單獨測試該 node。

---

## 八、跨平台（含 Windows）注意事項

00 的環境架構是「你的電腦」與「另一台 Mac」兩機器分工，Orchestrator（你的電腦）若是 Windows，需注意：

| 項目 | 問題 | 建議做法 |
|---|---|---|
| venv 啟動指令 | `source .venv/bin/activate` 在 Windows 不通 | Windows 用 `.venv\Scripts\activate`（cmd）或 `.venv\Scripts\Activate.ps1`（PowerShell） |
| 環境變數 | `.env` 讀取方式不同 shell 語法不同（`export` vs `set`） | 一律用 `python-dotenv` 在程式內讀取 `.env`，不依賴 shell 語法，跨平台一致 |
| 路徑分隔符 | 硬編 `"fixtures/golden/" + module` 在 Windows 可能產生 `\` `/` 混用 | State 與 node 程式碼一律用 `pathlib.Path`，不手動字串拼路徑 |
| `psql` 呼叫 | Windows 需另外安裝 PostgreSQL client 並確認在 PATH 中 | `DbEnvironment`（見 02a/02b）用 `subprocess.run([...], shell=False)`，避免依賴 shell 差異；或改用 `psycopg2`/`asyncpg` 直接連線執行 SQL，完全避開 `psql` 執行檔問題（注意：這只是換一種方式連同一顆 PostgreSQL，資料庫仍是 00 三、已定案的 PostgreSQL，並非變更技術選型） |
| npm 全域安裝路徑 | `openapi-to-postmanv2`、`newman` 全域安裝後，Windows 的可執行檔路徑與 Unix 不同 | 呼叫時一律用 `npx <工具名>` 而非假設全域指令已在 PATH，`npx` 在兩平台行為一致 |
| Java 啟動指令 | `java -jar app.jar` 本身跨平台，但背景執行/關閉的方式不同（`&` vs Windows 無等價語法） | 一律用 Python 的 `subprocess.Popen` 管理 Java/Python 服務的啟動與終止，不透過 shell 的背景執行語法 |
| 換行符（CRLF/LF） | git snapshot + AST 插入（translator-cli）對 CRLF 敏感，Windows checkout 預設可能轉換換行符 | 專案根目錄加 `.gitattributes` 統一鎖定 `* text=auto eol=lf`，翻譯後的 Python 檔案一律用 LF |
| asyncio + subprocess | `implement_node`／`DbEnvironment` 用 asyncio 呼叫 subprocess（`npx`、`psql` 等）時，Windows 在事件迴圈關閉階段偶爾會拋出無害但擾人的 `RuntimeError: Event loop is closed` | Python 3.8+ 在 Windows 上預設已是 `ProactorEventLoop`（支援 subprocess），通常不需要手動設定；若遇到此類訊息干擾（或懷疑被其他套件改了 policy），可在 `main.py` 入口顯式加上 `asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())` 保險 |

> translator-cli 內部的 AST 處理、git snapshot 細節屬於 `07a_translator_cli_architecture.md` 的範圍，這裡只列 LangGraph Orchestrator 層級會直接踩到的坑。

---

## 九、執行與觀察

```python
# main.py
import asyncio
import os
from dotenv import load_dotenv
from graph.builder import build_graph

load_dotenv()

async def main():
    graph = build_graph()
    initial_state = {
        "java_project_path": os.environ["JAVA_PROJECT_PATH"],  # 進入點輸入，① 解析 Agent 讀取用，見 00 五章「環境建立」
        "test_dsn": os.environ["TEST_DB_DSN"],           # implement node 的 DbEnvironment 用
        "python_base_url": os.environ["PYTHON_BASE_URL"],# implement node 的 GoldenVerifier 用
        "retry_count": 0,
        "completed_tasks": [],
        "failed_tasks": [],
        "partial_reports": [],
        "blocked_modules": [],
        "failed_modules": [],
    }
    final_state = await graph.ainvoke(initial_state)
    print(final_state["test_results"])

if __name__ == "__main__":
    asyncio.run(main())
```

- 執行：`python main.py`
- 圖形檢視：LangGraph 內建 `graph.get_graph().draw_mermaid()` 可輸出 Mermaid 語法，貼到任何 Mermaid renderer 檢查平行分支與 retry 迴圈接線是否符合預期——建議在 stub 階段（見七）就先跑一次，比跑完整流程更快發現接錯線的問題。