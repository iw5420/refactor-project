# ③ 架構設計 Agent 詳細設計

> 本文件承接 `00_refactor_architecture.md` 的整體架構（見七、③ 架構設計 Agent 一節），是這個 Agent 的**設計面**文件：決策、契約、資料結構、流程。實際程式碼實作見 `05b_design_agent_code.md`；本文件不出現可執行的實作邏輯。

---

## 一、本文件範圍與定位

**本文件涵蓋**：
- ③ 的輸入來源（① 的 `module_list`／`api_to_python_target` ＋ [A] 的 `openapi_spec`，見二章）
- Python 專案分層與命名慣例（三章）
- 方法簽名的機械基礎資料：對 Java 原始碼的輕量再掃描（四章）
- Java → Python 型別對應，以及 API 邊界方法如何改用 `openapi_spec` 覆寫（五章）
- LLM 設計階段的處理單位與排程順序（六章）
- `interfaces` 的涵蓋率規則（七章）
- `route_to_file_mapping` 與 `route_to_module_mapping` 的產出演算法（八章）
- 輸出資料如何對應 `RefactorState` 的 `python_structure`／`route_to_file_mapping`

**本文件不涵蓋**：
- 實際程式碼——見 05b
- DB schema／SQLAlchemy ORM model 的欄位層級生成——00 七章已明文交給④骨架實作 Agent（`generate_scaffold()` 的職責，見九章說明），③ 不做這件事
- [P]／④／⑤ 如何消費 `python_structure`——見 06a／08a／09a
- translator-cli 的填空契約、`generate_scaffold()` 內部如何呼叫本地模型——見 07a

---

## 二、輸入與前置資料

| 輸入 | 來源 | 說明 |
|---|---|---|
| `module_list` | State，① 產出 | 模組清單、`summary`、`depends_on`、`methods`（`java_method`／`class_name`／`description`／`complexity`）——**不含**參數與回傳型別，見下方說明 |
| `api_to_python_target` | State，① 產出 | `endpoint`／`http_method`／`java_controller`／`module` 對應，決定哪些方法是「API 邊界方法」（見五章） |
| `openapi_spec` | State，[A] 產出 | 完整 OpenAPI 3.0 JSON；③ 是第一個真正讀取 `paths.*.parameters`／`requestBody`／`responses`／`components.schemas` 的 Agent——00 七章備註「① 不解析 request/response schema」，這份工作由③承接 |
| Java 原始碼（`module_list[*].java_files` 指向的檔案） | `java_project_path` | 見四章：③ 需要精確的方法簽名，① 沒有提供，③ 對每個 module 的 `java_files` 做輕量再掃描取得 |

> **為什麼 `module_list.methods` 不夠、③ 要重新碰 Java 原始碼**：04a 的 `MethodInfo` 有 `java_method`／`class_name`／`description`／`complexity`，但沒有 `params`／`return_type` 這種精確到型別的簽名資訊，這是刻意的邊界（① 的職責是「這個方法在做什麼、多複雜、屬於哪個 class」，不是「精確簽名」）。`class_name` 這個欄位的必要性：同一個 module 內常見跨層同名方法（如 `UserService.getById()` 與 `UserRepository.getById()`），沒有這個欄位，③ 重新掃描 Java 簽名時無法把 `MethodInfo` 比對回正確的類別（見四章）。但③要產出的 `InterfaceSpec` 還是需要 `params`／`return_type`，若交給 Claude 憑 `description` 這段文字自由心證去猜參數型別，等於讓 LLM 在完全不必要的地方憑空生成、增加幻覺風險——這正是可以「用程式判斷」的情況（00 二章），不該交給 LLM。

**方法對應到 API 邊界的判定**：一個方法是否為「API 邊界方法」（router 層、直接對應某個 endpoint），由 `api_to_python_target` 反查：`java_controller` 相同、且方法名與三章步驟 5（04a）建立的 `endpoint_key → method_id` 精神一致的方法即為邊界方法。實際比對交由③自己對 Java 原始碼再掃描時一併判斷（見四章），不重新依賴 04a 內部、未落地到 State 的 `ParsedProject`——04a 的呼叫圖／route index 是 `parse_agent` 內部的暫存資料，執行完就丟棄，不會留到③這一步。

---

## 三、Python 專案分層與命名慣例

00 三章已定案技術棧（FastAPI + SQLAlchemy），③ 不重新判斷框架選型，只決定**分層與檔案配置**。

**三層架構**，每個 module 在每一層各自一個檔案；另有**全域層級**的基礎設施檔案（不屬於任何 module，見本章末「全域基礎設施檔案」）：

```
app/
├── main.py                           # FastAPI 進入點，集中 include_router()（見本章末）
├── core/
│   └── database.py                   # SQLAlchemy engine/session，讀 DATABASE_URL（見本章末）
├── routers/{module}_router.py        # 對應 java_controller，@RestController/@Controller 方法
├── services/{module}_service.py      # 業務邏輯層，@Service/@Component 方法
├── repositories/{module}_repository.py  # 資料存取層，@Repository 方法
├── schemas/{module}.py               # Pydantic request/response model（見五章，僅文字描述，不產生 InterfaceSpec）
└── models/{module}.py                # SQLAlchemy ORM model（佔位，欄位內容由④生成，見九章）
```

- **檔名規則**：`{module}_{layer_singular}.py`（`module` 沿用 `ModuleInfo.module`，已是 snake_case 慣例字串，不需要③額外轉換大小寫）
- **層級判定（機械，非 LLM）**：Java class 的 stereotype 決定歸屬層級——`@RestController`／`@Controller` → `routers`；`@Service`／`@Component` → `services`；`@Repository` → `repositories`。這個 stereotype 資訊 `module_list` 沒有帶（同二章原因），由四章的輕量再掃描一併取得。
- **無 stereotype 的類別**（純工具類、無 annotation 的 helper）：不是機械規則能決定的情況，這部分**保留給六章的 LLM 設計階段判斷**歸屬哪一層（多半併入 `services`，但實際歸屬可能因業務語意而異），呼應 04a 四章「機械規則判斷不了時交給 LLM」的同一種分工原則。
- **共用類別（04a 定義的 in-degree ≥ 2 類別）**：04a 的 Reduce 階段已經把每個共用類別指派到唯一一個 `module`（見 04a 四章），③ 直接信任這個歸屬，不重新判斷——一個 Java class 對應的 Python 檔案只會出現在它所屬 `module` 的那一層檔案裡，不會跨 module 重複產生。跨 module 呼叫共用類別，就是正常的 Python import，不需要特殊處理。

**`directory_tree` 的定位**：`PythonStructure.directory_tree` 型別是 `str`（見 `graph/state.py`），是一段**文字**而非結構化資料。除了上面的目錄/檔案清單，③ 也把 `schemas/{module}.py` 底下**應包含哪些 Pydantic 類別與欄位**用文字列出（見五章、九章）——因為 `InterfaceSpec` 只能表達函式簽名，資料類別的欄位定義沒有對應的結構化欄位可放，只能靠這段文字傳遞給④的骨架生成呼叫（`generate_scaffold(python_project_path, python_structure, db_models=None)`，見九章、`07a_translator_cli_architecture.md` 四章）當作依據。`models/{module}.py`（SQLAlchemy ORM）不在這裡產出欄位內容——九章已明訂 DB schema 的欄位層級規格由④直接取得，不是③的職責，避免這裡跟九章各說各話。

**格式慣例（機械產生，固定格式，不是自由文字）**：`directory_tree` 字串固定分三段——

