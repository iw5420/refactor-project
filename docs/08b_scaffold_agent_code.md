# scaffold_agent 程式碼實作

> 08a 是設計面文件，本文件是實作面文件，一一對應、不重複設計理由——每節開頭註明對應 08a 章節，這裡只講怎麼落地成程式碼。對應 `00_refactor_architecture.md` 十一章文件索引的 `08b_scaffold_agent_code.md`。

08a 十一章定義的套件結構列了 7 個檔案（`entity_scan.py`／`reference_resolver.py`／`column_mapping.py`／`model_builder.py`／`types.py`／`exceptions.py`／`__init__.py`）。本文件落地時省略了 `exceptions.py`——見一章「與 08a 十一章套件結構的落差」。

## 目錄

| 檔案 | 對應章節 | 說明 |
|---|---|---|
| `scaffold_agent/types.py` | 08a 二、四、六、九章 | `ModuleInfo`／`BuildDbModelsResult`（對外）＋ `FieldRecord`／`EntityRecord`／`EnumRecord`／`JoinTableSpec`／`ScanIndex`（套件內部共用資料流）＋ `PYTHON_TO_SQLALCHEMY` |
| `scaffold_agent/entity_scan.py` | 08a 四章 | javalang 掃描 JPA entity／enum：Literal 規則、`@Table`／`@Where`／`@JoinTable`、field-level annotation、`is_primitive` |
| `scaffold_agent/reference_resolver.py` | 08a 六章 | 兩階段全域索引：FQN 建構、`resolve_reference()`、`@MappedSuperclass` 欄位合併、FK 依賴圖與 SCC 循環偵測、`join_table_owners`、`enum_python_names` 三層消歧、`fallback_pk_type` |
| `scaffold_agent/column_mapping.py` | 08a 七、八章 | 逐欄位渲染：純量／`UUID`／`Enum`／未知型別降級、外鍵、`@ManyToMany` 中介表 |
| `scaffold_agent/model_builder.py` | 08a 九章 | 逐 entity 隔離驗證、`__table_args__`、檔案組裝（含 `_enums.py`） |
| `scaffold_agent/__init__.py` | 08a 十二章 | 對外唯一入口 `build_db_models()` |
| `graph/state.py` | 08a 十二章 | 新增 `RefactorState.skipped_interfaces`／`skipped_db_models` |
| `graph/nodes/scaffold_node.py` | 08a 十二章 | 接上 `scaffold_agent.build_db_models()`（取代 `db_models=None` 的既有 stub） |
| `main.py` | 08a 十二章 | `initial_state` 新增兩個新欄位 |
| `tests/scaffold_agent/`、`tests/graph/test_scaffold_node.py` | 對應各章 | 單元測試，見各節「已驗證」 |

**已驗證**：`python -m pytest tests/ -q` 全數通過。合成測試資料之外，也對真實 `lang-exam-api-refactor` 專案跑過 `build_db_models()`（`tests/design_agent/fixtures/real_module_list.json`，① 解析 Agent 的既有真實輸出，47 個 java 檔案、6 個 module），產出的檔案手動驗證過 `ast.parse()` 合法、`import` 成功、`create_all()` 建表成功——這次真實驗證發現並修正了「entity 完全沒有可辨識主鍵時會在 import 階段拋錯」這個合成測試資料沒踩到的真實缺口（見六章「一個 `@Id` 都沒有的 entity 要在渲染前跳過」）。合成測試（`test_model_builder.py::test_full_pipeline_end_to_end_create_all_with_sqlite`）涵蓋 `@MappedSuperclass` 繼承、`@Enumerated`、唯一約束、`@ManyToMany` 中介表、`@ManyToOne` 外鍵一次串起來；另手動驗證過 `build_db_models()` → `translator_cli.generate_scaffold()` 整合（自訂型別索引正確找到 `db_models` 產出的 model class）。

---

## 一、與 08a 十一章套件結構的落差

### 省略 `exceptions.py`

`scaffold_agent` 的失敗路徑只有兩種，都不需要自訂例外型別：`javalang.parser.JavaSyntaxError`（08a 四章「掃描失敗的處理」）原樣往上拋，沿用 `javalang` 自己的例外；其餘所有失敗（欄位型別解析不到、entity 撞名、`@Table` 缺席、渲染後語法不合法……）一律走 `skipped_entities`／`logger.warning()`（08a 九章「逐 entity 隔離失敗」），不是例外。沒有第三種「呼叫端需要 `except` 特定型別決定要不要重試」的情境（`build_db_models()` 不在 `retry_count` 迴圈內，見 08a 十三章），因此不需要 `translator_cli.exceptions` 那種例外階層。

---

## 二、`types.py`——對外型別與內部共用資料結構

對應 08a 二、四、六、九章。

```python
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
```

**`PYTHON_TO_SQLALCHEMY` 定義在這裡而不是 `column_mapping.py`**：`reference_resolver.py` 判斷主鍵型別（六章 `primary_key_type`）也需要這張表；放在兩者共同的依賴基底，避免 `column_mapping.py`／`reference_resolver.py` 互相 import 造成循環。

**`primary_keys`／`fallback_pk_type` 存 canonical 型別字串，不是最終渲染文字**：`(python_type, sqlalchemy_type)` 只回答「這個型別是什麼」，不回答「怎麼寫成 Python 原始碼」——`"UUID"` 要渲染成 `PgUUID(as_uuid=True)` 這個轉換規則只有 `column_mapping.py` 知道，`reference_resolver.py` 不需要知道渲染細節。

---

## 三、`entity_scan.py`——javalang 掃描

對應 08a 四章全節。

```python
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
```

### javalang 的 annotation 元素值形狀

08a 四章定義的是「要接受什麼、拒絕什麼」的規則（Literal 規則），底下是 javalang 這個特定 parser 表達這些規則的具體資料形狀，08a 未涵蓋此層級細節：

1. **`Literal.value` 是未經解析的原始文字**：字串帶引號（`'"users"'`）、布林是不帶引號的 `"false"`／`"true"` 字面字串、數字是數字字面字串。`_literal_value()` 需先判斷形狀才能還原成 Python `str`／`bool`／`int`。
2. **`Annotation.element` 有三種形狀**：marker annotation（如 `@Id`）是 `None`；多個具名元素（如 `@Column(nullable = false, length = 100)`）是 `list[ElementValuePair]`；只有一個未具名位置元素（如 `@Enumerated(EnumType.STRING)`）javalang 直接回傳該節點本身，不包一層 `list`。`_annotation_elements()` 把第三種正規化成 `{"value": 節點}`（JPA 規格裡未具名位置元素本來就叫 `value`），統一成同一種 `dict` 介面。
3. **陣列語法可能是單一節點或 `ElementArrayValue`**：`@UniqueConstraint(columnNames = ...)`、`@JoinTable` 的 `joinColumns`／`inverseJoinColumns` 都可能是單一值或 `{...}` 陣列。`_get_literal_array()`／`_first_join_column_name()`／`_extract_unique_constraints()` 統一用 `getattr(node, "values", None) or [node]` 同時支援兩種語法。

---

## 四、`reference_resolver.py`——兩階段全域索引

對應 08a 六章全節。

