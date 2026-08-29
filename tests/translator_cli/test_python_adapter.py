"""translator_cli/python_adapter.py，對應 07a 六、十章。"""
import ast

import pytest

from translator_cli.exceptions import TranslatorCliModelOutputError
from translator_cli.python_adapter import (
    PythonAdapter,
    extract_body_statements,
    extract_referenced_classes,
    extract_specific_functions,
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


def test_extract_body_statements_duplicate_signature_mixed_with_other_statements_raises():
    # 對應 docs/09b_bug_trace.md #52 真實案例：⑦ Debug Agent 的 fixed_body
    # 夾帶了 import／裝飾器等其他陳述式，同名巢狀函式只是其中一筆，不是
    # body 唯一的陳述式——舊版 len(body)==1 判斷攔不住這種情況，會被
    # splice_body() 當成合法陳述式插進去，寫出巢狀死程式碼。
    body = (
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "@router.post('/api/file/image')\n"
        "async def image(kind: str) -> None:\n"
        "    return None\n"
    )
    with pytest.raises(TranslatorCliModelOutputError, match="模型重複輸出函式簽名"):
        extract_body_statements(body, "image")


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


class TestExtractSpecificFunctions:
    """對應 06a 七章新設計「referenced_interfaces 函式層級抽取」，見
    docs/09b_bug_trace.md #37 根因。"""

    _SOURCE = (
        "from __future__ import annotations\n\n"
        "from sqlalchemy.orm import Session\n\n\n"
        "class ExamRepository:\n"
        "    def find_by_kind(self, kind: str, db: Session) -> object:\n"
        "        pass\n"
        "    def find_by_card(self, card: str, db: Session) -> object | None:\n"
        "        return db.query(1).filter(2).one_or_none()\n\n\n"
        "class ExamSpecification:\n"
        "    def with_year(self, year: str) -> object:\n"
        "        return Specification(lambda x: x.year == year)\n\n\n"
        "def standalone_helper() -> int:\n"
        "    return 42\n"
    )

    def test_keeps_only_the_targeted_method_full_body(self):
        result = extract_specific_functions(self._SOURCE, [("ExamRepository", "find_by_card")])
        assert "def find_by_card(self, card: str, db: Session) -> object | None:" in result
        assert "return db.query(1).filter(2).one_or_none()" in result

    def test_drops_untargeted_sibling_method_entirely(self):
        result = extract_specific_functions(self._SOURCE, [("ExamRepository", "find_by_card")])
        assert "find_by_kind" not in result

    def test_drops_untargeted_class_entirely(self):
        result = extract_specific_functions(self._SOURCE, [("ExamRepository", "find_by_card")])
        assert "ExamSpecification" not in result
        assert "with_year" not in result

    def test_drops_untargeted_standalone_function(self):
        result = extract_specific_functions(self._SOURCE, [("ExamRepository", "find_by_card")])
        assert "standalone_helper" not in result

    def test_keeps_module_level_imports(self):
        result = extract_specific_functions(self._SOURCE, [("ExamRepository", "find_by_card")])
        assert "from __future__ import annotations" in result
        assert "from sqlalchemy.orm import Session" in result

    def test_supports_multiple_targets_across_different_classes(self):
        result = extract_specific_functions(
            self._SOURCE, [("ExamRepository", "find_by_card"), ("ExamSpecification", "with_year")]
        )
        assert "find_by_card" in result
        assert "with_year" in result
        assert "find_by_kind" not in result

    def test_supports_standalone_function_target(self):
        result = extract_specific_functions(self._SOURCE, [(None, "standalone_helper")])
        assert "return 42" in result
        assert "ExamRepository" not in result
        assert "ExamSpecification" not in result

    def test_no_targets_drops_all_functions_and_classes(self):
        result = extract_specific_functions(self._SOURCE, [])
        assert "class" not in result
        assert "def" not in result
        assert "from __future__ import annotations" in result  # import 維持不變

    def test_missing_target_is_silently_absent_not_an_error(self):
        # 對應 docstring：找不到的組合不視為錯誤，單純不出現在結果裡，
        # 由呼叫端既有的 context_files 讀取容錯機制處理。
        result = extract_specific_functions(self._SOURCE, [("NoSuchClass", "no_such_method")])
        assert "ExamRepository" not in result

    def test_output_is_valid_python(self):
        result = extract_specific_functions(self._SOURCE, [("ExamRepository", "find_by_card")])
        ast.parse(result)  # 不應拋出

    def test_invalid_source_raises_syntax_error(self):
        with pytest.raises(SyntaxError):
            extract_specific_functions("def (:\n", [(None, "x")])

    def test_significantly_smaller_than_full_bloated_file(self):
        # 對應真實案例（09b_bug_trace.md #37）：只抽一個方法，應該遠小於
        # strip_all_function_bodies() 那種「保留所有簽名」的裁減方式。
        big_source = "class ExamSpecification:\n" + "\n".join(
            f"    def with_{name}(self, {name}: str) -> object:\n"
            f"        validator = ValidationUtil()\n"
            f"        if validator.is_valid_field({name}):\n"
            f"            return Specification(lambda x: x.{name} == {name})\n"
            f"        else:\n"
            f"            return conjunction()\n"
            for name in ("year", "grade", "classes", "kind", "status", "card", "random_id")
        )
        result = extract_specific_functions(big_source, [("ExamSpecification", "with_year")])
        assert len(result.encode("utf-8")) < len(big_source.encode("utf-8")) * 0.25
        assert "with_grade" not in result


class TestExtractReferencedClasses:
    """對應 docs/09b_bug_trace.md #45：schemas／models 這類純欄位宣告
    檔案沒有函式本體可以剝，strip_all_function_bodies() 對這類檔案是
    空操作，改用這個函式依 class 名稱過濾。"""

    _SOURCE = (
        "from __future__ import annotations\n\n"
        "from pydantic import BaseModel\n\n\n"
        "class ResponseResultCreaterandomRs(BaseModel):\n"
        "    code: int | None\n"
        "    msg: str | None\n"
        "    data: CreaterandomRs | None\n\n\n"
        "class CreaterandomRs(BaseModel):\n"
        "    randomId: str | None\n"
        "    result: str | None\n\n\n"
        "class GetExamRq(BaseModel):\n"
        "    year: str | None\n\n\n"
        "class ResponseResultGetExamRs(BaseModel):\n"
        "    code: int | None\n"
        "    data: GetExamRs | None\n\n\n"
        "class GetExamRs(BaseModel):\n"
        "    exam: list[str] | None\n"
    )

    def test_keeps_only_seeded_class(self):
        result = extract_referenced_classes(self._SOURCE, {"GetExamRq"})
        assert "class GetExamRq" in result

    def test_drops_unrelated_class_entirely(self):
        result = extract_referenced_classes(self._SOURCE, {"GetExamRq"})
        assert "CreaterandomRs" not in result
        assert "ResponseResultGetExamRs" not in result

    def test_transitive_closure_follows_field_type_reference(self):
        """種子集合只給 ResponseResultCreaterandomRs，但它的 data 欄位型別
        標註指到 CreaterandomRs——這個真實案例（task_045）就是靠這一層
        遞迴閉包才不會漏掉 data 欄位實際指向的類別，光看函式簽名／
        description 通常抓不到這種只出現在欄位型別標註裡的名稱。"""
        result = extract_referenced_classes(self._SOURCE, {"ResponseResultCreaterandomRs"})
        assert "class ResponseResultCreaterandomRs" in result
        assert "class CreaterandomRs" in result
        assert "GetExamRq" not in result
        assert "GetExamRs" not in result

    def test_empty_seed_names_returns_source_unchanged(self):
        result = extract_referenced_classes(self._SOURCE, set())
        assert result == self._SOURCE

    def test_seed_names_with_no_intersection_returns_source_unchanged(self):
        # 種子集合完全命中不到這個檔案定義的任何 class——代表呼叫端的
        # 文字比對機制沒抓到線索，不是「這個檔案真的用不到任何東西」，
        # 寧可不裁也不要錯砍掉模型可能需要的資訊。
        result = extract_referenced_classes(self._SOURCE, {"SomethingElseEntirely"})
        assert result == self._SOURCE

    def test_keeps_module_level_imports(self):
        result = extract_referenced_classes(self._SOURCE, {"GetExamRq"})
        assert "from __future__ import annotations" in result
        assert "from pydantic import BaseModel" in result

    def test_output_is_valid_python(self):
        result = extract_referenced_classes(self._SOURCE, {"ResponseResultCreaterandomRs"})
        ast.parse(result)  # 不應拋出
