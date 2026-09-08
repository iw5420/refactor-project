"""DEBUG_OUTPUT_SCHEMA 本身是合法 JSON Schema（比照既有 Agent 慣例）。"""
import jsonschema

from debug_agent.prompts import DEBUG_OUTPUT_SCHEMA, DEBUG_SYSTEM_PROMPT


def test_schema_is_valid_json_schema():
    jsonschema.Draft7Validator.check_schema(DEBUG_OUTPUT_SCHEMA)


def test_valid_response_passes():
    instance = {
        "root_cause_summary": "缺必填欄位",
        "fixable": True,
        "task_fixes": [
            {
                "task_id": "t1", "diagnosis": "缺 score 欄位", "retranslate": False,
                "fixed_body": "補上 score", "file_fixes": [],
            },
        ],
        "unfixable_reasons": [],
    }
    jsonschema.validate(instance, DEBUG_OUTPUT_SCHEMA)


def test_valid_response_with_retranslate_passes():
    instance = {
        "root_cause_summary": "從沒成功翻譯過",
        "fixable": True,
        "task_fixes": [
            {
                "task_id": "t1", "diagnosis": "known_fill_failure，需要重新翻譯", "retranslate": True,
                "fixed_body": None, "file_fixes": [],
            },
        ],
        "unfixable_reasons": [],
    }
    jsonschema.validate(instance, DEBUG_OUTPUT_SCHEMA)


class TestKnownFillFailuresMandatoryRetranslate:
    """對應 docs/refactor_bug_trace.md #9：⑤ 本地模型完全失敗
    （fill_failed）的 task 一旦進了除錯迴圈，⑦ 必須標記 retranslate=true
    交還給⑤重新翻譯，**不能自己**根據 description（06a 已判定「對模型
    沒有任何有效信號」的欄位）編一份 fixed_body——⑦ 沒有這些函式對應的
    真實 Java 原始碼可以核對，自己猜寫的可靠度天生比不上⑤。"""

    def test_prompt_mandates_retranslate_for_every_known_fill_failure(self):
        assert "known_fill_failures" in DEBUG_SYSTEM_PROMPT
        assert "一律要求" in DEBUG_SYSTEM_PROMPT
        assert "retranslate=true" in DEBUG_SYSTEM_PROMPT

    def test_prompt_instructs_not_fabricating_fixed_body_from_description(self):
        """跟一般 fixed_body（修正既有 bug）不同：fill_failed 的
        source_files 內容可能是殘缺骨架或半成品，prompt 必須明講「不要
        在裡面找 bug、也不要自己根據 description 編一份 fixed_body」，
        改用 retranslate 交給有真實 Java 原始碼可核對的⑤處理。"""
        assert "抓 bug" in DEBUG_SYSTEM_PROMPT
        assert "不要自己根據" in DEBUG_SYSTEM_PROMPT
        assert "沒有任何" in DEBUG_SYSTEM_PROMPT

    def test_prompt_still_allows_unfixable_when_truly_undecidable(self):
        """不是無腦要求一定要生出 task_fix——這個 task_id 因為其他理由
        （例如同時也在 known_scaffold_gaps 裡）判斷完全無法修時，仍然
        允許誠實回報 unfixable。"""
        assert "unfixable_reasons" in DEBUG_SYSTEM_PROMPT
        assert "判斷完全無法修" in DEBUG_SYSTEM_PROMPT


