# parse_agent/exceptions.py
"""① 解析 Agent 例外階層，對應 04a 九章「錯誤處理範圍」。呼叫圖建構
（三章）的失敗一律視為輸入端問題，直接往上拋、中止整條 LangGraph run，
不在這裡額外包裝——javalang 拋出的 `JavaSyntaxError`／檔案 I/O 例外原樣
往上傳即可，呼叫端不需要特別分類。這裡只定義 Map/Reduce（四章）專屬的
例外，因為那裡有明確的「重試佇列、重試仍失敗才視為硬性失敗」中間狀態，
需要一個專屬型別承接。
"""
from __future__ import annotations


class ParseAgentMapReduceError(Exception):
    """Map 或 Reduce 階段的 Claude API 呼叫，在 summarize.py 的重試佇列
    機制（見 04a 四章、summarize.py `run_map_phase_with_retry()`）跑完
    仍有分組失敗時拋出，中止整條 run（見 04a 十章、summarize.py
    `run_map_phase_with_retry()` 模組說明的保守預設理由）。日後若要改成
    「缺摘要繼續跑」，只需要改 `run_map_phase_with_retry()` 內部處理，
    不需要動這個例外型別本身。
    """