```python
# scaffold_agent/reference_resolver.py
"""兩階段全域索引建構，對應 08a 六章全節。第一階段（`build_scan_index()`）
掃完全部 module，建立跨檔案查找用的 `ScanIndex`；第二階段（逐 module
渲染）交給 `model_builder.py`，本檔案不碰任何渲染邏輯。
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict

from common.java_type_mapping import camel_to_snake, map_java_type
from scaffold_agent import entity_scan
from scaffold_agent.types import (
    PYTHON_TO_SQLALCHEMY,
    EntityRecord,
    EnumRecord,
    FieldRecord,
    ModuleInfo,
    ScanIndex,
)

logger = logging.getLogger(__name__)


def _fqn(package: str | None, class_name: str, file_path: str) -> str:
    """對應六章「key 是 FQN，不是單純的 class_name」：`package` 為
    `None`（沒有 package 宣告）時改用檔案相對路徑當前綴——
    `module_list.java_files` 裡的路徑本來就保證唯一，碰撞機率直接歸零。
    """
    if package is None:
        return f"{file_path}::{class_name}"
    return f"{package}.{class_name}"


def resolve_reference(simple_name: str, referencing_package: str | None, referencing_import_map: dict[str, str]) -> str | None:
    """對應六章「跨檔案型別參照解析」。只算候選 FQN，不驗證是否真的存在
    於任何索引裡（existence check 由呼叫端自己查 `entities`／`enums`／
    `mapped_superclasses`，見六章「existence check 一律由呼叫端做」）。

    1. 顯式 import 命中
    2. 同 package 隱式參照（`referencing_package` 為 `None` 時，這一步
       無法安全推論，直接落到第 3 步——`resolve_reference()` 需要一個
       具體的 package 字串才能組出候選 FQN；六章原文描述的「同 package」
       規則本身就預設呼叫端有 package 可用，這是它結構上的前提）
    3. 兩者都沒命中 → `None`（wildcard import 刻意不解析，見六章）
    """
    if simple_name in referencing_import_map:
        return referencing_import_map[simple_name]
    if referencing_package:
        return f"{referencing_package}.{simple_name}"
    return None


def _resolve_entity_name_collisions(entities: dict[str, EntityRecord]) -> list[dict]:
    """對應六章「簡短類別名稱跨 package 撞名的偵測」。落在不同 module
    只記警告；落在同一個 module 用 **FQN 字典序**做 tie-break（跟
    `_build_join_table_owners()` 同一種理由：不依賴 `entities` 的插入
    順序，也就是不依賴 `module_list`——那是 Agent ①（LLM）產出的順序，
    不是這個系統裡有語意保證的排序），只保留字典序最小的那個，其餘從
    `entities`（就地修改）移除並記進回傳的 `skipped_entities`。

    **這一步必須在呼叫端（`build_scan_index()`）計算
    `effective_fields`／`primary_keys`／`fk_use_alter_edges` 之前執行**：
    輸家從 `entities` 被移除後，任何指向它的 `@ManyToOne`／`@OneToOne`
    外鍵，`resolve_reference()` 算出的候選 FQN 在 `entities` 裡已經查
    不到，會自然落入八章「外鍵解析失敗的降級處理」（`render_foreign_
    key()` 的 `target is None` 分支：降級成 `fallback_pk_type` 型別 ＋
    TODO 註解），而不是留著一個看似有效、實際上永遠不會被渲染出來的
    entity 讓其他欄位的 `ForeignKey(...)` 指向一個 `create_all()` 找不到
    的 table（那樣會在 mapper 設定階段直接炸 `NoReferencedTableError`，
    是比「語法合法、留 TODO」嚴重得多的失敗模式）。
    """
    by_short_name: dict[str, list[str]] = defaultdict(list)
    for fqn, record in sorted(entities.items()):
        by_short_name[record.class_name].append(fqn)

    skipped: list[dict] = []
    to_remove: list[str] = []
    for class_name, fqns in by_short_name.items():
        if len(fqns) <= 1:
            continue
        logger.warning(
            "class_name %r 在 %d 個不同 FQN 間碰撞：%s（見 08a 六章「簡短類別名稱跨 package 撞名」）",
            class_name,
            len(fqns),
            fqns,
        )
        by_module: dict[str, list[str]] = defaultdict(list)
        for fqn in fqns:
            by_module[entities[fqn].module].append(fqn)
        for module, module_fqns in by_module.items():
            if len(module_fqns) <= 1:
                continue
            keep, *rest = module_fqns
            for fqn in rest:
                to_remove.append(fqn)
                skipped.append(
                    {
                        "file_path": entities[fqn].file_path,
                        "class_name": entities[fqn].class_name,
                        "error": f"同一 module ({module}) 內類別名稱碰撞（與 {keep} 撞名，見 08a 六章）",
                    }
                )
                logger.warning(
                    "同一 module (%s) 內 class_name %r 碰撞：跳過 %s，保留 %s", module, class_name, fqn, keep
                )

    for fqn in to_remove:
        del entities[fqn]
    return skipped


def _collect_ancestor_field_layers(
    entity: EntityRecord,
    entities: dict[str, EntityRecord],
    mapped_superclasses: dict[str, EntityRecord],
) -> list[list[FieldRecord]]:
    """對應六章「`@MappedSuperclass` 欄位繼承合併」：沿 `extends` 鏈往上
    走，每一層若命中 `mapped_superclasses` 就收下它的 `fields` 並繼續
    往上；命中 `entities`（父類別本身是另一個 `@Entity`，即 JPA
    `@Inheritance`）記警告並停止（08a 六章第 3 點、十章已知限制）；
    解析不到（`None`）是正常情況，不記警告，直接停止。回傳由「最頂層
    祖先在前」排序的欄位層清單。
    """
    layers: list[list[FieldRecord]] = []
    current = entity
    seen: set[str] = set()
    while current.extends is not None:
        parent_fqn = resolve_reference(current.extends, current.package, current.import_map)
        if parent_fqn is None or parent_fqn in seen:
            break
        seen.add(parent_fqn)
        parent = mapped_superclasses.get(parent_fqn)
        if parent is not None:
            layers.append(parent.fields)
            current = parent
            continue
        if parent_fqn in entities:
            logger.warning(
                "%s.%s：父類別 %s 是另一個 @Entity（JPA @Inheritance），本實作不解析繼承策略，"
                "欄位不會被合併（見 08a 六章第 3 點、十章已知限制）",
                entity.package,
                entity.class_name,
                parent_fqn,
            )
        break
    return list(reversed(layers))


def _merge_effective_fields(
    entity: EntityRecord, entities: dict[str, EntityRecord], mapped_superclasses: dict[str, EntityRecord]
) -> list[FieldRecord]:
    """對應六章合併演算法第 4 點：依「最頂層祖先在前，entity 自己最後」
    接續，同一個 Python 屬性名稱在多層出現時只保留較近層級（離 entity
    本身較近）的那一筆。用 `camel_to_snake()` 判斷屬性名稱是否相同——
    這正是七章渲染欄位時實際使用的 Python 識別字，不是原始 Java 欄位名。
    """
    ordered: list[FieldRecord] = []
    for layer in _collect_ancestor_field_layers(entity, entities, mapped_superclasses):
        ordered.extend(layer)
    ordered.extend(entity.fields)

    position_of: dict[str, int] = {}
    result: list[FieldRecord] = []
    for record in ordered:
        attr = camel_to_snake(record.name)
        if attr in position_of:
            result[position_of[attr]] = record
        else:
            position_of[attr] = len(result)
            result.append(record)
    return result


_SQLALCHEMY_TO_PYTHON = {v: k for k, v in PYTHON_TO_SQLALCHEMY.items()}


def _pk_type_info(field: FieldRecord) -> tuple[str, str]:
    """把欄位型別轉成 `(canonical Python 型別, canonical SQLAlchemy
    Column 型別)`，供 `ScanIndex.primary_keys`／`fallback_pk_type` 使用。
    `"UUID"` 是唯一的特例 sentinel（見 `types.PYTHON_TO_SQLALCHEMY`
    docstring）——`column_mapping.py` 渲染實際欄位時才把它換成完整的
    `PgUUID(as_uuid=True)` 寫法，這裡只需要一組可比較、可統計次數的
    穩定字串。不在 `PYTHON_TO_SQLALCHEMY` 表裡的型別（理論上不該拿來
    當主鍵，如自訂 Enum）保守退回 `("int", "Integer")`。
    """
    python_type = map_java_type(field.java_type)
    if python_type == "UUID":
        return "UUID", "UUID"
    if python_type in PYTHON_TO_SQLALCHEMY:
        return python_type, PYTHON_TO_SQLALCHEMY[python_type]
    return "int", "Integer"


def _compute_primary_key(fields: list[FieldRecord]) -> tuple[str, str, str] | None:
    """對應六章「`primary_key_column`／`primary_key_type`」：取有效欄位
    清單裡標 `@Id` 的那一個；`0` 個或 `>1` 個（複合主鍵，見 08a 十章已知
    限制）都算「主鍵不明確」，回傳 `None`。
    """
    id_fields = [f for f in fields if f.is_id]
    if len(id_fields) != 1:
        return None
    field = id_fields[0]
    python_type, sqlalchemy_type = _pk_type_info(field)
    return camel_to_snake(field.name), python_type, sqlalchemy_type


def _fallback_pk_type(primary_keys: dict[str, tuple[str, str, str] | None]) -> tuple[str, str]:
    """對應六章「降級外鍵的型別 fallback」：資料驅動的多數決，依
    `sqlalchemy_type` 統計出現次數，並列時 `Integer` 優先；整個專案沒有
    任何主鍵型別可統計時才真正退回 `("int", "Integer")`。
    """
    counter = Counter(pk[2] for pk in primary_keys.values() if pk is not None)
    if not counter:
        return "int", "Integer"
    max_count = max(counter.values())
    winners = sorted(t for t, c in counter.items() if c == max_count)
    sqlalchemy_type = "Integer" if "Integer" in winners else winners[0]
    python_type = "UUID" if sqlalchemy_type == "UUID" else _SQLALCHEMY_TO_PYTHON.get(sqlalchemy_type, "int")
    return python_type, sqlalchemy_type


def _tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """標準 Tarjan 強連通分量演算法，供「外鍵依賴圖與循環偵測」use。
    節點數不大（單一 Java 專案的 entity 數量），遞迴實作足夠，不需要
    改寫成迭代版本。
    """
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    result: list[list[str]] = []

    def strongconnect(node: str) -> None:
        indices[node] = index_counter[0]
        lowlink[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in graph.get(node, ()):
            if neighbor not in indices:
                strongconnect(neighbor)
                lowlink[node] = min(lowlink[node], lowlink[neighbor])
            elif neighbor in on_stack:
                lowlink[node] = min(lowlink[node], indices[neighbor])

        if lowlink[node] == indices[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            result.append(component)

    for node in graph:
        if node not in indices:
            strongconnect(node)
    return result


def _build_fk_use_alter_edges(
    entities: dict[str, EntityRecord], effective_fields: dict[str, list[FieldRecord]], primary_keys: dict[str, tuple[str, str] | None]
) -> set[tuple[str, str]]:
    """對應六章「外鍵依賴圖與循環偵測」：只對真正成環（SCC 節點數 > 1）
    的邊標記 `use_alter`，自我參照不算邊。
    """
    graph: dict[str, set[str]] = {fqn: set() for fqn in entities}
    for fqn, entity in entities.items():
        for field in effective_fields[fqn]:
            if field.relation not in ("ManyToOne", "OneToOne"):
                continue
            if field.relation == "OneToOne" and field.mapped_by is not None:
                continue
            target_fqn = resolve_reference(field.java_type, field.declaring_package, field.declaring_import_map)
            if target_fqn is None or target_fqn == fqn or target_fqn not in entities:
                continue
            if primary_keys.get(target_fqn) is None:
                continue
            graph[fqn].add(target_fqn)

    cyclic_nodes: set[str] = set()
    for component in _tarjan_scc(graph):
        if len(component) > 1:
            cyclic_nodes.update(component)

    edges: set[tuple[str, str]] = set()
    for source, targets in graph.items():
        if source not in cyclic_nodes:
            continue
        for target in targets:
            if target in cyclic_nodes:
                edges.add((source, target))
    return edges


def _build_join_table_owners(entities: dict[str, EntityRecord], effective_fields: dict[str, list[FieldRecord]]) -> dict[str, str]:
    """對應六章「`@ManyToMany` 中介表的擁有者」：只在「兩側都宣告
    `@JoinTable`」這種不規範寫法才需要 tie-break，任一種確定性排序都
    行——這裡選 **FQN 字典序**，不是 `entities` 的插入順序（＝
    `module_list` 原始順序）。理由：`module_list` 的順序由 Agent ①
    （LLM）產出，不是這個系統裡有語意保證的排序（不像同 module 內
    `depends_on` 那樣是刻意設計的依賴拓撲）；FQN 字典序不依賴上游 Agent
    的輸出順序、人工看 log 也能直接理解「為什麼是這個 entity 贏」，是
    更自我解釋、跟上游解耦的確定性依據。
    """
    owners: dict[str, str] = {}
    for fqn, entity in sorted(entities.items()):
        for field in effective_fields[fqn]:
            if field.relation != "ManyToMany" or field.join_table is None:
                continue
            name = field.join_table.name
            if name not in owners:
                owners[name] = fqn
            else:
                logger.warning(
                    "中介表 %r 被多個 entity 宣告 @JoinTable（%s、%s），只有先登記的 %s 會渲染這張表"
                    "（見 08a 六章「@ManyToMany 中介表的擁有者」，這是不規範的 Java 原始碼）",
                    name,
                    owners[name],
                    fqn,
                    owners[name],
                )
    return owners


def _build_enum_python_names(enums: dict[str, EnumRecord]) -> dict[str, str]:
    """對應七章「同名碰撞改在 `_enums.py` 自己的命名階段解決」：三層
    tier（裸名稱 → `{class_name}_{module}` → `{class_name}_{package 或
    file 前綴}`），第 2 步已經唯一的成員維持較短命名，不因同組其他成員
    需要退到第 3 步而跟著降級。
    """
    by_short_name: dict[str, list[str]] = defaultdict(list)
    for fqn, record in enums.items():
        by_short_name[record.class_name].append(fqn)

    result: dict[str, str] = {}
    for class_name, fqns in by_short_name.items():
        if len(fqns) == 1:
            result[fqns[0]] = class_name
            continue

        tier2: dict[str, list[str]] = defaultdict(list)
        for fqn in fqns:
            tier2[f"{class_name}_{enums[fqn].module}"].append(fqn)

        for candidate, group_fqns in tier2.items():
            if len(group_fqns) == 1:
                result[group_fqns[0]] = candidate
                continue
            for fqn in group_fqns:
                if "::" in fqn:
                    prefix = fqn.split("::", 1)[0].replace("/", "_").replace(".", "_")
                else:
                    prefix = fqn.rsplit(".", 1)[0].replace(".", "_")
                result[fqn] = f"{class_name}_{prefix}"
    return result


def build_scan_index(java_project_path: str, module_list: list[ModuleInfo]) -> ScanIndex:
    """對應六章「兩階段：先建全域索引，再逐 module 渲染」第一階段。
    `module_list` 的原始順序即 `ScanIndex.entities`／`join_table_owners`
    tie-break 依據的順序（見各自函式 docstring）——插入順序由這裡的
    迭代順序保證，下游函式不重新排序。
    """
    scan_index = ScanIndex()

    for module in module_list:
        entities, enums = entity_scan.scan_module(java_project_path, module)
        for record in entities:
            fqn = _fqn(record.package, record.class_name, record.file_path)
            if record.jpa_kind == "Entity":
                scan_index.entities[fqn] = record
            elif record.jpa_kind == "MappedSuperclass":
                scan_index.mapped_superclasses[fqn] = record
            else:  # "Embeddable"，見十章已知限制
                scan_index.embeddables[fqn] = record
        for enum_record in enums:
            fqn = _fqn(enum_record.package, enum_record.class_name, enum_record.file_path)
            scan_index.enums[fqn] = enum_record

    scan_index.duplicate_entity_skips = _resolve_entity_name_collisions(scan_index.entities)

    for fqn, entity in scan_index.entities.items():
        scan_index.effective_fields[fqn] = _merge_effective_fields(
            entity, scan_index.entities, scan_index.mapped_superclasses
        )

    for fqn in scan_index.entities:
        scan_index.primary_keys[fqn] = _compute_primary_key(scan_index.effective_fields[fqn])

    scan_index.fallback_pk_type = _fallback_pk_type(scan_index.primary_keys)
    scan_index.fk_use_alter_edges = _build_fk_use_alter_edges(
        scan_index.entities, scan_index.effective_fields, scan_index.primary_keys
    )
    scan_index.join_table_owners = _build_join_table_owners(scan_index.entities, scan_index.effective_fields)
    scan_index.enum_python_names = _build_enum_python_names(scan_index.enums)

    entity_class_names = {record.class_name for record in scan_index.entities.values()}
    enum_final_names = set(scan_index.enum_python_names.values())
    for name in entity_class_names & enum_final_names:
        logger.warning(
            "Entity 與 Enum 跨型別撞名：%r 同時是某個 Entity 與某個 Enum 的最終渲染類別名稱"
            "（見 08a 六章「跨型別撞名」、十章已知限制）",
            name,
        )

    return scan_index
```

