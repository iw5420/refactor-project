"""design_agent/design.py 的孤兒類別／資料容器占位決策樹測試（見 05a
三章「孤兒類別／資料容器占位」判斷優先序）。只測機械分類邏輯本身，不觸發
`_call_design_llm()`（不呼叫真實 Claude API）——測試用的 module["methods"]
一律留空，讓 `_build_method_contexts()` 產出空 contexts、`classes_needing_
layer` 也因為所有測試類別的 `methods` 皆空而不會被觸發（見 `_design_module()`
docstring 對 `classes_needing_layer` 的既有說明）。
"""
from design_agent.design import _design_module, design_all_modules

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


def _run_design_module(tmp_path, openapi_spec=None):
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
        openapi_spec=openapi_spec if openapi_spec is not None else {"paths": {}},
        java_project_path=str(tmp_path),
        interfaces_by_module={},
        skip_excluded_overloads=[],
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


def test_class_already_a_named_openapi_schema_is_not_rendered_as_orphan(tmp_path, caplog):
    """對應 docs/refactor_bug_trace.md #25 真實案例：`CreaterandomRs` 同時
    被 `candidate` 模組（自己的 API 邊界方法用到，openapi 具名 schema，
    正確渲染成 Pydantic BaseModel）跟 `exam` 模組（不是自己的端點、但
    `ExamService.createRandom()` 這個橫跨兩模組的共用 service class 把它
    拖進了 exam 模組的 java_files，Lombok 標記符合分支 2）各自定義一份，
    兩個是不同的 class 物件，Pydantic 驗證時互相不相容。修好之前，這裡
    完全沒有查過 openapi_spec，只憑 Java AST 上的 Lombok 標記猜測，把
    `UserDto`（跟上面 Lombok 分支測試共用同一個 fixture，這裡刻意把它的
    名稱也登記進 openapi_spec 具名 schema，模擬『別的模組已經翻好』的
    情境）誤判成孤兒、另外渲染一份不相容的 dataclass。"""
    with caplog.at_level("WARNING"):
        result = _run_design_module(
            tmp_path,
            openapi_spec={"components": {"schemas": {"UserDto": {"type": "object"}}}},
        )
    fragment = result.directory_tree_fragment
    assert "class UserDto" not in (fragment or "")
    assert any("UserDto" in record.message for record in caplog.records)
    # 其餘分支不受影響，維持既有行為。
    assert "@dataclass(frozen=True)\nclass Point:" in fragment


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
                skip_excluded_overloads=[],
            )

    assert any(iface["function_name"] == "ping" for iface in result.interfaces)
    assert not any("FileController" in record.message for record in caplog.records)
    assert result.directory_tree_fragment is None


