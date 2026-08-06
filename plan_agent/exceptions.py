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
    `{module}_{layer}.py` 檔名格式。直接中止整條 plan run，交由人工
    核對③的輸出，不是可以重試化解的暫時性錯誤（見 06a 十一章）。
    """


class PlanAgentModuleError(Exception):
    """單一 module 的五章 Claude API 呼叫，在 `planning.py` 的重試佇列
    機制（待重試清單、5 分鐘後統一重試一次，比照 05a 六章）跑完仍失敗
    時拋出，中止整條 plan run（見 06a 五章：`task_list` 是⑤唯一輸入，
    任一 module 的 task 缺失會讓涵蓋率保證失效，風險遠高於重新執行
    一次）。
    """


class PlanAgentCoverageError(Exception):
    """八章涵蓋率驗證失敗時拋出——`python_structure.interfaces` 沒有被
    恰好一個 task 認領（缺漏或重複）。這是 `planning.py` 自己組裝邏輯
    該保證但沒保證到的不變量，不是需要人工判斷的模糊情況，也不進五章
    的 LLM 重試佇列（見 06a 八章、十一章）。

    正常執行路徑下理論上不會觸發：五章對每個 module 的 LLM 回應已經
    做過「回應的三元組集合必須與輸入完全一致」的核對（缺漏視同呼叫
    失敗、多餘的直接略過，見 `planning._plan_module()`），八章這裡是
    最後一道 defense-in-depth，不是用來擋一個已知會發生的情況（比照
    06a 十二章對 05a 多載消歧的同一種定位）。
    """
