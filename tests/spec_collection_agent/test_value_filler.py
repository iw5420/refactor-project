"""value_filler.py 的假資料單元測試（見 docs/03a 三章「人工填值機制」）。
不呼叫真實 Claude API——填值本身是人工在 manual_fill 模板裡完成的，這裡
只測套用與排除邏輯。
"""
import json

import pytest

from spec_collection_agent import manual_fill
from spec_collection_agent.value_filler import (
    _apply_values_to_item,
    _operation_param_schema,
    _prune_excluded,
    _scalar_param_value,
    apply_manual_fill_to_collections,
    guess_resource_name,
)


class TestGuessResourceName:
    def test_takes_last_static_segment(self):
        assert guess_resource_name("/api/v1/users/{id}") == "users"

    def test_skips_generic_prefixes(self):
        assert guess_resource_name("/api/v2/orders") == "orders"

    def test_no_static_segment_returns_empty(self):
        assert guess_resource_name("/api/v1/{id}") == ""

    def test_rpc_style_path_takes_last_verb_segment(self):
        # 這個專案的真實 endpoint 是動詞式 RPC 路徑，猜出來的就是動詞，
        # 只影響 folder_grouper 產生的可讀名稱，不影響任何功能正確性
        # （見 value_filler.guess_resource_name docstring）。
        assert guess_resource_name("/api/exam/answer/save") == "save"


