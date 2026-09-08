# [P] Plan Agent 詳細設計

> 本文件承接 `00_refactor_architecture.md` 的整體架構（見七、[P] Plan Agent 一節；八、task list 欄位定義），是這個 Agent 的**設計面**文件：決策、契約、資料結構、流程。實際程式碼實作見 `06b_plan_agent_code.md`；本文件不出現可執行的實作邏輯。
>
> **本輪修訂**：依 `refactor_plan.md`（分層分階段＋全域關卡設計）更新。核心變化：[P] 原本呼叫 Claude API 產生 `description`／`context`／`referenced_interfaces` 語意摘要的職責**整個拿掉**——那正是 `refactor_call_chain_implement_prompt.md` 與 `refactor_plan.md` 一章要消除的失真來源（⑤ 現在直接讀「呼叫鏈 + 完整原始碼」，不吃 [P] 生成的摘要）。[P] 從「產生內容」降級成「機械打標籤＋組裝」，不再呼叫 Claude API（見五章）；但新增一項機械職責——**呼叫鏈範圍查找**（六章）：⑤ 需要知道「這個函式該參照哪些其他函式的真實原始碼」，這個「看哪裡、看多少」的範圍界定由 [P] 用①既有的呼叫圖機械算出，⑤ 只負責把界定好的目標實際讀出來、組進 prompt——「規劃範圍」與「執行讀取」是兩個各自單純的職責，不疊在同一步做。

---

## 一、本文件範圍與定位

**本文件涵蓋**：
- [P] 的輸入來源（① `module_list` ＋ ③ `python_structure` ＋ `java_project_path`，見二章）
- task 的產生單位與涵蓋率保證機制（三章）
- module／layer 歸屬判定（四章）
- `phase`／`translator_backend` 機械標記、`context` 的新定位（五章）
- 呼叫鏈範圍查找：`reference_targets` 組裝（六章）
- `task.depends_on` 組裝演算法（七章）
- `target_files` 組裝規則（八章）
- 輸出資料如何對應 `RefactorState.task_list`（九章）

**本文件不涵蓋**：
- 實際程式碼——見 06b
- ⑤ 如何把 `reference_targets` 實際讀成原始碼文字、組成呼叫鏈 context、如何消費 `task_list`、`ModuleScheduler` 排程細節（含六章「三層全域關卡」的實際執行機制）——見 `01_langgraph_architecture.md` 六章、`09a_implement_agent_architecture.md`、`refactor_plan.md` 一章
- `route_to_file_mapping`——由③產出並直接寫入 `config/harness.yaml`，見 `05a_design_agent_architecture.md` 八章，[P] 不重複這件事

---

## 二、輸入與前置資料

| 輸入 | 來源 | 說明 |
|---|---|---|
| `module_list` | State，①產出 | `module.summary`／`depends_on`／`methods`（`java_method`／`class_name`／`description`／`complexity`）——業務語境來源，**不是**涵蓋率權威來源，見三章 |
| `python_structure.interfaces` | State，③產出 | `file_path`／`class_name`／`function_name`／`params`／`return_type`／`phase`／`java_method_id`——[P] 拆 task 的權威輸入，見三章 |
| `python_structure.java_index` | State，③產出 | `java_method_id → {file_path, class_name, function_name, phase}` 對照表，供六章呼叫鏈查找把①呼叫圖裡的 Java callee 轉回 Python 對應資訊，見 `05a_design_agent_architecture.md` 對應章節 |
| `java_project_path` | State（`RefactorState.java_project_path`） | 六章呼叫鏈查找需要重用①既有的 `parse_agent.call_graph.parse_java_project()` 取得呼叫圖，見六章 |

**不需要** `openapi_spec`／`api_to_python_target`：API 契約層級的判斷已由③消化進 `python_structure`（見 05a 五章「API 邊界方法改用 openapi_spec 覆寫」），[P] 不重新碰觸這個上游輸入——呼應 00 二章的分工原則，不重複判斷已經判斷過的事。

**`java_project_path`／呼叫圖的取用範圍刻意收斂到「查表」，不是「理解 Java 語意」**：[P] 重用①的呼叫圖純粹是為了六章的機械圖走訪（誰呼叫誰的事實），不解析、不抽取任何 Java 原始碼文字本身——「抽取 Java 方法完整原始碼」這件事仍然整個歸⑤（見 `refactor_plan.md` 一章「新增能力與歸屬」），[P] 不做這件事，維持「[P] 不產生會失真的中間表示、只做機械查表」的核心原則不變。

---

## 三、處理單位：以 `python_structure.interfaces` 為權威輸入，不是 `module_list.methods`

00 八章要求 task list「以①輸出的完整方法清單為準」。但①的 `MethodInfo` 與③的 `InterfaceSpec` 之間**沒有形式化的對應欄位**：04a 六章明講「① 不產出任何 Python 命名建議欄位」；③內部用 `camelCase → snake_case` ＋ 私有方法底線前綴建立 `java_method → function_name` 的對應（見 05a 七章、05b 實作），但這個中繼結果過去是 `design_agent` 套件內部的暫存資料、即用即丟——**本輪修訂後不再即用即丟**：③現在把這個對應關係機械輸出成 `InterfaceSpec.java_method_id`／`python_structure.java_index`（見二章、05a 對應章節），六章的呼叫鏈查找需要用到它。

**決策：[P] 產 task 的基礎單位是 `python_structure.interfaces`，不是 `module_list.methods`**，理由：

1. **涵蓋率已經由③保證過一次**：05a 七章訂為強制規則——`interfaces` 必須涵蓋 `module_list` 裡每一個 module 的每一個方法。[P] 若改用 `interfaces` 當基礎，直接繼承這個已驗證過的涵蓋率保證，不需要重新比對 Java 方法名稱這種脆弱的字串匹配。
2. **`interfaces` 才是實際要填空的對象**：translator-cli 的 `fill_function()`（00 七章⑤）操作的是③＋④已經建好骨架的 Python 函式，不是 Java 方法本身——一個 Java 方法可能被③拆成一個以上的 Python 函式，也可能因框架慣例參數（05a 七章）被加了額外參數，`interfaces` 是這些變化後唯一權威的下游狀態。

**涵蓋率規則**：每一個 `InterfaceSpec` 恰好對應一個 task（1:1），不聚合、不拆分。這讓涵蓋率檢查完全機械化——task_list 產出後，比對是否每個 `InterfaceSpec` 都被恰好一個 task 認領（見九章）。這條規則同時回答了 `refactor_plan.md` 六章「[P] 對 Phase 1 的 task 產生邏輯是否完全不需要 LLM」的待決定事項：**不需要**——Phase 1（entity/dto/repository/utils）的每一個 `InterfaceSpec` 一樣是恰好一個 task，不需要額外的語意判斷來決定怎麼拆。

