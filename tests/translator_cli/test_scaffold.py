"""translator_cli/scaffold.py，對應 07a 四章。"""
import ast

import pytest

from translator_cli.exceptions import TranslatorCliAssemblyError
from translator_cli.scaffold import (
    _build_custom_type_index,
    _extract_code_blocks,
    _merge_schema_blocks,
    _module_for_file_path,
    _normalize_type,
    _render_function_snippet,
    _resolve_imports,
    _scan_project_custom_types,
    _table_assignment_names,
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


def test_resolve_imports_response_keyword():
    # ResponseEntity<T> 型別對應（common/java_type_mapping.py）與全域
    # 例外處理（design_agent/design.py _design_global_advice_module()）
    # 都用 "Response" 當 return_type，見 docs/09b_bug_trace.md #11/#12/#30。
    lines = _resolve_imports(["Response"], {})
    assert "from fastapi import Response" in lines


def test_resolve_imports_column_element_keyword():
    # Specification<T> 型別對應（common/java_type_mapping.py）用
    # "ColumnElement | None" 當 return_type，見
    # docs/refactor_bug_trace.md #14。
    lines = _resolve_imports(["ColumnElement | None"], {})
    assert "from sqlalchemy.sql.elements import ColumnElement" in lines


def test_resolve_imports_form_keyword():
    # 對應 docs/refactor_bug_trace.md #20：MultipartFile 端點的其餘
    # @RequestParam 欄位型別字串帶 "= Form(...)"，見
    # design_agent/type_mapping.py::_classify_params() 新增分支。
    lines = _resolve_imports(["str = Form(...)"], {})
    assert "from fastapi import Form" in lines


def test_resolve_imports_custom_type_via_ast():
    lines = _resolve_imports(["ResponseResult[User]"], {"User": "app/models/user.py", "ResponseResult": "app/services/common_service.py"})
    assert "from app.models.user import User" in lines
    assert "from app.services.common_service import ResponseResult" in lines


def test_resolve_imports_unknown_type_not_added():
    lines = _resolve_imports(["int"], {})
    assert lines == []


def test_resolve_imports_excludes_same_file_class_names():
    # 對應真實案例：exam-platform-api 的 app/services/common_service.py，
    # ResponseResult.error_4(self, code: ErrorCode) 的簽名引用 ErrorCode——
    # ErrorCode 是同一個檔案稍後才定義的另一個 class，不是跨檔案自訂型別。
    # custom_type_index 涵蓋全專案（本來就會收錄這個檔案自己的 class），
    # 若不排除 same_file_class_names，會產生一行恆為 True 的循環 import。
    custom_type_index = {
        "ErrorCode": "app/services/common_service.py",
        "ResponseResult": "app/services/common_service.py",
        "User": "app/models/user.py",
    }
    lines = _resolve_imports(
        ["ErrorCode", "ResponseResult[User]"],
        custom_type_index,
        same_file_class_names=frozenset({"ErrorCode", "ResponseResult"}),
    )
    assert lines == ["from app.models.user import User"]


def test_module_for_file_path_extracts_module_for_all_three_layers():
    assert _module_for_file_path("app/routers/file_router.py") == "file"
    assert _module_for_file_path("app/services/exam_service.py") == "exam"
    assert _module_for_file_path("app/repositories/user_repository.py") == "user"


def test_module_for_file_path_returns_none_for_utils_and_global_advice():
    # utils 橫跨多個 module（見 file_path_for_utils()），_global 保留
    # 模組是固定單一檔案——兩者都沒有 module 概念。
    assert _module_for_file_path("app/utils/validation_util.py") is None
    assert _module_for_file_path("app/core/exception_handlers.py") is None


def test_resolve_imports_prefers_own_module_schema_over_global_index():
    # 對應 docs/refactor_bug_trace.md #8：同名的具名回應包裝 class（如
    # ResponseResultString）被兩個模組各自獨立定義一份時，全域
    # custom_type_index 只留得住其中一個（這裡故意讓它指向 exam，模擬
    # 覆寫後的結果）——own_module="file" 且 file 自己的 schema 檔案也
    # 定義了同名 class 時，該優先用 file 自己的，不查全域索引。
    custom_type_index = {"ResponseResultString": "app/schemas/exam.py"}
    schema_classes_by_module = {
        "exam": frozenset({"ResponseResultString"}),
        "file": frozenset({"ResponseResultString"}),
    }
    lines = _resolve_imports(
        ["ResponseResultString"],
        custom_type_index,
        schema_classes_by_module=schema_classes_by_module,
        own_module="file",
    )
    assert lines == ["from app.schemas.file import ResponseResultString"]


def test_resolve_imports_falls_back_to_global_index_when_own_module_lacks_the_class():
    # own_module 存在、但自己的模組沒有定義這個 class 名稱時，退回既有
    # 的全域索引行為——不因為新增 own_module 判斷就影響既有的跨模組
    # 引用案例。
    custom_type_index = {"SharedDto": "app/schemas/common.py"}
    lines = _resolve_imports(
        ["SharedDto"],
        custom_type_index,
        schema_classes_by_module={"file": frozenset({"ResponseResultString"})},
        own_module="file",
    )
    assert lines == ["from app.schemas.common import SharedDto"]


def test_resolve_imports_repository_layer_ignores_own_module_schema_priority():
    # 對應 docs/refactor_bug_trace.md #17 真實案例：exam 模組的
    # ExamkindEntity 同時被 app/schemas/exam.py（Pydantic DTO）跟
    # app/models/exam.py（SQLAlchemy ORM）定義——own_module="exam" 且
    # exam 自己的 schema 檔案剛好也定義了同名 class 時，#8 的既有優先序
    # 會讓 repositories 層的檔案錯誤選到 schema，把 Pydantic BaseModel
    # 子類別傳給 SQLAlchemy 查詢。is_repository_layer=True 時要完全跳過
    # 這條「優先查同模組 schema」的規則，改用全域 custom_type_index——
    # 這裡模擬 _build_custom_type_index() 的既有寫入順序（db_models 最後
    # 寫入覆蓋掉同名的 schema 條目），custom_type_index 已經是「同名時
    # model 贏」。
    custom_type_index = {"ExamkindEntity": "app/models/exam.py"}
    schema_classes_by_module = {"exam": frozenset({"ExamkindEntity"})}
    lines = _resolve_imports(
        ["ExamkindEntity"],
        custom_type_index,
        schema_classes_by_module=schema_classes_by_module,
        own_module="exam",
        is_repository_layer=True,
    )
    assert lines == ["from app.models.exam import ExamkindEntity"]


def test_resolve_imports_non_repository_layer_still_prefers_own_module_schema():
    # 對照組：is_repository_layer 預設 False，其餘層級（routers／
    # services／utils）不受這次修正影響，#8 的既有行為維持不變。
    custom_type_index = {"ExamkindEntity": "app/models/exam.py"}
    schema_classes_by_module = {"exam": frozenset({"ExamkindEntity"})}
    lines = _resolve_imports(
        ["ExamkindEntity"],
        custom_type_index,
        schema_classes_by_module=schema_classes_by_module,
        own_module="exam",
    )
    assert lines == ["from app.schemas.exam import ExamkindEntity"]


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


def test_resolve_imports_uuid_type():
    # 08a_scaffold_agent_architecture.md 五章新增：common/java_type_mapping.py
    # 的 map_java_type() 補上 java.util.UUID 對應後，③的方法簽名也可能
    # 產生 "UUID" 這個型別字串，需要對應的 import 規則。
    lines = _resolve_imports(["UUID"], {})
    assert "from uuid import UUID" in lines


def test_resolve_imports_date_time_types_resolve_independently():
    # date／datetime／time 三者都以英數字元結尾，走單字邊界比對——
    # "datetime" 內含 "date"／"time" 兩個子字串，但邊界比對不應該讓
    # "datetime" 這個型別字串誤觸發 "date" 或 "time" 各自獨立的 import。
    assert _resolve_imports(["date"], {}) == ["from datetime import date"]
    assert _resolve_imports(["time"], {}) == ["from datetime import time"]
    lines = _resolve_imports(["datetime"], {})
    assert lines == ["from datetime import datetime"]
    assert "from datetime import date" not in lines
    assert "from datetime import time" not in lines


def test_resolve_imports_date_and_datetime_together_both_present():
    # 同一個檔案同時出現 date 型別欄位與 datetime 型別欄位是常見情況
    # （如 birth_date: date、created_at: datetime），兩者都要各自加上
    # 對應 import，不能因為 "date" 是 "datetime" 的子字串就漏掉一個。
    lines = _resolve_imports(["date", "datetime"], {})
    assert "from datetime import date" in lines
    assert "from datetime import datetime" in lines


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


def test_build_files_same_file_class_signature_reference_not_self_imported():
    # 對應真實案例：exam-platform-api 的 app/services/common_service.py，
    # 同一個檔案兩個 class，其中一個 class 的方法簽名引用另一個同檔案
    # 稍後才定義的 class（如 ResponseResult.error_4(self, code: ErrorCode)）。
    # 骨架生成階段若不排除同檔案自己的 class，會產生一行恆為 True 的
    # 循環 import，讓這個模組 import 階段直接 ImportError。
    structure = {
        "directory_tree": "app/\n  app/services/common_service.py\n",
        "interfaces": [
            {
                "file_path": "app/services/common_service.py",
                "class_name": "ResponseResult",
                "function_name": "error_4",
                "params": [{"name": "code", "type": "ErrorCode"}],
                "return_type": "ResponseResult",
            },
            {
                "file_path": "app/services/common_service.py",
                "class_name": "ErrorCode",
                "function_name": "value",
                "params": [],
                "return_type": "int",
            },
        ],
    }
    files, skipped_interfaces, _ = build_files(structure, {})

    assert skipped_interfaces == []
    source = files["app/services/common_service.py"]
    assert "import" not in "\n".join(
        line for line in source.splitlines() if "common_service" in line
    )
    assert "class ResponseResult:" in source
    assert "class ErrorCode:" in source


def test_build_files_same_named_schema_class_in_two_modules_each_import_their_own(tmp_path):
    # 對應 docs/refactor_bug_trace.md #8 真實案例：exam-platform-api 的
    # ResponseResultString 同時被 exam／file 兩個模組各自獨立定義一份
    # （③ 逐模組產生 schema，模組之間不知道彼此定義了同名的東西）。
    # 修好之前，_build_custom_type_index() 的全域扁平索引會被其中一個
    # 模組覆寫，導致另一個模組的 router 明明自己也定義了同名 class，
    # import 卻指向了別的模組（真實案例：file_router.py 匯入
    # `from app.schemas.exam import ResponseResultString`）。
    structure = {
        "directory_tree": (
            "app/\n  app/routers/exam_router.py\n  app/routers/file_router.py\n"
            "  app/schemas/exam.py\n  app/schemas/file.py\n\n"
            "### app/schemas/exam.py\n```python\nfrom pydantic import BaseModel\n\n"
            "class ResponseResultString(BaseModel):\n    code: int\n    data: str\n```\n\n"
            "### app/schemas/file.py\n```python\nfrom pydantic import BaseModel\n\n"
            "class ResponseResultString(BaseModel):\n    code: int\n    data: str\n```\n"
        ),
        "interfaces": [
            {
                "file_path": "app/routers/exam_router.py",
                "class_name": None,
                "function_name": "search",
                "params": [],
                "return_type": "ResponseResultString",
                "http_method": "GET",
                "route_path": "/api/exam/search",
            },
            {
                "file_path": "app/routers/file_router.py",
                "class_name": None,
                "function_name": "voice",
                "params": [],
                "return_type": "ResponseResultString",
                "http_method": "POST",
                "route_path": "/api/file/voice",
            },
        ],
    }
    files, skipped_interfaces, _ = build_files(structure, {})

    assert skipped_interfaces == []
    assert "from app.schemas.exam import ResponseResultString" in files["app/routers/exam_router.py"]
    assert "from app.schemas.file import ResponseResultString" in files["app/routers/file_router.py"]
    # 兩邊都不該指向對方的模組。
    assert "app.schemas.file" not in files["app/routers/exam_router.py"]
    assert "app.schemas.exam" not in files["app/routers/file_router.py"]


def test_build_files_repository_layer_prefers_model_over_same_named_schema_class():
    # 對應 docs/refactor_bug_trace.md #17 真實案例：exam 模組的
    # ExamkindEntity 同時被 app/schemas/exam.py（Pydantic DTO，openapi
    # 展開產出）跟 app/models/exam.py（SQLAlchemy ORM，④ 掃 entity 產出）
    # 定義一份——修好之前，repositories 層的檔案會被 #8 的既有優先序
    # 誤導成優先選 schema，導致 `db.query(ExamkindEntity)...` 在真正查詢
    # 時把 Pydantic BaseModel 子類別傳給 SQLAlchemy，丟出
    # sqlalchemy.exc.ArgumentError（本體程式碼本身完全正確，純粹被錯誤
    # 的 import 拖累）。
    structure = {
        "directory_tree": (
            "app/\n  app/repositories/exam_repository.py\n  app/schemas/exam.py\n"
            "  app/models/exam.py\n\n"
            "### app/schemas/exam.py\n```python\nfrom pydantic import BaseModel\n\n"
            "class ExamkindEntity(BaseModel):\n    kind: str\n```\n"
        ),
        "interfaces": [
            {
                "file_path": "app/repositories/exam_repository.py",
                "class_name": "ExamkindRepository",
                "function_name": "find_by_kind",
                "params": [{"name": "kind", "type": "str"}, {"name": "db", "type": "Session"}],
                "return_type": "list[ExamkindEntity]",
            },
        ],
    }
    db_models = {
        "app/models/exam.py": "class ExamkindEntity:\n    kind: str\n",
    }
    files, skipped_interfaces, _ = build_files(structure, db_models)

    assert skipped_interfaces == []
    source = files["app/repositories/exam_repository.py"]
    assert "from app.models.exam import ExamkindEntity" in source
    assert "app.schemas.exam" not in source


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
    # services／repositories／utils 目錄底下，理論上不會觸發——但這裡
    # 驗證萬一上游出現非預期 file_path，不會被靜默丟棄，而是記進
    # skipped_interfaces（比照語法錯誤等其餘失敗路徑的一貫處理風格）。
    structure = _minimal_python_structure()
    structure["interfaces"].append(
        {
            "file_path": "app/unknown/helper.py",  # 不在任何已知目錄底下
            "class_name": None,
            "function_name": "unexpected",
            "params": [],
            "return_type": "None",
        }
    )
    files, skipped_interfaces, _ = build_files(structure, {})
    assert "app/unknown/helper.py" not in files
    assert len(skipped_interfaces) == 1
    assert skipped_interfaces[0]["function_name"] == "unexpected"
    assert "unknown layer" in skipped_interfaces[0]["error"]
    # 其餘介面不受影響，正常產出
    assert "app/routers/user_router.py" in files


def test_build_files_renders_utils_file_as_free_functions(tmp_path):
    # 對應真實 04->08 端對端驗證發現的缺口：app/utils/ 底下的 interface
    # 原本會被判定成 unknown layer 整個跳過（見上一個測試修正前的舊
    # 案例），導致 generate_scaffold() 從未建立任何 app/utils/*.py 檔案，
    # 12 個 utils 函式全部消失。08a 二章「Utils 檔案結構——已定案」：
    # Java 靜態工具方法對應 Python module-level 自由函式，不歸屬任何
    # module，渲染規則跟 routers／global_advice 同一種形狀——不包 class、
    # 不包 APIRouter 樣板。
    structure = _minimal_python_structure()
    structure["interfaces"].append(
        {
            "file_path": "app/utils/validation_util.py",
            "class_name": None,
            "function_name": "is_null_or_empty",
            "params": [{"name": "value", "type": "str"}],
            "return_type": "bool",
        }
    )
    files, skipped_interfaces, _ = build_files(structure, {})
    assert skipped_interfaces == []
    source = files["app/utils/validation_util.py"]
    assert "def is_null_or_empty(value: str) -> bool:" in source
    assert "APIRouter" not in source
    assert "router = " not in source
    assert "class " not in source
    # 其餘介面不受影響，正常產出
    assert "app/routers/user_router.py" in files


def test_build_files_renders_global_advice_file_without_api_router_boilerplate():
    # 對應 docs/09b_bug_trace.md #11/#12：真實端對端測試發現
    # app/core/exception_handlers.py（_global 保留模組固定輸出）原本會
    # 被判定成 unknown layer 整個跳過，導致 generate_scaffold() 從未
    # 建立這個檔案。修正後應正常渲染，且不像 routers 層那樣包
    # APIRouter 樣板（這不是真正的路由檔案）。
    structure = _minimal_python_structure()
    structure["interfaces"].append(
        {
            "file_path": "app/core/exception_handlers.py",
            "class_name": None,
            "function_name": "handle_all",
            "params": [{"name": "request", "type": "Request"}, {"name": "exc", "type": "Exception"}],
            "return_type": "Response",
            "http_method": None,
            "route_path": None,
        }
    )
    files, skipped_interfaces, _ = build_files(structure, {})
    assert skipped_interfaces == []
    source = files["app/core/exception_handlers.py"]
    assert "def handle_all(request: Request, exc: Exception) -> Response:" in source
    assert "APIRouter" not in source
    assert "router = " not in source
    # 其餘介面不受影響，正常產出
    assert "app/routers/user_router.py" in files


def test_build_files_raises_on_broken_schema_pseudocode():
    structure = _minimal_python_structure()
    structure["directory_tree"] += '\n### app/schemas/broken.py\n```python\nclass (:\n```\n'
    with pytest.raises(TranslatorCliAssemblyError):
        build_files(structure, {})


def test_build_files_repository_with_jpa_base_entity_inherits_base_repository():
    """對應 docs/refactor_bug_trace.md #10／#16：`InterfaceSpec.
    jpa_base_entity` 非 None 時，repositories 層的 class 要渲染成繼承
    `BaseRepository[Entity]`，並注入 `model = Entity`，同時正確 import
    `BaseRepository` 本身與 entity 型別（來源三：db_models）。
    """
    structure = _minimal_python_structure()
    structure["directory_tree"] += (
        "\n### app/models/exam.py\n```python\nclass ExamEntity:\n    id: int\n```\n"
    )
    structure["interfaces"].append(
        {
            "file_path": "app/repositories/exam_repository.py",
            "class_name": "ExamRepository",
            "function_name": "find_all",
            "params": [{"name": "db", "type": "Session"}],
            "return_type": "list[ExamEntity]",
            "jpa_base_entity": "ExamEntity",
        }
    )
    files, skipped_interfaces, _ = build_files(structure, {})
    assert skipped_interfaces == []
    source = files["app/repositories/exam_repository.py"]
    assert "class ExamRepository(BaseRepository[ExamEntity]):" in source
    assert "    model = ExamEntity" in source
    assert "from app.core.base_repository import BaseRepository" in source
    assert "from app.models.exam import ExamEntity" in source
    # 其餘沒有 jpa_base_entity 的既有 repository class 渲染規則不受影響
    assert "class UserRepository:" in files["app/repositories/user_repository.py"]


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

    index, schema_classes_by_module = _scan_project_custom_types(str(tmp_path))
    assert index["UserRepository"] == "app/repositories/user_repository.py"
    assert index["UserResponse"] == "app/schemas/user.py"
    assert schema_classes_by_module == {"user": frozenset({"UserResponse"})}


def test_scan_project_custom_types_skips_unparseable_file(tmp_path):
    (tmp_path / "app" / "services").mkdir(parents=True)
    (tmp_path / "app" / "services" / "broken.py").write_text("class (:\n", encoding="utf-8")
    (tmp_path / "app" / "services" / "ok.py").write_text("class OkService:\n    pass\n", encoding="utf-8")

    index, _ = _scan_project_custom_types(str(tmp_path))
    assert index == {"OkService": "app/services/ok.py"}


def test_scan_project_custom_types_missing_app_dir_returns_empty(tmp_path):
    assert _scan_project_custom_types(str(tmp_path)) == ({}, {})


def test_table_assignment_names_finds_bare_call():
    # 08a_scaffold_agent_architecture.md 八章「@ManyToMany：產生中介表
    # 定義」：user_roles = Table(...) 是 ast.Assign，不是 ast.ClassDef，
    # 兩處自訂型別索引（_build_custom_type_index／_scan_project_
    # custom_types）都得靠這個函式才找得到。
    tree = ast.parse(
        "from sqlalchemy import Column, ForeignKey, Table\n\n"
        'user_roles = Table(\n    "user_roles",\n    Base.metadata,\n'
        '    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),\n)\n'
    )
    assert _table_assignment_names(tree) == ["user_roles"]


def test_table_assignment_names_finds_attribute_call():
    # func 是 ast.Attribute（sqlalchemy.Table(...) 完整路徑呼叫），不是
    # ast.Name——兩種匯入風格都要認得。
    tree = ast.parse('user_roles = sqlalchemy.Table("user_roles", Base.metadata)\n')
    assert _table_assignment_names(tree) == ["user_roles"]


def test_table_assignment_names_ignores_unrelated_assignments():
    # 一般常數賦值、非單一目標賦值、呼叫其他函式，都不該被誤收進索引。
    tree = ast.parse(
        "DEFAULT_TIMEOUT = 30\n"
        "a = b = Table(\"x\", Base.metadata)\n"
        "user_roles = SomeOtherThing()\n"
    )
    assert _table_assignment_names(tree) == []


def test_scan_project_custom_types_finds_table_assignment(tmp_path):
    (tmp_path / "app" / "models").mkdir(parents=True)
    (tmp_path / "app" / "models" / "user.py").write_text(
        "from sqlalchemy import Column, ForeignKey, Integer, Table\n\n"
        'user_roles = Table(\n    "user_roles",\n    Base.metadata,\n'
        '    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),\n)\n',
        encoding="utf-8",
    )

    index, _ = _scan_project_custom_types(str(tmp_path))
    assert index["user_roles"] == "app/models/user.py"


def test_build_custom_type_index_source_three_includes_table_assignment():
    # 來源三（db_models）除了既有的 ClassDef 掃描，也要納入 Table(...)
    # 賦值——這是 _build_custom_type_index() 三個來源合併時的完整路徑，
    # 不只是 _table_assignment_names() 這個底層函式本身正確。
    db_models_valid = {
        "app/models/order.py": ast.parse(
            "from sqlalchemy import Column, ForeignKey, Integer, Table\n\n"
            'user_roles = Table(\n    "user_roles",\n    Base.metadata,\n'
            '    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),\n)\n'
        )
    }
    index, _ = _build_custom_type_index(interfaces=[], schema_trees={}, db_models_valid=db_models_valid)
    assert index["user_roles"] == "app/models/order.py"


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


def test_resolve_body_imports_excludes_class_defined_in_same_file(tmp_path):
    # 對應真實案例：exam-platform-api 的 app/services/common_service.py，
    # ResponseResult 這個 class 的方法回傳自己（如
    # error() -> ResponseResult(...)）——這個 class 本身就在同一個檔案
    # 的 tree.body 裡，同檔案內引用不需要 import。若 _scan_project_
    # custom_types() 掃描到磁碟上這個檔案自己也定義了同名 class，會誤判
    # 成「要從自己匯入自己」，產生一行永遠成立的循環 import，讓這個
    # 模組 import 階段直接 ImportError（已用真實案例重現，見
    # docs/09b_implement_agent_code.md 十章「已知限制」）。
    (tmp_path / "app" / "services").mkdir(parents=True)
    # 磁碟上另一個檔案剛好也叫這個名字的情況也要一併確認不會誤命中，
    # 但這裡先驗證最直接的案例：正在處理的這個檔案自己。
    (tmp_path / "app" / "services" / "common_service.py").write_text(
        "class ResponseResult:\n    pass\n", encoding="utf-8"
    )

    tree = ast.parse(
        "from __future__ import annotations\n\n\nclass ResponseResult:\n    pass\n"
    )
    body = ast.parse("return ResponseResult(success=False, data=None, code=code, message=msg)\n").body

    lines = resolve_body_imports(str(tmp_path), tree, body, bound_names={"code", "msg"})
    assert lines == []


def test_resolve_body_imports_excludes_function_defined_in_same_file(tmp_path):
    tree = ast.parse(
        "from __future__ import annotations\n\n\ndef helper():\n    return 1\n"
    )
    body = ast.parse("return helper()\n").body

    lines = resolve_body_imports(str(tmp_path), tree, body, bound_names=set())
    assert lines == []


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


def test_scan_project_custom_types_groups_schema_classes_by_module(tmp_path):
    # 對應 docs/refactor_bug_trace.md #45：GetAllGradeRs 同時在
    # exam.py／school.py 各自定義一份，schema_classes_by_module 要能
    # 分別記錄兩個模組各自有這個名稱，供 resolve_body_imports() 依模組
    # 查詢，不能只看扁平索引。
    (tmp_path / "app" / "schemas").mkdir(parents=True)
    (tmp_path / "app" / "schemas" / "exam.py").write_text(
        "class GetAllGradeRs:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "app" / "schemas" / "school.py").write_text(
        "class GetAllGradeRs:\n    pass\n", encoding="utf-8"
    )

    _, schema_classes_by_module = _scan_project_custom_types(str(tmp_path))
    assert schema_classes_by_module["exam"] == frozenset({"GetAllGradeRs"})
    assert schema_classes_by_module["school"] == frozenset({"GetAllGradeRs"})


def test_resolve_body_imports_prefers_own_module_schema_over_global_index(tmp_path, caplog):
    """對應 docs/refactor_bug_trace.md #45 真實案例：`GetAllGradeRs` 同時
    在 `app/schemas/exam.py`／`app/schemas/school.py` 定義，字母序
    "school" 排在 "exam" 後面、覆寫掉全域索引裡 "exam" 的條目——
    `exam_router.py::grades()` 傳入 `own_module="exam"` 時，即使全域
    索引指向 school.py，也要優先解析成自己模組（exam.py）的定義，並且
    留下 log 可觀察這個優先序真的生效。"""
    (tmp_path / "app" / "schemas").mkdir(parents=True)
    (tmp_path / "app" / "schemas" / "exam.py").write_text(
        "class GetAllGradeRs:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "app" / "schemas" / "school.py").write_text(
        "class GetAllGradeRs:\n    pass\n", encoding="utf-8"
    )

    tree = ast.parse("from __future__ import annotations\n")
    body = ast.parse("data = GetAllGradeRs(grade=grade_list)\n").body

    with caplog.at_level("INFO"):
        lines = resolve_body_imports(
            str(tmp_path), tree, body, bound_names={"grade_list"}, own_module="exam"
        )

    assert lines == ["from app.schemas.exam import GetAllGradeRs"]
    assert "GetAllGradeRs" in caplog.text
    assert "#45" in caplog.text

    # 反過來：own_module="school" 時應該解析成 school.py 自己的定義。
    lines_school = resolve_body_imports(
        str(tmp_path), tree, body, bound_names={"grade_list"}, own_module="school"
    )
    assert lines_school == ["from app.schemas.school import GetAllGradeRs"]


def test_resolve_body_imports_falls_back_to_global_index_when_own_module_lacks_class(tmp_path):
    # own_module 有值，但這個模組自己沒有定義這個名稱時，退回既有的
    # 全域索引行為，不因為傳了 own_module 就整個失效。
    (tmp_path / "app" / "repositories").mkdir(parents=True)
    (tmp_path / "app" / "repositories" / "user_repository.py").write_text(
        "class UserRepository:\n    pass\n", encoding="utf-8"
    )

    tree = ast.parse("from __future__ import annotations\n")
    body = ast.parse("return UserRepository(db).get_by_id(user_id)\n").body

    lines = resolve_body_imports(
        str(tmp_path), tree, body, bound_names={"db", "user_id"}, own_module="exam"
    )
    assert lines == ["from app.repositories.user_repository import UserRepository"]


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
