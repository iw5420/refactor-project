"""translator_cli/python_adapter.py，對應 07a 六、十章。"""
import ast

import pytest

from translator_cli.exceptions import TranslatorCliModelOutputError
from translator_cli.python_adapter import (
    PythonAdapter,
    extract_body_statements,
    strip_all_function_bodies,
)


def test_locate_function_free_function():
    adapter = PythonAdapter()
    tree = adapter.parse("def get_by_id(user_id: int) -> int:\n    pass\n")
    node = adapter.locate_function(tree, None, "get_by_id")
    assert isinstance(node, ast.FunctionDef)
    assert node.name == "get_by_id"


def test_locate_function_class_method():
    adapter = PythonAdapter()
    tree = adapter.parse("class UserRepository:\n    def get_by_id(self, user_id: int) -> int:\n        pass\n")
    node = adapter.locate_function(tree, "UserRepository", "get_by_id")
    assert node is not None
    assert node.name == "get_by_id"


def test_locate_function_missing_class_returns_none():
    adapter = PythonAdapter()
    tree = adapter.parse("class UserRepository:\n    def get_by_id(self, user_id: int) -> int:\n        pass\n")
    assert adapter.locate_function(tree, "OrderRepository", "get_by_id") is None


def test_locate_function_missing_method_returns_none():
    adapter = PythonAdapter()
    tree = adapter.parse("class UserRepository:\n    def get_by_id(self, user_id: int) -> int:\n        pass\n")
    assert adapter.locate_function(tree, "UserRepository", "delete") is None


def test_render_signature_excludes_body():
    adapter = PythonAdapter()
    tree = adapter.parse("def get_by_id(user_id: int) -> int:\n    return 1\n")
    node = adapter.locate_function(tree, None, "get_by_id")
    signature = adapter.render_signature(node)
    assert signature == "def get_by_id(user_id: int) -> int:"


def test_render_signature_keeps_decorator():
    adapter = PythonAdapter()
    tree = adapter.parse('@router.get("/users/{id}")\ndef get_by_id(id: int) -> int:\n    pass\n')
    node = adapter.locate_function(tree, None, "get_by_id")
    signature = adapter.render_signature(node)
    assert signature.splitlines()[0] == '@router.get(\'/users/{id}\')'
    assert "pass" not in signature


def test_extract_body_statements_normal_case():
    stmts = extract_body_statements("x = 1\nreturn x\n", "get_by_id")
    assert len(stmts) == 2


def test_extract_body_statements_syntax_error_raises():
    with pytest.raises(TranslatorCliModelOutputError, match="ast.parse"):
        extract_body_statements("def (:\n", "get_by_id")


def test_extract_body_statements_empty_raises():
    with pytest.raises(TranslatorCliModelOutputError, match="陳述式清單為空"):
        extract_body_statements("\n   \n", "get_by_id")


def test_extract_body_statements_duplicate_signature_raises():
    body = "def get_by_id(user_id: int) -> int:\n    return user_id\n"
    with pytest.raises(TranslatorCliModelOutputError, match="模型重複輸出函式簽名"):
        extract_body_statements(body, "get_by_id")


def test_extract_body_statements_nested_function_with_different_name_is_allowed():
    # 巢狀函式但名稱跟目標函式不同，不觸發 6b（模型可能合理地在本體內定義 helper closure）
    body = "def _helper(x):\n    return x\nreturn _helper(1)\n"
    stmts = extract_body_statements(body, "get_by_id")
    assert len(stmts) == 2


def test_splice_body_and_render_roundtrip():
    adapter = PythonAdapter()
    tree = adapter.parse("def get_by_id(user_id: int) -> int:\n    pass\n")
    node = adapter.locate_function(tree, None, "get_by_id")
    adapter.splice_body(node, "return user_id * 2\n")
    rendered = adapter.render(tree)
    assert "return user_id * 2" in rendered
    adapter.validate_syntax(rendered)  # 不應拋出


def test_splice_body_empty_body_raises_and_does_not_mutate():
    adapter = PythonAdapter()
    tree = adapter.parse("def get_by_id(user_id: int) -> int:\n    pass\n")
    node = adapter.locate_function(tree, None, "get_by_id")
    original_body = node.body
    with pytest.raises(TranslatorCliModelOutputError):
        adapter.splice_body(node, "\n")
    assert node.body is original_body


