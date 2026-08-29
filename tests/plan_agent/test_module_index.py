"""plan_agent/module_index.py 的機械反查／編碼／全序邏輯測試，對應 06a
四章「module 歸屬判定」、六章「防環規則」固定全序、八章「id 產生規則」。
不需要任何 mock——這個檔案的函式全部是純機械字串規則，不碰 Claude API。
"""
import pytest

from plan_agent import module_index
from plan_agent.exceptions import PlanAgentModuleLookupError

_MODULE_NAMES = frozenset({"user", "order"})


def test_classify_repository_layer():
    assert module_index.classify("app/repositories/user_repository.py", _MODULE_NAMES) == ("user", "repositories")


def test_classify_service_layer():
    assert module_index.classify("app/services/order_service.py", _MODULE_NAMES) == ("order", "services")


def test_classify_router_layer():
    assert module_index.classify("app/routers/order_router.py", _MODULE_NAMES) == ("order", "routers")


def test_classify_exception_handlers_file_maps_to_global_module():
    # 對應 docs/09b_bug_trace.md #11/#12：真實端對端測試發現
    # app/core/exception_handlers.py（_global 保留模組固定輸出）不符合
    # {module}_{layer}.py 命名慣例，會被一般規則誤判成中止。
    assert module_index.classify("app/core/exception_handlers.py", frozenset({"_global"})) == ("_global", "routers")


def test_classify_unknown_suffix_raises():
    """檔名不符合 `{module}_{layer}.py` 三種固定後綴之一（05a 三章「檔名
    規則」），代表③輸出違反自己的格式承諾，見 06a 四章「找不到對應後綴
    ...→ 直接中止」。"""
    with pytest.raises(PlanAgentModuleLookupError):
        module_index.classify("app/schemas/user.py", _MODULE_NAMES)


def test_classify_unknown_module_raises():
    """後綴反查得出的 module 名稱不存在於 `module_list`（見 06a 四章
    「比對不到任何 module...→ 直接中止」）。"""
    with pytest.raises(PlanAgentModuleLookupError):
        module_index.classify("app/repositories/payment_repository.py", _MODULE_NAMES)


def test_interface_id_encodes_triple_with_class_name():
    assert (
        module_index.interface_id("app/services/user_service.py", "UserService", "get_user")
        == "app/services/user_service.py::UserService::get_user"
    )


def test_interface_id_uses_empty_placeholder_for_none_class_name():
    """routers 層 `class_name` 恆為 `None`（05a 七章），編碼時用空字串
    佔位，不需要在 JSON schema 處理 nullable（見 06b 二章說明）。"""
    assert (
        module_index.interface_id("app/routers/order_router.py", None, "get_order_endpoint")
        == "app/routers/order_router.py::::get_order_endpoint"
    )


def test_file_path_of_extracts_only_file_path_segment():
    iid = module_index.interface_id("app/services/user_service.py", "UserService", "get_user")
    assert module_index.file_path_of(iid) == "app/services/user_service.py"


def test_parse_interface_id_roundtrips_with_class_name():
    iid = module_index.interface_id("app/services/user_service.py", "UserService", "get_user")
    assert module_index.parse_interface_id(iid) == ("app/services/user_service.py", "UserService", "get_user")


def test_parse_interface_id_restores_none_for_empty_class_name_placeholder():
    # 對應 06a 七章新設計：referenced_interfaces 函式層級抽取需要完整
    # 三元組，routers 層編碼時的空字串佔位要正確還原成 None。
    iid = module_index.interface_id("app/routers/order_router.py", None, "get_order_endpoint")
    assert module_index.parse_interface_id(iid) == ("app/routers/order_router.py", None, "get_order_endpoint")


def test_parse_interface_id_function_name_may_contain_no_further_delimiters():
    file_path, class_name, function_name = module_index.parse_interface_id(
        "app/repositories/exam_repository.py::ExamSpecification::with_card"
    )
    assert file_path == "app/repositories/exam_repository.py"
    assert class_name == "ExamSpecification"
    assert function_name == "with_card"


def test_schema_and_model_file_path_naming():
    assert module_index.schema_file_path("order") == "app/schemas/order.py"
    assert module_index.model_file_path("order") == "app/models/order.py"


def test_full_order_key_ranks_repositories_before_services_before_routers():
    module_rank = {"user": 0}
    repo_key = module_index.full_order_key(module_rank, "user", "repositories", "get_by_id", "UserRepository")
    service_key = module_index.full_order_key(module_rank, "user", "services", "get_user", "UserService")
    router_key = module_index.full_order_key(module_rank, "user", "routers", "get_user_endpoint", None)
    assert repo_key < service_key < router_key


def test_full_order_key_same_layer_orders_by_function_name_then_class_name():
    module_rank = {"user": 0}
    a = module_index.full_order_key(module_rank, "user", "services", "a_method", "ZService")
    b = module_index.full_order_key(module_rank, "user", "services", "b_method", "AService")
    # function_name 字母序優先於 class_name tie-break。
    assert a < b


def test_full_order_key_module_rank_is_outermost_sort_key():
    module_rank = {"user": 0, "order": 1}
    # order module 即使層級／函式名字母序較前，仍排在 module_rank 較大的 user 之後。
    order_key = module_index.full_order_key(module_rank, "order", "repositories", "a", None)
    user_key = module_index.full_order_key(module_rank, "user", "routers", "z", None)
    assert user_key < order_key
