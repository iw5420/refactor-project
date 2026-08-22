# tests/common/test_run_context.py
"""common/run_context.py，對應 11a_logging_architecture.md 六章。"""
import re

from common.run_context import adhoc_run_id, new_run_id

_RUN_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{6}$")


def test_new_run_id_matches_expected_format():
    assert _RUN_ID_RE.match(new_run_id())


def test_new_run_id_is_unique_across_calls():
    assert new_run_id() != new_run_id()


def test_adhoc_run_id_has_distinct_prefix():
    # 前綴必須跟 new_run_id() 的格式明確不同，讓「漏傳 run_id 的 bug」跟
    # 「刻意的旁路呼叫」在查詢結果裡都能一眼認出來（見六章）。
    value = adhoc_run_id()
    assert value.startswith("adhoc_")
    assert _RUN_ID_RE.match(value[len("adhoc_"):])