def test_validate_syntax_raises_syntax_error():
    adapter = PythonAdapter()
    with pytest.raises(SyntaxError):
        adapter.validate_syntax("def (:\n")


class TestStripAllFunctionBodies:
    """對應 09b_bug_trace.md #37：context_files 過大時的裁減機制。"""

    def test_strips_free_function_body(self):
        source = "def add(a: int, b: int) -> int:\n    result = a + b\n    return result\n"
        trimmed = strip_all_function_bodies(source)
        tree = ast.parse(trimmed)
        assert len(tree.body) == 1
        assert isinstance(tree.body[0], ast.FunctionDef)
        assert [type(s) for s in tree.body[0].body] == [ast.Pass]

    def test_strips_class_method_bodies_keeps_signatures(self):
        source = (
            "class Foo:\n"
            "    def bar(self, x: int) -> int:\n"
            "        y = x * 2\n"
            "        return y\n"
            "    def baz(self) -> None:\n"
            "        pass\n"
        )
        trimmed = strip_all_function_bodies(source)
        assert "y = x * 2" not in trimmed
        assert "def bar(self, x: int) -> int:" in trimmed
        assert "def baz(self) -> None:" in trimmed

    def test_keeps_imports_and_class_level_attributes(self):
        # SQLAlchemy Column／Pydantic 欄位宣告是資料形狀，不是函式本體，
        # 裁減不該動到——模型仍需要知道有哪些欄位可用。
        source = (
            "from __future__ import annotations\n\n"
            "from pydantic import BaseModel\n\n\n"
            "class UserRs(BaseModel):\n"
            "    name: str | None\n"
            "    age: int | None\n\n"
            "    def to_dict(self) -> dict:\n"
            "        return {'name': self.name, 'age': self.age}\n"
        )
        trimmed = strip_all_function_bodies(source)
        assert "from pydantic import BaseModel" in trimmed
        assert "name: str | None" in trimmed
        assert "age: int | None" in trimmed
        assert "return {'name': self.name, 'age': self.age}" not in trimmed

    def test_keeps_decorators(self):
        source = "class Foo:\n    @staticmethod\n    def bar() -> int:\n        return 42\n"
        trimmed = strip_all_function_bodies(source)
        assert "@staticmethod" in trimmed
        assert "return 42" not in trimmed

    def test_strips_nested_function_bodies(self):
        source = (
            "def outer() -> int:\n"
            "    def inner() -> int:\n"
            "        return 1\n"
            "    return inner()\n"
        )
        trimmed = strip_all_function_bodies(source)
        # outer 本體被整個換成 pass，連帶巢狀的 inner 定義一起消失。
        assert "def inner" not in trimmed
        assert "return inner()" not in trimmed

    def test_async_function_bodies_also_stripped(self):
        source = "async def fetch() -> str:\n    data = await call()\n    return data\n"
        trimmed = strip_all_function_bodies(source)
        assert "data = await call()" not in trimmed
        assert "async def fetch() -> str:" in trimmed

    def test_output_is_valid_python(self):
        source = (
            "class Repo:\n"
            "    def find_by_id(self, id: int) -> object | None:\n"
            "        return db.query(Entity).filter(Entity.id == id).one_or_none()\n"
            "    def find_all(self) -> list[object]:\n"
            "        return db.query(Entity).all()\n"
        )
        trimmed = strip_all_function_bodies(source)
        ast.parse(trimmed)  # 不應拋出

    def test_invalid_source_raises_syntax_error(self):
        with pytest.raises(SyntaxError):
            strip_all_function_bodies("def (:\n")

    def test_empty_module_returns_empty(self):
        assert strip_all_function_bodies("").strip() == ""

    def test_significantly_reduces_size_of_realistic_bloated_file(self):
        # 對應真實案例（09b_bug_trace.md #37）：同一個檔案裡有多支已經
        # 填完真實邏輯的姊妹函式，裁減後應該明顯變小。
        source = "\n".join(
            f"    def with_{name}(self, {name}: str) -> object:\n"
            f"        validator = ValidationUtil()\n"
            f"        if validator.is_valid_field({name}):\n"
            f"            return Specification(lambda x: x.{name} == {name})\n"
            f"        else:\n"
            f"            return conjunction()\n"
            for name in ("year", "grade", "classes", "kind", "status")
        )
        source = "class ExamSpecification:\n" + source
        trimmed = strip_all_function_bodies(source)
        assert len(trimmed.encode("utf-8")) < len(source.encode("utf-8")) * 0.6
