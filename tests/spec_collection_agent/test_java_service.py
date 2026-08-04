"""`java_service.py` 的假資料單元測試（見 docs/03b_spec_agent_code.md 六章）。

只測不依賴外部服務的純邏輯：JDBC URL 解析、jar 路徑解析。
`JavaServiceProcess.start()` 對真實 `java -jar` 進程的行為留給整合驗證
（見 03b 六章「沒有驗證的部分」）。
"""
import pytest

from spec_collection_agent.java_service import PROJECT_ROOT, _parse_jdbc_host_port, resolve_jar_path


class TestParseJdbcHostPort:
    def test_explicit_port(self):
        host, port = _parse_jdbc_host_port(
            "jdbc:postgresql://127.0.0.1:5432/MOC_MATSUEXAM_TEST"
        )
        assert host == "127.0.0.1"
        assert port == 5432

    def test_default_port_when_unspecified(self):
        host, port = _parse_jdbc_host_port("jdbc:postgresql://db.internal/MOC_MATSUEXAM_TEST")
        assert host == "db.internal"
        assert port == 5432

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="只支援 PostgreSQL"):
            _parse_jdbc_host_port("jdbc:sqlserver://127.0.0.1:1433;databaseName=Foo")

    def test_missing_host_raises(self):
        with pytest.raises(ValueError, match="無法從 SPRING_DATASOURCE_URL 解析出 host"):
            _parse_jdbc_host_port("jdbc:postgresql:///MOC_MATSUEXAM_TEST")


class TestResolveJarPath:
    def test_relative_path_anchored_to_project_root(self):
        resolved = resolve_jar_path("../lang-exam-api-refactor/target/app.jar")
        expected = (PROJECT_ROOT / "../lang-exam-api-refactor/target/app.jar").resolve()
        assert resolved == expected

    def test_absolute_path_passed_through(self, tmp_path):
        abs_path = tmp_path / "app.jar"
        resolved = resolve_jar_path(str(abs_path))
        assert resolved == abs_path
