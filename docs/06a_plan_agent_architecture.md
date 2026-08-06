# [P] Plan Agent 詳細設計

> 本文件承接 `00_refactor_architecture.md` 的整體架構（見七、[P] Plan Agent 一節；八、task list 欄位定義），是這個 Agent 的**設計面**文件：決策、契約、資料結構、流程。實際程式碼實作見 `06b_plan_agent_code.md`；本文件不出現可執行的實作邏輯。

---

## 一、本文件範圍與定位

**本文件涵蓋**：
- [P] 的輸入來源（① `module_list` ＋ ③ `python_structure`，見二章）
- task 的產生單位與涵蓋率保證機制（三章）
- module 歸屬判定（四章）
- LLM 設計階段：業務描述整合與呼叫關係判斷（五章）
- `task.depends_on` 組裝演算法（六章）
- `target_files` 組裝規則（七章）
- 輸出資料如何對應 `RefactorState.task_list`（八章）

**本文件不涵蓋**：
- 實際程式碼——見 06b
- ⑤ 如何消費 `task_list`、`ModuleScheduler` 排程細節——見 `01_langgraph_architecture.md` 六章、`09a_implement_agent_architecture.md`（待建立）
- `route_to_file_mapping`——由③產出並直接寫入 `config/harness.yaml`，見 `05a_design_agent_architecture.md` 八章，[P] 不重複這件事

---

## 二、輸入與前置資料

| 輸入 | 來源 | 說明 |
|---|---|---|
| `module_list` | State，①產出 | `module.summary`／`depends_on`／`methods`（`java_method`／`class_name`／`description`／`complexity`）——業務語境來源，**不是**涵蓋率權威來源，見三章 |
| `python_structure.interfaces` | State，③產出 | `file_path`／`class_name`／`function_name`／`params`／`return_type`——[P] 拆 task 的權威輸入，見三章 |

**不需要** `openapi_spec`／`api_to_python_target`／`java_project_path`：API 契約層級的判斷已由③消化進 `python_structure`（見 05a 五章「API 邊界方法改用 openapi_spec 覆寫」），Java 原始碼層級的判斷已由①②③各自消化完畢並沉澱進 State，[P] 不重新碰觸這些上游輸入——呼應 00 二章的分工原則，不重複判斷已經判斷過的事。

---

## 三、處理單位：以 `python_structure.interfaces` 為權威輸入，不是 `module_list.methods`

00 八章要求 task list「以①輸出的完整方法清單為準」。但①的 `MethodInfo` 與③的 `InterfaceSpec` 之間**沒有形式化的對應欄位**：04a 六章明講「① 不產出任何 Python 命名建議欄位」；③內部用 `camelCase → snake_case` ＋ 私有方法底線前綴建立 `java_method → function_name` 的對應（見 05a 七章、05b 實作），但這個中繼結果是 `design_agent` 套件內部的暫存資料，不落地進 State（跟 04a 的 `ParsedProject`、05a 四章的輕量再掃描結果一樣，即用即丟，見 04a 二章、05a 二章的既有先例）。

**決策：[P] 產 task 的基礎單位是 `python_structure.interfaces`，不是 `module_list.methods`**，理由：

1. **涵蓋率已經由③保證過一次**：05a 七章訂為強制規則——`interfaces` 必須涵蓋 `module_list` 裡每一個 module 的每一個方法。[P] 若改用 `interfaces` 當基礎，直接繼承這個已驗證過的涵蓋率保證，不需要重新比對 Java 方法名稱這種脆弱的字串匹配。
2. **`interfaces` 才是實際要填空的對象**：translator-cli 的 `fill_function()`（00 七章⑤）操作的是③＋④已經建好骨架的 Python 函式，不是 Java 方法本身——一個 Java 方法可能被③拆成一個以上的 Python 函式，也可能因框架慣例參數（05a 七章）被加了額外參數，`interfaces` 是這些變化後唯一權威的下游狀態。

