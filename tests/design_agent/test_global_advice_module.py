"""③ 架構設計 Agent 消費 `_global` 保留模組（parse_agent 端見
`tests/parse_agent/test_grouping.py`），機械產生全域例外處理，對應
`docs/09b_bug_trace.md` #11/#12。涵蓋 `design_agent/design.py` 的
`_exception_handler_targets()`／`_design_global_advice_module()`，以及
`design_agent/layout.py::render_main_py()` 的例外處理註冊行組裝。
"""
import logging

from design_agent import design, layout
from design_agent.design import _design_module, design_all_modules
from design_agent.layout import render_main_py

_GLOBAL_HANDLER_JAVA = """
package com.example.exception;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.bind.annotation.ExceptionHandler;
@RestControllerAdvice
public class GlobalExceptionHandler {
    @ExceptionHandler(BaseException.class)
    public Object handleBaseException(BaseException e) {
        return null;
    }

    @ExceptionHandler(Exception.class)
    public Object handleAll(Exception e) {
        return null;
    }
}
"""


def _global_module(java_files):
    return {
        "module": "_global",
        "summary": "全域例外處理",
        "java_files": java_files,
        "depends_on": [],
        "methods": [
            {
                "java_method": "handleBaseException",
                "class_name": "GlobalExceptionHandler",
                "description": "處理已知業務例外",
                "complexity": "low",
            },
            {
                "java_method": "handleAll",
                "class_name": "GlobalExceptionHandler",
                "description": "接住所有未處理例外，回傳 SERVER_ERROR",
                "complexity": "low",
            },
        ],
    }


def _write_global_handler(tmp_path):
    (tmp_path / "GlobalExceptionHandler.java").write_text(_GLOBAL_HANDLER_JAVA, encoding="utf-8")
    return ["GlobalExceptionHandler.java"]


# --------------------------------------------------------------------------
# _exception_handler_targets()
# --------------------------------------------------------------------------


def test_exception_handler_targets_extracts_single_class_literal(tmp_path):
    java_files = _write_global_handler(tmp_path)
    targets = design._exception_handler_targets(java_files, str(tmp_path))
    assert targets == {"handleBaseException": "BaseException", "handleAll": "Exception"}


def test_exception_handler_targets_ignores_non_exception_handler_methods(tmp_path):
    (tmp_path / "Advice.java").write_text(
        """
        package com.example;
        import org.springframework.web.bind.annotation.RestControllerAdvice;
        @RestControllerAdvice
        public class Advice {
            @Override
            public String notAHandler() { return "x"; }
        }
        """,
        encoding="utf-8",
    )
    targets = design._exception_handler_targets(["Advice.java"], str(tmp_path))
    assert targets == {}


# --------------------------------------------------------------------------
# _design_module()（module="_global"）：只有 catch-all 產生 InterfaceSpec
# --------------------------------------------------------------------------


def test_design_module_only_covers_catch_all_exception_handler(tmp_path, caplog):
    java_files = _write_global_handler(tmp_path)
    module = _global_module(java_files)

    with caplog.at_level(logging.WARNING):
        result = _design_module(
            module=module, boundary_index={}, openapi_spec={"paths": {}},
            java_project_path=str(tmp_path), interfaces_by_module={},
        )

    assert result.module == "_global"
    assert len(result.interfaces) == 1
    iface = result.interfaces[0]
    assert iface["file_path"] == "app/core/exception_handlers.py"
    assert iface["class_name"] is None
    assert iface["function_name"] == "handle_all"
    assert iface["params"] == [
        {"name": "request", "type": "Request"},
        {"name": "exc", "type": "Exception"},
    ]
    assert iface["return_type"] == "Response"
    assert iface["http_method"] is None
    assert iface["route_path"] is None
    assert result.directory_tree_fragment is None

    assert any("handleBaseException" in r.message and "範圍刻意收斂" in r.message for r in caplog.records)


def test_design_module_no_interfaces_when_nothing_is_catch_all(tmp_path):
    (tmp_path / "GlobalExceptionHandler.java").write_text(
        """
        package com.example.exception;
        import org.springframework.web.bind.annotation.RestControllerAdvice;
        import org.springframework.web.bind.annotation.ExceptionHandler;
        @RestControllerAdvice
        public class GlobalExceptionHandler {
            @ExceptionHandler(BaseException.class)
            public Object handleBaseException(BaseException e) { return null; }
        }
        """,
        encoding="utf-8",
    )
    module = {
        "module": "_global", "summary": "", "java_files": ["GlobalExceptionHandler.java"], "depends_on": [],
        "methods": [
            {"java_method": "handleBaseException", "class_name": "GlobalExceptionHandler",
             "description": "d", "complexity": "low"}
        ],
    }
    result = _design_module(
        module=module, boundary_index={}, openapi_spec={"paths": {}},
        java_project_path=str(tmp_path), interfaces_by_module={},
    )
    assert result.interfaces == []


# --------------------------------------------------------------------------
# layout.render_main_py()：例外處理註冊行
# --------------------------------------------------------------------------


def test_render_main_py_registers_exception_handler():
    interfaces = [
        {
            "file_path": "app/core/exception_handlers.py", "class_name": None,
            "function_name": "handle_all", "params": [], "return_type": "Response",
            "http_method": None, "route_path": None,
        },
    ]
    output = render_main_py(interfaces)
    assert "from app.core.exception_handlers import handle_all" in output
    assert "app.add_exception_handler(Exception, handle_all)" in output


def test_render_main_py_no_registration_when_no_exception_handler():
    output = render_main_py([])
    assert "add_exception_handler" not in output
    assert "exception_handlers" not in output


def test_render_main_py_combines_router_includes_and_exception_handler():
    interfaces = [
        {
            "file_path": "app/routers/widget_router.py", "class_name": None,
            "function_name": "list_widgets", "params": [], "return_type": "list",
            "http_method": "GET", "route_path": "/widgets",
        },
        {
            "file_path": "app/core/exception_handlers.py", "class_name": None,
            "function_name": "handle_all", "params": [], "return_type": "Response",
            "http_method": None, "route_path": None,
        },
    ]
    output = render_main_py(interfaces)
    assert "app.include_router(widget_router)" in output
    assert "app.add_exception_handler(Exception, handle_all)" in output
    # include_router 在前、例外處理註冊在後（見 render_main_py 組裝順序）
    assert output.index("app.include_router(widget_router)") < output.index("app.add_exception_handler")


# --------------------------------------------------------------------------
# design_all_modules()：端對端，"_global" 不需要呼叫 Claude API
# --------------------------------------------------------------------------


def test_design_all_modules_end_to_end_with_only_global_module(tmp_path):
    java_files = _write_global_handler(tmp_path)
    module_list = [_global_module(java_files)]

    interfaces, directory_tree, modules_with_schema_file = design_all_modules(
        module_list=module_list, api_to_python_target=[], openapi_spec={"paths": {}},
        java_project_path=str(tmp_path),
    )

    assert len(interfaces) == 1
    assert interfaces[0]["file_path"] == "app/core/exception_handlers.py"
    assert modules_with_schema_file == set()
    assert "app/core/exception_handlers.py" in directory_tree
    assert "app.add_exception_handler(Exception, handle_all)" in directory_tree
