"""plan_agent/planning.py 的四～八章核心邏輯測試，對應 06a 全文。[P] 不
再呼叫 Claude API（見 06a 五章），因此不需要任何 LLM mock——這裡直接
呼叫 `planning.plan_all_modules()`，驗證純機械組裝的結果。
"""
import pytest

from graph.scheduler import ModuleScheduler
from plan_agent import module_index, planning
from plan_agent.exceptions import PlanAgentCoverageError, PlanAgentModuleLookupError


def _iface(file_path, class_name, function_name, phase, params=None, return_type="None",
           java_method_id=None, **extra):
    iface = {
        "file_path": file_path,
        "class_name": class_name,
        "function_name": function_name,
        "params": params or [],
        "return_type": return_type,
        "phase": phase,
        # 大部分測試不關心呼叫鏈查找的具體結果（call_chain 有自己專屬的
        # tests/plan_agent/test_call_chain.py），這裡沒指定時自動生成一個
        # 佔位識別碼，格式跟真實資料一致但內容任意——反正對應的
        # java_project_path 底下沒有真實 .java 檔案，呼叫圖天生是空的。
        "java_method_id": java_method_id or f"{file_path}::{class_name}::{function_name}",
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


# ── 固定資料：user（無 schema 檔）／order（有 schema 檔）／utils／_global ──

USER_REPO = _iface("app/repositories/user_repository.py", "UserRepository", "get_by_id", phase=1,
                    params=[{"name": "user_id", "type": "int"}], return_type="User | None")
USER_SERVICE = _iface("app/services/user_service.py", "UserService", "get_user", phase=2,
                       params=[{"name": "user_id", "type": "int"}], return_type="User | None")
ORDER_REPO = _iface("app/repositories/order_repository.py", "OrderRepository", "get_by_id", phase=1,
                     params=[{"name": "order_id", "type": "int"}], return_type="Order | None")
ORDER_SERVICE = _iface("app/services/order_service.py", "OrderService", "get_order", phase=2,
                        params=[{"name": "order_id", "type": "int"}], return_type="Order | None")
ORDER_ROUTER = _iface("app/routers/order_router.py", None, "get_order_endpoint", phase=2,
                       params=[{"name": "order_id", "type": "int"}], return_type="OrderResponse",
                       http_method="GET", route_path="/api/v1/orders/{id}")
VALIDATION_UTIL = _iface("app/utils/validation_util.py", None, "is_valid_field", phase=1,
                          params=[{"name": "value", "type": "str"}], return_type="bool")
GLOBAL_HANDLER = _iface("app/core/exception_handlers.py", None, "handle_all", phase=2,
                         params=[{"name": "request", "type": "Request"}, {"name": "exc", "type": "Exception"}],
                         return_type="Response")


def _module_list():
    return [_module("user"), _module("order", depends_on=["user"])]


def _python_structure(interfaces, directory_tree="### app/schemas/order.py\n```python\nfrom pydantic import BaseModel\n```\n"):
    return {"directory_tree": directory_tree, "interfaces": interfaces}


def test_plan_all_modules_happy_path(tmp_path):
    structure = _python_structure(
        [USER_REPO, USER_SERVICE, ORDER_REPO, ORDER_SERVICE, ORDER_ROUTER, VALIDATION_UTIL, GLOBAL_HANDLER]
    )
    tasks, module_list = planning.plan_all_modules(_module_list(), structure, str(tmp_path))
    by_key = {(t["class_name"], t["function_name"]): t for t in tasks}

    # 涵蓋率：7 個 InterfaceSpec 對應恰好 7 個 task。
    assert len(tasks) == 7

    user_repo_t = by_key[("UserRepository", "get_by_id")]
    user_service_t = by_key[("UserService", "get_user")]
    order_repo_t = by_key[("OrderRepository", "get_by_id")]
    order_service_t = by_key[("OrderService", "get_order")]
    router_t = by_key[(None, "get_order_endpoint")]
    utils_t = by_key[(None, "is_valid_field")]
    global_t = by_key[(None, "handle_all")]

    # java_method_id：這個 task 自己對應的 Java 方法識別碼，逐字等於
    # InterfaceSpec.java_method_id——⑤抽取「這個函式自己」的 Java 原始碼
    # 時的座標，跟 reference_targets（呼叫到的其他函式）是互補的兩件事。
    assert user_repo_t["java_method_id"] == USER_REPO["java_method_id"]
    assert router_t["java_method_id"] == ORDER_ROUTER["java_method_id"]

    # module／layer 歸屬（四章）。
    assert user_repo_t["module"] == "user"
    assert order_service_t["module"] == "order"
    assert router_t["module"] == "order"
    assert utils_t["module"] == module_index.UTILS_MODULE_NAME
    assert global_t["module"] == module_index.GLOBAL_MODULE_NAME

    # phase：直接複製 InterfaceSpec.phase（五章）。
    assert user_repo_t["phase"] == 1
    assert user_service_t["phase"] == 2
    assert utils_t["phase"] == 1
    assert global_t["phase"] == 2

    # translator_backend：只有 repositories 層 qwen，其餘 claude（五章）。
    assert user_repo_t["translator_backend"] == "qwen"
    assert order_repo_t["translator_backend"] == "qwen"
    assert user_service_t["translator_backend"] == "claude"
    assert router_t["translator_backend"] == "claude"
    assert utils_t["translator_backend"] == "claude"
    assert global_t["translator_backend"] == "claude"

    # description：純機械模板，不含業務語意（五章）。
    assert user_repo_t["description"] == "填入 app/repositories/user_repository.py 的 UserRepository.get_by_id()"
    assert router_t["description"] == "填入 app/routers/order_router.py 的 get_order_endpoint()"

    # context：沒有 config_field_mappings 對應項目時維持空字串（五章）。
    assert user_repo_t["context"] == ""

    # id 依固定全序（module_rank → layer → function_name）穩定編號，
    # _utils／_global 排在所有業務 module 之後（八章）。
    assert user_repo_t["id"] < user_service_t["id"] < order_repo_t["id"]
    assert order_repo_t["id"] < order_service_t["id"] < router_t["id"]
    assert router_t["id"] < utils_t["id"] < global_t["id"]

    # depends_on：同 module 內、且同一個 translator_backend 的 task 依
    # 全序串成一條鏈（六章，不再依賴任何「業務上是否真的呼叫到」的
    # 判斷）；module 邊界、後端邊界都會重置——repositories（qwen）跟
    # services／routers（claude）在全序裡相鄰但後端不同，鏈在這裡斷開，
    # 不會讓一個孤立的 qwen 失敗連坐同 module 的 Claude task（見
    # docs/refactor_bug_trace.md）；_utils／_global 內部固定留空。
    assert user_repo_t["depends_on"] == []
    assert user_service_t["depends_on"] == []  # 後端邊界：user_repo 是 qwen，user_service 是 claude
    assert order_repo_t["depends_on"] == []  # module 邊界：跟前一名（user_service）不同 module
    assert order_service_t["depends_on"] == []  # 後端邊界：order_repo 是 qwen，order_service 是 claude
    assert router_t["depends_on"] == [order_service_t["id"]]  # 同後端（claude）：order_service → router
    assert utils_t["depends_on"] == []
    assert global_t["depends_on"] == []

    # target_files（七章）：repositories 只帶 model；services 依 module
    # 是否有 schema 檔決定要不要帶 schema、無條件帶 model；routers 依
    # 是否有 schema 檔決定要不要帶 schema，不帶 model；utils／_global
    # 只有自己的檔案。
    assert user_repo_t["target_files"] == ["app/repositories/user_repository.py", "app/models/user.py"]
    assert user_service_t["target_files"] == ["app/services/user_service.py", "app/models/user.py"]  # user 無 schema 檔
    assert order_repo_t["target_files"] == ["app/repositories/order_repository.py", "app/models/order.py"]
    assert order_service_t["target_files"] == [
        "app/services/order_service.py", "app/schemas/order.py", "app/models/order.py",
    ]
    assert router_t["target_files"] == ["app/routers/order_router.py", "app/schemas/order.py"]
    assert utils_t["target_files"] == ["app/utils/validation_util.py"]
    assert global_t["target_files"] == ["app/core/exception_handlers.py"]

    # reference_targets（六章）：java_project_path 底下沒有真實 .java
    # 檔案，呼叫圖天生是空的，因此每個 task 的呼叫鏈查找都找不到任何
    # 直接呼叫對象——這裡只驗證欄位存在且為空清單、沒有截斷，具體的
    # 呼叫鏈判斷邏輯見 test_call_chain.py。
    for t in tasks:
        assert t["reference_targets"] == []
        assert "reference_targets_truncated" not in t

    # module_list 第二回傳值：補回 _utils 保留 module（見
    # planning.plan_all_modules() docstring「第二個回傳值」）——真實環境
    # 發現 graph/scheduler.py::ModuleScheduler 用呼叫端傳入的 module_list
    # 建構自己追蹤的 module 集合，_utils 若沒有對應條目，這個 module 底下
    # 的 task 永遠排不進 get_ready_tasks()。這裡驗證補回的條目本身。
    utils_module = next(m for m in module_list if m["module"] == module_index.UTILS_MODULE_NAME)
    assert utils_module["depends_on"] == []
    assert utils_module["java_files"] == [VALIDATION_UTIL["java_method_id"].split("::", 1)[0]]
    # 原始 module_list（user／order）不受影響，只多這一筆。
    assert {m["module"] for m in module_list} == {"user", "order", module_index.UTILS_MODULE_NAME}


def test_utils_module_list_entry_makes_utils_tasks_schedulable(tmp_path):
    """端對端證實這次修的是什麼：拿 `plan_all_modules()` 回傳的
    `module_list`（已補回 `_utils`）直接餵給真實 `ModuleScheduler`，
    utils task 要出現在第一輪 `get_ready_tasks()`——修好之前，用①原始
    `module_list`（沒有 `_utils` 條目）餵同一份 `task_list`，
    `get_ready_tasks()` 永遠不會回傳任何 utils task（已用真實
    `lang-exam-api-refactor` pipeline 資料證實過，見對話紀錄）。
    """
    structure = _python_structure([USER_REPO, VALIDATION_UTIL], directory_tree="")
    tasks, module_list = planning.plan_all_modules([_module("user")], structure, str(tmp_path))

    scheduler = ModuleScheduler(module_list, tasks)
    ready_modules = {t["module"] for t in scheduler.get_ready_tasks()}
    assert module_index.UTILS_MODULE_NAME in ready_modules


def test_module_list_unchanged_when_no_utils_tasks(tmp_path):
    """沒有任何 utils 類別的目標專案，module_list 原封不動傳回，不會
    無中生有多一筆空的 `_utils` 條目——見 `_build_utils_module_entry()`
    docstring「只在真的有 task 落在 _utils module 時才產生」。
    """
    structure = _python_structure([USER_REPO, USER_SERVICE], directory_tree="")
    original_module_list = [_module("user")]
    tasks, module_list = planning.plan_all_modules(original_module_list, structure, str(tmp_path))
    assert module_list == original_module_list


def test_phase_is_copied_verbatim_not_reinterpreted(tmp_path):
    structure = _python_structure([USER_REPO, USER_SERVICE], directory_tree="")
    tasks, module_list = planning.plan_all_modules([_module("user")], structure, str(tmp_path))
    by_key = {t["function_name"]: t for t in tasks}
    assert by_key["get_by_id"]["phase"] == 1
    assert by_key["get_user"]["phase"] == 2


def test_missing_phase_raises_key_error(tmp_path):
    """06a 五章「不重新判斷、不需要 fallback」：InterfaceSpec 缺少 phase
    代表③違反自己的輸出契約，直接讓 KeyError 往上拋，不嘗試靜默補值。"""
    broken = dict(USER_REPO)
    del broken["phase"]
    with pytest.raises(KeyError):
        planning.plan_all_modules([_module("user")], _python_structure([broken], directory_tree=""), str(tmp_path))


def test_return_type_is_copied_verbatim_not_reinterpreted(tmp_path):
    """對應 docs/refactor_bug_trace.md #46：`TaskSpec.return_type` 逐字
    複製 `InterfaceSpec.return_type`，不重新判斷——`GLOBAL_HANDLER` 的
    "Response" 是③對 ResponseEntity<...> 簽名機械覆寫出來的結果（見
    #30），這裡只驗證複製本身正確，判斷邏輯是③的職責，不是 plan_agent
    的職責。"""
    structure = _python_structure([USER_REPO, GLOBAL_HANDLER], directory_tree="")
    tasks, _ = planning.plan_all_modules([_module("user")], structure, str(tmp_path))
    by_key = {t["function_name"]: t for t in tasks}
    assert by_key["get_by_id"]["return_type"] == "User | None"
    assert by_key["handle_all"]["return_type"] == "Response"


def test_missing_return_type_raises_key_error(tmp_path):
    broken = dict(USER_REPO)
    del broken["return_type"]
    with pytest.raises(KeyError):
        planning.plan_all_modules([_module("user")], _python_structure([broken], directory_tree=""), str(tmp_path))


def test_config_field_mappings_appended_to_matching_task_context(tmp_path):
    """對應 docs/09b_bug_trace.md #45 修法：`python_structure.
    config_field_mappings` 裡有這個 task 對應檔案的項目時，機械附加
    一段提示——這是 context 唯一還會被填內容的來源（06a 五章）。"""
    structure = {
        "directory_tree": "",
        "interfaces": [USER_SERVICE],
        "config_field_mappings": {
            "app/services/user_service.py": {"code": "app.core.config.LANGUAGE_CODE"},
        },
    }
    tasks, module_list = planning.plan_all_modules([_module("user")], structure, str(tmp_path))
    context = tasks[0]["context"]
    assert "`code` 欄位改成 `from app.core.config import LANGUAGE_CODE` 後直接使用 `LANGUAGE_CODE`" in context
    assert "app/core/config.py" in context
    # 對應 #58：明講沒有 settings 物件，避免⑤把點記法誤讀成物件屬性存取。
    assert "沒有 settings 物件" in context
    assert "settings.LANGUAGE_CODE" in context


def test_no_config_field_mappings_leaves_context_empty(tmp_path):
    structure = {"directory_tree": "", "interfaces": [USER_SERVICE]}
    tasks, module_list = planning.plan_all_modules([_module("user")], structure, str(tmp_path))
    assert tasks[0]["context"] == ""


def test_utils_and_global_tasks_have_no_referenced_functions_field(tmp_path):
    """[P] 不再產生 referenced_interfaces／referenced_functions（見 06a
    七章、八章），TaskSpec 建構時完全不設這個 key——既有消費端
    （translator_cli／implement_node.py）用 `task.get("referenced_
    functions", [])` 安全處理缺席，這裡驗證 [P] 真的沒有寫入這個 key。
    """
    structure = _python_structure([USER_REPO], directory_tree="")
    tasks, module_list = planning.plan_all_modules([_module("user")], structure, str(tmp_path))
    assert "referenced_functions" not in tasks[0]


def test_duplicate_interfaces_raises_coverage_error(tmp_path):
    """06a 八章 defense-in-depth：`python_structure.interfaces` 本身有
    重複的三元組（③輸出的正確性缺陷）時，涵蓋率驗證要能偵測到。"""
    duplicated = _python_structure([USER_REPO, dict(USER_REPO)], directory_tree="")
    with pytest.raises(PlanAgentCoverageError):
        planning.plan_all_modules([_module("user")], duplicated, str(tmp_path))


def test_bad_file_path_raises_module_lookup_error(tmp_path):
    """06a 四章：`file_path` 不符合 `{module}_{layer}.py` 格式、也不是
    utils／_global 特例時直接中止。"""
    bad_structure = _python_structure(
        [_iface("app/schemas/user.py", None, "not_a_valid_layer_file", phase=2)], directory_tree=""
    )
    with pytest.raises(PlanAgentModuleLookupError):
        planning.plan_all_modules([_module("user")], bad_structure, str(tmp_path))


def test_empty_interfaces_yields_empty_task_list(tmp_path):
    tasks, module_list = planning.plan_all_modules(_module_list(), _python_structure([], directory_tree=""), str(tmp_path))
    assert tasks == []


def test_depends_on_chain_breaks_at_translator_backend_boundary(tmp_path):
    """對應 docs/refactor_bug_trace.md：真實環境重現過一個孤立的 qwen
    repository task 失敗，把同 module 底下毫無關聯的 Claude service／
    router task 全部拖死——根因是 _build_depends_on_map() 原本不分
    translator_backend、無條件把同 module 全序串成一條鏈。這裡直接鎖住
    修正後的行為：repository（qwen）→ service（claude）的鏈在後端邊界
    斷開；同為 repository（qwen）的兩個 task 之間依然維持自動序列化
    （qwen 併發數鎖死為 1 的原始理由對這半段仍然成立）。
    """
    repo_a = _iface("app/repositories/user_repository.py", "UserRepository", "get_by_id", phase=1,
                     params=[{"name": "user_id", "type": "int"}], return_type="User | None")
    repo_b = _iface("app/repositories/user_repository.py", "UserRepository", "get_by_name", phase=1,
                     params=[{"name": "name", "type": "str"}], return_type="User | None")
    structure = _python_structure([repo_a, repo_b, USER_SERVICE], directory_tree="")
    tasks, _ = planning.plan_all_modules([_module("user")], structure, str(tmp_path))
    by_key = {(t["class_name"], t["function_name"]): t for t in tasks}

    repo_a_t = by_key[("UserRepository", "get_by_id")]
    repo_b_t = by_key[("UserRepository", "get_by_name")]
    service_t = by_key[("UserService", "get_user")]

    assert repo_a_t["translator_backend"] == "qwen"
    assert repo_b_t["translator_backend"] == "qwen"
    assert service_t["translator_backend"] == "claude"

    # 同為 qwen：維持既有的自動序列化。
    assert repo_b_t["depends_on"] == [repo_a_t["id"]]
    # 後端邊界（qwen → claude）：鏈在這裡斷開，不是接在 repo_b 後面。
    assert service_t["depends_on"] == []
