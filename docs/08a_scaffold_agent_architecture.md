# ④ 骨架實作 Agent 詳細設計

> 本文件承接 `00_refactor_architecture.md`（七、④骨架實作 Agent 一節）與 `07a_translator_cli_architecture.md` 四章「`db_models`：④ 自行取得的 DB schema 內容如何併入」——07a 十二章明訂 `db_models` 這個字典怎麼組出來屬於 08a 的範圍。本文件是這個 Agent 的**設計面**文件：決策、契約、資料結構、流程。實際程式碼實作見 `08b_scaffold_agent_code.md`（待建立）；本文件不出現可執行的實作邏輯。

---

## 一、本文件範圍與定位

**本文件涵蓋**：
- `db_models: dict[str, str]` 從哪裡來、怎麼組出來（二、三章）
- JPA entity／enum 的 Java 原始碼掃描（四章）
- 與③共用的型別／命名轉換邏輯（五章）
- table 名稱決策與跨模組索引（六章）
- Java 欄位 → SQLAlchemy Column 的對應規則（七章）
- 外鍵與集合關聯的取捨（八章）
- 模型檔案組裝、逐 entity 隔離失敗（九章）
- 已知限制（十章）
- `scaffold_agent/` 模組結構規劃（十一章）
- 與 `graph/nodes/scaffold_node.py`／`RefactorState` 的介面異動（十二章）

**本文件不涵蓋**：
- `generate_scaffold()` 拿到 `db_models` 之後怎麼併入骨架（`ast.parse()` 驗證、寫入時機、與 `interfaces` 共用的自訂型別索引）——見 07a 四章、07b `scaffold.py`，本文件只負責把字典組出來
- ③ 為什麼要跳過 JPA entity、孤兒類別／資料容器占位的其餘四種情況——見 `05a_design_agent_architecture.md` 三章，本文件只接手③明確排除在外的第 1 種情況（`@Entity`／`@Embeddable`／`@MappedSuperclass`）
- Harness 對 `app/models/{module}.py` 產出結果的驗證——見 `02a_harness_architecture.md`，本文件只保證語法正確、可以被 import，語意是否正確由 Harness 局部驗證與 ⑦ Debug Agent 把關

---

## 二、輸入與資料來源

| 輸入 | 來源 | 說明 |
|---|---|---|
| `module_list` | State，①產出 | `ModuleInfo.java_files`（JPA entity 的 `.java` 檔案本來就混在裡面，不是額外的資料來源，見四章）、`ModuleInfo.module` |
| `java_project_path` | State | 解析 `module_list.java_files` 相對路徑用 |

**不需要** `python_project_path`：`db_models` 只是純記憶體字典，寫入磁碟是 `translator_cli.generate_scaffold()` 的職責，`scaffold_agent` 本身不碰檔案系統寫入。

**不需要** `test_dsn`／連線任何 Postgres（見三章）。

---

## 三、決策：`db_models` 的來源是 Java entity 原始碼，不做即時 DB 內省

00／07a 列了兩個候選來源：既有 Postgres 測試 DB 或 Java entity 原始碼。這裡選**後者**：

1. **兩個來源都繞不開 Java entity**：「這個 entity 屬於哪個 module、要寫進 `app/models/{module}.py` 的哪一個檔案」，唯一的權威依據是 `module_list.java_files`——DB 內省列不出 table 對應哪個 module。改用 DB 內省取得欄位型別，仍然得先掃 Java entity 找出「class → module」對應，DB 內省只是疊加第二個資料來源。
2. **不引入新的執行期依賴**：DB 內省需要④在這個時間點連上測試 Postgres，而 Harness（②/⑥）已經是這條連線的唯一使用者。Java entity 原始碼跟①③一樣純靜態分析，`scaffold_agent` 因此可以獨立測試，不需要任何服務或連線就位。
3. **型別來源單一**：DB 內省要另外維護一份「Postgres 型別字串 → SQLAlchemy 型別」對應表；Java entity 原始碼直接沿用①③已經在用的 `map_java_type()`（見五章），不會出現兩份型別轉換邏輯互相漂移的風險。

**代價**：Java entity 原始碼的型別、nullable、主鍵資訊若跟手動維護的真實 SQL schema 有落差（例如 entity 忘了標 `@Column(nullable = false)`，但 DB 端確實是 `NOT NULL`），骨架階段不會發現——交給 Harness 局部驗證與 ⑦ Debug Agent 抓，骨架階段只求機械可推導的部分正確。

---

## 四、Entity 掃描

### 判斷依據

沿用 `common/java_annotations.JPA_ENTITY_ANNOTATIONS`（`@Entity`／`@Embeddable`／`@MappedSuperclass`）——③掃描 `module_list.java_files` 時遇到這批 annotation 一律跳過（05a 三章），④要做的是同一個判斷的另一半：掃同一份 `java_files`，找出這批被③跳過的 class，實際處理它們。兩邊用同一份常數，保證「③略過的」與「④接手的」是同一個集合。

### 掃描方式：獨立實作，不重用 `design_agent.signature_scan`

`design_agent/signature_scan.py` 的 `scan_java_files()` 取的是方法簽名、annotation *名稱*（`_annotation_names()` 只回傳 `ann.name`）；④需要的是 annotation 的**元素值**（`@Table(name = "...")`、`@Column(nullable = ...)` 這些具名參數的實際字面值），兩者從 AST 取出的資訊形狀不同。比照①③彼此獨立掃描 Java 原始碼的既有先例，`scaffold_agent` 用自己的輕量掃描，只取它需要的欄位，不讓 `design_agent` 背上跨 Agent 的耦合。**這裡「不重用」指的是 `scan_java_files()` 建構完整 `JavaClassSignature`（方法簽名、annotation 名稱、建構子）的那一整套流程**，不包含底層單純的型別字串還原這種更小顆粒度的機械邏輯——那一塊兩邊確實是同一件事，見五章、下方「型別字面字串還原」，已經搬進 `common/`，不是這裡說的「不重用」的例外，是不同層次的兩件事。

**掃描範圍**：逐 `module_list` 的每個 `ModuleInfo`，對其 `java_files` 逐檔 `javalang.parse.parse()`，取出：

- `ClassDeclaration` 中 annotation 名稱交集 `JPA_ENTITY_ANNOTATIONS` 非空的 class
- `EnumDeclaration`（見下方）

**每個檔案額外擷取 `package`／`import`**，供六章「跨檔案型別參照解析」使用：`tree.package.name`、`tree.imports`（逐一取 `imp.path`／`imp.wildcard`／`imp.static`，沿用 `parse_agent/call_graph.py` 既有對 `tree.imports` 的取值方式）——這兩項本來就在 javalang 解析出的同一棵 AST 裡。

**Entity 記錄欄位**：

| 欄位 | 來源 | 說明 |
|---|---|---|
| `class_name` | `decl.name` | 單純簡短名稱，FQN 組裝見六章 |
| `package` | `tree.package.name` | |
| `module` | 外層迴圈的 `ModuleInfo.module` | 決定寫進 `app/models/{module}.py` 的哪一個檔案 |
| `jpa_kind` | annotation 交集裡命中的名稱 | `"Entity"`／`"Embeddable"`／`"MappedSuperclass"`，見十章 |
| `extends` | `decl.extends`（單一 `ReferenceType`，Java 單一繼承） | 父類別的簡短名稱，`None` 代表沒有 `extends`。供六章「`@MappedSuperclass` 欄位繼承合併」解析用 |
| `table_name` | `@Table` 的 `name` 元素值 | 只有 `@Entity` 需要，見六章 |
| `unique_constraints` | `@Table` 的 `uniqueConstraints` 元素值 | `@UniqueConstraint(columnNames = {...})` 陣列，逐一取 `columnNames`，見七章 |
| `soft_delete_clause` | `@Where` 的 `clause` 元素值，或 `@SQLRestriction` 的 `value` 元素值 | 兩者擇一存在即記錄（字串 Literal，正常通過 Literal 規則），見七章「邏輯刪除標記」 |
| `fields` | `decl.fields` | 名稱、型別字面字串（`common.java_type_mapping.type_str()`，見五章、下方「型別字面字串還原」）、`is_primitive`（見下方）、該欄位上的 annotation 清單（含元素值） |
| `import_map` | `tree.imports` | 供解析欄位引用的其他型別（FK 目標、Enum、父類別）用 |

**`is_primitive`**：欄位的原始 Java 型別字面字串（`map_java_type()` 轉換**之前**）是否屬於 `{int, long, short, byte, float, double, boolean, char}` 這八個 Java 基礎型別之一。這個判斷必須在呼叫 `map_java_type()` 之前完成並獨立記錄——`map_java_type()` 把 `int`／`Integer` 都對到同一個 `"int"`，把 `boolean`／`Boolean` 都對到同一個 `"bool"`（見五章對應表），這個轉換後基礎型別跟包裝類別（wrapper）的區別就永久遺失了。這個區別對七章「Nullable 判斷」是必要資訊：Java 基礎型別在語言層級就不可能是 `null`，跟宣告成同名包裝類別（`Integer`／`Boolean`）語意完全不同，不能只看轉換後的 Python 型別字串反推。

### 同時掃描 `EnumDeclaration`

企業級 entity 的狀態欄位（`OrderStatus`／`Role` 這類）在 Java 端幾乎都是獨立宣告的 `enum`，javalang 解析成 `EnumDeclaration`（跟 `ClassDeclaration`／`InterfaceDeclaration` 平行的第三種頂層宣告）。逐一記錄：

| 欄位 | 來源 | 說明 |
|---|---|---|
| `class_name` | `decl.name` | |
| `package` | `tree.package.name` | |
| `module` | 外層迴圈的 `ModuleInfo.module` | 不決定檔案放置位置（Enum 一律渲染進共用檔案 `app/models/_enums.py`，見七章），只在同簡短類別名稱碰撞時供消歧命名使用 |
| `members` | `decl.body.constants` 逐一取 `.name` | 只取常數名稱（如 `["PENDING", "PAID"]`），不處理常數建構子引數 |

### `@ManyToMany` 欄位額外擷取 `@JoinTable`

`@ManyToMany` 關聯在關聯式資料庫裡必然對應一張獨立的中介表（join table），這張表不是任何一個 entity 自己的欄位，但它是真實存在、必須出現在 SQLAlchemy metadata 裡的資料庫物件（見八章「`@ManyToMany`：產生中介表定義」）。掃描 `@ManyToMany` 欄位時，額外檢查同一個欄位上是否有 `@JoinTable` annotation，若有，取三個具名元素（皆受「Literal 規則」約束）：

| 元素 | 說明 |
|---|---|
| `name` | 中介表的 table 名稱 |
| `joinColumns`（`@JoinColumn` 或 `@JoinColumn[]`） | 這一側（宣告欄位的 entity）的外鍵欄位名稱，取陣列第一個元素的 `name` 即可（複合欄位不處理，見十章） |
| `inverseJoinColumns`（`@JoinColumn` 或 `@JoinColumn[]`） | 另一側（`targetEntity`）的外鍵欄位名稱，同上只取第一個 |

`@JoinTable` 缺席時，這個欄位若有 `mappedBy = "..."` 元素（一般字串 `Literal`，正常通過 Literal 規則，不需要例外）→ 代表這是雙向 `@ManyToMany` 的非擁有端，中介表定義由擁有端（有 `@JoinTable` 的那一側）產生，這一側不重複渲染；`@JoinTable` 與 `mappedBy` 都缺席（完全依賴 Hibernate 命名慣例的隱性寫法）→ 無法機械決定中介表的實際名稱與欄位，走七章「無法解析型別的最終降級」同一種精神：不猜表名，記警告。

### 型別字面字串還原：`common.java_type_mapping.type_str()`

`design_agent/signature_scan.py` 原有的私有函式 `_type_str()`（把 javalang 型別節點還原成含泛型的 Java 型別字面字串，如 `List<UserDto>`，供 `map_java_type()` 解析）只處理一般型別節點的 `.name`／`.arguments`，不處理陣列——這不是恰好夠用，是這個函式原本就沒有陣列型別的使用情境（③的方法簽名、既有真實案例都沒踩到）。④渲染 entity 欄位需要處理 `byte[]` 這種陣列型別（見五章、七章），是同一個「javalang 型別節點 → Java 型別字面字串」還原動作的自然延伸，不是另一個獨立函式，因此連同陣列處理一起搬進 `common/java_type_mapping.py`（見五章「共用邏輯」），`design_agent/signature_scan.py` 改為從這裡 import。

**javalang 的陣列型別表示法（實測釐清）**：javalang **不是**用獨立的 `ArrayType` 節點類別表示陣列（`javalang.tree` 裡沒有這個類別），而是在同一個 `BasicType`／`ReferenceType` 節點上多帶一個 `.dimensions` 屬性——`byte[]` 是 `BasicType(name="byte", dimensions=[None])`，`int[][]` 的 `dimensions` 長度是 2（型別宣告情境下每個維度的值固定是 `None`）。`type_str()` 因此不需要另外處理「元素型別」這個概念，直接對同一個節點的 `.name`／`.arguments` 組出基底型別字串，依 `len(dimensions)` 接上對應層數的 `[]` 即可。

`common.java_type_mapping._SIMPLE_JAVA_TYPES`（五章）只認得 `"byte[]"`／`"Byte[]"` 這兩個單一維度、元素型別是 `byte`／`Byte` 的組合，對到 `bytes`；其餘陣列形狀（多維陣列、非 `byte` 元素型別）`type_str()` 一樣能正確組出字面字串（如 `"int[][]"`），但這個字串不在任何對應表裡，`map_java_type()` 原樣沿用，最終走七章「含 `[`／`]` 的容器型別」既有 fallback——`type_str()` 本身沒有「只處理 byte[]」的限制，限制在 `_SIMPLE_JAVA_TYPES` 這張表收哪些 key，兩者是不同層次的事。

### 掃描失敗的處理

`javalang.parser.JavaSyntaxError` 原樣往上拋，整條 `build_db_models()` 中止，不吞掉、不跳過該檔案——這批檔案已經被①③解析成功過（②是對已啟動的 Java 服務打 Postman／newman 記錄 golden output 的測試 Agent，不解析 `.java` 原始碼，見 00 三章「② 測試 Agent」），再次解析失敗代表輸入端出現本文件範圍外的異常，不是可以重試化解的暫時性錯誤。

**08b 實作層級的效能建議（不影響本文件的設計決策，供實作時參考）**：`build_db_models()` 雖然已經包了 `asyncio.to_thread()`（十二章）避免佔住 event loop，完整跑一次 `javalang.parse()` 仍是這條路徑上最貴的操作，而 `module_list.java_files` 裡絕大多數檔案（純 service／controller 邏輯，沒有任何 JPA 相關 annotation、也不是 Enum 宣告）對 `build_db_models()` 而言完全不相關。08b 可以在真正呼叫 `javalang.parse()` 之前，先用一次輕量正則掃描檔案原始文字，檢查是否包含 `@Entity`／`@Embeddable`／`@MappedSuperclass`／`@JoinTable`／`enum`（**這五個關鍵字缺一不可**，見下方）這幾個字面字串，完全沒出現才跳過——這只是輸入篩選層級的最佳化、不改變任何比對邏輯或輸出結果，篩掉的檔案本來就會被完整解析後判定「跟 build_db_models() 無關」，兩者結果等價，只是省去中間那次不會有結果的 AST 建構。

**`enum` 這個關鍵字不能漏，否則「兩者結果等價」這句話不成立**：四章「同時掃描 `EnumDeclaration`」要求獨立宣告的 Java `enum`（如 `public enum OrderStatus { ... }`，企業專案狀態列舉幾乎都是這種寫法）也要收進 `scan_index.enums`——這種檔案本來就不會有任何 JPA annotation，若正則預過濾只檢查前四個 annotation 關鍵字，會把這類檔案誤判成「跟 build_db_models() 無關」而跳過掃描，導致對應的 Enum 完全沒被收錄。後果不是報錯，而是靜默降級：任何欄位引用這個 Enum，`resolve_reference()`／`scan_index.enums` 查不到，直接落入七章「無法解析型別的最終降級」，變成不帶值域限制的 `String`，且用的是通用 TODO 文字（「可能是 Enum，需要人工或⑤確認」），不會特別提示「這其實是預過濾漏篩的」——比對照組的資訊量更少，也更難追查根因。

### Annotation 元素值萃取：只接受 `Literal`

四、六、七、八章多處需要讀 annotation 的具名元素值（`@Table(name=..., schema=...)`／`@Column(...)`／`@JoinColumn(...)`）。企業級 Java 專案常用常數代入這些元素而不是字面值，例如：

```java
@Column(length = UserConstants.MAX_EMAIL_LENGTH)
private String email;
```

`length = UserConstants.MAX_EMAIL_LENGTH` 解析出的元素值節點是 `MemberReference`，不是 `Literal`。若掃描邏輯不分辨兩者、直接把節點還原成字串塞進渲染樣板，會產出 `String(UserConstants.MAX_EMAIL_LENGTH)`——語法合法，但 `UserConstants` 在 Python 端從未定義，`ast.parse()` 抓不到（不是 `SyntaxError`，是 import 當下才會炸的 `NameError`），繞過九章「逐 entity 隔離失敗」設計的整道防線。

**規則**：任何 annotation 元素值，掃描時檢查該 javalang 節點是不是 `javalang.tree.Literal`：

- 是 → 取其字面值，正常使用
- 不是（`MemberReference`、方法呼叫、二元運算式等）→ 視同這個元素缺席，套用該元素的既有 fallback 規則，並記一筆 `logger.warning`

