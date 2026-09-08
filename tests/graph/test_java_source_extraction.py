"""graph/java_source_extraction.py，對應 `resolve_java_source()`／
`resolve_referenced_source()` 這次改版：`java_source`（task 自己的 Java
方法）改成讀整個檔案，不再用大括號配對切出單一方法；`referenced_source`
維持單方法抽取為主，總量超過門檻才先嘗試整份讀入、再視情況裁減。與
使用者確認過的設計方向，見對話紀錄——真實 `lang-exam-api-refactor` 專案
90 個 `.java` 檔案每個恰好一個頂層 class，讀整個檔案＝讀整個 class。
"""
import graph.java_source_extraction as jse
from graph.java_source_extraction import (
    JavaSourceExtractionError,
    resolve_java_source,
    resolve_referenced_source,
)

_EXAM_SPECIFICATION_JAVA = """package com.example.repository;
import org.springframework.data.jpa.domain.Specification;
public class ExamSpecification {
    public static Specification<Object> withYear(String year) {
        return (root, query, cb) -> cb.equal(root.get("year"), year);
    }
    public static Specification<Object> withGrade(String grade) {
        return (root, query, cb) -> cb.equal(root.get("grade"), grade);
    }
}
"""

_EXAM_SERVICE_JAVA = """package com.example.service;
public class ExamService {
    public int doA(int x) {
        return this.doB(x) + 1;
    }
    public int doB(int x) {
        return x * 2;
    }
}
"""


class TestResolveJavaSource:
    def test_returns_whole_file_content(self, tmp_path):
        (tmp_path / "ExamService.java").write_text(_EXAM_SERVICE_JAVA, encoding="utf-8")
        result = resolve_java_source(str(tmp_path), "ExamService.java::ExamService::doA")
        assert result == _EXAM_SERVICE_JAVA

    def test_sibling_method_is_visible_regardless_of_which_method_is_the_seed(self, tmp_path):
        """整個檔案都在，doA 是 seed 時 doB 的真實原始碼也看得到——不需要
        呼叫圖正確解析出 doA 呼叫 doB 這條邊，見模組 docstring。
        """
        (tmp_path / "ExamService.java").write_text(_EXAM_SERVICE_JAVA, encoding="utf-8")
        result = resolve_java_source(str(tmp_path), "ExamService.java::ExamService::doA")
        assert "public int doB(int x)" in result

    def test_class_and_method_segments_of_java_method_id_are_ignored(self, tmp_path):
        """只用 java_method_id 的第一段（檔案路徑）；class_name／
        function_name 兩段刻意不使用，即使寫錯也不影響結果——讀整個檔案
        不需要定位到特定方法。
        """
        (tmp_path / "ExamService.java").write_text(_EXAM_SERVICE_JAVA, encoding="utf-8")
        result = resolve_java_source(str(tmp_path), "ExamService.java::WrongClass::wrongMethod")
        assert result == _EXAM_SERVICE_JAVA

    def test_missing_file_raises(self, tmp_path):
        try:
            resolve_java_source(str(tmp_path), "Missing.java::X::y")
            assert False, "應該拋出 JavaSourceExtractionError"
        except JavaSourceExtractionError:
            pass


