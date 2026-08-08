# [A] Spec Agent 程式碼實作

> 03a 是設計面文件，本文件是實作面文件，一一對應、不重複設計理由——每節開頭註明對應 03a 章節，這裡只講怎麼落地成程式碼。對應 `00_refactor_architecture.md` 十一章文件索引的 `03b_spec_agent_code.md`。[B] Collection Agent 的程式碼在 `03c_collection_agent_code.md`；兩者共用的基礎設施（套件總覽、`types.py`、`exceptions.py`）放在本文件二章，03c 直接參照、不重複貼。

---

## 一、模組總覽

沿用 03a 四章的套件配置，[A]/[B] 的邏輯規劃為獨立套件 `spec_collection_agent/`：

| 檔案 | 對應 03a 章節 | 備註 |
|---|---|---|
| `types.py` | 三章兩張輸入輸出表格 | 共用型別，避免重複定義 |
| `exceptions.py` | 六章 retry_count 邊界 | 例外階層即邊界的程式碼體現 |
| `postman_tree.py` | 三章多節共用的 item tree 走訪 | [B] 專用，見 03c 一、1.1 |
| `llm.py` | 三章「少量 LLM」共用呼叫方式 | [B] 專屬的模型選擇；實際呼叫邏輯在 `common/llm_client.py`（跨 Agent 共用，見 00 六章），見 03c 一、1.2 |
| `prompts.py` | 三章各節的 Claude system prompt | [B] 專用，見 03c 一、1.3 |
| `java_service.py` | 二章 | [A]，見本文件三章 |
| `collection_converter.py` | 三章轉換流程／分類規則 | [B]，見 03c 二、2.1 |
| `folder_grouper.py` | 三章 Mutation 頂層 folder 分組 | [B]，見 03c 二、2.2 |
| `value_filler.py` | 三章 LLM 填值邏輯 | [B]，見 03c 二、2.3 |
| `chain_dependency_detect.py` | 三章鏈式依賴偵測（map/reduce） | [B]，見 03c 二、2.4 |
| `chain_dependency_inject.py` | 三章鏈式依賴注入機制 | [B]，見 03c 二、2.5 |
| `manual_fill.py` | 03a 未涵蓋，01 五章新增「人工補值關卡」 | [B]，見 03c 二、2.6 |
| `__init__.py` | 四章對外唯一入口 | [A]/[B] 共用單一檔案，`run_spec_agent()` 見本文件四章、`run_collection_agent()` 見 03c 三章 |
| `graph/nodes/spec_node.py` | 五章 LangGraph 整合 | [A]，見本文件五章；沿用專案既有 `graph/nodes/` 目錄，非新建頂層 `nodes/` |
| `graph/nodes/collection_node.py` | 五章 LangGraph 整合 | [B]，見 03c 四章 |

目錄結構：

```
refactor-project/
├── spec_collection_agent/
│   ├── __init__.py
│   ├── types.py
│   ├── exceptions.py
│   ├── postman_tree.py
│   ├── llm.py
│   ├── prompts.py
│   ├── java_service.py
│   ├── collection_converter.py
│   ├── folder_grouper.py
│   ├── value_filler.py
│   ├── chain_dependency_detect.py
│   ├── chain_dependency_inject.py
│   └── manual_fill.py           # [B]，見 03c 二、2.6「人工補值關卡」
├── graph/
│   └── nodes/
│       ├── spec_node.py             # [A]，本文件五章；沿用專案既有 graph/nodes/ 目錄
│       ├── collection_node.py       # [B]，見 03c 四章
│       └── await_manual_fill_node.py # 見 01 五章「人工補值關卡」
├── specs/
│   └── openapi.json
└── postman/
    ├── collection_readonly.json
    ├── collection_mutation.json
    ├── unfilled_endpoints.json
    └── manual_fill/              # 見 03c 二、2.6，每個待補值 endpoint 一份模板
```

### 相依套件

| 套件 | 用途 |
|---|---|
| `requests` | `java_service.py` 輪詢 `/v3/api-docs` |
| `anthropic` | `common/llm_client.py` 呼叫 Claude API（讀 `ANTHROPIC_API_KEY`，見 00 五章）；跨 Agent 共用，[B] 端設定見 03c 六章 |

