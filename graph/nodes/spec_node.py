"""
[A] Spec Agent（程式邏輯）
啟動 Java 服務，取得 OpenAPI 3.0 JSON
見 03a_spec_collection_agent_architecture.md 二、03b_spec_agent_code.md

`JAVA_JAR_PATH`／`JAVA_EXECUTABLE_PATH`／`SPRING_DATASOURCE_*` 不放進
`RefactorState`，直接讀 `os.environ`（理由見 03a 五章備註）。
"""
import asyncio
import os

from graph.state import RefactorState
from spec_collection_agent import run_spec_agent


async def run(state: RefactorState) -> RefactorState:
    # run_spec_agent() 內部是同步阻塞呼叫（輪詢 Java 進程，最長可達 90 秒
    # 啟動逾時預算），丟到執行緒跑，避免卡住事件迴圈。
    openapi_spec = await asyncio.to_thread(
        run_spec_agent,
        java_jar_path=os.environ["JAVA_JAR_PATH"],
        java_base_url=os.environ["JAVA_BASE_URL"],
        java_executable_path=os.environ.get("JAVA_EXECUTABLE_PATH", "java"),
        spring_datasource_url=os.environ["SPRING_DATASOURCE_URL"],
        spring_datasource_username=os.environ["SPRING_DATASOURCE_USERNAME"],
        spring_datasource_password=os.environ["SPRING_DATASOURCE_PASSWORD"],
    )

    return {
        **state,
        "openapi_spec": openapi_spec,
    }
