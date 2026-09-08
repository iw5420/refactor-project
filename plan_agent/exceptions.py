# plan_agent/exceptions.py
"""[P] Plan Agent 例外階層，對應 06a 十一章「錯誤處理範圍」。比照
design_agent/exceptions.py 的既有先例——05a 十章／06a 九章的套件結構都
沒有列出這個檔案，是落地時必然需要、但不屬於設計文件決策範圍的基礎
設施檔案（見 05b 開頭「補上四個檔案」的同一種說明）。
"""
from __future__ import annotations


class PlanAgentModuleLookupError(Exception):
    """`InterfaceSpec.file_path` 反查不出 module（06a 四章
    `module_index.classify()`）時拋出——代表③輸出違反自己承諾的
    `{module}_{layer}.py` 檔名格式（且不落在 `app/utils/`／
    `app/core/exception_handlers.py` 這兩個已知特例）。直接中止整條
    plan run，交由人工核對③的輸出，不是可以重試化解的暫時性錯誤（見
    06a 十一章）。
    """


class PlanAgentCoverageError(Exception):
    """八章涵蓋率驗證失敗時拋出——`python_structure.interfaces` 沒有被
    恰好一個 task 認領（缺漏或重複）。[P] 不再呼叫 Claude API（見 06a
    五章），涵蓋率單純由「對每個 `InterfaceSpec` 產生恰好一個 task」的
    迴圈結構保證，理論上不會觸發；這裡是最後一道 defense-in-depth，
    專門攔截 `python_structure.interfaces` 本身就存在重複三元組（③輸出
    的正確性缺陷）這種上游輸入問題，不是需要人工判斷的模糊情況（見
    06a 八章、十一章）。
    """