不引入 `openapi-to-postmanv2`／`newman` 的 Python binding，一律經 `npx` 呼叫（見 03a 三章）。

---
## 二、共用元件（[A]/[B] 共用）

### 2.1 `types.py`——共用型別定義

對應 03a 三章兩張表格；`ChainDependency` 欄位照抄、不增減。**`FillResult` 已移除**——原本用來表達單一 endpoint 的 LLM 填值結果，人工填值機制上線後不再有 LLM 填值這個步驟，`value_filler.py`／`test_value_filler.py` 也已同步不再引用（見 03a 三章「刪除的部分」）。

```python
# spec_collection_agent/types.py
"""[A]/[B] 共用型別定義，對應 03a 三章「鏈式依賴偵測」表格與「人工填值機制」。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# openapi.json／Postman Collection 都直接用 dict 表示，不特別包裝成物件，
# 因為兩者都是「讀進來、轉一手、寫出去」的過境資料，包裝成 dataclass
# 反而要多寫一層 to_dict/from_dict。
OpenAPISpec = dict[str, Any]
PostmanCollection = dict[str, Any]
PostmanItem = dict[str, Any]

HttpMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]

# 對應 03a 三章「Readonly / Mutation 分類規則」
READONLY_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})
MUTATION_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class ChainDependency:
    """鏈式依賴偵測結果單筆紀錄，欄位對應 03a 三章表格。"""

    producer_endpoint: str  # 如 "POST /api/v1/users"
    producer_field: str  # response 欄位路徑，點號分隔，如 "data.id"
    consumer_endpoint: str  # 如 "GET /api/v1/users/{id}"
    consumer_param: str  # 對應的參數名稱（path/query/body 皆可）
    env_var_name: str  # 如 "created_user_id"

    @property
    def producer_method(self) -> str:
        return self.producer_endpoint.split(" ", 1)[0].upper()

    @property
    def producer_path(self) -> str:
        return self.producer_endpoint.split(" ", 1)[1]

    @property
    def consumer_method(self) -> str:
        return self.consumer_endpoint.split(" ", 1)[0].upper()

    @property
    def consumer_path(self) -> str:
        return self.consumer_endpoint.split(" ", 1)[1]


@dataclass
class CollectionAgentResult:
    """run_collection_agent() 的回傳值，對應 03a 三章「產出與交接」。"""

    collection_readonly_path: str
    collection_mutation_path: str
    unfilled_endpoints_path: str
    # 尚未被人工解決（既沒填值、也沒標記 skip）的 endpoint 清單，供
    # graph 層的 conditional edge 判斷要不要暫停等人工處理（見 01 五
    # 「人工補值關卡」、03a 三章「人工填值機制」）。空清單＝全部解決，
    # 不阻擋流程；已標記 skip 的不算在這裡面。
    manual_fill_pending: list[str]

```

### 2.2 `exceptions.py`——例外階層

對應 03a 六章 `retry_count` 邊界：這裡的例外一律代表硬性失敗，直接往上拋。唯一例外是人工標記 `Decision.SKIP` 或尚未填值的 endpoint，這不算例外，是排除、記錄進 `unfilled_endpoints.json`（見 `manual_fill.py`、03a 三章「人工填值機制」）。

