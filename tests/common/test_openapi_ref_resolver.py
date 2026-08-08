"""openapi_ref_resolver.py 的假資料單元測試。原為 spec_collection_agent
內部各自實作的測試（見 docs/03c_collection_agent_code.md），[B] Collection
Agent 遷移至共用實作後一併搬到這裡，見 00 六章「OpenAPI `$ref` 展開
（共用工具）」、05a 十三章已解決事項。
"""
from common.openapi_ref_resolver import resolve_refs


class TestResolveRefs:
    def _spec(self, schemas: dict) -> dict:
        return {"components": {"schemas": schemas}}

    def test_simple_ref_resolved_to_schema_body(self):
        spec = self._spec({
            "SaveScoreRq": {"type": "object", "properties": {"id": {"type": "string"}}}
        })
        obj = {"schema": {"$ref": "#/components/schemas/SaveScoreRq"}}
        resolved = resolve_refs(obj, spec)
        assert resolved == {
            "schema": {"type": "object", "properties": {"id": {"type": "string"}}}
        }

    def test_ref_inside_array_items(self):
        spec = self._spec({"Foo": {"type": "object", "properties": {"x": {"type": "string"}}}})
        obj = {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Foo"}}}
        resolved = resolve_refs(obj, spec)
        assert resolved["schema"]["items"] == {
            "type": "object", "properties": {"x": {"type": "string"}}
        }

    def test_nested_ref_resolved_recursively(self):
        spec = self._spec({
            "Outer": {"type": "object", "properties": {"inner": {"$ref": "#/components/schemas/Inner"}}},
            "Inner": {"type": "object", "properties": {"id": {"type": "string"}}},
        })
        obj = {"$ref": "#/components/schemas/Outer"}
        resolved = resolve_refs(obj, spec)
        assert resolved == {
            "type": "object",
            "properties": {"inner": {"type": "object", "properties": {"id": {"type": "string"}}}},
        }

    def test_circular_ref_stops_and_marks(self):
        spec = self._spec({
            "A": {"type": "object", "properties": {"b": {"$ref": "#/components/schemas/B"}}},
            "B": {"type": "object", "properties": {"a": {"$ref": "#/components/schemas/A"}}},
        })
        obj = {"$ref": "#/components/schemas/A"}
        resolved = resolve_refs(obj, spec)
        # A -> B -> A：第二次碰到 A 時應該停止，不要無限遞迴（不 assert 完整
        # 結構，只要能跑完、且在某處出現 _circular 標記即可）。
        assert resolved["properties"]["b"]["properties"]["a"].get("_circular") is True

    def test_unresolvable_ref_kept_as_is(self):
        spec = self._spec({})
        obj = {"schema": {"$ref": "#/components/schemas/DoesNotExist"}}
        resolved = resolve_refs(obj, spec)
        assert resolved == obj  # 找不到就原樣保留，不拋例外

    def test_non_ref_dict_and_list_pass_through(self):
        spec = self._spec({})
        obj = {"a": [1, 2, {"b": "c"}], "d": None}
        assert resolve_refs(obj, spec) == obj

    def test_scalar_passthrough(self):
        spec = self._spec({})
        assert resolve_refs("plain string", spec) == "plain string"
        assert resolve_refs(42, spec) == 42
