"""plan_agent/planning.py 的五～八章核心邏輯測試，對應 06a 全文。全部
用 monkeypatch 換掉 `plan_agent.planning.call_claude_for_json`，不呼叫
真實 Claude API（比照 tests/design_agent/test_design.py 對 LLM 呼叫點的
既有 mock 方式）。

06b 五章「已驗證」列出的情境（happy path、涵蓋率 defense-in-depth、
`module_index.classify()`）先前只用一次性 dry-run 腳本驗證過，沒有留下
對應的 pytest 檔案（`git log` 上實作 commit `1358ce8` 沒有新增任何
`tests/plan_agent/*.py`）——本檔案把這些情境落地成可重複執行的迴歸測試，
同時覆蓋 `07a_translator_cli_architecture.md` 五章新增的
`class_name`／`function_name` 落地行為。
"""
import json

import pytest

from plan_agent import module_index, planning
from plan_agent.exceptions import PlanAgentCoverageError, PlanAgentModuleError, PlanAgentModuleLookupError


def _iface(file_path, class_name, function_name, params=None, return_type="None", **extra):
    iface = {
        "file_path": file_path,
        "class_name": class_name,
        "function_name": function_name,
        "params": params or [],
        "return_type": return_type,
    }
    iface.update(extra)
    return iface


def _module(name, depends_on=None):
    return {
        "module": name,
        "summary": f"{name} module summary",
        "java_files": [],
        "depends_on": depends_on or [],
        "methods": [],
    }


# ── happy path 固定資料：user（無 schema 檔）／order（有 schema 檔，依賴 user）──

USER_REPO = _iface("app/repositories/user_repository.py", "UserRepository", "get_by_id",
                    params=[{"name": "user_id", "type": "int"}], return_type="User | None")
USER_SERVICE = _iface("app/services/user_service.py", "UserService", "get_user",
                       params=[{"name": "user_id", "type": "int"}], return_type="User | None")
ORDER_REPO = _iface("app/repositories/order_repository.py", "OrderRepository", "get_by_id",
                     params=[{"name": "order_id", "type": "int"}], return_type="Order | None")
ORDER_SERVICE = _iface("app/services/order_service.py", "OrderService", "get_order",
                        params=[{"name": "order_id", "type": "int"}], return_type="Order | None")
ORDER_ROUTER = _iface("app/routers/order_router.py", None, "get_order_endpoint",
                       params=[{"name": "order_id", "type": "int"}], return_type="OrderResponse",
                       http_method="GET", route_path="/api/v1/orders/{id}")

_ID = lambda i: module_index.interface_id(i["file_path"], i["class_name"], i["function_name"])  # noqa: E731


def _module_list():
    return [_module("user"), _module("order", depends_on=["user"])]


def _python_structure():
    return {
        # order 有 Schema 定義段，user 沒有——用來驗證 _modules_with_schema_file()
        # 只依 `### {file_path}` 字面比對（見 06a 七章、planning.py 說明）。
        "directory_tree": "app/...\n\n### app/schemas/order.py\n```python\nfrom pydantic import BaseModel\n```\n",
        "interfaces": [USER_REPO, USER_SERVICE, ORDER_REPO, ORDER_SERVICE, ORDER_ROUTER],
    }


def _happy_responses():
    """依 module 名稱回傳 tasks 陣列。referenced_interfaces 只包含合法、
    正向（依 06a 六章固定全序）的引用，用來驗證 `depends_on`／
    `target_files` 的正常組裝路徑。"""
    return {
        "user": [
            {"interface_id": _ID(USER_REPO), "description": "查詢使用者", "context": "", "referenced_interfaces": []},
            {"interface_id": _ID(USER_SERVICE), "description": "取得使用者", "context": "",
             "referenced_interfaces": [_ID(USER_REPO)]},
        ],
        "order": [
            {"interface_id": _ID(ORDER_REPO), "description": "查詢訂單", "context": "", "referenced_interfaces": []},
            {"interface_id": _ID(ORDER_SERVICE), "description": "取得訂單（含使用者資訊）", "context": "",
             "referenced_interfaces": [_ID(ORDER_REPO), _ID(USER_SERVICE)]},
            {"interface_id": _ID(ORDER_ROUTER), "description": "訂單查詢 API", "context": "",
             "referenced_interfaces": [_ID(ORDER_SERVICE)]},
        ],
    }


