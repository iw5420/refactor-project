# LangGraph 實作細節：Java → Python 重構 Orchestrator

> 本文件承接 `00_refactor_architecture.md` 的整體架構，是 LangGraph 實作面的細節文件，範疇見一。
> Harness（Agent ②/⑥）的實作見 `02a_harness_architecture.md` + `02b_harness_code.md`；translator-cli（Agent ④/⑤ 呼叫本地模型的橋接工具）的實作見 `03a_translator_cli_architecture.md` + `03b_translator_cli_code.md`。本文件的 node 只示範「怎麼呼叫」，不重複這兩份文件已定義的內部邏輯。

---

## 一、本文件的定位

00 的九、State 設計與「LangGraph 圖的節點與邊」只描述了應該有哪些節點、邊怎麼接；本文件補三個 00 沒展開的實作問題：

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
│       ├── spec_node.py          # [A] Spec Agent（程式邏輯）
│       ├── collection_node.py    # [B] Collection Agent（程式邏輯 + LLM）
│       ├── design_node.py        # ③ 架構設計 Agent（Claude API）
│       ├── plan_node.py          # [P] Plan Agent（Claude API）
│       ├── scaffold_node.py      # ④ 骨架實作 Agent（translator-cli，骨架生成模式，非填空模式，見四備註）
│       ├── implement_node.py     # ⑤ 功能改寫 Agent（translator-cli + scheduler）
│       ├── debug_node.py         # ⑦ Debug Agent（Claude API）
│       └── give_up_node.py       # 超過重試次數的收尾（通知人工）
│
├── refactor_harness/            # 見 02a / 02b（Agent ②/⑥ 與共用核心）
├── translator_cli/              # 見 03a / 03b（Agent ④/⑤ 呼叫本地模型）
│
├── config/
│   ├── harness.yaml
│   └── mask_rules.yaml
├── postman/                     # Agent B 產出的兩份 Collection
├── fixtures/
│   ├── seed.sql
│   └── golden/
│
├── requirements.txt
├── .env.example
└── .gitignore
```

**設計原則**：`graph/` 只放 LangGraph 相關的組裝邏輯與 node 定義；每個 Agent 實際呼叫的「重活」（Harness 比對、translator-cli 呼叫）都委派給 `refactor_harness/` 和 `translator_cli/` 這兩個獨立套件，node 函式本身盡量薄，方便 stub-first 開發（見七）。

### requirements.txt（核心依賴，鎖定版本）

```
langgraph==1.2.6
langchain-anthropic==1.4.0
pyyaml==6.0.3
python-dotenv==1.2.2
httpx==0.28.1
```

> **鎖死版本而非用 `>=`**：orchestrator 長時間無人值守運行，`langgraph`／`langchain-anthropic` 仍在 1.x 早期，版本間可能有 breaking change（`Send` API、reducer 行為、checkpointer 介面）。用 `==` 精確鎖定，升級時主動跑 `pip install -U langgraph` 並重跑 stub-first 驗證（見七）。上面版本號僅供參考，建立專案時用 `pip index versions langgraph` 確認最新版即可。
>
> `newman`、`openapi-to-postmanv2` 是 Node.js 工具，見 00 五；建議 `package.json` 同樣鎖版本，用 `npm ci`。

### .env.example

```
ANTHROPIC_API_KEY=
JAVA_BASE_URL=http://localhost:8080
PYTHON_BASE_URL=http://localhost:8000

# 本地模型（v3.4）：另一台 Mac 上 ollama 前面掛了 nginx 做 token 驗證，
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

> `SPRING_DATASOURCE_*` 是 [A] Spec Agent／⑥ 啟動 Java 服務（`subprocess.Popen`，見八）時帶入的環境變數，Spring Boot 會自動覆蓋 `spring.datasource.*`，不需改 `application.properties`。
>
> `OLLAMA_API_KEY`（v3.4 新增）：translator-cli 呼叫 `OLLAMA_BASE_URL` 時，須在 request header 帶上 `Authorization: Bearer {OLLAMA_API_KEY}`，讓另一台 Mac 上的 nginx 驗證通過後才轉發給 ollama。這個變數只在 translator-cli 內部使用，不放進 `RefactorState`（與 `OLLAMA_BASE_URL` 同一類——性質同 `DATABASE_URL`，是外部工具自己讀的環境變數，不是 Orchestrator 決策要用的資料，見三章 State 設計）。實際 header 組裝與 request schema 見 `03a_translator_cli_architecture.md`。

