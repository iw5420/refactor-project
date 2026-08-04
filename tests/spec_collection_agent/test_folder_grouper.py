"""folder_grouper.py 的假資料單元測試（見 docs/03c_collection_agent_code.md 五章）。
LLM 呼叫全部 monkeypatch 掉，不連真實 Claude API。
"""
from spec_collection_agent import folder_grouper
from spec_collection_agent.folder_grouper import (
    _extract_singleton_folders,
    _group_by_chain_dependencies,
    _group_singletons,
    _mechanical_group_name,
    group_mutation_folders,
)
from spec_collection_agent.types import ChainDependency


def _leaf(method: str, path_segments: list) -> dict:
    return {
        "name": f"{method} {'/'.join(path_segments)}",
        "request": {"method": method, "url": {"path": path_segments}},
    }


class TestGroupByChainDependencies:
    def test_dependent_items_grouped_together(self):
        producer = _leaf("POST", ["users"])
        consumer = _leaf("DELETE", ["users", ":id"])
        independent = _leaf("POST", ["orders"])
        items = [producer, consumer, independent]

        dep = ChainDependency(
            producer_endpoint="POST /users",
            producer_field="data.id",
            consumer_endpoint="DELETE /users/{id}",
            consumer_param="id",
            env_var_name="created_user_id",
        )
        mandatory, singletons = _group_by_chain_dependencies(items, [dep])

        assert len(mandatory) == 1
        assert set(id(i) for i in mandatory[0]) == {id(producer), id(consumer)}
        assert singletons == [independent]


class TestMechanicalGroupName:
    def test_dedups_and_labels_resources(self):
        group = [_leaf("POST", ["users"]), _leaf("DELETE", ["users", ":id"])]
        name = _mechanical_group_name(group)
        assert "users" in name
        assert name.endswith("_flow")

    def test_no_resource_name_falls_back_to_default(self):
        group = [_leaf("POST", ["{id}"])]
        assert _mechanical_group_name(group) == "business_flow"


class TestExtractSingletonFolders:
    def test_llm_output_mismatched_endpoints_falls_back(self):
        result = _extract_singleton_folders(
            {"singleton_folders": [{"folder_name": "x", "endpoints": ["GET /a"]}]},
            singleton_endpoints=["GET /a", "GET /b"],
        )
        assert result == [("GET /a", ["GET /a"]), ("GET /b", ["GET /b"])]

    def test_llm_output_used_when_consistent(self):
        result = _extract_singleton_folders(
            {"singleton_folders": [{"folder_name": "combo", "endpoints": ["GET /a", "GET /b"]}]},
            singleton_endpoints=["GET /a", "GET /b"],
        )
        assert result == [("combo", ["GET /a", "GET /b"])]

    def test_none_output_falls_back(self):
        result = _extract_singleton_folders(None, singleton_endpoints=["GET /a"])
        assert result == [("GET /a", ["GET /a"])]


class TestGroupSingletons:
    def test_empty_input_skips_llm_call(self, monkeypatch):
        def _fail(*args, **kwargs):
            raise AssertionError("不應該呼叫 LLM")

        monkeypatch.setattr(folder_grouper, "call_claude_for_json", _fail)
        assert _group_singletons([]) == []

    def test_non_empty_input_calls_llm(self, monkeypatch):
        captured = {}

        def _fake_call(*, system_prompt, user_prompt, schema, model):
            captured["user_prompt"] = user_prompt
            captured["schema"] = schema
            return {"singleton_folders": [{"folder_name": "x", "endpoints": ["GET /a"]}]}

        monkeypatch.setattr(folder_grouper, "call_claude_for_json", _fake_call)
        result = _group_singletons(["GET /a"])
        assert result == [("x", ["GET /a"])]
        assert "GET /a" in captured["user_prompt"]
        assert captured["schema"] is not None


class TestGroupMutationFolders:
    def test_mandatory_group_does_not_call_llm_for_naming(self, monkeypatch):
        def _fail(*args, **kwargs):
            raise AssertionError("mandatory group 命名不應呼叫 LLM")

        monkeypatch.setattr(folder_grouper, "call_claude_for_json", _fail)

        producer = _leaf("POST", ["users"])
        consumer = _leaf("DELETE", ["users", ":id"])
        mutation = {"item": [producer, consumer]}
        dep = ChainDependency(
            producer_endpoint="POST /users",
            producer_field="data.id",
            consumer_endpoint="DELETE /users/{id}",
            consumer_param="id",
            env_var_name="created_user_id",
        )
        regrouped = group_mutation_folders(mutation=mutation, chain_dependencies=[dep])
        assert len(regrouped["item"]) == 1
        assert len(regrouped["item"][0]["item"]) == 2