這是橫切關注點，下面各章不重複這段文字，只在表格裡註明「（受本節 Literal 規則約束）」。

**唯一的例外：`@Enumerated(EnumType.STRING)`／`@Enumerated(EnumType.ORDINAL)`**。這個 annotation 的元素值語法上一定是 `MemberReference`，不可能是 `Literal`；若不特別處理，`@Enumerated` 會永遠被判定「缺席」。這裡精確辨認一種固定、封閉的寫法：`MemberReference` 的 `qualifier == "EnumType"` 且 `member` 是 `"STRING"` 或 `"ORDINAL"`（`EnumType` 是 JPA 規格定義的框架列舉，只有這兩個成員，跟開放集合的專案常數性質不同）。命中才視為「有標註」，其餘任何 `MemberReference` 回到一般規則、視同缺席。用途見七章「Enum 欄位」。

---

## 五、共用邏輯：`common/java_type_mapping.py`

`map_java_type()`／`camel_to_snake()` 放在 `common/java_type_mapping.py`，③（方法簽名的型別／命名轉換）與④（entity 欄位）共用——兩邊面對的都是「Java 型別字面字串／camelCase 識別字轉成 Python 慣用寫法」，規則完全相同，沒有④專屬的變化，比照 `common/openapi_ref_resolver.py` 的既有搬遷先例。`design_agent/type_mapping.py` 從這裡 import；保留的是 API 邊界／`openapi_spec` 相關函式，只有③需要。

同一個檔案也收 `type_str()`（原 `design_agent/signature_scan.py` 的私有函式 `_type_str()`）——把 javalang 型別節點還原成含泛型的 Java 型別字面字串，是 `map_java_type()` 的輸入前一步，③④都需要，同一種搬遷理由，見四章「型別字面字串還原」。`design_agent/signature_scan.py` 同樣改為 import，不再自己維護一份。

### `java.time`／`java.util` 型別對應

`_SIMPLE_JAVA_TYPES` 涵蓋日期時間與 `UUID`：

| Java 型別 | Python 型別 |
|---|---|
| `LocalDate` | `date` |
| `LocalDateTime` | `datetime` |
| `LocalTime` | `time` |
| `Date`（`java.util.Date`） | `datetime` |
| `Instant` | `datetime` |
| `UUID`（`java.util.UUID`） | `UUID`（對到 `uuid.UUID`） |
| `byte[]`／`Byte[]` | `bytes`（見四章「型別字面字串還原」——唯一用完整陣列語法字面字串當 key 的特例） |

`Date`／`Instant` 都對到 `datetime`，不逐一區分時區細節，跟 `BigDecimal` 一樣是「損失一點資訊、換取單一穩定對應」的決策。`UUID` 這一項不可省略：企業級專案用 `UUID` 當主鍵或對外暴露的唯一識別碼是常見慣例，若欄位剛好是主鍵，缺這一項會讓整個 entity 沒有可用主鍵。

**`translator_cli/scaffold.py` 的 `_KNOWN_KEYWORD_IMPORTS`（07a 四章）需要同步補上 `date`／`datetime`／`time`／`uuid.UUID` 四筆 import 規則**：這個表服務 `interfaces`（routers／services／repositories 三層函式簽名）的渲染，跟 `db_models`（④自己組裝，見九章）是兩條獨立路徑，本來不影響本文件的 `db_models` 產出；但若③的方法簽名用到這批型別，套用上表後會產生沒有對應 import 規則的型別字串。

---

## 六、Table 名稱決策與跨模組索引

### 兩階段：先建全域索引，再逐 module 渲染

`app/models/{module}.py` 裡的外鍵欄位（八章）需要引用「其他 entity 的 table 名稱＋主鍵欄位名稱」，被引用的 entity 可能屬於另一個 module。`build_db_models()` 因此分兩階段：

**第一階段（全域）**：對所有 module 的 `java_files` 掃過一輪，建立一個單一容器 `ScanIndex`，把這個階段產出的所有資料收在一起（見下方「為什麼要包一層容器」）：

```python
@dataclass
class ScanIndex:
    entities: dict[fqn, EntityTableInfo]  # EntityTableInfo(schema, table_name, primary_key_column, primary_key_type)
    enums: dict[fqn, EnumInfo]            # EnumInfo(module, members)
    mapped_superclasses: dict[fqn, MappedSuperclassInfo]  # MappedSuperclassInfo(fields, extends, package, import_map)
    enum_python_names: dict[fqn, str]     # 見七章「Enum 欄位」同名碰撞消歧規則
    fallback_pk_type: SqlAlchemyColumnType  # 見下方「降級外鍵的型別 fallback」
    fk_use_alter_edges: set[tuple[fqn, fqn]]  # 見下方「外鍵依賴圖與循環偵測」
    join_table_owners: dict[str, fqn]     # 見八章「@ManyToMany：產生中介表定義」，table_name → 唯一渲染這張表的 entity fqn
```

**`mapped_superclasses` 是下方「`@MappedSuperclass` 欄位繼承合併」演算法第 2 步實際查詢的結構**：那段文字寫「解析到的 FQN 若命中掃描階段記錄的 `@MappedSuperclass` 類別 → 取它的 `fields`」，指的就是這個欄位——`jpa_kind == "MappedSuperclass"` 的掃描結果（四章）收在這裡，不是 `entities`（那裡明確只收 `"Entity"`），也不是憑空冒出來的資料。`MappedSuperclassInfo` 除了 `fields`／`extends`（支援下方演算法的遞迴往上找），還帶自己的 `package`／`import_map`——原因跟下方「每個欄位要連同它原始宣告處的 package／import_map 一起帶著走」是同一件事：合併演算法往上層遞迴時，每一層 `@MappedSuperclass` 自己的欄位需要用**它自己**的宣告語境解析型別參照，不能借用最終繼承它的 entity 的語境。

**為什麼要包一層容器，不是直接用兩個裸 `dict`**：`fallback_pk_type`（全域統計出的單一型別）與 `fk_use_alter_edges`（環偵測結果）都不是「以 FQN 查詢」這種字典查找，是這個階段順便算出的另外兩個獨立結果——裸 `dict` 物件沒有地方掛這兩個值，硬要掛只會產生「這個 dict 到底是純查詢表還是帶了額外屬性的特製物件」的歧義。用一個明確的 `dataclass` 把「這個階段產出的全部東西」收在一起，下游（六、七、八章）一律呼叫 `scan_index.entities[fqn]`／`scan_index.fallback_pk_type`／`scan_index.fk_use_alter_edges`／`scan_index.enum_python_names[fqn]` 存取，不會混淆。

**key 是 FQN（`package.ClassName`），不是單純的 `class_name`**：不同 package 出現同名類別（如 `com.hr.User`／`com.auth.User`）是常見模式，拿裸類別名稱當全域字典的 key，兩個不同 entity 會互相靜默覆蓋——查詢正常成功，只是查到另一個同名類別的資料，比七、八章其餘「查不到就降級」的情況嚴重得多，那些情況至少會觸發降級路徑留下痕跡，這種情況會產出一份看起來正常、實際指向錯誤 table 的骨架。`fqn` 組成：`f"{package}.{class_name}"`；`package` 為 `None` 時（沒有 package 宣告，極端罕見），改用這個檔案在 `module_list.java_files` 裡的相對路徑當前綴——`f"{java_file_relative_path}::{class_name}"`——不是單純退回裸 `class_name`：兩個不同、都沒有 package 宣告的檔案，理論上仍可能剛好宣告同名 class，退回裸 `class_name` 只是把碰撞機率降低、沒有真正消除；檔案相對路徑在 `module_list.java_files` 裡本來就保證唯一（不可能有兩個檔案共用同一個路徑），用它當 key 前綴不需要額外資訊、也不需要猜任何東西，碰撞機率直接歸零。這個 fallback 觸發時記一筆 `logger.warning`。

只收 `jpa_kind == "Entity"` 進 `scan_index.entities`（`@Embeddable`／`@MappedSuperclass` 不是獨立 table，見十章）；`scan_index.enums` 收 `EnumDeclaration` 掃描結果。`primary_key_column`／`primary_key_type` 取自 entity 的**有效欄位清單**（見下方「`@MappedSuperclass` 欄位繼承合併」，不是只看 `decl.fields` 這個 entity 自己直接宣告的欄位）裡標 `@Id` 的那一個，`primary_key_type` 是這個欄位依七章對應表算出的 SQLAlchemy Column 型別，供八章渲染外鍵欄位時決定 FK 欄位自己該用什麼型別——外鍵欄位的型別必須跟被參照的主鍵型別一致，不能一律假設是 `Integer`。若一個 entity 有多個 `@Id` 欄位（複合主鍵）或完全沒有，記為「主鍵不明確」，`primary_key_column`／`primary_key_type` 為 `None`，任何引用它當外鍵目標的欄位改走八章「外鍵解析失敗」的降級處理。

**「一個 @Id 都沒有」跟「複合主鍵」雖然都判定為主鍵不明確，但對這個 entity 自己能不能渲染的後果完全不同**：複合主鍵對 SQLAlchemy 完全合法（每個 `@Id` 欄位各自標 `primary_key=True`，`create_all()` 正常成功），只是「當外鍵目標」這件事做不到；但一個 `@Id` 都沒有時，SQLAlchemy 宣告式 class 缺少任何 `primary_key=True` 欄位不是「語法合法但語意不完整」，是 **import 當下就直接拋 `ArgumentError`**（"could not assemble any primary key columns"）——這個錯誤發生在 class 定義那一刻，會讓同一個檔案裡排在後面的其他 entity 連帶無法被定義，比九章其餘任何降級路徑都嚴重（不是 `ast.parse()` 能攔到的語法問題，是要真的 import 才會炸的執行期錯誤）。九章「逐 entity 隔離失敗」因此在渲染前多一道判斷：有效欄位清單裡完全沒有任何 `@Id` 欄位的 entity，直接跳過整個 entity、記進 `skipped_entities`，不進入渲染；複合主鍵的 entity 不受影響，正常渲染。

> 這條規則是接上真實 `lang-exam-api-refactor` 專案才發現的缺口：某些 entity 的 `@Id` 標註在共用的 `@MappedSuperclass`（如 `BaseEntity`）上，但這個父類別檔案沒有被 `module_list.java_files` 收錄到任何 module——六章「`@MappedSuperclass` 欄位繼承合併」對「解析不到父類別」的既定行為是「正常情況，不記警告」，導致這個 entity 的有效欄位清單裡完全沒有 `@Id`，一路渲染到 import 階段才炸。這不是本文件能單方面解決的輸入完整性問題（`module_list` 涵蓋率是①解析 Agent 的職責），但④在自己的邊界內能做的是：偵測到就跳過並記錄，不讓一個 entity 的問題連累同一個檔案裡其他正常的 entity。

**簡短類別名稱跨 package 撞名的偵測**：`scan_index.entities`（FQN-keyed）與 `scan_index.enums` 建完後，各自依 FQN 最後一段（簡短 `class_name`）分組統計一次，找出同一個簡短名稱對到多個不同 FQN 的分組。**依碰撞的 FQN 分別落在哪個 module，分兩種情況處理，不是一律只記一筆 warning**：

- **落在不同 module**：只記一筆 `logger.warning`（列出全部碰撞的 FQN 與 `table_name`／`members`），不改變任何渲染結果——`scan_index` 本身仍然是 FQN-keyed，④自己的渲染（entity 欄位、Enum 欄位都透過 `resolve_reference()` 解析 FQN）不受這個碰撞影響，各自的 `class` 定義落在不同的 `app/models/{module}.py` 檔案，不會互相覆蓋。示警的目的是讓十章「Entity／Enum 簡短類別名稱跨 package 撞名」這個已知限制不必等到 07a／Harness 才被動發現，見十章該節
- **落在同一個 module（只有 Entity 需要處理，Enum 已經在 `_enums.py` 用 `enum_python_names` 消歧，不會落入這個情況）**：兩個不同 FQN 的 entity 若渲染進同一個 `app/models/{module}.py`，會產生兩個同名的 `class {class_name}(Base):` 頂層定義——這在 Python 語法上完全合法，但後定義的會在執行期直接覆蓋前一個，前一個 entity 的 SQLAlchemy mapping 整個消失，且沒有任何 TODO 註解可查，比跨 module 那種「07a 索引層取錯檔案」更隱蔽（那邊至少檔案本身還在，只是被匯錯）。這裡不能沿用 Enum 的 `{class_name}_{module}` 消歧改名（理由同十章「Entity 簡短類別名稱跨 package 撞名」——entity 類別名稱已經是③ `python_structure.interfaces` 裸名稱的依據，改名只會讓③留下的裸引用變成孤兒），也不能兩者都照常渲染（必然互相覆蓋）——比照 `join_table_owners` 同一種確定性 tie-break：依 **FQN 字典序**（理由同下方「`@ManyToMany` 中介表的擁有者」一節：不依賴 `module_list` 這種沒有語意保證的上游輸出順序），字典序最小的 entity 正常渲染，同一 module 內同名的其餘 entity 全部跳過渲染、記進九章 `skipped_entities`（`{file_path, class_name, error}`，`error` 說明是同 module 類別名稱碰撞），並各記一筆 `logger.warning`——這是貨真價實的渲染失敗，不只是主動示警，`skipped_entities` 才能讓 `scaffold_node.py`／09a／⑦ 之後追溯這個 entity 為什麼整個不見了，而不是留下一個看起來完整、實際上少一個 class 的骨架檔案。

  **這一步必須在計算 `effective_fields`／`primary_keys`／`fk_use_alter_edges` 之前完成，不是等到第二階段（逐 module 渲染）才做**：輸家從 `scan_index.entities` 移除後，任何指向它的 `@ManyToOne`／`@OneToOne` 外鍵才會正確落入八章「外鍵解析失敗的降級處理」（`resolve_reference()` 算出的候選 FQN 在 `entities` 裡查不到，走 TODO 註解＋`fallback_pk_type` 那條路徑）。若這一步拖到第二階段才做，其他 entity 的外鍵欄位在渲染當下仍然查得到輸家「有效」的 `table_name`／主鍵資訊，會產生一個指向永遠不會被渲染出來的 table 的 `ForeignKey(...)`——`create_all()`／mapper 設定階段會直接拋 `NoReferencedTableError`，是比「語法合法、留 TODO 等 Harness 發現」嚴重得多的失敗等級（整個服務起不來）。因此本節的 tie-break 屬於「第一階段（全域）」的最後一步，在下方「第二階段（逐 module）」開始渲染任何檔案之前就已經定案，`ScanIndex` 最終只包含贏家。
- **跨型別（Entity vs Enum）撞名**：上面兩種統計是「各自」分開跑的——`scan_index.entities` 自己一組、`scan_index.enums` 自己一組，從未互相比對過。但 07a 四章「自訂型別索引」來源三是對整個 `db_models` 字典逐一 `ast.parse()` 取頂層 `ClassDef`，`app/models/{module}.py`（Entity）與 `app/models/_enums.py`（Enum）都在這個掃描範圍內，不分來源類別——若某個 module 有一個 Entity 叫 `Status`，另一個完全沒有跟其他 Enum 撞名（因此維持裸名、不會觸發上面 Enum-vs-Enum 那組統計）的 Enum 也叫 `Status`，兩者各自的統計都不會發現對方，但 `db_models` 裡确实同時存在 `class Status(Base)` 與 `class Status(enum.Enum)`，07a 來源三一樣會被後掃到的那個覆蓋——這正是七章「Enum class 一律渲染進單一共用檔案」那段解釋改用 `_enums.py` 的理由（「只會依 `db_models` 字典的掃描順序被後面掃到的那個檔案靜默覆蓋」）所描述的同一種失敗機制，只是換成 Entity／Enum 兩種不同來源，從側門溜回來，不是新設計解決掉的那個 Enum-vs-Enum 案例。**這一步必須等 `enum_python_names`（七章「同名碰撞改在 `_enums.py` 自己的命名階段解決」）算完才能做**：拿 `scan_index.entities` 的裸 `class_name` 集合，比對 `enum_python_names` 所有**消歧後的最終值**（不是消歧前的裸 Enum `class_name`）——Entity 永遠不改名，只有 Enum 會依自己的規則調整名稱，用最終落地的名稱比對才反映 `db_models` 實際渲染出的內容。找到交集的每一組記一筆 `logger.warning`（列出碰撞的 Entity FQN 與 Enum FQN），跟「跨 module」那組同一個處理層級——是主動示警，不是能在④邊界內修正的問題，原因跟十章「Entity／Enum 簡短類別名稱跨 package 撞名」是同一個根因（③已經對裸名稱定案，④事後改名下游看不到）

**第二階段（逐 module）**：對每個 module，用第一階段的索引渲染這個 module 自己的 entity（欄位、外鍵、Enum，見七、八章），組出 `app/models/{module}.py` 的完整檔案文字——「同一個 module 內撞名」的 tie-break（上方）此時已經在第一階段定案，`scan_index.entities` 只剩下贏家，這裡不需要（也不能）重新判斷一次。

### 跨檔案型別參照解析：Fully Qualified Name

七章「Enum 欄位」與八章「外鍵」都需要把某個欄位的裸識別字型別（如 `User`／`OrderStatus`）解析成 FQN 才能查索引，這是同一個解析問題，集中成一個共用演算法：

```
resolve_reference(simple_name, referencing_package, referencing_import_map) -> fqn | None
```

