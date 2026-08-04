"""chain_dependency_detect.py 的假資料單元測試（見 docs/03c_collection_agent_code.md 五章）。
LLM 呼叫全部 monkeypatch 掉，不連真實 Claude API。
"""
from spec_collection_agent import chain_dependency_detect as cdd
from spec_collection_agent.chain_dependency_detect import (
    _chunk_operations,
    group_operations_by_tag,
    _map_analyze_group,
    _map_phase,
    detect_chain_dependencies,
)
from spec_collection_agent.exceptions import ChainDependencyDetectionError


def _spec(paths: dict) -> dict:
    return {"paths": paths}


class TestGroupOperationsByTag:
    def test_multi_tag_uses_first(self):
        spec = _spec({"/a": {"get": {"tags": ["Users", "Other"]}}})
        groups = group_operations_by_tag(spec)
        assert "Users" in groups
        assert "Other" not in groups

    def test_no_tag_goes_to_untagged(self):
        spec = _spec({"/a": {"get": {}}})
        groups = group_operations_by_tag(spec)
        assert "_untagged" in groups


class TestChunkOperations:
    def test_splits_when_exceeding_char_budget(self, monkeypatch):
        monkeypatch.setattr(cdd, "_MAX_CHARS_PER_MAP_CHUNK", 10)
        ops = [("/a", "GET", {"x": "y" * 20}), ("/b", "GET", {"x": "y" * 20})]
        chunks = _chunk_operations(ops)
        assert len(chunks) == 2

    def test_single_oversized_operation_still_one_chunk(self, monkeypatch):
        monkeypatch.setattr(cdd, "_MAX_CHARS_PER_MAP_CHUNK", 5)
        ops = [("/a", "GET", {"x": "y" * 50})]
        chunks = _chunk_operations(ops)
        assert len(chunks) == 1

    def test_small_operations_not_over_split(self):
        ops = [("/a", "GET", {"x": 1}), ("/b", "GET", {"x": 2})]
        chunks = _chunk_operations(ops)
        assert len(chunks) == 1


class TestMapAnalyzeGroup:
    def test_well_formed_candidates_parsed_directly(self, monkeypatch):
        # output_config.format（見 llm.py）已經保證回傳結構合法，不需要
        # 再逐筆檢查格式、丟棄不合法的候選——這裡驗證正常情況能正確轉成
        # _CandidateProducer/_CandidateConsumer。
        monkeypatch.setattr(
            cdd, "call_claude_for_json",
            lambda **kw: {
                "candidate_producers": [{"endpoint": "POST /a", "field_path": "id", "hint": "x"}],
                "candidate_consumers": [
                    {"endpoint": "GET /a/{id}", "param_name": "id", "param_location": "path"}
                ],
            },
        )
        producers, consumers = _map_analyze_group("tag", [("/a", "POST", {})])
        assert len(producers) == 1
        assert producers[0].endpoint == "POST /a"
        assert len(consumers) == 1
        assert consumers[0].param_location == "path"
        assert consumers[0].hint == ""  # 沒給 hint 時預設空字串

    def test_call_failure_raises_detection_error(self, monkeypatch):
        from common.llm_client import LlmJsonError

        def _raise(**kw):
            raise LlmJsonError("boom")

        monkeypatch.setattr(cdd, "call_claude_for_json", _raise)
        try:
            _map_analyze_group("tag", [("/a", "POST", {})])
            assert False, "should have raised"
        except ChainDependencyDetectionError:
            pass


class TestMapPhaseCancellation:
    def test_failure_stops_pending_calls(self, monkeypatch):
        call_count = {"n": 0}

        def _fake(**kw):
            call_count["n"] += 1
            raise ChainDependencyDetectionError("boom")

        monkeypatch.setattr(cdd, "call_claude_for_json", _fake)
        monkeypatch.setattr(cdd, "_MAX_MAP_WORKERS", 1)

        spec = _spec({
            f"/t{i}": {"get": {"tags": [f"tag{i}"]}} for i in range(5)
        })
        try:
            _map_phase(spec)
            assert False, "should have raised"
        except ChainDependencyDetectionError:
            pass
        # worker=1 序列執行，失敗後應取消剩餘尚未開始的呼叫
        assert call_count["n"] < 5


class TestDetectChainDependenciesSkipsReduce:
    def test_empty_producers_skips_reduce(self, monkeypatch):
        monkeypatch.setattr(cdd, "_map_phase", lambda spec: ([], []))

        def _fail_reduce(*a, **kw):
            raise AssertionError("不應呼叫 reduce")

        monkeypatch.setattr(cdd, "_reduce_phase", _fail_reduce)
        assert detect_chain_dependencies({"paths": {}}) == []
