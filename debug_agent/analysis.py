"""⑦ Debug Agent 核心邏輯：四章「Module 級 LLM 分析」（依模組平行呼叫，
比照 05a 六章「依模組取代整包 Map-Reduce」的既有模式，非 map-reduce）、
五章 task_id 反查驗證、七章 give_up_early 判斷、六章 retry_count 遞增。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from pathlib import Path, PurePosixPath

from common.concurrency import default_concurrency
from common.llm_client import LlmJsonError, call_claude_for_json
from debug_agent.llm import DEBUG_AGENT_MAX_TOKENS, DEFAULT_MODEL
from debug_agent.prompts import DEBUG_OUTPUT_SCHEMA, DEBUG_SYSTEM_PROMPT
from debug_agent.triage import ModuleFailureContext, build_module_contexts, compute_zero_endpoint_modules, module_tasks
from graph.state import DebugRound, FileFix, RefactorState, TaskFix
from refactor_harness.core.route_mapper import RouteMapper

logger = logging.getLogger(__name__)

_MAX_MAP_WORKERS = default_concurrency()
# 比照 design_agent/plan_agent 既有的批次重試慣例（同一顆常數
# _RETRY_WAIT_SECONDS=300.0）：SDK 層級的 max_retries（見
# common/llm_client.py）已經處理過短暫的傳輸層錯誤，這裡的等待是給
# 「比 SDK 重試窗口更久」的暫時性外部服務中斷一個恢復機會。跟
# design_agent/plan_agent 不同的地方只在於重試後仍失敗時的處理方式：
# 那兩個 Agent 會中止整條 run，這裡改成把該 module 視為「未分析」
# （見 10a 4.4、七章「give_up_early 的判斷邊界」），不阻斷其餘 module
# 的分析結果，也不讓整個 debug 節點失敗。
_RETRY_WAIT_SECONDS = 300.0

# 固定文字，比照 09a 五章 _RELATIONSHIP_GAP_NOTICE 的既有慣例：機械可
# 確定的結論不需要重新問 LLM 一次措辭（見 10a 3.3）。
_SCAFFOLD_GAP_REASON = "④ 骨架階段未渲染此模組任何函式（100% 骨架缺口），無法透過重新生成修好，需人工介入或等待重新 scaffold"
_MODULE_MISMATCH_REASON = "route_to_module_mapping／task_list 找不到這個 module 對應的 task，需人工核對③/[P]輸出或 skip 呼叫鏈設定，見 02a 十六章"

# 保留模組名稱，跟 design_agent/design.py、parse_agent/summarize.py、
# plan_agent/module_index.py 各自獨立定義的同名常數是同一個字面值（見
# 10a 四章「_global 的檔案永遠不會被任何模組的 referenced_interfaces
# 引用」）——這幾個套件之間沒有共用常數的既有慣例，這裡沿用同一種
# 「各自定義一份」的作法，不新開先例。
_GLOBAL_MODULE_NAME = "_global"


def _read_source_files(python_project_path: str, related_files: set[str]) -> dict[str, str]:
    sources: dict[str, str] = {}
    root = Path(python_project_path)
    for rel_path in related_files:
        path = root / rel_path
        try:
            sources[rel_path] = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # 見 10a 十一章「錯誤處理範圍」：略過該檔案，不中斷整個 module
            # 的分析，理由同 07a「context_files 讀取容錯」既有機制。
            logger.warning("Debug Agent 讀取 related_file 失敗（不存在）：%s", path)
    return sources


def _is_global_infra_file(
    path: str,
    zero_endpoint_modules: frozenset[str] = frozenset(),
    file_to_module: dict[str, str] | None = None,
) -> bool:
    """10a 四章「過濾規則不能只列 schemas/models，還必須涵蓋 core」：
    target_files 除了 schemas/models/core 外還可能帶入
    referenced_interfaces（跨 module 的 router／service／repository
    唯讀參考），無差別聯集會稀釋 context、增加不必要的 token 成本。只取
    父目錄落在 schemas／models／core 任一個的項目——這三者共同的性質是
    「機械產生、不對應任何 TaskSpec，只能透過其他 task 的唯讀 context
    被看到」（05a 三章「全域基礎設施檔案」）——只列 schemas/models 會讓
    透過 app/core/exception_handlers.py 這類共用輔助檔案造成的問題完全
    看不到原始碼，因此一併涵蓋 core。

    **10a 四章「這條規則仍然不夠」、docs/09b_bug_trace.md #49 的修法**：
    純目錄名判斷漏掉「有擁有 task、但擁有它的模組自己沒有 HTTP
    endpoint」這一類檔案（如 app/services/common_service.py）——這種
    module 永遠不會出現在 test_results.failures，3.2 的模組篩選機制
    永遠選不中它，只能靠這裡被其他 module 的 referenced_interfaces 帶進
    context。`zero_endpoint_modules`（`triage.compute_zero_endpoint_modules()`
    算出）、`file_to_module`（`{target_files[0]: module}`，見呼叫端）都是
    可選參數、預設值等同舊行為（只看目錄名）——這是刻意的向後相容設計，
    既有測試以單一參數呼叫這個函式，不該被這次擴充打破。

    `_global` 保留模組（如 app/core/exception_handlers.py）不歸這裡管：
    它從未被任何模組的 referenced_interfaces 引用，連「被聯集進
    target_files」的機會都沒有，必須無條件塞進每個 root_cause module 的
    context，見 run_debug_analysis() 的 global_module_source_files。
    """
    if PurePosixPath(path).parent.name in ("schemas", "models", "core"):
        return True
    if file_to_module is None:
        return False
    owning_module = file_to_module.get(path)
    return owning_module is not None and owning_module in zero_endpoint_modules


def _analyze_root_cause_module(
    ctx: ModuleFailureContext,
    module_summary: str,
    all_tasks: list[dict],
    python_project_path: str,
    service_diagnostics: str | None,
    zero_endpoint_modules: frozenset[str],
    file_to_module: dict[str, str],
    global_module_source_files: dict[str, str],
    run_id: str,
) -> dict:
    """單一 module 的 Claude API 呼叫，回傳 output schema 的原始解析結果。
    呼叫失敗時往上拋 LlmJsonError，由呼叫端的重試佇列機制接手（見
    run_debug_analysis()）。

    `run_id`：對應真實 pipeline 這一輪的 run_id，必填、往下傳給
    `call_claude_for_json()`——原本沒有傳這個參數，`call_claude_for_json()`
    省略時會各自呼叫 `adhoc_run_id()`，導致每一次⑦的分析呼叫都散落在
    自己獨立的 `adhoc_*` run_id 底下，用 `llmlog recent --run <真正的
    run_id>` 完全查不到任何⑦的呼叫紀錄，只能改用時間窗＋caller 名稱
    去撈，違背 11a「查某個 run 到底做了什麼」的查詢設計初衷。

    service_diagnostics：10a 四章「service_diagnostics 要傳給所有
    root_cause module」——這一輪所有 root_cause module 統一傳入，不限於
    special_reason=="batch_reload_timeout" 的 module 才傳（正常情境下是
    None，見呼叫端）。

    zero_endpoint_modules／file_to_module：docs/09b_bug_trace.md #49 修法，
    傳給下面 _is_global_infra_file() 判斷 target_files[1:] 時使用。
    global_module_source_files：`_global` 保留模組的原始碼，見
    run_debug_analysis()，無條件併入 source_files，不受任何過濾規則篩選
    ——`_global` 的檔案從未被任何模組的 referenced_interfaces 引用，沒有
    「被聯集進 related_files」的機會。
    """
    # 10a 四章「為什麼要疊加 target_files，不能只給 related_files」：
    # route_to_file_mapping（related_files 的來源）只涵蓋 routers／
    # services／repositories 三層，不含 schemas/{module}.py／
    # models/{module}.py——missing_fields／type_mismatch 這兩種
    # failure_type 的根因常常就落在這兩類檔案。這兩類檔案不對應任何一個
    # TaskSpec（④機械渲染，不是⑤逐函式填空的對象），因此只能透過 06a
    # 七章既有設計裡、各 task 的唯讀 context（target_files 除了自己的
    # 寫入目標外，還包含這類參考檔案）取得。
    related_files = {rf for f in ctx["harness_failures"] for rf in f.get("related_files", [])}

    # 10a 四章「例外」：判斷條件是「已經算出的 related_files 聯集是否
    # 非空」，不是「ctx["harness_failures"] 是否非空」——後者只涵蓋
    # service_unreachable（harness_failures 整個是空陣列）這一種成因，
    # 漏掉了另一種一樣真實的情況：harness_failures 非空，但這一輪全部
    # 案例剛好都是 golden_not_found、且 route_to_file_mapping 查不到
    # 對應的 route，related_files 因此逐筆都是空清單，聯集起來一樣是
    # 空的。用「聯集本身」當判斷條件，兩種成因用同一個條件自然涵蓋，
    # 不需要窮舉列出。
    if related_files:
        # 有 related_files 可用時，「這個 task 自己的主要寫入目標」
        # （target_files[0]）永遠納入——這是 all_tasks 本來就限定「這個
        # module 底下的 task」（見 module_tasks()），target_files[0] 就是
        # 這個 module 自己的程式碼，不是別的 module 引用進來的東西，不該
        # 被下面 schemas/models/core 的過濾規則擋住。
        #
        # 真實環境重跑時發現的缺陷：route_to_file_mapping 只涵蓋 routers／
        # services／repositories 三層，但只針對「這個 endpoint 呼叫鏈上
        # 的檔案」，涵蓋不到 CollectionUtil.find_distinct_field() 這種
        # 真正的根因所在、卻不屬於任何單一 endpoint 呼叫鏈的共用工具檔案
        # （見 docs/09b_bug_trace.md #43）——這種檔案若剛好是這個 module
        # 自己某個 task 的 target_files[0]，過濾前的版本會把它跟「跨
        # module 引用」的 referenced_interfaces 混為一談整個濾掉，讓
        # Debug Agent 看不到真正藏著 bug 的程式碼、給出錯誤診斷（實測
        # 案例：exam-platform-api 的 CollectionUtil.find_distinct_field()
        # 用 list 當參數名遮蔽 builtin，這正是這個 module 自己的 task，
        # 卻因為父目錄是 services 而非 schemas/models/core 被濾掉）。
        #
        # target_files[1:]（referenced_interfaces 帶進來的唯讀參考檔案，
        # 見 06a 七章）才是真正該套用 schemas/models/core 過濾的對象——
        # 這些才是「這個 task 讀但不寫」的其他 module 檔案，過濾理由（見
        # 下方「為什麼要限縮」）只對這部分成立。
        for t in all_tasks:
            if t["target_files"]:
                related_files.add(t["target_files"][0])
            related_files |= {
                tf for tf in t["target_files"][1:]
                if _is_global_infra_file(tf, zero_endpoint_modules, file_to_module)
            }
    else:
        # 沒有任何已聚焦的訊號可用（不論成因），schemas／models／core-only
        # 的過濾規則在這裡完全不適用：若仍然只給這三類，Debug Agent 會
        # 完全看不到 routers／services／repositories 的原始碼，但
        # batch_reload_timeout 最常見的成因（模組層級匯入／語法錯誤，見
        # 09a 三章）、以及 golden_not_found 對應的路由實作，都正好在
        # 那些檔案裡。這裡改成整份 target_files 不過濾，寧可 context
        # 寬一點，也不能讓 LLM 完全看不到可能藏著問題的程式碼。
        related_files |= {tf for t in all_tasks for tf in t["target_files"]}

    source_files = _read_source_files(python_project_path, related_files)
    # `_global` 保留模組無條件併入，不經過上面任何過濾規則——見本函式
    # 開頭 docstring、docs/09b_bug_trace.md #49。source_files 的 key 已經
    # 用 related_files（真正的 target_files 路徑）算過一輪，這裡直接用
    # dict 聯集，被跳過重複讀取也不會產生副作用。
    source_files = {**global_module_source_files, **source_files}

    known_fill_failures = [
        {"task_id": f["task_id"], "function_name": f["function_name"], "error": f["error"]}
        for f in ctx["task_failures"]
        if f["reason"] == "fill_failed"
    ]
    known_scaffold_gaps = [
        {"task_id": t["id"], "function_name": t["function_name"]}
        for t in all_tasks
        if t["id"] in ctx["scaffold_gap_task_ids"]
    ]

    user_prompt = _build_user_prompt(
        module_summary=module_summary,
        harness_failures=ctx["harness_failures"],
        known_fill_failures=known_fill_failures,
        known_scaffold_gaps=known_scaffold_gaps,
        tasks=all_tasks,
        source_files=source_files,
        special_reason=ctx["special_reason"] or "",
        batch_sibling_modules=ctx["batch_sibling_modules"],
        service_diagnostics=service_diagnostics or "",
    )

    return call_claude_for_json(
        system_prompt=DEBUG_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema=DEBUG_OUTPUT_SCHEMA,
        model=DEFAULT_MODEL,
        max_tokens=DEBUG_AGENT_MAX_TOKENS,
        run_id=run_id,
    )


def _build_user_prompt(
    *,
    module_summary: str,
    harness_failures: list[dict],
    known_fill_failures: list[dict],
    known_scaffold_gaps: list[dict],
    tasks: list[dict],
    source_files: dict[str, str],
    special_reason: str,
    batch_sibling_modules: list[str],
    service_diagnostics: str,
) -> str:
    payload = {
        "module_summary": module_summary,
        "harness_failures": harness_failures,
        "known_fill_failures": known_fill_failures,
        "known_scaffold_gaps": known_scaffold_gaps,
        "tasks": [
            {
                "task_id": t["id"],
                "function_name": t["function_name"],
                "class_name": t.get("class_name"),
                "target_files": t["target_files"],
                "description": t["description"],
            }
            for t in tasks
        ],
        "source_files": source_files,
        "special_note": special_reason,
        "batch_sibling_modules": batch_sibling_modules,
        "service_diagnostics": service_diagnostics,
    }
    # harness_failures 裡的 body_diff（DeepDiff 格式）可能含 Python type
    # 物件（如 {'old_type': <class 'int'>}），json.dumps 沒有 default=str
    # 兜底會直接 TypeError——這是真實環境重跑時實際發生過的內容形狀，跟
    # refactor_harness/langgraph_nodes/test_nodes.py::run_postman_tests()
    # 寫 logs/report_{run_id}.json 時遇到的同一個坑。
    return json.dumps(payload, ensure_ascii=False, default=str)


def _validate_task_fixes(raw_task_fixes: list[dict], valid_task_ids: set[str]) -> list[TaskFix]:
    """10a 4.3：task_id 反查驗證，捨棄不在合法集合內的 task_fix 並記警告，
    不中止整個 module 的分析。10a 八章「phase 2」：也捨棄
    retranslate／fixed_body／file_fixes 三者都沒有真正生效的 task_fix
    ——這種輸出沒有任何實際修正內容，留著只會在 pending_fixed_bodies／
    pending_retranslate_tasks／pending_file_fixes 裡製造一筆什麼都不做
    的空紀錄。

    對應 `docs/refactor_bug_trace.md` #9：`retranslate=True` 時強制把
    `fixed_body` 視為 `None`（即使 LLM 違反 prompt 指示、兩者都給了值）
    ——`retranslate` 優先，理由見 `debug_agent/prompts.py`：⑦ 沒有真實
    Java 原始碼可以核對，這種情況下自己寫的 `fixed_body` 可靠度低於
    交給⑤重新翻譯，不能讓一次沒遵守指示的回應繞過這個保護。
    """
    validated: list[TaskFix] = []
    for tf in raw_task_fixes:
        if tf["task_id"] not in valid_task_ids:
            logger.warning(
                "Debug Agent 回應引用了不存在或不合法的 task_id=%s，捨棄該筆 task_fix", tf["task_id"]
            )
            continue
        retranslate = bool(tf.get("retranslate", False))
        fixed_body = tf["fixed_body"]
        if retranslate and fixed_body is not None:
            logger.warning(
                "task_id=%s：Debug Agent 同時給了 retranslate=true 與非 null 的 fixed_body（違反 "
                "prompt 指示），優先採用 retranslate、捨棄這次的 fixed_body 內容",
                tf["task_id"],
            )
            fixed_body = None
        if not retranslate and not fixed_body and not tf["file_fixes"]:
            logger.warning(
                "Debug Agent 對 task_id=%s 的回應 retranslate／fixed_body／file_fixes 三者皆空，"
                "捨棄該筆 task_fix",
                tf["task_id"],
            )
            continue
        file_fixes = [
            FileFix(
                task_id=tf["task_id"], target_file=ff["target_file"],
                old_snippet=ff["old_snippet"], new_snippet=ff["new_snippet"],
            )
            for ff in tf["file_fixes"]
        ]
        validated.append(TaskFix(
            task_id=tf["task_id"], diagnosis=tf["diagnosis"], retranslate=retranslate,
            fixed_body=fixed_body, file_fixes=file_fixes,
        ))
    return validated


def _run_batch(
    contexts: list[ModuleFailureContext],
    module_summaries: dict[str, str],
    task_list: list[dict],
    python_project_path: str,
    service_diagnostics: str | None,
    zero_endpoint_modules: frozenset[str],
    file_to_module: dict[str, str],
    global_module_source_files: dict[str, str],
    run_id: str,
) -> tuple[dict[str, dict], list[ModuleFailureContext]]:
    """平行分析一批 root_cause module，回傳
    (module -> 原始回應, 失敗待重試的 ModuleFailureContext 清單)。每個
    module 的失敗互相隔離，比照 05a 六章「依模組取代整包 Map-Reduce」。
    """
    results: dict[str, dict] = {}
    failed: list[ModuleFailureContext] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_MAP_WORKERS, len(contexts))) as pool:
        futures = {
            pool.submit(
                _analyze_root_cause_module,
                ctx,
                module_summaries.get(ctx["module"], ""),
                [dict(t) for t in module_tasks(task_list, ctx["module"])],
                python_project_path,
                service_diagnostics,
                zero_endpoint_modules,
                file_to_module,
                global_module_source_files,
                run_id,
            ): ctx
            for ctx in contexts
        }
        for future in concurrent.futures.as_completed(futures):
            ctx = futures[future]
            try:
                results[ctx["module"]] = future.result()
            except LlmJsonError as exc:
                logger.warning("module %s 的⑦除錯分析呼叫失敗，列入待重試清單: %s", ctx["module"], exc)
                failed.append(ctx)
    return results, failed


def _run_root_cause_analysis(
    root_cause_ctxs: list[ModuleFailureContext],
    module_summaries: dict[str, str],
    task_list: list[dict],
    python_project_path: str,
    service_diagnostics: str | None,
    zero_endpoint_modules: frozenset[str],
    file_to_module: dict[str, str],
    global_module_source_files: dict[str, str],
    run_id: str,
) -> dict[str, dict | None]:
    """對 root_cause_ctxs 逐一呼叫 Claude，失敗的批次重試一次；比照
    design_agent/plan_agent 既有的批次重試慣例，但重試仍失敗時**不中止
    整個 debug 節點**——這裡回傳的 dict 用 None 標記「視為未分析」（見
    10a 4.4），由 run_debug_analysis() 的 give_up_early 判斷正確排除，
    不當作「LLM 判斷不可修」的證據。
    """
    if not root_cause_ctxs:
        return {}

    results, failed = _run_batch(
        root_cause_ctxs, module_summaries, task_list, python_project_path, service_diagnostics,
        zero_endpoint_modules, file_to_module, global_module_source_files, run_id,
    )
    if not failed:
        return results

    logger.warning(
        "%d 個 module 的⑦除錯分析呼叫失敗，等待 %.0f 秒後統一重試一次: %s",
        len(failed), _RETRY_WAIT_SECONDS, [c["module"] for c in failed],
    )
    time.sleep(_RETRY_WAIT_SECONDS)

    retry_results, still_failed = _run_batch(
        failed, module_summaries, task_list, python_project_path, service_diagnostics,
        zero_endpoint_modules, file_to_module, global_module_source_files, run_id,
    )
    results.update(retry_results)
    for ctx in still_failed:
        results[ctx["module"]] = None
    return results


def run_debug_analysis(state: RefactorState) -> dict:
    """⑦ Debug Agent 的唯一對外入口，同步函式（見 10a 十章：由
    graph/nodes/debug_node.py 包一層 asyncio.to_thread() 呼叫）。

    對應 10a 六章「retry_count 遞增：推翻既有 stub 邏輯」：既有 stub
    （`increment = 1 if state.get("failed_modules") else 0`）依賴的
    `failed_modules` 只反映 ⑤ 的 readonly 局部驗證，⑤ 依 09a 三章刻意
    不驗證 mutation——一個 module 若只在 mutation 或跨模組 regression上
    有真正的 bug，會被 ⑤ 永遠判定「沒問題」，`failed_modules` 因此永遠
    不包含它，`retry_count` 若沿用舊邏輯會永遠停在原地，形成
    `implement → run_tests → debug` 無限迴圈（見 10a 六章完整論證）。
    這裡只要真的進入這個函式（等同於 `should_debug_or_done()` 已經確認
    `test_results.status == "fail"`），就無條件遞增，不再檢查
    `failed_modules`。
    """
    retry_count = state.get("retry_count", 0)  # 這一輪呼叫當下的值，用於 DebugRound.round（遞增前）
    new_retry_count = retry_count + 1

    contexts = build_module_contexts(state)
    # 若 test_results.reason=="service_unreachable" 且 state["failed_modules"]
    # 也是空，contexts 會是空清單——不另外分岔，直接沿用下面一般路徑：
    # root_cause_ctxs 自然也是空，give_up_early 走「root_cause_ctxs 為空」
    # 那條既有規則（見下方），give_up 節點通知人工從 state["service_
    # diagnostics"] 查起，見 10a 三章。

    module_summaries = {m["module"]: m.get("summary", "") for m in state.get("module_list", [])}
    task_list = state.get("task_list", [])
    python_project_path = state["python_project_path"]

    root_cause_ctxs = [c for c in contexts if c["origin"] == "root_cause"]

    # 10a 四章「service_diagnostics 要傳給所有 root_cause module」：這一輪
    # 所有 root_cause module 統一傳入，不限於 special_reason=="batch_reload_
    # timeout" 的 module，理由見該節「為什麼不是只在...才傳」。正常情境
    # 下（⑥ 沒有遇到 service_unreachable）這裡是 None，不影響一般案例。
    service_diagnostics = state.get("service_diagnostics")

    # docs/09b_bug_trace.md #49 修法（10a 四章）：算出「沒有任何 HTTP
    # endpoint」的模組全集，供 _is_global_infra_file() 判斷 target_files
    # [1:] 時使用；順便無條件讀出 `_global` 保留模組的原始碼（它從未被
    # 任何模組的 referenced_interfaces 引用，見 _is_global_infra_file()
    # docstring），等一下塞給每一個 root_cause module，比照
    # service_diagnostics「給所有 root_cause module」的既有模式。全部
    # 條件在 root_cause_ctxs 非空時才算——沒有任何 module 要分析時，連
    # RouteMapper() 這個檔案讀取（route_to_module_mapping 是③寫進
    # config/harness.yaml、唯一的權威來源，不在 RefactorState 裡，見
    # 02a 十三章 RouteMapper）都省下來，理由同 give_up_early 案例二的
    # 既有短路精神：這一輪根本不會呼叫 LLM，不需要為了呼叫準備任何 context。
    if root_cause_ctxs:
        zero_endpoint_modules = frozenset(
            compute_zero_endpoint_modules(state.get("module_list", []), RouteMapper().module_mapping)
        )
        file_to_module = {
            t["target_files"][0]: t["module"] for t in task_list if t["target_files"]
        }
        global_module_files = {
            t["target_files"][0]
            for t in task_list
            if t["module"] == _GLOBAL_MODULE_NAME and t["target_files"]
        }
        global_module_source_files = _read_source_files(python_project_path, global_module_files)
    else:
        zero_endpoint_modules = frozenset()
        file_to_module = {}
        global_module_source_files = {}

    analyzed_results = _run_root_cause_analysis(
        root_cause_ctxs, module_summaries, task_list, python_project_path, service_diagnostics,
        zero_endpoint_modules, file_to_module, global_module_source_files, state["run_id"],
    )

    # ⚠️ this_round_rounds 只裝這一次呼叫產生的 DebugRound，絕對不能跟
    # state.get("debug_rounds", [])（掛 operator.add reducer、逐輪累積
    # 的歷史欄位）混用或誤讀——give_up_early 的判斷必須只看這一輪，見
    # 10a 七章「為什麼一定要是這一輪、不能是累積歷史」：歷史紀錄裡可能
    # 混著更早輪次「fixable=True」的舊記錄，用累積歷史判斷會讓
    # give_up_early 被過期資料卡死、永遠回傳 False。
    this_round_rounds: list[DebugRound] = []
    pending_fixed_bodies: dict[str, str] = {}
    pending_retranslate_tasks: dict[str, str] = {}
    pending_file_fixes: list[FileFix] = []

    for ctx in contexts:
        module = ctx["module"]

        if ctx["origin"] == "blocked":
            this_round_rounds.append(DebugRound(
                round=retry_count, module=module, origin="blocked", fixable=False,
                root_cause_summary="此模組被上游卡住（見 blocked_reasons），非此模組自身問題",
                task_fixes=[], unfixable_reasons=[],
            ))
            continue

        if ctx["origin"] == "module_mismatch":
            # 10a 3.3：test_results.failures 的 module 對不上 task_list
            # 裡任何一個 task，不呼叫 LLM、不嘗試用 related_files 反查
            # 猜測，直接記固定診斷文字。
            this_round_rounds.append(DebugRound(
                round=retry_count, module=module, origin="module_mismatch", fixable=False,
                root_cause_summary=_MODULE_MISMATCH_REASON,
                task_fixes=[], unfixable_reasons=[_MODULE_MISMATCH_REASON],
            ))
            continue

        if ctx["origin"] == "scaffold_gap":
            this_round_rounds.append(DebugRound(
                round=retry_count, module=module, origin="scaffold_gap", fixable=False,
                root_cause_summary=_SCAFFOLD_GAP_REASON,
                task_fixes=[], unfixable_reasons=[_SCAFFOLD_GAP_REASON],
            ))
            continue

        raw = analyzed_results.get(module)
        if raw is None:
            # API 呼叫失敗、重試仍失敗：視為「未分析」，不產生 DebugRound
            # （不能算進 fixable=False 的證據，見 10a 七章「為什麼排除
            # API 失敗未分析到的 module」）。root_cause_ctxs 本身（下面
            # give_up_early 判斷用的集合）仍然完整保留這個 module，不會
            # 因為這裡沒有 append DebugRound 就從集合裡消失。
            continue

        # ⚠️ 順序固定：4.3（task_id 反查驗證）必須先跑，fixable 才能從
        # 驗證過的 task_fixes 推導——見 10a 4.2「這個順序不能反過來」。
        # fixable 直接定義成 bool(task_fixes)，不是讀 raw["fixable"] 再
        # 視情況覆寫：這樣寫從結構上就不可能出現「fixable=True 但
        # task_fixes=[]」這種非法狀態。
        valid_task_ids = {t["id"] for t in module_tasks(task_list, module)} - ctx["scaffold_gap_task_ids"]
        task_fixes = _validate_task_fixes(raw["task_fixes"], valid_task_ids)

        fixable = bool(task_fixes)
        unfixable_reasons = list(raw["unfixable_reasons"])
        if raw["fixable"] and not task_fixes:
            # 10a 4.2：這裡只是補一筆說明文字，不是「校正」fixable 本身
            # ——fixable 從上面 bool(task_fixes) 那行起就已經是對的。
            unfixable_reasons.append("LLM 回應宣稱可修但未提供任何有效 task_fix，視為分析失敗")

        this_round_rounds.append(DebugRound(
            round=retry_count, module=module, origin="root_cause", fixable=fixable,
            root_cause_summary=raw["root_cause_summary"],
            task_fixes=task_fixes, unfixable_reasons=unfixable_reasons,
        ))
        for tf in task_fixes:
            if tf.get("retranslate"):
                pending_retranslate_tasks[tf["task_id"]] = tf["diagnosis"]
            elif tf["fixed_body"] is not None:
                pending_fixed_bodies[tf["task_id"]] = tf["fixed_body"]
            pending_file_fixes.extend(tf["file_fixes"])

    # 10a 七章「判斷規則」：root_cause_ctxs 為空（案例二）時直接
    # give_up_early=True——三章的機械分類已經證明這一輪沒有任何 module
    # 有機會透過重試修好（blocked 待的上游若真有救必然自己也在
    # root_cause_ctxs 裡，module_mismatch／scaffold_gap 定義上就是不可能
    # 靠重試修，見 10a 七章完整論證），不需要呼叫 LLM 才能知道這一輪
    # 沒救，回 implement 只會被排程器的 already_failed 跳過，白白浪費一輪。
    if not root_cause_ctxs:
        give_up_early = True
    else:
        # 案例一：root_cause_ctxs 非空時，give_up_early 必須以這個
        # **完整集合**為準，不能只看「已經出現在 this_round_rounds 裡」
        # 的子集——API 呼叫失敗、重試仍失敗的 module 不會產生 DebugRound
        # （見上方 `if raw is None: continue`），若判斷式只從
        # this_round_rounds 反推，這種 module 會直接從集合裡消失，讓
        # all_analyzed 這一關形同虛設。
        all_analyzed = all(analyzed_results.get(ctx["module"]) is not None for ctx in root_cause_ctxs)
        root_cause_rounds = [r for r in this_round_rounds if r["origin"] == "root_cause"]
        give_up_early = all_analyzed and all(not r["fixable"] for r in root_cause_rounds)

    # 供 give_up_node.py 在 retry_count 用盡時判斷「這一輪是不是 Claude
    # API 本身打不通」，不是 ⑦ 判斷邏輯錯誤——這兩種情況印出的訊息目前
    # 完全一樣，人工得自己去翻 debug_rounds 或 llmlog 才能分辨。這裡只是
    # 把 analyzed_results 已經算出來的資料原樣往外傳，不是新的判斷邏輯。
    unanalyzed_root_cause_modules = [
        ctx["module"] for ctx in root_cause_ctxs if analyzed_results.get(ctx["module"]) is None
    ]

    return {
        **state,
        "retry_count": new_retry_count,
        "debug_rounds": this_round_rounds,  # reducer（operator.add）在這裡才把這一輪併進歷史
        "pending_fixed_bodies": pending_fixed_bodies,
        "pending_retranslate_tasks": pending_retranslate_tasks,
        "pending_file_fixes": pending_file_fixes,
        "give_up_early": give_up_early,
        "unanalyzed_root_cause_modules": unanalyzed_root_cause_modules,
    }