`referencing_import_map` 是這個欄位所在檔案自己的 import 表，只收非 `wildcard`、非 `static` 的 import。**`resolve_reference()` 只負責用 Java 語言的作用域規則算出一個候選 FQN，不驗證這個 FQN 是否真的存在於任何索引裡**——它不吃 `scan_index`，也不知道呼叫端要拿這個 FQN 去查 `entities` 還是 `enums`，這是刻意的分工：候選 FQN 的計算規則（顯式 import／同 package）跟「這個 FQN 查不查得到資料」是兩件獨立的事，後者依查詢目的（entity 還是 enum）而不同，讓函式本身去猜「該查哪一份」反而模糊了介面。解析順序：

1. **顯式 import 命中**：`referencing_import_map` 裡有這個 `simple_name` → 回傳對應的 `fqn` 當候選
2. **同 package 隱式參照**：同一個 package 內的類別互相引用不需要 import，第 1 步沒命中時回傳 `f"{referencing_package}.{simple_name}"` 當候選——這一步是 Java 作用域規則的直接推論（若 `simple_name` 真的指向一個使用者宣告的類別、且沒有被顯式 import，它就只能在同一個 package 裡，否則 Java 原始碼本身就編譯不過），不是用索引驗證出來的
3. **兩者都沒命中 → 回傳 `None`**：刻意不嘗試解析 wildcard import（`import com.x.*;`），沿用 `parse_agent/call_graph.py` 既有立場——wildcard 給不出具體類別名稱，不強行猜測

**existence check 一律由呼叫端做**：拿到候選 FQN（非 `None`）後，呼叫端自己查對應的索引（六章「`resolve_reference()` → `scan_index.entities`」或「→ `scan_index.enums`」，查哪一份由呼叫端的查詢目的決定）；查不到（`scan_index.entities.get(fqn)`／`scan_index.enums.get(fqn)` 回傳 `None`）視同 `resolve_reference()` 本身回傳了 `None`，走同一套降級路徑（七章 Enum 降級成 `String`、八章外鍵降級處理）——步驟 2 算出的候選 FQN 語法上一定正確，但那個位置不一定真的是索引裡收錄的 entity／enum（也可能是同 package 裡一個既非 entity 也非 enum 的普通類別），這種「候選正確但查無資料」的情況跟「根本算不出候選」在下游眼中是同一種結果，不需要 `resolve_reference()` 自己去區分。步驟 1／2 涵蓋 Java 專案裡壓倒性多數的實際寫法，漏接的案例集中在本來就故意不追的 wildcard import 範圍。

### `@MappedSuperclass` 欄位繼承合併

企業級 Java 專案常把主鍵與共通欄位（`id`／`createdAt`／`updatedAt` 這類）集中定義在一個標 `@MappedSuperclass` 的基底類別（如 `BaseEntity`），具體 `@Entity` 用 `extends BaseEntity` 繼承。若只看 `decl.fields`（entity 自己直接宣告的欄位），這個 entity 會完全看不到繼承來的 `@Id` 欄位——**不是精度打折的降級，是結構性錯誤**：`scan_index.entities` 會把它誤判成「主鍵不明確」，波及所有指向它的外鍵（八章降級）；更嚴重的是，這個 entity 自己的 SQLAlchemy class 定義若完全沒有任何 `primary_key=True` 的欄位，`mapper` 設定階段會直接拋錯，整個模組連 import 都會失敗，不是某個欄位、某個 entity 局部受影響。

**合併演算法**：entity 的**有效欄位清單**＝沿著 `extends` 鏈，從最頂層祖先往下，逐層合併每一層 `@MappedSuperclass` 的 `fields`，最後接上 entity 自己的 `fields`：

1. 若 entity 的 `extends` 非 `None`，用 `resolve_reference()`（帶入這個 entity 檔案自己的 `package`／`import_map`）解析父類別的 FQN
2. 解析到的 FQN 若命中掃描階段記錄的 `@MappedSuperclass` 類別 → 取它的 `fields`，並遞迴檢查它自己的 `extends`（Java 允許多層 `@MappedSuperclass` 疊加，如 `Order extends AuditableEntity extends BaseEntity`），直到某一層沒有 `extends`、或父類別解析不到／不是 `@MappedSuperclass`
3. 這一層停止合併，依解析結果分兩種情況：
   - **`resolve_reference()` 解析不到（回傳 `None`）**：不記警告——這是正常情況，`extends` 的父類別本來就不保證是專案內部類別（可能是第三方函式庫的基底類別、或壓根沒被 javalang 掃描到的範圍外程式碼），不是每個解析失敗都代表真的漏掉了 JPA 欄位映射
   - **解析到 FQN，但那個 FQN 命中的是 `scan_index.entities`（父類別本身是另一個 `@Entity`，即 JPA `@Inheritance` 繼承策略：`SINGLE_TABLE`／`JOINED`／`TABLE_PER_CLASS`）**：這跟「父類別解析不到」是完全不同性質的情況——父類別確實存在、確實是 JPA 管理的類別，只是不是 `@MappedSuperclass`。目前 ④ 不解析 `@Inheritance` 繼承策略，父 entity 的欄位不會被合併進來，這是真正的欄位遺漏，必須記一筆 `logger.warning`（附子類別與父類別的 FQN），跟六章其他「查得到但主動不處理」的情況（`resolve_reference()` 命中但目標不在對應索引裡）採同一套「查得到就要吭聲」的原則。此限制同時列入十章「已知限制」
4. 各層欄位依「最頂層祖先在前，entity 自己在最後」的順序接續；若同一個 Python 欄位名稱在多層出現（子類別／較近的祖先重新宣告了跟更上層祖先同名的欄位——這在 Java 語法上是合法的欄位隱藏〔field hiding〕，不是編譯錯誤，尤其常見於 `private` 欄位，Java 不要求、也不阻止子類別宣告一個與父類別同名的私有欄位），只保留**較近層級**（離 entity 本身較近）的那一筆，捨棄較遠祖先的同名欄位——渲染順序天然是「祖先在前、子類別在後」，但保留哪一筆是刻意的去重規則，不是單純接續：若照原始順序不去重直接接續，渲染出的檔案會出現兩個同名的 `mapped_column(...)` 陳述式（Python class body 允許這樣寫，語法不會炸，但後面那個賦值會在執行期直接覆蓋前面那個，前面那筆變成永遠不會生效的死程式碼，讀這份骨架的人會誤以為兩個都有作用）——去重後渲染出的檔案沒有這種誤導性的死程式碼，且結果與「不去重、讓 Python 覆蓋」的最終行為完全一致，純粹是輸出品質的改善

**每個欄位要連同它原始宣告處的 `package`／`import_map` 一起帶著走，不能套用最終擁有它的 entity 的語境**：七、八章渲染欄位時（Enum 型別解析、FK 目標解析）都要呼叫 `resolve_reference()`，這個函式吃的是「這個型別參照是在哪個檔案的 import 語境下寫的」——繼承來的欄位，它的型別參照在 Java 語法上是在**祖先類別自己的檔案**裡寫的，不是子類別的檔案。例如 `@MappedSuperclass BaseEntity`（package `com.common`）有個欄位型別是 `AuditStatus`（`import com.common.enums.AuditStatus;`，只在 `BaseEntity.java` 裡 import），具體 entity `Order`（package `com.orders`）繼承這個欄位時，Java 語法上完全不需要在 `Order.java` 裡重複 import `AuditStatus`——繼承欄位不需要子類別自己 import。若解析這個欄位型別時錯用 `Order` 自己的 `package`／`import_map`（而不是 `BaseEntity` 原本的），`resolve_reference()` 會找不到，這個繼承來的 Enum 欄位就會被誤判成「解析不到」，降級成 `String`。因此有效欄位清單裡每一筆欄位實際上是 `(field, declaring_package, declaring_import_map)` 三元組，不是單純的裸 `field`——`declaring_package`／`declaring_import_map` 取自這個欄位原本所在的那個類別（entity 自己的欄位就是 entity 自己的 `package`／`import_map`；繼承來的欄位是對應那一層 `@MappedSuperclass` 的 `package`／`import_map`），七、八章呼叫 `resolve_reference()` 一律傳這個欄位自己的語境，不是統一傳 entity 最終所在檔案的語境。

有效欄位清單（連同每筆的宣告語境）取代 `decl.fields` 成為七、八章渲染欄位、六章判斷 `primary_key_column`／`primary_key_type`、下方「外鍵依賴圖」建構邊的唯一依據。**只解 `extends`，不解 `@Embeddable`／`@Embedded` 的組合欄位**：那是透過欄位型別（`private Address address;` ＋ `@Embedded`）而非類別繼承組成，是另一種機制，目前仍是刻意排除的已知限制（見十章）。

### 外鍵依賴圖與循環偵測：只對真正成環的邊加 `use_alter`

八章「外鍵防禦性設定」需要知道哪些外鍵約束會讓 `Base.metadata.create_all()` 因為循環依賴排不出建表順序。**不是對所有外鍵一律加 `use_alter=True`**：這個專案已知未來可能導入 Alembic 做 migration 版本控管（見 `02a_harness_architecture.md` 十六章「Schema 同步機制」待實作清單），Alembic 的 autogenerate 機制會依實際 DDL 反推 migration script，無差別 `use_alter=True` 會讓每一條外鍵都多一段獨立的 `ALTER TABLE ADD CONSTRAINT`，對 autogenerate 的 diff 結果造成不必要的干擾；且 `resolve_reference()` 這套機制本來就已經在第一階段把每個 entity 的外鍵目標解析出來了，多加一個環偵測不需要重新掃描任何東西，成本比想像中低。

**建構外鍵依賴圖**：第一階段掃完全部 entity 後，逐一檢查每個 entity 的**有效欄位清單**（不是 `decl.fields`——跟上方「`@MappedSuperclass` 欄位繼承合併」是同一份清單，若這裡只看 `decl.fields`，繼承來的外鍵欄位（如基底類別上的 `tenant_id`）就不會被納入依賴圖，即使八章渲染時仍然會正確產生這條 `ForeignKey`；一旦這條漏看的邊剛好是造成循環的那一條，就不會被標記 `use_alter`，`create_all()` 可能因此建表順序失敗——這不是精度問題，是這個機制的前提被破壞）裡的 `@ManyToOne`／`@OneToOne` 欄位（`@OneToOne` 且有 `mappedBy` 的非擁有端不算，見八章），用 `resolve_reference()`（帶入該欄位自己的宣告語境，上方已強調）解析目標——解析成功且目標「主鍵不明確」以外的情況，記一條有向邊 `來源 entity fqn → 目標 entity fqn`。**自我參照（如 `manager_id` 指回同一個 entity 自己的 `id`）不算邊**：單一 table 參照自己的欄位不影響 `create_all()` 的建表順序（表只需要建立一次，FK 約束本來就能在同一張表建完後立刻加上），不是這裡要防的問題。

**環偵測**：對這個有向圖跑標準的強連通分量（SCC）演算法——任何節點數 > 1 的 SCC 代表一組真正互相依賴、找不出線性建表順序的 entity；SCC 內部的所有邊都收進 `scan_index.fk_use_alter_edges`。節點數 = 1 的 SCC（不論有沒有自我參照）不收。

**八章渲染時查 `scan_index.fk_use_alter_edges`**：邊在集合裡 → `ForeignKey(..., use_alter=True, name="fk_...")`；不在集合裡 → 一般的 `ForeignKey("table.col")`，不加 `use_alter`／`name`。真實專案裡循環外鍵是少數，多數外鍵渲染出來會是精簡的一般寫法，只有真正需要繞過建表順序問題的邊才多出 `ALTER TABLE` 這一段。

### `@ManyToMany` 中介表的擁有者：`join_table_owners`

八章「`@ManyToMany`：產生中介表定義」需要保證同一張中介表**只被渲染一次**。標準 JPA 寫法是雙向 `@ManyToMany` 只有一側標 `@JoinTable`、另一側用 `mappedBy`（見四章「`@ManyToMany` 欄位額外擷取 `@JoinTable`」），但真實企業專案不保證每一份 Java 原始碼都遵守這個規範——若兩側都標了 `@JoinTable`（同一張中介表被兩個 entity 各自宣告一次，常見於沒有嚴格 code review 的舊專案），四章的判斷邏輯（「有 `@JoinTable` 就渲染」）會讓兩個 entity 各自的檔案都產生 `user_roles = Table("user_roles", Base.metadata, ...)`：兩個模組都被 import 時，SQLAlchemy 對同一個 `MetaData` 物件註冊兩次同名 `Table` 會直接拋出 `InvalidRequestError`——這不是骨架階段的語法問題，`ast.parse()`／`ast.unparse()` 兩份檔案各自看都是合法的，只有兩者**一起被 import** 時才會炸，比 Harness 局部驗證更晚、更難定位。

**第一階段（全域）掃完全部 entity 的 `@JoinTable` 宣告後，逐一按 FQN 字典序處理**：每個帶 `@JoinTable` 的 `@ManyToMany` 欄位，取它的中介表名稱（四章 `@JoinTable.name`），若這個名稱**尚未**出現在 `scan_index.join_table_owners` → 記錄 `join_table_owners[table_name] = 這個欄位所屬 entity 的 fqn`（這個 entity 成為唯一擁有者）；若這個名稱**已經**被別的 entity 登記過 → 這一筆是重複宣告，不登記，記一筆 `logger.warning`（含兩個 entity 的 fqn、共同的中介表名稱，提示這是不規範的 Java 原始碼，非本文件的錯誤）。

**八章渲染時查 `scan_index.join_table_owners`**：只有 `join_table_owners[table_name] == 目前這個 entity 的 fqn` 才渲染 `Table(...)`；不是（代表這張表已經被另一個 entity 登記為擁有者）→ 這個欄位比照 `mappedBy` 非擁有端處理，不重複渲染、不記進 `skipped_entities`（這不是失敗，是四章「只有擁有端渲染」規則的自然延伸，只是擁有者的判定從「看 `mappedBy` 存不存在」多了一層「兩側都宣告時，字典序較小的 FQN 算數」）。**用 FQN 字典序決定先登記的是哪一個，是確定性的 tie-break，不是隨機**，這保證同一份輸入每次跑出來的結果一致、可重現；改用 FQN 而不是 `module_list` 原始順序（早期設計的既有做法），是因為 `module_list` 由 Agent ①（LLM）產出，不是這個系統裡有語意保證的排序，FQN 字典序不依賴上游 Agent 的輸出順序、人工看 log 也能直接理解「為什麼是這個 entity 贏」——這只影響「兩側都宣告 `@JoinTable`」這種不規範寫法的極端情況，正常的單側宣告不受影響。

### 降級外鍵的型別 fallback

八章「外鍵解析失敗的降級處理」需要在完全查不到目標主鍵型別時，仍然給這個欄位一個具體的 SQLAlchemy 型別。不是寫死猜 `Integer`——若這個專案的主鍵清一色用 `UUID`，寫死猜 `Integer` 命中率會系統性偏低。

**改為**：第一階段掃完全部 module 後，統計 `scan_index.entities` 裡所有成功解出 `primary_key_type` 的 entity，取出現次數最多的型別，記為 `scan_index.fallback_pk_type`；並列時 `Integer` 優先；若整個專案沒有任何 entity 解出過主鍵型別，才真正退回 `Integer`。這是資料驅動的 fallback，反映這個專案已經證實存在的慣例，計算成本很低（`scan_index.entities` 已在記憶體裡，多做一次 `Counter` 統計）。

### Table 名稱決定規則

1. **`@Table(name = "...")` 存在**（受 Literal 規則約束）→ 直接採用。00 五章已確認這個專案的 DB schema 是手動 SQL 建的，不是 `ddl-auto` 自動建表——entity 若沒有明確 `@Table(name=...)`，Hibernate 預設命名規則不保證跟手動維護的實際 table 名稱一致，因此 `@Table(name=...)` 幾乎必然存在（沒有它 Java 服務本身也連不上正確的 table）。
2. **`@Table` 缺席** → fallback 用 `camel_to_snake(class_name)`，記一筆 `logger.warning`。若這批 entity 事後被 Harness 局部驗證發現找不到對應 table，屬於預期中會發生、需要人工介入的情況，不阻擋 pipeline。

刻意不接 DB 驗證這個 table 是否真的存在——需要一條 Postgres 連線，違背三章「不引入新的執行期依賴」的決策動機。

### `@Table(schema = "...")`：多 schema 支援

企業級 PostgreSQL 專案常用多個 schema 隔離不同領域資料（如 `hr.users`）。`@Table` 除了 `name` 也可能標註 `schema`（同受 Literal 規則約束）：

- **存在** → `EntityTableInfo.schema` 記下這個值；渲染這個 entity 時 class 定義內加 `__table_args__ = {"schema": "{schema}"}`；被其他 entity 當外鍵目標引用時，`ForeignKey(...)` 字串改用 `"{schema}.{table_name}.{primary_key_column}"`——不帶 schema 前綴的參照預設只在 `search_path` 第一個 schema 裡找，找不到會直接是「relation does not exist」這種執行期錯誤
- **缺席** → 沿用 PostgreSQL 預設的 `public` schema，不渲染 `__table_args__`，`ForeignKey(...)` 字串不帶 schema 前綴

`lang-exam-api-refactor` 這個真實專案目前的連線設定只指向單一 DB、沒有出現多 schema 的跡象，這條路徑是預先支援；但實作成本低（只是多讀一個 annotation 元素、多渲染一個可選的 `__table_args__`），一併納入。

---

## 七、Java 欄位 → SQLAlchemy Column 對應

### 兩段式型別轉換

