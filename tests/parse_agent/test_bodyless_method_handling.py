"""04a 四章「Map 摘要必要性判斷」新增的第 2 層規則：無方法本體、且找不到
實作類別的介面（典型如 Spring Data JPA Repository 衍生查詢方法）不送
LLM，改機械解析。涵蓋 call_graph 的 has_body/query_value 擷取、
grouping.needs_llm_summary()/classify_trivial_classes() 的新分支、
summarize._describe_derived_query()/_describe_bodyless_method()/
_mechanical_summary() 的機械描述邏輯。
"""
from parse_agent.call_graph import parse_java_project
from parse_agent.grouping import classify_trivial_classes, needs_llm_summary
from parse_agent.summarize import _describe_bodyless_method, _describe_derived_query, _mechanical_summary
from parse_agent.types import ClassInfo, MethodEntry


# --------------------------------------------------------------------------
# call_graph: has_body / query_value 擷取
# --------------------------------------------------------------------------


def test_interface_derived_query_method_has_no_body(tmp_path):
    (tmp_path / "QuestionRepository.java").write_text(
        """
        package com.example;
        import org.springframework.data.jpa.repository.JpaRepository;
        public interface QuestionRepository extends JpaRepository<Question, String> {
            java.util.List<Question> findByTypeAndPart(String type, String part);
        }
        """,
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    method = project.classes["QuestionRepository"].methods[0]
    assert method.name == "findByTypeAndPart"
    assert method.has_body is False
    assert method.query_value is None


def test_interface_query_annotated_method_captures_literal_value(tmp_path):
    (tmp_path / "ExamRepository.java").write_text(
        """
        package com.example;
        import org.springframework.data.jpa.repository.JpaRepository;
        import org.springframework.data.jpa.repository.Query;
        public interface ExamRepository extends JpaRepository<Exam, Long> {
            @Query("select e from Exam e where e.id = ?1")
            Exam customFind(Long id);
        }
        """,
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    method = project.classes["ExamRepository"].methods[0]
    assert method.has_body is False
    assert method.query_value == "select e from Exam e where e.id = ?1"


def test_concrete_class_method_has_body_true(tmp_path):
    (tmp_path / "UserService.java").write_text(
        """
        package com.example;
        import org.springframework.stereotype.Service;
        @Service
        public class UserService {
            public String greet(String name) {
                return "hello " + name;
            }
        }
        """,
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    method = project.classes["UserService"].methods[0]
    assert method.has_body is True
    assert method.query_value is None


# --------------------------------------------------------------------------
# grouping.needs_llm_summary() / classify_trivial_classes()
# --------------------------------------------------------------------------


def _bodyless_method(name: str) -> MethodEntry:
    return MethodEntry(name=name, return_type=None, has_body=False)


def _body_method(name: str) -> MethodEntry:
    return MethodEntry(name=name, return_type=None, has_body=True)


def test_bodyless_interface_without_implementor_skips_llm():
    repo = ClassInfo(
        file_path="QuestionRepository.java",
        class_name="QuestionRepository",
        stereotype=None,
        bean_name_override=None,
        methods=[_bodyless_method("findByTypeAndPart")],
    )
    assert needs_llm_summary(repo, has_implementor=False) is False


def test_bodyless_interface_with_implementor_still_needs_llm():
    """介面有具體實作類別時，即使自己的抽象宣告沒有本體，也不該被機械
    搶答——真正的邏輯在實作類別裡，見 needs_llm_summary() 規則 2 docstring
    「刻意排除在有具體實作類別的情況之外」。
    """
    service_iface = ClassInfo(
        file_path="ExamService.java",
        class_name="ExamService",
        stereotype=None,
        bean_name_override=None,
        methods=[_bodyless_method("calculate")],
    )
    assert needs_llm_summary(service_iface, has_implementor=True) is True


def test_mixed_body_and_bodyless_methods_still_needs_llm():
    """全部方法都沒本體才算數；只要有一個方法有本體，就不是純 Spring Data
    proxy 介面的形狀，維持送 Map。
    """
    mixed = ClassInfo(
        file_path="MixedRepository.java",
        class_name="MixedRepository",
        stereotype=None,
        bean_name_override=None,
        methods=[_bodyless_method("findByType"), _body_method("customLogic")],
    )
    assert needs_llm_summary(mixed, has_implementor=False) is True


def test_interface_with_zero_methods_and_no_implementor_skips_llm():
    """全無自訂方法（純繼承 CRUD）：all() 在空清單上天生成立，同樣視為
    trivial，見 needs_llm_summary() 規則 2 docstring。
    """
    empty_repo = ClassInfo(
        file_path="PlainRepository.java",
        class_name="PlainRepository",
        stereotype=None,
        bean_name_override=None,
        methods=[],
    )
    assert needs_llm_summary(empty_repo, has_implementor=False) is False


def test_dynamic_query_signal_overrides_bodyless_rule():
    """規則 1（Specification 動態查詢訊號）優先序高於規則 2，即使方法
    都沒本體、也沒實作類別。
    """
    dynamic = ClassInfo(
        file_path="DynamicRepository.java",
        class_name="DynamicRepository",
        stereotype=None,
        bean_name_override=None,
        methods=[_bodyless_method("findAll")],
        uses_dynamic_query_signal=True,
    )
    assert needs_llm_summary(dynamic, has_implementor=False) is True


def test_classify_trivial_classes_end_to_end(tmp_path):
    (tmp_path / "QuestionRepository.java").write_text(
        """
        package com.example;
        import org.springframework.data.jpa.repository.JpaRepository;
        public interface QuestionRepository extends JpaRepository<Question, String> {
            java.util.List<Question> findByTypeAndPart(String type, String part);
        }
        """,
        encoding="utf-8",
    )
    (tmp_path / "ExamService.java").write_text(
        """
        package com.example;
        public interface ExamService {
            String calculate(String examId);
        }
        """,
        encoding="utf-8",
    )
    (tmp_path / "ExamServiceImpl.java").write_text(
        """
        package com.example;
        import org.springframework.stereotype.Service;
        @Service
        public class ExamServiceImpl implements ExamService {
            public String calculate(String examId) {
                return "result";
            }
        }
        """,
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    trivial = classify_trivial_classes(project)
    assert "QuestionRepository" in trivial
    # ExamService 有具體實作類別（ExamServiceImpl），不該被機械搶答
    assert "ExamService" not in trivial
    assert "ExamServiceImpl" not in trivial


# --------------------------------------------------------------------------
# summarize._describe_derived_query() / _describe_bodyless_method()
# --------------------------------------------------------------------------


def test_describe_derived_query_simple_and_condition():
    description = _describe_derived_query("findByTypeAndPart")
    assert description is not None
    assert "Type" in description
    assert "Part" in description
    assert "查詢" in description


def test_describe_derived_query_three_fields():
    description = _describe_derived_query("findByKindAndTypeNumberAndPartNumber")
    assert "Kind" in description
    assert "Type Number" in description
    assert "Part Number" in description


def test_describe_derived_query_exists_verb():
    description = _describe_derived_query("existsByEmail")
    assert "判斷是否存在" in description


def test_describe_derived_query_complex_keyword_falls_back_to_loose_note():
    """含 Top/OrderBy 等複雜關鍵字時，不硬拆欄位（避免把 Top3/OrderBy
    誤當成欄位名稱），改用誠實的通用說明。
    """
    description = _describe_derived_query("findTop3ByOrderByCreatedAtDesc")
    assert description is not None
    assert "Top" in description or "複雜關鍵字" in description
    # 不該把 "OrderByCreatedAtDesc" 當成欄位名稱硬拆出來混進描述
    assert "Order By Created At Desc" not in description


def test_describe_derived_query_no_match_returns_none():
    assert _describe_derived_query("saveAll") is None
    assert _describe_derived_query("customBusinessLogic") is None


def test_describe_bodyless_method_prefers_query_annotation():
    method = MethodEntry(name="customFind", has_body=False, query_value="select e from Exam e")
    description, complexity = _describe_bodyless_method(method)
    assert "select e from Exam e" in description
    assert complexity == "low"


def test_describe_bodyless_method_falls_back_to_derived_query():
    method = MethodEntry(name="findByType", has_body=False, query_value=None)
    description, complexity = _describe_bodyless_method(method)
    assert "Type" in description
    assert complexity == "low"


def test_describe_bodyless_method_honest_placeholder_when_nothing_matches():
    method = MethodEntry(name="doSomethingCustom", has_body=False, query_value=None)
    description, complexity = _describe_bodyless_method(method)
    assert "無法" in description
    assert "人工核對" in description
    assert complexity == "medium"


# --------------------------------------------------------------------------
# summarize._mechanical_summary()
# --------------------------------------------------------------------------


def test_mechanical_summary_describes_bodyless_methods_and_skips_accessors():
    repo = ClassInfo(
        file_path="QuestionRepository.java",
        class_name="QuestionRepository",
        stereotype=None,
        bean_name_override=None,
        methods=[
            _bodyless_method("findByTypeAndPart"),
            MethodEntry(name="getId", return_type="String", has_body=False),  # 存取器樣式，即使沒本體也略過
        ],
    )
    result = _mechanical_summary(repo)
    assert len(result.methods) == 1
    assert result.methods[0].method_name == "findByTypeAndPart"
    assert "未呼叫 LLM" in result.summary or "未呼叫 Claude API" in result.summary