### 初始化步驟

```bash
mkdir refactor-project && cd refactor-project
python3 -m venv .venv
source .venv/bin/activate          # Windows 見八
pip install -r requirements.txt
cp .env.example .env               # 填入實際值
git init
```

---

## 三、State Schema 設計

00 的九、State 表格列出了 13 個欄位，這裡用 `TypedDict` 定義實際結構。關鍵決策：

- 用 `TypedDict` 而非 Pydantic——State 是普通 dict，不需要額外的 serialize/validate 開銷
- `completed_tasks`、`failed_tasks`、`partial_reports` 是逐 task 累積寫入的（見六），掛 `Annotated[list, operator.add]` reducer
- `python_structure` 鎖死到「檔案路徑＋函式簽名」層級，而非自由格式字串，讓 [P] 和 ④ 消費同一份有結構保證的資料

```python
# graph/state.py
from typing import TypedDict, Annotated, Literal
import operator


# ── Agent ① 輸出的子型別 ──────────────────────────────
class MethodInfo(TypedDict):
    java_method: str
    python_method: str
    description: str
    complexity: Literal["low", "medium", "high"]


class ModuleInfo(TypedDict):
    module: str                    # 對應 fixtures/golden/{module}/ 子目錄
    java_files: list[str]
    python_files: list[str]
    depends_on: list[str]          # 依賴的其他 module 名稱（⑤ 排程依此做 topological sort）
    methods: list[MethodInfo]


class ApiMapping(TypedDict):
    endpoint: str
    http_method: str
    java_controller: str
    python_target: str
    module: str


# ── Agent ③ 輸出：python_structure（v3.2 鎖死到函式簽名層級）──
class ParamSpec(TypedDict):
    name: str
    type: str


class InterfaceSpec(TypedDict):
    file_path: str                 # 相對路徑，如 "app/repositories/user_repository.py"
    class_name: str | None
    function_name: str
    params: list[ParamSpec]
    return_type: str


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

    # 環境設定（main.py 從 .env 讀入，見九；implement node 需要，之前遺漏未列入 State）
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

> `partial_reports`／`blocked_modules`／`failed_modules` 不在 00 的核心 State 表格裡，是排程實作（見六）新增的內部欄位。`partial_reports` 是逐次累加的事件記錄，掛 reducer 正確；`blocked_modules`／`failed_modules` 是每次結束當下的狀態快照，不掛 reducer、由 `implement_node.run()` 整包覆蓋。兩者讓 `run_tests`／`debug` 能區分「程式碼根本沒被排到」和「程式碼確實跑過但驗證沒過」，決定要不要消耗 `retry_count`（見六）。

---

## 四、Graph 節點與 Agent 對應

| Node 名稱 | 對應 Agent | 型態 | 檔案 |
|---|---|---|---|
| `parse` | ① 解析 Agent | Claude API | `nodes/parse_node.py` |
| `extract_spec` | [A] Spec Agent | 程式邏輯 | `nodes/spec_node.py` |
| `gen_collection` | [B] Collection Agent | 程式邏輯 + LLM | `nodes/collection_node.py` |
| `record_tests` | ② 測試 Agent | 程式邏輯（Harness） | `refactor_harness/langgraph_nodes/test_nodes.py`（見 02b） |
| `design` | ③ 架構設計 Agent | Claude API | `nodes/design_node.py` |
| `plan` | [P] Plan Agent | Claude API | `nodes/plan_node.py` |
| `scaffold` | ④ 骨架實作 Agent | translator-cli（骨架生成模式） | `nodes/scaffold_node.py` |
| `implement` | ⑤ 功能改寫 Agent | translator-cli（填空模式）＋ 排程器 | `nodes/implement_node.py` |
| `run_tests` | ⑥ 測試執行 Agent | 程式邏輯（Harness） | `refactor_harness/langgraph_nodes/test_nodes.py`（見 02b） |
| `debug` | ⑦ Debug Agent | Claude API | `nodes/debug_node.py` |
| `give_up` | — | 程式邏輯 | `nodes/give_up_node.py` |

> **`scaffold` 與 `implement` 呼叫 translator-cli 的兩種不同模式，不是同一支 API**：`implement`（⑤）用「填空模式」`fill_function()`——目標檔案與空函式簽名已存在，模型只回傳單一函式本體，用 AST 插入。`scaffold`（④）從無到有建立目錄、檔案、class、空函式簽名，沒有既有結構可插入，因此呼叫另一個「骨架生成模式」介面（如 `translator_cli.generate_scaffold(python_structure)`，整檔輸出）。精確介面定義見 `03a_translator_cli_architecture.md`，這裡先釘死「不是同一個契約」，避免誤用 `fill_function()` 處理不存在的檔案。

---

## 五、Graph 建構：主流程、平行分支、Retry 迴圈

### 主流程（線性）

```python
# graph/builder.py
from langgraph.graph import StateGraph, END
from graph.state import RefactorState
from graph.nodes import (
    parse_node, spec_node, collection_node, design_node,
    plan_node, scaffold_node, implement_node, debug_node, give_up_node,
)
from refactor_harness.langgraph_nodes.test_nodes import (
    record_golden_output, run_postman_tests, should_debug_or_done,
)

