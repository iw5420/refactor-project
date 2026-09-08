"""common/jpa_base_repository.py：Spring Data JpaRepository 對應的
Python 泛型基底類別，共用常數與偵測函式，對應 docs/refactor_bug_trace.md
#10／#16。
"""
import ast

import javalang

from common.jpa_base_repository import (
    BASE_REPOSITORY_CONTENT,
    JPA_BASE_METHOD_NAME_MAP,
    detect_jpa_base_entity,
    synthetic_java_method_id,
)


def _parse_extends(java_snippet: str):
    tree = javalang.parse.parse(java_snippet)
    decl = tree.types[0]
    return decl.extends


class TestDetectJpaBaseEntity:
    def test_single_jpa_repository_extends(self):
        extends = _parse_extends(
            "package com.example;\n"
            "import org.springframework.data.jpa.repository.JpaRepository;\n"
            "public interface ExamRepository extends JpaRepository<ExamEntity, String> {}\n"
        )
        assert detect_jpa_base_entity(extends) == "ExamEntity"

    def test_multiple_extends_only_one_is_jpa_base(self):
        # 真實案例：ExamRepository extends JpaRepository<ExamEntity, String>,
        # JpaSpecificationExecutor<ExamEntity> ——只要其中一個是已知基底
        # 介面就命中，不要求全部都是。
        extends = _parse_extends(
            "package com.example;\n"
            "public interface ExamRepository extends "
            "JpaRepository<ExamEntity, String>, JpaSpecificationExecutor<ExamEntity> {}\n"
        )
        assert detect_jpa_base_entity(extends) == "ExamEntity"

    def test_crud_repository_also_recognized(self):
        extends = _parse_extends(
            "package com.example;\n"
            "public interface UserRepository extends CrudRepository<UserEntity, Long> {}\n"
        )
        assert detect_jpa_base_entity(extends) == "UserEntity"

    def test_non_jpa_interface_returns_none(self):
        extends = _parse_extends(
            "package com.example;\n"
            "public interface Runnable2 extends Runnable {}\n"
        )
        assert detect_jpa_base_entity(extends) is None

    def test_no_extends_returns_none(self):
        assert detect_jpa_base_entity(None) is None
        assert detect_jpa_base_entity([]) is None


class TestSyntheticJavaMethodId:
    def test_format(self):
        result = synthetic_java_method_id("findAll")
        assert result == "__jpa_base_repository__::BaseRepository::findAll"

    def test_all_known_methods_produce_distinct_ids(self):
        ids = {synthetic_java_method_id(name) for name in JPA_BASE_METHOD_NAME_MAP}
        assert len(ids) == len(JPA_BASE_METHOD_NAME_MAP)


class TestBaseRepositoryContent:
    def test_content_is_valid_python(self):
        ast.parse(BASE_REPOSITORY_CONTENT)

    def test_content_defines_all_mapped_python_methods(self):
        tree = ast.parse(BASE_REPOSITORY_CONTENT)
        class_def = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BaseRepository")
        defined = {n.name for n in class_def.body if isinstance(n, ast.FunctionDef)}
        assert defined == set(JPA_BASE_METHOD_NAME_MAP.values())
