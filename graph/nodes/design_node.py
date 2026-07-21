"""
③ 架構設計 Agent（Claude API）
輸入：Agent ① 的解析結果 + openapi.json
輸出：python_structure, route_to_file_mapping
"""
from graph.state import RefactorState, PythonStructure, InterfaceSpec, ParamSpec


async def run(state: RefactorState) -> RefactorState:
    # TODO: 實作 Claude API 呼叫，設計 Python 專案結構
    # 見 00_refactor_architecture.md 七/③

    python_structure: PythonStructure = {
        "directory_tree": "app/\n  repositories/\n  services/\n  routers/",
        "interfaces": [
            {
                "file_path": "app/repositories/user_repository.py",
                "class_name": "UserRepository",
                "function_name": "get_by_id",
                "params": [{"name": "user_id", "type": "int"}],
                "return_type": "User | None",
            }
        ],
    }

    route_to_file_mapping = {
        "GET_api_v1_users_{id}": ["app/routers/user_router.py", "app/services/user_service.py", "app/repositories/user_repository.py"]
    }

    return {
        **state,
        "python_structure": python_structure,
        "route_to_file_mapping": route_to_file_mapping,
    }
