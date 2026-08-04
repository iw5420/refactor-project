"""[A]/[B] 共用型別定義，對應 03a 三章「鏈式依賴偵測」表格與「人工填值機制」。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# openapi.json／Postman Collection 都直接用 dict 表示，不特別包裝成物件，
# 因為兩者都是「讀進來、轉一手、寫出去」的過境資料，包裝成 dataclass
# 反而要多寫一層 to_dict/from_dict。
OpenAPISpec = dict[str, Any]
PostmanCollection = dict[str, Any]
PostmanItem = dict[str, Any]

HttpMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]

# 對應 03a 三章「Readonly / Mutation 分類規則」
READONLY_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})
MUTATION_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class ChainDependency:
    """鏈式依賴偵測結果單筆紀錄，欄位對應 03a 三章表格。"""

    producer_endpoint: str  # 如 "POST /api/v1/users"
    producer_field: str  # response 欄位路徑，點號分隔，如 "data.id"
    consumer_endpoint: str  # 如 "GET /api/v1/users/{id}"
    consumer_param: str  # 對應的參數名稱（path/query/body 皆可）
    env_var_name: str  # 如 "created_user_id"

    @property
    def producer_method(self) -> str:
        return self.producer_endpoint.split(" ", 1)[0].upper()

    @property
    def producer_path(self) -> str:
        return self.producer_endpoint.split(" ", 1)[1]

    @property
    def consumer_method(self) -> str:
        return self.consumer_endpoint.split(" ", 1)[0].upper()

    @property
    def consumer_path(self) -> str:
        return self.consumer_endpoint.split(" ", 1)[1]


@dataclass
class CollectionAgentResult:
    """run_collection_agent() 的回傳值，對應 03a 三章「產出與交接」。"""

    collection_readonly_path: str
    collection_mutation_path: str
    unfilled_endpoints_path: str
    # 尚未被人工解決（既沒填值、也沒標記 skip）的 endpoint 清單，供
    # graph 層的 conditional edge 判斷要不要暫停等人工處理（見 01 五
    # 「人工補值關卡」、03a 三章「人工填值機制」）。空清單＝全部解決，
    # 不阻擋流程；已標記 skip 的不算在這裡面。
    manual_fill_pending: list[str]
