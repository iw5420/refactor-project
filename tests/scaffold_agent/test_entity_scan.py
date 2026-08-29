"""scaffold_agent/entity_scan.py，對應 08a 四章。"""
import textwrap

import pytest

from scaffold_agent.entity_scan import project_has_jpa_auditing_enabled, scan_module


def _write(tmp_path, rel_path: str, source: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(textwrap.dedent(source), encoding="utf-8")


def _scan_one(tmp_path, rel_path: str, source: str, module: str = "hr", jpa_auditing_enabled: bool = True):
    """`jpa_auditing_enabled` 預設 True，保留既有測試（大多不關心稽核
    旗標本身）原本的行為；只有 `test_audit_annotations_detected` 這類
    真的在測這個旗標語意的案例才會明確覆寫，見 docs/09b_bug_trace.md。"""
    _write(tmp_path, rel_path, source)
    entities, enums = scan_module(
        str(tmp_path),
        {"module": module, "java_files": [rel_path]},
        jpa_auditing_enabled=jpa_auditing_enabled,
    )
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
    assert enums[0].constructor_params == []
    assert enums[0].member_args == {}


def test_scan_enum_with_constructor_args_extracts_values(tmp_path):
    """對應 docs/09b_bug_trace.md #44：Java enum 帶建構子參數（如
    ErrorCode 的具體實作 CommonErrorCode(code, msg)）時，原本只保留成員
    名稱，數值資訊整個遺失。"""
    _, enums = _scan_one(
        tmp_path,
        "CommonErrorCode.java",
        """
        package com.hr;
        public enum CommonErrorCode {
            SUCCESS(200, "ok"),
            NOT_FOUND(404, "not found");
            private final int code;
            private final String msg;
            CommonErrorCode(int code, String msg) {
                this.code = code;
                this.msg = msg;
            }
        }
        """,
    )
    assert enums[0].constructor_params == ["code", "msg"]
    assert enums[0].member_args == {"SUCCESS": [200, "ok"], "NOT_FOUND": [404, "not found"]}


def test_scan_enum_arg_count_mismatch_skips_that_member(tmp_path):
    """常數的引數數量跟建構子參數數量對不上時，這個常數不進
    member_args（見 Literal 規則同一套「查得到才收，查不到就跳過」精神）
    ——理論上不該出現在合法 Java 原始碼裡，這裡測的是防禦性行為。"""
    _, enums = _scan_one(
        tmp_path,
        "Weird.java",
        """
        package com.hr;
        public enum Weird {
            A(1, "x"),
            B(2);
            private final int code;
            private final String msg;
            Weird(int code, String msg) { this.code = code; this.msg = msg; }
        }
        """,
    )
    assert enums[0].constructor_params == ["code", "msg"]
    assert enums[0].member_args == {"A": [1, "x"]}
    assert "B" not in enums[0].member_args


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


def test_spring_jpa_audit_annotations_ignored_when_auditing_not_enabled(tmp_path):
    """對應 docs/09b_bug_trace.md：`@CreatedDate`／`@LastModifiedDate`
    （Spring Data JPA 稽核）只有專案某處有 `@EnableJpaAuditing` 才會真的
    生效，否則純粹是裝飾、欄位永遠是 null——真實案例：目標專案的
    `BaseEntity` 用這兩個 annotation，但專案完全沒有
    `@EnableJpaAuditing`，SQLAlchemy 卻機械生成自動填值的欄位，跟 Java
    端永遠是 null 的實際行為對不上。"""
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
        jpa_auditing_enabled=False,
    )
    created, updated = entities[0].fields
    assert not created.is_audit_created
    assert not updated.is_audit_updated


def test_hibernate_native_timestamp_annotations_always_detected(tmp_path):
    """`@CreationTimestamp`／`@UpdateTimestamp` 是 Hibernate 原生機制，
    不需要 `@EnableJpaAuditing` 就會生效，不受這個旗標影響——即使旗標為
    False 也要照樣偵測到。"""
    entities, _ = _scan_one(
        tmp_path,
        "Order.java",
        """
        package com.hr;
        import javax.persistence.*;
        import org.hibernate.annotations.CreationTimestamp;
        import org.hibernate.annotations.UpdateTimestamp;
        @Entity
        public class Order {
            @CreationTimestamp
            private java.time.LocalDateTime createdAt;
            @UpdateTimestamp
            private java.time.LocalDateTime updatedAt;
        }
        """,
        jpa_auditing_enabled=False,
    )
    created, updated = entities[0].fields
    assert created.is_audit_created and not created.is_audit_updated
    assert updated.is_audit_updated and not updated.is_audit_created


def test_project_has_jpa_auditing_enabled_true_when_annotation_present(tmp_path):
    _write(
        tmp_path,
        "Application.java",
        """
        package com.hr;
        import org.springframework.data.jpa.repository.config.EnableJpaAuditing;
        @EnableJpaAuditing
        public class Application {}
        """,
    )
    assert project_has_jpa_auditing_enabled(str(tmp_path)) is True


def test_project_has_jpa_auditing_enabled_false_when_absent(tmp_path):
    _write(
        tmp_path,
        "Application.java",
        """
        package com.hr;
        public class Application {}
        """,
    )
    assert project_has_jpa_auditing_enabled(str(tmp_path)) is False


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
