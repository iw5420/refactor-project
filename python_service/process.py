"""管理容器化 Python 服務（`uvicorn _reload_probe_wrapper:wrapper_app
--reload`，跑在 Docker 容器內）的啟動、就緒判定、關閉。

**為什麼跑在 Docker 容器內，不直接在 Orchestrator 所在機器上跑**：
實測（見 `tests/python_service/test_reload_probe_integration.py`、
`09b_implement_agent_code.md` 十章「已知限制」的完整根因記錄）發現
`uvicorn --reload` 在 Windows 上經常無法真正完成重啟——uvicorn 在
Windows 走的重啟路徑是 `os.kill(pid, signal.CTRL_C_EVENT)`
（`uvicorn/supervisors/basereload.py`），這個機制要求目標行程與發送端
共享同一個 console process group，Orchestrator 自己 spawn 的子行程
經常不滿足這個條件，導致 `restart()` 卡在 `self.process.join()`
永遠等不到，`_wait_for_service_reload()`（見 09a 三章）因此逾時失敗，
不是我們自己的同步邏輯有問題，是 reload 這件事在這個環境下常常根本
沒有發生。容器內是真正的 Linux 環境，uvicorn 走的是穩定的 POSIX
`self.process.terminate()`（SIGTERM）路徑，不受這個限制影響，已用
真實 Docker 容器＋bind mount 驗證過完整的重啟週期（含修改
`python_project_path` 底下的檔案後容器內確實重新載入新程式碼）。

比照 `spec_collection_agent/java_service.py` 的 `JavaServiceProcess`
介面慣例（見 03a 二章）：`start()`／`stop()`／`diagnostics`。
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

DEFAULT_STARTUP_TIMEOUT_SECONDS = 90.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.5
DEFAULT_SHUTDOWN_GRACE_SECONDS = 15.0

RELOAD_PROBE_PATH = "/__reload_probe__"

# 目標 Python 服務的技術棧已在 00 三章定案（FastAPI + SQLAlchemy，
# Python ≥ 3.10）；容器內只安裝這個已知的最小基線集合，供
# _reload_probe_wrapper.py／app/main.py 匯入時不至於直接 ImportError。
# **這是刻意簡化，不是完整的依賴管理方案**：若目標專案實際還需要更多
# 套件（如 alembic、目標專案自己的其他第三方依賴），這裡還沒有機制能
# 自動偵測、安裝——屬於尚未解決的缺口，見 09b 十章「已知限制」。
BASELINE_PACKAGES = ("fastapi", "uvicorn[standard]", "sqlalchemy", "psycopg2-binary")

DOCKER_IMAGE = "python:3.12-slim"


class PythonServiceStartupTimeout(RuntimeError):
    def __init__(self, message: str, *, diagnostics: str = ""):
        super().__init__(message)
        self.diagnostics = diagnostics


class PythonServiceContainer:
    """用 `docker run` 啟動一個容器，把 `python_project_path` bind mount
    進去（容器內路徑固定 `/srv`），容器內執行
    `uvicorn _reload_probe_wrapper:wrapper_app --reload`。

    用法：

        service = PythonServiceContainer(
            python_project_path=state["python_project_path"],
            base_url=state["python_base_url"],
            database_url=os.environ["DATABASE_URL"],
        )
        service.start()
        ...
        service.stop()

    與 `JavaServiceProcess` 的關鍵差異：`JavaServiceProcess` 直接管理
    一個本機 `subprocess.Popen`；這裡管理的是一個 Docker 容器（透過
    `docker` CLI 呼叫），啟動指令固定是
    `uvicorn _reload_probe_wrapper:wrapper_app --reload`（不是
    `uvicorn app.main:app --reload`，見 09a 三章「Python 服務只啟動
    一次」），不是 `python_project_path` 自己的啟動腳本。
    """

    def __init__(
        self,
        *,
        python_project_path: str,
        base_url: str,
        database_url: str,
        container_name: str = "refactor_python_service",
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self.python_project_path = str(Path(python_project_path).resolve())
        self.base_url = base_url.rstrip("/")
        self.database_url = database_url
        self.container_name = container_name
        self.startup_timeout = startup_timeout
        self.poll_interval = poll_interval
        self.shutdown_grace = shutdown_grace
        self._started = False

    def start(self) -> None:
        """同步、阻塞版本，比照 `JavaServiceProcess.start()` 的介面慣例。
        呼叫端需要用 `asyncio.to_thread()` 包一層。
        """
        # 啟動前先確保沒有同名的殘留容器（例如上一輪異常中斷留下的），
        # 否則 `docker run --name` 會直接失敗。忽略執行結果——容器本來
        # 就不存在時 `docker rm` 回非 0 是正常情況，不是這裡要處理的錯誤。
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True, text=True)

        pip_install = " ".join(BASELINE_PACKAGES)
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            # 容器內的 127.0.0.1／localhost 指向容器自己，不是跑這個
            # Orchestrator 的 Windows host——測試 DB 通常監聽在 host 上
            # （見 00 五章 TEST_DB_DSN／DATABASE_URL 慣例）。--add-host
            # 明確把 host.docker.internal 對應到 host-gateway，不依賴
            # Docker Desktop 版本是否預設就會自動提供這個 DNS 名稱
            # （已實測：部分版本組合下不加這個旗標會直接解析失敗）。
            "--add-host=host.docker.internal:host-gateway",
            "-v", f"{self.python_project_path}:/srv",
            "-w", "/srv",
            "-p", f"{self._port()}:8000",
            "-e", f"DATABASE_URL={self._container_database_url()}",
            DOCKER_IMAGE,
            "bash", "-c",
            f"pip install --quiet {pip_install} && "
            "uvicorn _reload_probe_wrapper:wrapper_app --reload --host 0.0.0.0 --port 8000",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise PythonServiceStartupTimeout(f"docker run 啟動失敗：{result.stderr.strip()}")

        self._started = True
        if not self._poll_until_ready():
            diagnostics = self.diagnostics
            self.stop()
            raise PythonServiceStartupTimeout(
                f"Python 服務容器在 {self.startup_timeout} 秒內未能就緒"
                f"（{self.base_url}{RELOAD_PROBE_PATH}）",
                diagnostics=diagnostics,
            )

    def _port(self) -> str:
        parsed = urlparse(self.base_url)
        return str(parsed.port or 8000)

    def _container_database_url(self) -> str:
        """把 `database_url` 裡指向 host 自己的位址（`127.0.0.1`／
        `localhost`）換成 `host.docker.internal`——這個字串是從 host
        角度寫的（`.env` 的 `DATABASE_URL`，Orchestrator／`psql` 都從
        host 直接連），容器內必須用不同的位址才能連到同一顆 DB。只替換
        host 部分，不動 port／帳密／DB 名稱。
        """
        for host in ("127.0.0.1", "localhost"):
            if f"@{host}:" in self.database_url or f"@{host}/" in self.database_url:
                return self.database_url.replace(f"@{host}", "@host.docker.internal")
        return self.database_url

    def _poll_until_ready(self) -> bool:
        deadline = time.monotonic() + self.startup_timeout
        url = f"{self.base_url}{RELOAD_PROBE_PATH}"
        while time.monotonic() < deadline:
            try:
                resp = requests.get(url, timeout=self.poll_interval)
                if resp.status_code == 200:
                    return True
            except requests.exceptions.RequestException:
                pass
            time.sleep(self.poll_interval)
        return False

    def stop(self) -> None:
        if not self._started:
            return
        subprocess.run(
            ["docker", "stop", "-t", str(int(self.shutdown_grace)), self.container_name],
            capture_output=True, text=True,
        )
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True, text=True)
        self._started = False

    @property
    def diagnostics(self) -> str:
        """對外提供容器最後 200 行輸出，供啟動失敗時定位原因（比照
        `JavaServiceProcess.diagnostics`）。
        """
        result = subprocess.run(
            ["docker", "logs", "--tail", "200", self.container_name],
            capture_output=True, text=True,
        )
        return result.stdout + result.stderr
