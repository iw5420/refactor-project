# ① 解析 Agent 詳細設計

> 本文件承接 `00_refactor_architecture.md` 的整體架構（見七、① 解析 Agent 一節），是這個 Agent 的**設計面**文件：決策、契約、資料結構、流程。實際程式碼實作見 `04b_parse_agent_code.md`；本文件不出現可執行的實作邏輯。

---

## 一、本文件範圍與定位

**本文件涵蓋**：
- ① 解析 Agent 的輸入來源（Java 原始碼＋[B] Collection Agent 產出的 skip 清單，見二章）
- Java 專案的靜態呼叫圖建構方式（機械分析，不用 LLM）
- Map-Reduce 語意摘要流程（Claude API，依 00 六章「map-reduce 模式」）
- skip endpoint 呼叫鏈排除邏輯
- 輸出資料如何對應 `RefactorState` 的 `module_list`／`api_to_python_target`

**本文件不涵蓋**：
- 實際程式碼——見 04b
- ③ 架構設計 Agent 如何消費 `module_list`／`api_to_python_target` 設計 Python 結構——見 05a
- `postman/unfilled_endpoints.json` 本身怎麼產生、`skip` 的完整語意定義——見 03a

---

## 二、輸入與前置資料

| 輸入 | 來源 | 說明 |
|---|---|---|
| Java 專案原始碼 | `RefactorState.java_project_path` | 全量原始碼；① 是唯一直接讀取 Java 原始碼的 Agent |
| skip endpoint 清單 | `postman/unfilled_endpoints.json`，固定路徑，直接讀檔案 | 取 `category="skip"` 的項目，作為五章呼叫鏈排除的起點；**不進 State**（這份檔案是「給人看的產出，不影響 pipeline 後續走向」，只有 ① 這個 node 內部需要，不需要跨 Agent 共用） |
| openapi_spec 的完整 endpoint 清單 | `RefactorState.openapi_spec`，[A] Spec Agent 產出，已在 State 裡，不需額外讀檔 | 只取 `paths` 底下所有 `(path, HTTP method)` 組合，列舉「全部 endpoint」，供五章判定「非-skip 全集」使用；不解析 schema 內容（見下方既有備註），不做其他語意判斷 |

> 這份輸入依賴 [B] Collection Agent（含人工填值/skip 關卡）先執行完畢，`unfilled_endpoints.json` 才會存在，對應 00 一章流程圖「[A] → [B] → ① → ② → ③」的順序，`01_langgraph_architecture.md`、`graph/builder.py`／`main.py` 均已依這個順序實作（`extract_spec` 為 entry point）。

> ① 不解析 request/response schema——這部分由 [A] Spec Agent 從 springdoc-openapi 自動取得，見 00 七。上面的 `openapi_spec` 輸入只用來**列舉 endpoint 字串**，完全不讀 `operation` 物件裡的 request/response schema 欄位，跟這條備註的精神一致，不是例外。

---

## 三、靜態呼叫圖建構（機械分析，不用 LLM）

依「能用程式判斷的，就不要交給 LLM」原則（00 二），呼叫圖建構是純程式邏輯，不呼叫 Claude API。這份呼叫圖是四、五兩章共用的基礎資料，只建構一次。

**決策：解析工具採用 `javalang`**（純 Python 套件，語法層級 AST parser，需新增進 `requirements.txt`）。理由：
- 這裡只需要語法層級資訊（class/method 簽名、field 宣告、annotation、method invocation 運算式），不需要完整語意型別解析（泛型推導、overload resolution 等）
- 純 Python 套件，不需額外裝 JDK 以外的工具鏈或 grammar 套件，跟專案其餘部分維持 Python-only 依賴一致
- 相較 tree-sitter-java 需要額外裝 grammar binding，對這個規模的需求是更輕量的選擇

已用目標專案 `lang-exam-api-refactor`（`pom.xml` 鎖定 `java.version=11`）驗證：全部 `.java` 檔案 100% 解析成功，含使用 `var` 的檔案；決策成立，不需要換 tree-sitter-java。

**建構流程**：

1. 掃描 `java_project_path` 下所有 `.java` 檔案，用 javalang 解析成 AST
2. 抽取每個 class 的 field 宣告（含型別），供解析 `this.xxxService.method()` 這類呼叫時，把 `xxxService` field 對應回它宣告的型別
3. 抽取每個方法內的 method invocation 運算式，嘗試解析呼叫目標：
   - 呼叫目標型別在專案內只有唯一一個實作類別 → 直接連接
   - 呼叫目標是 interface，且有多個 `@Service`／`@Component`／`@Repository` 實作類別 → 先比對注入點（field／建構子參數）上的 `@Qualifier`（value 是否對得上某個候選類別的 bean name）或候選類別上的 `@Primary`——這是 Spring 本身要求的消歧機制，沒有這項資訊又有多個實作，應用程式啟動時就會拋 `NoUniqueBeanDefinitionException`。比對到 → 只連那一個實作；比對不到（`@Qualifier` value 非字面字串常量、或消歧發生在 XML config／`@Profile` 這類分析範圍外的機制）→ **保守全連結**：連到所有候選實作類別，不用變數命名相似度之類的 heuristic 猜測
   - 完全無法解析（reflection、動態呼叫、鏈式呼叫中間回傳值型別未被步驟 2 追蹤——如 `this.orderService.getDetail().calculate()`，`calculate()` 連候選 class 都列不出來）→ 該呼叫連結留白，**不**當作任何方法被排除的依據；沒有其他路徑可達的話，最終落入五章步驟 4「預設保留」