```python
# spec_collection_agent/exceptions.py
"""[A]/[B] 例外階層。對應 03a 六章 `retry_count` 邊界：這裡的例外一律視為
硬性失敗，直接往上拋、中止整條 pipeline。唯一例外是人工標記
`Decision.SKIP` 或尚未填值的 endpoint，這不算例外，是排除、記錄進
`unfilled_endpoints.json`（見 manual_fill.py、03a 三章「人工填值機制」）。
"""
from __future__ import annotations


class JavaServiceError(Exception):
    """[A] Spec Agent 的基底例外。"""


class DbUnreachableError(JavaServiceError):
    """啟動前的 DB TCP 連線快篩失敗（03a 二章「服務生命週期管理」步驟 0）。"""


class JavaServiceStartupTimeout(JavaServiceError):
    """逾時預算內沒有拿到合法的 `/v3/api-docs` 回應（03a 二章「就緒判定與擷取合一」）。"""

    def __init__(self, message: str, last_error: str | None, diagnostics: str) -> None:
        super().__init__(message)
        self.last_error = last_error
        self.diagnostics = diagnostics

    def __str__(self) -> str:  # pragma: no cover - 純格式化
        base = super().__str__()
        return f"{base}\n最後一次候選錯誤: {self.last_error}\n---- 進程輸出（最後數行）----\n{self.diagnostics}"


class CollectionConversionError(Exception):
    """`openapi-to-postmanv2` 轉換失敗（非 0 結束碼），見 03a 三章「轉換流程」。"""

    def __init__(self, message: str, stderr: str) -> None:
        super().__init__(message)
        self.stderr = stderr


class ChainDependencyDetectionError(Exception):
    """鏈式依賴偵測（map 或 reduce 任一階段）的 LLM 呼叫失敗或回傳格式
    不合法；沒有局部排除的粒度，任一階段失敗視為整個偵測失敗（見 03a
    六章）。map 階段單筆候選項格式不合法屬例外——那是丟棄該筆、記
    warning，不觸發這個例外（候選清單是 best-effort 中間產物，見四、
    4.4）。
    """

```

---

## 三、[A] Spec Agent 實作：`java_service.py`

對應 03a 二章全部內容，核心是 `JavaServiceProcess` 類別。介面契約對照 03a 二章「服務生命週期管理」表格：

| 03a 表格能力 | 對應程式碼 |
|---|---|
| 啟動 | `start()` → `_launch_process()` |
| 背景抽乾輸出 | `_drain_output()`（背景執行緒，`deque(maxlen=200)`） |
| 關閉 | `stop()`（`terminate()`，逾時再 `kill()`） |
| 診斷輸出 | `diagnostics` property |
| 生命週期綁定 | `__enter__`／`__exit__` |

幾個對應點：`resolve_jar_path()` 以 `PROJECT_ROOT = Path(__file__).resolve().parent.parent` 為錨點解析相對路徑，不依賴 cwd（見 03a「路徑格式」）；`check_db_reachable()` 只做 TCP 連線、不驗證帳密（見 03a 步驟 0）；`_poll_until_ready()` 單一迴圈同時判斷就緒與取得結果，進程若提前退出（如 port 衝突）立即結束輪詢，不空等 90 秒逾時。**沒有**額外的 port 佔用偵測與清理邏輯，對應 03a 排除的誤殺風險。

Agent ②（Harness 錄製端）直接 import 這個類別重用（見 03a「與 Agent ② 共用的邊界」）。

