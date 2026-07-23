# Harness 程式碼：Java → Python Multi-Agent 重構情境

> 本文件收錄所有 Harness 的程式碼與設定檔，按檔案路徑組織。架構說明與流程設計見配套文件：`02a_harness_architecture.md`。

---

## 目錄

| 檔案 | 說明 |
|---|---|
| `config/harness.yaml` | 全域設定（DB、服務、collection、路由對應、diff 規則） |
| `config/mask_rules.yaml` | 動態欄位遮罩規則 |
| `core/postman_runner.py` | Newman 共用執行器 + 頂層 folder 列舉 |
| `core/masker.py` | Response 動態欄位遮罩（readonly / mutation 兩種情境） |
| `core/diff_engine.py` | Diff 比對邏輯 |
| `core/route_mapper.py` | route → related_files 解析（兩個 Verifier 共用） |
| `core/reporter.py` | 結構化 Report 產生器 |
| `fixtures/db_env.py` | 測試 DB 環境管理 |
| `recorder/golden_writer.py` | Golden Output 錄製（Agent ②） |
| `verifier/comparator.py` | Golden Output 比對（Agent ⑥） |
| `verifier/mutation_verifier.py` | Mutation Collection 比對（Agent ⑥） |
| `langgraph_nodes/test_nodes.py` | LangGraph Node 整合 |

---

## config/harness.yaml

```yaml
databases:
  production:
    dsn: "postgresql://postgres:password@127.0.0.1:5432/MOC_MATSUEXAM"       # 原始 DB，Harness 絕不連這個
  test:
    # 值同 .env 的 TEST_DB_DSN，這裡僅為 fallback 用的靜態預設值。正常執行時
    # GoldenRecorder／MutationVerifier／run_postman_tests 都是由呼叫端傳入
    # state["test_dsn"]（見 langgraph_nodes/test_nodes.py），只有在沒有
    # State 可用的情境（stub-first 開發、單元測試）才會落回這裡的值。
    dsn: "postgresql://postgres:password@127.0.0.1:5432/MOC_MATSUEXAM_TEST"  # 測試專用，所有 Harness 操作都在這
    # 下面是範例展示值（對應 MOC_MATSUEXAM 專案的表）。這份清單不會自動產生，
    # 要換成實際專案的表名——照 seed.sql 實際灌了哪些表填，漏列的表不會被
    # apply_seed() 清空，可能讓上一輪測試殘留資料污染下一輪結果。
    tables_to_truncate:                      # 統一管理，不寫死在程式碼裡
      - order_items                          # 順序不重要，TRUNCATE CASCADE 自動處理外鍵
      - orders
      - users
      # 未來新增資料表只需在這裡加一行，程式碼不需要改

services:
  java:
    base_url: "http://localhost:8080"
    # Spring Boot 環境變數覆蓋，JDBC 格式，不同於 Python 的 DATABASE_URL
    db_env_var: "SPRING_DATASOURCE_URL"
  python:
    base_url: "http://localhost:8000"
    # FastAPI + SQLAlchemy 統一讀取的變數名，psycopg2/asyncpg 格式
    db_env_var: "DATABASE_URL"

collections:
  readonly:
    path: "postman/collection_readonly.json"
    seed_strategy: "once_before_all"      # 整個 collection 跑前 seed 一次
  mutation:
    path: "postman/collection_mutation.json"
    seed_strategy: "before_each_folder"   # 每個 folder（case）前都 seed

diff_rules:
  ignore_order_at:
    # 在這裡列出允許忽略排序的陣列路徑
    # ⚠️  路徑格式必須用 bracket 記法（與 DeepDiff level.path() 一致），
    #     且必須指向「陣列節點本身」，不是陣列裡的元素
    #    ✅  正確："root['data']['items']"（items 是陣列欄位）
    #    ✅  正確："root"（response 最上層本身就是 JSON Array 時，如 GET 直接回傳 [...]）
    #    ❌  錯誤："root.data.items"（dot 記法不會被 level.path() 匹配到）
    #    ❌  錯誤："root[*]"（DeepDiff 不支援萬用字元，載入時會被格式檢查擋下）
    #    ❌  錯誤："root['data'][0]"（以索引結尾＝指向元素而非陣列，格式檢查會放行
    #                但執行時永遠匹配不到，規則靜默失效）
    # 每次新增前先確認：Java 和 Python 是否已有明確 ORDER BY？
    # 若有，就不需要加這裡；若暫時沒有，先加這裡，並開 ticket 追蹤
    # - "root['data']['items']"   ← 範例，先留空，按需加入

# route → Python 原始碼對應表
# ⚠️  此區段由 Agent ③（架構設計 Agent）自動產生並寫入，不應手動維護。
#    若需要修改，應更新 Agent ③ 的輸出邏輯，而不是直接編輯這裡。
#
# key 格式：{METHOD}_{path_normalized}，動態段（數字 ID / UUID）用 {id} 佔位
# 這樣 GET /api/v1/users/123 和 GET /api/v1/users/123/profiles 就不會互相假匹配
# key 不含 item_name（item_name 只在 case_id 裡用來去重）
#
# 下面是範例展示值，實際內容 100% 來自 Agent ③ 當次輸出的 file_path，
# 若 Agent ③ 決定的目錄結構不是 app/，這裡也會跟著變，不是寫死的規定。
route_to_file_mapping:
  "GET_api_v1_users":
    - "app/routers/user_router.py"
    - "app/services/user_service.py"
    - "app/repositories/user_repository.py"
  "GET_api_v1_users_{id}":
    - "app/routers/user_router.py"
    - "app/services/user_service.py"
  "GET_api_v1_users_{id}_profiles":   # ← 巢狀資源，精確 key，不會被上一條假匹配
    - "app/routers/profile_router.py"
    - "app/services/profile_service.py"
  "POST_api_v1_orders":
    - "app/routers/order_router.py"
    - "app/services/order_service.py"
```

---

## config/mask_rules.yaml

```yaml
# 下面 masked_fields／masked_fields_mutation_only 的內容是範例展示值（對應
# MOC_MATSUEXAM 專案），不會自動產生，要換成實際 API 回應裡真正的動態欄位——
# 照你專案回應格式過一輪，比對照抄這份清單準。
#
# ⚠️  masked_fields 只放「純量型態（string / number）」的動態欄位。
#    值為陣列或物件的欄位不應放在這裡：
#    若兩端在該欄位回傳了不同型態（如 Java [] vs Python null），
#    兩者都會被打成 <<MASKED>>，型態不一致的 bug 就會被掩蓋。
#    結構性欄位內若含動態子欄位，讓 Masker 遞迴處理子欄位即可。
masked_fields:
  - created_at
  - updated_at
  - deleted_at
  - request_id
  - trace_id
  - session_id
  - token
  - access_token
  - refresh_token

# 只在驗證 mutation collection（POST/PUT/DELETE 的寫入回應）時套用，readonly 一律不遮罩。
# 動機：Java／Python 兩端的 DB 自增序列不會產生相同的 ID（見 00 七/[B]），寫入操作
# 剛建立的資源其 ID 本來就無法逐值比對，因此只在這個情境下遮罩，換取可以比對其餘欄位。
# readonly 查詢（如 GET /users/1）不適用這條規則：這種情境下 ID 是查詢條件本身，
# 若一併遮罩，會連「Python 撈錯資料實體」這種嚴重 bug 都測不出來。
masked_fields_mutation_only:
  - id
  - order_id
  - user_id

masked_patterns:
  - "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}"   # ISO datetime
  - "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"          # UUID v4
  - "^Bearer\\s"                                       # Bearer token
```

