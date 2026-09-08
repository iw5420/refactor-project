"""design_agent/layout.py 新增的 render_dataclass_section()（見 05a 三章
「孤兒類別／資料容器占位」格式慣例，跟既有 render_schema_section()／
render_class_placeholder_section() 平行的第三種渲染函式），以及 Phase
1／Phase 2 分階段翻譯設計新增的 utils package 判斷、層級判定、phase
計算、utils 檔案路徑（見 05a 三章「Utils 特例」、九章「phase 欄位」、
`refactor_plan.md` 二章）。
"""
from design_agent.layout import (
    file_path_for_utils,
    is_utils_package,
    layer_for_class,
    phase_for_layer,
    render_dataclass_section,
    render_schema_section,
)


def test_renders_header_and_import():
    output = render_dataclass_section(
        "app/schemas/user.py",
        [("UserDto", "UserDto.java", False, [("name", "str")], "偵測到 Lombok/JPA 資料標記")],
    )
    assert output.startswith("### app/schemas/user.py\n```python\nfrom dataclasses import dataclass\n")
    assert output.rstrip("\n").endswith("```")


def test_frozen_flag_selects_decorator():
    frozen_output = render_dataclass_section(
        "app/schemas/geo.py",
        [("Point", "Point.java", True, [("x", "int"), ("y", "int")], "note")],
    )
    assert "@dataclass(frozen=True)\nclass Point:" in frozen_output
    assert "    x: int" in frozen_output
    assert "    y: int" in frozen_output

    mutable_output = render_dataclass_section(
        "app/schemas/geo.py",
        [("Point", "Point.java", False, [("x", "int")], "note")],
    )
    assert "@dataclass\nclass Point:" in mutable_output
    assert "@dataclass(frozen=True)" not in mutable_output


def test_class_with_no_fields_renders_pass_body():
    output = render_dataclass_section(
        "app/schemas/misc.py",
        [("Empty", "Empty.java", False, [], "note")],
    )
    assert "class Empty:" in output
    assert "    pass" in output


def test_confidence_note_appears_in_comment():
    output = render_dataclass_section(
        "app/schemas/misc.py",
        [("Holder", "Holder.java", False, [("label", "str")], "無 Lombok 標記，依欄位宣告推斷")],
    )
    assert "無 Lombok 標記，依欄位宣告推斷" in output


def test_multiple_classes_each_get_their_own_block():
    output = render_dataclass_section(
        "app/schemas/user.py",
        [
            ("A", "A.java", False, [("x", "int")], "note-a"),
            ("B", "B.java", True, [("y", "str")], "note-b"),
        ],
    )
    assert "class A:" in output
    assert "class B:" in output
    assert output.index("class A:") < output.index("class B:")


# ── is_utils_package()／layer_for_class()／phase_for_layer()／
# file_path_for_utils()：Phase 1／Phase 2 分階段翻譯設計新增，見 05a
# 三章「Utils 特例」、九章「phase 欄位」──


def test_is_utils_package_matches_nested_utils_package():
    assert is_utils_package("com.teachLanguage.utils") is True


def test_is_utils_package_rejects_non_utils_package():
    assert is_utils_package("com.teachLanguage.service") is False


def test_is_utils_package_handles_bare_utils_package():
    assert is_utils_package("utils") is True


def test_is_utils_package_rejects_class_name_that_merely_contains_utils():
    # package 最後一段必須「等於」utils，不是子字串比對——
    # "com.example.utilslib" 不該被誤判成 utils package。
    assert is_utils_package("com.example.utilslib") is False


def test_is_utils_package_handles_none():
    assert is_utils_package(None) is False


def test_layer_for_class_utils_package_wins_over_stereotype():
    # 即使（理論上不該發生）同時有 stereotype，utils package 判斷優先。
    assert layer_for_class("Service", "com.example.utils") == "utils"


def test_layer_for_class_falls_back_to_stereotype_outside_utils_package():
    assert layer_for_class("Repository", "com.example.repository") == "repositories"


def test_layer_for_class_returns_none_when_neither_matches():
    assert layer_for_class(None, "com.example.misc") is None


def test_phase_for_layer_repositories_and_utils_are_phase_1():
    assert phase_for_layer("repositories") == 1
    assert phase_for_layer("utils") == 1


def test_phase_for_layer_services_and_routers_are_phase_2():
    assert phase_for_layer("services") == 2
    assert phase_for_layer("routers") == 2


def test_file_path_for_utils_converts_camel_case_class_name():
    assert file_path_for_utils("ValidationUtil") == "app/utils/validation_util.py"


class TestRenderSchemaSectionAnyImport:
    """對應 docs/refactor_bug_trace.md #41：`extract_schema_fields()`
    對 Java `Void` 泛型抹除這類「裸 object 且無其他資訊」的欄位會退回
    `Any` 型別——`render_schema_section()` 要比照既有 `Field` import
    視內容需要才加的慣例，補上 `from typing import Any`。"""

    def test_any_import_added_when_a_field_uses_any(self):
        output = render_schema_section(
            "app/schemas/grading.py",
            [("ResponseResultVoid", [("code", "int"), ("data", "Any | None = None")])],
        )
        assert "from typing import Any" in output
        assert "data: Any | None = None" in output

    def test_any_import_not_added_when_no_field_uses_any(self):
        output = render_schema_section(
            "app/schemas/exam.py",
            [("GetAllExamKindRs", [("kinds", "list[str]")])],
        )
        assert "from typing import Any" not in output

    def test_any_import_not_falsely_triggered_by_similar_substring(self):
        """`\\bAny\\b` 邊界比對，避免剛好含 "Any" 子字串的識別字誤判成
        需要 typing.Any（雖然目前的型別產生邏輯不會真的產出這種名稱，
        這裡鎖住比對方式本身的正確性）。"""
        output = render_schema_section(
            "app/schemas/exam.py",
            [("Foo", [("thing", "CompanyEntity")])],
        )
        assert "from typing import Any" not in output
