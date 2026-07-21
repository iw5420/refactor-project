"""
④ 骨架實作 Agent（translator-cli，骨架生成模式）
依 Agent ③ 的目錄結構與 interface 定義，建立目錄、base class、router 骨架、config
見 03a_translator_cli_architecture.md
"""
from graph.state import RefactorState


async def run(state: RefactorState) -> dict:
    # TODO: 實作呼叫 translator-cli 的骨架生成模式
    # translator_cli.generate_scaffold(python_structure)

    # 注意：scaffold 是平行分支 node，只回傳自己的 key，不展開 state
    return {"scaffold_done": True}