1. **目錄結構段**：純文字樹狀圖（如本章開頭範例），列出所有檔案路徑
2. **Schema 定義段**：每個 `schemas/{module}.py` 各自一段（見上方「models/{module}.py 不在這裡產出」）。同一個檔案路徑底下實際上可能疊加三種不同來源的內容——API 邊界 schema（本節）、建構子占位、資料容器占位（後兩者見下方「孤兒類別與資料容器占位」小節）——彼此各自是獨立的 `### {file_path}` + `python` code block，依「這個 module 有沒有對應內容」各自決定要不要出現，不是三選一。API 邊界 schema 部分：用固定的 `### {file_path}` 標題起頭，緊接著一個 `python` fenced code block，內容是機械渲染出的 pseudocode（欄位名＋型別，一行一個欄位）——一律標成 `(BaseModel)` 並帶 `from pydantic import BaseModel`，讓④能直接辨識這是 Pydantic model、不用自己猜測要不要補繼承。`Field` 的 import 視內容需要才加：只有這個檔案裡任一欄位真的用到下方「有驗證限制的欄位改用 `Field(...)`」渲染出 `Field(...)` 時，才在 import 行加上 `Field`（`from pydantic import BaseModel, Field`），沒有任何欄位帶驗證限制時維持只 import `BaseModel`，不無條件多帶一個用不到的名稱。例如 `app/schemas/user.py` 會渲染成：

   > `### app/schemas/user.py`
   > ```python
   > from pydantic import BaseModel
   >
   > class UserCreateRequest(BaseModel):
   >     name: str
   >     email: str
   >     age: int
   > ```

   用固定的 `### {file_path}` 標題加 `python` code block，是為了讓④的骨架生成呼叫（本地模型）能穩定辨識「這是哪個檔案、有哪些類別與欄位」，不依賴模型自己去猜測自由格式文字的邊界——這段渲染本身是機械字串組裝（欄位名/型別直接來自五章 `$ref` 展開後的 schema），不需要 LLM 生成這段文字本身，只有六章「這個 schema 該切進哪個 module」這類歸屬判斷需要 LLM。

   每個 `schemas/{module}.py` 段落固定以 `from __future__ import annotations` 起頭：Java entity／DTO 常見雙向關聯（如 `User` 含 `List[Order]`、`Order` 又含 `User`），轉成 Pydantic model 若兩個類別分屬不同 `schemas/{module}.py`，逐字面型別註記在模組載入當下就需要對方已經定義完成，容易撞上循環問題。這一行讓型別註記延遲求值（PEP 563），可以化解**同一個檔案內**的循環參照；跨檔案的循環 import（`schemas/user.py` 直接 `import` `schemas/order.py`、反之亦然）不會被這一行解決——那需要 `TYPE_CHECKING` guard 的匯入寫法＋明確呼叫 `model_rebuild()`，屬於④如何實際生成、串接檔案間 import 的問題，不是③這裡的 pseudocode 渲染能單獨解決的，留給 `07a_translator_cli_architecture.md`／`08a_scaffold_agent_architecture.md`（兩者皆待建立）處理。
3. **基礎設施段**：`app/main.py`／`app/core/database.py` 各自一段，格式與 Schema 定義段相同（`### {file_path}` + `python` code block），內容見下方「全域基礎設施檔案」。

### 孤兒類別與資料容器占位（機械產生，僅類別歸屬層級判斷需要 LLM）

**觸發情境**：六章 `classes_needing_layer` 只問「有一般方法」的無 stereotype 類別要歸哪一層（見六章「單一 module 呼叫內容」）——一個 class 若 `methods` 是空清單，永遠不會有任何方法被追蹤、也永遠不會產生 `InterfaceSpec`，若不另外處理，這批 class 在 `python_structure` 裡會完全沒有任何痕跡，④／⑤下游不會被告知要建立對應的 Python 定義。`methods` 為空清單常見於兩種情況：**自訂例外類別**（Java 端只有建構子、沒有一般方法）、以及 **Lombok 標記的資料類別**（`@Data`／`@Value`／`@Getter`／`@Setter` 等由 annotation processor 在編譯期生成 getter/setter，javalang 是純原始碼解析器，結構上看不到這些生成的方法，即使類別欄位齊全，`methods` 一樣是空清單）。

**判斷優先序**（對每個 `methods` 為空清單、沒被任何 `InterfaceSpec` 覆蓋、且 class 名稱沒有出現在這個 module 已收錄的 API 邊界 schema 名稱裡的 class，依序判斷，命中即停止）：

`methods` 是否為空清單是唯一的前置條件，不是 `stereotype`——`routers` 層（`@RestController`）的 `InterfaceSpec.class_name` 依七章規則一律是 `None`，光憑「有沒有被 `InterfaceSpec` 覆蓋」判斷不出一個方法齊全的 Controller 是否已經處理過；`methods` 非空直接代表這個 class 已經（或即將）透過 `InterfaceSpec` 產出，不該再進這個分支——一個正常、方法齊全的 `@RestController`（尤其是剛好沒有欄位、沒有建構子的無狀態 Controller）若略過這個檢查，會被誤判成孤兒類別，嚴重時甚至被誤渲染成錯誤的 `dataclass` 段落。

1. **標註 `@Entity`／`@Embeddable`／`@MappedSuperclass`（見 `common/java_annotations.JPA_ENTITY_ANNOTATIONS`，00 六章「Java class annotation 判斷（共用工具）」）→ 跳過，不渲染任何段落**。DB schema 欄位層級規格不是③的職責（見九章、`app/models/{module}.py` 說明），這批類別的欄位定義由④直接從既有 Postgres 測試 DB 或 Java entity 原始碼取得，③在這裡渲染反而會跟九章的既有分工衝突。
2. **標註任一 Lombok／JPA 資料類別 annotation（見 `common/java_annotations.DATA_CLASS_ANNOTATIONS`）→ 渲染成 Python `dataclass`**：不用 Pydantic `BaseModel`（那個保留給 API 邊界契約，來源是 `openapi_spec` 展開，見五章），也不用 SQLAlchemy model（那是④的職責，理由同上一點）——`dataclass` 沒有隱含驗證行為、也不綁定 ORM 語意，是「純粹當籃子傳接值」在 Python 端最直接的對應。欄位是否全部標註 `final`（javalang 看得到的欄位修飾字，不需要另外偵測 Lombok 是不是用了 `@Value` 這類特定的不可變 annotation）決定渲染成 `@dataclass(frozen=True)` 還是一般 `@dataclass`。
3. **有建構子（不論有沒有 Lombok 標記）→ 渲染建構子占位段落**：格式沿用既有的「只列建構子簽名，不假設任何 Python 基底類別」慣例（常見案例：自訂例外類別，如 `AuthException` 只有 `AuthException(String message)` 這種建構子，實際該對應 `Exception` 子類別或其他寫法，交由④／⑤依 Java 原始碼自行判斷）。
4. **無 Lombok 標記、但有欄位宣告 → 渲染成 `dataclass`**，跟第 2 點格式相同，只是這是機械推斷（沒有 annotation 這種明確信號，純粹因為原始碼裡有 public/private 欄位、沒有方法也沒有建構子），渲染出的註解會標明「無 Lombok 標記，依欄位宣告推斷」，供人工核對時追溯這是高信心（Lombok 標記）還是低信心（純欄位推斷）判斷。
5. **什麼都沒有（無 annotation、無建構子、無欄位）→ 只記一筆警告，不渲染任何段落**——沒有任何機械事實可以講，不強行渲染空內容。

第 2／4 點渲染出的 `dataclass` 段落跟 Schema 定義段共用同一個 `schemas/{module}.py` 檔案路徑與既有的 `### {file_path}` + `python` code block 格式慣例，不是新的檔案分類——這代表七章 `interfaces` 涵蓋率規則、`06a_plan_agent_architecture.md` 七章 `target_files` 組裝規則裡「本 module 的 `schemas/{module}.py`（若存在）」這條既有規則自動涵蓋這批新內容，不需要 [P] Plan Agent 或任何下游文件跟著改。

### 全域基礎設施檔案（機械產生，不需 LLM，不產生 InterfaceSpec）

`app/main.py`、`app/core/database.py` 不屬於任何 module，也不是「函式」——延續三章開頭已經定調的分工原則（`InterfaceSpec` 只能表達函式簽名，資料類別/樣板檔案的內容只能靠 `directory_tree` 文字傳遞），這兩個檔案的內容一律**機械組裝**，不佔用六章的 LLM 呼叫，也不產生對應的 `InterfaceSpec` 條目：

