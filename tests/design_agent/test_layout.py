"""design_agent/layout.py 新增的 render_dataclass_section()（見 05a 三章
「孤兒類別／資料容器占位」格式慣例，跟既有 render_schema_section()／
render_class_placeholder_section() 平行的第三種渲染函式）。
"""
from design_agent.layout import render_dataclass_section


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
