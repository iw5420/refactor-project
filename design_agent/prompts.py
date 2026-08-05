# design_agent/prompts.py
"""③ 架構設計 Agent 用到的 Claude API system prompt 與對應 output
schema，對應 05a 六章「LLM 設計階段」。schema 跟 prompt 放同一個檔案的
理由，比照 parse_agent/prompts.py 的說明——調整其中一個時另一個就在
旁邊，不容易顧此失彼。

**輸出契約刻意只問「機械規則判斷不了的部分」，不要求 LLM 吐出完整
InterfaceSpec**：05a 三／五／七章已經把絕大部分 InterfaceSpec 欄位定成
機械規則（層級、檔名、私有方法命名、Java→Python 型別對應、API 邊界
方法的 openapi 覆寫），真正需要判斷的只剩三件事：(1) 無 stereotype
類別該歸哪一層、(2) 找不到 openapi 對應的參數該怎麼翻譯成 FastAPI
寫法、(3) repository／service 方法要不要加 `db: Session` 這類框架慣例
參數。design.py 把這三件事的機械前置資料組好、`design.py` 呼叫 LLM 前
先做完能做的部分，收到回應後再跟機械算好的骨架合併——呼應 00 二章
「能用程式判斷的，就不要交給 LLM」，也讓輸出 schema 跟重試失敗時的
「缺哪個 key」判斷都更單純。
"""
from __future__ import annotations

DESIGN_SYSTEM_PROMPT = """\
你是協助把 Java（Spring Boot）專案設計成 Python（FastAPI + SQLAlchemy）
專案結構的助手。你會收到一個模組（module）的資料，任務是回答三類機械
規則無法決定的設計問題，不需要重新輸出整份介面規格。

輸入包含：
1. module：模組名稱與業務摘要
2. classes_needing_layer：這個模組裡沒有 Spring stereotype annotation
   的類別（如 `@Service`／`@Repository`／`@RestController` 都沒有），
   需要你依業務語意判斷它屬於 FastAPI 專案的哪一層：
   - "routers"：直接處理 HTTP 請求/回應
   - "services"：業務邏輯層
   - "repositories"：資料存取層
   多數情況會落在 "services"，但如果類別明顯只做資料查詢/存取，判斷為
   "repositories"。
3. methods：這個模組的每一個方法，包含方法簽名、業務描述、以及需要你
   判斷的項目（可能兩者都有，也可能都沒有——都沒有的方法不需要你輸出
   任何 method_decisions 項目）：
   - uncovered_params：Java 簽名裡有、但在 openapi_spec 業務參數裡找不到
     對應的參數（候選框架注入物件，如 HttpServletRequest／Authentication／
     從 header 取出的 token）。針對每一個，決定它在 FastAPI 對應的
     寫法，輸出到 extra_params——每一筆是 `{"name": "參數名稱",
     "type": "型別寫法"}`，例如 `{"name": "request", "type": "Request"}`、
     `{"name": "current_user", "type": "Annotated[User,
     Depends(get_current_user)]"}`——依 Java 型別與方法描述的語境判斷
     合理的 FastAPI 慣例寫法，不要直接照抄 Java 型別名稱。
   - needs_db_session_decision：true 代表這個方法屬於 repositories／
     services 層（或所屬類別的層級由你在 classes_needing_layer 決定），
     需要你判斷這個方法要不要加一個 `db: Session` 參數（SQLAlchemy
     session，供實際存取資料庫用）——純轉發、純計算、不碰資料庫的方法
     可以判斷不需要。
4. upstream_interfaces：這個模組依賴的上游模組已經產出的介面簽名，
   純參考用，讓你知道有哪些函式/類別已經存在，不要在 extra_params 裡
   虛構呼叫不存在的東西。

輸出兩個陣列：

1. class_layers：對每一個 classes_needing_layer 裡的類別，輸出
   `{class_name, layer}`——**每一個都要回答，不能省略**。
2. method_decisions：對每一個「有 uncovered_params 或
   needs_db_session_decision=true」的方法，輸出
   `{signature_key, extra_params, needs_db_session}`——**每一個都要
   回答，不能省略**；`extra_params` 沒有需要新增的參數時給空陣列；
   `needs_db_session` 一律要有明確的 true/false，不能省略這個欄位。

不要輸出任何其他文字，不要用 markdown code fence 包裹。
"""

DESIGN_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "class_layers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "class_name": {"type": "string"},
                    "layer": {"type": "string", "enum": ["routers", "services", "repositories"]},
                },
                "required": ["class_name", "layer"],
                "additionalProperties": False,
            },
        },
        "method_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "signature_key": {"type": "string"},
                    "extra_params": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string"},
                            },
                            "required": ["name", "type"],
                            "additionalProperties": False,
                        },
                    },
                    "needs_db_session": {"type": "boolean"},
                },
                "required": ["signature_key", "extra_params", "needs_db_session"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["class_layers", "method_decisions"],
    "additionalProperties": False,
}