- **`app/core/database.py`**：內容是固定樣板，跟任何一個專案的業務模組無關，落實 00 五章「Python 服務統一讀環境變數 `DATABASE_URL`」的規範——寫死讀取 `DATABASE_URL`、建立 SQLAlchemy engine／`SessionLocal`／`Base`／`get_db()` dependency，字面模板（欄位、變數名固定）如下，不需要每個專案重新設計：

  ```python
  from sqlalchemy import create_engine
  from sqlalchemy.orm import sessionmaker, declarative_base
  import os

  DATABASE_URL = os.environ["DATABASE_URL"]
  engine = create_engine(DATABASE_URL)
  SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
  Base = declarative_base()

  def get_db():
      db = SessionLocal()
      try:
          yield db
      except Exception:
          db.rollback()
          raise
      finally:
          db.close()
  ```

  這段模板寫死進 `directory_tree` 的基礎設施段，是為了避免④的骨架生成呼叫（本地模型 qwen）自行臆測別的環境變數名稱或連線寫法——00 五章已經定案的變數名稱，不該在③到④這一步之間，因為交給模型自由發揮而流失。

  `except Exception: db.rollback()` 明確在例外發生當下立即 rollback，不依賴 `Session.close()` 的隱含 rollback 行為，讓④／⑤產生的程式碼交易邊界更明確。

  **維持同步（`create_engine`／`Session`），不改非同步（`asyncpg`／`AsyncEngine`／`AsyncSession`），已定案**：這是重構專案，目標是跟 Java 服務（傳統 blocking JDBC）行為對齊，不是從零打造高併發 API，見 00 三章「Python 目標技術棧」。改非同步會讓④／⑤產生的每一支碰到 DB 的函式（repository／service／router 三層）都要正確加上 `async`/`await`，這個正確性負擔會落在⑤呼叫的本地模型（qwen2.5-coder:32b）身上——本地模型的可靠度本來就是這個專案風險最高的環節（見 00 三章「LLM 分工」硬體限制），不該再加大它的出錯面。

- **`app/main.py`**：內容依賴「這次專案實際有哪些 module 產出了 router 層檔案」，不是完全靜態的模板，但仍是機械組裝，不需要 LLM 判斷——在六章所有波次的 per-module 設計都完成後（即八章「機械合併」同一個時間點），對每一個「`interfaces` 裡出現過 `app/routers/{module}_router.py` 這個 `file_path`」的 module，機械產生一行 import 與一行 `include_router()`：

  ```python
  from fastapi import FastAPI
  from app.routers.user_router import router as user_router
  from app.routers.order_router import router as order_router

  app = FastAPI()
  app.include_router(user_router)
  app.include_router(order_router)
  ```

  沒有產出 router 層檔案的 module（例如整個 module 只有 service/repository、沒有直接對外的 Controller）不會出現在這份清單裡，這是機械判斷（檢查 `interfaces` 的 `file_path` 是否落在 `app/routers/` 底下），不需要 LLM 介入。

---

## 四、方法簽名的機械基礎資料（javalang 輕量再掃描）

**決策：③ 對每個 module 的 `java_files` 做一次獨立的輕量 javalang 掃描**，只取兩件事：

1. 每個 method 的完整簽名——參數清單（名稱＋ Java 型別）與回傳型別（Java 型別）
2. 每個 class 的 stereotype annotation（供三章層級判定）

**這不是重跑 04a 三章的呼叫圖建構**：04a 的呼叫圖需要欄位依賴、method invocation 解析、`@Qualifier`/`@Primary` 消歧，是為了「這個方法還會不會被呼叫到」；③ 要的只是「這個方法長什麼樣子」，範圍窄得多，不需要建立呼叫關係。沿用 04a 已驗證過的同一顆 `javalang`（同一個 `lang-exam-api-refactor` 專案已證實 100% 解析成功，見 04a 三章「決策」），不需要重新驗證解析器可行性，只是抽取的欄位不同。

> **多載（overload）方法的處理**：延續 04a 三章「決策」的既有限制——`module_list.methods` 本身在多載情況下已有精準度限制（04a 五章也提過同樣的限制）。③ 再掃描時若遇到同名多載方法，比照④/⑤ 消費 `module_list` 時的既有認知：分別以各自完整簽名（含參數型別）建立獨立的 `InterfaceSpec`，不因為同名而合併——這裡③是直接讀完整 AST 節點（含參數型別），不像 04a 的 `method_id` 只用方法名不含簽名，掃描與比對階段不會有多載碰撞問題。
>
> **`function_name` 消歧**：`InterfaceSpec.function_name` 單純用 camelCase→snake_case 轉換（見七章）時，同一組多載會算出相同名稱——Python 不支援多載，會讓④的骨架生成撞名。③對同一組多載依 javalang 掃描到的宣告順序消歧：第一個保留原始轉換名稱，其餘依序加上數字後綴 `_2`、`_3`……，比照 04a 三章已引用的 springdoc 前例（`voice`／`voice_1`）。實作見 `05b_design_agent_code.md` 七章 `_build_method_contexts()`。

**掃描失敗的處理**：比照 04a 九章，Java 原始碼若有 javalang 無法解析的語法，直接往上拋，整條 LangGraph run 中止——這是輸入端問題，不是可以重試化解的暫時性錯誤。

---

## 五、Java → Python 型別對應與 API 邊界覆寫

### 基礎型別對應表（機械，非 LLM）

| Java | Python |
|---|---|
| `int`／`Integer`／`short`／`Short` | `int` |
| `long`／`Long` | `int` |
| `String` | `str` |
| `boolean`／`Boolean` | `bool` |
| `double`／`Double`／`float`／`Float` | `float` |
| `BigDecimal` | `Decimal`（見下方說明，不是 `float`） |
| `List<T>`／`Set<T>`／`Collection<T>` | `list[T]` |
| `Map<K, V>` | `dict[K, V]` |
| `Optional<T>` | `T \| None` |
| `void` | `None` |
| 專案內自訂 class `Xxx` | `Xxx`（假設同名 Python 類別存在，實際定義由對應 module 的 `schemas`／`models`／service 回傳型別決定） |
| `java.util.function` 常見 functional interface（`Function`／`BiFunction`／`Supplier`／`Consumer`／`BiConsumer`／`Predicate`／`BiPredicate`） | `Callable[...]`（依各自 arity 組裝，如 `Function<T, R>` → `Callable[[T], R]`，見下方「未知泛型包裝類別」） |
| 其他未知的泛型包裝類別（專案自訂泛型如 `ResponseResult<T>`、其他框架型別如 `Specification<T>`） | 遞迴正規化內層型別參數＋符號轉換 `Foo<Bar>` → `Foo[Bar]`，不猜測外層類別語意（見下方說明） |

這張表是純字串對應，套用規則本身不需要 LLM——呼應 00 二章「能用程式判斷的，就不要交給 LLM」。

**`BigDecimal` 獨立於 `double`／`float` 之外，刻意不共用同一個對應**：Java 生態系統選 `BigDecimal` 通常就是為了避開 IEEE 754 浮點誤差（金額、需要精確小數運算的場景），對到 Python `float` 會直接把這個精度保證丟掉。標準庫 `decimal` 模組的 `Decimal` 型別是 Pydantic 原生支援的型別，語意上才是正確對應。**表格裡寫的是不帶模組前綴的 `Decimal`，不是 `decimal.Decimal`**——`InterfaceSpec.params[].type`／`return_type` 裡的型別字串全部是裸名稱，`Session`／`Request`／`Annotated[User, Depends(get_current_user)]`／專案內自訂 class 都是同一個慣例（見上表其他列），實際 import 語句由④骨架生成階段自行解析，不是③在這裡就要組出完整的 dotted path。

**這個對應只在非 API 邊界方法生效，覆蓋不到 API 邊界方法**：API 邊界方法改用 `openapi_spec` 決定型別（見下方「API 邊界方法：改用 openapi_spec 覆寫」），不會走這張表。springdoc 產生的 OpenAPI schema 對 `BigDecimal` 欄位通常序列化成通用的 `number` type，沒有任何欄位標示「這原本是 BigDecimal」，這種情況下 API 邊界方法的這個欄位型別仍是 `float`，不是 `Decimal`——這是「openapi_spec 為 API 邊界唯一權威來源」這個既有設計原則下的既知落差，不是型別對應表能單獨解決的問題。

**未知泛型包裝類別的處理**：`map_java_type()` 這一層機械解決——已知的 functional interface 查表轉成 `Callable[...]`（上表新增列）；其餘未知泛型遞迴正規化內層型別參數、外層符號轉換 `Foo<Bar>` → `Foo[Bar]`，保證回傳的字串永遠是合法 Python 泛型 subscript 語法（見 `type_mapping.map_java_type()` docstring）。

