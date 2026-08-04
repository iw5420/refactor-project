"""跨 Agent 共用的「依累積字元數切批次」演算法，見 00 六章。

[B] Collection Agent 的鏈式依賴偵測（依 tag 切 operation 子批次）與
① 解析 Agent 的 Map 階段（依字元預算切 class 批次）各自需要同一種切批次
演算法：依序累加項目的字元數，一旦加上下一個項目會超過門檻就先切一批，
單一項目本身超過門檻時仍自成一批（不繼續往下拆）。這條演算法原本在兩個
Agent 底下各自維護一份幾乎相同的實作，集中到這裡後不再各自重寫；每個
Agent 仍自行決定門檻值要用哪個環境變數、預設值多少，這裡只負責切批次
本身。
"""
from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")


def chunk_by_char_budget(items: list[T], size_of: Callable[[T], int], budget: int) -> list[list[T]]:
    chunks: list[list[T]] = []
    current: list[T] = []
    current_chars = 0
    for item in items:
        item_chars = size_of(item)
        if current and current_chars + item_chars > budget:
            chunks.append(current)
            current, current_chars = [], 0
        current.append(item)
        current_chars += item_chars
    if current:
        chunks.append(current)
    return chunks
