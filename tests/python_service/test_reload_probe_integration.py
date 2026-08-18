"""python_service 對 implement_node 的熱重載同步屏障（_get_reload_probe_id／
_wait_for_service_reload）的整合驗證：對一個真正在跑的容器化 Python 服務
送出真實 HTTP 請求，而不是 mock httpx——09a 三章「決定性的同步屏障」設計
核心正是「輪詢到的是不是新 worker」，這件事不能只靠靜態 assertion 驗證，
必須真的經歷一次 reload 週期才有意義。

**為什麼是容器化，不是直接在本機跑 uvicorn --reload**：實測發現
`uvicorn --reload` 在 Windows 上經常無法真正完成重啟（見
`python_service/process.py` docstring、`09b_implement_agent_code.md`
十章「已知限制」的完整根因記錄——Windows 的 `CTRL_C_EVENT` 送達機制不
可靠）。改用 `PythonServiceContainer` 在 Docker（Linux）容器內執行目標
服務，uvicorn 走的是穩定的 POSIX SIGTERM 重啟路徑。

跑這個測試需要：
- Docker（`docker info` 需成功，否則整個模組會被跳過）
- 網路可以下載 `python:3.12-slim` 映像檔（第一次跑會稍慢，之後有本地
  快取）

比單元測試慢很多（容器啟動＋容器內 `pip install`），單一模組耗時約
數十秒，屬預期範圍，不建議放進每次存檔就觸發的快速回饋迴圈。
"""
import asyncio
import os
import socket
import subprocess
import time

import httpx
import pytest
from dotenv import load_dotenv

import graph.nodes.implement_node as implement_node
from python_service.process import PythonServiceContainer
from python_service.reload_probe import ensure_reload_probe_infra

# 沒有 conftest.py 統一載入 .env（main.py 自己呼叫，測試套件其餘部分不
# 依賴真實 .env 值）——這裡的 test_container_can_reach_host_database_
# via_host_docker_internal 是唯一需要讀 TEST_DB_DSN 的測試，缺 .env 或
# 沒設定這個值時 _real_test_db_reachable() 回傳 None，測試會被跳過，
# 不影響套件其餘部分。
load_dotenv()


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="需要本機可用的 Docker daemon")


def _real_test_db_reachable() -> str | None:
    """回傳 `TEST_DB_DSN`（host 角度，供本機直接連線用），連不上時回傳
    `None`。獨立於 Docker 可用性判斷——`test_container_can_reach_host_
    database_via_host_docker_internal` 需要這兩個條件都滿足。
    """
    dsn = os.environ.get("TEST_DB_DSN")
    if not dsn:
        return None
    try:
        import psycopg2

        with psycopg2.connect(dsn, connect_timeout=3):
            return dsn
    except Exception:
        return None


