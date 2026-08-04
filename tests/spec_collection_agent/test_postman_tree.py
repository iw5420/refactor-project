"""postman_tree.py 的假資料單元測試（見 docs/03c_collection_agent_code.md 五章）。"""
import pytest

from spec_collection_agent.postman_tree import (
    find_containing_top_level_folder,
    find_item,
    get_nested_value,
    has_nested_key,
    is_folder,
    item_method,
    iter_leaf_items,
    normalized_path_from_item,
    set_nested_value,
)


def _get_item(method: str, path_segments: list) -> dict:
    return {
        "name": f"{method} {'/'.join(str(s) for s in path_segments)}",
        "request": {"method": method, "url": {"path": path_segments}},
    }


class TestItemMethodAndFolder:
    def test_item_method_returns_upper(self):
        item = {"request": {"method": "get"}}
        assert item_method(item) == "GET"

    def test_item_method_none_for_folder(self):
        assert item_method({"item": []}) is None

    def test_is_folder_true_when_no_request(self):
        assert is_folder({"item": []}) is True

    def test_is_folder_false_for_leaf(self):
        assert is_folder(_get_item("GET", ["users"])) is False


class TestNormalizedPath:
    def test_colon_segment_converted_to_brace(self):
        item = _get_item("GET", ["api", "v1", "users", ":id"])
        assert normalized_path_from_item(item) == "/api/v1/users/{id}"

    def test_dict_segment_uses_value_key(self):
        item = {"request": {"method": "GET", "url": {"path": ["users", {"value": ":id", "type": "any"}]}}}
        assert normalized_path_from_item(item) == "/users/{id}"

    def test_non_string_non_dict_segment_becomes_empty(self):
        item = {"request": {"method": "GET", "url": {"path": ["users", 123]}}}
        assert normalized_path_from_item(item) == "/users/"

    def test_no_url_returns_none(self):
        assert normalized_path_from_item({"request": {"method": "GET"}}) is None


class TestIterLeafAndFind:
    def test_iter_leaf_items_recurses_into_folders(self):
        tree = [
            {
                "name": "folder",
                "item": [_get_item("GET", ["a"]), _get_item("POST", ["b"])],
            },
            _get_item("DELETE", ["c"]),
        ]
        leaves = list(iter_leaf_items(tree))
        assert len(leaves) == 3

    def test_find_item_matches_method_and_path(self):
        tree = [_get_item("GET", ["users", ":id"])]
        found = find_item(tree, "GET", "/users/{id}")
        assert found is not None
        assert find_item(tree, "POST", "/users/{id}") is None

    def test_find_containing_top_level_folder_uses_identity(self):
        leaf_a = _get_item("GET", ["a"])
        leaf_b = _get_item("GET", ["b"])  # same shape as duplicate content
        tree = [{"name": "folder1", "item": [leaf_a]}, {"name": "folder2", "item": [leaf_b]}]
        assert find_containing_top_level_folder(tree, leaf_a)["name"] == "folder1"
        assert find_containing_top_level_folder(tree, leaf_b)["name"] == "folder2"

    def test_find_containing_top_level_folder_not_found(self):
        stray = _get_item("GET", ["z"])
        tree = [{"name": "folder1", "item": [_get_item("GET", ["a"])]}]
        assert find_containing_top_level_folder(tree, stray) is None


class TestNestedKeyTools:
    def test_has_nested_key_true(self):
        assert has_nested_key({"user": {"id": 1}}, "user.id") is True

    def test_has_nested_key_false_when_intermediate_not_dict(self):
        assert has_nested_key({"user": [1, 2]}, "user.id") is False

    def test_get_nested_value_raises_keyerror_when_missing(self):
        with pytest.raises(KeyError):
            get_nested_value({"user": {}}, "user.id")

    def test_set_nested_value_creates_intermediate_dicts(self):
        obj = {}
        set_nested_value(obj, "user.id", 42)
        assert obj == {"user": {"id": 42}}

    def test_set_nested_value_preserves_siblings(self):
        obj = {"user": {"id": 0, "name": "placeholder"}}
        set_nested_value(obj, "user.id", 99)
        assert obj == {"user": {"id": 99, "name": "placeholder"}}

    def test_set_nested_value_conflict_raises_typeerror(self):
        obj = {"user": [1, 2]}
        with pytest.raises(TypeError):
            set_nested_value(obj, "user.id", 1)

    def test_set_nested_value_empty_key_raises(self):
        with pytest.raises(ValueError):
            set_nested_value({}, "", 1)
