# LangGraph Orchestrator 實作進度

## 完成項目（Stub-First 第一～三階段）

### 1. 專案基礎設施
- [x] 目錄結構（見 `01_langgraph_architecture.md` 二）
  - `graph/` - LangGraph 組裝與 node 定義
  - `refactor_harness/` - Harness stub
  - `translator_cli/` - translator-cli stub
  - `config/`, `postman/`, `fixtures/` - 配置與資料目錄

- [x] 配置檔案
  - `requirements.txt` - 依賴鎖定版本（langgraph 1.2.6 等）
  - `.env.example` - 環境變數模板
  - `.gitignore` - Git 忽略規則
  - `.gitattributes` - 換行符統一（Windows 相容，見 `01` 八）

### 2. State Schema（state.py）
- [x] 完整 TypedDict 定義（見 `01_langgraph_architecture.md` 三）
  - Agent ① 輸出：`ModuleInfo`, `ApiMapping`
  - Agent ③ 輸出：`PythonStructure`, `InterfaceSpec`
  - [P] Plan Agent 輸出：`TaskSpec`
  - 13 個 State 欄位 + reducer（`Annotated[list, operator.add]`）

### 3. ModuleScheduler（scheduler.py）
- [x] 排程邏輯實作（見 `01_langgraph_architecture.md` 六）
  - Module topological sort（依 `depends_on`）
  - Task 級依賴（`_task_deps_satisfied`）
  - Regression 偵測（`check_upstream_regression`）
  - 防呆：自動串接無序 task（`_backfill_missing_task_deps`）

### 4. Graph Builder（builder.py）
- [x] 節點註冊（11 個 node）
- [x] 邊連接
  - 主線性流程：parse → extract_spec → gen_collection → record_tests → design
  - 平行分支：design → (plan + scaffold) 同時觸發 → implement
  - Retry 迴圈：implement → run_tests → (conditional) → debug → implement
  - Conditional edge：`should_debug_or_done` 依 test_results.status 與 retry_count 分流

### 5. Node Stub
- [x] 線性 node（可安心展開 state）
  - `parse_node.py` (①) - 回傳 module_list, api_to_python_target
  - `spec_node.py` ([A]) - 回傳 openapi_spec
  - `collection_node.py` ([B]) - 回傳 collection 路徑
  - `design_node.py` (③) - 回傳 python_structure, route_to_file_mapping
  - `debug_node.py` (⑦) - 遞增 retry_count
  - `give_up_node.py` - 列印 fail 資訊

- [x] 平行分支 node（只回傳自己的 key）
  - `plan_node.py` ([P]) - 回傳 task_list
  - `scaffold_node.py` (④) - 回傳 scaffold_done=True

- [x] 複雜 node
  - `implement_node.py` (⑤) - ModuleScheduler 串接、MODEL_SEMAPHORE(1)、局部驗證邏輯框架
    - 內部排程邏輯完整
    - 呼叫 translator-cli / Harness 部分標記為 TODO（見下節）
    - 回傳值正確區分 reducer key 與 snapshot key

### 6. Harness Stub（refactor_harness/）
- [x] LangGraph 節點
  - `langgraph_nodes.py`
  - `record_golden_output` (②) - stub 回傳 recorded
  - `run_postman_tests` (⑥) - stub 回傳 pass
  - `should_debug_or_done` - conditional edge 邏輯完整

- [x] 輔助型別（stub 介面）
  - `verifier.py` - `GoldenVerifier` class（stub）
  - `db_env.py` - `DbEnvironment` class（stub）

### 7. translator-cli Stub（translator_cli/）
- [x] 客戶端與型別
  - `client.py` - `fill_function()`, `generate_scaffold()` (stub)
  - `types.py` - `FillResult` TypedDict

### 8. 進入點（main.py）
- [x] Graph 組裝與執行
- [x] Initial state 初始化
- [x] Windows 相容（`asyncio.WindowsProactorEventLoopPolicy`）

### 9. 邏輯驗證（Stub-First 第一～三階段）
- [x] 線性流程完整連通
- [x] 平行分支：plan/scaffold 同時觸發，implement 等兩者完成
- [x] Retry 迴圈：run_tests → conditional → debug/give_up → implement (或 END)
- [x] ModuleScheduler 邏輯
  - Module 依賴排序（topological）
  - Task 依賴滿足檢查
  - Regression 偵測（check_upstream_regression）
- [x] Reducer 累加（completed_tasks, failed_tasks, partial_reports）
- [x] 跨 module 依賴解鎖路徑（驗證多 module 依賴鏈正常運作）
  - module A (無依賴) → module B (依賴 A) → module C (依賴 B)
  - mark_module_verified() 被正確呼叫
  - 依賴未滿足的下游 module task 不進入就緒佇列

---

## 待實作項目（Stub-First 第四階段）