```python
# spec_collection_agent/java_service.py
"""[A] Spec Agent 核心：`JavaServiceProcess`。

不依賴 State、不依賴本套件其他模組——03a 二章要求 Agent ②（Harness 錄製
端）能重用同一套「啟動、就緒判定、關閉」邏輯，`02b` 會直接
`from spec_collection_agent.java_service import JavaServiceProcess` 匯入。
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import requests

from spec_collection_agent.exceptions import (
    DbUnreachableError,
    JavaServiceStartupTimeout,
)
from spec_collection_agent.types import OpenAPISpec

logger = logging.getLogger(__name__)

# 時間預算與間隔為預設值（見 03a 二章「就緒判定與擷取合一」），實作可調，
# 因此都做成建構子參數、不寫死成模組常數直接用。
DEFAULT_STARTUP_TIMEOUT_SECONDS = 90.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.5
DEFAULT_DB_PRECHECK_TIMEOUT_SECONDS = 3.0
DEFAULT_SHUTDOWN_GRACE_SECONDS = 10.0
OUTPUT_TAIL_LINES = 200

API_DOCS_PATH = "/v3/api-docs"

# spec_collection_agent/ 相對於 orchestrator 專案根目錄的深度是 1 層，
# 用來把 JAVA_JAR_PATH 這類相對路徑解析成不依賴 cwd 的絕對路徑
# （見 03a 二章「路徑格式」說明：真正要避免的是「解析依賴 cwd」）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_jar_path(java_jar_path: str) -> Path:
    """把 `JAVA_JAR_PATH`（相對於專案根目錄）解析成絕對路徑，不依賴 cwd
    （見 03a 二章「路徑格式」）。
    """
    p = Path(java_jar_path)
    if p.is_absolute():
        # 00 五已明確不建議寫死絕對路徑，但不強制禁止——換機器會失效的
        # 風險留給使用者自己承擔，這裡不擋。
        return p
    return (PROJECT_ROOT / p).resolve()


_JDBC_POSTGRESQL_SCHEME = "postgresql"
_JDBC_POSTGRESQL_DEFAULT_PORT = 5432


def _parse_jdbc_host_port(jdbc_url: str) -> tuple[str, int]:
    """從 `jdbc:postgresql://host:port/db` 抓出 host/port，只給 TCP 快篩
    用（03a 二章步驟 0），不需要解析成完整 DSN。

    目前測試 DB 固定是 PostgreSQL（見 00 五章），這裡只支援
    `jdbc:postgresql://host[:port]/db` 這一種語法。未顯式指定 port 時
    （例如 `jdbc:postgresql://host/db`，依賴 PostgreSQL 預設的 5432），
    `urllib.parse` 會回傳 `parsed.port is None`；這種寫法本身合法，補上
    預設 port 再回傳，不當成解析失敗。

    **刻意不在這裡支援其他資料庫 scheme**：若日後測試 DB 換成別的
    資料庫，不要直接在這個函式裡加 if/else 分支——不同資料庫的 JDBC
    連線字串語法差異可能很大（例如 SQL Server 用 `;key=value` 接參數，
    不是 URL 風格），混在同一個函式裡容易顧此失彼，屆時再視實際需求另外
    設計，不預先假設。
    """
    # urllib.parse 不認得 "jdbc:" 這個 scheme 前綴，先剝掉再交給它處理。
    without_jdbc_prefix = jdbc_url.removeprefix("jdbc:")
    parsed = urllib.parse.urlparse(without_jdbc_prefix)

    if parsed.scheme != _JDBC_POSTGRESQL_SCHEME:
        raise ValueError(
            f"_parse_jdbc_host_port() 目前只支援 PostgreSQL "
            f"（scheme={_JDBC_POSTGRESQL_SCHEME!r}），收到 "
            f"scheme={parsed.scheme!r}: {jdbc_url!r}"
        )

    if not parsed.hostname:
        raise ValueError(f"無法從 SPRING_DATASOURCE_URL 解析出 host: {jdbc_url!r}")

    try:
        port = parsed.port or _JDBC_POSTGRESQL_DEFAULT_PORT
    except ValueError as exc:
        raise ValueError(
            f"無法從 SPRING_DATASOURCE_URL 解析出合法的 port: {jdbc_url!r}"
        ) from exc

    return parsed.hostname, port


def check_db_reachable(
    jdbc_url: str, *, timeout: float = DEFAULT_DB_PRECHECK_TIMEOUT_SECONDS
) -> bool:
    """輕量 TCP 連線測試，只確認能連上 port、不驗證帳密，用來在幾秒內
    排除「DB 整個連不上」這種最常見的啟動失敗原因（03a 二章步驟 0）。
    """
    host, port = _parse_jdbc_host_port(jdbc_url)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        logger.warning("DB TCP 快篩失敗 host=%s port=%s: %s", host, port, exc)
        return False


@dataclass
class _StartupOutcome:
    openapi_spec: OpenAPISpec | None = None
    last_error: str | None = None


class JavaServiceProcess:
    """管理單一 `java -jar` 進程的啟動、就緒判定、關閉，介面契約見 03a
    二章「服務生命週期管理」表格。

    用法（[A] Spec Agent）：

        with JavaServiceProcess(
            jar_path=resolve_jar_path(os.environ["JAVA_JAR_PATH"]),
            base_url=os.environ["JAVA_BASE_URL"],
            java_executable=os.environ.get("JAVA_EXECUTABLE_PATH", "java"),
            env_overrides={
                "SPRING_DATASOURCE_URL": os.environ["SPRING_DATASOURCE_URL"],
                "SPRING_DATASOURCE_USERNAME": os.environ["SPRING_DATASOURCE_USERNAME"],
                "SPRING_DATASOURCE_PASSWORD": os.environ["SPRING_DATASOURCE_PASSWORD"],
            },
        ) as svc:
            openapi_spec = svc.openapi_spec

    Agent ② 用同一個類別、換一組 env_overrides 啟動自己的實例（見 03a
    二章「與 Agent ② 共用的邊界」）。
    """

    def __init__(
        self,
        *,
        jar_path: Path,
        base_url: str,
        env_overrides: dict[str, str],
        java_executable: str = "java",
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
        output_tail_lines: int = OUTPUT_TAIL_LINES,
    ) -> None:
        self.jar_path = jar_path
        self.base_url = base_url.rstrip("/")
        self.env_overrides = env_overrides
        # 預設交給系統 PATH 解析；PATH 上有多個 JDK 版本時，PATH 解析到的
        # 版本不保證與 Java 專案要求的版本相符，改由 JAVA_EXECUTABLE_PATH
        # 明確指定要用哪個 java 執行檔，避免 UnsupportedClassVersionError
        # （見 03a 二章「Java 執行檔路徑」）。
        self.java_executable = java_executable
        self.startup_timeout = startup_timeout
        self.poll_interval = poll_interval
        self.shutdown_grace = shutdown_grace

        self._output_tail: deque[str] = deque(maxlen=output_tail_lines)
        self._process: subprocess.Popen[str] | None = None
        self._drain_thread: threading.Thread | None = None
        self.openapi_spec: OpenAPISpec | None = None

    # ---- 生命週期綁定（context manager） ----

    def __enter__(self) -> "JavaServiceProcess":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # 不論 start() 之後是否拋例外、或 with 區塊內部拋例外，都要保證
        # 關閉，避免遺留孤兒進程（見 03a 二章表格「生命週期綁定」）。
        self.stop()

    # ---- 啟動 ----

    def start(self) -> OpenAPISpec:
        if not self.jar_path.exists():
            raise FileNotFoundError(
                f"JAVA_JAR_PATH 指向的檔案不存在: {self.jar_path}"
            )

        spring_datasource_url = self.env_overrides.get("SPRING_DATASOURCE_URL")
        if spring_datasource_url and not check_db_reachable(spring_datasource_url):
            raise DbUnreachableError(
                f"無法連線到測試 DB（TCP 快篩失敗）: {spring_datasource_url}；"
                "請檢查 00 五章的測試 DB 是否已建立、連線資訊是否正確。"
            )

        self._launch_process()
        outcome = self._poll_until_ready()

        if outcome.openapi_spec is None:
            self.stop()
            raise JavaServiceStartupTimeout(
                f"Java 服務在 {self.startup_timeout} 秒內未能就緒",
                last_error=outcome.last_error,
                diagnostics=self.diagnostics,
            )

        self.openapi_spec = outcome.openapi_spec
        return self.openapi_spec

    def _launch_process(self) -> None:
        env = {**os.environ, **self.env_overrides}
        self._process = subprocess.Popen(
            [self.java_executable, "-jar", str(self.jar_path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,  # 不需要跟這個進程互動，明確阻斷
            # 而非繼承父進程的 stdin，避免特定終端機環境下意外掛起。
            text=True,
            encoding="utf-8",  # 不依賴作業系統預設編碼——Windows 常見
            # locale（如 cp950）會讓 Spring Boot 開機日誌裡的中文字元
            # 觸發 UnicodeDecodeError，讓 `_drain_output` 背景執行緒崩潰。
            errors="replace",  # 診斷輸出是 best-effort：真的遇到不是
            # UTF-8 的位元組（例如 JVM 在某些環境仍用系統編碼輸出）時，
            # 寧可顯示替代字元，也不要讓抽乾執行緒直接掛掉、吃掉後續
            # 所有診斷輸出。
            bufsize=1,
        )
        self._drain_thread = threading.Thread(
            target=self._drain_output, name="java-service-output-drain", daemon=True
        )
        self._drain_thread.start()

    def _drain_output(self) -> None:
        """背景持續讀取 stdout/stderr、只保留最後 N 行，避免開機日誌塞滿
        pipe buffer 卡死進程（03a 二章「背景抽乾輸出」）；不解析內容。
        """
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            self._output_tail.append(line.rstrip("\n"))

    def _poll_until_ready(self) -> _StartupOutcome:
        outcome = _StartupOutcome()
        deadline = time.monotonic() + self.startup_timeout
        api_docs_url = f"{self.base_url}{API_DOCS_PATH}"

        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                # 進程已經自己退出（如 port 衝突），不需要繼續輪詢等到逾時，
                # 診斷輸出裡通常已經有明確的錯誤行（見 03a 二章錯誤情境表）。
                outcome.last_error = (
                    f"Java 進程已退出，exit code={self._process.returncode}"
                )
                break

            try:
                resp = requests.get(api_docs_url, timeout=self.poll_interval)
            except requests.exceptions.RequestException as exc:
                # 連線被拒或逾時：服務尚未起來，屬正常現象，繼續重試。
                outcome.last_error = f"連線失敗（服務可能尚未就緒）: {exc}"
                time.sleep(self.poll_interval)
                continue

            if resp.status_code == 200:
                try:
                    outcome.openapi_spec = resp.json()
                    return outcome
                except ValueError as exc:
                    outcome.last_error = f"/v3/api-docs 回 200 但不是合法 JSON: {exc}"
                    time.sleep(self.poll_interval)
                    continue

            # 非 200 但也非連線層級錯誤（例如 404）：記錄為候選錯誤原因，
            # 繼續重試直到逾時預算耗盡（見 03a 二章步驟 5）。
            outcome.last_error = (
                f"/v3/api-docs 回應非 200: status={resp.status_code} "
                f"body={resp.text[:500]!r}"
            )
            time.sleep(self.poll_interval)

        return outcome

    # ---- 關閉 ----

    def stop(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=self.shutdown_grace)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Java 進程逾 %s 秒未回應終止信號，強制關閉", self.shutdown_grace
                )
                self._process.kill()
                self._process.wait()
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=self.shutdown_grace)

    # ---- 診斷輸出 ----

    @property
    def diagnostics(self) -> str:
        """對外提供最後 N 行進程輸出，供啟動失敗時定位原因。"""
        return "\n".join(self._output_tail)
```

---

## 四、對外唯一入口：`__init__.py`（`run_spec_agent()` 部分）

`spec_collection_agent/__init__.py` 是 [A]/[B] 共用的單一檔案，本節只先展示 [A] 用到的 `run_spec_agent()`，方便跟 03a 二章的介面對照——完整檔案（含 [B] 的 `run_collection_agent()`）以 03c 三章為準，本節片段不要單獨複製使用（缺 [B] 那邊的 import）。

```python
# spec_collection_agent/__init__.py（節錄：run_spec_agent 部分，完整檔案見 03c 三章）
from __future__ import annotations

from spec_collection_agent.java_service import JavaServiceProcess, resolve_jar_path
from spec_collection_agent.types import OpenAPISpec


def run_spec_agent(
    *,
    java_jar_path: str,
    java_base_url: str,
    java_executable_path: str,
    spring_datasource_url: str,
    spring_datasource_username: str,
    spring_datasource_password: str,
) -> OpenAPISpec:
    """[A] Spec Agent：啟動 Java 服務、擷取 `/v3/api-docs`、關閉服務（對應
    03a 二章）。`spring_datasource_*`／`java_executable_path` 由呼叫端從
    環境變數讀出傳入，本函式不直接讀 `os.environ`（見 03a 五章備註）。
    """
    with JavaServiceProcess(
        jar_path=resolve_jar_path(java_jar_path),
        base_url=java_base_url,
        java_executable=java_executable_path,
        env_overrides={
            "SPRING_DATASOURCE_URL": spring_datasource_url,
            "SPRING_DATASOURCE_USERNAME": spring_datasource_username,
            "SPRING_DATASOURCE_PASSWORD": spring_datasource_password,
        },
    ) as svc:
        return svc.openapi_spec  # start() 已在 __enter__ 內呼叫，此時必不為 None
```

---

## 五、LangGraph node 封裝

對應 03a 五章。線性 node，比照 01 七 stub 慣例整包展開 state。`JAVA_JAR_PATH`／`JAVA_EXECUTABLE_PATH`／`SPRING_DATASOURCE_*` 只在這裡讀，不放進 `RefactorState`（理由見 03a 五章備註）。沿用專案既有的 `graph/nodes/` 目錄，取代該路徑下原本的 stub。

**函式命名與 async 包裝**：專案內所有 node 檔案一律匯出 `run(state)`（見 `parse_node.py`／`collection_node.py` 既有慣例），graph node id（如 `extract_spec`）只在 `graph/builder.py` 用 `builder.add_node("extract_spec", spec_node.run)` 對應，不是函式名稱本身。`main.py` 用 `graph.ainvoke()` 驅動整張圖，所有 node 都須是 `async def`；`run_spec_agent()` 內部是同步阻塞呼叫（輪詢 Java 進程，最長 90 秒），直接同步呼叫會卡住事件迴圈，因此用 `asyncio.to_thread()` 丟到執行緒跑。

### `graph/nodes/spec_node.py`

```python
# graph/nodes/spec_node.py
"""
[A] Spec Agent（程式邏輯）
啟動 Java 服務，取得 OpenAPI 3.0 JSON
見 03a_spec_collection_agent_architecture.md 二、03b_spec_agent_code.md

