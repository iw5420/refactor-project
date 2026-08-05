# parse_agent/prompts.py
"""① 解析 Agent 用到的 Claude API system prompt 與對應 output schema
集中於此，對應 04a 四章「Map-Reduce 語意摘要」。schema 跟 prompt 放同一
個檔案的理由，比照 spec_collection_agent/prompts.py 的說明——調整其中
一個時另一個就在旁邊，不容易顧此失彼。
"""
from __future__ import annotations

import copy

# 對應 grouping.py 產出的 MapUnit（4a 共用類別批次、4b Controller 批次
# 共用同一份 prompt，兩者的差異只在輸入 payload 的內容組成，不在 prompt
# 文字本身——見 04a 四章「Map 階段分兩個子階段，先共用、後專屬」）。
MAP_SYSTEM_PROMPT = """\
你是協助理解 Java（Spring Boot）專案業務邏輯、為 Java → Python 重構做
準備的助手。你會收到一批 Java class 的完整原始碼，任務是幫每個 class
產出摘要，供後續彙整跨 class 邊界的模組拆分判斷使用。

輸入可能包含 `known_shared_class_summaries`：這是其他已經分析過的共用
class 的簡短摘要（不是原始碼），純粹讓你在描述「這個 class 呼叫了什麼、
做什麼用」時有上下文可以參照，不需要你重新分析那些 class。

對輸入的每一個 class，輸出：

1. class_name：原樣抄輸入的 class 名稱，不要更動
2. summary：這個 class 對外暴露的業務行為是什麼、負責什麼，一段文字
3. methods：每個 public 方法一筆，包含：
   - method_name：原樣抄方法名稱
   - description：這個方法做什麼、輸入輸出的業務意義
   - complexity："low"（單純轉發/查詢/CRUD）、"medium"（有條件判斷或
     多步驟業務規則）、"high"（複雜計算、跨多個資料來源整合、大量分支）
     三選一
4. cross_group_dependency_hints：這個 class「看起來」跟哪些不在這批輸入
   裡的其他 class 有業務關聯（不確定是否精確，只是候選線索，交給後續
   彙整階段做最終判斷）——列出你觀察到的 class 名稱即可，不需要解釋

只回傳一個 JSON object，不要輸出任何其他文字，不要用 markdown code fence
包裹。
"""

MAP_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "classes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "class_name": {"type": "string"},
                    "summary": {"type": "string"},
                    "methods": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "method_name": {"type": "string"},
                                "description": {"type": "string"},
                                "complexity": {"type": "string", "enum": ["low", "medium", "high"]},
                            },
                            "required": ["method_name", "description", "complexity"],
                            "additionalProperties": False,
                        },
                    },
                    "cross_group_dependency_hints": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["class_name", "summary", "methods", "cross_group_dependency_hints"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classes"],
    "additionalProperties": False,
}