```
Java 欄位型別（如 "BigDecimal"、"LocalDateTime"、"String"）
      ↓  common.java_type_mapping.map_java_type()（五章）
Python 型別字串（如 "Decimal"、"datetime"、"str"）
      ↓  本章「Python 型別 → SQLAlchemy Column 型別」對應表（④專屬，不搬進 common/）
SQLAlchemy Column 型別（如 Numeric、DateTime、String）
```

第二段只有④需要「Python 型別 → SQLAlchemy 型別」這個方向的轉換，③不需要，不符合五章的搬遷判準：

| Python 型別 | SQLAlchemy Column 型別 |
|---|---|
| `int` | `Integer` |
| `str` | `String`（`@Column(length=...)` 存在時帶入；缺席時不帶長度，PostgreSQL 的 `varchar` 不指定長度是合法語法） |
| `bool` | `Boolean` |
| `float` | `Float` |
| `Decimal` | `Numeric`（`@Column(precision=..., scale=...)` 存在時帶入；缺席時不帶精度） |
| `date` | `Date` |
| `datetime` | `DateTime` |
| `time` | `Time` |
| `UUID` | `sqlalchemy.dialects.postgresql.UUID(as_uuid=True)`（`as_uuid=True` 讓 Python 端讀寫直接是 `uuid.UUID` 物件）。**匯入時固定別名 `from sqlalchemy.dialects.postgresql import UUID as PgUUID`**——`Mapped[UUID]` 的 `UUID` 是 Python 標準庫 `uuid.UUID`（型別註記），跟這裡的 SQLAlchemy Column 型別若都裸名匯入 `UUID` 會互相覆蓋，只要一個 entity 有 `UUID` 欄位，這兩個 import 必然同時出現。**只別名 SQLAlchemy 這一側，Python 標準庫的 `UUID` 維持裸名不加別名**：這兩者是文件本身固定會同時出現、確定必然碰撞的一組（渲染樣板寫死的），別名是解決這個確定衝突的必要手段；標準庫 `UUID` 額外跟其他「碰巧」也叫 `UUID` 的自訂型別（如某個 Java class 剛好取名 `UUID`）發生 **import 別名層級**的衝突，確實不會發生——`map_java_type()` 的 `_SIMPLE_JAVA_TYPES`（五章）對 `"UUID"` 這個字面字串做精確匹配，任何叫這個名字的 Java 型別（不論實際 package）都會先被這條規則攔下對應到 `java.util.UUID`，永遠不會進到七章「Enum 欄位」／`scan_index.enums` 那條路徑被當成自訂型別解析，也就不會有機會被排進 import 清單造成衝突，這一步的別名策略不需要再加一層。**但「型別對應結果本身正確與否」是另一件事，不能因為別名不衝突就一併斷言沒有風險**：`map_java_type()` 純粹是字面字串比對，不查 `import_map`——若某個欄位型別字面字串剛好是 `UUID`、但這個欄位宣告檔案的 `import_map`（六章「每個欄位要連同它原始宣告處的 package／import_map 一起帶著走」）顯示 `UUID` 這個簡短名稱其實顯式匯入自其他 package（`import_map.get("UUID")` 存在且不是 `"java.util.UUID"`），代表這個欄位實際指向一個自訂類別，只是剛好也叫 `UUID`，仍會被誤判成 `java.util.UUID` 渲染成 `PgUUID`。這是理論上存在的型別誤判風險，跟十章 Entity／Enum 撞名同一個等級，不該只在那邊才主動示警——七章渲染這個欄位前，比對它的 `import_map` 是否有這個訊號，命中則記一筆 `logger.warning`（附欄位所在的 entity FQN 與 `import_map` 顯示的實際來源），不改變渲染結果（仍照標準庫 `UUID` 處理），但至少不再假裝這個路徑不會被觸發——這個檢查留在④自己的欄位渲染邏輯（七章），不擴大共用的 `map_java_type()` 職責範圍（它本來就是刻意設計成不需要 import 語境的輕量比對，見五章） |
| `bytes` | `LargeBinary` |
| 含 `[`／`]` 的容器型別（`map_java_type()` 展開的泛型容器，或不支援的陣列形狀） | 不渲染這個欄位，記警告並跳過（見九章）——結構上不可能是單一純量 column，沒有「降級」這個選項 |
| 裸識別字，且 `resolve_reference()` 能解析到 `scan_index.enums` | 渲染成完整的 SQLAlchemy `Enum`＋對應的 Python `enum.Enum` class（見下方「Enum 欄位」） |
| 其餘無法辨識的純量型別（裸識別字，`resolve_reference()` 對 `scan_index.enums` 解析不到） | 降級渲染成 `String`（不帶長度）＋ `# TODO` 註解（見下方「無法解析型別的最終降級」）。這裡不查 `scan_index.entities`：能查它的欄位是標 `@ManyToOne`／`@OneToOne` 的關聯欄位，走的是八章完全獨立的渲染路徑 |

### Enum 欄位

企業級 entity 的核心業務狀態（訂單狀態、角色權限等）在 Java 端幾乎都用自訂 Enum 表示，這種欄位不能只降級成不帶值域限制的 `String`——訂單狀態若完全失去值域防護，⑤填空時容易寫入不合法字串，且要到執行期才會爆出來。

**解析流程**：欄位型別是裸識別字、且不是任何已知純量型別時，用 `resolve_reference()`（帶入這個欄位自己的宣告語境——`package`／`import_map`，繼承來的欄位用它原本所在的 `@MappedSuperclass` 的語境，不是最終擁有它的 entity 的語境，見六章「`@MappedSuperclass` 欄位繼承合併」）查 `scan_index.enums`，命中則：

1. **`@Enumerated` 模式判斷**（受 `EnumType` 例外約束）：`@Enumerated(EnumType.STRING)` 或完全沒有標註 → 走 `STRING` 路徑；明確標註 `@Enumerated(EnumType.ORDINAL)` → 不嘗試渲染成 Enum，降級成 `String`（不帶長度限制），但**不是**套用下方「無法解析型別的最終降級」那句通用 TODO 文字——那句話的語意是「`resolve_reference()` 查不到這個型別」，跟這裡的成因完全相反：這裡 `resolve_reference()` 明明已經成功解析到這個 Enum（`scan_index.enums` 查得到），是因為 `ORDINAL` 這個儲存格式本身不穩定才主動選擇不渲染，不是解析失敗。共用同一句 TODO 文字會讓事後看骨架的人或⑦ Debug Agent 誤判成「找不到型別定義」，實際上型別找得到，只是儲存格式問題。這裡改用專屬文字：`# TODO: Enum {EnumClassName} 已解析到，但標註 @Enumerated(EnumType.ORDINAL)，儲存格式（依宣告順序的整數索引）不穩定，降級為 String，需要人工或⑤確認實際欄位語意`（`EnumClassName` 帶進實際的 Enum 名稱，不是佔位字串，比通用降級的 `SomeType` 更明確，因為這裡確實知道是哪一個 Enum）。JPA 規格預設值其實是 `ORDINAL`，但業界共識高度不建議依賴這個預設值（`ORDINAL` 存的是常數宣告順序的整數索引，日後調整 Enum 常數順序會讓既有資料語意錯位），實務上明確標 `EnumType.STRING` 是壓倒性主流寫法，完全不標註反而多半是舊程式碼技術債——缺少標註時賭 `STRING` 命中率更高，猜錯的後果不會比明確標 `ORDINAL` 時直接放棄更差
2. **產生 Python `enum.Enum` class**：`members` 逐一渲染成 `NAME = "NAME"`（成員值＝成員名稱本身，對應 `EnumType.STRING` 的實際持久化格式）
3. **欄位渲染**：`mapped_column(SqlEnum(OrderStatus, native_enum=False, length=255), nullable=...)`（`length` 見下方），型別註記 `Mapped[OrderStatus | None]`（`SqlEnum` 是 `from sqlalchemy import Enum as SqlEnum` 的別名匯入，避免跟 Python 標準庫 `enum.Enum` 撞名）

**`native_enum=False` 是必要參數，不能省略**：SQLAlchemy 的 `Enum` 型別搭配 PostgreSQL dialect 時，預設（`native_enum=True`）會對應到 Postgres 原生 `CREATE TYPE ... AS ENUM` 型別。但 00 五章已確認這個專案的 DB schema 是手動 SQL 建的，`@Enumerated(EnumType.STRING)` 對應的實際欄位在絕大多數手動維護的 schema 裡就是普通 `VARCHAR`，不是 Postgres 原生 enum 型別——用 `native_enum=True` 會讓 SQLAlchemy 對這個欄位的型別認知跟 DB 實際的欄位型別不一致。`native_enum=False` 讓 SQLAlchemy 把這個型別當成「值域受限的字串」處理（底層是 `VARCHAR`＋應用層檢查），對齊實際 schema 的慣例。

**`length` 明確帶入，不依賴 SQLAlchemy 自動計算**：`@Column(length = N)` 存在時（受 Literal 規則約束）直接採用；缺席時**固定用 `255`**，不留給 SQLAlchemy 依目前 Enum 成員裡最長的名稱自動計算——JPA／Hibernate 的 `@Column` 規格預設 `length` 就是 `255`，這是 `@Enumerated(EnumType.STRING)` 沒有明確指定長度時實際落地的欄位長度慣例。若改用 SQLAlchemy 自動計算（依「目前」成員最長名稱），今天算出的長度只是巧合地夠用，未來 Enum 新增一個比現有成員都長的成員時，這個舊骨架當初鎖定的長度就會過短，造成資料寫入截斷——`255` 是跟 JPA 實際行為對齊、面向未來成員擴充的穩定選擇，不是隨便挑的數字。

**Enum class 一律渲染進單一共用檔案 `app/models/_enums.py`，不放進任何 entity 所屬的 `app/models/{module}.py`**：`scan_index.enums`（六章）裡每一個 FQN 都在這個檔案渲染出恰好一個 `class`，任何 module 的 entity 需要引用某個 Enum 時，一律 `from app.models._enums import {EnumClassName}`，沒有例外——不存在「這個 Enum 归屬某個 module，其他 module 才需要跨檔案匯入」這種區分，`_enums.py` 本身不歸屬任何 `ModuleInfo.module`，性質上比照 05a 三章「全域基礎設施檔案」（`app/main.py`／`app/core/database.py`）同一種「跟業務模組無關、全域共用」的檔案。

**不採用「就地重複定義」（讓 Enum 定義放在擁有 module 的檔案裡，跨 module 引用時匯入；兩個 module 的 Enum 互相引用形成匯入環時，在其中一側就地重新渲染一份相同定義以避免 `ImportError`）這種替代設計**：那個做法有一個解不開的後遺症：`db_models` 裡因此可能同時存在兩份**同名但獨立定義**的 Enum class（分別在兩個檔案裡），07a 四章「自訂型別索引」的來源三對 `db_models` 做 `class_name → file_path` 索引時，並不知道也不會處理「這個名字在 `db_models` 內部自己就重複了」這種情況——只會依 `db_models` 字典的掃描順序被後面掃到的那個檔案靜默覆蓋，`interfaces` 渲染時 import 到的可能是「循環中被迫就地重複的那份拷貝」，不是設計上該用的那一份。這本身不會讓程式碼壞掉（兩份定義的成員字面值完全一致），但兩個獨立的 `class OrderStatus(enum.Enum)` 物件在 Python 裡是不同的型別——若程式碼裡有跨檔案的 Enum 值比較（如 `order.status == target_status`），兩邊剛好各自來自不同那一份定義時，`==`／`isinstance` 的判斷會不符合預期，而且這種問題只存在於 Python 物件層級，序列化成 JSON 之後看不出差異，Harness 的黃金輸出比對抓不到。**單一共用檔案讓整個問題連前提都不成立**：只會有一份 `class OrderStatus`，沒有「兩份定義、選到哪一份」這個疑慮，也不需要另外設計一套「哪一份是典範、哪一份要從索引排除」的例外規則——07a 既有的自訂型別索引機制完全不用跟著改，因為 `db_models` 裡本來就不會出現同名重複的 `ClassDef`。

**同名碰撞改在 `_enums.py` 自己的命名階段解決**：`scan_index.enums` 是 FQN-keyed（六章），不同 package 出現同簡短名稱的 Enum（如 `com.hr.Status`／`com.billing.Status`）在 Java 端本來就合法；全部塞進同一個 Python 檔案後，若直接用簡短類別名稱，會變成同一個檔案裡兩個 `class Status(enum.Enum)`，後定義的覆蓋前一個。渲染 `_enums.py` 前，先統計 `scan_index.enums` 所有 FQN 的簡短類別名稱出現次數：

1. 只出現一次 → 直接用簡短名稱
2. 出現多次（真正撞名）→ 組內每一筆先各自試 `f"{class_name}_{EnumInfo.module}"`（如 `Status_hr`／`Status_billing`，用擁有這個 Enum 的 Java module 名稱消歧，可讀、確定性）
3. **第 2 步算出來的候選名稱，同一組內若彼此還是有重複**（例如 `com.hr.enums.Status` 與 `com.hr.legacy.Status` 剛好屬於同一個 `module`，兩者都會算出 `Status_hr`——消歧本身又撞名一次）→ 只有這批仍然重複的成員，改用 `f"{class_name}_{package.replace('.', '_')}"`（package 全路徑，底線取代點號，如 `Status_com_hr_enums`／`Status_com_hr_legacy`）。這一步保證不會再撞：兩個 FQN 若 `class_name` 與 `module` 都相同，`package` 必然不同（否則就是同一個 FQN，不是碰撞），用完整 package 路徑消歧在數學上是碰撞免疫的最後一道防線，不需要再往下一層。**`package` 為 `None` 時**（六章「跨檔案型別參照解析」的 FQN fallback——該檔案沒有 `package` 宣告，FQN 退化成 `f"{檔案相對路徑}::{class_name}"`）：不能直接對 `None` 呼叫 `.replace()`，沿用同一個 FQN 退化邏輯，改用 FQN 裡 `::` 前面那一段（檔案相對路徑）取代 `package`，一樣把路徑分隔符與點號換成底線：`f"{class_name}_{fqn.split('::')[0].replace('/', '_').replace('.', '_')}"`。理由跟六章的 fallback 相同：檔案相對路徑在 `module_list.java_files` 裡本來就保證唯一，用它消歧一樣是碰撞免疫的，不需要另外發明一套規則

第 2 步已經唯一的成員維持模組名後綴（較短、較可讀），不因為同一組裡有其他成員需要退到第 3 步而跟著降級——只有真正還在撞的那幾筆才用比較長的 package 全路徑命名。這份「FQN → 最終 Python class 名稱」的對應收進 `scan_index.enum_python_names: dict[fqn, str]`（六章，跟 `fallback_pk_type`／`fk_use_alter_edges` 一樣是 `ScanIndex` 的一個欄位），七章渲染欄位、`_enums.py` 本身渲染 class 定義時都查這份對應，不是直接拿 FQN 的最後一段當 class 名稱用。

### 建構子引數還原（對應 `docs/09b_bug_trace.md` #44 根因的一部分）

上方「產生 Python `enum.Enum` class」原本只取 `decl.body.constants` 的成員**名稱**（`NAME = "NAME"`），完全不處理 Java enum 常見的建構子參數寫法（如 `CommonErrorCode(int code, String msg) { ... } SUCCESS(200, "操作成功")`）——對本章原本設想的情境（JPA entity 欄位、`EnumType.STRING` 持久化格式，如 `OrderStatus { PENDING, PAID }`）這是正確、刻意的選擇：SQLAlchemy 的 `Enum` 型別持久化用的是成員的 `.name`，不是 `.value`，成員名稱本來就是唯一需要保留的資訊。但 09b 端對端測試接上①的 `_extract_enums()`（`04a_parse_agent_architecture.md` 十二章）後才發現：`scan_index.enums`（六章）不只收「entity 欄位引用到的 enum」，而是**無條件收整個 `module_list.java_files` 裡所有掃到的 `EnumDeclaration`**——這包含大量根本不是 entity 欄位、單純帶著業務資料的「常數類別」enum（如自訂錯誤碼列舉），這批 enum 的建構子參數（`code`／`msg`）才是它們存在的**唯一理由**，原本的「只取名稱」渲染會讓這些數值資訊整個遺失（`CommonErrorCode.SUCCESS(200, "操作成功")` 渲染成只剩 `SUCCESS = "SUCCESS"`）。

**決策：`entity_scan.py::scan_module()` 額外還原建構子參數，`model_builder.py` 據此決定渲染成帶值的 enum**：

- `EnumRecord` 新增 `constructor_params: list[str]`（建構子參數名稱，依宣告順序）與 `member_args: dict[str, list[object]]`（member 名稱 → 對應引數的字面值清單）。取哪個建構子：正常只有一個（Java enum 多載建構子極少見）；真的出現多個時用第一個宣告的參數清單當唯一依據，記警告——不追求完美（不同常數理論上可能呼叫不同多載），只求比完全不處理更接近事實。沒有建構子（一般狀態列舉）→ 兩者皆為空，維持修改前的既有行為。
- **Literal 規則**（同四章「Annotation 元素值萃取」同一套精神）：一個常數的引數清單若跟建構子參數數量對不上、或任一引數不是 `javalang.tree.Literal`，整個常數視同沒有可用的建構子引數，不寫進 `member_args`，記警告——不嘗試部分還原。
- **渲染時全有全無，不逐常數各自判斷**：`record.constructor_params` 非空、且每一個 `record.members` 都在 `record.member_args` 裡有對應項目（`len` 相等，代表沒有任何常數在掃描階段被跳過）時，才渲染成帶值的 enum：`SUCCESS = (200, "操作成功")` ＋ `def __init__(self, code, msg): self.code = code; self.msg = msg`；否則整個 enum 退回原本「只渲染名稱」的寫法。**這不是保守，是必要**：Python `enum.Enum` 一旦定義 `__init__`，所有成員的賦值都必須是同一種形狀（tuple 對應多參數 `__init__`），若這個 enum 裡有些常數有還原出引數、有些沒有，硬要混合渲染會讓沒有引數的常數在 `__init__` 呼叫時缺參數，直接在 class body 求值階段拋 `TypeError`——比完全不渲染值更嚴重（原本只是資訊遺失，這樣會整個檔案 import 失敗）。
- **不影響既有的 `EnumType.STRING` 持久化語意**：SQLAlchemy 的 `mapped_column(SqlEnum(OrderStatus, ...))` 持久化用的是成員的 `.name`（不是 `.value`），這裡新增的 `.code`／`.msg` 這類屬性是額外附加的資料，不改變 `.name` 本身，即使某個 entity 欄位引用到的 enum剛好也帶建構子參數，既有的欄位渲染（`mapped_column(SqlEnum(...))`）完全不受影響。

