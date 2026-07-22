import os
import yaml
from graph.state import RefactorState
from refactor_harness.recorder.golden_writer import GoldenRecorder
from refactor_harness.verifier.comparator import GoldenVerifier
from refactor_harness.verifier.mutation_verifier import MutationVerifier
from refactor_harness.core.reporter import HarnessReporter
from refactor_harness.fixtures.db_env import DbEnvironment

with open("config/harness.yaml", encoding="utf-8") as f:
    HARNESS_CONFIG = yaml.safe_load(f)

TABLES = HARNESS_CONFIG["databases"]["test"]["tables_to_truncate"]
MAX_RETRY = 3  # 已定案（見 00 九、State 表格 retry_count）

# Java 服務位置不放進 RefactorState，直接讀 .env
JAVA_BASE_URL = os.environ["JAVA_BASE_URL"]


# ── Agent ②：錄製 golden output（readonly + mutation）──
def record_golden_output(state: RefactorState) -> RefactorState:
    """
    分別呼叫 record()（readonly）與 record_mutation()（mutation），兩者的 DB
    reset 策略不同，已封裝在 GoldenRecorder 內部。
    """
    recorder = GoldenRecorder(
        java_base_url=JAVA_BASE_URL,
        golden_dir="fixtures/golden",
        test_dsn=state["test_dsn"],
    )
    readonly_result = recorder.record("postman/collection_readonly.json")
    mutation_result = recorder.record_mutation("postman/collection_mutation.json")
    recorder.write_metadata(readonly_result, mutation_result)

    return {
        **state,
        "golden_output": {
            "readonly": readonly_result,
            "mutation": mutation_result,
        },
    }


# ── Agent ⑥：驗證 Python 服務（全量，跨模組 regression 的最終防線）──
def run_postman_tests(state: RefactorState) -> RefactorState:
    """
    readonly 與 mutation 的原始結果合併後只呼叫一次 build_report()。
    """
    db = DbEnvironment(test_dsn=state["test_dsn"])
    db.apply_seed("fixtures/seed.sql", tables_to_truncate=TABLES)

    verifier = GoldenVerifier(
        python_base_url=state["python_base_url"],
        golden_dir="fixtures/golden"
    )
    readonly_raw = verifier.verify_raw("postman/collection_readonly.json")

    mutation_verifier = MutationVerifier(
        python_base_url=state["python_base_url"],
        golden_dir="fixtures/golden",
        test_dsn=state["test_dsn"],
    )
    mutation_raw = mutation_verifier.verify_all_raw()

    report = HarnessReporter().build_report(readonly_raw + mutation_raw)

    return {**state, "test_results": report}


# ── 條件邊：決定去 Debug、結束、還是通知人工 ────────────────────
def should_debug_or_done(state: RefactorState) -> str:
    """
    retry_count 只在 failed_modules（確實跑過、驗證過但沒過）非空時才計入
    重試判斷；blocked_modules 不影響這裡的判斷。
    """
    report = state["test_results"]

    if report["status"] == "pass":
        return "done"

    failed_modules = state.get("failed_modules", [])
    if not failed_modules:
        # 只有 blocked_modules、沒有 failed_modules
        return "debug"

    if state["retry_count"] >= MAX_RETRY:
        return "give_up"
    return "debug"