---

## core/postman_runner.py

```python
import json
import re
import subprocess
import tempfile

def run_newman(collection_path: str, base_url: str, folder: str = None) -> dict:
    """
    執行 newman 並回傳解析後的 JSON 輸出。
    兩端（Recorder / Verifier）共用同一份，確保執行行為一致。

    若 newman 失敗（服務沒起來、collection 路徑錯誤）立即拋出明確例外，
    不讓錯誤靜默流入後續比對邏輯。
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_path = tmp.name

    cmd = [
        "newman", "run", collection_path,
        "--env-var", f"base_url={base_url}",
        "--reporters", "json",
        "--reporter-json-export", output_path
    ]
    if folder:
        cmd += ["--folder", folder]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"newman 執行失敗（return code {result.returncode}）\n"
            f"collection: {collection_path}\n"
            f"base_url: {base_url}\n"
            f"stderr: {result.stderr[:500]}"
        )

    with open(output_path) as f:
        return json.load(f)


def list_top_level_folders(collection_path: str) -> list[str]:
    """
    讀取 Postman Collection JSON，回傳所有頂層 folder 的名稱。
    Recorder／Verifier 共用同一份實作，避免兩邊各自實作而漂移。

    每個頂層 folder＝一條完整鏈式情境，是 apply_seed／newman --folder 的執行單位
    （見 02a 六章）。Agent B 產生 collection_mutation.json 時，必須把有鏈式依賴的
    request 群組成同一個頂層 folder，而不是拆成各自獨立的 folder。
    """
    with open(collection_path) as f:
        collection = json.load(f)
    return [
        entry["name"]
        for entry in collection.get("item", [])
        if "item" in entry  # 有 item 子陣列的才是 folder，單一 request 沒有
    ]


def make_case_id(item: dict) -> str:
    """
    產生 case_id：{item_name}_{method}_{path}，同時作為 golden 檔案名稱。

    Postman item name 與 URL path 可能含有檔名非法字元（: ? * / \\ 空白等，
    例如 item 命名為 "GET /users:search" 或 path 含 matrix parameter）。
    case_id 直接作為檔名（{case_id}.json），未消毒在 Windows 會直接
    OSError，在 POSIX 上 "/" 會被誤當目錄分隔。統一把「字母數字、底線、
    連字號」以外的字元全部替換為底線。

    錄製端寫檔、驗證端讀檔都呼叫這個共用函式，從結構上保證兩端演算法一致——
    任一端漂移，golden 就永遠找不到。
    """
    def _sanitize(raw: str) -> str:
        return re.sub(r'[^\w\-]', '_', raw)

    item_name = _sanitize(item.get("name", "unnamed").lower())
    method = item["request"]["method"]
    path = "_".join(_sanitize(p) for p in item["request"]["url"]["path"])
    return f"{item_name}_{method}_{path}"


def get_module(url_parts: list[str]) -> str:
    """
    從 URL path 推斷 module 名稱：取第一個非版本前綴的路徑段。
    例：["api", "v1", "orders", "123"] → "orders"

    ⚠️ 這個函式是整個 Harness 的 module 詞彙表「唯一權威來源」：
    - golden 檔案的存放目錄（fixtures/golden/{module}/）用它決定
    - verify_module(module_filter) 的過濾條件用它比對
    - 02a 十三章規定 task 的 module 欄位「對應 fixtures/golden/ 的子目錄名稱」，
      因此 [P] Plan Agent 填入 task.module 時**必須使用與本函式相同的推斷結果**。
      兩邊詞彙不一致時不會報錯，而是 verify_module 靜默漏測——例如 Plan Agent
      認定 /api/v1/admin/orders/audit 屬於 "orders" 模組，但本函式推斷為 "admin"，
      該 case 就永遠不會被納入 orders 的局部驗證（golden 寫入與載入端內部自洽，
      所以不會假失敗，只會無聲消失在局部驗證的涵蓋範圍外，比假失敗更難發現）。

    ⚠️ 深層／跨模組路由的已知限制：本推斷只看第一個非版本段，
    /api/v1/admin/orders/audit 會歸入 "admin" 而非語意上的 "orders"。
    若專案存在這類路由，兩個對策擇一：
    (a) 讓 Plan Agent 直接採用本函式的推斷結果作為 task.module（機械一致，推薦）；
    (b) 未來擴充：由 Agent ③ 在 harness.yaml 產出 route → module 對應表，
        本函式改查表、URL 推斷降為 fallback（目前未實作，列於 02a 十六章）。
    """
    return next(
        (p for p in url_parts if p not in ("api", "v1", "v2")),
        "misc"
    )
```

---

## core/masker.py

```python
import re
from typing import Any, Literal
import yaml

class ResponseMasker:
    def __init__(self, rules_path: str = "config/mask_rules.yaml"):
        with open(rules_path) as f:
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
```

---

## core/diff_engine.py

```python
import re
import yaml
from deepdiff import DeepDiff

class DiffEngine:
    def __init__(self, config_path: str = "config/harness.yaml"):
        with open(config_path) as f:
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
        預期行為——DeepDiff 的 ignore_order_func 逐節點精確比對路徑字串，
        不支援萬用字元展開。

        語法合法不代表語意正確：本檢查只擋「格式錯誤」，擋不掉「格式正確但
        永遠匹配不到」的設定——路徑必須指向**陣列節點本身**：
        - ✅ "root['data']['items']"：items 是陣列，level.path() 會產生這個值
        - ❌ "root['data'][0]"：以數字索引**結尾**＝指向陣列的某個「元素」而非
          陣列本身，除非該元素本身又是陣列（陣列包陣列，罕見），否則
          ignore_order_func 永遠比對不中，規則靜默失效——這幾乎必然是設定錯誤
        - ⚠️ "root['items'][0]['tags']"：合法且有意義（items 的第 0 個元素裡的
          tags 陣列），但只涵蓋第 0 個元素——DeepDiff 不支援萬用字元，若要忽略
          每個元素裡 tags 的順序，每個索引都要列一條（[0]、[1]、…），實務上
          建議改在 Java/Python 端加 ORDER BY 根治，而不是列舉索引
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
        # (1) 避免 ignore_order=True 模式下部分 DeepDiff 版本對 None 輸入的邊界行為；
        # (2) 「一端沒有 body、另一端有」給出明確的 body_presence_mismatch 分類，
        #     而不是讓 DeepDiff 的 type_changes 結構被 Reporter 粗分成 value_mismatch。
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
```

