# parse_agent/prompts.py
"""① 解析 Agent 用到的 Claude API system prompt 與對應 output schema
集中於此，對應 04a 四章「Map-Reduce 語意摘要」。schema 跟 prompt 放同一
個檔案的理由，比照 spec_collection_agent/prompts.py 的說明——調整其中
一個時另一個就在旁邊，不容易顧此失彼。
"""
from __future__ import annotations

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
