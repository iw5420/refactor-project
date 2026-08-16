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
