# translator_cli/prompts.py
"""system／user prompt 模板，對應 07a 七章「System / User Prompt 組裝」。
system prompt 固定不變（只定義輸出格式契約，不含業務內容），user prompt
依每次呼叫組裝。
"""
from __future__ import annotations

BODY_START = "<<<TRANSLATOR_CLI_BODY_START>>>"
BODY_END = "<<<TRANSLATOR_CLI_BODY_END>>>"

SYSTEM_PROMPT = f"""你是一個 Python 程式碼填空工具。你會收到一個已經定義好簽名的 Python 函式（骨架已建好，
可能是空的 `pass`，也可能已有既有邏輯），以及這個函式應該做什麼的描述。

你的任務：只回傳這個函式的「本體」陳述式，不要重複函式簽名、不要重複裝飾器、不要加任何
解說文字。也不要在程式碼裡用 `#` 寫任何註解——這個工具用 AST 處理輸出，`#` 註解一律會被
直接丟棄，不會出現在最終寫入的檔案裡，寫了也是白費。回傳格式固定如下，只在這兩個標記之間
寫程式碼，標記本身也要原樣附上：

{BODY_START}
（函式本體陳述式，視為第一層縮排，不要自己加縮排）
{BODY_END}
"""


def build_user_prompt(
    *,
    current_signature: str,
    description: str,
    context: str,
    context_files: list[tuple[str, str]],
    error_feedback: str | None = None,
) -> str:
    """對應 07a 七章「User prompt 組裝內容」：函式目前的簽名（不含
    body）、`description`、`context`（若非空）、`context_files` 逐檔案
    列出「路徑 + 完整內容」。`error_feedback` 非 `None` 時，額外附上
    七章「模型輸出格式錯誤的修正重試」規定的錯誤回饋文字。
    """
    parts = [
        f"函式目前的簽名：\n```python\n{current_signature}\n```",
        f"這個函式該做什麼：\n{description}",
    ]
    if context:
        parts.append(f"補充說明：\n{context}")
    if context_files:
        file_blocks = "\n\n".join(f"### {path}\n```python\n{content}\n```" for path, content in context_files)
        parts.append(f"相關檔案內容：\n\n{file_blocks}")
    if error_feedback:
        parts.append(
            f"上一次回應違反格式／語法錯誤，錯誤訊息是：{error_feedback}，請重新產生，務必遵守 delimiter 格式"
        )
    return "\n\n".join(parts)
