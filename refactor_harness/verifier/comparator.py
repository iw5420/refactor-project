import json
from pathlib import Path
# 套件內部一律用帶 refactor_harness. 前綴的絕對匯入（見 02a 二章「匯入慣例」）
from refactor_harness.core.masker import ResponseMasker
from refactor_harness.core.diff_engine import DiffEngine
from refactor_harness.core.reporter import HarnessReporter
from refactor_harness.core.postman_runner import run_newman, make_case_id
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
        給 run_postman_tests 用來跟 MutationVerifier 的結果合併成單一 report。
        """
        newman_output = run_newman(collection_path, self.python_base_url)
        return self._process_executions(newman_output["run"]["executions"])

    def verify_module(self, collection_path: str, module_filter: str) -> dict:
        """只比對指定模組的 golden cases（用於局部驗證）"""
        newman_output = run_newman(collection_path, self.python_base_url)
        executions = newman_output["run"]["executions"]

        filtered = [
            ex for ex in executions
            if self._get_module(
                ex["item"]["request"]["method"], ex["item"]["request"]["url"]["path"]
            ) == module_filter
        ]
        return self.reporter.build_report(self._process_executions(filtered))

    def _get_module(self, method: str, url_parts: list[str]) -> str:
        # 委派給共用的 RouteMapper.resolve_module()（見 02a 十三章）
        return self.route_mapper.resolve_module(method, url_parts)

    def _process_executions(self, executions: list[dict]) -> list[dict]:
        """回傳尚未分類的原始 case 結果清單。"""
        results = []
        for execution in executions:
            item = execution["item"]
            actual_response = execution["response"]

            case_id = make_case_id(item)
            url_parts = item["request"]["url"]["path"]
            method = item["request"]["method"]
            module = self._get_module(method, url_parts)

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
            # GoldenVerifier 只處理 readonly collection，context 用預設值 "readonly"
            actual_masked = self.masker.mask(actual_body, context="readonly")

            # status_match 提前算好，供 passed／status_match 兩處共用。
            status_match = golden["response"]["status_code"] == actual_response["code"]
            diff = self.diff_engine.compare(
                expected=golden["response"]["body"],
                actual=actual_masked
            )

            results.append({
                "case_id": case_id,
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
        with open(golden_path, encoding="utf-8") as f:
            return json.load(f)
