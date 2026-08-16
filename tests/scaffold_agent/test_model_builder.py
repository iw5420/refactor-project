"""scaffold_agent/model_builder.py ＋ scaffold_agent/__init__.py，對應 08a 九、十二章。"""
import ast
import sys
import textwrap

from scaffold_agent import build_db_models


def _write(tmp_path, rel_path: str, source: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(textwrap.dedent(source), encoding="utf-8")


def _create_all_against_sqlite(result, tmp_path):
    """把 `result.db_models` 寫進一個臨時 Python 專案、真正 import 執行、
    對 sqlite in-memory 跑 `Base.metadata.create_all()`——共用於本檔案
    多個需要「不只驗證語法，還要驗證真的能建表」的測試。
    """
    project_root = tmp_path / "python_project"
    (project_root / "app" / "core").mkdir(parents=True)
    (project_root / "app" / "models").mkdir(parents=True)
    (project_root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (project_root / "app" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (project_root / "app" / "models" / "__init__.py").write_text("", encoding="utf-8")
    (project_root / "app" / "core" / "database.py").write_text(
        "from sqlalchemy.orm import declarative_base\nBase = declarative_base()\n", encoding="utf-8"
    )
    for path, content in result.db_models.items():
        full = project_root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    sys.path.insert(0, str(project_root))
    try:
        import importlib

        database_module = importlib.import_module("app.core.database")
        importlib.import_module("app.models.hr")

        from sqlalchemy import create_engine

        engine = create_engine("sqlite:///:memory:")
        database_module.Base.metadata.create_all(engine)
        return database_module.Base
    finally:
        sys.path.remove(str(project_root))
        for name in ("app.core.database", "app.models.hr", "app.models._enums", "app.core", "app.models", "app"):
            sys.modules.pop(name, None)


def test_module_without_any_entity_produces_no_file(tmp_path):
    _write(tmp_path, "Helper.java", "package com.hr;\npublic class Helper {\n}\n")
    result = build_db_models(str(tmp_path), [{"module": "hr", "java_files": ["Helper.java"]}])
    assert result.db_models == {}
    assert result.skipped_entities == []


def test_single_entity_file_parses_and_contains_expected_pieces(tmp_path):
    _write(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        @Table(name = "users", uniqueConstraints = @UniqueConstraint(columnNames = {"email"}))
        public class User {
            @Id @GeneratedValue
            private Long id;
            @Column(nullable = false)
            private String email;
        }
        """,
    )
    result = build_db_models(str(tmp_path), [{"module": "hr", "java_files": ["User.java"]}])
    content = result.db_models["app/models/hr.py"]
    ast.parse(content)  # 語法必須合法
    assert "class User(Base):" in content
    assert '__tablename__ = "users"' in content
    assert '__table_args__ = (UniqueConstraint(\'email\', name="uq_users_email"),)' in content
    assert "from sqlalchemy.orm import Mapped, mapped_column" in content
    assert "from app.core.database import Base" in content


def test_embeddable_class_recorded_as_skipped_not_rendered(tmp_path):
    _write(
        tmp_path,
        "Address.java",
        """
        package com.hr;
        import javax.persistence.Embeddable;
        @Embeddable
        public class Address {
            private String city;
        }
        """,
    )
    result = build_db_models(str(tmp_path), [{"module": "hr", "java_files": ["Address.java"]}])
    assert result.db_models == {}
    assert len(result.skipped_entities) == 1
    assert result.skipped_entities[0]["class_name"] == "Address"
    assert "class_name" in result.skipped_entities[0]


def test_soft_delete_todo_comment_preserved(tmp_path):
    _write(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        import org.hibernate.annotations.Where;
        @Entity
        @Table(name = "orders")
        @Where(clause = "deleted = false")
        public class Order {
            @Id
            private Long id;
        }
        """,
    )
    result = build_db_models(str(tmp_path), [{"module": "hr", "java_files": ["Order.java"]}])
    content = result.db_models["app/models/hr.py"]
    ast.parse(content)
    assert "# TODO: 此實體原有邏輯刪除過濾條件" in content


def test_enums_file_generated_when_referenced(tmp_path):
    _write(tmp_path, "Status.java", "package com.hr;\npublic enum Status { PENDING, PAID }\n")
    _write(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        @Table(name = "orders")
        public class Order {
            @Id private Long id;
            @Enumerated(EnumType.STRING)
            private Status status;
        }
        """,
    )
    result = build_db_models(str(tmp_path), [{"module": "hr", "java_files": ["Order.java", "Status.java"]}])
    enums_content = result.db_models["app/models/_enums.py"]
    ast.parse(enums_content)
    assert "class Status(enum.Enum):" in enums_content
    assert 'PENDING = "PENDING"' in enums_content


def test_no_enum_declared_means_no_enums_file(tmp_path):
    _write(tmp_path, "Order.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"orders\")\npublic class Order {\n@Id private Long id;\n}\n")
    result = build_db_models(str(tmp_path), [{"module": "hr", "java_files": ["Order.java"]}])
    assert "app/models/_enums.py" not in result.db_models


def test_missing_table_annotation_falls_back_to_snake_case_name(tmp_path):
    _write(
        tmp_path,
        "UserProfile.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        public class UserProfile {
            @Id
            private Long id;
        }
        """,
    )
    result = build_db_models(str(tmp_path), [{"module": "hr", "java_files": ["UserProfile.java"]}])
    content = result.db_models["app/models/hr.py"]
    assert '__tablename__ = "user_profile"' in content


def test_join_table_name_with_hyphen_is_sanitized_not_dropped(tmp_path):
    """對應 08a 八章「中介表變數名稱正規化」：`@JoinTable(name = "...")`
    的字面值若含連字號這類 DB 命名慣例常見、但不是合法 Python 識別字的
    字元（如 "user-roles"），變數名稱正規化成 `user_roles`，`Table(...)`
    的 DB table 名稱字面值仍然是原始的 `"user-roles"`——中介表因此正常
    渲染進檔案，不會被隔離機制當成語法錯誤跳過。
    """
    _write(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        import java.util.List;
        @Entity
        @Table(name = "users")
        public class User {
            @Id private Long id;
            @ManyToMany
            @JoinTable(name = "user-roles", joinColumns = @JoinColumn(name = "user_id"), inverseJoinColumns = @JoinColumn(name = "role_id"))
            private List<Role> roles;
        }
        """,
    )
    _write(tmp_path, "Role.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"roles\")\npublic class Role {\n@Id private Long id;\n}\n")

    result = build_db_models(str(tmp_path), [{"module": "hr", "java_files": ["User.java", "Role.java"]}])

    content = result.db_models["app/models/hr.py"]
    ast.parse(content)
    assert "class User(Base):" in content
    assert "class Role(Base):" in content
    assert 'user_roles = Table(\n    "user-roles",' in content  # 變數名合法，DB table 名稱原樣保留
    assert result.skipped_entities == []


def test_join_table_name_colliding_with_python_keyword_is_still_isolated(tmp_path):
    """對應 08a 九章步驟 2：識別字正規化（`_sanitize_python_identifier()`）
    只處理非法字元，不處理「合法字元組成、但剛好是 Python 關鍵字」這種
    殘留情況（如 table 名稱剛好叫 `"class"`）——這仍然是無效的 Python
    陳述式，AST 隔離驗證要接住，不能讓整個 module 檔案（含其他正常的
    entity）被 07a 全域最終檢查一起拖垮。
    """
    _write(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        import java.util.List;
        @Entity
        @Table(name = "users")
        public class User {
            @Id private Long id;
            @ManyToMany
            @JoinTable(name = "class", joinColumns = @JoinColumn(name = "user_id"), inverseJoinColumns = @JoinColumn(name = "role_id"))
            private List<Role> roles;
        }
        """,
    )
    _write(tmp_path, "Role.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"roles\")\npublic class Role {\n@Id private Long id;\n}\n")

    result = build_db_models(str(tmp_path), [{"module": "hr", "java_files": ["User.java", "Role.java"]}])

    content = result.db_models["app/models/hr.py"]
    ast.parse(content)  # 檔案本身仍然完整、語法合法
    assert "class User(Base):" in content
    assert "class Role(Base):" in content
    assert "= Table(" not in content  # 不合法的中介表定義沒有寫進檔案

    skips = [s for s in result.skipped_entities if s["class_name"] == "class"]
    assert len(skips) == 1
    assert skips[0]["file_path"] == "User.java"


def test_full_pipeline_end_to_end_create_all_with_sqlite(tmp_path):
    """對應 09/12 章：完整跑一次 build_db_models()，再實際 import 產出的
    模組、對 sqlite in-memory 執行 create_all()——驗證的不只是語法合法，
    是「這份骨架真的能被 SQLAlchemy 接受、建出表」。涵蓋
    @MappedSuperclass 欄位繼承、@Column(name=...) 覆寫、@Enumerated、
    唯一約束、@ManyToMany 中介表、@ManyToOne 外鍵（含自訂 DB 欄位名）
    這條主線的所有機制一次串起來。
    """
    import sys

    _write(
        tmp_path,
        "BaseEntity.java",
        """
        package com.common;
        import javax.persistence.*;
        import java.time.LocalDateTime;
        @MappedSuperclass
        public class BaseEntity {
            @Id @GeneratedValue
            private Long id;
            @CreatedDate
            private LocalDateTime createdAt;
        }
        """,
    )
    _write(tmp_path, "Role.java", "package com.hr;\nimport javax.persistence.*;\nimport com.common.BaseEntity;\n@Entity\n@Table(name=\"roles\")\npublic class Role extends BaseEntity {\nprivate String name;\n}\n")
    _write(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        import java.util.List;
        import com.common.BaseEntity;
        @Entity
        @Table(name = "users", uniqueConstraints = @UniqueConstraint(columnNames = {"email_address"}))
        public class User extends BaseEntity {
            @Column(nullable = false, length = 100, unique = true)
            private String emailAddress;
            @Enumerated(EnumType.STRING)
            private Status status;
            @ManyToMany
            @JoinTable(name = "user_roles", joinColumns = @JoinColumn(name = "user_id"), inverseJoinColumns = @JoinColumn(name = "role_id"))
            private List<Role> roles;
            @ManyToOne
            @JoinColumn(name = "MGR_ID")
            private User manager;
        }
        """,
    )
    _write(tmp_path, "Status.java", "package com.hr;\npublic enum Status { PENDING, PAID }\n")

    module_list = [{"module": "hr", "java_files": ["Role.java", "User.java", "Status.java", "BaseEntity.java"]}]
    result = build_db_models(str(tmp_path), module_list)
    assert result.skipped_entities == []

    project_root = tmp_path / "python_project"
    (project_root / "app" / "core").mkdir(parents=True)
    (project_root / "app" / "models").mkdir(parents=True)
    (project_root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (project_root / "app" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (project_root / "app" / "models" / "__init__.py").write_text("", encoding="utf-8")
    (project_root / "app" / "core" / "database.py").write_text(
        "from sqlalchemy.orm import declarative_base\nBase = declarative_base()\n", encoding="utf-8"
    )
    for path, content in result.db_models.items():
        full = project_root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    sys.path.insert(0, str(project_root))
    try:
        import importlib

        database_module = importlib.import_module("app.core.database")
        hr_module = importlib.import_module("app.models.hr")
        enums_module = importlib.import_module("app.models._enums")

        from sqlalchemy import create_engine

        engine = create_engine("sqlite:///:memory:")
        database_module.Base.metadata.create_all(engine)

        assert set(database_module.Base.metadata.tables.keys()) == {"roles", "users", "user_roles"}
        assert [m.value for m in enums_module.Status] == ["PENDING", "PAID"]
        assert "MGR_ID" in [c.name for c in hr_module.User.__table__.columns]
        assert set(c.name for c in database_module.Base.metadata.tables["user_roles"].columns) == {"user_id", "role_id"}
    finally:
        sys.path.remove(str(project_root))
        for name in ("app.core.database", "app.models.hr", "app.models._enums", "app.core", "app.models", "app"):
            __import__("sys").modules.pop(name, None)


def test_same_module_collision_loser_produces_downgraded_fk_not_dangling_reference(tmp_path):
    """對應六章「同一個 module 內撞名」tie-break 與八章「外鍵解析失敗的
    降級處理」的銜接：tie-break 淘汰的那個 entity（`com.hr.b.Address`）
    必須在 `effective_fields`／`primary_keys`／`fk_use_alter_edges` 算出
    之前就已經從 `scan_index.entities` 移除，這樣其他 entity 對它的
    `@ManyToOne` 才會落入「目標主鍵不明確」的降級路徑（TODO 註解 +
    `fallback_pk_type`），而不是產生一個指向「永遠不會被渲染出來的
    table」的 `ForeignKey(...)`——後者會讓 `create_all()`／mapper 設定
    階段直接拋 `NoReferencedTableError`，是比骨架階段其他降級路徑嚴重
    得多的失敗等級（服務整個起不來，不是留 TODO 讓人工事後確認）。
    """
    _write(
        tmp_path,
        "a/Address.java",
        "package com.hr.a;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"addr_a\")\npublic class Address {\n@Id private Long id;\n}\n",
    )
    _write(
        tmp_path,
        "b/Address.java",
        "package com.hr.b;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"addr_b\")\npublic class Address {\n@Id private Long id;\n}\n",
    )
    _write(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        import com.hr.b.Address;
        @Entity
        @Table(name = "users")
        public class User {
            @Id private Long id;
            @ManyToOne
            private Address address;
        }
        """,
    )
    module_list = [{"module": "hr", "java_files": ["a/Address.java", "b/Address.java", "User.java"]}]
    result = build_db_models(str(tmp_path), module_list)

    content = result.db_models["app/models/hr.py"]
    ast.parse(content)
    assert "class Address(Base):" in content
    assert content.count("class Address(Base):") == 1  # 只有贏家渲染，沒有兩個同名 class
    assert "# TODO: 概念上是外鍵，指向 Address" in content  # 輸家的 FK 正確降級，不是懸空引用
    assert 'ForeignKey("addresses' not in content and 'ForeignKey("addr_b' not in content

    base = _create_all_against_sqlite(result, tmp_path)
    assert set(base.metadata.tables.keys()) == {"addr_a", "users"}  # create_all() 不會因為懸空 FK 而失敗


def test_entity_without_resolvable_primary_key_is_skipped_not_rendered(tmp_path):
    """對應 08a 六章「primary_key_column／primary_key_type 為 None」：
    對真實 lang-exam-api-refactor 專案跑 build_db_models() 才發現的缺口
    ——entity 的 @Id 標註在一個沒被 module_list.java_files 掃描到的
    @MappedSuperclass 上（如共用的 BaseEntity 沒被納入任何 module），這個
    entity 合併後完全沒有可辨識主鍵。SQLAlchemy 宣告式 class 缺少
    primary_key=True 欄位不是「語法合法但語意不完整」，是 import 當下
    直接拋 ArgumentError，而且會讓同一個檔案裡排在後面的其他 entity
    連帶無法被定義——必須在渲染前就跳過整個 entity，不能留給
    ast.parse()（那攔不住這種執行期錯誤）。
    """
    _write(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        @Table(name = "orders")
        public class Order {
            private String status;
        }
        """,
    )
    _write(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        @Table(name = "users")
        public class User {
            @Id private Long id;
            private String name;
        }
        """,
    )
    result = build_db_models(str(tmp_path), [{"module": "hr", "java_files": ["Order.java", "User.java"]}])

    content = result.db_models["app/models/hr.py"]
    ast.parse(content)
    assert "class User(Base):" in content
    assert "class Order(Base):" not in content  # 沒有主鍵，整個 entity 被跳過

    skips = [s for s in result.skipped_entities if s["class_name"] == "Order"]
    assert len(skips) == 1
    assert "沒有可辨識的主鍵" in skips[0]["error"]

    base = _create_all_against_sqlite(result, tmp_path)
    assert set(base.metadata.tables.keys()) == {"users"}  # create_all() 不會因為 Order 而失敗


def test_composite_primary_key_entity_is_not_skipped(tmp_path):
    """對應 08a 十章已知限制「複合主鍵不支援」：那條限制指的是「這個
    entity 當外鍵目標」做不到（六章 `primary_key_column`／
    `primary_key_type` 因為 `len(id_fields) != 1` 判定為主鍵不明確），
    不是這個 entity 自己不能渲染——多個 @Id 欄位對 SQLAlchemy 是合法的
    複合主鍵，每個欄位各自 `primary_key=True` 就能正常 `create_all()`。
    跟「完全沒有 @Id 欄位」（前一個測試）是兩種不同情況，不能共用同一個
    跳過條件，否則會誤殺複合主鍵這種本來能正常渲染的 entity。
    """
    _write(
        tmp_path,
        "OrderItem.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        @Table(name = "order_items")
        public class OrderItem {
            @Id
            private Long orderId;
            @Id
            private Long itemId;
            private String note;
        }
        """,
    )
    result = build_db_models(str(tmp_path), [{"module": "hr", "java_files": ["OrderItem.java"]}])

    content = result.db_models["app/models/hr.py"]
    ast.parse(content)
    assert "class OrderItem(Base):" in content
    assert content.count("primary_key=True") == 2
    assert result.skipped_entities == []

    base = _create_all_against_sqlite(result, tmp_path)
    assert set(base.metadata.tables.keys()) == {"order_items"}
