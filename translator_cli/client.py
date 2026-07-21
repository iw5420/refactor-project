"""
translator-cli 的 Python 客戶端 stub
實際介面定義見 03a_translator_cli_architecture.md

這裡是 implement_node 呼叫的介面：
- fill_function(): 填空模式，用於 Agent ⑤
- generate_scaffold(): 骨架生成模式，用於 Agent ④（見 scaffold_node）

暫時用 stub 回傳假資料，實作見 03b_translator_cli_code.md
"""


class FillResult:
    def __init__(self, success: bool, error: str | None = None, diff: str = ""):
        self.success = success
        self.error = error
        self.diff = diff


async def fill_function(
    target_file: str,
    description: str,
    context_files: list[str],
) -> FillResult:
    """
    填空模式：給定目標檔案路徑 + 空函式簽名 + 描述，
    呼叫本地模型（qwen2.5-coder:32b via ollama）回傳函式本體，
    用 AST 精準插入原檔案。

    TODO: 實作呼叫 ollama HTTP API 的邏輯
    見 03a/03b

    Args:
        target_file: 相對路徑，如 "app/repositories/user_repository.py"
        description: task 描述，組裝進 prompt
        context_files: 該 task 需要的相關檔案清單（控制 context 大小）

    Returns:
        FillResult: success, error, diff（用於 regression 偵測）
    """
    # stub
    return FillResult(success=True, error=None, diff="")


async def generate_scaffold(python_structure: dict) -> dict:
    """
    骨架生成模式：給定 Agent ③ 產出的 PythonStructure，
    呼叫本地模型從零生成整個目錄結構、基礎 class、空函式簽名、config 等。

    TODO: 實作呼叫 ollama HTTP API 的邏輯
    見 03a/03b

    Args:
        python_structure: Agent ③ 的 PythonStructure (含 directory_tree + interfaces)

    Returns:
        {success: bool, error: str | None}
    """
    # stub
    return {"success": True, "error": None}
