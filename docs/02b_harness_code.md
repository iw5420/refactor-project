# Harness 程式碼：Java → Python Multi-Agent 重構情境

> 本文件收錄所有 Harness 的程式碼與設定檔，按檔案路徑組織。
> 架構說明與流程設計請見配套文件：`02a_harness_architecture.md`

---

## 目錄

| 檔案 | 說明 |
|---|---|
| `config/harness.yaml` | 全域設定（DB、服務、collection、路由對應、diff 規則） |
| `config/mask_rules.yaml` | 動態欄位遮罩規則 |
| `core/postman_runner.py` | Newman 共用執行器 |
| `core/masker.py` | Response 動態欄位遮罩 |
| `core/diff_engine.py` | Diff 比對邏輯 |
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
    dsn: "postgresql://localhost/myapp"      # 原始 DB，Harness 絕不連這個
  test:
    dsn: "postgresql://localhost/myapp_test" # 測試專用，所有 Harness 操作都在這
    tables_to_truncate:                      # 統一管理，不寫死在程式碼裡
      - order_items                          # 順序不重要，TRUNCATE CASCADE 自動處理外鍵
      - orders
      - users
      # 未來新增資料表只需在這裡加一行，程式碼不需要改

services:
  java:
    base_url: "http://localhost:8080"
    db_env_var: "DB_URL"
  python:
    base_url: "http://localhost:8000"
    db_env_var: "DB_URL"

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
    # ⚠️  路徑格式必須用 bracket 記法（與 DeepDiff level.path() 一致）
    #    ✅  正確："root['data']['items']"
    #    ❌  錯誤："root.data.items"（dot 記法不會被 level.path() 匹配到）
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
route_to_file_mapping:
  "GET_api_v1_users":
    - "src/routers/user_router.py"
    - "src/services/user_service.py"
    - "src/repositories/user_repository.py"
  "GET_api_v1_users_{id}":
    - "src/routers/user_router.py"
    - "src/services/user_service.py"
  "GET_api_v1_users_{id}_profiles":   # ← 巢狀資源，精確 key，不會被上一條假匹配
    - "src/routers/profile_router.py"
    - "src/services/profile_service.py"
  "POST_api_v1_orders":
    - "src/routers/order_router.py"
    - "src/services/order_service.py"
```

---

## config/mask_rules.yaml

```yaml
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
  # Mutation golden 專用：寫入操作回傳的動態 ID 欄位
  # （讓 MutationVerifier 可以比對 body 結構，而不是完全跳過 body 驗證）
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
```

---

## core/masker.py

```python
import re
from typing import Any
import yaml

class ResponseMasker:
    def __init__(self, rules_path: str = "config/mask_rules.yaml"):
        with open(rules_path) as f:
            rules = yaml.safe_load(f)
        self.masked_fields = set(rules.get("masked_fields", []))
        self.masked_patterns = rules.get("masked_patterns", [])
        self.MASK = "<<MASKED>>"

    def mask(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: self.MASK if k in self.masked_fields else self.mask(v)
                for k, v in obj.items()
            }
        elif isinstance(obj, list):
            return [self.mask(item) for item in obj]
        elif isinstance(obj, str):
            for pattern in self.masked_patterns:
                if re.match(pattern, obj):
                    return self.MASK
        return obj
```

---

## core/diff_engine.py

```python
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

    def compare(self, expected: dict, actual: dict) -> dict | None:
        """
        比對 expected 和 actual。
        回傳 None 表示完全一致；回傳 dict 表示有差異。

        陣列比對策略：
        - ignore_order_at 指定的路徑：使用 DeepDiff ignore_order_func 精準忽略
        - 其餘路徑：order-sensitive（順序不同 = diff）

        改用 DeepDiff 原生的 ignore_order_func，不再手動改動資料。
        """
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

## core/reporter.py

```python
class HarnessReporter:
    def build_report(self, results: list[dict]) -> dict:
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
            "passed_cases": [p["case_id"] for p in passed]
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
            if "missing_fields" in str(diff):
                return "missing_fields"
            if "type_changed" in str(diff):
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
        "src/routers/users_router.py",
        "src/services/users_service.py",
        "src/repositories/users_repository.py"
      ],
      "debug_hint": "Response 缺少欄位，檢查 Pydantic schema 定義是否完整"
    }
  ]
}
```

---

## fixtures/db_env.py

