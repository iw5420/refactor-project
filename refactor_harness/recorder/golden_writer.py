import json
import yaml
from datetime import datetime
from pathlib import Path
# 套件內部一律用帶 refactor_harness. 前綴的絕對匯入（見 02a 二章「匯入慣例」）
from refactor_harness.core.masker import ResponseMasker
from refactor_harness.core.postman_runner import run_newman, list_top_level_folders, make_case_id
from refactor_harness.core.route_mapper import RouteMapper
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
        # module 分區用共用的 RouteMapper.resolve_module()，與 GoldenVerifier／
        # MutationVerifier 共用同一套（見 core/route_mapper.py、02a 十三章）。
        self.route_mapper = RouteMapper(config_path)

        with open(config_path, encoding="utf-8") as f:
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
        with open(self.golden_dir / "_metadata.json", "w", encoding="utf-8") as f:
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
        # module 分區用共用的 RouteMapper.resolve_module()（見
        # core/route_mapper.py），與驗證端的載入／過濾用同一套詞彙。
        module = self.route_mapper.resolve_module(
            golden["request"]["method"], golden["request"]["path"]
        )
        dir_path = self.golden_dir / module
        dir_path.mkdir(parents=True, exist_ok=True)

        with open(dir_path / f"{case_id}.json", "w", encoding="utf-8") as f:
            json.dump(golden, f, ensure_ascii=False, indent=2)
