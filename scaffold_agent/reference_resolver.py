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

    # 對應 docs/09b_bug_trace.md 稽核時間戳失真案例：@EnableJpaAuditing
    # 是全專案層級的設定，只需要對整個 java_project_path 掃一次，見
    # entity_scan.scan_module() docstring。
    jpa_auditing_enabled = entity_scan.project_has_jpa_auditing_enabled(java_project_path)

    for module in module_list:
        entities, enums = entity_scan.scan_module(
            java_project_path, module, jpa_auditing_enabled=jpa_auditing_enabled
        )
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
