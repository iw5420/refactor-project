"""
Orchestrator 進入點：組裝 graph 並執行
見 01_langgraph_architecture.md 九
"""
import asyncio
import os
from dotenv import load_dotenv
from graph.builder import build_graph


load_dotenv()


async def main():
    graph = build_graph()

    initial_state = {
        "java_project_path": "./java-project",           # ① 解析 Agent 讀取用
        "test_dsn": os.environ.get("TEST_DB_DSN", ""),   # implement node 用
        "python_base_url": os.environ.get("PYTHON_BASE_URL", "http://localhost:8000"),  # implement node 用
        "module_list": [],
        "api_to_python_target": [],
        "openapi_spec": {},
        "collection_readonly_path": "",
        "collection_mutation_path": "",
        "golden_output": {},
        "python_structure": {"directory_tree": "", "interfaces": []},
        "route_to_file_mapping": {},
        "task_list": [],
        "scaffold_done": False,
        "completed_tasks": [],
        "failed_tasks": [],
        "partial_reports": [],
        "blocked_modules": [],
        "failed_modules": [],
        "test_results": {},
        "retry_count": 0,
    }

    print("Starting Refactor Orchestrator...")
    final_state = await graph.ainvoke(initial_state)

    print("\n=== Final State ===")
    print(f"Test Results: {final_state.get('test_results')}")
    print(f"Completed Tasks: {final_state.get('completed_tasks')}")
    print(f"Failed Tasks: {final_state.get('failed_tasks')}")
    print(f"Retry Count: {final_state.get('retry_count')}")


if __name__ == "__main__":
    # Windows 上避免事件迴圈關閉錯誤（見 01_langgraph_architecture.md 八）
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(main())