```python
import subprocess
import psycopg2

class DbEnvironment:
    def __init__(self, test_dsn: str):
        # 永遠只連 myapp_test，絕不連 myapp
        self.test_dsn = test_dsn

    def apply_seed(self, seed_file: str, tables_to_truncate: list[str]):
        """
        每次測試前執行：
        1. 清除 myapp_test 的指定資料表（不影響 myapp）
        2. 重新注入 seed data

        tables_to_truncate 從 config/harness.yaml 讀入，不寫死在程式碼裡。

        TRUNCATE 用 psycopg2 單一指令，CASCADE 自動處理外鍵順序，避免多次鎖表。
        seed 注入用 psql subprocess：psycopg2 的 execute() 不支援一次執行多條 SQL。
        連線用 context manager 確保例外時不洩漏資源。
        """
        # TRUNCATE：psycopg2 單一指令，CASCADE 自動處理外鍵順序
        with psycopg2.connect(self.test_dsn) as conn:
            with conn.cursor() as cur:
                tables_sql = ", ".join(tables_to_truncate)
                cur.execute(f"TRUNCATE TABLE {tables_sql} RESTART IDENTITY CASCADE")
            conn.commit()

        # seed 注入：psql subprocess，正確支援多條 SQL 語句
        result = subprocess.run(
            ["psql", self.test_dsn, "-f", seed_file],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"seed.sql 注入失敗\n"
                f"file: {seed_file}\n"
                f"stderr: {result.stderr[:500]}"
            )

    def sync_schema(self, source_dsn: str):
        """
        當 myapp 的 schema 有變動時，同步到 myapp_test。
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
from datetime import datetime
from pathlib import Path
from core.masker import ResponseMasker
from core.postman_runner import run_newman

class GoldenRecorder:
    def __init__(self, java_base_url: str, golden_dir: str):
        self.java_base_url = java_base_url
        self.golden_dir = Path(golden_dir)
        self.masker = ResponseMasker()

    def record(self, collection_path: str) -> dict:
        """執行 newman 並記錄所有 API 的 golden output"""
        newman_output = run_newman(collection_path, self.java_base_url)

        recorded = []
        skipped = []
        for execution in newman_output["run"]["executions"]:
            item = execution["item"]
            response = execution["response"]

            case_id = self._make_case_id(item)
            golden = self._build_golden(item, response)

            if golden is None:
                skipped.append(case_id)  # 非 JSON response，記錄但跳過
                continue

            self._write_golden(case_id, golden)
            recorded.append(case_id)

        self._write_metadata(recorded, skipped)
        return {
            "recorded_count": len(recorded),
            "skipped_count": len(skipped),
            "cases": recorded,
            "skipped": skipped  # 供人工確認哪些 API 沒被錄製
        }

    def _build_golden(self, request_item: dict, response: dict) -> dict | None:
        """
        解析單一 execution 的 response。
        非 JSON response（二進位、空回應、HTML error page）一律跳過。
        """
        headers = {h["key"].lower(): h["value"]
                   for h in (response.get("headers", {}).get("members", []))}
        content_type = headers.get("content-type", "")

        if "application/json" not in content_type:
            return None

        raw_body = response.get("body") or "{}"
        try:
            body = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            return None

        masked_body = self.masker.mask(body)

        return {
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

    def _make_case_id(self, item: dict) -> str:
        """
        格式：{item_name}_{method}_{path}
        範例：create_order_POST_api_v1_orders
        """
        item_name = item.get("name", "unnamed").replace(" ", "_").lower()
        method = item["request"]["method"]
        path = "_".join(item["request"]["url"]["path"])
        return f"{item_name}_{method}_{path}"

    def _write_golden(self, case_id: str, golden: dict):
        url_parts = golden["request"]["path"]
        module = next(
            (p for p in url_parts if p not in ("api", "v1", "v2")),
            "misc"
        )
        dir_path = self.golden_dir / module
        dir_path.mkdir(parents=True, exist_ok=True)

        with open(dir_path / f"{case_id}.json", "w") as f:
            json.dump(golden, f, ensure_ascii=False, indent=2)

    def _write_metadata(self, recorded: list[str], skipped: list[str]):
        metadata = {
            "recorded_at": datetime.utcnow().isoformat() + "Z",
            "total_cases": len(recorded),
            "cases": recorded,
            "skipped": skipped
        }
        with open(self.golden_dir / "_metadata.json", "w") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
```

---

## verifier/comparator.py

