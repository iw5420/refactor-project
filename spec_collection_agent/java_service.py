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
