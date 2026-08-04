# ① 解析 Agent 執行流程速覽

> 本文件是 `04b_parse_agent_code.md` 的執行流程整理，只列「呼叫誰 → 做什麼 → 產出什麼／為什麼需要」，不重複程式碼與設計理由。細節與對應章節見 04b。

---

## 總覽

```
graph/nodes/parse_node.py: run()
  └─ asyncio.to_thread(run_parse_agent)          # 同步阻塞呼叫丟到獨立 thread，避免卡住事件迴圈

parse_agent/__init__.py: run_parse_agent()
  ├─ 1. call_graph.parse_java_project()          Java 原始碼 → ParsedProject
  ├─ 2. summarize.run_map_reduce()               ParsedProject → (module_drafts, class_to_module)
  ├─ 3. skip_filter.load_skip_endpoints()        unfilled_endpoints.json → skip endpoint 清單
  ├─ 4. skip_filter.compute_excluded_methods()   → 排除的 method_id 集合
  ├─ 5. summarize.filter_excluded_methods()      module_drafts − 排除方法 → filtered_drafts
  ├─ 6. summarize.assemble_api_mapping()         → api_to_python_target
  └─ 7. summarize.finalize_module_list()         → module_list
```

---

## 一、`parse_java_project()`（call_graph.py，對應 04b 三章）

| function | 做什麼 | 產出／為什麼 |
|---|---|---|
| `_extract_classes()` | 逐檔 AST 抽取 class 的 field／method／stereotype／routes／annotations | `ClassInfo`，四、五章分析的基礎單位 |
| `_extract_interfaces()` | 逐檔 AST 抽取 interface 宣告（不含 field/route） | 讓 Spring Data JPA Repository 這類無實作類別的 interface 也能被依賴解析找到，補 `module_list` 完整性缺口 |
| `_extract_project_imports()` | 抽這個檔案 import 的專案內類別簡單名稱 | 補欄位／呼叫圖都解析不到的依賴（靜態呼叫、方法參考等），供四章 `controller_dependency_closure()` 使用 |
| `_uses_dynamic_query_signal()` | 檢查是否 import 已知動態查詢型別（如 `Specification`） | 供四章 `needs_llm_summary()` 判斷「這個類別一律要送 Map」 |
| `_extract_fields()` / `_constructor_qualifier_hints()` | 抽欄位與建構子參數上的 `@Qualifier` | 供多實作 interface 消歧用的線索 |
| `build_interface_implementors()` | 建 interface → 實作類別清單 | 供欄位型別解析時找候選類別 |
| `resolve_field_target_classes()` | 單一欄位解析到目標類別（`@Qualifier`／`@Primary` 消歧，失敗則保守全連結） | 呼叫圖建構、四章依賴閉包共用的核心解析函式 |
| `_build_call_graph()` → `_walk_and_resolve()` / `_continue_chain()` | 走訪每個方法 body，遞迴解析呼叫鏈（含鏈式呼叫） | `method_id → 呼叫的 method_id 集合`，供五章可達性分析 |
| `_extract_routes()` → `_build_route_index()` | 由 Controller annotation 組出完整 path | `endpoint_key → method_id` 索引，供五章 skip 比對、六章 ApiMapping 組裝 |
| `_read_context_path()` | 讀主設定檔 `server.servlet.context-path`（含佔位符處理） | 讓 route_index 路徑前綴跟 `openapi_spec` 一致 |

**輸出**：`ParsedProject(classes, call_graph, route_index)` —— 四、五章共用的唯一基礎資料，只建構一次。

---

## 二、`run_map_reduce()`（summarize.py，對應 04b 四、六章）

