"""
④ 骨架實作 Agent（scaffold_agent + translator-cli，骨架生成模式）
依 Agent ③ 的目錄結構與 interface 定義，建立目錄、base class、router 骨架、config；
db_models 由 scaffold_agent 從 Java entity 原始碼組出（見 08a_scaffold_agent_architecture.md）。
見 07a_translator_cli_architecture.md、07b_translator_cli_code.md、08a、08b。

平行分支 node：與 [P] Plan Agent 同以 record_tests／design 為共同前驅
（見 01 五章），只回傳自己的 key，不展開 state。
"""
import asyncio
import logging

from graph.state import RefactorState
from scaffold_agent import build_db_models
from translator_cli import client as translator_cli

logger = logging.getLogger(__name__)


async def run(state: RefactorState) -> dict:
    # build_db_models() 是純同步 CPU 運算（javalang 解析＋索引建構＋渲染，
    # 不呼叫任何 LLM／外部服務，見 08a 十二、十三章），必須用
    # asyncio.to_thread() 包一層，否則會佔住 event loop、讓同一個 superstep
    # 平行執行的 plan（[P]，async 呼叫 Claude API）退化成排隊（見 08a 十二章）。
    scaffold_result = await asyncio.to_thread(
        build_db_models,
        java_project_path=state["java_project_path"],
        module_list=state["module_list"],
    )

    result = await translator_cli.generate_scaffold(
        python_project_path=state["python_project_path"],
        python_structure=state["python_structure"],
        db_models=scaffold_result.db_models,
    )

    # 兩個不同粒度來源合併成同一份 RefactorState.skipped_db_models：
    # result["skipped_db_models"]（07a 四章，檔案級，{file_path, error}）
    # 補上 class_name=None；scaffold_result.skipped_entities（08a 九章，
    # entity 級，{file_path, class_name, error}）原樣併入——固定三欄位
    # schema，下游不需要用 .get() 防禦性判斷這個 dict 是哪一種來源
    # （見 08a 十二章）。
    skipped_db_models = [
        {**item, "class_name": None} for item in result["skipped_db_models"]
    ] + scaffold_result.skipped_entities

    if result["skipped_interfaces"] or skipped_db_models:
        logger.warning(
            "generate_scaffold() 有 %d 個 interface、%d 個 db_model 被跳過，"
            "見回傳值 skipped_interfaces／skipped_db_models（07a 四章、08a 九、十二章）",
            len(result["skipped_interfaces"]),
            len(skipped_db_models),
        )
    if not result["success"]:
        logger.error("generate_scaffold() 失敗：%s", result["error"])

    # 注意：scaffold 是平行分支 node，只回傳自己的 key，不展開 state
    return {
        "scaffold_done": result["success"],
        "skipped_interfaces": result["skipped_interfaces"],
        "skipped_db_models": skipped_db_models,
    }
