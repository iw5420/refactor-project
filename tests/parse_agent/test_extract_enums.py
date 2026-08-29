"""① 解析 Agent 對獨立宣告 Java `enum` 的擷取，對應
docs/09b_bug_trace.md #44 根因：`parse_agent/call_graph.py` 原本只有
`_extract_classes()`（`ClassDeclaration`）與 `_extract_interfaces()`
（`InterfaceDeclaration`），沒有 `_extract_enums()`——獨立宣告的業務
enum（如自訂錯誤碼列舉）永遠不會出現在 `project.classes`，即使有其他
class 明確 `import` 它，`controller_dependency_closure()` 的 import 依賴
邊也找不到對應節點可以連，導致這個 enum 永遠無法被任何 module 收進
`module_list.java_files`。
"""
from parse_agent.call_graph import parse_java_project
from parse_agent.grouping import classify_trivial_classes, controller_dependency_closure


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
    CommonErrorCode(int code, String msg) {
        this.code = code;
        this.msg = msg;
    }
}
"""

_RESPONSE_RESULT_JAVA = """
package com.example.common;
public class ResponseResult<T> {
    public static <T> ResponseResult<T> error(ErrorCode code) {
        return null;
    }
}
"""

_CONTROLLER_JAVA = """
package com.example.controller;
import com.example.common.ResponseResult;
import com.example.common.CommonErrorCode;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.GetMapping;
@RestController
public class GeneralController {
    @GetMapping("/x")
    public ResponseResult<String> x() {
        return ResponseResult.error(CommonErrorCode.SUCCESS);
    }
}
"""


def _write_project(tmp_path):
    (tmp_path / "ErrorCode.java").write_text(_ERROR_CODE_JAVA, encoding="utf-8")
    (tmp_path / "CommonErrorCode.java").write_text(_COMMON_ERROR_CODE_JAVA, encoding="utf-8")
    (tmp_path / "ResponseResult.java").write_text(_RESPONSE_RESULT_JAVA, encoding="utf-8")
    (tmp_path / "GeneralController.java").write_text(_CONTROLLER_JAVA, encoding="utf-8")


def test_extract_enums_builds_class_info_with_implements(tmp_path):
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))

    enum_info = project.classes["CommonErrorCode"]
    assert enum_info.is_enum is True
    assert enum_info.implements == ["ErrorCode"]
    assert enum_info.methods == []
    assert enum_info.fields == []

    interface_info = project.classes["ErrorCode"]
    assert interface_info.is_enum is False


def test_enum_only_reachable_via_transitive_import_enters_dependency_closure(tmp_path):
    """對應真實案例：GeneralController 明確 import CommonErrorCode（只是
    當成方法呼叫的參數值，不是欄位型別），既有 import 依賴邊機制
    （`_extract_project_imports()`）本來就抓得到這條邊——修復前 #44
    實際卡住的地方是 CommonErrorCode 完全沒有 ClassInfo 可以連，這條邊
    連候選節點都不存在，`_extract_enums()` 補上之後這條既有邊自然生效。
    """
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))

    closure = controller_dependency_closure(project)
    assert "CommonErrorCode" in closure["GeneralController"]


def test_enum_classified_as_trivial_no_llm_summary_needed(tmp_path):
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))

    trivial = classify_trivial_classes(project)
    assert "CommonErrorCode" in trivial