**真實環境驗證**：對真實 `CommonErrorCode.java`（16 個成員，`code`／`msg` 兩個建構子參數）跑過完整 `build_db_models()`，渲染出的 `_enums.py` 通過 `ast.parse()`，`exec()` 後 `CommonErrorCode.SUCCESS.code == 200`、`.msg` 為真實 Java 原始碼裡的中文訊息文字（非亂碼，UTF-8 正確保留）——不只是語法正確，是數值正確。

程式碼實作見 `08b_scaffold_agent_code.md` 對應章節；單元測試見 `tests/scaffold_agent/test_entity_scan.py`（`test_scan_enum_with_constructor_args_extracts_values`／`test_scan_enum_arg_count_mismatch_skips_that_member`）、`tests/scaffold_agent/test_model_builder.py`（`test_enum_with_constructor_args_renders_values_not_just_names`／`test_enum_with_partial_constructor_args_falls_back_to_name_only`）。

### 無法解析型別的最終降級

裸識別字既不是已知純量型別、也解析不到 `scan_index.enums`（例如透過 wildcard import 引用、或型別不在 `module_list.java_files` 掃描範圍內）時，降級渲染成 `String`（不帶長度限制），並在欄位上方加一行 `# TODO` 註解記下原始 Java 型別（例如 `# TODO: 未知型別 SomeType，降級為 String，可能是 Enum（無法解析引用來源）或其他自訂型別，需要人工或⑤確認`）。**這條路徑的前提是「型別解析失敗」，跟上方「Enum 欄位」`EnumType.ORDINAL` 那條路徑不一樣**（那裡是型別解析成功、只是主動選擇不渲染，用的是專屬 TODO 文字，不是這裡的通用文字）——兩者最終都降級成不帶長度的 `String`，渲染結果看起來一樣，但成因不同，TODO 文字必須分開寫，讓事後排查的人知道該往哪個方向查。這個 TODO 註解保留機制跟八章「外鍵解析失敗的降級處理」用的是同一套：`db_models` 走原始字串直寫、不經 `ast.unparse()` 重新格式化，註解不會被工具鏈丟掉。`String` 是這條防線唯一合理的選擇——不知道實際型別時，`String` 能承載絕大多數可能的資料形狀，且比直接跳過欄位保留更多資訊。

### `UUID` 主鍵的預設值：`default=uuid4()`

`autoincrement` 是關聯式資料庫「數值自增」的概念，對 `UUID` 型別沒有意義。`@Id`＋`@GeneratedValue` 因此依欄位型別分流：`Integer` → `autoincrement=True`；`UUID` → 機械補上 `default=uuid4`（`from uuid import uuid4`）。

**不用 PostgreSQL 端函式（如 `gen_random_uuid()`）**：這是 PostgreSQL 13 才內建到核心的函式，更早版本需要額外啟用 `pgcrypto`；`scaffold_agent` 依三章決策不連線任何 Postgres，沒有管道確認目標 DB 版本或擴充套件——寫死 `server_default=text("gen_random_uuid()")` 是對無法驗證的環境細節下賭注，猜錯會讓這個 table 的 `CREATE TABLE` 直接執行失敗。`default=uuid4`（Python 端）零環境假設、任何 Python ≥ 3.10 環境都能跑，`uuid4()` 是 128-bit 加密亂數，碰撞機率低到可忽略。

**這個決策沒有涵蓋的情境**：`default=` 只在寫入經過這個 SQLAlchemy ORM session 時才生效——若這個 table 未來被其他服務或程式直接 `INSERT`，繞過 ORM 層的寫入仍會撞 `NOT NULL`。這是否是真實風險取決於這個 table 未來會不會有其他寫入者，`scaffold_agent` 拿不到這個資訊，因此機械補上的這一行附帶一行提示註解：`# 若此表可能被其他服務或程式直接寫入（不經過本服務的 ORM），建議改用 server_default=text("gen_random_uuid()")（需確認目標 PostgreSQL ≥ 13 或已啟用 pgcrypto）`。

### 稽核時間戳：`@CreatedDate`／`@LastModifiedDate`

Spring Boot 專案高度依賴這組（或 Hibernate 的 `@CreationTimestamp`／`@UpdateTimestamp`）自動維護 `created_at`／`updated_at`。若骨架完全忽略，這些欄位在 nullable 規則下若被判定 `nullable=False`（例如標了 `@Column(nullable = false)`），⑤填空時若沒有手動補值會直接違反約束。

- `@CreatedDate`／`@CreationTimestamp` → 額外帶 `server_default=func.now()`
- `@LastModifiedDate`／`@UpdateTimestamp` → 額外帶 `server_default=func.now(), onupdate=func.now()`（初次寫入時給預設值，之後每次更新自動刷新）

**`@CreatedDate`／`@LastModifiedDate` 只在專案有 `@EnableJpaAuditing` 時才視為生效，`@CreationTimestamp`／`@UpdateTimestamp` 不受這個條件限制**：前者是 Spring Data JPA 的稽核機制，需要 `@EnableJpaAuditing`（還需要 `AuditingEntityListener` 掛勾，但那個前提條件如果連 `@EnableJpaAuditing` 都沒有就一定不成立，只查這個當保守的必要條件已經足夠）才會真的由框架寫入值；後者是 Hibernate 原生機制，不需要任何額外設定就會生效。兩者語法上長得像、常被搞混，但生效條件完全不同——`lang-exam-api-refactor` 專案的 `BaseEntity` 用 `@CreatedDate`／`@LastModifiedDate`，但整個專案沒有任何 `@EnableJpaAuditing`（也沒有 `@EntityListeners(AuditingEntityListener.class)`），這兩個 annotation 純粹是裝飾，Java 端 `created`／`updated` 永遠是 `null`；若骨架不判斷這個條件、機械生成 `server_default=func.now()`／`onupdate=func.now()`，Python 版本會在每次寫入／更新後自動填值，跟 golden output 對不上（`docs/09b_bug_trace.md` #69）。

判斷方式是對整個 `java_project_path` 做一次字面字串搜尋，檢查有沒有任何檔案出現 `@EnableJpaAuditing`——這是一次性的專案級二元判斷，不需要 javalang 解析成結構化資訊。掃描到 `@CreatedDate`／`@LastModifiedDate` 但這個條件不成立時，欄位仍然正確辨識出來，只是不產生 `server_default`／`onupdate`，保留一般 nullable 欄位的行為，貼近 Java 端 annotation 裝飾但實際上沒有生效的真實狀態。

兩者都需要 `from sqlalchemy import func`。Java 端這兩個 annotation 實際上是**應用層**在 persist／update 前寫入值（Spring Data JPA 透過 `AuditingEntityListener`），不是 DB 端觸發——這裡改用 DB 端 `server_default`／`onupdate` 達成的是**同樣的最終可觀察行為**（欄位確實會被自動填入、自動更新的時間戳），機制不同但效果對齊，是骨架階段能機械做到的最接近選擇；若這兩個時間戳的實際來源需要跟應用層邏輯完全一致（例如同一個 request 內多次寫入要共用同一個時間戳），交由⑤視需要調整。

**這幾個欄位的 Column 型別固定用 `DateTime(timezone=True)`，不是七章對應表原本的裸 `DateTime`**：PostgreSQL 的 `func.now()` 回傳帶時區的 `TIMESTAMPTZ`，若欄位型別宣告成不帶時區的 `DateTime`（對應 `TIMESTAMP WITHOUT TIME ZONE`），型別跟這裡機械塞進去的 `server_default` 不一致，某些驅動（如 asyncpg）在這種不一致下可能發出警告甚至影響讀回時的時區處理。**只有稽核時間戳這幾個欄位改用 `timezone=True`，不是把七章「Python 型別 → SQLAlchemy Column 型別」對應表裡 `datetime → DateTime` 這條規則整條改掉**：Java 的 `LocalDateTime` 語意上就是不帶時區的「地方時間」，一般的 `LocalDateTime` 欄位維持裸 `DateTime`（不帶時區）才是跟原始 Java 型別語意一致的對應；只有這裡因為改用 `func.now()` 這個帶時區的 DB 端函式，才需要讓 Column 型別跟著調整以匹配，是這個特定情境的例外，不是通用規則的修正。

### 唯一約束：`@Column(unique = true)`

欄位級唯一約束直接對應 `mapped_column(..., unique=True)`；DB 層級的唯一性防線若在重構階段消失，Harness 的正常路徑測試不一定能發現（併發寫入才會踩到），骨架階段機械保留是低成本、高必要性的對應。

`@Table(uniqueConstraints = @UniqueConstraint(columnNames = {...}))` 是 table 層級的複合唯一約束（受 Literal 規則約束，`columnNames` 是字串陣列），四章已記錄在 `unique_constraints`，渲染進 `__table_args__`（跟六章「多 schema 支援」的 `__table_args__` 是同一個機制，`schema` 與 `UniqueConstraint(...)` 可以同時出現）：

```python
__table_args__ = (UniqueConstraint("email", "tenant_id", name="uq_users_email_tenant_id"), {"schema": "hr"})
```

**`name=` 固定用 `f"uq_{table_name}_{'_'.join(columns)}"`**（如上例 `table_name="users"`、`columns=["email", "tenant_id"]` → `"uq_users_email_tenant_id"`），不是隨意取名——這個組成規則保證同一張表底下不同欄位組合的約束不會撞名（欄位組合不同，名稱字面就不同），跟八章外鍵約束的命名規則（見八章）是同一種「內容決定名稱」的機械規則，不需要額外去重檢查。若同一張表出現兩個**欄位組合完全相同**的 `@UniqueConstraint`（Java 原始碼本身重複宣告，理論上不該發生），這個公式算出的名稱會撞名——這種情況視為輸入端資料異常，不在這裡特別處理，比照本文件其餘「機械規則對合理輸入必然正確，不窮舉病態輸入」的既有立場。

**只有一個 `UniqueConstraint`、且沒有 `schema` 時，`__table_args__` 必須渲染成單元素 tuple，尾隨逗號不能省略**：`__table_args__ = (UniqueConstraint(...))`（沒有逗號）在 Python 語法上不是 tuple，只是括號包住的單一運算式，求值結果就是那個 `UniqueConstraint` 物件本身——SQLAlchemy 的 `__table_args__` 只接受 tuple 或 dict，收到裸 `UniqueConstraint` 物件會直接報錯。正確寫法是 `__table_args__ = (UniqueConstraint("email", name="uq_users_email"),)`。這個陷阱只在**恰好一個元素**時出現：兩個以上 `UniqueConstraint`（`(UC1, UC2)`）或 `UniqueConstraint` 加 `schema`（`(UC1, {"schema": ...})`）本來就有兩個以上元素，逗號分隔語法天然合法，不需要特別處理；只有「唯一約束只有一條、且沒有 `schema`」這個最常見的組合，才是真正需要小心補上尾隨逗號的情況——反而是最容易被實作時忽略的一格。`schema` 存在、`unique_constraints` 為空時，`__table_args__` 直接是裸 dict（`{"schema": "hr"}`），不包一層 tuple，SQLAlchemy 對 `__table_args__` 是 dict 的情況原生支援，不需要湊成 tuple。

`@Table(indexes = ...)`（純索引，非唯一）不在本文件處理範圍——索引缺失影響的是查詢效能，不是資料完整性，跟唯一約束缺失（可能允許重複資料寫入）性質不同，見十章。

### 邏輯刪除標記：`@Where`／`@SQLRestriction`

Hibernate 的 `@Where(clause = "...")`／`@SQLRestriction(value = "...")` 對整個 entity 套用全域查詢過濾（常見於軟刪除模型，如 `deleted = false`）。SQLAlchemy 沒有對等的宣告式機制（要複製同樣的行為需要在 session／engine 層級掛 `with_loader_criteria()` 事件監聽或自訂 Query class，這是跨越單一 entity 渲染範圍的整體查詢管線設定，不是骨架階段能決定的事）。

四章掃描到這個 annotation 時，`soft_delete_clause` 有值 → 在這個 entity 的 class 定義正上方渲染一行醒目註解：

```python
# TODO: 此實體原有邏輯刪除過濾條件（deleted = false），SQLAlchemy 未自動套用，
# 所有查詢須手動加上對應條件，否則會讀到已刪除的資料
class Order(Base):
    ...
```

這行註解會被保留（理由同八章「這個註解會被保留」——`db_models` 走原始字串直寫，不經過 `ast.unparse()`）。⑤填空涉及這個 entity 的查詢方法時，`context_files` 一定包含這個 model 檔案（06a 七章「models/{module}.py 為什麼是無條件加入」），這行 class 級註解因此保證會被⑤看到，不需要額外的跨 Agent 資料通道。

### 欄位級 annotation（元素值，受 Literal 規則約束）

| annotation | 影響 |
|---|---|
| `@Id` | `primary_key=True`；`@GeneratedValue` 且欄位型別是 `Integer` → `autoincrement=True`；欄位型別是 `UUID` → `default=uuid4`（見上方） |
| `@Column(name = "...")` | **只覆蓋 DB 欄位名稱，不影響 Python 屬性名稱**（見下方說明） |
| `@Column(nullable = false)` | `nullable=False`（明確標註一律優先套用，不論欄位是不是 Java 基礎型別）；`@Column(nullable=...)` 缺席時，依欄位的 `is_primitive`（四章）分流：基礎型別（`int`／`long`／`short`／`byte`／`float`／`double`／`boolean`／`char`）→ `nullable=False`（Java 語言層級就不可能是 `null`，型別註記不加 `| None`）；包裝類別或其他型別 → `nullable=True`（不額外解析 Bean Validation 的 `@NotNull`／`@NotBlank`，見十章） |
| `@Column(length = N)` | 只在型別是 `String` 時套用 |
| `@Column(precision = P, scale = S)` | 只在型別是 `Numeric` 時套用 |
| `@Column(unique = true)` | `unique=True`（見上方「唯一約束」） |
| `@CreatedDate`／`@CreationTimestamp`／`@LastModifiedDate`／`@UpdateTimestamp` | 見上方「稽核時間戳」 |
| `@Transient` | 這個欄位完全不渲染 |
| 其他未列出的 annotation | 忽略，不影響渲染 |

**`@Column(name = "...")` 覆蓋的是 DB 欄位名稱，Python 屬性名稱固定是 `camel_to_snake(field_name)`，兩者不是同一件事**：Python 屬性名稱（`mapped_column(...)` 賦值左邊那個名字）永遠是 `camel_to_snake(field_name)`，不論 `@Column(name=...)` 有沒有標註——這是給 Python 這邊看的識別字，維持 Python 慣例最重要，不該因為 Java 端的 DB 欄位命名習慣（常見全大寫加底線，如 `EMAIL_ADDR`）就跟著把 Python 屬性名稱也弄成不符合 PEP 8 的寫法。`@Column(name=...)` 的值只在**DB 欄位名稱跟 Python 屬性名稱不同**時才需要額外提供給 `mapped_column()`（SQLAlchemy 預設用屬性名稱當 DB 欄位名稱，沒有明確指定時兩者本來就相同，不需要多寫）：

```java
private String emailAddress;
@Column(name = "EMAIL_ADDR")
```

```python
email_address: Mapped[str] = mapped_column("EMAIL_ADDR", String(100), nullable=False)
```

`mapped_column()` 的第一個位置引數是 DB 欄位名稱字面字串（沒有指名關鍵字，SQLAlchemy 用位置判斷），第二個才是七章對應表算出的 SQLAlchemy Column 型別，其餘關鍵字引數（`nullable`／`unique`／`primary_key` 等）順序不變接在後面。`@Column(name=...)` 缺席時（DB 欄位名稱等於屬性名稱的一般情況）不多這個位置引數，維持既有渲染範例的寫法（如 `total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)`）不變。

### 渲染風格：SQLAlchemy 2.0 `Mapped`／`mapped_column()`

00 三章已定案目標 Python 服務執行環境 ≥ 3.10。④渲染 entity 欄位改用 SQLAlchemy 2.0 的 `Mapped[...]`／`mapped_column(...)` 宣告風格，不是 1.x 傳統的 `id = Column(Integer, ...)` 寫法：

