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

from common.jpa_base_repository import (
    JPA_BASE_METHOD_NAME_MAP,
    detect_jpa_base_entity,
    synthetic_java_method_id,
)
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
        # 這個檔案的 package 是否落在 xxx.utils 底下（見 _is_utils_package()
        # docstring、docs/refactor_bug_trace.md #15），跟 file_imports 一樣
        # 整份檔案共用。
        file_is_utils_class = _is_utils_package(tree.package.name if tree.package is not None else None)
        for class_info in [
            *_extract_classes(tree, rel_path),
            *_extract_interfaces(tree, rel_path),
            *_extract_enums(tree, rel_path),
        ]:
            class_info.imports = file_imports
            class_info.uses_dynamic_query_signal = file_uses_dynamic_query_signal
            class_info.is_utils_class = file_is_utils_class
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


# 已知會承載動態／模糊查詢邏輯的型別（完整 package 路徑），供
# grouping.py 判斷「這個類別要不要送 Map 摘要」時當作特殊情況：不管
# method body 裡的邏輯藏多深（例如包在 lambda 運算式裡），只要用到這個
# 型別，就直接判定「有業務邏輯」，不嘗試用「method body 有沒有控制
# 流程」這種通用啟發式去判斷——那類判斷法對這個型別的典型用法
# （`Specification<T>` 回傳一個 lambda，實際過濾規則寫在 lambda 內部）
# 會誤判成「沒有邏輯」，見 04a 四章相關討論。是一份可擴充的清單，不是
# 窮舉所有可能藏邏輯的型別，之後接上新專案若發現其他同類型別（如
# `org.springframework.data.jpa.domain.Example`），可以直接加進來。
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


def _is_utils_package(package: str | None) -> bool:
    """對應 docs/refactor_bug_trace.md #15：`CollectionUtil`／`CodeUtil`
    這類純靜態工具類，呼叫端一律用「類別名稱直接呼叫」（`ClassName.
    staticMethod()`），不透過欄位注入——`_resolve_qualifier_string()`
    既有的兩個特例（`uses_dynamic_query_signal`／`is_generic_response_
    wrapper`）都是針對特定結構訊號設計，utils 工具類兩者都不符合，這類
    呼叫因此完全不會被記進呼叫圖，[P] 的 `reference_targets` 自然也看
    不到，⑤ 翻譯時只能照 Java 靜態呼叫語法瞎猜 Python 端的呼叫慣例。

    判斷方式沿用 `design_agent/layout.py::is_utils_package()` 已經定案
    的同一個訊號——package 名稱最後一段是不是 `utils`，比「無 stereotype
    ＋全靜態方法」這種行為推斷簡單、可靠得多（見 05a 三章「Utils
    特例」）。`parse_agent`／`design_agent` 是各自獨立套件，不互相
    import（01 二章「設計原則」），這裡是同一個判斷邏輯的獨立副本，不是
    忘記共用——這個邏輯只有一行、極不可能跟 design_agent 那份走向分岔，
    重複維護的成本遠低於為了共用一行邏輯在兩個套件間建立耦合。
    """
    if not package:
        return False
    return package.rsplit(".", 1)[-1] == "utils"


