import logging
import re
import yaml

from refactor_harness.core.postman_runner import get_module

logger = logging.getLogger(__name__)


class RouteMapper:
    """
    讀取 config/harness.yaml 的 route_to_file_mapping／route_to_module_mapping
    （皆由 Agent ③ 自動產生，見 02a 十一章、05a 八章），把 route 解析成對應的
    Python 原始碼檔案清單，或對應的 module 名稱。
    """

    _UUID_RE = re.compile(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    )

    def __init__(self, config_path: str = "config/harness.yaml"):
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        self.route_mapping = config.get("route_to_file_mapping", {})
        self.module_mapping = config.get("route_to_module_mapping", {})

    def normalize_path_key(self, method: str, url_parts: list[str]) -> str:
        """
        將 URL 中的動態段（純數字或 UUID）替換為 {id} 後再組 key。

        GET /api/v1/users/123/profiles → GET_api_v1_users_{id}_profiles
        harness.yaml key 也用 {id} 佔位，精確匹配就能區分巢狀資源。
        """
        normalized = []
        for part in url_parts:
            if re.fullmatch(r'\d+', part):
                normalized.append("{id}")
            elif self._UUID_RE.fullmatch(part):
                normalized.append("{id}")
            else:
                normalized.append(part)
        return f"{method}_" + "_".join(normalized)

    def resolve_related_files(self, method: str, url_parts: list[str]) -> list[str]:
        """
        從 method 和 url_parts 組出 normalized key，查 route_to_file_mapping。
        精確匹配優先；fallback 到前綴匹配時，取候選中 pattern 字串「最長
        （最精確）」的一筆，不依賴 harness.yaml 裡 key 的撰寫順序決定命中
        結果。找不到則回傳空清單。
        """
        key = self.normalize_path_key(method, url_parts)

        if key in self.route_mapping:
            return self.route_mapping[key]

        candidates = [
            (pattern, files) for pattern, files in self.route_mapping.items()
            if key.startswith(pattern)
        ]
        if candidates:
            _, files = max(candidates, key=lambda pf: len(pf[0]))
            return files

        return []

    def resolve_module(self, method: str, url_parts: list[str]) -> str:
        """
        module 詞彙表的唯一權威來源（見 02a 十三章）：精確匹配
        route_to_module_mapping，查無對應才 fallback 回
        core.postman_runner.get_module() 的 URL 推斷（記警告）。不做
        resolve_related_files() 那種前綴匹配——這裡的值是單一 module
        字串，前綴候選之間沒有可比較的排序意義。
        """
        key = self.normalize_path_key(method, url_parts)

        if key in self.module_mapping:
            return self.module_mapping[key]

        logger.warning(
            "route_to_module_mapping 查無對應 key=%s，fallback 回 URL 推斷", key
        )
        return get_module(url_parts)
