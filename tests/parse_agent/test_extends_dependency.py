"""① 解析 Agent 對 Java `extends`（class 繼承）的依賴邊擷取，對應
docs/09b_bug_trace.md「新發現：@MappedSuperclass（如 BaseEntity）未被
任何 module 的 java_files 收錄」：`parse_agent/grouping.py::_direct_deps()`
原本只認欄位型別依賴與 import 依賴，完全沒有管道知道「這個 class 繼承了
哪個父類別」。真實案例：JPA entity 常見把 `@Id` 標在共用的
`@MappedSuperclass`（如 `BaseEntity`）上，具體 entity `extends BaseEntity`
——這個繼承關係在同一個 package 內，Java 語言特性上不需要 import 陳述式，
`_extract_project_imports()` 找不到這條邊；`extends` 資訊本身以前也完全
沒被 `ClassInfo` 記錄，導致 `BaseEntity.java` 永遠進不了任何 module 的
`java_files`，連鎖造成④ `build_db_models()` 判定這批 entity「主鍵不明確」
而整批跳過。
"""
from parse_agent.call_graph import parse_java_project
from parse_agent.grouping import classify_trivial_classes, controller_dependency_closure


_BASE_ENTITY_JAVA = """
package com.example.entity;
import javax.persistence.*;
@MappedSuperclass
public abstract class BaseEntity {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
}
"""

_EXAM_ENTITY_JAVA = """
package com.example.entity;
import javax.persistence.Entity;
import javax.persistence.Table;
@Entity
@Table(name = "exam")
public class ExamEntity extends BaseEntity {
    private String name;
}
"""

_EXAM_REPOSITORY_JAVA = """
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
        return "ok";
    }
}
"""


def _write_project(tmp_path):
    (tmp_path / "BaseEntity.java").write_text(_BASE_ENTITY_JAVA, encoding="utf-8")
    (tmp_path / "ExamEntity.java").write_text(_EXAM_ENTITY_JAVA, encoding="utf-8")
    (tmp_path / "ExamRepository.java").write_text(_EXAM_REPOSITORY_JAVA, encoding="utf-8")
    (tmp_path / "ExamController.java").write_text(_CONTROLLER_JAVA, encoding="utf-8")


def test_extends_recorded_on_class_info(tmp_path):
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))

    assert project.classes["ExamEntity"].extends == "BaseEntity"
    assert project.classes["BaseEntity"].extends is None


def test_same_package_superclass_without_any_import_enters_dependency_closure(tmp_path):
    """對應真實案例：ExamEntity 跟 BaseEntity 同一個 package，Java 語法
    上完全不需要 import 陳述式就能 extends——修復前這條邊連候選節點都
    不存在，_direct_deps() 只認欄位型別依賴與 import 依賴。
    """
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))

    assert "BaseEntity" not in project.classes["ExamEntity"].imports

    closure = controller_dependency_closure(project)
    assert "ExamEntity" in closure["ExamController"]
    assert "BaseEntity" in closure["ExamController"]


def test_mapped_superclass_classified_as_trivial_no_llm_summary_needed(tmp_path):
    _write_project(tmp_path)
    project = parse_java_project(str(tmp_path))

    trivial = classify_trivial_classes(project)
    assert "BaseEntity" in trivial


def test_extends_target_not_in_project_is_ignored(tmp_path):
    """extends 一個專案外的類別（如標準函式庫、未掃描到範圍，這裡用
    簡短名稱模擬——完整 dotted path 寫法有既有的獨立已知限制，見
    call_graph.py implements docstring「FQN 寫法時 .name 只抓第一段」，
    extends 用的是同一個 javalang 節點形狀，不是這裡要測的行為）時，
    resolve 不到就不加邊，不拋錯——比照既有 import 依賴邊「查無此類別
    就不算依賴」的既有精神。"""
    (tmp_path / "Standalone.java").write_text(
        "package com.example;\npublic class Standalone extends AbstractBase {\n}\n",
        encoding="utf-8",
    )
    project = parse_java_project(str(tmp_path))
    assert project.classes["Standalone"].extends == "AbstractBase"
    # 沒有拋錯、也沒有 project.classes["AbstractBase"]，_direct_deps() 內部
    # 的 in project.classes 檢查會讓這條邊被安靜忽略。
    assert "AbstractBase" not in project.classes