def _is_self_returning_static_factory(class_decl: javalang.tree.ClassDeclaration) -> bool:
    """判斷這個 class 是不是「自我回傳靜態工廠」模式——回應包裝類別
    （`ResponseResult<T>`／`Result<T>` 的 `ok()`／`error()`／`success()`／
    `failure()`）跟 entity 轉換 DTO（`UserProfileRs.fromEntity()`）共有
    的結構特徵：真實案例（見 `ExamController::search()` 呼叫
    `ResponseResult.ok(...)`）證實這類呼叫跟 `ExamSpecification` 這類
    Specification helper 一樣，是不透過欄位注入使用的靜態呼叫，
    `_resolve_qualifier_string()` 靜態呼叫解析（見下方）需要一個訊號
    辨識「這是可以直接靜態呼叫的工廠類別」，才知道要不要放行。

    **判斷條件**：至少有一個 `static` 方法的宣告回傳型別就是這個 class
    自己。**根因訂正（docs/refactor_bug_trace.md #38）**：原本額外要求
    「class 本身宣告泛型型別參數」（`ClassName<T>`），理由是「一般 DTO
    不是泛型類別，也不會有 static 工廠方法回傳自己」——真實案例
    `UserProfileRs.fromEntity(ExamEntity entity): UserProfileRs` 推翻了
    這個假設：它是非泛型 DTO，卻真的有 static 工廠方法回傳自己，導致
    `CandidateController.getCandidate()` 呼叫 `UserProfileRs.fromEntity()`
    這條邊在呼叫圖裡完全消失，⑤ 看不到 `UserProfileRs.java` 建構子的
    真實欄位對應（`this.name = entity.getUserName();`），猜錯成
    `entity.name`（`ExamEntity` 沒有這個欄位），真實觸發
    `AttributeError`。已對整個 Java 專案掃過全部 `public static` 方法
    確認：拿掉泛型限制後，只有 `UserProfileRs` 這一個類別會新命中，
    不會誤判其他任何類別（`GetExamResultRq`／`GetExamResultRs` 這兩個
    名稱帶 `Result` 字樣的一般 DTO 完全沒有 `static` 方法）。
    """
    return any(
        "static" in method.modifiers
        and isinstance(method.return_type, javalang.tree.ReferenceType)
        and method.return_type.name == class_decl.name
        for method in class_decl.methods
    )


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

    **`extends`**：只取父類別簡單名稱（Java 單一繼承，`class_decl.extends`
    是單一 `ReferenceType` 或 `None`，不是清單，跟 `implements` 不同）。
    對應 `docs/09b_bug_trace.md`「新發現：`@MappedSuperclass`（如
    `BaseEntity`）未被任何 module 的 `java_files` 收錄」——沒有這個欄位，
    `grouping.py::_direct_deps()` 完全沒有管道知道「這個 class 繼承了
    哪個父類別」，同套件內的 `extends`（Java 語言特性上不需要 import）
    因此連候選依賴邊都不存在，不是「解析不到」，是從一開始就沒被問過。
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
                extends=class_decl.extends.name if class_decl.extends is not None else None,
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
                is_self_returning_static_factory=_is_self_returning_static_factory(class_decl),
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
    Spring 動態產生的實際邏輯），但比「完全無法解析」更接近事實。

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
                # 對應 docs/refactor_bug_trace.md #16：`extends` 鏈裡若有
                # Spring Data 基底介面（JpaRepository/CrudRepository/
                # PagingAndSortingRepository），記下它的 entity 型別，供
                # `_yield_call()` 合成繼承來的方法呼叫關係。跟上面
                # docstring「這裡不解析 extends 鏈」講的是不同層次的
                # 解析——那句話說的是「不嘗試把 extends 目標當成專案內
                # class 去查表」（JpaRepository 本來就查不到），這裡只是
                # 讀出 extends 引用本身的名稱／泛型引數，不查表，兩者不
                # 衝突。
                jpa_base_entity=detect_jpa_base_entity(decl.extends),
            )
        )
    return result