def _make_fake_call(responses_by_module):
    """建一個可以直接 monkeypatch 給 `planning.call_claude_for_json` 的
    假函式：依 user_prompt 裡的 module 名稱回傳對應的 canned tasks，不打
    真實 API。"""

    def _fake(*, system_prompt, user_prompt, schema, model, max_tokens):
        payload = json.loads(user_prompt)
        module_name = payload["module"]["module"]
        return {"tasks": responses_by_module[module_name]}

    return _fake


def test_plan_all_modules_happy_path(monkeypatch):
    monkeypatch.setattr(planning, "call_claude_for_json", _make_fake_call(_happy_responses()))

    tasks = planning.plan_all_modules(_module_list(), _python_structure())
    by_key = {(t["class_name"], t["function_name"]): t for t in tasks}

    # 涵蓋率：5 個 InterfaceSpec 對應恰好 5 個 task。
    assert len(tasks) == 5

    # 07a 新增：class_name／function_name 必須落地進最終 TaskSpec，且與
    # 對應 InterfaceSpec 完全一致（routers 層 class_name 為 None）。
    assert by_key[("UserRepository", "get_by_id")]["module"] == "user"
    assert by_key["OrderService", "get_order"]["module"] == "order"
    router_task = by_key[(None, "get_order_endpoint")]
    assert router_task["module"] == "order"

    # id 依固定全序（module_rank → layer → function_name）穩定編號。
    ids_in_order = [t["id"] for t in tasks]
    assert ids_in_order == sorted(ids_in_order)
    assert by_key[("UserRepository", "get_by_id")]["id"] < by_key[("UserService", "get_user")]["id"]
    assert by_key[("OrderRepository", "get_by_id")]["id"] < by_key[("OrderService", "get_order")]["id"]
    assert by_key[("OrderService", "get_order")]["id"] < by_key[(None, "get_order_endpoint")]["id"]

    # depends_on：同 module 正向依賴保留；跨 module 的引用不進 depends_on
    # （見 06a 六章，只影響 target_files）。
    order_service_task = by_key[("OrderService", "get_order")]
    order_repo_task = by_key[("OrderRepository", "get_by_id")]
    user_service_task = by_key[("UserService", "get_user")]
    assert order_service_task["depends_on"] == [order_repo_task["id"]]
    assert router_task["depends_on"] == [order_service_task["id"]]
    assert by_key[("UserRepository", "get_by_id")]["depends_on"] == []
    assert user_service_task["depends_on"] == [by_key[("UserRepository", "get_by_id")]["id"]]

    # target_files：routers 層帶 schema（因為 order 有 schema 檔）、不帶 model；
    # services 層無條件帶 model，且依 order 是否有 schema 檔決定要不要帶 schema；
    # repositories 層只帶 model；跨 module 引用依對方層級套用同一套規則。
    assert router_task["target_files"] == [
        "app/routers/order_router.py",
        "app/services/order_service.py",
        "app/schemas/order.py",
    ]
    assert order_service_task["target_files"] == [
        "app/services/order_service.py",
        "app/repositories/order_repository.py",
        "app/services/user_service.py",
        "app/schemas/order.py",
        "app/models/order.py",
        "app/models/user.py",  # user_service 屬 services 層，跨 module 無條件帶 model
    ]
    assert order_repo_task["target_files"] == ["app/repositories/order_repository.py", "app/models/order.py"]
    # user module 沒有 schema 檔，即使 layer 是 services 也不帶 schema。
    assert user_service_task["target_files"] == [
        "app/services/user_service.py",
        "app/repositories/user_repository.py",
        "app/models/user.py",
    ]

    # referenced_functions（06a 七章新設計）：只記錄「因引用而拉進來」的
    # 檔案裡，具體是哪個函式被引用到——不含 schemas／models（這些從來
    # 不是 referenced_interfaces 的一部分，見三章）。
    assert user_service_task["referenced_functions"] == [
        {"file_path": "app/repositories/user_repository.py", "class_name": "UserRepository", "function_name": "get_by_id"},
    ]
    assert order_service_task["referenced_functions"] == [
        {"file_path": "app/repositories/order_repository.py", "class_name": "OrderRepository", "function_name": "get_by_id"},
        {"file_path": "app/services/user_service.py", "class_name": "UserService", "function_name": "get_user"},
    ]
    assert router_task["referenced_functions"] == [
        {"file_path": "app/services/order_service.py", "class_name": "OrderService", "function_name": "get_order"},
    ]
    # 沒有 referenced_interfaces 的 task，referenced_functions 是空清單。
    assert order_repo_task["referenced_functions"] == []


