"""
[P] Plan Agent（Claude API）
輸入：Agent ① 的模組清單 + Agent ③ 的架構設計
輸出：task_list
見 00_refactor_architecture.md 七/[P]
"""
from graph.state import RefactorState, TaskSpec


async def run(state: RefactorState) -> dict:
    # TODO: 實作 Claude API 呼叫，產生 task list
    # 注意：plan 是平行分支 node，只回傳自己的 key，不展開 state

    task_list: list[TaskSpec] = [
        {
            "id": "task_user_repo_001",
            "module": "user",
            "description": "實作 UserRepository.get_by_id() 方法",
            "target_files": ["app/repositories/user_repository.py"],
            "context": "根據 Java 的 getUserById 邏輯改寫",
            "depends_on": [],
        }
    ]

    return {"task_list": task_list}
