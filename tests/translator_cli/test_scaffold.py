"""translator_cli/scaffold.py，對應 07a 四章。"""
import ast

import pytest

from translator_cli.exceptions import TranslatorCliAssemblyError
from translator_cli.scaffold import (
    _extract_code_blocks,
    _merge_schema_blocks,
    _normalize_type,
    _render_function_snippet,
    _resolve_imports,
    _scan_project_custom_types,
    build_files,
    insert_import_lines,
    resolve_body_imports,
    write_files,
)


def test_normalize_type_converts_angle_brackets():
    assert _normalize_type("ResponseResult<User>") == "ResponseResult[User]"
    assert _normalize_type("int") == "int"


def test_extract_code_blocks_finds_multiple_blocks():
    tree = (
        "app/\n  app/schemas/user.py\n\n"
        "### app/schemas/user.py\n```python\nfrom pydantic import BaseModel\n\n"
        "class UserCreateRequest(BaseModel):\n    name: str\n```\n\n"
        "### app/core/database.py\n```python\nDATABASE_URL = 1\n```\n"
    )
    blocks = _extract_code_blocks(tree)
    assert [path for path, _ in blocks] == ["app/schemas/user.py", "app/core/database.py"]


def test_merge_schema_blocks_dedupes_future_and_imports():
    blocks = [
        "from __future__ import annotations\n\nfrom pydantic import BaseModel\n\nclass A(BaseModel):\n    x: int\n",
        "from __future__ import annotations\n\nfrom pydantic import BaseModel, Field\n\nclass B(BaseModel):\n    y: str = Field(..., max_length=5)\n",
    ]
    merged = _merge_schema_blocks(blocks)
    assert merged.count("from __future__ import annotations") == 1
    assert merged.index("from __future__ import annotations") == 0
    assert "from pydantic import BaseModel, Field" in merged
    assert "class A(BaseModel):" in merged
    assert "class B(BaseModel):" in merged
    import ast

    ast.parse(merged)  # 合併後必須是合法 Python


def test_render_function_snippet_router_boundary_method():
    iface = {
        "file_path": "app/routers/user_router.py",
        "class_name": None,
        "function_name": "get_by_id",
        "params": [{"name": "id", "type": "int"}],
        "return_type": "UserResponse",
        "http_method": "GET",
        "route_path": "/api/v1/users/{id}",
    }
    snippet = _render_function_snippet(iface, is_class_method=False)
    assert snippet.startswith('@router.get("/api/v1/users/{id}")\n')
    assert "def get_by_id(id: int) -> UserResponse:" in snippet
    assert "    pass" in snippet


def test_render_function_snippet_class_method_gets_self():
    iface = {
        "file_path": "app/repositories/user_repository.py",
        "class_name": "UserRepository",
        "function_name": "get_by_id",
        "params": [{"name": "user_id", "type": "int"}],
        "return_type": "User | None",
    }
    snippet = _render_function_snippet(iface, is_class_method=True)
    assert "def get_by_id(self, user_id: int) -> User | None:" in snippet


def test_render_function_snippet_no_params_class_method():
    iface = {
        "file_path": "app/services/user_service.py",
        "class_name": "UserService",
        "function_name": "list_all",
        "params": [],
        "return_type": "list[User]",
    }
    snippet = _render_function_snippet(iface, is_class_method=True)
    assert "def list_all(self) -> list[User]:" in snippet


def test_resolve_imports_known_keyword():
    lines = _resolve_imports(["Session"], {})
    assert "from sqlalchemy.orm import Session" in lines


def test_resolve_imports_custom_type_via_ast():
    lines = _resolve_imports(["ResponseResult[User]"], {"User": "app/models/user.py", "ResponseResult": "app/services/common_service.py"})
    assert "from app.models.user import User" in lines
    assert "from app.services.common_service import ResponseResult" in lines


def test_resolve_imports_unknown_type_not_added():
    lines = _resolve_imports(["int"], {})
    assert lines == []


def test_resolve_imports_custom_type_containing_keyword_substring_not_falsely_matched():
    # 自訂型別名稱剛好包含已知關鍵字當子字串（Java DTO 常見命名如
    # LoginRequest／UserSession）不該誤觸發已知關鍵字表的無效 import——
    # 用單字邊界比對取代單純子字串比對（見 _keyword_hits() docstring）。
    lines = _resolve_imports(
        ["LoginRequest"], {"LoginRequest": "app/schemas/auth.py"}
    )
    assert "from fastapi import Request" not in lines
    assert "from app.schemas.auth import LoginRequest" in lines