### 實作補充

- **`_tarjan_scc()`**：08a 六章只定義了輸入輸出契約（依賴圖 → 只對真正成環的邊標記 `use_alter`），未指定演算法。Tarjan 是單次 DFS（`O(V+E)`）找出所有強連通分量的標準演算法，不需額外依賴。
- **`_pk_type_info()`／`_fallback_pk_type()` 回傳 `(python_type, sqlalchemy_type)` pair，不是單一字串**：`column_mapping.py` 渲染 `Mapped[...]` 型別註記需要 Python 型別，渲染 `mapped_column(...)` 需要 SQLAlchemy Column 型別；由 `reference_resolver.py` 一次算出兩者，`column_mapping.py` 直接用，不需反推。
- **`resolve_reference()` 對 `referencing_package is None`**：08a 六章第 2 步「同 package 隱式參照」隱含假設呼叫端有 package 可用；`referencing_package` 為 `None` 時（對應 `entity_scan.py` 沒有 `package` 宣告的 fallback FQN 情境）直接跳過第 2 步、回傳 `None`，與第 3 步「都沒命中」走同一條路徑。
- **`_build_join_table_owners()` 的 tie-break 依 FQN 字典序，不是 `entities` 的插入順序**：初版實作沿用 `entities.items()` 原始迭代順序（＝ `module_list` 掃描順序）決定「兩側都宣告 `@JoinTable`」時哪一側算擁有者。改成 `sorted(entities.items())`——`module_list` 的順序由 Agent ①（LLM）產出，不是這個系統裡有語意保證的排序，FQN 字典序不依賴上游輸出順序，人工看 log 也能直接理解「為什麼是這個 entity 贏」（見 08a 六章）。只影響「兩側都宣告」這種不規範寫法的 tie-break，正常單側宣告不受影響。
- **`_resolve_entity_name_collisions()` 的 tie-break 同步改成 FQN 字典序**：這是跟上一項同一類問題（多筆宣告衝突，需要確定性選一個贏家），初版只改了 `join_table_owners`，這裡漏改仍沿用 `entities.items()` 插入順序，是文件內部論證不一致——已同步改成 `sorted(entities.items())`。
- **`_resolve_entity_name_collisions()` 必須在 `build_scan_index()` 計算 `effective_fields`／`primary_keys`／`fk_use_alter_edges` 之前呼叫**：這不是效能考量，是正確性前提。輸家從 `scan_index.entities` 移除後，其他 entity 對它的 `@ManyToOne`／`@OneToOne` 才會正確落入「外鍵解析失敗的降級處理」（`target = scan_index.entities.get(target_fqn)` 查不到，走 TODO 註解＋`fallback_pk_type`）；若晚於 FK 圖／欄位渲染才移除，其他 entity 的外鍵會指向一個永遠不會被渲染出來的 table，`create_all()`／mapper 設定階段直接拋 `NoReferencedTableError`（見 08a 六章）。已用真實 sqlite `create_all()` 驗證過這個順序正確——三個 entity（含撞名的兩個、外鍵指向輸家的第三個）成功建表，沒有懸空 FK。

