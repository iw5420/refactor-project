import json
import os
import time
import yaml
import httpx
from graph.state import RefactorState
from python_service import manager as python_service_manager
from design_agent.route_mapping import build_route_to_module_mapping
from refactor_harness.recorder.golden_writer import GoldenRecorder
from refactor_harness.verifier.comparator import GoldenVerifier
from refactor_harness.verifier.mutation_verifier import MutationVerifier
from refactor_harness.core.postman_runner import NewmanTimeoutError
from refactor_harness.core.reporter import HarnessReporter
from refactor_harness.fixtures.db_env import DbEnvironment
from spec_collection_agent.java_service import JavaServiceProcess, resolve_jar_path

with open("config/harness.yaml", encoding="utf-8") as f:
    HARNESS_CONFIG = yaml.safe_load(f)

TABLES = HARNESS_CONFIG["databases"]["test"]["tables_to_truncate"]
MAX_RETRY = 1  # 使用者決定：能修就該在第一輪修出來，第二、三輪的邊際效益太低，
# 不值得多花那些 LLM 呼叫額度；改壞的話，翻譯品質問題該去強化 ⑤／⑦ 第一輪
# 本身的機制，不是靠多跑幾輪碰運氣（原值 3，見 docs/refactor_bug_trace.md #11）

# Java 服務位置不放進 RefactorState，直接讀 .env
JAVA_BASE_URL = os.environ["JAVA_BASE_URL"]

# 見 10a 二章「⑥ 對外呼叫前必須先確認 Python 服務有回應」：只確認連線
# 層級可達，不是 09a 三章 _wait_for_service_reload() 那種要比對特定
# token 的同步屏障——⑤ 該做的等待已經做過，這裡只需要知道「現在」連
# 不連得上。逾時預算刻意比 09a 的 SERVICE_READY_TIMEOUT_SECONDS（預設
# 120s）短很多：這裡的目的不是「等它恢復」，是「快速判斷這一輪要不要
# 跳過 Newman」，服務若真的當機，多等也不會自己好。
RUN_TESTS_HEALTH_CHECK_TIMEOUT_SECONDS = float(
    os.environ.get("RUN_TESTS_HEALTH_CHECK_TIMEOUT_SECONDS", "15")
)
RUN_TESTS_HEALTH_CHECK_POLL_INTERVAL_SECONDS = float(
    os.environ.get("RUN_TESTS_HEALTH_CHECK_POLL_INTERVAL_SECONDS", "3")
)

# 見 docs/09b_bug_trace.md #47：`implement_node.py` 一輪可能套用多筆
# `pending_fixed_bodies`／`pending_file_fixes`（⑦一輪可能一次開出好幾筆
# 修正），各自的檔案寫入被 uvicorn `--reload`（watchfiles）依短暫的
# debounce 視窗各自分批觸發，可能連續產生不只一次的 reload 週期；
# `_wait_for_service_reload()`（09a 三章）只確認「牠自己最後一次寫入的
# token」已經生效，不保證這是這一輪「最後一次」被觸發的 reload——已重現
# 三次的症狀：`run_postman_tests()` 這裡的健康檢查回報 service_unreachable，
# 但用獨立診斷腳本立刻探測同一顆容器，服務其實完全正常，是健康檢查時機
# 撞上還沒收斂穩定的 reload 窗口，不是真的壞掉。根因（uvicorn 忙碌／
# reload 批次時機）未 100% 證實，這裡先採用 bug_trace 記錄的其中一個
# 建議緩解方向：呼叫健康檢查之前先固定等一段緩衝時間，讓可能還在進行中
# 的 reload 有機會收斂——不是同步屏障（不像 `_wait_for_service_reload()`
# 那樣比對特定 token），純粹是給連續 reload 一點喘息時間，緩解機率、
# 不保證根除（見 09b_bug_trace.md #47「已重現三次」段落）。
RUN_TESTS_PRE_HEALTH_CHECK_BUFFER_SECONDS = float(
    os.environ.get("RUN_TESTS_PRE_HEALTH_CHECK_BUFFER_SECONDS", "5")
)


