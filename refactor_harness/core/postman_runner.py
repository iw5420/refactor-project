import json
import re
import shutil
import subprocess
import tempfile


def run_newman(collection_path: str, base_url: str, folder: str = None) -> dict:
    """
    執行 newman 並回傳解析後的 JSON 輸出。
    兩端（Recorder / Verifier）共用同一份，確保執行行為一致。

    若 newman 失敗（服務沒起來、collection 路徑錯誤）立即拋出明確例外，
    不讓錯誤靜默流入後續比對邏輯。

    **`shutil.which()` 解析完整路徑，不是直接傳字面字串 "newman"**：
    Windows 上全域 npm 套件的執行檔是 `newman.cmd`（batch wrapper），
    `subprocess.run(["newman", ...], shell=False)` 不會自動嘗試附加
    `.cmd`／`.exe` 等副檔名去 PATH 上找，會直接 `FileNotFoundError`，
    即使 `newman` 明明能在命令列打字執行——命令列底下是 shell 自己做了
    這層副檔名比對，`subprocess.run(shell=False)` 沒有這層行為。
    `shutil.which()` 內部用 `os.environ["PATHEXT"]`（Windows）逐一嘗試
    副檔名，回傳真正可執行的完整路徑，兩平台行為一致，不需要引入
    `shell=True`（避免字串注入風險）。找不到就在這裡直接拋出明確錯誤，
    不要留給 `subprocess.run` 丟一個「檔案或路徑無效」這種難以第一眼
    看懂根因的原生例外。
    """
    newman_path = shutil.which("newman")
    if newman_path is None:
        raise RuntimeError(
            "找不到 newman 執行檔（PATH 上沒有 newman／newman.cmd）："
            "請先 `npm install -g newman`，見 00 五章「環境建立」"
        )

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_path = tmp.name

    cmd = [
        newman_path, "run", collection_path,
        # collection 裡實際的變數名稱是 "baseUrl"（駝峰式，[B] Collection
        # Agent 產生 collection 時內建的變數，見 postman/collection_*.json
        # 的頂層 "variable" 陣列），不是 "base_url"（底線）。舊寫法傳錯
        # 變數名稱，newman 永遠不會覆寫，一律 fallback 回 collection 內建
        # 的預設值——已用真實 Java／Python 服務核對過：這個預設值恰好等於
        # Java 的網址，導致這個 bug 長期被掩蓋（錄製對 Java 「碰巧」正確，
        # 驗證對 Python 則從未真正命中過 Python 服務，永遠連到 Java／或
        # 連線被拒），見 09b_implement_agent_code.md 十章「已知限制」。
        "--env-var", f"baseUrl={base_url}",
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


def extract_response_body(response: dict) -> str | None:
    """
    從 newman 單一 execution 的 `response` 物件取出原始 body 文字。

    真實 newman（6.2.2）的 JSON reporter **沒有** `response["body"]` 這個
    欄位——已用真實 Java 服務重現過：`response.keys()` 只有
    `['id', 'status', 'code', 'header', 'stream', 'cookie', 'responseTime',
    'responseSize']`，不含 `body`。實際內容序列化在 `response["stream"]`，
    是 Node.js Buffer 的 JSON 表示（`{"type": "Buffer", "data": [位元組
    陣列]}`），要自己組回位元組再解碼。

    舊寫法 `response.get("body")` 永遠回傳 `None`（鍵不存在，`.get()`
    無預設值），即使實際回應內容非空——這代表這個 bug 修好之前，錄製端
    （`golden_writer.py`）錄到的每一筆 golden output body 都是空的，
    驗證端（`comparator.py`／`mutation_verifier.py`）比對到的 actual
    body 也一樣永遠是空的，body diff 從未真正比對過任何內容，只有
    status code 比對還有意義（見 09b_bug_trace.md）。

    回傳 `None` 代表這個 response 真的沒有 body（如 204 No Content，
    `stream` 缺席或 `data` 是空陣列）；`stream` 存在但形狀不符預期
    （不是 `{"type": "Buffer", "data": [...]}`）視為程式碼對 newman
    輸出格式的假設有誤，讓例外往外拋，不吞——這跟後續 JSON parse 失敗
    走 `response_not_json` 分類是不同層級的錯誤，不應該混在一起被
    這裡的容錯吞掉。
    """
    stream = response.get("stream")
    if stream is None:
        return None
    data = stream["data"]
    if not data:
        return None
    return bytes(data).decode("utf-8")


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
    module 詞彙表的 fallback 推斷：取第一個非版本前綴的路徑段。
    例：["api", "v1", "orders", "123"] → "orders"

    ⚠️ 唯一權威來源是 config/harness.yaml 的 route_to_module_mapping
    （見 02a 十三章、十一章、05a 八章），由 RouteMapper.resolve_module()
    優先查詢；這個函式只在查無對應時當 fallback，不應被其他模組直接呼叫
    （golden_writer.py／comparator.py／mutation_verifier.py 一律經由
    self.route_mapper.resolve_module(method, url_parts)）。
    """
    return next(
        (p for p in url_parts if p not in ("api", "v1", "v2")),
        "misc"
    )
