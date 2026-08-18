"""PythonServiceContainer 的純邏輯部分（不需要真的啟動 Docker），對應
python_service/process.py 的 `_container_database_url()`／`_port()`，
以及 `start()` 實際組出的 `docker run` 指令內容（mock `subprocess.run`，
不需要真的啟動 Docker，但要鎖住「這個旗標真的有被放進指令」這件事——
`_container_database_url()` 的字串重寫測試只證明了字串重寫邏輯本身
正確，沒有證明 `start()` 真的把重寫後的值、以及 `--add-host` 旗標放進
實際執行的指令）。

容器化服務啟動＋熱重載週期、以及「容器內真的能連到 host DB」的真實
驗證見 tests/python_service/test_reload_probe_integration.py（需要
Docker＋真實可連線的測試 DB）。
"""
from pathlib import Path

import pytest

from python_service.process import PythonServiceContainer, PythonServiceStartupTimeout


def _container(database_url: str) -> PythonServiceContainer:
    return PythonServiceContainer(
        python_project_path="/unused",
        base_url="http://127.0.0.1:18000",
        database_url=database_url,
    )


def test_container_database_url_rewrites_127_0_0_1_to_host_docker_internal():
    """容器內的 127.0.0.1 指向容器自己，不是跑 Orchestrator 的 host——
    測試 DB 通常監聽在 host 上，這裡的位址必須換掉才連得到，已用真實
    postgres:17-alpine 容器 + --add-host=host.docker.internal:host-gateway
    驗證過連線成功。
    """
    svc = _container("postgresql://postgres:password@127.0.0.1:5432/MOC_MATSUEXAM_TEST")
    assert svc._container_database_url() == (
        "postgresql://postgres:password@host.docker.internal:5432/MOC_MATSUEXAM_TEST"
    )


def test_container_database_url_rewrites_localhost():
    svc = _container("postgresql://postgres:password@localhost/MOC_MATSUEXAM_TEST")
    assert svc._container_database_url() == (
        "postgresql://postgres:password@host.docker.internal/MOC_MATSUEXAM_TEST"
    )


def test_container_database_url_leaves_other_hosts_unchanged():
    """已經指向非 host 位址（例如另一台機器、或已經是
    host.docker.internal）的情況不應該被誤改。
    """
    svc = _container("postgresql://postgres:password@db.internal.example.com:5432/prod")
    assert svc._container_database_url() == "postgresql://postgres:password@db.internal.example.com:5432/prod"


def test_port_extracted_from_base_url():
    svc = _container("postgresql://unused/unused")
    assert svc._port() == "18000"


def test_port_defaults_to_8000_when_base_url_has_no_explicit_port():
    svc = PythonServiceContainer(
        python_project_path="/unused", base_url="http://example.com", database_url="postgresql://unused/unused",
    )
    assert svc._port() == "8000"


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def test_start_builds_docker_run_command_with_add_host_and_rewritten_database_url(monkeypatch):
    """鎖住 start() 真正組出的 docker run 指令內容——不能只測
    _container_database_url() 這個字串重寫函式本身，那沒辦法保證
    start() 真的把結果放進實際執行的指令裡（例如日後有人不小心刪掉
    --add-host 那一行，_container_database_url() 的既有測試完全不會
    發現）。
    """
    calls = []

    def _fake_run(cmd, capture_output, text):
        calls.append(cmd)
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr("python_service.process.subprocess.run", _fake_run)
    monkeypatch.setattr(PythonServiceContainer, "_poll_until_ready", lambda self: True)

    # __init__ 內部用 Path(...).resolve() 正規化路徑（見 process.py），
    # 在不同作業系統上解析結果的分隔符號不同（POSIX vs Windows），這裡
    # 動態算出預期值，不寫死某一種平台的路徑格式。
    project_path = "/srv/target"
    expected_mount_src = str(Path(project_path).resolve())

    svc = PythonServiceContainer(
        python_project_path=project_path,
        base_url="http://127.0.0.1:18500",
        database_url="postgresql://postgres:password@127.0.0.1:5432/MOC_MATSUEXAM_TEST",
        container_name="test_container",
    )
    svc.start()

    # 第一次呼叫是啟動前的 `docker rm -f` 清理，第二次才是真正的 `docker run`
    run_cmd = next(c for c in calls if c[:2] == ["docker", "run"])

    assert "--add-host=host.docker.internal:host-gateway" in run_cmd
    assert "-e" in run_cmd
    database_url_arg = run_cmd[run_cmd.index("-e") + 1]
    assert database_url_arg == "DATABASE_URL=postgresql://postgres:password@host.docker.internal:5432/MOC_MATSUEXAM_TEST"
    assert "-v" in run_cmd
    assert run_cmd[run_cmd.index("-v") + 1] == f"{expected_mount_src}:/srv"
    assert "-p" in run_cmd
    assert run_cmd[run_cmd.index("-p") + 1] == "18500:8000"
    assert "--name" in run_cmd
    assert run_cmd[run_cmd.index("--name") + 1] == "test_container"


def test_start_raises_when_docker_run_fails(monkeypatch):
    def _fake_run(cmd, capture_output, text):
        if cmd[:2] == ["docker", "run"]:
            return _FakeCompletedProcess(returncode=1, stderr="Cannot connect to the Docker daemon")
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr("python_service.process.subprocess.run", _fake_run)

    svc = PythonServiceContainer(
        python_project_path="/srv/target",
        base_url="http://127.0.0.1:18500",
        database_url="postgresql://unused/unused",
    )
    with pytest.raises(PythonServiceStartupTimeout, match="docker run 啟動失敗"):
        svc.start()
