"""① 解析 Agent「全域生效類別」收集路徑測試，對應
docs/09b_bug_trace.md #11/#12 根因：`@RestControllerAdvice`／
`@ControllerAdvice` 類別靠 Spring component-scan 自動生效，不被任何
Controller 用欄位注入，結構上永遠進不了 `controller_dependency_
closure()` 的 BFS 閉包，需要獨立的第二收集路徑（`collect_global_
advice_classes()`），機械組成一筆保留模組 `"_global"`（見
`summarize._assemble_global_advice_draft()`）。
"""
import json

from parse_agent import summarize
from parse_agent.call_graph import parse_java_project
from parse_agent.grouping import build_global_advice_units, collect_global_advice_classes
from parse_agent.prompts import MAP_SYSTEM_PROMPT, REDUCE_SYSTEM_PROMPT


# --------------------------------------------------------------------------
# collect_global_advice_classes()
# --------------------------------------------------------------------------


def test_detects_rest_controller_advice(tmp_path):
    (tmp_path / "GlobalExceptionHandler.java").write_text(
        """
        package com.example.exception;
        import org.springframework.web.bind.annotation.RestControllerAdvice;
        import org.springframework.web.bind.annotation.ExceptionHandler;
        @RestControllerAdvice
        public class GlobalExceptionHandler {
            @ExceptionHandler(Exception.class)
            public Object handleAll(Exception e) {
                return null;
            }
        }
        """,
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    assert collect_global_advice_classes(project) == {"GlobalExceptionHandler"}


def test_detects_plain_controller_advice(tmp_path):
    (tmp_path / "LegacyAdvice.java").write_text(
        """
        package com.example.exception;
        import org.springframework.web.bind.annotation.ControllerAdvice;
        @ControllerAdvice
        public class LegacyAdvice {
            public String handle() { return "x"; }
        }
        """,
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    assert collect_global_advice_classes(project) == {"LegacyAdvice"}


def test_ignores_regular_controller_and_service(tmp_path):
    (tmp_path / "WidgetController.java").write_text(
        """
        package com.example;
        import org.springframework.web.bind.annotation.RestController;
        @RestController
        public class WidgetController {
            public String list() { return "ok"; }
        }
        """,
        encoding="utf-8",
    )
    (tmp_path / "WidgetService.java").write_text(
        """
        package com.example;
        import org.springframework.stereotype.Service;
        @Service
        public class WidgetService {
            public String compute() { return "ok"; }
        }
        """,
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    assert collect_global_advice_classes(project) == set()


def test_empty_project_yields_empty_set(tmp_path):
    (tmp_path / "Plain.java").write_text(
        "package com.example;\npublic class Plain {}\n", encoding="utf-8"
    )
    project = parse_java_project(str(tmp_path))
    assert collect_global_advice_classes(project) == set()


def test_build_global_advice_units_batches_requested_classes(tmp_path):
    (tmp_path / "GlobalExceptionHandler.java").write_text(
        """
        package com.example.exception;
        import org.springframework.web.bind.annotation.RestControllerAdvice;
        @RestControllerAdvice
        public class GlobalExceptionHandler {
            public Object handleAll(Exception e) { return null; }
        }
        """,
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    names = collect_global_advice_classes(project)
    units = build_global_advice_units(project, names)
    assert len(units) == 1
    assert [c.class_name for c in units[0].classes] == ["GlobalExceptionHandler"]
    assert units[0].label.startswith("4c:global_advice_batch_")


# --------------------------------------------------------------------------
# run_map_reduce()：全域類別機械組成 "_global" 模組，不進 Reduce、
# 不影響其餘業務模組分派
# --------------------------------------------------------------------------


def _fake_call_claude_for_json(*, system_prompt, user_prompt, schema, model, max_tokens=4096):
    payload = json.loads(user_prompt)
    if system_prompt == MAP_SYSTEM_PROMPT:
        known_methods = {
            "WidgetController": ["list"],
            "GlobalExceptionHandler": ["handleAll"],
        }
        return {
            "classes": [
                {
                    "class_name": c["class_name"],
                    "summary": f"{c['class_name']} 的摘要",
                    "methods": [
                        {"method_name": m, "description": f"{m} 做的事", "complexity": "low"}
                        for m in known_methods[c["class_name"]]
                    ],
                    "cross_group_dependency_hints": [],
                }
                for c in payload["classes"]
            ]
        }
    if system_prompt == REDUCE_SYSTEM_PROMPT:
        # Reduce 只應該看到 WidgetController（業務類別），不該看到
        # GlobalExceptionHandler（全域類別走獨立路徑，不進 Reduce）。
        reduce_classes = {c["class_name"] for c in payload["classes"]}
        assert reduce_classes == {"WidgetController"}, (
            f"Reduce 不應該看到全域生效類別，實際收到: {reduce_classes}"
        )
        return {
            "modules": [
                {
                    "module": "widget",
                    "summary": "widget 業務模組",
                    "java_classes": ["WidgetController"],
                    "depends_on": [],
                }
            ]
        }
    raise AssertionError(f"未預期的 system_prompt: {system_prompt!r}")


def test_run_map_reduce_appends_global_module_without_touching_reduce(tmp_path, monkeypatch):
    (tmp_path / "WidgetController.java").write_text(
        """
        package com.example;
        import org.springframework.web.bind.annotation.RestController;
        @RestController
        public class WidgetController {
            public String list() { return "ok"; }
        }
        """,
        encoding="utf-8",
    )
    (tmp_path / "GlobalExceptionHandler.java").write_text(
        """
        package com.example.exception;
        import org.springframework.web.bind.annotation.RestControllerAdvice;
        @RestControllerAdvice
        public class GlobalExceptionHandler {
            public Object handleAll(Exception e) { return null; }
        }
        """,
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))

    monkeypatch.setattr(summarize, "call_claude_for_json", _fake_call_claude_for_json)

    drafts, class_to_module = summarize.run_map_reduce(project)

    modules = {d.module: d for d in drafts}
    assert set(modules) == {"widget", "_global"}

    global_draft = modules["_global"]
    assert global_draft.depends_on == []
    assert global_draft.java_files == ["GlobalExceptionHandler.java"]
    assert [dm.method["java_method"] for dm in global_draft.methods] == ["handleAll"]
    assert global_draft.methods[0].class_name == "GlobalExceptionHandler"

    widget_draft = modules["widget"]
    assert widget_draft.depends_on == []
    assert [dm.method["java_method"] for dm in widget_draft.methods] == ["list"]

    assert class_to_module == {"WidgetController": "widget", "GlobalExceptionHandler": "_global"}


def test_run_map_reduce_no_global_module_when_no_advice_class_present(tmp_path, monkeypatch):
    (tmp_path / "WidgetController.java").write_text(
        """
        package com.example;
        import org.springframework.web.bind.annotation.RestController;
        @RestController
        public class WidgetController {
            public String list() { return "ok"; }
        }
        """,
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    monkeypatch.setattr(summarize, "call_claude_for_json", _fake_call_claude_for_json)

    drafts, class_to_module = summarize.run_map_reduce(project)

    assert {d.module for d in drafts} == {"widget"}
    assert "GlobalExceptionHandler" not in class_to_module
