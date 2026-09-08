"""plan_agent/call_chain.py 的呼叫鏈範圍查找測試，對應 06a 六章。純用
合成的 call_graph／java_index 資料直接測試演算法本身，不需要真實 .java
檔案（那是 planning.py 端對端測試涵蓋的範圍，見 test_planning.py）。
"""
import pytest

from plan_agent import call_chain


def _entry(file_path, class_name, function_name, phase):
    return {"file_path": file_path, "class_name": class_name, "function_name": function_name, "phase": phase}


def _module(name, depends_on=None):
    return {"module": name, "summary": "", "java_files": [], "depends_on": depends_on or [], "methods": []}


_MODULE_NAMES = frozenset({"user", "order"})


def test_build_module_closures_is_transitive():
    module_list = [_module("a", depends_on=["b"]), _module("b", depends_on=["c"]), _module("c")]
    closures = call_chain.build_module_closures(module_list)
    assert closures["a"] == {"b", "c"}
    assert closures["b"] == {"c"}
    assert closures["c"] == set()


def test_callee_more_basic_layer_marked_python_and_not_expanded():
    """callee 比 caller 更基礎的層（如 service 呼叫 repository）→
    language="python"，不繼續展開——即使呼叫圖裡這個 callee 還有自己的
    下游呼叫，也不該出現在 reference_targets 裡。"""
    call_graph = {
        "Service.java::UserService::getUser": ["Repo.java::UserRepository::findById"],
        "Repo.java::UserRepository::findById": ["ShouldNotAppear.java::X::y"],
    }
    java_index = {
        "Repo.java::UserRepository::findById": _entry(
            "app/repositories/user_repository.py", "UserRepository", "find_by_id", 1
        ),
    }
    targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id="Service.java::UserService::getUser",
        seed_module="user", seed_layer="services",
        call_graph=call_graph, java_index=java_index,
        module_names=_MODULE_NAMES, module_closures={"user": set()},
    )
    assert truncated is False
    assert targets == [
        {
            "file_path": "app/repositories/user_repository.py",
            "class_name": "UserRepository",
            "function_name": "find_by_id",
            "language": "python",
        }
    ]


def test_router_calling_utils_function_marked_python_not_java():
    """對應 docs/refactor_bug_trace.md #23 真實案例：utils 是 Phase 1
    （跟 repositories 同一階，見 design_agent/layout.py::_PHASE_1_LAYERS），
    router／service 呼叫 utils 函式時保證那個函式早就翻譯完成，應該跟
    「service 呼叫 repository」同一種處理，language="python"。修好之前
    `LAYER_RANK["utils"]`（3）比 routers（2）還大，這個判斷式恆為假，
    utils 呼叫一律被誤判成「java」，reference_targets 給模型看的是 Java
    原始碼而不是已翻譯的 Python 模組層級函式——真實案例：`ExamController.
    getAll()` 呼叫 `ValidationUtil.isValidField(...)`，模型因此照 Java
    的 `ClassName.staticMethod()` 寫法猜成 `from app.utils.validation_util
    import ValidationUtil`，但 Python 端 utils 是模組層級函式、沒有這個
    class，執行期 `ImportError`。"""
    call_graph = {"Controller.java::ExamController::getAll": ["Util.java::ValidationUtil::isValidField"]}
    java_index = {
        "Util.java::ValidationUtil::isValidField": _entry(
            "app/utils/validation_util.py", None, "is_valid_field", 1
        ),
    }
    targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id="Controller.java::ExamController::getAll",
        seed_module="exam", seed_layer="routers",
        call_graph=call_graph, java_index=java_index,
        module_names=_MODULE_NAMES, module_closures={"exam": set()},
    )
    assert truncated is False
    assert targets == [
        {
            "file_path": "app/utils/validation_util.py",
            "class_name": None,
            "function_name": "is_valid_field",
            "language": "python",
        }
    ]


def test_same_module_same_layer_marked_java_and_expanded():
    """同 module、同層呼叫（沒有任何機制保證完成順序）→ language="java"，
    繼續展開它自己的呼叫對象。"""
    call_graph = {
        "A.java::ExamService::foo": ["B.java::AnswerService::bar"],
        "B.java::AnswerService::bar": ["C.java::ExamRepository::baz"],
    }
    java_index = {
        "B.java::AnswerService::bar": _entry("app/services/exam_service.py", "AnswerService", "bar", 2),
        "C.java::ExamRepository::baz": _entry("app/repositories/exam_repository.py", "ExamRepository", "baz", 1),
    }
    targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id="A.java::ExamService::foo",
        seed_module="exam", seed_layer="services",
        call_graph=call_graph, java_index=java_index,
        module_names=frozenset({"exam"}), module_closures={"exam": set()},
    )
    assert truncated is False
    by_function = {t["function_name"]: t for t in targets}
    # 同 module 同層：java，且有繼續展開，找到它下游的 repository（更
    # 基礎的層，python，不再繼續展開）。
    assert by_function["bar"]["language"] == "java"
    assert by_function["baz"]["language"] == "python"


def test_java_language_target_uses_java_names_not_python_projection():
    """language="java" 的項目必須是①呼叫圖 method_id 拆出來的 Java 原始
    座標（camelCase 方法名、Java 檔案路徑），不能沿用 java_index entry
    的 Python 投影（snake_case、.py 路徑）——⑤（09a）要拿這個座標去解析
    真實 .java 檔案，兩邊命名空間不同（見 graph/state.py::ReferenceTarget
    docstring）。這裡刻意讓 java_index 的 Python 投影跟 Java 原始名稱不同
    （camelCase vs snake_case），確保測試真的會在退回舊行為時失敗。
    """
    call_graph = {
        "A.java::ExamService::foo": ["B.java::AnswerService::getUserAnswer"],
    }
    java_index = {
        "B.java::AnswerService::getUserAnswer": _entry(
            "app/services/exam_service.py", "AnswerService", "get_user_answer", 2
        ),
    }
    targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id="A.java::ExamService::foo",
        seed_module="exam", seed_layer="services",
        call_graph=call_graph, java_index=java_index,
        module_names=frozenset({"exam"}), module_closures={"exam": set()},
    )
    assert truncated is False
    assert targets == [
        {
            "file_path": "B.java",
            "class_name": "AnswerService",
            "function_name": "getUserAnswer",
            "language": "java",
        }
    ]


