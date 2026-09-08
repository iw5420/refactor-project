"""① 解析 Agent 對 Spring Data 繼承方法呼叫（如 `examRepository.findAll()`）
的呼叫圖合成，對應 docs/refactor_bug_trace.md #16／#21：`parse_agent/
call_graph.py::_yield_call()` 對這類呼叫的處理，沒有任何既有測試檔案
覆蓋過（先前只靠對真實 lang-exam-api-refactor 專案手動執行腳本驗證，見
#16／#21 記錄），這裡補上永久回歸測試。
"""
from parse_agent.call_graph import parse_java_project
from parse_agent.types import method_id
from common.jpa_base_repository import synthetic_java_method_id

_EXAM_ENTITY_JAVA = """
package com.example.entity;
import javax.persistence.Entity;
@Entity
public class ExamEntity {
    private String card;
}
"""

_EXAM_REPOSITORY_WITH_OTHER_METHODS_JAVA = """
package com.example.repository;
import com.example.entity.ExamEntity;
import org.springframework.data.jpa.repository.JpaRepository;
public interface ExamRepository extends JpaRepository<ExamEntity, Long> {
    ExamEntity findByCard(String card);
}
"""

_EXAM_REPOSITORY_NO_OTHER_METHODS_JAVA = """
package com.example.repository;
import com.example.entity.ExamEntity;
import org.springframework.data.jpa.repository.JpaRepository;
public interface ExamRepository extends JpaRepository<ExamEntity, Long> {
}
"""

_CONTROLLER_JAVA = """
package com.example.controller;
import com.example.repository.ExamRepository;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.beans.factory.annotation.Autowired;
@RestController
public class ExamController {
    @Autowired
    private ExamRepository examRepository;

    @GetMapping("/exam")
    public String get() {
        return examRepository.findAll().toString();
    }
}
"""


def _write_project(tmp_path, exam_repository_java: str):
    (tmp_path / "ExamEntity.java").write_text(_EXAM_ENTITY_JAVA, encoding="utf-8")
    (tmp_path / "ExamRepository.java").write_text(exam_repository_java, encoding="utf-8")
    (tmp_path / "ExamController.java").write_text(_CONTROLLER_JAVA, encoding="utf-8")


def test_inherited_method_call_yields_synthetic_id(tmp_path):
    """對應 #16：`examRepository.findAll()` 呼叫 Java 原始碼從未顯式宣告
    的繼承方法，即使 `ExamRepository` 還有其他顯式方法，呼叫圖仍要記到
    共用的合成座標（指向 Python `BaseRepository.find_all`），呼叫不能
    從呼叫圖裡消失。
    """
    _write_project(tmp_path, _EXAM_REPOSITORY_WITH_OTHER_METHODS_JAVA)
    project = parse_java_project(str(tmp_path))

    caller = method_id("ExamController.java", "ExamController", "get")
    callees = project.call_graph[caller]
    assert synthetic_java_method_id("findAll") in callees


def test_inherited_method_call_also_yields_sibling_method_reference_when_class_has_other_methods(tmp_path):
    """對應 #21：`ExamRepository` 除了這次呼叫的純繼承方法（`findAll`）
    之外還有其他顯式方法（`findByCard`）時，額外多帶一個指向 `findByCard`
    的參考——這個參考會被翻譯成真正的 java_index 項目（指向 `ExamRepository`
    實際定義的檔案），讓 [P] 六章 reference_targets 順便帶出「這個具體
    子類別定義在哪個檔案」，不讓⑤只看得到抽象基底 `BaseRepository` 而
    瞎猜檔名慣例（真實案例：猜成不存在的 `app.repositories.exam_repository`）。
    """
    _write_project(tmp_path, _EXAM_REPOSITORY_WITH_OTHER_METHODS_JAVA)
    project = parse_java_project(str(tmp_path))

    caller = method_id("ExamController.java", "ExamController", "get")
    callees = project.call_graph[caller]
    assert method_id("ExamRepository.java", "ExamRepository", "findByCard") in callees


def test_inherited_method_call_on_pure_orphan_repository_does_not_yield_sibling_reference(tmp_path):
    """對應 #21 docstring「`cls.methods` 全空時沒有東西可以 piggyback」：
    `ExamRepository` 完全沒有任何顯式方法時，只 yield 合成座標，不能
    平白多出一個不存在的 sibling 方法參考——這種純孤兒類別的情況交給
    `design_agent/design.py::_synthesize_inherited_repository_reads()`
    合成出「真正的」InterfaceSpec／java_method_id，不是這裡的職責。
    """
    _write_project(tmp_path, _EXAM_REPOSITORY_NO_OTHER_METHODS_JAVA)
    project = parse_java_project(str(tmp_path))

    caller = method_id("ExamController.java", "ExamController", "get")
    callees = project.call_graph[caller]
    assert synthetic_java_method_id("findAll") in callees
    assert len(callees) == 1
