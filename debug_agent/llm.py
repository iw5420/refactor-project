"""⑦ Debug Agent 專屬的 Claude API 模型選擇與輸出長度上限。實際呼叫邏輯
（client 初始化、Structured Outputs、log_usage() 整合、錯誤處理）在
common/llm_client.py，所有需要呼叫 Claude API 的 Agent 共用同一份（見
00 六章）。debug_agent/ 全套件（這個檔案、analysis.py）嚴禁自行初始化
Anthropic client 或另開一條呼叫路徑——所有 Claude API 呼叫一律經
common.llm_client.call_claude_for_json()，這是 00 六章的硬性規定，不是
建議：唯有如此 record_llm_call() 才會被統一呼叫到，API 呼叫才會確實
記進 llm_traces.db，不會因為漏接而算不準（見 11a 七、八章）。

DEBUG_AGENT_MAX_TOKENS 不沿用 common/llm_client.py 的 DEFAULT_MAX_TOKENS
（4096）：10a 八章「⑦ 直接產生修正後程式碼」之後，task_fixes 每一筆的
`fixed_body` 是完整函式本體（不是一句自然語言指令），`file_fixes`（phase
2）甚至可能帶完整函式定義，輸出密度大幅提高，一個 module 有多個
task_fixes 時很容易逼近甚至超過 4096——理由同 06a 五章
PLAN_AGENT_MAX_TOKENS 的既有判斷方式。8192 這個數字目前只有一筆新
schema 的真實測量支持：4 個 task_fixes（其中一筆含 file_fixes）實測
`in=7242／out=1355` token，遠低於上限，暫不需要調整；但 task_fixes／
file_fixes 數量明顯更多的大型 module 是否會逼近或超過這個上限仍未實測，
待接上真實環境累積更多案例後再校準。
"""
from __future__ import annotations

import os

from common.llm_client import DEFAULT_MODEL_FALLBACK

DEFAULT_MODEL = os.environ.get("DEBUG_AGENT_MODEL", DEFAULT_MODEL_FALLBACK)
DEBUG_AGENT_MAX_TOKENS = int(os.environ.get("DEBUG_AGENT_MAX_TOKENS", "8192"))