**全域基礎設施檔案與資料類別排除**：`app/main.py`／`app/core/database.py`（05a 三章「全域基礎設施檔案」）、`schemas/{module}.py`（Pydantic model 欄位）、`models/{module}.py`（SQLAlchemy ORM，00 七章交給④生成）都不產生 `InterfaceSpec` 條目，因此天然不會出現在 task_list 裡，不需要 [P] 額外過濾。

---

## 四、module／layer 歸屬判定（機械）

`InterfaceSpec` 沒有 `module` 欄位，需要從 `file_path` 反查；`layer` 同樣沒有獨立欄位，`file_path` 本身就編碼了層級資訊。這兩者由同一個判定函式一次算出——`layer` 是五章 `translator_backend` 規則與七章 `depends_on` layer baseline 的共同輸入，不是各自重新推導。

### 一般規則：`{module}_{layer_singular}.py`

05a 三章訂死的檔名規則：`file_path` 為 `app/{layer}/{module}_{layer_singular}.py`（`layer` ∈ `routers`／`services`／`repositories`），`module` **逐字沿用** `ModuleInfo.module`，不額外轉換大小寫。

**演算法**：取 `file_path` 檔名（去掉副檔名），依 `_router`／`_service`／`_repository` 三個固定後綴之一去掉後綴得到 `layer` 與候選 `module` 字串，`module` 字串直接比對 `module_list` 的 `module` 集合確認命中——檔名本來就是③逐字拼接 `ModuleInfo.module` 產生，這裡是逐字反查，不是猜測，理論上必然精確命中。

### 特例一：`app/utils/*.py`（Utils，不屬於任何業務 module）

05a 三章「Utils 特例」、`refactor_plan.md` 二章：utils 不套用 `{module}_{layer}.py` 規則，直接複製 Java package 結構（`app/utils/{class_name_snake}.py`），因為一個 utils class 經常橫跨多個業務 module（如 `ValidationUtil` 同時被 `ExamController`／`GradingController` 使用），套用 module 前綴會出現「這個 util 該算哪個 module」的假問題——這正是 05a 選擇不加前綴的原因。

後果是：`file_path` 完全不編碼 module 資訊，上面的一般規則對 `app/utils/` 開頭的路徑必然無法命中。**新增規則，比照 05a 十四章 `_global` 保留模組的既有先例**：`file_path` 落在 `app/utils/` 底下時，直接回傳固定的保留 module 名稱 `("_utils", "utils")`，不進入一般規則。`_utils` 底下的 task 彼此間沒有跨層依賴（`layer` 只有 `utils` 一種），也沒有 `module_list.depends_on` 可循——不是真實業務 module，只是排程與 `task.module` 欄位需要一個歸屬值時的容器，語意等同「這批 task 不屬於任何業務 module，但仍需要一個 module 分組供 `ModuleScheduler` 追蹤完成狀態」。已對真實 `lang-exam-api-refactor` 專案跑過完整 ①→③→[P]，確認這個規則成立（見 06b 三章「真實環境驗證」）；`_utils` 在 `ModuleScheduler`／局部驗證機制裡該怎麼對待仍是未定案項目，見十三章。

### 特例二：`app/core/exception_handlers.py`（`_global` 保留模組）

09b 端對端整合測試發現這個檔案不符合 `{module}_{layer}.py` 命名慣例（見 04a 十一章、05a 十四章）。`classify()` 在一般規則之前新增精確比對：命中這個固定路徑直接回傳 `("_global", "routers")`——`layer` 選 `"routers"` 純粹是語意上最接近（`class_name=None` 自由函式、在 `main.py` 被機械註冊）。

### 找不到對應時

排除上述兩個特例後，仍找不到 `_router`／`_service`／`_repository` 後綴、或比對不到任何 module → 直接中止，交由人工核對③的輸出（代表③違反自己承諾的檔名格式）。

### 與 02a 十三章「module 詞彙一致性」的關係

一般規則反查出的字串就是 `ModuleInfo.module` 本身，[P] 沒有發明新字串——`task.module` 必須逐字等於 `ModuleInfo.module`，這是 `01_langgraph_architecture.md` 六章 `ModuleScheduler` 用 `module` 當 dict key 分組任務、比對 `module_list.depends_on` 的結構性前提。`_utils`／`_global` 這兩個保留名稱不落在 `ModuleInfo.module` 集合裡，`ModuleScheduler` 需要知道它們沒有 `depends_on` 可循、也不需要等待任何上游 module（見十三章待決定事項）。

---

## 五、`phase`／`translator_backend` 機械標記；`context` 的新定位

### 為什麼這裡不再是「LLM 設計階段」

舊版此章是 [P] 對每個 module 呼叫一次 Claude API，把 Java method 的業務描述、跨函式呼叫關係整合成 `description`／`context`／`referenced_interfaces` 三個欄位，供 translator-cli 當作填空依據。`refactor_call_chain_implement_prompt.md`（上一輪 session 交接）與 `refactor_plan.md` 一章明確指出：**這個 LLM 生成的語意摘要正是失真來源**——⑤ 不該吃這種中間表示法，該直接讀「呼叫鏈 + 完整 Java 原始碼」（Phase 1 已完成的部分讀真實已翻譯 Python，其餘讀真實 Java 原始碼）。抽取 Java 方法完整原始碼、把六章界定出的範圍實際讀成文字，這件事歸⑤，不歸 [P]（見 `refactor_plan.md` 一章「新增能力與歸屬」、00 七章「⑤ 新增兩項職責」）。

[P] 因此**不再呼叫 Claude API**，這裡剩下的只有兩個純機械標記：

### `phase`：直接複製 `InterfaceSpec.phase`

③（`design_agent`）已依 05a 三章、`refactor_plan.md` 二章的機械規則算好每個 interface 的 `phase`（`@Repository`／utils package → 1；entity/dto → 1（既有機制）；`@Service`／`@RestController` → 2；`_global` 保留模組固定 2，見 05b `_design_global_advice_module()`）。[P] 原樣讀取 `InterfaceSpec.phase` 寫進 `TaskSpec.phase`，不重新判斷、不需要 fallback——`InterfaceSpec.phase` 由③保證每一筆都有值。

### `translator_backend`：依 `layer` 機械決定

`layer` 由四章的 module／layer 歸屬判定一併算出。規則（`refactor_plan.md` 三章「雙後端分工」，實測依據見該章五章）：

| `layer` | `translator_backend` |
|---|---|
| `repositories` | `"qwen"` |
| `routers`／`services`／`utils`（含 `_global`、`_utils`） | `"claude"` |

