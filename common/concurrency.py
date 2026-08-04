"""跨 Agent 共用的併發數預設值計算，見 00 六章「Claude API 端的大範圍語意判斷：map-reduce 模式」。

Map 階段可平行呼叫 Claude API 的場景（[B] Collection Agent 的鏈式依賴偵測、
① 解析 Agent 的 Controller 批次摘要）都採同一條政策：可用核心數 − 1，
執行期動態計算、不寫死，避免佔滿本機其餘資源（同一台機器通常還跑著
translator-cli 的本地模型）。這條政策原本在兩份文件裡各自複述一次，
集中到這裡後，日後其他 Agent 有平行呼叫 Claude API 的併發數需求時
直接呼叫這個函式，不需要重新推導。
"""
from __future__ import annotations

import os


def default_concurrency() -> int:
    cpu_count = os.cpu_count() or 1
    return max(1, cpu_count - 1)