4. 產出 `method_id → 直接呼叫的 method_id 清單`（`method_id` 格式：`{java_file_path}::{class_name}::{method_name}`，避免同名方法跨類別衝突）
5. **Controller Route 索引**（供五章使用）：同一次掃描，對每個 `@RestController`／`@Controller` 抽取 route 資訊，產出 `endpoint_key → method_id` 索引：
   - base path 讀 class 上 `@RequestMapping` 的 `value`／`path`；沒標注則視為空字串
   - method path／HTTP method 讀 `@GetMapping`／`@PostMapping`／`@PutMapping`／`@DeleteMapping`／`@PatchMapping`，或 `@RequestMapping` 的 `method` 屬性
   - 完整 path＝base path＋method path，依 Spring 規則拼接；`value`／`path`／`method` 宣告成陣列時逐一展開，一個 method 可對應多個 `endpoint_key`
   - `endpoint_key` 格式沿用 `unfilled_endpoints.json` 的 `endpoint` 欄位（`{HTTP_METHOD} {path}`，如 `POST /api/exam/answer/save`，見 03a）——不套用 02a `route_to_file_mapping` 的「動態段換成 `{id}`」normalize 規則，這裡兩邊比對的都是路徑樣板，不是實際打出去的 URL
   - **不用 openapi.json 的 `operationId` 對回 method**：實測 `lang-exam-api-refactor` 的 `FileController` 裡有兩個同名方法（`voice`：一個 `@PostMapping`、一個 `@GetMapping`），springdoc 產生的 `operationId` 分別是 `voice`／`voice_1`，哪個掛 `_1` 由 springdoc 內部生成順序決定，不是能從 Java 方法名反推的規則。`{HTTP_METHOD} {path}` 這個 key 在同一個案例裡完全不會混淆（GET/POST 本來就是不同 key），比 `operationId` 可靠
   - annotation 值非字面字串常量（引用常數、SpEL 表達式）時不產生索引項目
6. **Context-Path 前綴校正**：建索引前掃描主設定檔（`java_project_path` 下 `src/main/resources/application.properties`／`application.yml`）讀取 `server.servlet.context-path`（Spring Boot 2.x key 名稱，對應目前專案的 Spring Boot 2.7.11），讀到就當成所有 `endpoint_key` 的共同前綴——springdoc 產生的 `openapi.json` path 會帶這個前綴，`@RequestMapping` 字面值不會。只涵蓋寫死在主設定檔的情況；profile-specific 檔案覆蓋、環境變數覆蓋這類殘餘情況交給五章的 warning log fallback 兜底，不在此模擬 Spring 的設定解析邏輯

> **設計原則**：呼叫解析遇到無法唯一確定的情況，一律偏向「多連、少排除」——多留一個沒用到的連結，代價只是後續分析多考慮一種可能；漏掉一個實際存在的連結，可能讓還在使用的方法被誤判成可排除，是不可逆的錯誤。

---

## 四、Map-Reduce 語意摘要（Claude API）

依 00 六章「Claude API 端的大範圍語意判斷：map-reduce 模式」，整包 Java 原始碼不能一次丟給 Claude API，須依自然邊界拆分後平行產出局部候選，再做跨邊界合併。

**Map 階段分兩個子階段，先共用、後專屬**：從每個 `@RestController`／`@Controller` 出發，沿 field 依賴關係**遞迴展開**，取得該 Controller 完整可達的 class 依賴閉包——不能只算 Controller 自己宣告的欄位這一層。Spring Boot 專案常見 Controller → Service → Repository 三層架構，Repository 通常只被 Service 注入、不會被 Controller 直接注入，若只算一層，Repository 這類二階以上的 class 會永遠進不了 Map 階段、Reduce 完全看不到，`module_list` 因此靜默漏掉這些 class 的所有方法——這在真實 Spring 分層架構下沒有邏輯根據站得住腳，因此改為遞迴展開閉包（實作見 `04b_parse_agent_code.md` 四章 `controller_dependency_closure()`）。遞迴展開需要用 visited 集合防止 class 間互相依賴造成無窮迴圈（如 ServiceA 依賴 ServiceB、ServiceB 又依賴回 ServiceA）。統計每個 class 出現在幾個不同 Controller 的依賴閉包裡（in-degree；閉包本身會遞迴走過 Service/Repository 彼此之間的依賴，但 in-degree 只計算「這個 class 屬於幾個不同 Controller 各自的閉包」，同一個 Controller 閉包內不重複計數）。in-degree ≥ 2 的 class 判定為共用類別。