def test_resolve_imports_callable_with_nested_brackets_still_matches():
    # Callable[" 保留純子字串比對（不加單字邊界）：多引數 functional
    # interface 常見的雙層中括號寫法，"[" 後面接的是另一個 "["（非文字
    # 字元），單字邊界比對會誤判成不匹配。
    lines = _resolve_imports(["Callable[[int, str], bool]"], {})
    assert "from typing import Callable" in lines


def test_merge_schema_blocks_handles_bare_import():
    # 正則版本會漏抓 `import uuid`，把它當成一般 body 文字重複塞進合併
    # 後的檔案；改用 AST 定位後應該正確去重、只在檔頭出現一次。
    blocks = [
        "import uuid\n\nclass A:\n    id: uuid.UUID\n",
        "import uuid\n\nclass B:\n    id: uuid.UUID\n",
    ]
    merged = _merge_schema_blocks(blocks)
    assert merged.count("import uuid") == 1
    assert merged.index("import uuid") == 0
    assert "class A:" in merged
    assert "class B:" in merged
    import ast

    ast.parse(merged)


def test_merge_schema_blocks_handles_multiline_import():
    # PEP 8 括號跨行 import，單行正則會完全失效；AST 逐行號範圍挖除後
    # 應該正確辨識整個陳述式並去重。
    blocks = [
        "from typing import (\n    List,\n    Optional,\n)\n\nclass A:\n    x: List[int]\n",
        "from typing import Optional\n\nclass B:\n    y: Optional[int]\n",
    ]
    merged = _merge_schema_blocks(blocks)
    assert merged.count("from typing import") == 1
    assert "List" in merged.splitlines()[merged.splitlines().index("from typing import List, Optional")]
    assert "class A:" in merged
    assert "class B:" in merged
    import ast

    ast.parse(merged)


def test_merge_schema_blocks_preserves_comments_in_body():
    # render_class_placeholder_section()（05a 三章）產出的機械註解是這個
    # 佔位機制存在的核心資訊，合併演算法不能把它們弄丟（不能整段丟進
    # ast.unparse() 重新序列化，那會把註解全部拿掉）。
    block = (
        "# AuthException（Java 原始碼：AuthException.java）\n"
        "# 建構子 1: AuthException(message: str)\n"
        "class AuthException:\n    pass\n"
    )
    merged = _merge_schema_blocks([block])
    assert "# AuthException（Java 原始碼：AuthException.java）" in merged
    assert "# 建構子 1: AuthException(message: str)" in merged


# 單一 block 語法有誤時，_merge_schema_blocks() 內部逐 block
# ast.parse() 會先炸，build_files() 的 try/except 必須涵蓋到這一步、
# 不能只包住合併後的最終驗證（見 build_files() 對應註解）——這個情境
# 由下方既有的 test_build_files_raises_on_broken_schema_pseudocode()
# 覆蓋，不重複另寫一個。


def _minimal_python_structure():
    directory_tree = (
        "app/\n  app/routers/user_router.py\n  app/repositories/user_repository.py\n"
        "  app/schemas/user.py\n  app/models/user.py\n\n"
        "### app/schemas/user.py\n```python\nfrom pydantic import BaseModel\n\n"
        "class UserResponse(BaseModel):\n    id: int\n    name: str\n```\n\n"
        "### app/core/database.py\n```python\nDATABASE_URL = 1\n```\n\n"
        "### app/main.py\n```python\nfrom fastapi import FastAPI\n\napp = FastAPI()\n```\n"
    )
    interfaces = [
        {
            "file_path": "app/routers/user_router.py",
            "class_name": None,
            "function_name": "get_by_id",
            "params": [{"name": "id", "type": "int"}],
            "return_type": "UserResponse",
            "http_method": "GET",
            "route_path": "/api/v1/users/{id}",
        },
        {
            "file_path": "app/repositories/user_repository.py",
            "class_name": "UserRepository",
            "function_name": "get_by_id",
            "params": [{"name": "user_id", "type": "int"}],
            "return_type": "User | None",
        },
    ]
    return {"directory_tree": directory_tree, "interfaces": interfaces}


