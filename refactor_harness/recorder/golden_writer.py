import json
import yaml
from datetime import datetime
from pathlib import Path
# 套件內部一律用帶 refactor_harness. 前綴的絕對匯入（見 02a 二章「匯入慣例」）
from refactor_harness.core.masker import ResponseMasker
from refactor_harness.core.postman_runner import run_newman, list_top_level_folders, make_case_id, get_module
from refactor_harness.fixtures.db_env import DbEnvironment


class GoldenRecorder:
    """
    readonly／mutation 分別呼叫 record()／record_mutation()，兩者的 DB reset
    粒度不同（見 02a 六章），各自封裝好對應的 apply_seed 邏輯。
    """
    def __init__(self, java_base_url: str, golden_dir: str,
                 test_dsn: str | None = None,
                 config_path: str = "config/harness.yaml"):
        """
        test_dsn 由呼叫端傳入，確保 Recorder 與 Verifier 連的是同一顆測試 DB；
        留 None 時才 fallback 讀 harness.yaml 的靜態預設值。
        """
        self.java_base_url = java_base_url
        self.golden_dir = Path(golden_dir)
        self.masker = ResponseMasker()

        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        self.test_dsn = test_dsn or config["databases"]["test"]["dsn"]
        self.tables = config["databases"]["test"]["tables_to_truncate"]
        self.db = DbEnvironment(test_dsn=self.test_dsn)

    def record(self, collection_path: str) -> dict:
        """
        錄製 readonly collection：DB 狀態全程不變，seed 一次即可跑完整份 collection。
        """
        self.db.apply_seed("fixtures/seed.sql", self.tables)
        newman_output = run_newman(collection_path, self.java_base_url)
        recorded, skipped = self._record_executions(newman_output["run"]["executions"])
        return {
            "recorded_count": len(recorded),
            "skipped_count": len(skipped),
            "cases": recorded,
            "skipped": skipped
        }

    def record_mutation(self, collection_path: str) -> dict:
        """
        錄製 mutation collection 的 golden output。

        關鍵差異（對應 02a 六章的鏈式依賴設計）：mutation collection 的每個「頂層 folder」
        代表一條完整的鏈式情境，每個 folder 開跑前都要重新 apply_seed。
        """
        folder_names = list_top_level_folders(collection_path)
        recorded, skipped = [], []
        for folder_name in folder_names:
            self.db.apply_seed("fixtures/seed.sql", self.tables)
            newman_output = run_newman(
                collection_path, self.java_base_url, folder=folder_name
            )
            folder_recorded, folder_skipped = self._record_executions(
                newman_output["run"]["executions"], context="mutation"
            )
            recorded += folder_recorded
            skipped += folder_skipped
        return {
            "recorded_count": len(recorded),
            "skipped_count": len(skipped),
            "cases": recorded,
            "skipped": skipped
        }

    def write_metadata(self, *results: dict):
        """
        合併 record() 與 record_mutation() 的結果，寫一份 _metadata.json。
        """
        recorded = [c for r in results for c in r["cases"]]
        skipped = [c for r in results for c in r["skipped"]]
        metadata = {
            "recorded_at": datetime.utcnow().isoformat() + "Z",
            "total_cases": len(recorded),
            "cases": recorded,
            "skipped": skipped
        }
        with open(self.golden_dir / "_metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

    def _record_executions(self, executions: list[dict], context: str = "readonly") -> tuple[list[str], list[str]]:
        recorded, skipped = [], []
        for execution in executions:
            item = execution["item"]
            response = execution["response"]

            case_id = make_case_id(item)
            golden = self._build_golden(item, response, context)

            if golden is None:
                skipped.append(case_id)
                continue

            self._write_golden(case_id, golden)
            recorded.append(case_id)
        return recorded, skipped

    def _build_golden(self, request_item: dict, response: dict, context: str = "readonly") -> dict | None:
        """
        解析單一 execution 的 response。
        非 JSON response 一律跳過。
        """
        headers = {h["key"].lower(): h["value"]
                   for h in (response.get("headers", {}).get("members", []))}
        content_type = headers.get("content-type", "")

        if "application/json" not in content_type:
            return None

        raw_body = response.get("body")
        if raw_body is None or raw_body.strip() == "":
            body = None
        else:
            try:
                body = json.loads(raw_body)
            except (json.JSONDecodeError, TypeError):
                return None

        masked_body = self.masker.mask(body, context=context) if body is not None else None

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

    def _write_golden(self, case_id: str, golden: dict):
        module = get_module(golden["request"]["path"])
        dir_path = self.golden_dir / module
        dir_path.mkdir(parents=True, exist_ok=True)

        with open(dir_path / f"{case_id}.json", "w", encoding="utf-8") as f:
            json.dump(golden, f, ensure_ascii=False, indent=2)
