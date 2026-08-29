"""python_service/manager.py，對應 docs/10a_debug_agent_architecture.md
八章「診斷資料改走 State，不是 debug_agent/ 直接 import implement_node」。
"""
import asyncio

import python_service.manager as manager


class _FakeContainer:
    def __init__(self, *, python_project_path, base_url, database_url, extra_env=None):
        self.python_project_path = python_project_path
        self.base_url = base_url
        self.database_url = database_url
        self.extra_env = extra_env
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    @property
    def diagnostics(self):
        return "fake container log"


def _reset_singleton(monkeypatch):
    monkeypatch.setattr(manager, "_python_service", None)


class TestEnsureStarted:
    def test_starts_container_first_time(self, monkeypatch):
        _reset_singleton(monkeypatch)
        monkeypatch.setattr(manager, "PythonServiceContainer", _FakeContainer)
        monkeypatch.setenv("DATABASE_URL", "postgresql://unused")

        asyncio.run(manager.ensure_started("/proj", "http://unused"))

        assert manager._python_service is not None
        assert manager._python_service.started is True

    def test_second_call_does_not_restart(self, monkeypatch):
        _reset_singleton(monkeypatch)
        monkeypatch.setattr(manager, "PythonServiceContainer", _FakeContainer)
        monkeypatch.setenv("DATABASE_URL", "postgresql://unused")

        asyncio.run(manager.ensure_started("/proj", "http://unused"))
        first_instance = manager._python_service

        asyncio.run(manager.ensure_started("/proj", "http://unused"))

        assert manager._python_service is first_instance

    def test_extra_env_empty_when_java_project_path_and_config_env_vars_not_given(self, monkeypatch):
        """對應 docs/09b_bug_trace.md #46：沒有任何 @Value 欄位的專案，
        state["python_structure"] 不會有 config_env_vars 這個 key，呼叫端
        傳 None（見 graph/nodes/implement_node.py::
        _ensure_python_service_started()）——這裡必須表現得跟這個機制
        完全不存在一樣，不嘗試解析。"""
        _reset_singleton(monkeypatch)
        monkeypatch.setattr(manager, "PythonServiceContainer", _FakeContainer)
        monkeypatch.setenv("DATABASE_URL", "postgresql://unused")

        asyncio.run(manager.ensure_started("/proj", "http://unused"))

        assert manager._python_service.extra_env == {}

    def test_extra_env_resolved_when_java_project_path_and_config_env_vars_given(self, monkeypatch):
        _reset_singleton(monkeypatch)
        monkeypatch.setattr(manager, "PythonServiceContainer", _FakeContainer)
        monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
        monkeypatch.setattr(
            manager, "resolve_config_env_values",
            lambda java_project_path, config_env_vars: {"LANGUAGE_CODE": "macuhau"},
        )

        asyncio.run(
            manager.ensure_started(
                "/proj", "http://unused",
                java_project_path="/java-proj",
                config_env_vars=[{"property_key": "language.code", "constant_name": "LANGUAGE_CODE"}],
            )
        )

        assert manager._python_service.extra_env == {"LANGUAGE_CODE": "macuhau"}


class TestStop:
    def test_stops_and_clears_singleton(self, monkeypatch):
        _reset_singleton(monkeypatch)
        monkeypatch.setattr(manager, "PythonServiceContainer", _FakeContainer)
        monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
        asyncio.run(manager.ensure_started("/proj", "http://unused"))
        instance = manager._python_service

        asyncio.run(manager.stop())

        assert instance.stopped is True
        assert manager._python_service is None

    def test_stop_when_never_started_is_a_noop(self, monkeypatch):
        _reset_singleton(monkeypatch)
        asyncio.run(manager.stop())  # 不應該拋例外
        assert manager._python_service is None


class TestGetDiagnostics:
    def test_returns_none_when_not_started(self, monkeypatch):
        _reset_singleton(monkeypatch)
        assert manager.get_diagnostics() is None

    def test_returns_container_diagnostics_when_started(self, monkeypatch):
        _reset_singleton(monkeypatch)
        monkeypatch.setattr(manager, "PythonServiceContainer", _FakeContainer)
        monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
        asyncio.run(manager.ensure_started("/proj", "http://unused"))

        assert manager.get_diagnostics() == "fake container log"
