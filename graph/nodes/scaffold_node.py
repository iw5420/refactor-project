"""
④ 骨架實作 Agent（translator-cli，骨架生成模式）
依 Agent ③ 的目錄結構與 interface 定義，建立目錄、base class、router 骨架、config
見 07a_translator_cli_architecture.md、07b_translator_cli_code.md

平行分支 node：與 [P] Plan Agent 同以 record_tests／design 為共同前驅
（見 01 五章），只回傳自己的 key，不展開 state。
"""
import logging

from graph.state import RefactorState
from translator_cli import client as translator_cli

logger = logging.getLogger(__name__)


async def run(state: RefactorState) -> dict:
    # db_models 由④自行從既有 DB 或 Java entity 原始碼取得（見 07a 四章
    # 「db_models」一節），這部分屬於 08a（骨架實作 Agent 詳細設計，待
    # 建立）的範圍——07b 只接上 generate_scaffold() 本身，db_models 暫時
    # 固定傳 None，等 08a 落地時再補上真正的字典（見 07a 十二章）。
    result = await translator_cli.generate_scaffold(
        python_project_path=state["python_project_path"],
        python_structure=state["python_structure"],
        db_models=None,
    )

    # result["skipped_interfaces"]／["skipped_db_models"]（見 07a 四章）
    # 目前還沒有管道寫進 RefactorState——這是 07a 十二章、00 十章已知的
    # 結構性缺口，State 是否需要新增對應欄位留給 08a 評估，這裡先記警告
    # 供人工事後查閱，不阻擋 pipeline。
    if result["skipped_interfaces"] or result["skipped_db_models"]:
        logger.warning(
            "generate_scaffold() 有 %d 個 interface、%d 個 db_model 被跳過，"
            "見回傳值 skipped_interfaces／skipped_db_models（07a 四章、十二章）",
            len(result["skipped_interfaces"]),
            len(result["skipped_db_models"]),
        )
    if not result["success"]:
        logger.error("generate_scaffold() 失敗：%s", result["error"])

    # 注意：scaffold 是平行分支 node，只回傳自己的 key，不展開 state
    return {"scaffold_done": result["success"]}
