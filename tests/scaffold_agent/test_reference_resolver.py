"""scaffold_agent/reference_resolver.py，對應 08a 六章。"""
import textwrap

from scaffold_agent.reference_resolver import build_scan_index, resolve_reference


def _write(tmp_path, rel_path: str, source: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(textwrap.dedent(source), encoding="utf-8")


def test_resolve_reference_via_explicit_import():
    assert resolve_reference("Status", "com.hr", {"Status": "com.common.enums.Status"}) == "com.common.enums.Status"


def test_resolve_reference_via_same_package():
    assert resolve_reference("Order", "com.hr", {}) == "com.hr.Order"


def test_resolve_reference_no_package_returns_none():
    assert resolve_reference("Order", None, {}) is None


def test_resolve_reference_wildcard_import_not_resolved():
    # wildcard import 不進 import_map（entity_scan.py 已過濾），因此
    # resolve_reference() 對這種情況天然落到「同 package」或 None，
    # 這裡直接驗證呼叫端行為，不是這個函式自己過濾 wildcard。
    assert resolve_reference("List", "com.hr", {}) == "com.hr.List"


def _base_entity_fixture(tmp_path):
    _write(
        tmp_path,
        "BaseEntity.java",
        """
        package com.common;
        import javax.persistence.*;
        import java.time.LocalDateTime;
        @MappedSuperclass
        public class BaseEntity {
            @Id
            @GeneratedValue
            private Long id;

            @CreatedDate
            private LocalDateTime createdAt;
        }
        """,
    )


def test_mapped_superclass_fields_merged_into_effective_fields(tmp_path):
    _base_entity_fixture(tmp_path)
    _write(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        import com.common.BaseEntity;
        @Entity
        @Table(name = "users")
        public class User extends BaseEntity {
            private String name;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java", "BaseEntity.java"]}])
    fields = [f.name for f in idx.effective_fields["com.hr.User"]]
    assert fields == ["id", "createdAt", "name"]
    assert idx.primary_keys["com.hr.User"] == ("id", "int", "Integer")


def test_field_hiding_keeps_nearest_declaration(tmp_path):
    _write(
        tmp_path,
        "Base.java",
        """
        package com.common;
        import javax.persistence.MappedSuperclass;
        @MappedSuperclass
        public class Base {
            private String label;
        }
        """,
    )
    _write(
        tmp_path,
        "Child.java",
        """
        package com.hr;
        import javax.persistence.Entity;
        import com.common.Base;
        @Entity
        public class Child extends Base {
            private int label;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["Child.java", "Base.java"]}])
    fields = idx.effective_fields["com.hr.Child"]
    assert len(fields) == 1
    assert fields[0].java_type == "int"  # 子類別重新宣告同名欄位，較近層級勝出


def test_entity_extends_entity_stops_merge_and_warns(tmp_path, caplog):
    _write(
        tmp_path,
        "Parent.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        public class Parent {
            @Id
            private Long id;
        }
        """,
    )
    _write(
        tmp_path,
        "Child.java",
        """
        package com.hr;
        import javax.persistence.Entity;
        @Entity
        public class Child extends Parent {
            private String name;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["Parent.java", "Child.java"]}])
    # Child 自己沒有 @Id 欄位，父類別是另一個 @Entity（@Inheritance，不支援），
    # 不合併欄位，因此 Child 的主鍵不明確。
    assert idx.primary_keys["com.hr.Child"] is None


def test_fk_cycle_detection_marks_use_alter(tmp_path):
    _write(tmp_path, "Base.java", "package com.common;\nimport javax.persistence.*;\n@MappedSuperclass\npublic class Base {\n@Id @GeneratedValue\nprivate Long id;\n}\n")
    _write(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        import com.common.Base;
        @Entity
        @Table(name = "users")
        public class User extends Base {
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
        import com.common.Base;
        @Entity
        @Table(name = "orders")
        public class Order extends Base {
            @ManyToOne
            private User user;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java", "Order.java", "Base.java"]}])
    assert ("com.hr.User", "com.hr.Order") in idx.fk_use_alter_edges
    assert ("com.hr.Order", "com.hr.User") in idx.fk_use_alter_edges


def test_fk_self_reference_not_an_edge(tmp_path):
    _write(
        tmp_path,
        "User.java",
        """
        package com.hr;
        import javax.persistence.*;
        @Entity
        @Table(name = "users")
        public class User {
            @Id
            private Long id;
            @ManyToOne
            private User manager;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java"]}])
    assert idx.fk_use_alter_edges == set()


def test_fk_non_cyclic_not_marked(tmp_path):
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
            @Id
            private Long id;
            @ManyToOne
            private User user;
        }
        """,
    )
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java", "Order.java"]}])
    assert idx.fk_use_alter_edges == set()


def test_join_table_owner_tie_break_by_fqn_not_scan_order(tmp_path):
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
            @Id
            private Long id;
            @ManyToMany
            @JoinTable(name = "user_roles", joinColumns = @JoinColumn(name = "user_id"), inverseJoinColumns = @JoinColumn(name = "role_id"))
            private List<Role> roles;
        }
        """,
    )
    _write(
        tmp_path,
        "Role.java",
        """
        package com.hr;
        import javax.persistence.*;
        import java.util.List;
        @Entity
        @Table(name = "roles")
        public class Role {
            @Id
            private Long id;
            @ManyToMany
            @JoinTable(name = "user_roles", joinColumns = @JoinColumn(name = "role_id"), inverseJoinColumns = @JoinColumn(name = "user_id"))
            private List<User> users;
        }
        """,
    )
    # java_files 刻意把 User.java 排在 Role.java 前面（掃描／module_list
    # 順序上 User 先出現），驗證 tie-break 依據的是 FQN 字典序
    # （"com.hr.Role" < "com.hr.User"），不是掃描順序——若還是舊的
    # 「先掃到的贏」規則，這裡會判給 User，跟斷言矛盾。
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["User.java", "Role.java"]}])
    assert idx.join_table_owners["user_roles"] == "com.hr.Role"