def build_graph():
    builder = StateGraph(RefactorState)

    builder.add_node("parse", parse_node.run)
    builder.add_node("extract_spec", spec_node.run)
    builder.add_node("gen_collection", collection_node.run)
    builder.add_node("record_tests", record_golden_output)
    builder.add_node("design", design_node.run)
    builder.add_node("plan", plan_node.run)
    builder.add_node("scaffold", scaffold_node.run)
    builder.add_node("implement", implement_node.run)
    builder.add_node("run_tests", run_postman_tests)
    builder.add_node("debug", debug_node.run)
    builder.add_node("give_up", give_up_node.run)

    builder.set_entry_point("parse")
    builder.add_edge("parse", "extract_spec")
    builder.add_edge("extract_spec", "gen_collection")
    builder.add_edge("gen_collection", "record_tests")
    builder.add_edge("record_tests", "design")
```

### 平行分支：③ → [P] / ④ → ⑤

00 的流程圖裡 `design` 完成後 `plan` 和 `scaffold` 平行執行，兩者都完成才進 `implement`。在 LangGraph 的 `StateGraph` 裡，這個 fan-out / fan-in 不需要額外的 API：**只要兩個節點都以同一個節點為前驅、又都指向同一個後繼節點，LangGraph 執行時會在同一個 superstep 平行呼叫兩者，並等兩者都完成後才觸發後繼節點。**

```python
    # 平行分支：design 完成後，plan 與 scaffold 同時進入就緒狀態
    builder.add_edge("design", "plan")
    builder.add_edge("design", "scaffold")

    # fan-in：implement 的兩個前驅都完成後才觸發一次
    builder.add_edge("plan", "implement")
    builder.add_edge("scaffold", "implement")
