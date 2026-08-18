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


class PythonStructure(TypedDict):
    directory_tree: str             # 目錄結構的文字表示，供 ④ 建立骨架時參照
    interfaces: list[InterfaceSpec] # [P] target_files 與 ④ 骨架簽名的唯一權威來源


# ── Agent ⑤ 輸出：task 失敗根因（見 09a 六章）──
class TaskFailure(TypedDict):
    task_id: str
    module: str
    file_path: str
    class_name: str | None
    function_name: str
    reason: Literal["scaffold_skipped", "fill_failed"]
    error: str


# ── [P] Plan Agent 輸出：task list ────────────────────
class TaskSpec(TypedDict):
    id: str
    module: str
    description: str
    target_files: list[str]        # 必須是 python_structure.interfaces 中已存在的 file_path
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


# ── 整體 State ─────────────────────────────────────────
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
    # 純附加診斷資訊，不是排程判斷依據：blocked_modules 裡每個 module
    # 對回它 depends_on 裡狀態還不是 "verified" 的直接上游 module 名稱
    # 清單，供人工／未來 ⑦ Debug Agent 不需要反查 module_list.depends_on
    # 就能直接讀出「這個 module 被誰卡住」。見 docs/09b_bug_trace.md #29。
    blocked_reasons: dict[str, list[str]]

    # Agent ⑥
    test_results: dict

    # Orchestrator
    retry_count: int
