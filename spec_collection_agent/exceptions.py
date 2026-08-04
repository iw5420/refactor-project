"""[A]/[B] 例外階層。對應 03a 六章 `retry_count` 邊界：這裡的例外一律視為
硬性失敗，直接往上拋、中止整條 pipeline。唯一例外是人工標記
`Decision.SKIP` 或尚未填值的 endpoint，這不算例外，是排除、記錄進
`unfilled_endpoints.json`（見 manual_fill.py、03a 三章「人工填值機制」）。
"""
from __future__ import annotations


class JavaServiceError(Exception):
    """[A] Spec Agent 的基底例外。"""


class DbUnreachableError(JavaServiceError):
    """啟動前的 DB TCP 連線快篩失敗（03a 二章「服務生命週期管理」步驟 0）。"""


class JavaServiceStartupTimeout(JavaServiceError):
    """逾時預算內沒有拿到合法的 `/v3/api-docs` 回應（03a 二章「就緒判定與擷取合一」）。"""

    def __init__(self, message: str, last_error: str | None, diagnostics: str) -> None:
        super().__init__(message)
        self.last_error = last_error
        self.diagnostics = diagnostics

    def __str__(self) -> str:  # pragma: no cover - 純格式化
        base = super().__str__()
        return f"{base}\n最後一次候選錯誤: {self.last_error}\n---- 進程輸出（最後數行）----\n{self.diagnostics}"


class CollectionConversionError(Exception):
    """`openapi-to-postmanv2` 轉換失敗（非 0 結束碼），見 03a 三章「轉換流程」。"""

    def __init__(self, message: str, stderr: str) -> None:
        super().__init__(message)
        self.stderr = stderr


class ChainDependencyDetectionError(Exception):
    """鏈式依賴偵測（map 或 reduce 任一階段）的 LLM 呼叫失敗或回傳格式
    不合法；沒有局部排除的粒度，任一階段失敗視為整個偵測失敗（見 03a
    六章）。map 階段單筆候選項格式不合法屬例外——那是丟棄該筆、記
    warning，不觸發這個例外（候選清單是 best-effort 中間產物，見四、
    4.4）。
    """