- `Mapped[X]` 的型別引數直接用第一段（`map_java_type()`）的輸出，`nullable=True` 的欄位額外加 `| None`（PEP 604）
- `mapped_column(...)` 第一個位置引數固定明確帶入本章對應表算出的 SQLAlchemy Column 型別（`@Column(name=...)` 存在時例外，DB 欄位名稱字面字串才是第一個位置引數、型別退到第二位，見上方「`@Column(name = "...")` 覆蓋的是 DB 欄位名稱」），不依賴 SQLAlchemy 從 `Mapped[X]` 自動推斷 SQL 型別——自動推斷是 SQLAlchemy 內部的隱性對應表，跟這裡自己維護的對應表是兩份獨立來源，明確傳入才能保證這個欄位的 SQL 型別唯一依據就是本章這張表
- `nullable`／`primary_key`／`autoincrement`／`default` 等關鍵字引數寫法與 1.x 的 `Column(...)` 相同，只是外層換成 `mapped_column(...)`，變數宣告改成帶型別註記
- `Base = declarative_base()`（05a 三章固定樣板）與 `Mapped`／`mapped_column` 相容，不需要改動這個固定樣板
- **每個 db_models 檔案（`app/models/{module}.py`／`app/models/_enums.py`）開頭固定加 `from __future__ import annotations`**：跟 07a 四章對 `interfaces`（routers／services／repositories）三層檔案的既有規定一致，理由相同——這裡的欄位型別字串一樣來自 `map_java_type()`（五章），跟 `interfaces` 共用同一套殘留風險（07a 四章「型別字串正規化」提到的萬用字元泛型等尚未窮舉的殘留情況），套用同一道防線是把兩邊的一致性做完整，不是只顧到 `interfaces` 那一半；額外好處是讓整個生成專案的檔案開頭慣例統一，不需要讓⑤／人工去記「這一批檔案有這行、那一批沒有」的差異

**渲染範例**（`Order` entity：`id` 標 `@Id`／`@GeneratedValue`，`total: BigDecimal` 標 `@Column(nullable = false, precision = 10, scale = 2)`，`quantity: int`（Java 基礎型別，沒有任何 `@Column` 標註），`created_at: LocalDateTime`，`status: OrderStatus` 標 `@Enumerated(EnumType.STRING)` 且在 `scan_index.enums` 裡解析到，`extra: SomeUnresolvedType` 解析不到、走最終降級）。`OrderStatus` 渲染進共用檔案，`Order` 所在的 `app/models/order.py` 只匯入不重新定義：

```python
# app/models/_enums.py（共用檔案，見下方「Enum class 一律渲染進單一共用檔案」）
from __future__ import annotations

import enum


class OrderStatus(enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    CANCELLED = "CANCELLED"
```

```python
# app/models/order.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum as SqlEnum, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._enums import OrderStatus


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # 原始 Java 型別是基礎型別 int（is_primitive=True），沒有 @Column 標註，
    # 仍然機械判定 nullable=False，不加 | None（見「欄位級 annotation」表格）
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[OrderStatus | None] = mapped_column(SqlEnum(OrderStatus, native_enum=False, length=255), nullable=True)
    # TODO: 未知型別 SomeUnresolvedType，降級為 String，可能是 Enum（無法解析引用來源）或其他自訂型別，需要人工或⑤確認
    extra: Mapped[str | None] = mapped_column(String, nullable=True)
```

Import 段落只列這個檔案實際用到的型別（同一個模組內出現多次只 import 一次，候選集合就是本章對應表的固定清單，機械條列即可）；`Mapped`／`mapped_column` 固定從 `sqlalchemy.orm` import；`from app.core.database import Base` 固定加入。欄位型別若用到 `UUID`，額外加 `from uuid import UUID`（型別註記用）與 `from sqlalchemy.dialects.postgresql import UUID as PgUUID`（Column 型別用）；用到 `UUID` 主鍵預設值時額外加 `from uuid import uuid4`；欄位型別是 Enum 時額外加 `from sqlalchemy import Enum as SqlEnum` 與 `from app.models._enums import {EnumClassName}`（`EnumClassName` 是 `scan_index.enum_python_names[fqn]` 算出的最終名稱，見下方「Enum class 一律渲染進單一共用檔案」，不是這個檔案自己定義的 class，一律用匯入）；有渲染稽核時間戳時額外加 `from sqlalchemy import func`；`__table_args__` 有 `UniqueConstraint(...)` 時額外加 `from sqlalchemy import UniqueConstraint`；只要 `schema` 或 `unique_constraints` 任一個非空就會渲染 `__table_args__`。`app/models/_enums.py` 本身固定 `import enum`，不需要其他 import。

**這份清單只涵蓋七章自己會用到的型別，八章外鍵／中介表渲染另外各自加自己需要的 import，不是這份清單的一部分**：這個檔案只要渲染出任何一個 `@ManyToOne`／`@OneToOne` 外鍵欄位（八章「欄位命名與 nullable」），固定加 `from sqlalchemy import ForeignKey`；只要渲染出任何一個 `@ManyToMany` 中介表（八章「產生中介表定義」），固定加 `from sqlalchemy import Column, ForeignKey, Table`（中介表用的是 SQLAlchemy Core 的 `Column`，不是 `mapped_column`——這個檔案若同時也有一般 entity 用 `mapped_column`，`Column`／`mapped_column` 兩個名稱不衝突，各自來自不同 import，不需要額外處理）。

---

## 八、外鍵與集合關聯的取捨

### 決策：只產生外鍵純量欄位，不產生 `relationship()`

`@ManyToOne`／`@OneToOne`（僅擁有端，見下方「`@OneToOne` 的擁有端／非擁有端」）標註的欄位，在 DB 層對應一個外鍵純量欄位，機械、必然存在，可以放心產生。欄位型別必須跟隨被參照主鍵的實際型別（`scan_index.entities[fqn].primary_key_type`），不能一律假設是 `Integer`——`ForeignKey` 兩端型別不一致，SQLAlchemy 在 `mapper` 設定階段就會報錯。

### 欄位命名與 nullable：讀 `@JoinColumn`，不是 `@Column`

**這是本章最容易誤植的一步**：`@ManyToOne`／`@OneToOne` 欄位在 JPA 裡標的是 `@JoinColumn`，不是 `@Column`——七章「欄位級 annotation」那張表只適用於純量欄位（`@Column` 系列），不適用於這裡。若沿用七章的規則（`@Column(name=...)` 缺席時 fallback `camel_to_snake(field_name)`），對 `private User user;`（沒有標 `@Column`，只標了 `@JoinColumn`）會算出欄位名 `"user"`，跟真實 DB 欄位 `user_id` 對不上，Harness 驗證會直接炸「column does not exist」。

**規則**（`@JoinColumn` 的 `name`／`nullable` 元素值同受四章 Literal 規則約束）：

- **Python 屬性名稱（固定公式，不受 `@JoinColumn(name=...)` 是否出現影響）**：`f"{camel_to_snake(field_name)}_{primary_key_column}"`（如欄位 `user` ＋ 目標主鍵欄位 `id` → `user_id`）——這是 JPA／Hibernate 本身的預設命名慣例（屬性名稱 ＋ 底線 ＋ 被參照主鍵欄位名稱），不是這裡發明的規則，也是七章「Python 屬性名稱固定是 `camel_to_snake(field_name)`」原則在外鍵欄位上的延伸：Python 這一側的識別字永遠機械算出，不受 Java／DB 命名習慣影響。若目標主鍵欄位解析不到（見下方「外鍵解析失敗的降級處理」），沒有 `primary_key_column` 可接，退回單純 `camel_to_snake(field_name)`（不帶後綴）——這種情況本來就已經在降級路徑上，欄位名稱同樣不精確是預期中的一部分
- **DB 欄位名稱**：`@JoinColumn(name = "...")` 存在 → 用它的字面值；缺席 → 沿用上面算出的 Python 屬性名稱字串（JPA 沒明講欄位命名時，Hibernate 預設的 DB 欄位名稱慣例本來就跟這條公式一致）。**只有 `@JoinColumn(name=...)` 的字面值跟上面算出的 Python 屬性名稱不同時，才需要把這個 DB 欄位名稱字面字串額外傳給 `mapped_column()`**，寫法比照七章「`@Column(name = "...")` 覆蓋的是 DB 欄位名稱」的雙位置引數形式（`mapped_column("DB_NAME", Type, ForeignKey(...), ...)`，DB 名稱固定是第一個位置引數）——**不能讓 `@JoinColumn(name=...)` 的字面值直接當 Python 屬性名稱用**：企業專案常見的 DB 命名慣例（如全大寫加底線 `MGR_ID`）若直接拿來當 Python 識別字，語法合法、`ast.parse()` 抓不到，但會破壞整個生成專案本應一致的 `snake_case` 慣例——⑤（qwen 填空）在其他地方看到的都是 `camel_to_snake` 屬性名，遇到這種例外欄位很可能直覺寫成不存在的屬性名稱，觸發只有 Harness／⑦ Debug Agent 才能抓到的 `AttributeError`，這正是七章特意要避免、不該只在 `@Column` 這條路徑上把關的錯誤模式
- **nullable**：`@JoinColumn(nullable = false)` 存在 → `nullable=False`；缺席 → `nullable=True`（JPA 對 `@JoinColumn.nullable` 的規格預設值本來就是 `true`，不是「不知道所以猜」）

```python
# 欄位 `private User user;` 標 @ManyToOne + @JoinColumn(name = "user_id", nullable = false)
# @JoinColumn 給的 DB 名稱剛好跟公式算出的 Python 屬性名稱一致（user_id），不需要額外傳 DB 名稱字串
# 目標主鍵型別是 Integer、這條邊沒有落在六章環偵測到的循環裡（一般情況）
user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)

# 欄位 `private User owner;` 標 @OneToOne，沒有 @JoinColumn（用預設命名慣例：owner_id）
# 目標主鍵型別是 UUID、該 entity 標了 schema="hr"、且這條邊落在六章環偵測到的循環裡
# （UUID／PgUUID 分別是 uuid.UUID 型別註記與 sqlalchemy.dialects.postgresql.UUID Column 型別的別名匯入）
owner_id: Mapped[UUID | None] = mapped_column(
    PgUUID(as_uuid=True),
    ForeignKey("hr.users.id", use_alter=True, name="fk_orders_owner_id"),
    nullable=True,
)

# 欄位 `private User assignedManager;` 標 @ManyToOne + @JoinColumn(name = "MGR_ID")
# DB 名稱（MGR_ID，企業專案常見的全大寫慣例）跟公式算出的 Python 屬性名稱（assigned_manager_id）不同，
# 比照七章 @Column(name=...) 的雙位置引數寫法，DB 名稱字面字串固定當第一個位置引數，
# Python 屬性名稱仍然維持 snake_case，不被 DB 命名習慣污染
assigned_manager_id: Mapped[int] = mapped_column("MGR_ID", Integer, ForeignKey("users.id"), nullable=True)
```

`ForeignKey(...)` 字串組成：`"{schema}.{table_name}.{primary_key_column}"`（`schema` 存在時）或 `"{table_name}.{primary_key_column}"`，三個片段都來自六章 `scan_index.entities`。`use_alter=True`／`name=` 只在六章「外鍵依賴圖與循環偵測」判定這條邊落在循環裡才加，見該節。**`name=` 固定用 `f"fk_{來源 table_name}_{這個 FK 欄位的 DB 欄位名稱}"`**（不是 Python 屬性名稱——約束名稱是 DB 端物件，要跟實際落地的 DB 欄位對齊；如上例 `fk_orders_owner_id`：來源 table 是 `orders`，DB 欄位是 `owner_id`；若換成 `assigned_manager_id` 那個例子且這條邊也落在循環裡，會是 `fk_orders_MGR_ID`，不是 `fk_orders_assigned_manager_id`）——這保證同一張表底下的多個 FK 約束不會撞名（一張表不可能有兩個同名 DB 欄位，DB 欄位名稱本身已經是這張表內唯一的），跟七章 `UniqueConstraint` 的 `name=` 公式（見七章）是同一種「內容決定名稱」的機械規則。

**刻意不產生 `relationship()`**（雙向物件導航屬性，如 `user = relationship("User", back_populates="orders")`）：

- `relationship()` 需要決定 `back_populates`／`backref` 命名、`lazy` 載入策略、`cascade` 規則——這些是業務層級的設計選擇，不是能從 Java entity annotation 機械推導出的單一正確答案
- 沒有 `relationship()` 不影響骨架可用性：外鍵純量欄位已經足以讓 ⑤ 在填空時写出正確的 join query，`relationship()` 只是語法糖
- 不能用程式判斷的，就不要用程式硬猜——`relationship()` 的細節設定交給⑤或人工事後補上，比骨架階段猜一個可能錯的設定更安全

`create_all()` 依哪個順序建表，是 SQLAlchemy Core 層根據 `ForeignKeyConstraint` 拓撲排序決定的，`relationship()` 是 ORM 層的物件導航設定，兩者是不同層級——有沒有 `relationship()` 不影響建表順序，真正處理循環外鍵風險的是六章的環偵測＋`use_alter`。

### `@OneToOne` 的擁有端／非擁有端

`@OneToOne` 跟 `@ManyToOne` 不同——`@ManyToOne` 語意上必然是「多」的那一側持有外鍵，不可能是非擁有端，不需要判斷；但 `@OneToOne` 是雙向對等關係，**可以**有一側用 `mappedBy` 宣告自己是非擁有端：

```java
// 擁有端（User）：真正持有外鍵欄位
@OneToOne
@JoinColumn(name = "profile_id")
private Profile profile;

// 非擁有端（Profile）：mappedBy 指向擁有端的欄位名稱，DB 裡沒有對應欄位
@OneToOne(mappedBy = "profile")
private User user;
```

**規則**：`@OneToOne` 欄位若有 `mappedBy = "..."` 元素（字串 `Literal`，正常通過 Literal 規則）→ 這是非擁有端，直接跳過、不產生任何 Column，不記警告——跟下方 `@OneToMany` 是同一種「外鍵在對方身上，這一側本來就不該有欄位」的情況。`mappedBy` 缺席 → 擁有端，照上方「欄位命名與 nullable」的規則渲染。這是與下方 `@ManyToMany` 的 `mappedBy` 判斷同一個原則的第三次套用（`@OneToMany` 恆定跳過、`@ManyToMany` 靠 `mappedBy`／`@JoinTable` 判斷、`@OneToOne` 靠 `mappedBy` 判斷），不是三套獨立規則。

### `@OneToMany`：不渲染任何欄位

外鍵在對方 table 上，這個 entity 自己 table 沒有對應欄位。標註 `@OneToMany` 的欄位直接跳過、不產生任何 Column，不記警告——這是預期中的正常情況。

### `@ManyToMany`：產生中介表定義（Core `Table`），不產生 ORM class

`@ManyToMany` 不像 `@OneToMany`，它必然對應一張真實存在的中介表（join table）——這張表不屬於任何一個 entity，但它是資料庫裡確實存在的物件，若完全不渲染，這張表不會出現在 SQLAlchemy 的 metadata 裡：以 `create_all()` 重建測試 DB 會漏建這張表，⑤填空時若需要對這張表直接操作（新增／刪除一筆多對多關聯），也沒有任何 Python 端的參照可用。

四章掃描到 `@ManyToMany` 且同一欄位有 `@JoinTable` 時，渲染成 SQLAlchemy Core 的 `Table`（不是 ORM class——這張表沒有對應的 Java entity，沒有業務行為，只是純粹的關聯資料）：

```python
from sqlalchemy import Column, ForeignKey, Table

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
    Column("role_id", Integer, ForeignKey("roles.id"), primary_key=True),
)
```

`user_roles`／`user_id`／`role_id` 分別對應 `@JoinTable` 的 `name`／`joinColumns[0].name`／`inverseJoinColumns[0].name`（四章）；兩個 FK 欄位的型別、`ForeignKey(...)` 字串組成規則、`use_alter` 判斷，都跟一般外鍵欄位（上方）共用同一套機制，不是另一套規則。兩欄位一起組成複合主鍵——純關聯表沒有額外payload 欄位時，這是標準寫法，同一組 `(user_id, role_id)` 不該重複。

**中介表沿用擁有端的 schema**：中介表沒有對應的 Java entity，不會有自己的 `@Table(schema = "...")`（`@JoinTable` 的 `schema` 元素這批資料裡沒出現，不處理，見十章已知限制），若擁有端標了 `@Table(schema = "hr")`，`Table(...)` 建構子要多帶一個 `schema="hr"` 引數，直接沿用擁有端的 schema——不加這個引數，中介表會落在 SQLAlchemy 預設 schema（通常是 `public`），跟 `ForeignKey("hr.users.id")` 這類已經帶 schema 前綴的 FK 目標所在的 schema 不一致：

```python
user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("hr.users.id"), primary_key=True),
    Column("role_id", Integer, ForeignKey("hr.roles.id"), primary_key=True),
    schema="hr",
)
```

**`user_roles` 這個變數名稱本身能被⑤填空時正確 import**：`user_roles = Table(...)` 是 `ast.Assign`，不是 `ast.ClassDef`——07a 兩處自訂型別索引機制（四章來源三、五章「填空模式：本體 import 解析」）已同步擴充成同時認得這種「模組層級變數賦值成 `Table(...)`」的宣告形狀，不是只認 class，見 07a 對應章節。

**中介表變數名稱正規化，不影響實際 DB table 名稱**：`@JoinTable(name = "...")` 的字面值不保證是合法 Python 識別字——企業級 DB 常見的連字號命名（如 `user-roles`）、開頭數字等，直接當賦值左手邊的變數名稱會產生 `SyntaxError`。渲染時把這個字面值套一次「非英數字元、非底線的字元全部換成底線，開頭是數字再補一個底線前綴」的機械正規化，只換賦值左手邊的識別字，`Table(...)` 第一個位置引數（實際 DB table 名稱）維持原始字面值不變：

```python
user_roles = Table(
    "user-roles",
    Base.metadata,
    ...
)
```