def test_build_files_end_to_end_produces_valid_python(tmp_path):
    db_models = {"app/models/user.py": "class User:\n    id: int\n"}
    files, skipped_interfaces, skipped_db_models = build_files(_minimal_python_structure(), db_models)

    assert skipped_interfaces == []
    assert skipped_db_models == []
    assert set(files) == {
        "app/schemas/user.py",
        "app/core/database.py",
        "app/main.py",
        "app/models/user.py",
        "app/routers/user_router.py",
        "app/repositories/user_repository.py",
    }

    router_source = files["app/routers/user_router.py"]
    assert "router = APIRouter()" in router_source
    assert "def get_by_id(id: int) -> UserResponse:" in router_source
    # ③ 已產出的自訂 schema 型別需要被正確 import（四章「import 解析」來源一）
    assert "from app.schemas.user import UserResponse" in router_source

    repo_source = files["app/repositories/user_repository.py"]
    assert "class UserRepository:" in repo_source
    assert "def get_by_id(self, user_id: int) -> User | None:" in repo_source
    assert "from app.models.user import User" in repo_source  # 來源三：db_models


def test_build_files_isolates_invalid_interface(tmp_path):
    structure = _minimal_python_structure()
    # 注入一個型別字串仍殘留非法字元的介面（見 07a 四章「已知殘留限制」：萬用字元泛型）
    structure["interfaces"].append(
        {
            "file_path": "app/repositories/user_repository.py",
            "class_name": "UserRepository",
            "function_name": "find_matching",
            "params": [{"name": "spec", "type": "List<? extends Foo>"}],
            "return_type": "None",
        }
    )
    files, skipped_interfaces, _ = build_files(structure, {})
    assert len(skipped_interfaces) == 1
    assert skipped_interfaces[0]["function_name"] == "find_matching"
    # 其餘介面不受影響，正常產出
    assert "def get_by_id(self, user_id: int)" in files["app/repositories/user_repository.py"]


def test_build_files_records_skipped_interface_for_unknown_layer(tmp_path):
    # 按 05a／07a 契約，interfaces 的 file_path 只會落在 routers／
    # services／repositories 三層，理論上不會觸發——但這裡驗證萬一
    # 上游出現非預期 file_path，不會被靜默丟棄，而是記進
    # skipped_interfaces（比照語法錯誤等其餘失敗路徑的一貫處理風格）。
    structure = _minimal_python_structure()
    structure["interfaces"].append(
        {
            "file_path": "app/utils/helper.py",  # 不在三層目錄底下
            "class_name": None,
            "function_name": "unexpected",
            "params": [],
            "return_type": "None",
        }
    )
    files, skipped_interfaces, _ = build_files(structure, {})
    assert "app/utils/helper.py" not in files
    assert len(skipped_interfaces) == 1
    assert skipped_interfaces[0]["function_name"] == "unexpected"
    assert "unknown layer" in skipped_interfaces[0]["error"]
    # 其餘介面不受影響，正常產出
    assert "app/routers/user_router.py" in files


def test_build_files_raises_on_broken_schema_pseudocode():
    structure = _minimal_python_structure()
    structure["directory_tree"] += '\n### app/schemas/broken.py\n```python\nclass (:\n```\n'
    with pytest.raises(TranslatorCliAssemblyError):
        build_files(structure, {})


def test_write_files_creates_directories(tmp_path):
    write_files(str(tmp_path), {"app/routers/user_router.py": "x = 1\n"})
    assert (tmp_path / "app" / "routers" / "user_router.py").read_text(encoding="utf-8") == "x = 1\n"


def test_scan_project_custom_types_finds_classes_across_layers(tmp_path):
    (tmp_path / "app" / "repositories").mkdir(parents=True)
    (tmp_path / "app" / "schemas").mkdir(parents=True)
    (tmp_path / "app" / "repositories" / "user_repository.py").write_text(
        "class UserRepository:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "app" / "schemas" / "user.py").write_text(
        "class UserResponse:\n    pass\n", encoding="utf-8"
    )

    index = _scan_project_custom_types(str(tmp_path))
    assert index["UserRepository"] == "app/repositories/user_repository.py"
    assert index["UserResponse"] == "app/schemas/user.py"


