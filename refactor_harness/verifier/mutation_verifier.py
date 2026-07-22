import json
import yaml
from pathlib import Path
# 套件內部一律用帶 refactor_harness. 前綴的絕對匯入
from refactor_harness.core.postman_runner import run_newman, list_top_level_folders, make_case_id, get_module
from refactor_harness.core.masker import ResponseMasker
from refactor_harness.core.diff_engine import DiffEngine
from refactor_harness.core.reporter import HarnessReporter
from refactor_harness.core.route_mapper import RouteMapper
from refactor_harness.fixtures.db_env import DbEnvironment


class MutationVerifier:
    """
    每個「頂層 folder」（＝一條完整鏈式情境）獨立跑，跑之前先 reset DB。
    folder 內部的多個 request 共用同一次 newman run 的 Postman Environment。
    動態 ID 欄位已由 ResponseMasker 統一抹平。
    """
    def __init__(self, python_base_url: str, golden_dir: str,
                 test_dsn: str | None = None,
                 config_path: str = "config/harness.yaml"):
        """
        test_dsn 由呼叫端傳入。
        """
        self.python_base_url = python_base_url
        self.golden_dir = Path(golden_dir)
        self.masker = ResponseMasker()
        self.diff_engine = DiffEngine()
        self.reporter = HarnessReporter()
        # 與 GoldenVerifier 共用同一套 route → related_files 解析
        self.route_mapper = RouteMapper(config_path)

        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        self.test_dsn = test_dsn or config["databases"]["test"]["dsn"]
        self.tables = config["databases"]["test"]["tables_to_truncate"]
        self.collection_path = config["collections"]["mutation"]["path"]
        self.db = DbEnvironment(test_dsn=self.test_dsn)

    def verify_all(self) -> dict:
        """對 collection_mutation.json 的每一個頂層 folder 依序呼叫，彙整成單一 report"""
        return self.reporter.build_report(self.verify_all_raw())

    def verify_all_raw(self) -> list[dict]:
        """
        與 verify_all() 相同，但回傳尚未分類的原始 case 結果清單。
        給 run_postman_tests 用來跟 GoldenVerifier.verify_raw() 的結果合併。
        """
        folder_names = list_top_level_folders(self.collection_path)
        all_raw_results = []
        for folder_name in folder_names:
            all_raw_results.extend(self._verify_one_raw(folder_name))
        return all_raw_results

    def verify_one(self, folder_name: str) -> dict:
        """單一頂層 folder 獨立跑，回傳已分類的 report。"""
        return self.reporter.build_report(self._verify_one_raw(folder_name))

    def _verify_one_raw(self, folder_name: str) -> list[dict]:
        """
        實際執行單一 folder 並回傳「尚未分類」的原始 case 結果清單。
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

            if golden:
                expected_status = golden["response"]["status_code"]
                status_match = actual_response["code"] == expected_status

                raw_body = actual_response.get("body")
                try:
                    actual_body = None if (raw_body is None or raw_body.strip() == "") else json.loads(raw_body)
                    # mutation 情境：額外遮罩 masked_fields_mutation_only
                    actual_masked = self.masker.mask(actual_body, context="mutation") if actual_body is not None else None
                    body_diff = self.diff_engine.compare(
                        golden["response"]["body"], actual_masked
                    )
                except Exception as e:
                    body_diff = {"error": f"解析或比對失敗: {str(e)}"}
            else:
                expected_status = None
                status_match = 200 <= actual_response["code"] < 300

            results.append({
                "case_id": case_id,
                "passed": status_match and (body_diff is None),
                "expected_status": expected_status,
                "actual_status": actual_response["code"],
                "status_match": status_match,
                "body_diff": body_diff,
                "related_files": self.route_mapper.resolve_related_files(method, url_parts),
            })

        return results

    def _get_module(self, url_parts: list[str]) -> str:
        # 委派給共用的 get_module
        return get_module(url_parts)

    def _load_golden(self, case_id: str, module: str) -> dict | None:
        golden_path = self.golden_dir / module / f"{case_id}.json"
        if not golden_path.exists():
            return None
        with open(golden_path, encoding="utf-8") as f:
            return json.load(f)