```python
import json
import re
import yaml
from pathlib import Path
from core.masker import ResponseMasker
from core.diff_engine import DiffEngine
from core.reporter import HarnessReporter
from core.postman_runner import run_newman

class GoldenVerifier:
    def __init__(self, python_base_url: str, golden_dir: str,
                 config_path: str = "config/harness.yaml"):
        self.python_base_url = python_base_url
        self.golden_dir = Path(golden_dir)
        self.masker = ResponseMasker()
        self.diff_engine = DiffEngine()
        self.reporter = HarnessReporter()

        with open(config_path) as f:
            config = yaml.safe_load(f)
        self.route_mapping = config.get("route_to_file_mapping", {})

    def verify(self, collection_path: str) -> dict:
        """執行 newman 並與 golden output 比對（全量）"""
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
        return self._process_executions(filtered)

    def _get_module(self, url_parts: list[str]) -> str:
        return next(
            (p for p in url_parts if p not in ("api", "v1", "v2")),
            "misc"
        )

    def _process_executions(self, executions: list[dict]) -> dict:
        results = []
        for execution in executions:
            item = execution["item"]
            actual_response = execution["response"]

            case_id = self._make_case_id(item)
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

            raw_body = actual_response.get("body") or "{}"
            try:
                actual_body = json.loads(raw_body)
            except (json.JSONDecodeError, TypeError):
                results.append({
                    "case_id": case_id,
                    "passed": False,
                    "error": "response_not_json"
                })
                continue
            actual_masked = self.masker.mask(actual_body)

            diff = self.diff_engine.compare(
                expected=golden["response"]["body"],
                actual=actual_masked
            )

            results.append({
                "case_id": case_id,
                "passed": diff is None,
                "expected_status": golden["response"]["status_code"],
                "actual_status": actual_response["code"],
                "status_match": golden["response"]["status_code"] == actual_response["code"],
                "body_diff": diff,
                "related_files": self._resolve_related_files(method, url_parts)
            })

        return self.reporter.build_report(results)

    def _make_case_id(self, item: dict) -> str:
        """格式：{item_name}_{method}_{path}"""
        item_name = item.get("name", "unnamed").replace(" ", "_").lower()
        method = item["request"]["method"]
        path = "_".join(item["request"]["url"]["path"])
        return f"{item_name}_{method}_{path}"

    def _load_golden(self, case_id: str, module: str) -> dict | None:
        golden_path = self.golden_dir / module / f"{case_id}.json"
        if not golden_path.exists():
            return None
        with open(golden_path) as f:
            return json.load(f)

    def _normalize_path_key(self, method: str, url_parts: list[str]) -> str:
        """
        將 URL 中的動態段（純數字或 UUID）替換為 {id} 後再組 key。

        GET /api/v1/users/123/profiles → GET_api_v1_users_{id}_profiles
        harness.yaml key 也用 {id} 佔位，精確匹配就能區分巢狀資源。
        """
        normalized = []
        for part in url_parts:
            if re.fullmatch(r'\d+', part):
                normalized.append("{id}")
            elif re.fullmatch(
                r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', part
            ):
                normalized.append("{id}")
            else:
                normalized.append(part)
        return f"{method}_" + "_".join(normalized)

    def _resolve_related_files(self, method: str, url_parts: list[str]) -> list[str]:
        """
        從 method 和 url_parts 組出 normalized key，查 route_to_file_mapping。
        精確匹配優先，沒有才 fallback 到前綴匹配。
        """
        key = self._normalize_path_key(method, url_parts)

        if key in self.route_mapping:
            return self.route_mapping[key]

        for pattern, files in self.route_mapping.items():
            if key.startswith(pattern):
                return files

        return []
```

---

## verifier/mutation_verifier.py

