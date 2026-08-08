"""design_agent/design.py 的孤兒類別／資料容器占位決策樹測試（見 05a
三章「孤兒類別／資料容器占位」判斷優先序）。只測機械分類邏輯本身，不觸發
`_call_design_llm()`（不呼叫真實 Claude API）——測試用的 module["methods"]
一律留空，讓 `_build_method_contexts()` 產出空 contexts、`classes_needing_
layer` 也因為所有測試類別的 `methods` 皆空而不會被觸發（見 `_design_module()`
docstring 對 `classes_needing_layer` 的既有說明）。
"""
from design_agent.design import _design_module

_JAVA_SOURCES = {
    # 分支 3：有建構子、無 Lombok 標記——既有行為（迴歸測試，確認優先序
    # 調整後這個既有案例沒有被改壞）。
    "AuthException.java": """
        package com.example;
        public class AuthException extends RuntimeException {
            public AuthException(String message) {
                super(message);
            }
        }
    """,
    # 分支 2：有 Lombok 標記、部分欄位非 final → mutable dataclass。
    "UserDto.java": """
        package com.example;
        import lombok.Data;
        @Data
        public class UserDto {
            private final String name;
            private int age;
        }
    """,
    # 分支 2：Lombok @Value、全部欄位 final → frozen dataclass。
    "Point.java": """
        package com.example;
        import lombok.Value;
        @Value
        public class Point {
            private final int x;
            private final int y;
        }
    """,
    # 分支 4：無 Lombok 標記、有欄位——低信心推斷仍渲染 dataclass。
    "PlainHolder.java": """
        package com.example;
        public class PlainHolder {
            String label;
        }
    """,
    # 分支 5：什麼都沒有——只記警告，不渲染。
    "Marker.java": """
        package com.example;
        public class Marker {
        }
    """,
    # 分支 1：@Entity——完全跳過，不是③的職責。
    "Product.java": """
        package com.example;
        import javax.persistence.Entity;
        @Entity
        public class Product {
            private final String sku;
        }
    """,
}


def _write_sources(tmp_path):
    for filename, source in _JAVA_SOURCES.items():
        (tmp_path / filename).write_text(source, encoding="utf-8")
    return [filename for filename in _JAVA_SOURCES]


def _run_design_module(tmp_path):
    java_files = _write_sources(tmp_path)
    module = {
        "module": "auth",
        "summary": "",
        "java_files": java_files,
        "depends_on": [],
        "methods": [],  # 刻意留空，避免觸發 _call_design_llm()（見檔案 docstring）
    }
    return _design_module(
        module=module,
        boundary_index={},
        openapi_spec={"paths": {}},
        java_project_path=str(tmp_path),
        interfaces_by_module={},
    )


def test_auth_exception_still_renders_constructor_placeholder(tmp_path):
    result = _run_design_module(tmp_path)
    assert "AuthException" in result.directory_tree_fragment
    assert "建構子 1: AuthException(message: str)" in result.directory_tree_fragment
    # 不應該被誤判進 dataclass 段落。
    assert "class AuthException" not in result.directory_tree_fragment


def test_lombok_data_class_with_mixed_finality_is_mutable_dataclass(tmp_path):
    result = _run_design_module(tmp_path)
    fragment = result.directory_tree_fragment
    assert "偵測到 Lombok/JPA 資料標記" in fragment
    assert "@dataclass\nclass UserDto:" in fragment
    assert "name: str" in fragment
    assert "age: int" in fragment
    assert "@dataclass(frozen=True)\nclass UserDto" not in fragment


def test_lombok_value_class_with_all_final_fields_is_frozen(tmp_path):
    result = _run_design_module(tmp_path)
    fragment = result.directory_tree_fragment
    assert "@dataclass(frozen=True)\nclass Point:" in fragment
    assert "x: int" in fragment
    assert "y: int" in fragment


def test_plain_holder_without_lombok_marker_uses_low_confidence_note(tmp_path):
    result = _run_design_module(tmp_path)
    fragment = result.directory_tree_fragment
    assert "無 Lombok 標記，依欄位宣告推斷" in fragment
    assert "@dataclass\nclass PlainHolder:" in fragment
    assert "label: str" in fragment


def test_truly_empty_class_only_logs_warning_and_is_not_rendered(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        result = _run_design_module(tmp_path)
    fragment = result.directory_tree_fragment
    assert "Marker" not in fragment
    assert any("Marker" in record.message for record in caplog.records)


def test_entity_annotated_class_is_skipped_entirely_without_warning(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        result = _run_design_module(tmp_path)
    fragment = result.directory_tree_fragment
    assert "Product" not in fragment
    assert not any("Product" in record.message for record in caplog.records)


def test_stateless_controller_with_real_methods_is_not_treated_as_orphan(tmp_path, caplog):
    """迴歸測試：對照真實 lang-exam-api-refactor 專案跑一次完整 04a→05a
    後發現的 bug——`covered_class_names` 只看 InterfaceSpec.class_name，
    但 routers 層的 class_name 一律是 None（05a 七章既有規則），導致一個
    正常、方法齊全的 @RestController（若剛好沒有欄位也沒有建構子，如
    無狀態的 FileController）會被誤判成孤兒類別、印出不該出現的警告；
    若該 Controller 有 @Autowired 欄位，甚至會被誤渲染成錯誤的
    dataclass 段落。修正是在决策樹最前面加一道 `sig.methods` 非空即
    跳過的門檻。這裡用 `unittest.mock.patch` 掉 `_call_design_llm`（
    router 方法一律 `needs_db_session_decision=True`，光靠留空
    `module["methods"]` 無法避開 LLM 呼叫，見 `_build_method_contexts()`
    docstring），確認 `_design_module()` 對這個案例的呼叫結果不受影響。
    """
    (tmp_path / "FileController.java").write_text(
        """
        package com.example;
        import org.springframework.web.bind.annotation.RestController;
        import org.springframework.web.bind.annotation.GetMapping;
        @RestController
        public class FileController {
            @GetMapping("/api/file/ping")
            public String ping() {
                return "pong";
            }
        }
        """,
        encoding="utf-8",
    )
    module = {
        "module": "file",
        "summary": "",
        "java_files": ["FileController.java"],
        "depends_on": [],
        "methods": [
            {
                "java_method": "ping",
                "class_name": "FileController",
                "description": "health check",
                "complexity": "low",
            }
        ],
    }

    from unittest.mock import patch

    with patch("design_agent.design._call_design_llm", return_value=({}, {}, {"FileController::ping()": False})):
        with caplog.at_level("WARNING"):
            result = _design_module(
                module=module,
                boundary_index={},
                openapi_spec={"paths": {}},
                java_project_path=str(tmp_path),
                interfaces_by_module={},
            )

    assert any(iface["function_name"] == "ping" for iface in result.interfaces)
    assert not any("FileController" in record.message for record in caplog.records)
    assert result.directory_tree_fragment is None