def test_referenced_interfaces_invalid_and_self_reference_filtered(monkeypatch, caplog):
    """06a 五章「核對規則」：`referenced_interfaces` 引用不存在的
    interface_id 一律過濾並記警告；引用自己也要濾掉（見 planning.py
    `_plan_module()` 的 `rid != iid` 條件）。"""
    responses = {
        "user": [
            {"interface_id": _ID(USER_REPO), "description": "查詢使用者", "context": "",
             "referenced_interfaces": [_ID(USER_REPO), "app/does/not/exist.py::Foo::bar"]},
            {"interface_id": _ID(USER_SERVICE), "description": "取得使用者", "context": "",
             "referenced_interfaces": []},
        ],
        "order": [
            {"interface_id": _ID(ORDER_REPO), "description": "查詢訂單", "context": "", "referenced_interfaces": []},
            {"interface_id": _ID(ORDER_SERVICE), "description": "取得訂單", "context": "",
             "referenced_interfaces": []},
            {"interface_id": _ID(ORDER_ROUTER), "description": "訂單查詢 API", "context": "",
             "referenced_interfaces": []},
        ],
    }
    monkeypatch.setattr(planning, "call_claude_for_json", _make_fake_call(responses))

    with caplog.at_level("WARNING"):
        tasks = planning.plan_all_modules(_module_list(), _python_structure())

    by_key = {(t["class_name"], t["function_name"]): t for t in tasks}
    # 自我引用與不存在的 interface_id 都被濾掉，depends_on／target_files
    # 都不應該包含它們。
    assert by_key[("UserRepository", "get_by_id")]["depends_on"] == []
    assert by_key[("UserRepository", "get_by_id")]["target_files"] == [
        "app/repositories/user_repository.py",
        "app/models/user.py",
    ]
    assert any("does/not/exist" in record.message for record in caplog.records)


def test_depends_on_drops_backward_and_same_rank_edges(monkeypatch, caplog):
    """06a 六章「防環規則」：只保留「被依賴 task 的全序索引 < 依賴方 task
    的全序索引」的邊，反向或同層邊直接捨棄並記 log——這裡刻意讓
    repositories 層的方法反向引用 services 層（違反固定全序），驗證
    這條邊被捨棄，且不會讓 depends_on 出現環。"""
    responses = {
        "user": [
            # 反向引用：repositories 引用 services（違反 repositories < services 全序）。
            {"interface_id": _ID(USER_REPO), "description": "查詢使用者", "context": "",
             "referenced_interfaces": [_ID(USER_SERVICE)]},
            {"interface_id": _ID(USER_SERVICE), "description": "取得使用者", "context": "",
             "referenced_interfaces": []},
        ],
        "order": [
            {"interface_id": _ID(ORDER_REPO), "description": "查詢訂單", "context": "", "referenced_interfaces": []},
            {"interface_id": _ID(ORDER_SERVICE), "description": "取得訂單", "context": "",
             "referenced_interfaces": []},
            {"interface_id": _ID(ORDER_ROUTER), "description": "訂單查詢 API", "context": "",
             "referenced_interfaces": []},
        ],
    }
    monkeypatch.setattr(planning, "call_claude_for_json", _make_fake_call(responses))

    with caplog.at_level("INFO"):
        tasks = planning.plan_all_modules(_module_list(), _python_structure())

    by_key = {(t["class_name"], t["function_name"]): t for t in tasks}
    # 反向邊被捨棄，depends_on 維持空——不是把 user_service 錯誤地排到
    # user_repository 之前。
    assert by_key[("UserRepository", "get_by_id")]["depends_on"] == []
    # 但 target_files 不受方向限制，仍然帶入被引用的檔案（見 06a 七章）。
    assert "app/services/user_service.py" in by_key[("UserRepository", "get_by_id")]["target_files"]
    assert any("防環規則捨棄" in record.message for record in caplog.records)


