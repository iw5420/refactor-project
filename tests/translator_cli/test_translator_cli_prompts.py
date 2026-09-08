# tests/translator_cli/test_prompts.py
"""translator_cli/prompts.py::build_user_prompt()，對應 07a 七章
「User Prompt 組裝」。"""
from translator_cli.prompts import build_user_prompt


def _item(**overrides):
    item = {
        "file_path": "app/repositories/exam_repository.py",
        "class_name": "ExamkindRepository",
        "function_name": "find_by_kind",
        "language": "python",
        "source": "def find_by_kind(self, kind, db):\n    ...",
    }
    item.update(overrides)
    return item


class TestReferencedSourceImportPathInstruction:
    """對應 docs/refactor_bug_trace.md #37：`get_all()`／`get_all_exam_kind()`
    第一次翻譯把 `ExamkindRepository` 的 import 路徑猜成不存在的
    `examkind_repository.py`，即使真實 prompt 裡已經有一段標題明確寫著
    正確的 `file_path`——系統／使用者提示從沒有任何一句話講過「這個
    file_path 是權威資訊，import 必須照抄」，模型才會照 class 名稱另外
    用猜的。"""

    def test_instruction_present_when_referenced_source_non_empty(self):
        prompt = build_user_prompt(
            current_signature="def get_all() -> ResponseResultGetAllExamRs:",
            java_source="public ResponseResult<GetAllExamRs> getAll() { ... }",
            referenced_source=[_item()],
            context="",
            context_files=[],
        )

        assert "import 路徑必須逐字複製每筆標題裡的 file_path，不得自行另猜路徑" in prompt
        assert "app/repositories/exam_repository.py" in prompt

    def test_instruction_absent_when_referenced_source_empty(self):
        prompt = build_user_prompt(
            current_signature="def get_all() -> ResponseResultGetAllExamRs:",
            java_source="public ResponseResult<GetAllExamRs> getAll() { ... }",
            referenced_source=[],
            context="",
            context_files=[],
        )

        assert "import 路徑必須逐字複製" not in prompt