> **Repository interface 的補充掃描**：上一段的遞迴閉包解決的是「Repository 是一個 class、只是深度較深」的情況。但 Spring Data JPA 常見寫法是 `interface XxxRepository extends JpaRepository<...>`，沒有手寫實作類別，執行期由 Spring 動態產生 proxy——三章 `_extract_classes()` 只掃 `ClassDeclaration`，這種 interface 不會建立 `ClassInfo`，遞迴閉包再深也走不到。因此三章另外用 `_extract_interfaces()` 把 interface 宣告也建成 `ClassInfo`，只在 `_resolve_type_name_to_classes()` 原本「完全無法解析」的 fallback 分支生效——`interface_implementors` 優先查詢的順序不變，interface 有 `@Service`／`@Component`／`@Repository` 明確實作時的既有解析路徑不受影響。這不影響五章 skip 排除的正確性：Repository 方法從未成為呼叫圖節點，不可能被誤判排除，仍符合「多連、少排除」的安全方向。細節見 `04b_parse_agent_code.md` 三章 `_extract_interfaces()`。

> **Import 依賴補充**：欄位依賴與呼叫圖解析（`_walk_and_resolve()`）只認得透過 DI 欄位、或 `this.x.y()` 欄位鏈式呼叫建立的依賴，對靜態方法呼叫（`ClassName.staticMethod()`）、方法參考等不透過欄位建立關係的用法無法解析——這類依賴完全不會進 `controller_dependency_closure()`，對應 class 的業務邏輯會整個從 `module_list` 消失，沒有任何 warning（常見案例：用 `org.springframework.data.jpa.domain.Specification` 組動態查詢條件的 helper class，透過 `ClassName.staticMethod()` 呼叫，從未被注入成欄位）。
>
> **不試圖窮舉每一種 Java 呼叫語法去解決**（靜態呼叫只是其中一種，方法參考、反射會是同一類症狀，逐一補呼叫圖分支永遠可能還有下一種沒覆蓋到）；改用更通用、不依賴語法細節的訊號：**只要一個 class 明確 import 了另一個專案內的類別，兩者之間就算一條依賴**，不管這個依賴實際上透過欄位、靜態呼叫、方法參考、還是任何其他方式建立——import 陳述式是編譯期就確定的事實，不需要理解呼叫語法。三章 `_extract_project_imports()` 負責抽取，`controller_dependency_closure()` 的 `_direct_deps()` 在既有的欄位依賴之外多納入這份 import 依賴，只在專案內類別間生效（外部函式庫 import 沒有對應的 `ClassInfo` 可比對，天然被排除），不套用 `@Qualifier`／`@Primary` 那套 DI 消歧邏輯——import 是編譯期就確定的單一目標，沒有 DI 那種「多個實作選一個」的歧義。細節見 `04b_parse_agent_code.md` 三章 `_extract_project_imports()`、四章 `controller_dependency_closure()`。

- **子階段 4a（共用類別批次，先執行）**：把所有共用類別抽出、獨立成一批（或視數量拆多批，見十章待決定事項），各自摘要一次，跑完才進入下一子階段。共用類別當成 Map 階段的基礎層先建好，不是跟 Controller 分組混在一起搶跑。
- **子階段 4b（Controller 批次，後執行，組間可平行）**：每個 Controller 分組保留依賴閉包中 in-degree = 1 的專屬依賴（閉包扣掉共用類別後的其餘部分，不是只看 Controller 自己宣告的欄位型別）；分組依賴到的共用類別，直接把 4a 已經產出的候選摘要（一小段文字，不是原始碼）帶進這個分組的 prompt，讓分組摘要能完整交代「呼叫了 XXX，做什麼用」，不需要重新分析共用類別的原始碼。

4a 在 4b 之前完成，是這個設計成立的前提——4b 要引用的是「已經算好的共用摘要」，不是「猜共用類別可能長怎樣」。03a 是用 OpenAPI tag 分組，① 處理 Java 原始碼沒有現成的 tag 可用，改以呼叫圖依賴子圖當自然邊界。

這一步不需要額外圖論工具，只是一次輕量 BFS（沿 field 依賴關係遞迴展開閉包，寫法比照五章 `skip_filter.py` 既有的可達性分析；跟三章已建好的方法呼叫圖是兩份不同的資料——三章的呼叫圖是 method_id 層級的呼叫關係，這裡要的是 class 層級的欄位依賴，不能互相取代），呼應 00 二章「能用程式判斷的，就不要交給 LLM」。「哪些 Controller 的依賴閉包含這個共用 class」也是程式算出來的精確事實，Reduce 判斷共用 class 最終歸屬哪個 module 時直接拿這份事實當輸入，不需要依賴下方「跨組依賴線索」那個為了「呼叫圖抓不到、只能用語意猜」而設計的候選欄位。這麼做同時降低 token 成本（共用類別只摘要一次，不隨 fan-in 重複）、提升一致性（Reduce 拿到單一候選摘要，不是好幾個 Controller 分組各自產出、可能互相不一致的版本），4b 的 prompt 也因為直接複用 4a 的產出而變薄。