**刻意不做 LLM 判斷**：`ResponseResult<T>` 這類專案自訂泛型，唯一殘留的問題是「`ResponseResult` 這個 class 有沒有宣告成 `Generic[T]`」——這不是型別字串層級能決定的事，是④骨架生成 `ResponseResult` 這個類別定義時的職責，屬於三章「孤兒類別／資料容器占位」判斷的鄰近缺口，不是六章型別對應能解決的。`Specification<ExamEntity>` 這類框架內部型別，真正困難在函式本體要怎麼寫等價的 SQLAlchemy 動態查詢，問 LLM「該用什麼型別」換不到解決真正問題的幫助，那是⑤/⑦ 該處理的層次。兩個代表案例拆解後都不是型別判斷能解決的問題，因此不新增 LLM 呼叫；07a 四章的符號轉換邏輯仍保留，但對這裡輸出的字串而言恆為 no-op，純粹是 defense-in-depth。

### API 邊界方法：改用 `openapi_spec` 覆寫

一個方法若同時是 `api_to_python_target` 的邊界方法（router 層，直接處理某個 endpoint 的 controller 方法），它的參數與回傳型別**改以 `openapi_spec` 為準**，不用上表機械翻譯 Java 簽名——原因：FastAPI 的 request/response 邊界必須匹配 OpenAPI 契約（這是 Postman Collection、Harness golden output 比對的依據），Java 端的內部型別（如 Entity 直接當 return type）不是 API 契約真正需要的形狀，用 Java 型別硬翻譯反而可能不符 springdoc 實際產出的 schema。

**查找方式**：`ApiMapping.endpoint` 本身就是 `openapi_spec["paths"]` 的 key（來自同一份 `openapi_spec`，不需要 04a 三章那套 context-path 校正——那是用來比對 Java 端 `@RequestMapping` 字面值，`openapi_spec` 自己的 path 已經是最終形式），直接以 `openapi_spec["paths"][endpoint][http_method.lower()]` 取得 operation 物件。

**不用 `operationId` 比對**：延續 04a 三章步驟 5 的既有結論（`voice`／`voice_1` 案例），`endpoint`＋`http_method` 已是可靠的 key，不需要也不應該用 `operationId` 反查。

**`$ref` 展開**：沿用 03a 已定案的展開原則（見 03a「OpenAPI `$ref` 展開」一節）——只展開這次任務相關 operation 片段裡用到的 `$ref`，不整包攤平 `components.schemas`；遞迴展開到底；不處理 `allOf`／`oneOf`／`anyOf` 組合語法（等真的遇到再處理）。

**決策：抽為共用工具 `common/openapi_ref_resolver.py`，不在 `design_agent` 內重新實作一份**。理由：[B] Collection Agent（03a/03c）與③現在都需要同一段「遞迴展開 `$ref`、不整包攤平」邏輯，這正是 00 六章「Map 階段併發數／切批次」抽出 `common/concurrency.py`／`common/chunking.py` 的同一種情況——兩個 Agent 各自需要同一份機械邏輯，不是恰好想法一致，是同一件事，應比照抽到 `common/`，避免 `design_agent` 直接依賴 `spec_collection_agent` 內部函式造成跨套件耦合，也避免日後兩邊各自修出不一致的展開行為。

`common/openapi_ref_resolver.py` 對外唯一函式：`resolve_refs(fragment: dict, full_spec: dict) -> dict`，`fragment` 是呼叫端已經取出的 operation/schema 片段，`full_spec` 是完整 `openapi_spec`（供 JSON Pointer 解析用）。③、[B] 都只傳入「這次任務相關」的片段，不是整份 spec，維持 00 六章「只帶當次任務相關資料」的既有原則。

**遺留事項（已解決）**：~~03a/03c 既有的 `$ref` 展開實作目前仍是 `spec_collection_agent` 內部函式，尚未搬到 `common/`~~——`spec_collection_agent/openapi_refs.py` 已刪除，`chain_dependency_detect.py`／`value_filler.py` 改直接呼叫 `common/openapi_ref_resolver.py`。

**型別命名**：展開後的 schema 若有明確名稱（springdoc 通常會產生具名 schema，如 `UserCreateRequest`），③ 直接沿用這個名稱作為 Pydantic 類別名稱，寫入 `InterfaceSpec.params[].type`／`return_type`；同時把這個類別**應包含的欄位**（欄位名＋型別，來自展開後的 schema）機械渲染成文字，寫入三章提到的 `directory_tree` 文字區塊（`app/schemas/{module}.py` 底下），供④的骨架生成呼叫使用。這一步是機械的文字渲染，不需要 LLM 判斷。

**有驗證限制的欄位改用 `Field(...)`**：Java 端常見的 Bean Validation annotation（`@Size`／`@Pattern`／`@Min`／`@Max`）springdoc 會轉譯進 OpenAPI schema 的 `maxLength`／`minLength`／`pattern`／`maximum`／`minimum`，欄位渲染時一併轉成 Pydantic `Field()` 的對應關鍵字引數（如 `max_length`／`pattern`／`le`／`ge`），機械字串組裝，不需要 LLM。這不是風格偏好——00 一章「測試夾住重構」的核心原則要求 Python 服務跟 Java 服務行為對齊，Java 對超長字串／格式不符的輸入會回傳驗證錯誤，Python 若沒有複製這條限制，Harness 拿同一組 Postman 請求打兩邊時，這類邊界案例會直接對不上（Java 4xx vs. Python 200）。

**路徑／查詢參數**：`operation.parameters` 裡的 path/query 參數，型別直接用其 `schema.type`（走 OpenAPI 型別 → Python 型別的簡單對照，如 `integer`→`int`、`string`→`str`），不需要回頭比對 Java `@PathVariable`/`@RequestParam` 的宣告型別——`openapi_spec` 已經是可信來源。

**router 層 API 邊界方法額外帶 `http_method`／`route_path`**：`InterfaceSpec` 新增兩個 nullable 欄位（見九章），只在這個方法是 router 層的 API 邊界方法時才非 `None`，值直接取自本章「查找方式」已經反查到的 `ApiMapping.http_method`／`ApiMapping.endpoint`——這筆對應在③組出這個 `InterfaceSpec` 的當下就已經算好，不是新的判斷，只是把既有的中繼結果一併寫進輸出。這樣做是為了解決④骨架生成階段的一個既有缺口：`translator_cli.generate_scaffold()` 的 `python_structure` 引數只吃 `PythonStructure`（另有 `python_project_path`／`db_models` 兩個引數，跟 `InterfaceSpec` 的欄位無關，見 `07a_translator_cli_architecture.md` 四章），若不把 HTTP method／路徑帶進 `InterfaceSpec`，④無法知道 router 函式該掛哪個 `@router.get(...)` 裝飾器；改由④／07a 自行拿 `api_to_python_target` 反查也行不通——`ApiMapping` 只到 `(module, java_controller)` 顆粒度，沒有 `function_name`，要配對到具體函式得重跑③內部「camelCase→snake_case＋多載消歧」那套邏輯（見七章「`function_name` 消歧」），等於在下游重新實作一次③已經做過的事。`route_path` 直接是 `ApiMapping.endpoint` 的原始字面值（如 `/api/v1/users/{userId}`），不需要額外轉換——path/query 參數的 `ParamSpec.name` 沿用原始 Java 參數命名、不轉 snake_case（見上方「路徑／查詢參數」），FastAPI 裝飾器裡的 `{userId}` 佔位符因此天然對得上函式參數名稱。多載方法只有 `_select_boundary_overload()`（05b 七章）選中、真正對應這個 endpoint 的那一個多載會帶上這兩個欄位，其餘多載維持 `None`，跟五章其餘欄位（`params`／`return_type`）已有的處理方式一致。`APIRouter(prefix=...)` 或裝飾器要不要寫完整路徑這類慣例，仍是④／08a 的決定範圍，③只保證資料送得到。

### 框架注入物件：openapi_spec 覆寫的例外

上面「API 邊界方法改以 `openapi_spec` 為準」只涵蓋**業務參數**（path/query/body，也就是 API 契約真正約定的形狀）。Java Controller 方法簽名裡常見的 Spring 框架注入物件——`HttpServletRequest`／`HttpSession`／`Authentication`／`Principal`／`@RequestHeader` 取出的 token 等——這些**不屬於 API 契約**，springdoc 本來就不會把它們列進 `parameters`，若照字面「以 openapi_spec 為準」把 Java 簽名整個丟掉，會連同這些框架物件一起消失，而 FastAPI 這邊多半仍需要對應的東西（如 session/使用者身份），只是換一套慣用法（`request: Request`、`Depends(get_current_user)` 之類）。

