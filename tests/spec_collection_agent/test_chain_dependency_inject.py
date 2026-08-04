"""chain_dependency_inject.py 的假資料單元測試（見 docs/03c_collection_agent_code.md 五章）。
純程式邏輯，不呼叫 LLM，不需要 mock。
"""
import json

import pytest

from spec_collection_agent.chain_dependency_inject import (
    _is_valid_js_field_path,
    _reorder_top_level_folders,
    _rewrite_param_to_env_var,
    _stable_topological_order,
    inject_chain_scripts,
)
from spec_collection_agent.types import ChainDependency


def _leaf(method: str, path_segments: list, body: dict | None = None) -> dict:
    request: dict = {"method": method, "url": {"path": path_segments, "variable": [], "query": []}}
    if body is not None:
        request["body"] = {"mode": "raw", "raw": json.dumps(body)}
    return {"name": f"{method} {'/'.join(path_segments)}", "request": request}


class TestStableTopologicalOrder:
    def test_producer_before_consumer(self):
        order = _stable_topological_order(3, [(0, 2)])
        assert order.index(0) < order.index(2)

    def test_cycle_raises(self):
        with pytest.raises(ValueError):
            _stable_topological_order(2, [(0, 1), (1, 0)])


class TestReorderTopLevelFolders:
    def test_self_loop_filtered_not_treated_as_cycle(self):
        item = _leaf("GET", ["users", ":id"])
        folder = {"name": "f", "item": [item]}
        # producer_endpoint 與 consumer_endpoint 是同一個 item
        _reorder_top_level_folders([folder], [(folder, item, item)])
        assert folder["item"] == [item]


class TestRewriteParamToEnvVar:
    def test_numeric_body_field_unquoted_placeholder(self):
        item = _leaf("POST", ["orders"], body={"user_id": 0})
        _rewrite_param_to_env_var(item, "user_id", "created_user_id")
        assert '"user_id": {{created_user_id}}' in item["request"]["body"]["raw"]

    def test_string_body_field_keeps_quotes(self):
        item = _leaf("POST", ["orders"], body={"code": "placeholder"})
        _rewrite_param_to_env_var(item, "code", "created_code")
        assert '"code": "{{created_code}}"' in item["request"]["body"]["raw"]

    def test_nested_body_path_rewritten(self):
        item = _leaf("POST", ["orders"], body={"user": {"id": 0, "name": "x"}})
        _rewrite_param_to_env_var(item, "user.id", "created_user_id")
        raw = item["request"]["body"]["raw"]
        # 原值是數值，placeholder 應以不帶引號的形式出現（見函式 docstring
        # 的型別感知規則），因此改寫後的 raw 本身不再是合法 JSON——要等
        # newman 實際替換 {{created_user_id}} 之後才是合法 JSON，這裡直接
        # 比對字串內容，不嘗試 json.loads()。
        assert '"id": {{created_user_id}}' in raw
        assert '"name": "x"' in raw

    def test_url_variable_rewritten(self):
        item = {
            "name": "x",
            "request": {
                "method": "DELETE",
                "url": {"path": ["users", ":id"], "variable": [{"key": "id", "value": "1"}], "query": []},
            },
        }
        _rewrite_param_to_env_var(item, "id", "created_user_id")
        assert item["request"]["url"]["variable"][0]["value"] == "{{created_user_id}}"

    def test_duplicate_query_key_all_rewritten(self):
        # OpenAPI 陣列型 query 參數展開後，同一個 key 可能出現在多個
        # query item（如 ?status=A&status=B）——迴歸測試：舊版只改寫第
        # 一筆找到的就 return，其餘同名 item 留著過期靜態值。
        item = {
            "name": "x",
            "request": {
                "method": "GET",
                "url": {
                    "path": ["items"],
                    "variable": [],
                    "query": [
                        {"key": "status", "value": "A"},
                        {"key": "status", "value": "B"},
                    ],
                },
            },
        }
        _rewrite_param_to_env_var(item, "status", "created_status")
        queries = item["request"]["url"]["query"]
        assert queries[0]["value"] == "{{created_status}}"
        assert queries[1]["value"] == "{{created_status}}"

    def test_missing_nested_path_logs_and_skips(self, caplog):
        item = _leaf("POST", ["orders"], body={"user": {"name": "x"}})
        _rewrite_param_to_env_var(item, "user.id", "created_user_id")
        # 找不到路徑，body 應維持不變
        body_obj = json.loads(item["request"]["body"]["raw"])
        assert body_obj == {"user": {"name": "x"}}

    def test_array_root_body_logs_warning_and_skips(self, caplog):
        # body 根層是 array（manual_fill raw_body 模式常見形狀）：現有
        # 巢狀路徑機制以 object 為前提，無法定位陣列元素內的欄位，應該
        # 記警告並跳過，不是靜默不做事（見函式內對應註解）。
        item = _leaf("POST", ["orders"], body=[{"id": 1}, {"id": 2}])
        with caplog.at_level("WARNING"):
            _rewrite_param_to_env_var(item, "id", "created_id")
        raw = item["request"]["body"]["raw"]
        assert json.loads(raw) == [{"id": 1}, {"id": 2}]  # body 完全沒被動過
        assert "根層不是 JSON object" in caplog.text


