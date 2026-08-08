# ① 解析 Agent 程式碼實作

> 04a 是設計面文件，本文件是實作面文件，一一對應、不重複設計理由——每節開頭註明對應 04a 章節，這裡只講怎麼落地成程式碼。對應 `00_refactor_architecture.md` 十一章文件索引的 `04b_parse_agent_code.md`。

04a 七章定義的套件結構只列了 5 個檔案（`call_graph.py`／`grouping.py`／`summarize.py`／`skip_filter.py`／`prompts.py`）。比照 03c 新增 `postman_tree.py` 的先例，本文件另外補上 `types.py`（內部型別）、`exceptions.py`（例外階層）、`llm.py`（Claude API 呼叫封裝，各 Agent 自己一份，見 00 六章）、`__init__.py`（對外唯一入口）——都是落地時必然需要、但不屬於 04a 設計決策範圍的基礎設施檔案。

## 目錄

| 檔案 | 對應章節 | 說明 |
|---|---|---|
| `parse_agent/exceptions.py` | 09 | 例外階層 |
| `parse_agent/types.py` | 03～06 | 內部資料結構（`ClassInfo`／`ParsedProject` 等） |
| `parse_agent/call_graph.py` | 03 | javalang 掃描、呼叫圖建構、Controller Route 索引 |
| `parse_agent/grouping.py` | 04 | Map 階段分組（in-degree、共用類別、4a/4b 批次） |
| `parse_agent/prompts.py` | 04 | Map/Reduce system prompt 與 output schema |
| `parse_agent/llm.py` | 04 | Claude API 呼叫封裝 |
| `parse_agent/summarize.py` | 04, 06 | Map/Reduce 呼叫、重試佇列、輸出組裝 |
| `parse_agent/skip_filter.py` | 05 | skip 呼叫鏈排除 |
| `parse_agent/__init__.py` | 07 | 對外唯一入口 `run_parse_agent()` |
| `graph/nodes/parse_node.py` | 08 | LangGraph node（取代目前的 stub） |

---

## 一、`exceptions.py`——例外階層

對應 04a 九章「錯誤處理範圍」：呼叫圖建構失敗直接往上拋、中止整條 run；Map/Reduce 的 Claude API 呼叫失敗先走重試佇列緩衝，重試仍失敗才拋出硬性失敗（見四章 `summarize.py`）。

```python
# parse_agent/exceptions.py
"""① 解析 Agent 例外階層，對應 04a 九章「錯誤處理範圍」。呼叫圖建構
（三章）的失敗一律視為輸入端問題，直接往上拋、中止整條 LangGraph run，
不在這裡額外包裝——javalang 拋出的 `JavaSyntaxError`／檔案 I/O 例外原樣
往上傳即可，呼叫端不需要特別分類。這裡只定義 Map/Reduce（四章）專屬的
例外，因為那裡有明確的「重試佇列、重試仍失敗才視為硬性失敗」中間狀態，
需要一個專屬型別承接。
"""
from __future__ import annotations


class ParseAgentMapReduceError(Exception):
    """Map 或 Reduce 階段的 Claude API 呼叫，在 summarize.py 的重試佇列
    機制（見 04a 四章、summarize.py `run_map_phase_with_retry()`）跑完
    仍有分組失敗時拋出，中止整條 run（見 04a 十章、summarize.py
    `run_map_phase_with_retry()` 模組說明的保守預設理由）。日後若要改成
    「缺摘要繼續跑」，只需要改 `run_map_phase_with_retry()` 內部處理，
    不需要動這個例外型別本身。
    """
```

---

## 二、`types.py`——內部型別定義

對應 04a 三～六章。輸出面（`module_list`／`api_to_python_target`）直接沿用 `graph/state.py` 既有的 `ModuleInfo`／`MethodInfo`／`ApiMapping`（見 04a 六章「不重新定義結構」），這裡只定義 04b 處理過程需要、不需要跨模組交接的中間資料結構。

**已知限制：`ParsedProject.classes` 以 class 簡單名稱為 key，不處理跨檔案同名類別**——真實驗證過的目標專案（`lang-exam-api-refactor`，見 04a 三章）90 個檔案沒有這個情況，這裡不做完整的 import 解析／FQN 消歧（那需要額外追蹤每個檔案的 `import` 陳述式、判斷型別參照落在哪個 import，複雜度遠超過目前規模需要）。真的遇到同名類別時，`call_graph.py` 的欄位型別解析會退化成「這個名字底下所有候選類別」，套用跟 interface 多實作一樣的 `@Qualifier`／`@Primary` 消歧與保守全連結邏輯（見三章），不會直接壞掉，只是精準度下降——留待接上真實專案規模評估是否需要升級。

```python
# parse_agent/types.py
"""① 解析 Agent 內部型別定義，對應 04a 三～六章。輸出面
（module_list／api_to_python_target）直接用 graph/state.py 既有的
ModuleInfo／MethodInfo／ApiMapping，不重新定義（見 04a 六章）；這裡只放
04b 處理過程內部使用、不跨模組交接的中間資料結構。04a 七章的模組清單
沒有點名這個檔案，是比照 03c 新增 postman_tree.py 的先例補上的共用型別檔。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# method_id 格式："{java_file_path}::{class_name}::{method_name}"（見 04a
# 三章步驟 4），避免同名方法跨類別衝突。多載（overload）方法不區分參數
# 簽名，同類別內的多載方法共用同一個 method_id——javalang 只做語法層解析，
# overload resolution 不在範圍內（見 04a 三章「決策」），呼叫圖對多載方法
# 的連結因此是保守的（連到「這個名字的所有多載」一併視為同一個目標），
# 不會錯誤地漏連，只是精準度略降。
MethodId = str


def method_id(file_path: str, class_name: str, method_name: str) -> MethodId:
    return f"{file_path}::{class_name}::{method_name}"


@dataclass
class FieldInfo:
    """class 內單一 field 宣告，供三章步驟 3 解析
    `this.xxxService.method()` 這類呼叫時，把 `xxxService` 對回它宣告的
    型別。`type_name`／`is_collection` 由 `call_graph._resolve_declared_
    type()` 決定（見三章 3.1）：`List`／`Set`／`Collection`／`Iterable`／
    `Optional` 這類單一型別參數容器會展開取內層型別（如
    `List<UserService>` 的 `type_name` 是 `"UserService"`、
    `is_collection=True`），因為 Spring 對這類容器是把所有符合型別的
    bean 一起注入（策略模式常見寫法），展開後才能正確接上依賴分析；其餘
    泛型（如 `Map<K, V>`、自訂多參數泛型）仍只取外層型別名稱，`type_name`
    解析不到專案內任何類別，落入三章「完全無法解析」分支，連結留白，不會
    誤判成排除依據（見 04a 三章「設計原則：多連、少排除」）。
    """

    name: str
    type_name: str
    qualifier_value: str | None = None  # @Qualifier("beanName") 的字面值——欄位自己的 annotation 優先，沒有則退回同名建構子參數上的 @Qualifier（見 call_graph.py _constructor_qualifier_hints()），兩者都沒有則 None
    is_collection: bool = False  # type_name 是否來自展開 List/Set/Collection/Iterable/Optional 容器


@dataclass
class MethodEntry:
    name: str
    return_type: str | None = None  # 回傳型別的外層名稱（void 或無法取得時為 None），供三章 3.3 鏈式呼叫接續解析用
    has_body: bool = True  # 是否有方法本體（javalang MethodDeclaration.body is not None）。interface 的抽象宣告／
    # Spring Data JPA 衍生查詢方法一律是 False——供 grouping.needs_llm_summary() 判斷「這個方法 LLM 讀到的
    # 資訊是否跟機械解析器完全一樣（都只有名稱可用）」，見該函式 docstring 規則 2。
    query_value: str | None = None  # @Query("...")／@Query(value="...") 的字面字串值；沒有這個 annotation 或
    # 引用非字面字串常量解析不出來時為 None，供 summarize._describe_bodyless_method() 優先抄錄用。


@dataclass
class ClassInfo:
    """單一 Java class（僅限具體類別，interface 不建立這個結構——interface
    沒有欄位/方法本體可摘要，也不是呼叫圖的節點來源，見 call_graph.py）
    的解析結果，三章掃描階段的基礎單位。
    """

    file_path: str
    class_name: str
    stereotype: str | None  # "RestController"/"Controller"/"Service"/"Component"/"Repository"/None
    bean_name_override: str | None  # 如 @Service("userService") 的字面 value；沒有明確指定時為 None
    implements: list[str] = field(default_factory=list)  # interface 簡單名稱清單
    is_primary: bool = False  # 是否標註 @Primary
    fields: list[FieldInfo] = field(default_factory=list)
    methods: list[MethodEntry] = field(default_factory=list)
    request_mapping_base: list[str] = field(default_factory=list)  # class 上 @RequestMapping 的 base path（可能多個），未標註為空清單
    routes: list["RouteDecl"] = field(default_factory=list)  # method 層級 route 宣告，定義見三章 3.4 RouteDecl
    imports: list[str] = field(default_factory=list)  # 這個檔案 import 的簡單類別名稱（非 wildcard、非 static），見三章 _extract_project_imports()——補足欄位/呼叫圖都解析不到的依賴（靜態呼叫、方法參考等），供四章 controller_dependency_closure() 使用
    annotations: list[str] = field(default_factory=list)  # class 上所有 annotation 名稱（不只 stereotype），供四章判斷是否為純資料類別（@Entity/@Data/@Getter/@Setter 等）
    uses_dynamic_query_signal: bool = False  # 這個檔案是否 import 了已知會承載動態查詢邏輯的型別（如 org.springframework.data.jpa.domain.Specification），見三章 _uses_dynamic_query_signal()


@dataclass
class ParsedProject:
    """三章掃描階段的完整輸出：呼叫圖 + Controller Route 索引 + class
    資訊，是四／五章共用的基礎資料（04a 三章開頭：「只建構一次」）。
    """

    classes: dict[str, ClassInfo]  # key 為 class 簡單名稱，見本節前言「已知限制」
    call_graph: dict[MethodId, set[MethodId]]  # 直接呼叫關係（三章步驟 4）
    route_index: dict[str, list[MethodId]]  # endpoint_key -> method_id 清單（三章步驟 5，可能多個見五章備註）
    project_root: str  # java_project_path，供 grouping.py／summarize.py 用 class_info.file_path（相對路徑，見上方）還原絕對路徑讀原始碼；04a／04b 未點名這個欄位，是實作時發現「file_path 只相對 java_project_path，不相對 cwd」的落差後補上的，不影響 file_path 本身在 method_id／輸出裡維持相對路徑的設計


@dataclass(frozen=True)
class MapMethodResult:
    method_name: str
    description: str
    complexity: str  # "low" / "medium" / "high"


@dataclass(frozen=True)
class MapClassResult:
    """Map 階段（四章）單一 class 的摘要結果，對應 04a 四章 Map 階段表格。"""

    class_name: str
    summary: str
    methods: list[MapMethodResult]
    cross_group_dependency_hints: list[str]  # 「看起來」被依賴/依賴到的其他 class 名稱，僅供 Reduce 參考，不下最終判斷
```

---

## 三、`call_graph.py`——javalang 掃描、呼叫圖建構、Controller Route 索引

對應 04a 三章全節。`parse_java_project()` 是這個檔案唯一的對外函式，回傳 `ParsedProject`，一次建構完成，四／五章共用。

### 3.1 掃描與 class 結構抽取（三章步驟 1～2）

```python
# parse_agent/call_graph.py
"""① 解析 Agent：javalang 掃描、呼叫圖建構、Controller Route 索引，
對應 04a 三章全節（機械分析，不用 LLM，見 04a 三章開頭「能用程式判斷的，
就不要交給 LLM」）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import javalang
import javalang.ast
import javalang.tree
import yaml

from parse_agent.types import ClassInfo, FieldInfo, MethodEntry, MethodId, ParsedProject, RouteDecl, method_id

logger = logging.getLogger(__name__)

_STEREOTYPES = {"RestController", "Controller", "Service", "Component", "Repository"}
_MAPPING_METHOD_BY_ANNOTATION = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}
# 單一型別參數的集合容器：Spring 對這類欄位是把所有符合型別的 bean 一起
# 注入（策略模式常見寫法），見 FieldInfo.is_collection 說明。Map<K, V>
# 不在這裡——雙型別參數時「哪個參數才是注入目標」沒有一致慣例可判斷。
_UNWRAPPABLE_CONTAINERS = {"List", "Set", "Collection", "Iterable", "Optional"}


def parse_java_project(java_project_path: str) -> ParsedProject:
    """對外唯一入口：掃描 `java_project_path` 下所有 `.java` 檔案，依序
    完成 04a 三章步驟 1～6，回傳 `ParsedProject`。任何檔案解析失敗
    （`javalang.parser.JavaSyntaxError`）直接往上拋，不吞掉、不跳過該
    檔案——見 04a 九章「呼叫圖建構失敗...直接往上拋，整條 LangGraph run
    中止」，解析失敗代表輸入端（Java 原始碼或 javalang 版本相容性）有
    問題，不是這個函式該自己決定要不要降級處理的事。
    """
    project_root = Path(java_project_path)
    java_files = sorted(project_root.rglob("*.java"))

    classes: dict[str, ClassInfo] = {}
    for file_path in java_files:
        source = file_path.read_text(encoding="utf-8")
        try:
            tree = javalang.parse.parse(source)
        except javalang.parser.JavaSyntaxError:
            # 原樣往上拋（bare raise），不包裝成新例外——javalang 的例外
            # 建構子簽名不保證接受單一自訂訊息字串，重新組一個新實例有
            # 弄壞例外物件本身的風險；記一行 log 補上檔案路徑脈絡即可，
            # 原始例外的型別／訊息／traceback 完整保留（見 04a 九章「直接
            # 往上拋」）。
            logger.error("%s: javalang 解析失敗，中止整條 parse run（見 04a 九章）", file_path)
            raise

        rel_path = str(file_path.relative_to(project_root)).replace("\\", "/")  # 統一用 / 分隔，跨平台一致（見 01 八章）
        # _extract_interfaces()（見下方）補上 interface 宣告——最常見的是
        # Spring Data JPA Repository（`interface XxxRepository extends
        # JpaRepository<...>`，沒有手寫實作類別，Spring 執行期動態產生
        # proxy），這類 interface 若不建 ClassInfo，_resolve_type_name_
        # to_classes() 對這個型別的欄位會直接回傳空清單（完全無法解析），
        # 連「留白」都談不上，Repository 這層永遠進不了依賴圖、Map 摘要、
        # module_list.java_files（見 04a 四章「Repository interface 的
        # 補充掃描」）。跟 _extract_classes() 共用同一套重複名稱偵測，
        # 不另外處理。
        # 這個檔案 import 的專案內類別名稱，補足欄位/呼叫圖都解析不到的
        # 依賴（見 _extract_project_imports() docstring）；同一檔案若有
        # 多個 top-level class（少見，見 04b 十一章已知限制），全部共用
        # 同一份 import 清單，沒有另外拆分的必要——import 陳述式本來就是
        # 整個檔案共用，不是個別 class 的屬性。
        file_imports = _extract_project_imports(tree)
        # 這個檔案有沒有 import 已知會承載動態查詢邏輯的型別（見
        # _uses_dynamic_query_signal() docstring），跟 file_imports 一樣
        # 整份檔案共用，供 grouping.py 判斷是否需要送 Map 摘要用。
        file_uses_dynamic_query_signal = _uses_dynamic_query_signal(tree)
        for class_info in [*_extract_classes(tree, rel_path), *_extract_interfaces(tree, rel_path)]:
            class_info.imports = file_imports
            class_info.uses_dynamic_query_signal = file_uses_dynamic_query_signal
            if class_info.class_name in classes:
                logger.warning(
                    "類別名稱在專案內重複，型別解析會退化成保守全連結（見 types.py "
                    "ParsedProject 說明）: %s（%s 與 %s）",
                    class_info.class_name,
                    classes[class_info.class_name].file_path,
                    class_info.file_path,
                )
            classes[class_info.class_name] = class_info

    interface_implementors = build_interface_implementors(classes)
    call_graph = _build_call_graph(java_files, project_root, classes, interface_implementors)
    context_path = _read_context_path(project_root)
    route_index = _build_route_index(classes, context_path)

    return ParsedProject(
        classes=classes, call_graph=call_graph, route_index=route_index, project_root=str(project_root)
    )


def _extract_project_imports(tree: javalang.tree.CompilationUnit) -> list[str]:
    """回傳這個檔案 import 的類別簡單名稱清單（不篩選是否真的是專案內
    類別，篩選交給呼叫端——跟 `_resolve_type_name_to_classes()` 的既有
    分工一致，這裡只負責抽取，不負責判斷有沒有對應到專案內的 class）。

    補的是**欄位依賴（`resolve_field_target_classes()`）跟呼叫圖
    （`_walk_and_resolve()`）都解析不到的依賴**：這兩者都只認得「透過
    DI 注入的欄位」或「`this.x.y()` 這種欄位鏈式呼叫」，對「靜態方法
    呼叫」（`ClassName.staticMethod()`）、「方法參考」（`ClassName::
    method`）、單純的型別參照（catch 特定例外類別、instanceof 判斷）
    這些不透過欄位建立關係的用法完全看不到——這類 class 因此永遠進不了
    `controller_dependency_closure()`，不會被 Map 摘要、也不會出現在
    `module_list.java_files`（見 04a 四章「Import 依賴補充」）。

    **不嘗試窮舉每一種 Java 呼叫語法去解決這個問題**（那需要呼叫圖建構
    逐一新增靜態呼叫、方法參考等分支，且永遠可能還有下一種沒覆蓋到的
    語法）；改用更通用、更不依賴語法細節的訊號：**只要一個 class 明確
    import 了另一個專案內的類別，兩者之間就有依賴關係**——不管這個依賴
    實際上是透過欄位、靜態呼叫、方法參考、還是任何其他方式建立的，
    import 陳述式本身就是最終、最不會漏掉的事實來源。

    `wildcard`（`import com.x.*`）／`static`（`import static
    com.x.Y.method`）import 不處理：wildcard 給不出具體類別名稱；
    static import 通常是引入單一方法或常數，這裡只在意 class 層級的
    依賴，兩者都回傳不到可用的簡單名稱，略過不強行猜測。
    """
    names: list[str] = []
    for imp in tree.imports or []:
        if imp.wildcard or imp.static:
            continue
        names.append(imp.path.rsplit(".", 1)[-1])
    return names


# 已知會承載動態／模糊查詢邏輯的型別（完整 package 路徑），供 grouping.py
# 判斷「這個類別要不要送 Map 摘要」時當作特殊情況：不管 method body 裡的
# 邏輯藏多深（例如包在 lambda 運算式裡），只要用到這個型別，就直接判定
# 「有業務邏輯」，不嘗試用「method body 有沒有控制流程」這種通用啟發式
# 去判斷——那類判斷法對這個型別的典型用法（`Specification<T>` 回傳一個
# lambda，實際過濾規則寫在 lambda 內部）會誤判成「沒有邏輯」，見四章
# needs_llm_summary()。是一份可擴充的清單，不是窮舉所有可能藏邏輯的
# 型別，之後接上新專案若發現其他同類型別，可以直接加進來。
_DYNAMIC_QUERY_SIGNAL_IMPORTS = {
    "org.springframework.data.jpa.domain.Specification",
}