因此覆寫規則精確為：**openapi_spec 的 parameters/requestBody/responses 決定業務參數與回傳型別；四章機械掃描出的 Java 簽名裡，任何一個參數若在 openapi_spec 裡找不到對應（不是業務參數、而是框架物件），不能直接丟棄，改交給六章 LLM 設計階段判斷 FastAPI 對應寫法**（同七章「框架慣例參數」的判斷方式，因為 Java→FastAPI 的注入慣例轉換本來就是機械表格解決不了、需要 LLM 判斷的情況）。這個「找不到對應」的比對本身是機械的（Java 參數型別是否出現在 openapi 的 parameters/requestBody schema 裡），只是判斷結果——該怎麼翻譯成 FastAPI 寫法——需要交給 LLM。

**requestBody 比對不到型別時的退回啟發式**：path/query 參數用「參數名稱」比對（Spring 慣例上參數名稱本來就要跟 `@PathVariable`／`@RequestParam` 對齊），但 requestBody 在 Java 端通常是單一個 DTO 物件參數（如 `createUser(UserCreateRequest req)`），Java 參數名稱（`req`）不會等於 DTO 類別名稱，因此改用「Java 參數型別名稱是否等於 openapi 具名 requestBody schema 的名稱」比對。若 Java DTO 類別名稱跟 springdoc 產生的 schema 名稱不一致（常見於命名不完全一致的專案），型別比對會失敗——這種情況下，若這個方法在 path/query 比對完之後**剛好只剩一個未分類的參數**、且這個 operation 確實有 requestBody，就保守假設這唯一剩下的參數是 body 參數，不再送去跟框架注入物件混在一起判斷；若剩下不只一個未分類參數，則無法安全區分誰是 body、誰是真正的框架物件，全部歸類為「找不到對應」，交給六章 LLM 依方法描述語境判斷。

---

## 六、LLM 設計階段（Claude API）

### 處理單位：依模組取代整包 Map-Reduce

00 六章的 map-reduce 模式，適用於「Map 階段彼此看不到對方、需要 Reduce 做跨邊界最終判斷」的情況（如 04a 的 class 歸屬 module）。③ 的情況不同：**模組邊界①已經定案**（`module_list` 已經是最終切分），③ 不需要重新判斷「這個 class 該歸哪個 module」，只需要針對「已經分好的每個 module」設計 Python 結構——因此③不是 Map-Reduce，而是**依 `depends_on` 拓樸分波、組間平行**：

1. 對 `module_list` 依 `depends_on` 建拓樸順序，分成若干「波」（wave）：第一波是沒有 `depends_on` 的 module，之後每一波是「`depends_on` 全部落在前面已完成波次」的 module——這與 00 三章描述⑤排程器的依賴處理原則同構，只是套用在③身上而非⑤的 task 排程。
2. 同一波內的 module 平行呼叫 Claude API，併發數呼叫 `common.concurrency.default_concurrency()`（跟 04a 4b、03a 共用同一份實作，見 00 六章「Map 階段併發數（共用工具）」）。
3. 處理下一波某個 module 時，**帶入它所依賴的上游 module（已經處理完）的 `InterfaceSpec` 清單**（哪些 class／method 已經存在、簽名是什麼）作為 prompt 的一部分，讓下游 module 設計時能正確引用已存在的函式，而不是自己瞎猜一個不存在的函式名。

> **與 04a Map-Reduce 的關鍵差異**：04a 的 Reduce 階段是必要的「跨邊界語意判斷」（Map 階段彼此獨立、互不知情，只有 Reduce 才能發現組間關聯）。③ 因為模組邊界已經固定、且採**拓撲有序**處理（下游永遠看得到上游成品），組裝最終 `python_structure`／`route_to_file_mapping` 只是**機械彙整**（攤平各 module 的 `interfaces`、拼接 `directory_tree` 文字、依 `api_to_python_target` 建 `route_to_file_mapping`，見八章；同一個時間點也組裝 `app/main.py` 的 router 註冊清單、附加 `app/core/database.py` 固定樣板，見三章「全域基礎設施檔案」），不需要額外一次 LLM Reduce 呼叫去做跨模組最終判斷——這點③比①簡單。

**循環依賴**：`depends_on` 理論上應為 DAG（04a Reduce 階段的既有假設，⑤排程器也依賴同一假設）。若拓樸排序偵測到環，視為上游輸入資料錯誤，直接中止，交由人工排查——不嘗試自動打斷環或猜測合理順序。

**依賴完整性檢查與循環依賴分開判定**：`depends_on` 若引用了不存在於 `module_list` 的 module 名稱（例如①的 Reduce 階段拼錯依賴的 module 名稱），這個依賴永遠無法被滿足，拓樸排序最終同樣會卡住——但根因是「缺依賴」，不是「循環」，兩者對應的人工排查方向完全不同（前者要回頭查①的輸出，後者要查 `module_list` 本身的依賴關係設計）。因此拓樸排序前先做一輪存在性檢查，「缺依賴」與「循環依賴」用不同的錯誤路徑回報，不共用同一種中止訊息。

### 單一 module 的 Claude 呼叫內容

| 輸入 | 說明 |
|---|---|
| `module.summary`／`module.methods` | ① 的產出，提供「這個 module 在做什麼」的業務語境 |
| 四章機械掃描的簽名／stereotype | 這個 module 的 `java_files` 對應的精確 Java 簽名與層級 |
| 五章 `openapi_spec` 展開結果 | 僅限這個 module 中屬於 API 邊界方法的部分 |
| 五章「找不到 openapi 對應」的參數清單 | API 邊界方法裡，Java 簽名有、但 openapi_spec 業務參數裡找不到對應的參數（框架注入物件候選，見五章「框架注入物件」）——這份清單是機械比對出來的，明確列給 LLM，不能只給 openapi_spec 展開結果、指望 LLM 自己發現漏了什麼 |
| 上游依賴 module 已產出的 `InterfaceSpec` | 僅限 `depends_on` 列出的 module，不是全專案 |

**輸出契約刻意縮小成「機械規則判斷不了的部分」，不要求 LLM 吐出完整 `InterfaceSpec`**：三章（層級、檔名、私有方法命名）、五章（Java→Python 型別對應、API 邊界方法的 openapi 覆寫）已經把 `InterfaceSpec` 絕大部分欄位定成機械規則，真正需要 LLM 判斷的只剩兩件事——若連同「這個 module 的完整 `InterfaceSpec` 清單」都整批要求 LLM 重新產出，等於讓 LLM 在完全不必要的地方（機械規則早就決定好的欄位）重新生成一次，白白增加不一致與幻覺的風險，違反 00 二章「能用程式判斷的，就不要交給 LLM」。因此輸出只有兩類決策：

| 輸出 | 說明 |
|---|---|
| `class_layers`：無 stereotype 類別的層級決定 | 對 `classes_needing_layer`（三章判定不出層級的類別）逐一輸出 `{class_name, layer}`，**每一個都要回答，不能省略** |
| `method_decisions`：框架注入參數寫法、db session 判斷 | 只對「有五章框架注入參數候選、或需要判斷要不要加 `db: Session`」的方法逐一輸出 `{signature_key, extra_params, needs_db_session}`，**每一個都要回答，不能省略**；`extra_params` 是 `{name, type}` 物件陣列，不是單一字串 |

收到回應後，③把這兩類決策跟機械算好的骨架（層級已知時的檔名、私有方法命名、Java→Python 型別對應、API 邊界方法的 openapi 覆寫參數/回傳型別）合併，才組成最終這個 module 的完整 `InterfaceSpec` 清單——LLM 本身不直接產出 `InterfaceSpec`。`db: Session` 的兩種宣告方式（`routers` 層 `= Depends(get_db)`；`services`／`repositories` 層不帶預設值）依**最終解析出的層級**機械決定，不是 LLM 判斷的一部分（見七章「框架慣例參數」）。

| 輸出（延續） | 說明 |
|---|---|
| 這個 module 對應的 `directory_tree` 文字片段 | 含 `schemas/{module}.py` 欄位描述（若這個 module 有 API 邊界方法），機械組裝，不是 LLM 輸出的一部分 |

