"""common/java_annotations.py 的等價性測試。

DATA_CLASS_ANNOTATIONS 是從 parse_agent/grouping.py 原本的本地常數
_DATA_ANNOTATIONS 搬過來的（見該檔案改動註解），這裡鎖定搬移前後的集合
成員逐一相等，證明這是純重構、不影響 04a needs_llm_summary() 既有的
分類結果。
"""
from common.java_annotations import (
    DATA_CLASS_ANNOTATIONS,
    JPA_ENTITY_ANNOTATIONS,
    LOMBOK_DATA_ANNOTATIONS,
)

# 搬移前 parse_agent/grouping.py 的字面常數，逐一核對用，不從程式碼匯入
# （若從程式碼匯入，搬移後兩邊永遠相等，測試會失去意義）。
_ORIGINAL_DATA_ANNOTATIONS = {
    "Entity", "Embeddable", "MappedSuperclass",
    "Data", "Value", "Getter", "Setter", "Builder",
    "NoArgsConstructor", "AllArgsConstructor", "RequiredArgsConstructor",
}


def test_data_class_annotations_matches_original_literal_set():
    assert set(DATA_CLASS_ANNOTATIONS) == _ORIGINAL_DATA_ANNOTATIONS


def test_jpa_entity_annotations_is_subset_of_data_class_annotations():
    assert JPA_ENTITY_ANNOTATIONS <= DATA_CLASS_ANNOTATIONS


def test_lombok_and_jpa_partition_covers_full_set_without_overlap():
    assert JPA_ENTITY_ANNOTATIONS | LOMBOK_DATA_ANNOTATIONS == DATA_CLASS_ANNOTATIONS
    assert JPA_ENTITY_ANNOTATIONS & LOMBOK_DATA_ANNOTATIONS == set()


def test_grouping_module_uses_shared_constant_object():
    from parse_agent.grouping import _DATA_ANNOTATIONS

    assert _DATA_ANNOTATIONS is DATA_CLASS_ANNOTATIONS
