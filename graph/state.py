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
