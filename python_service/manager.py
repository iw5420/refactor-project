"""容器化 Python 服務的模組層級單例與生命週期管理，供
`graph/nodes/implement_node.py`（⑤，啟動／等待／關閉）與
`refactor_harness/langgraph_nodes/test_nodes.py`（⑥，健康檢查失敗時讀取
診斷）共用——這是兩個不同 node 都需要碰觸的共用基礎設施，因此獨立成
套件層級的模組，不歸屬於任一個 node 檔案，比照 `refactor_harness/`／
`translator_cli/` 的既有慣例（見 10a 八章「診斷資料改走 State，不是
debug_agent/ 直接 import implement_node」）。
"""
from __future__ import annotations

import asyncio
import os

from python_service.java_properties import resolve_config_env_values
from python_service.process import PythonServiceContainer

# 模組層級單例，比照 implement_node.py 既有的 MODEL_SEMAPHORE 模式：整條
# graph run 只跑在單一 Python process 裡，這個變數在 implement／
# run_tests／debug → implement 重入之間持續存在（見 09a 三章「Python
# 服務只啟動一次」）。
_python_service: PythonServiceContainer | None = None


async def ensure_started(
    python_project_path: str,
    python_base_url: str,
    java_project_path: str | None = None,
    config_env_vars: list[dict[str, str]] | None = None,
) -> None:
    """真正第一次進入 implement 時呼叫：容器還沒起來就建立並啟動，已經
    起來（同一個 process 內的後續呼叫，或 debug → implement 重入）就不
    重複啟動。`PythonServiceContainer.start()` 內部已經包含輪詢就緒的
    邏輯，逾時會拋出明確例外（見 `python_service/process.py`）。

    `java_project_path`／`config_env_vars`：對應 docs/09b_bug_trace.md
    #46——`config_env_vars` 是 `state["python_structure"].get(
    "config_env_vars")`（③ 輸出，見 graph/state.py），沒有任何 @Value
    欄位的專案這個 key 不存在，兩個參數都給 `None`／預設值時
    `resolve_config_env_values()` 直接回傳空字典，行為等同這個機制完全
    不存在，不影響既有沒有用到 @Value 的專案。
    """
    global _python_service
    if _python_service is not None:
        return
    extra_env = (
        resolve_config_env_values(java_project_path, config_env_vars)
        if java_project_path and config_env_vars
        else {}
    )
    service = PythonServiceContainer(
        python_project_path=python_project_path,
        base_url=python_base_url,
        database_url=os.environ["DATABASE_URL"],
        extra_env=extra_env,
    )
    await asyncio.to_thread(service.start)
    _python_service = service


async def stop() -> None:
    """main.py 在整條 graph run 結束（不論成功或失敗）時呼叫一次，關閉
    並移除容器——Docker 容器不會隨 Python process 結束自動清理。
    """
    global _python_service
    if _python_service is None:
        return
    await asyncio.to_thread(_python_service.stop)
    _python_service = None


def get_diagnostics() -> str | None:
    """⑥ 健康檢查失敗時讀取（見 `run_postman_tests()`），寫進
    `state["service_diagnostics"]`；⑦（debug_agent/）全程只讀 State，
    不呼叫這個函式，見 10a 八章。
    """
    return _python_service.diagnostics if _python_service is not None else None
