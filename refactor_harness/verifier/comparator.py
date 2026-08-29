import json
from pathlib import Path
# 套件內部一律用帶 refactor_harness. 前綴的絕對匯入（見 02a 二章「匯入慣例」）
from refactor_harness.core.masker import ResponseMasker
from refactor_harness.core.diff_engine import DiffEngine
from refactor_harness.core.reporter import HarnessReporter
from refactor_harness.core.postman_runner import extract_response_body, run_newman, make_case_id
from refactor_harness.core.route_mapper import RouteMapper


class GoldenVerifier:
    def __init__(self, python_base_url: str, golden_dir: str,
                 config_path: str = "config/harness.yaml",
                 route_to_module_mapping: dict[str, str] | None = None):
        """route_to_module_mapping：對應 docs/09b_bug_trace.md #64，留
        None 才 fallback 讀 harness.yaml 當下版本，見 RouteMapper.__init__
        docstring；呼叫端有 state 可用時應該直接傳 `design_agent.
        route_mapping.build_route_to_module_mapping(state["api_to_python_target"])`。
        """
        self.python_base_url = python_base_url
        self.golden_dir = Path(golden_dir)
        self.masker = ResponseMasker()
        self.diff_engine = DiffEngine()
        self.reporter = HarnessReporter()
        # route 解析用共用的 RouteMapper，與 MutationVerifier 共用同一套邏輯
        self.route_mapper = RouteMapper(config_path, module_mapping_override=route_to_module_mapping)
        # Recorder 錄製時主動判定為非 JSON（如 text/plain 的 /version 端點）
        # 而跳過、從未寫入 golden 的 case_id 清單——見 _load_skipped_case_ids()。
        self._skipped_case_ids = self._load_skipped_case_ids()
        self._last_excluded_cases: list[str] = []

    def _load_skipped_case_ids(self) -> set[str]:
        """讀取 {golden_dir}/_metadata.json 的 skipped 清單（見 02a 三章
        「非 JSON Response 與空 Body 的處理」）——這些 case 在錄製當下就
        被 Recorder 主動判定成非 JSON 而跳過，不是 golden 遺失或
        route_to_file_mapping 設定錯誤。沒有這份清單時，_process_
        executions() 會把它們誤判成 golden_not_found 失敗，即使 Python
        端回傳的內容其實跟 Java 一致（如 /version 端點兩邊都回傳同一種
        text/plain 版本字串）。_metadata.json 不存在時（例如尚未跑過
        record_golden_output）視為沒有任何已知跳過的 case，不拋例外——
        這是正常的初次執行情境，比照 MutationVerifier 對
        tainted_folders 的既有處理方式。
        """
        metadata_path = self.golden_dir / "_metadata.json"
        if not metadata_path.exists():
            return set()
        with open(metadata_path, encoding="utf-8") as f:
            metadata = json.load(f)
        return set(metadata.get("skipped", []))

    def get_excluded_cases(self) -> list[str]:
        """上一次 verify()／verify_raw()／verify_module() 呼叫中，因命中
        _skipped_case_ids 而被排除、沒有計入 summary／failures 的
        case_id 清單。"""
        return self._last_excluded_cases

    def verify(self, collection_path: str) -> dict:
        """執行 newman 並與 golden output 比對（全量）"""
        return self.reporter.build_report(
            self.verify_raw(collection_path), excluded_cases=self.get_excluded_cases()
        )

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
        return self.reporter.build_report(
            self._process_executions(filtered), excluded_cases=self.get_excluded_cases()
        )

    def _get_module(self, method: str, url_parts: list[str]) -> str:
        # 委派給共用的 RouteMapper.resolve_module()（見 02a 十三章）
        return self.route_mapper.resolve_module(method, url_parts)

    def _process_executions(self, executions: list[dict]) -> list[dict]:
        """回傳尚未分類的原始 case 結果清單。每次呼叫重新計算
        self._last_excluded_cases（覆蓋，不累加）——跟 MutationVerifier
        的 _last_excluded_folders 是同一種「每次呼叫都是一份新快照」
        的既有慣例。
        """
        results = []
        excluded: list[str] = []
        for execution in executions:
            item = execution["item"]
            actual_response = execution["response"]

            case_id = make_case_id(item)
            url_parts = item["request"]["url"]["path"]
            method = item["request"]["method"]
            module = self._get_module(method, url_parts)

            golden = self._load_golden(case_id, module)
            if golden is None:
                if case_id in self._skipped_case_ids:
                    # Recorder 錄製時就判定這個 case 是非 JSON 而主動跳過
                    # （見 __init__ 的 _load_skipped_case_ids()），不是
                    # golden 遺失或 route_to_file_mapping 設定錯誤——不算
                    # 失敗，直接排除，不計入 summary／failures。
                    excluded.append(case_id)
                    continue
                results.append({
                    "case_id": case_id,
                    "module": module,
                    "passed": False,
                    "error": "golden_not_found",
                    "related_files": self.route_mapper.resolve_related_files(method, url_parts),
                })
                continue

            raw_body = extract_response_body(actual_response)
            if raw_body is None or raw_body.strip() == "":
                actual_body = None
            else:
                try:
                    actual_body = json.loads(raw_body)
                except (json.JSONDecodeError, TypeError):
                    results.append({
                        "case_id": case_id,
                        "module": module,
                        "passed": False,
                        "error": "response_not_json",
                        "related_files": self.route_mapper.resolve_related_files(method, url_parts),
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
                "module": module,
                "passed": status_match and (diff is None),
                "expected_status": golden["response"]["status_code"],
                "actual_status": actual_response["code"],
                "status_match": status_match,
                "body_diff": diff,
                "related_files": self.route_mapper.resolve_related_files(method, url_parts)
            })

        self._last_excluded_cases = excluded
        return results

    def _load_golden(self, case_id: str, module: str) -> dict | None:
        golden_path = self.golden_dir / module / f"{case_id}.json"
        if not golden_path.exists():
            return None
        with open(golden_path, encoding="utf-8") as f:
            return json.load(f)