---

## core/route_mapper.py

route → Python 原始碼檔案的解析器，`GoldenVerifier` 與 `MutationVerifier` 共用同一份，確保 `related_files` 的解析行為在 readonly / mutation 兩邊完全一致，也讓 Debug Agent 在 mutation failure 時同樣能拿到引導檔案。

```python
import re
import yaml


class RouteMapper:
    """
    讀取 config/harness.yaml 的 route_to_file_mapping（由 Agent ③ 自動產生，
    見 02a 十一章），把 route 解析成對應的 Python 原始碼檔案清單。
    """

    _UUID_RE = re.compile(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    )

    def __init__(self, config_path: str = "config/harness.yaml"):
        with open(config_path) as f:
            config = yaml.safe_load(f)
        self.route_mapping = config.get("route_to_file_mapping", {})

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
        結果——否則巢狀資源可能被較不精確的前綴誤匹配（見 02a 十一章）。
        找不到則回傳空清單，由 Reporter 標記警告。
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
```

---

## core/reporter.py

```python
class HarnessReporter:
    def build_report(self, results: list[dict], excluded_folders: list[str] | None = None) -> dict:
        """
        excluded_folders：因 Recorder 錄製時判定為 tainted 而整個 folder 未參與
        這次驗證的情境（見 02a 三章「Mutation 錄製異常偵測」、四章「排除已知
        異常的 folder」、九章「excluded_folders 欄位」）。這些 case 不計入
        summary／failures／passed_cases 既有的計算邏輯——本方法其餘分類行為
        完全不變，excluded_folders 只是額外附加的頂層欄位。
        """
        passed = [r for r in results if r["passed"]]
        failed = [r for r in results if not r["passed"]]

        return {
            "summary": {
                "total": len(results),
                "passed": len(passed),
                "failed": len(failed),
                "pass_rate": round(len(passed) / len(results), 3) if results else 0
            },
            "status": "pass" if not failed else "fail",
            "failures": [
                {
                    "case_id": f["case_id"],
                    "failure_type": self._classify_failure(f),
                    "status_code_match": f.get("status_match", True),
                    "expected_status": f.get("expected_status"),
                    "actual_status": f.get("actual_status"),
                    "body_diff": f.get("body_diff"),
                    "related_files": f.get("related_files", []),
                    "debug_hint": self._generate_hint(f)
                }
                for f in failed
            ],
            "passed_cases": [p["case_id"] for p in passed],
            "excluded_folders": excluded_folders or []
        }

    def _classify_failure(self, result: dict) -> str:
        if result.get("error") == "golden_not_found":
            return "golden_not_found"
        if result.get("error") == "response_not_json":
            return "response_not_json"
        if not result.get("status_match", True):
            return "status_code_mismatch"
        if result.get("body_diff"):
            diff = result["body_diff"]
            if isinstance(diff, dict) and diff.get("type") == "array_order_only":
                return "array_order_only"
            if isinstance(diff, dict) and diff.get("type") == "body_presence_mismatch":
                return "body_presence_mismatch"
            if "missing_fields" in str(diff) or "dictionary_item_removed" in str(diff):
                return "missing_fields"
            # DeepDiff 的實際 key 是 "type_changes"（複數）
            if "type_changes" in str(diff):
                return "type_mismatch"
            return "value_mismatch"
        return "unknown"

    def _generate_hint(self, result: dict) -> str:
        ftype = self._classify_failure(result)
        hints = {
            "status_code_mismatch": "服務可能拋出未處理例外，檢查 router 的 exception handler",
            "missing_fields": "Response 缺少欄位，檢查 Pydantic schema 定義是否完整",
            "type_mismatch": "欄位型別不符，檢查 ORM model 的型別對應",
            "value_mismatch": "值不符但結構正確，檢查業務邏輯計算是否與 Java 一致",
            "body_presence_mismatch": "一端沒有 response body（null）另一端有，"
                                       "常見於 204 No Content 與 200+body 的混淆，"
                                       "檢查該 endpoint 的 status code 與回傳設計是否與 Java 一致",
            "golden_not_found": "找不到 golden output，檢查 route_to_file_mapping 的 key 格式是否為 {METHOD}_{path_normalized}",
            "response_not_json": "Python 服務回傳非 JSON，可能是未處理例外導致 HTML error page",
            "array_order_only": "兩端資料內容一致但陣列排序不同，"
                                "根本修法：在 Java 和 Python 的對應 SQL/ORM 加上相同的 ORDER BY；"
                                "暫時緩解：在 harness.yaml 的 ignore_order_at 加入此路徑",
        }
        return hints.get(ftype, "請檢查相關檔案的實作")
```

### Report 輸出範例

```json
{
  "summary": {
    "total": 42,
    "passed": 38,
    "failed": 4,
    "pass_rate": 0.905
  },
  "status": "fail",
  "failures": [
    {
      "case_id": "get_user_GET_users_123",
      "failure_type": "missing_fields",
      "status_code_match": true,
      "expected_status": 200,
      "actual_status": 200,
      "body_diff": {
        "dictionary_item_removed": ["root['data']['email']"]
      },
      "related_files": [
        "app/routers/users_router.py",
        "app/services/users_service.py",
        "app/repositories/users_repository.py"
      ],
      "debug_hint": "Response 缺少欄位，檢查 Pydantic schema 定義是否完整"
    }
  ],
  "passed_cases": ["..."],
  "excluded_folders": ["order_lifecycle_bad"]
}
```

`excluded_folders`：因 Recorder 錄製時判定為 tainted、整個 folder 未參與這次驗證的情境（見 `MutationVerifier`、`02a_harness_architecture.md` 四章「排除已知異常的 folder」、九章「excluded_folders 欄位」）。不計入 `summary` 統計，也不是 `failures`，預設空陣列。

---

## fixtures/db_env.py