只有 repository 交給 qwen——實測 8/8 PASS 零缺陷（含 native SQL、JPQL 聚合、三種回傳形狀）；model/entity 4/4 出現不同類型的 fatal 錯誤；utils 多數正確但有 3 個非 fatal、僅邊界輸入才現形的邏輯 bug（風險隱蔽，不對稱地不划算）；controller/service 未直接測試，但性質與 model/entity 相近，一併歸入 Claude API。entity/dto 本身不產生 `InterfaceSpec`（③機械渲染／④直接生成，見三章「全域基礎設施檔案與資料類別排除」），不會走到這條分派規則。

這兩個標記純粹是 dict 查表，不涉及任何語意判斷，[P] 因此不需要 `prompts.py`／`llm.py`，不呼叫 `common/llm_client.py`——比照 `refactor_plan.md` 六章「傾向不需要 LLM」的待決定事項，這裡正式定案：**不需要**。

### `description`：改為機械模板字串，不含業務語意

舊版 `description` 是 LLM 整合 Java 業務邏輯後的任務描述。新設計下這個角色被拿掉——⑤ 實際看到的是真實原始碼本體，不需要另一層文字描述來源業務邏輯，寫得越詳細反而越接近「重新引入一層可能失真的摘要」。`description` 因此改為純機械模板，只供 log／除錯追蹤時人眼辨識用途，**不送進 fill_function() 的 prompt**（對模型沒有任何有效信號，繼續塞進 prompt 只是雜訊，見七a_translator_cli_architecture.md）：

```
填入 {file_path} 的 {class_name}.{function_name}()
```

**只能用 `InterfaceSpec` 本身的欄位**（`file_path`／`class_name`／`function_name`），不能用 Java 方法名稱——二章、三章已經講過①的 `MethodInfo.java_method` 跟③的 `InterfaceSpec.function_name` 之間沒有形式化的對應欄位（`java_method_id` 是給機械查表用的識別碼，不是給人讀的方法名稱，不適合直接嵌進這個給人看的字串裡）。`class_name` 為 `None`（`routers`／`utils` 層既有慣例）時省略類別前綴，只寫 `填入 {file_path} 的 {function_name}()`。

### `context`：角色限縮為「呼叫鏈涵蓋不到的機械補充事實」

舊版 `context` 是 LLM 生成的補充依賴關係／邊界條件文字。新設計下，⑤ 的呼叫鏈展開已經涵蓋了「這個函式依賴哪些其他函式、怎麼呼叫」——`context` 不再需要重複這件事，角色限縮成只裝**呼叫鏈原始碼展開涵蓋不到、但 [P] 能機械算出的補充事實**。目前唯一已知的內容：

**`config_field_mappings` 折進 `context`**（對應 `docs/09b_bug_trace.md` #45，沿用舊版既有機制，機械性質不受本輪修訂影響）：`python_structure.config_field_mappings`（③ `design_agent.global_infra` 機械組出，見 `05a_design_agent_architecture.md` 十五章）記錄「哪個 Python 檔案的哪個 Java 欄位是 `@Value("${key}")` 注入、對應哪個環境變數常數」。這件事呼叫鏈原始碼展開接觸不到——Java 原始碼裡讀到的是 `this.xxx`，看不出這個欄位在 Python 端已經被④渲染成 `app/core/config.py` 的一個常數，需要 [P] 額外把這個事實寫進 `context`，讓⑤翻譯到這段讀取時有明確依據，不會幻覺出從未存在的型別（見 #45 實際案例）。

**組裝時機**：九章組出每個 task 時，額外檢查 `target_files[0]` 是否在 `config_field_mappings` 有對應項目——有就機械附加一段固定格式的提示文字；沒有則 `context` 為空字串。純機械字串附加，不呼叫 LLM——property key 到常數名稱的對應在③階段就已經是決定性事實，沒有語意判斷空間。

**真實環境驗證**：對真實 `GeneralController.java`（`language.code`／`language.displayName` 兩個 `@Value` 欄位）跑過完整真實 `plan_all_modules()`，確認對應 task 的 `context` 正確附加：「這個類別有 Spring @Value 屬性注入欄位，`code` 對應 `app.core.config.LANGUAGE_CODE`；`displayName` 對應 `app.core.config.LANGUAGE_DISPLAY_NAME`（環境變數注入，已由 ④ 生成，見 app/core/config.py）。業務邏輯中原本讀取這些欄位的地方，請改成引用對應的常數，不要臆測其他來源（如框架 bean）。」

未來若出現其他「[P] 能機械算出、但呼叫鏈原始碼展開接觸不到」的事實，比照這個模式擴充；沒有這類事實的 task，`context` 維持空字串，不強行塞入內容。

---

## 六、呼叫鏈範圍查找：`reference_targets` 組裝（機械圖走訪）

### 為什麼是 [P]，不是⑤

`refactor_plan.md` 一章「呼叫鏈規則」要求⑤翻譯一個函式時，能正確判斷它呼叫到的其他函式該讀真實 Java 原始碼還是真實已翻譯 Python——但「同層呼叫直接展開 Java 原始碼、遞迴下去」這條規則若不設界限，一個 `ServiceA → ServiceB → ServiceC → Repository` 的呼叫鏈會一路展開，context 大小完全不可控（`docs/09b_bug_trace.md #37` 已經證實這個問題會真實發生）。「該看多深、看多少」是需要**事先規劃、統一控制**的事，不該讓每個 task 在翻譯當下各自臨場決定、行為不一致——這正是「規劃」的定義，也是 [P]（Plan Agent）名稱本身暗示的職責。

因此**範圍界定**（看哪些 Java 方法、界線畫在哪）由 [P] 做，**實際讀取**（把界定好的目標讀成原始碼文字）留給⑤——兩者切開的理由：
1. 界定範圍只需要①的呼叫圖（`method_id → method_id` 的事實關係）＋③的 `java_index`（Java method_id → Python 對應資訊），是純粹的圖走訪＋查表，不需要碰任何原始碼內容，[P] 現有的「不讀 Java 原始碼、只碰結構化事實」邊界不需要因此打破。
2. 若讓⑤自己重新做這個範圍判斷，等於在每個 task 翻譯當下重複同一套圖走訪邏輯，且沒有一個統一的地方能套用大小上限——分開之後，「界定範圍」只需要做一次（[P] 執行時），「讀取內容」才是⑤逐 task 執行的事。

### 呼叫鏈規則：三層全域關卡（`refactor_plan.md` 一章）

