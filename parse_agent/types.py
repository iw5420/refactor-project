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


@dataclass
class RouteDecl:
    """單一 method 上的 route 宣告，三章步驟 5 的中繼資料。"""

    method_name: str
    http_method: str
    paths: list[str]  # 該 method 上的 path（可能多個），還沒接 base path／context-path


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
    routes: list[RouteDecl] = field(default_factory=list)  # method 層級 route 宣告
    imports: list[str] = field(default_factory=list)  # 這個檔案 import 的簡單類別名稱（非 wildcard、非 static），見 call_graph.py _extract_project_imports()——補足欄位/呼叫圖都解析不到的依賴（靜態呼叫、方法參考等），供 grouping.py controller_dependency_closure() 使用
    annotations: list[str] = field(default_factory=list)  # class 上所有 annotation 名稱（不只 stereotype），供 grouping.py 判斷是否為純資料類別（@Entity/@Data/@Getter/@Setter 等）
    uses_dynamic_query_signal: bool = False  # 這個檔案是否 import 了已知會承載動態查詢邏輯的型別（如 org.springframework.data.jpa.domain.Specification），見 call_graph.py _uses_dynamic_query_signal()


@dataclass
class ParsedProject:
    """三章掃描階段的完整輸出：呼叫圖 + Controller Route 索引 + class
    資訊，是四／五章共用的基礎資料（04a 三章開頭：「只建構一次」）。

    已知限制：`classes` 以 class 簡單名稱為 key，不處理跨檔案同名類別
    ——真實驗證過的目標專案（`lang-exam-api-refactor`，見 04a 三章）
    90 個檔案沒有這個情況，這裡不做完整的 import 解析／FQN 消歧（那需要
    額外追蹤每個檔案的 `import` 陳述式、判斷型別參照落在哪個 import，
    複雜度遠超過目前規模需要）。真的遇到同名類別時，`call_graph.py` 的
    欄位型別解析會退化成「這個名字底下所有候選類別」，套用跟 interface
    多實作一樣的 `@Qualifier`／`@Primary` 消歧與保守全連結邏輯，不會直接
    壞掉，只是精準度下降。
    """

    classes: dict[str, ClassInfo]  # key 為 class 簡單名稱，見上方「已知限制」
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
