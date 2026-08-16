# scaffold_agent/entity_scan.py
"""javalang 掃描 JPA entity／enum class，對應 08a 四章。獨立於
`design_agent/signature_scan.py`（不重用它建構 `JavaClassSignature` 的
整套流程，見四章「掃描方式：獨立實作」）——只搬用共用的
`common.java_type_mapping.type_str()`（javalang 型別節點還原）與
`common.java_annotations.JPA_ENTITY_ANNOTATIONS`（③④共用的 annotation
判斷常數）。

**Literal 規則**（四章「Annotation 元素值萃取：只接受 `Literal`」）貫穿
本檔案每一處讀 annotation 元素值的地方：任何元素值節點不是
`javalang.tree.Literal`（如 `MemberReference` 常數參照），一律視同該
元素缺席、記一筆 `logger.warning`，不嘗試把節點還原成字面字串塞進渲染
樣板——避免產出引用未定義 Python 名稱的骨架。唯一例外是
`@Enumerated(EnumType.STRING/ORDINAL)`，其元素值語法上一定是
`MemberReference`，見 `_enum_ordinal()`。

**掃描失敗的處理**：`javalang.parser.JavaSyntaxError` 原樣往上拋，不吞
不跳過（見四章「掃描失敗的處理」）——這批檔案已被①③解析成功過，再次
失敗代表輸入端有本文件範圍外的異常。
"""
from __future__ import annotations

import logging
from pathlib import Path

import javalang
import javalang.tree

from common.java_annotations import JPA_ENTITY_ANNOTATIONS
from common.java_type_mapping import type_str
from scaffold_agent.types import EntityRecord, EnumRecord, FieldRecord, JoinTableSpec, ModuleInfo

logger = logging.getLogger(__name__)

_JAVA_PRIMITIVES = frozenset({"int", "long", "short", "byte", "float", "double", "boolean", "char"})


def _literal_value(node) -> object | None:
    """把 `javalang.tree.Literal.value`（javalang 保留原始字面文字，字串
    帶引號、布林是 `"true"`／`"false"` 字面字串）還原成 Python
    `str`／`bool`／`int`／`float`。非 `Literal` 節點呼叫端負責判斷，這裡
    只處理已確認是 `Literal` 的節點。
    """
    raw = node.value
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw  # char literal 等未預期形狀，原樣保留，呼叫端目前不會用到


def _annotation_elements(ann) -> dict[str, object]:
    """把 `Annotation.element` 正規化成 `{key: 節點}`：`None`（marker
    annotation，如 `@Id`）→ 空字典；`list[ElementValuePair]`（一般具名
    元素形式）→ 逐一取 `.name`/`.value`；單一節點（`@Enumerated(EnumType.
    STRING)` 這種未具名的單一位置元素，JPA 規格裡等同 `value` 元素）→
    `{"value": 節點}`。
    """
    element = ann.element
    if element is None:
        return {}
    if isinstance(element, list):
        return {pair.name: pair.value for pair in element}
    return {"value": element}


def _get_literal(elements: dict, key: str, *, label: str) -> object | None:
    """對應「Literal 規則」：`key` 缺席回傳 `None`；存在但不是 `Literal`
    （常數參照等）記警告、視同缺席回傳 `None`；是 `Literal` 才還原成
    實際值。
    """
    node = elements.get(key)
    if node is None:
        return None
    if isinstance(node, javalang.tree.Literal):
        return _literal_value(node)
    logger.warning(
        "%s：annotation element %r 不是 Literal（可能是常數參照），視同缺席（見 08a 四章 Literal 規則）",
        label,
        key,
    )
    return None


def _get_literal_array(elements: dict, key: str, *, label: str) -> list[str]:
    """`@UniqueConstraint(columnNames = {"a", "b"})` 這種陣列語法：
    `ElementArrayValue.values` 逐一比對 Literal 規則；Java 也允許單元素
    陣列省略大括號寫成單一 `Literal`（`columnNames = "a"`），一併支援。
    """
    node = elements.get(key)
    if node is None:
        return []
    values = getattr(node, "values", None)
    if values is None:
        single = _get_literal(elements, key, label=label)
        return [single] if isinstance(single, str) else []
    result = []
    for value_node in values:
        if isinstance(value_node, javalang.tree.Literal):
            result.append(_literal_value(value_node))
        else:
            logger.warning(
                "%s：%r 陣列元素不是 Literal，該元素視同缺席（見 08a 四章 Literal 規則）", label, key
            )
    return result