def _uses_dynamic_query_signal(tree: javalang.tree.CompilationUnit) -> bool:
    """這個檔案是否 import 了 `_DYNAMIC_QUERY_SIGNAL_IMPORTS` 裡任何一個
    型別（含 wildcard import 涵蓋到的情況，如 `import org.springframework.
    data.jpa.domain.*;`）。
    """
    for imp in tree.imports or []:
        if imp.static:
            continue
        if imp.wildcard:
            if any(sig.startswith(imp.path + ".") for sig in _DYNAMIC_QUERY_SIGNAL_IMPORTS):
                return True
            continue
        if imp.path in _DYNAMIC_QUERY_SIGNAL_IMPORTS:
            return True
    return False


def _extract_classes(tree: javalang.tree.CompilationUnit, rel_path: str) -> list[ClassInfo]:
    """步驟 2：抽取單一檔案內每個具體 class 的 field／method／stereotype
    資訊。interface／enum／annotation 宣告不建立 ClassInfo（沒有欄位/方法
    本體，也不是呼叫圖節點來源），但仍會出現在 `implements` 清單裡當作
    型別名稱參照。

    **只掃 `tree.types`（頂層宣告），不用 `tree.filter(ClassDeclaration)`**
    ——後者是 javalang 內建的遞迴查詢，會把定義在另一個 class 內部的
    inner class 也各自抓成獨立 `ClassDeclaration` 節點。若改用 `filter`，
    inner class 會被建成一個跟外層 class 共用同一個 `file_path` 的獨立
    `ClassInfo`，導致 `summarize.py` 的 `_class_source_payload()` 把
    「同一份完整檔案原始碼」當成兩個不相干的 class 重複整份送進 Map
    階段——不只是多花 token，inner class 還會被誤判成獨立實體，產生自己
    的 summary，可能污染 Reduce 階段的模組拆分判斷。
    改用 `tree.types` 只列出頂層型別宣告，天生排除這個風險；代價見十一章
    「已知限制」。
    """
    result: list[ClassInfo] = []
    for class_decl in tree.types:
        if not isinstance(class_decl, javalang.tree.ClassDeclaration):
            continue
        stereotype, bean_name_override, is_primary = _stereotype_of(class_decl.annotations)

        # base_paths=None：沒有 @RequestMapping，或有但沒指定 value/path
        # ——合法情況，當作沒有 base path 前綴（見 3.4 _build_route_index()
        # 的 `base_paths = class_info.request_mapping_base or [""]`）。
        # base_paths=[]：@RequestMapping 有指定 value/path，但引用非字面
        # 字串常量解析不出來——這種情況下這個 class 底下所有 route 的完整
        # 路徑都無法確定，整批不索引（見 04a 三章步驟 5「不產生索引
        # 項目」），比只漏掉單一 route 更保守，但這裡連「哪一段路徑」都
        # 不知道，沒有能安全索引的部分。
        raw_base_paths = _annotation_values(class_decl.annotations, "RequestMapping")
        if raw_base_paths is not None and not raw_base_paths:
            logger.warning(
                "%s.%s 的 class 層級 @RequestMapping 引用了非字面字串常量，"
                "無法解析 base path，這個 Controller 底下所有 route 都不會"
                "被索引進 route_index（見 04a 三章步驟 5「不產生索引項目」）",
                rel_path,
                class_decl.name,
            )
            routes = []
        else:
            routes = _extract_routes(class_decl)  # 實作見 3.4「Controller Route 索引」

        result.append(
            ClassInfo(
                file_path=rel_path,
                class_name=class_decl.name,
                stereotype=stereotype,
                bean_name_override=bean_name_override,
                implements=[t.name for t in (class_decl.implements or [])],
                is_primary=is_primary,
                fields=_extract_fields(class_decl),
                methods=[
                    MethodEntry(
                        name=m.name,
                        return_type=m.return_type.name if m.return_type is not None else None,
                        has_body=m.body is not None,
                        query_value=_first_query_value(m.annotations),
                    )
                    for m in class_decl.methods
                ],
                request_mapping_base=raw_base_paths or [],
                routes=routes,
                annotations=[a.name for a in class_decl.annotations],
            )
        )
    return result


def _extract_interfaces(tree: javalang.tree.CompilationUnit, rel_path: str) -> list[ClassInfo]:
    """把 interface 宣告也建成 `ClassInfo`，解決 Spring Data JPA
    Repository（`interface XxxRepository extends JpaRepository<...>`，
    沒有手寫實作類別，Spring 在執行期動態產生 proxy）對
    `_resolve_type_name_to_classes()` 完全不可解析的問題——沒有這個函式，
    這類 interface 的欄位型別解析會直接落空（連候選都列不出來，不是
    「留白但保守全連結」），`controller_dependency_closure()` 因此永遠
    看不到這層依賴，Repository 進不了 Map 摘要、也不會出現在
    `module_list.java_files`（見 04a 四章「Repository interface 的補充
    掃描」）。

    **只在 `_resolve_type_name_to_classes()` 的 fallback 分支生效，不影響
    既有的「interface 有 `@Service`/`@Component`/`@Repository` 明確實作」
    路徑**：該函式優先查 `interface_implementors`（`build_interface_
    implementors()` 只收具體 stereotype 類別，這裡建的 interface 自身
    `ClassInfo` 幾乎必為 `stereotype=None`——Spring Data Repository 慣例
    上不標註任何 stereotype，靠 `extends JpaRepository` 讓 Spring 結構性
    識別，不會被 `build_interface_implementors()` 收錄），只有查不到任何
    候選實作時才退回 `classes.get(type_name)`，這裡建的條目才會被用到，
    等同「拿 interface 自己的宣告當它唯一已知的代表」——不完美（看不到
    Spring 動態產生的實際邏輯），但比「完全無法解析」更接近事實。已用
    合成範例驗證：interface 有 `@Service` 實作時，欄位解析仍正確指向
    具體實作類別，不會被 interface 自身的條目干擾。

    `implements=[]`：interface 用 `extends` 表達繼承，不是 `implements`，
    這裡不解析 `extends` 鏈——`JpaRepository` 是外部函式庫型別，解析了也
    對不上專案內任何 class。`fields=[]`：interface 沒有 instance field
    可供依賴解析用。`routes=[]`：interface 不會是 Controller。

    **不處理 `default`/`static` method body**（Java 8+ 允許 interface
    method 帶實作）——Spring Data Repository 極少使用這個寫法（實測
    `lang-exam-api-refactor` 全部方法 `body is None`），若真的遇到，這裡
    仍會把方法名稱納入 `methods`（供 `_yield_call()` 正確辨識成呼叫目標），
    只是不會追蹤這個方法自己的內部呼叫（`_build_call_graph()` 只走
    `ClassDeclaration`，不含 interface），維持既有「連結留白、預設保留」
    的安全方向，留待接上真的用到這個寫法的專案再評估是否需要擴充。
    """
    result: list[ClassInfo] = []
    for decl in tree.types:
        if not isinstance(decl, javalang.tree.InterfaceDeclaration):
            continue
        stereotype, bean_name_override, is_primary = _stereotype_of(decl.annotations)
        result.append(
            ClassInfo(
                file_path=rel_path,
                class_name=decl.name,
                stereotype=stereotype,
                bean_name_override=bean_name_override,
                implements=[],
                is_primary=is_primary,
                fields=[],
                methods=[
                    MethodEntry(
                        name=m.name,
                        return_type=m.return_type.name if m.return_type is not None else None,
                        has_body=m.body is not None,
                        query_value=_first_query_value(m.annotations),
                    )
                    for m in decl.methods
                ],
                request_mapping_base=[],
                routes=[],
                annotations=[a.name for a in decl.annotations],
            )
        )
    return result


def _resolve_declared_type(java_type) -> tuple[str, bool]:
    """回傳 (實際用來解析依賴的型別名稱, 是否為集合式包裝)。

    javalang 的 `ReferenceType` 把型別名稱（`.name`）跟泛型參數
    （`.arguments`，每個元素是 `TypeArgument`，`.type` 才是實際型別節點）
    分開存放，`.name` 本身不含 `<...>`——`List<UserService>` 的 `.name`
    就是 `"List"`，泛型參數要另外從 `.arguments` 取。這是這個函式存在的
    理由：`_UNWRAPPABLE_CONTAINERS` 命中時解開一層取內層型別的 `.name`
    （見 FieldInfo.is_collection 說明），沒命中或沒有 `.arguments` 時
    直接用 `.name`（`Map<K, V>`、無泛型的一般型別都落在這裡）。
    """
    arguments = getattr(java_type, "arguments", None)
    if java_type.name in _UNWRAPPABLE_CONTAINERS and arguments:
        inner_type = getattr(arguments[0], "type", None)
        inner_name = getattr(inner_type, "name", None) if inner_type is not None else None
        if inner_name:
            return inner_name, True
    return java_type.name, False


def _constructor_qualifier_hints(class_decl: javalang.tree.ClassDeclaration) -> dict[str, str]:
    """對應 04a 三章步驟 3 第二點「先比對注入點（field／建構子參數）上的
    @Qualifier」——欄位本身的 @Qualifier 由下方 `_extract_fields()` 直接
    處理，這裡另外掃建構子參數上的 @Qualifier，依參數名稱建索引。現代
    Spring 專案常見建構子注入、欄位本身不加任何 annotation 的寫法（如
    `private final PaymentService paymentService;` 配 `public
    OrderService(@Qualifier("x") PaymentService paymentService) { this.
    paymentService = paymentService; }`），若不掃建構子參數，這種寫法下
    `qualifier_value` 永遠是 `None`，@Qualifier 消歧形同失效。

    依「參數名稱＝欄位名稱」的 Java／Spring 慣例（手動建構子注入或
    Lombok `@RequiredArgsConstructor` 都遵循這個慣例）比對，不嘗試比對
    賦值語句（`this.x = x`）本體去確認真的有指派給同名欄位——多一層驗證
    的邊際效益低，多建構子時也只是取第一個掃到的同名參數，不判斷哪個
    建構子才是 Spring 實際 autowire 的那個（引數/建構子消歧不在 javalang
    語法層解析範圍內，見三章「決策」）。
    """
    hints: dict[str, str] = {}
    for ctor in class_decl.constructors:
        for param in ctor.parameters:
            values = _annotation_values(param.annotations, "Qualifier")
            if values and param.name not in hints:
                hints[param.name] = values[0]
    return hints


def _extract_fields(class_decl: javalang.tree.ClassDeclaration) -> list[FieldInfo]:
    constructor_hints = _constructor_qualifier_hints(class_decl)
    fields: list[FieldInfo] = []
    for field_decl in class_decl.fields:
        type_name, is_collection = _resolve_declared_type(field_decl.type)
        qualifier_values = _annotation_values(field_decl.annotations, "Qualifier")
        field_level_qualifier = qualifier_values[0] if qualifier_values else None
        for declarator in field_decl.declarators:
            # 欄位自己的 @Qualifier 優先；沒有才退回同名建構子參數上的
            # @Qualifier（見 _constructor_qualifier_hints()）。
            qualifier_value = field_level_qualifier or constructor_hints.get(declarator.name)
            fields.append(
                FieldInfo(
                    name=declarator.name,
                    type_name=type_name,
                    qualifier_value=qualifier_value,
                    is_collection=is_collection,
                )
            )
    return fields


def _stereotype_of(annotations: list) -> tuple[str | None, str | None, bool]:
    """回傳 (stereotype, bean_name_override, is_primary)。stereotype 取
    `_STEREOTYPES` 裡第一個命中的（一個 class 正常只會標一種），
    `bean_name_override` 是 stereotype annotation 本身的字面 value（如
    `@Service("userService")`／`@Service(value="userService")` 兩種寫法
    皆可，用 `_annotation_named_value()` 統一處理，不要直接呼叫
    `_element_to_strings(ann.element)`——後者只認得單一 element 值，
    named 參數形式（`ann.element` 是 `ElementValuePair` 清單）會被它
    誤判成沒有值，靜默漏抓 bean name），沒有明確指定時為 None——呼叫端
    需要 Spring 預設命名規則時另外算（見 `_default_bean_name()`），不在
    這裡快取，避免跟「使用者明確指定」的語意混在一起。
    """
    stereotype = None
    bean_name_override = None
    is_primary = False
    for ann in annotations:
        if ann.name == "Primary":
            is_primary = True
        elif ann.name in _STEREOTYPES and stereotype is None:
            stereotype = ann.name
            values = _annotation_named_value(ann, "value")
            bean_name_override = values[0] if values else None
    return stereotype, bean_name_override, is_primary


def _annotation_values(annotations: list, name: str, key: str = "value") -> list[str] | None:
    """在 annotation 清單裡找出 `name`（如 `"RequestMapping"`）對應的
    `key`（預設 `"value"`，找不到再退回 `"path"`——`@RequestMapping` 兩者
    等義，見 04a 三章步驟 5）字面字串值。

    **回傳 `None` 跟回傳 `[]` 意義不同，呼叫端不能混用**：`None` 代表
    `name` 這個 annotation 根本不存在，或存在但沒有指定 `key` 這個屬性
    ——合法的「沒寫」，呼叫端可以套用預設值（如當作根路徑）；`[]` 代表
    屬性有指定，但引用非字面字串常量（`static final` 常數、SpEL 表達式，
    見 04a 十章）解析不出來——呼叫端**不能**對 `[]` 套用「沒寫」的預設值，
    04a 三章步驟 5 明確要求這種情況「不產生索引項目」，兩者混用會把「無法
    解析」誤當成「沒有路徑」，靜默索引出一個錯誤的空字串路徑。
    """
    for ann in annotations:
        if ann.name != name:
            continue
        values = _annotation_named_value(ann, key)
        if values is None and key == "value":
            values = _annotation_named_value(ann, "path")
        return values
    return None


def _annotation_named_value(annotation, name: str) -> list[str] | None:
    """同一套 `None`／`[]` 語意（見 `_annotation_values()` docstring）：
    `None` 是「沒指定這個屬性」，`[]` 是「指定了但解析不出字面字串」。
    """
    element = annotation.element
    if element is None:
        return None
    if isinstance(element, list):  # 具名參數：@RequestMapping(value=..., method=...)
        for pair in element:
            if pair.name == name:
                return _element_to_strings(pair.value)
        return None
    if name == "value":  # 簡寫語法：@RequestMapping("/foo") 等同 value="/foo"
        return _element_to_strings(element)
    return None


def _element_to_strings(element) -> list[str]:
    """把 annotation element 值（Literal／ElementArrayValue）轉成字串
    清單。只處理字面字串常量；引用 static final 常數或 SpEL 表達式
    （MemberReference 等其他節點型別）無法解析，回傳空清單，不強行猜
    （javalang 對這類寫法能否可靠解析，實測結果見 04a 十章）。
    """
    if element is None:
        return []
    if isinstance(element, javalang.tree.ElementArrayValue):
        values: list[str] = []
        for v in element.values:
            values.extend(_element_to_strings(v))
        return values
    if isinstance(element, javalang.tree.Literal) and isinstance(element.value, str):
        raw = element.value
        if raw.startswith('"') and raw.endswith('"'):
            return [raw[1:-1]]
    return []


def _first_query_value(annotations: list) -> str | None:
    """`@Query("...")`／`@Query(value="...")` 的字面字串值，供
    `MethodEntry.query_value`（見該欄位 docstring）。重用
    `_annotation_values()` 同一套 `None`／`[]` 語意——這裡不需要區分
    「沒有 @Query」跟「有但解析不出字面值」，兩種情況呼叫端都是「沒有
    可抄錄的查詢字串」，統一收斂成 `None`。
    """
    values = _annotation_values(annotations, "Query")
    return values[0] if values else None
```

### 3.2 欄位型別解析：`@Qualifier`／`@Primary` 消歧（三章步驟 3 第二點）

```python
# parse_agent/call_graph.py（續）

def build_interface_implementors(classes: dict[str, ClassInfo]) -> dict[str, list[ClassInfo]]:
    """回傳 interface 簡單名稱 -> 實作類別清單。只收 `@Service`／
    `@Component`／`@Repository` 標註的類別（見 04a 三章：「呼叫目標是
    interface，且有多個 @Service/@Component/@Repository 實作類別」），
    沒有標註 stereotype 的類別即使 implements 了同一個 interface 也不
    收錄——那種情況通常不是 Spring 管理的 bean，不會出現在依賴注入的
    消歧情境裡。
    """
    result: dict[str, list[ClassInfo]] = {}
    for class_info in classes.values():
        if class_info.stereotype not in ("Service", "Component", "Repository"):
            continue
        for interface_name in class_info.implements:
            result.setdefault(interface_name, []).append(class_info)
    return result


def _default_bean_name(class_name: str) -> str:
    """Spring 預設 bean 命名規則：class 名稱首字母小寫（如
    `UserServiceImpl` -> `userServiceImpl`），沒有 `@Service("customName")`
    這類明確指定時套用。
    """
    return class_name[0].lower() + class_name[1:] if class_name else class_name


def _bean_name(class_info: ClassInfo) -> str:
    return class_info.bean_name_override or _default_bean_name(class_info.class_name)


def _resolve_type_name_to_classes(
    type_name: str, classes: dict[str, ClassInfo], interface_implementors: dict[str, list[ClassInfo]]
) -> list[ClassInfo]:
    """把一個型別名稱解析成專案內的候選類別清單，不含 field 專屬的
    `@Qualifier`／`@Primary`／集合注入判斷（那些是 `resolve_field_target_
    classes()` 疊加在這個核心邏輯上的欄位限定行為）。回傳：

    1. `type_name` 是 interface 且有登記實作 -> 全部實作類別（消歧交給
       呼叫端）
    2. `type_name` 直接命中專案內某個具體類別 -> 該類別本身
    3. 都沒有命中（外部函式庫型別、DTO、原生型別等）-> 空清單，代表
       「完全無法解析」（見 04a 三章「設計原則：多連、少排除」）

    這個核心邏輯同時也是三章 3.3 鏈式呼叫接續（方法回傳型別解析）的
    共用基礎——回傳型別沒有 `@Qualifier`／`@Primary` 這類 annotation
    可用，直接復用「找不到就代表無法解析」這個最基本的判斷。
    """
    implementors = interface_implementors.get(type_name)
    if implementors:
        return implementors
    direct = classes.get(type_name)
    return [direct] if direct is not None else []


def resolve_field_target_classes(
    field: FieldInfo, classes: dict[str, ClassInfo], interface_implementors: dict[str, list[ClassInfo]]
) -> list[ClassInfo]:
    """對應 04a 三章步驟 3 第二點的完整消歧流程，回傳這個 field 最終解析
    到的候選類別清單（正常情況下長度為 0 或 1，多實作消歧失敗、或欄位
    本身是集合式包裝時保守回傳全部候選）。
    """
    candidates = _resolve_type_name_to_classes(field.type_name, classes, interface_implementors)
    if len(candidates) <= 1:
        return candidates
    if field.is_collection:
        # List<T>／Set<T>／Optional<T> 這類集合式包裝：Spring 會把符合
        # 型別的所有 bean 一起注入（策略模式常見寫法），語意上本來就是
        # 「連到全部」，@Qualifier/@Primary 對集合注入不適用，不需要
        # （也不應該）走下面的單一消歧流程。
        return candidates
    if field.qualifier_value is not None:
        qualifier_matches = [c for c in candidates if _bean_name(c) == field.qualifier_value]
        if len(qualifier_matches) == 1:
            return qualifier_matches
    primaries = [c for c in candidates if c.is_primary]
    if len(primaries) == 1:
        return primaries
    logger.info(
        "欄位型別 %s 有 %d 個實作，@Qualifier/@Primary 無法唯一消歧，"
        "保守全連結（見 04a 三章步驟 3 第二點）: %s",
        field.type_name,
        len(candidates),
        [c.class_name for c in candidates],
    )
    return candidates