### 實作順序
按照 `01_langgraph_architecture.md` 七的替換順序，逐一換掉 stub：

1. **② 測試 Agent（Harness 錄製端）** → `02a_harness_architecture.md` + `02b_harness_code.md`
   - 啟動 Java 服務（subprocess 管理）
   - 執行 Postman collection（newman）
   - 記錄 golden output

2. **③ 架構設計 Agent** → Claude API 呼叫
   - 呼叫 `langchain_anthropic.ChatAnthropic`
   - 解析回應產出結構化 PythonStructure

3. **④ 骨架實作 Agent** → translator-cli
   - 實現 `translator_cli.generate_scaffold()`
   - 建立目錄、base class、簽名

4. **[P] Plan Agent** → Claude API 呼叫
   - 生成 task list（每個 task 鎖定單一函式）
   - 驗證覆蓋率（涵蓋所有 methods）

5. **⑤ 功能改寫 Agent** → translator-cli + scheduler
   - 實現 `translator_cli.fill_function()`
   - 呼叫 ollama HTTP API
   - 整合 `GoldenVerifier.verify_module()` 與 `DbEnvironment.apply_seed()`

6. **⑥ 測試執行 Agent（Harness 驗證端）** → 見 02a/02b
   - 對 Python 服務執行 Postman collection
   - 與 golden output 對比

7. **⑦ Debug Agent** → Claude API 呼叫
   - 分析 fail 清單 + diff 報告
   - 產生修正指令回饋

8. **[A] Spec Agent** → Java 服務啟動 + OpenAPI 提取

9. **[B] Collection Agent** → OpenAPI 轉 Postman

10. **① 解析 Agent** → 實際 Java 程式碼分析

### 待決定事項
見 `00_refactor_architecture.md` 十，目前保留的待決定事項：
- [ ] 測試 DB schema 來源（Java ddl-auto vs 手動 dump）
  - 影響 Spec Agent 啟動 Java 服務前的準備步驟
  - 決定後更新五、環境建立

### 介面統一
- [x] FillResult 定義統一在 `translator_cli.client.FillResult` class
  - 屬性存取：`.success` (不是 `["success"]`)
  - `translator_cli.types` 只 re-export，避免重複定義
  - implement_node.py 目前 stub 用字典，待接上真實 translator-cli 時改屬性存取

### 外部依賴
- Node.js 工具（全域安裝或 npx）
  - `openapi-to-postmanv2` - OpenAPI 轉 Postman
  - `newman` - 執行 Postman collection
- PostgreSQL（測試 DB）
  - 另開一顆 `_TEST` 資料庫
  - seed.sql 初始資料
- ollama（另一台 Mac）
  - 已下載 `qwen2.5-coder:32b`
  - HTTP API port 11434

---

## 快速開始

### 環境準備
```bash
python3 -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 編輯 .env 填入實際值
```

### 執行（目前為 stub，會直接結束）
```bash
python main.py
```

### 觀察 Graph 結構
```python
from graph.builder import build_graph
graph = build_graph()
print(graph.get_graph().draw_mermaid())
```

---

## 設計重點回顧

### 平行分支（不需 reducer）
`plan` 與 `scaffold` 都以 `design` 為前驅、都指向 `implement` 為後繼。
LangGraph 在同一個 superstep 平行呼叫兩者，兩者都完成後觸發 `implement` 一次。

**注意**：兩個分支只能回傳各自實際更動的 key，不能 `{**state, ...}` 展開，
否則同一個 key 被兩個分支各自寫入會產生未定義行為。

### 排程 vs. 執行併發
- **排程層**：`get_ready_tasks()` 可一次回傳多個就緒 task（見 implement_node)
- **執行層**：`MODEL_SEMAPHORE = asyncio.Semaphore(1)` 保證只有一個真的在呼叫本地模型
- **效益**：排程更有彈性，module 完成順序不死板卡住

### Regression 偵測
修改已驗證 module 的檔案時觸發，把該 module 打回 `needs_reverify`。
不是「當前 + 全部上游」重驗，只重驗被波及的那個。

### Retry 迴圈 + retry_count
- 只在 `failed_modules` 非空時扣減（代表程式碼確實跑過、驗證過但沒通過）
- `blocked_modules` 是被牽連的下游，待 `failed_modules` 修好後自然釋放

---

### Harness 修正記錄
- [x] Mutation 錄製異常偵測（非預期 status code 判定、tainted folder 排除、`excluded_folders` 串接進 report）— 對照 `02a`/`02b` 最新版更新 `golden_writer.py`／`mutation_verifier.py`／`reporter.py`／`test_nodes.py`，新增 `tests/refactor_harness/` 單元測試（4 pass）

---

*最後更新：2026-07-23*  
*下一個里程碑：實作 02a/02b（Harness 詳細設計與程式碼）*