---

## 五、`column_mapping.py`——欄位渲染

對應 08a 七、八章全節。

```python
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
```

### 主鍵欄位不帶 `nullable=`

08a 七章「渲染範例」的 `id` 欄位是 `mapped_column(Integer, primary_key=True, autoincrement=True)`，沒有 `nullable=` 引數、型別註記也不是 `int | None`——`primary_key=True` 已隱含 `NOT NULL`，額外帶 `nullable=True`（七章 nullable 判斷規則對 `Long id` 這種包裝類別欄位無差別套用會得到的結果）是自相矛盾的宣告。`_render_known_scalar_field()`／`_render_uuid_field()` 因此在 `record.is_id` 為真時整條跳過 `nullable=` 的計算與輸出，對齊 08a 範例的實際寫法。

### `render_join_table()` 不判斷擁有端／非擁有端

08a 八章「只有擁有端渲染」的判斷交給呼叫端（`model_builder._render_many_to_many()`），`render_join_table()` 本身不查 `scan_index.join_table_owners`——`column_mapping.py` 每個 `render_*` 函式職責統一是「給定已確認要渲染的欄位，回傳渲染結果」，是否要渲染由呼叫端決定。

### 中介表要帶擁有端的 `schema`

`render_join_table()` 組裝 `ForeignKey(...)` 目標時已經正確帶入 `entity.schema`／`inverse_entity.schema` 前綴，但初版實作漏了把 `entity.schema`（擁有端自己的 schema）傳給 `Table(...)` 建構子本身——`ForeignKey("hr.users.id")` 指向的目標正確帶 schema，但中介表這個 `Table` 物件卻沒有指定 `schema=`，落在 SQLAlchemy 預設 schema（通常是 `public`），跟它引用的 FK 目標所在的 schema 不一致。已在 `entity.schema` 存在時多帶一行 `schema="{entity.schema}"`（見 08a 八章「中介表沿用擁有端的 schema」）；已用真實 sqlite `create_all()` 驗證過中介表確實落在 `hr.user_roles`，不是 `user_roles`。

### 中介表變數名稱需要正規化，`camel_to_snake()` 解決不了

初版實作直接把 `join_table.name`（`@JoinTable(name = "...")` 的字面值）當賦值左手邊的 Python 變數名稱——企業級 DB 常見的連字號命名（如 `"user-roles"`）會產生 `SyntaxError`，靠九章的 AST 隔離驗證能接住、不拖垮整個檔案，但這張表就完全從 `db_models` 消失了，不是理想結果。改用 `_sanitize_python_identifier()`：把非英數字元、非底線的字元全部換成底線，開頭是數字再補一個底線前綴，只換賦值左手邊的識別字，`Table(...)` 第一個位置引數（實際 DB table 名稱）維持原始字面值：

```python
user_roles = Table(
    "user-roles",
    Base.metadata,
    ...
)
```

**不能用 `camel_to_snake()`（05a／08a 五章共用）處理這個問題**：它是依 camelCase 大小寫邊界切詞界的轉換，對 `"user-roles"` 這種完全沒有大小寫變化的輸入是 no-op（實測 `camel_to_snake("user-roles")` 原樣回傳 `"user-roles"`），連字號原樣保留，套用它並不會修好 `SyntaxError`，需要另一條專門的「非法識別字元替換」規則。正規化後仍可能剛好撞上 Python 保留字（如 table 剛好取名 `"class"`）——九章的 AST 隔離驗證仍是最後一道防線，接住這種殘留情況，不需要在正規化這一層額外處理保留字清單。

### 容器型別必須跳過，不能降級成 `String`

`render_scalar_field()` 一開始就檢查正規化後的 `python_type` 是否含 `[`／`]`（`map_java_type()` 把 `List<String>` 這類集合型別展開成 `"list[str]"`，或不支援的陣列形狀原樣保留方括號），命中即記警告、回傳 `None`——不落入後面「無法解析型別」那條 `_render_unresolved_scalar_field()` 降級路徑。這對應 08a 七章對應表原文「不渲染這個欄位，記警告並跳過…結構上不可能是單一純量 column，沒有『降級』這個選項」，初版實作漏了這個檢查，會把沒有 JPA 關聯 annotation、卻是集合型別的欄位（如純量誤用 `List<String>`）誤判成「自訂型別解析不到」，降級渲染成一個看似合法、實則語意錯誤的 `String` 欄位。

### `_GENERIC_WRAPPER_RE` 放寬支援點號，但這不是完整修法

`_unwrap_collection_type()` 用來取出 `@ManyToMany` 欄位（`List<Role>` 這類泛型包裝）的內層目標型別，外層 wrapper 名稱的比對已放寬成 `[\w.]+`（原本只有 `\w+`），能接住 `java.util.List<Role>` 這種完整套件路徑內嵌寫法。但這只解決了「正則本身」這一層——真正會讓這個情境失敗的是共用的 `common.java_type_mapping.type_str()`：它把 javalang `ReferenceType` 節點還原成型別字面字串時只讀 `.name`／`.arguments`，不展開 `.sub_type` 鏈，對 `java.util.List<Role>` 這種內嵌完全限定名稱會提早截斷成只剩第一段 `"java"`（已用 javalang 實際輸出驗證過），字串到不了這個正則就已經是錯的。`type_str()` 同時也被 `design_agent/signature_scan.py`（③ 方法簽名掃描）共用，修正它需要一併驗證不影響既有的方法簽名掃描結果，這裡先只做正則本身的防禦性放寬、不擴大改動範圍到共用檔案——真實 Java 專案幾乎一律用 `import` 陳述式＋裸型別名稱，內嵌完全限定泛型是低機率情境，記錄在這裡作為已知限制，不在本文件範圍內解決。