**Prompt 設計上的硬性要求**：六章這份表格裡「五章『找不到 openapi 對應』的參數清單」這一行輸入，是為了確保五章訂的規則不會停留在文件層次、實際 prompt 卻沒有把這份清單交給 LLM——若 prompt 只丟 `openapi_spec` 展開結果，LLM 沒有管道知道「這個 Java 方法還有一個 `HttpServletRequest` 參數沒被 openapi 覆寫」，容易產生兩種失敗模式：直接漏掉這個參數（FastAPI 骨架執行期才發現少東西），或是自己瞎猜一個不存在的框架寫法（幻覺）。05b 實作時，這份清單必須是 prompt 組裝時明確逐項列出的結構化輸入，不是靠 LLM 自己比對兩份原始資料湊出來。

**單一 module 呼叫失敗時**：比照 04a 四章「待重試清單」機制——失敗的 module 列入待重試清單，這一波其餘 module 呼叫完後等待 5 分鐘統一重試一次；仍失敗則整條 `design` run 中止（不同於 04a 允許缺摘要繼續跑的彈性空間——`python_structure` 是 [P] 與④唯一的權威規格，見 00 三章，任何一個 module 的介面缺失都會讓下游④／⑤對著不完整規格工作，風險遠高於重新執行一次，見十二章）。

**用量記錄**：一律經由 `common/llm_client.py` 的 `call_claude_for_json()`，不自行重新實作 client 初始化或 `log_usage()` 串接（見 00 六章）。

---

## 七、`interfaces` 涵蓋率規則

**強制規則：`python_structure.interfaces` 必須涵蓋 `module_list` 裡每一個 module 的每一個方法**，不能只涵蓋 `api_to_python_target` 有對應到的 API 邊界方法。

理由：00 八章明確要求 [P] Plan Agent「以 Agent ① 輸出的完整方法清單為準」拆 task，若③的 `interfaces` 只有 API 邊界方法，[P] 根本無法對內部 helper function 拆出對應 task（因為 `target_files` 必須是 `python_structure.interfaces` 中已存在的 `file_path`，`InterfaceSpec` 是 [P]／④ 唯一的權威規格，見 00 八章 `TaskSpec` 定義）——覆蓋率缺口會直接讓 04a 五章、00 八章反覆強調的「internal helper 不能漏」在③這一步就先破功。

**私有／內部方法的命名慣例**：Java 的 `private` 方法沒有對外可見性限制的對應，但 Python 慣例用底線前綴表示「內部使用」——③ 產出這類方法的 `InterfaceSpec.function_name` 時加上底線前綴（如 `_calculate_discount`），保留在同一個 class／檔案內，不特別切出獨立檔案。

**`self` 不算進 `params`**：延續既有 stub 慣例（見 `design_node.py`／01 七章 stub 範例）——`InterfaceSpec.params` 只列業務參數，不包含隱含的 `self`。

**`routers` 層的 `InterfaceSpec.class_name` 一律為 `None`**：FastAPI 慣例上 router 是用 `@router.get(...)` 這類裝飾器裝飾的自由函式，不是類別方法（跟 Java 的 `@RestController` class 方法不同）；`services`／`repositories` 層則沿用對應的 Java class 名稱不變（見 `design_node.py` stub 的 `UserRepository` 例子）。層級歸屬本身依三章規則決定（機械可判定的不問 LLM），但「歸到 `routers` 層之後 `class_name` 要填什麼」是機械規則，不需要另外問 LLM。

**框架慣例參數**：任一層（`routers`／`services`／`repositories`）的方法若需要 SQLAlchemy session，由③在設計時視情況加入 `params`——這是「怎麼寫出可執行的 Python 程式碼」判斷，Java 端沒有直接對應（Java 用 Spring 的 `@Autowired`/建構子注入，不是逐一方法傳參），交由 LLM 依 FastAPI + SQLAlchemy 的慣例決定，機械表格解決不了。**不只問 `repositories`／`services`，`routers` 層也要問**：FastAPI 的依賴注入只在被 `@router` 裝飾的端點函式這一層生效，`db: Session = Depends(get_db)` 只能宣告在 router 方法上，再以一般引數往下傳給它呼叫的 service/repository；router 方法若會呼叫到需要 db 的下游，同樣需要這個參數，排除在問題範圍外會漏掉這個最常見的宣告位置。**兩種宣告方式依最終解析出的層級機械決定，不是 LLM 判斷的一部分**：`routers` 層 → `db: Session = Depends(get_db)`；`services`／`repositories` 層 → 不帶預設值的 `db: Session`（呼叫端以一般引數往下傳，不是 FastAPI DI 進入點）。

**帶預設值的參數必須排在不帶預設值的參數之後**（Python 語法要求，否則 `SyntaxError: parameter without a default follows parameter with a default`，已實測驗證）：`params` 組裝順序是業務參數（一律不帶預設值）→ 六章 LLM 決定的框架注入參數（`extra_params`，型別字串可能像 `User = Depends(get_current_user)` 這樣自帶預設值，順序由 LLM 回應決定，沒有保證）→ 機械附加的 `db`（只有 `routers` 層帶預設值）。若 LLM 判斷某個框架注入參數該用 `Depends(...)` 慣例、且它不是這個清單裡最後一個不帶預設值的參數，組裝出的簽名就會違反這條規則——這不是理論邊角案例，`design.py` 因此在組裝完 `params` 後一律套用 `_reorder_params_defaults_last()`：不帶預設值的參數維持原相對順序排前面，帶預設值的排後面（穩定分割，不改變同一類別內部的順序），保證輸出永遠是合法 Python 函式簽名，不依賴 LLM 或後續任何環節自己保證順序正確。單元測試見 `tests/design_agent/test_reorder_params.py`。

---

## 八、`route_to_file_mapping` 與 `route_to_module_mapping` 產出（機械合併）

`route_to_file_mapping` 與 `route_to_module_mapping` 的產出**完全是程式邏輯，不需要 LLM**——所有需要的資訊（`api_to_python_target` 的 endpoint↔module 對應、每個 module 的 `interfaces` 檔案集合）在六章都已經產出完畢。兩者共用同一套 key 正規化邏輯（見下方「Key 格式」），在同一次迴圈裡一併產出，不是兩個獨立步驟。

**Key 格式**：沿用 02a 十一章已定案的格式，**③ 的輸出必須跟 02a `RouteMapper.normalize_path_key()` 的正規化結果完全一致**，否則 Harness 的比對會全部 miss：

```
{METHOD}_{path_normalized}
```

`path_normalized` 的產生規則：對 `ApiMapping.endpoint`（即 `openapi_spec` 的 path 樣板，如 `/api/v1/users/{userId}`）——

1. 去除開頭 `/`
2. 所有 `/` 換成 `_`
3. **所有 `{paramName}` 樣板，不論原始參數名稱是什麼，一律換成字面 `{id}`**——因為 02a 的 `normalize_path_key()` 是在**實際 URL**（如 `/api/v1/users/123`）上把「純數字/UUID」換成 `{id}`，並不知道原始參數名稱叫 `userId` 還是別的，兩邊要能精確匹配，③這邊也必須捨棄參數名稱、統一用 `{id}`

```
/api/v1/users/{userId} + GET  → GET_api_v1_users_{id}
/api/v1/users/{userId}/profiles + GET → GET_api_v1_users_{id}_profiles
```

**實作提醒：步驟 1／2 要按字面實作，不要用「切段、丟掉空字串、重新 join」代替**——兩者在一般路徑上結果相同，但遇到結尾多帶 `/` 的 endpoint（如 `/api/v1/users/`）就會分岔：字面實作（只去開頭一個 `/`、其餘 `/` 全部換成 `_`）會留下結尾底線（`GET_api_v1_users_`）；「切段丟空字串」會把結尾底線吃掉（`GET_api_v1_users`）。02a `RouteMapper.normalize_path_key()` 拿到的 `url_parts` 是 Postman `request.url.path` 這個已經切好的陣列，它的邏輯只是逐段判斷數字/UUID 後原樣 `"_".join(...)`，不會主動丟棄空字串——若 Postman 對這類 URL 產出帶結尾空字串的 `path` 陣列，「切段丟空字串」的實作就會跟 `RouteMapper` 對不上。不要事後對兩邊的 key 各自做 trim 去湊一致，除非能確認 `RouteMapper` 那側也真的做了同樣的 trim。