**Phase 2 內部再切一刀**：⑤ 的執行順序不是「Phase 1 → Phase 2」兩段，是「Phase 1（repo/model/dto/utils）→ service → controller/router」三段，段與段之間是全域硬性關卡（全部 module 的前一段都做完，才釋放任何 module 的下一段，見 `refactor_plan.md` 一章）——這一層新增的關卡，比對每個 task 各自算「呼叫關係要不要排序」（曾經評估過用呼叫圖＋環偵測做這件事，見下方「為什麼不做更精細的排程」）簡單、也更不容易出錯：controller 本來就幾乎只呼叫 service（Spring MVC 慣例上 controller 是入口、不會被其他 controller 呼叫），一旦 service 全部做完，controller 呼叫到的對象保證已經是真實翻好的 Python，不需要遞迴、不需要排程判斷。

`layer` 由四章的判定算出（`repositories`／`services`／`routers`／`utils`），呼叫鏈規則直接比較 caller／callee 的 `layer` 是否為同一層：

| 情境 | 規則 |
|---|---|
| callee 的 `layer` 比 caller 更基礎（如 caller=`routers`／callee=`services`；或 caller 在 Phase 2、callee 在 Phase 1） | 標記為 `language="python"`，指向已翻譯 Python 檔案的對應函式，**不繼續往下展開**這個 callee 自己的呼叫——全域關卡已經保證它翻完了 |
| callee 與 caller 的 `layer` 相同（`services` 呼叫 `services`，理論上也包含 `routers` 呼叫 `routers`，或 Phase 1 內部 `repositories`/`utils` 互相呼叫） | 標記為 `language="java"`，**遞迴展開**這個 callee 自己的呼叫（受下方上限約束） |

**同層 Java 參照的用途是「讓⑤知道這個函式做什麼、該怎麼呼叫它」，不是要⑤把它的邏輯合併進來**：⑤翻譯出的 Python 程式碼，呼叫到已完成的 callee 或同層函式時，都應該是**真正呼叫那個函式**（`from app.repositories.user_repository import get_by_id`／呼叫同 module 內另一個 service 方法），比照 Java 原始碼本來就是這樣呼叫的——不是把被參照函式的邏輯抄一份貼進來。`reference_targets` 提供的原始碼是給⑤「看懂該怎麼正確呼叫」（參數、回傳型別、行為），不是給它「拿去合併改寫」。這一點在⑤實際組 prompt 時要明確告知模型，見 `07a_translator_cli_architecture.md`。

**為什麼不做更精細的排程（呼叫圖依賴排序＋環偵測）**：曾經考慮讓 [P] 對每個 task 直接算出「同層呼叫的真實依賴關係」（誰呼叫誰、依此排出 `depends_on`，讓更多同層呼叫也能命中「讀已翻譯 Python」），但比較過後放棄——這套機制需要處理環（A 呼叫 B、B 也呼叫 A，Java 服務層互相委派是真實會出現的形狀），環偵測若寫錯，換來的是排程器真正的死結（某個 task 永遠排不進 ready queue），這比「多讀了幾層 Java 原始碼、或極端情況被截斷」嚴重得多、也更難在測試階段抓到。三層全域關卡（消掉 controller 呼叫 service 呼叫 repository 的深呼叫鏈）加上既有的 module 依賴排程（消掉跨 module 的同層呼叫，見下方「與既有 module 依賴排程的關係」）已經把絕大多數情況涵蓋掉，剩下「同 module、同層」這個很窄的殘餘範圍，用最簡單的「讀 Java、遞迴、上限保底」（見下方）處理，複雜度不值得為了這個邊際收益去換環偵測的風險。

### 演算法

1. **種子**：這個 task 自己的 `InterfaceSpec.java_method_id`，連同它自己的 `layer`（四章已算好）。
2. **一次性重用①的呼叫圖**：[P] 對 `java_project_path` 呼叫一次 `parse_agent.call_graph.parse_java_project()`（見二章，執行一次、所有 task 共用同一份結果，不逐 task 重算），取 `.call_graph`（`method_id → 直接呼叫的 method_id 清單`，見 04a 三章）。
3. **BFS 走訪**（用佇列而非遞迴函式呼叫，避免 Java 語言層級的深度遞迴撞到 Python 呼叫堆疊限制）：從種子出發，對每個彈出的 `method_id` 查 `call_graph` 取得直接呼叫對象；每個呼叫對象查 `java_index`：
   - **查不到**（不是任何 `InterfaceSpec` 對應的 Java 方法）→ 略過，不產生 `reference_targets` 項目，也不繼續展開（沒有下游可查）。**最常見的情況是 entity／model 的欄位存取（如 `user.getName()`）——entity 從一開始就不會產生 `InterfaceSpec`（③遇到 `@Entity` 直接跳過，見 05a 三章），`java_index` 只收有 `InterfaceSpec` 的方法，這裡查不到是預期行為，不是遺漏**：entity 的完整欄位定義已經由八章「target_files 組裝」無條件整份帶給 services／repositories 層的 task（`models/{module}.py`），⑤ 看得到完整定義，不需要六章這套「精準抽取單一函式」機制再處理一次——欄位存取不是函式呼叫，沒有「參照哪個函式」的問題。
   - **查到、`layer` 比目前節點更基礎**（依「`repositories`／`utils` < `services` < `routers`」全域順序比較，Phase 1 內部視為同一層級不特別再細分）→ 加入 `reference_targets`（`language="python"`），**不繼續展開**這個節點自己的呼叫——三層全域關卡保證。
   - **查到、`layer` 與目前節點相同、且屬於同一個 module** → 加入 `reference_targets`（`language="java"`），**繼續展開**（把它的直接呼叫對象加進佇列，用它自己的 `layer`／`module` 繼續套用這整套規則）——同 module 同層沒有任何機制保證彼此的完成順序，只能退回讀 Java 原始碼，範圍見下方「與既有 module 依賴排程的關係」。
   - **查到、`layer` 與目前節點相同、但屬於不同 module** → 查 `module_list`：若目前節點所屬 module 的 `depends_on`（含遞移）包含 callee 所屬的 module，代表 `ModuleScheduler` 既有的 module 級依賴排程已經保證 callee 的 module 會先完成 → 加入 `reference_targets`（`language="python"`），**不繼續展開**；沒有這層依賴關係（無關 module、或方向相反）→ 視同「同 module 同層」處理（`language="java"`，繼續展開）。見下方「與既有 module 依賴排程的關係」。
   - **查到、`layer` 比目前節點更後面**（如 repository 呼叫到 service——不符合正常分層架構，理論上不該發生）→ 保守視為同層規則處理（同 module 或跨 module 判斷方式同上），不假設一定不會發生，也不特別報錯。
   - 用 `visited` 集合排除重複造訪（避免同層方法互相呼叫、或遞迴呼叫造成無窮迴圈）；callee 的 `java_method_id` 與目前節點相同（直接遞迴，方法呼叫自己）時直接跳過，不產生項目——⑤翻譯當下本來就看得到自己的簽名，不需要額外參照。