```

### 3.3 呼叫圖建構（三章步驟 3～4）

**設計核心：追蹤方法的宣告回傳型別，接續解析下一段鏈式呼叫**，而不是遇到鏈式呼叫（如 `xxxService.getDetail().calculate()`）裡的方法呼叫就直接放棄解析剩餘部分。理由是安全性，不是精準度：若方法 M 同時被 skip 端點直接呼叫、也被非-skip 端點透過這種鏈式呼叫呼叫到，一旦鏈式的部分解析不出來，M 會出現在 skip 可達集合、但漏出非-skip 可達集合，導致五章的排除邏輯把 M 誤判為只服務 skip、進而錯誤排除——這正是 04a 三章「漏掉一個實際存在的連結...是不可逆的錯誤」要防的那一類情況。做法：`MethodEntry` 記錄 `return_type`（見 3.1），呼叫 `xxxService.getDetail()` 時若能把 `getDetail()` 的回傳型別解析到專案內的類別，就把該類別當成下一段 `.calculate()` 的解析基礎。

javalang 對「純欄位存取的鏈（沒有被方法呼叫打斷）」常會把它折成一個點號字串塞進 `qualifier`（如 `xxxService`、`this.xxxService`）；若這段鏈後面還接著一次欄位存取才呼叫方法（如 `a.b.c()`），折疊只到「呼叫前的最後一步」為止——`MethodInvocation` 的 `qualifier="a.b"`、`member="c"`；若是 `a.b.c.method()` 這種「存取到 c 之後還要再存取一層才呼叫」，則是 `MemberReference` 的 `qualifier="a.b"`、`member="c"`，`method()` 才是掛在它 `.selectors` 上的下一步。不是每一段都拆成獨立節點；只有鏈中真的出現方法呼叫，才會從那個呼叫點開始展開成巢狀的 `.selectors`。因此解析邏輯分兩層：`_resolve_qualifier_string()` 逐段解析 qualifier 字串本身，`_continue_chain()` 接續解析 `.selectors` 裡巢狀掛著的後續呼叫。

> **未驗證假設**：javalang 是否真的一律用「qualifier 字串折疊」表示純欄位存取鏈、只在遇到方法呼叫時才展開 `.selectors`，以及 `this.x.y()` 的 `this` 是否一定會出現在 qualifier 字串最前面，都還沒有真實 Java 專案跑過驗證，是本節演算法目前最大的不確定性，見十一章。

```python
# parse_agent/call_graph.py（續）

def _resolve_member_context(
    member_name: str,
    context: list[ClassInfo],
    classes: dict[str, ClassInfo],
    interface_implementors: dict[str, list[ClassInfo]],
) -> list[ClassInfo] | None:
    """在 context 內每個候選類別找同名 field，把各自解析到的目標類別
    （field 專屬的 is_collection／@Qualifier／@Primary 消歧對每個候選各自
    套用，見 3.2）彙整起來，依 class_name 去重。一個候選都解析不到就
    回傳 None（完全無法解析）。
    """
    targets: list[ClassInfo] = []
    seen: set[str] = set()
    for cls in context:
        field = next((f for f in cls.fields if f.name == member_name), None)
        if field is None:
            continue
        for t in resolve_field_target_classes(field, classes, interface_implementors):
            if t.class_name not in seen:
                seen.add(t.class_name)
                targets.append(t)
    return targets or None


def _resolve_qualifier_string(
    qualifier: str,
    current_class: ClassInfo,
    classes: dict[str, ClassInfo],
    interface_implementors: dict[str, list[ClassInfo]],
) -> list[ClassInfo] | None:
    """把 qualifier 字串（可能是單一 field 名稱，也可能是 javalang 折疊
    出的點號路徑，見本節前言）逐段解析：開頭的 `"this"` 視為
    `current_class` 本身，其餘每一段都當作「目前候選類別清單裡任一個的
    field 名稱」依序解析下去，任何一段解析不到就回傳 None。
    """
    segments = qualifier.split(".")
    context = [current_class]
    if segments[0] == "this":
        segments = segments[1:]
    for seg in segments:
        if not seg:
            return None
        context = _resolve_member_context(seg, context, classes, interface_implementors)
        if context is None:
            return None
    return context


def _method_return_context(
    member_name: str,
    context: list[ClassInfo],
    classes: dict[str, ClassInfo],
    interface_implementors: dict[str, list[ClassInfo]],
) -> list[ClassInfo] | None:
    """呼叫 context 內任一候選類別的 member_name 方法後，把回傳型別解析
    到的類別當成鏈式呼叫下一段的接續基礎（見本節前言）。

    **必須遍歷 context 內每個候選類別的每一個同名多載方法，聯集所有能
    解析出來的回傳型別，不能只取第一個成功的就回傳**——04a 三章「設計
    原則：多連、少排除」明講漏掉一個實際存在的連結是不可逆的錯誤，這裡
    有兩種情況會漏連：(1) 同一個 class 裡 `member_name` 有多個多載方法
    （見 `types.py` `MethodId` 的多載共用 method_id 說明），若只取第一個
    宣告的多載、剛好回傳 `void` 或無法解析，會讓後面明明可以解析的多載
    被忽略；(2) `context` 裡有多個介面實作類別時，不同實作的回傳型別
    未必相同（例如 covariant return type），只取第一個成功的候選、放棄
    其餘候選，會讓依賴其他實作回傳型別的後續呼叫鏈整條斷掉。這裡不去猜
    哪個多載或哪個實作才是「真正」被呼叫的那個（引數型別推導不在 javalang
    語法層解析範圍內，見三章「決策」），而是比照 `resolve_field_target_
    classes()` 消歧失敗時的「保守全連結」精神，把所有解析得到的候選都
    留著。
    """
    resolved: list[ClassInfo] = []
    seen: set[str] = set()
    for cls in context:
        for method in cls.methods:
            if method.name != member_name or method.return_type is None:
                continue
            for target in _resolve_type_name_to_classes(method.return_type, classes, interface_implementors):
                if target.class_name not in seen:
                    seen.add(target.class_name)
                    resolved.append(target)
    return resolved or None


def _yield_call(member_name: str, context: list[ClassInfo]):
    for cls in context:
        if any(m.name == member_name for m in cls.methods):
            yield method_id(cls.file_path, cls.class_name, member_name)


def _continue_chain(
    selectors: list,
    context: list[ClassInfo] | None,
    current_class: ClassInfo,
    classes: dict[str, ClassInfo],
    interface_implementors: dict[str, list[ClassInfo]],
):
    """接續走訪一段 `.selectors` 鏈。`context` 是目前解析到的候選類別
    （`None` 代表前一段已經無法解析，這段鏈剩下的部分全部視為無法解析，
    不強行接續，對應 04a 三章「完全無法解析」）。選擇器層級的
    `MemberReference`／`MethodInvocation` 若自己還帶 `qualifier`（javalang
    通常不會這樣產生，是選擇器層級的罕見情況），不強行模擬，直接視為
    無法解析（見十一章）。
    """
    for sel in selectors:
        if isinstance(sel, javalang.tree.MemberReference):
            next_context = (
                _resolve_member_context(sel.member, context, classes, interface_implementors)
                if context and not sel.qualifier
                else None
            )
            yield from _continue_chain(sel.selectors or [], next_context, current_class, classes, interface_implementors)
        elif isinstance(sel, javalang.tree.MethodInvocation):
            if context and not sel.qualifier:
                yield from _yield_call(sel.member, context)
                next_context = _method_return_context(sel.member, context, classes, interface_implementors)
            else:
                next_context = None
            for arg in sel.arguments or []:
                yield from _walk_and_resolve(arg, current_class, classes, interface_implementors)
            yield from _continue_chain(sel.selectors or [], next_context, current_class, classes, interface_implementors)
        else:
            yield from _walk_and_resolve(sel, current_class, classes, interface_implementors)


def _walk_and_resolve(
    node,
    current_class: ClassInfo,
    classes: dict[str, ClassInfo],
    interface_implementors: dict[str, list[ClassInfo]],
):
    """通用遞迴：找到呼叫鏈起點（`This`／`MethodInvocation`／
    `MemberReference`）時解析並接續鏈式呼叫，其餘節點用 javalang 內建的
    `attrs` 機制展開所有子屬性繼續找。
    """
    if node is None:
        return
    if isinstance(node, javalang.tree.This):
        yield from _continue_chain(node.selectors or [], [current_class], current_class, classes, interface_implementors)
        return
    if isinstance(node, javalang.tree.MethodInvocation):
        qualifier = node.qualifier or None
        context = (
            _resolve_qualifier_string(qualifier, current_class, classes, interface_implementors)
            if qualifier
            else [current_class]  # 沒有 qualifier：可能是 self-call（隱含 this）
        )
        if context:
            yield from _yield_call(node.member, context)
            next_context = _method_return_context(node.member, context, classes, interface_implementors)
        else:
            next_context = None
        for arg in node.arguments or []:
            yield from _walk_and_resolve(arg, current_class, classes, interface_implementors)
        yield from _continue_chain(node.selectors or [], next_context, current_class, classes, interface_implementors)
        return
    if isinstance(node, javalang.tree.MemberReference):
        # MemberReference 的 qualifier 是「member 之前」的路徑，跟
        # MethodInvocation 不同——member 本身還要再當一步 field 解析
        # （見本節前言 a.b.c 的例子：qualifier="a.b"、member="c"）。
        prefix_context = (
            _resolve_qualifier_string(node.qualifier, current_class, classes, interface_implementors)
            if node.qualifier
            else [current_class]
        )
        context = (
            _resolve_member_context(node.member, prefix_context, classes, interface_implementors)
            if prefix_context
            else None
        )
        yield from _continue_chain(node.selectors or [], context, current_class, classes, interface_implementors)
        return
    if isinstance(node, javalang.ast.Node):
        for attr_name in node.attrs:
            yield from _walk_and_resolve(getattr(node, attr_name, None), current_class, classes, interface_implementors)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk_and_resolve(item, current_class, classes, interface_implementors)
        return
    # 純量值（字串、數字、None、bool）：沒有子節點，遞迴到此結束


def _iter_method_invocation_targets(
    method_decl: javalang.tree.MethodDeclaration,
    current_class: ClassInfo,
    classes: dict[str, ClassInfo],
    interface_implementors: dict[str, list[ClassInfo]],
):
    """對外入口：走訪單一 method body 內所有呼叫鏈，直接 yield 已解析的
    呼叫目標 method_id。interface／abstract method 沒有 body（`method_
    decl.body is None`）時直接回傳空。
    """
    if method_decl.body is None:
        return
    for statement in method_decl.body:
        yield from _walk_and_resolve(statement, current_class, classes, interface_implementors)


def _build_call_graph(
    java_files: list[Path],
    project_root: Path,
    classes: dict[str, ClassInfo],
    interface_implementors: dict[str, list[ClassInfo]],
) -> dict[MethodId, set[MethodId]]:
    """步驟 3～4：對每個具體 class 的每個 method，走訪 body 內的呼叫鏈，
    解析呼叫目標，組出 `method_id -> 直接呼叫的 method_id 清單`。需要
    重新解析檔案（`parse_java_project()` 步驟 1 的 AST 沒有保留下來，
    `ClassInfo` 只存結構化摘要，不保留原始 AST 節點）——javalang 解析
    速度足夠快（04a 三章驗證的 90 個檔案量級），重掃一次的成本可以接受，
    換取 `types.py` 的 `ClassInfo` 保持乾淨、不用背著 AST 節點到處傳。
    """
    graph: dict[MethodId, set[MethodId]] = {}
    for file_path in java_files:
        source = file_path.read_text(encoding="utf-8")
        tree = javalang.parse.parse(source)  # 步驟 1 已驗證過語法，這裡不會再拋 JavaSyntaxError
        rel_path = str(file_path.relative_to(project_root)).replace("\\", "/")

        for class_decl in tree.types:
            if not isinstance(class_decl, javalang.tree.ClassDeclaration):
                continue
            # 只掃頂層宣告，理由同 3.1 _extract_classes()：避免用會遞迴的
            # tree.filter() 把 inner class 也各自抓成獨立節點。
            current_class = classes.get(class_decl.name)
            if current_class is None or current_class.file_path != rel_path:
                continue  # 理論上不會發生（_extract_classes 用同一份掃描結果建立），防禦性檢查
            for method_decl in class_decl.methods:
                caller_id = method_id(rel_path, class_decl.name, method_decl.name)
                callees = graph.setdefault(caller_id, set())
                callees.update(
                    _iter_method_invocation_targets(method_decl, current_class, classes, interface_implementors)
                )
    return graph
```

### 3.4 Controller Route 索引與 Context-Path 校正（三章步驟 5～6）

Route 資訊（HTTP method／path）在 3.1 掃描階段就跟著 `ClassInfo` 一併抽出（`RouteDecl`，見下方），`_build_route_index()` 只需要純字串組裝，不必為了補抽 route 資訊而重新讀檔解析一次 AST。`types.py` 補上這個中繼資料型別，`ClassInfo` 新增對應欄位：

```python
# parse_agent/types.py（補充）
@dataclass
class RouteDecl:
    """單一 method 上的 route 宣告，三章步驟 5 的中繼資料。"""

    method_name: str
    http_method: str
    paths: list[str]  # 該 method 上的 path（可能多個），還沒接 base path／context-path


# ClassInfo 的欄位定義（二章）補上這行：
#     routes: list[RouteDecl] = field(default_factory=list)
```

3.1 的 `_extract_classes()` 在建立每個 `ClassInfo` 時，多帶一個 `routes=_extract_routes(class_decl)`：

```python
# parse_agent/call_graph.py（續，補進 3.1 _extract_classes() 的抽取邏輯）

def _extract_routes(class_decl: javalang.tree.ClassDeclaration) -> list[RouteDecl]:
    routes: list[RouteDecl] = []
    for method_decl in class_decl.methods:
        for ann in method_decl.annotations:
            if ann.name in _MAPPING_METHOD_BY_ANNOTATION:
                paths = _method_mapping_paths(ann, class_decl.name, method_decl.name)
                if paths is None:
                    continue
                routes.append(RouteDecl(method_decl.name, _MAPPING_METHOD_BY_ANNOTATION[ann.name], paths))
            elif ann.name == "RequestMapping":
                http_methods = _extract_request_mapping_methods(ann)
                paths = _method_mapping_paths(ann, class_decl.name, method_decl.name)
                if paths is None:
                    continue
                for http_method in http_methods:
                    routes.append(RouteDecl(method_decl.name, http_method, paths))
    return routes


def _method_mapping_paths(annotation, class_name: str, method_name: str) -> list[str] | None:
    """對應 04a 三章步驟 5：解析單一 route annotation 的 value／path。
    回傳 `None` 代表這個 annotation 引用了非字面字串常量、解析不出來，
    呼叫端要直接跳過、不產生這筆 `RouteDecl`——不能落到 `[""]`「當作根
    路徑」的 fallback，那等於把「不知道路徑是什麼」誤索引成「路徑是空字
    串」，違反 04a 三章步驟 5「不產生索引項目」的要求。回傳 `[""]` 是
    另一種合法情況：annotation 真的沒寫 value/path（如裸 `@GetMapping`），
    等同對應到 base path 本身。
    """
    values = _annotation_named_value(annotation, "value")
    if values is None:
        values = _annotation_named_value(annotation, "path")
    if values is None:
        return [""]
    if not values:
        logger.warning(
            "%s.%s 的 @%s 引用了非字面字串常量，無法解析 path，這個 route "
            "不會被索引進 route_index（見 04a 三章步驟 5「不產生索引項目」）",
            class_name,
            method_name,
            annotation.name,
        )
        return None
    return values


def _extract_request_mapping_methods(annotation) -> list[str]:
    """`@RequestMapping(method = RequestMethod.GET)` 這種寫法，`method`
    屬性值是 `MemberReference`（如 `RequestMethod.GET`），不是字面字串，
    `_element_to_strings()` 解析不出來（見 04a 十章）。這裡另外處理：
    直接取 MemberReference 的 `.member`（`"GET"`）當 HTTP method 名稱。
    沒有 `method` 屬性（`@RequestMapping` 不限定方法）的情況目前不索引
    任何 HTTP method，記警告——這種寫法在 REST controller 較罕見，實際
    觸發頻率留待接上真實專案驗證。
    """
    element = annotation.element
    pairs = element if isinstance(element, list) else []
    for pair in pairs:
        if pair.name != "method":
            continue
        values = pair.value.values if isinstance(pair.value, javalang.tree.ElementArrayValue) else [pair.value]
        methods = [v.member for v in values if isinstance(v, javalang.tree.MemberReference)]
        if methods:
            return methods
    logger.warning(
        "@RequestMapping 沒有指定 method（或用了非 MemberReference 的寫法），"
        "略過，不索引任何 HTTP method: %s",
        annotation.name,
    )
    return []
```

`_build_route_index()` 純粹依 `ClassInfo.routes` 組字串，不讀檔：

```python
# parse_agent/call_graph.py（續）

def _build_route_index(classes: dict[str, ClassInfo], context_path: str) -> dict[str, list[MethodId]]:
    """步驟 5：對每個 `@RestController`／`@Controller` 組出
    `endpoint_key -> method_id 清單` 索引；步驟 6：所有 endpoint_key
    統一加上 `context_path` 前綴。`endpoint_key` 格式沿用
    `unfilled_endpoints.json` 的 `endpoint` 欄位（`{HTTP_METHOD} {path}`），
    見 04a 三章步驟 5——不套用 02a `route_to_file_mapping` 的動態段
    normalize 規則，這裡兩邊比對的都是路徑樣板。
    """
    index: dict[str, list[MethodId]] = {}
    for class_info in classes.values():
        if class_info.stereotype not in ("RestController", "Controller"):
            continue
        base_paths = class_info.request_mapping_base or [""]

        for route in class_info.routes:
            method_entry_id = method_id(class_info.file_path, class_info.class_name, route.method_name)
            for base in base_paths:
                for method_path in route.paths:
                    full_path = context_path + _join_path(base, method_path)
                    endpoint_key = f"{route.http_method} {full_path}"
                    index.setdefault(endpoint_key, []).append(method_entry_id)
    return index


def _join_path(base: str, suffix: str) -> str:
    joined = f"{base.rstrip('/')}/{suffix.lstrip('/')}" if suffix else base
    return joined if joined.startswith("/") else f"/{joined}"
