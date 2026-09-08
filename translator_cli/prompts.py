# translator_cli/prompts.py
"""system／user prompt 模板，對應 07a 七章「System / User Prompt 組裝」。
qwen／Claude 兩條路徑各自的 system prompt 固定不變（只定義輸出格式契約，
不含業務內容），user prompt 依每次呼叫組裝，兩條路徑共用同一個
`build_user_prompt()`。

不吃 `description`（06a 五章已定調改為純機械模板，只供 log 用途，不是
模型輸入）——⑤ 直接讀 `java_source`（這個函式所在的整個 Java 檔案真實
原始碼，見 07a 五章「`java_source` 是整個檔案」）與 `referenced_source`
（呼叫鏈展開出的真實原始碼，Java 或已翻譯 Python），見 07a 五章。兩條
路徑的 system prompt 都明確加入「呼叫，不要內嵌」核心指示（見 06a 六章
「同層 Java 參照的用途」）——`referenced_source` 是給模型看「該怎麼正確
呼叫」，不是給它「拿去合併改寫」。
"""
from __future__ import annotations

from typing import Any

from translator_cli.types import ReferencedSourceItem

BODY_START = "<<<TRANSLATOR_CLI_BODY_START>>>"
BODY_END = "<<<TRANSLATOR_CLI_BODY_END>>>"

_CALL_NOT_INLINE_INSTRUCTION = """你的任務：把對應的 Java 邏輯改寫成正確的 Python 實作。你收到的其他函式原始碼是給你參考
「該怎麼呼叫它」（參數、回傳型別、行為），不是要你把那些函式的邏輯合併或複製進來——你的
實作應該真正呼叫（import + invoke）那些函式，就像原始 Java 程式碼本來就是這樣呼叫的一樣。"""

SYSTEM_PROMPT_QWEN = f"""你是一個 Python 程式碼填空工具。你會收到一個已經定義好簽名的 Python 函式（骨架已建好，
可能是空的 `pass`，也可能已有既有邏輯），以及這個函式對應的真實原始碼、以及它呼叫到的其他
函式的真實原始碼（Java 或已翻譯完成的 Python）。

{_CALL_NOT_INLINE_INSTRUCTION}

只回傳這個函式的「本體」陳述式，不要重複函式簽名、不要重複裝飾器、不要加任何解說文字。
也不要在程式碼裡用 `#` 寫任何註解——這個工具用 AST 處理輸出，`#` 註解一律會被直接丟棄，
不會出現在最終寫入的檔案裡，寫了也是白費。回傳格式固定如下，只在這兩個標記之間寫程式碼，
標記本身也要原樣附上：

{BODY_START}
（函式本體陳述式，視為第一層縮排，不要自己加縮排）
{BODY_END}
"""

SYSTEM_PROMPT_CLAUDE = f"""你是一個 Python 程式碼填空工具。你會收到一個已經定義好簽名的 Python 函式（骨架已建好，
可能是空的 `pass`，也可能已有既有邏輯），以及這個函式對應的真實原始碼、以及它呼叫到的其他
函式的真實原始碼（Java 或已翻譯完成的 Python）。

{_CALL_NOT_INLINE_INSTRUCTION}

只回傳這個函式的「本體」陳述式，不要重複函式簽名、不要重複裝飾器、不要加任何解說文字。
也不要在程式碼裡用 `#` 寫任何註解——這個工具用 AST 處理輸出，`#` 註解一律會被直接丟棄，
不會出現在最終寫入的檔案裡，寫了也是白費。
"""

# 對應 07a 七章「Claude 路徑：連線方式」，Structured Outputs 的
# constrained decoding schema——`body_statements` 就是 qwen 路徑 delimiter
# 抽取出來的同一份 body_text，兩條路徑取得之後走同一套六章驗證／替換流程。
BODY_STATEMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "body_statements": {"type": "string"},
    },
    "required": ["body_statements"],
    "additionalProperties": False,
}


def _format_referenced_source(items: list[ReferencedSourceItem]) -> str:
    blocks = []
    for item in items:
        label = f"{item['class_name']}.{item['function_name']}" if item["class_name"] else item["function_name"]
        lang = "Java" if item["language"] == "java" else "已翻譯 Python"
        blocks.append(f"### {item['file_path']} :: {label}（{lang}）\n```\n{item['source']}\n```")
    return "\n\n".join(blocks)


def build_user_prompt(
    *,
    current_signature: str,
    java_source: str,
    referenced_source: list[ReferencedSourceItem],
    context: str,
    context_files: list[tuple[str, str]],
    error_feedback: str | None = None,
) -> str:
    """對應 07a 七章「User / System Prompt 組裝」：函式目前的簽名（不含
    body）、`java_source`、`referenced_source`（逐項列出 `file_path` /
    `class_name` / `function_name` / `language` + 原始碼內容）、`context`
    （若非空）、`context_files` 逐檔案列出「路徑 + 完整內容」。`description`
    不放進這裡（見 06a 五章、07a 五章，純 log 用途）。`error_feedback`
    非 `None` 時，額外附上七章「兩條路徑共用：body_text 格式修正重試」
    規定的錯誤回饋文字。
    """
    parts = [
        f"函式目前的簽名：\n```python\n{current_signature}\n```",
        f"這個函式對應的原始 Java 方法：\n```\n{java_source}\n```",
    ]
    if referenced_source:
        parts.append(
            "這個函式呼叫到的其他函式的真實原始碼（import 路徑必須逐字複製每筆標題裡的 "
            f"file_path，不得自行另猜路徑）：\n\n{_format_referenced_source(referenced_source)}"
        )
    if context:
        parts.append(f"補充說明：\n{context}")
    if context_files:
        file_blocks = "\n\n".join(f"### {path}\n```python\n{content}\n```" for path, content in context_files)
        parts.append(f"相關檔案內容：\n\n{file_blocks}")
    if error_feedback:
        parts.append(
            f"上一次回應違反格式／語法錯誤，錯誤訊息是：{error_feedback}，請重新產生"
        )
    return "\n\n".join(parts)