```python
import subprocess
import psycopg2

class DbEnvironment:
    def __init__(self, test_dsn: str):
        # 永遠只連 test_dsn（如 MOC_MATSUEXAM_TEST），絕不連 production DB（如 MOC_MATSUEXAM）
        self.test_dsn = test_dsn

    def apply_seed(self, seed_file: str, tables_to_truncate: list[str]):
        """
        每次測試前執行：
        1. 清除測試 DB（test_dsn）的指定資料表（不影響 production DB）
        2. 重新注入 seed data

        tables_to_truncate 從 config/harness.yaml 讀入，不寫死在程式碼裡。

        TRUNCATE 與 seed 注入包在同一個 psycopg2 交易裡——psycopg2 走 simple
        query protocol，本來就支援分號分隔的多條語句。seed 失敗時整體
        rollback，不會留下「已清空但沒灌資料」的中間態，也不依賴 psql
        命令列工具。

        ⚠️ 限制：seed.sql 必須是標準 SQL（INSERT / UPDATE / ...），
        不能包含 psql meta-command（\\copy、\\i、\\set 等）或 COPY FROM stdin
        （pg_dump 的資料匯出格式）——psycopg2 無法執行這些。
        若未來 seed 改用 pg_dump 匯出格式，需改回 psql subprocess 或改用 copy_expert。
        """
        with psycopg2.connect(self.test_dsn) as conn:
            with conn.cursor() as cur:
                # 1. TRUNCATE：單一指令，CASCADE 自動處理外鍵順序，避免多次鎖表
                tables_sql = ", ".join(tables_to_truncate)
                cur.execute(f"TRUNCATE TABLE {tables_sql} RESTART IDENTITY CASCADE")

                # 2. seed 注入：與 TRUNCATE 同一交易，失敗時一起 rollback
                with open(seed_file, "r", encoding="utf-8") as f:
                    seed_sql = f.read()
                cur.execute(seed_sql)
            conn.commit()

    def sync_schema(self, source_dsn: str):
        """
        當 production DB（如 MOC_MATSUEXAM）的 schema 有變動時，同步到測試 DB（test_dsn）。
        僅適用於 Flyway/Liquibase／手動 migration 的情況；若 Java 用 Hibernate/JPA
        `ddl-auto`，schema 由 [A] Spec Agent 啟動 Java 服務時自動建立，不需呼叫這個方法
        （見 02a 十四章「Schema 來源」）。
        在每次 migration 後執行一次即可。
        """
        dump = subprocess.run(
            ["pg_dump", "--schema-only", source_dsn],
            capture_output=True, text=True
        )
        if dump.returncode != 0:
            raise RuntimeError(
                f"pg_dump 失敗，schema 同步中止\n"
                f"stderr: {dump.stderr[:500]}"
            )

        psql = subprocess.run(
            ["psql", self.test_dsn],
            input=dump.stdout, capture_output=True, text=True
        )
        if psql.returncode != 0:
            raise RuntimeError(
                f"psql schema 匯入失敗\n"
                f"stderr: {psql.stderr[:500]}"
            )
        return True
```

---

## recorder/golden_writer.py

