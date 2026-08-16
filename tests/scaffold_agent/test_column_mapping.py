"""scaffold_agent/column_mapping.py，對應 08a 七、八章。"""
import textwrap

from scaffold_agent.column_mapping import _sanitize_python_identifier, render_field, render_join_table
from scaffold_agent.reference_resolver import build_scan_index


def _write(tmp_path, rel_path: str, source: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(textwrap.dedent(source), encoding="utf-8")


def _field_by_name(idx, fqn, name):
    return next(f for f in idx.effective_fields[fqn] if f.name == name)


def test_primary_key_field_no_nullable_kwarg(tmp_path):
    _write(tmp_path, "User.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"users\")\npublic class User {\n@Id @GeneratedValue\nprivate Long id;\n}\n")
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java"]}])
    entity = idx.entities["com.hr.User"]
    render = render_field("com.hr.User", entity, _field_by_name(idx, "com.hr.User", "id"), idx)
    assert render.text == "id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)"
    assert render.from_imports == {"sqlalchemy": {"Integer"}}


def test_primitive_field_defaults_not_null(tmp_path):
    _write(tmp_path, "Order.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"orders\")\npublic class Order {\n@Id private Long id;\nprivate int quantity;\n}\n")
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["Order.java"]}])
    entity = idx.entities["com.hr.Order"]
    render = render_field("com.hr.Order", entity, _field_by_name(idx, "com.hr.Order", "quantity"), idx)
    assert render.text == "quantity: Mapped[int] = mapped_column(Integer, nullable=False)"


def test_wrapper_field_defaults_nullable(tmp_path):
    _write(tmp_path, "User.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"users\")\npublic class User {\n@Id private Long id;\nprivate String name;\n}\n")
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java"]}])
    entity = idx.entities["com.hr.User"]
    render = render_field("com.hr.User", entity, _field_by_name(idx, "com.hr.User", "name"), idx)
    assert render.text == "name: Mapped[str | None] = mapped_column(String, nullable=True)"


def test_column_name_override_only_when_different_from_attr(tmp_path):
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
            @Column(name = "EMAIL_ADDR", nullable = false, length = 100)
            private String emailAddress;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java"]}])
    entity = idx.entities["com.hr.User"]
    render = render_field("com.hr.User", entity, _field_by_name(idx, "com.hr.User", "emailAddress"), idx)
    assert render.text == "email_address: Mapped[str] = mapped_column('EMAIL_ADDR', String(100), nullable=False)"


def test_decimal_precision_scale(tmp_path):
    _write(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        import java.math.BigDecimal;
        @Entity
        @Table(name = "orders")
        public class Order {
            @Id private Long id;
            @Column(nullable = false, precision = 10, scale = 2)
            private BigDecimal total;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["Order.java"]}])
    entity = idx.entities["com.hr.Order"]
    render = render_field("com.hr.Order", entity, _field_by_name(idx, "com.hr.Order", "total"), idx)
    assert render.text == "total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)"
    assert render.from_imports == {"sqlalchemy": {"Numeric"}, "decimal": {"Decimal"}}


def test_uuid_primary_key_with_default(tmp_path):
    _write(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        import java.util.UUID;
        @Entity
        @Table(name = "users")
        public class User {
            @Id @GeneratedValue
            private UUID id;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java"]}])
    entity = idx.entities["com.hr.User"]
    render = render_field("com.hr.User", entity, _field_by_name(idx, "com.hr.User", "id"), idx)
    assert "default=uuid4" in render.text
    assert "primary_key=True" in render.text
    assert render.from_imports["uuid"] == {"UUID", "uuid4"}
    assert render.from_imports["sqlalchemy.dialects.postgresql"] == {"UUID as PgUUID"}


def test_enum_field_renders_shared_enum_reference(tmp_path):
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
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["Order.java", "Status.java"]}])
    entity = idx.entities["com.hr.Order"]
    render = render_field("com.hr.Order", entity, _field_by_name(idx, "com.hr.Order", "status"), idx)
    assert render.text == "status: Mapped[Status | None] = mapped_column(SqlEnum(Status, native_enum=False, length=255), nullable=True)"
    assert render.from_imports == {"sqlalchemy": {"Enum as SqlEnum"}, "app.models._enums": {"Status"}}


def test_enum_ordinal_downgrades_to_string_with_dedicated_todo(tmp_path):
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
            @Enumerated(EnumType.ORDINAL)
            private Status status;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["Order.java", "Status.java"]}])
    entity = idx.entities["com.hr.Order"]
    render = render_field("com.hr.Order", entity, _field_by_name(idx, "com.hr.Order", "status"), idx)
    assert "@Enumerated(EnumType.ORDINAL)" in render.text
    assert "status: Mapped[str | None] = mapped_column(String, nullable=True)" in render.text


def test_unresolved_type_downgrades_to_string_with_generic_todo(tmp_path):
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
            private SomeUnresolvedType extra;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["Order.java"]}])
    entity = idx.entities["com.hr.Order"]
    render = render_field("com.hr.Order", entity, _field_by_name(idx, "com.hr.Order", "extra"), idx)
    assert "# TODO: 未知型別 SomeUnresolvedType" in render.text
    assert "extra: Mapped[str | None] = mapped_column(String, nullable=True)" in render.text


def test_transient_and_one_to_many_and_non_owning_one_to_one_skip(tmp_path):
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
            @Transient
            private String temp;
            @OneToMany
            private List<Order> orders;
            @OneToOne(mappedBy = "user")
            private Profile profile;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java"]}])
    entity = idx.entities["com.hr.User"]
    for name in ("temp", "orders", "profile"):
        assert render_field("com.hr.User", entity, _field_by_name(idx, "com.hr.User", name), idx) is None


