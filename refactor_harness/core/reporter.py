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