```python
import json
import yaml
from pathlib import Path
from core.postman_runner import run_newman
from core.masker import ResponseMasker
from core.diff_engine import DiffEngine
from core.reporter import HarnessReporter
from fixtures.db_env import DbEnvironment

class MutationVerifier:
    """
    每個 mutation case 獨立跑，跑之前先 reset DB。
    動態 ID 欄位（id、order_id 等）已由 ResponseMasker 統一抹平，
    因此 Mutation golden 可以正常比對 body 結構與非動態欄位的值。
    """
    def __init__(self, python_base_url: str, golden_dir: str,
                 config_path: str = "config/harness.yaml"):
        self.python_base_url = python_base_url
        self.golden_dir = Path(golden_dir)
        self.masker = ResponseMasker()
        self.diff_engine = DiffEngine()
        self.reporter = HarnessReporter()

        with open(config_path) as f:
            config = yaml.safe_load(f)

        self.test_dsn = config["databases"]["test"]["dsn"]
        self.tables = config["databases"]["test"]["tables_to_truncate"]
        self.collection_path = config["collections"]["mutation"]["path"]
        self.db = DbEnvironment(test_dsn=self.test_dsn)

    def verify_one(self, folder_name: str) -> dict:
        """
        每個 mutation case 獨立跑，跑之前先 reset DB。

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
            case_id = self._make_case_id(item)
            module = self._get_module(item["request"]["url"]["path"])

            status_match = 200 <= actual_response["code"] < 300

            golden = self._load_golden(case_id, module)
            body_diff = None

            if golden:
                raw_body = actual_response.get("body") or "{}"
                try:
                    actual_body = json.loads(raw_body)
                    actual_masked = self.masker.mask(actual_body)
                    body_diff = self.diff_engine.compare(
                        golden["response"]["body"], actual_masked
                    )
                except Exception as e:
                    body_diff = {"error": f"解析或比對失敗: {str(e)}"}

            results.append({
                "case_id": case_id,
                "passed": status_match and (body_diff is None),
                "actual_status": actual_response["code"],
                "status_match": status_match,
                "body_diff": body_diff,
            })

        return self.reporter.build_report(results)

    def _make_case_id(self, item: dict) -> str:
        item_name = item.get("name", "unnamed").replace(" ", "_").lower()
        method = item["request"]["method"]
        path = "_".join(item["request"]["url"]["path"])
        return f"{item_name}_{method}_{path}"

    def _get_module(self, url_parts: list[str]) -> str:
        return next(
            (p for p in url_parts if p not in ("api", "v1", "v2")),
            "misc"
        )

    def _load_golden(self, case_id: str, module: str) -> dict | None:
        golden_path = self.golden_dir / module / f"{case_id}.json"
        if not golden_path.exists():
            return None
        with open(golden_path) as f:
            return json.load(f)
```

---

## langgraph_nodes/test_nodes.py

```python
import yaml
from refactor_harness.recorder.golden_writer import GoldenRecorder
from refactor_harness.verifier.comparator import GoldenVerifier
from refactor_harness.fixtures.db_env import DbEnvironment

with open("config/harness.yaml") as f:
    HARNESS_CONFIG = yaml.safe_load(f)

TABLES = HARNESS_CONFIG["databases"]["test"]["tables_to_truncate"]
MAX_RETRY = 3

# ── Agent ②：錄製 golden output ────────────────────────────
def record_golden_output(state: RefactorState) -> RefactorState:
    db = DbEnvironment(test_dsn=TEST_DB_DSN)
    db.apply_seed("fixtures/seed.sql", tables_to_truncate=TABLES)

    recorder = GoldenRecorder(
        java_base_url=JAVA_BASE_URL,
        golden_dir="fixtures/golden"
    )
    result = recorder.record("postman/collection_readonly.json")

    return {**state, "golden_output": result}


# ── Agent ⑥：驗證 Python 服務（全量）─────────────────────
def run_postman_tests(state: RefactorState) -> RefactorState:
    db = DbEnvironment(test_dsn=TEST_DB_DSN)
    db.apply_seed("fixtures/seed.sql", tables_to_truncate=TABLES)

    verifier = GoldenVerifier(
        python_base_url=PYTHON_BASE_URL,
        golden_dir="fixtures/golden"
    )
    report = verifier.verify("postman/collection_readonly.json")

    return {
        **state,
        "test_results": report,
        "retry_count": state.get("retry_count", 0)
    }


# ── 條件邊：決定去 Debug 還是結束 ────────────────────────────
def should_debug_or_done(state: RefactorState) -> str:
    report = state["test_results"]

    if report["status"] == "pass":
        return "done"
    if state["retry_count"] >= MAX_RETRY:
        return "give_up"  # 超過重試次數，人工介入
    return "debug"


# ── LangGraph 圖的條件邊設定 ─────────────────────────────
builder.add_conditional_edges(
    "run_tests",
    should_debug_or_done,
    {
        "done": END,
        "debug": "debug",
        "give_up": END  # 可改成通知人工的 node
    }
)
builder.add_edge("debug", "implement")


# ── 局部驗證：每個 task 完成後呼叫 ──────────────────────────
def partial_verify(task: dict, state: RefactorState) -> dict:
    """只跑與這個 task 相關的 readonly golden cases"""
    module = task.get("module")  # Agent ⑤ 在 task 定義時就應填入

    db = DbEnvironment(test_dsn=TEST_DB_DSN)
    db.apply_seed("fixtures/seed.sql", tables_to_truncate=TABLES)

    verifier = GoldenVerifier(
        python_base_url=PYTHON_BASE_URL,
        golden_dir="fixtures/golden"
    )
    report = verifier.verify_module(
        collection_path="postman/collection_readonly.json",
        module_filter=module
    )

    return {
        "task_id": task["id"],
        "module": module,
        "partial_report": report
    }
```

---

*本文件針對「Java → Python Multi-Agent 重構」情境設計，隨實作推進持續更新。*
