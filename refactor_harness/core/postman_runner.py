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

    with open(output_path, encoding="utf-8") as f:
        return json.load(f)


def list_top_level_folders(collection_path: str) -> list[str]:
    """
    讀取 Postman Collection JSON，回傳所有頂層 folder 的名稱。
    Recorder／Verifier 共用同一份實作，避免兩邊各自實作而漂移。

    每個頂層 folder＝一條完整鏈式情境，是 apply_seed／newman --folder 的執行單位
    （見 02a 六章）。Agent B 產生 collection_mutation.json 時，必須把有鏈式依賴的
    request 群組成同一個頂層 folder，而不是拆成各自獨立的 folder。
    """
    with open(collection_path, encoding="utf-8") as f:
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
      兩邊詞彙不一致時不會報錯，而是 verify_module 靜默漏測。
    """
    return next(
        (p for p in url_parts if p not in ("api", "v1", "v2")),
        "misc"
    )
