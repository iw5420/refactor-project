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


def test_http_mapping_annotations_extracted_per_method(tmp_path):
    """對應 docs/09b_bug_trace.md #70：5 種 HTTP method 簡寫 annotation
    各自機械對應到固定的 HTTP method 大寫字面字串。真實案例：
    `FileController.voice()` 有 `@PostMapping`／`@GetMapping` 兩個同名
    多載，各自的 `http_method` 必須正確區分，不能混淆或都是 None。"""
    (tmp_path / "FileController.java").write_text(
        """
        package com.example;
        import org.springframework.web.bind.annotation.*;
        @RestController
        public class FileController {
            @PostMapping("/voice")
            public String voice(String kind) { return null; }
            @GetMapping("/voice")
            public String voice(String location) { return null; }
            @PutMapping("/x")
            public void putX() {}
            @DeleteMapping("/x")
            public void deleteX() {}
            @PatchMapping("/x")
            public void patchX() {}
        }
        """,
        encoding="utf-8",
    )

    result = scan_java_files(["FileController.java"], str(tmp_path))
    methods = {(m.method_name, len(m.params)): m for m in result["FileController"].methods}

    assert methods[("voice", 1)].http_method in ("POST", "GET")
    voice_methods = [m for m in result["FileController"].methods if m.method_name == "voice"]
    assert {m.http_method for m in voice_methods} == {"POST", "GET"}
    assert methods[("putX", 0)].http_method == "PUT"
    assert methods[("deleteX", 0)].http_method == "DELETE"
    assert methods[("patchX", 0)].http_method == "PATCH"


def test_method_without_http_mapping_annotation_has_none_http_method(tmp_path):
    (tmp_path / "UserService.java").write_text(
        """
        package com.example;
        import org.springframework.stereotype.Service;
        @Service
        public class UserService {
            public void doWork() {}
        }
        """,
        encoding="utf-8",
    )

    result = scan_java_files(["UserService.java"], str(tmp_path))
    sig = result["UserService"].methods[0]

    assert sig.http_method is None


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