class TestOperationParamSchema:
    def test_no_params_returns_none(self):
        spec = {"paths": {"/health": {"get": {}}}}
        assert _operation_param_schema(spec, "/health", "GET") is None

    def test_has_request_body_returns_schema(self):
        spec = {
            "paths": {
                "/a": {"post": {"requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}}}
            }
        }
        result = _operation_param_schema(spec, "/a", "POST")
        assert result is not None
        assert "requestBody" in result


class TestApplyValuesToItem:
    def test_writes_nested_body_field_without_overwriting_siblings(self):
        item = {
            "request": {
                "url": {"variable": [], "query": []},
                "body": {"mode": "raw", "raw": json.dumps({"user": {"id": 0, "name": "x"}})},
            }
        }
        _apply_values_to_item(item, {"user.id": 5})
        body_obj = json.loads(item["request"]["body"]["raw"])
        assert body_obj == {"user": {"id": 5, "name": "x"}}

    def test_url_variable_and_query_scalar(self):
        item = {
            "request": {
                "url": {
                    "variable": [{"key": "id", "value": "0"}],
                    "query": [{"key": "status", "value": "0"}],
                }
            }
        }
        _apply_values_to_item(item, {"id": 5, "status": "A"})
        assert item["request"]["url"]["variable"][0]["value"] == "5"
        assert item["request"]["url"]["query"][0]["value"] == "A"

    def test_dict_value_for_url_param_raises(self):
        item = {"request": {"url": {"variable": [{"key": "id", "value": "0"}], "query": []}}}
        with pytest.raises(ValueError):
            _apply_values_to_item(item, {"id": {"nested": "boom"}})

    def test_array_root_body_raises(self):
        item = {
            "request": {
                "url": {"variable": [], "query": []},
                "body": {"mode": "raw", "raw": json.dumps([{"id": 1}])},
            }
        }
        with pytest.raises(ValueError, match="body 根層級不是 JSON object"):
            _apply_values_to_item(item, {"id": 5})

    def test_repeated_query_key_all_updated(self):
        item = {
            "request": {
                "url": {
                    "variable": [],
                    "query": [{"key": "status", "value": "0"}, {"key": "status", "value": "1"}],
                }
            }
        }
        _apply_values_to_item(item, {"status": "A"})
        values = [q["value"] for q in item["request"]["url"]["query"]]
        assert values == ["A", "A"]


class TestScalarParamValue:
    def test_scalar_passthrough_as_string(self):
        assert _scalar_param_value(5, "id") == "5"

    def test_list_raises(self):
        with pytest.raises(ValueError):
            _scalar_param_value([1, 2], "id")


class TestPruneExcluded:
    def test_removes_matching_item_and_empty_folder(self):
        items = [
            {
                "name": "folder",
                "item": [
                    {"name": "keep", "request": {"method": "GET", "url": {"path": ["a"]}}},
                    {"name": "drop", "request": {"method": "POST", "url": {"path": ["b"]}}},
                ],
            }
        ]
        result = _prune_excluded(items, {("POST", "/b")})
        assert len(result[0]["item"]) == 1
        assert result[0]["item"][0]["name"] == "keep"


class TestApplyManualFillToCollections:
    def _spec(self):
        return {
            "paths": {
                "/a": {
                    "post": {"requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}}
                },
                "/health": {"get": {}},
            }
        }

    def _mutation(self):
        return {
            "item": [
                {
                    "name": "post_a",
                    "request": {
                        "method": "POST",
                        "url": {"path": ["a"], "variable": [], "query": []},
                        "body": {"mode": "raw", "raw": "{}"},
                    },
                }
            ]
        }

    def test_no_param_endpoint_untouched(self, tmp_path):
        readonly = {"item": [{"name": "health", "request": {"method": "GET", "url": {"path": ["health"], "variable": [], "query": []}}}]}
        _readonly, _mutation, unfilled = apply_manual_fill_to_collections(
            openapi_spec=self._spec(), readonly=readonly, mutation={"item": []}, manual_fill_dir=tmp_path
        )
        assert unfilled == []
        assert len(_readonly["item"]) == 1

    def test_readonly_get_with_path_param_never_excluded(self, tmp_path):
        # 迴歸測試：readonly 的 GET 帶路徑參數時，manual_fill 從未替它產生
        # 模板（generate_manual_fill_templates() 只對 MUTATION_METHODS 產生），
        # 這個函式不該把它當成「尚未填值」而排除——readonly 原封不動保留
        # （見 03a 三章「Readonly / Mutation 分類規則」）。
        spec = {
            "paths": {
                "/exam/{id}": {
                    "get": {
                        "parameters": [
                            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                        ]
                    }
                }
            }
        }
        readonly = {
            "item": [
                {
                    "name": "get_exam",
                    "request": {
                        "method": "GET",
                        "url": {"path": ["exam", ":id"], "variable": [{"key": "id", "value": "1"}], "query": []},
                    },
                }
            ]
        }
        # 故意不呼叫 manual_fill.generate_manual_fill_templates()，模擬「這個
        # endpoint 從沒有、也不會有 manual_fill 模板」的常態情況。
        _readonly, _mutation, unfilled = apply_manual_fill_to_collections(
            openapi_spec=spec, readonly=readonly, mutation={"item": []}, manual_fill_dir=tmp_path
        )
        assert unfilled == []
        assert len(_readonly["item"]) == 1
        assert _readonly["item"][0]["name"] == "get_exam"

    def test_unresolved_entry_excluded(self, tmp_path):
        manual_fill.generate_manual_fill_templates(openapi_spec=self._spec(), manual_fill_dir=tmp_path)
        _readonly, mutation, unfilled = apply_manual_fill_to_collections(
            openapi_spec=self._spec(), readonly={"item": []}, mutation=self._mutation(), manual_fill_dir=tmp_path
        )
        assert mutation["item"] == []
        assert unfilled == [
            {"endpoint": "POST /a", "category": "retry", "detail": "尚未完成人工填值"}
        ]

    def test_missing_entry_excluded(self, tmp_path):
        # 連模板都沒產生過（例如漏跑階段一）：一樣走排除，不當成例外中止。
        _readonly, mutation, unfilled = apply_manual_fill_to_collections(
            openapi_spec=self._spec(), readonly={"item": []}, mutation=self._mutation(), manual_fill_dir=tmp_path
        )
        assert mutation["item"] == []
        assert unfilled[0]["category"] == "retry"
        assert unfilled[0]["detail"] == "尚未完成人工填值"

    def test_resolved_fields_entry_applied_and_kept(self, tmp_path):
        manual_fill.generate_manual_fill_templates(openapi_spec=self._spec(), manual_fill_dir=tmp_path)
        path = tmp_path / list(tmp_path.glob("*.json"))[0].name
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["endpoints"][0]["values"] = {"foo": "bar"}
        path.write_text(json.dumps(raw), encoding="utf-8")

        _readonly, mutation, unfilled = apply_manual_fill_to_collections(
            openapi_spec=self._spec(), readonly={"item": []}, mutation=self._mutation(), manual_fill_dir=tmp_path
        )
        assert unfilled == []
        assert len(mutation["item"]) == 1
        assert json.loads(mutation["item"][0]["request"]["body"]["raw"]) == {"foo": "bar"}

    def test_skip_decision_excluded_with_skip_reason(self, tmp_path):
        manual_fill.generate_manual_fill_templates(openapi_spec=self._spec(), manual_fill_dir=tmp_path)
        path = tmp_path / list(tmp_path.glob("*.json"))[0].name
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["endpoints"][0]["decision"] = "skip"
        path.write_text(json.dumps(raw), encoding="utf-8")

        _readonly, mutation, unfilled = apply_manual_fill_to_collections(
            openapi_spec=self._spec(), readonly={"item": []}, mutation=self._mutation(), manual_fill_dir=tmp_path
        )
        assert mutation["item"] == []
        assert unfilled == [
            {"endpoint": "POST /a", "category": "skip", "detail": "人工確認排除"}
        ]

    def test_apply_failure_excluded_with_reason(self, tmp_path):
        manual_fill.generate_manual_fill_templates(openapi_spec=self._spec(), manual_fill_dir=tmp_path)
        path = tmp_path / list(tmp_path.glob("*.json"))[0].name
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["endpoints"][0]["values"] = {"id": {"nested": "boom"}}
        path.write_text(json.dumps(raw), encoding="utf-8")

        mutation = self._mutation()
        mutation["item"][0]["request"]["url"]["variable"] = [{"key": "id", "value": "0"}]

        _readonly, mutation_out, unfilled = apply_manual_fill_to_collections(
            openapi_spec=self._spec(), readonly={"item": []}, mutation=mutation, manual_fill_dir=tmp_path
        )
        assert mutation_out["item"] == []
        assert unfilled[0]["category"] == "retry"
        assert "人工補值套用失敗" in unfilled[0]["detail"]

        # 迴歸測試：套用失敗要回寫 last_apply_error 到模板檔案，讓 is_resolved
        # 變回 False，重跑時能被暫停關卡重新抓到（見 03a 三章「套用失敗的
        # 重填機制」）。
        entry = manual_fill.read_entry("POST /a", tmp_path)
        assert entry.last_apply_error is not None
        assert not entry.is_resolved

    def test_apply_success_clears_previous_apply_error(self, tmp_path):
        # 上一輪套用失敗留下的 last_apply_error，這一輪人工修正、套用
        # 成功後要自動清空，不需要人工手動處理這個欄位。
        manual_fill.generate_manual_fill_templates(openapi_spec=self._spec(), manual_fill_dir=tmp_path)
        path = tmp_path / list(tmp_path.glob("*.json"))[0].name
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["endpoints"][0]["values"] = {"foo": "bar"}
        raw["endpoints"][0]["last_apply_error"] = "上一輪的錯誤訊息"
        path.write_text(json.dumps(raw), encoding="utf-8")

        _readonly, mutation, unfilled = apply_manual_fill_to_collections(
            openapi_spec=self._spec(), readonly={"item": []}, mutation=self._mutation(), manual_fill_dir=tmp_path
        )
        assert unfilled == []
        assert len(mutation["item"]) == 1

        entry = manual_fill.read_entry("POST /a", tmp_path)
        assert entry.last_apply_error is None
        assert entry.is_resolved

    def test_templates_never_deleted(self, tmp_path):
        # 舊設計會在解決後刪模板檔，新設計永久保留——apply_manual_fill_
        # to_collections() 完全不該去動 manual_fill_dir 裡的檔案。
        manual_fill.generate_manual_fill_templates(openapi_spec=self._spec(), manual_fill_dir=tmp_path)
        path = tmp_path / list(tmp_path.glob("*.json"))[0].name
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["endpoints"][0]["values"] = {"foo": "bar"}
        path.write_text(json.dumps(raw), encoding="utf-8")

        apply_manual_fill_to_collections(
            openapi_spec=self._spec(), readonly={"item": []}, mutation=self._mutation(), manual_fill_dir=tmp_path
        )
        assert path.exists()
