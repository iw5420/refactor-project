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


def test_response_entity_boundary_method_gets_response_return_type_and_skips_schema(tmp_path):
    """對應 docs/09b_bug_trace.md #30：API 邊界方法若用 ResponseEntity<T>
    顯式控制 HTTP status（真實案例 FileController.voice），InterfaceSpec
    的 return_type 要被覆寫成 "Response"，且不該為它渲染任何 schema
    class（openapi 的 response schema 對這種方法沒有代表性，見
    resolve_api_boundary_signature() docstring）。
    """
    (tmp_path / "FileController.java").write_text(
        """
        package com.example;
        import org.springframework.web.bind.annotation.RestController;
        import org.springframework.web.bind.annotation.GetMapping;
        import org.springframework.http.ResponseEntity;
        @RestController
        public class FileController {
            @GetMapping("/voice")
            public ResponseEntity<FileRs> voice(String location) {
                return null;
            }
        }
        """,
        encoding="utf-8",
    )
    module = {
        "module": "file", "summary": "", "java_files": ["FileController.java"], "depends_on": [],
        "methods": [
            {"java_method": "voice", "class_name": "FileController", "description": "取得語音檔", "complexity": "low"}
        ],
    }
    api_to_python_target = [
        {"endpoint": "/voice", "http_method": "GET", "java_controller": "FileController.voice", "module": "file"}
    ]
    from design_agent.design import _build_boundary_index

    boundary_index = _build_boundary_index(api_to_python_target)
    openapi_spec = {
        "paths": {
            "/voice": {
                "get": {
                    "parameters": [{"name": "location", "schema": {"type": "string"}}],
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/FileRs"}}}}
                    },
                }
            }
        }
    }

    from unittest.mock import patch

    with patch("design_agent.design._call_design_llm", return_value=({}, {}, {"FileController::voice(String)": False})):
        result = _design_module(
            module=module, boundary_index=boundary_index, openapi_spec=openapi_spec,
            java_project_path=str(tmp_path), interfaces_by_module={},
        )

    assert len(result.interfaces) == 1
    iface = result.interfaces[0]
    assert iface["return_type"] == "Response"
    assert iface["file_path"] == "app/routers/file_router.py"
    assert result.directory_tree_fragment is None


def test_same_named_overloads_with_different_http_methods_each_get_own_boundary(tmp_path):
    """對應 docs/09b_bug_trace.md #70 真實案例：`FileController.voice()`
    有 `@PostMapping`（上傳）／`@GetMapping`（下載）兩個同名多載。修正前
    `_build_boundary_index()` 用 `(module, class, method_name)` 當 key，
    兩個 ApiMapping 互相覆寫，只留得住最後一筆——另一個完全查不到
    boundary，退回機械型別對應 Java 原始碼字面型別，翻出不合法的回傳
    型別注記（如裸的 `ResponseResult`），讓 FastAPI 在匯入階段直接崩潰、
    整個服務起不來。這裡直接驗證兩個 overload **都**正確拿到各自的
    boundary／schema，不是其中一個被犧牲。
    """
    (tmp_path / "FileController.java").write_text(
        """
        package com.example;
        import org.springframework.web.bind.annotation.RestController;
        import org.springframework.web.bind.annotation.PostMapping;
        import org.springframework.web.bind.annotation.GetMapping;
        @RestController
        public class FileController {
            @PostMapping("/voice")
            public VoiceRs voice(String kind) {
                return null;
            }
            @GetMapping("/voice")
            public VoiceDownloadRs voice(String location) {
                return null;
            }
        }
        """,
        encoding="utf-8",
    )
    module = {
        "module": "file", "summary": "", "java_files": ["FileController.java"], "depends_on": [],
        "methods": [
            {"java_method": "voice", "class_name": "FileController", "description": "上傳／下載語音檔", "complexity": "low"}
        ],
    }
    api_to_python_target = [
        {"endpoint": "/voice", "http_method": "POST", "java_controller": "FileController.voice", "module": "file"},
        {"endpoint": "/voice", "http_method": "GET", "java_controller": "FileController.voice", "module": "file"},
    ]
    from design_agent.design import _build_boundary_index

    boundary_index = _build_boundary_index(api_to_python_target)
    # 對應 #70 修正：兩筆 ApiMapping 現在各自有獨立的 key，不會互相覆寫。
    assert len(boundary_index) == 2

    openapi_spec = {
        "paths": {
            "/voice": {
                "post": {
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/VoiceRs"}}}}
                    },
                },
                "get": {
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/VoiceDownloadRs"}}}}
                    },
                },
            }
        }
    }

    from unittest.mock import patch

    with patch(
        "design_agent.design._call_design_llm",
        return_value=({}, {}, {"FileController::voice(String)": False}),
    ):
        result = _design_module(
            module=module, boundary_index=boundary_index, openapi_spec=openapi_spec,
            java_project_path=str(tmp_path), interfaces_by_module={},
        )

    assert len(result.interfaces) == 2
    return_types = {iface["function_name"]: iface["return_type"] for iface in result.interfaces}
    # 兩個 overload 都要拿到各自正確、具名的 schema 型別——不是其中一個
    # 被犧牲成裸的、不合法的機械型別（修正前的真實崩潰案例）。
    assert return_types["voice"] == "VoiceRs"
    assert return_types["voice_2"] == "VoiceDownloadRs"
