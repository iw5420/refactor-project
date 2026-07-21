"""
translator-cli 的型別定義
"""
from typing import TypedDict


class FillResult(TypedDict):
    """fill_function() 的回傳型別"""
    success: bool
    error: str | None
    diff: str  # 用於 regression 偵測
