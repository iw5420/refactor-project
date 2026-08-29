"""design_agent/global_infra.py，對應 05a 十五章、docs/09b_bug_trace.md
#44（剩餘部分：ErrorCode 本身）、#45（@Value 屬性注入根因）。LLM 呼叫點
（design_enum_backed_interfaces()）monkeypatch 掉 call_claude_for_json，
不打真實 API，比照 tests/plan_agent/test_planning.py 既有的 mock 方式。
"""
import textwrap

import pytest

from design_agent import global_infra
from design_agent.exceptions import DesignAgentModuleError


def _write(tmp_path, rel_path: str, source: str) -> None:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(textwrap.dedent(source), encoding="utf-8")


def _module(name: str, java_files: list[str]) -> dict:
    return {"module": name, "summary": "", "depends_on": [], "methods": [], "java_files": java_files}


# ── scan_value_injected_fields() ──────────────────────────────────────


def test_scan_value_injected_field_extracts_property_key_and_python_file_path(tmp_path):
    _write(
        tmp_path,
        "GeneralController.java",
        """
        package com.example.controller;
        import org.springframework.web.bind.annotation.RestController;
        import org.springframework.beans.factory.annotation.Value;
        @RestController
        public class GeneralController {
            @Value("${language.code}")
            private String code;
        }
        """,
    )
    module_list = [_module("general", ["GeneralController.java"])]
    fields = global_infra.scan_value_injected_fields(module_list, str(tmp_path))

    assert len(fields) == 1
    f = fields[0]
    assert f.class_name == "GeneralController"
    assert f.field_name == "code"
    assert f.property_key == "language.code"
    # RestController → routers 層，見 layout.layer_for_stereotype()。
    assert f.python_file_path == "app/routers/general_router.py"


def test_scan_value_injected_field_without_stereotype_has_no_python_file_path(tmp_path, caplog):
    """無 Spring stereotype 的類別，layer_for_stereotype() 判斷不出層級
    ——config.py 仍然照常產生（見 render_config_py() 測試），只是這個
    欄位不會有 config_field_mappings 條目可用，記警告不中止。"""
    _write(
        tmp_path,
        "PlainHolder.java",
        """
        package com.example;
        import org.springframework.beans.factory.annotation.Value;
        public class PlainHolder {
            @Value("${some.key}")
            private String value;
        }
        """,
    )
    module_list = [_module("hr", ["PlainHolder.java"])]
    fields = global_infra.scan_value_injected_fields(module_list, str(tmp_path))

    assert len(fields) == 1
    assert fields[0].python_file_path is None
    assert "無法機械判定對應的 Python 檔案" in caplog.text


def test_scan_value_injected_field_non_literal_element_skipped(tmp_path, caplog):
    """@Value(SomeConstants.KEY) 這種常數參照不是 Literal，視同缺席
    （Literal 規則，見 08a 四章同一套精神）。"""
    _write(
        tmp_path,
        "Weird.java",
        """
        package com.example;
        import org.springframework.web.bind.annotation.RestController;
        import org.springframework.beans.factory.annotation.Value;
        @RestController
        public class Weird {
            @Value(SomeConstants.KEY)
            private String value;
        }
        """,
    )
    module_list = [_module("hr", ["Weird.java"])]
    fields = global_infra.scan_value_injected_fields(module_list, str(tmp_path))
    assert fields == []
    assert "不是字面字串" in caplog.text


def test_scan_value_injected_field_non_dollar_brace_format_skipped(tmp_path, caplog):
    """@Value("literal") 或 SpEL（沒有 ${...} 包裝）不是這裡處理的形式。"""
    _write(
        tmp_path,
        "Weird2.java",
        """
        package com.example;
        import org.springframework.web.bind.annotation.RestController;
        import org.springframework.beans.factory.annotation.Value;
        @RestController
        public class Weird2 {
            @Value("#{someBean.value}")
            private String value;
        }
        """,
    )
    module_list = [_module("hr", ["Weird2.java"])]
    fields = global_infra.scan_value_injected_fields(module_list, str(tmp_path))
    assert fields == []
    assert "不是 ${key} 格式" in caplog.text