class TestIsValidJsFieldPath:
    def test_plain_dotted_path_valid(self):
        assert _is_valid_js_field_path("data.id") is True

    def test_indexed_array_access_valid(self):
        assert _is_valid_js_field_path("data.list[0].id") is True

    def test_bare_brackets_invalid(self):
        assert _is_valid_js_field_path("data.list[].id") is False

    def test_bare_brackets_with_whitespace_invalid(self):
        assert _is_valid_js_field_path("data.list[ ].id") is False


class TestInjectChainScriptsInvalidProducerField:
    def test_invalid_producer_field_syntax_skips_whole_dependency(self, caplog):
        # producer_field 含裸 []（LLM 沒遵守 MAP_SYSTEM_PROMPT 的 [0] 規則
        # 時的防呆）：capture script、consumer 改寫都不該發生，不能只擋
        # 掉腳本那一段、consumer 卻還是被改成引用一個永遠不會被設定的
        # env var。
        producer = _leaf("POST", ["users"], body={"name": "x"})
        producer["event"] = []
        mutation = {"item": [{"name": "user_flow", "item": [producer]}]}
        readonly = {"item": []}

        dep = ChainDependency(
            producer_endpoint="POST /users",
            producer_field="data.list[].id",
            consumer_endpoint="GET /users/{id}",
            consumer_param="id",
            env_var_name="created_user_id",
        )
        new_mutation, _new_readonly = inject_chain_scripts(
            mutation=mutation, readonly=readonly, chain_dependencies=[dep]
        )

        folder = new_mutation["item"][0]
        assert folder["item"][0]["event"] == []  # 沒有被加上 capture script
        assert len(folder["item"]) == 1  # 沒有新增鏈式驗證 GET item
        assert "不合法的裸陣列索引語法" in caplog.text


class TestInjectChainScriptsIntegration:
    def test_get_consumer_added_as_new_item_in_same_folder(self):
        producer = _leaf("POST", ["users"], body={"name": "x"})
        producer["event"] = []
        mutation = {"item": [{"name": "user_flow", "item": [producer]}]}
        readonly_get = _leaf("GET", ["users", ":id"])
        readonly = {"item": [readonly_get]}

        dep = ChainDependency(
            producer_endpoint="POST /users",
            producer_field="data.id",
            consumer_endpoint="GET /users/{id}",
            consumer_param="id",
            env_var_name="created_user_id",
        )
        new_mutation, new_readonly = inject_chain_scripts(
            mutation=mutation, readonly=readonly, chain_dependencies=[dep]
        )

        folder = new_mutation["item"][0]
        assert len(folder["item"]) == 2  # 原本的 POST + 新增的 GET 驗證 item
        # producer 已加上 capture script
        assert folder["item"][0]["event"]
        # readonly 原本的 GET 不受影響（inject_chain_scripts 對 readonly
        # 做 deepcopy 才回傳，identity 不會相同，比對內容即可）
        assert "event" not in readonly_get or readonly_get["event"] == []
        assert new_readonly["item"][0] == readonly_get
