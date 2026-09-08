"""① 解析 Agent 對 utils 純靜態工具類呼叫（`ClassName.staticMethod()`）
的呼叫圖解析，對應 `docs/refactor_bug_trace.md` #15 真實案例。

真實案例（`lang-exam-api-refactor`）：`ExamService::createRandom()` 呼叫
`CodeUtil.generateRandomCode()`，`ExamController::getAllExamKind()`／
`RegistrationController::classes/grades/schools()`／`GeneralController::
locals/grades/schools()` 都呼叫 `CollectionUtil.findDistinctField(...)`
——這類 package 落在 `xxx.utils` 底下的純靜態工具類，`uses_dynamic_
query_signal`／`is_self_returning_static_factory` 兩個既有結構訊號都不命中，
修復前這些呼叫完全不會被加進呼叫圖，連鎖後果跟
`test_static_method_call_resolution.py` 模組 docstring 描述的一致：
`plan_agent/call_chain.py::build_reference_targets()` 依賴同一份呼叫圖
走訪，漏掉的呼叫永遠不會出現在 `reference_targets`，⑤ 翻譯呼叫端時看
不到 utils 已經被翻成什麼 Python 名稱（模組層級函式，不是類別），只能
照 Java 靜態呼叫語法瞎猜，猜成 `from app.utils.x import ClassName` 這種
class-style import，導致 `ImportError`。

判斷方式：`ClassInfo.is_utils_class`，沿用 `design_agent/layout.py::
is_utils_package()` 已經定案的同一個訊號——package 名稱最後一段是不是
`utils`，見 `call_graph.py::_is_utils_package()`。
"""
from parse_agent.call_graph import parse_java_project


_CODE_UTIL_JAVA = """
package com.example.utils;
public class CodeUtil {
    public static String generateRandomCode() {
        return "x";
    }
}
"""

_EXAM_SERVICE_JAVA = """
package com.example.service;
import com.example.utils.CodeUtil;
public class ExamService {
    public String createRandom() {
        return CodeUtil.generateRandomCode();
    }
}
"""


def _write_project(tmp_path):
    (tmp_path / "CodeUtil.java").write_text(_CODE_UTIL_JAVA, encoding="utf-8")
    (tmp_path / "ExamService.java").write_text(_EXAM_SERVICE_JAVA, encoding="utf-8")


def test_static_method_call_on_utils_class_is_resolved(tmp_path):
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))

    caller = "ExamService.java::ExamService::createRandom"
    callees = project.call_graph.get(caller, set())

    assert "CodeUtil.java::CodeUtil::generateRandomCode" in callees


def test_code_util_class_is_flagged_as_utils_class(tmp_path):
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))

    assert project.classes["CodeUtil"].is_utils_class is True
    assert project.classes["ExamService"].is_utils_class is False


def test_static_call_on_class_outside_utils_package_is_not_resolved(tmp_path):
    """刻意畫出這條邊界：跟 utils 工具類結構幾乎一樣（純靜態方法，非
    泛型），但 package **不在** `xxx.utils` 底下——不該只因為「看起來
    像工具類」（全靜態方法）就命中，`is_utils_class` 刻意用 package 名稱
    這個結構訊號判斷，不是靠方法簽名特徵推斷，見 `_is_utils_package()`
    docstring。
    """
    (tmp_path / "MathHelper.java").write_text(
        "package com.example.service.helper;\n"
        "public class MathHelper {\n"
        "    public static int square(int x) { return x * x; }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "Caller.java").write_text(
        "package com.example.service;\n"
        "import com.example.service.helper.MathHelper;\n"
        "public class Caller {\n"
        "    public int run() {\n"
        "        return MathHelper.square(2);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    assert project.classes["MathHelper"].is_utils_class is False
    callees = project.call_graph.get("Caller.java::Caller::run", set())
    assert callees == set()


def test_field_qualified_instance_call_still_resolves_after_utils_fix(tmp_path):
    """既有的欄位鏈呼叫解析不能被新的 utils 靜態呼叫判斷搶先攔截而失效
    ——同一個檔案裡兩種呼叫（欄位注入的 repository＋靜態呼叫的 utils）
    要能同時正確解析，互不干擾。"""
    (tmp_path / "CodeUtil.java").write_text(_CODE_UTIL_JAVA, encoding="utf-8")
    (tmp_path / "ExamRepository.java").write_text(
        "package com.example.repository;\n"
        "public class ExamRepository {\n"
        "    public String findByCard(String card) { return null; }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "ExamService.java").write_text(
        "package com.example.service;\n"
        "import com.example.utils.CodeUtil;\n"
        "import com.example.repository.ExamRepository;\n"
        "public class ExamService {\n"
        "    private ExamRepository examRepository;\n"
        "    public String createRandom(String card) {\n"
        "        String code = CodeUtil.generateRandomCode();\n"
        "        return examRepository.findByCard(card);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    callees = project.call_graph.get("ExamService.java::ExamService::createRandom", set())
    assert "CodeUtil.java::CodeUtil::generateRandomCode" in callees
    assert "ExamRepository.java::ExamRepository::findByCard" in callees