### 與既有 module 依賴排程的關係

`ModuleScheduler` 既有機制（`module_list.depends_on`，①算好的真實模組依賴關係）保證「module X 依賴 module Y 時，Y 的工作先於 X 完成」——這條規則不是這輪新增的，本來就存在。**這裡延伸的地方是：這個保證現在要在三層全域關卡的每一層內部都生效**，不能只在「整個 module 全部完成」才檢查——例如 X／Y 兩個 module 都進入 service 階段後，若 X 的 service 呼叫到 Y 的 service，需要 `ModuleScheduler` 保證 Y 的 service task 在 X 的這個 service task 之前完成，不是等到 Y 的 controller/router 也做完（那要等到全域第二層關卡才會發生，時機太晚）。這是 `graph/scheduler.py` 09a 落地時要實作的部分，[P] 這裡只負責在算 `reference_targets` 時查 `module_list` 判斷「能不能安全假設 Y 已經做完」，不負責讓這件事在執行期真的成立，見十三章。

**因此真正沒有任何機制保證順序、只能退回讀 Java 原始碼的情況，收斂到一個很窄的範圍：同一個 module 內、同一層互相呼叫**（如同一個 `exam` module 裡 `ExamService` 呼叫 `AnswerService`，或呼叫自己 class 裡的另一個方法）——跨 phase、跨層有全域關卡，跨 module 同層有既有的 module 依賴排程，都不需要遞迴讀 Java。
4. **上限**：`reference_targets` 累積數量達到 `PLAN_AGENT_MAX_REFERENCE_TARGETS`（環境變數可調，預設 `20`）時停止整個 BFS——不是「這一輪展開完再停」，是查到會讓清單超過上限的那個節點就直接不加、不再繼續彈佇列。BFS 保證越早加入的節點是呼叫鏈上越淺層、離目前 task 最近的節點，達到上限時捨棄的必然是最深層、最邊緣的節點，不是隨機丟棄——⑤ 拿到的一定是「離這個函式最近的 N 個參照」，不會發生「留了一個很遠的、卻漏掉一個很近的」這種不合理情況。三層全域關卡＋既有 module 依賴排程已經把絕大多數情況消掉，這個上限現在只是殘餘的「同 module、同層互相呼叫」情境的保底，預期觸發頻率遠低於當初的估計。

### 上限被觸發時：截斷可見化，不是靜默降級

`reference_targets` 被截斷後，這個 task 依然正常產生、正常送去翻譯——**不中止、不整條 run 失敗**，理由跟 04a／05a 的 `skipped_interfaces`／`skipped_db_models` 同一種精神：這是「這次沒能完整涵蓋，需要留意」的訊號，不是「壞掉了」。但截斷這個事實不能只留在一行 log 裡就沒了下文——log 在一次真實 pipeline 執行裡可能有幾千行，沒有人會逐行翻，等到這個函式的翻譯品質出問題（很可能正是因為漏看了被砍掉的那段呼叫鏈）才要回頭查「這個 task 當初是不是被截斷了」，翻 log 找不到等於没有留下這個線索。

**決策：`TaskSpec` 新增 `reference_targets_truncated: NotRequired[bool]`**，只有真的被截斷時才設為 `True`（未截斷的 task 不設這個 key，比照 `InterfaceSpec.phase` 等既有欄位的 `NotRequired` 慣例，「缺席」本身就代表「沒有這回事」，不需要额外一個 `False` 分支）。這個布林值直接跟著 `task_list` 一路往下傳——`task_list` 是 `RefactorState` 既有欄位，任何下游（人工事後檢視、未來 09a／⑦ Debug Agent 想知道「這個函式翻譯品質可疑，是不是因為呼叫鏈被砍過」）都查得到，不需要另外去翻 log 檔案時間戳比對。

**只留布林值，不留「砍掉了幾個」的精確數字**：BFS 在達到上限的當下就整個停止，佇列裡剩下多少沒展開的節點只反映「還沒探到的部分」，不是「總共漏掉多少」的精確值（要探到底才知道真正的總數，但那正是設上限想避免的事）。精確數字容易誤導使用者以為它是可信賴的統計，不如老實只給「有沒有被截斷」這個確定的事實。

**仍然記一筆警告**（`task_id`／`java_method_id`／目前累積筆數），供開發時查 log 用，但這是**輔助**，`TaskSpec.reference_targets_truncated` 才是這個訊號的**權威落地位置**——兩者不是二選一，警告是給當下盯著 log 的人看，結構化欄位是給之後（可能隔了很久、甚至不同的人）回頭查的人看。

**這個上限值（`20`）本身還沒有真實資料佐證，是否需要調整見十三章**——這裡只定案「截斷發生時系統該怎麼反應」，不是「20 這個數字對不對」，兩者是不同層次的問題。

### 輸出：`TaskSpec.reference_targets`／`reference_targets_truncated`

```python
class ReferenceTarget(TypedDict):
    file_path: str      # language="java" 時是 Java 檔案路徑；language="python" 時是已翻譯 Python 檔案路徑
    class_name: str | None  # language="java" 時是 Java class 名稱；language="python" 時是 Python class 名稱
    function_name: str  # language="java" 時是 Java 方法名稱（camelCase）；language="python" 時是 Python function_name（snake_case）
    language: Literal["java", "python"]
```

**三個欄位的來源依 `language`分岔，不是都從 `java_index` 查出來的值**：`java_index` 的 value（`JavaIndexEntry`）存的是③已經投影過的 Python 側事實（`InterfaceSpec.file_path`／`class_name`／經 `camel_to_snake()` 轉換過的 `function_name`，見四章「`description`：改為機械模板字串」對應的命名慣例）——這對 `language="python"` 的項目正好是需要的內容（⑤要拿去讀已翻譯的 `.py` 檔案），但對 `language="java"` 的項目是錯的：⑤要拿這個座標去解析真實 `.java` 檔案，需要 Java 原始的 camelCase 方法名與 Java class 名稱，不是 Python 投影後的名稱。演算法步驟 3 因此在組裝 `language="java"` 項目時，直接拆呼叫圖裡的 `callee_id`（本身就是①的 `method_id()` 格式，"{java_file_path}::{java_class_name}::{java_method_name}"）取得這三個 Java 原始欄位，不查 `java_index` entry——這是實作 `graph/java_source_extraction.py`（09a 對接，見 07a）時發現的真實落差，兩者命名慣例通常不同（如 `getUserAnswer` vs `get_user_answer`），單元測試用 java／python 兩側巧合同名的 fixture 資料一開始沒能測出來，見 `tests/plan_agent/test_call_chain.py::test_java_language_target_uses_java_names_not_python_projection`。