**涵蓋率規則**：每一個 `InterfaceSpec` 恰好對應一個 task（1:1），不聚合、不拆分。這讓涵蓋率檢查完全機械化——task_list 產出後，比對是否每個 `InterfaceSpec` 都被恰好一個 task 認領（見八章）。

**全域基礎設施檔案與資料類別排除**：`app/main.py`／`app/core/database.py`（05a 三章「全域基礎設施檔案」）、`schemas/{module}.py`（Pydantic model 欄位）、`models/{module}.py`（SQLAlchemy ORM，00 七章交給④生成）都不產生 `InterfaceSpec` 條目，因此天然不會出現在 task_list 裡，不需要 [P] 額外過濾。

---

## 四、module 歸屬判定（機械）

`InterfaceSpec` 沒有 `module` 欄位，需要從 `file_path` 反查。05a 三章訂死的檔名規則是唯一依據：`{module}_{layer_singular}.py`，`module` **逐字沿用** `ModuleInfo.module`，不額外轉換大小寫。

**演算法**：取 `file_path` 檔名（去掉副檔名），依 `_router`／`_service`／`_repository` 三個固定後綴之一去掉後綴，剩餘字串直接比對 `module_list` 的 `module` 集合——檔名本來就是③逐字拼接 `ModuleInfo.module` 產生，這裡是逐字反查，不是猜測，理論上必然精確命中。找不到對應後綴、或比對不到任何 module（不應發生，代表③輸出違反自己承諾的檔名格式）→ 直接中止，交由人工核對③的輸出（見十一章）。

**與 02a 十三章「module 詞彙一致性」的關係**：這裡反查出的字串就是 `ModuleInfo.module` 本身，[P] 沒有發明新字串，這是**唯一正確的做法**，不是權宜之計——`task.module` 必須逐字等於 `ModuleInfo.module`，這是 `01_langgraph_architecture.md` 六章 `ModuleScheduler` 用 `module` 當 dict key 分組任務、比對 `module_list.depends_on` 的結構性前提：`self.modules`／`self.module_status` 都以 `ModuleInfo.module` 為 key，若 `task.module` 改用其他來源（例如 Harness 的 `get_module()` URL 推斷），這個 module 的所有 task 會被塞進排程器永遠不會查詢的 key 底下，`get_ready_tasks()` 永遠找不到它們，整個 module 卡在 `pending` 且無法診斷——這比「局部驗證漏測」嚴重得多，是結構性錯誤，不是覆蓋率風險。

Harness 端 `get_module()` 與 `ModuleInfo.module` 之間過去確實存在既知落差（深層路由如 `/api/v1/admin/orders/audit` 可能被 `get_module()` 誤判成 `admin`），但這個問題已在 02a 十三章、十一章、02b `core/route_mapper.py` 一併解決：③在計算 `route_to_file_mapping` 的同一次迴圈裡，額外輸出 `route_to_module_mapping`（見 05a 八章），`get_module()` 改為優先查這份表（`RouteMapper.resolve_module()`），只有真正落在①③解析範圍外的殘餘情況才 fallback 回 URL 推斷。因此 [P] 這裡逐字沿用 `ModuleInfo.module`，Harness 端的模組分區也會查到同一個值，兩邊天生一致，不再是「[P] 不負責解決的既有風險」——問題已經在更上游被消掉了。

---

## 五、LLM 設計階段（Claude API）

### 處理單位：依 module 平行處理，不需要拓樸分波

跟③（05a 六章）的關鍵差異：③需要拓樸分波，是因為下游 module 設計時需要引用「上游 module 已經產出的 `InterfaceSpec`」——上游還沒設計完，下游就沒東西可引用。**[P] 執行時 `python_structure` 已經是③完整跑完的最終輸出，所有 module 的 `interfaces` 同時可得**，不存在「上游還沒完成」的問題。因此 [P] 對每個 module 的 Claude 呼叫互相獨立，**全部平行**，不依 `module_list.depends_on` 分波——這是與③刻意不同、也應該不同的設計，照搬③的拓樸分波在這裡是多餘的複雜度。

併發數呼叫 `common.concurrency.default_concurrency()`（沿用既有共用工具，見 00 六章）。