def _init_repo(path):
    subprocess.run(["git", "init"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, capture_output=True, text=True, check=True)


_APP_MAIN_TEMPLATE = '''from fastapi import FastAPI

app = FastAPI()


@app.get("/ping")
def ping():
    return {{"marker": "{marker}"}}
'''


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_wrapper_service(tmp_path, monkeypatch):
    """啟動一個真正的 `PythonServiceContainer`，指向 `tmp_path` 底下最小
    的 FastAPI app，yield `(project_path, base_url)`，測試結束後負責
    終止並移除容器。
    """
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text(
        _APP_MAIN_TEMPLATE.format(marker="v1"), encoding="utf-8"
    )

    _init_repo(tmp_path)
    # app/main.py 也先 commit 一次，讓這個測試的 git 狀態合法——不影響
    # 09a 三章要驗證的核心行為（token round-trip），單純避免干擾
    # ensure_reload_probe_infra() 自己的 precondition 檢查。
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "test: minimal fastapi app"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )

    ensure_reload_probe_infra(str(tmp_path))

    base_url = f"http://127.0.0.1:{_free_port()}"
    service = PythonServiceContainer(
        python_project_path=str(tmp_path),
        base_url=base_url,
        database_url="postgresql://unused/unused",  # 這個最小 app 不連 DB
        container_name=f"reload_probe_test_{tmp_path.name}",
        startup_timeout=90.0,  # 容器內第一次 pip install 需要較長預算
    )
    service.start()

    # 09a 三章「決定性的同步屏障」的等待邏輯預設逾時預算是 120 秒，這裡
    # 調小成測試友善的值——容器內 reload 一次通常數秒內完成。
    monkeypatch.setattr(implement_node, "SERVICE_READY_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(implement_node, "SERVICE_READY_POLL_INTERVAL_SECONDS", 0.5)

    yield tmp_path, base_url

    service.stop()


def test_get_reload_probe_id_succeeds_against_real_service(running_wrapper_service):
    _project_path, base_url = running_wrapper_service
    asyncio.run(implement_node._get_reload_probe_id(base_url))  # 不應拋出


def test_wait_for_service_reload_round_trips_token(running_wrapper_service):
    """對應 09a 三章「決定性的同步屏障」步驟 1～3：寫入的 token 必須被
    這個真正在跑的 wrapper 原樣回應出來（沒有經過任何程式碼變更、也沒有
    觸發真正的 reload——純粹驗證 token 讀寫與探測端點本身接得起來）。
    """
    project_path, base_url = running_wrapper_service

    ok = asyncio.run(implement_node._wait_for_service_reload(str(project_path), base_url))

    assert ok is True
    token_content = (project_path / "_reload_token.py").read_text(encoding="utf-8")
    assert token_content.startswith('TOKEN = "')


def test_wait_for_service_reload_correctly_waits_for_new_worker_after_real_reload(running_wrapper_service):
    """09a 三章整個設計要解決的核心問題：修改 app/main.py 觸發一次
    「真正的」容器內 uvicorn --reload（不是模擬），再呼叫一次
    _wait_for_service_reload() 帶入新 token。若這個函式錯誤地把舊 worker
    仍在回應的舊 token 當成就緒，第二次呼叫理論上仍會 timeout 或讀到舊
    值——只有真的等到新 worker（讀到新 token 檔案內容之後才啟動的那個
    worker）才會回應相符的值，讓這裡的斷言通過。
    """
    project_path, base_url = running_wrapper_service

    first_ok = asyncio.run(implement_node._wait_for_service_reload(str(project_path), base_url))
    assert first_ok is True

    # 觸發一次真正的程式碼變更 + reload：修改 app/main.py 的回應內容，
    # 透過 bind mount 讓容器內的檔案系統也看到這次變更。
    (project_path / "app" / "main.py").write_text(
        _APP_MAIN_TEMPLATE.format(marker="v2"), encoding="utf-8"
    )
    time.sleep(0.5)  # 給 bind mount 的檔案系統事件傳遞留一點緩衝

    second_ok = asyncio.run(implement_node._wait_for_service_reload(str(project_path), base_url))
    assert second_ok is True

    # 佐證服務真的換了新程式碼，不是恰好連線成功但還是舊版本：/ping 現在
    # 回傳新版的 marker，證明剛才等到的確實是載入新程式碼後的 worker。
    resp = httpx.get(f"{base_url}/ping", timeout=5.0)
    assert resp.json() == {"marker": "v2"}


_DB_CHECK_APP_MAIN_TEMPLATE = """from fastapi import FastAPI
import os
import psycopg2

app = FastAPI()


@app.get("/db-check")
def db_check():
    with psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            return {"result": cur.fetchone()[0]}
"""


def test_container_can_reach_host_database_via_host_docker_internal(tmp_path, monkeypatch):
    """對應 python_service/process.py `_container_database_url()`／
    `--add-host=host.docker.internal:host-gateway`：先前只用手動一次性
    的 `docker run postgres:17-alpine psql ...` 驗證過這個機制可行（見
    09b_implement_agent_code.md 十章），沒有寫進自動化測試——
    test_process.py 的既有測試只驗證字串重寫邏輯本身、以及旗標有沒有
    出現在指令裡，都不是「容器裡的服務真的成功查詢到 host 上的資料」
    這件事本身。這裡用真實 TEST_DB_DSN（跟這個 repo 其餘 Harness 邏輯
    連的是同一顆測試 DB）補上這一段，跑一個會實際執行 SQL 查詢的
    容器化 app，而不是只測連線層。

    需要 `.env` 設定 `TEST_DB_DSN` 且該 DB 可連線，否則跳過（見
    `_real_test_db_reachable()`）——這是本模組唯一額外依賴真實 DB 的
    測試，其餘測試都不需要。
    """
    dsn = _real_test_db_reachable()
    if dsn is None:
        pytest.skip("需要可連線的 TEST_DB_DSN（見 .env），本機環境未設定或連不上")

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text(_DB_CHECK_APP_MAIN_TEMPLATE, encoding="utf-8")

    _init_repo(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "test: db-check app"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )

    ensure_reload_probe_infra(str(tmp_path))

    base_url = f"http://127.0.0.1:{_free_port()}"
    # dsn 是從 host 角度寫的（TEST_DB_DSN，例如指向 127.0.0.1）——這裡
    # 刻意直接把它當 database_url 傳進去，讓 PythonServiceContainer 自己
    # 的 _container_database_url() 做位址改寫，這正是要驗證的行為本身，
    # 不能在測試這一層先手動改寫掉。
    service = PythonServiceContainer(
        python_project_path=str(tmp_path),
        base_url=base_url,
        database_url=dsn,
        container_name=f"reload_probe_dbcheck_{tmp_path.name}",
        startup_timeout=90.0,
    )
    service.start()
    try:
        resp = httpx.get(f"{base_url}/db-check", timeout=10.0)
        assert resp.status_code == 200
        assert resp.json() == {"result": 1}
    finally:
        service.stop()
