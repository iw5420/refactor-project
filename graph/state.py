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
    # None：這個 task 的函式本體不需要改（只有 file_fixes 需要套用）。
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
    # target_files 裡，因 referenced_interfaces 而被拉進來的檔案，各自
    # 精確引用到哪些 (class_name, function_name)——translator-cli 讀取
    # 這些檔案時只抽取這裡列出的函式，不整份帶入，避免把同檔案裡無關的
    # 姊妹函式一併送給本地模型（見 06a 七章「已知設計缺陷」修正、
    # docs/09b_bug_trace.md #37）。不含 target_files[0]（自己的檔案，
    # 本來就整份帶入）與 schemas／models 檔案（資料形狀定義，沒有函式
    # 可抽取）。NotRequired，理由同上。
    referenced_functions: NotRequired[list[ReferencedFunctionRef]]


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
