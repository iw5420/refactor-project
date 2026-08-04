# parse_agent/llm.py
"""① 解析 Agent 專屬的 Claude API 模型選擇。實際呼叫邏輯（client 初始化、
Structured Outputs、log_usage() 整合、錯誤處理）在 common/llm_client.py，
所有需要呼叫 Claude API 的 Agent 共用同一份（見 00 六章）。這個檔案只
負責一件事：① 用哪個模型，不跟 [B] 共用同一個 `SPEC_COLLECTION_AGENT_MODEL`。
"""
from __future__ import annotations

import os

from common.llm_client import DEFAULT_MODEL_FALLBACK

DEFAULT_MODEL = os.environ.get("PARSE_AGENT_MODEL", DEFAULT_MODEL_FALLBACK)