> **Map 摘要必要性判斷（省 API 額度）**：import 依賴補完後，Controller 依賴閉包會涵蓋大量 entity／DTO／enum／工具類別——這些類別若每個都送一次 Claude API 摘要，多半是浪費，entity／DTO 通常只是欄位容器，沒有值得摘要的業務邏輯。**但不能用 package／命名慣例判斷「這是 entity 所以沒邏輯」**：DDD 風格的富領域模型常把業務規則直接寫在 entity 自己的方法裡（如 `isEligible()`），這種情況即使類別標了 `@Entity`，也確實有邏輯要摘要，用命名／package 篩選會直接誤判掉，這類篩選在通用性上站不住腳。
>
> 改用內容判斷，分四層（實作見 `04b_parse_agent_code.md` 四章 `needs_llm_summary()`）：
> 1. **用到已知的動態查詢型別（如 `org.springframework.data.jpa.domain.Specification`）一律判定有邏輯**——這類型別的實際邏輯常常寫在回傳的 lambda 裡，看 method body 有沒有控制流程這種通用判斷法會直接誤判成「沒邏輯」，只能靠型別本身當訊號，繞開 body 內容判斷。
> 2. **介面、找不到任何具體實作類別、且方法清單裡沒有任何方法帶本體 → 判定不需要送 Map**——Spring Data JPA Repository 的典型形狀（`interface XxxRepository extends JpaRepository<...>`，Spring 在執行期動態生成 proxy，原始碼裡不存在任何實作）：這批方法沒有方法本體，LLM 讀到的資訊跟機械解析器完全一樣（都只有方法名稱／`@Query` annotation 可用），送 Map 花錢請 LLM 用比較不穩定的方式做一件機械解析能做得更準、更便宜、每次結果一致的事，違反 00 二章「能用程式判斷的，就不要交給 LLM」。**若這個介面有具體實作類別**（`@Service`/`@Component`/`@Repository` 標註），真正的業務邏輯寫在實作類別自己的方法裡（有本體，正常送 Map），介面本身的抽象宣告不適用這條規則，避免同一個業務方法產生兩筆不一致的記錄。
> 3. **有 Lombok／JPA 資料類別標記（`@Entity`／`@Data`／`@Getter`／`@Setter` 等），且方法清單裡只有存取器方法（`get*`／`set*`／`is*`／`equals`／`hashCode`／`toString`）→ 判定為純資料類別，不送 Map**——annotation 只是「要不要進一步檢查方法清單」的觸發點，真正決定「有沒有邏輯」的是方法清單本身：DDD entity 若有 `isEligible()` 這種非存取器命名的方法，即使標了 `@Entity`，也會落到規則 4，正常送 Map，不會被規則 3 誤判掉。
> 4. **其餘情況（沒有標記，或有標記但還有非存取器方法）→ 預設有邏輯，送 Map**，呼應「多連、少排除」同一種保守精神：不確定就當作可能有邏輯，多花一次 API 呼叫的代價遠低於漏掉真實業務邏輯的代價。
>
> 判定不需要送 Map 的類別，仍要出現在 `module_list.java_files`（③ 需要知道這個檔案存在），改用機械組出的佔位摘要取代（不呼叫 API），連同真的呼叫 Claude API 摘要出來的結果一起送進 Reduce——不這樣做的話，這批類別連 Reduce 都看不到，等於把 import 掃描想解決的完整性問題重新引入。規則 3（Lombok 純資料類別）的存取器方法本來就不值得個別翻譯，佔位摘要的 `methods` 維持空清單；規則 2（無實作類別的介面）不一樣，這批方法是真實業務行為，只是不需要 LLM 描述，佔位摘要改用機械解析組出真正的方法描述，優先序：**有 `@Query` 字面字串 → 直接抄錄**（比任何摘要都精確）；**否則符合 Spring Data 衍生查詢命名慣例**（`findBy`／`existsBy`／`countBy` 等 + 欄位條件）**→ 拆解欄位名稱組出描述**，遇到 `Top`／`Distinct`／`OrderBy` 這類複雜關鍵字保守退回一句誠實說明，不強行拆解、以免把非欄位關鍵字誤當成欄位名稱；**都不符合 → 誠實占位**，明講「無法機械推斷語意，需人工核對」，`complexity` 標記 `"medium"` 提醒多留意。細節見 `04b_parse_agent_code.md` 四章 `classify_trivial_classes()`、七章 `_mechanical_summary()`／`_describe_derived_query()`／`_describe_bodyless_method()`。
>
> 規則 2 用到的「有無實作類別」判斷重用 `build_interface_implementors()`（三章步驟 3.2 消歧邏輯的既有產出，不重新計算）；規則 3 用到的 Lombok／JPA annotation 清單集中在 `common/java_annotations.py`（見 00 六章「Java class annotation 判斷（共用工具）」），跟 ③ 架構設計 Agent 判斷「孤兒類別／資料容器占位」共用同一份定義（見 `05a_design_agent_architecture.md` 三章）——這裡只引用，不重複定義。