**`route_to_module_mapping`：Harness module 分區的權威來源**

Harness 的 `get_module()`（02a 十三章、02b `core/postman_runner.py`）原本用「URL 第一個非版本路徑段」猜測一個 request 屬於哪個 module，這其實是①已經用業務語意判斷過、寫進 `api_to_python_target.module` 的同一件事——`get_module()` 完全不知道這個判斷存在，自己重新用字串規則猜一次，深層／跨模組路由（如 `/api/v1/admin/orders/audit`）因此可能被猜成 `admin`，而不是①實際判定的 `orders`（見 02a 十三章「已知限制」）。這是 00 六章反覆強調的「兩個地方需要同一件事，應該共用，不是各自重新推導」原則的反例，不是 `get_module()` 演算法不夠聰明，而是它從一開始就不該用猜的——這個資訊①早就算出來了。

**決策：③ 在計算 `route_to_file_mapping` 的同一次迴圈裡，額外輸出 `route_to_module_mapping`**（同一個正規化 key → `ApiMapping.module`，不是新的判斷，只是把六章已經算好的 `class_to_module` 結果，用跟 `route_to_file_mapping` 相同的 key 格式再存一份），寫入 `config/harness.yaml` 跟 `route_to_file_mapping` 同一層級的新段落。Harness 的 `get_module()` 改為優先查這份表，查得到就直接採用（保證跟 `ModuleInfo.module` 逐字一致，見 `06a_plan_agent_architecture.md` 四章）；查不到（真正落在①③解析範圍外的邊界情況，如 04a 十一章列出的已知限制）才 fallback 回原本「URL 第一段」的猜測，並記警告——這條 fallback 路徑本身不變，只是不再是唯一機制。

**只寫入 `config/harness.yaml`，不進 `RefactorState`**：唯一消費者是 Harness 自己讀取設定檔的既有路徑，跟 `mask_rules.yaml` 一樣是純粹供 Harness 用的設定檔案，不需要複製一份進執行期 State——`route_to_file_mapping` 雖然同時是 State 欄位也是 yaml 段落，但那是既有設計，這裡不需要對稱地也加一個新 State 欄位，只會徒增沒有消費者的資料。

**`related_files` 的組成**：一個 `ApiMapping` 項目的 `related_files` = 該項目 `module` 底下所有 `interfaces` 的 `file_path`（去重），**加上**（若該 module 有對應的 `schemas/{module}.py`）該 schema 檔案路徑——後者雖然沒有 `InterfaceSpec` 條目（五章、九章已說明原因），但作為除錯參考仍應納入，02a 的 `failure_type` 表格裡 `missing_fields`／`type_mismatch` 的 debug_hint 明確指向「檢查 Pydantic schema」，缺了這個檔案路徑會讓 Debug Agent 少一個關鍵線索。

**設計原則延續**：一個 endpoint 對應的 `related_files` 給整個 module 的檔案集合，而不是嘗試精算「這次呼叫實際上只會執行到哪幾個函式」——後者需要真正的呼叫鏈分析，成本遠高於效益（Debug Agent 本來就需要看 router/service/repository 全部三層才能定位問題），也符合 04a 反覆強調的「多連、少排除」保守精神：多給一個不相關的檔案，代價只是 Debug Agent 多看一眼；漏掉一個相關檔案，才是真正拖慢除錯的錯誤。

---

## 九、輸出格式與 State 對應

`python_structure`／`route_to_file_mapping` 直接對應 `graph/state.py` 既有的 `PythonStructure`／`InterfaceSpec`／`ParamSpec`，不重新定義結構：

```python
class ParamSpec(TypedDict):
    name: str
    type: str

class InterfaceSpec(TypedDict):
    file_path: str
    class_name: str | None
    function_name: str
    params: list[ParamSpec]
    return_type: str
    http_method: NotRequired[str | None]  # 僅 routers 層 API 邊界方法非 None，見五章
    route_path: NotRequired[str | None]   # 僅 routers 層 API 邊界方法非 None，見五章

class PythonStructure(TypedDict):
    directory_tree: str
    interfaces: list[InterfaceSpec]
```

`route_to_file_mapping: dict` 產出後直接寫入 `config/harness.yaml` 的 `route_to_file_mapping` 段（見 `00_refactor_architecture.md` 八章、`02a_harness_architecture.md` 十一章），不需人工填寫。`route_to_module_mapping`（見八章）與它在同一次呼叫中一併產出、寫進 `config/harness.yaml` 的另一個段落，但**不是** `RefactorState` 欄位（理由見八章「只寫入 config/harness.yaml，不進 RefactorState」）。

**與④的邊界**：③ 的輸出是 `translator_cli.generate_scaffold(python_project_path, python_structure, db_models=None)`（見 `07a_translator_cli_architecture.md` 四章）的 `python_structure` 這一個引數——這是③唯一貢獻的部分，不吃 `openapi_spec`。這代表：

- `interfaces`：函式簽名層級，④直接依此建立空函式骨架，這是 ⑤ 呼叫 translator-cli 填空模式的目標（00 七章④）；routers 層的 API 邊界方法額外帶 `http_method`／`route_path`（見五章），讓④知道該掛哪個 `@router` 裝飾器
- Pydantic schema／SQLAlchemy model 的**欄位內容**：`InterfaceSpec` 沒有欄位可以表達資料類別的欄位定義，因此③把這部分內容以文字形式併入 `directory_tree`（見三章、五章）；DB schema 本身（SQLAlchemy model 對應的資料表結構）不是③的職責，由④直接從既有 Postgres 測試 DB（`MOC_MATSUEXAM_TEST`，schema 已於 00 五章確認同步完成）或 Java entity 原始碼取得，封裝成 `generate_scaffold()` 的 `db_models` 引數（檔案路徑 → 完整檔案內容字串），00 七章原文即已明訂「④…建立目錄、base class、router 骨架、config、**DB schema**」，07a 四章「`db_models`：④ 自行取得的 DB schema 內容如何併入」定義了這個引數的完整契約

③ 不越界去產生 DB schema 的欄位層級規格，也不需要在 `python_structure` 之外新增 State 欄位——所有必要資訊都在既有兩個欄位的既有型別範圍內傳遞；`db_models` 是④自己組裝、經由 `generate_scaffold()` 的獨立引數傳遞，不流經③的輸出。

---

## 十、模組結構規劃

比照 `parse_agent/`、`spec_collection_agent/` 的組織方式，③ 的邏輯規劃為獨立套件 `design_agent/`，`graph/nodes/design_node.py` 維持薄封裝：

```
refactor-project/
└── design_agent/
    ├── signature_scan.py   # 四章：javalang 輕量再掃描（簽名＋stereotype），與 parse_agent/call_graph.py 各自獨立、不共用
    ├── type_mapping.py     # 五章：Java→Python 型別對應表、openapi_spec $ref 展開與覆寫邏輯
    ├── layout.py           # 三章：分層/命名規則、module 依賴拓樸排序、全域基礎設施檔案（main.py／database.py）組裝
    ├── design.py           # 六章：逐波呼叫 Claude API，組裝單一 module 的 InterfaceSpec
    ├── route_mapping.py    # 八章：機械合併 route_to_file_mapping、route_to_module_mapping
    └── prompts.py          # 六章 system prompt 集中於此
```

| 職責 | 說明 |
|---|---|
| Java 簽名／stereotype 再掃描 | 對應四章 |
| 型別對應與 openapi 覆寫 | 對應五章 |
| 分層與拓樸排序 | 對應三章、六章 |
| Claude API 逐模組設計 | 對應六章、七章 |
| route_to_file_mapping／route_to_module_mapping 機械組裝 | 對應八章 |
| 全域基礎設施檔案組裝（main.py／database.py） | 對應三章「全域基礎設施檔案」 |
| 對外唯一入口 | 供 `graph/nodes/design_node.py` 呼叫，node 本身不直接碰觸上述任何細節 |

---

## 十一、與 LangGraph 整合