class TestResolveReferencedSource:
    def test_java_target_returns_whole_file_when_under_threshold(self, tmp_path):
        (tmp_path / "ExamSpecification.java").write_text(_EXAM_SPECIFICATION_JAVA, encoding="utf-8")
        targets = [
            {
                "file_path": "ExamSpecification.java", "class_name": "ExamSpecification",
                "function_name": "withYear", "language": "java",
            },
        ]
        items = resolve_referenced_source(str(tmp_path), str(tmp_path), targets)
        assert len(items) == 1
        assert items[0]["source"] == _EXAM_SPECIFICATION_JAVA
        assert "withGrade" in items[0]["source"]  # 整份檔案，不是只有 withYear 那段

    def test_python_target_stays_single_function_extraction(self, tmp_path):
        """language="python" 的項目不受這次改版影響，維持既有的
        `ast.get_source_segment()` 單函式精確抽取，不會因為 java 端改成
        整份讀入就跟著變成整份。
        """
        (tmp_path / "exam_repository.py").write_text(
            "class ExamRepository:\n"
            "    def find_by_card(self, card):\n"
            "        return None\n\n"
            "    def find_by_year(self, year):\n"
            "        return None\n",
            encoding="utf-8",
        )
        targets = [
            {
                "file_path": "exam_repository.py", "class_name": "ExamRepository",
                "function_name": "find_by_card", "language": "python",
            },
        ]
        items = resolve_referenced_source(str(tmp_path), str(tmp_path), targets)
        assert len(items) == 1
        assert "find_by_card" in items[0]["source"]
        assert "find_by_year" not in items[0]["source"]  # 只有目標函式，不是整個 class

    def test_missing_reference_target_is_skipped_not_fatal(self, tmp_path, caplog):
        targets = [
            {"file_path": "Missing.java", "class_name": "X", "function_name": "y", "language": "java"},
        ]
        items = resolve_referenced_source(str(tmp_path), str(tmp_path), targets)
        assert items == []

    def test_total_size_over_threshold_trims_java_items_to_single_method(self, tmp_path, monkeypatch):
        """超過門檻時，java 項目退回單方法抽取（既有的
        `extract_java_method_source()` 大括號配對機制），不是整份保留。
        """
        monkeypatch.setattr(jse, "REFERENCED_SOURCE_TRIM_THRESHOLD_BYTES", 50)
        (tmp_path / "ExamSpecification.java").write_text(_EXAM_SPECIFICATION_JAVA, encoding="utf-8")
        targets = [
            {
                "file_path": "ExamSpecification.java", "class_name": "ExamSpecification",
                "function_name": "withYear", "language": "java",
            },
        ]
        items = resolve_referenced_source(str(tmp_path), str(tmp_path), targets)
        assert len(items) == 1
        # 裁減後只剩 withYear 這個方法本身，不再包含整份檔案（withGrade 不見了）。
        assert "withYear" in items[0]["source"]
        assert "withGrade" not in items[0]["source"]
        assert "package com.example.repository" not in items[0]["source"]

    def test_python_items_not_trimmed_even_when_total_exceeds_threshold(self, tmp_path, monkeypatch):
        """裁減只套用在 java 項目——python 項目本來就是精確單函式抽取，
        沒有「整份 vs 單方法」這個裁減空間，門檻判斷不該動它。
        """
        monkeypatch.setattr(jse, "REFERENCED_SOURCE_TRIM_THRESHOLD_BYTES", 1)
        (tmp_path / "exam_repository.py").write_text(
            "class ExamRepository:\n    def find_by_card(self, card):\n        return None\n",
            encoding="utf-8",
        )
        targets = [
            {
                "file_path": "exam_repository.py", "class_name": "ExamRepository",
                "function_name": "find_by_card", "language": "python",
            },
        ]
        items = resolve_referenced_source(str(tmp_path), str(tmp_path), targets)
        assert len(items) == 1
        assert "find_by_card" in items[0]["source"]

    def test_under_threshold_multiple_targets_all_kept_whole(self, tmp_path):
        (tmp_path / "A.java").write_text(
            "package com.example;\npublic class A {\n    public void a() {}\n}\n", encoding="utf-8"
        )
        (tmp_path / "B.java").write_text(
            "package com.example;\npublic class B {\n    public void b() {}\n}\n", encoding="utf-8"
        )
        targets = [
            {"file_path": "A.java", "class_name": "A", "function_name": "a", "language": "java"},
            {"file_path": "B.java", "class_name": "B", "function_name": "b", "language": "java"},
        ]
        items = resolve_referenced_source(str(tmp_path), str(tmp_path), targets)
        assert len(items) == 2