# ── property_key_to_constant_name() / render_config_py() ──────────────


@pytest.mark.parametrize(
    "key, expected",
    [
        ("language.code", "LANGUAGE_CODE"),
        ("language.displayName", "LANGUAGE_DISPLAY_NAME"),
    ],
)
def test_property_key_to_constant_name(key, expected):
    assert global_infra.property_key_to_constant_name(key) == expected


def test_render_config_py_empty_returns_none():
    content, mapping, env_vars = global_infra.render_config_py([])
    assert content is None
    assert mapping == {}
    assert env_vars == []


def test_render_config_py_renders_env_reads_and_field_mappings():
    fields = [
        global_infra.ValueInjectedField(
            file_path="GeneralController.java", class_name="GeneralController", field_name="code",
            property_key="language.code", python_file_path="app/routers/general_router.py",
        ),
        global_infra.ValueInjectedField(
            file_path="GeneralController.java", class_name="GeneralController", field_name="displayName",
            property_key="language.displayName", python_file_path="app/routers/general_router.py",
        ),
    ]
    content, mapping, env_vars = global_infra.render_config_py(fields)
    assert content == (
        'import os\n\nLANGUAGE_CODE = os.environ["LANGUAGE_CODE"]\n'
        'LANGUAGE_DISPLAY_NAME = os.environ["LANGUAGE_DISPLAY_NAME"]\n'
    )
    assert mapping == {
        "app/routers/general_router.py": {
            "code": "app.core.config.LANGUAGE_CODE",
            "displayName": "app.core.config.LANGUAGE_DISPLAY_NAME",
        }
    }
    assert env_vars == [
        {"property_key": "language.code", "constant_name": "LANGUAGE_CODE"},
        {"property_key": "language.displayName", "constant_name": "LANGUAGE_DISPLAY_NAME"},
    ]


def test_render_config_py_skips_fields_without_python_file_path():
    """python_file_path 是 None 的欄位仍然產生環境變數常數（config.py
    本身不受影響），也仍然出現在 config_env_vars 裡（容器啟動時一樣需要
    注入這個值，跟 [P] 有沒有 task.context 提示可用是兩件事），但不會
    出現在 config_field_mappings 裡（見 ValueInjectedField.
    python_file_path docstring）。"""
    fields = [
        global_infra.ValueInjectedField(
            file_path="Plain.java", class_name="Plain", field_name="value",
            property_key="some.key", python_file_path=None,
        ),
    ]
    content, mapping, env_vars = global_infra.render_config_py(fields)
    assert 'SOME_KEY = os.environ["SOME_KEY"]' in content
    assert mapping == {}
    assert env_vars == [{"property_key": "some.key", "constant_name": "SOME_KEY"}]


def test_render_config_py_dedupes_env_vars_by_property_key():
    """同一個 property key 被兩個欄位/檔案各自引用時，config_env_vars
    只出現一次（跟 constants 字典本身的去重語意一致，見函式 docstring）。
    """
    fields = [
        global_infra.ValueInjectedField(
            file_path="A.java", class_name="A", field_name="code",
            property_key="language.code", python_file_path="app/routers/a_router.py",
        ),
        global_infra.ValueInjectedField(
            file_path="B.java", class_name="B", field_name="code",
            property_key="language.code", python_file_path="app/services/b_service.py",
        ),
    ]
    _, _, env_vars = global_infra.render_config_py(fields)
    assert env_vars == [{"property_key": "language.code", "constant_name": "LANGUAGE_CODE"}]


# ── scan_enum_backed_interfaces() ──────────────────────────────────────


_ERROR_CODE_JAVA = """
package com.example.common;
public interface ErrorCode {
    int getCode();
    String getMsg();
}
"""

_COMMON_ERROR_CODE_JAVA = """
package com.example.common;
public enum CommonErrorCode implements ErrorCode {
    SUCCESS(200, "ok");
    private final int code;
    private final String msg;
    CommonErrorCode(int code, String msg) { this.code = code; this.msg = msg; }
}
"""