`JAVA_JAR_PATH`／`JAVA_EXECUTABLE_PATH`／`SPRING_DATASOURCE_*` 不放進
`RefactorState`，直接讀 `os.environ`（理由見 03a 五章備註）。
"""
import asyncio
import os

from graph.state import RefactorState
from spec_collection_agent import run_spec_agent


async def run(state: RefactorState) -> RefactorState:
    # run_spec_agent() 內部是同步阻塞呼叫（輪詢 Java 進程，最長可達 90 秒
    # 啟動逾時預算），丟到執行緒跑，避免卡住事件迴圈。
    openapi_spec = await asyncio.to_thread(
        run_spec_agent,
        java_jar_path=os.environ["JAVA_JAR_PATH"],
        java_base_url=os.environ["JAVA_BASE_URL"],
        java_executable_path=os.environ.get("JAVA_EXECUTABLE_PATH", "java"),
        spring_datasource_url=os.environ["SPRING_DATASOURCE_URL"],
        spring_datasource_username=os.environ["SPRING_DATASOURCE_USERNAME"],
        spring_datasource_password=os.environ["SPRING_DATASOURCE_PASSWORD"],
    )

    return {
        **state,
        "openapi_spec": openapi_spec,
    }
```

---

## 六、驗證方式與測試建議（[A] 部分）

呼應 00 一章「不依賴外部服務的純邏輯模組可先用假資料單元測試」的精神。開發時已用假資料驗證以下行為，建議整理成 `tests/spec_collection_agent/` 底下的正式單元測試：

| 驗證項目 | 對應函式 | 驗證內容 |
|---|---|---|
| JDBC URL 解析 | `_parse_jdbc_host_port()` | 正確從 `jdbc:postgresql://host:port/db` 抓出 host/port；未顯式指定 port（依賴 PostgreSQL 預設 5432）時補上預設值而非拋例外；scheme 不是 `postgresql` 時明確拋出「不支援」的錯誤，不會誤解析出錯誤的 hostname |
| jar 路徑解析 | `resolve_jar_path()` | 相對路徑以 `spec_collection_agent/` 所在目錄為錨點解析，不受 cwd 影響 |

**沒有**驗證的部分（需要真實外部服務，留給 00 一章第 2 步接上 Harness 錄製端時整合驗證）：

- `JavaServiceProcess.start()` 對真實 `java -jar` 進程的啟動、輪詢、逾時、關閉行為

[B] 相關的測試項目見 03c 五章。

---

## 七、環境需求與待落實事項（[A] 部分）

### 7.1 安裝

在 00 五章既有的 Python 虛擬環境裡，額外安裝：

```bash
pip install requests
```

`requests` 供 `java_service.py` 輪詢 `/v3/api-docs` 使用。[B] 額外需要的 `anthropic`、`ANTHROPIC_API_KEY` 見 03c 六章。

### 7.2 實作時需要留意的點

- `_parse_jdbc_host_port()` 目前只支援 PostgreSQL——現況測試 DB（`MOC_MATSUEXAM_TEST`，見 00 五章）固定是 PostgreSQL，不需要在這個函式裡預先支援其他資料庫。若日後測試 DB 換成其他資料庫，不要直接在這個函式裡加 if/else 分支，屆時再視實際需求另外設計。

[B] 相關的待落實事項見 03c 六章。