```

**Context-Path 讀取**（步驟 6）——含 `${ENV_VAR:default}` 佔位符處理：

企業專案的 `application.properties`／`.yml` 常把 `context-path` 寫成 `${SERVER_CONTEXT_PATH:/api}` 這種可被環境變數覆蓋的佔位符，而不是寫死字面路徑。如果直接把這個字面字串當前綴，`route_index` 的**每一個** endpoint_key 都會帶著這段垃圾字串，導致跟 `unfilled_endpoints.json` 的比對全面查無對應——不是「少數幾筆」的問題，是整個 skip 排除機制連帶 `api_to_python_target` 組裝一起靜默失效。因此讀出原始值後，一律先過 `_resolve_placeholder()`：有預設值（冒號後半段）就用預設值當最佳猜測（畢竟這裡是純靜態原始碼分析，讀不到執行期真正注入的環境變數值），沒有預設值就視為沒有前綴，兩種情況都記警告，讓後續 skip 比對若真的查無對應時，有跡可循。

```python
# parse_agent/call_graph.py（續）

_PLACEHOLDER_RE = re.compile(r"^\$\{([^:}]+)(?::(.*))?\}$")


def _resolve_placeholder(raw_value: str) -> str:
    """解析 Spring 的 `${ENV_VAR:default}` 佔位符寫法。這裡做的是純靜態
    原始碼分析，不是真的啟動 Java 服務，讀不到執行期真正注入的環境變數
    值：有預設值（冒號後半段）就用預設值當最佳猜測，沒有預設值就沒有
    任何依據可用，回傳空字串（等同沒有前綴，比照 04a 三章步驟 6「找不到
    主設定檔」的 fallback），三種情況都記警告——實際部署若真的用環境變數
    覆蓋了這個值，後續 skip 比對查無對應的 warning log 是唯一能發現這件
    事的地方。

    **巢狀佔位符（`${SERVER_PATH:${DEFAULT_PATH:/api}}`）需要另外判斷**：
    `_PLACEHOLDER_RE` 只解一層，正則表達式本身對這種輸入**不會解析失敗
    ——`group(2)`（預設值）會抓到 `"${DEFAULT_PATH:/api}"` 這個還沒被
    解開的巢狀佔位符字面字串，直接當成解析結果回傳。若不特別檢查，這個
    看起來「有值」的字串會被當成合法 context-path 前綴，讓 `route_index`
    每一個 endpoint_key 都帶上一段 `${DEFAULT_PATH:/api}` 垃圾字串，後果
    跟「完全沒讀到前綴」一樣糟（大量查無對應），卻不會觸發任何警告，比
    真的沒有前綴更難察覺。因此 `default` 裡如果還含有 `${`，視同「無法
    解析」，不當成最佳猜測使用。
    """
    match = _PLACEHOLDER_RE.match(raw_value.strip())
    if match is None:
        return raw_value

    default = match.group(2)
    if default is not None and "${" not in default:
        logger.warning(
            "server.servlet.context-path 使用環境變數佔位符 %s，靜態分析讀不到"
            "執行期實際注入值，改用預設值 %r 當最佳猜測；若實際部署時有覆蓋這個"
            "環境變數，後續 endpoint 比對可能會查無對應",
            raw_value,
            default,
        )
        return default

    if default is not None:
        logger.warning(
            "server.servlet.context-path 的佔位符預設值本身還帶有巢狀佔位符 %r"
            "（正則表達式只解一層，不遞迴展開），無法當成字面值使用，視為沒有"
            "前綴: %s",
            default,
            raw_value,
        )
        return ""

    logger.warning(
        "server.servlet.context-path 使用環境變數佔位符 %s 且沒有預設值，"
        "靜態分析無法得知實際值，視為沒有前綴",
        raw_value,
    )
    return ""


def _nested_get(data: dict, *keys: str):
    """依序取巢狀 key，中途任一層不是 dict（含值為 `None` 的情況，如
    yaml 寫成 `server:` 沒有子節點）就回傳 `None`，不拋例外。"""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _read_context_path(project_root: Path) -> str:
    """只讀主設定檔（`server.servlet.context-path`，Spring Boot 2.x key
    名稱，對應目前專案的 Spring Boot 2.7.11），優先 `.properties`，找不到
    再試 `.yml`。profile-specific 檔案覆蓋、環境變數覆蓋這類殘餘情況不
    模擬，交給五章的 warning log fallback 兜底（見 04a 三章步驟 6）。
    找不到主設定檔或沒有這個 key 時回傳空字串（等同沒有前綴）。
    """
    resources = project_root / "src" / "main" / "resources"

    properties_path = resources / "application.properties"
    if properties_path.exists():
        for line in properties_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("server.servlet.context-path"):
                _, _, value = line.partition("=")
                return _resolve_placeholder(value.strip())

    yaml_path = resources / "application.yml"
    if yaml_path.exists():
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        context_path = _nested_get(data, "server", "servlet", "context-path")
        if context_path:
            return _resolve_placeholder(str(context_path))

    return ""
```

---

## 四、`grouping.py`——Map 階段分組

對應 04a 四章 Map 階段的分組邏輯：field 依賴子圖、in-degree 計算、共用類別抽取、4a／4b 兩個子階段的批次切分。**不呼叫 Claude API**——這裡只算「怎麼分組」，實際呼叫在 `summarize.py`（見五）。

```python
# parse_agent/grouping.py
"""① 解析 Agent：Map 階段分組，對應 04a 四章「Map-Reduce 語意摘要」分組
部分（4a 共用類別批次、4b Controller 批次）。純程式邏輯，呼應 04a 四章
「這一步不需要額外圖論工具...呼應 00 二章『能用程式判斷的，就不要交給
LLM』」。
"""
from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from common.chunking import chunk_by_char_budget
from parse_agent.call_graph import build_interface_implementors, resolve_field_target_classes
from parse_agent.types import ClassInfo, ParsedProject

# 4a／4b 共用的批次字元預算：用「批次內容序列化後字元數」當 context 大小
# 的代理指標，比單純數 class 數量更能反映真實 payload 大小。門檻值是
# 保守估計（見 04a 十章），開放環境變數覆蓋，不寫死在程式碼裡，實際數值
# 留待接上真實專案規模評估調整。切批次演算法本身跟 [B] Collection Agent
# 的 `_chunk_operations()` 共用 `common.chunking.chunk_by_char_budget()`
# （見 00 六章「Map 階段切批次（共用工具）」），這裡只留門檻值。
_MAX_CHARS_PER_MAP_CHUNK = int(os.environ.get("PARSE_AGENT_MAP_CHUNK_CHARS", "20000"))


def controller_dependency_closure(project: ParsedProject) -> dict[str, set[str]]:
    """對應 04a 四章第一段：從每個
    @RestController/@Controller 出發，沿欄位型別依賴
    （`resolve_field_target_classes()`，三章 3.2）**遞迴展開**，取得完整
    依賴閉包，不能只算 Controller 自己宣告的欄位這一層。

    **為什麼要遞迴，不能只算一層**：Spring Boot 專案常見
    Controller -> Service -> Repository 三層架構，Repository 通常只被
    Service 注入、不會被 Controller 直接注入——若只算 Controller 自己的
    欄位，Repository 這類二階以上的 class 永遠不會出現在任何 Controller
    的依賴集合裡，連帶不會被送進 Map 階段摘要（見 `build_controller_
    units()`），Reduce 完全看不到，`module_list` 會靜默漏掉這些 class 的
    所有方法，在真實 Spring 分層架構下沒有邏輯根據站得住腳。

    這裡用的是欄位型別依賴，不是方法呼叫圖（`call_graph`）——兩者是不同
    層次的圖：`call_graph` 是 method_id 層級的呼叫關係，供 `skip_filter.py`
    的可達性分析使用；這裡要的是 class 層級的欄位依賴，供 Map 分組使用，
    不能互相取代。遞迴展開用 visited 集合防止 class 間互相依賴造成無窮
    迴圈（如 ServiceA 依賴 ServiceB、ServiceB 又依賴回 ServiceA），寫法
    比照 `skip_filter.py` 既有的 BFS 可達性分析（見八章 `_bfs_reachable()`）。
    """
    interface_implementors = build_interface_implementors(project.classes)

    def _direct_deps(class_info: ClassInfo) -> set[str]:
        deps: set[str] = set()
        for f in class_info.fields:
            for target in resolve_field_target_classes(f, project.classes, interface_implementors):
                deps.add(target.class_name)
        # 補上欄位依賴解析不到的關係（靜態呼叫、方法參考等，見 call_
        # graph._extract_project_imports() docstring）：只要明確 import
        # 了專案內的類別，就算一條依賴，不透過 @Qualifier/@Primary 這類
        # DI 消歧——import 是編譯期就確定的單一目標，沒有 DI 那種「多個
        # 實作選一個」的歧義，不需要、也不應該套用 resolve_field_target_
        # classes() 那套消歧邏輯。
        for imported_name in class_info.imports:
            if imported_name != class_info.class_name and imported_name in project.classes:
                deps.add(imported_name)
        return deps

    def _closure(start_name: str) -> set[str]:
        visited: set[str] = set()
        queue: deque[str] = deque([start_name])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            current_class = project.classes.get(current)
            if current_class is None:
                continue
            for dep in _direct_deps(current_class):
                if dep not in visited:
                    queue.append(dep)
        visited.discard(start_name)  # 只要「依賴到的」class，不含 Controller 自己
        return visited

    result: dict[str, set[str]] = {}
    for class_info in project.classes.values():
        if class_info.stereotype not in ("RestController", "Controller"):
            continue
        result[class_info.class_name] = _closure(class_info.class_name)
    return result


def find_shared_classes(controller_deps: dict[str, set[str]]) -> set[str]:
    """in-degree（class 出現在幾個不同 Controller 的依賴閉包裡，同一個
    Controller 閉包內不重複計數）>= 2 的 class 判定為共用類別，對應
    04a 四章第一段。
    """
    in_degree: dict[str, int] = {}
    for deps in controller_deps.values():
        for dep in deps:
            in_degree[dep] = in_degree.get(dep, 0) + 1
    return {cls for cls, count in in_degree.items() if count >= 2}


# 標記「這個類別大概率是純資料容器」的 Lombok／JPA annotation——只是觸發
# 進一步檢查的訊號，不是最終判斷依據（見 needs_llm_summary() docstring，
# 真正決定「有沒有邏輯」的是方法清單本身，不是這份 annotation 清單）。
# 定義本身搬到 common/java_annotations.py 跟 design_agent 共用（見 00 六章
# 「Java class annotation 判斷（共用工具）」），這裡只留這個模組內沿用的
# 別名，成員集合與搬移前逐一相等，不影響本函式既有的分類結果——
# tests/common/test_java_annotations.py 鎖定這個等價性。
from common.java_annotations import DATA_CLASS_ANNOTATIONS as _DATA_ANNOTATIONS
# 存取器方法命名慣例：get*/set*/is*，或 Lombok/Java 慣例產生的
# equals/hashCode/toString/canEqual/builder/toBuilder。方法名稱只要
# 不落在這個樣式裡，就代表「除了欄位存取以外還有其他行為」。
_ACCESSOR_METHOD_RE = re.compile(
    r"^(get|set|is)[A-Z0-9_]|^(equals|hashCode|toString|canEqual|builder|toBuilder)$"
)


def needs_llm_summary(class_info: ClassInfo, has_implementor: bool) -> bool:
    """判斷這個 class 值不值得花一次 Claude API 呼叫做 Map 摘要，對應
    「Specification 特殊處理、無本體且無實作類別的介面、一般類別看方法
    清單、其餘預設當有邏輯」四層規則：

    1. **用到已知動態查詢型別（`uses_dynamic_query_signal`，見
       call_graph.py `_uses_dynamic_query_signal()`）一律判定為有邏輯**
       ——這類型別（如 `Specification<T>`）的實際過濾規則常常寫在回傳的
       lambda／匿名類別裡（見 `ExamSpecification.withYear()` 這類案例：
       `withYear()` 自己的方法本體只有一句 `return (lambda);`，`"string"`
       這種防呆判斷藏在 lambda 內部），不管有沒有其他 annotation，一律
       送 Map，不嘗試判斷 method body 內部藏了多少邏輯。

    2. **`has_implementor=False`（`build_interface_implementors()` 查無
       任何具體實作類別），且方法清單全部沒有本體（`all()` 在空清單上
       天生成立，涵蓋「完全沒有自訂方法，純靠繼承 CRUD」的情況）→ 判定
       不需要 LLM，機械處理**。這是 Spring Data JPA Repository 的典型
       形狀（`interface XxxRepository extends JpaRepository<...>`，
       Spring 在執行期動態生成 proxy，原始碼裡不存在任何實作）：這批
       方法沒有方法本體，LLM 讀到的資訊跟機械解析器完全一樣（都只有
       方法名稱／`@Query` annotation 可用），送 Map 花錢請 LLM 用比較
       不穩定的方式做一件機械解析能做得更準、更便宜、每次結果一致的
       事，不符合 00 二章「能用程式判斷的，就不要交給 LLM」。**刻意
       排除在「有具體實作類別」的情況之外**：若這個介面有
       `@Service`/`@Component`/`@Repository` 標註的具體實作類別
       （`has_implementor=True`），真正的業務邏輯寫在實作類別自己的
       方法裡（有本體，會正常送 Map），介面本身的抽象宣告不需要、也不
       應該被機械處理搶答——避免同一個業務方法產生兩筆不一致的記錄
       （見 `_mechanical_summary()` 對應這一段的處理）。

    3. **有 `_DATA_ANNOTATIONS` 標記，且方法清單只有存取器方法（或完全
       沒有明確方法，如純靠 Lombok `@Data` 生成 getter/setter，javalang
       看不到這些生成的方法）→ 判定為純資料類別，不送 Map**。這裡刻意
       不是「只要有 `@Entity` 就跳過」——DDD 風格的富領域模型常把業務
       規則寫在 entity 自己的方法裡（如 `isEligible()`／`calculateTotal()`
       這類非存取器命名的方法），這種情況下即使有 `@Entity`，只要方法
       清單裡出現任何一個不是存取器樣式的方法名稱，就會落到規則 4，
       維持送 Map——annotation 只決定「要不要進一步檢查方法清單」，不
       單獨決定「有沒有邏輯」。

    4. **其餘情況（沒有標記、或有標記但還有非存取器方法）→ 預設有
       邏輯，送 Map**，呼應 04a 三章「多連、少排除」同一種保守精神：
       不確定的情況一律當作「可能有邏輯」而不是「大概沒有」，多花一次
       API 呼叫的代價，遠低於漏掉真實業務邏輯的代價。
    """
    if class_info.uses_dynamic_query_signal:
        return True
    if not has_implementor and all(not m.has_body for m in class_info.methods):
        return False
    has_data_annotation = bool(set(class_info.annotations) & _DATA_ANNOTATIONS)
    if has_data_annotation:
        only_accessors = all(_ACCESSOR_METHOD_RE.match(m.name) for m in class_info.methods)
        if only_accessors:
            return False
    return True


def classify_trivial_classes(project: ParsedProject) -> set[str]:
    """對 `project.classes` 全部類別跑一次 `needs_llm_summary()`，回傳
    判定不需要送 Map 的類別名稱集合。分類本身不呼叫 Claude API、不花
    任何額度（純看 annotation／方法清單／有無實作類別），跟 04a 二章
    「能用程式判斷的，就不要交給 LLM」同一種分工——這裡的判斷不需要
    語意理解，只是機械的結構事實比對。`interface_implementors` 重用
    `controller_dependency_closure()` 內部已經在算的同一份資料（見
    `build_interface_implementors()`），這裡另外算一次——`ParsedProject`
    沒有把它落地成欄位，重算成本極低（純字典掃描），不值得為了省這一次
    重算去改變 `ParsedProject`／`controller_dependency_closure()` 的既有
    介面。
    """
    interface_implementors = build_interface_implementors(project.classes)
    return {
        name
        for name, info in project.classes.items()
        if not needs_llm_summary(info, has_implementor=name in interface_implementors)
    }


@dataclass(frozen=True)
class MapUnit:
    """單一 Map 呼叫的輸入單位（04a 四章「單一分組」）。`label` 只供
    logging／重試佇列追蹤用，不影響呼叫內容。`known_shared_summaries`
    只有 4b 批次會非空——4a 已完成的共用類別摘要，帶進 4b 這個分組的
    prompt（見 04a 四章子階段 4b 說明）。`project_root` 隨批次帶著走，供
    `summarize._class_source_payload()` 把 `ClassInfo.file_path`（相對
    路徑）還原成可讀檔的絕對路徑，見 `ParsedProject.project_root` 說明。
    """

    label: str
    classes: list[ClassInfo]
    project_root: str
    known_shared_summaries: dict[str, str] = field(default_factory=dict)


def build_shared_class_units(project: ParsedProject, shared_class_names: set[str]) -> list[MapUnit]:
    """4a：把共用類別抽出、依字元預算切成一批或多批（見 04a 四章子階段
    4a、十章「共用類別批次...若數量或原始碼體積過大，單一批次可能還是
    需要再拆子批次」）。批次之間彼此獨立，可平行呼叫（見 summarize.py），
    但 4a 整體要在 4b 開始前全部完成（見 04a 四章「4a 在 4b 之前完成」）。
    """
    shared_classes = [project.classes[name] for name in sorted(shared_class_names) if name in project.classes]
    chunks = chunk_by_char_budget(
        shared_classes, size_of=lambda c: _class_source_chars(project.project_root, c), budget=_MAX_CHARS_PER_MAP_CHUNK
    )
    return [
        MapUnit(label=f"4a:shared_batch_{i + 1}", classes=chunk, project_root=project.project_root)
        for i, chunk in enumerate(chunks)
    ]


def build_controller_units(
    project: ParsedProject,
    controller_deps: dict[str, set[str]],
    shared_class_names: set[str],
    shared_summaries: dict[str, str],
    trivial_class_names: set[str] = frozenset(),
) -> list[MapUnit]:
    """4b：每個 Controller 保留依賴閉包中 in-degree=1 的專屬依賴（`deps -
    shared_class_names`，`deps` 是 `controller_dependency_closure()` 算出
    的完整閉包，不只是 Controller 自己宣告的欄位型別），依字元預算切
    批次（同一個 Controller 若過大也可能被切成多批，是相對 04a 十章的
    額外保護，統一套用同一個安全網，不增加額外設計負擔）；分組依賴到的
    共用類別，從 `shared_summaries`（4a 完成後的產出）取出對應摘要放進
    `known_shared_summaries`，見 04a 四章子階段 4b。

    `trivial_class_names`：`classify_trivial_classes()` 判定不需要送 Map
    的類別（見該函式），從 `private_deps` 排除——這些類別仍留在
    `controller_deps` 裡（Reduce 的 `controller_dependencies` fact table
    需要完整依賴關係，見 `summarize._reduce_phase()`），只是不消耗一次
    Map API 呼叫；`java_files` 的完整性由 `summarize.py` 另外用機械產生
    的摘要補上，不受這裡排除的影響。
    """
    units: list[MapUnit] = []
    for controller_name, deps in controller_deps.items():
        controller = project.classes.get(controller_name)
        if controller is None:
            continue
        private_deps = [
            project.classes[d]
            for d in sorted(deps - shared_class_names - trivial_class_names)
            if d in project.classes
        ]
        shared_deps_used = sorted(deps & shared_class_names)
        known = {name: shared_summaries[name] for name in shared_deps_used if name in shared_summaries}

        chunks = chunk_by_char_budget(
            [controller, *private_deps],
            size_of=lambda c: _class_source_chars(project.project_root, c),
            budget=_MAX_CHARS_PER_MAP_CHUNK,
        )
        for i, chunk in enumerate(chunks):
            suffix = "" if len(chunks) == 1 else f"_part{i + 1}"
            units.append(
                MapUnit(
                    label=f"4b:{controller_name}{suffix}",
                    classes=chunk,
                    project_root=project.project_root,
                    known_shared_summaries=known,
                )
            )
    return units