只有座標與語言標記，**沒有原始碼文字**——把座標實際讀成文字（javalang 抽取 Java 方法本體，或讀已翻譯 Python 檔案的對應函式）是⑤的工作，見 `refactor_plan.md` 一章「新增能力與歸屬」、`07a_translator_cli_architecture.md`。`reference_targets_truncated` 見上方「截斷可見化」。

### `_utils`／`_global` 的呼叫鏈查找不特殊處理

演算法本身不區分 module／保留 module——`_utils`／`_global` 底下的 task 一樣有真實的 `java_method_id`（見 05a 對應章節），一樣可以正常做這個查找。utils 方法之間互相呼叫（如 `CollectionUtil` 呼叫 `ValidationUtil`，見 `refactor_plan.md` 五章實測案例）本來就是 Phase 1 內部同層呼叫，走「同層展開 Java」這條規則，不套用「讀已翻譯 Python」的捷徑——跟 `refactor_plan.md` 一章「Phase 1 內部呼叫...同樣直接展開 Java 原始碼，不套用讀已翻譯 Python 這個捷徑」的既有規則一致。

---

## 七、`task.depends_on` 組裝（機械層級排序）

00 八章：`depends_on` 只決定**同一 module 內**的執行順序，跨 module 順序由 `ModuleScheduler` 依 `module_list.depends_on` 保證（見 01 六章）。**另外疊加六章「三層全域關卡」**（`refactor_plan.md` 一章）：全部 module 的 Phase 1 都完成才釋放 service，全部 module 的 service 都完成才釋放 controller/router——這層關卡由 `graph/scheduler.py::ModuleScheduler` 負責，不由 `task.depends_on` 表達，[P] 不需要、也不應該在 `depends_on` 裡編碼跨關卡依賴。

### 排序目的的轉變：不再是 context 品質

舊版排序的理由是「context 品質」——被依賴的函式若已經填入真實邏輯，呼叫端 prompt 才能看到有意義的參照實作。**這個理由在新設計下不再成立**：⑤ 對呼叫鏈上的每個節點都直接讀真實原始碼（Java 或已翻譯 Python，見六章），不讀取「同伴函式是否已經翻譯」這件事，因此同 phase 內的執行順序不影響翻譯品質。真正需要保證順序的只有一個邊界——Phase 1 必須全部完成、Phase 2 才能開始，而這個邊界是全域關卡，不是 task 級 `depends_on` 能表達或需要表達的粒度。

**保留 `depends_on` 的理由變成純粹的排程決定性／可除錯性**：固定、可重現的執行順序比不確定的併發交錯更容易追蹤問題（同一份輸入重跑會產生同樣的排程結果），不是正確性要求。

### 機械規則（單層）

1. **layer baseline**：同 module 內，`repositories`／`utils` 層任一 task 一律先於 `services` 層任一 task；`services` 層任一 task 一律先於 `routers` 層任一 task。這條規則現在**整段**都已經被六章「三層全域關卡」涵蓋（Phase 1 全部做完才輪到 service，service 全部做完才輪到 controller/router），[P] 這裡的表達是冗餘但無害的——不影響正確性，純粹是同 module 內 `id` 編號／執行順序的決定性依據，不重複依賴外層關卡才能確定順序。
2. 同層內再依 `function_name` 字母序（`class_name` 為 tie-break）排定全序，`depends_on` 直接取全序中緊接在前一名的 task id（單一前置依賴即可表達完整順序，不需要每個 task 都列出全部前置 task）。

`_utils`／`_global` 這兩個保留 module：內部只有單一 `layer`（`utils`／`routers`），沒有跨層 baseline 可套用，僅依 `function_name` 字母序排定全序供 `id` 編號穩定（見九章），`depends_on` 可以留空——這批 task 彼此間沒有排程順序要求，可以完全平行執行。

### 待銜接：同檔案 task 的併發寫入（不在本章解決）

同一個檔案（`target_files[0]` 相同）常見有多個 task（同一個 router／service／repository 檔案裡的多個函式）。qwen 這條路徑本來就被 `graph/scheduler.py::MODEL_SEMAPHORE = asyncio.Semaphore(1)` 全域序列化（見 01 六章），天然不會有同檔案併發寫入的問題；但 Claude API 這條路徑（repository 以外的所有層）不受這個序列化限制，若排程允許同檔案的多個 task 併發執行，AST 插入同一份檔案是否需要額外的檔案級鎖，是尚未定案的執行期併發模型問題——**不是 [P] 產生 `depends_on` 這一步該解決的**，[P] 目前只保證同 module 內的邏輯排序穩定可重現。留給 09a／`graph/scheduler.py` 更新雙後端併發模型時一併定案，見十三章。

---

## 八、`target_files` 組裝（純機械）

`target_files` 只負責「哪些檔案要整份帶入」，跟六章「哪些函式要精準參照」是兩件不同的事——`target_files` 涵蓋的是**資料形狀定義**（schemas／models，沒有「函式」這個切分單位，只能整份帶入）與**自己要寫入的檔案**，`reference_targets` 涵蓋的是**呼叫鏈上的函式參照**（精準到單一函式，不整份帶入）。兩者互不重疊，合起來才是⑤ 這次呼叫需要的完整輸入。

| 組成 | 判定方式 |
|---|---|
| 自己的 `file_path` | 機械，必然存在，固定放 `target_files[0]`，整份帶入——translator-cli 執行期要在這個檔案裡定位並替換目標函式，不能只有片段 |
| 本 module 的 `schemas/{module}.py`（若存在） | 這個 task 屬於 `routers` 或 `services` 層（見四章判定）且該檔案存在（`directory_tree` 有這個檔案的 Schema 定義段，見 05a 三章格式慣例）時一律加入，不細究具體用到哪幾個欄位——呼應 04a／05a「多連、少排除」的保守精神；整份帶入，資料形狀定義沒有「函式」這個切分單位 |
| 本 module 的 `models/{module}.py` | 這個 task 屬於 `services` 或 `repositories` 層時一律加入，不做存在性判斷（`models/{module}.py` 出現在 `directory_tree` 目錄結構段，但只代表 mkdir 建了目錄，內容是否真的產出取決於④有沒有對應 DB 表，見 07a 十四章）；整份帶入，理由同上 |

**`utils`／`_global` 層**：不套用上面兩條 schemas／models 規則——utils 是純函式集合，不特定屬於任何一個 module 的資料形狀；`_global` 的例外處理函式簽名固定，同樣不需要額外的 schema／model 參考。`target_files` 對這兩層永遠只有 `target_files[0]` 自己。