def test_scan_project_custom_types_skips_unparseable_file(tmp_path):
    (tmp_path / "app" / "services").mkdir(parents=True)
    (tmp_path / "app" / "services" / "broken.py").write_text("class (:\n", encoding="utf-8")
    (tmp_path / "app" / "services" / "ok.py").write_text("class OkService:\n    pass\n", encoding="utf-8")

    index = _scan_project_custom_types(str(tmp_path))
    assert index == {"OkService": "app/services/ok.py"}


def test_scan_project_custom_types_missing_app_dir_returns_empty(tmp_path):
    assert _scan_project_custom_types(str(tmp_path)) == {}


def test_resolve_body_imports_finds_known_keyword_and_custom_type(tmp_path):
    # 對應真實案例：本體引用了框架例外（HTTPException，簽名沒有）與
    # 跨檔案自訂類別（UserRepository，也不在簽名裡）。
    (tmp_path / "app" / "repositories").mkdir(parents=True)
    (tmp_path / "app" / "repositories" / "user_repository.py").write_text(
        "class UserRepository:\n    pass\n", encoding="utf-8"
    )

    tree = ast.parse("from __future__ import annotations\n\nrouter = None\n")
    body = ast.parse(
        "user = UserRepository(db).get_by_id(user_id)\n"
        "if user is None:\n"
        "    raise HTTPException(status_code=404)\n"
        "return user\n"
    ).body

    lines = resolve_body_imports(str(tmp_path), tree, body, bound_names={"db", "user_id"})
    assert "from fastapi import HTTPException" in lines
    assert "from app.repositories.user_repository import UserRepository" in lines


def test_resolve_body_imports_excludes_bound_names_and_builtins(tmp_path):
    tree = ast.parse("from __future__ import annotations\n")
    body = ast.parse("user_id = str(user_id)\nreturn len(user_id)\n").body

    lines = resolve_body_imports(str(tmp_path), tree, body, bound_names={"user_id"})
    assert lines == []  # str／len 是 builtin，user_id 是已綁定參數，都不該被當成缺 import


def test_resolve_body_imports_excludes_local_assignment_target(tmp_path):
    # HTTPException 在本體裡被拿來當區域變數名稱賦值（極端案例）——
    # 不該被誤判成需要 import，因為它已經是這個作用域裡的區域名稱。
    tree = ast.parse("from __future__ import annotations\n")
    body = ast.parse("HTTPException = 1\nreturn HTTPException\n").body

    lines = resolve_body_imports(str(tmp_path), tree, body, bound_names=set())
    assert lines == []


def test_resolve_body_imports_skips_already_imported_name(tmp_path):
    tree = ast.parse("from __future__ import annotations\n\nfrom fastapi import HTTPException\n")
    body = ast.parse("raise HTTPException(status_code=404)\n").body

    lines = resolve_body_imports(str(tmp_path), tree, body, bound_names=set())
    assert lines == []  # 已經在檔案頂層 import 過，不重複加


def test_resolve_body_imports_unresolvable_name_not_guessed(tmp_path):
    # 兩層都比對不到時保守不加、不猜（對齊 _resolve_imports() 的既有
    # 原則）——SomeCompletelyUnknownThing 不在關鍵字表也不在專案裡。
    tree = ast.parse("from __future__ import annotations\n")
    body = ast.parse("return SomeCompletelyUnknownThing()\n").body

    lines = resolve_body_imports(str(tmp_path), tree, body, bound_names=set())
    assert lines == []


def test_insert_import_lines_after_existing_imports_and_docstring():
    tree = ast.parse(
        '"""module docstring"""\n'
        "from __future__ import annotations\n\n"
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n"
    )
    insert_import_lines(tree, ["from fastapi import HTTPException"])
    rendered = ast.unparse(tree)
    lines = rendered.splitlines()
    # 新 import 應該接在既有 import 區塊之後、router = APIRouter() 之前
    assert "from fastapi import HTTPException" in lines
    router_idx = next(i for i, l in enumerate(lines) if "APIRouter()" in l)
    import_idx = next(i for i, l in enumerate(lines) if l == "from fastapi import HTTPException")
    assert import_idx < router_idx


def test_insert_import_lines_empty_list_is_noop():
    tree = ast.parse("x = 1\n")
    original = ast.dump(tree)
    insert_import_lines(tree, [])
    assert ast.dump(tree) == original