這不是用 `camel_to_snake()`（五章）處理——`camel_to_snake()` 是依 camelCase 大小寫邊界切詞界的轉換，對 `"user-roles"`這種沒有大小寫變化的輸入是 no-op，連字號原樣保留，解決不了這裡的問題，需要另一條專門去除非法識別字元的規則。正規化後仍可能剛好撞上 Python 保留字（如 table 剛好取名 `"class"`）——這種殘留情況交給九章「逐 entity 隔離失敗」的 `ast.parse()` 驗證接住，不在這裡窮舉。

渲染進宣告 `@JoinTable` 的那一側（擁有端）所屬的 `app/models/{module}.py`。**只有擁有端渲染，且擁有端只有一個，即使兩側都宣告了 `@JoinTable`**：`mappedBy` 的非擁有端（四章）不重複渲染同一張表；兩側都標 `@JoinTable`（不規範但真實可能發生的寫法）時，用六章 `scan_index.join_table_owners` 判斷，只有登記為擁有者的那一側渲染，另一側視同非擁有端不重複渲染，避免同一張中介表在兩個檔案各自宣告一次、兩個模組一起被 import 時 SQLAlchemy 對同一個 `MetaData` 註冊兩次同名 `Table` 直接拋出 `InvalidRequestError`（見六章該節）；`@JoinTable`／`mappedBy` 都缺席（完全依賴 Hibernate 隱性命名慣例）時，不猜表名與欄位名，記一筆 `logger.warning`，這個 `@ManyToMany` 欄位沒有任何渲染結果——比照七章「不能用程式判斷的，就不要用程式硬猜」的一貫立場。**這個情況不記進 `skipped_entities`**：宿主 entity 的其他欄位、class 定義本身都正常渲染進 `app/models/{module}.py`，缺的只是這一個欄位，跟九章 `skipped_entities` 的粒度承諾（見九章「`skipped_entities`：entity 級失敗清單」，整個 class／中介表沒有被渲染）不是同一件事，見九章該節的說明。

### 外鍵解析失敗的降級處理

`@ManyToOne`／`@OneToOne` 欄位滿足下列任一情況，就不產生 `ForeignKey(...)` 約束：

- `resolve_reference()` 解析不到目標 class 的 FQN
- 解析到 FQN，但目標 entity 本身「主鍵不明確」

這個欄位仍然渲染成 `mapped_column(...)`，型別改用 `scan_index.fallback_pk_type`（六章「降級外鍵的型別 fallback」，不是寫死猜 `Integer`），Python 屬性名稱同上一節「找不到 `primary_key_column` 時退回 `camel_to_snake(field_name)`」；DB 欄位名稱規則不受影響，`@JoinColumn(name=...)` 存在時仍照上一節規則用它的字面值（超出上面兩種 fallback 型別／屬性名稱調整的範圍）。並在這個 Column 上方加一行 `# TODO` 註解，記下這原本概念上是指向哪個 table 的外鍵（例如 `# TODO: 概念上是外鍵，指向 orders 表，因目標主鍵不明確無法產生 ForeignKey 約束`）——⑤填寫涉及這個欄位的 join 查詢時，這行註解是唯一能看到的線索。

**這個註解會被保留**：`db_models` 是④自己組裝的完整檔案內容字串，07b `translator_cli/scaffold.py` 的 `build_files()` 對 `db_models` 只做 `ast.parse()` 驗證、直接把原始字串寫入磁碟，不像 `interfaces` 那樣經過 `ast.parse → ast.unparse` 重新格式化（那個流程會丟掉註解）。

**對下游（⑤／⑥）的連鎖影響，由 09a 承接**：③依 `openapi_spec` 產生的 FastAPI response schema 若期待巢狀結構（如 `Order` response 內含完整 `User` 物件），沒有 `relationship()` 代表 SQLAlchemy 物件不會自動帶出這個巢狀屬性——⑤在填空時若直接把 ORM 物件原樣塞給 Pydantic 序列化，會在 Agent ⑥ 執行測試時出現屬性缺漏或驗證失敗。09a（⑤ 功能改寫 Agent 詳細設計，待建立）必須明確承接：qwen 填空時若目標函式的 response schema 需要巢狀物件，應在 service／router 層手動組裝 DTO 或用 `selectinload()`／額外查詢動態取得關聯資料。

---

## 九、模型檔案組裝與隔離失敗原則

### 逐 entity 隔離失敗，不是全域 all-or-nothing

單一 entity 的欄位型別、table 名稱解析若出問題，不該拖累同一個 module 甚至其他 module 的所有 entity。`build_db_models()` 因此逐 entity 驗證：

1. 逐欄位渲染成 `mapped_column(...)` 陳述式，單一欄位渲染失敗（annotation 元素值格式異常等真正的渲染錯誤——未知純量型別已改為降級渲染成 `String`，不算失敗）→ 這個欄位跳過，記警告，繼續渲染同一個 entity 其餘欄位
2. 整個 entity 的 class 定義（或八章 `@ManyToMany` 的 `Table(...)` 定義）組完後，做一次 `ast.parse()` 驗證這個片段語法合法——失敗（理論上不該發生）→ 這個 entity／中介表整個跳過，不寫入這個 module 的檔案，記進 `skipped_entities`（見下方）
3. 同一個 module 底下若有 entity／中介表順利渲染，其餘失敗的不影響它們——檔案仍然會產出，只是內容少幾個定義
4. 一個 module 完全沒有任何 entity 或中介表（純外部 API 串接、沒有 DB 表的模組）→ 不產生這個檔案的 key，不是錯誤
5. `app/models/_enums.py`（七章「Enum 欄位」）不屬於任何 module，是額外獨立處理的一個 key：`scan_index.enums` 非空才產生這個檔案（同一種「內容真的存在才產生 key」的原則），逐 Enum 各自 `ast.parse()` 驗證，單一 Enum 驗證失敗（理論上不該發生，成員清單是機械收集的名稱字串）比照步驟 2 記進 `skipped_entities`，不拖累其他 Enum

**整份 `db_models` 最終格式**：`dict[file_path, 完整檔案內容字串]`，同一個 module 的多個 entity class 定義依掃描順序接續寫進同一個檔案文字。這個字典直接是傳給 `translator_cli.generate_scaffold(..., db_models=...)` 的引數，不需要轉換。

**組裝順序固定是「先算出這個檔案全部 entity 合起來需要的 import 集合，統一渲染成單一 import 區塊放最前面，再依序接上每個 entity 的 class 定義」，不是逐 entity 各自的 `import + class` 片段直接串接**：後者（每個 entity 各自帶著自己的 import 緊接著自己的 class，一段一段串起來）在 Python 語法上不會出錯（import 陳述式在模組任何位置都合法，只要在使用前執行過），但會產生 import 陳述式散落在整個檔案各處、不集中在檔頭的結構，不符合這個專案其餘生成檔案（`interfaces`、七章單一 entity 的既有渲染範例）一致採用的「檔頭一個 import 區塊」慣例，也讓人工／⑦ Debug Agent 事後想確認「這個檔案到底 import 了什麼」時要看過整份檔案而不是只看檔頭幾行。實際組裝分兩步：先對這個 module 底下每個 entity 各自算出的 import 需求（七章「Import 段落」）取聯集、去重、排序，渲染成單一區塊；區塊之後才依序接上各 entity 的 class 定義（class 定義本身不再重複帶 import）。

**與 07a 既有驗證的分工**：本章的 `ast.parse()` 只驗證單一 entity 片段；07a 四章「全域最終檢查」（整份 `db_models[file_path]` 各自 `ast.parse()` 一次）仍會在 `generate_scaffold()` 內部重跑一次——這裡的 entity 級驗證是提早攔截、縮小失敗範圍，07a 那一層是防禦性的最後一道關卡，兩者不衝突。

### `skipped_entities`：entity 級失敗清單，不是 `db_models` 的一部分，要另外回傳

**這是一個容易漏接的邊界**：`build_db_models()` 的回傳值若只是 `dict[str, str]`（十一章 `build_db_models()` 簽名），上面步驟 2「跳過的 entity」這件事就沒有管道傳出這個函式——`scaffold_node.py` 只拿得到寫成功的那些檔案，完全不知道哪些 entity 曾經被跳過。這跟 07a `generate_scaffold()` 回傳值裡的 `skipped_db_models`（見 07a 四章）是**不同粒度的兩件事**：07a 那個是整個 `db_models[file_path]` 字串本身 `ast.parse()` 失敗（檔案級，`generate_scaffold()` 自己的最終檢查失敗才會觸發，見上方「與 07a 既有驗證的分工」）；這裡的 `skipped_entities` 是同一個檔案內、單一 entity／中介表級的失敗（檔案本身仍會產出，只是缺這一塊）——`scaffold_agent` 這一層必須自己開一個獨立的回傳管道裝這份清單，不能寄望 07a 那個檔案級欄位順便帶到。

**`build_db_models()` 的實際回傳型別因此不是裸 `dict[str, str]`**（十一章原始簽名需要修正，見該章）：

```python
@dataclass
class BuildDbModelsResult:
    db_models: dict[str, str]
    skipped_entities: list[dict]  # {file_path, class_name, error}
```

`skipped_entities` 收三種來源：本節步驟 2 的驗證失敗、十章「已知限制」提到 `@Embeddable` class 本身這種刻意排除情況、以及六章「簡短類別名稱跨 package 撞名的偵測」裡「同一個 module 內撞名」的 tie-break 跳過（後出現的同名 entity）——這三種都符合「整個 class／中介表沒有被渲染」這個粒度承諾，統一記進同一份清單，不是各自只留一筆 `logger.warning`。`{file_path, class_name, error}` 的形狀比照 07a `skipped_interfaces` 既有慣例（`{file_path, class_name, function_name, error}` 少一個 `function_name`，因為這裡跳過的是整個 class／中介表，不是單一函式）。**`@ManyToMany` 完全沒有 `@JoinTable`／`mappedBy` 不算在這三種來源裡**（見八章「產生中介表定義」）：那是單一欄位沒有渲染結果，宿主 entity 的 class 定義、其餘欄位都正常產出，混進這份「整個 class／中介表消失」粒度的清單，會讓消費者（09a／⑦，見十二章「用 `class_name` 是否存在分辨兩種粒度」）誤判成整個 class 找不到、進而導向錯誤的根因分析——這種情況維持只留一筆 `logger.warning`（八章），不進 `skipped_entities`。

`scaffold_node.py`（十二章）把這份清單併入 `RefactorState.skipped_db_models`——07a 那個檔案級清單（`{file_path, error}`，沒有 `class_name`）與這裡的 entity 級清單（`{file_path, class_name, error}`）合併成同一個 list 一起寫進 State，消費者（09a／⑦ Debug Agent）用 `class_name` 是否存在分辨兩種粒度，不需要兩個獨立的 State 欄位——兩者對下游而言語意一致（「這個東西沒被④渲染出來，原因記在這裡」），差別只在粒度，見十二章。

---

## 十、已知限制

- **`@Embeddable` 不解開組合欄位**：`@MappedSuperclass` 的繼承欄位已透過六章「欄位繼承合併」正確處理，`@Embeddable` 是另一種機制（透過 `@Embedded` 欄位型別組合，不是類別繼承），要正確處理需要解析「哪個欄位用 `@Embedded` 引用了它、再把它的欄位攤平進來」，這次不處理。`@Embeddable` class 本身記進九章 `skipped_entities`（原因是「非獨立 table，非本次範圍」），是刻意排除，不是渲染失敗；引用它的欄位（標 `@Embedded`）走七章「無法解析型別的最終降級」
- **複合主鍵（`@IdClass`／`@EmbeddedId`）不支援**：`primary_key_column`／`primary_key_type` 皆為 `None`——標 `@Id` 的欄位各自渲染成 `primary_key=True`（SQLAlchemy 原生支援複合主鍵），但「這個 entity 當外鍵目標」退化成「外鍵解析失敗的降級處理」
- **`@AttributeOverride`（`@MappedSuperclass` 欄位繼承合併，六章）不處理**：JPA 允許具體 `@Entity` 用 `@AttributeOverride(name = "...", column = @Column(name = "..."))` 覆寫繼承欄位的 column 對應（不重新宣告欄位本身），這是比六章「較近層級覆蓋」更細緻的機制——那條規則處理的是子類別**重新宣告同名欄位**（欄位隱藏），這裡是子類別**完全不重新宣告欄位、只覆寫它的 column 對應**，掃描階段目前不讀這個 annotation，繼承來的欄位一律沿用祖先原本的 `@Column` 元素值（或缺席時的 fallback），若真實專案用到 `@AttributeOverride` 改欄位名稱，渲染出的 column 名稱會沿用祖先的舊名稱，不是子類別想要的新名稱——這種錯誤語法合法、`ast.parse()` 抓不到，要等 Harness 才會發現
- **`@JoinColumns`（複數，複合外鍵）不支援**：`@ManyToOne`／`@OneToOne` 極少數情況會用 `@JoinColumns` 宣告多欄位複合外鍵，這裡只處理單一 `@JoinColumn`，跟複合主鍵同一種「不追組合鍵」的既有立場，會落入「外鍵解析失敗的降級處理」
- **Bean Validation annotation（`@NotNull`／`@NotBlank`／`@Size` 等）不影響 nullable／length 判斷**：只讀 `@Column` 本身的元素值，不交叉比對 Bean Validation annotation——那是不同的資料來源與目的，不假設兩者恆等
- **Enum 完整解析只涵蓋 `@Enumerated(EnumType.STRING)`／缺席這兩種情境**，明確標 `EnumType.ORDINAL` 仍降級成 `String`——`ORDINAL` 在業界本來就少見
- **Enum／Entity 簡短類別名稱跨 package 撞名時，07a 的自訂型別索引可能漏 import 或取錯檔案，無法在④這一層完全解決**：根因是③（05a）在④執行前就已經對裸名稱（不含 package）定案寫進 `python_structure.interfaces`，管線順序 ①→(②∥③)→([P]∥④)→⑤，③早於④——任何④事後才做的消歧或改名，下游（07a）都看不到。
  - **Enum**：`enum_python_names` 的撞名消歧（七章）只對④自己在 `db_models` 內部的渲染正確；07a 索引裡只找得到消歧後的名稱（如 `Status_hr`），比對不到 `interfaces` 留下的裸字串 `"Status"`，觸發「兩層都比對不到時保守不加、不猜」，這個 import 會整個漏掉。刻意不採用「相容別名」（`Status = Status_hr`）：那等於在③本來沒有能力分辨的地方替它猜一個可能錯的綁定，比明確漏 import 更危險（程式碼看起來成功 import、實際上可能綁錯 Enum）。
  - **Entity**（僅限落在不同 module 的撞名，同 module 已由六章 tie-break 跳過渲染處理）：07a 四章「自訂型別索引」來源三是扁平的 `class_name → file_path` 索引，不是 FQN-keyed，兩個不同 package 的同名 `@Entity`（如 `com.hr.Address`／`com.billing.Address`）會被後掃到的檔案靜默覆蓋，裸型別引用可能接上錯誤的 `import`——語法合法、`ast.parse()` 抓不到，只有 Harness 才可能發現，也可能因欄位結構恰好相容而完全不報錯。同一根因也涵蓋 Entity 與 Enum 之間的跨型別撞名（六章已補上這組交集檢查、記 `logger.warning`）。
  - **不修改 05a 輸出契約（改用 FQN）＋改寫 07a 索引邏輯**：技術上可行，但代價是讓③額外背上一整套 `resolve_reference()` 型 import 解析邏輯，並同步改動已定案、通過真實專案端對端驗證的 07a 索引——在一個目前未證實會發生的風險上重寫三份已穩定對齊的文件，不符合「先驗證再擴充」的一貫立場。成本低、留在④邊界內能做的是主動示警：六章第一階段建完 `scan_index` 後統計簡短 `class_name` 是否對到多個 FQN，`> 1` 律記 `logger.warning`（已實作）——不消除風險，但讓風險在④跑完當下就可見，不必被動等 Harness 撞到、甚至永遠不被發現。
