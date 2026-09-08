"""① 解析 Agent 對 Specification 動態查詢 helper class 靜態方法呼叫
（`ClassName.staticMethod()`）的呼叫圖解析，對應 04a 三章「Import 依賴
補充」提到的既知案例：用 `org.springframework.data.jpa.domain.
Specification` 組動態查詢條件的 helper class，透過裸類別名稱靜態呼叫，
不是欄位注入。

`_resolve_qualifier_string()` 修復前只把 qualifier 每一段當「目前候選
類別的 field 名稱」解析，靜態呼叫的 qualifier（裸類別名稱，不是欄位）
永遠解析不到、`context` 變成 `None`，這個呼叫完全不會被加進呼叫圖——
連鎖後果：`plan_agent/call_chain.py::build_reference_targets()`
（06a 六章「呼叫鏈範圍查找」）依賴同一份呼叫圖走訪，漏掉的呼叫永遠不會
出現在 `reference_targets`，⑤ 翻譯呼叫端時看不到這個 helper class 已經
被翻譯成什麼 Python 名稱、該怎麼呼叫它。

**刻意只處理少數已知結構訊號命中的特例，不是任何裸類別名稱靜態呼叫都
解析**：呼叫圖同時供 `skip_filter.py` 可達性分析、`reference_targets`
BFS 使用，放寬到所有靜態呼叫會擴大這個函式的行為變動範圍到整個呼叫圖
的語意；只鎖定 `ClassInfo.uses_dynamic_query_signal=True`（該檔案
import 了 `org.springframework.data.jpa.domain.Specification`）、
`is_self_returning_static_factory=True`（泛型自我回傳靜態工廠）、
`is_utils_class=True`（package 落在 `xxx.utils` 底下的純靜態工具類，
見 `docs/refactor_bug_trace.md` #15、`tests/parse_agent/
test_utils_static_call_resolution.py`）三種類別，範圍精準、風險可控——
這是跟使用者確認過的設計方向：「只依照已知的特定結構訊號才走這條
邏輯」，不是通用的靜態呼叫解析器。

真實案例（`lang-exam-api-refactor`）：`ExamController::search()` 呼叫
`ExamSpecification.withYear(...)`／`withGrade(...)` 等一串靜態方法組
`Specification<ExamEntity>` 動態查詢條件——修復前 `search()` 在呼叫圖裡
的呼叫清單是空的，已用真實專案驗證過（見對話紀錄）。
"""
from parse_agent.call_graph import parse_java_project


_SPECIFICATION_HELPER_JAVA = """
package com.example.repository;
import org.springframework.data.jpa.domain.Specification;
import com.example.entity.ExamEntity;
public class ExamSpecification {
    public static Specification<ExamEntity> withYear(String year) {
        return (root, query, cb) -> cb.equal(root.get("year"), year);
    }
    public static Specification<ExamEntity> withGrade(String grade) {
        return (root, query, cb) -> cb.equal(root.get("grade"), grade);
    }
}
"""

_CONTROLLER_JAVA = """
package com.example.controller;
import com.example.repository.ExamSpecification;
import org.springframework.data.jpa.domain.Specification;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.GetMapping;
@RestController
public class ExamController {
    @GetMapping("/exam")
    public String search(String year, String grade) {
        Specification<Object> spec = Specification.where(ExamSpecification.withYear(year))
                .and(ExamSpecification.withGrade(grade));
        return spec.toString();
    }
}
"""


def _write_project(tmp_path):
    (tmp_path / "ExamSpecification.java").write_text(_SPECIFICATION_HELPER_JAVA, encoding="utf-8")
    (tmp_path / "ExamController.java").write_text(_CONTROLLER_JAVA, encoding="utf-8")


def test_static_method_call_on_specification_helper_is_resolved(tmp_path):
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))

    caller = "ExamController.java::ExamController::search"
    callees = project.call_graph.get(caller, set())

    assert "ExamSpecification.java::ExamSpecification::withYear" in callees
    assert "ExamSpecification.java::ExamSpecification::withGrade" in callees


def test_static_call_on_non_specification_class_is_not_resolved(tmp_path):
    """刻意畫出這條邊界：靜態呼叫的目標類別**三個既知訊號都不命中**
    （package 不在 `xxx.utils` 底下、沒 import `Specification`、也沒有
    任何 static 方法回傳自己）時，維持修復前的行為——欄位解析找不到就
    整條呼叫從呼叫圖裡消失，不會被新的靜態呼叫分支撿起來。這是設計上
    的刻意選擇，不是尚未涵蓋到的缺口。

    **訂正（docs/refactor_bug_trace.md #38）**：這裡原本用「非泛型的
    `ResponseResult`（沒有 `<T>`）」當「三個訊號都不命中」的邊界案例，
    理由是舊版 `is_generic_response_wrapper` 要求泛型——`UserProfileRs`
    真實案例推翻了「必須泛型」這個限制後，非泛型的自我回傳靜態工廠
    現在**應該**被 `is_self_returning_static_factory` 命中，這個舊案例
    不再適合當「不命中」的邊界，改用一個 static 方法**不回傳自己**的
    類別（`Helper.build()` 回傳 `String`），才是真正三個訊號都不命中
    的案例。
    """
    (tmp_path / "Helper.java").write_text(
        "package com.example.common;\n"
        "public class Helper {\n"
        "    public static String build(Object data) { return data.toString(); }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "Caller.java").write_text(
        "package com.example;\n"
        "import com.example.common.Helper;\n"
        "public class Caller {\n"
        "    public String run() {\n"
        "        return Helper.build(\"x\");\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    callees = project.call_graph.get("Caller.java::Caller::run", set())
    assert callees == set()


def test_static_call_to_class_outside_project_is_silently_skipped(tmp_path):
    """靜態呼叫的目標類別不在專案掃描範圍內（外部函式庫、或掃描範圍外）
    時，比照既有「查不到就不算依賴、不拋錯」的一貫精神，不加邊、不報錯。
    """
    (tmp_path / "Caller.java").write_text(
        "package com.example;\n"
        "public class Caller {\n"
        "    public void run() {\n"
        "        java.util.Collections.emptyList();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    callees = project.call_graph.get("Caller.java::Caller::run", set())
    assert callees == set()


def test_field_qualified_instance_call_still_resolves_after_static_call_fix(tmp_path):
    """既有的欄位鏈呼叫解析（`xxxRepository.foo()` 這類，欄位名稱隱含
    `this`）不能被靜態呼叫判斷搶先攔截而失效——類別名稱與欄位名稱在合法
    Java 命名慣例下不會撞名，這裡驗證兩種解析路徑可以同時正確運作，
    互不干擾。（`this.xxx.yyy()` 這種顯式 `this` 開頭的鏈式寫法是
    `_walk_and_resolve()` 另一條路徑`This` 節點處理，不是這裡測的
    `_resolve_qualifier_string()`，該路徑本身有獨立、既有、跟這次修復
    無關的未驗證假設，見 call_graph.py 3.3 節開頭註解，不在本檔案範圍。）
    """
    (tmp_path / "UserRepository.java").write_text(
        "package com.example.repository;\n"
        "public class UserRepository {\n"
        "    public String findById(Long id) { return null; }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "UserService.java").write_text(
        "package com.example.service;\n"
        "import com.example.repository.UserRepository;\n"
        "public class UserService {\n"
        "    private UserRepository userRepository;\n"
        "    public String getUser(Long id) {\n"
        "        return userRepository.findById(id);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    callees = project.call_graph.get("UserService.java::UserService::getUser", set())
    assert "UserRepository.java::UserRepository::findById" in callees
