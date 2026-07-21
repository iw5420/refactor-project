"""
[A] Spec Agent（程式邏輯）
啟動 Java 服務，取得 OpenAPI 3.0 JSON
"""
from graph.state import RefactorState


async def run(state: RefactorState) -> RefactorState:
    # TODO: 實作 Java 服務啟動、OpenAPI 取得邏輯
    # 見 02a_harness_architecture.md

    openapi_spec = {
        "openapi": "3.0.0",
        "info": {"title": "Java Service", "version": "1.0.0"},
        "paths": {
            "/api/v1/users/{id}": {
                "get": {
                    "operationId": "getUser",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                    "responses": {"200": {"description": "User found"}},
                }
            }
        },
    }

    return {
        **state,
        "openapi_spec": openapi_spec,
    }
