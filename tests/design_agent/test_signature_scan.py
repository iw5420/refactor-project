"""design_agent/signature_scan.py 新增的欄位／annotation 擷取（見 05a
四章、`JavaClassSignature.annotations`／`.fields` docstring）。
"""
from design_agent.signature_scan import scan_java_files


def test_scan_extracts_annotations_and_fields_for_class_declaration(tmp_path):
    (tmp_path / "UserDto.java").write_text(
        """
        package com.example;
        import lombok.Data;
        @Data
        public class UserDto {
            private final String name;
            private int age, score;
        }
        """,
        encoding="utf-8",
    )

    result = scan_java_files(["UserDto.java"], str(tmp_path))
    sig = result["UserDto"]

    assert sig.annotations == ["Data"]
    fields_by_name = {f.name: f for f in sig.fields}
    assert set(fields_by_name) == {"name", "age", "score"}
    assert fields_by_name["name"].java_type == "String"
    assert fields_by_name["name"].is_final is True
    assert fields_by_name["age"].is_final is False
    assert fields_by_name["score"].is_final is False


def test_interface_declaration_has_empty_annotations_and_fields(tmp_path):
    (tmp_path / "UserRepository.java").write_text(
        """
        package com.example;
        public interface UserRepository extends JpaRepository<User, Long> {
        }
        """,
        encoding="utf-8",
    )

    result = scan_java_files(["UserRepository.java"], str(tmp_path))
    sig = result["UserRepository"]

    assert sig.annotations == []
    assert sig.fields == []


def test_multiple_annotations_are_all_captured_not_just_stereotype():
    from design_agent.signature_scan import _annotation_names
    import javalang

    tree = javalang.parse.parse(
        """
        package com.example;
        import javax.persistence.Entity;
        import lombok.Getter;
        @Entity
        @Getter
        public class Product {
        }
        """
    )
    decl = list(tree.types)[0]
    assert _annotation_names(decl.annotations) == ["Entity", "Getter"]