def test_foreign_key_uses_join_column_name_and_default_nullable(tmp_path):
    _write(tmp_path, "User.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"users\")\npublic class User {\n@Id private Long id;\n}\n")
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
            @ManyToOne
            private User user;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java", "Order.java"]}])
    entity = idx.entities["com.hr.Order"]
    render = render_field("com.hr.Order", entity, _field_by_name(idx, "com.hr.Order", "user"), idx)
    assert render.text == "user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('users.id'), nullable=True)"


def test_foreign_key_custom_db_name_from_join_column(tmp_path):
    _write(tmp_path, "User.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"users\")\npublic class User {\n@Id private Long id;\n}\n")
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
            @ManyToOne
            @JoinColumn(name = "MGR_ID")
            private User manager;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java", "Order.java"]}])
    entity = idx.entities["com.hr.Order"]
    render = render_field("com.hr.Order", entity, _field_by_name(idx, "com.hr.Order", "manager"), idx)
    assert render.text == "manager_id: Mapped[int | None] = mapped_column('MGR_ID', Integer, ForeignKey('users.id'), nullable=True)"


def test_foreign_key_unresolved_target_downgrades_with_todo(tmp_path):
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
            @ManyToOne
            private NoSuchEntity thing;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["Order.java"]}])
    entity = idx.entities["com.hr.Order"]
    render = render_field("com.hr.Order", entity, _field_by_name(idx, "com.hr.Order", "thing"), idx)
    assert "# TODO: 概念上是外鍵，指向 NoSuchEntity" in render.text
    assert "ForeignKey(" not in render.text.splitlines()[-1]


def test_foreign_key_use_alter_when_cyclic(tmp_path):
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
            @ManyToOne
            private Order lastOrder;
        }
        """,
    )
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
            @ManyToOne
            private User user;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java", "Order.java"]}])
    entity = idx.entities["com.hr.User"]
    render = render_field("com.hr.User", entity, _field_by_name(idx, "com.hr.User", "lastOrder"), idx)
    assert "use_alter=True" in render.text
    assert 'name="fk_users_last_order_id"' in render.text


def test_container_type_field_is_skipped_not_downgraded_to_string(tmp_path):
    """對應 08a 七章「含 [／] 的容器型別...不渲染這個欄位，記警告並跳過」：
    沒有 JPA 關聯 annotation、純量欄位卻是集合型別（如 List<String>），
    map_java_type() 正規化後含 [/]，結構上不可能是單一純量 column，
    不能落入「無法解析型別」那條 String 降級路徑。
    """
    _write(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        import java.util.List;
        @Entity
        @Table(name = "orders")
        public class Order {
            @Id private Long id;
            private List<String> tags;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["Order.java"]}])
    entity = idx.entities["com.hr.Order"]
    render = render_field("com.hr.Order", entity, _field_by_name(idx, "com.hr.Order", "tags"), idx)
    assert render is None


def test_join_table_render_for_owner(tmp_path):
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
            @JoinTable(name = "user_roles", joinColumns = @JoinColumn(name = "user_id"), inverseJoinColumns = @JoinColumn(name = "role_id"))
            private List<Role> roles;
        }
        """,
    )
    _write(tmp_path, "Role.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"roles\")\npublic class Role {\n@Id private Long id;\n}\n")
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java", "Role.java"]}])
    entity = idx.entities["com.hr.User"]
    field = _field_by_name(idx, "com.hr.User", "roles")
    result = render_join_table("com.hr.User", entity, field, idx)
    assert result is not None
    stmt, imports = result
    assert 'user_roles = Table(' in stmt
    assert 'Column("user_id", Integer, ForeignKey("users.id"), primary_key=True)' in stmt
    assert 'Column("role_id", Integer, ForeignKey("roles.id"), primary_key=True)' in stmt
    assert imports["sqlalchemy"] >= {"Table", "Column", "ForeignKey", "Integer"}
    assert "schema=" not in stmt


def test_join_table_inherits_owner_schema(tmp_path):
    """對應八章「多 schema 支援」延伸到中介表：擁有端標了
    `@Table(schema = "hr")` 時，中介表的 `Table(...)` 建構子也要帶
    `schema="hr"`，否則會落在 SQLAlchemy 預設 schema，跟 FK 目標
    （`ForeignKey("hr.users.id")`）實際所在的 schema 不一致。
    """
    _write(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        import java.util.List;
        @Entity
        @Table(name = "users", schema = "hr")
        public class User {
            @Id private Long id;
            @ManyToMany
            @JoinTable(name = "user_roles", joinColumns = @JoinColumn(name = "user_id"), inverseJoinColumns = @JoinColumn(name = "role_id"))
            private List<Role> roles;
        }
        """,
    )
    _write(tmp_path, "Role.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"roles\", schema=\"hr\")\npublic class Role {\n@Id private Long id;\n}\n")
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java", "Role.java"]}])
    entity = idx.entities["com.hr.User"]
    field = _field_by_name(idx, "com.hr.User", "roles")
    result = render_join_table("com.hr.User", entity, field, idx)
    assert result is not None
    stmt, _ = result
    assert 'schema="hr"' in stmt
    assert 'ForeignKey("hr.users.id")' in stmt
    assert 'ForeignKey("hr.roles.id")' in stmt


def test_sanitize_python_identifier():
    assert _sanitize_python_identifier("user_roles") == "user_roles"
    assert _sanitize_python_identifier("user-roles") == "user_roles"
    assert _sanitize_python_identifier("USER_ROLES") == "USER_ROLES"
    assert _sanitize_python_identifier("123table") == "_123table"
    assert _sanitize_python_identifier("user roles") == "user_roles"