def _extract_unique_constraints(table_elements: dict, *, label: str) -> list[list[str]]:
    """對應四章 `unique_constraints`：`@Table(uniqueConstraints = ...)`
    可能是單一 `@UniqueConstraint`，也可能是 `{@UniqueConstraint(...), ...}`
    陣列——兩種形狀都用 `getattr(..., "values", [node])` 統一處理。
    """
    node = table_elements.get("uniqueConstraints")
    if node is None:
        return []
    candidates = getattr(node, "values", None) or [node]
    result = []
    for uc in candidates:
        if not isinstance(uc, javalang.tree.Annotation):
            continue
        cols = _get_literal_array(_annotation_elements(uc), "columnNames", label=label)
        if cols:
            result.append(cols)
    return result


def _first_join_column_name(node, *, label: str) -> str | None:
    """對應四章「取陣列第一個元素的 `name`」：`joinColumns`／
    `inverseJoinColumns` 可能是單一 `@JoinColumn` 或陣列，只取第一個
    （複合欄位不處理，見 08a 十章「已知限制」）。
    """
    if node is None:
        return None
    candidates = getattr(node, "values", None) or [node]
    first = candidates[0]
    if not isinstance(first, javalang.tree.Annotation):
        return None
    value = _get_literal(_annotation_elements(first), "name", label=label)
    return value if isinstance(value, str) else None


def _enum_ordinal(elements: dict) -> bool:
    """對應四章「唯一的例外：`@Enumerated(EnumType.STRING)`／
    `@Enumerated(EnumType.ORDINAL)`」：元素值語法上必然是
    `MemberReference`，精確辨認 `qualifier == "EnumType"` 且
    `member in ("STRING", "ORDINAL")` 這個固定、封閉的寫法，回傳是否為
    `ORDINAL`（`STRING` 或完全沒標註都不是 ordinal，見 08a 七章「Enum
    欄位」解析流程第 1 步）。
    """
    value = elements.get("value")
    return (
        isinstance(value, javalang.tree.MemberReference)
        and value.qualifier == "EnumType"
        and value.member == "ORDINAL"
    )


def _is_primitive(field_type) -> bool:
    """對應四章「`is_primitive`」：javalang 用 `BasicType` 節點表示 Java
    八個基礎型別，`ReferenceType` 表示包裝類別／自訂類別——陣列
    （`dimensions` 非空，如 `byte[]`）語意上不是純量基礎型別，排除在外。
    必須在 `map_java_type()` 轉換之前判斷（見四章：轉換後 `int`／
    `Integer` 都變成同一個 `"int"`，這個區別就永久遺失了）。
    """
    return isinstance(field_type, javalang.tree.BasicType) and not field_type.dimensions


_RELATION_ANNOTATIONS = ("ManyToOne", "OneToOne", "OneToMany", "ManyToMany")


def _scan_field(
    field_decl: javalang.tree.FieldDeclaration, *, label: str
) -> list[FieldRecord]:
    """對應四章 `fields`：一個 `FieldDeclaration` 可能一次宣告多個變數
    （`private int a, b;`），逐一展開，共用同一個型別與 annotation 集合
    （比照 `design_agent/signature_scan.py` 的 `_field_signature()` 既有
    先例）。`declaring_package`／`declaring_import_map` 由呼叫端
    （`scan_module()`）事後補上——這裡先留空，見該函式。
    """
    ann_by_name: dict[str, dict[str, object]] = {
        ann.name: _annotation_elements(ann) for ann in field_decl.annotations
    }
    java_type = type_str(field_decl.type)
    is_primitive = _is_primitive(field_decl.type)

    relation = next((r for r in _RELATION_ANNOTATIONS if r in ann_by_name), None)
    is_transient = "Transient" in ann_by_name

    column_elements = ann_by_name.get("Column", {})
    join_column_elements = ann_by_name.get("JoinColumn", {})
    source_elements = join_column_elements if relation in ("ManyToOne", "OneToOne") else column_elements

    enumerated_elements = ann_by_name.get("Enumerated")

    mapped_by = None
    join_table = None
    if relation in ("OneToOne", "ManyToMany"):
        mapped_by_value = _get_literal(ann_by_name.get(relation, {}), "mappedBy", label=label)
        mapped_by = mapped_by_value if isinstance(mapped_by_value, str) else None
    if relation == "ManyToMany" and "JoinTable" in ann_by_name:
        jt_elements = ann_by_name["JoinTable"]
        jt_name = _get_literal(jt_elements, "name", label=label)
        if isinstance(jt_name, str):
            join_table = JoinTableSpec(
                name=jt_name,
                join_column=_first_join_column_name(jt_elements.get("joinColumns"), label=label),
                inverse_join_column=_first_join_column_name(jt_elements.get("inverseJoinColumns"), label=label),
            )

    column_name = _get_literal(source_elements, "name", label=label)
    nullable = _get_literal(source_elements, "nullable", label=label)
    length = _get_literal(column_elements, "length", label=label) if relation is None else None
    precision = _get_literal(column_elements, "precision", label=label) if relation is None else None
    scale = _get_literal(column_elements, "scale", label=label) if relation is None else None
    unique = _get_literal(column_elements, "unique", label=label) if relation is None else None

    records = []
    for declarator in field_decl.declarators:
        records.append(
            FieldRecord(
                name=declarator.name,
                java_type=java_type,
                is_primitive=is_primitive,
                relation=relation,
                is_transient=is_transient,
                is_id="Id" in ann_by_name,
                is_generated_value="GeneratedValue" in ann_by_name,
                column_name=column_name if isinstance(column_name, str) else None,
                nullable=nullable if isinstance(nullable, bool) else None,
                length=length if isinstance(length, int) else None,
                precision=precision if isinstance(precision, int) else None,
                scale=scale if isinstance(scale, int) else None,
                unique=unique if isinstance(unique, bool) else None,
                enum_ordinal=_enum_ordinal(enumerated_elements) if enumerated_elements is not None else False,
                is_audit_created=bool({"CreatedDate", "CreationTimestamp"} & ann_by_name.keys()),
                is_audit_updated=bool({"LastModifiedDate", "UpdateTimestamp"} & ann_by_name.keys()),
                mapped_by=mapped_by,
                join_table=join_table,
                declaring_package=None,  # scan_module() 補上
                declaring_import_map={},  # scan_module() 補上
            )
        )
    return records