Map 階段（依組拆分，4a 先、4b 後、4b 組間可平行呼叫 Claude API），每組只做：

| 產出 | 說明 |
|---|---|
| class 業務邏輯摘要 | 每個 class 負責什麼，一段文字 |
| method 功能描述與複雜度初判 | 對應 `MethodInfo.description`／`complexity` |
| 跨組依賴線索 | 「看起來」呼叫了其他組的 class（不下最終模組歸屬判斷，只標記候選） |

Map 階段輸出的是局部候選結果，同一組看不到其他組的內容，不做跨組判斷。

**併發數**：4b（Controller 批次，組間可平行）呼叫 `common/concurrency.py` 的 `default_concurrency()` 決定併發數（可用核心數 − 1，執行期動態計算，不寫死），跟 03a 共用同一份實作，不各自重新推導（見 00 六章「Map 階段併發數（共用工具）」）。4a 只有一批（或視體積拆出的少數幾批，見十章），不特別談併發。

**單一分組（4a 或 4b 任一組）呼叫 Claude API 失敗時**：
1. 失敗的組列入「待重試清單」，不立即讓整個 `parse` 中止
2. 該次 Map 呼叫全部跑完後，等待 5 分鐘，對待重試清單裡的組統一重試一次（只重試一次，不是無限重試或指數退避——4a／4b 全部加起來也就個位數到十幾組，一次性的固定延遲已經夠用，不需要更複雜的退避策略）
3. 重試仍失敗 → 留在待重試清單，標記「已重試且失敗」

這個「待重試清單」只在單次 `parse` 執行內存在（記憶體中，不落地、不跨執行續存）——`parse` 不在 `retry_count` 迴圈裡（見九章），這裡的重試是為了扛過網路抖動、API 短暫限流這類幾秒到幾分鐘內會自己恢復的雜訊，不是要取代 `retry_count` 迴圈那種「整條 pipeline 層級」的重試語意。重試仍失敗之後的處理見十章。

**Reduce 階段**（單次呼叫，輸入為 Map 階段濃縮後的候選結果，非原始碼全量）：

| 產出 | 對應 State 欄位 |
|---|---|
| 最終 module 拆分（哪些 class 歸在同一個 module） | `ModuleInfo.module`／`java_files` |
| module 業務邏輯摘要（彙整同 module 內各 class 摘要，重新以模組為單位摘要，不是機械拼接） | `ModuleInfo.summary`（見六章） |
| module 間依賴關係 | `ModuleInfo.depends_on` |

Reduce **不**決定方法層級的內容——`MethodInfo`（`java_method`／`class_name`／`description`／`complexity`）全部沿用 Map 階段已經產出的方法清單，Reduce 只負責「這個 class 歸哪個 module」，見六章、`04b_parse_agent_code.md` 七章 7.3。

> 若合併階段只是機械拼接各組候選、不重新做跨邊界判斷，會系統性漏掉組與組之間的關聯（00 六章已強調過這點），因此 Reduce 階段的「重新摘要」不可省略，不能只是把 Map 階段的 class 摘要照抄堆疊成 module 摘要。

**用量記錄**：Map／Reduce 呼叫 Claude API 一律經由 `common/llm_client.py` 的 `call_claude_for_json()`（跨 Agent 共用的呼叫封裝，見 00 六章「Claude API 呼叫封裝」），不自己重新實作 client 初始化或 `log_usage()` 串接——`log_usage()` 的呼叫已經在 `common/llm_client.py` 內部處理好，`parse_agent/summarize.py`（見七章）只需要呼叫 `call_claude_for_json()` 並傳入自己的 `model`（讀 `PARSE_AGENT_MODEL` 環境變數，見七章 `llm.py`）。

---

## 五、skip 呼叫鏈排除

**排除條件**：一個方法被排除、不進最終 `module_list`，若且唯若它「只能從 skip endpoint 到達、且完全無法從任何非-skip endpoint 到達」。

**流程**：

