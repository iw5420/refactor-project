# scaffold_agent/types.py
"""對外輸入輸出型別 + 套件內部共用資料結構，對應 08a 二、四、六、九章。

`ModuleInfo` 結構對齊 `graph/state.py` 的同名 TypedDict，但刻意在這裡
重新定義、不 import `graph.state`（見 08a 十一章「輸入端 module_list 的
型別比照 translator_cli/types.py 的既有先例」）——`scaffold_agent` 不依賴
`design_agent`／`graph.state`。只列出這個套件實際會讀的欄位（`module`／
`java_files`），不是逐欄位照抄：TypedDict 只在型別檢查工具眼中要求
「這幾個 key 必須存在」，執行期呼叫端傳入欄位更多的
`graph.state.ModuleInfo` 完全相容，多出來的 `summary`／`depends_on`／
`methods` 這幾個 key 不影響任何一處存取。

`BuildDbModelsResult` 是 `build_db_models()`（`scaffold_agent/__init__.py`）
的回傳型別，對應 08a 九章「`skipped_entities`：entity 級失敗清單」。

其餘資料類別（`FieldRecord`／`EntityRecord`／`EnumRecord`／
`JoinTableSpec`／`ScanIndex`）是 `entity_scan.py`／`reference_resolver.py`
／`column_mapping.py`／`model_builder.py` 四個模組共用的內部資料流，集中
定義在這裡避免循環 import。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict


class ModuleInfo(TypedDict):
    module: str
    java_files: list[str]


# 對應 08a 七章「Python 型別 → SQLAlchemy Column 型別」表格前半段（純量
# 型別，不含 Enum／UUID 這兩種需要額外語境才能渲染的特例）。定義在這裡
# （而不是 column_mapping.py 或 reference_resolver.py）是為了讓兩者都能
# 直接 import 同一份常數、不互相依賴：`reference_resolver.py` 判斷主鍵
# 型別（六章 `primary_key_type`）只需要這張表算出一個「canonical 型別
# 字串」；`column_mapping.py` 渲染欄位時再把這個字串轉成實際的 Python
# 原始碼與對應 import——canonical 字串跟渲染結果是兩個層次，`UUID` 是
# 這張表唯一的例外，兩邊呼叫端各自判斷 `python_type == "UUID"` 再各自
# 決定怎麼處理（見 reference_resolver.py `_pk_type_str()`、
# column_mapping.py `render_field()`）。
PYTHON_TO_SQLALCHEMY: dict[str, str] = {
    "int": "Integer",
    "str": "String",
    "bool": "Boolean",
    "float": "Float",
    "Decimal": "Numeric",
    "date": "Date",
    "datetime": "DateTime",
    "time": "Time",
    "bytes": "LargeBinary",
}


@dataclass
class BuildDbModelsResult:
    """對應 08a 九章。`db_models` 直接是傳給
    `translator_cli.generate_scaffold(..., db_models=...)` 的字典，
    `skipped_entities` 是 `{file_path, class_name, error}` 清單。
    """

    db_models: dict[str, str]
    skipped_entities: list[dict]


# ── entity_scan.py 產出的原始欄位/類別記錄 ──────────────────────────


@dataclass
class JoinTableSpec:
    """對應 08a 四章「`@ManyToMany` 欄位額外擷取 `@JoinTable`」。
    `join_column`／`inverse_join_column` 為 `None` 代表對應的
    `@JoinColumn` 缺席或無法解析（八章「只支援單一欄位」限制）。
    """

    name: str
    join_column: str | None
    inverse_join_column: str | None


@dataclass
class FieldRecord:
    """對應 08a 四章「Entity 記錄欄位」表格裡 `fields` 這一列，以及七、
    八章渲染時需要的 annotation 元素值。所有欄位值都已通過「Literal 規則」
    （四章）過濾——非 `Literal` 的 annotation 元素在掃描階段就已經視同
    缺席，這裡看到的一律是可以直接使用的具體值，不再是 AST 節點。

    `declaring_package`／`declaring_import_map`：這個欄位原始宣告所在
    類別（entity 自己，或某一層 `@MappedSuperclass`）的 package／
    import_map，供 `resolve_reference()` 解析型別參照用（見 08a 六章
    「每個欄位要連同它原始宣告處的 package/import_map 一起帶著走」）。
    在 `entity_scan.scan_module()` 掃描當下，這兩個值天然就是這個欄位
    所屬 `EntityRecord` 自己的 `package`／`import_map`——之所以在
    `FieldRecord` 上重複記一份而不是讓呼叫端去查外層 `EntityRecord`，
    是因為 `reference_resolver._merge_mapped_superclass_fields()` 合併
    有效欄位清單後，一筆 `FieldRecord` 最終落腳的 `EntityRecord`（子
    entity）跟它原始宣告的類別（可能是某層祖先）已經不是同一個，欄位
    必須自帶語境才能正確解析。
    """

    name: str  # camelCase Java 欄位名
    java_type: str  # type_str() 還原出的 Java 型別字面字串（含泛型/陣列）
    is_primitive: bool

    relation: str | None  # None（純量）｜"ManyToOne"｜"OneToOne"｜"OneToMany"｜"ManyToMany"
    is_transient: bool

    is_id: bool
    is_generated_value: bool

    column_name: str | None  # @Column(name=...) 或 @JoinColumn(name=...) 字面值
    nullable: bool | None  # @Column/@JoinColumn(nullable=...)，None=未標註
    length: int | None
    precision: int | None
    scale: int | None
    unique: bool | None

    enum_ordinal: bool  # @Enumerated(EnumType.ORDINAL)
    is_audit_created: bool  # @CreatedDate / @CreationTimestamp
    is_audit_updated: bool  # @LastModifiedDate / @UpdateTimestamp

    mapped_by: str | None  # @OneToOne / @ManyToMany 的 mappedBy
    join_table: JoinTableSpec | None

    declaring_package: str | None
    declaring_import_map: dict[str, str] = field(default_factory=dict)


@dataclass
class EntityRecord:
    """對應 08a 四章「Entity 記錄欄位」表格。`jpa_kind` 為
    `"Embeddable"` 時 `table_name`／`schema`／`unique_constraints` 恆為
    `None`／空清單（`@Embeddable` 本身不對應獨立 table，見 08a 十章）。
    """

    class_name: str
    package: str | None
    module: str
    jpa_kind: str  # "Entity" | "Embeddable" | "MappedSuperclass"
    extends: str | None
    table_name: str | None
    schema: str | None
    unique_constraints: list[list[str]]
    soft_delete_clause: str | None
    fields: list[FieldRecord]
    import_map: dict[str, str]
    file_path: str


@dataclass
class EnumRecord:
    class_name: str
    package: str | None
    module: str
    members: list[str]
    file_path: str


# ── reference_resolver.py 產出的全域索引 ────────────────────────────


@dataclass
class ScanIndex:
    """對應 08a 六章「第一階段（全域）」。所有欄位 key 皆為 FQN（六章
    「跨檔案型別參照解析」），只有 `join_table_owners`（key 是中介表
    名稱）與 `fallback_pk_type`（單一全域值）例外。
    """

    entities: dict[str, EntityRecord] = field(default_factory=dict)  # jpa_kind == "Entity" only
    mapped_superclasses: dict[str, EntityRecord] = field(default_factory=dict)
    enums: dict[str, EnumRecord] = field(default_factory=dict)
    embeddables: dict[str, EntityRecord] = field(default_factory=dict)  # 十章已知限制，僅供 model_builder 記 skipped_entities

    # fqn -> 有效欄位清單（entity 自己的欄位 + 沿 extends 鏈合併的
    # @MappedSuperclass 欄位，見六章「@MappedSuperclass 欄位繼承合併」）。
    # 只有 entities 需要，mapped_superclasses 不需要展開自己的祖先鏈。
    effective_fields: dict[str, list[FieldRecord]] = field(default_factory=dict)

    # fqn -> (Python 屬性名稱, canonical Python 型別, canonical SQLAlchemy
    # Column 型別) | None（主鍵不明確，見六章「primary_key_column／
    # primary_key_type」）。canonical 型別字串只有兩種特殊值需要呼叫端
    # （column_mapping.py）額外處理："UUID" 代表要渲染
    # `PgUUID(as_uuid=True)` + `uuid.UUID` 型別註記，其餘直接是
    # `types.PYTHON_TO_SQLALCHEMY` 表格裡的 key/value。
    primary_keys: dict[str, tuple[str, str, str] | None] = field(default_factory=dict)

    enum_python_names: dict[str, str] = field(default_factory=dict)  # fqn -> 消歧後最終 class 名稱
    # (canonical Python 型別, canonical SQLAlchemy Column 型別)，見六章
    # 「降級外鍵的型別 fallback」。
    fallback_pk_type: tuple[str, str] = ("int", "Integer")
    fk_use_alter_edges: set[tuple[str, str]] = field(default_factory=set)
    join_table_owners: dict[str, str] = field(default_factory=dict)  # table_name -> 擁有者 entity fqn

    # 六章「簡短類別名稱跨 package 撞名」tie-break：同一 module 內撞名，
    # 後出現的 entity 不渲染，記進這裡（{file_path, class_name, error}）。
    duplicate_entity_skips: list[dict] = field(default_factory=list)
