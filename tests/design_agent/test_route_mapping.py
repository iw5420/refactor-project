"""對應 docs/09b_bug_trace.md #64：`build_route_to_module_mapping()` 是
新拆出來的獨立函式，之前完全沒有專屬測試（`build_route_mappings()` 也是，
一併補上，之前只靠真實環境跑過才會被間接涵蓋）。"""
from design_agent.route_mapping import build_route_mappings, build_route_to_module_mapping


def _api(endpoint: str, http_method: str, module: str) -> dict:
    return {
        "endpoint": endpoint,
        "http_method": http_method,
        "java_controller": "SomeController.someMethod",
        "module": module,
    }


class TestBuildRouteToModuleMapping:
    def test_maps_normalized_key_to_module(self):
        result = build_route_to_module_mapping([
            _api("/api/general/language", "GET", "school"),
            _api("/api/exam/search", "POST", "exam"),
        ])
        assert result == {
            "GET_api_general_language": "school",
            "POST_api_exam_search": "exam",
        }

    def test_path_param_normalized_to_id_placeholder(self):
        result = build_route_to_module_mapping([_api("/api/users/{userId}", "GET", "user")])
        assert result == {"GET_api_users_{id}": "user"}

    def test_empty_input_returns_empty_mapping(self):
        assert build_route_to_module_mapping([]) == {}


class TestBuildRouteMappingsModuleMappingConsistency:
    """對應 09b #64：`build_route_mappings()` 拆分後，`route_to_module_
    mapping` 這一半改成委派給 `build_route_to_module_mapping()`——這裡
    確認拆分沒有改變既有行為，兩個函式對同一份輸入算出的 module_mapping
    完全一致。"""

    def test_route_to_module_mapping_matches_standalone_function(self):
        api_to_python_target = [
            _api("/api/general/language", "GET", "school"),
            _api("/api/exam/search", "POST", "exam"),
        ]
        _, module_mapping = build_route_mappings(
            api_to_python_target, interfaces=[], modules_with_schema_file=set()
        )
        assert module_mapping == build_route_to_module_mapping(api_to_python_target)