```python
import json
import yaml
from datetime import datetime
from pathlib import Path
# 套件內部一律用帶 refactor_harness. 前綴的絕對匯入（見 02a 二章「匯入慣例」）
from refactor_harness.core.masker import ResponseMasker
from refactor_harness.core.postman_runner import run_newman, list_top_level_folders, make_case_id, get_module
from refactor_harness.fixtures.db_env import DbEnvironment

# mutation collection 只接受 2xx（含 204）視為預期成功；不在此範圍內的一律判定為
# 異常，不寫入 golden（見 02a 三章「Mutation 錄製異常偵測」）。只在 record_mutation()
# 傳入 context="mutation" 時套用，readonly 不受影響。
_EXPECTED_STATUS_RANGE = range(200, 300)

class GoldenRecorder:
    """
    readonly／mutation 分別呼叫 record()／record_mutation()，兩者的 DB reset
    粒度不同（見 02a 六章），各自封裝好對應的 apply_seed 邏輯。
    """
    def __init__(self, java_base_url: str, golden_dir: str,
                 test_dsn: str | None = None,
                 config_path: str = "config/harness.yaml"):
        """
        test_dsn 由呼叫端傳入（通常是 state["test_dsn"]，見下方
        langgraph_nodes/test_nodes.py 的 record_golden_output），確保
        Recorder 與 Verifier 連的是同一顆測試 DB；留 None 時才 fallback
        讀 harness.yaml 的靜態預設值，只給 stub-first 開發或單元測試等
        沒有完整 State 可用的情境使用。
        """
        self.java_base_url = java_base_url
        self.golden_dir = Path(golden_dir)
        self.masker = ResponseMasker()

        with open(config_path) as f:
            config = yaml.safe_load(f)
        self.test_dsn = test_dsn or config["databases"]["test"]["dsn"]
        self.tables = config["databases"]["test"]["tables_to_truncate"]
        self.db = DbEnvironment(test_dsn=self.test_dsn)

    def record(self, collection_path: str) -> dict:
        """
        錄製 readonly collection：DB 狀態全程不變，seed 一次即可跑完整份 collection。

        readonly 不做「非預期 status code」判定（見 02a 三章「Mutation 錄製異常
        偵測」的判斷基準——readonly 的 path 參數用 seed.sql 既有資料，理論上不會
        觸發業務規則錯誤），所以這裡拿到的 anomalies 必然是空的，直接把 built
        全部寫入即可，行為與異常偵測加入前完全等價。
        """
        self.db.apply_seed("fixtures/seed.sql", self.tables)
        newman_output = run_newman(collection_path, self.java_base_url)
        built, skipped, _anomalies = self._build_executions(
            newman_output["run"]["executions"], context="readonly"
        )
        for case_id, golden in built:
            self._write_golden(case_id, golden)
        return {
            "recorded_count": len(built),
            "skipped_count": len(skipped),
            "cases": [case_id for case_id, _ in built],
            "skipped": skipped
        }

    def record_mutation(self, collection_path: str) -> dict:
        """
        錄製 mutation collection 的 golden output。

        關鍵差異（對應 02a 六章的鏈式依賴設計）：mutation collection 的每個「頂層 folder」
        代表一條完整的鏈式情境（建立 → 查詢／修改 → 清理），而不是單一 endpoint；
        每個 folder 開跑前都要重新 apply_seed，folder 內部的多個 request 共用同一次
        newman run 的 Postman Environment（讓上一步建立的 ID 能被下一步引用）。
        這個 reset 粒度必須跟 MutationVerifier.verify_one() 完全一致——
        錄製端用「連續整份 collection 跑一次」而驗證端用「逐 folder reset」，
        會讓兩端的 DB 初始狀態不同，比對必然出現偽 fail。

        異常偵測（見 02a 三章「Mutation 錄製異常偵測」）：先用 _build_executions()
        建構整個 folder 的結果、但不寫入磁碟；若這個 folder 裡有任何一個 case 的
        status code 落在預期範圍之外，代表這條鏈式情境的前提已經不可信（鏈式注入
        的 test script 很可能沒抓到預期欄位，環境變數最終會被設成字面字串
        "undefined"），因此**整個 folder 都不寫入**，包含 folder 內其餘原本正常的
        case——不細究哪些 case 個別正常，這是「頂層 folder＝一條不可分割的情境」
        既有約定的自然延伸，記錄進 tainted_folders 供人工複查。只有 folder 內完全
        沒有異常時，才把 built 寫入磁碟。
        """
        folder_names = list_top_level_folders(collection_path)
        recorded, skipped, tainted_folders = [], [], []
        for folder_name in folder_names:
            self.db.apply_seed("fixtures/seed.sql", self.tables)
            newman_output = run_newman(
                collection_path, self.java_base_url, folder=folder_name
            )
            built, folder_skipped, folder_anomalies = self._build_executions(
                newman_output["run"]["executions"], context="mutation"
            )

            if folder_anomalies:
                tainted_folders.append({
                    "folder": folder_name,
                    "anomalies": folder_anomalies,
                    # 整個 folder 的 case 都視為不可信，不只是觸發異常的那幾筆
                    # （見上方 docstring 說明）。
                    "excluded_case_ids": (
                        [case_id for case_id, _ in built]
                        + [a["case_id"] for a in folder_anomalies]
                    ),
                })
                continue

            for case_id, golden in built:
                self._write_golden(case_id, golden)
            recorded += [case_id for case_id, _ in built]
            skipped += folder_skipped

        return {
            "recorded_count": len(recorded),
            "skipped_count": len(skipped),
            "cases": recorded,
            "skipped": skipped,
            "tainted_folders": tainted_folders,
        }

    def write_metadata(self, *results: dict):
        """
        合併 record() 與 record_mutation() 的結果，寫一份 _metadata.json。
        故意不在 record()/record_mutation() 內部各自寫一次——那樣後呼叫的會把
        先呼叫的覆蓋掉，readonly 的錄製摘要就不見了。呼叫端（見 test_nodes.py）
        負責在兩者都跑完後呼叫一次這個方法。

        tainted_folders 用 .get(..., []) 取——record() 的回傳沒有這個 key（readonly
        不會產生 tainted folder），避免合併時 KeyError。
        """
        recorded = [c for r in results for c in r["cases"]]
        skipped = [c for r in results for c in r["skipped"]]
        tainted_folders = [t for r in results for t in r.get("tainted_folders", [])]
        metadata = {
            "recorded_at": datetime.utcnow().isoformat() + "Z",
            "total_cases": len(recorded),
            "cases": recorded,
            "skipped": skipped,
            "tainted_folders": tainted_folders
        }
        with open(self.golden_dir / "_metadata.json", "w") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

    def _build_executions(
        self, executions: list[dict], context: str = "readonly"
    ) -> tuple[list[tuple[str, dict]], list[str], list[dict]]:
        """
        建構整批 execution 的 golden 內容並分類，**不寫入磁碟**——是否真的落地
        由呼叫端（record()／record_mutation()）決定：record_mutation() 需要先看
        過整個 folder 有沒有異常，才能決定該 folder 要不要整批捨棄（見上方
        record_mutation() docstring、02a 三章）。取代舊版直接寫檔的
        _record_executions()。

        回傳 (built, skipped, anomalies)：
        - built：[(case_id, golden_dict), ...]，正常應寫入的
        - skipped：[case_id, ...]，非 JSON response 被跳過的（沿用既有語意）
        - anomalies：[{"case_id", "status_code", "body_preview"}, ...]，
          status code 異常被跳過的（只在 context="mutation" 時可能非空）
        """
        built: list[tuple[str, dict]] = []
        skipped: list[str] = []
        anomalies: list[dict] = []

        for execution in executions:
            item = execution["item"]
            response = execution["response"]
            case_id = make_case_id(item)  # 共用函式，含檔名消毒（見 core/postman_runner.py）

            golden, skip_reason = self._build_golden(item, response, context)

            if golden is not None:
                built.append((case_id, golden))
                continue

            if skip_reason == "unexpected_status":
                anomalies.append({
                    "case_id": case_id,
                    "status_code": response.get("code"),
                    "body_preview": (response.get("body") or "")[:500],
                })
            else:
                skipped.append(case_id)  # 非 JSON response，記錄但跳過

        return built, skipped, anomalies

    def _build_golden(
        self, request_item: dict, response: dict, context: str = "readonly"
    ) -> tuple[dict | None, str | None]:
        """
        解析單一 execution 的 response，回傳 (golden, skip_reason)。
        非 JSON response（二進位、HTML error page）一律跳過，skip_reason 為
        "non_json_response"；context="mutation" 且 status code 不在 2xx 範圍內
        （見模組層級常數 _EXPECTED_STATUS_RANGE、02a 三章「Mutation 錄製異常
        偵測」）時，skip_reason 為 "unexpected_status"。golden 不為 None 時
        skip_reason 必為 None。

        空 body（如 204 No Content）不強制轉成 "{}" 再 parse——
        那樣會把「真的沒有 body」和「回傳了空物件 {}」這兩種不同語意混為一談，
        導致 Java 回 204、Python 誤回 200+{} 這類真實 bug 被 fallback 掩蓋掉。
        改成明確存成 body: null，交由 DiffEngine 忠實比對兩端是否真的一致。

        context 決定 masker 是否額外遮罩 id 系欄位（見 mask_rules.yaml 的
        masked_fields_mutation_only 說明）——record() 呼叫時用預設值 "readonly"，
        record_mutation() 呼叫時明確傳入 "mutation"。
        """
        headers = {h["key"].lower(): h["value"]
                   for h in (response.get("headers", {}).get("members", []))}
        content_type = headers.get("content-type", "")

        if "application/json" not in content_type:
            return None, "non_json_response"

        raw_body = response.get("body")
        if raw_body is None or raw_body.strip() == "":
            body = None
        else:
            try:
                body = json.loads(raw_body)
            except (json.JSONDecodeError, TypeError):
                return None, "non_json_response"

        if context == "mutation" and response["code"] not in _EXPECTED_STATUS_RANGE:
            return None, "unexpected_status"

        masked_body = self.masker.mask(body, context=context) if body is not None else None

        golden = {
            "_meta": {
                "recorded_at": datetime.utcnow().isoformat() + "Z",
                "source": "java"
            },
            "request": {
                "method": request_item["request"]["method"],
                "path": request_item["request"]["url"]["path"],
            },
            "response": {
                "status_code": response["code"],
                "body": masked_body
            }
        }
        return golden, None

    def _write_golden(self, case_id: str, golden: dict):
        # module 推斷用共用的 get_module（見 core/postman_runner.py），
        # 與驗證端的載入／過濾用同一套詞彙。
        module = get_module(golden["request"]["path"])
        dir_path = self.golden_dir / module
        dir_path.mkdir(parents=True, exist_ok=True)

        with open(dir_path / f"{case_id}.json", "w") as f:
            json.dump(golden, f, ensure_ascii=False, indent=2)
```

---

## verifier/comparator.py