---

## 六、`model_builder.py`——逐 entity 隔離驗證與檔案組裝

對應 08a 九章全節。

```python
# scaffold_agent/model_builder.py
"""逐 entity 隔離驗證、組裝 `app/models/{module}.py` 與共用
`app/models/_enums.py`，對應 08a 九章全節。本檔案是 `column_mapping.py`
唯一的呼叫端——`build_db_models()`（`__init__.py`）不直接呼叫
`column_mapping`，只呼叫這裡的 `assemble()`。
"""
from __future__ import annotations

import ast
import logging
from collections import defaultdict

from common.java_type_mapping import camel_to_snake
from scaffold_agent.column_mapping import merge_imports, render_field, render_join_table
from scaffold_agent.types import BuildDbModelsResult, EntityRecord, FieldRecord, ModuleInfo, ScanIndex

logger = logging.getLogger(__name__)


def _table_name(entity: EntityRecord) -> str:
    """對應六章「Table 名稱決定規則」：`@Table` 缺席時 fallback
    `camel_to_snake(class_name)`，記一筆警告——這批 entity 若事後被
    Harness 局部驗證發現對不到真實 table，屬於預期中會發生、需要人工
    介入的情況，不阻擋 pipeline。
    """
    if entity.table_name:
        return entity.table_name
    logger.warning(
        "%s.%s：缺少 @Table(name=...)，fallback 用 camel_to_snake(class_name) 當 table 名稱（見 08a 六章）",
        entity.package,
        entity.class_name,
    )
    return camel_to_snake(entity.class_name)


def _render_table_args(table_name: str, entity: EntityRecord) -> tuple[str | None, dict[str, set[str]]]:
    """對應七章「唯一約束」＋「`@Table(schema = "...")`：多 schema
    支援」。`UniqueConstraint` 的 `name=` 固定用
    `f"uq_{table_name}_{'_'.join(columns)}"`（七章）。單一
    `UniqueConstraint` 且無 `schema` 時，尾隨逗號不能省略（七章「單元素
    tuple」陷阱）。
    """
    unique_exprs = []
    for columns in entity.unique_constraints:
        name = f"uq_{table_name}_{'_'.join(columns)}"
        args = ", ".join(repr(c) for c in columns)
        unique_exprs.append(f'UniqueConstraint({args}, name="{name}")')
    schema_literal = repr(entity.schema) if entity.schema else None

    if not unique_exprs and schema_literal is None:
        return None, {}

    imports: dict[str, set[str]] = {"sqlalchemy": {"UniqueConstraint"}} if unique_exprs else {}

    if unique_exprs and schema_literal is not None:
        stmt = f'__table_args__ = ({", ".join(unique_exprs)}, {{"schema": {schema_literal}}})'
    elif unique_exprs:
        joined = f"{unique_exprs[0]}," if len(unique_exprs) == 1 else ", ".join(unique_exprs)
        stmt = f"__table_args__ = ({joined})"
    else:
        stmt = f"__table_args__ = {{\"schema\": {schema_literal}}}"
    return stmt, imports


def _render_many_to_many(
    entity_fqn: str,
    entity: EntityRecord,
    field: FieldRecord,
    scan_index: ScanIndex,
    join_table_stmts: list[str],
    imports: dict[str, set[str]],
    skipped_join_tables: list[dict],
) -> None:
    """對應八章「`@ManyToMany`：產生中介表定義」。只有
    `scan_index.join_table_owners` 判定的擁有端才渲染；非擁有端
    （`mappedBy`，或 `@JoinTable` 存在但擁有者是別的 entity）不重複渲染
    也不記警告；`@JoinTable`／`mappedBy` 都缺席才記警告（見該章節）。
    `join_table_stmts`／`imports`／`skipped_join_tables` 就地累加，供
    呼叫端（`_render_entity_class()`）跟純量欄位的渲染結果合併進同一份
    檔案。

    **`Table(...)` 陳述式在這裡就地 `ast.parse()` 驗證，不是留給
    `_render_entity_class()` 最後的整個 class 驗證代勞**：對應九章步驟 2
    「整個 entity 的 class 定義（或八章 `@ManyToMany` 的 `Table(...)`
    定義）組完後，做一次 `ast.parse()` 驗證這個片段語法合法」——中介表
    定義是跟 entity class 定義平行、獨立的頂層陳述式，不屬於任何一個
    entity 的 class body，若不在這裡單獨驗證，語法錯誤的中介表會原樣
    流進 `_assemble_module_file()`，只在 `translator_cli.scaffold.
    build_files()` 的全域最終檢查才會被抓到——那個階段失敗是**整個檔案
    all-or-nothing**（見 07a 四章），會連累同一個 module 檔案裡其他本來
    正常的 entity 一起被跳過，違反九章「逐 entity／中介表隔離失敗」的
    設計原則。驗證失敗記進 `skipped_join_tables`（`class_name` 用中介表
    名稱，供比照 `skipped_entities` 既有欄位形狀），不寫入
    `join_table_stmts`。
    """
    if field.join_table is not None:
        if scan_index.join_table_owners.get(field.join_table.name) == entity_fqn:
            result = render_join_table(entity_fqn, entity, field, scan_index)
            if result is None:
                logger.warning(
                    "%s.%s 欄位 %s：@JoinTable 主鍵或目標型別不明確，無法產生中介表定義（見 08a 八章）",
                    entity.package,
                    entity.class_name,
                    field.name,
                )
                return
            stmt, stmt_imports = result
            try:
                ast.parse(stmt)
            except SyntaxError as exc:
                skipped_join_tables.append(
                    {
                        "file_path": entity.file_path,
                        "class_name": field.join_table.name,
                        "error": f"中介表 Table(...) 定義無法通過 ast.parse()（見 08a 九章步驟 2）：{exc}",
                    }
                )
                logger.warning(
                    "%s.%s 欄位 %s：中介表 %r 渲染後語法不合法，跳過此中介表（見 08a 九章）：%s",
                    entity.package,
                    entity.class_name,
                    field.name,
                    field.join_table.name,
                    exc,
                )
                return
            join_table_stmts.append(stmt)
            for module, names in stmt_imports.items():
                imports.setdefault(module, set()).update(names)
        # else：擁有者是別的 entity，非擁有端，不重複渲染，不記警告（見八章）
        return
    if field.mapped_by is not None:
        return  # mappedBy 非擁有端，不渲染，不記警告
    logger.warning(
        "%s.%s 欄位 %s：@ManyToMany 完全沒有 @JoinTable 也沒有 mappedBy，"
        "無法機械決定中介表名稱與欄位名稱，不猜表名（見 08a 八章）",
        entity.package,
        entity.class_name,
        field.name,
    )


def _render_entity_class(
    entity_fqn: str, entity: EntityRecord, scan_index: ScanIndex
) -> tuple[str | None, dict[str, set[str]], list[str], list[dict]]:
    """對應九章「逐 entity 隔離失敗」：單一欄位渲染失敗只跳過那個欄位
    （記警告）；整個 class 定義組完後 `ast.parse()` 驗證一次，失敗才讓
    整個 entity 落入 `skipped_entities`（回傳 `(None, {}, [], ...)`）。
    `@ManyToMany` 中介表的 `Table(...)` 陳述式是跟 class 定義平行、獨立
    的頂層陳述式，各自的 `ast.parse()` 驗證在 `_render_many_to_many()`
    內部就地完成（見該函式 docstring），不受 class 本身是否驗證通過
    影響。回傳 `(class 定義文字, import 需求, 這個 entity 名下驗證通過的
    join table 陳述式, 這個 entity 名下驗證失敗的 join table 清單)`。
    """
    imports: dict[str, set[str]] = {}
    body_lines: list[str] = []
    join_table_stmts: list[str] = []
    skipped_join_tables: list[dict] = []

    for field in scan_index.effective_fields[entity_fqn]:
        if field.relation == "ManyToMany":
            _render_many_to_many(
                entity_fqn, entity, field, scan_index, join_table_stmts, imports, skipped_join_tables
            )
            continue

        rendered = render_field(entity_fqn, entity, field, scan_index)
        if rendered is None:
            continue
        try:
            ast.parse(rendered.text)
        except SyntaxError as exc:
            logger.warning(
                "%s.%s 欄位 %s 渲染後語法不合法，跳過此欄位（見 08a 九章「逐 entity 隔離失敗」）：%s",
                entity.package,
                entity.class_name,
                field.name,
                exc,
            )
            continue
        body_lines.extend(f"    {line}" if line else "" for line in rendered.text.splitlines())
        imports = merge_imports(imports, rendered.from_imports)

    table_name = _table_name(entity)
    table_args_stmt, table_args_imports = _render_table_args(table_name, entity)
    imports = merge_imports(imports, table_args_imports)

    class_lines: list[str] = []
    if entity.soft_delete_clause:
        # 對應七章「邏輯刪除標記」：db_models 走原始字串直寫（見九章
        # 「組裝順序」），這行註解不會被 ast.unparse() 丟掉。
        class_lines.append(
            "# TODO: 此實體原有邏輯刪除過濾條件（deleted = false），SQLAlchemy 未自動套用，"
        )
        class_lines.append("# 所有查詢須手動加上對應條件，否則會讀到已刪除的資料")
    class_lines.append(f"class {entity.class_name}(Base):")
    class_lines.append(f'    __tablename__ = "{table_name}"')
    if table_args_stmt:
        class_lines.append(f"    {table_args_stmt}")
    class_lines.append("")
    # 九章步驟 3：同一個 class 分組若所有方法（這裡是欄位）都被跳過，
    # 仍要渲染出來，body 用 pass 佔位，維持檔案語法合法。
    class_lines.extend(body_lines if body_lines else ["    pass"])

    class_text = "\n".join(class_lines)
    try:
        ast.parse(class_text)
    except SyntaxError:
        return None, {}, [], skipped_join_tables
    return class_text, imports, join_table_stmts, skipped_join_tables


def _assemble_module_file(class_bodies: list[str], join_table_stmts: list[str], imports: dict[str, set[str]]) -> str:
    """對應九章「組裝順序」：先統一渲染 import 區塊，再依序接上每個
    entity 的 class 定義，最後才是這個檔案名下的中介表定義（若有）。
    `from app.core.database import Base` 無條件加入——這個檔案至少有
    一個 entity class 或一張中介表，兩者都需要 `Base`。
    """
    base_imports: dict[str, set[str]] = {"app.core.database": {"Base"}}
    if class_bodies:
        # 每個 entity class 都用 SQLAlchemy 2.0 的 Mapped/mapped_column
        # 宣告風格（七章「渲染風格」），無條件需要；join-table-only 的
        # 檔案（只有 Table(...) 賦值、沒有任何 entity class）不需要。
        base_imports["sqlalchemy.orm"] = {"Mapped", "mapped_column"}
    all_imports = merge_imports(imports, base_imports)
    header = ["from __future__ import annotations", ""]
    header.extend(f"from {module} import {', '.join(sorted(names))}" for module, names in sorted(all_imports.items()))
    parts = ["\n".join(header), *class_bodies, *join_table_stmts]
    return "\n\n\n".join(parts) + "\n"


def _render_enums_file(scan_index: ScanIndex) -> tuple[str | None, list[dict]]:
    """對應七章「Enum class 一律渲染進單一共用檔案 `app/models/_enums.py`」
    ＋九章步驟 5：逐 Enum 各自 `ast.parse()` 驗證，單一失敗不拖累其他
    Enum。`scan_index.enums` 為空時回傳 `(None, [])`（不產生這個檔案的
    key，見九章步驟 4 同一種「內容真的存在才產生 key」原則）。
    """
    if not scan_index.enums:
        return None, []

    class_bodies: list[str] = []
    skipped: list[dict] = []
    for fqn, record in scan_index.enums.items():
        python_name = scan_index.enum_python_names[fqn]
        lines = [f"class {python_name}(enum.Enum):"]
        lines.extend(f'    {member} = "{member}"' for member in record.members)
        text = "\n".join(lines)
        try:
            ast.parse(text)
        except SyntaxError as exc:
            skipped.append({"file_path": "app/models/_enums.py", "class_name": record.class_name, "error": str(exc)})
            continue
        class_bodies.append(text)

    if not class_bodies:
        return None, skipped

    header = "from __future__ import annotations\n\nimport enum\n"
    content = "\n\n\n".join([header, *class_bodies]) + "\n"
    return content, skipped


def assemble(scan_index: ScanIndex, module_list: list[ModuleInfo]) -> BuildDbModelsResult:
    """對外唯一入口，對應九章全節＋十一章 `build_db_models()` 內部呼叫
    的檔案組裝步驟。`module_list` 只用來保證輸出檔案涵蓋所有 module（即
    使某 module 完全沒有 entity，也不強行產生空檔案，見九章步驟 4）——
    entity 分組本身依 `scan_index.entities` 現有的 `module` 欄位，不重新
    走一次 `module_list.java_files` 比對。
    """
    db_models: dict[str, str] = {}
    skipped_entities: list[dict] = list(scan_index.duplicate_entity_skips)

    for record in scan_index.embeddables.values():
        skipped_entities.append(
            {
                "file_path": record.file_path,
                "class_name": record.class_name,
                "error": "@Embeddable 非獨立 table，本次範圍不處理（見 08a 十章已知限制）",
            }
        )

    by_module: dict[str, list[str]] = defaultdict(list)
    for fqn, entity in scan_index.entities.items():
        by_module[entity.module].append(fqn)

    for module in module_list:
        fqns = by_module.get(module["module"], [])
        if not fqns:
            continue

        class_bodies: list[str] = []
        join_table_stmts: list[str] = []
        file_imports: dict[str, set[str]] = {}

        for fqn in fqns:
            entity = scan_index.entities[fqn]

            # 對應六章「primary_key_column／primary_key_type 為 None」：
            # 原文只講這個 entity 被別人當外鍵目標時要走降級處理，沒有講
            # 這個 entity 自己完全沒有可辨識主鍵時該怎麼渲染自己。實測對
            # 真實 lang-exam-api-refactor 專案跑過 build_db_models() 才
            # 發現這個缺口：SQLAlchemy 宣告式 class 沒有任何
            # `primary_key=True` 欄位時，不是「語法合法但語意不完整」，
            # 是 import 當下就直接拋 `ArgumentError`（"could not assemble
            # any primary key columns"）——這個錯誤發生在 class 定義那一刻，
            # 會讓同一個檔案裡排在後面的其他 entity 連帶無法被定義，是比
            # 九章其餘任何降級路徑都嚴重的失敗等級（不是 ast.parse() 能
            # 攔到的語法問題，是要真的 import 才會炸的執行期錯誤）。
            #
            # **這裡刻意檢查「一個 @Id 都沒有」，不是直接沿用
            # `scan_index.primary_keys.get(fqn) is None`**：後者在複合主鍵
            # （多個 @Id 欄位）的情況下也會是 `None`（六章「主鍵不明確」
            # 的定義是 `len(id_fields) != 1`），但複合主鍵對 SQLAlchemy
            # 完全合法——每個 @Id 欄位各自標 `primary_key=True` 就能組成
            # 複合主鍵，`create_all()` 不會報錯（已用真實 SQLAlchemy 驗證
            # 過），只是「當外鍵目標」這件事做不到（八章既有的降級處理，
            # 跟這裡要防的「entity 自己 import 就炸」是完全不同的問題）。
            # 若這裡誤用 `primary_keys.get(fqn) is None` 當跳過條件，會
            # 連累複合主鍵這種本來能正常渲染的 entity 一起被跳過。
            if not any(f.is_id for f in scan_index.effective_fields[fqn]):
                skipped_entities.append(
                    {
                        "file_path": entity.file_path,
                        "class_name": entity.class_name,
                        "error": (
                            "沒有可辨識的主鍵（@Id 未標註，或標註在未被 module_list.java_files 掃描到的"
                            "父類別上），SQLAlchemy 宣告式 class 缺少 primary_key=True 欄位會在 import "
                            "當下直接拋 ArgumentError，因此不渲染這個 entity（見 08a 六章）"
                        ),
                    }
                )
                logger.warning(
                    "%s.%s：完全沒有 @Id 欄位（不是複合主鍵，是一個都沒有），跳過渲染，"
                    "否則 SQLAlchemy 會在 import 當下拋 ArgumentError（見 08a 六章）",
                    entity.package,
                    entity.class_name,
                )
                continue

            class_text, imports, entity_join_tables, entity_join_table_skips = _render_entity_class(
                fqn, entity, scan_index
            )
            # entity_join_table_skips 獨立於 class_text 是否驗證通過——
            # 中介表定義是跟 class 平行的頂層陳述式，各自的驗證結果各自
            # 記錄，不因為其中一邊失敗就連帶捨棄另一邊的失敗記錄。
            skipped_entities.extend(entity_join_table_skips)
            if class_text is None:
                skipped_entities.append(
                    {
                        "file_path": entity.file_path,
                        "class_name": entity.class_name,
                        "error": "組裝後的 class 定義無法通過 ast.parse()（見 08a 九章步驟 2）",
                    }
                )
                continue
            class_bodies.append(class_text)
            join_table_stmts.extend(entity_join_tables)
            file_imports = merge_imports(file_imports, imports)

        if class_bodies or join_table_stmts:
            db_models[f"app/models/{module['module']}.py"] = _assemble_module_file(
                class_bodies, join_table_stmts, file_imports
            )

    enums_content, enum_skips = _render_enums_file(scan_index)
    if enums_content is not None:
        db_models["app/models/_enums.py"] = enums_content
    skipped_entities.extend(enum_skips)

    return BuildDbModelsResult(db_models=db_models, skipped_entities=skipped_entities)
```

