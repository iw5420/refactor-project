import re
from typing import Any, Literal
import yaml


class ResponseMasker:
    def __init__(self, rules_path: str = "config/mask_rules.yaml"):
        with open(rules_path, encoding="utf-8") as f:
            rules = yaml.safe_load(f)
        self.masked_fields = set(rules.get("masked_fields", []))
        self.masked_fields_mutation_only = set(rules.get("masked_fields_mutation_only", []))
        self.masked_patterns = rules.get("masked_patterns", [])
        # 見 docs/09b_bug_trace.md「golden 對隨機欄位無法比對」：跟上面
        # masked_patterns（re.match 整個字串值，符合就整個值換成 MASK）不同
        # ——voice 這類端點把隨機碼包在一段更長的訊息字串裡（如
        # "上傳成功: C:\voice\...\xMpIV\1_1.wav"），隨機碼本身不是獨立欄位、
        # 也不是整個字串值，用 masked_fields／masked_patterns 兩種既有機制
        # 都無法只換掉字串裡的一小段、保留其餘內容。這裡用 re.sub() 只替換
        # 匹配到的子字串，不是整個值。只在 mutation 情境套用（比照
        # masked_fields_mutation_only 的既有理由：這種隨機值只在 mutation
        # 剛建立的資源裡才會發生，readonly 的精確查詢不該被這條規則影響）。
        self.masked_value_substring_patterns_mutation_only = rules.get(
            "masked_value_substring_patterns_mutation_only", []
        )
        self.MASK = "<<MASKED>>"

    def mask(self, obj: Any, context: Literal["readonly", "mutation"] = "readonly") -> Any:
        """
        readonly 情境（預設）只套用 masked_fields，不遮罩 id / order_id / user_id
        這類欄位，讓實體 ID 是否撈對能被真正比對到；mutation 情境額外套用
        masked_fields_mutation_only／masked_value_substring_patterns_mutation_only
        （見 mask_rules.yaml 說明）。
        呼叫端要自行傳對 context——comparator.py（readonly）不傳即為預設值，
        mutation_verifier.py／golden_writer.record_mutation() 需明確傳 "mutation"。
        """
        active_fields = self.masked_fields | (
            self.masked_fields_mutation_only if context == "mutation" else set()
        )
        substring_patterns = (
            self.masked_value_substring_patterns_mutation_only if context == "mutation" else []
        )
        return self._mask(obj, active_fields, substring_patterns)

    def _mask(self, obj: Any, active_fields: set, substring_patterns: list) -> Any:
        if isinstance(obj, dict):
            return {
                k: self.MASK if k in active_fields else self._mask(v, active_fields, substring_patterns)
                for k, v in obj.items()
            }
        elif isinstance(obj, list):
            return [self._mask(item, active_fields, substring_patterns) for item in obj]
        elif isinstance(obj, str):
            for pattern in self.masked_patterns:
                if re.match(pattern, obj):
                    return self.MASK
            for pattern in substring_patterns:
                obj = re.sub(pattern, self.MASK, obj)
            return obj
        return obj
