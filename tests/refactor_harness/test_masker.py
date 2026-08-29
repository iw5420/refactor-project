"""ResponseMasker（refactor_harness/core/masker.py）對應 02a 七章「共用核心：
Masker」。對應 docs/09b_bug_trace.md「golden 對隨機欄位無法比對」：`randomId`
加入 masked_fields_mutation_only，`voice` 上傳成功訊息裡包在檔案路徑字串裡
的 randomId 用新增的 masked_value_substring_patterns_mutation_only 處理。
"""
from refactor_harness.core.masker import ResponseMasker


def _masker():
    return ResponseMasker()


class TestExistingFieldAndPatternMasking:
    """既有行為的回歸鎖定，確保這次擴充沒有動到既有規則。"""

    def test_readonly_masks_only_masked_fields(self):
        masker = _masker()
        result = masker.mask({"token": "abc", "name": "王小明"}, context="readonly")
        assert result == {"token": "<<MASKED>>", "name": "王小明"}

    def test_readonly_does_not_mask_mutation_only_fields(self):
        masker = _masker()
        result = masker.mask({"id": 1, "order_id": 2}, context="readonly")
        assert result == {"id": 1, "order_id": 2}

    def test_mutation_masks_mutation_only_fields(self):
        masker = _masker()
        result = masker.mask({"id": 1, "order_id": 2, "user_id": 3}, context="mutation")
        assert result == {"id": "<<MASKED>>", "order_id": "<<MASKED>>", "user_id": "<<MASKED>>"}

    def test_iso_datetime_pattern_masks_whole_value(self):
        masker = _masker()
        result = masker.mask({"x": "2026-08-25T01:43:10.673670Z"}, context="readonly")
        assert result == {"x": "<<MASKED>>"}

    def test_recurses_into_nested_dicts_and_lists(self):
        masker = _masker()
        result = masker.mask({"items": [{"token": "a"}, {"token": "b"}]}, context="readonly")
        assert result == {"items": [{"token": "<<MASKED>>"}, {"token": "<<MASKED>>"}]}


class TestRandomIdFieldMasking:
    """對應 docs/09b_bug_trace.md：candidate/generateRandomId、
    candidate/search、exam/search、grading/analyze 這類端點回傳的
    randomId 每次執行都不同（CodeUtil.generateRandomCode() 現生），跟
    id／order_id／user_id 同一種道理，只在 mutation 情境遮罩。"""

    def test_random_id_masked_in_mutation_context(self):
        masker = _masker()
        result = masker.mask({"data": {"randomId": "A59vF", "result": "true"}}, context="mutation")
        assert result == {"data": {"randomId": "<<MASKED>>", "result": "true"}}

    def test_random_id_not_masked_in_readonly_context(self):
        """readonly 的精確查詢，randomId 是查詢正確性的一部分，不能遮
        （見 02a 七章「為什麼要分情境」同一種理由）。"""
        masker = _masker()
        result = masker.mask({"randomId": "xMpIV"}, context="readonly")
        assert result == {"randomId": "xMpIV"}

    def test_random_id_masked_inside_array_of_records(self):
        masker = _masker()
        result = masker.mask(
            {"exam": [{"randomId": "xMpIV", "name": "a"}, {"randomId": "bU4O9", "name": "b"}]},
            context="mutation",
        )
        assert result == {
            "exam": [{"randomId": "<<MASKED>>", "name": "a"}, {"randomId": "<<MASKED>>", "name": "b"}]
        }


class TestVoiceMessageSubstringMasking:
    """對應 docs/09b_bug_trace.md：voice 上傳成功訊息把 randomId 包在檔案
    路徑字串裡（如 "上傳成功: C:\\voice\\2025\\macuhau\\TAA\\xMpIV\\1_1.wav"），
    不是獨立欄位，masked_fields 擋不住，改用
    masked_value_substring_patterns_mutation_only 做子字串替換。"""

    def test_random_segment_in_upload_message_is_masked(self):
        masker = _masker()
        msg = "上傳成功: C:\\voice\\2025\\macuhau\\TAA\\xMpIV\\1_1.wav"
        result = masker.mask({"data": msg}, context="mutation")
        assert result == {"data": "上傳成功: C:\\voice\\2025\\macuhau\\TAA\\<<MASKED>>\\1_1.wav"}

    def test_not_applied_in_readonly_context(self):
        masker = _masker()
        msg = "上傳成功: C:\\voice\\2025\\macuhau\\TAA\\xMpIV\\1_1.wav"
        result = masker.mask({"data": msg}, context="readonly")
        assert result == {"data": msg}

    def test_does_not_misfire_on_five_letter_path_segments_like_voice(self):
        """實測發現的既有陷阱：CodeUtil 的隨機碼跟 "voice" 一樣剛好都是 5
        個英數字元，若只靠「前後都是路徑分隔符的 5 碼英數字段」判斷會連
        "voice" 本身也一起誤遮——這裡改用檔名（`{part}_{question}.{ext}`）
        當錨點，只匹配緊接在檔名前面的那一段。"""
        masker = _masker()
        msg = "上傳成功: C:\\voice\\2025\\macuhau\\TAA\\xMpIV\\1_1.wav"
        result = masker.mask({"data": msg}, context="mutation")
        assert "voice" in result["data"]
        assert "<<MASKED>>\\1_1.wav" in result["data"]

    def test_message_without_the_anchored_filename_shape_is_left_untouched(self):
        masker = _masker()
        result = masker.mask({"data": "操作成功"}, context="mutation")
        assert result == {"data": "操作成功"}