def test_cross_module_same_layer_with_dependency_marked_python():
    """跨 module 同層呼叫，且 caller 的 module 依賴 callee 的 module
    （既有 module 依賴排程保證）→ language="python"，不繼續展開。"""
    call_graph = {"A.java::OrderService::foo": ["B.java::UserService::bar"]}
    java_index = {
        "B.java::UserService::bar": _entry("app/services/user_service.py", "UserService", "bar", 2),
    }
    targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id="A.java::OrderService::foo",
        seed_module="order", seed_layer="services",
        call_graph=call_graph, java_index=java_index,
        module_names=_MODULE_NAMES, module_closures={"order": {"user"}},
    )
    assert truncated is False
    assert targets[0]["language"] == "python"


def test_cross_module_same_layer_without_dependency_marked_java():
    """跨 module 同層呼叫，但兩個 module 之間沒有依賴關係 → 沒有任何
    機制保證順序，退回跟同 module 同層一樣的處理（language="java"）。"""
    call_graph = {"A.java::OrderService::foo": ["B.java::UserService::bar"]}
    java_index = {
        "B.java::UserService::bar": _entry("app/services/user_service.py", "UserService", "bar", 2),
    }
    targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id="A.java::OrderService::foo",
        seed_module="order", seed_layer="services",
        call_graph=call_graph, java_index=java_index,
        module_names=_MODULE_NAMES, module_closures={"order": set()},  # 無依賴關係
    )
    assert truncated is False
    assert targets[0]["language"] == "java"


def test_callee_not_in_java_index_is_skipped():
    """查不到（entity 欄位存取等）→ 不產生 reference_targets 項目，也
    不繼續展開，見六章「查不到」說明。"""
    call_graph = {"A.java::UserService::foo": ["Entity.java::User::getName"]}
    targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id="A.java::UserService::foo",
        seed_module="user", seed_layer="services",
        call_graph=call_graph, java_index={},
        module_names=_MODULE_NAMES, module_closures={"user": set()},
    )
    assert targets == []
    assert truncated is False


def test_self_recursion_is_skipped_not_treated_as_reference():
    """方法呼叫自己（直接遞迴）不產生 reference_targets 項目——⑤翻譯
    當下本來就看得到自己的簽名，見六章演算法步驟 3。"""
    call_graph = {"A.java::UserService::foo": ["A.java::UserService::foo"]}
    targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id="A.java::UserService::foo",
        seed_module="user", seed_layer="services",
        call_graph=call_graph, java_index={"A.java::UserService::foo": _entry("x.py", "X", "foo", 2)},
        module_names=_MODULE_NAMES, module_closures={"user": set()},
    )
    assert targets == []
    assert truncated is False


def test_cycle_does_not_infinite_loop():
    """同 module 同層兩個方法互相呼叫（環）——visited 集合要能防止無窮
    迴圈，兩者都應該各自出現一次，不是各自反覆出現。"""
    call_graph = {
        "A.java::ServiceA::foo": ["B.java::ServiceB::bar"],
        "B.java::ServiceB::bar": ["A.java::ServiceA::foo"],
    }
    java_index = {
        "A.java::ServiceA::foo": _entry("app/services/exam_service.py", "ServiceA", "foo", 2),
        "B.java::ServiceB::bar": _entry("app/services/exam_service.py", "ServiceB", "bar", 2),
    }
    targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id="A.java::ServiceA::foo",
        seed_module="exam", seed_layer="services",
        call_graph=call_graph, java_index=java_index,
        module_names=frozenset({"exam"}), module_closures={"exam": set()},
    )
    assert truncated is False
    assert [t["function_name"] for t in targets] == ["bar"]  # 只有 bar，foo 是種子自己，不會出現在清單裡


def test_truncation_stops_bfs_and_reports_truncated(monkeypatch):
    """超過 PLAN_AGENT_MAX_REFERENCE_TARGETS 上限時立即停止整個 BFS，
    回傳 truncated=True；BFS 保證越早加入的越淺層，見六章「上限」。"""
    monkeypatch.setattr(call_chain, "MAX_REFERENCE_TARGETS", 2)

    call_graph = {"seed": ["c1", "c2", "c3", "c4"]}
    java_index = {
        f"c{i}": _entry("app/repositories/user_repository.py", f"R{i}", "m", 1) for i in range(1, 5)
    }
    targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id="seed",
        seed_module="user", seed_layer="services",
        call_graph=call_graph, java_index=java_index,
        module_names=_MODULE_NAMES, module_closures={"user": set()},
    )
    assert truncated is True
    assert len(targets) == 2


def test_no_truncation_when_under_limit():
    call_graph = {"seed": ["c1"]}
    java_index = {"c1": _entry("app/repositories/user_repository.py", "R", "m", 1)}
    targets, truncated = call_chain.build_reference_targets(
        seed_java_method_id="seed",
        seed_module="user", seed_layer="services",
        call_graph=call_graph, java_index=java_index,
        module_names=_MODULE_NAMES, module_closures={"user": set()},
    )
    assert truncated is False
    assert len(targets) == 1