class TestRetranslatePreferredOverFixedBody:
    """對應 docs/refactor_bug_trace.md #9：函式本體邏輯有問題時（不論是
    從沒翻譯過，還是翻過但邏輯錯），prompt 必須明確引導模型優先選
    retranslate，把 fixed_body 限縮成「不需要核對 Java 原始碼就能確定
    對錯」的極少數情況——這是修正 ⑦ 自己缺乏 Java 原始碼、只能憑猜測
    寫程式碼這個根本問題的核心措辭，不能只加欄位不改引導文字。"""

    def test_prompt_frames_retranslate_as_default_for_body_logic_issues(self):
        assert "預設做法" in DEBUG_SYSTEM_PROMPT
        assert "這個函式對應的真實 Java 原始碼可以核對" in DEBUG_SYSTEM_PROMPT

    def test_prompt_frames_fixed_body_as_last_resort(self):
        assert "只在你能百分之百確定" in DEBUG_SYSTEM_PROMPT
        assert "不要因為手上剛好看得到 source_files 就順手自己" in DEBUG_SYSTEM_PROMPT

    def test_prompt_forbids_setting_both_retranslate_and_fixed_body(self):
        assert "`true`、`fixed_body` 設 `null`" in DEBUG_SYSTEM_PROMPT


class TestFileFixesFullReplacementForbidsRedundantFixedBody:
    """對應 docs/09b_bug_trace.md #52：真實環境重跑撞到 ⑦ 對同一個 task
    同時輸出 file_fixes（已經完整替換函式簽名＋本體）與 fixed_body（又
    塞一份幾乎一樣的內容），splice_body() 把 fixed_body 當成陳述式插進
    「file_fixes 剛換好的新函式」裡面，寫出巢狀死程式碼——prompt 原本
    「兩者不衝突，同時也要填」的措辭沒有涵蓋這種情況，這裡鎖住新增的
    例外文字確實存在。"""

    def test_prompt_forbids_fixed_body_when_file_fixes_already_replaces_signature(self):
        assert "函式（簽名＋本體）的完整替換" in DEBUG_SYSTEM_PROMPT
        assert "必須設為" in DEBUG_SYSTEM_PROMPT

    def test_prompt_explains_the_nested_dead_code_consequence(self):
        assert "巢狀函式定義" in DEBUG_SYSTEM_PROMPT
        assert "#52" in DEBUG_SYSTEM_PROMPT


class TestResponseNotJsonPointsAtGlobalExceptionHandler:
    """對應 docs/09b_bug_trace.md：真實環境重跑證實，`response_not_json`
    這種失敗類型幾乎全是 app/core/exception_handlers.py 呼叫
    ResponseResult／Result 時關鍵字參數名稱猜錯（如 message= 而非
    msg=）造成的，但過去 prompt 完全沒有把「這個 failure_type 該優先
    懷疑全域例外處理器」這件事講出來——即使該檔案的原始碼一直都在
    source_files 裡（_global 保留模組無條件併入），⑦ 逐模組分析時也
    只會在自己模組的程式碼裡找答案，永遠沒機會聯想到共用檔案。用真實
    Claude API 對照實驗驗證過：同一份 payload，加這句提示前 ⑦ 完全不會
    提到 exception_handlers.py，加了之後能正確找出 msg／message 的
    參數名稱錯誤，並且不影響它同時找到模組自己真正的 bug（見對照實驗
    debug_fix_verify_result3.json／result4.json）。"""

    def test_prompt_names_the_exception_handlers_file(self):
        assert "response_not_json" in DEBUG_SYSTEM_PROMPT
        assert "app/core/exception_handlers.py" in DEBUG_SYSTEM_PROMPT

    def test_prompt_instructs_cross_checking_keyword_arguments_against_real_signature(self):
        assert "關鍵字參數名稱" in DEBUG_SYSTEM_PROMPT
        assert "真實簽名" in DEBUG_SYSTEM_PROMPT

    def test_prompt_allows_file_fixes_to_target_files_outside_own_module(self):
        assert "即使" in DEBUG_SYSTEM_PROMPT and "不是這個模組自己的檔案" in DEBUG_SYSTEM_PROMPT


def test_extra_property_rejected():
    instance = {
        "root_cause_summary": "x", "fixable": False, "task_fixes": [], "unfixable_reasons": [],
        "unexpected_extra_key": "x",
    }
    try:
        jsonschema.validate(instance, DEBUG_OUTPUT_SCHEMA)
        assert False, "additionalProperties=False 應該拒絕多餘欄位"
    except jsonschema.ValidationError:
        pass
