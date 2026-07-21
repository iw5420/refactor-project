"""
[B] Collection Agent（程式邏輯 + LLM）
將 OpenAPI JSON 轉換為 Postman Collection（readonly / mutation）
見 02a_harness_architecture.md
"""
from graph.state import RefactorState


async def run(state: RefactorState) -> RefactorState:
    # TODO: 實作 OpenAPI → Postman Collection 轉換
    # 使用 openapi-to-postmanv2 工具

    collection_readonly_path = "postman/collection_readonly.json"
    collection_mutation_path = "postman/collection_mutation.json"

    return {
        **state,
        "collection_readonly_path": collection_readonly_path,
        "collection_mutation_path": collection_mutation_path,
    }
