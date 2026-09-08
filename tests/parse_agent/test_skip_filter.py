"""parse_agent/skip_filter.py::compute_skip_excluded_overloads()。

對應 docs/refactor_bug_trace.md #8：使用者填 skip，是人工判斷「這個
endpoint 整段不進翻譯流程」，不只是跳過自動化測試（見
docs/03a_spec_collection_agent_architecture.md「Decision.SKIP 的語意」）。
同名不同 HTTP method 的多載（如 FileController 的 voice/image）在
method_id 層級會共用同一個 id，這裡改用 HTTP method 精確的
(class_name, method_name, http_method) 三元組排除，讓 skip 只命中真正
該排除的那一個多載。
"""
from parse_agent.skip_filter import compute_skip_excluded_overloads


def test_skip_endpoint_excludes_only_its_own_http_method():
    # FileController 的 image：POST 版被使用者標 skip，GET 版沒有——
    # 兩者共用同一個 method_id，只有 HTTP method 精確排除才能只挑出 POST。
    route_index = {
        "POST /api/file/image": ["FileController.java::FileController::image"],
        "GET /api/file/image": ["FileController.java::FileController::image"],
    }
    result = compute_skip_excluded_overloads(
        skip_endpoints=["POST /api/file/image"],
        route_index=route_index,
    )
    assert result == [("FileController", "image", "POST")]


def test_no_skip_endpoints_returns_empty_list():
    result = compute_skip_excluded_overloads(
        skip_endpoints=[],
        route_index={"GET /api/file/image": ["FileController.java::FileController::image"]},
    )
    assert result == []


def test_skip_endpoint_missing_from_route_index_is_silently_ignored():
    # route_index 沒有這筆對應時（見 _endpoints_to_method_ids() 既有的
    # 「查無對應」warning 機制），這裡不額外拋錯，只是不產生排除項目。
    result = compute_skip_excluded_overloads(
        skip_endpoints=["POST /api/nonexistent"],
        route_index={},
    )
    assert result == []


def test_multiple_method_ids_for_same_endpoint_all_excluded():
    # route_index 的值本身是清單（見既有 "多連、少排除" 原則），一個
    # endpoint 若查到多個 method_id，全部視為要排除的多載。
    route_index = {
        "POST /api/file/image": [
            "FileController.java::FileController::image",
            "FileController.java::FileControllerAlias::image",
        ],
    }
    result = compute_skip_excluded_overloads(
        skip_endpoints=["POST /api/file/image"],
        route_index=route_index,
    )
    assert set(result) == {
        ("FileController", "image", "POST"),
        ("FileControllerAlias", "image", "POST"),
    }