def _class_source_chars(project_root: str, class_info: ClassInfo) -> int:
    """用來源檔案大小當這個 class 的 payload 字元數估計——同一個檔案若有
    多個 top-level class，字元數會重複計入各自的 class，估計值偏高但
    保守（寧可切得比實際需要更細，也不要低估導致單批過大），跟
    `summarize.py` 實際組 payload 時「整個檔案原始碼」的做法一致（見
    五章 `_class_source_payload()`）。`class_info.file_path` 只相對
    `project_root`，這裡組回絕對路徑才能實際讀到檔案（見
    `ParsedProject.project_root` 說明）。
    """
    try:
        return len(Path(project_root, class_info.file_path).read_text(encoding="utf-8"))
    except OSError:
        return 0
```

---

## 五、`prompts.py`——Map/Reduce system prompt 與 output schema

對應 04a 四章 Map／Reduce 兩階段的輸出契約。跟 `spec_collection_agent/prompts.py` 一樣，schema 跟對應的 system prompt 放同一個檔案，用 `output_config.format`（Structured Outputs，見六章 `llm.py`）保證輸出結構，不靠 prompt 文字單方面拜託模型遵守。

**Map 階段**：`classes` 陣列裡每個元素對應輸入的一個 class（4a 批次是共用類別、4b 批次是「一個 Controller ＋ 它的專屬依賴」，見四章），要求輸出的 `class_name` 必須與輸入一一對應（04a 六章 Reduce 階段要靠 `class_name` 把 Map 產出的每個 class 歸屬到 Reduce 決定的 module，這個對應鏈的起點就在這裡）。

**Reduce 階段**：輸入是所有 Map 批次的 `classes` 彙整結果，外加程式算好的事實（見四章 `controller_dependency_closure()`）——「哪些 Controller 的依賴閉包含哪個共用 class」直接當精確輸入，不要求模型重新猜測（04a 四章：「這也是程式算出的精確事實…不需要依賴…為了『呼叫圖抓不到、只能用語意猜』而設計的候選欄位」）。Reduce **只決定 class 歸屬哪個 module**，不重新輸出 `description`／`complexity`／方法清單——那些是 Map 階段已經給過的資訊，程式在 `summarize.py` 直接沿用 Map 產出的 `methods`（依 class 歸屬彙整），不要求模型重新謄寫方法清單或猜測 Python 命名，避免抄錯或增加不必要的 token 成本，也不會出現「Reduce 虛構一個 Map 沒摘要過的方法」這種需要額外驗證的風險。

```python
# parse_agent/prompts.py
"""① 解析 Agent 用到的 Claude API system prompt 與對應 output schema
集中於此，對應 04a 四章「Map-Reduce 語意摘要」。schema 跟 prompt 放同一
個檔案的理由，比照 spec_collection_agent/prompts.py 的說明——調整其中
一個時另一個就在旁邊，不容易顧此失彼。
"""
from __future__ import annotations

import copy

# 對應 grouping.py 產出的 MapUnit（4a 共用類別批次、4b Controller 批次
# 共用同一份 prompt，兩者的差異只在輸入 payload 的內容組成，不在 prompt
# 文字本身——見 04a 四章「Map 階段分兩個子階段，先共用、後專屬」）。
MAP_SYSTEM_PROMPT = """\
你是協助理解 Java（Spring Boot）專案業務邏輯、為 Java → Python 重構做
準備的助手。你會收到一批 Java class 的完整原始碼，任務是幫每個 class
產出摘要，供後續彙整跨 class 邊界的模組拆分判斷使用。

輸入可能包含 `known_shared_class_summaries`：這是其他已經分析過的共用
class 的簡短摘要（不是原始碼），純粹讓你在描述「這個 class 呼叫了什麼、
做什麼用」時有上下文可以參照，不需要你重新分析那些 class。

對輸入的每一個 class，輸出：

1. class_name：原樣抄輸入的 class 名稱，不要更動
2. summary：這個 class 對外暴露的業務行為是什麼、負責什麼，一段文字
3. methods：每個 public 方法一筆，包含：
   - method_name：原樣抄方法名稱
   - description：這個方法做什麼、輸入輸出的業務意義
   - complexity："low"（單純轉發/查詢/CRUD）、"medium"（有條件判斷或
     多步驟業務規則）、"high"（複雜計算、跨多個資料來源整合、大量分支）
     三選一
4. cross_group_dependency_hints：這個 class「看起來」跟哪些不在這批輸入
   裡的其他 class 有業務關聯（不確定是否精確，只是候選線索，交給後續
   彙整階段做最終判斷）——列出你觀察到的 class 名稱即可，不需要解釋

只回傳一個 JSON object，不要輸出任何其他文字，不要用 markdown code fence
包裹。
"""

MAP_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "classes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "class_name": {"type": "string"},
                    "summary": {"type": "string"},
                    "methods": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "method_name": {"type": "string"},
                                "description": {"type": "string"},
                                "complexity": {"type": "string", "enum": ["low", "medium", "high"]},
                            },
                            "required": ["method_name", "description", "complexity"],
                            "additionalProperties": False,
                        },
                    },
                    "cross_group_dependency_hints": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["class_name", "summary", "methods", "cross_group_dependency_hints"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classes"],
    "additionalProperties": False,
}

# 對應 04a 四章 Reduce 階段。輸入見 summarize.py `_reduce_phase()` 組的
# payload：所有 Map 批次的 classes 彙整結果 + controller_dependencies
# （程式算好的精確事實）。
REDUCE_SYSTEM_PROMPT = """\
你是協助把 Java 專案拆分成 Python 重構模組的助手。你會收到：

1. classes：一批 Java class 的摘要（來自前一階段的分析），每個 class
   包含業務摘要、每個方法的描述與複雜度、以及「看起來」跟哪些其他 class
   有關聯的線索
2. controller_dependencies：每個 Controller class 實際依賴哪些 class
   （程式碼精確分析得出的事實，不是推測，可以直接信任）

任務：決定最終的模組（module）拆分——哪些 class 應該歸在同一個模組。
一個模組通常對應一個或多個 Controller 以及它們專屬依賴的
repository/service class；被多個 Controller 共用的 class 依業務關聯
判斷歸入最相關的模組（即使被其他模組依賴，也只屬於一個模組，其他模組
透過 depends_on 表示跨模組依賴，不重複歸屬）。

對每個模組輸出：

1. module：模組名稱，snake_case，簡短有語意（如 "user"、"order"）
2. summary：重新彙整這個模組內所有 class 的摘要，寫成模組層級的業務
   摘要——不要只是把各 class 的摘要接起來，要重新歸納「這個模組整體
   對外暴露的業務行為」「為什麼這些 class 被歸在一起」「依賴其他模組
   的業務原因」
3. java_classes：這個模組包含哪些 class（原樣抄 class_name）
4. depends_on：這個模組依賴哪些其他模組（填 module 名稱，不是 class
   名稱）

**java_classes 只能是輸入 classes 陣列裡出現過的 class_name，禁止填入
任何沒出現過的名稱**：輸入的 classes 陣列就是這次要分類的完整 class
清單，不是範例或摘要。即使你從某個方法描述聯想到「這個功能應該會用到
一個 CreateExamRequest 或 LoginResponse 這類 DTO class」，只要這個名稱
沒有原樣出現在輸入的 classes 陣列裡，就絕對不能把它填進 java_classes
——這種 DTO/Request/Response class 通常沒有業務邏輯，本來就不會出現在
你收到的 classes 清單裡，這是預期內的正常情況，不代表你需要幫忙補上。
填入清單外的名稱不會產生任何效果，只會被直接丟棄，純粹浪費你的輸出。

不要輸出任何其他文字，不要用 markdown code fence 包裹。
"""

REDUCE_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "modules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "module": {"type": "string"},
                    "summary": {"type": "string"},
                    "java_classes": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["module", "summary", "java_classes", "depends_on"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["modules"],
    "additionalProperties": False,
}


def build_reduce_output_schema(valid_class_names: list[str]) -> dict:
    """回傳 `REDUCE_OUTPUT_SCHEMA` 的動態版本：`java_classes` 陣列的每個
    元素加上 `enum: valid_class_names` 約束，`valid_class_names` 是呼叫端
    傳入的、這次 Map 階段實際摘要過的 class_name 完整集合（封閉集合，
    Reduce 呼叫前就已經確定，不是猜測）。

    **為什麼用 schema 約束、不只靠 prompt 指令**：REDUCE_SYSTEM_PROMPT
    已經有「java_classes 只能是輸入 classes 陣列裡出現過的名稱，禁止
    杜撰」的文字指令，但這只能降低模型虛構 class 名稱的機率，降不到 0
    ——模型看到方法描述提到「建立考試」，語意上很容易聯想到一個
    `CreateExamRequest`／`CreateExamRq` 這類 DTO 應該存在，即使明確被
    告知不要這樣做。既然合法的 class 名稱集合在呼叫前就已經是確定的
    封閉集合，這正是 00 二章「能用程式判斷的，就不要交給 LLM」的情況：
    用 Structured Outputs 的 `enum` 讓 API 在生成階段就不可能輸出集合外
    的字串，不是「生成後再靠程式碼濾掉」（`_assemble_module_drafts()`
    的 `missing_classes` 檢查會繼續留著，當作 defense-in-depth，不因為
    這裡加了 enum 約束就拿掉）。`enum` 只限制這個欄位「填的字串必須是
    這些之一」，不影響模型判斷要怎麼分組、哪些 class 該歸同一個模組，
    Reduce 階段原本的判斷空間完全不受影響。

    `depends_on` 刻意不做同樣的 enum 約束：`depends_on` 引用的是這次
    回應**自己產出**的 module 名稱，屬於自我參照，呼叫前不存在一個
    「合法 module 名稱」的封閉集合可以拿來約束（`module` 名稱本身也是
    這次回應才決定的），這個欄位仍然只能依賴 `_assemble_module_drafts()`
    既有的事後驗證（見 04a 四章「depends_on 引用不存在的 module 名稱」）。
    """
    schema = copy.deepcopy(REDUCE_OUTPUT_SCHEMA)  # 淺拷貝不夠，這裡巢狀結構要整份複製，避免動到共用的模組常數
    schema["properties"]["modules"]["items"]["properties"]["java_classes"]["items"] = {
        "type": "string",
        "enum": valid_class_names,
    }
    return schema
```

---

## 六、`llm.py`——① 專屬的模型選擇

對應 00 六章「Claude API 呼叫封裝」：client 初始化、Structured Outputs、`log_usage()` 整合、錯誤處理已經集中在 `common/llm_client.py`，供所有需要呼叫 Claude API 的 Agent 共用（見 `docs/03c_collection_agent_code.md` 一、1.2 節，`spec_collection_agent/llm.py` 是第一個接上的）。① 不需要再重新實作一份幾乎相同的 client／`call_claude_for_json()`，`parse_agent/llm.py` 只留一件事：① 用哪個模型。

```python
# parse_agent/llm.py
"""① 解析 Agent 專屬的 Claude API 模型選擇。實際呼叫邏輯（client 初始化、
Structured Outputs、log_usage() 整合、錯誤處理）在 common/llm_client.py，
所有需要呼叫 Claude API 的 Agent 共用同一份（見 00 六章）。這個檔案只
負責一件事：① 用哪個模型，不跟 [B] 共用同一個 `SPEC_COLLECTION_AGENT_MODEL`。
"""
from __future__ import annotations

import os

from common.llm_client import DEFAULT_MODEL_FALLBACK

