# LangGraph 實作細節：Java → Python 重構 Orchestrator

---

## 一、本文件的定位

00 的一、整體流程概覽（流程圖＋各階段對應文件表）只勾勒整體流程與 Agent 職責邊界，State 完整定義與 Graph 節點/邊建構整段都在本文件（00 九章目前只留一段指向本文件的摘要，不重複列表）。本文件補四個更深一層的實作問題：

1. State 的實際型別（尤其 `python_structure` 需精確到檔案路徑＋函式簽名）
2. ③ 完成後的平行分支、⑤ 的 module 排程在 LangGraph 裡具體怎麼寫
3. 專案怎麼從零建立、圖怎麼組裝、跨平台會踩到什麼坑
4. `run_id`／一般執行 log（`11a_logging_architecture.md`）在 `main.py` 與 graph 執行流程裡具體怎麼串接（見九）——11a 本身聚焦在記錄機制的設計，這裡只收斂「跟 graph 執行流程接口在哪裡」這一小塊

> **文件範圍**：涉及單一 Agent 內部演算法的深入細節（如 ⑤ 的 Python 服務容器管理、⑦ 的診斷邏輯）不在此重複，指向 09a/09b、10a/10b 等對應文件，理由見 00 十一章文件索引「各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件」；本文件只收斂 State 型別、graph 節點/邊、跨節點共用的排程與執行機制這幾塊。

---

## 二、專案目錄結構

> 每個會呼叫 Claude API 或做非平凡運算的 Agent 都各自獨立成一個頂層套件（`parse_agent/`／`design_agent/`／`plan_agent/`／`scaffold_agent/`／`debug_agent/`，加上既有的 `refactor_harness/`／`spec_collection_agent/`／`translator_cli/`），`graph/nodes/*.py` 對這些 Agent 而言只是薄封裝（`await asyncio.to_thread(run_xxx_agent, ...)`，理由見七），真正的演算法、prompt、型別定義都在各自套件裡（對應文件見 00 十一章文件索引）。`implement_node.py` 是例外——⑤ 的排程／Python 服務容器生命週期／Harness 局部驗證邏輯直接寫在這個檔案裡，不是薄封裝，見六。

```
refactor-project/
├── main.py                     # 進入點：組裝並執行 graph，串接 run_id／一般 log／astream 卡住偵測／收尾報告，見九
├── graph/
│   ├── __init__.py
│   ├── state.py                 # RefactorState 與所有子型別定義（見三）
│   ├── builder.py                # StateGraph 組裝（node 註冊、edge 連接，見五）
│   ├── scheduler.py              # ⑤ 的 module 依賴圖排程器（見六）
│   ├── stream_watchdog.py        # main.py 用 astream() 觀察進度、偵測卡住的輔助工具（見九）
│   ├── java_source_extraction.py # 把 [P] 算好的呼叫鏈座標解析成真正的 Java／已翻譯 Python 原始碼文字（見 00 六章、09a/09b）
│   └── nodes/
│       ├── __init__.py
│       ├── spec_node.py          # [A] Spec Agent，薄封裝，邏輯在 spec_collection_agent/（見 03a/03b）
│       ├── collection_node.py    # [B] Collection Agent，薄封裝，邏輯在 spec_collection_agent/（見 03a/03c）
│       ├── await_manual_fill_node.py  # 人工填值關卡的終止節點（見五）
│       ├── parse_node.py         # ① 解析 Agent，薄封裝，邏輯在 parse_agent/（見 04a/04b）
│       ├── design_node.py        # ③ 架構設計 Agent，薄封裝，邏輯在 design_agent/（見 05a/05b）
│       ├── plan_node.py          # [P] Plan Agent，薄封裝，邏輯在 plan_agent/（見 06a/06b）
│       ├── scaffold_node.py      # ④ 骨架實作 Agent，薄封裝，邏輯在 scaffold_agent/ ＋ translator_cli 骨架生成模式（見 08a/08b）
│       ├── implement_node.py     # ⑤ 功能改寫 Agent：排程器＋Python 服務容器生命週期＋Harness 局部驗證，本檔案本身即主要邏輯所在（見六、09a/09b）
│       ├── debug_node.py         # ⑦ Debug Agent，薄封裝，邏輯在 debug_agent/（見 10a/10b）
│       └── give_up_node.py       # 超過重試次數／scaffold 失敗／⑦ 提早判定不可修的收尾（通知人工）
│
├── common/                      # 跨 Agent 共用工具（見 00 六章「共用工具」系列）
│   ├── llm_client.py             # call_claude_for_json()：Claude API 呼叫唯一入口
│   ├── llm_trace.py              # llm_traces.db 讀寫，Claude／Ollama 兩條呼叫路徑共用（見 11a 七章）
│   ├── run_context.py            # new_run_id()／adhoc_run_id()（見 11a 六章、九章）
│   ├── trace_context.py          # current_trace_id：單次 LLM 呼叫的 trace_id contextvar（見 11a 六章）
│   ├── logging_setup.py          # configure_logging()：一般執行 log 初始化（見 11a 五章、九章）
│   ├── run_report.py             # pipeline 結束時寫 logs/reports/{日期}/{run_id}.json／.md（見九）
│   ├── concurrency.py            # default_concurrency()
│   ├── chunking.py               # chunk_by_char_budget()
│   ├── openapi_ref_resolver.py   # resolve_refs()
│   ├── java_type_mapping.py      # map_java_type()／camel_to_snake()
│   ├── java_annotations.py       # DATA_CLASS_ANNOTATIONS／JPA_ENTITY_ANNOTATIONS
│   └── jpa_base_repository.py    # BaseRepository[T]、JPA 衍生查詢方法名對應（見 04a/05a/07a，docs/refactor_bug_trace.md #10/#16）
│
├── llmlog/                      # `python -m llmlog` CLI：llm_traces.db 查詢層，與 ⑦ Debug Agent 共用同一組函式（見 11a 十一章、11b）
│
├── spec_collection_agent/       # 見 03a / 03c（[A]/[B] Agent 核心邏輯）
├── parse_agent/                 # 見 04a / 04b（① 解析 Agent 核心邏輯）
├── design_agent/                 # 見 05a / 05b（③ 架構設計 Agent 核心邏輯）
├── plan_agent/                   # 見 06a / 06b（[P] Plan Agent 核心邏輯）
├── translator_cli/              # 見 07a / 07b（Agent ④/⑤ 呼叫本地模型 qwen 或 Claude API／骨架生成模式；`client.py` 依 `translator_backend` 分派到 `ollama_client.py` 或 `claude_client.py`）
├── scaffold_agent/               # 見 08a / 08b（④ db_models 組裝核心邏輯，呼叫 translator_cli 骨架生成模式）
├── debug_agent/                  # 見 10a / 10b（⑦ Debug Agent 核心邏輯）
├── refactor_harness/            # 見 02a / 02b（Agent ②/⑥ 與共用核心）
├── python_service/               # Python 目標服務的 Docker 容器生命週期管理，⑤／⑥ 共用（見 09a 三、八章）
│   ├── manager.py                 # 模組層級單例：ensure_started()／stop()／get_diagnostics()
│   ├── process.py                 # PythonServiceContainer：docker run 啟停、就緒輪詢、診斷擷取
│   └── reload_probe.py            # ensure_reload_probe_infra()：熱重載同步屏障的一次性前置準備
│
├── config/
│   ├── harness.yaml
│   └── mask_rules.yaml
├── specs/
│   └── openapi.json             # [A] 落地檔案，見 03a 二
├── postman/                     # [B] 產出，見 03a 三
│   ├── collection_readonly.json
│   ├── collection_mutation.json
│   ├── unfilled_endpoints.json
│   └── manual_fill/              # 人工填值模板／已填值檔案，見 03a 三章「人工填值機制」
├── fixtures/
│   ├── seed.sql
│   └── golden/
├── logs/                        # 見 11a_logging_architecture.md
│   ├── orchestrator.log          # 一般執行 log，rotating file handler（見 11a 五章）
│   ├── llm_traces.db             # Claude／Ollama 呼叫紀錄統一儲存（見 11a 七章）
│   ├── payloads/                  # 超過大小門檻的 prompt／response 落檔（見 11a 七章「門檻判斷」）
│   ├── report_{run_id}.json      # Harness 驗證報告，每輪 debug 迴圈覆寫，只留最後一輪（見 02b）
│   └── reports/{YYYY-MM-DD}/{run_id}.json／.md   # 整條 run 結束時的總結報告（見九）
│
├── tests/
├── requirements.txt
├── .env
└── .gitignore
```

**設計原則**：`graph/` 只放 LangGraph 相關的組裝邏輯與 node 定義；每個 Agent 實際呼叫的「重活」都委派給各自獨立的頂層套件，node 函式本身盡量薄——這個原則在 stub-first 階段（見七）之後持續保留，不是只在開發初期適用。

### requirements.txt（核心依賴，鎖定版本）

```
langgraph==1.2.6
pyyaml==6.0.3
python-dotenv==1.2.2
httpx==0.28.1
requests==2.32.3
anthropic==0.117.0
psycopg2-binary>=2.9.9
deepdiff>=8.0.0
pytest>=8.0.0
javalang>=0.13.0
ruff>=0.8.0

# 測試用依賴，驗證 python_service/（09a/09b 熱重載同步屏障）對一個真正
# 在跑的 ASGI 服務有效——不是 Orchestrator 執行期依賴。
fastapi>=0.115
uvicorn[standard]>=0.32
starlette>=0.40
```

> **鎖死版本而非用 `>=`**：orchestrator 長時間無人值守運行，`langgraph` 仍在 1.x 早期，版本間可能有 breaking change（`Send` API、reducer 行為、checkpointer 介面）。用 `==` 精確鎖定，升級時主動跑 `pip install -U langgraph` 並重跑 stub-first 驗證（見七，已完成，但升級 `langgraph` 時仍建議照這套方法重新走一次）。上面版本號僅供參考，建立專案時用 `pip index versions langgraph` 確認最新版即可。
>
> **不含 `langchain-anthropic`**：Claude API 呼叫統一走 `anthropic` SDK 直接呼叫（見 03b 一章「相依套件」、00 六章「Claude API 呼叫封裝」），全 repo 沒有任何地方 import `langchain`，不需要這個依賴——但 `anthropic` 本身要裝，這是所有會呼叫 Claude API 的 Agent（①③⑦、[P]、[B]）共用 `common/llm_client.py` 唯一依賴的 SDK。
>
> `psycopg2-binary`／`deepdiff`／`javalang`／`ruff`（Harness／① 解析 Agent／translator-cli 語法驗證的依賴）已落地，不再是「隨各自 Agent 落地才加進來」的未來式；`requests` 是 `python_service/process.py` 輪詢容器就緒狀態用的同步 HTTP client（`httpx` 已被非同步節點佔用，這裡刻意用同步套件避免在同步輪詢迴圈裡另外包一層事件迴圈）；`fastapi`／`uvicorn`／`starlette` 只在測試 `python_service/reload_probe.py` 時作為被測 ASGI 服務使用，不是 Orchestrator 執行期依賴。實際完整清單以 `requirements.txt` 為準。
>
> `newman`、`openapi-to-postmanv2` 是 Node.js 工具，見 00 五；建議 `package.json` 同樣鎖版本，用 `npm ci`。

### .env

