"""① 解析 Agent 對顯式 `this.` 開頭的欄位鏈式呼叫（`this.欄位.方法()`）
的呼叫圖解析，對應 `call_graph.py::_continue_chain()` 修復。

**根因**：javalang 對 `this.a.b().c()` 這類鏈式呼叫，把整條鏈攤平成
`This.selectors` 同一層清單（`this.userRepository.findById(id)` 是
`[MemberReference(member="userRepository"), MethodInvocation(member=
"findById")]` 兩個同層元素），不是巢狀在前一個元素自己的 `.selectors`
屬性裡（每個元素自己的 `.selectors` 在這個情境下實測永遠是 `None`，見
本檔案 `test_javalang_selectors_are_flat_not_nested`）。修復前的
`_continue_chain()` 把新算出的 `context` 只傳進對 `sel.selectors`
（永遠是 `None`）的遞迴呼叫，等於每次都在原地丟棄剛算出的 context，
外層 `for` 迴圈前進到下一個清單元素時用的還是舊 context——`this.欄位.
方法()` 這種寫法因此永遠解析失敗。拿掉 `this.` 直接寫
`欄位.方法()`（更常見的寫法，走 `_resolve_qualifier_string()`，不經過
`_continue_chain()`）反而正確，兩者行為不一致正是這個 bug 的訊號。
"""
import javalang

from parse_agent.call_graph import parse_java_project


def test_javalang_selectors_are_flat_not_nested():
    """釘死修法所依賴的 javalang 行為假設本身（不是測我們的程式碼，是
    記錄第三方函式庫的實際輸出形狀，避免未來 javalang 版本升級後這個
    假設默默失效卻沒人發現）。"""
    tree = javalang.parse.parse(
        "package com.example;\n"
        "public class X {\n"
        "    public void m() { this.userRepository.findById(id); }\n"
        "}\n"
    )
    this_node = next(n for _, n in tree if isinstance(n, javalang.tree.This))
    assert len(this_node.selectors) == 2
    member_ref, method_invocation = this_node.selectors
    assert isinstance(member_ref, javalang.tree.MemberReference)
    assert member_ref.member == "userRepository"
    assert member_ref.selectors is None  # 攤平：不是巢狀在這裡
    assert isinstance(method_invocation, javalang.tree.MethodInvocation)
    assert method_invocation.member == "findById"


def _write_project(tmp_path):
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
        "        return this.userRepository.findById(id);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )


def test_explicit_this_field_chain_call_is_resolved(tmp_path):
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))
    callees = project.call_graph.get("UserService.java::UserService::getUser", set())
    assert "UserRepository.java::UserRepository::findById" in callees


def test_this_prefixed_and_bare_field_call_now_behave_the_same(tmp_path):
    """拿掉 `this.` 的等價寫法本來就正確（走 `_resolve_qualifier_string()`
    這條不同的路徑）——這裡驗證兩種寫法現在解析出同一個結果，不是碰巧
    改對了一種、漏了另一種。"""
    (tmp_path / "UserRepository.java").write_text(
        "package com.example.repository;\n"
        "public class UserRepository {\n"
        "    public String findById(Long id) { return null; }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "ServiceWithThis.java").write_text(
        "package com.example.service;\n"
        "import com.example.repository.UserRepository;\n"
        "public class ServiceWithThis {\n"
        "    private UserRepository userRepository;\n"
        "    public String getUser(Long id) { return this.userRepository.findById(id); }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "ServiceWithoutThis.java").write_text(
        "package com.example.service;\n"
        "import com.example.repository.UserRepository;\n"
        "public class ServiceWithoutThis {\n"
        "    private UserRepository userRepository;\n"
        "    public String getUser(Long id) { return userRepository.findById(id); }\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    with_this = project.call_graph.get("ServiceWithThis.java::ServiceWithThis::getUser", set())
    without_this = project.call_graph.get("ServiceWithoutThis.java::ServiceWithoutThis::getUser", set())
    assert with_this == without_this == {"UserRepository.java::UserRepository::findById"}


def test_deeper_this_chain_with_multiple_method_calls_is_resolved(tmp_path):
    """`this.a().b()` 這種鏈式方法呼叫（不只欄位+方法，是方法+方法），
    context 要在每一段呼叫之間正確傳遞，不是只有第一段可以，第二段開始
    就斷掉。"""
    (tmp_path / "Query.java").write_text(
        "package com.example;\n"
        "public class Query {\n"
        "    public Query filter(String x) { return this; }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "Builder.java").write_text(
        "package com.example;\n"
        "public class Builder {\n"
        "    public Query newQuery() { return null; }\n"
        "    public void run() {\n"
        "        this.newQuery().filter(\"x\");\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    callees = project.call_graph.get("Builder.java::Builder::run", set())
    assert "Builder.java::Builder::newQuery" in callees
    assert "Query.java::Query::filter" in callees


def test_this_chain_target_field_type_not_in_project_stops_cleanly(tmp_path):
    """欄位型別不在專案掃描範圍內（外部函式庫型別）時，鏈接下來的呼叫
    解析不到，比照既有「查不到就不算依賴、不拋錯」的一貫精神，安靜停止
    而不是拋例外。"""
    (tmp_path / "Caller.java").write_text(
        "package com.example;\n"
        "import java.util.List;\n"
        "public class Caller {\n"
        "    private List<String> items;\n"
        "    public void run() {\n"
        "        this.items.clear();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    assert project.call_graph.get("Caller.java::Caller::run", set()) == set()
