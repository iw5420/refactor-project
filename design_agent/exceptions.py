# design_agent/exceptions.py
"""③ 架構設計 Agent 例外階層，對應 05a 十二章「錯誤處理範圍」。Java
原始碼再掃描（四章）失敗一律視為輸入端問題，直接往上拋、中止整條
LangGraph run，不在這裡額外包裝——javalang 拋出的 `JavaSyntaxError`／
檔案 I/O 例外原樣往上傳即可。這裡定義六章 LLM 設計階段專屬的四種硬性
失敗：單一 module 呼叫重試仍失敗、depends_on 出現循環依賴、depends_on
引用不存在的 module 名稱、`module_list.methods` 對不到任何真實 Java
方法（`DesignAgentCoverageError`，見該類別 docstring「與 05b 原始設計
的差異」）。
"""
from __future__ import annotations


class DesignAgentModuleError(Exception):
    """單一 module 的六章 Claude API 呼叫，在 design.py 的重試佇列機制
    （待重試清單、5 分鐘後統一重試一次，比照 04a 四章）跑完仍失敗時
    拋出，中止整條 design run（見 05a 十二章：`python_structure` 是
    [P]／④ 唯一的權威規格，任何一個 module 的介面缺失都會讓兩者在不
    知情狀況下對著不完整規格工作，風險遠高於重新執行一次——比 04a
    允許缺摘要繼續跑更嚴格，見 05a 六章「單一 module 呼叫失敗時」）。
    """


class DesignAgentCycleError(Exception):
    """`module_list.depends_on` 拓樸排序時偵測到循環依賴時拋出，中止
    整條 design run，交由人工排查（見 05a 六章「循環依賴」）——不嘗試
    自動打斷環或猜測合理順序：`depends_on` 理論上應為 DAG（04a Reduce
    階段、⑤ 排程器都依賴同一假設），真的出現循環代表上游輸入資料本身
    有錯誤，不是這裡能安全修復的情況。

    只在`depends_on` 引用的 module 名稱**都確實存在**於 `module_list`
    的前提下才會拋出——引用不存在的 module 名稱是另一種輸入錯誤（見
    `DesignAgentUnknownDependencyError`），不會走到這裡：`layout.
    build_waves()` 會先做過一輪存在性檢查，避免「依賴引用了拼錯或不
    存在的 module 名稱」被誤判成循環依賴，讓除錯方向對不上真正的
    根因（缺依賴 vs. 真的循環）。
    """


class DesignAgentUnknownDependencyError(Exception):
    """`module_list.depends_on` 引用了不存在於 `module_list` 裡任何一個
    `module` 名稱時拋出（見 `layout.build_waves()`）——常見成因是①的
    Reduce 階段拼錯依賴的 module 名稱（04a 四章「missing_classes 只檢查
    一個方向」的姊妹情況，這裡換成 module 名稱層級）。跟循環依賴
    （`DesignAgentCycleError`）刻意分開成兩種例外：兩者的根因（缺依賴
    vs. 依賴形成環）不同，共用同一個例外訊息會讓人工排查時先入為主
    地往「循環」方向找，實際上問題出在①的輸出資料本身。
    """


class DesignAgentCoverageError(Exception):
    """`_build_method_contexts()` 在 `module_list.methods` 裡的一筆方法
    找不到對應的真實 Java class／method 時拋出（`class_signatures.
    get(method_info["class_name"])` 是 `None`，或該 class 底下找不到
    同名方法），中止整條 design run，不進 LLM 呼叫重試佇列。

    **不是 LLM 呼叫失敗，不該走 `LlmJsonError` 那條重試路線**：這個
    情況發生在呼叫 Claude API 之前（`_build_method_contexts()` 是純
    機械比對），根因是①的輸出（`module_list.methods`）跟③這次重新
    掃描 Java 原始碼的結果對不上——同一份 `java_project_path`，理論上
    應該永遠對得上，對不上代表某處有結構性錯誤（例如 `signature_scan.
    scan_java_files()` 漏掃了某種 Java 語法結構，見之前修過的 interface
    掃描缺漏案例）。重跑同一次 Claude API 呼叫解決不了「程式碼掃描邏輯
    有 bug」這種問題，5 分鐘後重試只是白等，因此刻意用獨立的例外型別，
    確保不會被 `_run_wave_batch()` 的 `except LlmJsonError` 攔截、送進
    重試佇列。

    **與 05b 原始設計的差異**：05b 原本的決定是「記警告並跳過，不中止
    整條 design run」（理由：「理論上不該發生」）。實測對真實
    `lang-exam-api-refactor` 專案跑出這個情況真的會發生，且發生時
    影響幅度不小（一次涵蓋 26% 的方法）——根本原因後來確認是
    `signature_scan.py` 沒有掃描 Java `interface` 宣告，已經修好；但
    「找不到對應」這個分支本身沒有變，若之後又出現另一種目前沒遇過的
    Java 語法造成同樣的落空，還是會被原本的「跳過＋警告」悄悄吃掉，
    只留在容易被忽略的 log 裡。因此改成立即中止＋在錯誤訊息裡列出
    精確的 module／class／method 清單，讓這類問題無法再悄悄流入不完整
    的 `python_structure`。
    """