```
ANTHROPIC_API_KEY=
JAVA_PROJECT_PATH=../lang-exam-api-refactor
JAVA_BASE_URL=http://localhost:8080
JAVA_JAR_PATH=../lang-exam-api-refactor/target/app.jar
JAVA_EXECUTABLE_PATH=java
PYTHON_BASE_URL=http://localhost:8000
# translator-cli 寫入目標的 Python 專案根目錄，與 refactor-project/、
# java_project_path 同層、各自獨立的 git repo（見 07a 二章「新輸入：
# python_project_path」）。09a 之後同時也是 python_service/ 容器
# bind mount 進去的目錄（容器內固定路徑 /srv）。
PYTHON_PROJECT_PATH=../exam-platform-api

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

# 一般執行 log（見 11a_logging_architecture.md 五章、九章）
ORCHESTRATOR_LOG_PATH=logs/orchestrator.log
ORCHESTRATOR_LOG_LEVEL=INFO

# LLM Trace 統一儲存（見 11a_logging_architecture.md 七章）
LLM_TRACE_DB_PATH=logs/llm_traces.db
LLM_TRACE_PAYLOAD_DIR=logs/payloads
LLM_TRACE_PAYLOAD_THRESHOLD_BYTES=100000

# 各 Agent 呼叫 Claude API 用的模型，未設定時退回
# common.llm_client.DEFAULT_MODEL_FALLBACK（見 00 六章「Claude API 呼叫封裝」）
SPEC_COLLECTION_AGENT_MODEL=
PARSE_AGENT_MODEL=
DESIGN_AGENT_MODEL=
PLAN_AGENT_MODEL=
DEBUG_AGENT_MODEL=

# Python 服務容器（Docker，見 09a 三章、python_service/process.py）與
# astream() 卡住偵測（見九）的逾時／輪詢參數，均有程式內建預設值，
# 這裡列出是為了不用翻程式碼就知道可以調整
STUCK_REPORT_SECONDS=600
SERVICE_READY_TIMEOUT_SECONDS=120
SERVICE_READY_POLL_INTERVAL_SECONDS=2
```

> `JAVA_JAR_PATH` 用相對於 `refactor-project/` 的相對路徑（見 03a 二「路徑格式」），不是絕對路徑，避免 `.env` 換機器/換使用者就失效。`PYTHON_PROJECT_PATH` 同一慣例。
>
> `JAVA_EXECUTABLE_PATH`（見 03a 二「Java 執行檔路徑」）：預設值 `java`，交給系統 PATH 解析；PATH 上有多個 JDK 版本、且預設解析到的版本與 Java 專案要求版本不符時，改填該 JDK 的 `java`／`java.exe` 完整路徑，避免啟動時因位元組碼版本不符而失敗。這個值機器規格相關、不強求可攜，性質同 `OLLAMA_BASE_URL` 的 IP。
>
> `SPRING_DATASOURCE_*` 是 [A] Spec Agent／② 測試 Agent 啟動 Java 服務（`subprocess.Popen`，見八）時帶入的環境變數，Spring Boot 會自動覆蓋 `spring.datasource.*`，不需改 `application.properties`（⑥ 測試執行 Agent 操作的是 Python 服務，用不到這組 JDBC 格式的變數）。
>
> `OLLAMA_API_KEY`：translator-cli 呼叫 `OLLAMA_BASE_URL` 時，須在 request header 帶上 `Authorization: Bearer {OLLAMA_API_KEY}`，讓另一台 Mac 上的 nginx 驗證通過後才轉發給 ollama。這個變數只在 translator-cli 內部使用，不放進 `RefactorState`（與 `OLLAMA_BASE_URL` 同一類——性質同 `DATABASE_URL`，是外部工具自己讀的環境變數，不是 Orchestrator 決策要用的資料，見三章 State 設計）。實際 header 組裝與 request schema 見 `07a_translator_cli_architecture.md`。
>
> **需要額外安裝 Docker**：`python_service/process.py` 用 `docker run` 啟動目標 Python 服務容器（`python_project_path` bind mount 進容器，見六、09a 三章）——`uvicorn --reload` 在 Windows 上經常無法真正完成重啟，容器化（Linux）能穩定繞開這個問題，這是 09a 落地時新增的環境依賴，Orchestrator 所在機器需另外安裝 Docker Desktop（或等價的 Docker Engine）。

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
- `completed_tasks`、`failed_tasks`、`task_failures`、`partial_reports`、`debug_rounds` 是逐 task／逐輪累積寫入的（見六、10a），掛 `Annotated[list, operator.add]` reducer
- `python_structure` 鎖死到「檔案路徑＋函式簽名」層級，而非自由格式字串，讓 [P] 和 ④ 消費同一份有結構保證的資料

> 以下是 `graph/state.py` 目前的完整內容（唯一權威來源）。④／⑤／⑦ 落地時陸續新增了 `skipped_interfaces`／`skipped_db_models`、`task_failures`、`service_diagnostics`／`debug_rounds`／`pending_fixed_bodies`／`pending_file_fixes`／`give_up_early`／`unanalyzed_root_cause_modules`，以及跨節點都需要的 `run_id`／`python_project_path`——各欄位的用途與來由見下方程式碼裡的註解。

