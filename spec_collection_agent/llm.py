"""[B] Collection Agent 專屬的 Claude API 模型選擇。實際呼叫邏輯（client
初始化、Structured Outputs、log_usage() 整合、錯誤處理）已集中在
`common/llm_client.py`，所有需要呼叫 Claude API 的 Agent 共用同一份，
不再各自重複實作（見 00 六章）。這個檔案只負責一件事：[B] 用哪個模型。
"""
from __future__ import annotations

import os

from common.llm_client import DEFAULT_MODEL_FALLBACK

# 三個呼叫端（singleton 分組、map、reduce）共用這一個環境變數，
# 不寫死模型字面值；下面只是沒設環境變數時的保底 fallback。
DEFAULT_MODEL = os.environ.get("SPEC_COLLECTION_AGENT_MODEL", DEFAULT_MODEL_FALLBACK)