DEFAULT_MODEL = os.environ.get("PARSE_AGENT_MODEL", DEFAULT_MODEL_FALLBACK)
```

`summarize.py` 的 Map／Reduce 呼叫端（見七章）改成 `from common.llm_client import LlmJsonError, call_claude_for_json` ＋ `from parse_agent.llm import DEFAULT_MODEL`，呼叫時傳 `model=DEFAULT_MODEL`——跟 `spec_collection_agent` 的 `chain_dependency_detect.py`／`folder_grouper.py` 是同一種寫法。

---

## 七、`summarize.py`——Map/Reduce 呼叫、重試佇列、輸出組裝

對應 04a 四章（Map/Reduce 執行與失敗重試）與六章（輸出組裝）。這個檔案是 04a 七章模組結構裡負責「組裝 module_list／api_to_python_target」的實際落點——04a 七章的職責表把這件事單獨列一行，沒有指定檔案，這裡歸給 `summarize.py`：組裝邏輯直接消費 Map/Reduce 的原始輸出，跟兩者的呼叫邏輯放同一個檔案，不需要為了「組裝」這一步另開檔案。

### 7.1 Map 呼叫與重試佇列（四章）

**跟 `chain_dependency_detect.py` 的失敗處理策略刻意不同**：[B] 的 `_map_phase()` 任一批次失敗就整體中止、取消其餘尚未開始的批次（鏈式依賴偵測的 map 候選彼此依賴 reduce 階段才有意義，任一批次缺失就會讓 reduce 的配對不完整，語意上等於整體失敗）。① 的 Map 分組彼此獨立（每組摘要各自的 class，不互相依賴），所以 04a 四章刻意設計成「失敗的組列入待重試清單，不立即中止」——這裡的 `_run_map_batch()` 讓每個批次的呼叫互相隔離，一個失敗不影響其他批次繼續執行。

```python
# parse_agent/summarize.py
"""① 解析 Agent：Map/Reduce 呼叫與輸出組裝，對應 04a 四章（Map/Reduce
執行與失敗重試）、六章（輸出組裝）。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from graph.state import ApiMapping, MethodInfo, ModuleInfo
from parse_agent.exceptions import ParseAgentMapReduceError
from parse_agent.grouping import (
    _ACCESSOR_METHOD_RE,
    MapUnit,
    build_controller_units,
    build_shared_class_units,
    classify_trivial_classes,
    controller_dependency_closure,
    find_shared_classes,
)
from common.concurrency import default_concurrency
from common.llm_client import LlmJsonError, call_claude_for_json
from parse_agent.llm import DEFAULT_MODEL
from parse_agent.prompts import (
    MAP_OUTPUT_SCHEMA,
    MAP_SYSTEM_PROMPT,
    REDUCE_SYSTEM_PROMPT,
    build_reduce_output_schema,
)
from parse_agent.types import ClassInfo, MapClassResult, MapMethodResult, MethodEntry, MethodId, ParsedProject, method_id

logger = logging.getLogger(__name__)

# 併發數＝可用核心數 - 1，跟 spec_collection_agent/chain_dependency_
# detect.py 共用同一份 common/concurrency.py 實作，不各自重新推導（見
# 04a 四章「併發數」、00 六章「Map 階段併發數（共用工具）」）。4a 通常
# 只有一批，這個上限對 4a 沒有實際影響，兩個子階段共用同一個常數不重複
# 定義。
_MAX_MAP_WORKERS = default_concurrency()

# 04a 四章：「等待 5 分鐘，對待重試清單裡的組統一重試一次」。
_RETRY_WAIT_SECONDS = float(os.environ.get("PARSE_AGENT_MAP_RETRY_WAIT_SECONDS", "300"))


def _class_source_payload(project_root: str, class_info: ClassInfo) -> dict:
    """組單一 class 送給 Map 的 payload：直接讀整個檔案原始碼。假設
    Java 慣例「每檔一個 top-level class」（04a 三章驗證過的目標專案
    對應這個假設，實際檔案數見十一章）——不做 AST 節點到原始碼片段的
    精確切割，模型看到整個檔案（含 import、其他非 top-level 的巢狀
    類別）多花一點 token，換取不用處理「怎麼從 AST 節點精確還原原始碼
    區間」這個額外複雜度。

    `class_info.file_path` 只相對 `project_root`（見
    `ParsedProject.project_root` 說明），這裡組回絕對路徑才能實際讀到
    檔案。
    """
    return {
        "class_name": class_info.class_name,
        "source": Path(project_root, class_info.file_path).read_text(encoding="utf-8"),
    }


def _map_analyze_batch(unit: MapUnit) -> list[MapClassResult]:
    """對單一 MapUnit（4a 或 4b 的一個分組）呼叫 Claude，回傳這批
    class 各自的摘要結果。`MAP_OUTPUT_SCHEMA`（output_config.format）已
    保證輸出結構，不需要再逐筆檢查型別——但「classes 陣列的 class_name
    是否恰好對應輸入」是語意層面的事，schema 保證不了，這裡仍要核對
    （做法比照 folder_grouper._extract_singleton_folders() 的語意核對，
    見 03c 二章 2.2）。

    **核對到遺漏時視同呼叫失敗，直接拋 `LlmJsonError`，交給
    `run_map_phase_with_retry()` 的重試佇列**——只記 warning、照樣把不
    完整的結果傳下去，會跟 `run_map_phase_with_retry()` 自己的
    docstring 立場矛盾：那裡明講「留空繼續會讓 ③ 在不知情的狀況下對著
    不完整的模組清單做設計決策，風險更高」，這句話對「整批呼叫失敗」和
    「呼叫成功但內容漏答」這兩種情況都成立，沒有理由只在前者中止、後者
    卻放行。Structured Outputs（`output_config.format`）保證的是**每一筆
    輸出項目**符合 schema，不保證陣列筆數等於輸入筆數，LLM 漏答完全是
    schema-valid 的合法輸出，不會被 `call_claude_for_json()` 攔下。拋出
    後整個 unit（不只是遺漏的那幾個 class）會被丟回待重試清單，等 5 分鐘
    後重新送出同一批 payload——不保證重試一定補齊，但至少不會讓「這批
    模型漏答的 class」跟「模型正常摘要的 class」用同一套「僅記 log」的
    待遇被靜默放行。
    """
    payload = {
        "classes": [_class_source_payload(unit.project_root, c) for c in unit.classes],
        "known_shared_class_summaries": unit.known_shared_summaries,
    }
    user_prompt = json.dumps(payload, ensure_ascii=False)

    result = call_claude_for_json(
        system_prompt=MAP_SYSTEM_PROMPT, user_prompt=user_prompt, schema=MAP_OUTPUT_SCHEMA, model=DEFAULT_MODEL
    )

    expected_names = {c.class_name for c in unit.classes}
    returned_names = {entry["class_name"] for entry in result["classes"]}
    missing = expected_names - returned_names
    if missing:
        raise LlmJsonError(
            f"Map 分組 {unit.label} 的回應遺漏了部分輸入 class 的摘要"
            f"（視同呼叫失敗，交給待重試清單處理）: {sorted(missing)}"
        )

    known_methods_by_class = {c.class_name: {m.name for m in c.methods} for c in unit.classes}

    return [
        MapClassResult(
            class_name=entry["class_name"],
            summary=entry["summary"],
            methods=_normalize_class_methods(entry, known_methods_by_class.get(entry["class_name"], set())),
            cross_group_dependency_hints=entry["cross_group_dependency_hints"],
        )
        for entry in result["classes"]
    ]


def _normalize_class_methods(entry: dict, known_names: set[str]) -> list[MapMethodResult]:
    """對單一 class 的 Map 回應逐筆呼叫 `_normalize_method_name()`，回傳
    值為 `None`（完全對不上這個 class 任何真實方法，見該函式 docstring）
    的項目直接不進最終清單——這個名稱在 javalang 掃描出的真實方法集合
    裡不存在（常見成因：LLM 把建構子當成方法回報，如 `AuthException`／
    `ExamException` 這類只有多載建構子、沒有一般方法的例外類別），留著
    它只會讓 `module_list.methods` 混進一筆永遠對不到任何 Java 方法的
    幽靈記錄——③ 架構設計 Agent 的 `_build_method_contexts()` 找不到
    對應方法只能整批略過（05a 六章「已知限制」），[P] Plan Agent 依
    04a 六章「以完整方法清單為準」拆 task 時也會對著這筆不存在的方法
    產生一個永遠做不完的 task。比照 04a 五章「多連、少排除」精神在
    method_id 這層的做法——那裡「連」的前提是候選確實可能是真實依賴；
    這裡的情況相反，一個 method_name 完全不在 javalang 權威來源的方法
    集合裡，不是「不確定要不要留」，是「確定這個方法不存在」，因此
    這裡改成「明確無效就排除」，跟呼叫圖那邊的保守方向並不矛盾。
    """
    normalized: list[MapMethodResult] = []
    for m in entry["methods"]:
        name = _normalize_method_name(m["method_name"], known_names)
        if name is None:
            continue
        normalized.append(MapMethodResult(name, m["description"], m["complexity"]))
    return normalized


_METHOD_NAME_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _normalize_method_name(raw_name: str, known_names: set[str]) -> str | None:
    """Map 階段回傳的 `method_name` 理論上應該原樣抄自輸入原始碼（見
    `prompts.MAP_SYSTEM_PROMPT`「method_name：原樣抄方法名稱」），但一個
    class 內有同名多載方法時（如兩個 `voice`，一個 `@PostMapping`、一個
    `@GetMapping`），即使 prompt 明確要求原樣抄，模型仍可能自行加註
    `"(POST)"`／`"(GET)"` 這類後綴消歧同名方法。這個後綴一旦留著，
    `filter_excluded_methods()`／`assemble_api_mapping()` 用字串完全比對
    method_id 時就會對不上 `route_index`／`call_graph` 裡 javalang 解析
    出的真實方法名稱（沒有這個後綴），導致該方法對應的 endpoint 整批從
    `api_to_python_target` 消失——`assemble_api_mapping()` 會把這誤判成
    「方法已被排除」的合法情況，不會產生任何 warning。

    這裡在合併回 `MapMethodResult` 之前正規化：原樣名稱若不在這個 class
    實際宣告的方法名稱集合（`known_names`，來自 javalang 掃描結果，權威
    來源）裡，嘗試剝掉結尾的括號後綴再比對一次。

    **兩種情況的處理刻意不同**：多載消歧後綴剝掉後能對上，代表這就是
    一個真實存在的方法，只是名稱被模型加了註記，正規化回真實名稱即可。
    但剝掉後綴仍然對不上任何真實方法時（`known_names` 是 javalang 掃描
    出的權威來源，不是猜測），代表這個 `method_name` 根本不對應這個
    class 的任何真實方法——最常見的成因是模型把建構子（javalang 不會把
    建構子算進 `class_decl.methods`）誤報成方法。這種情況不是「正規化不
    出正確名稱」，是「這筆方法本身就不存在」，因此回傳 `None`，由呼叫端
    （`_normalize_class_methods()`）直接排除這筆記錄，不讓一個確定不存在
    的方法混進 `module_list.methods`（見該函式 docstring）。
    """
    if raw_name in known_names:
        return raw_name
    stripped = _METHOD_NAME_SUFFIX_RE.sub("", raw_name).strip()
    if stripped != raw_name and stripped in known_names:
        logger.info(
            "Map 回應的 method_name %r 正規化為 %r（多載方法消歧後綴，見 "
            "_normalize_method_name() docstring）",
            raw_name,
            stripped,
        )
        return stripped
    logger.warning(
        "Map 回應的 method_name %r 在對應 class 的實際方法清單中找不到"
        "（剝掉消歧後綴後仍對不上，常見成因是模型把建構子誤報成方法），"
        "判定這筆方法不存在，已從 module_list 排除，不需要人工介入"
        "（見 _normalize_method_name() docstring）: 已知方法清單=%s",
        raw_name,
        sorted(known_names),
    )
    return None


def _run_map_batch(units: list[MapUnit]) -> tuple[list[MapClassResult], list[MapUnit]]:
    """平行執行一批 MapUnit（4a 或 4b 各自呼叫一次這個函式），回傳
    (成功結果攤平清單, 失敗待重試的 MapUnit 清單)。每個 unit 的失敗
    互相隔離，不取消其他 unit（見本節前言，跟 chain_dependency_detect.py
    的策略刻意不同）。
    """
    results: list[MapClassResult] = []
    failed: list[MapUnit] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_MAP_WORKERS, len(units))) as pool:
        futures = {pool.submit(_map_analyze_batch, u): u for u in units}
        for future in concurrent.futures.as_completed(futures):
            unit = futures[future]
            try:
                results.extend(future.result())
            except LlmJsonError as exc:
                logger.warning("Map 分組呼叫失敗，列入待重試清單: %s（%s）", unit.label, exc)
                failed.append(unit)
    return results, failed


def run_map_phase_with_retry(units: list[MapUnit]) -> list[MapClassResult]:
    """對應 04a 四章「單一分組（4a 或 4b 任一組）呼叫 Claude API 失敗
    時」的三步驟：先跑一輪、失敗的列入待重試清單；全部跑完後等待
    `_RETRY_WAIT_SECONDS` 秒，統一重試一次；重試仍失敗的視為硬性失敗。

    **重試仍失敗後採「中止整條 run」的保守預設（見 04a 十章）**——理由：
    留空繼續代表這批 class 的方法完全不會出現在 module_list，讓 ③ 架構
    設計 Agent 在完全不知情的狀況下對著不完整的模組清單做設計決策，比起
    直接中止讓人工介入，風險更高、更難事後追查根因。這個理由對「整批
    呼叫失敗」（如網路逾時、API 限流）和「呼叫成功但 LLM 漏答部分 class」
    （見 7.1 `_map_analyze_batch()`，一律拋 `LlmJsonError` 統一走這裡的
    重試佇列）同樣成立——不完整的資料會誤導下游這件事，不會因為「失敗
    原因是傳輸層還是模型內容層」而有差別，因此兩者共用同一套重試／中止
    機制，不分開處理。日後若要改成「留空繼續」，只需要把下面的
    `raise ParseAgentMapReduceError` 改成 log warning＋回傳目前已有的
    `results`，不影響本函式的呼叫端介面。

    **`time.sleep()` 為什麼不會拖住整個 Orchestrator**：這個函式（連同
    整個 `run_parse_agent()`）是被 `parse_node.py` 用 `asyncio.to_thread()`
    丟到獨立 worker thread 執行的（見九章／十章），`sleep` 只擋住那一條
    thread，不會擋住 asyncio 事件迴圈本身。這裡是單次批次流程（見 00／01
    架構：單一 Orchestrator 跑單一 Java 專案，`parse` 在圖上是單點
    執行），等待的這幾分鐘裡沒有其他工作在跟這條 thread 搶執行緒池資源；
    若日後這個套件被套用到會同時執行多個 pipeline 的情境，才需要重新
    評估要不要把這個等待邏輯移出 thread pool。
    """
    if not units:
        return []

    results, failed = _run_map_batch(units)
    if not failed:
        return results

    logger.warning(
        "%d 組 Map 呼叫失敗，等待 %.0f 秒後統一重試一次: %s",
        len(failed),
        _RETRY_WAIT_SECONDS,
        [u.label for u in failed],
    )
    time.sleep(_RETRY_WAIT_SECONDS)

    retry_results, still_failed = _run_map_batch(failed)
    results.extend(retry_results)

    if still_failed:
        raise ParseAgentMapReduceError(
            f"{len(still_failed)} 組 Map 呼叫重試後仍失敗，中止整個 parse run"
            f"（04a 十章的保守預設，見本函式 docstring）: "
            f"{[u.label for u in still_failed]}"
        )
    return results
```

### 7.2 Reduce 呼叫（四章）

```python
# parse_agent/summarize.py（續）

def _reduce_phase(map_results: list[MapClassResult], controller_deps: dict[str, set[str]]) -> dict:
    """單次呼叫，輸入是 4a+4b 全部 Map 結果的彙整（濃縮後的候選結果，
    非原始碼全量，見 04a 四章 Reduce 階段）+ 程式算好的
    controller_dependencies 事實（見 04a 四章「這也是程式算出的精確
    事實」）。回傳 output schema 的原始 dict，組裝成 ModuleInfo 是 7.3
    的事，這裡只負責呼叫。

    **`java_classes` 用動態 enum 約束，不是靜態 `REDUCE_OUTPUT_SCHEMA`**：
    `map_results` 涵蓋的 class_name 集合在呼叫這次 Reduce 之前就已經
    確定（Map 階段已經跑完），是封閉集合，透過 `build_reduce_output_
    schema()` 把這個集合灌進 schema 的 `enum`，讓 API 在生成階段就不
    可能吐出集合外的 class 名稱——見 `prompts.build_reduce_output_
    schema()` docstring「為什麼用 schema 約束、不只靠 prompt 指令」。
    """
    valid_class_names = sorted({r.class_name for r in map_results})
    payload = {
        "classes": [
            {
                "class_name": r.class_name,
                "summary": r.summary,
                "methods": [
                    {"method_name": m.method_name, "description": m.description, "complexity": m.complexity}
                    for m in r.methods
                ],
                "cross_group_dependency_hints": r.cross_group_dependency_hints,
            }
            for r in map_results
        ],
        "controller_dependencies": {
            controller: sorted(deps) for controller, deps in controller_deps.items()
        },
    }
    user_prompt = json.dumps(payload, ensure_ascii=False)

    try:
        return call_claude_for_json(
            system_prompt=REDUCE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema=build_reduce_output_schema(valid_class_names),
            model=DEFAULT_MODEL,
        )
    except LlmJsonError as exc:
        # Reduce 只有單次呼叫，沒有「多組互相隔離」的概念，失敗直接視為
        # 硬性失敗（跟 Map 階段的重試佇列語意不同，這裡不重試）——重試一次
        # 能解決的通常是網路抖動這類 anthropic SDK 內建 max_retries 已經
        # 處理過的暫時性錯誤，這裡再重試意義不大。
        raise ParseAgentMapReduceError(f"Reduce 階段呼叫失敗（{len(map_results)} 筆 class 摘要）: {exc}") from exc
```

### 7.3 輸出組裝（六章）

**為什麼需要 `_ModuleDraft` 這個中繼結構**：`graph/state.py` 的 `MethodInfo` 含 `class_name`（供 ③ 架構設計 Agent 重新掃描 Java 簽名時比對回正確的類別——同一 module 內跨層同名方法會歧義，見 05a 二章），但沒有 `file_path`：`file_path` 已經由 `ModuleInfo.java_files` 在模組層級提供，方法層級不需要重複帶。但 `filter_excluded_methods()`（下方）需要用 `method_id`（含 `class_name`／`file_path`）判斷是否落在 `skip_filter` 算出的排除集合裡，如果直接組成 `MethodInfo` 就沒有 `file_path` 可用，之後就無法正確比對。因此組裝分兩步：先組出額外保留 `file_path` 的內部草稿 `_ModuleDraft`，排除計算跟 `assemble_api_mapping()`（同樣需要 `file_path`）都在草稿階段完成，最後才用 `finalize_module_list()` 剝除 `file_path`、產出真正符合 `ModuleInfo` 型別的公開輸出。

```python
# parse_agent/summarize.py（續）

@dataclass(frozen=True)
class _DraftMethod:
    class_name: str
    file_path: str
    method: MethodInfo


@dataclass
class _ModuleDraft:
    module: str
    summary: str
    java_files: list[str]
    depends_on: list[str]
    methods: list[_DraftMethod]


def _assemble_module_drafts(
    map_results: list[MapClassResult], reduce_result: dict, project: ParsedProject
) -> tuple[list[_ModuleDraft], dict[str, str]]:
    """把 Reduce 的模組拆分決策（哪些 class 歸在同一個 module），跟 Map
    階段已經產出的 methods（description／complexity）機械合併，組成
    `_ModuleDraft` 清單。Reduce 只決定 class 層級的歸屬，方法清單完全
    沿用 Map 的輸出（見本節前言），不需要 Reduce 重新謄寫或猜測 Python
    命名，也就不會出現「Reduce 虛構一個 Map 沒摘要過的方法」這種需要
    額外核對的情況。同時回傳 `class_name -> module` 對照表，供
    `assemble_api_mapping()` 使用（04a 六章：ApiMapping.module 要對回這個
    class 最終被分進哪個模組）。這裡產出的是**排除 skip 呼叫鏈之前**的
    完整版本，method 層級的排除交給 `filter_excluded_methods()`（下一個
    函式），對應 04a 五章「排除發生在 Reduce 階段輸出 module_list...之前」。

    **`missing_classes` 只檢查一個方向**：Reduce 輸出的 `java_classes`
    引用了不存在的 class（模型虛構／拼錯名稱）。這裡另外反向檢查
    `map_class_names - class_to_module.keys()`——Map 階段已經成功摘要、
    但 Reduce 完全沒有把它分進任何 module 的 class：這種情況不會觸發
    `missing_classes`（因為 Reduce 根本沒提到這個 class 的名字，沒有
    「引用不存在的東西」），卻會讓一個真實存在、已經花錢摘要過的 class
    整個從 `module_list` 消失、沒有任何訊號。只記警告，不代為指派
    module——指派哪個 module 是需要業務判斷的事，不該由這裡的程式邏輯
    瞎猜一個（呼應 00 二章「能用程式判斷的，就不要交給 LLM」的反面：這裡
    是「不該交給程式判斷的，也不要瞎猜」）。

    另外也驗證 `depends_on`（見本函式最後一段）：比照 `missing_classes`
    對 `java_classes` 的處理方式，過濾掉引用不存在 module 名稱的項目並
    記警告，不讓虛構的依賴關係原樣流入最終輸出。
    """
    # class_name -> Map 階段對這個 class 的摘要結果（含 methods），供機械合併用
    class_to_map_result: dict[str, MapClassResult] = {r.class_name: r for r in map_results}

    drafts: list[_ModuleDraft] = []
    class_to_module: dict[str, str] = {}

    for module_entry in reduce_result["modules"]:
        valid_classes = [cls for cls in module_entry["java_classes"] if cls in project.classes]
        missing_classes = [cls for cls in module_entry["java_classes"] if cls not in project.classes]
        if missing_classes:
            logger.warning(
                "Reduce 產出的 module %s 引用了不存在於呼叫圖掃描結果的 class"
                "（模型可能虛構或拼錯名稱），已略過: %s",
                module_entry["module"],
                missing_classes,
            )

        usable_classes: list[str] = []
        draft_methods: list[_DraftMethod] = []
        for cls in valid_classes:
            map_result = class_to_map_result.get(cls)
            if map_result is None:
                # 7.1 _map_analyze_batch() 現在遺漏任何一個輸入 class 就會
                # 直接拋例外、交給重試佇列，重試仍失敗會中止整條 run（見
                # 7.1 docstring），所以能執行到這裡代表 map_results 對所有
                # 送進過 Map 階段的 class 都是完整的——這裡會觸發只可能是
                # Reduce 自己虛構了一個「存在於專案、但沒被送進過 Map 階段」
                # 的 class 名稱。
                logger.warning(
                    "Reduce 產出的 module %s 引用了 Map 階段沒有摘要過的 class"
                    "（模型可能虛構），已略過: %s",
                    module_entry["module"],
                    cls,
                )
                continue
            usable_classes.append(cls)
            class_to_module[cls] = module_entry["module"]
            for m in map_result.methods:
                draft_methods.append(
                    _DraftMethod(
                        class_name=cls,
                        file_path=project.classes[cls].file_path,
                        method=MethodInfo(
                            java_method=m.method_name,
                            class_name=cls,
                            description=m.description,
                            complexity=m.complexity,  # type: ignore[typeddict-item]
                        ),
                    )
                )

        drafts.append(
            _ModuleDraft(
                module=module_entry["module"],
                summary=module_entry["summary"],
                java_files=sorted({project.classes[cls].file_path for cls in usable_classes}),
                depends_on=module_entry["depends_on"],
                methods=draft_methods,
            )
        )

    map_class_names = {r.class_name for r in map_results}
    unassigned = map_class_names - set(class_to_module)
    if unassigned:
        logger.warning(
            "Map 階段已摘要、但 Reduce 沒有把它分進任何 module 的 class"
            "（模型可能漏看，這些 class 的方法不會出現在 module_list，見"
            "本函式 docstring「missing_classes 只檢查一個方向」）: %s",
            sorted(unassigned),
        )

    # depends_on 引用的是其他 module 的名稱，不是 class，只有等所有
    # module_entry 都跑完、drafts 收集齊全才知道「合法的 module 名稱」有
    # 哪些，所以放在迴圈外面單獨一輪檢查——跟 missing_classes／unassigned
    # 是同一類「驗證 Reduce 有沒有引用不存在的東西」，但檢查對象、時機點
    # 不同，不能塞進上面的迴圈裡順便做。
    valid_module_names = {d.module for d in drafts}
    for d in drafts:
        invalid_deps = [dep for dep in d.depends_on if dep not in valid_module_names]
        if invalid_deps:
            logger.warning(
                "module %s 的 depends_on 引用了不存在於本次輸出的 module 名稱"
                "（模型可能虛構或拼錯名稱），已從 depends_on 移除: %s",
                d.module,
                invalid_deps,
            )
            d.depends_on = [dep for dep in d.depends_on if dep in valid_module_names]

    return drafts, class_to_module


def filter_excluded_methods(drafts: list[_ModuleDraft], excluded: set[MethodId]) -> list[_ModuleDraft]:
    """對應 04a 五章「排除發生在 Reduce 階段輸出 module_list...之前，
    被排除的方法從一開始就不會出現在最終輸出裡」——這裡是實際套用排除
    的地方：`excluded` 來自 `skip_filter.compute_excluded_methods()`
    （見八章），只排除 method 層級，不影響 module／其餘方法。排除後
    方法清單為空的 module 予以保留（module 底下所有方法都只服務 skip
    endpoint 的極端情況）——是否要連 module 一起拿掉不在 04a 五章的排除
    定義範圍內，交給下游 ③ 架構設計 Agent 自行判斷是否需要一個空模組，
    這裡不做額外決策。
    """
    return [
        _ModuleDraft(
            module=d.module,
            summary=d.summary,
            java_files=d.java_files,
            depends_on=d.depends_on,
            methods=[
                dm
                for dm in d.methods
                if method_id(dm.file_path, dm.class_name, dm.method["java_method"]) not in excluded
            ],
        )
        for d in drafts
    ]


def assemble_api_mapping(
    project: ParsedProject,
    class_to_module: dict[str, str],
    drafts: list[_ModuleDraft],
    skip_endpoints: set[str],
) -> list[ApiMapping]:
    """對應 04a 六章 ApiMapping：把三章機械建構的 `route_index`（Java
    原始碼實際擁有的 endpoint，見三章步驟 5～6）跟 Reduce 決定的模組
    歸屬、method 排除結果串起來。呼叫端必須傳入**已經 `filter_excluded_
    methods()` 過的** `drafts`——route_index 裡指向的 method_id 若已經
    被排除（skip 呼叫鏈排除，或 Reduce 沒有覆蓋到），這筆 endpoint 直接
    不進最終 api_to_python_target：不只是「該 method 被排除」，而是這個
    endpoint 本身的處理方法都不會被下游改寫，繼續留著這筆映射沒有意義。

    `skip_endpoints`：`skip_filter.load_skip_endpoints()` 的原始輸出。
    **這裡對 `skip_endpoints` 做無條件、endpoint 層級的排除，跟下面
    method 層級的 `known_methods` 檢查是兩道各自獨立生效的關卡**——對應
    04a 五章「skip endpoint 的絕對排除保證」：`method_id` 不含參數簽名
    （javalang 不做 overload resolution，見三章「決策」），若同一 class
    內有兩個同名多載方法各自掛不同 HTTP method 的 route（如
    `FileController` 的 `voice`／`image`，一個 `@PostMapping`、一個
    `@GetMapping`），且其中一個是 skip、另一個不是，兩者會共用同一個
    `method_id`——`compute_excluded_methods()` 的「共用方法會被保護」
    規則這時會誤判成「這個方法也被非-skip endpoint 使用，不該排除」，
    讓人工明確排除的那個 endpoint 透過 `known_methods` 檢查悄悄復活。
    人工標記 skip 是對這一個 endpoint 下的絕對判斷，不該因為底層 method_id 共用
    被覆蓋，因此直接、無條件排除 `skip_endpoints` 裡的每一個
    endpoint_key，不透過 method_id 這層間接關係。這道排除只影響
    `api_to_python_target`，不影響 `module_list`——共用 method_id 的另一
    個非-skip 分支的方法描述，仍依既有規則正常留在 `module_list`
    （`compute_excluded_methods()` 對這類碰撞會記警告，見八章）。

    `ApiMapping` 不含 Java→Python 的檔案/函式對應——那唯一權威來源是
    ③ 的 `python_structure.interfaces`，① 沒有可靠依據能替每個方法猜出
    實際落點，這裡只負責「這個 Java controller method 最終歸屬哪個
    module」（見 04a 六章）。
    """
    # (class_name, java_method) 有沒有出現在 drafts 裡，用來判斷這個方法
    # 是否留在最終 module_list（沒被 skip 呼叫鏈排除、也確實被 Reduce 分進某個 module）
    known_methods: set[tuple[str, str]] = {
        (dm.class_name, dm.method["java_method"])
        for d in drafts
        for dm in d.methods
    }

    result: list[ApiMapping] = []
    for endpoint_key, method_ids in project.route_index.items():
        if endpoint_key in skip_endpoints:
            continue  # 人工明確排除，endpoint 層級絕對排除，見本函式 docstring
        http_method, _, path = endpoint_key.partition(" ")
        for mid in method_ids:
            _, class_name, method_name = mid.split("::")
            module = class_to_module.get(class_name)
            if module is None or (class_name, method_name) not in known_methods:
                continue  # 未歸屬任何模組，或方法已被排除（見本函式 docstring）
            result.append(
                ApiMapping(
                    endpoint=path,
                    http_method=http_method,
                    java_controller=f"{class_name}.{method_name}",
                    module=module,
                )
            )
    return result


def finalize_module_list(drafts: list[_ModuleDraft]) -> list[ModuleInfo]:
    """剝除 `_ModuleDraft` 只有內部組裝過程需要的 `file_path`，產出符合
    `graph/state.py` `ModuleInfo` 型別的最終輸出（`class_name` 已經在
    `dm.method` 裡，不需要額外剝除，見本節前言）。必須在
    `filter_excluded_methods()`／`assemble_api_mapping()` 都跑完之後才
    呼叫——這兩者都依賴草稿階段保留的 `file_path`。
    """
    return [
        ModuleInfo(
            module=d.module,
            summary=d.summary,
            java_files=d.java_files,
            depends_on=d.depends_on,
            methods=[dm.method for dm in d.methods],
        )
        for d in drafts
    ]
```

### 7.4 對外入口：`run_map_reduce()`

`trivial_class_names`（`grouping.classify_trivial_classes()`，見四章）判定不需要送 Map 的類別，從 4a／4b 實際送出的批次裡剔除，省下對應的 API 呼叫；但這些類別若真的被某個 Controller 依賴到，仍要透過 `_mechanical_summary()` 補一份機械摘要，混進 Map 結果一起送進 Reduce——不這樣做的話，這些類別連 Reduce 都看不到，`module_list.java_files` 又會漏掉它們，等於把四章一開始想解決的完整性問題重新引入。

```python
# parse_agent/summarize.py（續）

_DERIVED_QUERY_RE = re.compile(
    r"^(?P<verb>find|read|get|query|stream|count|exists|delete|remove)By(?P<condition>[A-Z].*)$"
)
_LOOSE_DERIVED_QUERY_RE = re.compile(r"^(find|read|get|query|stream|count|exists|delete|remove)\w*By[A-Z]")
_VERB_DESCRIPTIONS = {
    "find": "查詢", "read": "查詢", "get": "查詢", "query": "查詢", "stream": "查詢",
    "count": "計數", "exists": "判斷是否存在", "delete": "刪除", "remove": "刪除",
}
_AND_OR_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(And|Or)(?=[A-Z])")
_CAMEL_WORD_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _describe_derived_query(method_name: str) -> str | None:
    """Spring Data JPA 衍生查詢方法命名慣例的機械解析，供
    `_describe_bodyless_method()` 使用。**刻意保守，不窮舉所有 Spring
    Data 關鍵字**（`Top`／`Distinct`／`OrderBy`／`GreaterThan`／`Like`
    等運算子一律不嘗試解析）：只處理「動詞 + By + 欄位條件（僅
    And／Or 連接）」這個最常見、最沒有歧義的形狀，欄位名稱本身一定
    正確（直接來自方法簽名，不是猜的），只是不逐一判讀運算子語意。
    解析不出這個嚴格形狀、但外觀確實像衍生查詢方法（`_LOOSE_DERIVED_
    QUERY_RE` 命中）時，回傳一句誠實的通用說明，不假裝完整解析出欄位
    條件——寧可少講一點，也不要把 `Top3`／`OrderBy` 這類非欄位關鍵字誤
    當成欄位名稱拆進描述裡，見 05a／04a 反覆強調的「不確定就不要假裝
    知道」精神在方法層級的延伸。
    """
    strict = _DERIVED_QUERY_RE.match(method_name)
    if strict is not None:
        verb_desc = _VERB_DESCRIPTIONS[strict.group("verb")]
        segments = [s for s in _AND_OR_SPLIT_RE.split(strict.group("condition")) if s not in ("And", "Or")]
        fields = "、".join(_CAMEL_WORD_SPLIT_RE.sub(" ", s).strip() for s in segments)
        return (
            f"Spring Data 衍生查詢方法，依 {fields} 欄位條件{verb_desc}"
            "（由方法名稱依 Spring Data JPA 命名慣例機械解析，未逐一判讀運算子如 GreaterThan／Like 等）"
        )
    if _LOOSE_DERIVED_QUERY_RE.match(method_name):
        return (
            f"Spring Data 衍生查詢方法（方法名稱 `{method_name}` 含 Top／Distinct／OrderBy 等複雜關鍵字，"
            "機械解析僅辨識出這是衍生查詢方法，實際條件請參照方法簽名，未逐一拆解欄位）"
        )
    return None


def _describe_bodyless_method(method: MethodEntry) -> tuple[str, str]:
    """機械描述一個沒有方法本體的方法（見 `grouping.needs_llm_summary()`
    「`has_implementor=False` 且方法清單全部沒有本體」分支）：LLM 讀到的
    資訊跟這裡機械解析用的完全一樣（只有方法名稱／`@Query` annotation
    可用），沒有理由讓 LLM 猜，猜的結果也不會比機械解析更可靠。依信心
    高低分三層：

    1. **有 `@Query` 字面字串** → 直接抄錄，比任何摘要都精確
    2. **符合 Spring Data 衍生查詢命名慣例**（見 `_describe_derived_
       query()`）→ 機械拆解欄位條件
    3. **兩者都不是** → 誠實占位，明講「無法機械推斷語意，需人工核對」
       ，不假裝知道，`complexity` 用 `"medium"` 標記需要多留意（而非
       跟前兩層一樣的 `"low"`），呼應「不確定就不要假裝知道」精神。
    """
    if method.query_value:
        return f"自訂查詢（`@Query`）：{method.query_value}", "low"
    derived = _describe_derived_query(method.name)
    if derived is not None:
        return derived, "low"
    return (
        "抽象方法，無方法本體，且無法從命名慣例或 @Query 機械推斷語意，建議人工核對實際語意",
        "medium",
    )


def _mechanical_summary(class_info: ClassInfo) -> MapClassResult:
    """對 `grouping.classify_trivial_classes()` 判定不需要送 Map 的類別，
    機械組出一份佔位摘要，不呼叫 Claude API。目的單純是完整性：讓這個
    類別仍然能透過 Reduce 被分進某個 module、出現在
    `module_list.java_files`，不是真的做了語意摘要，`summary` 內容本身
    要老實反映這件事，不偽裝成 LLM 產出的摘要。

    `needs_llm_summary()` 有兩種不同理由判定「不需要 LLM」，`methods`
    的機械組裝方式因此不同：

    - **存取器方法**（`get*`／`set*`／`is*`／`equals`／`hashCode`／
      `toString`）：略過，不產生任何 `MapMethodResult`——這些方法多半是
      Lombok／手寫 getter/setter，個別拆成 [P] 的 task 去翻譯沒有意義，
      Python 端通常用 dataclass／SQLAlchemy model 的屬性表達。
    - **無方法本體、非存取器的方法**（`needs_llm_summary()` 規則 2，如
      Spring Data JPA 衍生查詢方法）：透過 `_describe_bodyless_method()`
      機械組出真正有意義的描述，不是空清單——這批方法是真實業務行為，
      只是不需要（也不該）讓 LLM 猜。
    - **其餘情況**（理論上不該出現在這裡：有本體又不是存取器的方法，
      代表會落在規則 3／4，本來就該送 Map，不會被判定為 trivial）：
      防禦性地記一筆 warning 並略過，不中止——真的發生代表分類邏輯本身
      有 bug，需要回頭檢查 `needs_llm_summary()`，不是這個函式能修正的。
    """
    annotation_note = "、".join(class_info.annotations) or "無 Lombok/JPA 標記"
    methods: list[MapMethodResult] = []
    for m in class_info.methods:
        if _ACCESSOR_METHOD_RE.match(m.name):
            continue
        if not m.has_body:
            description, complexity = _describe_bodyless_method(m)
            methods.append(MapMethodResult(m.name, description, complexity))
            continue
        logger.warning(
            "%s.%s 被 needs_llm_summary() 判定為不需要 LLM 摘要，但有方法本體且非存取器方法，"
            "理論上不該發生（見 _mechanical_summary() docstring「其餘情況」），已略過此方法",
            class_info.class_name, m.name,
        )
    return MapClassResult(
        class_name=class_info.class_name,
        summary=(
            f"機械判定不需要 Claude API 摘要（{annotation_note}），未呼叫 LLM"
            "（見 grouping.needs_llm_summary()）。"
        ),
        methods=methods,
        cross_group_dependency_hints=[],
    )


def run_map_reduce(project: ParsedProject) -> tuple[list[_ModuleDraft], dict[str, str]]:
    """對應 04a 四章全節：串接分組（grouping.py）、4a→4b 兩階段 Map（見
    「4a 在 4b 之前完成」）、Reduce、輸出組裝。回傳排除 skip 呼叫鏈之前
    的 `(module_drafts, class_to_module)`——skip 排除交給呼叫端（見
    `parse_agent/__init__.py`）在這之後才做，因為排除計算（五章）跟
    Map/Reduce（四章）是兩條互不相依的資料流，分開呼叫比較清楚；回傳
    草稿型別而非 `ModuleInfo`，讓呼叫端能先做 `filter_excluded_methods()`
    ／`assemble_api_mapping()`，最後才呼叫 `finalize_module_list()`
    （見 7.3 說明、`parse_agent/__init__.py` 的完整組裝順序）。
    """
    controller_deps = controller_dependency_closure(project)
    shared_class_names = find_shared_classes(controller_deps)
    trivial_class_names = classify_trivial_classes(project)

    shared_units = build_shared_class_units(project, shared_class_names - trivial_class_names)
    map_results_4a = run_map_phase_with_retry(shared_units)
    shared_summaries = {r.class_name: r.summary for r in map_results_4a}

    controller_units = build_controller_units(
        project, controller_deps, shared_class_names, shared_summaries, trivial_class_names
    )
    map_results_4b = run_map_phase_with_retry(controller_units)

    reachable_classes = {c for deps in controller_deps.values() for c in deps}
    mechanical_results = [
        _mechanical_summary(project.classes[name])
        for name in sorted(trivial_class_names & reachable_classes)
        if name in project.classes
    ]

    all_map_results = map_results_4a + map_results_4b + mechanical_results
    reduce_result = _reduce_phase(all_map_results, controller_deps)

    return _assemble_module_drafts(all_map_results, reduce_result, project)
```

---

## 八、`skip_filter.py`——skip 呼叫鏈排除

對應 04a 五章全節；`openapi_spec` 是 ① 的輸入之一，見 04a 二章。

**「非-skip 全集」的來源是 `openapi_spec["paths"]`，不是 `route_index.keys()`**——`route_index` 是三章從 Java 原始碼機械掃出的索引，annotation 引用非字面字串常量、或 route 標在 interface 方法上等情況（見三章步驟 5、十一章已知限制）會讓部分實際存在的 route 完全不出現在 `route_index` 裡；如果拿 `route_index` 自己當「全集」，這些消失的 route 不會產生任何「查無對應」警告——因為根本沒有外部基準可以比對出「少了什麼」。這不只是精準度問題：若消失的剛好是一個合法的非-skip endpoint，而它呼叫到的方法又同時被某個 skip endpoint 的呼叫鏈碰到、且沒有其他非-skip 路徑能到達，`excluded = skip_reachable − non_skip_reachable` 會把這個方法**誤判為只服務 skip、實際上仍在被使用**，正是 04a 三章設計原則要防的「不可逆的錯誤」。

`openapi_spec["paths"]`（[A] Spec Agent 對著實際跑起來的 Java 服務取得，不是靜態分析，因此沒有 `route_index` 的漏掃問題）是「全集」來源，`route_index` 只當 `endpoint_key → method_id` 的查表工具。任何 route 若因為三章的解析限制而沒有進 `route_index`，不論它屬於 skip 組還是非-skip 組，都會在查表時觸發同一套「查無對應」warning（見下方 `_endpoints_to_method_ids()`），偵測機制對兩組對稱生效。`openapi_spec` 是 `RefactorState` 既有欄位（[A] Spec Agent 產出，見 `graph/nodes/spec_node.py`），① 作為 graph node 直接讀 `state["openapi_spec"]` 即可，不需要額外讀檔或呼叫 API，也不影響「① 不解析 request/response schema」這條原則（見 04a 二章備註）——這裡只用 `paths` 的 key 列舉 endpoint 字串。

```python
# parse_agent/skip_filter.py
"""① 解析 Agent：skip 呼叫鏈排除，對應 04a 五章全節。「非-skip 全集」
來源見本節前言——openapi_spec["paths"]，不是 route_index.keys()；
route_index 只當 endpoint_key → method_id 的查表工具。
"""
from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path

from parse_agent.types import MethodId

logger = logging.getLogger(__name__)

# 跟 spec_collection_agent/chain_dependency_detect.py 的 _HTTP_METHODS
# 同一份 OpenAPI paths 物件列舉邏輯，但不跨套件 import——各 Agent 套件
# 自我封裝、不互相依賴，見 06 章 llm.py 的既有慣例。
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def load_skip_endpoints(unfilled_endpoints_path: Path) -> list[str]:
    """讀 `postman/unfilled_endpoints.json`（[B] Collection Agent 已定案
    的輸出，見 04a 二章），取 `category == "skip"` 的項目（見
    `spec_collection_agent/value_filler.py` 的 unfilled 清單格式：
    `{"endpoint": ..., "category": "skip"|"retry", "detail": ...}`）。
    檔案不存在時視為沒有任何 skip（空清單），不拋例外——理論上 ① 排在
    [B] 之後執行（見 04a 二章），這份檔案一定已經存在；防禦性處理只是
    避免單元測試或手動單獨執行 ① 時因為缺這個檔案而整段失敗。
    """
    if not unfilled_endpoints_path.exists():
        logger.warning("找不到 %s，視為沒有任何 skip endpoint", unfilled_endpoints_path)
        return []
    entries = json.loads(unfilled_endpoints_path.read_text(encoding="utf-8"))
    return [e["endpoint"] for e in entries if e.get("category") == "skip"]


def _all_endpoints_from_openapi(openapi_spec: dict) -> list[str]:
    """把 `openapi_spec["paths"]` 展開成 `{HTTP_METHOD} {path}` 字串清單，
    當作 04a 五章「非-skip 全集」的來源（見本節前言）。`path` 直接沿用
    OpenAPI 的樣板格式（如 `/api/users/{id}`），跟 `unfilled_endpoints.json`
    的 `endpoint` 欄位、三章 `route_index` 的 key 是同一種格式——
    `spec_collection_agent/postman_tree.py` 的 `normalized_path_from_item()`
    就是為了把 Postman `:id` 風格路徑換回這種格式、對回
    `openapi_spec["paths"]`，見該函式 docstring，這裡不需要再處理一次
    轉換。非 HTTP method 的 path item 欄位（如 `parameters`／`summary`）
    直接過濾掉，不當成 route。
    """
    endpoints: list[str] = []
    for path, path_item in (openapi_spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in path_item:
            if method.lower() not in _HTTP_METHODS:
                continue
            endpoints.append(f"{method.upper()} {path}")
    return endpoints


def _endpoints_to_method_ids(
    endpoints: list[str], route_index: dict[str, list[MethodId]], *, group: str
) -> set[MethodId]:
    """對應 04a 五章步驟 1：把 endpoint 字串查表轉成 method_id 起點集合。
    查到多個 method_id（route_index 值本身就是清單）全部視為起點，沿用
    「多連、少排除」原則（見 04a 五章步驟 1 第二點）。`group`（`"skip"`
    或 `"非-skip"`）只供下面 warning log 標明是哪一組查無對應，不影響
    排除邏輯本身——兩組都會走這裡，查無對應時的警告因此對稱生效（見
    本節前言）。
    """
    method_ids: set[MethodId] = set()
    unmatched: list[str] = []
    for ep in endpoints:
        ids = route_index.get(ep)
        if not ids:
            unmatched.append(ep)
            continue
        method_ids.update(ids)
    if unmatched:
        logger.warning(
            "%s endpoint 在 Controller Route 索引中查無對應 method_id，"
            "略過（不貢獻任何排除起點，見 04a 五章步驟 1 第三點）: %s",
            group,
            unmatched,
        )
    return method_ids


def _bfs_reachable(starts: set[MethodId], call_graph: dict[MethodId, set[MethodId]]) -> set[MethodId]:
    """對應 04a 五章步驟 2：從一組起點沿呼叫圖做可達性分析（BFS，圖可能
    有環——同一個方法互相遞迴呼叫的情況，`visited` 集合天然防止重複
    展開，不需要額外處理環偵測）。
    """
    visited: set[MethodId] = set()
    queue: deque[MethodId] = deque(starts)
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for callee in call_graph.get(current, ()):
            if callee not in visited:
                queue.append(callee)
    return visited


def compute_excluded_methods(
    *,
    skip_endpoints: list[str],
    openapi_spec: dict,
    route_index: dict[str, list[MethodId]],
    call_graph: dict[MethodId, set[MethodId]],
) -> set[MethodId]:
    """對應 04a 五章步驟 1～4 全流程，回傳最終要排除的 method_id 集合
    （= skip 可達集合 − 非-skip 可達集合）。孤立方法（兩個集合都沒碰到
    的方法）不會出現在回傳集合裡——`excluded` 只從 `skip_reachable` 扣
    掉 `nonskip_reachable`，孤立方法從未進過 `skip_reachable`，因此
    「預設保留」（04a 五章步驟 4）是這個計算方式的自然結果，不需要
    額外的特判分支。

    `openapi_spec`：`RefactorState.openapi_spec`，[A] Spec Agent 產出，
    這裡是「非-skip 全集」的來源，不是 `route_index`（見本節前言）。
    """
    skip_set = set(skip_endpoints)
    non_skip_endpoints = [ep for ep in _all_endpoints_from_openapi(openapi_spec) if ep not in skip_set]

    skip_starts = _endpoints_to_method_ids(skip_endpoints, route_index, group="skip")
    non_skip_starts = _endpoints_to_method_ids(non_skip_endpoints, route_index, group="非-skip")

    skip_reachable = _bfs_reachable(skip_starts, call_graph)
    non_skip_reachable = _bfs_reachable(non_skip_starts, call_graph)

    excluded = skip_reachable - non_skip_reachable
    if excluded:
        logger.info("skip 呼叫鏈排除：%d 個方法只能從 skip endpoint 到達，將不會出現在 module_list", len(excluded))

    # skip_starts 與 non_skip_starts 的交集：某個 method_id 同時是 skip
    # endpoint 與非-skip endpoint 的直接繫結目標。這只可能發生在同一個
    # class 內有多個同名多載方法、各自掛不同 HTTP method 的 route（如
    # FileController 的 voice/image，見 04a 五章「skip endpoint 的絕對
    # 排除保證」）——method_id 不含參數簽名，兩個不同的物理方法被誤判成
    # 同一個方法，讓「共用方法會被保護」規則把 skip 的那個分支也保護
    # 下來。這個方法不會進 excluded（module_list 仍會保留它的描述），但
    # 對應的 skip endpoint 本身在 assemble_api_mapping()（七章）有獨立的
    # 絕對排除保證，不受這裡的判斷影響——這裡只負責記警告，讓 module_
    # list 裡這個方法的描述（可能混雜了 skip 分支的行為）能被人工核對到。
    protected_by_sharing = skip_starts & non_skip_starts
    if protected_by_sharing:
        logger.warning(
            "%d 個方法同時是 skip 與非-skip endpoint 的直接繫結目標（多載"
            "方法同名導致 method_id 共用，見 04a 三章「決策」）：這個方法"
            "在 module_list 的描述可能混雜了 skip 分支的行為，對應的 skip "
            "endpoint 本身已由 assemble_api_mapping() 絕對排除，但方法"
            "描述本身建議人工核對: %s",
            len(protected_by_sharing),
            sorted(protected_by_sharing),
        )

    return excluded
```

---

## 九、`__init__.py`——對外唯一入口

對應 04a 七章「對外唯一入口，供 `graph/nodes/parse_node.py` 呼叫，node 本身不直接碰觸上述任何細節」。串起三～六章全部流程：呼叫圖建構 → Map/Reduce → skip 排除 → 輸出組裝，比照 `spec_collection_agent/__init__.py`／`run_collection_agent()` 的慣例。

```python
# parse_agent/__init__.py
"""① 解析 Agent 對外唯一入口，`graph/nodes/parse_node.py` 只呼叫這裡的
函式（見 04a 七章）。串接順序：call_graph（三章，機械分析）→
summarize.run_map_reduce（四章，Claude API）→ skip_filter（五章，機械
分析，可以跟四章平行做，但兩者都很快，這裡選擇簡單的循序寫法，不為了
省幾秒鐘增加併發複雜度）→ 輸出組裝收尾（六章）。
"""
from __future__ import annotations

from pathlib import Path

from graph.state import ApiMapping, ModuleInfo
from parse_agent import skip_filter, summarize
from parse_agent.call_graph import parse_java_project


def run_parse_agent(
    *, java_project_path: str, unfilled_endpoints_path: Path, openapi_spec: dict
) -> tuple[list[ModuleInfo], list[ApiMapping]]:
    """對應 04a 全文：解析 Java 專案，輸出 `(module_list,
    api_to_python_target)`，直接對應 `RefactorState` 的
    `module_list`／`api_to_python_target` 兩個欄位（見 04a 六章）。

    `openapi_spec`：`RefactorState.openapi_spec`，[A] Spec Agent 產出，
    供 `skip_filter` 判定「非-skip 全集」使用（見 04a 二章、04b 八章，
    不是新增的 Claude API 呼叫或檔案讀取，單純從 state 轉傳）。
    """
    project = parse_java_project(java_project_path)

    module_drafts, class_to_module = summarize.run_map_reduce(project)

    skip_endpoints = skip_filter.load_skip_endpoints(unfilled_endpoints_path)
    excluded_methods = skip_filter.compute_excluded_methods(
        skip_endpoints=skip_endpoints,
        openapi_spec=openapi_spec,
        route_index=project.route_index,
        call_graph=project.call_graph,
    )
    filtered_drafts = summarize.filter_excluded_methods(module_drafts, excluded_methods)

    api_to_python_target = summarize.assemble_api_mapping(project, class_to_module, filtered_drafts, set(skip_endpoints))
    module_list = summarize.finalize_module_list(filtered_drafts)

    return module_list, api_to_python_target
```

---

## 十、`graph/nodes/parse_node.py`——LangGraph node 實作

比照 `spec_node.py`／`collection_node.py` 的慣例：`run_parse_agent()` 內部是同步阻塞呼叫（javalang 掃描＋多次 Claude API 呼叫，且 Map 重試佇列的 5 分鐘等待，見 04a 四章），丟到執行緒跑，避免卡住事件迴圈。

```python
# graph/nodes/parse_node.py
"""
① 解析 Agent（Claude API）
輸入：Java 專案路徑、[B] Collection Agent 已定案的 postman/unfilled_endpoints.json、
     state 既有的 openapi_spec（供 skip_filter 判定「非-skip 全集」，見 04a 二章、04b 八章）