# 對應 04a 四章 Reduce 階段。輸入見 summarize.py `_reduce_phase()` 組的
# payload：所有 Map 批次的 classes 彙整結果 + controller_dependencies
# （程式算好的精確事實）。
REDUCE_SYSTEM_PROMPT = """\
你是協助把 Java 專案拆分成 Python 重構模組的助手。你會收到：

1. classes：一批 Java class 的摘要（來自前一階段的分析），每個 class
   包含業務摘要、每個方法的描述與複雜度、以及「看起來」跟哪些其他 class
   有關聯的線索
2. controller_dependencies：每個 Controller class 實際依賴哪些 class
   （程式碼精確分析得出的事實，不是推測，可以直接信任）

任務：決定最終的模組（module）拆分——哪些 class 應該歸在同一個模組。
一個模組通常對應一個或多個 Controller 以及它們專屬依賴的
repository/service class；被多個 Controller 共用的 class 依業務關聯
判斷歸入最相關的模組（即使被其他模組依賴，也只屬於一個模組，其他模組
透過 depends_on 表示跨模組依賴，不重複歸屬）。

對每個模組輸出：

1. module：模組名稱，snake_case，簡短有語意（如 "user"、"order"）
2. summary：重新彙整這個模組內所有 class 的摘要，寫成模組層級的業務
   摘要——不要只是把各 class 的摘要接起來，要重新歸納「這個模組整體
   對外暴露的業務行為」「為什麼這些 class 被歸在一起」「依賴其他模組
   的業務原因」
3. java_classes：這個模組包含哪些 class（原樣抄 class_name）
4. depends_on：這個模組依賴哪些其他模組（填 module 名稱，不是 class
   名稱）

**java_classes 只能是輸入 classes 陣列裡出現過的 class_name，禁止填入
任何沒出現過的名稱**：輸入的 classes 陣列就是這次要分類的完整 class
清單，不是範例或摘要。即使你從某個方法描述聯想到「這個功能應該會用到
一個 CreateExamRequest 或 LoginResponse 這類 DTO class」，只要這個名稱
沒有原樣出現在輸入的 classes 陣列裡，就絕對不能把它填進 java_classes
——這種 DTO/Request/Response class 通常沒有業務邏輯，本來就不會出現在
你收到的 classes 清單裡，這是預期內的正常情況，不代表你需要幫忙補上。
填入清單外的名稱不會產生任何效果，只會被直接丟棄，純粹浪費你的輸出。

不要輸出任何其他文字，不要用 markdown code fence 包裹。
"""

REDUCE_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "modules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "module": {"type": "string"},
                    "summary": {"type": "string"},
                    "java_classes": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["module", "summary", "java_classes", "depends_on"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["modules"],
    "additionalProperties": False,
}


def build_reduce_output_schema(valid_class_names: list[str]) -> dict:
    """回傳 `REDUCE_OUTPUT_SCHEMA` 的動態版本：`java_classes` 陣列的每個
    元素加上 `enum: valid_class_names` 約束，`valid_class_names` 是呼叫端
    傳入的、這次 Map 階段實際摘要過的 class_name 完整集合（封閉集合，
    Reduce 呼叫前就已經確定，不是猜測）。

    **為什麼用 schema 約束、不只靠 prompt 指令**：REDUCE_SYSTEM_PROMPT
    已經有「java_classes 只能是輸入 classes 陣列裡出現過的名稱，禁止
    杜撰」的文字指令，但這只能降低模型虛構 class 名稱的機率，降不到 0
    ——模型看到方法描述提到「建立考試」，語意上很容易聯想到一個
    `CreateExamRequest`／`CreateExamRq` 這類 DTO 應該存在，即使明確被
    告知不要這樣做。既然合法的 class 名稱集合在呼叫前就已經是確定的
    封閉集合，這正是 00 二章「能用程式判斷的，就不要交給 LLM」的情況：
    用 Structured Outputs 的 `enum` 讓 API 在生成階段就不可能輸出集合外
    的字串，不是「生成後再靠程式碼濾掉」（`_assemble_module_drafts()`
    的 `missing_classes` 檢查會繼續留著，當作 defense-in-depth，不因為
    這裡加了 enum 約束就拿掉）。`enum` 只限制這個欄位「填的字串必須是
    這些之一」，不影響模型判斷要怎麼分組、哪些 class 該歸同一個模組，
    Reduce 階段原本的判斷空間完全不受影響。

    `depends_on` 刻意不做同樣的 enum 約束：`depends_on` 引用的是這次
    回應**自己產出**的 module 名稱，屬於自我參照，呼叫前不存在一個
    「合法 module 名稱」的封閉集合可以拿來約束（`module` 名稱本身也是
    這次回應才決定的），這個欄位仍然只能依賴 `_assemble_module_drafts()`
    既有的事後驗證（見 04a 四章「depends_on 引用不存在的 module 名稱」）。
    """
    schema = copy.deepcopy(REDUCE_OUTPUT_SCHEMA)  # 淺拷貝不夠，這裡巢狀結構要整份複製，避免動到共用的模組常數
    schema["properties"]["modules"]["items"]["properties"]["java_classes"]["items"] = {
        "type": "string",
        "enum": valid_class_names,
    }
    return schema