### 單一 module 呼叫內容

| 輸入 | 說明 |
|---|---|
| `module.summary`／`module.methods`（含 `description`） | 業務語境來源 |
| 這個 module 的完整 `interfaces` 清單 | [P] 實際要拆 task 的對象 |
| `depends_on` 列出的上游 module 的 `interfaces` 清單 | 供判斷跨模組呼叫關係，決定 `target_files`（見七章），**不**影響 `task.depends_on`（`task.depends_on` 只管同 module 內順序，見六章） |

**為什麼不由 [P] 自己先做 Java method ↔ Python interface 的機械配對，再把配對結果交給 LLM**：這個配對規則（`camelCase → snake_case` ＋ 私有前綴）雖然機械，但不落地進 State（見三章），[P] 若要重新推導需要重新實作同一套轉換邏輯；而且 `routers` 層 `InterfaceSpec.class_name` 一律為 `None`（05a 七章），Java Controller 方法卻有明確 `class_name`，用「`class_name`＋`function_name` 反查對應 Java method」在 router 層會產生額外歧義（兩側 `class_name` 不對稱）。**這是語意層面的比對，機械規則做不到，因此直接交給 LLM 判斷**——把 module 的 Java methods 與 Python interfaces 一起丟給同一次呼叫，讓 LLM 利用兩者自然的對應線索（函式名稱轉寫關係、參數個數、描述語意）自己配對，同時完成「這個函式該怎麼寫」的描述整合，不需要 [P] 自己先算出一份可能出錯的配對表。

### 輸出

對這個 module 的**每一個** `interfaces` 條目，輸出一筆：

| 欄位 | 說明 |
|---|---|
| `file_path`／`class_name`／`function_name` | 原樣抄回輸入，供核對完整性用，做法比照 04a 四章 Map 階段的 `class_name` 核對 |
| `description` | 整合對應 Java method 業務邏輯後，給 translator-cli 的任務描述 |
| `context` | 補充依賴關係／邊界條件文字 |
| `referenced_interfaces` | 這個函式業務邏輯上會呼叫到的其他 interface（同 module 或依賴 module 皆可），每筆用 `file_path`／`class_name`／`function_name` 三元組標示；查無業務關聯時為空陣列 |

**核對規則**：回應的 `(file_path, class_name, function_name)` 集合必須與輸入的 `interfaces` 集合完全一致，缺漏視同呼叫失敗（比照 04a 7.1 `_map_analyze_batch()`「核對到遺漏時視同呼叫失敗」，不放行不完整結果）。`referenced_interfaces` 若指向不存在的 `(file_path, class_name, function_name)` 組合（LLM 虛構或拼錯），六／七章組裝時一律過濾並記警告，不中止（比照 04a 7.3 `_assemble_module_drafts()` 對 `depends_on`／`java_classes` 的既有驗證方式）。

**失敗處理**：比照 05a 六章——單一 module 失敗列入待重試清單，這一輪其餘 module 跑完後等待 5 分鐘統一重試一次；仍失敗中止整條 `plan` run（理由同 05a：`task_list` 是⑤唯一輸入，任一 module 的 task 缺失會讓涵蓋率保證失效，風險遠高於重新執行一次）。

**用量記錄**：一律經由 `common/llm_client.py` 的 `call_claude_for_json()`，不自行重新實作（見 00 六章）。

---

## 六、`task.depends_on` 組裝（同 module 內排序）

00 八章：`depends_on` 只決定**同一 module 內**的執行順序，跨 module 順序由 `ModuleScheduler` 依 `module_list.depends_on` 保證（見 01 六章），[P] 不需要、也不應該在 `task.depends_on` 裡編碼跨 module 依賴。

