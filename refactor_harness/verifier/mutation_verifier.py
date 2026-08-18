import json
import yaml
from pathlib import Path
# 套件內部一律用帶 refactor_harness. 前綴的絕對匯入
from refactor_harness.core.postman_runner import extract_response_body, run_newman, list_top_level_folders, make_case_id
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

        with open(config_path, encoding="utf-8") as f:
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
        with open(metadata_path, encoding="utf-8") as f:
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
            module = self._get_module(method, url_parts)

            golden = self._load_golden(case_id, module)
            body_diff = None

            # 有 golden 時精確比對 status code（201 Created vs 200 OK 是 API
            # 行為的一部分，不能只用 200 <= code < 300 寬鬆判斷）。
            if golden:
                expected_status = golden["response"]["status_code"]
                status_match = actual_response["code"] == expected_status

                raw_body = extract_response_body(actual_response)
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

    def _get_module(self, method: str, url_parts: list[str]) -> str:
        # 委派給共用的 RouteMapper.resolve_module()（module 詞彙表唯一
        # 權威來源，見 02a 十三章），與 GoldenVerifier 共用。
        return self.route_mapper.resolve_module(method, url_parts)

    def _load_golden(self, case_id: str, module: str) -> dict | None:
        golden_path = self.golden_dir / module / f"{case_id}.json"
        if not golden_path.exists():
            return None
        with open(golden_path, encoding="utf-8") as f:
            return json.load(f)
