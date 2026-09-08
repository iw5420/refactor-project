"""Java Spring Data `JpaRepository<Entity, ID>`／`CrudRepository<Entity, ID>`
的 Python 對應：泛型基底類別 `BaseRepository[T]`（見
`docs/refactor_bug_trace.md` #10／#16）。

**為什麼放在 `common/`，不是各自獨立維護一份**：`parse_agent`（①的呼叫圖
需要辨識「這個介面繼承了 Spring Data 基底介面」＋合成對應的參考座標）
跟 `design_agent`（③的骨架生成需要決定「這個類別該不該繼承 BaseRepository」
＋把 `BaseRepository` 自己的 7 個方法登記進 `java_index`）**必須用同一份
「Java 方法名稱 → Python 方法名稱」對照表、同一個合成座標格式**，兩邊
稍有出入就會對不上——跟 `is_utils_package()` 那種「邏輯一樣、各自複製
一份也不會壞」的情況不同，這裡的字串格式本身就是兩邊溝通的契約，比照
`common/java_type_mapping.py` 先例，共用單一副本。

**設計精神**：不是機械偵測每一個 Spring Data 方法呼叫、逐一合成對應的
`InterfaceSpec`（那是 #10 最初的合成式修法，範圍有限、只涵蓋「整個類別
零方法」這一種情況）。而是讓 Python 端也有一個對應 Java `JpaRepository<T,
ID>` 的泛型基底類別，任何 repository 類別繼承它＋指定 `model` 屬性，就
自動、真實地擁有 `find_all`／`find_by_id`／`save`／`delete_by_id`／
`delete`／`count`／`exists_by_id` 這幾個標準方法——跟 Java 端「繼承
`JpaRepository<Entity, ID>` 就自動有這些方法」同一種設計，不需要為每個
repository、每個內建方法個別合成。真實測試證實：⑤（qwen／Claude 皆然）
只要看得到 `BaseRepository` 的宣告＋一句話說明這個 pattern，就能自己正確
推理出「呼叫端該怎麼呼叫」，不需要窮舉每一種 Spring Data 呼叫寫法。
"""
from __future__ import annotations

# 這個檔案在生成專案裡的固定路徑／類別名稱——機械產生（不經過 LLM），
# 內容固定，任何專案只要偵測到至少一個 repository 繼承 Spring Data 基底
# 介面，就會被寫進去，見 design_agent/design.py。
BASE_REPOSITORY_FILE = "app/core/base_repository.py"
BASE_REPOSITORY_CLASS = "BaseRepository"

# Spring Data 常見的幾個基底介面（完整清單見 Spring Data JPA 文件，這裡
# 只收這個專案實際會遇到的幾種——`JpaSpecificationExecutor` 刻意不收錄，
# 它提供的是 `Specification<T>` 動態查詢方法，不是固定命名的 CRUD 方法，
# 屬於 #14 已經處理過的獨立問題）。
JPA_REPOSITORY_BASE_INTERFACES = frozenset({
    "JpaRepository",
    "CrudRepository",
    "PagingAndSortingRepository",
})

# Java 端方法名稱 → Python 端（`BaseRepository`）對應方法名稱。這是
# Spring Data 這幾個基底介面公開方法的固定清單，不是這個專案自訂的東西，
# 只要繼承的是這幾個已知介面之一，方法名稱／簽名就是固定的，不會因專案
# 而異，維護這張表不是「窮舉每個專案的呼叫慣例」那種會隨專案膨脹的清單。
JPA_BASE_METHOD_NAME_MAP: dict[str, str] = {
    "findAll": "find_all",
    "findById": "find_by_id",
    "save": "save",
    "deleteById": "delete_by_id",
    "delete": "delete",
    "count": "count",
    "existsById": "exists_by_id",
}

# 合成座標的固定前綴：`BaseRepository` 本身沒有對應的真實 Java 原始碼
# 位置（它對應的是 Spring Data 框架本身的 `JpaRepository<T, ID>` 介面，
# 不是這個專案 `lang-exam-api-refactor` 底下的檔案），因此不能比照一般
# InterfaceSpec 用「真實 Java 檔案路徑」組 java_method_id——這裡用一個
# 固定、不會跟任何真實檔案路徑衝突的字面字串當「虛擬檔案」，①（呼叫圖
# 合成 fallback）跟③（`java_index` 登記）各自獨立算出來的字串必須逐字
# 相同，才能透過 `java_index` 這個共用查找表接上線，因此这个格式訂在
# 這裡、只有一份，不是兩邊各自決定。
_SYNTHETIC_JAVA_FILE = "__jpa_base_repository__"


def synthetic_java_method_id(java_method_name: str) -> str:
    """`java_method_name` 是 Java 端的方法名稱（如 `"findAll"`，
    `JPA_BASE_METHOD_NAME_MAP` 的 key），回傳①②③共用的合成座標字串，
    格式比照既有 `method_id()`（"{file}::{class}::{method}"）但檔案路徑
    換成固定的虛擬字面字串。"""
    return f"{_SYNTHETIC_JAVA_FILE}::{BASE_REPOSITORY_CLASS}::{java_method_name}"


# 機械產生、不經過 LLM 的固定檔案內容——`find_all`／`find_by_id`／`save`
# ／`delete_by_id`／`delete`／`count`／`exists_by_id` 都是純機械、決定性
# 的 SQLAlchemy 操作，不需要問模型怎麼寫，比照 `design_agent/global_
# infra.py::render_config_py()` 同一種「固定樣板」性質。
BASE_REPOSITORY_CONTENT = '''from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy.orm import Session

T = TypeVar("T")


class BaseRepository(Generic[T]):
    model: type[T]

    def find_all(self, db: Session) -> list[T]:
        return db.query(self.model).all()

    def find_by_id(self, id, db: Session) -> T | None:
        return db.query(self.model).get(id)

    def save(self, entity: T, db: Session) -> T:
        db.add(entity)
        db.commit()
        db.refresh(entity)
        return entity

    def delete_by_id(self, id, db: Session) -> None:
        entity = self.find_by_id(id, db)
        if entity is not None:
            db.delete(entity)
            db.commit()

    def delete(self, entity: T, db: Session) -> None:
        db.delete(entity)
        db.commit()

    def count(self, db: Session) -> int:
        return db.query(self.model).count()

    def exists_by_id(self, id, db: Session) -> bool:
        return self.find_by_id(id, db) is not None
'''


def detect_jpa_base_entity(extends_refs) -> str | None:
    """`extends_refs` 是 javalang `InterfaceDeclaration.extends`（`list[
    ReferenceType] | None`，Java 介面可以同時繼承多個介面）。回傳這個
    介面繼承的 Spring Data 基底介面的第一個泛型型別引數（Entity 型別的
    簡單名稱），沒有繼承任何已知基底介面時回傳 `None`。

    只取第一個泛型引數：`JpaRepository<Entity, ID>`／`CrudRepository<
    Entity, ID>`／`PagingAndSortingRepository<Entity, ID>` 這三個介面的
    泛型引數順序都是 `<Entity, ID>`，第一個固定是 entity 型別，不需要
    分開處理。同時繼承多個介面時（如 `ExamRepository extends
    JpaRepository<ExamEntity, String>, JpaSpecificationExecutor<
    ExamEntity>`），只要任一個是已知基底介面就命中，不要求全部都是。
    """
    for ref in extends_refs or []:
        if ref.name not in JPA_REPOSITORY_BASE_INTERFACES:
            continue
        if not ref.arguments:
            continue
        first_arg_type = ref.arguments[0].type
        if first_arg_type is None:
            continue
        return first_arg_type.name
    return None
