"""
① 解析 Agent（Claude API）
輸入：Java 專案路徑
輸出：module_list, api_to_python_target
"""
from graph.state import RefactorState, ModuleInfo, ApiMapping, MethodInfo


async def run(state: RefactorState) -> RefactorState:
    # TODO: 實作 Claude API 呼叫，解析 Java 專案
    # 暫時用 stub 回傳固定資料，確認圖的邊與 reducer 跑通

    module_list: list[ModuleInfo] = [
        {
            "module": "user",
            "java_files": ["UserRepository.java", "UserService.java", "UserController.java"],
            "python_files": ["app/repositories/user_repository.py", "app/services/user_service.py", "app/routers/user_router.py"],
            "depends_on": [],
            "methods": [
                {
                    "java_method": "getUserById",
                    "python_method": "get_by_id",
                    "description": "根據 ID 查詢使用者",
                    "complexity": "low",
                }
            ],
        }
    ]

    api_to_python_target: list[ApiMapping] = [
        {
            "endpoint": "/api/v1/users/{id}",
            "http_method": "GET",
            "java_controller": "UserController.getUserById",
            "python_target": "app/routers/user_router.py::get_user",
            "module": "user",
        }
    ]

    return {
        **state,
        "module_list": module_list,
        "api_to_python_target": api_to_python_target,
    }
