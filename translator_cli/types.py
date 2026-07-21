"""
translator-cli 的型別定義

FillResult 定義統一在 client.py，此模組只 re-export
"""
from translator_cli.client import FillResult

__all__ = ["FillResult"]