### `_render_many_to_many()` 的四種分支

08a 六章「`join_table_owners`」與八章渲染規則併成一段完整判斷邏輯：

1. `field.join_table is not None` 且擁有者是自己：渲染
2. `field.join_table is not None` 但擁有者是別的 entity：不渲染、不記警告（正常的非擁有端）
3. `field.join_table is None` 但 `field.mapped_by is not None`：不渲染、不記警告（標準雙向寫法的非擁有端）
4. `field.join_table is None` 且 `field.mapped_by is None`：不渲染、記警告（完全依賴 Hibernate 隱性命名慣例，機械掃描拿不到足夠資訊）

只有第 4 種需要 `logger.warning()`，第 2、3 種是正常、預期中的情況。

### 中介表 `Table(...)` 陳述式要單獨 `ast.parse()` 驗證，不能只靠 class 收尾時的驗證代勞

初版實作把 `render_join_table()` 產出的陳述式直接收進 `join_table_stmts`，只靠 `_render_entity_class()` 最後對「整個 class 定義」跑一次 `ast.parse()`——但中介表定義是跟 class 平行、獨立的頂層陳述式，不在任何 class 的 body 裡，那次驗證根本不會涵蓋到它。`_sanitize_python_identifier()`（見五章）上線後，連字號這類最常見的非法字元已經在渲染當下就被處理掉，但正規化後仍可能剛好撞上 Python 保留字（如 table 剛好取名 `"class"`，正規化不動它，`class = Table(...)` 依然是 `SyntaxError`）——這種殘留情況在初版實作裡完全不會被攔下，會一路流到 `translator_cli.scaffold.build_files()` 的全域最終檢查才爆，而那一層是整個檔案 all-or-nothing（見 07a 四章），會連累同一個 module 檔案裡其他本來正常的 entity 一起被跳過，違反 08a 九章步驟 2「entity 的 class 定義（或八章 `Table(...)` 定義）」逐一隔離驗證的設計。