**`models/{module}.py` 為什麼是無條件加入，不是比照 `schemas` 做存在性判斷**：`repositories` 層方法幾乎必然回傳／查詢 ORM entity，`services` 層則經常直接處理 repository 回傳的 entity，兩層都納入；`routers` 層不納入 `models`——router 層方法的 `params`／`return_type` 依 05a 五章「API 邊界方法改用 openapi_spec 覆寫」規則改用 `schemas`（Pydantic），不應該直接碰觸 ORM model。缺檔案（該 module 沒有對應 DB 表）的情況由 07a 七章「`context_files` 讀取容錯」承接（`FileNotFoundError` 記警告後跳過），不是 [P] 這一步該解決的事。

---

## 九、輸出格式與 State 對應

`run_plan_agent()` 回傳 `(task_list, module_list)` 兩個值。`task_list` 對應 `graph/state.py` 的 `TaskSpec`——新增 `phase`／`translator_backend`／`java_method_id`／`reference_targets`；`description`／`context` 角色改變（見五章）。

**`module_list`：補回 `_utils` 保留 module 之後的版本，取代①原始輸出**——真實環境發現 `graph/scheduler.py::ModuleScheduler` 用呼叫端傳入的 `module_list` 建構自己追蹤的 module 集合，`_utils`（四章特例一，utils 檔案路徑不帶 module 前綴的保留 module）只存在於 `task.module` 欄位上，①的 `module_list` 從來沒有對應條目——`ModuleScheduler.get_ready_tasks()` 因此永遠不會走訪到 `_utils`，這個 module 底下的 task 永遠排不進就緒佇列（已用真實 `lang-exam-api-refactor` 資料證實：12 個 `_utils` task，`get_ready_tasks()` 從頭到尾不回傳任何一筆）。`_global` 沒有這個問題——①（`parse_agent/summarize.py::_assemble_global_advice_draft()`）本來就會機械組一筆真正的 `ModuleInfo` 塞進 `module_list`；`_utils` 沒有對應的①端機制，因為它是這次 call-chain-implement 重構才新增的 [P] 端保留 module 概念。[P] 因此在九章組裝收尾時，只要 `task_list` 裡真的有落在 `_utils` 的 task，就補一筆對應的 `ModuleInfo`（`depends_on=[]`，比照 `_global` 既有先例——utils 是最基礎的層，不依賴任何業務模組完成才能開始實作），把補完的版本回傳給 `graph/nodes/plan_node.py` 寫回 `RefactorState.module_list`，取代①的原始版本；沒有 utils 類別的目標專案，`module_list` 原樣傳回，不會無中生有多一筆。

**`java_method_id`：這個 task 自己對應的 Java 方法識別碼**——逐字等於 `InterfaceSpec.java_method_id`（見 05a 對應章節），跟 `reference_targets`（呼叫到的**其他**函式的座標，見六章）是互補的兩件事，不是重複：⑤ 要翻譯這個函式本身，第一步是抽出「這個函式自己」的 Java 原始碼文字，需要靠這個欄位定位；`reference_targets` 只涵蓋呼叫鏈上其他節點，不含 task 自己。[P] 在四章、六章判定 module／layer、算呼叫鏈範圍時本來就已經讀過 `iface["java_method_id"]`（當 BFS 種子），這裡只是把這個既有值原樣寫進最終輸出，不是新的計算。

**`referenced_functions`／`ReferencedFunctionRef`（舊機制）暫時保留在 TypedDict 定義裡，[P] 只是不再填它**：這個舊欄位的功能被本輪新增的 `reference_targets`（六章）取代，但 `translator_cli/client.py`、`graph/nodes/implement_node.py`（07a／09a 尚未依本輪設計更新的既有真實程式碼）目前仍會讀取 `task.get("referenced_functions")` 這個欄位。在 07a／09a 真的更新之前直接把欄位從 `TaskSpec` 刪掉，會讓這兩個目前還在正常運作的模組壞掉（見 [[feedback_implementation_workflow]] 「動既有程式碼前先查引用」）。因此：`TaskSpec.referenced_functions` 欄位定義**不動**，[P] 組裝 `TaskSpec` 時單純不設這個 key（`NotRequired`，既有消費端本來就要處理它缺席的情況）。等 07a／09a 完成更新、確認沒有任何地方還會讀這個欄位時，才是真正刪除這個欄位定義的時機。

```python
class TaskSpec(TypedDict):
    id: str
    module: str
    phase: Literal[1, 2]
    translator_backend: Literal["qwen", "claude"]
    java_method_id: str  # 這個 task 自己的 Java 方法識別碼，見上方說明
    description: str
    target_files: list[str]
    reference_targets: list[ReferenceTarget]  # 見六章
    reference_targets_truncated: NotRequired[bool]  # 見六章「上限被觸發時：截斷可見化」，只在真的截斷時設 True
    context: str
    depends_on: list[str]
    class_name: NotRequired[str | None]
    function_name: NotRequired[str]
    referenced_functions: NotRequired[list[ReferencedFunctionRef]]  # 舊機制，[P] 不再填值，見上方說明
```

**`id` 產生規則**：全部 task 依「module（依 `module_list` 原始順序，`_utils`／`_global` 這兩個保留 module 固定排在最後）→ 層級（`repositories`／`services`／`routers`／`utils`）→ `function_name` 字母序」的固定全序（即七章排序用的同一套全序）依序編號 `task_{:03d}`——沿用同一套排序，編號穩定、可重現，不需要為編號另外設計第二套排序邏輯。

**`class_name`／`function_name` 落地進最終輸出**：`target_files[0]` 只是路徑字串，但同一個檔案正常會有多個函式、對應多個不同的 task——translator-cli 的 `fill_function()` 執行期需要靠 `(class_name, function_name)` 才能在同一個檔案裡精準定位這次要填的是哪一個函式，只有檔案路徑不夠。這兩個欄位在四章 module／layer 判定與 InterfaceSpec 逐筆對應時就已經算好，直接一併寫進 `TaskSpec`。

**涵蓋率驗證（機械，收尾步驟）**：`task_list` 產出後，驗證每個 `InterfaceSpec` 都被恰好一個 task 認領——組裝階段的內部草稿保有對應 `InterfaceSpec` 的 `file_path`／`class_name`／`function_name` 三元組，據此核對是否恰好覆蓋 `python_structure.interfaces` 全集且無重複認領。任何缺漏或重複視為 [P] 自己組裝邏輯的 bug，直接拋出中止——不是需要人工判斷的模糊情況，是程式該保證但沒保證到的不變量。

---

## 十、模組結構規劃