def test_missing_interface_in_response_retries_then_raises(monkeypatch):
    """06a 五章「失敗處理」：LLM 回應遺漏 interfaces 視同呼叫失敗，列入
    待重試清單；重試仍失敗則中止整條 plan run。用 monkeypatch 把
    `_RETRY_WAIT_SECONDS` 歸零，不真的等 5 分鐘。"""
    monkeypatch.setattr(planning, "_RETRY_WAIT_SECONDS", 0.0)

    incomplete_responses = {
        "user": [
            # 故意漏掉 USER_SERVICE，觸發 `_plan_module()` 的 missing 檢查。
            {"interface_id": _ID(USER_REPO), "description": "查詢使用者", "context": "",
             "referenced_interfaces": []},
        ],
        "order": [
            {"interface_id": _ID(ORDER_REPO), "description": "查詢訂單", "context": "", "referenced_interfaces": []},
            {"interface_id": _ID(ORDER_SERVICE), "description": "取得訂單", "context": "",
             "referenced_interfaces": []},
            {"interface_id": _ID(ORDER_ROUTER), "description": "訂單查詢 API", "context": "",
             "referenced_interfaces": []},
        ],
    }
    monkeypatch.setattr(planning, "call_claude_for_json", _make_fake_call(incomplete_responses))

    with pytest.raises(PlanAgentModuleError):
        planning.plan_all_modules(_module_list(), _python_structure())


def test_duplicate_interfaces_raises_coverage_error(monkeypatch):
    """06a 十二章 defense-in-depth：`python_structure.interfaces` 本身有
    重複的三元組（③輸出的正確性缺陷）時，八章涵蓋率驗證要能偵測到，
    見 06b `PlanAgentCoverageError` docstring「不是用來擋一個已知會發生
    的情況」。"""
    duplicated = _python_structure()
    duplicated["interfaces"] = [USER_REPO, dict(USER_REPO)]  # 同一個三元組出現兩次

    responses = {"user": [
        {"interface_id": _ID(USER_REPO), "description": "查詢使用者", "context": "", "referenced_interfaces": []},
    ]}
    monkeypatch.setattr(planning, "call_claude_for_json", _make_fake_call(responses))

    with pytest.raises(PlanAgentCoverageError):
        planning.plan_all_modules([_module("user")], duplicated)


def test_referenced_functions_excludes_same_file_reference(monkeypatch):
    """06a 七章新設計：引用同一個檔案裡的另一個函式時，不該出現在
    `referenced_functions` 裡——那個檔案是 `target_files[0]`，本來就整份
    帶入，_read_context_files() 若對它套用函式層級抽取會把目標函式本身
    也濾掉（見 `plan_agent/planning.py::_build_referenced_functions()`
    docstring）。"""
    sibling = _iface("app/services/user_service.py", "UserService", "get_user_summary",
                      params=[{"name": "user_id", "type": "int"}], return_type="str")
    structure = {
        "directory_tree": "",
        "interfaces": [USER_SERVICE, sibling],
    }
    responses = {
        "user": [
            {"interface_id": _ID(USER_SERVICE), "description": "取得使用者", "context": "",
             "referenced_interfaces": [_ID(sibling)]},  # 引用同檔案的另一個函式
            {"interface_id": _ID(sibling), "description": "取得使用者摘要", "context": "",
             "referenced_interfaces": []},
        ],
    }
    monkeypatch.setattr(planning, "call_claude_for_json", _make_fake_call(responses))

    tasks = planning.plan_all_modules([_module("user")], structure)
    by_key = {(t["class_name"], t["function_name"]): t for t in tasks}

    user_service_task = by_key[("UserService", "get_user")]
    assert user_service_task["referenced_functions"] == []
    # target_files 仍然只有自己的檔案一份（同檔案引用不會產生重複項目）。
    assert user_service_task["target_files"] == ["app/services/user_service.py", "app/models/user.py"]


