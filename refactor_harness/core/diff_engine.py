import re
import yaml
from deepdiff import DeepDiff


class DiffEngine:
    def __init__(self, config_path: str = "config/harness.yaml"):
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        # 允許忽略順序的 JSONPath 列表，格式必須與 DeepDiff level.path() 一致
        # 例如：["root['data']['items']", "root['orders']"]
        self.ignore_order_paths = set(
            config.get("diff_rules", {}).get("ignore_order_at", [])
        )
        self._validate_ignore_order_paths()

    def _validate_ignore_order_paths(self):
        """
        DeepDiff 的 level.path() 只回傳 bracket 記法（root['a']['b']），若設定檔
        誤寫成 dot 記法（root.a.b），ignore_order_fn 裡的字串比對永遠不會命中，
        規則形同沒設定，卻會持續回傳 array_order_only 或比對失敗，增加無謂的
        debug 耗時卻查不出問題在設定本身。因此在載入當下就 fail-fast。

        正則中的量詞是 *（0 次以上），純 "root" 本身是合法值——對應「response
        最上層就是 JSON Array」的情況（如 GET /api/v1/users 直接回傳
        [{...}, {...}]），此時 DeepDiff 對頂層陣列的 level.path() 就是 "root"。
        "root[*]" 這類萬用字元寫法不是 DeepDiff 的合法路徑，被此檢查擋下是
        預期行為。

        語法合法不代表語意正確：本檢查只擋「格式錯誤」，擋不掉「格式正確但
        永遠匹配不到」的設定——路徑必須指向**陣列節點本身**。
        """
        bracket_pattern = re.compile(r"^root(\['[^']+'\]|\[\d+\])*$")
        invalid = [p for p in self.ignore_order_paths if not bracket_pattern.match(p)]
        if invalid:
            raise ValueError(
                f"harness.yaml 的 diff_rules.ignore_order_at 格式錯誤：{invalid}\n"
                f"必須用 DeepDiff 的 bracket 記法且指向「陣列節點本身」，"
                f"例如 \"root['data']['items']\"，或純 \"root\"（最上層即陣列時）；"
                f"不能用 dot 記法（如 \"root.data.items\"）或萬用字元（如 \"root[*]\"）。"
                f"另注意：以數字索引結尾的路徑（如 \"root['data'][0]\"）雖可通過本格式"
                f"檢查，但那指向的是陣列「元素」而非陣列本身，規則會靜默失效——"
                f"請確認路徑落在陣列欄位上。"
            )

    def compare(self, expected, actual) -> dict | None:
        """
        比對 expected 和 actual。
        回傳 None 表示完全一致；回傳 dict 表示有差異。

        陣列比對策略：
        - ignore_order_at 指定的路徑：使用 DeepDiff ignore_order_func 精準忽略
        - 其餘路徑：order-sensitive（順序不同 = diff）
        """
        # expected / actual 可能是 None（空 body 規格化為 None，不 fallback 成 {}）。
        # 進 DeepDiff 之前先明確處理：
        if expected is None or actual is None:
            if expected is None and actual is None:
                return None  # 兩端都沒有 body，一致
            return {
                "type": "body_presence_mismatch",
                "expected_type": type(expected).__name__,
                "actual_type": type(actual).__name__,
                "hint": "一端沒有 response body（null），另一端有。"
                        "常見於 204 No Content 與 200+body 的混淆，"
                        "檢查 Python 端該 endpoint 的 status code 與回傳設計是否與 Java 一致"
            }

        # 嚴格比對（所有路徑都 order-sensitive）
        diff_strict = DeepDiff(expected, actual, ignore_order=False)
        if not diff_strict:
            return None  # 完全一致

        # 如果有設定局部忽略順序的路徑，再做一次局部忽略比對
        if self.ignore_order_paths:
            def ignore_order_fn(level):
                # level.path() 回傳 bracket 格式，如 "root['data']['items']"
                return level.path() in self.ignore_order_paths

            diff_local = DeepDiff(expected, actual, ignore_order_func=ignore_order_fn)
            if not diff_local:
                return None  # 局部忽略順序後一致，視為通過

        # 全域忽略順序，用來判斷剩餘差異是否只是排序問題
        diff_loose = DeepDiff(expected, actual, ignore_order=True)
        if not diff_loose:
            return {
                "type": "array_order_only",
                "hint": "兩端回傳內容一致但陣列順序不同，"
                        "根本修法：在 SQL/ORM 加 ORDER BY；"
                        "暫時緩解：在 harness.yaml 的 ignore_order_at 加入此路徑"
            }

        # 有實質差異，回傳詳細 diff
        return diff_strict.to_dict()