1. **兩組起點清單的來源不同，但都要查三章步驟 5 的 `endpoint_key → method_id` 索引**才能轉成 Java controller method：
   - skip 組：二章讀到的 skip endpoint 清單（`postman/unfilled_endpoints.json` 的 `category="skip"` 項目）
   - 非-skip 組：`openapi_spec["paths"]` 底下**全部** `(path, HTTP method)` 組合，扣掉 skip 組——不是查三章 `route_index.keys()`：`route_index` 是機械掃出的索引，可能漏掉部分實際存在的 route（見三章步驟 5），拿它自己當全集會讓這類漏掃的 route 不產生任何「查無對應」警告；`openapi_spec` 來自 [A] Spec Agent 對實際跑起來的 Java 服務取得，沒有這個問題，能讓下面的警告機制對 skip／非-skip 兩組同等生效

   兩組清單各自逐筆用 `endpoint` 欄位（`{HTTP_METHOD} {path}`）查 `endpoint_key → method_id` 索引：
   - 查到一個 method_id → 作為對應組的起點
   - 查到多個 method_id（不會是 Spring 本身的路由衝突——[A] Spec Agent 執行 ① 之前已經成功啟動 Java 服務、拿到 `openapi.json`，代表這個服務跑得起來，就不可能存在真正重複的 `(HTTP method, path)`。這裡出現多個只可能是三章步驟 5 自己的索引邏輯有 bug）→ 全部視為起點，沿用「多連、少排除」原則兜底，不特別處理
   - 查無對應 method_id（route 索引沒解析出來、或三章步驟 6 沒涵蓋到的 context-path 殘餘情況）→ **不中止、不報錯**，這筆 endpoint 直接跳過、不貢獻任何起點，但記錄一筆 warning log（含原始 `endpoint` 字串、所屬組別 skip／非-skip）——查無對應時「不排除」是安全的，但保持沉默會讓一份人工維護的 skip 決策、或是一支實際存在的非-skip API，全部失效卻沒人發現，所以用 log 讓開發者能事後檢查
2. 沿三章建好的呼叫圖分別做可達性分析，得到「skip 可達集合」與「非-skip 可達集合」
3. 真正排除的方法 = `skip 可達集合 − 非-skip 可達集合`
4. 兩個集合都沒碰到的方法（呼叫圖分析不到任何起點連過去的孤立方法）**預設保留**，不受這次排除邏輯影響——維持「預設保留、只有明確證據才排除」的原則

**共用方法會被保護**：一個方法只要同時出現在非-skip 可達集合裡，無論是否也被 skip endpoint 呼叫到，都會保留，正常進入 `module_list`，後續照常派 task 改寫。

**skip endpoint 的絕對排除保證（`api_to_python_target`）**：上面「共用方法會被保護」的判斷單位是 `method_id`（`{java_file_path}::{class_name}::{method_name}`，不含參數簽名，見三章「決策」：javalang 不做 overload resolution）。若同一個 class 內有兩個同名多載方法各自掛不同 HTTP method 的 route（例如一個方法用 `@PostMapping` 處理上傳、另一個同名方法用 `@GetMapping` 處理下載），這兩個物理上不同的方法會共用同一個 `method_id`。此時若其中一個掛的 route 是 skip、另一個不是，「共用方法會被保護」規則會把兩者都當成同一個方法保護下來——結果是人工明確標記 skip 的那個 endpoint，透過這個 method_id 共用關係悄悄復活，重新出現在 `api_to_python_target`，繞過了人工的排除決策。

人工標記 skip 是對**這一個 endpoint** 下的判斷，不該因為底層 `method_id` 剛好被另一個方法共用就被覆蓋。因此 `api_to_python_target` 的組裝多一道**獨立、無條件生效**的關卡：`skip_endpoints` 清單裡的每一個 endpoint_key，不論底層 method_id 判斷結果為何，一律不會出現在 `api_to_python_target`。這道關卡只影響 `api_to_python_target` 這個 endpoint 級輸出，不改變 `module_list` 的排除邏輯——共用 `method_id` 的另一個非-skip 分支的方法描述，仍依「共用方法會被保護」規則正常留在 `module_list`（`module_list` 對這類多載方法本來就有「精準度略降」的既有限制，見三章「決策」）；這種 method_id 碰撞發生時額外記一筆 warning，供人工核對 `module_list` 裡的描述是否混雜了 skip 分支的行為。

排除發生在 Reduce 階段輸出 `module_list`／`api_to_python_target` 之前，被排除的方法從一開始就不會出現在最終輸出裡——③／[P]／④／⑤／⑦ 這些下游 Agent 完全不需要知道 skip 這個概念存在，只會看到已經排除乾淨的模組清單。

---

## 六、輸出格式與 State 對應

`module_list`／`api_to_python_target` 直接對應 `graph/state.py` 的 `ModuleInfo`／`MethodInfo`／`ApiMapping`，不重新定義結構：

```python
class MethodInfo(TypedDict):
    java_method: str
    class_name: str                # 所屬 Java class（見下方說明）
    description: str
    complexity: Literal["low", "medium", "high"]

class ModuleInfo(TypedDict):
    module: str
    summary: str                   # 見下方說明
    java_files: list[str]
    depends_on: list[str]
    methods: list[MethodInfo]

class ApiMapping(TypedDict):
    endpoint: str
    http_method: str
    java_controller: str
    module: str
```

**`MethodInfo.class_name`**：所屬 Java class 名稱。同一 module 內常見跨層同名方法（如 `UserService.getById()` 與 `UserRepository.getById()`，service 委派 repository 時命名本來就容易一致）——若沒有這個欄位，③ 架構設計 Agent 重新掃描 `module.java_files` 的簽名時，光憑 `java_method` 字面名稱無法判斷這筆描述原本對應哪個類別，見 `05a_design_agent_architecture.md` 二章、四章。

