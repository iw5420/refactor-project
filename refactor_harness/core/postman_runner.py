import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile

logger = logging.getLogger(__name__)

# newman 打的目標服務可能處於「socket 還開著、但 app 沒真的載入」這種
# 半死不活狀態（例如容器內 uvicorn --reload 的 app import 階段拋
# SyntaxError，reload watcher 仍在監聽，連進去的請求會一直掛著不回應，
# 不是乾脆的 connection refused）——這種情況下 newman 本身也會卡住不
# 結束，若 `subprocess.run()` 沒設 timeout，Python 會無界等下去，見
# docs/09b_bug_trace.md #34（真實重跑卡了一個多小時，用 py-spy dump
# 活行程才抓到卡在這裡）。可用環境變數覆蓋，預設值遠大於一般 collection
# 的正常執行時間，只是拿來擋「目標服務死掉」這種異常情境。
NEWMAN_TIMEOUT_SECONDS = float(os.environ.get("NEWMAN_TIMEOUT_SECONDS", "180"))


def _spawn(cmd: list[str]) -> subprocess.Popen:
    """啟動子行程時讓它自成一個獨立的行程群組／session，見
    `_kill_process_tree()` docstring——逾時要砍的時候才砍得到整棵行程樹，
    不會漏殺孫行程。
    """
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs
    )


def _kill_process_tree(pid: int) -> None:
    """對應 docs/09b_bug_trace.md #36：Windows 上 `newman` 實際執行的是
    `newman.cmd`（npm batch wrapper），這個 `.cmd` 檔案內部會再啟動一個
    真正在做事的 `node.exe` 子行程。`Popen.kill()`／`subprocess.run(timeout=)`
    逾時時只會砍掉 Python 直接持有 handle 的那個行程（`cmd.exe`／
    `newman.cmd` wrapper 本身），**不會連帶砍掉這個 wrapper 底下的
    `node.exe` 子行程**——Windows 沒有 POSIX 那種預設的行程群組／session
    語意。結果是 wrapper 被砍了，但 `node.exe` 還活著、還占著
    `capture_output` 開的 stdout/stderr 管線沒放手，讓
    `communicate(timeout=...)` 逾時後續的收尾等待永遠等不到 EOF，即使
    設了 `timeout=` 整個呼叫實際上還是沒有真正的上限（真實重跑卡了
    24 分鐘以上才被 py-spy＋netstat 查出來）。

    改用 `taskkill /T /F` 對整棵行程樹下手（`/T` 是關鍵：連子行程、孫
    行程一起砍），是 Windows 上唯一可靠的做法；POSIX 對應 `_spawn()`
    用 `start_new_session=True` 建立的整個 session 用 `os.killpg()` 處理。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True,
        )
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # 行程樹已經自己結束了，不是錯誤


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

    process = _spawn(cmd)
    try:
        stdout, stderr = process.communicate(timeout=NEWMAN_TIMEOUT_SECONDS)
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        # 見 _kill_process_tree() docstring、docs/09b_bug_trace.md #36：
        # 光是 process.kill() 砍不乾淨 newman.cmd 底下真正在跑的
        # node.exe，會讓下面這次收尾用的 communicate() 也卡住——所以
        # 逾時後一定要先把整棵行程樹砍乾淨，才能安全地做收尾讀取。
        _kill_process_tree(process.pid)
        process.communicate()  # 收尾：確認管線真的關閉，避免留下殭屍行程
        raise RuntimeError(
            f"newman 執行逾時（timeout={NEWMAN_TIMEOUT_SECONDS}s）：目標服務可能處於"
            "「socket 還開著但沒有真的回應」的異常狀態（如容器內 app 載入時就掛掉，"
            "reload watcher 仍在監聽）\n"
            f"collection: {collection_path}\n"
            f"base_url: {base_url}"
        ) from None

    # newman 的 exit code 只反映「collection 裡的 test script 斷言是否全部
    # 通過」，不是「這次執行本身有沒有成功產生報表」——鏈式依賴注入的
    # capture script 斷言失敗時 exit code 也會是非 0，但 JSON reporter仍
    # 正常寫出完整報表（已用真實案例核對過：POST /api/candidate/search
    # 的斷言失敗、exit code=1，報表檔案仍含完整 executions，見
    # docs/09b_bug_trace.md #32）。呼叫端（GoldenVerifier／MutationVerifier／
    # GoldenRecorder）都是自己重新比對 response 內容，從不依賴 newman 自身
    # 的斷言結果，所以只要報表存在且是合法 JSON 就該當成執行成功回傳，讓
    # 斷言失敗與否交給呼叫端自己的邏輯判斷（如 record_mutation() 的
    # tainted-folder 機制）；報表根本沒產生（服務沒起來、collection 路徑
    # 錯誤等真正的執行失敗）才是這裡要擋下的硬性錯誤。
    if returncode != 0:
        logger.warning(
            "newman exit code=%s（collection 內部斷言失敗，非執行本身失敗），"
            "仍嘗試讀取報表: collection=%s base_url=%s",
            returncode, collection_path, base_url,
        )

    try:
        with open(output_path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        raise RuntimeError(
            f"newman 執行失敗（return code {returncode}），且未產生有效報表\n"
            f"collection: {collection_path}\n"
            f"base_url: {base_url}\n"
            f"stderr: {(stderr or '')[:500]}"
        )


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