```python
# graph/state.py
from typing import TypedDict, Annotated, Literal, NotRequired
import operator


# ── Agent ① 輸出的子型別 ──────────────────────────────
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
    # 僅 routers 層 API 邊界方法非 None，值沿用 ApiMapping.http_method／
    # .endpoint（見 05a 五章、九章）；NotRequired 讓既有所有建構 InterfaceSpec
    # 的地方不需要跟著補這兩個欄位。
    http_method: NotRequired[str | None]
    route_path: NotRequired[str | None]
    # Phase 1（entity/dto/repository/utils）／Phase 2（controller/service）
    # 分階段翻譯設計新增，見 refactor_plan.md 一、二章、05a 三章「層級
    # 判定」、九章「phase 欄位」。NotRequired：理由同 http_method／
    # route_path，既有建構 InterfaceSpec 的地方（測試 fixture 等）不需要
    # 跟著補這個欄位；design_agent 產出的每一筆 InterfaceSpec 都會設定。
    phase: NotRequired[Literal[1, 2]]
    # 這個 interface 對應的 Java 方法識別碼，格式逐字沿用①既有的
    # parse_agent/types.py::method_id()（"{java_file_path}::{class_name}::
    # {method_name}"，不含參數型別，跟①的呼叫圖同一種 key 格式，可以
    # 直接查表）。供 [P] 六章「呼叫鏈範圍查找」用，見 06a_plan_agent_
    # architecture.md 六章、05a_design_agent_architecture.md 對應章節。
    # NotRequired 理由同 phase。
    java_method_id: NotRequired[str]
    # 這個 InterfaceSpec 所屬的 class 若繼承了 Spring Data 基底介面
    # （JpaRepository/CrudRepository），這裡存它的 entity 型別簡單名稱，
    # 供 translator_cli/scaffold.py 決定要不要把這個 class 的宣告改成
    # 繼承 BaseRepository[Entity]。見 common/jpa_base_repository.py、
    # docs/refactor_bug_trace.md #10／#16。NotRequired 理由同 phase。
    jpa_base_entity: NotRequired[str | None]


class JavaIndexEntry(TypedDict):
    file_path: str
    class_name: str | None
    function_name: str
    phase: Literal[1, 2]


class PythonStructure(TypedDict):
    directory_tree: str             # 目錄結構的文字表示，供 ④ 建立骨架時參照
    interfaces: list[InterfaceSpec] # [P] target_files 與 ④ 骨架簽名的唯一權威來源
    # java_method_id → Python 對應資訊，[P] 六章「呼叫鏈範圍查找」查表用
    # ——把③內部本來就算過、跑完即丟的 Java↔Python 對應關係正式落地保留，
    # 不需要 [P]／⑤ 各自重新實作一次③的 camelCase／多載消歧邏輯。見
    # 05a_design_agent_architecture.md 對應章節。NotRequired 理由同
    # config_field_mappings：既有建構 PythonStructure 字面值的地方（測試）
    # 不需要跟著補，design_agent 產出的每一筆都會設定。
    java_index: NotRequired[dict[str, JavaIndexEntry]]
    # ③ design_agent.global_infra.render_config_py() 機械組出，對應
    # docs/09b_bug_trace.md #45（Java @Value("${key}") 屬性注入欄位 →
    # app/core/config.py 環境變數常數）。key 是 Java 檔案的 file_path，
    # value 是 {java_field_name: "app.core.config.CONSTANT_NAME"}，供 [P]
    # （plan_agent）組 task.context 時折入一段提示文字，讓 ⑤ 翻譯到這個
    # 欄位時有明確依據可用，不用瞎猜（見 06a 對應章節）。NotRequired：
    # 沒有任何 @Value 欄位的專案完全不需要這個 key，既有建構 PythonStructure
    # 字面值的地方（如測試）不需要跟著補，比照 InterfaceSpec.http_method
    # 的既有慣例。
    config_field_mappings: NotRequired[dict[str, dict[str, str]]]
    # ③ design_agent.global_infra.render_config_py() 同一次呼叫機械組出，
    # 對應 docs/09b_bug_trace.md #46（@Value 屬性注入機制只設計了「怎麼
    # 命名／怎麼讀」，沒有設計「值從哪裡來」）。每筆 {"property_key":
    # "language.code", "constant_name": "LANGUAGE_CODE"}——property_key
    # 是 Java application-{profile}.properties 裡的原始 key，供
    # python_service（⑤，見 09a 對應章節）啟動容器前讀取 Java 端實際
    # 屬性值、解析出 {constant_name: value} 當額外 -e 環境變數注入。跟
    # config_field_mappings 是同一份 value_fields 算出來的兩種不同用途
    # 的投影，不是重複資料——那個是給 [P] 折進 task.context 的「哪個
    # Python 檔案該引用哪個常數」，這個是給容器啟動時「這個常數該填什麼
    # 值」。NotRequired 理由同 config_field_mappings。
    config_env_vars: NotRequired[list[dict[str, str]]]


# ── Agent ⑤ 輸出：task 失敗根因（見 09a 六章）──
class TaskFailure(TypedDict):
    task_id: str
    module: str
    file_path: str
    class_name: str | None
    function_name: str
    reason: Literal["scaffold_skipped", "fill_failed", "file_fix_failed"]
    error: str


# 06a 七章新設計（referenced_interfaces 函式層級抽取，見
# docs/09b_bug_trace.md #37 根因）：target_files 裡「因為引用才被拉進來」
# 的檔案，只需要送這個函式實際引用到的那幾支方法，不是整份檔案。
class ReferencedFunctionRef(TypedDict):
    file_path: str
    class_name: str | None
    function_name: str


# ── Agent ⑦ 輸出：task 級修正後程式碼與每輪除錯記錄（見 10a 六章、
# 八章「⑦ 直接產生修正後程式碼」、十二章「phase 2：檔案層級修正」）──
class FileFix(TypedDict):
    task_id: str
    target_file: str
    old_snippet: str
    new_snippet: str


class TaskFix(TypedDict):
    task_id: str
    diagnosis: str
    # 對應 docs/refactor_bug_trace.md #9：⑦ 沒有這個 task 對應的真實
    # Java 原始碼可以核對（見 debug_agent/analysis.py 的 source_files
    # 只讀 python_project_path），自己憑症狀猜寫 fixed_body 的可靠度
    # 天生比不上⑤（真的看得到 java_source／referenced_source）——這個
    # task 的函式本體邏輯有問題（不論是從沒成功翻譯過，還是翻過但邏輯
    # 錯）時，優先設 True、交還給⑤用真實 Java 原始碼重新翻譯，diagnosis
    # 會被當成這次重新翻譯的提示一併附上（見 graph/nodes/implement_node.py
    # 的 pending_retranslate_tasks）。NotRequired：既有建構 TaskFix 的
    # 測試 fixture 不用跟著補，預設視為 False（沿用既有 fixed_body 行為）。
    retranslate: NotRequired[bool]
    # None：這個 task 的函式本體不需要改（只有 file_fixes 需要套用，或
    # 已由 retranslate=True 處理）。只在 retranslate 是 False（或缺省）
    # 時才可能非 None——兩者互斥，見 debug_agent/prompts.py 說明。
    fixed_body: str | None
    file_fixes: list[FileFix]


class DebugRound(TypedDict):
    round: int
    module: str
    origin: Literal["blocked", "module_mismatch", "scaffold_gap", "root_cause"]
    fixable: bool
    root_cause_summary: str
    task_fixes: list[TaskFix]
    unfixable_reasons: list[str]


# 06a 六章「呼叫鏈範圍查找」：[P] 用①的呼叫圖 + java_index 機械算出的
# 參照座標，只有座標與語言標記，不含原始碼文字——把座標實際讀成文字是
# ⑤ 的工作（見 refactor_plan.md 一章「新增能力與歸屬」）。
class ReferenceTarget(TypedDict):
    # 三個欄位的命名空間都跟著 language 走，不是固定的 Python 或 Java 側
    # ——language="java" 時三者是①呼叫圖 method_id 拆出來的 Java 原始座標
    # （原始檔案路徑／Java class 名稱／Java 方法名稱，camelCase，可能跟
    # Python 側的檔名／snake_case 名稱不同）；language="python" 時三者是
    # java_index 投影過的 Python 側事實（已翻譯 .py 檔案路徑／Python class
    # 名稱／Python function_name）。見 plan_agent/call_chain.py::
    # build_reference_targets() 依 language 分別組裝的邏輯。
    file_path: str
    class_name: str | None
    function_name: str
    language: Literal["java", "python"]


# ── [P] Plan Agent 輸出：task list ────────────────────
class TaskSpec(TypedDict):
    id: str
    module: str
    # Phase 1（entity/dto/repository/utils）／Phase 2（controller/service）
    # 分階段翻譯設計新增，見 refactor_plan.md 一、二章、06a_plan_agent_
    # architecture.md 五章。[P] 直接複製對應 InterfaceSpec.phase 的值，
    # 不重新判斷。NotRequired 理由同 InterfaceSpec.phase：既有建構
    # TaskSpec 字面值的地方（測試 fixture、01 文件 stub 範例）不需要
    # 跟著補這個欄位；plan_agent 產出的每一筆都會設定。
    phase: NotRequired[Literal[1, 2]]
    # 雙後端分工設計新增，見 refactor_plan.md 三章、06a 五章：依這個
    # task 所屬層級機械決定（repository → qwen，其餘 → claude），不需要
    # LLM 判斷。NotRequired 理由同上。
    translator_backend: NotRequired[Literal["qwen", "claude"]]
    # 這個 task 自己對應的 Java 方法識別碼（格式沿用①的 method_id()，逐字
    # 等於 InterfaceSpec.java_method_id）——⑤抽取「這個函式自己」的 Java
    # 原始碼文字時的座標，跟 reference_targets（呼叫到的其他函式的座標）
    # 是同一種資訊、但描述的對象不同，兩者合起來才是⑤這次呼叫的完整輸入
    # （見 07a_translator_cli_architecture.md、refactor_plan.md 一章「新增
    # 能力與歸屬」）。NotRequired 理由同 phase／translator_backend。
    java_method_id: NotRequired[str]
    description: str
    target_files: list[str]        # 必須是 python_structure.interfaces 中已存在的 file_path
    # 06a 六章「呼叫鏈範圍查找」新增，見上方 ReferenceTarget。NotRequired
    # 理由同 phase／translator_backend。
    reference_targets: NotRequired[list[ReferenceTarget]]
    # 六章「上限被觸發時：截斷可見化」——只在 reference_targets 因為觸及
    # PLAN_AGENT_MAX_REFERENCE_TARGETS 上限而被截斷時才設為 True，未截斷
    # 的 task 不設這個 key（缺席即代表「沒有這回事」，不需要額外的 False
    # 分支）。供人工事後檢視／未來 09a／⑦ Debug Agent 查「這個函式翻譯
    # 品質可疑是不是因為呼叫鏈被砍過」，不用大海撈針翻 log。
    reference_targets_truncated: NotRequired[bool]
    context: str
    depends_on: list[str]
    # 對回 target_files[0] 這個 InterfaceSpec 的 (class_name, function_name)，
    # 供 translator-cli 的 fill_function() 在同一個檔案有多個函式時精準定位
    # 要填的是哪一個（見 07a_translator_cli_architecture.md 五章「為什麼
    # fill_function 需要 class_name／function_name」）。NotRequired 讓既有
    # 建構 TaskSpec 字面值的地方（如 01 文件 stub 範例）不需要跟著補這兩個
    # 欄位，比照 InterfaceSpec.http_method／route_path 的既有慣例。
    class_name: NotRequired[str | None]
    function_name: NotRequired[str]
    # 舊版：target_files 裡，因 referenced_interfaces 而被拉進來的檔案，
    # 各自精確引用到哪些 (class_name, function_name)，translator-cli
    # 讀取這些檔案時只抽取這裡列出的函式，不整份帶入（見
    # docs/09b_bug_trace.md #37）。**Phase 1/2 分階段翻譯設計後，[P] 不
    # 再產生 referenced_interfaces，因此也不再填這個欄位**——同樣的功能
    # 現在由⑤自己的呼叫圖查找取代（一開始就只抽取被呼叫到的那個函式，
    # 見 refactor_plan.md 一章、06a_plan_agent_architecture.md 七章）。
    # 欄位定義暫時保留（不是移除）：`translator_cli/client.py`／
    # `graph/nodes/implement_node.py` 仍會讀取這個欄位（`task.get(
    # "referenced_functions", [])`，缺席時安全退化成空清單）——[P] 已經
    # 不再產生這份資料，這個欄位現在永遠是空清單，但貿然刪除型別定義
    # 會影響這兩個既有模組目前仍在讀取它的程式碼，見 06a 八章說明。
    referenced_functions: NotRequired[list[ReferencedFunctionRef]]
    # 對應 docs/refactor_bug_trace.md #46：逐字複製 InterfaceSpec.
    # return_type（見 05a 五章「ResponseEntity<T> 覆寫」、
    # design_agent/type_mapping.py::resolve_api_boundary_signature()）——
    # ③在這個函式原始 Java 簽名是 ResponseEntity<...>（依情境動態回傳不同
    # status／body，如 file_router.py 的 voice／image GET）時，機械地把
    # 這裡覆寫成字面字串 "Response"，不經過 LLM。⑤填空階段需要這個信號
    # 才能精準判斷「這個函式的錯誤分支也該用 JSONResponse 包成專案統一的
    # code／msg／data 格式，不能只回傳裸文字」（見
    # graph/nodes/implement_node.py::_RAW_RESPONSE_ERROR_JSON_NOTICE），
    # 不能只看檔案路徑這種跟原因無關的替代訊號。NotRequired 理由同
    # phase／java_method_id：既有建構 TaskSpec 的地方（測試 fixture 等）
    # 不需要跟著補這個欄位；plan_agent 產出的每一筆都會設定。
    return_type: NotRequired[str]


# ── 整體 State ─────────────────────────────────────────
class RefactorState(TypedDict):
    # 進入點輸入（main.py 組裝 initial_state 時填入，見九）
    java_project_path: str
    # 這次 pipeline 執行的唯一識別碼，main.py 用 common/run_context.py::
    # new_run_id() 產生一次，貫穿一般 log 與 llm_traces.db 兩邊（見
    # 11a_logging_architecture.md 六章）。implement_node.py 透過
    # translator_cli.fill_function(run_id=...) 往下傳。
    run_id: str
    # translator-cli 寫入目標的 Python 專案根目錄，對應 .env 的
    # PYTHON_PROJECT_PATH（見 07a_translator_cli_architecture.md 二章
    # 「新輸入：python_project_path」）——與 refactor-project/、
    # java_project_path 同層、各自獨立的 git repo，scaffold_node.py／
    # implement_node.py 呼叫 translator_cli.generate_scaffold()／
    # fill_function() 時顯式傳入，translator_cli 本身不 import
    # graph.state（見 07a 十二章）。
    python_project_path: str

    # 環境設定（main.py 從 .env 讀入，見九；implement node 需要，之前遺漏未列入 State）
    test_dsn: str          # 對應 .env 的 TEST_DB_DSN，Orchestrator 直接連 DB 用
    python_base_url: str   # 對應 .env 的 PYTHON_BASE_URL，Orchestrator 打 API 用
    # 註：.env 的 DATABASE_URL 不放進這個 State——那是 Python 服務進程自己讀的環境變數，
    # Orchestrator 不會用到，只需確保啟動 Python 服務的 subprocess 有繼承 .env。

    # Agent ①
    module_list: list[ModuleInfo]
    api_to_python_target: list[ApiMapping]
    # 使用者填 skip，是人工判斷「這個 endpoint 整段不進翻譯流程」，不只是
    # 跳過自動化測試（見 docs/03a_spec_collection_agent_architecture.md
    # 「Decision.SKIP 的語意」）。同名不同 HTTP method 的多載（如
    # FileController 的 voice/image）在 method_id 層級會共用同一個 id，
    # 排除不了單一個多載，這裡改用 HTTP method 精確的
    # (class_name, method_name, http_method) 三元組清單，供
    # design_agent/design.py 在重新掃描出每個多載各自的 http_method 之後
    # 把該排除的多載從資料裡濾掉（見 parse_agent/skip_filter.py::
    # compute_skip_excluded_overloads()）。
    skip_excluded_overloads: list[tuple[str, str, str]]

    # Agent A / B
    openapi_spec: dict
    collection_readonly_path: str
    collection_mutation_path: str
    # [B] 填值失敗、且尚未被人工解決（補值或明確 skip）的 endpoint 清單，
    # 供 gen_collection 後的條件邊判斷要不要暫停等人工處理（見 01 五「人工
    # 補值關卡」、03c 二 2.6 章 `manual_fill.py`）。空清單＝全部解決。
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
    # Agent ④：generate_scaffold() 回傳的 skipped_interfaces／
    # skipped_db_models（見 07a 四章、08a 十二章），單次寫入的快照，不
    # 逐次累加，不需要 reducer——scaffold 不在 retry_count 迴圈內，只會
    # 執行一次。skipped_db_models 固定是 {file_path, class_name, error}
    # 三欄位 schema（class_name 為 None 代表 07a 檔案級失敗，非 None 代表
    # 08a entity 級失敗，見 08a 十二章），下游（09a／⑦ Debug Agent）不需要
    # 分辨兩種來源各自的欄位形狀。
    skipped_interfaces: list[dict]
    skipped_db_models: list[dict]

    # Agent ⑤（逐 task 累積寫入，需要 reducer）
    completed_tasks: Annotated[list[str], operator.add]
    failed_tasks: Annotated[list[str], operator.add]
    partial_reports: Annotated[list[dict], operator.add]   # 每個 module 局部驗證結果（含 regression 重驗），逐次累加供 ⑦ 回溯
    # task 失敗時的錯誤訊息與根因分類（"scaffold_skipped" vs "fill_failed"），
    # 供 ⑦ Debug Agent（10a，待建立）不需要重新比對 skipped_interfaces 就能
    # 分辨兩種失敗。歷史累積，不因後續重試成功而移除——一筆 task 若第一次
    # 失敗、重試後成功，這筆記錄仍保留（同時 task_id 也會出現在
    # completed_tasks），供除錯追溯。見 09a 六章。
    task_failures: Annotated[list[TaskFailure], operator.add]

    # Agent ⑤：每次呼叫重新計算的「當下完整快照」，不是累加事件，故不掛 reducer
    blocked_modules: list[str]    # 因上游 module 未驗證通過而從未進入就緒佇列的 module
    failed_modules: list[str]     # 確實執行過、驗證過、但沒通過的 module（含 regression 造成的失敗）
    # 對應 docs/refactor_bug_trace.md #13：blocked_modules／failed_modules
    # 只涵蓋 "pending"／"failed" 兩種狀態，一個 module 卡在 "in_progress"
    # 永遠到不了終態時兩者都不會列到它，會被 common/run_report.py 誤判成
    #「沒出現在壞掉清單裡＝修好了」。這裡明確列出真正 "verified" 的
    # module，讓「確認修好」的判斷可以用正面訊號（真的驗證通過），不必
    # 從兩份不完整的負面清單去推論。
    verified_modules: list[str]
    # 純附加診斷資訊，不是排程判斷依據：blocked_modules 裡每個 module
    # 對回它 depends_on 裡狀態還不是 "verified" 的直接上游 module 名稱
    # 清單，供人工／未來 ⑦ Debug Agent 不需要反查 module_list.depends_on
    # 就能直接讀出「這個 module 被誰卡住」。見 docs/09b_bug_trace.md #29。
    blocked_reasons: dict[str, list[str]]

    # Agent ⑥
    test_results: dict

    # Agent ⑥：⑥ 判定 Python 服務不可達時（見
    # refactor_harness/langgraph_nodes/test_nodes.py::run_postman_tests()
    # 前置健康檢查），連同 test_results 一起寫入這個欄位——容器崩潰
    # 當下的 docker log（見 python_service/manager.py::get_diagnostics()）。
    # 不掛 reducer，每輪覆寫，跟 test_results 本身的既有覆寫語意一致。
    # ⑦（debug_agent/）全程只讀這個欄位，不呼叫任何跨節點函式，見 10a
    # 八章「診斷資料改走 State」。
    service_diagnostics: str | None

    # Agent ⑦（逐輪累積寫入，需要 reducer）：每一輪 debug 對每個分析過
    # module 的完整記錄，含未呼叫 LLM 的 blocked／scaffold_gap 機械判斷，
    # 供人工事後查看、也是未來評估「要不要讓 LLM 看到上一輪建議」的既有
    # 資料來源，見 10a 十二章。
    debug_rounds: Annotated[list[DebugRound], operator.add]

    # Agent ⑦：這一輪產出的 task 級修正後程式碼，key 是 task_id、value
    # 是完整函式本體。不掛 reducer，整包覆寫——這是「這一輪的修正」，不是
    # 累加事件，見 10a 六章。implement_node._run_one_task() 讀取這份資料，
    # 有值的 task 直接把這份程式碼傳給 translator_cli.fill_function() 的
    # fixed_body 參數，完全跳過 ⑤ 本地模型（見 10a 八章「⑦ 直接產生
    # 修正後程式碼」）。
    pending_fixed_bodies: dict[str, str]

    # Agent ⑦（對應 docs/refactor_bug_trace.md #9）：這一輪判定「函式
    # 本體邏輯需要重新翻譯」的 task，key 是 task_id、value 是 ⑦ 的
    # diagnosis（作為重新翻譯的提示）。不掛 reducer，整包覆寫，跟
    # pending_fixed_bodies 同一種「這一輪的修正」語意。implement_node.py
    # 讀取這份資料時**不**把它當成 fixed_body 使用——這個 task 走跟⑤
    # 首輪翻譯完全相同的路徑（解析真實 java_source／referenced_source、
    # 呼叫 fill_function() 的真實模型呼叫），只是把 diagnosis 疊加進
    # context，讓模型知道上一輪錯在哪。跟 pending_fixed_bodies 互斥：
    # 同一個 task_id 不會同時出現在兩份清單裡（見 debug_agent/analysis.py
    # 的建構邏輯）。
    pending_retranslate_tasks: dict[str, str]

    # Agent ⑦：這一輪產出的檔案層級修正（見 10a 八章「phase 2」）——
    # fixed_body／fill_function() 的 AST 函式定位機制只能碰函式本體，
    # import 敘述這類模組層級的修正走這裡，由
    # translator_cli.apply_file_fix() 用精確字串替換套用，不經過排程器
    # （這些修正不對應任何要重新生成的函式本體）。不掛 reducer，整包
    # 覆寫，語意同 pending_fixed_bodies。
    pending_file_fixes: list[FileFix]

    # Agent ⑦：這一輪分析後，若確認這一輪所有 root_cause module 都無法
    # 修，設為 True，供 debug_node.should_retry_or_give_up() 路由到
    # give_up，不進 implement 浪費一輪重試（見 10a 七章）。每輪覆寫，
    # 不掛 reducer。
    give_up_early: bool

    # Agent ⑦：這一輪 origin=="root_cause" 的 module 裡，Claude API 呼叫
    # （含批次重試）仍然失敗、被視為「未分析」的 module 名稱（見 10a
    # 4.4、七章）。只有這一輪的快照，不掛 reducer——give_up_node.py 在
    # retry_count 用盡那個分支讀取，若非空就提示人工這一輪可能是 Claude
    # API 本身的問題，不是 ⑦ 判斷錯誤，見 give_up_node.py。
    unanalyzed_root_cause_modules: list[str]

    # Orchestrator
    retry_count: int
```

> `partial_reports`／`blocked_modules`／`failed_modules`／`blocked_reasons` 是排程實作（見六）新增的內部欄位，00 的流程圖層級不會細到列出這幾個欄位（00 九章已不重複列 State 欄位，見一）。`partial_reports` 是逐次累加的事件記錄，掛 reducer 正確；`blocked_modules`／`failed_modules`／`blocked_reasons` 是每次結束當下的狀態快照，不掛 reducer、由 `implement_node.run()` 整包覆蓋。前兩者讓 `run_tests`／`debug` 能區分「程式碼根本沒被排到」和「程式碼確實跑過但驗證沒過」，決定要不要消耗 `retry_count`（見六）；`blocked_reasons` 純粹是診斷用途，不影響任何排程或路由邏輯，已用真實環境驗證：`registration`／`grading` 兩個模組被 `exam` 卡住時，`blocked_reasons` 正確回報 `{"registration": ["exam"], "grading": ["exam"]}`（見 `docs/09b_bug_trace.md`）。
>
> **`run_id` 是 11a 落地時補上的欄位，不是 State 最初設計就有**：main.py 的 `initial_state` 必須同步補上這個 key，否則任何一個節點讀取 `state["run_id"]` 會直接 `KeyError`（見九、`11a_logging_architecture.md` 十三章相容性表格）。`task_failures`／`service_diagnostics`／`debug_rounds`／`pending_fixed_bodies`／`pending_file_fixes`／`give_up_early`／`unanalyzed_root_cause_modules` 是 09a／10a 落地時新增，同樣需要在 `initial_state` 裡有對應的初始值（空清單／空字典／`False`／`None`），詳見九章實際的 `initial_state` 內容。

---

## 四、Graph 節點與 Agent 對應

| Node 名稱 | 對應 Agent | 型態 | 檔案 |
|---|---|---|---|
| `extract_spec` | [A] Spec Agent | 程式邏輯，薄封裝 | `graph/nodes/spec_node.py` → `spec_collection_agent.run_spec_agent()`（見 03a/03b） |
| `gen_manual_fill_templates` | [B] Collection Agent（階段一） | 程式邏輯，不呼叫 LLM，薄封裝 | `graph/nodes/collection_node.py`（`run_generate_templates`）→ `spec_collection_agent.generate_manual_fill_templates()`（見 03a 三章「人工填值機制」、03c） |
| `gen_collection` | [B] Collection Agent（階段二） | 程式邏輯 + LLM，薄封裝 | `graph/nodes/collection_node.py`（`run`）→ `spec_collection_agent.run_collection_agent()`（見 03a/03c） |
| `await_manual_fill` | — | 程式邏輯 | `graph/nodes/await_manual_fill_node.py`（見五「人工填值關卡」，`gen_manual_fill_templates`／`gen_collection` 共用同一個終止節點） |
| `parse` | ① 解析 Agent | Claude API，薄封裝 | `graph/nodes/parse_node.py` → `parse_agent.run_parse_agent()`（見 04a：排在 `gen_collection` 之後，因為 skip 呼叫鏈排除需要讀 [B] 已定案的 `unfilled_endpoints.json`） |
| `record_tests` | ② 測試 Agent | 程式邏輯（Harness） | `refactor_harness/langgraph_nodes/test_nodes.py`（見 02b；與 `design` 平行執行，見五章「平行分支」） |
| `design` | ③ 架構設計 Agent | Claude API，薄封裝 | `graph/nodes/design_node.py` → `design_agent.run_design_agent()`（不依賴 `golden_output`，與 `record_tests` 平行執行，見 05a 十一章、五章「平行分支」） |
| `plan` | [P] Plan Agent | 純機械邏輯（不呼叫 LLM），薄封裝 | `graph/nodes/plan_node.py` → `plan_agent.run_plan_agent()`（見 06a：`phase`／`translator_backend` 打標籤＋呼叫鏈範圍查找，不呼叫 Claude API） |
| `scaffold` | ④ 骨架實作 Agent | 程式邏輯（scaffold_agent）＋ translator-cli（骨架生成模式） | `graph/nodes/scaffold_node.py` → `scaffold_agent.build_db_models()` ＋ `translator_cli.client` |
| `implement` | ⑤ 功能改寫 Agent | translator-cli（填空模式）＋ 排程器＋ Harness 局部驗證，非薄封裝 | `graph/nodes/implement_node.py`（本檔案即主要邏輯所在，見六） |
| `run_tests` | ⑥ 測試執行 Agent | 程式邏輯（Harness） | `refactor_harness/langgraph_nodes/test_nodes.py`（見 02b） |
| `debug` | ⑦ Debug Agent | Claude API，薄封裝 | `graph/nodes/debug_node.py` → `debug_agent.run_debug_analysis()`（同步函式，用 `asyncio.to_thread()` 包一層，見 10a 十章） |
| `give_up` | — | 程式邏輯 | `graph/nodes/give_up_node.py` |

> **`scaffold` 與 `implement` 呼叫 translator-cli 的兩種不同模式，不是同一支 API**：`implement`（⑤）用「填空模式」`fill_function()`——目標檔案與空函式簽名已存在，模型只回傳單一函式本體，用 AST 插入。`scaffold`（④）從無到有建立目錄、檔案、class、空函式簽名，沒有既有結構可插入，因此呼叫另一個「骨架生成模式」介面（`translator_cli.generate_scaffold()`，整檔輸出）。精確介面定義見 `07a_translator_cli_architecture.md`，這裡先釘死「不是同一個契約」，避免誤用 `fill_function()` 處理不存在的檔案。
>
> **薄封裝的共同模式**：`parse`／`design`／`plan`／`debug`（以及 `extract_spec`／`gen_manual_fill_templates`／`gen_collection`）都是 `async def run(state)` 直接 `await asyncio.to_thread(run_xxx_agent, ...)`——各自對應的 Agent 套件內部是同步阻塞呼叫（javalang 掃描、多次 Claude API 呼叫、Map 階段重試佇列的等待），包一層 `asyncio.to_thread()` 避免卡住事件迴圈，讓同一個 superstep 裡平行的其他 async 節點（例如 `design` 與 `record_tests`）不會被拖累退化成排隊。這個模式從 ① 落地時就確立，後續每個新 Agent 套件接上時都照抄，不是各自獨立決定。

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

> fan-in 不需要 reducer 的**前提**是：`record_tests`／`design`／`plan`／`scaffold` 的回傳值只包含各自實際更動的 key（互不相交），**絕對不能用 `{**state, ...}` 展開整包 state**——一旦多個分支在同一個 superstep 對同一個 key 各自寫入，就違反「無 reducer 時每個 key 只能被一個節點寫入」的前提，是未定義行為。`design` 雖然只有單一前驅（`parse`），但因為它同時也是 `record_tests` 的平行分支，仍需遵守「只回傳自己實際更動的 key」——不套用七章對純線性 node 的 `{**state, ...}` stub 慣例（見七章）。`implement` 節點**內部**對 module/task 的平行處理才真的需要 reducer（見六）。

### Retry 迴圈（Conditional Edge）

```python
    builder.add_conditional_edges(
        "implement",
        implement_node.should_run_tests_or_give_up,
        {
            "run_tests": "run_tests",
            "give_up": "give_up",
        },
    )

    builder.add_conditional_edges(
        "run_tests",
        should_debug_or_done,
        {
            "done": END,
            "debug": "debug",
            "give_up": "give_up",
        },
    )

    # debug 迴圈：debug → implement 不再是無條件邊——10a 落地後，⑦ 可能
    # 判斷「這一輪所有 root_cause module 都不可修」（give_up_early，見三
    # 章 State 定義），這種情況直接路由到 give_up，不進 implement 浪費
    # 一輪重試。retry_count 上限檢查仍在 should_debug_or_done() 做（見
    # 02b），這裡只需要判斷 give_up_early，不重複檢查 retry_count（見
    # 10a 七章「為什麼要統一套用」，避免兩處各自用不同比較符號產生
    # off-by-one）。
    builder.add_conditional_edges(
        "debug",
        debug_node.should_retry_or_give_up,
        {
            "implement": "implement",
            "give_up": "give_up",
        },
    )
    builder.add_edge("give_up", END)

    return builder.compile()
```

`should_debug_or_done`（定義在 `02b_harness_code.md` 的 `test_nodes.py`）依 `test_results.status` 與 `retry_count` 三分流：全過結束、失敗但未超重試上限進 `debug`、超過上限進 `give_up`。`debug_node.should_retry_or_give_up`（`graph/nodes/debug_node.py`）只判斷 `state["give_up_early"]`——`run_debug_analysis()`（10a）分析完這一輪後若確認所有 `root_cause` module 都無法修，就把這個欄位設為 `True`。`give_up` 不是 END 本身，而是一個獨立節點——保留這個節點是為了讓「通知人工」這個動作有明確落點（Slack/email，見 02a 十六待實作清單），而不是讓圖靜默結束。

`implement` 之後的邊也是 conditional edge，不是單純的 `add_edge`——原因見下一小節「scaffold 失敗時的收尾路徑」。

### scaffold 失敗時的收尾路徑（Conditional Edge）

`scaffold`（④）呼叫 `generate_scaffold()` 若失敗（`result["success"] = False`，例如 git working tree 不乾淨、非 git repo，見 07a 十三章），這個 pipeline run 底下不會有任何一個 `target_file` 真的存在。若放任 `implement` 之後無條件接 `run_tests`，`run_tests`（⑥）打的是一個從未被正確產出程式碼的 Python 服務——02a 五章「Newman 共用執行器」明訂服務沒起來時 newman 執行器會直接拋出例外，不是回傳一筆失敗的比對結果，這個例外會讓整條 pipeline 執行崩潰（見九章 `astream()`／`pump_graph_stream()`：node 內未接住的例外會被包成 `("error", exc)` 丟進 `main.py` 的 queue，`main()` 收到後直接 `raise`），根本走不到 `should_debug_or_done()`。

**不能動的邊界**：`scaffold → implement` 是上一節描述的 fan-in 合流點（`plan`／`scaffold` 都是 `implement` 的前驅），改成 conditional edge 會破壞「兩者都完成才觸發一次」的既有語意，因此這裡不碰。改在 `implement → run_tests` 之間插入判斷——`implement` 只有單一前驅，沒有 fan-in 風險：

```python
# graph/nodes/implement_node.py
def should_run_tests_or_give_up(state: RefactorState) -> str:
    return "give_up" if state.get("scaffold_done") is False else "run_tests"
```

`implement.run()` 本身也在最前面短路：`scaffold_done is False` 時直接跳過整個排程器（`ModuleScheduler`／`translator_cli.fill_function()`），把 `module_list` 全部標成 `failed_modules`（不是 `blocked_modules`——`blocked_modules` 不消耗 `retry_count`，見六章 `should_debug_or_done` 下方說明，這裡誤用會讓 `debug ↔ implement` 永遠原地打轉），`completed_tasks`／`failed_tasks`／`partial_reports` 回傳空陣列。

路由直接跳 `give_up`、不進 `debug` 重試迴圈：07a 十三章明訂 `generate_scaffold()` 的失敗（working tree 不乾淨等）「需要人工介入核對，不是可以自動化解的暫時性錯誤」，`debug → implement` 的重試對這類失敗無能為力，進 `debug` 只會浪費重試次數在注定不會改變結果的迴圈上。`give_up_node.py` 依 `scaffold_done` 是否為 `False` 印不同的診斷訊息，避免跟「`retry_count` 用盡」這個既有路徑的訊息混淆。

對應實作：`graph/nodes/implement_node.py`、`graph/builder.py`、`graph/nodes/give_up_node.py`；測試：`tests/graph/test_implement_node.py`（短路邏輯）、`tests/graph/test_manual_fill_gate.py::TestGraphBuild`（`implement`/`give_up` 的邊結構）。

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

目前（尚未接上 checkpointer）的務實做法：**直接呼叫 `run_collection_agent()`**（`specs_dir`/`postman_dir` 帶跟階段一同一組路徑，不透過整個 graph、也不需要重跑 `generate_manual_fill_templates()`——`specs/openapi.json` 跟 `postman/manual_fill/` 都已經是階段一落地的檔案，`run_collection_agent()` 直接讀就好）。**要注意這個「續跑」的實際成本**：每次呼叫 `run_collection_agent()` 都會重新完整跑一次鏈式依賴偵測（MAP/REDUCE，真實 Claude API 呼叫），不是只處理「這次新填的值」——這在實測中是真實成本的主要來源之一（見 `docs/03_spent_cost_estimate.md`），如果卡在第二道防禦性關卡、需要多次補值才能填完，每補一次都要重新付一次鏈式依賴偵測的成本，不是免費的。真正讓 `python main.py` 能從 `gen_manual_fill_templates`／`gen_collection` 斷點續跑（而不必重跑 `parse`／`design`，也不必每次重付鏈式依賴偵測的成本），需要接上 LangGraph 的 checkpointer（如 `MemorySaver` 或持久化版本）＋固定 `thread_id`，留待專案需要支援長時間、可中斷續跑的執行模式時再一併處理，見九章「已知限制」。

---

## 六、⑤ 功能改寫 Agent：Module 排程器實作

對應 00 三章的排程原則：**module 間依賴關係沿用 Agent ① `module_list` 的 `depends_on`，優先讓同一 module 的 task 連續完成並通過局部驗證，才釋放依賴它的下游 module；本地模型的實際生成請求全域序列化（併發數＝1）。**

### 設計決策：為什麼不用 LangGraph 的 `Send` API 做 task 級平行節點

LangGraph 的 `Send` API 可把一個 node 動態展開成多個平行子節點，但這裡刻意不用：qwen（僅 repository 層）的併發數鎖死為 1，`Send` 的「多個子節點同時跑」在這條路徑上仍會被同一個資源瓶頸收斂成序列，只增加圖的複雜度（reducer、子節點失敗回報）換不到平行效益；Claude API 那條路徑則本來就不需要序列化。因此 `implement` 維持**單一 node**，內部用輕量排程器決定「哪些 task 就緒」，`asyncio.gather()` 一次送出全部就緒 task——實際的併發限制不在 `implement_node.py` 這一層，而是依 backend 各自下沉到 `translator_cli` 內部（見下方）。

### 排程器

`graph/scheduler.py` 在 ⑤ 落地後多了兩處擴充，都是為了修 `debug → implement` 重入時的真實 bug（`docs/09b_bug_trace.md #41`），不是最初設計就有：

- `already_failed_modules` 建構參數：module 一旦底下所有 task 都已完成、且不在 `already_verified_modules` 裡，必須明確標成 `"failed"`，不能放著讓它預設落回 `"pending"`——否則會被 `implement_node.py` 誤判成 `blocked_modules`（等上游修好會自然釋放的語意），但它其實是真的驗證沒過，`debug_node.py` 只在 `failed_modules` 非空時才遞增 `retry_count`，這個誤判會讓 `retry_count` 停止遞增，形成不會終止的 `debug ↔ implement` 迴圈（真實重跑量到：`partial_reports` 每繞一圈疊加一筆，最終 `MemoryError`）
- `force_reschedule(module, task_ids)` 方法：由 ⑦ Debug Agent 產生的 `pending_fixed_bodies` 指向一個 `"verified"`／`"failed"` module 底下的 task 時呼叫（見 10a 八章）——把該 module 打回 `"pending"`，並把 `task_ids` 從 `task_done`／`task_failed` 移除，讓 `get_ready_tasks()` 重新排到它們。只重開 `task_ids` 指名的 task，同一 module 底下其他已完成的 task 維持完成狀態

```python
# graph/scheduler.py
from graph.state import ModuleInfo, TaskSpec

# 對應 refactor_plan.md 一章「三層全域關卡」：全專案 Phase 1（entity/dto/
# repository/utils）全部完成（成功或永久失敗，見 _tier_barrier_satisfied()）
# 才釋放任何 module 的 service task；全專案 service 全部完成才釋放任何
# module 的 controller/router task。跟 translator_cli/scaffold.py 的
# _LAYER_PREFIX 是同一種各自维护的機械慣例（見該檔案註解），這裡不 import
# plan_agent，避免 graph 套件對 plan_agent 產生新的 import-time 依賴。
_LAYER_PREFIX = {"app/services/": "services", "app/routers/": "routers"}


def _global_tier(task: TaskSpec) -> int:
    """task 屬於三層全域關卡的第幾層：0=Phase 1、1=service、2=controller/
    router。`phase` 缺席（NotRequired，見 graph/state.py TaskSpec 註解）
    時視同 Phase 1（tier 0）——這是限制最少的預設值，只有 fixture／測試
    才會缺這個欄位，plan_agent 產出的每一筆都會設定。`phase == 2` 但
    `target_files[0]` 不落在 services／routers 任何一個前綴下（理論上
    不會發生，phase_for_layer() 只對 services／routers 回傳 2，見
    design_agent/layout.py）時，保守視為最外層（tier 2），不假設它一定
    安全、可以提早釋放。
    """
    if task.get("phase", 1) != 2:
        return 0
    layer = next((v for prefix, v in _LAYER_PREFIX.items() if task["target_files"][0].startswith(prefix)), None)
    return 1 if layer == "services" else 2


class ModuleScheduler:
    """
    依 module_list 的 depends_on 做 topological 排程：
    - 只有「所有依賴 module 都已通過局部驗證」的 module，其 task 才會進入就緒佇列
    - 同一 module 內，task 依 depends_on 序列化（repository → service → router）
    - 全專案三層全域關卡（見上方 _global_tier()）：Phase 1 全部完成才放行任何
      module 的 service task，service 全部完成才放行任何 module 的
      controller/router task——這是跨 module 的全域關卡，跟前兩條同 module
      內部的排序規則是互不取代的兩層機制（見 get_ready_tasks()）

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
        already_failed_modules: set[str] | None = None,
    ):
        """
        `already_*` 讓 scheduler 從前一輪 implement 的執行結果恢復狀態——debug → implement
        是回頭呼叫同一個 node，若每次從零建立 scheduler，module_status 會被重置成全部
        pending，regression 偵測（見 check_upstream_regression）就抓不到已驗證過的 module。

        `already_failed_modules`：對應 docs/09b_bug_trace.md #41。module 一旦
        底下所有 task 都已完成（成功或失敗），且不在 `already_verified_modules`
        （通過）裡，就必須明確標成 "failed"，不能放著讓它預設落回 "pending"
        ——這種 module 已經沒有剩餘的 task 可以再排進 `get_ready_tasks()`，
        重建後的 scheduler 不會再把它排進 `touched_modules`，`module_status`
        會永遠停在建構時的初始值。放著預設值 "pending" 會讓它被
        `implement_node.py` 誤判成 `blocked_modules`（等上游修好會自然釋放
        的語意），但它從來不是被上游卡住，是真的驗證沒過——`debug_node.py`
        只在 `failed_modules` 非空時才遞增 `retry_count`，這個誤判會讓
        `retry_count` 停止遞增，`should_debug_or_done()` 的 `if not
        failed_modules: return "debug"` 分支沒有上限檢查，形成不會終止的
        `debug ↔ implement` 迴圈（真實環境重跑量到：`partial_reports` 等
        `operator.add` accumulator 每繞一圈疊加一筆，最終 `MemoryError`）。
        """
        self.modules = {m["module"]: m for m in module_list}
        self.tasks_by_module: dict[str, list[TaskSpec]] = {}
        for task in task_list:
            self.tasks_by_module.setdefault(task["module"], []).append(task)

        # module 名下擁有哪些檔案，用來偵測「後續寫入是否波及已驗證過的上游 module」（見下方 check_upstream_regression）。
        # 從 task_list 的 target_files 彙整，而不是 Agent ① module_list 的檔名——
        # ① 的檔名只是猜測，③ 架構設計 Agent 可能整個改寫；task_list.target_files
        # 必須是 python_structure.interfaces 中已存在的 file_path（見 graph/state.py
        # TaskSpec 註解），才是這個時間點真正權威的檔案路徑來源。
        # 只取 target_files[0]：這是 translator-cli 實際寫入的唯一目標檔案，target_files 其餘
        # 元素只是唯讀 context（referenced_interfaces、跨 module 的 schemas/models，見 06a 七章），
        # 若整份 target_files 都算「擁有」，會把只是讀取過的其他 module 檔案誤判成這個 module 名下，
        # 造成不相干 module 的偽 regression。
        self.module_owned_files: dict[str, set[str]] = {}
        for module, tasks in self.tasks_by_module.items():
            self.module_owned_files[module] = {t["target_files"][0] for t in tasks}

        self._backfill_missing_task_deps()

        # 全域三層關卡（見上方 _global_tier()）：task_id → tier，跟 module
        # 邊界無關，一次算好供 get_ready_tasks() 逐輪查詢。
        self._task_tier: dict[str, int] = {t["id"]: _global_tier(t) for t in task_list}

        # module 狀態：pending → in_progress → verified / needs_reverify / failed
        self.module_status = {m: "pending" for m in self.modules}
        for m in (already_verified_modules or ()):
            self.module_status[m] = "verified"
        for m in (already_failed_modules or ()):
            self.module_status[m] = "failed"
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

    def force_reschedule(self, module: str, task_ids: set[str]):
        """由 ⑦ Debug Agent 產生的 pending_fixed_bodies 指向一個
        "verified"／"failed" module 底下的 task 時呼叫（見 10a 八章）：
        把該 module 打回 "pending"，並把 `task_ids` 從 `task_done`／
        `task_failed` 移除，讓 `get_ready_tasks()` 重新排到它們。

        只重開 `task_ids` 指名的 task，同一 module 底下其他已完成的
        task 維持完成狀態，不會被誤重新排程——`get_ready_tasks()` 對
        `task_id in self.task_done` 的檢查跟 `module_status` 是各自獨立
        的兩道關卡，只把 module_status 改回 "pending"（不動
        task_done／task_failed）並不足以讓已完成的 task 重新被排到，這
        跟 `flag_for_reverify()` 的 "needs_reverify"（那個狀態只觸發重新
        跑驗證，本來就不影響 get_ready_tasks()）是不同的機制。
        """
        self.module_status[module] = "pending"
        self.task_done -= task_ids
        self.task_failed -= task_ids

    def _backfill_missing_task_deps(self):
        """同 module 內沒有 depends_on、也未被引用的 task，依原始順序自動
        串成序列依賴——但**只對 `translator_backend == "qwen"` 的 task**
        這樣做。原始理由（本地模型併發數鎖死為 1，這些 task 本來就得
        排隊，強制序列化不拉長總耗時）只對 qwen 成立；Claude 沒有這個
        併發限制（見 07a 七、八章），不該被拖進同一條序列鏈。

        對應 docs/refactor_bug_trace.md：真實環境重現過這條鏈跨後端造成
        的連坐——`exam` module 的某個 repository task（qwen）因為模型
        輸出格式錯誤重試耗盡而失敗（`task_failed`，不是 `task_done`），
        同一個 module 底下沒有明確 `depends_on` 的其餘 task（含用
        Claude、彼此毫無關聯的 service／router task）舊寫法會被串在
        它後面，`_task_deps_satisfied()` 只認 `task_done`，永遠不會
        放行，讓一次孤立的 qwen 格式錯誤拖垮整個 module，甚至連帶讓
        依賴它的其他 module／全域 Phase 關卡一起卡住。`translator_backend`
        缺席（`NotRequired`，只有舊版測試 fixture 會缺）時不視為 qwen，
        不會被自動串鏈——這是限制最少的預設值，跟 `_global_tier()` 對
        缺席 `phase` 的既有處理原則一致。
        """
        for tasks in self.tasks_by_module.values():
            referenced = {dep for t in tasks for dep in t["depends_on"]}
            unordered = [
                t for t in tasks
                if not t["depends_on"] and t["id"] not in referenced
                and t.get("translator_backend") == "qwen"
            ]
            for prev, curr in zip(unordered, unordered[1:]):
                curr["depends_on"] = curr["depends_on"] + [prev["id"]]

    def _module_deps_satisfied(self, module: str) -> bool:
        """依賴的每個模組只要**開始有進展**（狀態不是初始的 `"pending"`）
        就放行——不是只接受 `"verified"`。這裡要的是「upstream 的程式碼
        結構已經存在，downstream 可以 import／呼叫」，不是「upstream 的
        測試通過」——upstream 測試過不過，交給最終的全專案測試把關，不該
        同時也決定 downstream 排不排得進去。

        若只接受 `"verified"`，會產生一種循環死結（真實案例見
        docs/refactor_bug_trace.md #12）：upstream 要變成 `"verified"`，
        得先讓它自己的 router（tier 2）通過全域 Phase 關卡
        （`_tier_barrier_satisfied()`）；但全域關卡要求全專案 tier < 2 的
        task 都到終態——downstream 這個依賴 upstream 的 tier 1 task，本身
        就是這個集合的一份子。upstream 等 downstream 到終態才能放行自己
        的 router，downstream 卻要等 upstream 完整驗證通過才能開始——兩邊
        互相等對方，永遠沒有一方先動，而且 upstream 可能根本到不了
        `"verified"`／`"failed"` 任何一個終態（它自己卡在 `"in_progress"`，
        因為它的驗證前提——自己的 router 跑完——永遠不會成立）,只接受
        終態一樣解不開這個死結。

        改成「不是 pending 就放行」之後：upstream 只要有任何一個 task 成功
        （`mark_task_done()` 就會把它從 `"pending"` 改成 `"in_progress"`），
        downstream 立刻可以開始，不需要等 upstream 走到 router、走到完整
        驗證——這條循環邊在 upstream 都還沒排到自己的 router 之前就被打斷，
        不會出現互等。
        """
        deps = self.modules[module]["depends_on"]
        return all(self.module_status.get(d) != "pending" for d in deps)

    def _task_deps_satisfied(self, task: TaskSpec) -> bool:
        return all(dep in self.task_done for dep in task["depends_on"])

    def _tier_barrier_satisfied(self, tier: int) -> bool:
        """tier 0（Phase 1）永遠可以排——沒有更前面的關卡。tier 1／2 要求
        「所有 tier 數字更小的 task」都已到達終態（成功或永久失敗，見
        get_ready_tasks() 對 task_done／task_failed 的既有判斷）——用終態
        而不是只看成功，理由跟 module_ready_for_verification() 一致：
        scaffold 缺口這類永久失敗的 task 永遠不會進 task_done，若只等
        成功，關卡會被一個注定失敗的 task 卡死，永遠放不出下一層。
        """
        if tier == 0:
            return True
        return all(
            self._task_tier[task_id] >= tier or task_id in self.task_done or task_id in self.task_failed
            for task_id in self._task_tier
        )

    def get_ready_tasks(self) -> list[TaskSpec]:
        """回傳目前可以送去 translator-cli 的 task（尚未考慮模型併發限制）。

        對應 docs/refactor_bug_trace.md #8：module 依賴檢查
        （`_module_deps_satisfied()`）只套用在 tier ≥ 1（service／router）
        的 task 上，tier 0（repository／utils）不受它管——這不是效能
        優化，是打破一個真實死結的必要條件。真實案例：`common` module
        （如 `common_service.py` 的 ResponseResult／Result，全部是純
        service 層，沒有任何 repository 內容）的 task 全部是 tier 1，
        全域關卡（`_tier_barrier_satisfied()`）要求全專案 tier 0 先完成
        才放行；但其他 module 的 repository task（tier 0）若同時在
        module 層級依賴 `common`（`module_list.depends_on`，這在這個
        專案是常見寫法——業務 module 依賴 common 取得 ResponseResult／
        Result 這些共用型別），舊寫法（module 依賴檢查套用在這個 module
        的所有 task 上，不分 tier）會讓這些 repository task 也一併卡住，
        等 `common` 先被驗證通過——`common` 卻要等全專案 tier 0 完成才
        能開始，兩邊互相等對方，永遠不會有任何一方先動。語意上這個豁免
        是對的：Java 的 repository 層本來就幾乎不會跨 module 呼叫別的
        module 的邏輯，module 依賴排程原本要保護的是「跨 module 的同層
        業務呼叫」（refactor_plan.md 一章「呼叫鏈規則」，service 呼叫
        service 那一列），不該延伸到 repository 這一層——task 級
        `depends_on`（`_task_deps_satisfied()`）不受這個豁免影響，
        跨 module 的明確依賴（若真的出現）依然照樣生效。
        """
        ready = []
        for module, status in self.module_status.items():
            if status not in ("pending", "in_progress"):
                continue
            module_deps_ok = self._module_deps_satisfied(module)
            for task in self.tasks_by_module.get(module, []):
                if task["id"] in self.task_done or task["id"] in self.task_failed:
                    continue
                tier = self._task_tier[task["id"]]
                if tier != 0 and not module_deps_ok:
                    continue
                if not self._tier_barrier_satisfied(tier):
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

### `implement` node：串接排程器、依 backend 分流的併發限制、局部驗證

`graph/nodes/implement_node.py` 是 ⑤ 唯一的節點函式，也是全部節點裡少數不是薄封裝、直接把排程邏輯寫在 node 本身的檔案（見四章備註）。09a 落地後這個檔案（超過 1100 行）遠比下面展示的骨架複雜：多了 Python 服務 Docker 容器的啟停與熱重載同步等待（見 09a 三章、python_service/）、task 失敗根因分類（`task_failures`，見三章 State 定義）、⑦ Debug Agent 直接產生的修正程式碼套用（`pending_fixed_bodies`／`pending_file_fixes`／`pending_retranslate_tasks`，見 10a 八章）、scaffold 缺口的提前排除、上游模型服務異常的提早熔斷。**完整的、與程式碼同步的版本一律以 `graph/nodes/implement_node.py` 本身與 `09a_implement_agent_architecture.md`／`09b_implement_agent_code.md` 為準**；這裡只保留 LangGraph 排程層面的核心骨架，說明「排程可平行、依 backend 各自決定要不要序列化」這條原則怎麼落地：

**併發限制不在 `implement_node.py` 這一層，而是下沉到 `translator_cli` 內部、依 backend 各自處理**：`implement_node.py` 對這一輪所有就緒 task 直接 `asyncio.gather()`，不持有任何 semaphore；qwen task 呼叫模型時，會在 `translator_cli/ollama_client.py::OLLAMA_MODEL_SEMAPHORE`（`asyncio.Semaphore(1)`）排隊序列化，Claude task 的模型呼叫不受這個限制、可以真正平行；不論哪個 backend，「讀檔→AST 替換→寫入→commit」這段收尾都會在 `translator_cli/git_ops.py::WRITE_LOCK`（`asyncio.Lock()`）底下序列化——這段快（次秒等級），序列化不影響整體吞吐量，但避免多個 task 同時寫同一個 git repo 的 race condition（見 07a 七、八章）。

```python
# graph/nodes/implement_node.py（節錄核心骨架，Harness 局部驗證／
# Python 服務容器／task_failures 等細節省略，完整版見 09a/09b）
import asyncio
from graph.state import RefactorState
from graph.scheduler import ModuleScheduler
from translator_cli import client as translator_cli
from translator_cli.types import FillResult


async def _run_one_task(task: dict) -> FillResult:
    # backend 分流（qwen 序列化／Claude 平行）與寫入段的 WRITE_LOCK 都在
    # translator_cli 內部處理，這裡不需要知道、也不持有任何 semaphore。
    return await translator_cli.fill_function(
        translator_backend=task["translator_backend"],
        target_file=task["target_files"][0],
        description=task["description"],
        context_files=task["target_files"],
    )


async def run(state: RefactorState) -> RefactorState:
    # 從 state（上一輪 partial_reports）恢復 debug 迴圈重入的進度，否則
    # module_status 會被重置成全部 pending，regression 偵測就抓不到這輪
    # 改動波及了哪個已驗證的舊 module（完整恢復邏輯，含 already_failed_
    # modules，見六章排程器說明、09a 三章）。
    # 10a 落地後 already_failed 不再只含 scaffold 缺口：曾經進過本地模型
    # 且填空失敗（task_failures 裡 reason=="fill_failed"）的 task 也要
    # 一併排除，否則每輪 debug → implement 重入時排程器會把它們當成
    # 「還沒排過」的新 task，重新丟回 Ollama——這正是 09b_bug_trace.md
    # 記錄的、⑦ 已經開始接手除錯迴圈卻還被空跑好幾輪的根因（見 09a 七章
    # 「本節後續被 10a 推翻的部分」、10a「known_fill_failures」一節）。
    # scaffold_gap_task_ids／fill_failed_task_ids 的完整計算過程（含
    # scaffold 缺口的提前排除、task_failures 分類）略去，見 09a 六、七章
    # 與 graph/nodes/implement_node.py 本身。
    scaffold_gap_task_ids = {...}                                  # 09a 六章
    fill_failed_task_ids = {
        f["task_id"] for f in state.get("task_failures", []) if f["reason"] == "fill_failed"
    }
    already_failed = scaffold_gap_task_ids | fill_failed_task_ids
    scheduler = ModuleScheduler(
        state["module_list"],
        state["task_list"],
        already_completed=set(state.get("completed_tasks", [])),
        already_failed=already_failed,
        already_verified_modules=set(),
        # 簡化：這裡應從上一輪 partial_reports 算出真正的 verified／failed
        # module 集合，不是寫死空集合——否則就是上方 already_failed_modules
        # 說明的那個 bug 本身。實際計算見 graph/nodes/implement_node.py。
        already_failed_modules=set(),
    )

    while not scheduler.all_done():
        ready = scheduler.get_ready_tasks()
        if not ready:
            break  # 沒有可執行的 task：全部做完、卡在失敗的上游 module，或還有 needs_reverify 待處理

        # 排程層可以同時把多個就緒 task 丟進 gather——qwen task 會在
        # translator_cli 內部序列化，Claude task 可以真正平行（見上方）。
        results = await asyncio.gather(*(_run_one_task(t) for t in ready))

        for task, result in zip(ready, results):
            scheduler.mark_task_done(task, result.success)
            # regression 偵測：這次寫入的檔案若落在別的已驗證 module 名下，
            # 把該 module 打回 needs_reverify，強制重驗，而不是等到全量測試才發現。
            if result.success:
                for regressed in scheduler.check_upstream_regression(
                    [task["target_files"][0]], skip_module=task["module"]
                ):
                    scheduler.flag_for_reverify(regressed)

        # 局部驗證觸發：09a 落地後這裡會先批次等待熱重載完成，再一次跑完
        # 這一輪所有待驗證／待重驗的 module，不是逐一各自等待（見 09a 三章
        # 「批次執行」）——這裡的骨架只標出「module 完工就觸發驗證」這個
        # 時機點，實際的驗證呼叫略去，見 09a/09b。

    # while 迴圈跳出的兩種可能，對 run_tests/debug 的意義完全不同：
    # - blocked：從未被排到（上游從沒驗證過，屬於「程式碼不存在」）
    # - failed：曾經驗證過但沒過（不論是原生失敗還是 regression 造成的失敗，屬於「程式碼寫錯」）
    blocked_modules = [m for m, s in scheduler.module_status.items() if s == "pending"]
    failed_modules = [m for m, s in scheduler.module_status.items() if s == "failed"]

    # ⚠️ 回傳時兩種語意不能混：completed_tasks/failed_tasks/task_failures/
    # partial_reports 掛了 operator.add reducer，只能回傳這次新增的 delta；
    # blocked_modules/failed_modules/blocked_reasons 沒掛 reducer，必須
    # 回傳這次結束當下的完整快照（實際的 delta 收集略去，見 09a/09b）。
    return {
        **state,
        "blocked_modules": blocked_modules,
        "failed_modules": failed_modules,
    }
```

> **`blocked_modules` 與 `failed_modules` 的下游路由差異**：`should_debug_or_done`（見 02b）須分開判斷，不能只看 `test_results` 整體成敗——`retry_count` **只在 `failed_modules` 非空時才扣減**（代表 module 確實跑過、驗證過但沒通過，屬於「程式碼寫錯」）。兩者都非空時通常是因果關係（`failed_modules` 是 root cause，`blocked_modules` 是被牽連的下游），`debug_node` 只需針對 `failed_modules`（尤其 `regression: true` 的項目）分析，`blocked_modules` 待對應的 `failed_modules` 修好、重驗通過後排程器會自然釋放，不額外消耗 `retry_count`。

**對應 00 三章的兩條規則**：
- `_module_deps_satisfied` 保證下游 module 不會在上游通過局部驗證前被排進就緒佇列，避免疊在錯誤程式碼上的產物
- 「排程可平行、依 backend 各自決定要不要序列化」的具體實作：`get_ready_tasks()` 可一次回傳多個就緒 task，`asyncio.gather()` 全部送出；qwen 的實際生成請求被 `translator_cli/ollama_client.py::OLLAMA_MODEL_SEMAPHORE` 收斂成一個個跑，Claude 的模型呼叫不受這個限制

> **Python 服務改跑在 Docker 容器內，不直接在 Orchestrator 所在機器上跑**：`uvicorn --reload` 在 Windows 上經常無法真正完成重啟（見八），09a 落地時改成 `python_service/process.py` 用 `docker run` 啟動容器（`python_project_path` bind mount 進容器，見二）。容器在整條 graph run 第一次進入 `implement` 時啟動，`implement`／`run_tests`／`debug → implement` 重入期間持續共用同一個容器，由 `main.py` 在整條 graph 執行結束時統一呼叫 `implement_node.stop_python_service()` 關閉（見九）——這是原始設計沒有的一層基礎設施，`refactor_harness`（⑥）健康檢查失敗時也讀取同一個容器的診斷資料（`state["service_diagnostics"]`，見三章）。

---

## 七、Stub-First 開發策略（已完成，本章為歷史記錄）

所有節點目前都是真正呼叫 Claude API／translator-cli／Harness／Docker 的實作，沒有任何節點還停在 stub 階段——三、四、六章描述的都是接上真實實作後的狀態。本章保留原始的方法論說明，作為「當初怎麼驗證整張圖接對」的記錄，也是日後升級 `langgraph` 版本、或大幅調整圖結構時重新驗證接線的建議做法（見二章 requirements.txt 備註）。

在接上真正的 Claude API / translator-cli / Harness 之前，先用固定回傳值的 stub 節點把整張圖的**邊、conditional edge、reducer** 跑通，理由：graph 的路由邏輯（該平行的有沒有平行、retry 迴圈會不會無限迴圈）和每個 Agent 的實作邏輯是兩個獨立會出錯的地方，混在一起 debug 很難分辨錯誤來源。

### Stub 範例

```python
# nodes/design_node.py（stub 版本，design 是平行分支 node——與 record_tests
# 共用同一個前驅，只回傳自己實際更動的 key，不展開 state，見五章）
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

其餘**線性** node（`parse`、`extract_spec`、`implement`……，前驅只有一個、後繼也只有一個的 node）比照七章開頭「線性 node」的既有寫法，回傳 `{**state, ...}`；`record_tests`／`design`（見五章 `parse → (record_tests ∥ design)` 分支）與 `plan`／`scaffold` 這兩組**平行分支** node，一律比照上面四個範例，只回傳自己實際更動的 key（原因見五、平行分支段落——`design` 雖然邏輯上仍是「一個前驅、直接產出下一階段的權威規格」，但因為跟 `record_tests` 是同一個前驅的平行分支，一樣不能展開 state）。

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
| `psql` 呼叫 | Windows 需另外安裝 PostgreSQL client 並確認在 PATH 中 | `DbEnvironment.apply_seed()`（見 02a/02b、`refactor_harness/fixtures/db_env.py`）直接用 `psycopg2` 連線執行 SQL，完全避開 `psql` 執行檔問題；只有 `sync_schema()`（一次性 schema 同步，非每次驗證都跑）才會 `subprocess.run([...], shell=False)` 呼叫 `pg_dump`／`psql`，Windows 上這條路徑仍需要另外安裝 PostgreSQL client（注意：這只是連線／同步 schema 的手段選擇，資料庫仍是 00 三、已定案的 PostgreSQL，並非變更技術選型） |
| npm 全域安裝路徑 | `openapi-to-postmanv2`、`newman` 全域安裝後，Windows 的可執行檔路徑與 Unix 不同 | 呼叫時一律用 `npx <工具名>` 而非假設全域指令已在 PATH，`npx` 在兩平台行為一致 |
| Java 啟動指令 | `java -jar app.jar` 本身跨平台，但背景執行/關閉的方式不同（`&` vs Windows 無等價語法） | 一律用 Python 的 `subprocess.Popen` 管理 Java 服務的啟動與終止，不透過 shell 的背景執行語法 |
| Python 服務熱重載（`uvicorn --reload`） | uvicorn 的 Windows 重啟路徑是 `os.kill(pid, signal.CTRL_C_EVENT)`（`uvicorn/supervisors/basereload.py`），要求目標行程與發送端共享同一個 console process group，Orchestrator 自己 spawn 的子行程經常不滿足這個條件，導致 `restart()` 卡住、⑤ 的熱重載同步等待逾時失敗——不是同步邏輯寫錯，是這個環境下 reload 常常根本沒發生 | 這不是單純的 Windows 相容處理，而是改變了服務的執行位置：Python 目標服務改跑在 Docker 容器內（`python_service/process.py`，見六、09a 三章）——容器內是真正的 Linux 環境，uvicorn 走穩定的 POSIX `SIGTERM` 路徑，不受這個限制影響，已用真實 Docker 容器＋bind mount 驗證過完整重啟週期。Orchestrator 所在機器（含 Windows）因此需要另外安裝 Docker（見二章「初始化步驟」備註） |
| 換行符（CRLF/LF） | git snapshot + AST 插入（translator-cli）對 CRLF 敏感，Windows checkout 預設可能轉換換行符 | 專案根目錄加 `.gitattributes` 統一鎖定 `* text=auto eol=lf`，翻譯後的 Python 檔案一律用 LF |
| console 中文編碼 | Windows 預設 console 編碼是 cp950，中文 log 訊息會 mangle、事後 grep 會漏（見 11a 五章） | `common/logging_setup.py::configure_logging()` 啟動時對 `sys.stdout` 呼叫 `reconfigure(encoding="utf-8", errors="backslashreplace")`（見九），不需要另外的土法修補腳本 |
| asyncio + subprocess | `implement_node`／`DbEnvironment` 用 asyncio 呼叫 subprocess（`npx`、`docker`、`pg_dump`／`psql` 等）時，Windows 在事件迴圈關閉階段偶爾會拋出無害但擾人的 `RuntimeError: Event loop is closed` | Python 3.8+ 在 Windows 上預設已是 `ProactorEventLoop`（支援 subprocess），通常不需要手動設定；若遇到此類訊息干擾（或懷疑被其他套件改了 policy），可在 `main.py` 入口顯式加上 `asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())` 保險 |

> translator-cli 內部的 AST 處理、git snapshot 細節屬於 `07a_translator_cli_architecture.md` 的範圍，這裡只列 LangGraph Orchestrator 層級會直接踩到的坑。

---

## 九、執行與觀察

`main.py` 是整條 pipeline 的進入點：組裝 graph、初始化 `run_id`／一般 log（11a）、用 `astream()` 逐 node 執行並外接卡住偵測（`graph/stream_watchdog.py`）、收尾時關閉 Python 服務容器並寫入整條 run 的制式報告（`common/run_report.py`）。實際的 graph 組裝與執行拆到 `_run_pipeline()`，`main()` 本身只包一層 `reset_python_project_dir()`／`rollback_python_project_dir()`（`translator_cli/git_ops.py`，對應 `docs/refactor_bug_trace.md` #28／#29／#31）：每輪開始前把上一輪的 `python_project_path` 殘留整個改名備份、重新建立乾淨的空目錄＋`git init`，避免不同輪之間 Reduce 模組切法不同造成的殘留檔案累積；若 `_run_pipeline()` 拋出未接住的例外（代表這一輪連 `write_run_report()` 都沒跑到），`except` 區塊會把這次的半成品刪掉、把剛剛搬走的上一輪內容還原回來，避免下一輪誤把半成品當成有效產物、或讓真正有意義的舊產物被越埋越深。

```python
# main.py
"""
Orchestrator 進入點：組裝 graph 並執行
見 01_langgraph_architecture.md 九
"""
import asyncio
import logging
import os
from dotenv import load_dotenv

load_dotenv()  # 必須在下面幾個 import 之前執行——graph.builder 匯入鏈
                # 會連帶載入 refactor_harness/langgraph_nodes/test_nodes.py，
                # 該檔案在 import 期間就讀取 os.environ["JAVA_BASE_URL"]。

from common.logging_setup import configure_logging
from common.run_context import new_run_id
from common.run_report import write_human_readable_report, write_run_report
from graph.builder import build_graph
from graph.nodes import implement_node
from graph.stream_watchdog import STUCK_REPORT_SECONDS, dump_self_stack, pump_graph_stream
from python_service.reload_probe import ensure_reload_probe_infra
from translator_cli.git_ops import reset_python_project_dir, rollback_python_project_dir

logger = logging.getLogger(__name__)


async def main():
    # 一般 log 與 llm_traces.db 共用同一個 run_id（見
    # 11a_logging_architecture.md 六章）：必須在 configure_logging() 之前
    # 產生，才能把它閉包進 _ContextFilter，讓這條 pipeline 執行期間每一行
    # log 都能對回這次 run。
    run_id = new_run_id()
    configure_logging(run_id=run_id, file_handler=True)

    python_project_path = os.environ["PYTHON_PROJECT_PATH"]
    # 對應 docs/refactor_bug_trace.md #28／#29：每輪開始前先把上一輪的
    # 殘留內容整個改名備份、重新建立全新的空目錄＋git init，避免不同輪
    # 之間 Reduce 模組切法不同造成的殘留檔案累積。必須在 ensure_reload_
    # probe_infra() 之前執行——後者要在一個全新的 git repo 裡做初始
    # commit（見該函式 docstring）。
    backup_path = reset_python_project_dir(python_project_path, run_id)
    try:
        await _run_pipeline(python_project_path, run_id)
    except BaseException:
        # 對應 docs/refactor_bug_trace.md #31：走到這裡代表這一輪 pipeline
        # 整個沒跑完（連 write_run_report() 都沒執行到，例如真實案例
        # `20260906_060030_4019f4` 的 API 額度不足）——reset_python_
        # project_dir() 這次建立的新目錄只是半成品，不該留著取代上一輪
        # 的真實產物，也不該讓下一輪把這個半成品又搬進備份堆、真正有
        # 意義的舊產物反而被越埋越深。刪掉這次的半成品，把剛剛搬走的
        # 上一輪內容（如果有）還原回來，讓下一輪重新開始時看到的仍是
        # 上一輪的真實產物。跑完整個流程、有寫出報告的情況（不論成功
        # 或 give_up）不會走到這裡，維持現狀。
        rollback_python_project_dir(python_project_path, backup_path)
        raise


async def _run_pipeline(python_project_path: str, run_id: str) -> None:
    graph = build_graph()

    # 一次性前置準備：必須在 scaffold（④）第一次執行之前完成，見
    # python_service/reload_probe.py docstring、
    # 09b_implement_agent_code.md 二章。冪等，main.py 每次啟動都呼叫。
    ensure_reload_probe_infra(python_project_path)

    initial_state = {
        "run_id": run_id,
        "java_project_path": os.environ["JAVA_PROJECT_PATH"],  # ① 解析 Agent 讀取用，見 00 五章「環境建立」
        "python_project_path": python_project_path,  # translator-cli 寫入目標，見 07a 二章
        "test_dsn": os.environ.get("TEST_DB_DSN", ""),   # implement node 用
        "python_base_url": os.environ.get("PYTHON_BASE_URL", "http://localhost:8000"),  # implement node 用
        "module_list": [],
        "api_to_python_target": [],
        "skip_excluded_overloads": [],
        "openapi_spec": {},
        "collection_readonly_path": "",
        "collection_mutation_path": "",
        "collection_manual_fill_pending": [],
        "golden_output": {},
        "python_structure": {"directory_tree": "", "interfaces": []},
        "route_to_file_mapping": {},
        "task_list": [],
        "scaffold_done": False,
        "skipped_interfaces": [],
        "skipped_db_models": [],
        "completed_tasks": [],
        "failed_tasks": [],
        "task_failures": [],
        "partial_reports": [],
        "blocked_modules": [],
        "failed_modules": [],
        "verified_modules": [],
        "blocked_reasons": {},
        "test_results": {},
        "service_diagnostics": None,
        "debug_rounds": [],
        "pending_fixed_bodies": {},
        "pending_retranslate_tasks": {},
        "pending_file_fixes": [],
        "give_up_early": False,
        "unanalyzed_root_cause_modules": [],
        "retry_count": 0,
    }

    print("Starting Refactor Orchestrator...")
    final_state = dict(initial_state)
    queue: asyncio.Queue = asyncio.Queue()
    stream = graph.astream(initial_state, stream_mode="updates")
    pump_task = asyncio.create_task(pump_graph_stream(stream, queue))
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=STUCK_REPORT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning(
                    "卡住偵測：已經 %.0f 秒沒有任何 node 完成，"
                    "可能正在跑長時間任務（如 ⑤ 填空翻譯），也可能真的卡住了：",
                    STUCK_REPORT_SECONDS,
                )
                dump_self_stack()
                continue

            if kind == "done":
                break
            if kind == "error":
                raise payload

            node_name, node_output = next(iter(payload.items()))
            logger.info("node '%s' 完成", node_name)
            final_state.update(node_output)

        await pump_task  # 確保 pump 端沒有殘留未拋出的例外

        print("\n=== Final State ===")
        print(f"Test Results: {final_state.get('test_results')}")
        print(f"Completed Tasks: {final_state.get('completed_tasks')}")
        print(f"Failed Tasks: {final_state.get('failed_tasks')}")
        print(f"Retry Count: {final_state.get('retry_count')}")

        # 見 common/run_report.py：debug_rounds／task_failures 這些逐輪
        # 累積的歷史，沒有 checkpointer 的情況下 process 一結束就會消失，
        # 上面幾行 print 也沒有印出 debug_rounds——這裡落地成一份人工
        # 事後看得到的制式報告，不論成功或 give_up 都寫。兩份各自獨立：
        # JSON 給程式／未來工具解析，Markdown 是給人直接看的總覽。
        report_path = write_run_report(final_state)
        summary_path = write_human_readable_report(final_state)
        print(f"Run Report (JSON): {report_path}")
        print(f"Run Summary (Markdown): {summary_path}")
    finally:
        # Docker 容器不會隨 Python process 結束自動清理，不論
        # graph 執行成功或拋出例外都要收尾，見 09a 三章
        # 「Python 服務只啟動一次」、graph/nodes/implement_node.py
        # stop_python_service()。
        await implement_node.stop_python_service()


if __name__ == "__main__":
    # Windows 上避免事件迴圈關閉錯誤（見 01_langgraph_architecture.md 八）
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(main())
```

**跟七章的極簡 stub 慣例不同，`initial_state` 把 `RefactorState` 全部欄位都顯式初始化**（不是只列進入點必要的那幾個）：LangGraph 的 `TypedDict` state 沒有強制要求每個 key 一開始就存在，但缺欄位的 state 傳給某個提早讀取該欄位的 node（例如平行分支 node 之間互相不知道彼此進度）容易得到不直觀的 `KeyError`，顯式全部初始化成空值／預設值，讓每個 node 隨時能安全用 `.get()` 或直接索引讀取。三章 State 定義新增的每個欄位（`run_id`／`python_project_path`／`skip_excluded_overloads`／`skipped_interfaces`／`task_failures`／`service_diagnostics`／`verified_modules`／`debug_rounds`／`pending_fixed_bodies`／`pending_retranslate_tasks`／`pending_file_fixes`／`give_up_early`／`unanalyzed_root_cause_modules`）都必須同步補進這裡，否則第一個讀到該欄位的 node 會直接 `KeyError`（見三章結尾備註）。

### `ainvoke()` → `astream()`：卡住偵測

最初版本用 `await graph.ainvoke(initial_state)` 一次性等整條圖跑完——整個過程完全黑箱，任何一個 node 卡住都無從得知（真實重跑卡了一個多小時，只能用 `py-spy` 手動 dump 活行程才抓到卡點）。落地後改用 `graph.astream(stream_mode="updates")` 拿到逐 node 完成的進度事件，`graph/stream_watchdog.py::pump_graph_stream()` 把每個事件丟進一個 `asyncio.Queue`，外層用 `asyncio.wait_for(queue.get(), timeout=STUCK_REPORT_SECONDS)` 偵測「太久沒有任何 node 完成」，觸發時對自己這個 process 做一次 `py-spy dump`（`dump_self_stack()`），印出所有 thread 目前的 Python 呼叫堆疊——不中斷執行，只是把「卡住」從必須手動介入才看得到的黑箱，變成自動、有紀錄的可觀測狀態（`STUCK_REPORT_SECONDS` 預設 600 秒，見二章 `.env`）。

**不能直接對 `stream.__anext__()` 套 `asyncio.wait_for()`**：逾時時 `wait_for()` 會 cancel 被等待的 coroutine，若那個 coroutine 正是目前卡在某個 node 內部（例如卡在 `subprocess.run()`）的執行本體，等於卡住偵測機制自己把還在合理執行中的 node 砍斷。`pump_graph_stream()` 把「消費 stream」跟「等多久算卡住」拆成兩個獨立的 coroutine，`wait_for()` 逾時取消的只是 `queue.get()` 這個無副作用的操作，不會動到真正在跑的 graph 執行本體。這也是 `graph/stream_watchdog.py` 拆成獨立模組、不直接寫在 `main.py` 裡的原因之一——它本身不依賴任何需要 `.env` 才能匯入的模組，方便獨立寫單元測試。

### 一般執行 log 與 `run_id`（11a 落地）

`configure_logging(run_id=run_id, file_handler=True)` 取代了最初版本沒有的 `logging.basicConfig(...)`：裝好 rotating file handler（`logs/orchestrator.log`，10MB／5 份輪替）與 console handler，兩者都掛一個 `logging.Filter`，自動把 `run_id`（這個 process 全程不變）與 `trace_id`（每次 LLM 呼叫各自不同，從 `common/trace_context.py` 的 contextvar 讀取）注入每一行 log；同時把 `httpx`／`httpcore`／`anthropic`／`urllib3` 這幾個第三方套件的 log 等級調到 `WARNING`，避免連線層細節把 log 淹沒。`run_id` 本身由 `common/run_context.py::new_run_id()` 產生（格式 `{YYYYMMDD_HHMMSS}_{uuid4前6碼}`），寫進 `initial_state["run_id"]`，往下由 `implement_node.py` 顯式傳給 `translator_cli.fill_function(run_id=...)`，讓一般 log 與 `llm_traces.db`（見 `11a_logging_architecture.md` 七章）能用同一個識別碼互相對照。完整設計（trace_id 關聯、`llmlog` CLI、Claude／Ollama 兩條路徑怎麼記錄）見 `11a_logging_architecture.md`；`llmlog` 是獨立行程，查詢方式見專案根目錄 `CLAUDE.md`。

### 執行結束時的收尾

`try/finally` 確保不論 graph 正常跑完、`give_up`、還是中途拋例外，都會執行到 `finally` 區塊呼叫 `implement_node.stop_python_service()` 關閉並移除 Python 服務的 Docker 容器（見六）——容器不會隨 Python process 結束自動清理。正常路徑（`try` 區塊）額外呼叫 `write_run_report()` 與 `write_human_readable_report()`（`common/run_report.py`），把整條 run 的結果、`retry_count`、`task_failures`、`debug_rounds`（含每輪 ⑦ 的診斷與修正嘗試）分別落地成 `logs/reports/{YYYY-MM-DD}/{run_id}.json`（給程式／未來工具解析）與同目錄的 `.md`（給人直接看的總覽，含「翻譯總覽」「API 測試總覽」「Debug Agent 診斷總覽」幾個小節）——沒有 checkpointer 的情況下，這些逐輪累積在記憶體裡的欄位在 process 結束後就完全消失，這是目前唯一的事後留存方式。

### 已知限制：仍未接上 checkpointer

`main.py` 目前沒有配置 LangGraph 的 checkpointer（`MemorySaver` 或持久化版本），`astream()` 跑到 `END` 或例外中止就是這次執行的終點，狀態不會保留。五章「人工填值關卡」提到的「續跑」（重新呼叫 `run_collection_agent()` 而不整個重跑 graph）與本章的 `logs/reports/` 落地報告，都是在沒有 checkpointer 前提下的務實補償，不是真正的斷點續跑；真正支援長時間、可中斷續跑的執行模式，仍留待專案需要時再評估接上 checkpointer ＋固定 `thread_id`。

- 執行：`python main.py`
- 圖形檢視：LangGraph 內建 `graph.get_graph().draw_mermaid()` 可輸出 Mermaid 語法，貼到任何 Mermaid renderer 檢查平行分支與 retry 迴圈接線是否符合預期——建議在調整圖結構後就跑一次，比跑完整流程更快發現接錯線的問題。