**`ModuleInfo.summary` 的定位**：00 文件要求 ① 輸出「核心業務邏輯摘要」，這個欄位就是它的落地位置，設計目標是給 ③ 架構設計 Agent 使用，不是給人閱讀的專案文件。因此內容要直接支援 ③ 的判斷，不是泛泛複述程式碼在做什麼：

- 這個 module 對外暴露的業務行為是什麼（幫助 ③ 判斷哪些方法該進 interface、哪些是內部細節）
- 為什麼這些 class／method 被歸在同一個 module（幫助 ③ 判斷 Python 檔案切分是否合理，或需要進一步拆分）
- 與 `depends_on` 列出的其他 module 之間，依賴關係的業務原因（不只是「依賴誰」，還有「為什麼」，幫助 ③ 判斷 route_to_file_mapping 的合理邊界）

`MethodInfo.description` 維持方法層級的細節描述，兩者不是取代關係：`summary` 是 ③ 理解模組邊界用的「先讀」，`description` 是逐一設計 interface 簽名時的「細看」。

**① 不產出「Java → Python 技術對應表」**：00 七章只要求「模組清單與依賴關係、核心業務邏輯摘要、API 對應表」，不包含檔案／函式層級的 Java→Python 對應——這件事完全交給 ③ 架構設計 Agent 的 `python_structure.interfaces`（顆粒度要求：檔案相對路徑＋函式簽名層級，見 00 三章）。

**① 不產出任何 Python 命名建議欄位**：不存在 `ModuleInfo.python_files`、`MethodInfo.python_method`、`ApiMapping.python_target` 這幾個欄位——全專案沒有任何下游讀取它們。`graph/scheduler.py` 的 regression 偵測（`module_owned_files`，判斷⑤這次寫入是否波及已驗證過的上游 module）改從 `task_list.target_files`（③ 的權威路徑，見 `TaskSpec` 定義）彙整，不依賴 ① 對 Python 檔名的猜測——① 的檔名只是建議、③ 可能整個改寫，拿它當權威路徑比對會讓 regression 偵測悄悄失效。

[P]／④／⑤ 一律經由 ③ 的 `python_structure` 走。

---

## 七、模組結構規劃

比照 `spec_collection_agent/`、`refactor_harness/`、`translator_cli/` 的組織方式，① 的邏輯規劃為獨立套件 `parse_agent/`，`graph/nodes/parse_node.py` 維持薄封裝：

```
refactor-project/
└── parse_agent/
    ├── call_graph.py        # 三章：javalang 掃描、呼叫圖建構、Controller Route 索引
    ├── grouping.py          # 四章：Map 階段分組（Controller 依賴子圖、共用類別抽取）
    ├── summarize.py         # 四章：Map/Reduce 呼叫 Claude API
    ├── skip_filter.py       # 五章：skip endpoint 對照 method_id、可達性分析、排除邏輯
    └── prompts.py           # Map/Reduce 的 system prompt 集中於此
```

| 職責 | 說明 |
|---|---|
| 掃描 Java 專案、建呼叫圖、建 Controller Route 索引 | 對應三章 |
| Map/Reduce 語意摘要 | 對應四章 |
| skip 呼叫鏈排除 | 對應五章 |
| 組裝 `module_list`／`api_to_python_target` | 對應六章 |
| 對外唯一入口 | 供 `graph/nodes/parse_node.py` 呼叫，node 本身不直接碰觸上述任何細節 |

---

## 八、與 LangGraph 整合

`parse` 是線性 node，讀 `java_project_path`，寫回 `module_list`／`api_to_python_target`，比照 01 七 stub 慣例整包展開 state（`{**state, ...}`）。

排在 `gen_collection`（[B] Collection Agent 階段二）之後、`record_tests`（② 測試 Agent）之前，不是入口 node（依賴原因見二章）——`01_langgraph_architecture.md`、`graph/builder.py` 均已依這個順序實作（`extract_spec` 為 entry point）。

---

## 九、錯誤處理範圍

比照 03a 六章的邊界：`retry_count` 迴圈只包住 `implement → run_tests → debug`，`parse` 不在這個迴圈裡。呼叫圖建構失敗（如原始碼有無法解析的語法）直接往上拋，整條 LangGraph run 中止，交由人工排查——重跑 `implement`/`debug` 解決不了輸入端的問題。Map/Reduce 的 Claude API 呼叫失敗，先走四章「待重試清單、5 分鐘後重試一次」的緩衝；重試仍失敗之後的處理見十章。

---

## 十、待決定事項

以下事項已在 04b 落地為具體實作，列出對應位置供查閱（實際門檻值、觸發頻率仍待接上真實專案調校，不影響這裡的處理方式）：

