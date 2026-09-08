# translator_cli/claude_client.py
"""Claude API 連線與請求契約，對應 07a 七章「Claude 路徑：連線方式」。
與 `ollama_client.py` 平行的第二條 `get_function_body()` 路徑，串接既有的
`common/llm_client.py::call_claude_for_json()`（[③]／舊版 [P] 已在用的
同一支共用 wrapper，含 `llm_trace.py` 呼叫紀錄），不新增另一套 Claude
連線邏輯、不重複實作 trace 記錄——`call_claude_for_json()` 內部已經做好
`record_llm_call_start()`／`record_llm_call()`，這裡不需要像
`ollama_client.py` 那樣自己再包一層。

**不受 qwen 那種單模型號誌限制**：Claude API 是雲端服務，多個 task 平行
呼叫模型不衝突，這是導入雙後端本來要換到的效能收益（見 07a 七章「Claude
路徑：連線方式」）。寫入磁碟／commit 那一小段仍然跨 backend 序列化，見
`git_ops.py::WRITE_LOCK`、`client.py::fill_function()`。
"""
from __future__ import annotations

import asyncio
import logging
import os

from common.llm_client import DEFAULT_MODEL_FALLBACK, call_claude_for_json
from translator_cli import python_adapter
from translator_cli.exceptions import (
    TranslatorCliModelOutputError,
    TranslatorCliNetworkError,
)
from translator_cli.ollama_client import FORMAT_RETRY_COUNT
from translator_cli.prompts import (
    BODY_STATEMENTS_SCHEMA,
    SYSTEM_PROMPT_CLAUDE,
    build_user_prompt,
)
from translator_cli.types import ReferencedSourceItem

logger = logging.getLogger(__name__)

# ③ design_agent 既有慣例（見 design_agent/llm.py）：每個呼叫端各自的
# 環境變數，不跟其他 Agent 共用同一個，方便個別調整／個別限流。
CLAUDE_MODEL = os.environ.get("TRANSLATOR_CLI_CLAUDE_MODEL", DEFAULT_MODEL_FALLBACK)


async def get_function_body(
    *,
    current_signature: str,
    java_source: str,
    referenced_source: list[ReferencedSourceItem],
    context: str,
    context_files: list[tuple[str, str]],
    function_name: str,
    task_id: str | None = None,
    target_file: str | None = None,
    class_name: str | None = None,
    run_id: str,
) -> str:
    """對外唯一入口，跟 `ollama_client.get_function_body()` 是同一份契約
    （見 07a 七章「分派方式」：`client.py::fill_function()` 依
    `translator_backend` 二選一呼叫，取得 `body_text` 之後兩條路徑走
    同一套六章驗證／替換流程）。

    固定重試 `FORMAT_RETRY_COUNT`（=2，跟 qwen 路徑共用同一個上限，從
    `ollama_client.py` import，見 07a 七章「兩條路徑共用：body_text
    格式修正重試」）次格式修正：`call_claude_for_json()` 的 Structured
    Outputs 已經保證回應是合法 JSON、帶 `body_statements` 欄位，不會有
    qwen 路徑那種「找不到 delimiter」的失敗模式，這裡只需要驗證
    `body_statements` 的內容本身（`ast.parse()`／空 body／簽名重複輸出，
    見 07a 六章步驟 6a／6b，跟 qwen 共用同一份
    `python_adapter.extract_body_statements()`）。

    **API／網路層錯誤不重試**：`call_claude_for_json()` 本身沒有內建
    重試（雲端服務，不像 qwen 那樣需要應付 LAN 內部連線抖動），失敗
    （`LlmJsonError`）直接轉成 `TranslatorCliNetworkError` 往外拋，不進
    這裡的格式修正迴圈——理由同 `ollama_client.get_function_body()`
    既有原則：網路層錯誤救不回來，格式修正重試解決不了這個問題。這是
    07a 十四章「待決定事項」列出的已知簡化，尚未有真實 Claude API 資料
    驗證是否需要另外加一層重試。

    `call_claude_for_json()` 是同步函式（見 `common/llm_client.py`），
    用 `asyncio.to_thread()` 丟到執行緒池，不佔住 event loop——這也是
    Claude 路徑天然不受單一號誌限制的原因：多個 task 的
    `asyncio.to_thread()` 呼叫可以真正併發跑在不同執行緒。
    """
    error_feedback: str | None = None
    last_error: Exception | None = None

    for attempt in range(FORMAT_RETRY_COUNT + 1):
        user_prompt = build_user_prompt(
            current_signature=current_signature,
            java_source=java_source,
            referenced_source=referenced_source,
            context=context,
            context_files=context_files,
            error_feedback=error_feedback,
        )

        try:
            response = await asyncio.to_thread(
                call_claude_for_json,
                system_prompt=SYSTEM_PROMPT_CLAUDE,
                user_prompt=user_prompt,
                schema=BODY_STATEMENTS_SCHEMA,
                model=CLAUDE_MODEL,
                run_id=run_id,
                task_id=task_id,
                target_file=target_file,
                class_name=class_name,
                function_name=function_name,
            )
        except Exception as exc:
            # call_claude_for_json() 對已知的 anthropic.APIError／逾時／
            # JSON 解析失敗都轉成 LlmJsonError，但它自己的兜底分支（見
            # common/llm_client.py 該函式最後一個 except）對真正意外的
            # SDK 內部錯誤是原樣往外拋、不轉型別——這裡用廣義 Exception
            # 接住，統一轉成 TranslatorCliNetworkError，不讓任何原生例外
            # 洩漏擊穿 translator-cli 的錯誤回報契約（見 07a 十三章）。
            raise TranslatorCliNetworkError(f"Claude API 呼叫失敗：{exc}") from exc

        body_source = response["body_statements"]
        try:
            python_adapter.extract_body_statements(body_source, function_name)
            return body_source
        except TranslatorCliModelOutputError as exc:
            last_error = exc
            error_feedback = str(exc)
            if attempt < FORMAT_RETRY_COUNT:
                logger.warning("模型輸出格式錯誤（第 %d 次），重新呼叫一次修正：%s", attempt + 1, exc)
                continue

    raise TranslatorCliModelOutputError(f"模型輸出格式錯誤，修正重試仍失敗：{last_error}")
