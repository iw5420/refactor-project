"""對應 docs/09b_bug_trace.md #64：`RouteMapper` 之前完全沒有專屬測試
檔案。這裡只補新加的 `module_mapping_override` 行為——其餘既有行為
（`resolve_related_files()` 的精確/前綴匹配）已經被 `test_comparator.py`
`test_mutation_verifier.py` 間接涵蓋，不重複補。"""
import yaml

from refactor_harness.core.route_mapper import RouteMapper


def _write_harness_yaml(tmp_path, module_mapping: dict[str, str]) -> str:
    config_path = tmp_path / "harness.yaml"
    config_path.write_text(
        yaml.safe_dump({"route_to_module_mapping": module_mapping}), encoding="utf-8"
    )
    return str(config_path)


class TestModuleMappingOverride:
    def test_override_takes_precedence_over_file(self, tmp_path):
        """對應 09b #64：`record_tests`／`run_tests` 平行分支下，檔案裡的
        版本可能是這輪還沒被 ③ 更新過的舊版——傳了 override 就不該再讀
        檔案內容，避免這個競態。"""
        config_path = _write_harness_yaml(tmp_path, {"GET_api_general_language": "general"})
        mapper = RouteMapper(
            config_path, module_mapping_override={"GET_api_general_language": "school"}
        )
        assert mapper.resolve_module("GET", ["api", "general", "language"]) == "school"

    def test_none_override_falls_back_to_file(self, tmp_path):
        config_path = _write_harness_yaml(tmp_path, {"GET_api_general_language": "general"})
        mapper = RouteMapper(config_path, module_mapping_override=None)
        assert mapper.resolve_module("GET", ["api", "general", "language"]) == "general"

    def test_default_omitted_override_falls_back_to_file(self, tmp_path):
        """呼叫端（如 partial_verify.py）完全沒有 state 可用時，連參數
        都不會傳，必須維持既有的純檔案行為，不能要求呼叫端一定要顯式
        傳 None。"""
        config_path = _write_harness_yaml(tmp_path, {"GET_api_general_language": "general"})
        mapper = RouteMapper(config_path)
        assert mapper.resolve_module("GET", ["api", "general", "language"]) == "general"

    def test_empty_dict_override_is_respected_not_treated_as_falsy_none(self, tmp_path):
        """`{}` 是合法的「這次真的沒有任何 API 端點」情境，不該被誤判成
        「沒有傳 override」而跌回檔案內容——用 `is not None` 判斷，不是
        `if module_mapping_override`。"""
        config_path = _write_harness_yaml(tmp_path, {"GET_api_general_language": "general"})
        mapper = RouteMapper(config_path, module_mapping_override={})
        assert mapper.module_mapping == {}
