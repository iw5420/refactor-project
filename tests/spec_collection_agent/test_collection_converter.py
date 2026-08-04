"""collection_converter.py 的假資料單元測試（見 docs/03c_collection_agent_code.md 五章）。"""
import pytest

from spec_collection_agent import collection_converter
from spec_collection_agent.collection_converter import (
    convert_openapi_to_postman,
    split_readonly_mutation,
)


def _leaf(method: str, name: str) -> dict:
    return {"name": name, "request": {"method": method, "url": {"path": [name]}}}


class TestSplitReadonlyMutation:
    def test_splits_by_method_and_keeps_folder_structure(self):
        collection = {
            "info": {"name": "api"},
            "item": [
                {
                    "name": "users",
                    "item": [_leaf("GET", "get_user"), _leaf("POST", "create_user")],
                },
                _leaf("DELETE", "delete_thing"),
            ],
        }
        readonly, mutation = split_readonly_mutation(collection)

        assert readonly["item"][0]["name"] == "users"
        assert [i["name"] for i in readonly["item"][0]["item"]] == ["get_user"]

        assert any(f["name"] == "users" for f in mutation["item"])

    def test_empty_folder_dropped_after_filtering(self):
        collection = {
            "info": {"name": "api"},
            "item": [{"name": "only-gets", "item": [_leaf("GET", "get_x")]}],
        }
        _readonly, mutation = split_readonly_mutation(collection)
        assert mutation["item"] == []


class TestConvertOpenapiToPostman:
    def test_npx_not_found_raises_clear_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(collection_converter.shutil, "which", lambda cmd: None)
        with pytest.raises(FileNotFoundError, match="npx"):
            convert_openapi_to_postman(tmp_path / "openapi.json", tmp_path / "out.json")