def test_config_field_mappings_appended_to_matching_task_context(monkeypatch):
    """對應 docs/09b_bug_trace.md #45 修法：`python_structure.
    config_field_mappings`（③ design_agent.global_infra 產出）裡有這個
    task 對應檔案的項目時，機械附加一段提示進 context 尾端；沒有對應
    項目的 task（其他 module 的檔案）不受影響，context 維持原樣。"""
    structure = {
        "directory_tree": "",
        "interfaces": [USER_SERVICE],
        "config_field_mappings": {
            "app/services/user_service.py": {"code": "app.core.config.LANGUAGE_CODE"},
        },
    }
    responses = {
        "user": [
            {"interface_id": _ID(USER_SERVICE), "description": "取得使用者", "context": "既有 context 文字",
             "referenced_interfaces": []},
        ],
    }
    monkeypatch.setattr(planning, "call_claude_for_json", _make_fake_call(responses))

    tasks = planning.plan_all_modules([_module("user")], structure)
    task = tasks[0]
    assert "既有 context 文字" in task["context"]
    assert "`code` 欄位改成 `from app.core.config import LANGUAGE_CODE` 後直接使用 `LANGUAGE_CODE`" in task["context"]
    assert "app/core/config.py" in task["context"]


def test_config_hint_explicitly_forbids_settings_object_pattern(monkeypatch):
    """對應 docs/09b_bug_trace.md #58：⑤ 連續三次真實 pipeline run 都把
    舊版的 `app.core.config.LANGUAGE_CODE` 點記法提示誤讀成物件屬性存取，
    幻覺出從未存在的 `settings` 物件。新提示除了給完整 import 陳述式，
    還要明講「沒有 settings 物件」，直接點名禁止這個最常見的錯誤。"""
    structure = {
        "directory_tree": "",
        "interfaces": [USER_SERVICE],
        "config_field_mappings": {
            "app/services/user_service.py": {"code": "app.core.config.LANGUAGE_CODE"},
        },
    }
    responses = {
        "user": [
            {"interface_id": _ID(USER_SERVICE), "description": "取得使用者", "context": "既有 context 文字",
             "referenced_interfaces": []},
        ],
    }
    monkeypatch.setattr(planning, "call_claude_for_json", _make_fake_call(responses))

    tasks = planning.plan_all_modules([_module("user")], structure)
    context = tasks[0]["context"]
    assert "沒有 settings 物件" in context
    assert "settings.LANGUAGE_CODE" in context


def test_no_config_field_mappings_leaves_context_untouched(monkeypatch):
    responses = {
        "user": [
            {"interface_id": _ID(USER_SERVICE), "description": "取得使用者", "context": "既有 context 文字",
             "referenced_interfaces": []},
        ],
    }
    monkeypatch.setattr(planning, "call_claude_for_json", _make_fake_call(responses))

    tasks = planning.plan_all_modules([_module("user")], {"directory_tree": "", "interfaces": [USER_SERVICE]})
    assert tasks[0]["context"] == "既有 context 文字"


def test_bad_file_path_raises_module_lookup_error_before_calling_llm(monkeypatch):
    """06a 四章：`file_path` 不符合 `{module}_{layer}.py` 格式時直接中止
    ——這一步發生在 `plan_all_modules()` 呼叫 LLM 之前（見 planning.py
    `plan_all_modules()` 開頭的四章反查迴圈），因此就算把
    `call_claude_for_json` monkeypatch 成一定會拋錯，也不該被呼叫到。
    """

    def _fail_if_called(**kwargs):
        raise AssertionError("不應該呼叫到 LLM——四章反查應該在呼叫前就先中止")

    monkeypatch.setattr(planning, "call_claude_for_json", _fail_if_called)

    bad_structure = {
        "directory_tree": "",
        "interfaces": [_iface("app/schemas/user.py", None, "not_a_valid_layer_file")],
    }
    with pytest.raises(PlanAgentModuleLookupError):
        planning.plan_all_modules([_module("user")], bad_structure)