**排序目的是 context 品質，不是正確性**：④已經把這個 module 所有函式的骨架（含簽名）建好（00 七章），⑤填空時呼叫的函式即使還沒被填入真正邏輯，簽名也已存在，不會因呼叫順序而解析失敗。`depends_on` 真正影響的是 translator-cli 傳給本地模型的 `target_files` 內容——被依賴的函式若已經填入真實邏輯（而非空骨架），呼叫端的 prompt 才能看到更有意義的參照實作（見 00 六章「Context 控制策略」）。這個定位差異，決定了下面「疊加式」演算法在衝突時可以直接捨棄部分依賴邊，不當成硬性錯誤（跟 05a 六章 module 級 `depends_on` 若成環直接視為「上游輸入資料錯誤、中止」不同——那裡的依賴代表真實業務邊界，成環代表輸入矛盾；這裡的依賴只是品質最佳化的軟性提示，即使有循環引用，生成的程式碼本身仍合法可執行）。

### 兩層依賴，機械疊加

1. **層級 baseline（機械，不需要 LLM）**：同 module 內，`repositories` 層任一 task 一律先於 `services` 層任一 task；`services` 層任一 task 一律先於 `routers` 層任一 task（00 八章範例即為此順序）。無 stereotype、由③的 LLM 判斷歸屬的類別，最終仍落在這三層之一（05a 三章），因此層級永遠可從 `file_path` 所在目錄機械判斷，不需要額外資訊。
2. **同層／跨層補強**：五章 LLM 輸出的 `referenced_interfaces`，篩選出「屬於同一個 module」的部分（跨 module 的部分只用於七章 `target_files`，不進 `depends_on`），過濾掉指向自己的邊，轉成對應 task id 的依賴邊。

### 防環規則（機械，不需要偵測演算法）

給每個 module 內的 task 一個**固定全序**：先按層級（`repositories` < `services` < `routers`），同層再按 `function_name` 字母序（`class_name` 為 tie-break）。疊加 LLM 輸出的依賴邊時，**只保留「被依賴 task 的全序索引 < 依賴方 task 的全序索引」的邊**，索引相等或反向的邊直接捨棄並記 log（多半是同層函式互相引用，或 LLM 誤判方向）。

這個規則保證最終的 `depends_on` 圖必為這個全序的子集，結構上不可能出現環，不需要另外跑拓樸排序／環偵測演算法驗證——這是本章開頭「排序是品質最佳化、非正確性要求」定位的直接延伸：捨棄掉的邊只是少一點 context 品質，不是遺漏了必須存在的依賴。

---

## 七、`target_files` 組裝

| 組成 | 來源 | 判定方式 |
|---|---|---|
| 自己的 `file_path` | 這個 task 對應的 `InterfaceSpec.file_path` | 機械，必然存在，固定放 `target_files[0]` |
| 五章 `referenced_interfaces` 對應的 `file_path` | 不論同 module 或跨 module，機械查表取得（同 module 的還會反映進六章 `depends_on`；跨 module 的只反映在這裡） | 機械查表，[P] 不重新判斷「要不要納入」——LLM 已在五章判斷過業務關聯性 |
| 本 module 的 `schemas/{module}.py`（若存在） | `directory_tree` 是否有這個檔案的 Schema 定義段（見 05a 三章格式慣例） | 機械：這個 task 屬於 `routers` 或 `services` 層（見四章判定）且該檔案存在時一律加入，不細究具體用到哪幾個欄位——`services` 層之所以也納入，是因為它經常直接收發 router 傳下來的同一組 Pydantic model（05a 五章型別對應表本身也承認「專案內自訂 class 的實際定義由 schemas／models／service 回傳型別決定」，無法從型別字串機械判斷歸屬），呼應 04a／05a 反覆出現的「多連、少排除」保守精神 |
| 本 module 的 `models/{module}.py` | 無條件（見下方說明） | 機械：這個 task 屬於 `services` 或 `repositories` 層時一律加入，不做存在性判斷 |