修正：`_render_many_to_many()` 對 `render_join_table()` 的回傳值就地 `ast.parse()`，失敗記進新增的 `skipped_join_tables`（`class_name` 用中介表名稱），`_render_entity_class()` 回傳值多一個第四個元素回傳這份清單，`assemble()` 把它併入 `skipped_entities`——且這個併入動作在「entity class 本身是否驗證通過」的判斷之前，不會因為 class 驗證那條路徑先 `continue` 就漏接。

### 一個 `@Id` 都沒有的 entity 要在渲染前跳過，不能等 `ast.parse()`

`assemble()` 逐 entity 渲染前多一道檢查：`not any(f.is_id for f in scan_index.effective_fields[fqn])` 為真就直接跳過整個 entity、記進 `skipped_entities`，不呼叫 `_render_entity_class()`。這是接上真實 `lang-exam-api-refactor` 專案（見下方「已對真實專案驗證」）才發現的缺口——`ast.parse()` 攔不住這種錯誤：一個沒有任何 `primary_key=True` 欄位的 SQLAlchemy 宣告式 class，語法完全合法，但 **import 當下**會被 SQLAlchemy 直接拋 `ArgumentError`（"could not assemble any primary key columns"），而且是在 class 定義那一刻炸開，會讓同一個檔案裡排在它後面的其他 entity 連帶無法被定義——比九章其餘任何一種「語法合法但語意有問題」的降級路徑都嚴重。

**判斷條件刻意寫成「完全沒有 `@Id`」，不是直接沿用 `scan_index.primary_keys.get(fqn) is None`**：後者在複合主鍵（多個 `@Id` 欄位）時也會是 `None`（`_compute_primary_key()` 的判斷是 `len(id_fields) != 1`），但複合主鍵對 SQLAlchemy 完全合法——已用真實 SQLAlchemy 驗證過，兩個欄位各自 `primary_key=True` 能正常 `create_all()`，只是「當外鍵目標」做不到（八章既有的降級處理）。若誤用 `primary_keys.get(fqn) is None` 當跳過條件，會連累複合主鍵這種本來能正常渲染的 entity 一起被跳過——`tests/scaffold_agent/test_model_builder.py::test_composite_primary_key_entity_is_not_skipped` 專門鎖住這個區分。

**已對真實專案驗證**：`build_db_models("../lang-exam-api-refactor", real_module_list)`（`tests/design_agent/fixtures/real_module_list.json`，① 解析 Agent 對真實專案的既有輸出）跑出來，5 個 entity（`AnswerEntity`／`ExamEntity`／`ExamkindEntity`／`BackUserEntity`／`QuestionEntity`）的 `@Id` 都標在共用的 `@MappedSuperclass`（`BaseEntity`）上，但這個檔案沒有被 `real_module_list.json` 收進任何 module 的 `java_files`（六章「@MappedSuperclass 欄位繼承合併」對「解析不到父類別」的既定行為是「正常情況，不記警告」）——修正前，`app/models/exam.py` 的 `import` 直接拋出上述 `ArgumentError`；修正後，這 5 個 entity 正確被跳過並記錄原因，檔案裡其餘的 `ExamComponentConfig` 正常渲染，`create_all()` 成功。另外手動把 `BaseEntity.java` 加進 `common` module 的 `java_files` 重跑一次（模擬①有完整收錄父類別的情況），確認 `skipped_entities` 變成空、`AnswerEntity` 正確繼承到 `id`／`created`／`updated` 三個欄位（含稽核時間戳的 `server_default=func.now()`）——驗證的不只是「跳過機制有效」，也連帶驗證了六章 `@MappedSuperclass` 合併邏輯在真實資料完整時確實正確運作。這個 `module_list` 涵蓋率缺口本身不是 08a／08b 能單方面解的輸入完整性問題（屬於①解析 Agent 的職責範圍），但④在自己的邊界內做到了「偵測到就隔離，不讓一個 entity 的問題波及整個檔案」。

---

## 七、`__init__.py`——對外唯一入口

對應 08a 十二章。

```python
# scaffold_agent/__init__.py
"""④ 骨架實作 Agent：`db_models` 產出，對應 08a 全文。對外唯一入口是
`build_db_models()`（十二章），供 `graph/nodes/scaffold_node.py` 呼叫；
不依賴 `design_agent`／`graph.state`（十一章），只依賴 `common/`。
"""
from __future__ import annotations

from scaffold_agent import model_builder, reference_resolver
from scaffold_agent.types import BuildDbModelsResult, ModuleInfo

__all__ = ["BuildDbModelsResult", "ModuleInfo", "build_db_models"]


def build_db_models(java_project_path: str, module_list: list[ModuleInfo]) -> BuildDbModelsResult:
    """對應十二章：純同步 CPU 運算（`javalang.parse()`＋索引建構＋渲染），
    不呼叫任何 LLM／外部服務（十三章）。呼叫端（`scaffold_node.py`）必須
    用 `asyncio.to_thread()` 包一層，避免佔住 event loop——這是 08a 十二
    章明訂的呼叫慣例，不在這個函式內部處理（`build_db_models()` 本身
    維持同步簽名，才能被 `asyncio.to_thread()` 直接丟進執行緒池）。
    """
    scan_index = reference_resolver.build_scan_index(java_project_path, module_list)
    return model_builder.assemble(scan_index, module_list)
```

