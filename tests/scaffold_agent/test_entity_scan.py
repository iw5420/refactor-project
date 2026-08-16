"""scaffold_agent/entity_scan.py，對應 08a 四章。"""
import textwrap

import pytest

from scaffold_agent.entity_scan import scan_module


def _write(tmp_path, rel_path: str, source: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(textwrap.dedent(source), encoding="utf-8")


def _scan_one(tmp_path, rel_path: str, source: str, module: str = "hr"):
    _write(tmp_path, rel_path, source)
    entities, enums = scan_module(str(tmp_path), {"module": module, "java_files": [rel_path]})
    return entities, enums


def test_scan_entity_basic_fields(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        @Table(name = "users", schema = "hr")
        public class User {
            @Id
            @GeneratedValue
            private Long id;

            @Column(name = "EMAIL_ADDR", nullable = false, length = 100)
            private String emailAddress;
        }
        """,
    )
    assert len(entities) == 1
    entity = entities[0]
    assert entity.class_name == "User"
    assert entity.package == "com.hr"
    assert entity.jpa_kind == "Entity"
    assert entity.table_name == "users"
    assert entity.schema == "hr"

    id_field, email_field = entity.fields
    assert id_field.is_id and id_field.is_generated_value
    assert email_field.column_name == "EMAIL_ADDR"
    assert email_field.nullable is False
    assert email_field.length == 100


def test_scan_non_jpa_class_is_skipped(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Helper.java",
        """
        package com.hr;
        public class Helper {
            private String x;
        }
        """,
    )
    assert entities == []


def test_scan_embeddable_and_mapped_superclass_kind(tmp_path):
    entities, _ = _scan_one(
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
    assert entities[0].jpa_kind == "Embeddable"
    assert entities[0].table_name is None


def test_scan_enum_declaration(tmp_path):
    _, enums = _scan_one(tmp_path, "Status.java", "package com.hr;\npublic enum Status { PENDING, PAID }\n")
    assert len(enums) == 1
    assert enums[0].class_name == "Status"
    assert enums[0].members == ["PENDING", "PAID"]


def test_scan_field_is_primitive_true_for_basic_type(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        public class Order {
            @Id
            private long id;
            private int quantity;
        }
        """,
    )
    id_field, qty_field = entities[0].fields
    assert id_field.is_primitive is True
    assert qty_field.is_primitive is True
    # 沒有 @Column 標註時，is_primitive 決定 nullable fallback（七章），
    # 這裡只驗證 is_primitive 本身、nullable 判斷留給 column_mapping 測試。
    assert qty_field.nullable is None


def test_scan_field_is_primitive_false_for_wrapper_type(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        public class Order {
            @Id
            private Long id;
        }
        """,
    )
    assert entities[0].fields[0].is_primitive is False


def test_scan_byte_array_type(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Doc.java",
        """
        package com.hr;
        import javax.persistence.Entity;
        @Entity
        public class Doc {
            private byte[] content;
        }
        """,
    )
    assert entities[0].fields[0].java_type == "byte[]"
    assert entities[0].fields[0].is_primitive is False


def test_literal_rule_ignores_member_reference_element(tmp_path, caplog):
    """對應四章「Annotation 元素值萃取：只接受 Literal」：常數參照視同缺席。"""
    entities, _ = _scan_one(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        public class User {
            @Column(length = UserConstants.MAX_EMAIL_LENGTH)
            private String email;
        }
        """,
    )
    assert entities[0].fields[0].length is None


def test_unique_constraints_array_form(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        @Table(name = "users", uniqueConstraints = @UniqueConstraint(columnNames = {"email", "tenant_id"}))
        public class User {
        }
        """,
    )
    assert entities[0].unique_constraints == [["email", "tenant_id"]]


def test_soft_delete_clause_from_where(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.Entity;
        import org.hibernate.annotations.Where;
        @Entity
        @Where(clause = "deleted = false")
        public class Order {
        }
        """,
    )
    assert entities[0].soft_delete_clause == "deleted = false"


def test_soft_delete_clause_from_sql_restriction(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.Entity;
        import org.hibernate.annotations.SQLRestriction;
        @Entity
        @SQLRestriction("deleted = false")
        public class Order {
        }
        """,
    )
    assert entities[0].soft_delete_clause == "deleted = false"


def test_many_to_many_join_table_extraction(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        import java.util.List;
        @Entity
        public class User {
            @ManyToMany
            @JoinTable(name = "user_roles",
                       joinColumns = @JoinColumn(name = "user_id"),
                       inverseJoinColumns = @JoinColumn(name = "role_id"))
            private List<Role> roles;
        }
        """,
    )
    field = entities[0].fields[0]
    assert field.relation == "ManyToMany"
    assert field.join_table.name == "user_roles"
    assert field.join_table.join_column == "user_id"
    assert field.join_table.inverse_join_column == "role_id"


def test_enumerated_ordinal_detected(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        public class Order {
            @Enumerated(EnumType.ORDINAL)
            private Status status;
        }
        """,
    )
    assert entities[0].fields[0].enum_ordinal is True


def test_enumerated_string_is_not_ordinal(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        public class Order {
            @Enumerated(EnumType.STRING)
            private Status status;
        }
        """,
    )
    assert entities[0].fields[0].enum_ordinal is False


def test_audit_annotations_detected(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        import org.springframework.data.annotation.CreatedDate;
        import org.springframework.data.annotation.LastModifiedDate;
        @Entity
        public class Order {
            @CreatedDate
            private java.time.LocalDateTime createdAt;
            @LastModifiedDate
            private java.time.LocalDateTime updatedAt;
        }
        """,
    )
    created, updated = entities[0].fields
    assert created.is_audit_created and not created.is_audit_updated
    assert updated.is_audit_updated and not updated.is_audit_created


def test_one_to_one_mapped_by_detected(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Profile.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        public class Profile {
            @OneToOne(mappedBy = "profile")
            private User user;
        }
        """,
    )
    assert entities[0].fields[0].relation == "OneToOne"
    assert entities[0].fields[0].mapped_by == "profile"


def test_join_column_overrides_name_and_nullable(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        public class Order {
            @ManyToOne
            @JoinColumn(name = "MGR_ID", nullable = false)
            private User manager;
        }
        """,
    )
    field = entities[0].fields[0]
    assert field.relation == "ManyToOne"
    assert field.column_name == "MGR_ID"
    assert field.nullable is False


def test_transient_field_flagged(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        public class User {
            @Transient
            private String temp;
        }
        """,
    )
    assert entities[0].fields[0].is_transient is True


def test_multi_declarator_field_expands_to_multiple_records(tmp_path):
    entities, _ = _scan_one(
        tmp_path,
        "Point.java",
        """
        package com.hr;
        import javax.persistence.Entity;
        @Entity
        public class Point {
            private int x, y;
        }
        """,
    )
    names = [f.name for f in entities[0].fields]
    assert names == ["x", "y"]


def test_scan_java_syntax_error_propagates(tmp_path):
    with pytest.raises(Exception):
        _scan_one(tmp_path, "Bad.java", "package com.hr;\npublic class {{{ broken")
