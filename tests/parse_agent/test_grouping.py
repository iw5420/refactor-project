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


# --------------------------------------------------------------------------
# _assemble_module_drafts()：Reduce 把同一個 class 重複分進兩個 module 時
# 的機械防線，對應 docs/09b_bug_trace.md #50 根因——REDUCE_SYSTEM_PROMPT
# 只在文字裡要求「一個 class 只屬於一個 module」，schema 的 enum 約束擋不住
# 同一個合法 class 出現在兩個不同 module_entry 的 java_classes 裡。
# --------------------------------------------------------------------------


def _fake_call_claude_for_json_duplicate_class(*, system_prompt, user_prompt, schema, model, max_tokens=4096):
    payload = json.loads(user_prompt)
    if system_prompt == MAP_SYSTEM_PROMPT:
        known_methods = {
            "FileController": ["voice", "image"],
            "ExamController": ["search"],
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
        # 模擬 Reduce 分類失手：FileController 同時被分進 file 與 exam
        # 兩個 module_entry（09b_bug_trace.md #50 真實案例的重現）。
        return {
            "modules": [
                {
                    "module": "file",
                    "summary": "file 業務模組",
                    "java_classes": ["FileController"],
                    "depends_on": [],
                },
                {
                    "module": "exam",
                    "summary": "exam 業務模組",
                    "java_classes": ["ExamController", "FileController"],
                    "depends_on": [],
                },
            ]
        }
    raise AssertionError(f"未預期的 system_prompt: {system_prompt!r}")


def test_run_map_reduce_drops_class_duplicated_across_modules_keeping_first(tmp_path, monkeypatch):
    (tmp_path / "FileController.java").write_text(
        """
        package com.example;
        import org.springframework.web.bind.annotation.RestController;
        @RestController
        public class FileController {
            public String voice() { return "ok"; }
            public String image() { return "ok"; }
        }
        """,
        encoding="utf-8",
    )
    (tmp_path / "ExamController.java").write_text(
        """
        package com.example;
        import org.springframework.web.bind.annotation.RestController;
        @RestController
        public class ExamController {
            public String search() { return "ok"; }
        }
        """,
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    monkeypatch.setattr(summarize, "call_claude_for_json", _fake_call_claude_for_json_duplicate_class)

    drafts, class_to_module = summarize.run_map_reduce(project)

    modules = {d.module: d for d in drafts}
    assert set(modules) == {"file", "exam"}

    # 先宣告的 file module 保留 FileController 完整的 java_files／methods。
    file_draft = modules["file"]
    assert file_draft.java_files == ["FileController.java"]
    assert {dm.method["java_method"] for dm in file_draft.methods} == {"voice", "image"}

    # 後宣告的 exam module 只保留自己合法擁有的 ExamController，
    # FileController 的重複宣告被機械擋下，不會讓 voice/image 也流進
    # exam 的 java_files／methods（對應 09b_bug_trace.md #50 的症狀：
    # 若不擋，exam_router.py 會被④機械渲染出重複、且路由衝突的
    # voice/voice_2/image/image_2 端點）。
    exam_draft = modules["exam"]
    assert exam_draft.java_files == ["ExamController.java"]
    assert {dm.method["java_method"] for dm in exam_draft.methods} == {"search"}

    assert class_to_module == {"FileController": "file", "ExamController": "exam"}
