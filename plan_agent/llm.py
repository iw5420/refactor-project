# plan_agent/llm.py
"""[P] Plan Agent 專屬的 Claude API 模型選擇與輸出長度上限。實際呼叫
邏輯（client 初始化、Structured Outputs、log_usage() 整合、錯誤處理）在
common/llm_client.py，所有需要呼叫 Claude API 的 Agent 共用同一份（見
00 六章）。這個檔案只負責 [P] 自己的決策：用哪個模型、`max_tokens` 要
多少，不跟其他 Agent 共用同一個環境變數或同一個數字。

`PLAN_AGENT_MAX_TOKENS` 不用 `common/llm_client.DEFAULT_MAX_TOKENS`
（4096）：[P] 每個 task 都帶完整業務描述／context／依賴清單，輸出密度
遠高於其他 Agent 的分類型輸出。實測對真實 lang-exam-api-refactor 專案
最大的 module（27 個 interfaces）跑過，完整回應約需 5,591 token（用
Anthropic `count_tokens()` 對截斷前的部分回應實測換算，不是猜的），
4096 會被截斷成不合法的 JSON。8192 約為實測值的 1.5 倍，留有餘裕應付
同一 module 不同次呼叫的回應長度波動，以及比這次目標專案更大的 module。
"""
from __future__ import annotations

import os

from common.llm_client import DEFAULT_MODEL_FALLBACK

DEFAULT_MODEL = os.environ.get("PLAN_AGENT_MODEL", DEFAULT_MODEL_FALLBACK)
PLAN_AGENT_MAX_TOKENS = int(os.environ.get("PLAN_AGENT_MAX_TOKENS", "8192"))