輸出：module_list, api_to_python_target
見 04a_parse_agent_architecture.md、04b_parse_agent_code.md

排在 gen_collection（[B] Collection Agent 階段二）之後，見 04a 二章、
01 五章「parse（① 解析 Agent）排在 [B] 之後」；這個順序同時也是
openapi_spec 一定已經在 state 裡的前提——parse 排在 extract_spec（[A]）
之後，state["openapi_spec"] 這時必定已經填好。parse 完成後平行觸發
record_tests（②）與 design（③），③ 需要 module_list／api_to_python_target
（本節點的輸出）＋ openapi_spec 才能設計 Python 結構，見 05a 二章。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from graph.state import RefactorState
from parse_agent import run_parse_agent


async def run(state: RefactorState) -> RefactorState:
    # run_parse_agent() 內部是同步阻塞呼叫（javalang 掃描＋多次 Claude
    # API 呼叫，且 Map 重試佇列的 5 分鐘等待，見 04a 四章），丟到執行緒
    # 跑，避免卡住事件迴圈（與 spec_node.py／collection_node.py 做法一致）。
    module_list, api_to_python_target = await asyncio.to_thread(
        run_parse_agent,
        java_project_path=state["java_project_path"],
        unfilled_endpoints_path=Path("postman") / "unfilled_endpoints.json",
        openapi_spec=state["openapi_spec"],
    )

    return {
        **state,
        "module_list": module_list,
        "api_to_python_target": api_to_python_target,
    }