---

## 八、與既有程式碼的介面異動

對應 08a 十二章「`RefactorState` 新增欄位」。

### `graph/state.py`

`RefactorState` 新增兩個欄位，緊接在既有的 `scaffold_done: bool` 之後：

```python
    # Agent ④：generate_scaffold() 回傳的 skipped_interfaces／
    # skipped_db_models（見 07a 四章、08a 十二章），單次寫入的快照，不
    # 逐次累加，不需要 reducer——scaffold 不在 retry_count 迴圈內，只會
    # 執行一次。skipped_db_models 固定是 {file_path, class_name, error}
    # 三欄位 schema（class_name 為 None 代表 07a 檔案級失敗，非 None 代表
    # 08a entity 級失敗，見 08a 十二章），下游（09a／⑦ Debug Agent）不需要
    # 分辨兩種來源各自的欄位形狀。
    skipped_interfaces: list[dict]
    skipped_db_models: list[dict]
```

### `graph/nodes/scaffold_node.py`

07b 落地時的 stub 固定傳 `db_models=None`，這裡接上真正的 `scaffold_agent.build_db_models()`：

```python
"""
④ 骨架實作 Agent（scaffold_agent + translator-cli，骨架生成模式）
依 Agent ③ 的目錄結構與 interface 定義，建立目錄、base class、router 骨架、config；
db_models 由 scaffold_agent 從 Java entity 原始碼組出（見 08a_scaffold_agent_architecture.md）。
見 07a_translator_cli_architecture.md、07b_translator_cli_code.md、08a、08b。

平行分支 node：與 [P] Plan Agent 同以 record_tests／design 為共同前驅
（見 01 五章），只回傳自己的 key，不展開 state。
"""
import asyncio
import logging

from graph.state import RefactorState
from scaffold_agent import build_db_models
from translator_cli import client as translator_cli

logger = logging.getLogger(__name__)


async def run(state: RefactorState) -> dict:
    # build_db_models() 是純同步 CPU 運算（javalang 解析＋索引建構＋渲染，
    # 不呼叫任何 LLM／外部服務，見 08a 十二、十三章），必須用
    # asyncio.to_thread() 包一層，否則會佔住 event loop、讓同一個 superstep
    # 平行執行的 plan（[P]，async 呼叫 Claude API）退化成排隊（見 08a 十二章）。
    scaffold_result = await asyncio.to_thread(
        build_db_models,
        java_project_path=state["java_project_path"],
        module_list=state["module_list"],
    )

    result = await translator_cli.generate_scaffold(
        python_project_path=state["python_project_path"],
        python_structure=state["python_structure"],
        db_models=scaffold_result.db_models,
    )

    # 兩個不同粒度來源合併成同一份 RefactorState.skipped_db_models：
    # result["skipped_db_models"]（07a 四章，檔案級，{file_path, error}）
    # 補上 class_name=None；scaffold_result.skipped_entities（08a 九章，
    # entity 級，{file_path, class_name, error}）原樣併入——固定三欄位
    # schema，下游不需要用 .get() 防禦性判斷這個 dict 是哪一種來源
    # （見 08a 十二章）。
    skipped_db_models = [
        {**item, "class_name": None} for item in result["skipped_db_models"]
    ] + scaffold_result.skipped_entities

    if result["skipped_interfaces"] or skipped_db_models:
        logger.warning(
            "generate_scaffold() 有 %d 個 interface、%d 個 db_model 被跳過，"
            "見回傳值 skipped_interfaces／skipped_db_models（07a 四章、08a 九、十二章）",
            len(result["skipped_interfaces"]),
            len(skipped_db_models),
        )
    if not result["success"]:
        logger.error("generate_scaffold() 失敗：%s", result["error"])

    # 注意：scaffold 是平行分支 node，只回傳自己的 key，不展開 state
    return {
        "scaffold_done": result["success"],
        "skipped_interfaces": result["skipped_interfaces"],
        "skipped_db_models": skipped_db_models,
    }
```

### `main.py`

`initial_state` 新增對應的兩個空清單初始值（比照 01 九章「顯式全部初始化」慣例）：

```python
        "scaffold_done": False,
        "skipped_interfaces": [],
        "skipped_db_models": [],
```

---

## 九、範圍邊界

實作涵蓋 08a 十章列出的所有核心機制（`@MappedSuperclass` 欄位繼承合併、FK 循環偵測、`@ManyToMany` 中介表、Enum 三層消歧、entity 名稱碰撞 tie-break、多 schema、稽核時間戳、唯一約束、UUID 主鍵預設值、軟刪除標記），不額外實作 08a 十章列為已知限制的項目（`@Embeddable` 欄位攤平、複合主鍵、`@AttributeOverride`、`@JoinColumns` 複合外鍵、Bean Validation annotation、`@Table(indexes=...)`、`@Inheritance` 繼承策略等）——這些是 08a 自己定案不處理的範圍，沿用同一份界定。

08a 十章另列的統計性問題（這些機制在 `lang-exam-api-refactor` 真實專案的實際觸發比例）留待接上真實 `module_list`／`java_project_path` 才能確認，不在本文件範圍內。

---

## Enum 建構子引數還原（對應 08a「建構子引數還原」章節、`docs/09b_bug_trace.md` #44）

**`scaffold_agent/types.py::EnumRecord`** 新增兩個欄位：`constructor_params: list[str]`、`member_args: dict[str, list[object]]`（皆 `field(default_factory=...)`，向後相容既有呼叫端）。

**`scaffold_agent/entity_scan.py` 新增 `_enum_constructor_values(decl) -> tuple[list[str], dict[str, list[object]]]`**：取 `decl.body.declarations` 裡的 `ConstructorDeclaration`（正常唯一，多個時取第一個並記警告），逐一比對 `decl.body.constants` 每個常數的 `.arguments`（引數數量需與建構子參數數量一致，且全部是 `javalang.tree.Literal`，複用既有 `_literal_value()`）。`scan_module()` 掃到 `EnumDeclaration` 時呼叫這個函式，結果併入 `EnumRecord`。

**`scaffold_agent/model_builder.py` 新增 `_render_enum_class_body(python_name, record) -> str`**，取代原本內嵌在 `_render_enums_file()` 迴圈裡的兩行渲染：

```python
def _render_enum_class_body(python_name: str, record: EnumRecord) -> str:
    lines = [f"class {python_name}(enum.Enum):"]
    has_complete_args = bool(record.constructor_params) and len(record.member_args) == len(record.members)
    if has_complete_args:
        for member in record.members:
            values = ", ".join(repr(v) for v in record.member_args[member])
            lines.append(f"    {member} = ({values},)" if len(record.member_args[member]) == 1 else f"    {member} = ({values})")
        lines.append("")
        param_list = ", ".join(record.constructor_params)
        lines.append(f"    def __init__(self, {param_list}):")
        for param in record.constructor_params:
            lines.append(f"        self.{param} = {param}")
    else:
        lines.extend(f'    {member} = "{member}"' for member in record.members)
    return "\n".join(lines)
```

單一引數的 tuple 需要補逗號（`(200,)`）才是合法的單元素 tuple 語法，`len(...) == 1` 那個分支專門處理這個 Python 語法細節，不是可以省略的特殊情況。`repr(v)` 同時處理數字（`repr(200) == "200"`）與字串（`repr("操作成功")` 正確加上引號並跳脫），不需要另外分型別處理。

`_render_enums_file()` 迴圈內 `lines = [...]` ＋ `lines.extend(...)` 兩行改成單一呼叫 `text = _render_enum_class_body(python_name, record)`，其餘（`ast.parse()` 驗證、`skipped_entities` 收集）不變。

單元測試、真實環境驗證見 08a「建構子引數還原」章節。

---

*各 Agent／工具的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
