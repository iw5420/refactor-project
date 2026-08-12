"""translator_cli/python_adapter.py，對應 07a 六、十章。"""
import ast

import pytest

from translator_cli.exceptions import TranslatorCliModelOutputError
from translator_cli.python_adapter import PythonAdapter, extract_body_statements


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