```

---

## 十一、已知限制與待驗證事項

04a 十章已定案、在 04b 落地為具體行為的四項（`@RequestMapping` 非字面值解析、`@Qualifier` 消歧失敗頻率、4a 批次拆分門檻、Map/Reduce 重試仍失敗後的策略）見三章各處註解、四章 7.1 `run_map_phase_with_retry()`，不在這裡重複列出。以下是實作過程中浮現、04a 沒有點名的實作層級限制。除非特別註明，「目標專案」均指 `lang-exam-api-refactor`；「沒有觸發」不等於「已證明沒問題」，只代表這個專案剛好沒踩到，換一個專案仍可能踩到，不可因此刪除：

- **Repository interface 的 `default`／`static` method body 不會被追蹤**（見三章 3.1 `_extract_interfaces()`，設計見 04a 四章「Repository interface 的補充掃描」）——這類方法本身會被正確納入 `ClassInfo.methods`（能被 `_yield_call()` 辨識成呼叫目標），但方法自己內部呼叫的東西不會被追蹤（`_build_call_graph()` 只走 `ClassDeclaration`，不含 interface）。Spring Data Repository 極少用這個寫法，目標專案沒有觸發，維持既有「連結留白、預設保留」的安全方向，留待接上真的用到這個寫法的專案再評估。
- **鏈式呼叫解析（三章 3.3 `_resolve_qualifier_string()`／`_continue_chain()`／`_walk_and_resolve()`）是新寫的遞迴演算法，語意解析的精確度還沒有真實案例驗證過**——javalang 能成功 parse 只證明語法層沒問題，不代表「呼叫圖的語意解析（誰呼叫誰）精不精確」也對。這個演算法假設了 javalang 對純欄位存取鏈（沒有方法呼叫打斷）會把它折成點號字串塞進 `qualifier`、只在遇到方法呼叫時才展開成巢狀 `.selectors`，以及 `this.x.y()` 的 `"this"` 一定出現在 qualifier 字串最前面，第一次接上真實專案後應該優先檢查呼叫圖的連結數量是否合理（多連比漏連安全，但連結數量若跟 class 數量、方法數量的比例明顯失真，可能代表這裡對 javalang 節點結構的假設有誤，需要對照當時安裝的 javalang 版本原始碼核對）。選擇器層級（`.selectors` 內）的節點若自己還帶非空 `qualifier`（javalang 通常不會這樣產生），目前直接視為無法解析，不強行模擬，也是同一類尚待驗證的邊界情況。
- **`ParsedProject.classes` 用 class 簡單名稱當 key，不處理跨檔案同名類別**（見二章 `types.py` 前言）——沒有觸發，真的遇到時會退化成保守全連結，不會直接壞掉，但精準度下降。
- **欄位型別只展開單一型別參數的集合容器**（見二章 `FieldInfo` docstring、三章 `_resolve_declared_type()`）——`List<XxxService>`／`Set<XxxService>`／`Optional<XxxService>` 這類寫法已能正確解析出 `XxxService`；`Map<K, V>`、自訂多參數泛型仍只取外層型別名稱，內層型別參數不展開，因為「哪個型別參數才是真正的依賴目標」在雙參數以上沒有一致慣例可循。
- **`implements` 用完整 package 路徑寫法時，`_extract_classes()` 抓到的介面名稱會錯**（見三章 3.1 `implements=[t.name for t in (class_decl.implements or [])]`）——javalang 把 `implements com.example.iface.MyInterface` 解析成巢狀 `ReferenceType`／`sub_type` 鏈，最外層 `.name` 只是路徑第一段（`"com"`），真正的介面簡單名稱被埋在鏈的最深處，直接取 `.name` 會抓到 `"com"` 而不是 `"MyInterface"`。影響有界：抓錯的介面名稱在 `build_interface_implementors()` 對不上專案內任何 class，會落入既有的「完全無法解析」保守分支（連結留白，不誤排除），不會 crash，只是精準度下降。目標專案用 `import` 搭配簡短型別名稱寫 `implements`，沒有觸發這個情況，暫不需要為此特別解析巢狀 `sub_type` 鏈還原完整名稱。
- **`_extract_classes()`／`_build_call_graph()`（三章 3.1／3.3）只掃 `tree.types`（頂層 class 宣告），不含巢狀 inner class**——inner class 完全不會出現在 `ParsedProject.classes`／呼叫圖裡，其獨有的方法（沒有被外層 class 的方法呼叫，或呼叫鏈解析不到）不會被摘要、也不會進 `module_list`。這不是新增的風險，是原本「完全無法解析時連結留白、預設保留」設計（04a 三章「多連、少排除」）的自然延伸，只是這裡連 class 本身都沒被建立，不算排除、只是從未被納入。目標專案沒有用 inner class 承載業務邏輯的情況，維持現況已足夠；若目標專案大量用 inner class 承載業務邏輯（而非純資料結構如 Builder／DTO），需要另外評估是否要把 inner class 當成獨立分析單位。
- **`_class_source_payload()`／`_class_source_chars()` 假設「每個 `.java` 檔案只有一個 top-level class」**（見七章 7.1、四章 `_class_source_chars()`）——目標專案符合這個假設；這裡指的是同一份檔案裡有多個「平行」的 top-level class（Java 語法允許，只是慣例上少見，跟上面 inner class 是不同情況：`tree.types` 本來就會列出所有平行 top-level 宣告）。若目標專案真的有這種寫法，Map 階段送出的原始碼會包含「這批要分析的 class」以外的其他 class 定義，模型輸出的 `class_name` 需要靠 7.1 `_map_analyze_batch()` 的缺漏檢查兜底，不會直接造成錯誤，但可能讓摘要品質下降或多花 token。
- **Reduce 是單次呼叫，沒有切分**（見七章 7.2）——04a 四章沒有像 Map 階段那樣討論 Reduce 的輸入規模上限；如果 Map 階段產出的 class 數量非常多（濃縮後的候選仍然是全專案規模），單次 Reduce 呼叫的 payload 可能過大。目標專案規模不會觸發這個問題，但比它大上一個數量級的專案可能需要，留待接上更大規模的真實專案評估。
- **`_extract_routes()`（三章 3.4）只掃描 Controller 具體類別自己方法上的 annotation，沒有處理 route annotation 標在「Controller 實作的 interface」方法上、具體類別覆寫時沒有重複標注的寫法**——Spring MVC 允許把 `@RequestMapping`／`@GetMapping` 等標在 interface 方法上，執行期 Spring 會正確解析並生效，但 javalang 靜態掃描只看 `class_decl.methods` 上的 annotation，不會去追具體類別實作的 interface 方法是否也帶著 annotation，這種端點會整個從 `route_index` 消失。這類消失的端點如果不是 skip endpoint，會在非-skip 組查表時觸發「查無對應」warning（見八章 `_endpoints_to_method_ids()` 的 `group` 參數），至少能從 log 發現「這個端點沒被正確索引」；但 warning 不能讓這個端點自己重新變成合法的 BFS 起點——它依然不會貢獻任何非-skip 可達性，若它呼叫到的方法剛好也被某個 skip endpoint 的呼叫鏈碰到、且沒有其他非-skip 路徑能到達，那個方法理論上仍可能被誤判排除，需要靠 warning log 事後人工核對呼叫鏈。目標專案沒有觸發這個情況，暫不需要為此擴充 `_extract_routes()` 去解析 interface 繼承鏈；但下次接上新的目標專案時，若 log 出現非-skip 組的查無對應警告，需要人工檢查對應方法是否被錯誤排除。
- **`@RequestMapping` 沒有指定 `method` 屬性時，Spring 實際語意是「回應所有 HTTP method」，但 `_extract_request_mapping_methods()`（三章 3.4）目前回傳空清單、記警告、不索引任何 HTTP method**（見三章 3.4 該函式 docstring）——這種端點會整個從 `route_index` 消失，影響跟上一項「interface 方法上的 route annotation」同一等級：非-skip 組查表時會觸發「查無對應」warning，可從 log 發現，但無法讓端點自己變回合法的 BFS 起點。這種不指定 method 的寫法在 REST controller 較罕見，目標專案沒有觸發，暫不處理。
- **`module_list` 對「同名多載方法、其中一個 route 是 skip」這種情況仍有精準度限制，`api_to_python_target` 沒有這個問題**（見七章 3.3 `assemble_api_mapping()`、八章 `compute_excluded_methods()`）——`api_to_python_target` 已對 `skip_endpoints` 做無條件的 endpoint 層級排除；但 `module_list` 那個方法的描述（Map 階段對這個 `method_id` 產出的所有 `MethodInfo` 條目）仍會被「共用方法會被保護」規則整批保留，可能同時混著 skip 分支與非-skip 分支的行為描述，沒有欄位能區分兩者。`compute_excluded_methods()` 對這種 method_id 碰撞會記警告（見八章），但只是提示、不會自動修正描述內容，需要人工核對；徹底解決需要在 `route_index`／Map 輸出裡引入 per-route（而非 per-method-name）的識別，屬於比目前規模更大的設計變更，暫不處理。
- **Import 依賴掃描（三章 `_extract_project_imports()`，設計見 04a 四章「Import 依賴補充」）只認 import 陳述式，同套件內不需要 import 就能互相參照的情況（Java 語言特性）仍解析不到**——目標專案的跨層依賴一律跨 package、都有明確 import，沒有觸發；留待接上真的有這種寫法的專案再評估是否需要另外掃套件宣告＋目錄結構比對。
- **`needs_llm_summary()`／`classify_trivial_classes()`（四章，設計見 04a 四章「Map 摘要必要性判斷」）的分類是啟發式，不是完美判斷**——只有「無實作類別的介面、方法清單全部沒有本體」（Spring Data Repository 典型形狀）與「Lombok／JPA 資料類別標記＋方法清單只有存取器方法」這兩種情況跳過 Map，其餘一律送 Map，不確定時偏向保守（多送不少判斷少）。已知一個無害的邊界情況：只有建構子、沒有一般方法的例外類別（如自訂 `XxxException`），Map 有時會回傳一個跟類別同名的偽方法（把建構子描述當成方法）——`_normalize_method_name()` 剝掉多載消歧後綴後仍對不上 javalang 掃描出的真實方法清單時判定這筆方法不存在，`_normalize_class_methods()` 直接排除、不進最終 `module_list.methods`，只記一筆 warning 供人工核對，不需要額外處理。
- **Spring Data 衍生查詢命名慣例解析（`_describe_derived_query()`，七章）只認「動詞緊接 `By`」這個嚴格形狀**——`findAllBy`／`findFirstBy` 這類動詞與 `By` 之間夾了修飾詞的寫法，會退回「認得出是衍生查詢方法、但不逐一拆解欄位」的寬鬆說明；這個寬鬆說明的文字固定引用「`Top`／`Distinct`／`OrderBy` 等複雜關鍵字」，但實際觸發原因不一定包含這些關鍵字，只是文字上不夠精確，不影響正確性（仍然是誠實的「無法逐一拆解」）。已對真實 `lang-exam-api-refactor` 專案驗證過，10 個真實 Repository 方法（`findByGradeAndLocal`／`findTopByKindOrderByIdDesc`／`findAllByTypeNumberAndPartNumberAndQuestionNumber` 等）都正確產出可用描述，沒有觸發解析錯誤或程式例外。