`design` 讀 `module_list`／`api_to_python_target`／`openapi_spec`，寫回 `python_structure`／`route_to_file_mapping`。**`design` 是平行分支 node，不是純線性 node**：`parse` 完成後，`record_tests`（②）與 `design`（③）同時進入就緒狀態（見 `01_langgraph_architecture.md` 五章）——③ 不依賴 `golden_output`，兩者互不相依，因此不再沿用序列 `record_tests → design`。正因為如此，`design` 的回傳值**只能包含自己實際更動的 key**（`python_structure`／`route_to_file_mapping`），不能比照純線性 node 用 `{**state, ...}` 整包展開——道理同 `plan`／`scaffold`：平行分支下若對同一個 key 各自展開整包 state 寫入，會違反 LangGraph「無 reducer 時每個 key 只能被一個節點寫入」的前提。

`record_tests`／`design` 都完成後，才進入 `plan`／`scaffold`（[P]／④，另一組平行分支）；`plan`／`scaffold` 兩者都完成才進入 `implement`。

---

## 十二、錯誤處理範圍

比照 04a 九章的邊界：`retry_count` 迴圈只包住 `implement → run_tests → debug`，`design` 不在這個迴圈裡。

- Java 原始碼再掃描（四章）失敗（無法解析的語法）：直接往上拋，整條 run 中止，交由人工排查——這是輸入端問題
- 單一 module 的 Claude 呼叫失敗（六章）：先走「待重試清單、5 分鐘後重試一次」的緩衝；重試仍失敗 → **中止整條 `design` run**（不同於 04a 允許缺摘要繼續跑）——因為 `python_structure` 是 [P]／④ 唯一的權威規格，任何一個 module 的介面缺失都會讓兩者在不知情狀況下對著不完整規格工作，風險遠高於重新執行一次

---

## 十三、待決定事項

- [ ] 無 stereotype 類別的層級歸屬（三章）、框架注入物件轉換（五章）、框架慣例參數注入（七章）這三處「LLM 判斷」的實際 prompt 設計與品質，待接上真實專案輸出後校準
- [ ] `directory_tree` 的 Schema 定義段／基礎設施段／孤兒類別與資料容器占位段（三章）固定格式在 08a 設計 `generate_scaffold()` 實際解析方式時，需要反向確認本地模型（qwen2.5-coder:32b）對這個格式的辨識穩定度是否足夠，必要時調整 fenced code block 的標記慣例
- [ ] 孤兒類別／資料容器占位判斷優先序（三章）的第 4 分支（無 Lombok 標記、純欄位低信心推斷）尚未有真實案例觸發過——目前接上的 `lang-exam-api-refactor` 專案資料容器類別都有標註 Lombok annotation，仍待遇到真的沒標註的專案才能驗證這條路徑

---

## 十四、真實端對端測試發現並修正的三項缺口

以下三項是 09b 端對端整合測試才發現、原始 05a 六章版本沒有涵蓋的缺口（見 `docs/09b_bug_trace.md` #11/#12/#28/#30），已在 05b 落地並用真實環境驗證：

### 全域基礎設施檔案新增第三種：全域例外處理（對應 #11/#12）

三章「全域基礎設施檔案」原本只涵蓋 `app/core/database.py`／`app/main.py` 兩種機械組裝、不需 LLM 的檔案。`_global` 保留模組（見 `04a_parse_agent_architecture.md` 十一章）承接的 `@RestControllerAdvice` 全域例外處理，需要第三種處理路徑，但**不完全比照前兩者**——這批方法的實際邏輯（哪個例外對應什麼 code/msg）帶有業務語意，不是純樣板：

- `module["module"] == "_global"` 走完全獨立的專屬處理路徑，**不查 `_STEREOTYPE_LAYER`**，固定輸出 `app/core/exception_handlers.py`、`class_name=None`（自由函式，比照 routers 層無 class 的既有渲染慣例）、不產生 `http_method`／`route_path`。
- 函式簽名比照 FastAPI `@app.exception_handler(...)` 呼叫慣例固定為 `(request: Request, exc: Exception) -> Response`，不是 Java 原始簽名的機械轉換——FastAPI 的例外處理器協定本來就要求這個固定形狀。
- **範圍刻意收斂**：只處理 `@ExceptionHandler(Exception.class)` 這種全域 catch-all case（真實案例裡唯一有 golden output 佐證的情況）。若同一個 advice class 還有 `@ExceptionHandler(SomeSpecificException.class)` 這類更細的例外處理方法（真實案例確實存在 `handleBaseException(BaseException e)`），只記警告、不產生對應 `InterfaceSpec`——這些方法不會被 [P] 排進 task list，不會被實作，是刻意接受的限制，不是遺漏。
- `app/main.py` 新增一段：偵測到 `app/core/exception_handlers.py` 的 interface 時，機械產生 `app.add_exception_handler(Exception, {function_name})` 註冊行。
- 函式本體不在③機械產生——比照一般 service/repository 方法，經 [P] 產生 task、走⑤既有的 `fill_function()` 流程翻譯 Java handler 方法本體，維持「③只做機械骨架決策、業務邏輯留給既有 task pipeline」的既有分工邊界。

**真實環境驗證**：容器內實測確認 Starlette 的例外處理中介層確實會呼叫到⑤翻譯出的 `handle_all`（從未攔截例外時的 500 錯誤 traceback 直接看到呼叫鏈）——證實「這個方法會不會被框架呼叫到」這個架構層面的問題已解決；⑤實際翻譯出的函式本體品質（qwen 是否正確理解業務邏輯）另計，屬於翻譯品質範疇。

### 五章型別對應新增 `ResponseEntity<T>`（對應 #30）

`common/java_type_mapping.py::map_java_type()` 新增：`outer == "ResponseEntity"` 時直接回傳 `"Response"`，丟棄內層泛型參數，不落入「未知的泛型包裝類別」fallback（原本會產生不合法的 `ResponseEntity[?]`）。`design_agent/type_mapping.py` 新增 `is_response_entity_return_type()`，`resolve_api_boundary_signature()` 偵測到 Java 原始簽名以 `ResponseEntity` 開頭時，直接覆寫 `return_type="Response"`、不查 openapi 的 response schema——openapi_spec 只能表達一個代表性 status 的 body 形狀，對這種方法沒有代表性，這個資訊損失是刻意接受的，具體要回什麼交給⑤翻譯 Java 原始邏輯決定。對應地跳過 `collect_named_schemas()`，不為這類方法產生任何多餘的 Pydantic class。

**真實環境驗證**：對真實 `FileController.voice`／`image`（`ResponseEntity<FileRs>`）跑過完整 pipeline，兩者都正確標成 `return_type="Response"` 並產出可執行程式碼，不只是自建測試案例。

### `collect_named_schemas()` 遞迴收集巢狀具名 schema（對應 #28）

五章「API 邊界方法」原本的 `collect_named_schemas()` 只收集 requestBody + 第一個 2xx response 兩個**頂層**具名 schema，不遞迴走訪欄位內的巢狀 `$ref`。真實案例：`registration` 模組某方法的回應 schema 有一個欄位指向 `GetAllGradeRs`，但這個具名 schema 只有在**另一個**模組（`school`）剛好也把它當頂層 response schema 時才會被渲染出來，導致 `registration.py` 引用了一個只存在於別的模組檔案裡的 class（`PydanticUserError`）。

**決策（採簡化方案，非跨模組全域註冊表）**：讓每個 module 的 schema 檔案**自我完備**（self-contained）——`collect_named_schemas()` 新增遞迴邏輯，把巢狀 `$ref`（含陣列包裝）指向的具名 schema 一併收進來，一路收到底（`visited` 集合防止互相引用造成無窮遞迴）。**不建立跨模組的 schema 擁有權登記表**：同一個具名 schema 可能在多個 module 各自渲染一份同名 class，因為 Pydantic model 只在各自檔案內部使用，重複定義不影響正確性，換取結構上直接消除「缺 import」這整類 bug。

**真實環境驗證**：直接讀真實生成的 `app/schemas/registration.py`，確認 `GetAllGradeRs`／`GetAllClassesRs`／`GetAllSchoolRs` 全部自我完備定義在檔案內，不再需要跨檔案 import。

程式碼實作見 `05b_design_agent_code.md` 對應章節；單元測試見 `tests/design_agent/test_global_advice_module.py`、`tests/design_agent/test_type_mapping.py`（`TestResponseEntity`／`TestIsResponseEntityReturnType`／`TestResolveApiBoundarySignatureResponseEntity`／`TestCollectNamedSchemasRecursion`）。

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