```python
import json
from pathlib import Path
# 套件內部一律用帶 refactor_harness. 前綴的絕對匯入（見 02a 二章「匯入慣例」）
from refactor_harness.core.masker import ResponseMasker
from refactor_harness.core.diff_engine import DiffEngine
from refactor_harness.core.reporter import HarnessReporter
from refactor_harness.core.postman_runner import run_newman, make_case_id, get_module
from refactor_harness.core.route_mapper import RouteMapper

class GoldenVerifier:
    def __init__(self, python_base_url: str, golden_dir: str,
                 config_path: str = "config/harness.yaml"):
        self.python_base_url = python_base_url
        self.golden_dir = Path(golden_dir)
        self.masker = ResponseMasker()
        self.diff_engine = DiffEngine()
        self.reporter = HarnessReporter()
        # route 解析用共用的 RouteMapper，與 MutationVerifier 共用同一套邏輯
        self.route_mapper = RouteMapper(config_path)

    def verify(self, collection_path: str) -> dict:
        """執行 newman 並與 golden output 比對（全量）"""
        return self.reporter.build_report(self.verify_raw(collection_path))

    def verify_raw(self, collection_path: str) -> list[dict]:
        """
        與 verify() 相同，但回傳尚未分類的原始 case 結果清單，不呼叫 build_report()。
        給 run_postman_tests（見 test_nodes.py）用來跟 MutationVerifier 的結果合併成
        單一 report，避免重複分類造成靜默誤判（見下方合併注意事項）。
        """
        newman_output = run_newman(collection_path, self.python_base_url)
        return self._process_executions(newman_output["run"]["executions"])

    def verify_module(self, collection_path: str, module_filter: str) -> dict:
        """只比對指定模組的 golden cases（用於局部驗證）"""
        newman_output = run_newman(collection_path, self.python_base_url)
        executions = newman_output["run"]["executions"]

        filtered = [
            ex for ex in executions
            if self._get_module(ex["item"]["request"]["url"]["path"]) == module_filter
        ]
        return self.reporter.build_report(self._process_executions(filtered))

    def _get_module(self, url_parts: list[str]) -> str:
        # 委派給共用的 get_module（module 詞彙表唯一權威來源，見 core/postman_runner.py）
        return get_module(url_parts)

    def _process_executions(self, executions: list[dict]) -> list[dict]:
        """回傳尚未分類（未呼叫 build_report）的原始 case 結果清單。"""
        results = []
        for execution in executions:
            item = execution["item"]
            actual_response = execution["response"]

            case_id = make_case_id(item)  # 共用函式，與錄製端演算法一致（含消毒）
            url_parts = item["request"]["url"]["path"]
            method = item["request"]["method"]
            module = self._get_module(url_parts)

            golden = self._load_golden(case_id, module)
            if golden is None:
                results.append({
                    "case_id": case_id,
                    "passed": False,
                    "error": "golden_not_found"
                })
                continue

            raw_body = actual_response.get("body")
            if raw_body is None or raw_body.strip() == "":
                actual_body = None
            else:
                try:
                    actual_body = json.loads(raw_body)
                except (json.JSONDecodeError, TypeError):
                    results.append({
                        "case_id": case_id,
                        "passed": False,
                        "error": "response_not_json"
                    })
                    continue
            # GoldenVerifier 只處理 readonly collection，context 用預設值 "readonly"——
            # 不遮罩 id 系欄位，讓「撈錯資料實體」這類 bug 能被真正比對到（見 mask_rules.yaml）。
            actual_masked = self.masker.mask(actual_body, context="readonly")

            # status_match 提前算好，供 passed／status_match 兩處共用。
            status_match = golden["response"]["status_code"] == actual_response["code"]
            diff = self.diff_engine.compare(
                expected=golden["response"]["body"],
                actual=actual_masked
            )

            results.append({
                "case_id": case_id,
                # passed 須同時滿足 status_match 與 body diff 為空（AND 關係），
                # 與 MutationVerifier._verify_one_raw() 的判斷邏輯一致
                # （見 02a 四章「通過標準」）。
                "passed": status_match and (diff is None),
                "expected_status": golden["response"]["status_code"],
                "actual_status": actual_response["code"],
                "status_match": status_match,
                "body_diff": diff,
                "related_files": self.route_mapper.resolve_related_files(method, url_parts)
            })

        return results

    def _load_golden(self, case_id: str, module: str) -> dict | None:
        golden_path = self.golden_dir / module / f"{case_id}.json"
        if not golden_path.exists():
            return None
        with open(golden_path) as f:
            return json.load(f)
```

---

## verifier/mutation_verifier.py