def test_repository_with_all_methods_excluded_but_still_referenced_synthesizes_find_all(tmp_path, caplog):
    """迴歸測試：對照真實 `lang-exam-api-refactor` 專案跑一次完整
    04→10（含 debug 迴圈）後發現的真實案例——`QuestionRepository` 唯一
    顯式宣告的方法 `findByTypeAndPart()` 只被一個使用者標記 skip 的
    endpoint 呼叫，正確地被①的呼叫鏈排除機制從 `module["methods"]`
    移除；但沒有被 skip 的 `QuestionService.getList()` 仍然呼叫
    `questionRepository.findAll()`——`findAll()` 繼承自
    `JpaRepository`，Java 原始碼從未顯式宣告，`class_signatures` 永遠
    看不到它。修正前：`QuestionRepository` 因為③自己重新掃描仍看得到
    `findByTypeAndPart`（不受①排除影響），被孤兒判斷誤判成「已處理」
    整個跳過，不產生任何 InterfaceSpec、不留下任何警告，`QuestionService.
    getList()` 因此永遠依賴一個不存在的目標。這裡模擬同樣的落差：
    `QuestionRepository.findByTypeAndPart` 存在於 Java 原始碼、但刻意不
    放進 `module["methods"]`（模擬①的呼叫鏈排除）。

    `QuestionRepository` 顯式 `extends JpaRepository<QuestionEntity, String>`
    ——對應真實 `ExamRepository` 的寫法（見 docs/refactor_bug_trace.md
    #10／#16），entity 型別現在直接從 extends 子句讀出（`sig.
    jpa_base_entity`），不再從既有方法的回傳型別反推。
    """
    (tmp_path / "QuestionRepository.java").write_text(
        """
        package com.example.repository;
        import java.util.List;
        import org.springframework.data.jpa.repository.JpaRepository;
        public interface QuestionRepository extends JpaRepository<QuestionEntity, String> {
            List<QuestionEntity> findByTypeAndPart(String type, String part);
        }
        """,
        encoding="utf-8",
    )
    (tmp_path / "QuestionService.java").write_text(
        """
        package com.example.service;
        import java.util.List;
        import org.springframework.beans.factory.annotation.Autowired;
        import org.springframework.stereotype.Service;
        @Service
        public class QuestionService {
            @Autowired
            private QuestionRepository questionRepository;

            public List<QuestionEntity> getList() {
                return questionRepository.findAll();
            }
        }
        """,
        encoding="utf-8",
    )
    module = {
        "module": "grading",
        "summary": "",
        "java_files": ["QuestionRepository.java", "QuestionService.java"],
        "depends_on": [],
        # 刻意不放 QuestionRepository::findByTypeAndPart，模擬①的呼叫鏈
        # 排除（如 skip）已經把它從這裡移除。
        "methods": [
            {"java_method": "getList", "class_name": "QuestionService", "description": "", "complexity": "low"},
        ],
    }

    from unittest.mock import patch

    with patch(
        "design_agent.design._call_design_llm",
        return_value=(
            {"QuestionRepository": "repositories"},
            {},
            {"QuestionService::getList()": True},
        ),
    ):
        with caplog.at_level("WARNING"):
            result = _design_module(
                module=module,
                boundary_index={},
                openapi_spec={"paths": {}},
                java_project_path=str(tmp_path),
                interfaces_by_module={},
                skip_excluded_overloads=[],
            )

    synthesized = [iface for iface in result.interfaces if iface["function_name"] == "find_all"]
    assert len(synthesized) == 1
    assert synthesized[0]["class_name"] == "QuestionRepository"
    assert synthesized[0]["file_path"] == "app/repositories/grading_repository.py"
    assert synthesized[0]["return_type"] == "list[QuestionEntity]"
    assert synthesized[0]["params"] == [{"name": "db", "type": "Session"}]
    assert synthesized[0]["phase"] == 1
    assert synthesized[0]["java_method_id"] == "QuestionRepository.java::QuestionRepository::findAll"
    assert synthesized[0]["jpa_base_entity"] == "QuestionEntity"

    # findByTypeAndPart 本身不在 module["methods"] 裡，維持不被翻譯——
    # 這裡確認合成 find_all() 不會連帶意外產生 findByTypeAndPart 的介面。
    assert not any(iface["function_name"] == "find_by_type_and_part" for iface in result.interfaces)

    assert any("QuestionRepository" in record.message for record in caplog.records)
    # 不應該再落入孤兒判斷、被當成「什麼都沒有」的空段落。
    assert result.directory_tree_fragment is None


def test_repository_with_all_methods_excluded_and_unreferenced_stays_silent(tmp_path, caplog):
    """對照組：`QuestionRepository` 若真的沒有任何存活類別引用它（沒有
    `@Autowired` 欄位指向它），不應該被合成任何方法——避免對真正無人使用
    的孤兒 class 產生從沒被呼叫過的死程式碼。
    """
    (tmp_path / "QuestionRepository.java").write_text(
        """
        package com.example.repository;
        import java.util.List;
        public interface QuestionRepository {
            List<QuestionEntity> findByTypeAndPart(String type, String part);
        }
        """,
        encoding="utf-8",
    )
    module = {
        "module": "grading",
        "summary": "",
        "java_files": ["QuestionRepository.java"],
        "depends_on": [],
        "methods": [],
    }

    from unittest.mock import patch

    with patch(
        "design_agent.design._call_design_llm",
        return_value=({"QuestionRepository": "repositories"}, {}, {}),
    ):
        result = _design_module(
            module=module,
            boundary_index={},
            openapi_spec={"paths": {}},
            java_project_path=str(tmp_path),
            interfaces_by_module={},
            skip_excluded_overloads=[],
        )

    assert not any(iface["function_name"] == "find_all" for iface in result.interfaces)