| function | 做什麼 | 產出／為什麼 |
|---|---|---|
| `controller_dependency_closure()`（grouping.py） | 從每個 Controller 沿欄位依賴＋import 依賴 BFS 展開完整閉包 | 找出誰依賴誰，供下一步算共用類別；import 依賴補欄位/呼叫圖解析不到的關係（靜態呼叫等） |
| `find_shared_classes()` | in-degree ≥ 2 的 class 判定為共用 | 決定 4a 批次名單 |
| `classify_trivial_classes()`（grouping.py） → `needs_llm_summary()` | 判斷每個 class 值不值得送 Map（Specification 特殊型別一律送、純資料類別＋只有存取器方法不送、其餘預設送） | `trivial_class_names`，從 4a/4b 批次剔除，省 API 額度但不犧牲完整性 |
| `build_shared_class_units()` | 共用類別（扣掉 trivial）依字元預算切批 | 4a 的 `MapUnit` 清單 |
| `run_map_phase_with_retry(shared_units)`【4a】 → `_run_map_batch()` → `_map_analyze_batch()` → `_normalize_method_name()` | 平行呼叫 Claude API 摘要每個共用 class；正規化多載方法名稱的消歧後綴；失敗進待重試清單，等 5 分鐘重試一次 | 共用類別摘要（`shared_summaries`），4b 會直接引用，不重複分析 |
| `build_controller_units()` | 每個 Controller 保留專屬依賴（閉包 − 共用類別 − trivial 類別），附上 `known_shared_summaries` | 4b 的 `MapUnit` 清單 |
| `run_map_phase_with_retry(controller_units)`【4b】 | 同上機制，組間可平行 | Controller 摘要 |
| `_mechanical_summary()` | 對 trivial 但確實被依賴到的 class，機械組出佔位摘要，不呼叫 API | 補進 Map 結果，維持 `module_list.java_files` 完整性 |
| `_reduce_phase()` | 彙整全部 Map 結果（含機械摘要） + `controller_dependencies`（程式算好的事實）→ 單次呼叫 Claude API | 決定最終 module 拆分（哪些 class 同屬一個 module） |
| `_assemble_module_drafts()` | 合併 Reduce 的 module 歸屬 + Map 已產出的方法清單（machine merge，不重問模型） | `module_drafts`（保留 `class_name`）+ `class_to_module` 對照表 —— 之所以先產出草稿而非直接輸出 `ModuleInfo`，是因為五、六章的排除與 ApiMapping 組裝都還需要 `class_name`，但最終型別沒有這個欄位 |

---

## 三、`load_skip_endpoints()` + `compute_excluded_methods()`（skip_filter.py，對應 04b 五章）

| function | 做什麼 | 產出／為什麼 |
|---|---|---|
| `load_skip_endpoints()` | 讀 `unfilled_endpoints.json`，取 `category=="skip"` | skip endpoint 清單 |
| `_all_endpoints_from_openapi()` | 展開 `openapi_spec["paths"]` 全部 endpoint 字串 | 「非-skip 全集」來源（刻意不用 `route_index` 自己，避免漏掃的 route 讓誤排除偵測不到） |
| `_endpoints_to_method_ids()`（skip 組／非-skip 組各跑一次） | 查 `route_index` 把 endpoint 轉成 method_id 起點；查無對應記 warning | skip 組、非-skip 組各自的 BFS 起點集合 |
| `_bfs_reachable()`（各跑一次） | 沿 `call_graph` 做可達性分析 | `skip_reachable`／`non_skip_reachable` |
| `compute_excluded_methods()` 收尾 | `excluded = skip_reachable − non_skip_reachable`；另外對 `skip_starts ∩ non_skip_starts`（多載同名方法 method_id 碰撞）記警告 | 只有「只能被 skip endpoint 到達」的方法才會被排除；孤立方法、共用方法自動保留；method_id 碰撞的 warning 供人工核對 `module_list` 描述 |

---

## 四、輸出組裝（summarize.py，對應 04b 七章）

| function | 做什麼 | 產出／為什麼 |
|---|---|---|
| `filter_excluded_methods()` | 從 `module_drafts` 的每個 method 中濾掉出現在 `excluded` 的項目 | `filtered_drafts`（排除發生在輸出組裝前） |
| `assemble_api_mapping()` | 用 `route_index` + `class_to_module` + `filtered_drafts` 裡仍存在的方法做交叉比對；`skip_endpoints` 額外做 endpoint 層級絕對排除 | `api_to_python_target`（`ApiMapping` 清單）—— 方法已被排除、未歸屬任何 module、或 endpoint 本身被人工標記 skip 的直接跳過（絕對排除保證，見 04a 五章） |
| `finalize_module_list()` | 剝除 `_ModuleDraft` 內部用的 `class_name`／`file_path` | `module_list`（`ModuleInfo` 清單），對應 `RefactorState` 最終欄位 |

---

## 五、最終回傳

`run_parse_agent()` 回傳 `(module_list, api_to_python_target)`，由 `parse_node.py` 整包展開回寫 `RefactorState`。
