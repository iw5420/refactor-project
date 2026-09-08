"""plan_agent/module_index.py 的機械反查／全序邏輯測試，對應 06a 四章
「module／layer 歸屬判定」、六章「機械規則」、八章「id 產生規則」共用的
固定全序。不需要任何 mock——這個檔案的函式全部是純機械字串規則，[P]
不呼叫 Claude API（見 06a 五章）。
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
    assert module_index.classify("app/core/exception_handlers.py", _MODULE_NAMES) == ("_global", "routers")


def test_classify_utils_prefix_maps_to_utils_module_regardless_of_module_names():
    # 對應 06a 四章「特例一」：utils 檔案路徑不帶 module 前綴，一律歸
    # 固定的 _utils 保留 module，不進入一般規則、不受 module_names 影響
    # （即使傳一個完全不含任何真實 module 的空集合也一樣命中）。
    assert module_index.classify("app/utils/validation_util.py", frozenset()) == ("_utils", "utils")
    assert module_index.classify("app/utils/exam_card_utils.py", _MODULE_NAMES) == ("_utils", "utils")


def test_classify_unknown_suffix_raises():
    """檔名不符合 `{module}_{layer}.py` 三種固定後綴之一（05a 三章「檔名
    規則」），也不是 utils／_global 兩個特例，代表③輸出違反自己的格式
    承諾，見 06a 四章「找不到對應後綴...→ 直接中止」。"""
    with pytest.raises(PlanAgentModuleLookupError):
        module_index.classify("app/schemas/user.py", _MODULE_NAMES)


def test_classify_unknown_module_raises():
    """後綴反查得出的 module 名稱不存在於 `module_list`（見 06a 四章
    「比對不到任何 module...→ 直接中止」）。"""
    with pytest.raises(PlanAgentModuleLookupError):
        module_index.classify("app/repositories/payment_repository.py", _MODULE_NAMES)


def test_schema_and_model_file_path_naming():
    assert module_index.schema_file_path("order") == "app/schemas/order.py"
    assert module_index.model_file_path("order") == "app/models/order.py"


def test_full_order_key_ranks_repositories_and_utils_before_services_before_routers():
    """對應 docs/refactor_bug_trace.md #23：`utils` 跟 `repositories` 同屬
    Phase 1（見 design_agent/layout.py::_PHASE_1_LAYERS），`LAYER_RANK`
    兩者並列最小值——`_utils` 保留 module 內部只有單一 layer，這裡跟
    `repositories` 並列不會影響任何實際排序結果（見 module_index.py
    `LAYER_RANK` docstring），只是確保這個判斷式的語意跟 Phase 1／2 的
    實際事實一致，供 `plan_agent/call_chain.py::build_reference_targets()`
    正確判斷「utils 呼叫保證已經翻譯完成」。"""
    module_rank = {"user": 0}
    repo_key = module_index.full_order_key(module_rank, "user", "repositories", "get_by_id", "UserRepository")
    utils_key = module_index.full_order_key(module_rank, "user", "utils", "helper", None)
    service_key = module_index.full_order_key(module_rank, "user", "services", "get_user", "UserService")
    router_key = module_index.full_order_key(module_rank, "user", "routers", "get_user_endpoint", None)
    assert module_index.LAYER_RANK["repositories"] == module_index.LAYER_RANK["utils"]
    assert repo_key < service_key < router_key
    assert utils_key < service_key < router_key


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


def test_full_order_key_reserved_modules_use_module_rank_too():
    # _utils／_global 不在 module_list 裡，呼叫端（planning._build_module_rank()）
    # 會另外補上 rank——這裡只驗證 full_order_key() 本身不特殊處理保留
    # module 名稱，純粹照傳入的 module_rank 查表。
    module_rank = {"user": 0, module_index.UTILS_MODULE_NAME: 1, module_index.GLOBAL_MODULE_NAME: 2}
    user_key = module_index.full_order_key(module_rank, "user", "routers", "z", None)
    utils_key = module_index.full_order_key(module_rank, module_index.UTILS_MODULE_NAME, "utils", "a", None)
    global_key = module_index.full_order_key(module_rank, module_index.GLOBAL_MODULE_NAME, "routers", "a", None)
    assert user_key < utils_key < global_key