def _extract_enums(tree: javalang.tree.CompilationUnit, rel_path: str) -> list[ClassInfo]:
    """把獨立宣告的 Java `enum`（`EnumDeclaration`）也建成 `ClassInfo`，
    理由跟 `_extract_interfaces()` 完全對稱：沒有這個函式，enum 永遠不會
    出現在 `project.classes`，`controller_dependency_closure()` 的 import
    依賴邊（見 `_extract_project_imports()`）就算掃到某個檔案明確 import
    了這個 enum，也找不到對應節點可以連——這個 enum 因此永遠無法透過既有
    機制被任何 module 收進 `module_list.java_files`。JPA entity 欄位型別
    用到的 enum 有 ④（`scaffold_agent`，見 `08a_scaffold_agent_
    architecture.md` 四章）自己獨立掃描 db_models 用；但業務邏輯用的
    enum（如自訂錯誤碼列舉，只透過 import 被其他 class 引用，不是任何
    entity 欄位）完全不在④的掃描範圍內，必須先進 `module_list.java_files`
    才有機會被④的既有「同時掃描 EnumDeclaration」邏輯撿到並渲染進
    `app/models/_enums.py`——這就是這個函式存在的理由。

    `implements`：Java enum 可以 `implements` interface（`enum
    CommonErrorCode implements ErrorCode`），這裡如實記錄——一旦這個 enum
    進了 `project.classes`，既有的 `build_interface_implementors()`
    完全不用改，就能自動反查出「這個 interface 被哪些 enum 實作」。

    **刻意不解析 enum 成員（`decl.body.constants`）或建構子參數**：那是
    ④ 自己要做的事（08a 四章「同時掃描 EnumDeclaration」），這裡只需要
    讓這個檔案「被看見」，不需要重複④已經要做的完整解析。`fields=[]`：
    enum 沒有 instance field 可供依賴解析用（成員本身不是 field 宣告）。
    `methods=[]`：延續①「不解析 enum 方法本體」的範圍界線——`is_enum`
    因此固定為 `True`、`methods` 恆為空，純供下游診斷用（見 types.py
    `ClassInfo.is_enum` docstring），不驅動任何分支：`grouping.py::
    needs_llm_summary()` 既有規則 2（`not has_implementor and all(not
    m.has_body ...)`，空清單天生滿足 `all()`）本來就會判定這種 enum
    不需要送 Map，不需要另外新增判斷分支。`routes=[]`：enum 不會是
    Controller。
    """
    result: list[ClassInfo] = []
    for decl in tree.types:
        if not isinstance(decl, javalang.tree.EnumDeclaration):
            continue
        result.append(
            ClassInfo(
                file_path=rel_path,
                class_name=decl.name,
                stereotype=None,
                bean_name_override=None,
                implements=[i.name for i in (decl.implements or [])],
                is_primary=False,
                is_enum=True,
                fields=[],
                methods=[],
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


# --------------------------------------------------------------------------
# 3.2 欄位型別解析：@Qualifier／@Primary 消歧
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# 3.3 呼叫圖建構
# --------------------------------------------------------------------------
#
# 設計核心：追蹤方法的宣告回傳型別，接續解析下一段鏈式呼叫，而不是遇到
# 鏈式呼叫（如 `xxxService.getDetail().calculate()`）裡的方法呼叫就直接
# 放棄解析剩餘部分。理由是安全性，不是精準度：若方法 M 同時被 skip 端點
# 直接呼叫、也被非-skip 端點透過這種鏈式呼叫呼叫到，一旦鏈式的部分解析
# 不出來，M 會出現在 skip 可達集合、但漏出非-skip 可達集合，導致五章的
# 排除邏輯把 M 誤判為只服務 skip、進而錯誤排除（見 04a 三章「漏掉一個
# 實際存在的連結...是不可逆的錯誤」）。
#
# 未驗證假設：javalang 是否真的一律用「qualifier 字串折疊」表示純欄位
# 存取鏈、只在遇到方法呼叫時才展開 `.selectors`，以及 `this.x.y()` 的
# `this` 是否一定會出現在 qualifier 字串最前面，還沒有真實 Java 專案跑過
# 驗證，見 04b 十一章「已知限制與待驗證事項」。


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
    出的點號路徑，見本節前言）解析成目標類別清單。

    **三種已知靜態呼叫特例，優先判斷**：qualifier 整串直接是專案內某個
    類別名稱、且該類別命中下面任一個結構訊號時，視為靜態呼叫，直接把
    該類別當目標，不進入下面的欄位鏈解析——這幾類 helper class 都不是
    透過欄位注入使用，走欄位解析永遠找不到，之前會直接回傳 `None`、
    這個呼叫完全從呼叫圖裡消失：
    1. `uses_dynamic_query_signal=True`（這個檔案 import 了
       `org.springframework.data.jpa.domain.Specification`，見
       `_uses_dynamic_query_signal()`、04a 三章「Import 依賴補充」既知
       案例）——真實案例：`ExamController::search()` 呼叫
       `ExamSpecification.withYear/withGrade/...`。
    2. `is_self_returning_static_factory=True`（自我回傳靜態工廠，見
       `_is_self_returning_static_factory()`）——真實案例：`ExamController`
       等多個 controller 呼叫 `ResponseResult.ok(...)`／
       `Result.success(...)`；`CandidateController.getCandidate()` 呼叫
       `UserProfileRs.fromEntity(...)`（見 docs/refactor_bug_trace.md
       #38，這個訊號原本要求泛型，訂正後才涵蓋這個非泛型 DTO 案例）。
    3. `is_utils_class=True`（package 落在 `xxx.utils` 底下的純靜態工具
       類，見 `_is_utils_package()`）——真實案例（`docs/refactor_bug_
       trace.md` #15）：`ExamService::createRandom()` 呼叫
       `CodeUtil.generateRandomCode()`，`exam_router.py::getAllExamKind()`
       呼叫 `CollectionUtil.findDistinctField(...)`，兩者都因為兩個舊
       特例都不命中，完全消失在呼叫圖裡，[P] 的 `reference_targets`
       BFS 因此看不到這條呼叫，⑤ 只能照 Java 靜態呼叫語法瞎猜 Python
       端的呼叫慣例（utils 已經被翻成模組層級函式，不是類別）。

    **刻意不是「任何裸類別名稱靜態呼叫一律解析」這種通用規則**：呼叫圖
    同時供 `skip_filter.py` 的可達性分析、`plan_agent/call_chain.py` 的
    `reference_targets` BFS 使用，貿然放寬到所有靜態呼叫會擴大這個函式
    的行為變動範圍到整個呼叫圖的語意，需要重新評估對這些下游消費者的
    影響；只鎖定這幾個已經有明確結構訊號、已知問題模式的類別，範圍
    精準、風險可控——`is_self_returning_static_factory` 刻意不用類別
    名稱比對（`Result`／`Response` 這類字樣），因為這個專案裡就有名稱
    剛好帶 `Result` 字樣、但其實是一般 Request／Response DTO 的反例
    （`GetExamResultRq`／`GetExamResultRs`，兩者都沒有任何 `static`
    方法），名稱比對會誤判；`is_utils_class` 同樣不用類別名稱比對
    （`Util`／`Utils` 這類字樣），改用比對 package 名稱這個更可靠的
    結構訊號（見 `_is_utils_package()` docstring），理由跟
    `is_self_returning_static_factory` 一致。
    `_resolve_type_name_to_classes()` 找不到、或找到但三個訊號都沒命中，
    都落到下面既有的欄位鏈解析（`this.xxxService.foo()` 這類）。

    其餘每一段都當作「目前候選類別清單裡任一個的 field 名稱」依序解析
    下去，開頭的 `"this"` 視為 `current_class` 本身，任何一段解析不到
    就回傳 None。
    """
    static_targets = [
        t
        for t in _resolve_type_name_to_classes(qualifier, classes, interface_implementors)
        if t.uses_dynamic_query_signal or t.is_self_returning_static_factory or t.is_utils_class
    ]
    if static_targets:
        return static_targets

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

    必須遍歷 context 內每個候選類別的每一個同名多載方法，聯集所有能
    解析出來的回傳型別，不能只取第一個成功的就回傳（見 04a 三章「設計
    原則：多連、少排除」）：(1) 同一個 class 裡 `member_name` 有多個多載
    方法，若只取第一個宣告的多載、剛好回傳 `void` 或無法解析，會讓後面
    明明可以解析的多載被忽略；(2) `context` 裡有多個介面實作類別時，
    不同實作的回傳型別未必相同，只取第一個成功的候選會讓依賴其他實作
    回傳型別的後續呼叫鏈整條斷掉。這裡比照 `resolve_field_target_
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
    """對應 docs/refactor_bug_trace.md #16：`member_name` 若不是任何候選
    類別顯式宣告的方法，但候選類別繼承了 Spring Data 基底介面
    （`jpa_base_entity` 非 `None`）、且 `member_name` 剛好是那幾個已知的
    繼承來的固定方法名稱之一（`findAll`／`findById`／`save`……），一樣視為
    一次合法呼叫，只是目標換成合成座標（指向 Python 端 `BaseRepository`
    的對應方法，見 `common/jpa_base_repository.py`），不是這個 repository
    自己的檔案／類別——這幾個方法在 Java 原始碼裡從未顯式宣告過（Spring
    在執行期動態產生），沒有真實座標可以指，用固定的虛擬座標取代。

    對應 docs/refactor_bug_trace.md #21：只 yield 合成座標，⑤只看得到
    `BaseRepository` 這個抽象基底方法本身的內容，看不到「這次實際呼叫的
    是哪一個具體 repository 子類別、它自己定義在哪個檔案」——真實案例
    `ExamRepository`（有 `findByKind` 等其他顯式方法，但這次呼叫剛好是
    純繼承的 `findAll()`）證實模型會因此瞎猜一個檔名慣例（`app.repositories.
    exam_repository`），猜錯就整段 `ImportError`。若 `cls` 除了這個純繼承
    呼叫之外還有其他顯式方法（`cls.methods` 非空），額外 yield 一個指向
    `cls` 自己任一顯式方法的參考——這個參考本來就會被翻譯成真正的
    `java_index` 項目（指向 `cls` 實際定義的檔案），順便讓 reference_targets
    帶出「這個具體子類別定義在哪個檔案」這個資訊，不需要另外新增一種
    「class 宣告」座標格式。`cls.methods` 全空（真正的孤兒類別，如
    `QuestionRepository`）時沒有東西可以 piggyback，維持只 yield 合成
    座標——那種情況本來就交給 `_synthesize_inherited_repository_reads()`
    合成出「真正的」InterfaceSpec／`java_method_id`，不受這裡影響。
    """
    for cls in context:
        if any(m.name == member_name for m in cls.methods):
            yield method_id(cls.file_path, cls.class_name, member_name)
        elif cls.jpa_base_entity is not None and member_name in JPA_BASE_METHOD_NAME_MAP:
            yield synthetic_java_method_id(member_name)
            if cls.methods:
                yield method_id(cls.file_path, cls.class_name, cls.methods[0].name)


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
    無法解析（見 04b 十一章）。

    **`selectors` 是攤平的清單，`context` 要依序在清單元素之間傳遞**：
    真實案例證實，javalang 對 `this.a.b().c()` 這類鏈式呼叫，是把整條鏈
    攤平成同一層 `selectors` 清單（`this.userRepository.findById(id)`
    這種顯式 `this` 開頭的欄位鏈式呼叫，`This.selectors` 直接是
    `[MemberReference(member="userRepository"), MethodInvocation(member=
    "findById")]` 兩個同層元素），不是巢狀在前一個元素自己的
    `.selectors` 屬性裡（每個元素自己的 `.selectors` 在這個情境下實測
    永遠是 `None`）——修復前的版本把新算出的 `next_context` 只傳進對
    `sel.selectors`（永遠是 `None`）的遞迴呼叫，等於每次都在原地丟棄
    剛算出的 context，外層 `for` 迴圈前進到下一個清單元素時，用的還是
    這個元素自己以外、從未更新過的舊 `context`。結果是 `this.欄位.
    方法()` 這種寫法永遠解析失敗（`userRepository.findById(id)` 這種
    沒有 `this.` 開頭的等價寫法反而正確，因為那條路走的是
    `_resolve_qualifier_string()`，不經過這個函式）。改成 `context`
    在同一個 `for` 迴圈裡逐一累積更新（一般程式語言鏈式呼叫的直覺寫
    法），才會是真正的修法；`sel.selectors` 仍保留防禦性遞迴（真實資料
    從未觀察到非空的情況，但不假設它一定是空的）。
    """
    for sel in selectors:
        if isinstance(sel, javalang.tree.MemberReference):
            context = (
                _resolve_member_context(sel.member, context, classes, interface_implementors)
                if context and not sel.qualifier
                else None
            )
        elif isinstance(sel, javalang.tree.MethodInvocation):
            if context and not sel.qualifier:
                yield from _yield_call(sel.member, context)
                context = _method_return_context(sel.member, context, classes, interface_implementors)
            else:
                context = None
            for arg in sel.arguments or []:
                yield from _walk_and_resolve(arg, current_class, classes, interface_implementors)
        else:
            yield from _walk_and_resolve(sel, current_class, classes, interface_implementors)
            continue

        if sel.selectors:
            # 真實資料從未觀察到（見上方 docstring），防禦性保留：這個
            # 元素自己還帶巢狀鏈，視為在這裡分岔，遞迴走完巢狀部分，不
            # 再繼續這層迴圈剩餘的元素（避免同一段鏈被處理兩次）。
            yield from _continue_chain(sel.selectors, context, current_class, classes, interface_implementors)
            return


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


# --------------------------------------------------------------------------
# 3.4 Controller Route 索引與 Context-Path 校正
# --------------------------------------------------------------------------


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


_PLACEHOLDER_RE = re.compile(r"^\$\{([^:}]+)(?::(.*))?\}$")


def _resolve_placeholder(raw_value: str) -> str:
    """解析 Spring 的 `${ENV_VAR:default}` 佔位符寫法。這裡做的是純靜態
    原始碼分析，不是真的啟動 Java 服務，讀不到執行期真正注入的環境變數
    值：有預設值（冒號後半段）就用預設值當最佳猜測，沒有預設值就沒有
    任何依據可用，回傳空字串（等同沒有前綴，比照 04a 三章步驟 6「找不到
    主設定檔」的 fallback），三種情況都記警告——實際部署若真的用環境變數
    覆蓋了這個值，後續 skip 比對查無對應的 warning log 是唯一能發現這件
    事的地方。

    巢狀佔位符（`${SERVER_PATH:${DEFAULT_PATH:/api}}`）需要另外判斷：
    `_PLACEHOLDER_RE` 只解一層，`group(2)`（預設值）會抓到還沒被解開的
    巢狀佔位符字面字串，若不特別檢查，會被當成合法 context-path 前綴，
    後果跟「完全沒讀到前綴」一樣糟，卻不會觸發任何警告。因此 `default`
    裡如果還含有 `${`，視同「無法解析」，不當成最佳猜測使用。
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
