# plan_agent/prompts.py
"""[P] Plan Agent 用到的 Claude API system prompt 與對應 output schema，
對應 06a 五章「LLM 設計階段」。schema 跟 prompt 放同一個檔案的理由，
比照 design_agent/prompts.py 的說明——調整其中一個時另一個就在旁邊，
不容易顧此失彼。

**輸出契約用 `interface_id` 字串代替三元組物件**：06a 五章要求「回應的
`(file_path, class_name, function_name)` 集合必須與輸入的 interfaces
集合完全一致」，`referenced_interfaces` 也是同一種三元組。但 `routers`
層的 `class_name` 恆為 `None`（05a 七章），若逐欄位要求 LLM 回傳，
output schema 需要處理 `class_name` 的 nullable 型別，且
`referenced_interfaces` 會變成「三元組物件陣列」而不是單純字串陣列，
複雜度沒有必要地增加。`plan_agent/module_index.py` 的 `interface_id()`
把三元組編碼成單一字串（`"file_path::class_name::function_name"`，
`class_name` 為 `None` 時用空字串），語意上完全等價，但讓這裡的 schema
全程只用 `string`／`array of string`，不需要碰 nullable。
"""
from __future__ import annotations

PLAN_SYSTEM_PROMPT = """\
你是協助把 Java 方法的業務邏輯描述，銜接到已經設計好的 Python 函式
介面（interface）的助手。你會收到一個模組（module）的資料，任務是為
這個模組的每一個 Python interface，整合出對應的 Java 業務邏輯描述、
補充 context，並判斷它在業務邏輯上會呼叫到哪些其他 interface。

輸入包含：
1. module：模組名稱與業務摘要
2. java_methods：這個模組所有 Java 方法的清單，每筆含 class_name／
   java_method／description／complexity——這是原始業務邏輯的來源
3. interfaces：這個模組所有 Python interface 的清單，每筆含
   interface_id（唯一識別碼，原樣抄回即可，不要更動任何字元）、
   file_path、class_name（routers 層為 null，代表是自由函式、不屬於
   任何類別）、function_name、params、return_type——這是你要逐一填寫
   描述的對象
4. upstream_interfaces：這個模組依賴的上游模組已經產出的 interface
   清單（同樣格式，含 interface_id），純參考用，讓你知道有哪些函式
   已經存在，可以被 referenced_interfaces 引用

你的任務：對 interfaces 陣列裡**每一個**元素，透過函式名稱轉寫關係
（Java camelCase 方法名跟 Python snake_case 函式名的對應）、參數個數、
業務描述語意，找出它對應的 Java 方法（interfaces 與 java_methods 之間
沒有現成的對照表，需要你自己配對），輸出：

1. interface_id：原樣抄回輸入的 interface_id，一個字元都不要更動
2. description：整合對應 Java 方法業務邏輯後、給後續實作者的任務
   描述——具體描述這個函式該做什麼、遵循什麼業務規則，不要只是重複
   函式簽名本身
3. context：補充依賴關係／邊界條件文字，沒有特別要補充時給空字串
   （不要省略這個欄位）
4. referenced_interfaces：這個函式業務邏輯上會呼叫到的其他 interface
   的 interface_id 清單——只能引用 interfaces 或 upstream_interfaces
   這兩份清單裡出現過的 interface_id，不要虛構或猜測不存在的
   interface_id，也不要引用自己。沒有業務關聯時給空陣列。

**interfaces 陣列裡的每一個元素都必須在你的輸出裡出現恰好一次，不能
省略、不能重複、也不要為 interfaces／upstream_interfaces 以外的東西
生成項目。**

不要輸出任何其他文字，不要用 markdown code fence 包裹。
"""

PLAN_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "interface_id": {"type": "string"},
                    "description": {"type": "string"},
                    "context": {"type": "string"},
                    "referenced_interfaces": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["interface_id", "description", "context", "referenced_interfaces"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tasks"],
    "additionalProperties": False,
}