def test_scan_enum_backed_interfaces_detects_pure_enum_backed_interface(tmp_path):
    _write(tmp_path, "ErrorCode.java", _ERROR_CODE_JAVA)
    _write(tmp_path, "CommonErrorCode.java", _COMMON_ERROR_CODE_JAVA)
    module_list = [_module("common", ["ErrorCode.java", "CommonErrorCode.java"])]

    result = global_infra.scan_enum_backed_interfaces(module_list, str(tmp_path))

    assert len(result) == 1
    iface = result[0]
    assert iface.interface_name == "ErrorCode"
    assert set(iface.method_signatures) == {"int getCode()", "String getMsg()"}
    assert len(iface.implementors) == 1
    assert iface.implementors[0].class_name == "CommonErrorCode"
    assert ("code", "int") in iface.implementors[0].fields
    assert ("msg", "String") in iface.implementors[0].fields


def test_scan_enum_backed_interfaces_excludes_interface_with_class_implementor(tmp_path):
    """也被一般 class 實作的 interface 是一般共用 interface，不歸這個
    新機制管（走既有 _extract_interfaces()／共用類別既有路徑）。"""
    _write(tmp_path, "ErrorCode.java", _ERROR_CODE_JAVA)
    _write(tmp_path, "CommonErrorCode.java", _COMMON_ERROR_CODE_JAVA)
    _write(
        tmp_path,
        "ClassImpl.java",
        """
        package com.example.common;
        public class ClassImpl implements ErrorCode {
            public int getCode() { return 0; }
            public String getMsg() { return ""; }
        }
        """,
    )
    module_list = [_module("common", ["ErrorCode.java", "CommonErrorCode.java", "ClassImpl.java"])]
    result = global_infra.scan_enum_backed_interfaces(module_list, str(tmp_path))
    assert result == []


def test_scan_enum_backed_interfaces_no_match_returns_empty(tmp_path):
    _write(tmp_path, "Plain.java", "package com.example;\npublic class Plain {}\n")
    module_list = [_module("hr", ["Plain.java"])]
    assert global_infra.scan_enum_backed_interfaces(module_list, str(tmp_path)) == []


# ── design_enum_backed_interfaces()（monkeypatch 掉 Claude 呼叫）──────


def test_design_enum_backed_interfaces_empty_input_skips_llm_call(monkeypatch):
    def _boom(**kwargs):
        raise AssertionError("不該呼叫 Claude API")

    monkeypatch.setattr(global_infra, "call_claude_for_json", _boom)
    assert global_infra.design_enum_backed_interfaces([]) == []


def test_design_enum_backed_interfaces_returns_file_path_and_source(monkeypatch):
    iface = global_infra.EnumBackedInterface(
        interface_name="ErrorCode", file_path="ErrorCode.java",
        method_signatures=["int getCode()", "String getMsg()"],
        implementors=[global_infra._EnumImplementorInfo("CommonErrorCode", "CommonErrorCode.java", [("code", "int")])],
    )

    def _fake(*, system_prompt, user_prompt, schema, model, target_file, class_name):
        assert class_name == "ErrorCode"
        return {"python_module_path": "app/core/error_code.py", "python_source": "class ErrorCode: ...\n"}

    monkeypatch.setattr(global_infra, "call_claude_for_json", _fake)
    result = global_infra.design_enum_backed_interfaces([iface])
    assert result == [("app/core/error_code.py", "class ErrorCode: ...\n")]


def test_design_enum_backed_interfaces_retries_then_raises_on_persistent_failure(monkeypatch):
    """比照 05a 六章既有的「單一呼叫失敗」節奏：失敗列入待重試清單，
    等待後重試一次，仍失敗中止（DesignAgentModuleError）。"""
    iface = global_infra.EnumBackedInterface(
        interface_name="ErrorCode", file_path="ErrorCode.java",
        method_signatures=[], implementors=[],
    )

    def _always_fail(**kwargs):
        from common.llm_client import LlmJsonError
        raise LlmJsonError("boom")

    monkeypatch.setattr(global_infra, "call_claude_for_json", _always_fail)
    monkeypatch.setattr(global_infra.time, "sleep", lambda seconds: None)

    with pytest.raises(DesignAgentModuleError):
        global_infra.design_enum_backed_interfaces([iface])