def test_repository_with_all_methods_excluded_but_referenced_from_non_repository_layer_only_warns(tmp_path, caplog):
    """`resolved_layer` 不是 `repositories`（如剛好被 LLM 或 stereotype
    判成 services）時，沒有安全的內建 CRUD 預設值可以套用——只記警告，
    不合成任何方法，避免猜錯操作型態產生錯誤的程式碼。
    """
    (tmp_path / "SomeHelper.java").write_text(
        """
        package com.example.service;
        public class SomeHelper {
            public String doThing(String x) { return x; }
        }
        """,
        encoding="utf-8",
    )
    (tmp_path / "QuestionService.java").write_text(
        """
        package com.example.service;
        import org.springframework.beans.factory.annotation.Autowired;
        import org.springframework.stereotype.Service;
        @Service
        public class QuestionService {
            @Autowired
            private SomeHelper someHelper;

            public String getList() {
                return someHelper.someInheritedThing();
            }
        }
        """,
        encoding="utf-8",
    )
    module = {
        "module": "grading",
        "summary": "",
        "java_files": ["SomeHelper.java", "QuestionService.java"],
        "depends_on": [],
        "methods": [
            {"java_method": "getList", "class_name": "QuestionService", "description": "", "complexity": "low"},
        ],
    }

    from unittest.mock import patch

    with patch(
        "design_agent.design._call_design_llm",
        return_value=(
            {"SomeHelper": "services"},
            {},
            {"QuestionService::getList()": True},
        ),
    ):
        with caplog.at_level("WARNING"):
            result = _design_module(
                module=module,
                boundary_index={},
                openapi_spec={"paths": {}},
                java_project_path=str(tmp_path),
                interfaces_by_module={},
                skip_excluded_overloads=[],
            )

    assert not any(iface["class_name"] == "SomeHelper" for iface in result.interfaces)
    assert any("SomeHelper" in record.message for record in caplog.records)


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
            skip_excluded_overloads=[],
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
            skip_excluded_overloads=[],
        )

    assert len(result.interfaces) == 2
    return_types = {iface["function_name"]: iface["return_type"] for iface in result.interfaces}
    # 兩個 overload 都要拿到各自正確、具名的 schema 型別——不是其中一個
    # 被犧牲成裸的、不合法的機械型別（修正前的真實崩潰案例）。
    assert return_types["voice"] == "VoiceRs"
    assert return_types["voice_2"] == "VoiceDownloadRs"