def _is_service_reachable(base_url: str, timeout_seconds: float, poll_interval: float) -> bool:
    """只檢查連線層級是否可達，不檢查回應內容或狀態碼——即使服務回
    404／500，只要連得上就代表這不是「worker 崩潰、連線被拒絕」的情境，
    交給 run_newman() 正常執行、讓既有的比對邏輯去發現真正的問題。
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            httpx.get(base_url, timeout=5.0)
            return True
        except httpx.HTTPError:
            pass
        time.sleep(poll_interval)
    return False


# 見 docs/09b_bug_trace.md #54：run_postman_tests() 的三個 return 分支
# 都用 `**state` 帶過其餘欄位（省得每個分支重複列舉整份 RefactorState），
# 但 completed_tasks／failed_tasks／task_failures／partial_reports／
# debug_rounds 這五個欄位掛了 operator.add reducer（見 graph/state.py），
# `**state` 會把「目前已累積的完整值」原樣當成這次的回傳值，LangGraph
# 分不出這是「不小心重複回傳」還是「這次真的新增了這麼多」，直接把它
#加到既有累積值上——每次這個 node 完成，這些欄位的長度就再乘以 2。
# 真實重跑（run_id 20260827_104547_f29491）證實：task_045 的 1 筆真實
# fill_failed 記錄膨脹成 128 筆重複，report 因此肥大到 941KB。用這個
# 常數明確覆寫掉這五個欄位，讓 `**state` 只用來帶過真正「這裡沒動、也
# 不需要 reducer 累加」的欄位（如 test_results／service_diagnostics 這類
# 本來就該整份覆蓋的欄位），不是排除法——新增任何一個掛 reducer 的欄位
# 都要記得同步加進這裡。
_NO_REDUCER_DELTA = {
    "completed_tasks": [],
    "failed_tasks": [],
    "task_failures": [],
    "partial_reports": [],
    "debug_rounds": [],
}


_UNREACHABLE_TEST_RESULTS = {
    "summary": {"total": 0, "passed": 0, "failed": 0, "pass_rate": 0},
    "status": "fail",
    "reason": "service_unreachable",
    "failures": [],
    "passed_cases": [],
    "excluded_folders": [],
    "excluded_cases": [],
}


# ── Agent ②：錄製 golden output（readonly + mutation）──
def record_golden_output(state: RefactorState) -> dict:
    """
    分別呼叫 record()（readonly）與 record_mutation()（mutation），兩者的 DB
    reset 策略不同，已封裝在 GoldenRecorder 內部。

    test_dsn 傳入 state["test_dsn"]，確保 Recorder 與 run_postman_tests 的
    Verifier 連到同一顆測試 DB（見 02a 十四章）。

    平行分支 node：parse 完成後與 design（③）同時觸發（見 00 一章流程圖、
    01 五章、05a 十一章——③ 不依賴 golden_output），因此只回傳自己實際更動
    的 key，不能用 `{**state, ...}` 整包展開，避免跟 design 同一個
    superstep 對同一個 key 各自寫入。

    自己開一個 `JavaServiceProcess`（比照 `spec_collection_agent.run_spec_agent()`
    的既有寫法，換一組 env_overrides），見 03a 二章「與 Agent ② 共用的邊界」、
    `docs/09b_bug_trace.md` #39：① parse 用的那個實例只在 parse 期間存在，
    parse 一結束就 stop()，這裡不能假設 `JAVA_BASE_URL` 當下還有活著的服務。
    """
    with JavaServiceProcess(
        jar_path=resolve_jar_path(os.environ["JAVA_JAR_PATH"]),
        base_url=JAVA_BASE_URL,
        java_executable=os.environ.get("JAVA_EXECUTABLE_PATH", "java"),
        env_overrides={
            "SPRING_DATASOURCE_URL": os.environ["SPRING_DATASOURCE_URL"],
            "SPRING_DATASOURCE_USERNAME": os.environ["SPRING_DATASOURCE_USERNAME"],
            "SPRING_DATASOURCE_PASSWORD": os.environ["SPRING_DATASOURCE_PASSWORD"],
        },
    ):
        recorder = GoldenRecorder(
            java_base_url=JAVA_BASE_URL,
            golden_dir="fixtures/golden",
            test_dsn=state["test_dsn"],
            route_to_module_mapping=build_route_to_module_mapping(state["api_to_python_target"]),
        )
        readonly_result = recorder.record("postman/collection_readonly.json")
        mutation_result = recorder.record_mutation("postman/collection_mutation.json")
        recorder.write_metadata(readonly_result, mutation_result)

    return {
        "golden_output": {
            "readonly": readonly_result,
            "mutation": mutation_result,
        },
    }


# ── Agent ⑥：驗證 Python 服務（全量，跨模組 regression 的最終防線）──
def run_postman_tests(state: RefactorState) -> RefactorState:
    """
    readonly 與 mutation 的原始結果合併後只呼叫一次 build_report()。

    mutation_verifier.verify_all_raw() 內部已自動排除 tainted folder（見
    MutationVerifier、02a 四章「排除已知異常的 folder」）；跑完後透過
    get_excluded_folders() 取得這次實際跳過的 folder 清單，一併傳進
    build_report()，讓最終 report 帶有 excluded_folders 欄位（見 02a 九章）。

    見 docs/09b_bug_trace.md #42：02a 十一章流程圖從設計當下就明確畫出
    「輸出單一 report.json」交給 ⑦ Debug Agent 讀取，但 02b 從未真的把
    這一步寫進程式碼——`report` 只活在 `state["test_results"]`，graph run
    一結束就消失，⑦ 沒有檔案可讀。這裡補上落地：寫到
    `logs/report_{run_id}.json`，跟既有 `logs/orchestrator.log`／
    `logs/llm_traces.db` 同一個「不進 git 的執行期產物」目錄（見
    .gitignore），檔名帶 run_id 避免跨次執行互相覆蓋，同一次 run 內
    debug 迴圈重跑 run_tests 則直接覆寫成最新結果——⑦ 永遠讀這次 run
    目前最新的驗證結果，不需要自己判斷要看哪一份。

    **前置健康檢查**（10a 二章）：09a 三章「批次執行」描述的
    batch_reload_timeout 最常見成因（某個 task 寫入的程式碼有模組層級
    匯入／語法錯誤，讓容器內的 uvicorn worker 崩潰）在 implement 結束
    時可能還沒解決，Python 服務容器可能仍處於連不上的狀態（見 09b 六章
    「Python 服務只啟動一次」——⑤／⑥ 共用同一個持續運行的容器，⑤ 沒有
    機制把它修好）。若不做這個檢查，下面的 run_newman() 會直接拋出
    RuntimeError（02a 五章既有、刻意設計的行為），讓整條 graph.ainvoke()
    崩潰，⑦ Debug Agent 永遠不會被呼叫到。

    同一時間點順手讀取 `service_diagnostics`（10a 二章、八章）：這是
    診斷資料最新鮮的時間點，寫進 State 供 ⑦ 之後讀取——⑦（`debug_agent/`）
    全程不 import 任何 `graph/nodes/*.py`，也不 import
    `python_service.manager` 本身，只讀 `state["service_diagnostics"]`，
    避免 node 對 node 互相依賴（見 10a 八章）。

    **健康檢查前先固定緩衝**（見 `RUN_TESTS_PRE_HEALTH_CHECK_BUFFER_SECONDS`
    docstring、`docs/09b_bug_trace.md` #47）：不分岔判斷「這一輪是不是
    ⑦ 套用了多筆修正」——那需要額外傳遞、判斷跨節點狀態，且緩解方向本身
    是機率性的（讓可能還在進行中的 reload 有機會收斂），對「這一輪其實
    只有一筆改動」的情況多等這幾秒也無害，不值得為了省這幾秒緩衝時間
    增加狀態判斷的複雜度。
    """
    time.sleep(RUN_TESTS_PRE_HEALTH_CHECK_BUFFER_SECONDS)
    if not _is_service_reachable(
        state["python_base_url"],
        RUN_TESTS_HEALTH_CHECK_TIMEOUT_SECONDS,
        RUN_TESTS_HEALTH_CHECK_POLL_INTERVAL_SECONDS,
    ):
        return {
            **state,
            "test_results": dict(_UNREACHABLE_TEST_RESULTS),
            "service_diagnostics": python_service_manager.get_diagnostics(),
            **_NO_REDUCER_DELTA,
        }

    db = DbEnvironment(test_dsn=state["test_dsn"])
    db.apply_seed("fixtures/seed.sql", tables_to_truncate=TABLES)

    try:
        route_to_module_mapping = build_route_to_module_mapping(state["api_to_python_target"])
        verifier = GoldenVerifier(
            python_base_url=state["python_base_url"],
            golden_dir="fixtures/golden",
            route_to_module_mapping=route_to_module_mapping,
        )
        readonly_raw = verifier.verify_raw("postman/collection_readonly.json")

        mutation_verifier = MutationVerifier(
            python_base_url=state["python_base_url"],
            golden_dir="fixtures/golden",
            test_dsn=state["test_dsn"],
            route_to_module_mapping=route_to_module_mapping,
        )
        mutation_raw = mutation_verifier.verify_all_raw()
    except NewmanTimeoutError:
        # newman（Node.js）已知會在收到最後一筆回應、報表檔案已經完整
        # 寫出之後，行程本身卻不退出（keep-alive socket 未乾淨關閉），
        # NewmanTimeoutError 因此不等於服務沒回應——真正的「報表是否完整」
        # 判斷已經下沉到 run_newman() 自己（見 postman_runner.py，
        # docs/09b_bug_trace.md #48）；這裡攔到的都是報表也沒寫完的真逾時，
        # 落到跟前置健康檢查失敗時同一份 fallback 結構，讓 debug_agent
        # 三章既有的 service_unreachable 分支接手，不能讓例外往外炸穿
        # 整條 graph.ainvoke()（見 10a 二章）。
        return {
            **state,
            "test_results": dict(_UNREACHABLE_TEST_RESULTS),
            "service_diagnostics": python_service_manager.get_diagnostics(),
            **_NO_REDUCER_DELTA,
        }

    report = HarnessReporter().build_report(
        readonly_raw + mutation_raw,
        excluded_folders=mutation_verifier.get_excluded_folders(),
    )

    os.makedirs("logs", exist_ok=True)
    report_path = f"logs/report_{state['run_id']}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    # 對應 docs/09b_bug_trace.md：service_diagnostics 只在上面兩個
    # service_unreachable 分支被寫入，這條「服務有回應、report 真的產出
    # 來」的成功路徑過去從未清空它——這一輪服務明明打得通，state 裡卻
    # 可能還殘留上一輪服務連不上時擷取的舊崩潰日誌（因為 LangGraph 對
    # 沒回傳的欄位是維持原值，不是清空）。真實環境重跑證實這會讓 ⑦
    # Debug Agent 誤信一份過期但看起來「這一輪剛擷取」的容器崩潰日誌
    # （debug_agent 的 system prompt 明講 service_diagnostics 非空代表
    # 「這一輪」服務連不上），在已經修好的舊 bug 上原地打轉，完全沒機會
    # 注意到服務其實正常啟動、真正在發生的是別的 bug。這裡確認服務有
    # 回應之後，明確清空它，不讓過期診斷跨輪殘留。
    return {**state, "test_results": report, "service_diagnostics": None, **_NO_REDUCER_DELTA}


# ── 條件邊：決定去 Debug、結束、還是通知人工 ────────────────────
def should_debug_or_done(state: RefactorState) -> str:
    """
    retry_count 上限檢查對所有分支統一套用，不再依 failed_modules 是否
    為空分岔（見 10a 七章「為什麼要統一套用」）——這條分支原本存在的
    理由是「blocked_modules 只是排程還沒排到，不算真正失敗」，但 10a
    六章已經證明這個理由在目前 graph 的實際拓樸下不成立：能讓 debug
    被觸發，test_results.status == "fail" 已經是既成事實，failed_modules
    是否非空只反映 ⑤ 局部驗證（readonly-only）看不看得到問題，不是
    「有沒有真的壞」的可靠依據，不該拿來決定要不要檢查 retry_count 上限。
    """
    report = state["test_results"]
    if report["status"] == "pass":
        return "done"
    if state["retry_count"] >= MAX_RETRY:
        return "give_up"
    return "debug"