def test_enum_name_collision_dedup_tier2_by_module(tmp_path):
    _write(tmp_path, "a/Status.java", "package com.hr.a;\npublic enum Status { X }\n")
    _write(tmp_path, "b/Status.java", "package com.hr.b;\npublic enum Status { Y }\n")
    idx = build_scan_index(
        str(tmp_path),
        [
            {"module": "hr_a", "java_files": ["a/Status.java"]},
            {"module": "hr_b", "java_files": ["b/Status.java"]},
        ],
    )
    names = set(idx.enum_python_names.values())
    assert names == {"Status_hr_a", "Status_hr_b"}


def test_enum_name_collision_dedup_tier3_by_package_when_same_module(tmp_path):
    _write(tmp_path, "a/Status.java", "package com.hr.a;\npublic enum Status { X }\n")
    _write(tmp_path, "b/Status.java", "package com.hr.b;\npublic enum Status { Y }\n")
    idx = build_scan_index(
        str(tmp_path), [{"module": "hr", "java_files": ["a/Status.java", "b/Status.java"]}]
    )
    names = set(idx.enum_python_names.values())
    assert names == {"Status_com_hr_a", "Status_com_hr_b"}


def test_entity_name_collision_same_module_tie_break_by_fqn_not_scan_order(tmp_path):
    _write(tmp_path, "a/Address.java", "package com.hr.a;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"addr_a\")\npublic class Address {\n@Id private Long id;\n}\n")
    _write(tmp_path, "b/Address.java", "package com.hr.b;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"addr_b\")\npublic class Address {\n@Id private Long id;\n}\n")
    # java_files 刻意把 b/Address.java 排在 a/Address.java 前面（掃描順序
    # 上 b 先出現），驗證 tie-break 依據的是 FQN 字典序
    # （"com.hr.a.Address" < "com.hr.b.Address"），不是掃描順序——若還是
    # 舊的「先掃到的贏」規則，這裡會判給 b，跟斷言矛盾。
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["b/Address.java", "a/Address.java"]}])
    assert list(idx.entities.keys()) == ["com.hr.a.Address"]
    assert len(idx.duplicate_entity_skips) == 1
    assert idx.duplicate_entity_skips[0]["class_name"] == "Address"
    assert idx.duplicate_entity_skips[0]["file_path"] == "b/Address.java"


def test_entity_name_collision_cross_module_both_kept(tmp_path):
    _write(tmp_path, "a/Address.java", "package com.hr.a;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"addr_a\")\npublic class Address {\n@Id private Long id;\n}\n")
    _write(tmp_path, "b/Address.java", "package com.hr.b;\nimport javax.persistence.*;\n@Entity\n@Table(name=\"addr_b\")\npublic class Address {\n@Id private Long id;\n}\n")
    idx = build_scan_index(
        str(tmp_path),
        [
            {"module": "hr_a", "java_files": ["a/Address.java"]},
            {"module": "hr_b", "java_files": ["b/Address.java"]},
        ],
    )
    assert set(idx.entities.keys()) == {"com.hr.a.Address", "com.hr.b.Address"}
    assert idx.duplicate_entity_skips == []


def test_fallback_pk_type_majority_vote(tmp_path):
    _write(tmp_path, "A.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\npublic class A {\n@Id private Long id;\n}\n")
    _write(tmp_path, "B.java", "package com.hr;\nimport javax.persistence.*;\n@Entity\npublic class B {\n@Id private Long id;\n}\n")
    _write(tmp_path, "C.java", "package com.hr;\nimport javax.persistence.*;\nimport java.util.UUID;\n@Entity\npublic class C {\n@Id private UUID id;\n}\n")
    idx = build_scan_index(str(tmp_path), [{"module": "hr", "java_files": ["A.java", "B.java", "C.java"]}])
    assert idx.fallback_pk_type == ("int", "Integer")


def test_fallback_pk_type_defaults_to_integer_when_no_pk_resolved():
    from scaffold_agent.reference_resolver import _fallback_pk_type

    assert _fallback_pk_type({}) == ("int", "Integer")