def test_skip_excluded_overload_produces_no_interface_for_that_http_method_only(tmp_path):
    """對應 docs/refactor_bug_trace.md #8：使用者填 skip，是人工判斷
    「這個 endpoint 整段不進翻譯流程」，不只是跳過自動化測試。同名不同
    HTTP method 的多載（`FileController.voice()` 的 POST／GET）用同一個
    `method_id`，POST 版被標 skip 時，只有這一個多載該整段不產生
    InterfaceSpec，GET 版（沒被標 skip）要完全不受影響。
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
    # 只有 GET 版留在 api_to_python_target——POST 版被 skip，
    # assemble_api_mapping() 已經把它整筆排除（見 04a 五章、
    # docs/refactor_bug_trace.md #8）。
    api_to_python_target = [
        {"endpoint": "/voice", "http_method": "GET", "java_controller": "FileController.voice", "module": "file"},
    ]
    from design_agent.design import _build_boundary_index

    boundary_index = _build_boundary_index(api_to_python_target)

    openapi_spec = {
        "paths": {
            "/voice": {
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
            skip_excluded_overloads=[("FileController", "voice", "POST")],
        )

    # 只剩 GET 版一個 InterfaceSpec——POST 版整段沒有產生任何骨架，
    # 不是「產生了但缺裝飾器／型別壞掉」。
    assert len(result.interfaces) == 1
    assert result.interfaces[0]["return_type"] == "VoiceDownloadRs"


# ── Phase 1／Phase 2 分階段翻譯設計新增：utils package 機械分類、
# InterfaceSpec.phase 欄位（見 05a 三章「Utils 特例」、九章「phase 欄位」，
# `refactor_plan.md` 二章）──


def test_utils_package_class_gets_utils_layer_and_phase_1(tmp_path):
    """`com.example.utils` 下的無 stereotype 類別（真實案例：
    `ValidationUtil`）要機械歸類成 utils 層、`phase=1`，file_path 直接
    複製 package 結構（不套用 `{module}_{layer}.py`），class_name 比照
    routers 層既有慣例設 None。也驗證這條路徑**不會**觸發
    `classes_needing_layer`（不需要 LLM 判斷歸屬，見 `_design_module()`
    對 `classes_needing_layer` 的既有說明＋新增的 utils 排除）。
    """
    (tmp_path / "ValidationUtil.java").write_text(
        """
        package com.example.utils;
        public class ValidationUtil {
            public static boolean isValidField(String str) {
                return str != null;
            }
        }
        """,
        encoding="utf-8",
    )
    module = {
        "module": "exam", "summary": "", "java_files": ["ValidationUtil.java"], "depends_on": [],
        "methods": [
            {
                "java_method": "isValidField", "class_name": "ValidationUtil",
                "description": "檢查欄位是否有效", "complexity": "low",
            }
        ],
    }

    from unittest.mock import patch

    with patch(
        "design_agent.design._call_design_llm",
        return_value=({}, {}, {"ValidationUtil::isValidField(String)": False}),
    ) as mock_llm:
        result = _design_module(
            module=module, boundary_index={}, openapi_spec={"paths": {}},
            java_project_path=str(tmp_path), interfaces_by_module={},
            skip_excluded_overloads=[],
        )
        # classes_needing_layer 必須是空清單：utils package 機械判定出
        # "utils"，不是「機械規則判斷不了」的情況，不該問 LLM 歸屬。
        assert mock_llm.call_args.args[1] == []

    assert len(result.interfaces) == 1
    iface = result.interfaces[0]
    assert iface["file_path"] == "app/utils/validation_util.py"
    assert iface["class_name"] is None
    assert iface["function_name"] == "is_valid_field"
    assert iface["phase"] == 1


def test_repository_class_gets_phase_1_service_gets_phase_2(tmp_path):
    """`@Repository` → phase=1，`@Service` → phase=2（見 `refactor_plan.md`
    二章機械對應規則），確認既有 stereotype 判定路徑正確接上新增的
    phase 欄位計算，不是只有 utils 這條新路徑有 phase。
    """
    (tmp_path / "UserRepository.java").write_text(
        """
        package com.example.repository;
        import org.springframework.stereotype.Repository;
        @Repository
        public class UserRepository {
            public String findById(String id) { return null; }
        }
        """,
        encoding="utf-8",
    )
    (tmp_path / "UserService.java").write_text(
        """
        package com.example.service;
        import org.springframework.stereotype.Service;
        @Service
        public class UserService {
            public String getUser(String id) { return null; }
        }
        """,
        encoding="utf-8",
    )
    module = {
        "module": "user", "summary": "",
        "java_files": ["UserRepository.java", "UserService.java"], "depends_on": [],
        "methods": [
            {"java_method": "findById", "class_name": "UserRepository", "description": "", "complexity": "low"},
            {"java_method": "getUser", "class_name": "UserService", "description": "", "complexity": "low"},
        ],
    }

    from unittest.mock import patch

    with patch(
        "design_agent.design._call_design_llm",
        return_value=(
            {}, {},
            {"UserRepository::findById(String)": False, "UserService::getUser(String)": False},
        ),
    ):
        result = _design_module(
            module=module, boundary_index={}, openapi_spec={"paths": {}},
            java_project_path=str(tmp_path), interfaces_by_module={},
            skip_excluded_overloads=[],
        )

    phase_by_function = {iface["function_name"]: iface["phase"] for iface in result.interfaces}
    assert phase_by_function["find_by_id"] == 1
    assert phase_by_function["get_user"] == 2

    # java_method_id 逐字沿用①既有的 parse_agent/types.py::method_id()
    # 格式（"{file}::{class}::{method}"，不含參數型別），供 [P] 六章
    # 「呼叫鏈範圍查找」直接查①的呼叫圖，見 05a 對應章節。
    id_by_function = {iface["function_name"]: iface["java_method_id"] for iface in result.interfaces}
    assert id_by_function["find_by_id"] == "UserRepository.java::UserRepository::findById"
    assert id_by_function["get_user"] == "UserService.java::UserService::getUser"


def test_design_all_modules_builds_java_index_from_all_interfaces(tmp_path):
    """`design_all_modules()` 的第六個回傳值 `java_index` 是對
    `all_interfaces` 的收尾投影：每一筆 InterfaceSpec 的 java_method_id
    都要能查回它自己的 Python 對應資訊（file_path/class_name/
    function_name/phase），見 06a_plan_agent_architecture.md 六章。
    """
    (tmp_path / "UserRepository.java").write_text(
        """
        package com.example.repository;
        import org.springframework.stereotype.Repository;
        @Repository
        public class UserRepository {
            public String findById(String id) { return null; }
        }
        """,
        encoding="utf-8",
    )
    module_list = [
        {
            "module": "user", "summary": "",
            "java_files": ["UserRepository.java"], "depends_on": [],
            "methods": [
                {"java_method": "findById", "class_name": "UserRepository", "description": "", "complexity": "low"},
            ],
        }
    ]

    from unittest.mock import patch

    with patch("design_agent.design._call_design_llm", return_value=({}, {}, {})):
        interfaces, _, _, _, _, java_index = design_all_modules(
            module_list=module_list, api_to_python_target=[], openapi_spec={"paths": {}},
            java_project_path=str(tmp_path), skip_excluded_overloads=[],
        )

    assert len(interfaces) == 1
    iface = interfaces[0]
    assert java_index[iface["java_method_id"]] == {
        "file_path": iface["file_path"],
        "class_name": iface["class_name"],
        "function_name": iface["function_name"],
        "phase": iface["phase"],
    }
    # UserRepository 沒有 extends JpaRepository，這個專案不需要
    # BaseRepository，不應該出現任何合成的 java_index 條目或
    # app/core/base_repository.py 基礎設施段。
    from common.jpa_base_repository import BASE_REPOSITORY_FILE

    assert not any(entry["file_path"] == BASE_REPOSITORY_FILE for entry in java_index.values())


def test_design_all_modules_registers_base_repository_when_jpa_repository_present(tmp_path):
    """對應 docs/refactor_bug_trace.md #10／#16：專案裡至少一個 repository
    `extends JpaRepository<Entity, Id>` 時，`design_all_modules()` 要
    (1) 在 directory_tree 的基礎設施段輸出 `app/core/base_repository.py`，
    (2) 在 java_index 裡登記 7 個合成條目，全部指向同一個
    `BASE_REPOSITORY_FILE`／`BASE_REPOSITORY_CLASS`，供①的呼叫圖對
    `examRepository.findAll()` 這類繼承而來的呼叫合成的
    `synthetic_java_method_id()` 反查（見 parse_agent/call_graph.py
    `_yield_call()`）。
    """
    (tmp_path / "ExamRepository.java").write_text(
        """
        package com.example.repository;
        import org.springframework.data.jpa.repository.JpaRepository;
        public interface ExamRepository extends JpaRepository<ExamEntity, String> {
            ExamEntity findByCard(String card);
        }
        """,
        encoding="utf-8",
    )
    module_list = [
        {
            "module": "exam", "summary": "",
            "java_files": ["ExamRepository.java"], "depends_on": [],
            "methods": [
                {"java_method": "findByCard", "class_name": "ExamRepository", "description": "", "complexity": "low"},
            ],
        }
    ]

    from unittest.mock import patch

    from common.jpa_base_repository import (
        BASE_REPOSITORY_CLASS,
        BASE_REPOSITORY_FILE,
        JPA_BASE_METHOD_NAME_MAP,
        synthetic_java_method_id,
    )

    with patch(
        "design_agent.design._call_design_llm",
        return_value=({"ExamRepository": "repositories"}, {}, {}),
    ):
        _, directory_tree, _, _, _, java_index = design_all_modules(
            module_list=module_list, api_to_python_target=[], openapi_spec={"paths": {}},
            java_project_path=str(tmp_path), skip_excluded_overloads=[],
        )

    assert f"### {BASE_REPOSITORY_FILE}" in directory_tree
    assert "class BaseRepository(Generic[T]):" in directory_tree

    for java_name, python_name in JPA_BASE_METHOD_NAME_MAP.items():
        entry = java_index[synthetic_java_method_id(java_name)]
        assert entry == {
            "file_path": BASE_REPOSITORY_FILE,
            "class_name": BASE_REPOSITORY_CLASS,
            "function_name": python_name,
            "phase": 1,
        }