- `@RequestMapping` 系列 annotation 的 `value`／`path`／`method` 引用非字面字串常量（`static final` 常數、SpEL 表達式）時，javalang 解析不出來——04b 採「回傳空值、不索引該筆 route，記 warning」的保守處理，見 `04b_parse_agent_code.md` 三章 3.1／3.4 `_element_to_strings()`／`_extract_request_mapping_methods()`
- `@Qualifier` 比對不到、但專案仍能正常啟動的情況（消歧發生在 XML config／`@Profile` 這類分析範圍外的機制）——04b 落地為「保守全連結」，見 `04b_parse_agent_code.md` 三章 3.2 `resolve_field_target_classes()`
- 共用類別批次（四章子階段 4a）數量或原始碼體積過大時的拆分——04b 呼叫 `common.chunking.chunk_by_char_budget()`（跟 [B] Collection Agent 的 `_chunk_operations()` 共用同一份切批次演算法，見 00 六章「Map 階段切批次（共用工具）」），門檻值開放 `PARSE_AGENT_MAP_CHUNK_CHARS` 環境變數調整、不需要改程式碼，見 `04b_parse_agent_code.md` 四章
- Map/Reduce 單一分組（4a 或 4b）重試一次仍失敗後的處理——04b 先採「中止整條 parse run」的保守預設（理由：留空繼續會讓 ③ 在不知情的狀況下對著不完整的模組清單做設計決策，風險更高），日後若要改成「缺摘要繼續跑」，只需要調整該函式內部、不影響其他模組介面，見 `04b_parse_agent_code.md` 七章 7.1 `run_map_phase_with_retry()`
- Spring Data 衍生查詢命名慣例解析（規則 2 的機械描述）刻意只認「動詞緊接 `By`」這個嚴格形狀（`findByX`／`existsByXAndY`），`findAllBy`／`findFirstBy` 這類動詞與 `By` 之間夾了修飾詞的寫法會退回「認得出是衍生查詢方法、但不逐一拆解欄位」的寬鬆說明，不會誤判成複雜關鍵字（`Top`／`OrderBy`）——已對真實 `lang-exam-api-refactor` 專案驗證過，10 個真實 Repository 方法都正確產出可用描述，見 `04b_parse_agent_code.md` 七章 `_describe_derived_query()`

尚待定案：

- [ ] `findAllBy`／`findFirstBy` 這類修飾詞退回寬鬆說明時，訊息文字寫死引用「`Top`／`Distinct`／`OrderBy` 等複雜關鍵字」，但實際觸發原因可能只是動詞與 `By` 之間有修飾詞、不一定真的含這些關鍵字——訊息本身沒有講錯事實（仍然是誠實的「無法逐一拆解」），只是引用的原因不夠精確，待評估是否要把這類修飾詞也收進嚴格解析規則

---

## 十一、全域生效類別收集（`_global` 保留模組）

**背景**：09b 端對端整合測試發現（見 `docs/09b_bug_trace.md` #11/#12），Java 端 `@RestControllerAdvice`（全域例外處理，透過 Spring component-scan 自動生效、不被任何 Controller 用欄位注入）結構上永遠進不了四章 Map-Reduce 分組演算法——`controller_dependency_closure()` 只從 `RestController`／`Controller` 出發做欄位依賴 BFS，這類全域生效的類別不是任何 Controller 的依賴，也不是 BFS 起點，兩層都碰不到。

**決策**：新增完全獨立於四章 BFS 分組之外的第二收集路徑：

1. `parse_agent/grouping.py::collect_global_advice_classes(project)`：直接掃 `project.classes` 比對 `@RestControllerAdvice`／`@ControllerAdvice` 這兩個 annotation，不透過 BFS。
2. 找到的類別方法仍送四章的 Map 階段做語意摘要（沿用既有 Claude API 呼叫與重試機制，維持 [P] 消費描述的品質），但**不送進 Reduce 做模組歸屬判斷**——這批方法的歸屬是確定性的（本來就不屬於任何業務模組），不需要再問一次 LLM，呼應 00 二章「能用程式判斷的，就不要交給 LLM」。
3. `parse_agent/summarize.py::_assemble_global_advice_draft()` 機械組成一筆保留模組 `module="_global"`（`depends_on=[]`），沿用既有 `ModuleInfo`／`MethodInfo` schema，不新增型別，附加進 `run_map_reduce()` 最終回傳的 `module_list`。

**下游影響**：③ 架構設計 Agent 需要對 `module="_global"` 走專屬渲染路徑（見 `05a_design_agent_architecture.md` 十四章）；[P] Plan Agent 的檔名反查規則需要認得 `app/core/exception_handlers.py` 這個固定路徑（見 `06a_plan_agent_architecture.md` 四章）；④ translator-cli 骨架生成同樣需要認得這個檔案（見 `07a_translator_cli_architecture.md` 十五章）。

**真實環境驗證**：已對真實 `../lang-exam-api-refactor` 跑過完整 ①③[P]④⑤⑥，`module_list` 正確產出 `_global` 模組（`java_files=["src/main/java/com/teachLanguage/exception/GlobalExceptionHandler.java"]`），且下游一路串到⑤翻譯、⑥容器內實際觸發（Starlette 例外處理中介層確實呼叫到 ⑤ 產出的 `handle_all`），見 `09b_bug_trace.md`。

程式碼實作見 `04b_parse_agent_code.md` 對應章節；單元測試見 `tests/parse_agent/test_grouping.py`。