- **自訂類別剛好取名 `UUID`（跟 `java.util.UUID` 撞名）會被機械字面比對誤判成標準庫型別，只主動示警不糾正**：`map_java_type()`（五章）對 `"UUID"` 做的是字面字串精確匹配，不查 `import_map`——七章渲染欄位時，若偵測到欄位型別字面字串是 `UUID`、但該欄位宣告檔案的 `import_map` 顯示 `UUID` 這個簡短名稱其實顯式匯入自其他 package（見七章「UUID」型別對應說明），會記一筆 `logger.warning`，但渲染結果仍然照標準庫 `UUID` 處理，不會自動改判成自訂型別走 Enum／FK 解析路徑——要正確處理需要讓 `map_java_type()` 這類刻意設計成不吃 import 語境的輕量比對函式也吃 `import_map`，這已經超出五章訂下的既有設計判準（同一份函式 05a／07a 也在共用），這裡選擇示警取代靜默誤判，不擴大 `map_java_type()` 的職責範圍
- **陣列型別只支援 `byte[]`／`Byte[]`**：多維陣列、非 `byte` 元素型別的陣列落入「含 `[`／`]` 的容器型別」分支被跳過
- **`@ManyToMany` 只支援單一欄位的 `joinColumns`／`inverseJoinColumns`**：`@JoinTable` 的 `joinColumns`／`inverseJoinColumns` 若各自對到多個 `@JoinColumn`（複合外鍵），只取第一個，跟複合主鍵（上方）同一種「不追組合鍵」的既有立場
- **`@ManyToMany` 完全沒有 `@JoinTable` 也沒有 `mappedBy` 時，中介表不會出現在任何檔案裡**：這種寫法完全依賴 Hibernate 隱性命名慣例決定中介表名稱與欄位名稱，機械掃描拿不到足夠資訊，不猜表名，只記一筆 `logger.warning`（不進九章 `skipped_entities`——宿主 entity 本身正常渲染，這只是單一欄位級的缺角，見九章該節的粒度說明）
- **`@Column(length=...)`／`@Column(precision=..., scale=...)` 因常數參照被降級時，DB 層失去對應的長度／精度驗證防線**：Literal 規則保證不會產出執行期 `NameError` 的檔案，代價是這種情況下欄位不帶長度／精度限制，PostgreSQL 執行上合法、不會報錯，但少了資料庫層級的把關，責任轉移到 Harness——若 golden output 涵蓋長度邊界附近的測試資料，Harness 局部驗證能撈到這類行為差異；若沒有，這個限制不會在 pipeline 任何環節被自動抓到
- **annotation 元素值一律只認 `Literal`**：用常數代入 annotation 元素時視同該元素缺席，不嘗試解析常數實際的值——要解析常數值需要額外追蹤這個常數定義在哪個類別、值是多少，屬於完整的符號解析，超出這裡「輕量掃描」的範圍
- **`@Table(schema = "...")` 目前沒有真實案例驗證**：`lang-exam-api-refactor` 這個真實專案目前只用單一 DB，沒有觀察到多 schema 情況
- **`@Table(indexes = ...)` 不處理**：純索引（非唯一）不影響資料完整性，只影響查詢效能，跟七章已處理的 `uniqueConstraints`（資料完整性）性質不同，這次不追
- **多層 `@MappedSuperclass` 疊加（`Order extends AuditableEntity extends BaseEntity`）只驗證過理論設計，沒有真實案例**：六章的合併演算法本身支援任意層數遞迴，但 `lang-exam-api-refactor` 目前只出現單層疊加（entity 直接 `extends BaseEntity`，已由 `docs/09b_bug_trace.md` #69／#98 的真實 pipeline 執行驗證），超過一層的疊加沒有真實案例
- **`@Entity extends @Entity`（JPA `@Inheritance` 繼承策略：`SINGLE_TABLE`／`JOINED`／`TABLE_PER_CLASS`）不處理**：六章合併演算法只沿 `extends` 鏈合併 `@MappedSuperclass`，父類別若解析到的是另一個 `@Entity`，這一層停止合併並記 `logger.warning`（見六章），但父 entity 的欄位不會被合併進子 entity——子 entity 產生的 `app/models/{module}.py` 只含它自己直接宣告的欄位，`@Inheritance` 策略要求的欄位繼承（`SINGLE_TABLE` 需要父子欄位合併進同一張表、`JOINED` 需要子表額外關聯父表主鍵、`TABLE_PER_CLASS` 需要各自完整複製欄位）完全沒有實作——若子 entity 自己完全沒有宣告 `@Id` 欄位（主鍵定義在父 entity 上），會落入跟六章「`@MappedSuperclass` 欄位繼承合併」開頭描述的同一種「主鍵不明確」結構性錯誤
- **稽核時間戳、`@Where`／`@SQLRestriction` 邏輯刪除的關鍵字判斷是機械字面比對**：`@CreatedDate`／`@LastModifiedDate`／`@CreationTimestamp`／`@UpdateTimestamp`／`@Where`／`@SQLRestriction` 這幾個 annotation 名稱本身是固定字面比對，不處理專案自訂的、語意相同但名稱不同的稽核／軟刪除機制（如公司內部框架自己包的 `@AutoTimestamp`）
- **`use_alter` 循環外鍵偵測、`@ManyToMany`／`@JoinTable`、跨 package 同名類別（FQN 索引撞名）目前還沒有真實案例驗證**：`lang-exam-api-refactor` 目前沒有出現這幾種情況，是否觸發、觸發後行為是否正確，需要接上更多真實專案才能確認。`resolve_reference()`（同 package 隱式參照路徑）、Enum 完整解析（含建構子引數還原）、`@MappedSuperclass` 單層欄位合併已由 `docs/09b_bug_trace.md` #44／#69／#98 的真實 pipeline 執行驗證，不在此列

---

## 十一、模組結構規劃

比照 `parse_agent/`／`design_agent/`／`plan_agent/`／`translator_cli/` 的組織方式，規劃為新套件 `scaffold_agent/`：

```
refactor-project/
└── scaffold_agent/
    ├── entity_scan.py         # 四章：javalang 掃描，找出 JPA entity/enum class + extends +
    │                          # annotation 元素值（含 @JoinTable／unique_constraints／soft_delete_
    │                          # clause）+ 欄位清單（含 is_primitive）+ package/import
    ├── reference_resolver.py  # 六章：兩階段全域 ScanIndex 建構、resolve_reference()、
    │                          # @MappedSuperclass 欄位繼承合併、fallback_pk_type 多數決、外鍵
    │                          # 依賴圖與循環偵測、Enum 簡短名稱碰撞消歧（enum_python_names）
    ├── column_mapping.py      # 七、八章：Java 欄位 → SQLAlchemy Column 對應、annotation 元素值
    │                          # 解析、外鍵／@ManyToMany 中介表渲染
    ├── model_builder.py       # 九章：逐 entity 隔離驗證、組裝 app/models/{module}.py 與共用
    │                          # app/models/_enums.py 的完整檔案文字
    ├── types.py               # 對外輸入輸出型別：ModuleInfo／MethodInfo（結構對齊 graph/state.py
    │                          # 同名 TypedDict，不 import）、BuildDbModelsResult
    ├── exceptions.py          # 內部例外型別
    └── __init__.py            # 對外唯一入口：build_db_models(java_project_path, module_list) -> BuildDbModelsResult
```

`build_db_models()` 回傳 `BuildDbModelsResult`（`db_models: dict[str, str]` ＋ `skipped_entities: list[dict]`，見九章「`skipped_entities`：entity 級失敗清單」），不是裸 `dict[str, str]`——`db_models` 本身仍然是直接傳給 `translator_cli.generate_scaffold()` 的那個字典，`skipped_entities` 是額外多出來的第二個欄位，供 `scaffold_node.py` 併入 `RefactorState`（十二章）。

**輸入端 `module_list` 的型別比照 `translator_cli/types.py` 的既有先例，不直接 `from graph.state import ModuleInfo`**：十一章開頭已明訂「不依賴 `design_agent`／`graph.state`」，這句話不能只看回傳值——`build_db_models(java_project_path, module_list)` 的 `module_list` 參數同樣需要一個型別標註，若這裡直接 `import graph.state.ModuleInfo`，`scaffold_agent` 就會在 import 階段就依賴 `graph` 套件，違背這條獨立性原則（也違背 07a 十二章、07b 已經對 `translator_cli` 驗證過的「在 `graph` 套件完全不可 import 的情況下仍能正常 import」這個具體檢驗標準）。`scaffold_agent/types.py` 因此自己重新定義結構對齊的 `ModuleInfo`／`MethodInfo`（跟 `graph/state.py` 同名 TypedDict 逐欄位一致，但不 import）——TypedDict 本質上只是結構化的 dict，`scaffold_node.py`（十二章）傳入的實際物件是 `graph.state.ModuleInfo`，兩者結構相容，不需要任何轉換，這跟 `translator_cli/types.py` 對 `PythonStructure`／`InterfaceSpec`／`ParamSpec` 的既有作法（07a 十二章）是同一個模式，不是這裡才發明的新規則。

| 職責 | 說明 |
|---|---|
| Entity／Enum 掃描 | 對應四章，`entity_scan.py` |
| 全域索引、跨檔案型別參照解析 | 對應六章，`reference_resolver.py` |
| 欄位型別／annotation 對應、Enum 渲染、外鍵渲染 | 對應七、八章，`column_mapping.py` |
| 逐 entity 隔離驗證、檔案組裝 | 對應九章，`model_builder.py` |
| 輸入輸出型別定義 | `types.py`，見上方說明 |
| 對外唯一入口 | 供 `graph/nodes/scaffold_node.py` 呼叫，node 本身不直接碰觸上述任何細節 |

`scaffold_agent` 依賴 `common/java_annotations.py`（`JPA_ENTITY_ANNOTATIONS`）與 `common/java_type_mapping.py`（`map_java_type()`／`camel_to_snake()`），不依賴 `design_agent`／`graph.state`——輸入端 `module_list`（`scaffold_agent/types.py` 自己定義的 `ModuleInfo`）與回傳值 `BuildDbModelsResult` 都是 `scaffold_agent` 自己定義的型別，跟 `graph/state.py` 沒有 import-time 型別耦合。

---

## 十二、與 LangGraph／既有程式碼的介面異動

### `graph/nodes/scaffold_node.py`：接上真正的 `db_models`

```python
import asyncio

from scaffold_agent import build_db_models

scaffold_result = await asyncio.to_thread(
    build_db_models,
    java_project_path=state["java_project_path"],
    module_list=state["module_list"],
)
result = await translator_cli.generate_scaffold(
    python_project_path=state["python_project_path"],
    python_structure=state["python_structure"],
    db_models=scaffold_result.db_models,
)
```

**`build_db_models()` 必須包一層 `asyncio.to_thread()`，不能直接同步呼叫**：十三章已定案 `build_db_models()` 是純 CPU 運算（不呼叫 LLM／外部服務），但正因為是同步、CPU-bound，若在 `async def run(state)` 裡直接呼叫，會整段佔住 event loop——00 一章的圖結構是讓 `scaffold`（④）跟 `plan`（[P]，呼叫 Claude API，走 async I/O）在同一個 superstep 平行執行，若④這段同步運算耗時不小（真實專案可能有幾十上百個 `.java` 檔要跑 `javalang.parse()`＋FQN 索引＋SCC 環偵測），會讓同時間 [P] 的 async API 呼叫在這段期間完全無法推進——結構上是平行，實際上退化成排隊。`asyncio.to_thread()` 把這段同步運算丟進執行緒池執行，讓 event loop 在這段期間仍能推進 `plan` 的 async 呼叫，兩者才是真正意義上的併發，不只是圖結構上的並列。

**`translator_cli.generate_scaffold()` 這一行不需要額外包 `asyncio.to_thread()`，不是這裡漏掉了同一個問題**：`generate_scaffold()` 雖然宣告成 `async def`、內部不呼叫任何模型（07a 四章「決策：不呼叫本地模型」），乍看跟 `build_db_models()` 是同一種「裝著 async 外殼、內部其實同步阻塞」的情況——但兩者實際的因應方式不同，不是 08a 漏處理了對稱的另一半。`translator_cli/client.py`（07b，已實作）對 `generate_scaffold()` 內部**每一個**會碰觸子行程或磁碟的呼叫（`git_ops.ensure_git_repo`／`check_clean_working_tree`／`scaffold.build_files`／`scaffold.write_files`／`formatting.format_paths`／`git_ops.commit_scaffold`）各自獨立包了一層 `asyncio.to_thread()`（見 `client.py` 模組 docstring「所有會碰觸子行程或磁碟的呼叫都包在 `asyncio.to_thread()` 裡」），不是整個函式外面包一層——這是**細顆粒度**的做法：呼叫端（這裡）因此可以直接 `await translator_cli.generate_scaffold(...)`，函式內部已經自己保證不會整段佔住 event loop，外面再包一層 `asyncio.to_thread()` 反而是多餘、也不成立（`asyncio.to_thread()` 吃的是同步 callable，不是 coroutine，硬套一個已經是 `async def` 的函式在型別上就不對）。`build_db_models()` 選擇**粗顆粒度**（整個函式維持純同步、由呼叫端整包丟進 thread）而不是比照這裡改成細顆粒度，是因為 `build_db_models()` 內部的 `javalang.parse()` 是**記憶體內的 CPU 運算**，不是像 `git_ops`／磁碟 I/O 那樣「一個個獨立、天然適合切成多個 to_thread() 呼叫」的離散操作——硬要把它拆成細顆粒度反而要在每個小步驟間來回切換執行緒，不會有實質好處，見十一章「為什麼要包一層容器」同一種「選最適合這個函式性質的做法，不是機械套用同一個模式」的態度。兩者都不會佔住 event loop，只是基於函式內部工作的性質選了不同的實作策略。

### `RefactorState` 新增欄位

07a 十二章、00 十章都把「`skipped_interfaces`／`skipped_db_models` 是否要進 `RefactorState`」留給本文件決定。**決策：新增**：

```python
    # Agent ④：generate_scaffold() 回傳的 skipped_interfaces／skipped_db_models
    # （見 07a 四章），單次寫入的快照，不逐次累加，不需要 reducer——
    # scaffold 不在 retry_count 迴圈內，只會執行一次。
    skipped_interfaces: list[dict]
    skipped_db_models: list[dict]
```

跟 `scaffold_done` 同一類「平行分支旗標」欄位：`scaffold` 這個 node 在整條 pipeline 只執行一次（`implement → run_tests → debug` 的 retry 迴圈不會繞回 `scaffold`），不存在多次寫入需要累加的情境，直接整包覆蓋即可。

`scaffold_node.py` 對應改寫：

```python
    return {
        "scaffold_done": result["success"],
        "skipped_interfaces": result["skipped_interfaces"],
        "skipped_db_models": [
            {**item, "class_name": None} for item in result["skipped_db_models"]
        ] + scaffold_result.skipped_entities,
    }
```

**`skipped_db_models` 是兩個不同粒度來源合併出來的同一個 list，不是各自獨立的兩個 State 欄位**：`result["skipped_db_models"]`（07a `generate_scaffold()` 的既有欄位，`{file_path, error}`，檔案級——整份 `db_models[file_path]` 字串在 `generate_scaffold()` 內部最終檢查時 `ast.parse()` 失敗才會出現，見 07a 四章）與 `scaffold_result.skipped_entities`（`scaffold_agent` 自己的欄位，`{file_path, class_name, error}`，entity 級，見九章）合併。**併入時把檔案級項目補上 `"class_name": None`，不是直接串接兩種不同形狀的 dict**：`RefactorState.skipped_db_models` 因此固定是同一種三欄位 schema `{file_path, class_name, error}`（`class_name` 為 `None` 代表檔案級、非 `None` 代表 entity 級），下游（09a／10a）用同一套邏輯讀取所有項目，不需要用 `.get("class_name", None) is None` 這種防禦性寫法先判斷這個 dict 到底有沒有這個 key——固定 schema 比「有 key 就代表 A、沒有 key 就代表 B」的隱性契約更不容易在 08b／09a／10a 各自實作時互相猜錯。

**下游消費者**：09a（⑤，待建立）／10a（⑦ Debug Agent，待建立）之後可以讀這兩個欄位——`fill_function()` 回報「AST 定位失敗：scaffold/task 不一致」時，先比對 `state["skipped_interfaces"]` 是否包含這個 `(file_path, class_name, function_name)`，命中則代表根因是④的骨架缺口、不是⑤的實作問題；同樣地，若某個 Repository 方法回傳型別是某個 Model class，⑤填空後在 Harness 驗證階段出現 `NameError`／找不到這個 class，`state["skipped_db_models"]` 就是唯一能回溯「這個 class 當初為什麼沒被渲染出來」的地方（不論是整個檔案失敗，還是這個 class 自己被逐 entity 隔離跳過）。實際判斷邏輯與 debug 產出格式屬於 09a／10a 的範圍，本文件只確保資料有進 `RefactorState`。

### 新增套件：`scaffold_agent/`

已落地，見 `08b_scaffold_agent_code.md`。

---

## 十三、錯誤處理範圍

比照 04a／05a／06a／07a 的既有邊界：`retry_count` 迴圈只包住 `implement → run_tests → debug`，`scaffold`（含 `build_db_models()`）不在這個迴圈裡：

- 掃描失敗（`javalang.parser.JavaSyntaxError`）：直接往上拋，中止整個 `build_db_models()` 呼叫——輸入端問題
- 逐 entity 隔離失敗：不中止，記進 `skipped_entities`（九章，經 `scaffold_node.py` 併入 `RefactorState.skipped_db_models`，見十二章），繼續處理其餘 entity
- `build_db_models()` 不呼叫任何 LLM／外部服務（純靜態解析 Java 原始碼），沒有網路層錯誤，沒有重試機制的適用空間——但正因為是同步 CPU 運算，呼叫端必須用 `asyncio.to_thread()` 包一層，見十二章

---

## 十四、待決定事項

- [x] `scaffold_agent` 對 `lang-exam-api-refactor` 真實專案的端對端驗證——已跑過完整 `scaffold → implement → run_tests → debug` pipeline（4 輪 debug 迴圈，13/26 → 22/26），過程中發現並修好 Enum 掃描（`_extract_enums()`）、Enum 建構子引數還原、`@EnableJpaAuditing` 稽核時間戳誤判、`extends` 依賴邊缺失四個真實 bug，見 `docs/09b_bug_trace.md` #44／#69／#98
- [ ] 十章列出、目前仍沒有真實案例觸發的機制（`@Embeddable`、複合主鍵、`@Table(schema=...)`、循環外鍵 `use_alter`、`@ManyToMany`／`@JoinTable`、跨 package 撞名、多層 `@MappedSuperclass` 疊加）在 `lang-exam-api-refactor` 之外其他真實專案的觸發比例，待接上更多專案才能確認——細節見十章各條目，不重複列
- [ ] Entity／Enum 跨 package 撞名下游看不到（十章相應條目）：`lang-exam-api-refactor` 尚未踩到，是否優先處理待真的觀察到再評估

---

*各 Agent／工具的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
