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
    比照 `skip_filter.py` 既有的 BFS 可達性分析。
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
_DATA_ANNOTATIONS = {
    "Entity", "Embeddable", "MappedSuperclass",
    "Data", "Value", "Getter", "Setter", "Builder",
    "NoArgsConstructor", "AllArgsConstructor", "RequiredArgsConstructor",
}
# 存取器方法命名慣例：get*/set*/is*，或 Lombok/Java 慣例產生的
# equals/hashCode/toString/canEqual/builder/toBuilder。方法名稱只要
# 不落在這個樣式裡，就代表「除了欄位存取以外還有其他行為」。
_ACCESSOR_METHOD_RE = re.compile(
    r"^(get|set|is)[A-Z0-9_]|^(equals|hashCode|toString|canEqual|builder|toBuilder)$"
)


def needs_llm_summary(class_info: ClassInfo) -> bool:
    """判斷這個 class 值不值得花一次 Claude API 呼叫做 Map 摘要，對應
    「Specification 特殊處理、一般類別看方法清單、其餘預設當有邏輯」
    三層規則：

    1. **用到已知動態查詢型別（`uses_dynamic_query_signal`，見
       call_graph.py `_uses_dynamic_query_signal()`）一律判定為有邏輯**
       ——這類型別（如 `Specification<T>`）的實際過濾規則常常寫在回傳的
       lambda／匿名類別裡（見 `ExamSpecification.withYear()` 這類案例：
       `withYear()` 自己的方法本體只有一句 `return (lambda);`，`"string"`
       這種防呆判斷藏在 lambda 內部），不管有沒有其他 annotation，一律
       送 Map，不嘗試判斷 method body 內部藏了多少邏輯。

    2. **有 `_DATA_ANNOTATIONS` 標記，且方法清單只有存取器方法（或完全
       沒有明確方法，如純靠 Lombok `@Data` 生成 getter/setter，javalang
       看不到這些生成的方法）→ 判定為純資料類別，不送 Map**。這裡刻意
       不是「只要有 `@Entity` 就跳過」——DDD 風格的富領域模型常把業務
       規則寫在 entity 自己的方法裡（如 `isEligible()`／`calculateTotal()`
       這類非存取器命名的方法），這種情況下即使有 `@Entity`，只要方法
       清單裡出現任何一個不是存取器樣式的方法名稱，就會落到規則 3，
       維持送 Map——annotation 只決定「要不要進一步檢查方法清單」，不
       單獨決定「有沒有邏輯」。

    3. **其餘情況（沒有標記、或有標記但還有非存取器方法）→ 預設有
       邏輯，送 Map**，呼應 04a 三章「多連、少排除」同一種保守精神：
       不確定的情況一律當作「可能有邏輯」而不是「大概沒有」，多花一次
       API 呼叫的代價，遠低於漏掉真實業務邏輯的代價。
    """
    if class_info.uses_dynamic_query_signal:
        return True
    has_data_annotation = bool(set(class_info.annotations) & _DATA_ANNOTATIONS)
    if has_data_annotation:
        only_accessors = all(_ACCESSOR_METHOD_RE.match(m.name) for m in class_info.methods)
        if only_accessors:
            return False
    return True


def classify_trivial_classes(project: ParsedProject) -> set[str]:
    """對 `project.classes` 全部類別跑一次 `needs_llm_summary()`，回傳
    判定不需要送 Map 的類別名稱集合。分類本身不呼叫 Claude API、不花
    任何額度（純看 annotation／方法名稱），跟 04a 二章「能用程式判斷的，
    就不要交給 LLM」同一種分工——這裡的判斷不需要語意理解，只是機械的
    命名樣式比對。
    """
    return {name for name, info in project.classes.items() if not needs_llm_summary(info)}


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
    `summarize._class_source_payload()`）。`class_info.file_path` 只相對
    `project_root`，這裡組回絕對路徑才能實際讀到檔案（見
    `ParsedProject.project_root` 說明）。
    """
    try:
        return len(Path(project_root, class_info.file_path).read_text(encoding="utf-8"))
    except OSError:
        return 0
