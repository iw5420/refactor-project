# 任務：重新設計 ⑤ 功能改寫 Agent 的 context 組裝機制——從「LLM 語意摘要」改成「呼叫鏈 + 完整 Java 原始碼」

## 背景與動機

這個專案（`D:\test\2026\refactor-project`，LangGraph pipeline 把 Java Spring Boot 專案自動翻譯成 Python FastAPI 專案）目前 ⑤ 功能改寫 Agent（`translator_cli`／`graph/nodes/implement_node.py`）的 context 組裝方式是：

```
① parse（機械分析 Java call graph、methods）
→ ③ design（產生 Python interface 骨架：簽名、routes）
→ [P] plan_agent（LLM 分析 Java method + Python interface，產生 description／context／referenced_interfaces 這一層「語意摘要」）
→ ④ scaffold（依 plan 產出的 task_list 產生 Python 骨架）
→ ⑤ implement（根據 description + context + referenced_interfaces 推導出的 context_files，讓 Claude 重新「生成」function body）
```

**已經用真實完整 pipeline 重跑多次、逐一 diff 兩次執行的完整 prompt/response，確認了這個設計的根本弱點**：`[P] plan_agent` 產生的 `description`／`context`／`referenced_interfaces` 是 LLM 生成的一層「抽象語意摘要」，這層轉譯本身不穩定、不可靠——⑤ 即使模型能力很強，也會因為「拿到的資訊本身不完整或不準確」而產生錯誤翻譯。這不是單一 bug，是設計方向本身的問題。

**結論**：不管怎麼調整 `referenced_interfaces` 的過濾規則（nominal typing 或 structural typing），只要 ⑤ 拿到的还是「plan agent 生成的摘要」而不是「Java 原始碼本身」，這類問題會不断以新的形式冒出來——因為真正的正確資訊（正確的錯誤碼數值、正確的 API 呼叫慣例、正確的欄位名稱）只存在於 Java 原始碼裡，任何摘要／轉譯步驟都可能遺漏或扭曲它。

## 新方案構想

**不再依賴 plan agent 生成的抽象摘要間接指導 ⑤ 翻譯，改成直接把「這個 Java method 完整呼叫鏈上涉及的所有 Java 原始碼」餵給 Claude API，讓它照著 Java 原始邏輯直接復刻成 Python，而不是根據抽象描述重新創作。**

具體流程設想：

1. 對每一個要翻譯的 interface（對應一個 Java method），用呼叫圖分析找出它的**完整呼叫鏈**（遞迴展開：這個 method 呼叫的所有下游 method，以及那些 method 又呼叫的下游……直到沒有更多可展開的）。
2. 把呼叫鏈上涉及的所有 Java 原始碼——方法本體、相關的 class／Enum／DTO 定義——完整組裝，直接作為 ⑤ 呼叫 Claude API 的 context（取代現在的 `description`＋`context`＋`referenced_interfaces` 推導出的 `context_files`）。
3. 要求 Claude「照著這份完整的 Java 邏輯，逐层忠實復刻成對應的 Python 實作」，而不是「根據需求描述重新生成」。
4. 復刻完成後，沿用既有的 ⑥ `run_tests`（Postman collection + golden 驗證）機制驗證功能是否正確——這一段不需要改動。

## 技術可行性：這個專案已經有大部分需要的基礎設施，不用從零造輪子

**Python 端已經有跟 Java 端 1:1 對應的五層架構**（已確認）：
- `app/routers/{module}.py` ← Controller
- `app/services/{module}.py` ← Service
- `app/repositories/{module}.py` ← Repository
- `app/schemas/{module}.py` ← DTO（Request/Response，Pydantic）
- `app/models/{module}.py` ← Entity（SQLAlchemy ORM）

`plan_agent/module_index.py::_LAYER_SUFFIX` 與固定依賴全序 `repositories < services < routers` 已經明確定義這個分層；`design_agent/layout.py` 也把 schemas／models 拆成兩個獨立目錄。一條 Java 呼叫鏈（`Controller.foo() → Service → Repository → Entity`）在 Python 端本來就有結構完全對應的落點。

**Java 端呼叫圖建構與可達性分析已經存在（機械化、非 LLM，不會有摘要失真的問題）**：
- `parse_agent/call_graph.py::_build_call_graph()`——已經產出 `method_id -> 直接呼叫的 method_id 集合`（純 javalang AST 分析）。
- `parse_agent/skip_filter.py::_bfs_reachable(starts, call_graph)`——已經有從一組起點沿呼叫圖做 BFS 可達性分析的既有實作（環偵測靠 `visited` 集合天然處理）。目前只是被用在完全不同的用途（判斷 skip endpoint 該排除哪些方法），但機制本身可以直接復用來算「這個 task 對應的 Java method，完整呼叫鏈上有哪些 Java 方法」。

## 已知的權衡與風險（需要在設計階段就考慮）

1. **這是架構級改動，不是小修小補**：牽涉到 `[P] plan_agent` 和 ⑤ `implement` 的核心設計，可能需要大幅簡化或砍掉現在的 plan 階段（如果不再需要「LLM 分析業務邏輯生成描述」這一步，改成機械化的呼叫鏈分析）。這會影響 `06a_plan_agent_architecture.md`／`07a_translator_cli_architecture.md` 等既有設計文件，需要重新設計並更新文件，不只是改程式碼。
2. **不要一次性砍掉太多既有機制**：④ scaffold（Python 骨架生成）、⑥ run_tests（Postman + golden 驗證）、⑦ debug agent（除錯迴圈）這些機制目前運作正常，不在這次重構範圍內，只聚焦在「⑤ 的 context 組裝來源」這一件事。

## 目前的專案狀態

- 專案已經還原到 88.5% pass rate 那次（`run_id=20260831_032716_294015`）的乾淨程式碼狀態，對應 commit `3f8c811`（`docs: sync implement-agent docs and bug trace to current implementation`）。
- 這個 commit 已經同時存在於 `master` 與 `claude-api-implement-agent` 兩個分支（後者已併回 `master`，是 no-op merge）。
- 新分支 `refactor-call-chain-implement` 已經從 `master` 建立好，當前 HEAD 就是 `3f8c811`，working tree 乾淨。
- 這次追查 #74 過程中做的所有修復（`plan_agent/planning.py` 的型別相容性過濾邏輯、`spec_collection_agent/java_service.py` 的 port-conflict 偵測、`graph/nodes/implement_node.py` 的 log 措辭修正、`docs/09b_bug_trace.md` 的追查記錄）**都已經被捨棄**，因為新架構方向預期能從根本解決這類問題，不需要在舊架構上繼續打補丁。如果 port-conflict 偵測這類「跟翻譯架構無關的獨立改善」之後想要，可以再重新評估要不要補回來。
---

*這份文件是從另一個 session 的完整討論過程整理出來的任務交接，目的是讓新 session 不需要重新推導這些背景就能開始工作。如果需要查證某個具體案例的原始 prompt/response，可以用 `python -m llmlog` 查詢——但注意 `llm_traces.db` 是持續累積的，`run_id=20260831_032716_294015`（88.5%）和後續幾次真實測試的記錄應該還在。*