```

> fan-in 不需要 reducer 的**前提**是：`plan` 和 `scaffold` 的回傳值只包含各自實際更動的 key（互不相交），**絕對不能用 `{**state, ...}` 展開整包 state**——一旦兩個分支在同一個 superstep 對同一個 key 各自寫入，就違反「無 reducer 時每個 key 只能被一個節點寫入」的前提，是未定義行為。只有 `design` 這種非平行分支的線性 node 才能用 `{**state, ...}`（見七的 stub 慣例）。`implement` 節點**內部**對 module/task 的平行處理才真的需要 reducer（見六）。

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

---

## 六、⑤ 功能改寫 Agent：Module 排程器實作

對應 00 v3.2 的排程原則：**module 間依賴關係沿用 Agent ① `module_list` 的 `depends_on`，優先讓同一 module 的 task 連續完成並通過局部驗證，才釋放依賴它的下游 module；本地模型的實際生成請求全域序列化（併發數＝1）。**

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

        # module 名下擁有哪些檔案，用來偵測「後續寫入是否波及已驗證過的上游 module」（見下方 check_upstream_regression）
        self.module_owned_files = {m["module"]: set(m["python_files"]) for m in module_list}

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
from translator_cli.types import FillResult                  # 見 03a/03b：{success, error, diff, ...}
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
            if result.success:
                for regressed in scheduler.check_upstream_regression(task["target_files"], skip_module=task["module"]):
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

**對應 00 v3.2 的兩條規則**：
- `_module_deps_satisfied` 保證下游 module 不會在上游通過局部驗證前被排進就緒佇列，避免疊在錯誤程式碼上的產物
- `MODEL_SEMAPHORE = asyncio.Semaphore(1)` 是「排程可平行、執行序列化」的具體實作：`get_ready_tasks()` 可一次回傳多個就緒 task，但實際呼叫仍被 Semaphore 收斂成一個個跑

---

## 七、Stub-First 開發策略

在接上真正的 Claude API / translator-cli / Harness 之前，先用固定回傳值的 stub 節點把整張圖的**邊、conditional edge、reducer** 跑通，理由：graph 的路由邏輯（該平行的有沒有平行、retry 迴圈會不會無限迴圈）和每個 Agent 的實作邏輯是兩個獨立會出錯的地方，混在一起 debug 很難分辨錯誤來源。

### Stub 範例

```python
# nodes/design_node.py（stub 版本，design 是線性 node，可以安心展開 state）
async def run(state: RefactorState) -> RefactorState:
    return {
        **state,
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

其餘**線性** node（`parse`、`extract_spec`、`implement`……，前驅只有一個的 node）比照 `design_node` 的寫法，回傳 `{**state, ...}`；`plan`／`scaffold` 這兩個**平行分支** node 一律比照上面兩個範例，只回傳自己實際更動的 key（原因見五、平行分支段落）。

### 替換順序

1. 先用 stub 跑通 `parse → extract_spec → gen_collection → record_tests → design`，確認線性流程沒問題。
2. 換上 `design` 的 stub 後，驗證 `plan`／`scaffold` 平行分支確實同時觸發、`implement` 確實等兩者都完成才跑一次（可在 stub 裡印 timestamp 觀察）。
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
| ollama 連線 | 兩機器間的 HTTP 連線與作業系統無關，但防火牆預設規則不同；v3.4 後中間多一層 nginx 做 token 驗證，開放的 port 是 nginx 的 port，不是 ollama 原生的 `11434` | Windows 需確認防火牆對內部網段的對應 port（nginx 的對外 port）開放 inbound；另確認 `.env` 的 `OLLAMA_API_KEY` 與另一台 Mac 上 nginx 設定的 token 一致，否則會收到 401 而非連線逾時，兩者的除錯方向不同 |
| asyncio + subprocess | `implement_node`／`DbEnvironment` 用 asyncio 呼叫 subprocess（`npx`、`psql` 等）時，Windows 在事件迴圈關閉階段偶爾會拋出無害但擾人的 `RuntimeError: Event loop is closed` | Python 3.8+ 在 Windows 上預設已是 `ProactorEventLoop`（支援 subprocess），通常不需要手動設定；若遇到此類訊息干擾（或懷疑被其他套件改了 policy），可在 `main.py` 入口顯式加上 `asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())` 保險 |

> translator-cli 內部的 AST 處理、git snapshot 細節屬於 `03a_translator_cli_architecture.md` 的範圍，這裡只列 LangGraph Orchestrator 層級會直接踩到的坑。

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
        "java_project_path": "./java-project",   # 進入點輸入，① 解析 Agent 讀取用
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

---

*架構與流程的整體定位見 `00_refactor_architecture.md`；Harness 與 translator-cli 的內部邏輯分別見 02 系列與 03 系列文件。*