```python
import json
import yaml
from pathlib import Path
# 套件內部一律用帶 refactor_harness. 前綴的絕對匯入（見 02a 二章「匯入慣例」）
from refactor_harness.core.postman_runner import run_newman, list_top_level_folders, make_case_id, get_module
from refactor_harness.core.masker import ResponseMasker
from refactor_harness.core.diff_engine import DiffEngine
from refactor_harness.core.reporter import HarnessReporter
from refactor_harness.core.route_mapper import RouteMapper
from refactor_harness.fixtures.db_env import DbEnvironment

class MutationVerifier:
    """
    每個「頂層 folder」（＝一條完整鏈式情境：建立 → 查詢／修改 → 清理，見 02a 六章）
    獨立跑，跑之前先 reset DB。folder 內部的多個 request 共用同一次 newman run 的
    Postman Environment，讓建立步驟產生的 ID 能被同一 folder 內後續步驟引用；
    不同 folder 之間彼此不共用任何動態值。
    動態 ID 欄位（id、order_id 等）已由 ResponseMasker 統一抹平，
    因此 Mutation golden 可以正常比對 body 結構與非動態欄位的值。
    """
    def __init__(self, python_base_url: str, golden_dir: str,
                 test_dsn: str | None = None,
                 config_path: str = "config/harness.yaml"):
        """
        test_dsn 由呼叫端傳入，理由同 GoldenRecorder.__init__ 的說明——若各自
        去讀 harness.yaml 的靜態預設值，即使呼叫前已經用 state["test_dsn"]
        對 readonly 做過 apply_seed，這裡內部逐 folder 的 apply_seed 仍可能
        連到不同資料庫。呼叫端（test_nodes.py 的 run_postman_tests）一律
        傳入 state["test_dsn"]。

        同時讀取 {golden_dir}/_metadata.json 的 tainted_folders（見 02a 三章
        「Mutation 錄製異常偵測」、四章「排除已知異常的 folder」），取得 Recorder
        判定為不可信、整個 folder 都沒有寫入 golden 的情境清單，讓 verify_all_raw()
        能主動跳過，不誤判成 golden_not_found 失敗。_metadata.json 不存在時（例如
        尚未跑過 record_golden_output）視為沒有任何 tainted folder，不拋例外——
        這是正常的初次執行情境。
        """
        self.python_base_url = python_base_url
        self.golden_dir = Path(golden_dir)
        self.masker = ResponseMasker()
        self.diff_engine = DiffEngine()
        self.reporter = HarnessReporter()
        # 與 GoldenVerifier 共用同一套 route → related_files 解析
        self.route_mapper = RouteMapper(config_path)

        with open(config_path) as f:
            config = yaml.safe_load(f)

        self.test_dsn = test_dsn or config["databases"]["test"]["dsn"]
        self.tables = config["databases"]["test"]["tables_to_truncate"]
        self.collection_path = config["collections"]["mutation"]["path"]
        self.db = DbEnvironment(test_dsn=self.test_dsn)

        self._tainted_folder_names = self._load_tainted_folder_names()
        self._last_excluded_folders: list[str] = []

    def _load_tainted_folder_names(self) -> set[str]:
        metadata_path = self.golden_dir / "_metadata.json"
        if not metadata_path.exists():
            return set()
        with open(metadata_path) as f:
            metadata = json.load(f)
        return {t["folder"] for t in metadata.get("tainted_folders", [])}

    def verify_all(self) -> dict:
        """對 collection_mutation.json 的每一個頂層 folder 依序呼叫，彙整成單一 report
        （供 run_tests／全量驗證使用，見 test_nodes.py）。"""
        return self.reporter.build_report(
            self.verify_all_raw(), excluded_folders=self.get_excluded_folders()
        )

    def verify_all_raw(self) -> list[dict]:
        """
        與 verify_all() 相同，但回傳尚未分類的原始 case 結果清單，不呼叫 build_report()。
        給 run_postman_tests 用來跟 GoldenVerifier.verify_raw() 的結果合併成單一 report
        （故意不是「呼叫 verify_one() 拿 report 再合併」——原因見下方 _verify_one_raw 的說明）。

        自動跳過 self._tainted_folder_names 內的 folder（不執行 newman、不產生任何
        比對結果），見 02a 四章「排除已知異常的 folder」——這是必要行為，不是選配：
        不跳過的話，這些 case 會因為找不到 golden 被 golden_not_found 誤判成失敗，
        但那不是 Python 端的問題。實際跳過了哪些 folder 記錄在
        self._last_excluded_folders，供 get_excluded_folders() 讀取。
        """
        folder_names = list_top_level_folders(self.collection_path)
        all_raw_results = []
        excluded = []
        for folder_name in folder_names:
            if folder_name in self._tainted_folder_names:
                excluded.append(folder_name)
                continue
            all_raw_results.extend(self._verify_one_raw(folder_name))
        self._last_excluded_folders = excluded
        return all_raw_results

    def get_excluded_folders(self) -> list[str]:
        """回傳上一次 verify_all_raw() 實際跳過的 folder 名稱清單。"""
        return self._last_excluded_folders

    def verify_one(self, folder_name: str) -> dict:
        """
        單一頂層 folder（鏈式情境）獨立跑，跑之前先 reset DB。回傳已分類的 report。

        刻意不套用 verify_all_raw() 的 tainted folder 排除邏輯——除錯時可能就是
        想看某個被標記 tainted 的 folder 實際執行狀況（見 02a 四章）。
        """
        return self.reporter.build_report(self._verify_one_raw(folder_name))

    def _verify_one_raw(self, folder_name: str) -> list[dict]:
        """
        實際執行單一 folder 並回傳「尚未分類」的原始 case 結果清單，
        供 verify_one()／verify_all()／verify_all_raw() 各自決定要不要立刻 build_report()。

        拆成 raw／build_report 兩層的原因：build_report() 產生的 failure 物件欄位名稱
        （如 status_code_match）跟這裡的原始欄位（status_match）不同，若把已經
        build_report() 過的物件再丟進 build_report() 分類一次，會因為讀不到預期欄位、
        用預設值頂替而靜默誤判（例如把真正 fail 的 case 判斷成 status matched）。
        因此多 folder／多來源（readonly + mutation）合併時，一律先收集這裡的原始結果，
        最後只呼叫一次 build_report()。

        範例：POST /orders 的 Java golden 遮罩後為
          {"id": "<<MASKED>>", "status": "pending", "total": 120}
        若 Python 只回傳 {} 加 200，body diff 會抓到 missing_fields，
        不會因為只驗 status code 而 pass。
        """
        self.db.apply_seed("fixtures/seed.sql", self.tables)
        newman_output = run_newman(
            collection_path=self.collection_path,
            base_url=self.python_base_url,
            folder=folder_name
        )

        results = []
        for execution in newman_output["run"]["executions"]:
            item = execution["item"]
            actual_response = execution["response"]
            case_id = make_case_id(item)
            url_parts = item["request"]["url"]["path"]
            method = item["request"]["method"]
            module = self._get_module(url_parts)

            golden = self._load_golden(case_id, module)
            body_diff = None

            # 有 golden 時精確比對 status code（201 Created vs 200 OK 是 API
            # 行為的一部分，不能只用 200 <= code < 300 寬鬆判斷）。
            if golden:
                expected_status = golden["response"]["status_code"]
                status_match = actual_response["code"] == expected_status

                raw_body = actual_response.get("body")
                try:
                    actual_body = None if (raw_body is None or raw_body.strip() == "") else json.loads(raw_body)
                    # mutation 情境：額外遮罩 masked_fields_mutation_only（id/order_id/user_id），
                    # 因為兩端 DB 自增序列本來就不會給出相同值，見 mask_rules.yaml 說明。
                    actual_masked = self.masker.mask(actual_body, context="mutation") if actual_body is not None else None
                    body_diff = self.diff_engine.compare(
                        golden["response"]["body"], actual_masked
                    )
                except Exception as e:
                    body_diff = {"error": f"解析或比對失敗: {str(e)}"}
            else:
                # golden 不存在：正常情況是該 case 在錄製時被跳過
                # （非 JSON content-type 的回應，如純 204 No Content，見 golden_writer
                # 的 _build_golden——這類 case 出現在 _metadata.json 的 skipped 清單。
                # 若是因為整個 folder 被判定 tainted，這個分支根本不會被執行到，
                # 因為 verify_all_raw() 在跑到這裡之前就已經整個 folder 跳過了）。
                # 此時退回保守的 2xx sanity check，不能像 GoldenVerifier 那樣
                # 一律標成 golden_not_found 失敗，否則所有被合理跳過的 204 endpoint
                # 會永久 fail；也不能寫死 expected=200，否則 204 一樣永久 fail。
                expected_status = None
                status_match = 200 <= actual_response["code"] < 300

            results.append({
                "case_id": case_id,
                "passed": status_match and (body_diff is None),
                "expected_status": expected_status,
                "actual_status": actual_response["code"],
                "status_match": status_match,
                "body_diff": body_diff,
                # 解析 related_files（與 GoldenVerifier 共用 RouteMapper），
                # Debug Agent 不用自行從 case_id 推斷對應檔案。
                "related_files": self.route_mapper.resolve_related_files(method, url_parts),
            })

        return results

    def _get_module(self, url_parts: list[str]) -> str:
        # 委派給共用的 get_module（module 詞彙表唯一權威來源，見 core/postman_runner.py）
        return get_module(url_parts)

    def _load_golden(self, case_id: str, module: str) -> dict | None:
        golden_path = self.golden_dir / module / f"{case_id}.json"
        if not golden_path.exists():
            return None
        with open(golden_path) as f:
            return json.load(f)
```

---

## langgraph_nodes/test_nodes.py

> 提供 LangGraph 的 `record_tests`／`run_tests` 兩個 node 函式，以及條件邊判斷函式 `should_debug_or_done`。State 定義見 `01_langgraph_architecture.md` 三章；module 級局部驗證與 regression 重驗的實作在 `01` 六章的 `implement_node.py`（`_partial_verify` + `ModuleScheduler`），本檔不重複維護第二份局部驗證邏輯。圖形接線見 `01` 五章的 `graph/builder.py`。

