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
        self.MASK = "<<MASKED>>"

    def mask(self, obj: Any, context: Literal["readonly", "mutation"] = "readonly") -> Any:
        """
        readonly 情境（預設）只套用 masked_fields，不遮罩 id / order_id / user_id
        這類欄位，讓實體 ID 是否撈對能被真正比對到；mutation 情境額外套用
        masked_fields_mutation_only（見 mask_rules.yaml 說明）。
        呼叫端要自行傳對 context——comparator.py（readonly）不傳即為預設值，
        mutation_verifier.py／golden_writer.record_mutation() 需明確傳 "mutation"。
        """
        active_fields = self.masked_fields | (
            self.masked_fields_mutation_only if context == "mutation" else set()
        )
        return self._mask(obj, active_fields)

    def _mask(self, obj: Any, active_fields: set) -> Any:
        if isinstance(obj, dict):
            return {
                k: self.MASK if k in active_fields else self._mask(v, active_fields)
                for k, v in obj.items()
            }
        elif isinstance(obj, list):
            return [self._mask(item, active_fields) for item in obj]
        elif isinstance(obj, str):
            for pattern in self.masked_patterns:
                if re.match(pattern, obj):
                    return self.MASK
        return obj
