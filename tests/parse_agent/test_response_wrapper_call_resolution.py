"""① 解析 Agent 對「自我回傳靜態工廠」類別（如 `ResponseResult<T>`／
`Result<T>` 的 `ok()`／`error()`／`success()`／`failure()`，或非泛型 DTO
的 `UserProfileRs.fromEntity()`）靜態方法呼叫的呼叫圖解析，對應
`call_graph.py::_is_self_returning_static_factory()`。跟
`test_static_method_call_resolution.py`（Specification 動態查詢 helper）
是同一種問題（`_resolve_qualifier_string()` 的欄位鏈解析找不到裸類別
名稱的靜態呼叫）、但不同的結構訊號，兩個檔案分開維護。

**為什麼不用類別名稱比對（`Result`／`Response` 這類字樣）**：這個真實
專案裡就有名稱剛好帶 `Result` 字樣、但其實是一般 Request／Response
DTO 的反例（`GetExamResultRq`／`GetExamResultRs`），名稱比對會誤判成
回應包裝類別。改用結構訊號——至少一個 `static` 方法宣告回傳型別是
自己，見 `test_dto_with_result_in_name_is_not_misclassified`。

**根因訂正（docs/refactor_bug_trace.md #38）**：這個訊號原本額外要求
「class 本身宣告泛型型別參數」，理由是「一般 DTO 不是泛型類別，也不會
有 static 工廠方法回傳自己」——真實案例 `UserProfileRs.fromEntity(
ExamEntity entity): UserProfileRs`（非泛型 DTO）推翻了這個假設：
`CandidateController.getCandidate()` 呼叫 `UserProfileRs.fromEntity()`
這條邊因此在呼叫圖裡完全消失，⑤ 看不到 `UserProfileRs.java` 建構子的
真實欄位對應，猜錯成 `entity.name`（真正欄位是 `entity.user_name`），
真實觸發 `AttributeError`。已對整個 Java 專案掃過全部 `public static`
方法確認：拿掉泛型限制後，只有 `UserProfileRs` 這一個類別會新命中，
`GetExamResultRq`／`GetExamResultRs` 這兩個名稱帶 `Result` 字樣的一般
DTO 完全沒有 `static` 方法，不受影響——見
`test_non_generic_entity_conversion_factory_is_detected`。

真實案例（`lang-exam-api-refactor`）：`ExamController::search()`／
`searchAnswer()` 等多個 controller 方法呼叫 `ResponseResult.ok(...)`／
`ResponseResult.error(...)`——修復前這些呼叫在呼叫圖裡完全不存在，目前
仍靠 `implement_node.py` 的 `_RESULT_FACTORY_INSTANCE_METHOD_NOTICE`
硬編碼提示文字頂著（見對話紀錄，這次修復不影響那段既有提示，屬於
雙重保險，不衝突）。
"""
from parse_agent.call_graph import parse_java_project


_RESPONSE_RESULT_JAVA = """
package com.example.common;
public class ResponseResult<T> {
    private Integer code;
    private T data;
    public static <T> ResponseResult<T> ok(T data) { return new ResponseResult<>(); }
    public static <T> ResponseResult<T> error(int code, String msg) { return new ResponseResult<>(); }
}
"""

_CONTROLLER_JAVA = """
package com.example.controller;
import com.example.common.ResponseResult;
public class ExamController {
    public ResponseResult<String> search() {
        return ResponseResult.ok("x");
    }
    public ResponseResult<String> searchFailed() {
        return ResponseResult.error(500, "boom");
    }
}
"""


def _write_project(tmp_path):
    (tmp_path / "ResponseResult.java").write_text(_RESPONSE_RESULT_JAVA, encoding="utf-8")
    (tmp_path / "ExamController.java").write_text(_CONTROLLER_JAVA, encoding="utf-8")


def test_generic_self_returning_static_factory_is_detected(tmp_path):
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))
    assert project.classes["ResponseResult"].is_self_returning_static_factory is True


def test_static_call_on_response_wrapper_is_resolved(tmp_path):
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))

    search_callees = project.call_graph.get("ExamController.java::ExamController::search", set())
    assert "ResponseResult.java::ResponseResult::ok" in search_callees

    failed_callees = project.call_graph.get("ExamController.java::ExamController::searchFailed", set())
    assert "ResponseResult.java::ResponseResult::error" in failed_callees


def test_non_generic_entity_conversion_factory_is_detected(tmp_path):
    """真實案例（docs/refactor_bug_trace.md #38）：`UserProfileRs` 是
    非泛型 DTO，`fromEntity()` 是 static 工廠方法回傳自己——訂正後的
    訊號必須命中這個案例，且呼叫端 `CandidateController.getCandidate()`
    對 `UserProfileRs.fromEntity(...)` 這條呼叫要能正確解析進呼叫圖，
    後續 reference_targets 才看得到 `UserProfileRs.java` 建構子的真實
    欄位對應內容。
    """
    (tmp_path / "UserProfileRs.java").write_text(
        "package com.example.controllerRs;\n"
        "import com.example.entity.ExamEntity;\n"
        "public class UserProfileRs {\n"
        "    private String name;\n"
        "    public UserProfileRs(ExamEntity entity) {\n"
        "        this.name = entity.getUserName();\n"
        "    }\n"
        "    public static UserProfileRs fromEntity(ExamEntity entity) {\n"
        "        return new UserProfileRs(entity);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "CandidateController.java").write_text(
        "package com.example.controller;\n"
        "import com.example.controllerRs.UserProfileRs;\n"
        "public class CandidateController {\n"
        "    public UserProfileRs getCandidate() {\n"
        "        return UserProfileRs.fromEntity(null);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))

    assert project.classes["UserProfileRs"].is_self_returning_static_factory is True
    callees = project.call_graph.get("CandidateController.java::CandidateController::getCandidate", set())
    assert "UserProfileRs.java::UserProfileRs::fromEntity" in callees


def test_dto_with_result_in_name_is_not_misclassified(tmp_path):
    """真實反例：類別名稱帶 `Result` 字樣，但是一般 Request／Response
    DTO（沒有任何 static 方法）——不能被誤判成回應包裝類別，否則靜態
    呼叫解析會放行不該放行的目標，見模組 docstring「為什麼不用類別
    名稱比對」。
    """
    (tmp_path / "GetExamResultRs.java").write_text(
        "package com.example.controllerRs;\n"
        "public class GetExamResultRs {\n"
        "    private String examId;\n"
        "    private Integer score;\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    assert project.classes["GetExamResultRs"].is_self_returning_static_factory is False


def test_generic_class_without_self_returning_static_factory_is_not_flagged(tmp_path):
    """光是泛型還不夠——沒有 static 方法回傳自己的泛型型別，就不是這個
    模式（例如一般的泛型 DTO 容器，只有 instance getter，沒有靜態工廠）。
    """
    (tmp_path / "PageResult.java").write_text(
        "package com.example.common;\n"
        "public class PageResult<T> {\n"
        "    private T item;\n"
        "    public T getItem() { return item; }\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    assert project.classes["PageResult"].is_self_returning_static_factory is False


def test_static_factory_not_returning_self_is_not_flagged(tmp_path):
    """有 static 方法，但回傳的不是自己（例如回傳另一個型別）——不該
    命中，訊號要求的是「自我」回傳，不分泛型與否。
    """
    (tmp_path / "Converter.java").write_text(
        "package com.example.common;\n"
        "public class Converter {\n"
        "    public static String describe() { return \"x\"; }\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    assert project.classes["Converter"].is_self_returning_static_factory is False