def scan_module(java_project_path: str, module: ModuleInfo) -> tuple[list[EntityRecord], list[EnumRecord]]:
    """對 `module["java_files"]` 逐檔 `javalang.parse.parse()`，回傳
    `(entities, enums)`。對應四章「掃描範圍」＋「同時掃描
    `EnumDeclaration`」。
    """
    entities: list[EntityRecord] = []
    enums: list[EnumRecord] = []
    root = Path(java_project_path)

    for rel_path in module["java_files"]:
        source = (root / rel_path).read_text(encoding="utf-8")
        tree = javalang.parse.parse(source)  # JavaSyntaxError 原樣往上拋
        package = tree.package.name if tree.package else None
        import_map = {
            imp.path.rsplit(".", 1)[-1]: imp.path
            for imp in (tree.imports or [])
            if not imp.wildcard and not imp.static
        }

        for decl in tree.types:
            if isinstance(decl, javalang.tree.EnumDeclaration):
                enums.append(
                    EnumRecord(
                        class_name=decl.name,
                        package=package,
                        module=module["module"],
                        members=[c.name for c in decl.body.constants],
                        file_path=rel_path,
                    )
                )
                continue

            if not isinstance(decl, javalang.tree.ClassDeclaration):
                continue

            ann_names = {a.name for a in decl.annotations}
            jpa_hits = ann_names & JPA_ENTITY_ANNOTATIONS
            if not jpa_hits:
                continue
            jpa_kind = next(k for k in ("Entity", "Embeddable", "MappedSuperclass") if k in jpa_hits)
            label = f"{package}.{decl.name}" if package else f"{rel_path}::{decl.name}"

            table_ann = next((a for a in decl.annotations if a.name == "Table"), None)
            table_elements = _annotation_elements(table_ann) if table_ann else {}
            table_name = table_elements and _get_literal(table_elements, "name", label=label)
            schema = table_elements and _get_literal(table_elements, "schema", label=label)

            where_ann = next((a for a in decl.annotations if a.name == "Where"), None)
            restriction_ann = next((a for a in decl.annotations if a.name == "SQLRestriction"), None)
            soft_delete_clause = None
            if where_ann is not None:
                soft_delete_clause = _get_literal(_annotation_elements(where_ann), "clause", label=label)
            elif restriction_ann is not None:
                soft_delete_clause = _get_literal(_annotation_elements(restriction_ann), "value", label=label)

            fields: list[FieldRecord] = []
            for field_decl in decl.fields:
                for record in _scan_field(field_decl, label=label):
                    record.declaring_package = package
                    record.declaring_import_map = import_map
                    fields.append(record)

            entities.append(
                EntityRecord(
                    class_name=decl.name,
                    package=package,
                    module=module["module"],
                    jpa_kind=jpa_kind,
                    extends=decl.extends.name if decl.extends is not None else None,
                    table_name=table_name if isinstance(table_name, str) else None,
                    schema=schema if isinstance(schema, str) else None,
                    unique_constraints=_extract_unique_constraints(table_elements, label=label) if table_elements else [],
                    soft_delete_clause=soft_delete_clause if isinstance(soft_delete_clause, str) else None,
                    fields=fields,
                    import_map=import_map,
                    file_path=rel_path,
                )
            )

    return entities, enums
