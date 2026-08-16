# scaffold_agent/column_mapping.py
"""Java 欄位 → SQLAlchemy Column 對應，對應 08a 七、八章。每個渲染函式
回傳 `FieldRender`（陳述式文字 ＋ 依模組分組的 import 需求）；
`model_builder.py` 逐欄位呼叫、收集完一個 entity 的所有 `FieldRender`
後再統一組裝檔案（見 08a 九章「組裝順序」：先合併 import、後接 class
定義）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dc_field

from common.java_type_mapping import camel_to_snake, map_java_type
from scaffold_agent.reference_resolver import resolve_reference
from scaffold_agent.types import PYTHON_TO_SQLALCHEMY, EntityRecord, FieldRecord, ScanIndex

logger = logging.getLogger(__name__)

_TYPE_ANN_IMPORTS: dict[str, dict[str, set[str]]] = {
    "Decimal": {"decimal": {"Decimal"}},
    "date": {"datetime": {"date"}},
    "datetime": {"datetime": {"datetime"}},
    "time": {"datetime": {"time"}},
}

_INVALID_IDENTIFIER_CHAR_RE = re.compile(r"\W")


def _sanitize_python_identifier(name: str) -> str:
    """把 `@JoinTable(name = "...")` 的字面值轉成合法的 Python 變數名稱，
    供 `render_join_table()` 當中介表 `Table(...)` 賦值的左手邊識別字。

    企業級 DB 常見的 table 命名（`USER_ROLES` 這種全大寫加底線）本身就是
    合法 Python 識別字，不需要處理；但連字號（`user-roles`）、空白等字元
    在 SQL 識別字裡合法（引號包住即可），在 Python 識別字裡不合法——
    `camel_to_snake()` 不能解決這個問題：它是依 camelCase 大小寫邊界切
    詞界的轉換，對「user-roles」這種完全沒有大小寫變化的輸入是 no-op，
    連字號原樣保留，交給它一樣會產生 `SyntaxError`。這裡改用「非合法
    識別字元字全部換成底線」這個更直接的機械規則，`\\W`（非英數字元、
    非底線）逐一取代，開頭若變成數字再補一個底線前綴。

    **只換識別字，不換 `Table(...)` 內部的字面字串**——DB 實際 table
    名稱（第一個位置引數）必須維持原樣，否則 SQLAlchemy 建出來的表名
    會跟目標資料庫的真實名稱對不上。
    """
    sanitized = _INVALID_IDENTIFIER_CHAR_RE.sub("_", name)
    if sanitized[:1].isdigit():
        sanitized = f"_{sanitized}"
    return sanitized


# 外層 wrapper 名稱允許帶點號（如 java.util.List<Role> 這種完整套件
# 路徑的內嵌寫法），防禦性放寬——但實際能不能解析到這個型別，取決於
# common.java_type_mapping.type_str() 有沒有把完整合格名稱還原出來。
# type_str() 目前對 ReferenceType.sub_type 鏈結不展開，遇到這種內嵌
# 完全限定名稱時本身就會提早把型別字串截斷成第一段（僅剩 "java"），
# 這個正則放寬與否都補救不了那一層；真實 Java 專案幾乎一律用
# import 陳述式 + 裸型別名稱（如 List<Role>），這裡放寬只是避免這個
# 正則本身成為第二個瓶頸，不是宣稱已完整解決這個情境。
_GENERIC_WRAPPER_RE = re.compile(r"^[\w.]+<(.+)>$")


@dataclass
class FieldRender:
    text: str  # 一行或多行（含 TODO 註解）的完整陳述式文字
    from_imports: dict[str, set[str]] = dc_field(default_factory=dict)


def merge_imports(*dicts: dict[str, set[str]]) -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {}
    for d in dicts:
        for module, names in d.items():
            merged.setdefault(module, set()).update(names)
    return merged


def _unwrap_collection_type(java_type: str) -> str:
    """`@ManyToMany` 欄位型別是 `List<Role>` 這種泛型包裝，取內層目標
    entity 型別名稱——只處理單層 `Foo<Bar>`，這批集合型別欄位不會有更
    複雜的巢狀形狀（沒有巢狀集合的多對多關聯）。
    """
    match = _GENERIC_WRAPPER_RE.match(java_type.strip())
    return match.group(1).strip() if match else java_type.strip()


def _resolve_nullable(record: FieldRecord) -> bool:
    """對應七章「欄位級 annotation」表格：`@Column(nullable=...)` 明確
    標註一律優先；缺席時依 `is_primitive` 分流（基礎型別 → `False`，
    包裝類別/其他型別 → `True`）。
    """
    if record.nullable is not None:
        return record.nullable
    return not record.is_primitive


def _mapped_column_stmt(attr: str, db_name: str | None, type_ann: str, col_type_expr: str, extra_kwargs: list[str]) -> str:
    """對應七章「`@Column(name = "...")` 覆蓋的是 DB 欄位名稱」：只有
    `db_name` 跟 `attr`（Python 屬性名稱）不同時才多帶這個位置引數。
    """
    args = []
    if db_name and db_name != attr:
        args.append(repr(db_name))
    args.append(col_type_expr)
    args.extend(extra_kwargs)
    return f"{attr}: Mapped[{type_ann}] = mapped_column({', '.join(args)})"


def _sqlalchemy_type_for_canonical(python_type: str, sqlalchemy_type: str, *, nullable: bool) -> tuple[str, str, dict[str, set[str]]]:
    """把 `reference_resolver` 算出的 `(canonical python 型別, canonical
    sqlalchemy 型別)` 轉成 `(Mapped[...] 型別註記, Column 型別運算式,
    需要的 import)`。給外鍵渲染（純量欄位以外，pk 型別／降級 fallback
    型別都是 canonical 形式）與一般純量欄位共用同一份轉換規則。
    """
    if python_type == "UUID":
        type_ann = "UUID | None" if nullable else "UUID"
        imports = {"uuid": {"UUID"}, "sqlalchemy.dialects.postgresql": {"UUID as PgUUID"}}
        return type_ann, "PgUUID(as_uuid=True)", imports
    type_ann = f"{python_type} | None" if nullable else python_type
    imports = merge_imports({"sqlalchemy": {sqlalchemy_type}}, _TYPE_ANN_IMPORTS.get(python_type, {}))
    return type_ann, sqlalchemy_type, imports


def _render_uuid_field(attr: str, record: FieldRecord) -> FieldRender:
    """對應七章「`UUID` 主鍵的預設值」。`is_id` 的欄位不加 `nullable=`
    （見 `_render_known_scalar_field()` 同一個理由：`primary_key=True`
    已隱含 `NOT NULL`，07a 渲染範例對 `id` 欄位也不帶這個關鍵字引數）。
    """
    nullable = False if record.is_id else _resolve_nullable(record)
    type_ann, col_type, imports = _sqlalchemy_type_for_canonical("UUID", "UUID", nullable=nullable)

    kwargs: list[str] = []
    comment_lines: list[str] = []
    if record.is_id:
        kwargs.append("primary_key=True")
        if record.is_generated_value:
            kwargs.append("default=uuid4")
            imports = merge_imports(imports, {"uuid": {"uuid4"}})
            comment_lines.append(
                "# 若此表可能被其他服務或程式直接寫入（不經過本服務的 ORM），建議改用 "
                'server_default=text("gen_random_uuid()")'
                "（需確認目標 PostgreSQL ≥ 13 或已啟用 pgcrypto）"
            )
    else:
        kwargs.append(f"nullable={nullable}")
    if record.unique:
        kwargs.append("unique=True")

    stmt = _mapped_column_stmt(attr, record.column_name, type_ann, col_type, kwargs)
    return FieldRender(text="\n".join([*comment_lines, stmt]), from_imports=imports)


def _render_known_scalar_field(attr: str, record: FieldRecord, python_type: str) -> FieldRender:
    """對應七章對應表已知純量型別分支＋「稽核時間戳」「唯一約束」。

    `is_id` 的欄位不加 `nullable=` 關鍵字引數、型別註記也不加 `| None`
    ——`primary_key=True` 已隱含 `NOT NULL`，07a 七章「渲染範例」的 `id`
    欄位（`Mapped[int] = mapped_column(Integer, primary_key=True,
    autoincrement=True)`）同樣不帶這個關鍵字引數，這裡對齊同一份既有
    慣例，不是這裡另外發明的規則。
    """
    sqlalchemy_type = PYTHON_TO_SQLALCHEMY[python_type]
    col_args: list[str] = []
    if sqlalchemy_type == "String" and record.length is not None:
        col_args = [str(record.length)]
    elif sqlalchemy_type == "Numeric" and record.precision is not None:
        col_args = [str(record.precision)] + ([str(record.scale)] if record.scale is not None else [])
    col_type = f"{sqlalchemy_type}({', '.join(col_args)})" if col_args else sqlalchemy_type
    imports = merge_imports({"sqlalchemy": {sqlalchemy_type}}, _TYPE_ANN_IMPORTS.get(python_type, {}))

    kwargs: list[str] = []
    if record.is_id:
        kwargs.append("primary_key=True")
        if record.is_generated_value and sqlalchemy_type == "Integer":
            kwargs.append("autoincrement=True")

    if record.is_audit_created or record.is_audit_updated:
        # 七章「稽核時間戳」：只有這幾個欄位改用 timezone=True，對齊
        # func.now() 回傳的 TIMESTAMPTZ，不是整條 date/datetime/time
        # 對應規則的例外。
        col_type = "DateTime(timezone=True)"
        imports = merge_imports(imports, {"sqlalchemy": {"DateTime", "func"}})
        kwargs.append("server_default=func.now()" if record.is_audit_created else "server_default=func.now(), onupdate=func.now()")

    nullable = False if record.is_id else _resolve_nullable(record)
    if not record.is_id:
        kwargs.append(f"nullable={nullable}")
    if record.unique:
        kwargs.append("unique=True")

    type_ann = python_type if record.is_id else (f"{python_type} | None" if nullable else python_type)
    stmt = _mapped_column_stmt(attr, record.column_name, type_ann, col_type, kwargs)
    return FieldRender(text=stmt, from_imports=imports)


def _render_enum_field(attr: str, record: FieldRecord, enum_fqn: str, scan_index: ScanIndex) -> FieldRender:
    """對應七章「Enum 欄位」。"""
    enum_python_name = scan_index.enum_python_names[enum_fqn]
    nullable = _resolve_nullable(record)

    if record.enum_ordinal:
        comment = (
            f"# TODO: Enum {enum_python_name} 已解析到，但標註 @Enumerated(EnumType.ORDINAL)，"
            "儲存格式（依宣告順序的整數索引）不穩定，降級為 String，需要人工或⑤確認實際欄位語意"
        )
        type_ann = "str | None" if nullable else "str"
        stmt = _mapped_column_stmt(attr, record.column_name, type_ann, "String", [f"nullable={nullable}"])
        return FieldRender(text=f"{comment}\n{stmt}", from_imports={"sqlalchemy": {"String"}})

    length = record.length if record.length is not None else 255
    type_ann = f"{enum_python_name} | None" if nullable else enum_python_name
    col_type = f"SqlEnum({enum_python_name}, native_enum=False, length={length})"
    stmt = _mapped_column_stmt(attr, record.column_name, type_ann, col_type, [f"nullable={nullable}"])
    imports = {"sqlalchemy": {"Enum as SqlEnum"}, "app.models._enums": {enum_python_name}}
    return FieldRender(text=stmt, from_imports=imports)


def _render_unresolved_scalar_field(attr: str, record: FieldRecord, java_type: str) -> FieldRender:
    """對應七章「無法解析型別的最終降級」。"""
    nullable = _resolve_nullable(record)
    type_ann = "str | None" if nullable else "str"
    comment = (
        f"# TODO: 未知型別 {java_type}，降級為 String，可能是 Enum"
        "（無法解析引用來源）或其他自訂型別，需要人工或⑤確認"
    )
    stmt = _mapped_column_stmt(attr, record.column_name, type_ann, "String", [f"nullable={nullable}"])
    return FieldRender(text=f"{comment}\n{stmt}", from_imports={"sqlalchemy": {"String"}})


def render_scalar_field(record: FieldRecord, scan_index: ScanIndex) -> FieldRender | None:
    """對應七章全節。呼叫端（`render_field()`）保證傳入時 `record` 已經
    是純量欄位（`relation is None`、非 `@Transient`）。

    **含 `[`／`]` 的容器型別不降級，直接跳過（回傳 `None`）**：對應七章
    對應表「含 `[`／`]` 的容器型別（`map_java_type()` 展開的泛型容器，或
    不支援的陣列形狀）」這一列——結構上不可能是單一純量 column，沒有
    「降級成 `String`」這個選項，這條規則必須排在「未知型別最終降級」
    （`_render_unresolved_scalar_field()`）之前判斷，否則泛型容器（如
    `map_java_type()` 把 `List<String>` 轉成的 `"list[str]"`）會被誤判成
    「無法解析的自訂型別」一路降級成 `String`。
    """
    attr = camel_to_snake(record.name)
    python_type = map_java_type(record.java_type)

    if "[" in python_type or "]" in python_type:
        logger.warning(
            "%s：型別 %s 正規化後含 [/]（容器或不支援的陣列形狀），結構上不可能是單一純量 "
            "column，不渲染、跳過（見 08a 七章「含 [／] 的容器型別」）",
            record.name,
            record.java_type,
        )
        return None

    if python_type == "UUID":
        return _render_uuid_field(attr, record)
    if python_type in PYTHON_TO_SQLALCHEMY:
        return _render_known_scalar_field(attr, record, python_type)

    enum_fqn = resolve_reference(record.java_type, record.declaring_package, record.declaring_import_map)
    enum_record = scan_index.enums.get(enum_fqn) if enum_fqn else None
    if enum_record is not None:
        return _render_enum_field(attr, record, enum_fqn, scan_index)

    return _render_unresolved_scalar_field(attr, record, record.java_type)


def render_foreign_key(entity_fqn: str, entity: EntityRecord, record: FieldRecord, scan_index: ScanIndex) -> FieldRender:
    """對應八章「欄位命名與 nullable」＋「外鍵解析失敗的降級處理」。
    `record.relation` 必須是 `"ManyToOne"` 或非 `mappedBy` 的
    `"OneToOne"`（呼叫端 `render_field()` 先過濾）。
    """
    target_fqn = resolve_reference(record.java_type, record.declaring_package, record.declaring_import_map)
    target = scan_index.entities.get(target_fqn) if target_fqn else None
    pk = scan_index.primary_keys.get(target_fqn) if target_fqn else None
    nullable = record.nullable if record.nullable is not None else True  # @JoinColumn 缺席預設 True（八章）

    if target is None or pk is None:
        attr = camel_to_snake(record.name)
        python_type, sqlalchemy_type = scan_index.fallback_pk_type
        type_ann, col_type, imports = _sqlalchemy_type_for_canonical(python_type, sqlalchemy_type, nullable=nullable)
        comment = f"# TODO: 概念上是外鍵，指向 {record.java_type}，因目標主鍵不明確無法產生 ForeignKey 約束"
        stmt = _mapped_column_stmt(attr, record.column_name, type_ann, col_type, [f"nullable={nullable}"])
        return FieldRender(text=f"{comment}\n{stmt}", from_imports=imports)

    pk_attr, pk_python_type, pk_sqlalchemy_type = pk
    attr = f"{camel_to_snake(record.name)}_{pk_attr}"
    db_name = record.column_name or attr
    type_ann, col_type, imports = _sqlalchemy_type_for_canonical(pk_python_type, pk_sqlalchemy_type, nullable=nullable)

    fk_target = f"{target.schema}.{target.table_name}.{pk_attr}" if target.schema else f"{target.table_name}.{pk_attr}"
    use_alter = (entity_fqn, target_fqn) in scan_index.fk_use_alter_edges
    fk_args = [repr(fk_target)]
    if use_alter:
        fk_args.append("use_alter=True")
        fk_args.append(f'name="fk_{entity.table_name}_{db_name}"')
    imports = merge_imports(imports, {"sqlalchemy": {"ForeignKey"}})

    kwargs = [f"ForeignKey({', '.join(fk_args)})", f"nullable={nullable}"]
    stmt = _mapped_column_stmt(attr, db_name, type_ann, col_type, kwargs)
    return FieldRender(text=stmt, from_imports=imports)


def render_field(entity_fqn: str, entity: EntityRecord, record: FieldRecord, scan_index: ScanIndex) -> FieldRender | None:
    """對外唯一入口，供 `model_builder.py` 逐欄位呼叫。`None` 代表這個
    欄位不渲染任何 Column（`@Transient`、`@OneToMany`、`@OneToOne` 非
    擁有端、`@ManyToMany`——後者由 `model_builder.py` 另外呼叫
    `render_join_table()` 處理，不是這裡的職責，見八章「刻意不產生
    `relationship()`」＋「`@ManyToMany`：產生中介表定義」）。
    """
    if record.is_transient or record.relation == "OneToMany":
        return None
    if record.relation == "OneToOne" and record.mapped_by is not None:
        return None
    if record.relation == "ManyToMany":
        return None
    if record.relation in ("ManyToOne", "OneToOne"):
        return render_foreign_key(entity_fqn, entity, record, scan_index)
    return render_scalar_field(record, scan_index)


def render_join_table(entity_fqn: str, entity: EntityRecord, record: FieldRecord, scan_index: ScanIndex) -> tuple[str, dict[str, set[str]]] | None:
    """對應八章「`@ManyToMany`：產生中介表定義」。呼叫端
    （`model_builder.py`）保證只對 `scan_index.join_table_owners` 判定
    的擁有端呼叫這個函式。回傳 `None` 代表主鍵或目標型別不明確，無法
    機械決定欄位形狀——呼叫端記警告，這個欄位沒有任何渲染結果。
    """
    join_table = record.join_table
    if join_table is None or join_table.join_column is None or join_table.inverse_join_column is None:
        return None

    owner_pk = scan_index.primary_keys.get(entity_fqn)
    inverse_fqn = resolve_reference(
        _unwrap_collection_type(record.java_type), record.declaring_package, record.declaring_import_map
    )
    inverse_entity = scan_index.entities.get(inverse_fqn) if inverse_fqn else None
    inverse_pk = scan_index.primary_keys.get(inverse_fqn) if inverse_fqn else None
    if owner_pk is None or inverse_entity is None or inverse_pk is None:
        return None

    owner_attr, owner_python_type, owner_sqlalchemy_type = owner_pk
    inverse_attr, inverse_python_type, inverse_sqlalchemy_type = inverse_pk

    owner_target = f"{entity.schema}.{entity.table_name}.{owner_attr}" if entity.schema else f"{entity.table_name}.{owner_attr}"
    inverse_target = (
        f"{inverse_entity.schema}.{inverse_entity.table_name}.{inverse_attr}"
        if inverse_entity.schema
        else f"{inverse_entity.table_name}.{inverse_attr}"
    )

    _, owner_col_type, owner_imports = _sqlalchemy_type_for_canonical(owner_python_type, owner_sqlalchemy_type, nullable=False)
    _, inverse_col_type, inverse_imports = _sqlalchemy_type_for_canonical(inverse_python_type, inverse_sqlalchemy_type, nullable=False)

    # 對應八章「多 schema 支援」在中介表這條路徑的延伸：中介表沒有對應
    # 的 Java entity，不會有自己的 @Table(schema=...)（entity_scan.py
    # 四章沒有擷取 @JoinTable 的 schema 元素，這批資料裡沒有這種寫法，
    # 見十章已知限制），因此沿用擁有端 entity 的 schema——`entity` 參數
    # 保證是擁有端（呼叫端只在 scan_index.join_table_owners 判定的擁有
    # 端才呼叫這個函式，見本函式 docstring）。不加這一行，中介表會落在
    # SQLAlchemy 預設 schema（通常是 `public`），跟擁有端／FK 目標各自
    # 所在的 schema 不一致。
    schema_line = f'    schema="{entity.schema}",\n' if entity.schema else ""

    # 變數名稱（賦值左手邊）跟 DB table 名稱（Table() 第一個位置引數）
    # 分開處理：前者必須是合法 Python 識別字，後者必須是真實 DB 名稱，
    # 兩者不保證相同（見 _sanitize_python_identifier() docstring）。
    variable_name = _sanitize_python_identifier(join_table.name)
    if variable_name != join_table.name:
        logger.warning(
            "中介表 %r 的名稱不是合法 Python 識別字，變數名稱正規化為 %r（DB table 名稱不變，見 08a 八章）",
            join_table.name,
            variable_name,
        )

    stmt = (
        f"{variable_name} = Table(\n"
        f'    "{join_table.name}",\n'
        f"    Base.metadata,\n"
        f'    Column("{join_table.join_column}", {owner_col_type}, ForeignKey("{owner_target}"), primary_key=True),\n'
        f'    Column("{join_table.inverse_join_column}", {inverse_col_type}, ForeignKey("{inverse_target}"), primary_key=True),\n'
        f"{schema_line}"
        f")"
    )
    imports = merge_imports(owner_imports, inverse_imports, {"sqlalchemy": {"Column", "ForeignKey", "Table"}})
    return stmt, imports