[P] 不再呼叫 Claude API，`plan_agent/` 因此**不需要** `prompts.py`／`llm.py`（比照 `parse_agent/`／`design_agent/` 需要這兩個檔案的理由是它們要呼叫 Claude API；[P] 現在完全不呼叫）：

```
refactor-project/
└── plan_agent/
    ├── module_index.py   # 四章：file_path → (module, layer) 反查（含 utils／_global 特例）、七/九章共用的固定全序
    ├── call_chain.py      # 六章：呼叫圖走訪、reference_targets 組裝
    ├── planning.py        # 五章 phase／translator_backend／description／context 組裝、七章 depends_on 組裝、八章 target_files 組裝、九章 task_list 組裝與涵蓋率驗證
    └── __init__.py        # 對外唯一入口
```

| 職責 | 說明 |
|---|---|
| module／layer 歸屬判定、全序索引 | 對應四章、七章、九章 |
| `phase`／`translator_backend`／`description`／`context` 機械組裝 | 對應五章，純字串/dict 操作，不呼叫任何外部服務 |
| 呼叫鏈範圍查找 | 對應六章，`call_chain.py`，重用①呼叫圖 + ③ `java_index` |
| `depends_on`／`target_files` 組裝 | 對應七章、八章 |
| `task_list` 組裝與涵蓋率驗證 | 對應九章 |
| 對外唯一入口 | 供 `graph/nodes/plan_node.py` 呼叫，node 本身不直接碰觸上述任何細節 |

---

## 十一、與 LangGraph 整合

`plan` 讀 `module_list`／`python_structure`／`java_project_path`，寫回 `task_list`**與 `module_list`**（見九章「補回 `_utils` 保留 module 之後的版本」）。**`plan` 是平行分支 node，不是純線性 node**：`plan`／`scaffold`（④）同以 `record_tests`／`design` 為共同前驅（01 五章），兩者都完成才觸發 `implement`。回傳值只能包含自己實際更動的 key（`task_list`、`module_list`），不能用 `{**state, ...}` 整包展開——理由同 `design`／`scaffold`（見 05a 十一章、01 五章「fan-in 不需要 reducer 的前提」）；`module_list` 這裡也是 [P] 實際更動的 key，`scaffold`（④）不讀也不寫這個 key，兩個平行 node 不會對同一個 key 各自寫入。

**執行特性的變化**：[P] 不再呼叫 Claude API，這個 node 因此不再有「單一 module 呼叫失敗、待重試清單、5 分鐘後重試」這種延遲來源——`plan` 變成一個純同步、快速完成的機械組裝步驟（六章的呼叫圖走訪需要重新對整個 Java 專案跑一次 `parse_java_project()`，這是機械 javalang 掃描，秒級完成，不是 LLM 呼叫）。

---

## 十二、錯誤處理範圍

比照 04a 九章／05a 十二章的邊界：`retry_count` 迴圈只包住 `implement → run_tests → debug`，`plan` 不在這個迴圈裡。[P] 不再呼叫 Claude API，錯誤來源因此只剩機械組裝本身的 bug：

- 四章 module／layer 歸屬反查失敗（③輸出違反自己的檔名格式承諾，且不落在 `app/utils/`／`app/core/exception_handlers.py` 這兩個已知特例）：直接往上拋，中止整條 run——這是輸入端問題，不是可以重試化解的暫時性錯誤
- 六章 `parse_java_project()` 失敗（Java 原始碼有無法解析的語法）：直接往上拋，中止整條 run——同 04a 三章「呼叫圖建構失敗」的既有原則
- 九章涵蓋率驗證失敗：直接拋出中止——代表 [P] 自己的組裝邏輯有 bug，不是可以重試化解的情況

---

## 十三、待決定事項

- [ ] 八章「`routers` 層一律納入 `schemas/{module}.py`」是否過度保守（每個 router task 都多帶一個檔案）：若目標專案 schema 檔案體積偏大，可能需要改成只在真的用到的時候才帶入，屬於效能／成本 vs. 保守精準度的取捨，待接上真實專案規模評估
- [x] **（已解決）`_utils` 保留 module 完全沒有進 `ModuleScheduler` 追蹤的 module 集合，底下 task 永遠排不到**——不是「該怎麼對待」的設計問題，是真實 bug：`ModuleScheduler` 用呼叫端傳入的 `module_list` 建構自己的狀態，`_utils` 只存在於 task 的 `module` 欄位、①的 `module_list` 從來沒有對應條目，`get_ready_tasks()` 因此永遠不會走訪到它。已用真實 `lang-exam-api-refactor` 資料證實（12 個 `_utils` task 全部排不到）並修好：`plan_all_modules()` 現在額外回傳補回 `_utils` 條目後的 `module_list`（`depends_on=[]`，比照 `_global` 既有先例），見九章。`_utils` 局部驗證該怎麼觸發（沒有對應 API golden case）仍是待 09a／`graph/scheduler.py` 定案的獨立問題，不受這次修復影響。
- [ ] `_utils`／`_global` 在局部驗證機制裡該怎麼觸發——沒有對應的 API golden case，該略過只做語法驗證，還是需要別的觸發條件，待 09a／`graph/scheduler.py` 更新時定案
- [ ] 七章「待銜接」：同一個檔案（`target_files[0]` 相同）的多個 task，Claude API 這條較高併發的路徑是否需要檔案級鎖或強制序列化，待 09a／`graph/scheduler.py` 定案雙後端併發模型時一併處理
- [ ] 六章 `PLAN_AGENT_MAX_REFERENCE_TARGETS`（預設 20）這個上限只是初始估計值，尚未拿真實專案的呼叫鏈深度／分支數評估過是否合理——太小會讓⑤看不到必要的呼叫脈絡，太大會重演 `#37` 的 context 膨脹問題，待接上真實 ⑤ 呼叫後依實際命中率調整
- [ ] 六章的呼叫鏈查找對 `java_project_path` 重新跑一次 `parse_java_project()`，跟①執行時的解析結果理論上應該一致，但兩次呼叫之間 Java 原始碼若被意外修改（正常 pipeline 執行中不該發生），兩者可能不一致——目前沒有機制偵測這種情況，視為已知但機率極低的風險，不特別處理
- [ ] `graph/scheduler.py::ModuleScheduler` 需要把全域關卡從兩層（Phase 1／Phase 2）改成三層（Phase 1 → service → controller/router），且既有的 module 依賴排程（`module_list.depends_on`）要在**每一層內部**都生效，不能只在「整個 module 全部完成」才檢查——這是 09a 落地時的必要前提（六章「呼叫鏈規則」與「與既有 module 依賴排程的關係」的正確性都直接依賴這件事），目前只有本文件與 `refactor_plan.md` 一章記錄這個新增需求，`graph/scheduler.py` 尚未實作

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
