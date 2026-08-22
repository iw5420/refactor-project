"""
Orchestrator 進入點：組裝 graph 並執行
見 01_langgraph_architecture.md 九
"""
import asyncio
import logging
import os
from dotenv import load_dotenv
from common.logging_setup import configure_logging
from common.run_context import new_run_id
from graph.builder import build_graph
from graph.nodes import implement_node
from graph.stream_watchdog import STUCK_REPORT_SECONDS, dump_self_stack, pump_graph_stream
from python_service.reload_probe import ensure_reload_probe_infra


load_dotenv()

logger = logging.getLogger(__name__)


async def main():
    graph = build_graph()

    # 一般 log 與 llm_traces.db 共用同一個 run_id（見
    # 11a_logging_architecture.md 六章）：必須在 configure_logging() 之前
    # 產生，才能把它閉包進 _ContextFilter，讓這條 pipeline 執行期間每一行
    # log 都能對回這次 run。
    run_id = new_run_id()
    configure_logging(run_id=run_id, file_handler=True)

    python_project_path = os.environ["PYTHON_PROJECT_PATH"]
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
        "blocked_reasons": {},
        "test_results": {},
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
