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
            {"task_id": "t1", "diagnosis": "缺 score 欄位", "fixed_body": "補上 score", "file_fixes": []},
        ],
        "unfixable_reasons": [],
    }
    jsonschema.validate(instance, DEBUG_OUTPUT_SCHEMA)


class TestKnownFillFailuresMandatoryRewrite:
    """對應 docs/09b_bug_trace.md：⑤ 本地模型完全失敗（fill_failed）的
    task 一旦進了除錯迴圈，⑦ 必須直接寫出完整函式本體，不能讓它繼續被
    排程回本地模型重試——這條規則之前只存在於使用者先前的口頭要求，沒
    有被準確寫進 10a/10b 或 prompt 本身，這裡鎖住 prompt 文字確實要求
    了這件事，不再只是「有機會就做」的選擇性行為。"""

    def test_prompt_mandates_rewriting_every_known_fill_failure(self):
        assert "known_fill_failures" in DEBUG_SYSTEM_PROMPT
        assert "一律要求" in DEBUG_SYSTEM_PROMPT
        assert "不是選擇性的" in DEBUG_SYSTEM_PROMPT

    def test_prompt_instructs_ignoring_existing_broken_content(self):
        """跟一般 fixed_body（修正既有 bug）不同：fill_failed 的
        source_files 內容可能是殘缺骨架或半成品，prompt 必須明講「不要
        在裡面找 bug，直接從 description 重寫」，避免 LLM 誤以為要
        「修正」一份其實不可信的程式碼。"""
        assert "抓 bug" in DEBUG_SYSTEM_PROMPT
        assert "從零" in DEBUG_SYSTEM_PROMPT

    def test_prompt_still_allows_unfixable_when_description_insufficient(self):
        """不是無腦要求一定要生出東西——description 本身不夠判斷時，仍
        然允許誠實回報 unfixable，不要求用猜的邏輯硬湊一個編造的實作。"""
        assert "連猜都猜不出合理實作" in DEBUG_SYSTEM_PROMPT


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
