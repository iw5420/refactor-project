"""python_service/java_properties.py，對應 docs/09b_bug_trace.md #46
「@Value 屬性注入機制只設計了怎麼命名／怎麼讀，沒有設計值從哪裡來」。
"""
from python_service.java_properties import resolve_active_profile, resolve_config_env_values


def _write_properties(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestResolveActiveProfile:
    def test_reads_spring_profiles_active_from_base_properties(self, tmp_path):
        _write_properties(
            tmp_path / "src/main/resources/application.properties",
            "spring.profiles.active=macuhau\nserver.port=8080\n",
        )
        assert resolve_active_profile(str(tmp_path)) == "macuhau"

    def test_returns_none_when_key_missing(self, tmp_path):
        _write_properties(
            tmp_path / "src/main/resources/application.properties",
            "server.port=8080\n",
        )
        assert resolve_active_profile(str(tmp_path)) is None

    def test_returns_none_when_base_file_missing(self, tmp_path):
        assert resolve_active_profile(str(tmp_path)) is None


class TestParsePropertiesFileBehaviorViaResolveConfigEnvValues:
    """`_parse_properties_file()` 是私有函式，透過公開入口
    `resolve_config_env_values()` 間接驗證解析行為，不直接測試私有實作
    細節。"""

    def test_skips_blank_lines_and_comments(self, tmp_path):
        _write_properties(
            tmp_path / "src/main/resources/application.properties",
            "\n# a comment\n! another comment\nlanguage.code=taiwan\n",
        )
        result = resolve_config_env_values(
            str(tmp_path), [{"property_key": "language.code", "constant_name": "LANGUAGE_CODE"}]
        )
        assert result == {"LANGUAGE_CODE": "taiwan"}

    def test_decodes_unicode_escape_sequences(self, tmp_path):
        """對應真實案例：application-macuhau.properties 用
        \\uXXXX escape 表示非 ASCII 字元（Java .properties 標準格式），
        見 lang-exam-api-refactor 的實際檔案內容。"""
        _write_properties(
            tmp_path / "src/main/resources/application.properties",
            "spring.profiles.active=macuhau\n",
        )
        _write_properties(
            tmp_path / "src/main/resources/application-macuhau.properties",
            "language.displayName=\\u99AC\\u7956\\u8A9E\n",
        )
        result = resolve_config_env_values(
            str(tmp_path), [{"property_key": "language.displayName", "constant_name": "LANGUAGE_DISPLAY_NAME"}]
        )
        assert result == {"LANGUAGE_DISPLAY_NAME": "馬祖語"}

    def test_splits_on_colon_separator_too(self, tmp_path):
        _write_properties(
            tmp_path / "src/main/resources/application.properties",
            "language.code:taiwan\n",
        )
        result = resolve_config_env_values(
            str(tmp_path), [{"property_key": "language.code", "constant_name": "LANGUAGE_CODE"}]
        )
        assert result == {"LANGUAGE_CODE": "taiwan"}


class TestResolveConfigEnvValues:
    def test_empty_config_env_vars_returns_empty_dict(self, tmp_path):
        assert resolve_config_env_values(str(tmp_path), []) == {}

    def test_profile_specific_value_overrides_base(self, tmp_path):
        """比照 Spring Boot 既有合併順序：profile 專屬蓋過 base。"""
        _write_properties(
            tmp_path / "src/main/resources/application.properties",
            "spring.profiles.active=macuhau\nlanguage.code=DEFAULT\n",
        )
        _write_properties(
            tmp_path / "src/main/resources/application-macuhau.properties",
            "language.code=macuhau\n",
        )
        result = resolve_config_env_values(
            str(tmp_path), [{"property_key": "language.code", "constant_name": "LANGUAGE_CODE"}]
        )
        assert result == {"LANGUAGE_CODE": "macuhau"}

    def test_base_only_key_still_resolved_when_not_overridden_by_profile(self, tmp_path):
        _write_properties(
            tmp_path / "src/main/resources/application.properties",
            "spring.profiles.active=macuhau\nonly.in.base=x\n",
        )
        _write_properties(
            tmp_path / "src/main/resources/application-macuhau.properties",
            "language.code=macuhau\n",
        )
        result = resolve_config_env_values(
            str(tmp_path), [{"property_key": "only.in.base", "constant_name": "ONLY_IN_BASE"}]
        )
        assert result == {"ONLY_IN_BASE": "x"}

    def test_missing_property_key_is_skipped_not_raised(self, tmp_path):
        """對應函式 docstring：查不到就記警告、略過，不猜值、不拋例外
        ——讓容器啟動後 os.environ[...] 的 KeyError 自己指出問題所在。"""
        _write_properties(
            tmp_path / "src/main/resources/application.properties",
            "spring.profiles.active=macuhau\n",
        )
        result = resolve_config_env_values(
            str(tmp_path), [{"property_key": "does.not.exist", "constant_name": "DOES_NOT_EXIST"}]
        )
        assert result == {}

    def test_works_without_active_profile_using_base_only(self, tmp_path):
        """base 沒有宣告 spring.profiles.active 時，仍然用 base 自己的
        key 查找，不因為沒有 profile 就整個放棄。"""
        _write_properties(
            tmp_path / "src/main/resources/application.properties",
            "language.code=fallback\n",
        )
        result = resolve_config_env_values(
            str(tmp_path), [{"property_key": "language.code", "constant_name": "LANGUAGE_CODE"}]
        )
        assert result == {"LANGUAGE_CODE": "fallback"}

    def test_resolves_multiple_entries_independently(self, tmp_path):
        _write_properties(
            tmp_path / "src/main/resources/application.properties",
            "spring.profiles.active=taiwan\n",
        )
        _write_properties(
            tmp_path / "src/main/resources/application-taiwan.properties",
            "language.code=taiwan\nlanguage.displayName=\\u81FA\\u7063\\u53F0\\u8A9E\n",
        )
        result = resolve_config_env_values(
            str(tmp_path),
            [
                {"property_key": "language.code", "constant_name": "LANGUAGE_CODE"},
                {"property_key": "language.displayName", "constant_name": "LANGUAGE_DISPLAY_NAME"},
            ],
        )
        assert result == {"LANGUAGE_CODE": "taiwan", "LANGUAGE_DISPLAY_NAME": "臺灣台語"}