**`models/{module}.py` 為什麼是無條件加入，不是比照 `schemas` 做存在性判斷**：`schemas/{module}.py` 只在 `modules_with_schema_file` 集合裡的 module 才會真的產出（05a 六章、05b「已驗證」段），需要先判斷存不存在，否則會指向一個 directory_tree 裡不存在的幽靈檔案（05a 八章）；`models/{module}.py` 則是**每個 module 都無條件產出**的 SQLAlchemy ORM 佔位檔案（05a 三章、05b 十一章「目錄結構段涵蓋 schemas／models」已驗證：`app/models/{module}.py` 對 `all_module_names` 全部列出，不像 schemas 有子集限制），沒有幽靈檔案風險，不需要額外判斷。`repositories` 層方法幾乎必然回傳／查詢 ORM entity（00 六章例子本身就是「實作 `UserRepository.get_by_id()` 時，只需傳入 `user_repository.py` 和 `user.py`」——這裡的 `user.py` 就是 model 檔案），`services` 層則經常直接處理 repository 回傳的 entity，兩層都納入。`routers` 層不納入 `models`：router 層方法的 `params`／`return_type` 依 05a 五章「API 邊界方法改用 openapi_spec 覆寫」規則，改用 `schemas`（Pydantic）而非 Java entity 型別，不應該直接碰觸 ORM model。⑤若仍因缺檔案而生成錯誤，交給 module 局部驗證與⑦ Debug Agent 的既有回饋機制處理（02a 十三章），不在 [P] 這一步窮舉解決。

---

## 八、輸出格式與 State 對應

`task_list` 直接對應 `graph/state.py` 既有的 `TaskSpec`（00 八章已定義，這裡不重新定義結構）：

```python
class TaskSpec(TypedDict):
    id: str
    module: str
    description: str
    target_files: list[str]
    context: str
    depends_on: list[str]
```

**`id` 產生規則**：全部 task 依「module（依 `module_list` 原始順序）→ 層級（`repositories`／`services`／`routers`）→ `function_name` 字母序」的固定全序（即六章防環規則用的同一套全序）依序編號 `task_{:03d}`——沿用同一套排序，除了編號穩定、可重現（同一份輸入重跑會產生同樣的 id 分配，方便除錯與比對），也不需要為編號另外設計第二套排序邏輯。

**涵蓋率驗證（機械，收尾步驟）**：`task_list` 產出後，驗證每個 `InterfaceSpec` 都被恰好一個 task 認領（`target_files[0]` 對應到的 `file_path`／`class_name`／`function_name` 三元組覆蓋 `python_structure.interfaces` 全集，且無重複認領）。任何缺漏或重複視為 [P] 自己組裝邏輯的 bug，直接拋出中止——不是需要人工判斷的模糊情況，是程式該保證但沒保證到的不變量。

---

## 九、模組結構規劃

比照 `parse_agent/`／`design_agent/` 的組織方式，[P] 的邏輯規劃為獨立套件 `plan_agent/`，`graph/nodes/plan_node.py` 維持薄封裝：

```
refactor-project/
└── plan_agent/
    ├── module_index.py   # 四章：file_path → module 反查、interface 全序索引（六/八章共用）
    ├── prompts.py        # 五章 system prompt 與 output schema
    ├── llm.py            # [P] 專屬的模型選擇
    ├── planning.py        # 五章 LLM 呼叫、六章 depends_on 組裝、七章 target_files 組裝、八章 task_list 組裝與涵蓋率驗證
    └── __init__.py        # 對外唯一入口
```

| 職責 | 說明 |
|---|---|
| module 歸屬判定、全序索引 | 對應四章、六章 |
| Claude API 逐模組設計 | 對應五章 |
| `depends_on`／`target_files` 組裝 | 對應六章、七章 |
| `task_list` 組裝與涵蓋率驗證 | 對應八章 |
| 對外唯一入口 | 供 `graph/nodes/plan_node.py` 呼叫，node 本身不直接碰觸上述任何細節 |

---

## 十、與 LangGraph 整合

`plan` 讀 `module_list`／`python_structure`，寫回 `task_list`。**`plan` 是平行分支 node，不是純線性 node**：`plan`／`scaffold`（④）同以 `record_tests`／`design` 為共同前驅（01 五章），兩者都完成才觸發 `implement`。回傳值只能包含自己實際更動的 key（`task_list`），不能用 `{**state, ...}` 整包展開——理由同 `design`／`scaffold`（見 05a 十一章、01 五章「fan-in 不需要 reducer 的前提」）。