```python
import os
import yaml
from graph.state import RefactorState
from refactor_harness.recorder.golden_writer import GoldenRecorder
from refactor_harness.verifier.comparator import GoldenVerifier
from refactor_harness.verifier.mutation_verifier import MutationVerifier
from refactor_harness.core.reporter import HarnessReporter
from refactor_harness.fixtures.db_env import DbEnvironment

with open("config/harness.yaml") as f:
    HARNESS_CONFIG = yaml.safe_load(f)

TABLES = HARNESS_CONFIG["databases"]["test"]["tables_to_truncate"]
MAX_RETRY = 3  # 已定案（見 00 九、State 表格 retry_count），如需調整直接改這個常數

# Java 服務位置不放進 RefactorState（性質同 DATABASE_URL，是服務/外部工具自己讀的環境變數，
# 不是 Orchestrator 決策要用的資料），直接讀 .env，做法與 01 九章 main.py 讀 OLLAMA_BASE_URL 一致。
JAVA_BASE_URL = os.environ["JAVA_BASE_URL"]


# ── Agent ②：錄製 golden output（readonly + mutation）──
def record_golden_output(state: RefactorState) -> RefactorState:
    """
    分別呼叫 record()（readonly）與 record_mutation()（mutation），兩者的 DB
    reset 策略不同（見 00 六章 Seed 策略），已封裝在 GoldenRecorder 內部，
    這裡不重複 apply_seed；跑完後用 write_metadata() 合併寫一份 _metadata.json
    （分開呼叫各自的 record 不會互相覆蓋，見 GoldenRecorder 定義）。

    test_dsn 傳入 state["test_dsn"]，確保 Recorder 與 run_postman_tests 的
    Verifier 連到同一顆測試 DB（見 02a 十四章）。
    """
    recorder = GoldenRecorder(
        java_base_url=JAVA_BASE_URL,
        golden_dir="fixtures/golden",
        test_dsn=state["test_dsn"],
    )
    readonly_result = recorder.record("postman/collection_readonly.json")
    mutation_result = recorder.record_mutation("postman/collection_mutation.json")
    recorder.write_metadata(readonly_result, mutation_result)

    return {
        **state,
        "golden_output": {
            "readonly": readonly_result,
            "mutation": mutation_result,
        },
    }


# ── Agent ⑥：驗證 Python 服務（全量，跨模組 regression 的最終防線）──
def run_postman_tests(state: RefactorState) -> RefactorState:
    """
    readonly 與 mutation 的原始結果（尚未分類）合併後只呼叫一次 build_report()
    ——不能各自 build_report() 後再合併，欄位名稱不同、二次分類會靜默誤判
    （見 comparator.py／mutation_verifier.py 的說明）。只要其中一份有
    failure，整體 status 就是 fail，避免「readonly 全過但 mutation 其實在
    噴 500」被誤判為整體通過。

    MutationVerifier 內部逐 folder 自行 apply_seed（見 02a 六章），這裡不
    需要在呼叫前再 apply_seed 一次。test_dsn 統一傳入 state["test_dsn"]，
    確保 readonly／mutation 兩條路徑與 Recorder 連到同一顆測試 DB。

    mutation_verifier.verify_all_raw() 內部已自動排除 tainted folder（見
    MutationVerifier、02a 四章「排除已知異常的 folder」）；跑完後透過
    get_excluded_folders() 取得這次實際跳過的 folder 清單，一併傳進
    build_report()，讓最終 report 帶有 excluded_folders 欄位（見 02a 九章）。
    """
    db = DbEnvironment(test_dsn=state["test_dsn"])
    db.apply_seed("fixtures/seed.sql", tables_to_truncate=TABLES)

    verifier = GoldenVerifier(
        python_base_url=state["python_base_url"],
        golden_dir="fixtures/golden"
    )
    readonly_raw = verifier.verify_raw("postman/collection_readonly.json")

    mutation_verifier = MutationVerifier(
        python_base_url=state["python_base_url"],
        golden_dir="fixtures/golden",
        test_dsn=state["test_dsn"],
    )
    mutation_raw = mutation_verifier.verify_all_raw()

    report = HarnessReporter().build_report(
        readonly_raw + mutation_raw,
        excluded_folders=mutation_verifier.get_excluded_folders(),
    )

    return {**state, "test_results": report}


# ── 條件邊：決定去 Debug、結束、還是通知人工 ────────────────────
def should_debug_or_done(state: RefactorState) -> str:
    """
    retry_count 只在 failed_modules（確實跑過、驗證過但沒過）非空時才計入
    重試判斷；blocked_modules（因上游未驗證通過而從未被排到）不影響這裡的
    判斷，那是排程還沒排到，不是「這次寫錯了」，等對應的 failed_modules
    修好、上游重驗通過後，排程器（ModuleScheduler）會在下一輪 implement
    自然釋放，不需要額外的條件邊分支。

    state["test_results"]["status"] 故意不用 .get() 給預設值：能執行到這個
    條件邊，代表 run_postman_tests 已正常 return——若它中途拋例外（如
    Python 服務沒起來、newman 失敗），LangGraph 會直接讓例外傳播、整個
    graph 中斷，條件邊根本不會被呼叫；而只要它正常 return，test_results
    必然來自 build_report()，一定含有 status。若真的出現結構異常（例如
    未來有人改壞 build_report），KeyError 當場炸開才是正確行為——用
    .get(..., "fail") 之類的預設值反而會把結構性 bug 偽裝成普通測試失敗，
    白白消耗 retry_count 並誤導 Debug Agent。
    """
    report = state["test_results"]

    if report["status"] == "pass":
        return "done"

    failed_modules = state.get("failed_modules", [])
    if not failed_modules:
        # 只有 blocked_modules、沒有 failed_modules：不是程式碼寫錯，不消耗 retry_count，
        # 但全量測試仍是 fail，代表還有模組沒完工，回 debug 讓 ⑦ 判斷後續（通常會再進一次 implement）
        return "debug"

    if state["retry_count"] >= MAX_RETRY:
        return "give_up"  # 超過重試次數，交給 give_up node 通知人工（見 01 五章，不是直接 END）
    return "debug"


# ── module 級局部驗證：見 01_langgraph_architecture.md 六章 implement_node.py 的 _partial_verify ──
# 本檔不重複實作。record_golden_output／run_postman_tests 是全量驗證專用的獨立 node，
# implement_node 內部另外用同一組 GoldenVerifier／DbEnvironment 做 module 級局部驗證與 regression 重驗，
# 兩者共用底層 refactor_harness 套件，但呼叫時機與封裝位置不同，不應在本檔案重複維護第二份邏輯。
```

---

*本文件針對「Java → Python Multi-Agent 重構」情境設計，隨實作推進持續更新。*
