"""跨 Agent 共用的 Java class annotation 判斷（Lombok／JPA 資料類別偵測），見 00 六章。

① 解析 Agent（`parse_agent/grouping.py` 的 `needs_llm_summary()`）與 ③ 架構
設計 Agent（`design_agent/design.py` 的孤兒類別／資料容器占位判斷）都需要
判斷「這個 class 是不是 Lombok／JPA 標記的資料容器」，是同一件事，不是恰好
想法一致——比照 `common/chunking.py`／`common/concurrency.py` 的既有原則
集中到這裡，不各自維護一份，避免日後專案裡多用一個新 annotation（如
`@Builder`）時，改的人只記得改其中一份，兩個 Agent 的判斷悄悄不一致。

`DATA_CLASS_ANNOTATIONS` 只是「大概率是資料容器」的觸發訊號，不是最終判斷
依據——各自呼叫端仍需要自己的方法清單/欄位/InterfaceSpec 覆蓋範圍等資訊
才能下最終判斷，見 `needs_llm_summary()`／`design.py` 孤兒類別決策樹的
docstring。

`JPA_ENTITY_ANNOTATIONS` 是 `DATA_CLASS_ANNOTATIONS` 的子集，只有
`design_agent` 需要單獨判斷——DB schema 欄位層級規格不是③的職責（見 05a
九章「③ 不越界去產生 DB schema 的欄位層級規格」），偵測到這個子集時③要
直接跳過、不渲染，交由④直接從 DB 取得，不是 `parse_agent` 需要的區分
（04a 只需要「大概率是資料容器」這個粗粒度判斷，不需要分辨 JPA 與否）。
"""
from __future__ import annotations

JPA_ENTITY_ANNOTATIONS = frozenset({"Entity", "Embeddable", "MappedSuperclass"})

LOMBOK_DATA_ANNOTATIONS = frozenset({
    "Data", "Value", "Getter", "Setter", "Builder",
    "NoArgsConstructor", "AllArgsConstructor", "RequiredArgsConstructor",
})

DATA_CLASS_ANNOTATIONS = JPA_ENTITY_ANNOTATIONS | LOMBOK_DATA_ANNOTATIONS