---

## 十一、錯誤處理範圍

比照 04a 九章／05a 十二章的邊界：`retry_count` 迴圈只包住 `implement → run_tests → debug`，`plan` 不在這個迴圈裡。

- 四章 module 歸屬反查失敗（③輸出違反自己的檔名格式承諾）：直接往上拋，中止整條 run——這是輸入端問題，不是可以重試化解的暫時性錯誤
- 五章 Claude 呼叫失敗：先走「待重試清單、5 分鐘後重試一次」緩衝；仍失敗中止整條 `plan` run（見五章）
- 八章涵蓋率驗證失敗：直接拋出中止——代表 [P] 自己的組裝邏輯有 bug，不是可以重試化解的情況

---

## 十二、待決定事項

- [ ] 五章 LLM 判斷「函式呼叫關係」的 prompt 設計與品質，待接上真實③輸出後校準——尤其 `routers` 層 `class_name=None` 時，LLM 能否穩定從 `function_name`／描述語意配對回正確的 Java method，需要真實案例驗證
- [ ] 七章「`routers` 層一律納入 `schemas/{module}.py`」是否過度保守（每個 router task 都多帶一個檔案）：若目標專案 schema 檔案體積偏大，可能需要改成依 `referenced_interfaces` 精算實際用到的 schema class，屬於效能／成本 vs. 保守精準度的取捨，待接上真實專案規模評估
- [x] ~~③輸出若違反「同 class／檔案內函式名稱唯一」的隱含假設，三章的 1:1 映射與四章反查會失效~~——**已在 05a／05b 上游修正，不是 [P] 該擋的事**：Python 不支援多載，若③對兩個 Java 多載方法各自產出同一檔案／同一 class 下同名的 `InterfaceSpec`，本質是③輸出的正確性缺陷（連④要建立骨架都會撞名，不只是 [P] 的識別鍵歧義），不該留給下游每個消費者各自防禦。05a 四章現已明訂：③對同一組多載依宣告順序消歧，第一個保留原名，第二個起加 `_2`／`_3`……（比照 04a 三章 springdoc `voice`／`voice_1` 前例），`05b` `_build_method_contexts()` 已實作並驗證（見 05b 七章「已驗證」）。[P] 因此可以放心信任 `python_structure.interfaces` 內 `(file_path, class_name, function_name)` 三元組唯一——`06b` 仍值得保留一道機械檢查當 defense-in-depth（理論上不該觸發，觸發代表 05a 的消歧邏輯本身出了 bug，而不是正常會發生的輸入情況），但不再是「用來擋一個已知會發生的上游缺陷」，純粹是最後一道防線
- [x] ~~05a 十三章「router 層方法目前沒有任何管道把 HTTP method／路徑帶給④」的缺口，是否該由 [P] 把 `api_to_python_target` 的 HTTP method／路徑塞進 `task.context` 來補~~——**評估後確認不該由 [P] 補，這不是 06a 的範圍，也解不了問題**：`@router.get(...)` 裝飾器屬於函式**骨架**的一部分，由④（`generate_scaffold()`）產生；`task_list` 只餵給⑤的 `fill_function()`，只填函式本體、用 AST 插入，不碰裝飾器或簽名。更根本的是 00 一章的流程圖：`[P]` 與 `④` 是**平行分支**，兩者都只依賴③的輸出、互不依賴，[P] 執行當下不知道④在做什麼，[P] 的輸出也從不流向④。即使把 HTTP method／路徑寫進 `task.context`，這筆資料要到⑤才會被讀到，但④早已把骨架（含裝飾器有無）定案，時間點上也救不了；⑤更沒有管道去改裝飾器。缺口確實存在於③→④這條資料路徑（`generate_scaffold(python_structure)` 目前不吃 `api_to_python_target`），05a 十三章已經指向 07a／08a 才是正確的修復位置，維持原判，不需要 06a 二章讀 `api_to_python_target`

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
