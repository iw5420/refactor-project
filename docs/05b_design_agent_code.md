# ③ 架構設計 Agent 程式碼實作

> 05a 是設計面文件，本文件是實作面文件，一一對應——每節開頭註明對應 05a 章節，只講怎麼落地成程式碼。對應 `00_refactor_architecture.md` 十一章文件索引的 `05b_design_agent_code.md`。本文件與 `design_agent/`／`common/openapi_ref_resolver.py`／`graph/nodes/design_node.py` 實際程式碼逐檔對照過，程式碼區塊即目前的真實內容。

## 目錄

| 檔案 | 對應章節 | 說明 |
|---|---|---|
| `common/openapi_ref_resolver.py` | 05a 五章 | 共用 `$ref` 展開 |
| `design_agent/exceptions.py` | 05a 十二章 | 例外階層 |
| `design_agent/types.py` | 05a 四～六章 | 內部資料結構 |
| `design_agent/signature_scan.py` | 05a 四章 | javalang 輕量再掃描 |
| `design_agent/type_mapping.py` | 05a 五章 | Java→Python 型別對應、openapi_spec 覆寫、框架注入偵測 |
| `design_agent/layout.py` | 05a 三章 | 分層/命名規則、module 依賴拓樸分波、directory_tree 組裝、全域基礎設施檔案、Phase 1/2 判定 |
| `design_agent/prompts.py` | 05a 六章 | Claude system prompt 與 output schema |
| `design_agent/llm.py` | 05a 六章 | ③ 專屬的模型選擇 |
| `design_agent/design.py` | 05a 六章 | 逐波呼叫 Claude API、組裝 InterfaceSpec |
| `design_agent/route_mapping.py` | 05a 八、九章 | `route_to_file_mapping`／`route_to_module_mapping` 機械合併，並寫入 `config/harness.yaml` |
| `design_agent/global_infra.py` | 05a 十五章 | `@Value` 屬性注入 → `config.py`、enum-backed interface → LLM 設計 |
| `design_agent/__init__.py` | 05a 十章 | 對外唯一入口 `run_design_agent()` |
| `graph/nodes/design_node.py` | 05a 十一章 | LangGraph node |

---

## 零、`common/openapi_ref_resolver.py`——共用 `$ref` 展開

[B] Collection Agent 與③都需要遞迴展開 `operation`/`schema` 片段裡的 `$ref`（只展開實際用到的部分，不整包攤平 `components.schemas`；遞迴到底；不處理 `allOf`/`oneOf`/`anyOf`），因此抽成共用工具，兩邊都直接呼叫，不各自實作。

```python
# common/openapi_ref_resolver.py
from __future__ import annotations

from typing import Any


def resolve_refs(fragment: dict, full_spec: dict) -> dict:
    """遞迴展開 `fragment` 內出現的 `$ref`（JSON Pointer，如
    `"#/components/schemas/UserCreateRequest"`），只展開 `fragment` 實際
    用到的部分，不會把整份 `full_spec["components"]["schemas"]` 攤平塞
    進來。

    循環參照時停止繼續展開，回傳一個帶 `_circular` 標記的殘留 `$ref`，
    不無限遞迴。找不到指標對應的節點時原樣保留該 `$ref`，不拋例外中止
    ——展開失敗不該讓呼叫端連帶整個失敗，寧可讓呼叫端看到一個沒展開的
    指標，也不要讓這個函式本身變成新的硬性失敗點。
    """
    return _resolve(fragment, full_spec, seen=frozenset())


def _resolve(obj: Any, full_spec: dict, *, seen: frozenset[str]) -> Any:
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in seen:
                return {"$ref": ref, "_circular": True}
            target = _resolve_json_pointer(full_spec, ref)
            if target is None:
                return obj
            return _resolve(target, full_spec, seen=seen | {ref})
        return {k: _resolve(v, full_spec, seen=seen) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve(v, full_spec, seen=seen) for v in obj]
    return obj


def _resolve_json_pointer(full_spec: dict, ref: str) -> Any | None:
    """解析 `"#/a/b/c"`（RFC 6901 JSON Pointer），沿路徑走到 `full_spec`
    裡對應的節點；沿路徑走不下去（key 不存在）就回傳 `None`。
    """
    node: Any = full_spec
    for segment in ref[2:].split("/"):
        segment = segment.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        else:
            return None
    return node
```

---

## 一、`exceptions.py`——例外階層

四種硬性失敗：單一 module 的 Claude 呼叫重試仍失敗、`depends_on` 循環依賴、`depends_on` 引用不存在的 module、`module_list.methods` 對不到真實 Java class/method。Java 原始碼再掃描本身失敗（`JavaSyntaxError`）原樣往上拋，不在這裡包裝。

```python
# design_agent/exceptions.py
from __future__ import annotations


class DesignAgentModuleError(Exception):
    """單一 module 的六章 Claude API 呼叫，在重試佇列機制（待重試清單、
    5 分鐘後統一重試一次）跑完仍失敗時拋出，中止整條 design run——
    `python_structure` 是 [P]／④ 唯一的權威規格，任何一個 module 的
    介面缺失都會讓兩者對著不完整規格工作，風險遠高於重新執行一次。
    """


class DesignAgentCycleError(Exception):
    """`module_list.depends_on` 拓樸排序時偵測到循環依賴時拋出，中止
    整條 design run，交由人工排查——不嘗試自動打斷環或猜測合理順序。
    只在依賴的 module 名稱都確實存在於 `module_list` 的前提下才會拋出，
    引用不存在的名稱是另一種輸入錯誤，見 `DesignAgentUnknownDependencyError`。
    """


class DesignAgentUnknownDependencyError(Exception):
    """`module_list.depends_on` 引用了不存在於 `module_list` 的 module
    名稱時拋出（見 `layout.build_waves()`）。跟循環依賴分開成兩種例外：
    根因（缺依賴 vs. 依賴形成環）不同，共用同一個訊息會讓排查方向被
    誤導成「循環」，實際問題在①的輸出資料本身。
    """


class DesignAgentCoverageError(Exception):
    """`_build_method_contexts()` 在 `module_list.methods` 裡的一筆方法
    找不到對應的真實 Java class／method 時拋出，中止整條 design run，
    不進 LLM 呼叫重試佇列——根因是①的輸出跟③這次重新掃描 Java 原始碼
    的結果對不上，屬於結構性 bug（如 signature_scan 漏掃某種語法），
    重試呼叫 Claude 解決不了程式碼掃描邏輯的問題，5 分鐘後重試只是
    白等。錯誤訊息帶出精確的 module／class／method，方便直接定位。
    """
```

---

## 二、`types.py`——內部型別定義

輸出面（`python_structure`／`route_to_file_mapping`）直接沿用 `graph/state.py` 既有的 `PythonStructure`／`InterfaceSpec`／`ParamSpec`，這裡只定義處理過程需要、不跨模組交接的中間資料結構。

```python
# design_agent/types.py
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class JavaParam:
    name: str
    java_type: str  # 原始 Java 型別字面字串，含泛型，如 "List<UserDto>"、"int"


@dataclass(frozen=True)
class JavaMethodSignature:
    """單一 Java 方法的完整簽名。多載方法（同名不同參數）各自是獨立的
    一筆，不因同名合併——`signature_key` 因此含參數型別，天生不會有
    多載碰撞。
    """

    class_name: str
    method_name: str
    params: list[JavaParam]
    return_type: str | None  # None 代表 void
    is_private: bool = False  # 供 design.py 決定 Python function_name 是否加底線前綴
    # Spring 端點方法用 @GetMapping/@PostMapping/@PutMapping/@DeleteMapping/
    # @PatchMapping 這 5 種簡寫 annotation 之一標註時，機械讀出對應的
    # HTTP method（固定大寫，如 "GET"），供 design.py::_build_boundary_index()
    # 精確消歧「同名、不同 HTTP method」的多載方法。非端點方法、或用純
    # @RequestMapping(method=...)（沒有搭配 5 種簡寫之一）宣告的端點方法
    # 維持 None，退回機械型別對應，視為非邊界方法。
    http_method: str | None = None

    @property
    def signature_key(self) -> str:
        param_types = ",".join(p.java_type for p in self.params)
        return f"{self.class_name}::{self.method_name}({param_types})"


@dataclass(frozen=True)
class JavaField:
    """單一欄位宣告，供孤兒類別（無方法、可能是 Lombok 資料類別）的
    dataclass 渲染用。`java_type` 是含泛型的完整型別字面字串，能直接
    餵給 `type_mapping.map_java_type()`。`is_final` 供判斷這批欄位該
    渲染成 `@dataclass(frozen=True)` 還是一般 `@dataclass`：全部欄位
    都 `final` 時視為不可變。
    """

    name: str
    java_type: str
    is_final: bool


@dataclass(frozen=True)
class JavaClassSignature:
    """單一 Java class 的機械掃描結果，對應 05a 四章。`stereotype` 供
    三章 layout.py 做層級判定；`None` 代表無 stereotype annotation，
    層級歸屬留給六章 LLM 判斷（見 05a 三章「無 stereotype 的類別」）。

    `constructors`：javalang 的 `class_decl.methods` 不含建構子（見
    05a 四章「多載方法的處理」同一個既有事實），只有一般方法會進
    `methods`。有些 class（常見情況：自訂例外類別）只有建構子、沒有
    一般方法，`methods` 因此永遠是空清單，`_build_method_contexts()`
    也永遠不會追蹤到這種 class——若沒有另外記下建構子簽名，這批 class
    在 `python_structure` 裡會完全沒有任何痕跡，④／⑤下游不會被告知
    要建立對應的 Python 定義（見 `design.py` 的 orphan class 處理）。
    只有 `ClassDeclaration` 才有建構子，`InterfaceDeclaration` 一律是
    空清單。

    `annotations`／`fields`：同樣只有 `ClassDeclaration` 才擷取，
    `InterfaceDeclaration` 一律是空清單。`annotations` 是這個 class 上
    所有 annotation 名稱（不只 `stereotype` 認得的 5 種 Spring
    stereotype），供 `design.py` 判斷是否為 Lombok／JPA 資料容器（見
    `common/java_annotations.py`）；`fields` 是欄位宣告清單，供同一段
    判斷邏輯在「沒有建構子、只有欄位」時渲染 dataclass 用（見
    `JavaField` docstring）——這兩者是 `design_agent` 自己另外掃描出來
    的，跟 `parse_agent.types.ClassInfo.annotations`／`.fields` 各自
    獨立、不共用（05a 四章「這不是重跑 04a 三章的呼叫圖建構」同一個
    既有理由：範圍與精度需求不同，`parse_agent` 那份掃完即丟，不會
    留到這一步，見 05a 二章）。
    """

    file_path: str
    class_name: str
    stereotype: str | None  # "RestController"/"Controller"/"Service"/"Component"/"Repository"/None
    methods: list[JavaMethodSignature] = field(default_factory=list)
    constructors: list[JavaMethodSignature] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    fields: list[JavaField] = field(default_factory=list)
    # Java package 宣告（如 "com.teachLanguage.utils"），`None` 代表 default
    # package（沒有 package 宣告，罕見）。供 layout.is_utils_package()／
    # layer_for_class() 判斷這個 class 是不是落在 utils package 下——比
    # stereotype／行為推斷更簡單可靠，見 05a 三章「Utils 特例」、
    # refactor_plan.md 二章。
    package: str | None = None
    # 這個 interface 若繼承了 Spring Data 基底介面（JpaRepository/
    # CrudRepository/PagingAndSortingRepository），這裡存它的 entity 型別
    # 簡單名稱；沒有繼承則為 None。見 common/jpa_base_repository.py::
    # detect_jpa_base_entity()。對應 docs/refactor_bug_trace.md #10／#16：
    # ③掃到這個訊號時，把對應的 Python 類別宣告改成繼承 BaseRepository[Entity]，
    # 不再需要逐一合成內建方法。
    jpa_base_entity: str | None = None


@dataclass(frozen=True)
class UncoveredParam:
    """API 邊界方法裡，Java 簽名有、但 openapi_spec 業務參數（path/query/
    requestBody 展開後的欄位）裡找不到對應的參數——框架注入物件候選。
    這份清單是機械比對出來的，交給六章 LLM 判斷 FastAPI 對應寫法。
    """

    method_signature_key: str  # 對回 JavaMethodSignature.signature_key
    param: JavaParam


@dataclass(frozen=True)
class ModuleDesignResult:
    """六章單一 module 處理完的結果：這個 module 的 InterfaceSpec 清單 +
    directory_tree 文字片段（含 schemas/{module}.py 欄位描述，若這個
    module 有 API 邊界方法）。`directory_tree_fragment` 為 `None` 代表
    這個 module 沒有 API 邊界方法、不需要 schema 段落。
    """

    module: str
    interfaces: list[dict]
    directory_tree_fragment: str | None
```

---

## 三、`signature_scan.py`——javalang 輕量再掃描

`scan_java_files()` 是唯一的對外函式，逐 module 呼叫。取方法完整簽名（參數清單＋回傳型別）、建構子簽名、class stereotype、package、annotation 清單與欄位宣告，不建呼叫圖——跟 `parse_agent/call_graph.py` 各自獨立、不共用（那邊需要欄位依賴、method invocation 解析、`@Qualifier`/`@Primary` 消歧，範圍窄得多）。`ClassDeclaration` 與 `InterfaceDeclaration` 都會建立 `JavaClassSignature`（Spring Data Repository 慣例上寫成 interface），但只有 `ClassDeclaration` 才有建構子／annotations／fields 可讀。

```python
# design_agent/signature_scan.py
from __future__ import annotations

from pathlib import Path

import javalang
import javalang.tree

from common.java_type_mapping import type_str as _type_str
from common.jpa_base_repository import detect_jpa_base_entity
from design_agent.types import JavaClassSignature, JavaField, JavaMethodSignature, JavaParam

_STEREOTYPES = {"RestController", "Controller", "Service", "Component", "Repository"}

# Spring 5 種 HTTP method 簡寫 annotation，直接對應到固定的 HTTP method
# 大寫字面字串，機械查表即可、不需要解析 annotation 元素值（純
# @RequestMapping(method=RequestMethod.GET) 這種沒有搭配簡寫的寫法刻意
# 不解析）。
_HTTP_MAPPING_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}


def scan_java_files(java_files: list[str], project_root: str) -> dict[str, JavaClassSignature]:
    """對 `java_files`（相對路徑，通常直接來自 `ModuleInfo.java_files`）
    做輕量 javalang 掃描，回傳 `class_name -> JavaClassSignature`。同一份
    掃描結果涵蓋 `ClassDeclaration` 與 `InterfaceDeclaration`（見本檔
    module docstring）。

    **決策：③ 對每個 module 的 `java_files` 各自呼叫一次這個函式**（不是
    像 parse_agent 一樣對整個 Java 專案掃一次），對應 05a 四章「③ 對每個
    module 的 java_files 做一次獨立的輕量 javalang 掃描」——③ 逐波處理
    module（見 05a 六章），每次只需要當下這個 module 的簽名，不需要一次
    掃完整個專案再自己過濾。

    **掃描失敗的處理**：比照 04a 九章，`javalang.parser.JavaSyntaxError`
    原樣往上拋，不吞掉、不跳過該檔案，整條 design run 中止（見 05a 四章
    「掃描失敗的處理」）——這是輸入端問題，不是可以重試化解的暫時性
    錯誤。
    """
    result: dict[str, JavaClassSignature] = {}
    for rel_path in java_files:
        source = Path(project_root, rel_path).read_text(encoding="utf-8")
        tree = javalang.parse.parse(source)  # JavaSyntaxError 原樣往上拋
        # `tree.package` 是 None 的情況是 default package（沒有 package
        # 宣告），Java 專案裡極罕見，`JavaClassSignature.package` 保留
        # None，layout.is_utils_package() 對 None 一律回傳 False。
        package_name = tree.package.name if tree.package is not None else None
        for decl in tree.types:
            if not isinstance(decl, (javalang.tree.ClassDeclaration, javalang.tree.InterfaceDeclaration)):
                continue
            is_class_decl = isinstance(decl, javalang.tree.ClassDeclaration)
            # `ClassDeclaration.extends` 是單一 ReferenceType | None（Java
            # 單一繼承）；`InterfaceDeclaration.extends` 是 list（Java
            # 介面可以同時繼承多個介面）——detect_jpa_base_entity() 統一
            # 吃 list，這裡先正規化成同一種形狀。Spring Data Repository
            # 慣例上一律是 interface，ClassDeclaration 這條分支實務上不會
            # 命中，但不假設它一定是 None，保守處理。
            extends_list = (
                decl.extends
                if not is_class_decl
                else ([decl.extends] if decl.extends is not None else [])
            )
            result[decl.name] = JavaClassSignature(
                file_path=rel_path,
                class_name=decl.name,
                stereotype=_stereotype_of(decl.annotations),
                package=package_name,
                jpa_base_entity=detect_jpa_base_entity(extends_list),
                methods=[_method_signature(decl.name, m) for m in decl.methods],
                # 只有 ClassDeclaration 有建構子，InterfaceDeclaration 沒有
                # `.constructors` 屬性可讀（見 JavaClassSignature.constructors
                # docstring）。
                constructors=(
                    [_constructor_signature(decl.name, c) for c in decl.constructors]
                    if is_class_decl
                    else []
                ),
                # annotations／fields 同理只在 ClassDeclaration 有意義（見
                # JavaClassSignature docstring）：InterfaceDeclaration 沒有
                # `.fields` 屬性，且孤兒類別／資料容器判斷本來就只處理
                # class，不處理 interface（interface 無 stereotype 時走
                # 05a 三章既有的「無 stereotype 類別交給六章 LLM」路徑）。
                annotations=(_annotation_names(decl.annotations) if is_class_decl else []),
                # _field_signature() 對單一 FieldDeclaration 可能回傳多筆
                # （一次宣告多個變數），這裡攤平成單一清單。
                fields=(
                    [f for field_decl in decl.fields for f in _field_signature(field_decl)]
                    if is_class_decl
                    else []
                ),
            )
    return result


def _stereotype_of(annotations: list) -> str | None:
    for ann in annotations:
        if ann.name in _STEREOTYPES:
            return ann.name
    return None


def _annotation_names(annotations: list) -> list[str]:
    """回傳這個 class 上*全部* annotation 名稱（不像 `_stereotype_of()`
    只挑 5 種 Spring stereotype 裡的第一個命中）——供 `design.py` 比對
    `common/java_annotations.py` 的 Lombok／JPA 標記用，那組判斷需要看
    到完整清單。
    """
    return [ann.name for ann in annotations]


def _field_signature(field_decl: javalang.tree.FieldDeclaration) -> list[JavaField]:
    """一個 `FieldDeclaration` 可能一次宣告多個變數（如
    `private int a, b;`），逐一展開成獨立的 `JavaField`，共用同一個
    型別與 modifiers。
    """
    java_type = _type_str(field_decl.type)
    is_final = "final" in field_decl.modifiers
    return [
        JavaField(name=decl.name, java_type=java_type, is_final=is_final)
        for decl in field_decl.declarators
    ]


def _method_signature(class_name: str, method_decl: javalang.tree.MethodDeclaration) -> JavaMethodSignature:
    """多載（同名不同參數）方法各自對應輸入 `class_decl.methods` 裡
    獨立的一筆宣告，逐筆轉換、不做任何去重或合併——這裡直接讀完整 AST
    節點（含參數型別），天生不會有多載碰撞問題。
    """
    http_method = next(
        (_HTTP_MAPPING_ANNOTATIONS[a.name] for a in method_decl.annotations if a.name in _HTTP_MAPPING_ANNOTATIONS),
        None,
    )
    return JavaMethodSignature(
        class_name=class_name,
        method_name=method_decl.name,
        params=[JavaParam(name=p.name, java_type=_type_str(p.type)) for p in method_decl.parameters],
        return_type=_type_str(method_decl.return_type) if method_decl.return_type is not None else None,
        is_private="private" in method_decl.modifiers,
        http_method=http_method,
    )


def _constructor_signature(class_name: str, ctor_decl: javalang.tree.ConstructorDeclaration) -> JavaMethodSignature:
    """建構子沒有回傳型別（`return_type=None`，跟 `void` 方法用同一個
    字面表示，但呼叫端不會混淆——建構子只會出現在
    `JavaClassSignature.constructors`，不會混進 `methods`）。`method_name`
    沿用 class 名稱（Java 建構子語法本來就跟 class 同名）。
    """
    return JavaMethodSignature(
        class_name=class_name,
        method_name=ctor_decl.name,
        params=[JavaParam(name=p.name, java_type=_type_str(p.type)) for p in ctor_decl.parameters],
        return_type=None,
        is_private="private" in ctor_decl.modifiers,
    )


# `_type_str()` 已搬到 common/java_type_mapping.py——④骨架實作 Agent 對
# JPA entity 欄位需要同一個 javalang 型別節點還原邏輯，這裡改為 import。
```

**測試**：`tests/design_agent/test_signature_scan.py`（annotation／欄位擷取、`InterfaceDeclaration` 的空清單行為、HTTP method 簡寫 annotation、package 擷取）。

---

## 四、`type_mapping.py`——Java→Python 型別對應、openapi_spec 覆寫

`map_java_type()`／`camel_to_snake()` 在 `common/java_type_mapping.py`（④骨架實作 Agent 對 JPA entity 欄位需要同一套轉換，抽成共用工具），這裡 `import` 沿用，呼叫端仍用 `type_mapping.map_java_type` / `type_mapping.camel_to_snake` 的既有寫法。

```python
# design_agent/type_mapping.py
"""③ 架構設計 Agent：Java→Python 型別對應、openapi_spec 覆寫，對應
05a 五章全節。

`map_java_type()`／`camel_to_snake()` 已搬到 `common/java_type_mapping.py`
（見 00 六章「Java 型別／命名對應（共用工具）」、08a_scaffold_agent_
architecture.md 五章）——④ 骨架實作 Agent 對 JPA entity 欄位需要同一套
轉換，不是恰好想法一致，因此跟 `common/openapi_ref_resolver.py` 同一種
先例，這裡改為 import，行為逐字不變，呼叫端（`design.py`／`layout.py`）
透過 `type_mapping.map_java_type` / `type_mapping.camel_to_snake` 的既有
呼叫方式不需要任何改動。
"""
from __future__ import annotations

import logging

from common.java_type_mapping import _GENERIC_RE, _SIMPLE_JAVA_TYPES, camel_to_snake, map_java_type
from common.openapi_ref_resolver import resolve_refs
from design_agent.types import JavaMethodSignature, JavaParam, UncoveredParam
from graph.state import ParamSpec

logger = logging.getLogger(__name__)

__all__ = ["camel_to_snake", "map_java_type"]  # 供 `type_mapping.xxx` 既有呼叫方式沿用，見上方 module docstring


_OPENAPI_SCALAR_TYPE = {"integer": "int", "number": "float", "string": "str", "boolean": "bool"}

# 對應 docs/refactor_bug_trace.md #41：extract_schema_fields() 判斷一個
# 裸 object schema 是「真的沒有更多資訊」還是「帶著看不懂的其他線索」
# 時，這幾個鍵不算「線索」——只是敘述性中繼資料，不影響型別本身的判斷。
_NON_STRUCTURAL_SCHEMA_KEYS = {"type", "description", "example", "nullable", "title"}


def openapi_type_to_python(schema: dict) -> str:
    """05a 五章「路徑／查詢參數」：`operation.parameters` 的 path/query
    參數直接用其 `schema.type` 做簡單對照，不回頭比對 Java
    `@PathVariable`/`@RequestParam` 的宣告型別——`openapi_spec` 已經是
    可信來源。陣列型別遞迴展開一層 `items`；找不到對應時原樣保留
    schema 的 `type`／`title` 字串。

    **`$ref` 一律優先於其他判斷**：`schema` 若還帶著未展開的 `$ref`
    （呼叫端刻意不先做遞迴展開，見 `extract_schema_fields()`／
    `collect_named_schemas()`），直接取 `$ref` 最後一段當類別名稱，
    跟 `schema_name_for()` 同一套規則——不能等 `common/
    openapi_ref_resolver.resolve_refs()` 把巢狀 `$ref` 展開完才判斷，
    展開後巢狀物件只剩 `type: "object"`，沒有名稱可用，只能退回不合法
    的 `"object"` 字面字串。

    **`type: "object"` 帶 `additionalProperties` 時是 OpenAPI 表示 Map／
    dict 型別的標準寫法，要優先判斷**（對應 docs/refactor_bug_trace.md
    #40）：真實案例 `GetAllExamRs.allExams` 對應 Java `Map<String,
    List<ExamkindEntity>>`，springdoc 產生的 schema 是 `{"type":
    "object", "additionalProperties": {"type": "array", "items":
    {"$ref": ...}}}`，`additionalProperties` 才是真正的 value 型別，不是
    隨便一個可以忽略的欄位。修復前這裡直接落到最後一行
    `return schema.get("title") or schema_type or "Any"`，沒有 `title`
    時回傳字面字串 `"object"`——這個字串看起來像合法的型別註記，實際上
    完全不做型別檢查，⑤ 看到這種「型別隨便」的欄位會合理但錯誤地推論
    「不需要把 entity 轉成 schema 物件」，直接把原始 ORM entity 塞進去，
    序列化階段才真正壞掉（真實案例：`get_all()` 整個 `data` 變成
    `null`）。**有 `additionalProperties` 且沒有 `properties`（代表這是
    Map，不是固定形狀物件）時，正確轉成 `dict[str, V]`**；其餘 object
    情況（沒有 additionalProperties，可能有也可能沒有 title）維持原本
    退回 `title`／裸型別字串的既有寬容行為——**這個函式本身刻意不對
    「猜不出來」的情況 raise**：它同時服務 `_classify_params()`／
    `resolve_api_boundary_signature()` 這條「參數分類」呼叫路徑，那裡
    對 `object` 這個 fallback 值是已知、已測試、刻意容忍的合法結果
    （見 `TestMultipartFileBodyParam::test_non_multipart_body_param_
    unaffected`：一般 JSON request body 沒有更精確的 schema 資訊時，
    回傳裸 `"object"` 本來就是預期行為，呼叫端只是要知道「這個參數是
    body」，不需要精確型別）。真正會被這種裸 `"object"` 型別害到的是
    schema 欄位宣告這條路徑（`extract_schema_fields()`），那裡才會把
    這個字串直接當成 Pydantic 欄位型別寫進成品——「遇到猜不出來的
    object 就直接失敗」這個更嚴格的規則放在 `extract_schema_fields()`
    自己身上判斷，不要放在這個共用函式裡，才不會誤傷參數分類這條路徑
    既有、已測試過的容忍行為。
    """
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    schema_type = schema.get("type")
    if schema_type == "array":
        return f"list[{openapi_type_to_python(schema.get('items', {}))}]"
    if schema_type == "object":
        additional_properties = schema.get("additionalProperties")
        if isinstance(additional_properties, dict) and "properties" not in schema:
            value_type = openapi_type_to_python(additional_properties)
            logger.info(
                "openapi_type_to_python()：object schema 帶 additionalProperties"
                "（無 properties），判定為 Map，轉成 dict[str, %s]（見"
                " docs/refactor_bug_trace.md #40）：%s",
                value_type, schema,
            )
            return f"dict[str, {value_type}]"
    if schema_type in _OPENAPI_SCALAR_TYPE:
        return _OPENAPI_SCALAR_TYPE[schema_type]
    return schema.get("title") or schema_type or "Any"


def find_operation(endpoint: str, http_method: str, openapi_spec: dict) -> dict | None:
    """05a 五章「查找方式」：`ApiMapping.endpoint` 本身就是
    `openapi_spec["paths"]` 的 key，直接查表即可，不需要 04a 三章那套
    context-path 校正、也不用 operationId 反查（見 05a 五章「不用
    operationId 比對」，沿用 04a 三章步驟 5 的既有結論）。回傳原始
    （未展開 `$ref`）的 operation 物件。
    """
    path_item = (openapi_spec.get("paths") or {}).get(endpoint)
    if not isinstance(path_item, dict):
        return None
    return path_item.get(http_method.lower())


def resolve_api_boundary(operation: dict, openapi_spec: dict) -> dict:
    """05a 五章「$ref 展開」：只展開這個 operation 片段裡用到的
    `$ref`，透過 `common/openapi_ref_resolver`（00 六章、05a 五章已定案
    的共用介面）。
    """
    return resolve_refs(operation, openapi_spec)


def _simple_type_name(java_type: str) -> str:
    """去掉最外層泛型容器，取內層業務型別名稱（如
    `Optional<UserCreateRequest>`／`List<UserCreateRequest>` 都取
    `UserCreateRequest`），供跟 requestBody 具名 schema 比對用——沿用
    `map_java_type()` 已有的 `_GENERIC_RE`，只取內層（group 2），不遞迴
    （`List<Optional<X>>` 這種雙層包裝不在 requestBody 的常見形狀內，
    不處理）。沒有泛型包裝的型別（多數 requestBody 參數的實際形狀，如
    `UserCreateRequest req`）原樣回傳。
    """
    match = _GENERIC_RE.match(java_type.strip())
    return match.group(2).strip() if match else java_type.strip()


def _classify_params(
    method: JavaMethodSignature, operation: dict, openapi_spec: dict
) -> tuple[list[tuple[JavaParam, ParamSpec]], list[JavaParam]]:
    """核心分類邏輯：把 `method.params`（保留原始 Java 參數順序）分成
    「業務參數（已知道 FastAPI 型別）」跟「找不到對應的參數」兩組，供
    `resolve_api_boundary_signature()`（組裝正式參數清單）與
    `find_uncovered_framework_params()`（只回傳待 LLM 判斷的清單）共用
    同一份比對結果，避免兩處各自比對出不一致的結論。`operation` 必須是
    **未展開 `$ref`** 的原始物件（`schema_name_for()` 需要看到 `$ref`
    字面字串）。

    **path/query 用名稱比對，requestBody 用型別比對，兩者比對方式刻意
    不同**：05a 五章原文是「Java 參數型別是否出現在 openapi 的
    parameters/requestBody schema 裡」——path/query 參數在 Spring 慣例上
    參數名稱本來就要跟 `@PathVariable`／`@RequestParam` 對齊，名稱比對
    可靠；但 requestBody 在 Java 端通常是**單一個 DTO 物件參數**（如
    `createUser(UserCreateRequest req)`），Java 參數名稱（`req`）本來就
    不會等於 DTO 內部的欄位名稱，若沿用名稱比對，這個 body 參數會被
    每次都誤判成「找不到對應」——這是實作時發現的落差，05a 原文的機械
    比對描述用型別、不是欄位名稱，這裡照原文用型別比對修正。body 參數
    的 Python 命名沿用原始 Java 參數名稱轉 snake_case（`camel_to_snake()`），
    不是憑空發明一個新名字；型別則一律直接從 openapi_spec 的 requestBody
    schema 算（見下方 `body_type` 那行），不會退回 Java 端型別，因為
    `has_request_body` 已經確認這個 operation 確實有 requestBody——
    openapi_spec 才是 API 邊界方法的權威來源（05a 五章）。

    **已知限制（見 05a 十三章「框架注入物件轉換...待接上真實專案輸出後
    校準」）**：型別名稱比對比對不到時（Java DTO 類別名稱跟 springdoc
    產生的 schema 名稱不同，常見於命名不完全一致的專案），退回「path/
    query 比對完之後，若剩下唯一一個參數且這個 operation 確實有
    requestBody」就假設它是 body 參數的保守啟發式——多個未比對到型別
    的參數同時存在時，這個啟發式無法安全區分誰是 body、誰是真正的框架
    物件，全部歸類為「找不到對應」，交給六章 LLM 依 `description` 語境
    判斷。這個啟發式被觸發時會記一筆 `logger.warning`（含
    `signature_key`），供接上真實專案後統計觸發比例，判斷這個保守假設
    是否需要升級。

    **`MultipartFile` 搭配多個 `@RequestParam` 的 multipart 端點**（對應
    docs/refactor_bug_trace.md #20）：這種端點剩餘參數通常不只一個（檔案
    本身 + 好幾個一般欄位），上面「剩餘參數剛好只有一個」的啟發式不會
    觸發，改在 `remaining` 迴圈之後另外處理——見函式尾端。
    """
    resolved_operation = resolve_api_boundary(operation, openapi_spec)
    path_query_by_name = {
        p["name"]: p for p in resolved_operation.get("parameters", []) if isinstance(p, dict) and "name" in p
    }
    request_body_content = (operation.get("requestBody") or {}).get("content", {})
    has_request_body = bool(operation.get("requestBody"))
    body_schema_name = schema_name_for(operation, part="requestBody") if has_request_body else None

    covered: list[tuple[JavaParam, ParamSpec]] = []
    remaining: list[JavaParam] = []
    for p in method.params:
        openapi_param = path_query_by_name.get(p.name)
        if openapi_param is not None:
            covered.append((p, ParamSpec(name=p.name, type=openapi_type_to_python(openapi_param.get("schema", {})))))
        else:
            remaining.append(p)

    if has_request_body and remaining:
        by_type = [p for p in remaining if body_schema_name and _simple_type_name(p.java_type) == body_schema_name]
        if not by_type and len(remaining) == 1:
            logger.warning(
                "%s：requestBody 型別比對不到（Java 型別 %s 對不上具名 schema %s），"
                "退回「唯一剩餘參數」啟發式判定為 body 參數（見本函式 docstring「已知限制」）",
                method.signature_key, remaining[0].java_type, body_schema_name,
            )
        body_param = by_type[0] if by_type else (remaining[0] if len(remaining) == 1 else None)
        if body_param is not None:
            # docs/refactor_bug_trace.md #7：Spring 的 MultipartFile 參數
            # springdoc 幾乎不會產生具名 schema（multipart/form-data 的
            # requestBody 常常是 inline object、沒有 $ref／title），上面
            # 的型別名稱比對必然落空、退回「唯一剩餘參數」啟發式，這裡若
            # 再照舊呼叫 openapi_type_to_python() 去猜，schema 給不出
            # 名稱時只能吐出裸字串 "object"。Java 型別本身才是可靠依據：
            # Spring MVC 框架裡「這是檔案上傳」只會宣告成 MultipartFile
            # 這個型別，跟這個方法叫什麼名字、路徑長什麼樣無關，不需要
            # 靠 endpoint 名稱／參數名稱猜語意。命中就直接對應 FastAPI
            # 的 UploadFile，不再嘗試從 openapi schema 算型別。
            if _simple_type_name(body_param.java_type) == "MultipartFile":
                body_type = "UploadFile"
            else:
                # 型別一律用 openapi_type_to_python() 在**未展開**的
                # requestBody schema 上算（見 _first_media_schema()），不是
                # 直接沿用 body_schema_name 這個裸名稱——body_schema_name
                # 若是陣列包裝（見 schema_name_for() docstring「陣列包裝的
                # 具名 schema 也算」）只帶 items 的類別名稱，直接當型別字串
                # 會弄丟 list[...] 容器語意；openapi_type_to_python() 才會
                # 正確處理陣列，回傳 "list[UserDto]" 而不是 "UserDto"。
                body_type = openapi_type_to_python(_first_media_schema(request_body_content))
            covered.append((body_param, ParamSpec(name=camel_to_snake(body_param.name), type=body_type)))
            remaining = [p for p in remaining if p is not body_param]

    # 對應 docs/refactor_bug_trace.md #20：Spring 多個 @RequestParam 逐一
    # 宣告的 multipart 端點（如 FileController.voice()：4 個字串參數 +
    # 1 個 MultipartFile，不是單一 DTO body），openapi 不會把這些欄位放進
    # `parameters`（不是 path/query），上面的 requestBody 邏輯又只在
    # 「剩餘參數剛好只有一個」時才嘗試判定 body 參數——5 個剩餘參數完全
    # 不會觸發，全部原封不動留給 `find_uncovered_framework_params()` 交
    # 六章 LLM 逐一亂猜型別，猜出的其餘欄位維持裸 str，FastAPI 因此把它們
    # 當 query parameter 解析，真實 multipart 請求被判定缺必填參數。
    #
    # 這裡比照上面 `MultipartFile` → `UploadFile` 同一種「Java 型別本身
    # 就是可靠依據，不需要靠名稱猜語意」的既有原則：`remaining` 裡只要
    # 找得到一個 `MultipartFile`，同一組 `@RequestParam` 就是 Spring 對
    # 這個端點的既定綁定慣例，其餘欄位都是要從同一個 multipart form 讀
    # 的一般欄位。只對「經 `_SIMPLE_JAVA_TYPES` 確認是基本型別」的欄位
    # 補 `Form(...)`（見 `_SIMPLE_JAVA_TYPES` docstring）——刻意排除
    # `HttpServletRequest` 這類框架注入物件型別，那些不是 multipart
    # 表單欄位，不應該被誤包成 `Form(...)`，維持留在 `remaining` 交六章
    # LLM 判斷（05a 五章「框架注入物件」既有路徑，這裡不重複處理）。
    multipart_param = next(
        (p for p in remaining if _simple_type_name(p.java_type) == "MultipartFile"), None
    )
    if multipart_param is not None:
        still_remaining: list[JavaParam] = []
        for p in remaining:
            if p is multipart_param:
                covered.append((p, ParamSpec(name=p.name, type="UploadFile")))
            elif p.java_type.strip() in _SIMPLE_JAVA_TYPES:
                covered.append((p, ParamSpec(name=p.name, type=f"{map_java_type(p.java_type)} = Form(...)")))
            else:
                still_remaining.append(p)
        remaining = still_remaining

    return covered, remaining


def _first_media_schema(content: dict) -> dict:
    """`content` 是 OpenAPI `requestBody.content`／某個 response 的
    `content`（media type → object），這個專案不處理多 media type 協商，
    一律取第一個（沿用 `schema_name_for()`／`collect_named_schemas()`
    同樣的慣例）。回傳的是**未展開 `$ref`** 的原始 schema，交給
    `openapi_type_to_python()` 判斷型別字串（直接 `$ref`、陣列包裝的
    `$ref`、純量、或 inline 型別都在它的處理範圍內）。
    """
    for media in content.values():
        return media.get("schema", {})
    return {}


def is_response_entity_return_type(java_type: str | None) -> bool:
    """判斷 Java 方法簽名**原始**回傳型別字面字串是不是
    `ResponseEntity<...>`（可能依情境動態控制 HTTP status／header，見
    `docs/09b_bug_trace.md` #30）。用字面字串前綴判斷，不透過
    `map_java_type()`——那個函式已經把它轉成 `"Response"`，這裡要問的是
    「轉換之前的原始型別是不是它」，轉換後的結果反而看不出來。
    """
    return bool(java_type) and java_type.strip().startswith("ResponseEntity")


def resolve_api_boundary_signature(
    method: JavaMethodSignature, operation: dict, openapi_spec: dict
) -> tuple[list[ParamSpec], str]:
    """05a 五章「API 邊界方法：改用 openapi_spec 覆寫」：回傳
    `(業務參數清單, 回傳型別)`，保留原始 Java 參數順序（不含找不到對應
    的框架注入參數，那些由六章 LLM 決定後在 design.py 另外附加）。

    **回傳型別一律透過 `openapi_type_to_python()` 在未展開的 response
    schema 上算**，不使用 `schema_name_for()` 的回傳值直接當型別字串
    ——`schema_name_for()` 對陣列包裝的具名 schema 只回傳 items 的類別
    名稱（如 `"UserDto"`，見該函式 docstring「陣列包裝的具名 schema
    也算」），若直接拿來當 `return_type`，陣列回應會弄丟 `list[...]`
    容器語意，變成把 `List<UserDto>` 誤判成 `UserDto`。也不能用
    `resolve_api_boundary()` 展開後的 response schema 呼叫
    `openapi_type_to_python()`——展開後 `$ref` 字面字串已經被替換成完整
    物件，具名類別的名稱資訊就跟著消失了（同 `collect_named_schemas()`
    「為什麼不用遞迴展開後的版本」）。

    **`ResponseEntity<T>` 覆寫（見 `docs/09b_bug_trace.md` #30）**：這種
    情況下不查 openapi 的 response schema——openapi_spec 只能表達一個
    代表性 status 的 body 形狀，無法完整表達「依情境動態回傳不同
    status」這個語意，一律直接覆寫成 `"Response"`（跟
    `common/java_type_mapping.py::map_java_type()` 對同一個型別的轉換
    規則一致），交給函式本體自行用 `JSONResponse(...)` 動態組裝。
    """
    covered, _ = _classify_params(method, operation, openapi_spec)
    params = [param_spec for _, param_spec in covered]

    if is_response_entity_return_type(method.return_type):
        return params, "Response"

    responses = operation.get("responses", {})
    status = next((s for s in sorted(responses) if s.startswith("2")), next(iter(responses), None))
    if status is None:
        return params, "None"

    content = (responses.get(status) or {}).get("content", {})
    if not content:
        return params, "None"  # 204 No Content 這類沒有 content 的合法回應
    return params, openapi_type_to_python(_first_media_schema(content))


def _raw_named_schema(schema_name: str, openapi_spec: dict) -> dict:
    """直接從 `components.schemas` 取具名 schema 的**原始**定義，刻意
    不經過 `resolve_api_boundary()`／`resolve_refs()` 的遞迴展開——見
    `collect_named_schemas()` docstring「為什麼不用遞迴展開後的版本」。
    """
    return ((openapi_spec.get("components") or {}).get("schemas") or {}).get(schema_name, {})


def _nested_schema_refs(raw_schema: dict) -> list[str]:
    """從一個具名 schema 的 `properties` 裡找出所有直接／陣列包裝的
    `$ref` 具名 schema 引用，回傳類別名稱清單（可能重複，呼叫端自行
    去重）。只處理 `properties` 這一層，不處理 `allOf`／`oneOf`／
    `anyOf`（同 05a 五章既定範圍），供 `collect_named_schemas()` 遞迴
    收集用（見 `docs/09b_bug_trace.md` #28）。
    """
    refs: list[str] = []
    for prop_schema in (raw_schema.get("properties") or {}).values():
        ref = prop_schema.get("$ref")
        if isinstance(ref, str):
            refs.append(ref.rsplit("/", 1)[-1])
            continue
        if prop_schema.get("type") == "array":
            item_ref = (prop_schema.get("items") or {}).get("$ref")
            if isinstance(item_ref, str):
                refs.append(item_ref.rsplit("/", 1)[-1])
    return refs


def collect_named_schemas(operation: dict, openapi_spec: dict) -> list[tuple[str, dict]]:
    """回傳這個 operation 用到的具名 schema（requestBody + 第一個 2xx
    response，**遞迴展開巢狀具名 schema，見下方**），每個是
    `(schema_name, raw_schema)`，供
    `design_agent.layout.render_schema_section()` 渲染 directory_tree
    的 Schema 定義段用（見 05a 五章「型別命名」：具名 schema 直接沿用
    這個名稱作為 Pydantic 類別名稱，欄位機械渲染成文字）。inline
    schema（沒有 `$ref`，`schema_name_for()` 回傳 `None`）不會出現在
    回傳清單裡——沒有名稱就沒有可以渲染 `class {name}:` 的類別，這種
    情況下 InterfaceSpec 的型別字面字串已經是唯一能傳遞的資訊。

    **為什麼不用遞迴展開後的版本**：`resolve_api_boundary()` 會把
    schema 內任何巢狀 `$ref`（例如 `User` 的某個欄位是
    `List[Order]`）也遞迴展開成完整物件，展開後這個欄位就再也看不出
    它原本指向具名的 `Order`，`extract_schema_fields()` 只能靠
    `title`（springdoc 通常不會產生）退回不合法的 `"object"` 字面
    字串。這裡改成直接查 `components.schemas[schema_name]` 這一份
    **未展開**的原始定義，讓巢狀屬性上的 `$ref` 保留原樣，交給
    `extract_schema_fields()` → `openapi_type_to_python()` 逐欄位判斷
    （見該函式「`$ref` 一律優先於其他判斷」）。副作用：這個具名 schema
    本身若是靠 `allOf` 組合出來的（不是本次任務要處理的組合語法，見
    05a 五章「$ref 展開」既定範圍），仍然拿不到 `properties`——這點跟
    改用這份未展開版本前的既有限制一致，沒有變得更差。

    **遞迴收集巢狀具名 schema（見 `docs/09b_bug_trace.md` #28）**：頂層
    收集到的 requestBody／response schema，若欄位本身指向另一個具名
    schema（直接 `$ref` 或陣列包裝的 `$ref`），這個巢狀 schema 也要
    一併收進來、遞迴展開到底（`visited` 集合防止 `A`↔`B` 互相引用
    造成無窮迴圈）——真實案例：`registration` 模組某方法的頂層回應
    schema 有一個欄位指向 `GetAllGradeRs`，但這個具名 schema 過去只有
    在**另一個** module（`school`）剛好也把它當頂層 response schema時
    才會被渲染出來，導致 `registration.py` 引用了一個只存在於別的
    module 檔案裡的 class（`PydanticUserError`）。

    **決策：每個 module 的 schema 檔案自我完備（self-contained），不
    建立跨模組的 schema 擁有權登記表**——任何一個 module 只要引用了
    某個具名 schema 的欄位型別，就一定也在自己的
    `schemas/{module}.py` 渲染出這個 class 的定義，不需要跨檔案
    import。這代表同一個具名 schema 可能在多個 module 的 schema 檔案
    裡各自渲染一份同名 class——這是刻意接受的重複，不是缺陷：Pydantic
    model 只在各自檔案內部使用，不會跨模組傳遞同一個實例，重複定義不
    影響正確性，只是多一點產出檔案體積。比起「先做全域第一遍掃描決定
    唯一擁有者、再讓其他模組 import」，這個做法從結構上直接消除「缺
    import」這整類 bug，不需要额外的跨波次資料流（見 05a 六章「處理
    單位」逐波處理，下游 module 看得到上游已完成的 `InterfaceSpec`，
    但看不到上游已經渲染過的 schema class 清單）。
    """
    results: list[tuple[str, dict]] = []
    visited: set[str] = set()

    def _collect(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        raw_schema = _raw_named_schema(name, openapi_spec)
        results.append((name, raw_schema))
        for nested_name in _nested_schema_refs(raw_schema):
            _collect(nested_name)

    body_name = schema_name_for(operation, part="requestBody")
    if body_name:
        _collect(body_name)

    responses = operation.get("responses", {})
    status = next((s for s in sorted(responses) if s.startswith("2")), None)
    if status:
        response_name = schema_name_for(operation, part=status)
        if response_name:
            _collect(response_name)

    return results


def find_uncovered_framework_params(
    method: JavaMethodSignature, operation: dict, openapi_spec: dict
) -> list[UncoveredParam]:
    """05a 五章「框架注入物件：openapi_spec 覆寫的例外」：Java 簽名裡
    `_classify_params()` 分類不到業務參數的部分，視為框架注入物件候選
    （如 HttpServletRequest／Authentication／`@RequestHeader` 取出的
    token），列入清單交給六章 LLM 判斷 FastAPI 對應寫法。
    """
    _, remaining = _classify_params(method, operation, openapi_spec)
    return [UncoveredParam(method_signature_key=method.signature_key, param=p) for p in remaining]


# OpenAPI 3.0（見 00 一章，springdoc 產生的固定是 3.0 spec，不是 3.1）
# 的 exclusiveMinimum／exclusiveMaximum 是布林旗標、附掛在 minimum／
# maximum 旁邊，跟 JSON Schema／OpenAPI 3.1 把 exclusiveMinimum 本身
# 當數值的寫法不同——這裡只處理 3.0 的形狀，不處理 3.1。
_FIELD_CONSTRAINT_KEYS = ("maxLength", "minLength", "pattern", "maximum", "minimum")


def _field_constraint_kwargs(prop_schema: dict) -> list[str]:
    """把 OpenAPI schema 的驗證關鍵字（springdoc 從 Java Bean Validation
    annotation，如 `@Size`／`@Pattern`／`@Min`／`@Max`，轉譯出來的）轉成
    Pydantic `Field()` 的關鍵字引數字串清單。沒有任何限制時回傳空清單
    ——呼叫端據此判斷要不要包一層 `Field(...)`，不是每個欄位都硬套。
    """
    kwargs: list[str] = []
    if "maxLength" in prop_schema:
        kwargs.append(f"max_length={prop_schema['maxLength']}")
    if "minLength" in prop_schema:
        kwargs.append(f"min_length={prop_schema['minLength']}")
    if "pattern" in prop_schema:
        kwargs.append(f"pattern={prop_schema['pattern']!r}")
    if "maximum" in prop_schema:
        key = "lt" if prop_schema.get("exclusiveMaximum") is True else "le"
        kwargs.append(f"{key}={prop_schema['maximum']}")
    if "minimum" in prop_schema:
        key = "gt" if prop_schema.get("exclusiveMinimum") is True else "ge"
        kwargs.append(f"{key}={prop_schema['minimum']}")
    return kwargs


def extract_schema_fields(raw_schema: dict) -> list[tuple[str, str]]:
    """從 `collect_named_schemas()` 傳入的**未展開** schema（見該函式
    「為什麼不用遞迴展開後的版本」）取出欄位名＋Python 型別清單，供
    `design_agent.layout.render_schema_section()` 渲染進 directory_tree
    的 Schema 定義段。只取 `properties`，不處理 `allOf`／`oneOf`／
    `anyOf`。非 `required` 的欄位型別加上 `| None`。欄位本身若是指向
    另一個具名 schema 的 `$ref`（或其陣列），`openapi_type_to_python()`
    直接取 `$ref` 名稱，不會因為沒展開而拿不到型別。

    有驗證限制的欄位改用 `Field(...)`（理由見 05a 五章「型別命名」），
    回傳的第二個字串可能是 `"str"`、`"str = Field(..., max_length=50)"`
    或 `"str | None = Field(default=None, max_length=50)"` 這幾種形狀，
    已是完整的欄位宣告右手邊。`X | None` 語法要求目標 Python 服務
    ≥ 3.10（見 00 三章），若前提改變，這裡跟 `map_java_type()` 都需要
    一併改成 `typing.Optional[T]`。

    **`openapi_type_to_python()` 回傳裸字面字串 `"object"` 時直接
    raise，不寫進成品**（對應 docs/refactor_bug_trace.md #40）：那個
    共用函式對「猜不出來的 object」故意保留寬容 fallback（給
    `_classify_params()` 這條參數分類路徑用，見該函式 docstring），但
    這裡是在產生**真正會寫進 Pydantic model 的欄位型別宣告**——裸
    `object` 完全不做型別檢查，語法合法但語意等於沒有型別，會讓⑤合理
    但錯誤地推論「這個欄位不用把 entity 轉成 schema 物件」，真實案例
    `GetAllExamRs.allExams` 序列化階段才真正壞掉。

    **裸 `object` 進一步分流（對應 docs/refactor_bug_trace.md #41）**：
    真實重跑證實不能無條件 raise——`ResponseResultVoid.data` 這個真實
    案例對應 Java `ResponseResult<Void>`，`data` 這個欄位的 schema
    就只有 `{"type": "object"}`，除了 `type`沒有任何其他資訊，這不是
    漏掉了什麼可以救回來的結構，是 Java `Void` 泛型抹除後**本來就沒有
    資料可以描述**——這種情況正確答案是 `Any`（誠實表示「這裡本來就
    沒有更精確的型別」），不該讓整條 pipeline 為了一個本來就沒問題的
    欄位而崩潰。真正該 raise 的是「這個 schema 除了 `type` 之外還帶著
    其他這個函式看不懂的線索」（例如 `oneOf`／`patternProperties` 這類
    目前沒處理的鍵）——那才代表可能真的漏接了一個能救回來的結構，跟
    `GetAllExamRs.allExams`（有 `additionalProperties` 這個明確線索，
    只是沒被辨識）是同一種情況。
    """
    properties = raw_schema.get("properties", {})
    required = set(raw_schema.get("required", []))
    fields: list[tuple[str, str]] = []
    for name, prop_schema in properties.items():
        python_type = openapi_type_to_python(prop_schema)
        if python_type == "object":
            unexplained_keys = set(prop_schema) - _NON_STRUCTURAL_SCHEMA_KEYS
            if unexplained_keys:
                logger.warning(
                    "extract_schema_fields()：欄位 %s 的型別解析成裸 object"
                    "，而且 schema 還帶著這個函式看不懂的其他鍵 %s，拒絕"
                    "寫進成品，見 docs/refactor_bug_trace.md #40：%s",
                    name, sorted(unexplained_keys), prop_schema,
                )
                raise ValueError(
                    f"欄位 {name!r} 無法解析出明確型別（openapi_type_to_python() 只能"
                    f"回傳裸 \"object\"，且 schema 還帶著看不懂的其他鍵 {sorted(unexplained_keys)}）：{prop_schema}"
                )
            logger.info(
                "extract_schema_fields()：欄位 %s 除了 type 沒有其他資訊"
                "（像 Java Void 這種泛型抹除後沒有實際資料的情況），退回"
                " Any，見 docs/refactor_bug_trace.md #41：%s",
                name, prop_schema,
            )
            python_type = "Any"
        is_required = name in required
        if not is_required:
            python_type = f"{python_type} | None"

        constraint_kwargs = _field_constraint_kwargs(prop_schema)
        if constraint_kwargs:
            default_arg = "..." if is_required else "default=None"
            declaration = f"{python_type} = Field({', '.join([default_arg, *constraint_kwargs])})"
        elif is_required:
            declaration = python_type
        else:
            # 對應 docs/09b_bug_trace.md：`X | None` 這個型別註記本身不會
            # 讓 Pydantic v2 把欄位當成有預設值——沒有明確寫 `= None`，
            # 非必填欄位一樣會被當成必填，請求方（真實 Postman collection，
            # 對應 Java 端本來就可省略的 DTO 欄位）沒帶這個欄位就直接
            # 422/500。這個分支之前只有純型別注記、漏了預設值，只有走
            # 上面 `Field(...)` 那個分支的非必填欄位才有補 `default=None`，
            # 造成同一份 schema 生成邏輯裡「有沒有驗證限制」意外決定了
            # 「有沒有預設值」這兩件不相關的事。
            declaration = f"{python_type} = None"
        fields.append((name, declaration))
    return fields


def schema_name_for(operation: dict, *, part: str) -> str | None:
    """`operation`：**未展開 `$ref`** 的原始 operation 物件（`$ref`
    字串本身就是名稱來源，展開後這個字串會被實際內容取代，反而拿不到
    名稱，因此這個函式必須在 `resolve_api_boundary()` 之前呼叫）。
    `part` 是 `"requestBody"` 或某個 response status code（如
    `"200"`）。springdoc 通常會產生具名 schema（見 05a 五章「型別
    命名」），這裡直接從 `$ref` 字串取最後一段當類別名稱。inline schema
    （沒有 `$ref`）時回傳 `None`，呼叫端退回不具名的內嵌型別描述，不
    強行編造一個類別名稱。

    **陣列包裝的具名 schema（`type: "array", items: {"$ref": ...}}`）
    也算**：回傳的是 items 指向的類別名稱（如 `"UserDto"`），不是
    `"list[UserDto]"`——這個函式只負責「找出名稱」，給
    `collect_named_schemas()` 判斷要不要收這個 schema、要收哪一個名稱；
    要不要包 `list[...]` 是回傳型別/參數型別的語意，由呼叫端透過
    `openapi_type_to_python()`（在**未展開**的 schema 上）決定，這裡
    刻意不摻進來，避免呼叫端誤用這個回傳值當作最終型別字串（見
    `resolve_api_boundary_signature()`／`_classify_params()` 的用法）。
    """
    if part == "requestBody":
        content = (operation.get("requestBody") or {}).get("content", {})
    else:
        content = ((operation.get("responses") or {}).get(part) or {}).get("content", {})
    for media in content.values():
        schema = media.get("schema", {})
        ref = schema.get("$ref")
        if not isinstance(ref, str) and schema.get("type") == "array":
            ref = schema.get("items", {}).get("$ref")
        if isinstance(ref, str):
            return ref.rsplit("/", 1)[-1]
    return None
```

**測試**：`tests/design_agent/test_type_mapping.py`（型別對應、`$ref` 展開、`ResponseEntity`、巢狀 schema 遞迴收集、requestBody 型別比對）。

---

## 五、`layout.py`——分層、拓樸分波、directory_tree 組裝、Phase 1/2 判定

```python
# design_agent/layout.py
"""③ 架構設計 Agent：Python 分層與命名慣例、module 依賴拓樸分波、
directory_tree 組裝、全域基礎設施檔案，對應 05a 三章全節。
"""
from __future__ import annotations

import re

from common.java_type_mapping import camel_to_snake
from design_agent.exceptions import DesignAgentCycleError, DesignAgentUnknownDependencyError
from graph.state import InterfaceSpec, ModuleInfo

# Java class 的 stereotype annotation 決定歸屬層級，無 stereotype 的
# 類別（None）留給六章 LLM 判斷。
_STEREOTYPE_LAYER = {
    "RestController": "routers",
    "Controller": "routers",
    "Service": "services",
    "Component": "services",
    "Repository": "repositories",
}
_LAYER_SINGULAR = {"routers": "router", "services": "service", "repositories": "repository"}

# Phase 1（entity/dto/repository/utils）／Phase 2（controller/service）
# 分階段翻譯設計（見 refactor_plan.md 一、二章）：repositories／utils
# 歸 Phase 1，routers／services 歸 Phase 2。entity／dto 不經過這張表——
# 它們從不產生一般 InterfaceSpec（entity 直接跳過，DTO 走 openapi 展開
# 進 directory_tree 文字），phase 欄位的計算天然用不到。
_PHASE_1_LAYERS = {"repositories", "utils"}


def layer_for_stereotype(stereotype: str | None) -> str | None:
    """回傳 `stereotype` 機械對應到的層級目錄名稱；`None`（含未知
    stereotype）代表機械規則判斷不了，交給六章 LLM（多半併入
    services，但實際歸屬可能因業務語意而異）。

    不含 utils 判斷：這裡只看 stereotype，不看 package，維持
    `global_infra.py::scan_value_injected_fields()` 既有呼叫端的行為
    不變（`@Value` 欄位注入在 `@UtilityClass` 靜態方法類別上不是真實
    會出現的 Spring 模式，這條路徑不需要跟著改）。六章 `design.py` 的
    層級判定改呼叫 `layer_for_class()`（見下方），會先檢查 utils
    package 再退回這裡的 stereotype 對應。
    """
    return _STEREOTYPE_LAYER.get(stereotype) if stereotype else None


def is_utils_package(package: str | None) -> bool:
    """判斷 Java class 的 package 是否落在 `xxx.utils` 下——幾乎所有
    Java 專案都遵守這個慣例，比「無 stereotype + 全靜態方法」這種行為
    推斷簡單、可靠得多。`package` 為 `None`（default package，Java
    專案裡極罕見）一律回傳 `False`。
    """
    if not package:
        return False
    last_segment = package.rsplit(".", 1)[-1]
    return last_segment == "utils"


def layer_for_class(stereotype: str | None, package: str | None) -> str | None:
    """六章 `design.py` 逐 method 層級判定的唯一入口：優先判斷 package
    是不是 `xxx.utils`，是的話直接歸 `"utils"`，不看 stereotype；否則
    退回 `layer_for_stereotype()` 既有的 stereotype 對應。`None`（含
    無 stereotype 又不在 utils package）代表機械規則判斷不了，交給
    六章 LLM。
    """
    if is_utils_package(package):
        return "utils"
    return layer_for_stereotype(stereotype)


def phase_for_layer(layer: str) -> int:
    """`layer`（`layer_for_class()` 或 LLM `class_layers` 回傳的層級
    名稱）對應到 Phase 1 或 Phase 2：`repositories`／`utils` → 1，
    其餘（`routers`／`services`） → 2。
    """
    return 1 if layer in _PHASE_1_LAYERS else 2


def file_path_for_layer(module: str, layer: str) -> str:
    """`{module}_{layer_singular}.py`，`module` 沿用 `ModuleInfo.module`，
    已是 snake_case 慣例字串，不需要③額外轉換大小寫。不適用於 utils
    層——見 `file_path_for_utils()`。
    """
    return f"app/{layer}/{module}_{_LAYER_SINGULAR[layer]}.py"


def file_path_for_utils(java_class_name: str) -> str:
    """utils 不套用 `{module}_{layer}.py` 規則（utils 橫跨多個
    module，套用會出現歸屬假問題），直接複製 Java package 結構，一個
    Java class 對一個 Python 檔案，不分 module——`ValidationUtil` →
    `app/utils/validation_util.py`。
    """
    return f"app/utils/{camel_to_snake(java_class_name)}.py"


def schema_file_path(module: str) -> str:
    return f"app/schemas/{module}.py"


def model_file_path(module: str) -> str:
    return f"app/models/{module}.py"


# 全域例外處理（`_global` 保留模組）固定輸出的檔案路徑——這批方法不
# 屬於任何 module 分層慣例。
EXCEPTION_HANDLERS_FILE = "app/core/exception_handlers.py"


def build_waves(module_list: list[ModuleInfo]) -> list[list[ModuleInfo]]:
    """對 `module_list` 依 `depends_on` 建拓樸順序、分波。第一波是沒有
    `depends_on` 的 module，之後每一波是「`depends_on` 全部落在前面
    已完成波次」的 module。

    循環依賴視為上游輸入資料錯誤，直接中止——不嘗試自動打斷環或猜測
    合理順序，交由人工排查。

    依賴完整性檢查先於拓樸排序：`depends_on` 若引用了不存在於
    `module_list` 的 module 名稱，拓樸排序迴圈最終還是會因為 `ready`
    沒有新成員而卡住，但那樣拋出的會是 `DesignAgentCycleError`，把
    「缺依賴」誤導成「循環依賴」。因此先做一輪存在性檢查，缺依賴用
    專屬的 `DesignAgentUnknownDependencyError` 明確標示。
    """
    by_name = {m["module"]: m for m in module_list}

    unknown_deps = sorted(
        {(m["module"], dep) for m in module_list for dep in m["depends_on"] if dep not in by_name}
    )
    if unknown_deps:
        raise DesignAgentUnknownDependencyError(
            f"module_list.depends_on 引用了不存在於 module_list 的 module 名稱（見本函式 docstring）: {unknown_deps}"
        )

    remaining = set(by_name)
    done: set[str] = set()
    waves: list[list[ModuleInfo]] = []

    while remaining:
        ready = {name for name in remaining if all(dep in done for dep in by_name[name]["depends_on"])}
        if not ready:
            raise DesignAgentCycleError(
                f"module_list.depends_on 偵測到循環依賴，無法拓樸排序: {sorted(remaining)}"
            )
        waves.append([by_name[name] for name in sorted(ready)])
        done |= ready
        remaining -= ready
    return waves


# ── 全域基礎設施檔案（機械組裝，不需 LLM）──

_DATABASE_PY_TEMPLATE = '''from sqlalchemy import create_engine
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
'''


def render_database_py() -> str:
    """`app/core/database.py` 固定樣板——寫死讀取 `DATABASE_URL`、建立
    SQLAlchemy engine／`SessionLocal`／`Base`／`get_db()` dependency，
    字面模板不經過 LLM，避免④骨架生成呼叫的本地模型自行臆測環境變數
    名稱或連線寫法。

    維持同步（`create_engine`／`Session`），不改非同步——這是重構
    專案，目標是跟 Java 服務（傳統 blocking JDBC）行為對齊。改非同步
    會讓每一支碰到 DB 的函式都要正確加上 `async`/`await`，這個正確性
    負擔會落在 ⑤ 呼叫的本地模型身上，不該加大它的出錯面。

    `except Exception: db.rollback()` 是明確補上的清理步驟：例外發生
    當下立刻 rollback，不是隱含依賴 `Session.close()` 的內部行為，讓
    交易邊界更明確。
    """
    return _DATABASE_PY_TEMPLATE


def render_main_py(interfaces: list[InterfaceSpec]) -> str:
    """`app/main.py`：對每一個「`interfaces` 裡出現過
    `app/routers/{module}_router.py` 這個 `file_path`」的 module，機械
    產生一行 import 與一行 `include_router()`。沒有產出 router 層檔案
    的 module 不會出現在這份清單裡。

    全域例外處理註冊：`interfaces` 裡若有 `file_path ==
    EXCEPTION_HANDLERS_FILE` 的項目（`_design_global_advice_module()`
    產生），為每一個這樣的函式機械產生 import＋三行
    `app.add_exception_handler()` 註冊：`Exception`／`HTTPException`／
    `RequestValidationError`。只註冊 `Exception` 不夠——FastAPI 會替
    `HTTPException`／`RequestValidationError` 這兩種型別預先註冊自己
    的內建預設處理器，Starlette 分派例外時精確型別優先，內建的具體
    型別處理器會贏過只註冊 `Exception` 的泛用處理器，這兩種例外會被
    FastAPI 自己的預設處理器接走，回應是 FastAPI 內建格式，不是這個
    專案統一的 `ResponseResult` 回應慣例。三行都指向同一個 Python
    函式，不是三種不同處理方式。
    """
    router_files = sorted(
        {iface["file_path"] for iface in interfaces if iface["file_path"].startswith("app/routers/")}
    )

    imports: list[str] = []
    includes: list[str] = []
    for file_path in router_files:
        module = file_path.removeprefix("app/routers/").removesuffix("_router.py")
        var_name = f"{module}_router"
        module_path = file_path.removesuffix(".py").replace("/", ".")
        imports.append(f"from {module_path} import router as {var_name}")
        includes.append(f"app.include_router({var_name})")

    exception_handler_module_path = EXCEPTION_HANDLERS_FILE.removesuffix(".py").replace("/", ".")
    handler_imports: list[str] = []
    handler_registrations: list[str] = []
    for iface in interfaces:
        if iface["file_path"] != EXCEPTION_HANDLERS_FILE:
            continue
        fn = iface["function_name"]
        handler_imports.append(f"from {exception_handler_module_path} import {fn}")
        handler_registrations.append(f"app.add_exception_handler(Exception, {fn})")
        handler_registrations.append(f"app.add_exception_handler(HTTPException, {fn})")
        handler_registrations.append(f"app.add_exception_handler(RequestValidationError, {fn})")

    fastapi_import = "from fastapi import FastAPI, HTTPException" if handler_registrations else "from fastapi import FastAPI"
    lines = [
        fastapi_import,
        *(["from fastapi.exceptions import RequestValidationError"] if handler_registrations else []),
        "", *imports, *handler_imports,
        "", "app = FastAPI()", *includes, *handler_registrations,
    ]
    return "\n".join(lines) + "\n"


def render_schema_section(file_path: str, class_fields: list[tuple[str, list[tuple[str, str]]]]) -> str:
    """`### {file_path}` + python code block 段落（05a 三章「Schema
    定義段」），`class_fields` 是 `(class_name, [(field_name,
    python_type), ...])` 清單。純機械字串組裝，欄位名/型別直接來自
    `type_mapping.extract_schema_fields()` 的輸出，不需要 LLM 生成這段
    文字本身（見 05a 三章）。

    **一律標成 `(BaseModel)` 並帶上 `from pydantic import BaseModel`**：
    這個函式目前只有一個呼叫端（`design.py` 產出 `schemas/{module}.py`
    的欄位描述），FastAPI 的請求/回應驗證與 OpenAPI 規格生成完全依賴
    Pydantic model，若只渲染成純 class（沒有繼承），④骨架生成呼叫的
    本地模型不一定會自己補上 `BaseModel` 繼承——這段文字本身雖然只是
    `directory_tree` 裡的 pseudocode（見三章開頭），但正是④判斷「這個
    類別該怎麼寫」的唯一依據，含糊的 pseudocode 會直接反映成含糊或
    錯誤的骨架。若未來這個函式被挪去給 `app/models/{module}.py`
    （SQLAlchemy ORM，見九章）共用，需要另外處理繼承對象，不能沿用
    這裡寫死的 `BaseModel`。

    **`Field` import 視內容需要才加**：`class_fields` 裡任一欄位的型別
    字串含 `"Field("`（`type_mapping.extract_schema_fields()` 對有驗證
    限制的欄位會產出 `"str = Field(..., max_length=50)"` 這種形狀，見
    該函式），才在 import 行加上 `Field`——沒有任何欄位帶限制時維持
    原本乾淨的 `from pydantic import BaseModel`，不無條件多 import 一個
    用不到的名稱。

    **`typing.Any` import 同理視內容需要才加**（對應
    docs/refactor_bug_trace.md #41）：`extract_schema_fields()` 對裸
    `object` schema 且無其他線索的欄位（如 Java `Void` 泛型抹除，真實
    案例 `ResponseResultVoid.data`）會退回 `Any` 型別——只要任一欄位型別
    字串含這個字（用 `\bAny\b` 邊界比對，避免誤判其他剛好含
    "Any" 子字串的識別字），就在 import 段加上
    `from typing import Any`，不需要時不多加。

    **一律加 `from __future__ import annotations`**：Java entity／DTO
    常見雙向關聯（如 `User` 含 `List[Order]`、`Order` 又含 `User`），轉成
    Pydantic model 若兩個類別分屬不同 `schemas/{module}.py`，逐字面型別
    註記在模組載入當下就會需要對方已經定義完成，容易撞上循環 import。
    這一行讓型別註記延遲求值（PEP 563），可以化解**同一個檔案內**
    的循環參照；跨檔案的循環 import（`schemas/user.py` 直接
    `import` `schemas/order.py`、反之亦然）不會被這一行解決——那需要
    `TYPE_CHECKING` guard 的匯入寫法＋明確呼叫 `model_rebuild()`，屬於
    ④如何實際生成、串接檔案間 import 的問題，不是③這裡的 pseudocode
    渲染能單獨解決的，留給 `07a_translator_cli_architecture.md`／
    `08a_scaffold_agent_architecture.md`（兩者皆待建立）處理。
    """
    needs_field_import = any("Field(" in python_type for _, fields in class_fields for _, python_type in fields)
    needs_any_import = any(
        re.search(r"\bAny\b", python_type) for _, fields in class_fields for _, python_type in fields
    )
    import_line = "from pydantic import BaseModel, Field" if needs_field_import else "from pydantic import BaseModel"
    lines = [f"### {file_path}", "```python", "from __future__ import annotations", ""]
    if needs_any_import:
        lines.append("from typing import Any")
        lines.append("")
    lines += [import_line, ""]
    for class_name, fields in class_fields:
        lines.append(f"class {class_name}(BaseModel):")
        if not fields:
            lines.append("    pass")
        else:
            for field_name, python_type in fields:
                lines.append(f"    {field_name}: {python_type}")
        lines.append("")
    lines.append("```")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_class_placeholder_section(
    file_path: str, classes: list[tuple[str, str, list[list[tuple[str, str]]]]]
) -> str:
    """`### {file_path}` + python code block 段落，延伸自 Schema 定義段
    同一種機制——`InterfaceSpec` 只能表達函式簽名，遇到「這個 class 在
    `module_list.methods` 裡完全沒有被追蹤到任何一般方法」的情況，同樣
    沒有結構化欄位可以表達。

    常見成因：自訂例外類別在 Java 端只有建構子、沒有一般方法，javalang
    的 `class_decl.methods` 不含建構子，這批 class 永遠不會被
    `_build_method_contexts()` 追蹤到。

    刻意不假設 Python 基底類別：這批 class 可能是例外類別，也可能是
    其他用途，只列出建構子簽名這個機械事實＋原始 Java 檔案路徑，交由
    ④／⑤依 Java 原始碼自行判斷。

    `classes`：`(class_name, java_file_path, constructor_param_lists)`
    清單，`constructor_param_lists` 是每個多載建構子各自的參數清單。
    """
    lines = [
        f"### {file_path}",
        "```python",
        "# 以下類別在 Java 端只有建構子、沒有被 module_list 追蹤到任何一般方法",
        "# （常見情況：自訂例外類別）。InterfaceSpec 無法表達這類定義，只能列出",
        "# 建構子簽名；實際 Python 對應寫法（Exception 子類別／dataclass／其他）",
        "# 請依下方標註的 Java 原始碼路徑自行判斷，這裡不假設任何基底類別。",
        "",
    ]
    for class_name, java_file_path, ctor_param_lists in classes:
        lines.append(f"# {class_name}（Java 原始碼：{java_file_path}）")
        if not ctor_param_lists:
            lines.append("# （無建構子參數）")
        for i, params in enumerate(ctor_param_lists, start=1):
            param_str = ", ".join(f"{name}: {ptype}" for name, ptype in params)
            lines.append(f"# 建構子 {i}: {class_name}({param_str})")
        lines.append("")
    lines.append("```")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_dataclass_section(
    file_path: str,
    classes: list[tuple[str, str, bool, list[tuple[str, str]], str]],
) -> str:
    """`### {file_path}` + python code block 段落，跟
    `render_class_placeholder_section()` 是同一種機制的另一個分支——
    差別在於這裡的 class 有欄位可以列，對應到的是純粹當籃子傳接值用
    的 Python `dataclass`，不是 `render_schema_section()` 的
    `(BaseModel)`（那是留給 API 邊界契約用的）。

    觸發情境：這批 class 沒有被任何 `InterfaceSpec` 覆蓋、也不是
    `@Entity`，可能因為原始碼裡有 Lombok annotation（javalang 看不到
    annotation processor 生成的 getter/setter，`methods` 因此是空
    清單）、也可能完全沒有 Lombok 標記、單純是沒寫存取方法的欄位容器
    （信心較低的機械推斷，見 `confidence_note`）。

    `classes`：`(class_name, java_file_path, frozen, field_list,
    confidence_note)` 清單。`frozen`：這個 class 的欄位是否全部
    `final`，決定渲染成 `@dataclass(frozen=True)` 還是一般
    `@dataclass`。
    """
    lines = [
        f"### {file_path}",
        "```python",
        "from dataclasses import dataclass",
        "",
        "# 以下類別在 Java 端沒有被 module_list 追蹤到任何一般方法、也不是",
        "# @Entity（DB schema 欄位規格是④的職責，這裡不重複）。依欄位宣告",
        "# 機械推斷為單純傳接值用的資料容器，對應 Python dataclass，不是",
        "# API 邊界的 BaseModel（那類契約走 openapi_spec 展開）。",
        "",
    ]
    for class_name, java_file_path, frozen, fields, confidence_note in classes:
        lines.append(f"# {class_name}（Java 原始碼：{java_file_path}；{confidence_note}）")
        decorator = "@dataclass(frozen=True)" if frozen else "@dataclass"
        lines.append(decorator)
        lines.append(f"class {class_name}:")
        if not fields:
            lines.append("    pass")
        for field_name, python_type in fields:
            lines.append(f"    {field_name}: {python_type}")
        lines.append("")
    lines.append("```")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_code_section(file_path: str, code: str) -> str:
    """`### {file_path}` + python code block，內容是完整程式碼（給
    `render_database_py()`／`render_main_py()` 這類全域基礎設施檔案
    用）。
    """
    return f"### {file_path}\n```python\n{code}```\n"


def render_directory_tree(
    directory_lines: list[str], schema_fragments: list[str], infra_sections: list[str]
) -> str:
    """把三段組成最終 `PythonStructure.directory_tree` 字串：目錄結構段
    （純文字樹狀圖）＋ Schema 定義段（各 module 的片段，逐波處理完成後
    依序附加）＋ 基礎設施段。
    """
    parts = ["\n".join(directory_lines), "", *schema_fragments, *infra_sections]
    return "\n".join(parts)
```

**測試**：`tests/design_agent/test_layout.py`（拓樸分波、循環/缺依賴偵測、schema/dataclass 渲染、utils package 判斷、`layer_for_class()`、`phase_for_layer()`、`file_path_for_utils()`）。

---

## 六、`prompts.py` / `llm.py`——LLM 契約與模型選擇

輸出契約刻意只問機械規則判斷不了的部分，不要求 LLM 吐出完整 `InterfaceSpec`：真正需要判斷的只剩三件事——(1) 無 stereotype 類別該歸哪一層、(2) 找不到 openapi 對應的參數該怎麼翻譯成 FastAPI 寫法、(3) repository／service 方法要不要加 `db: Session`。`design.py` 把機械前置資料組好，收到回應後再合併。

```python
# design_agent/prompts.py
from __future__ import annotations

DESIGN_SYSTEM_PROMPT = """\
你是協助把 Java（Spring Boot）專案設計成 Python（FastAPI + SQLAlchemy）
專案結構的助手。你會收到一個模組（module）的資料，任務是回答三類機械
規則無法決定的設計問題，不需要重新輸出整份介面規格。

輸入包含：
1. module：模組名稱與業務摘要
2. classes_needing_layer：這個模組裡沒有 Spring stereotype annotation
   的類別（如 `@Service`／`@Repository`／`@RestController` 都沒有），
   需要你依業務語意判斷它屬於 FastAPI 專案的哪一層：
   - "routers"：直接處理 HTTP 請求/回應
   - "services"：業務邏輯層
   - "repositories"：資料存取層
   多數情況會落在 "services"，但如果類別明顯只做資料查詢/存取，判斷為
   "repositories"。
3. methods：這個模組的每一個方法，包含方法簽名、業務描述、以及需要你
   判斷的項目（可能兩者都有，也可能都沒有——都沒有的方法不需要你輸出
   任何 method_decisions 項目）：
   - uncovered_params：Java 簽名裡有、但在 openapi_spec 業務參數裡找不到
     對應的參數（候選框架注入物件，如 HttpServletRequest／Authentication／
     從 header 取出的 token）。針對每一個，決定它在 FastAPI 對應的
     寫法，輸出到 extra_params——每一筆是 `{"name": "參數名稱",
     "type": "型別寫法"}`，例如 `{"name": "request", "type": "Request"}`、
     `{"name": "current_user", "type": "Annotated[User,
     Depends(get_current_user)]"}`——依 Java 型別與方法描述的語境判斷
     合理的 FastAPI 慣例寫法，不要直接照抄 Java 型別名稱。
   - needs_db_session_decision：true 代表這個方法屬於 repositories／
     services 層（或所屬類別的層級由你在 classes_needing_layer 決定），
     需要你判斷這個方法要不要加一個 `db: Session` 參數（SQLAlchemy
     session，供實際存取資料庫用）——純轉發、純計算、不碰資料庫的方法
     可以判斷不需要。
4. upstream_interfaces：這個模組依賴的上游模組已經產出的介面簽名，
   純參考用，讓你知道有哪些函式/類別已經存在，不要在 extra_params 裡
   虛構呼叫不存在的東西。

輸出兩個陣列：

1. class_layers：對每一個 classes_needing_layer 裡的類別，輸出
   `{class_name, layer}`——**每一個都要回答，不能省略**。
2. method_decisions：對每一個「有 uncovered_params 或
   needs_db_session_decision=true」的方法，輸出
   `{signature_key, extra_params, needs_db_session}`——**每一個都要
   回答，不能省略**；`extra_params` 沒有需要新增的參數時給空陣列；
   `needs_db_session` 一律要有明確的 true/false，不能省略這個欄位。

不要輸出任何其他文字，不要用 markdown code fence 包裹。
"""

DESIGN_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "class_layers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "class_name": {"type": "string"},
                    "layer": {"type": "string", "enum": ["routers", "services", "repositories"]},
                },
                "required": ["class_name", "layer"],
                "additionalProperties": False,
            },
        },
        "method_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "signature_key": {"type": "string"},
                    "extra_params": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string"},
                            },
                            "required": ["name", "type"],
                            "additionalProperties": False,
                        },
                    },
                    "needs_db_session": {"type": "boolean"},
                },
                "required": ["signature_key", "extra_params", "needs_db_session"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["class_layers", "method_decisions"],
    "additionalProperties": False,
}
```

```python
# design_agent/llm.py
"""③ 專屬的 Claude API 模型選擇。實際呼叫邏輯（client 初始化、
Structured Outputs、log_usage() 整合、錯誤處理）在 common/llm_client.py，
所有需要呼叫 Claude API 的 Agent 共用同一份。
"""
from __future__ import annotations

import os

from common.llm_client import DEFAULT_MODEL_FALLBACK

DEFAULT_MODEL = os.environ.get("DESIGN_AGENT_MODEL", DEFAULT_MODEL_FALLBACK)
```

---

## 七、`design.py`——逐波呼叫 Claude API、組裝 InterfaceSpec

LLM 呼叫契約刻意只問機械規則判斷不了的部分（見六章）；本檔負責把機械規則能決定的部分（層級、檔名、私有方法命名、Java→Python 型別對應、API 邊界方法的 openapi 覆寫）先算好，組成 `_MethodContext`，再組出真正需要 LLM 回答的最小問題集合，收到回應後合併回完整的 `InterfaceSpec`。

```python
# design_agent/design.py
"""③ 架構設計 Agent：六章 LLM 設計階段——依 depends_on 拓樸分波，波內
平行呼叫 Claude API，逐模組組裝 InterfaceSpec 與 directory_tree 片段。

**LLM 呼叫契約刻意只問「機械規則判斷不了的部分」**，見 `prompts.py`
module docstring；本檔負責把「機械規則能決定的部分」（層級、檔名、
私有方法命名、Java→Python 型別對應、API 邊界方法的 openapi 覆寫）先
算好，組成 `_MethodContext`，再組出這次呼叫真正需要 LLM 回答的最小
問題集合，收到回應後合併回完整的 `InterfaceSpec`。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import javalang
import javalang.tree

from common.concurrency import default_concurrency
from common.java_annotations import DATA_CLASS_ANNOTATIONS, JPA_ENTITY_ANNOTATIONS
from common.jpa_base_repository import (
    BASE_REPOSITORY_CLASS,
    BASE_REPOSITORY_CONTENT,
    BASE_REPOSITORY_FILE,
    JPA_BASE_METHOD_NAME_MAP,
    synthetic_java_method_id,
)
from common.llm_client import LlmJsonError, call_claude_for_json
from design_agent import global_infra, layout, signature_scan, type_mapping
from design_agent.exceptions import DesignAgentCoverageError, DesignAgentModuleError
from design_agent.llm import DEFAULT_MODEL
from design_agent.prompts import DESIGN_OUTPUT_SCHEMA, DESIGN_SYSTEM_PROMPT
from design_agent.types import JavaClassSignature, JavaMethodSignature, ModuleDesignResult, UncoveredParam
from graph.state import ApiMapping, InterfaceSpec, ModuleInfo, ParamSpec

logger = logging.getLogger(__name__)

# 併發數＝可用核心數 - 1，跟 parse_agent/summarize.py、
# spec_collection_agent/chain_dependency_detect.py 共用同一份
# common/concurrency.py 實作，不各自重新推導（見 00 六章、05a 六章
# 步驟 2）。
_MAX_WAVE_WORKERS = default_concurrency()
# 05a 六章「單一 module 呼叫失敗時」：待重試清單、5 分鐘後統一重試一次，
# 比照 04a 四章的等待秒數。
_RETRY_WAIT_SECONDS = 300.0


@dataclass
class _MethodContext:
    """單一 Java 方法（可能是某個多載）在呼叫 LLM 之前已經機械算好的
    所有事實，design.py 內部使用，不跨模組交接。"""

    signature_key: str
    java_method: str
    class_name: str
    complexity: str
    function_name: str  # 已套用 camelCase→snake_case 與私有方法底線前綴（05a 七章）
    java_method_id: str  # 逐字沿用①既有的 method_id() 格式，見下方建構處說明
    layer: str | None  # None 代表機械規則判斷不了，等 LLM 的 class_layers 決定
    params: list[ParamSpec]  # 已知的業務參數，不含 LLM 決定的框架注入/db session 參數
    return_type: str
    uncovered_params: list[UncoveredParam]
    needs_db_session_decision: bool
    boundary_schemas: list[tuple[str, dict]] = field(default_factory=list)  # (schema_name, resolved_schema)，見五章
    http_method: str | None = None  # 僅這個 overload 精確查到 boundary 時非 None，見五章「router 層 API 邊界方法額外帶」
    route_path: str | None = None   # 同上，直接是 boundary["endpoint"] 原始字面值，不做轉換


def design_all_modules(
    module_list: list[ModuleInfo],
    api_to_python_target: list[ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    skip_excluded_overloads: list[tuple[str, str, str]],
) -> tuple[
    list[InterfaceSpec], str, set[str], dict[str, dict[str, str]], list[dict[str, str]], dict[str, dict]
]:
    """對外入口，對應 05a 六章全節。回傳
    `(全部 module 攤平的 InterfaceSpec 清單, 組裝完成的 directory_tree 字串,
    實際產出過 schemas/{module}.py 的 module 名稱集合,
    config_field_mappings, config_env_vars, java_index)`。

    第三個回傳值供 `route_mapping.build_route_mappings()` 判斷
    `related_files` 該不該納入 schema 檔案——不能只憑「這個 module 有沒有
    router 檔案」猜測，同一個 module 的 API 邊界方法若全部只用 inline
    schema（沒有 `$ref`，見 `type_mapping.schema_name_for()`），這個
    module 就不會真的產出 `schemas/{module}.py`，猜測會讓
    `route_to_file_mapping` 指向一個 directory_tree 裡實際上不存在的
    「幽靈檔案」，Debug Agent（⑦）跟著這個路徑去讀檔會撲空。

    第四、第五個回傳值見 `graph/state.py PythonStructure.
    config_field_mappings`／`config_env_vars` docstring，對應
    `docs/09b_bug_trace.md` #45／#46——都由
    `global_infra.scan_value_injected_fields()`／`render_config_py()`
    同一次呼叫機械組出，跟六章逐 module 的 LLM 呼叫無關。

    第六個回傳值 `java_index`（`java_method_id → {file_path, class_name,
    function_name, phase}`）見 `graph/state.py PythonStructure.java_index`
    docstring、`06a_plan_agent_architecture.md` 六章——對 `all_interfaces`
    做一次收尾投影即可，每一筆 `InterfaceSpec` 都已經帶著自己的
    `java_method_id`（見下方各建構處），不需要另外重新計算。
    """
    waves = layout.build_waves(module_list)
    boundary_index = _build_boundary_index(api_to_python_target)

    all_interfaces: list[InterfaceSpec] = []
    schema_fragments: list[str] = []
    modules_with_schema_file: set[str] = set()
    interfaces_by_module: dict[str, list[InterfaceSpec]] = {}

    for wave in waves:
        wave_results = _design_wave_with_retry(
            wave, boundary_index, openapi_spec, java_project_path, interfaces_by_module, skip_excluded_overloads,
        )
        for result in wave_results:
            interfaces_by_module[result.module] = result.interfaces  # type: ignore[assignment]
            all_interfaces.extend(result.interfaces)  # type: ignore[arg-type]
            if result.directory_tree_fragment:
                schema_fragments.append(result.directory_tree_fragment)
                modules_with_schema_file.add(result.module)

    directory_lines = _render_directory_lines(
        all_interfaces, modules_with_schema_file, [m["module"] for m in module_list]
    )
    infra_sections = [
        layout.render_code_section("app/core/database.py", layout.render_database_py()),
        layout.render_code_section("app/main.py", layout.render_main_py(all_interfaces)),
    ]

    # #45（機械，見 global_infra.py module docstring 第 1 點）：
    # @Value("${key}") 屬性注入欄位 → app/core/config.py。
    value_fields = global_infra.scan_value_injected_fields(module_list, java_project_path)
    config_py_content, config_field_mappings, config_env_vars = global_infra.render_config_py(value_fields)
    if config_py_content is not None:
        infra_sections.append(layout.render_code_section(global_infra.CONFIG_PY_FILE, config_py_content))

    # #44 剩餘部分（LLM，見 global_infra.py module docstring 第 2 點）：
    # 只被 enum 實作的 interface（如 ErrorCode），交給 LLM 設計 Python
    # 對等寫法。全域一次性偵測，不分 module，也不依賴六章逐 module 的
    # LLM 呼叫結果。
    enum_backed_interfaces = global_infra.scan_enum_backed_interfaces(module_list, java_project_path)
    for file_path, python_source in global_infra.design_enum_backed_interfaces(enum_backed_interfaces):
        infra_sections.append(layout.render_code_section(file_path, python_source))

    # 對應 docs/refactor_bug_trace.md #10／#16：只要有任一 InterfaceSpec
    # 帶 jpa_base_entity（代表這個專案至少一個 repository 需要繼承
    # BaseRepository），才輸出 app/core/base_repository.py——沒有任何
    # Spring Data repository 的專案不需要這個檔案，比照 config.py「只在
    # 有需要時才輸出」的既有先例（見上方 config_py_content 判斷）。
    needs_base_repository = any(iface.get("jpa_base_entity") for iface in all_interfaces)
    if needs_base_repository:
        infra_sections.append(layout.render_code_section(BASE_REPOSITORY_FILE, BASE_REPOSITORY_CONTENT))

    directory_tree = layout.render_directory_tree(directory_lines, schema_fragments, infra_sections)

    java_index = {
        iface["java_method_id"]: {
            "file_path": iface["file_path"],
            "class_name": iface["class_name"],
            "function_name": iface["function_name"],
            "phase": iface["phase"],
        }
        for iface in all_interfaces
    }
    if needs_base_repository:
        # BaseRepository 的內建方法只實作一份、被所有 repository 繼承
        # （見 common/jpa_base_repository.py），不像一般 InterfaceSpec
        # 一一對應一個 Python 檔案位置——這裡固定指向同一個
        # BASE_REPOSITORY_FILE／BASE_REPOSITORY_CLASS，供 parse_agent/
        # call_graph.py::_yield_call() 合成的 synthetic_java_method_id()
        # 反查（見該函式對應處），讓 [P] 六章「呼叫鏈範圍查找」／
        # reference_targets 解析能找到這批繼承而來的方法的 Python 定義
        # 位置，即使它們從未各自產生過 InterfaceSpec。
        java_index.update(
            {
                synthetic_java_method_id(java_name): {
                    "file_path": BASE_REPOSITORY_FILE,
                    "class_name": BASE_REPOSITORY_CLASS,
                    "function_name": python_name,
                    "phase": 1,
                }
                for java_name, python_name in JPA_BASE_METHOD_NAME_MAP.items()
            }
        )

    return all_interfaces, directory_tree, modules_with_schema_file, config_field_mappings, config_env_vars, java_index


def _render_directory_lines(
    interfaces: list[InterfaceSpec], modules_with_schema_file: set[str], all_module_names: list[str]
) -> list[str]:
    """05a 三章「格式慣例」第 1 段：純文字樹狀圖，列出所有檔案路徑。
    `interfaces` 是 routers／services／repositories 三層的權威來源；
    `app/schemas/{module}.py` 只在 `modules_with_schema_file`（見
    `design_all_modules()` 「幽靈檔案」說明）裡的 module 才列出，跟
    `route_mapping.build_route_mappings()` 判斷 `related_files`
    用的同一個集合，避免這裡列出一個實際沒有 Schema 定義段的檔案；
    `app/models/{module}.py` 是每個 module 都有的 SQLAlchemy ORM 佔位
    檔案（05a 三章，欄位內容由④生成，見九章），因此對 `all_module_names`
    逐一補上，不像 schemas 需要視內容而定。
    """
    files = {iface["file_path"] for iface in interfaces} | {"app/main.py", "app/core/database.py"}
    files |= {layout.schema_file_path(m) for m in modules_with_schema_file}
    files |= {layout.model_file_path(m) for m in all_module_names}
    return ["app/", *[f"  {f}" for f in sorted(files)]]


# --------------------------------------------------------------------------
# 波次執行與重試佇列（05a 六章「單一 module 呼叫失敗時」，比照 04a 四章）
# --------------------------------------------------------------------------


def _design_wave_with_retry(
    wave: list[ModuleInfo],
    boundary_index: dict[tuple[str, str, str, str], ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    interfaces_by_module: dict[str, list[InterfaceSpec]],
    skip_excluded_overloads: list[tuple[str, str, str]],
) -> list[ModuleDesignResult]:
    """對一整波（同一波內彼此不依賴）module 平行呼叫 Claude，失敗的
    module 列入待重試清單，這一波其餘 module 跑完後等待
    `_RETRY_WAIT_SECONDS` 秒統一重試一次；重試仍失敗則中止整條
    design run（見 05a 六章：`python_structure` 是 [P]／④ 唯一的權威
    規格，任何一個 module 的介面缺失風險遠高於重新執行一次）。
    """
    results, failed = _run_wave_batch(
        wave, boundary_index, openapi_spec, java_project_path, interfaces_by_module, skip_excluded_overloads,
    )
    if not failed:
        return results

    logger.warning(
        "%d 個 module 的六章設計呼叫失敗，等待 %.0f 秒後統一重試一次: %s",
        len(failed),
        _RETRY_WAIT_SECONDS,
        [m["module"] for m in failed],
    )
    time.sleep(_RETRY_WAIT_SECONDS)

    retry_results, still_failed = _run_wave_batch(
        failed, boundary_index, openapi_spec, java_project_path, interfaces_by_module, skip_excluded_overloads,
    )
    results.extend(retry_results)

    if still_failed:
        raise DesignAgentModuleError(
            f"{len(still_failed)} 個 module 的六章設計呼叫重試後仍失敗，中止整個 design run"
            f"（05a 六章的保守預設，見本函式 docstring）: {[m['module'] for m in still_failed]}"
        )
    return results


def _run_wave_batch(
    modules: list[ModuleInfo],
    boundary_index: dict[tuple[str, str, str, str], ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    interfaces_by_module: dict[str, list[InterfaceSpec]],
    skip_excluded_overloads: list[tuple[str, str, str]],
) -> tuple[list[ModuleDesignResult], list[ModuleInfo]]:
    """平行處理一批 module（同一波內），回傳
    (成功結果清單, 失敗待重試的 ModuleInfo 清單)。每個 module 的失敗
    互相隔離，不取消其他 module（同一波內彼此本來就互不依賴）。
    """
    results: list[ModuleDesignResult] = []
    failed: list[ModuleInfo] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_WAVE_WORKERS, len(modules))) as pool:
        futures = {
            pool.submit(
                _design_module, m, boundary_index, openapi_spec, java_project_path,
                interfaces_by_module, skip_excluded_overloads,
            ): m
            for m in modules
        }
        for future in concurrent.futures.as_completed(futures):
            module = futures[future]
            try:
                results.append(future.result())
            except LlmJsonError as exc:
                logger.warning("module %s 的六章設計呼叫失敗，列入待重試清單: %s", module["module"], exc)
                failed.append(module)
    return results, failed


# --------------------------------------------------------------------------
# 單一 module 處理：機械前置 → （視需要）呼叫 Claude → 合併
# --------------------------------------------------------------------------


def _build_boundary_index(api_to_python_target: list[ApiMapping]) -> dict[tuple[str, str, str, str], ApiMapping]:
    """`(module, class_name, method_name, http_method) -> ApiMapping`，
    供 `_build_method_contexts()` 判斷一個方法是不是 API 邊界方法，並且
    是哪一個 HTTP method 的邊界方法。`ApiMapping.java_controller` 是
    `"ClassName.method_name"`（見 04a 六章 `assemble_api_mapping()`），
    這裡拆開重組成 key 的一部分。

    **對應 docs/09b_bug_trace.md #70 的根治修正**：原本的 key 只有
    `(module, class_name, method_name)`，同一個 class 內若有「同名、
    不同 HTTP method」的多載方法（Java 方法多載，靠 `@GetMapping`／
    `@PostMapping` 等 annotation 而非參數簽名區分，如
    `FileController.voice()` 的 POST 上傳／GET 下載兩個 overload），
    dict 賦值會互相覆寫，只留得住最後一筆——另一個多載完全查不到
    boundary，退回機械型別對應，翻出不合法的回傳型別注記，讓 FastAPI
    在匯入階段直接崩潰、整個服務起不來（真實案例見 #70）。加上
    `http_method` 這一維後，`voice()` 的 POST／GET 兩個 ApiMapping 各自
    有自己的 key，不再互相覆寫；`_build_method_contexts()` 那邊改成用
    `JavaMethodSignature.http_method`（見 `signature_scan.py` 對應
    extraction）直接查對應的那一筆，不需要再靠 `_select_boundary_
    overload()` 的參數個數猜測消歧——這個函式因此已移除，見 git 記錄。
    """
    index: dict[tuple[str, str, str, str], ApiMapping] = {}
    for api in api_to_python_target:
        class_name, _, method_name = api["java_controller"].partition(".")
        index[(api["module"], class_name, method_name, api["http_method"])] = api
    return index


def _python_function_name(java_method: str, is_private: bool) -> str:
    """05a 七章「私有／內部方法的命名慣例」：camelCase → snake_case，
    Java `private` 方法加底線前綴，保留在同一個 class／檔案內，不特別
    切出獨立檔案。
    """
    name = type_mapping.camel_to_snake(java_method)
    return f"_{name}" if is_private else name


def _build_method_contexts(
    module: ModuleInfo,
    class_signatures: dict[str, JavaClassSignature],
    boundary_index: dict[tuple[str, str, str, str], ApiMapping],
    openapi_spec: dict,
) -> list[_MethodContext]:
    """對應 05a 七章「強制規則：interfaces 必須涵蓋 module_list 裡每一
    個 module 的每一個方法」——以 `module["methods"]` 為準逐一處理，
    一個 `MethodInfo` 若在 Java 簽名裡對到多個多載，各自展開成獨立的
    `_MethodContext`（見 05a 四章「多載方法的處理」）。

    **`MethodInfo.class_name` 對不到真實 Java class／method 時立即中止**
    （`DesignAgentCoverageError`，見該類別 docstring「與 05b 原始設計的
    差異」）：這代表①的 Map 階段摘要跟③這次重新掃描 Java 原始碼的結果
    對不上——理論上不該發生（兩者都是對同一份 `java_project_path` 的
    解析結果），實測發生時代表某處有結構性 bug（如過去 `signature_scan.
    scan_java_files()` 漏掃 interface 宣告的案例），不是可以放著跳過的
    暫時性不一致。中止在呼叫任何 Claude API 之前發生，不浪費任何 LLM
    呼叫額度，錯誤訊息帶出精確的 module／class／method，方便直接定位
    是①的輸出問題還是③的掃描邏輯問題。

    **`(class_name, java_method)` 先去重，才逐一展開 overloads**：①對
    同一組多載方法會各自產生一筆獨立的 `MethodInfo`（例如
    `FileController.voice` 的 `@PostMapping`／`@GetMapping` 兩個 overload，
    各自帶不同的 `description`），這是①的正常行為，不是輸出錯誤。但
    `overloads` 是依「方法名稱」查找（javalang 沒有 overload resolution，
    見 04a 三章「決策」），同一組多載的每一筆 `MethodInfo` 查到的都是同
    一份完整 overloads 清單——若不先去重，兩筆 `MethodInfo` 會分別把整組
    overloads 各展開一次，[P] 的涵蓋率驗證會抓到重複的
    `(file_path, class_name, function_name)` 三元組而中止（實測對
    `lang-exam-api-refactor` 的 `FileController.voice`／`image` 兩組
    overload 觸發過）。這裡的去重只影響「同一組 overloads 只展開一次」，
    不影響 05a 四章既有的多載消歧邏輯（第一個保留原名、其餘加 `_2`/`_3`）
    ——那段邏輯本來就是針對「一次展開」設計的，去重後才符合它的前提。
    """
    known_classes = frozenset(class_signatures)
    contexts: list[_MethodContext] = []

    seen_method_keys: set[tuple[str, str]] = set()
    for method_info in module["methods"]:
        method_key = (method_info["class_name"], method_info["java_method"])
        if method_key in seen_method_keys:
            continue
        seen_method_keys.add(method_key)

        class_sig = class_signatures.get(method_info["class_name"])
        if class_sig is None:
            raise DesignAgentCoverageError(
                f"module {module['module']} 的方法 {method_info['class_name']}."
                f"{method_info['java_method']} 在四章重新掃描結果中找不到所屬類別"
                f"（見 DesignAgentCoverageError docstring）"
            )

        overloads = [m for m in class_sig.methods if m.method_name == method_info["java_method"]]
        if not overloads:
            raise DesignAgentCoverageError(
                f"module {module['module']} 的方法 {method_info['class_name']}."
                f"{method_info['java_method']} 在四章重新掃描結果中找不到同名方法"
                f"（見 DesignAgentCoverageError docstring）"
            )

        layer = layout.layer_for_class(class_sig.stereotype, class_sig.package)

        # 多載消歧（05a 四章「多載方法的處理」）：同一組 overloads 的
        # sig.method_name 相同，_python_function_name() 對每個 sig 都會
        # 算出同一個基礎名稱，必須疊加計數器消歧，否則同一個 class／
        # 檔案會產出兩個同名 InterfaceSpec（Python 不支援多載，屬於非法
        # 輸出）。第一次出現保留原始名稱，第二次起加 `_2`、`_3`……
        seen_names: dict[str, int] = {}

        for sig in overloads:
            # 對應 docs/09b_bug_trace.md #70 根治修正：每個 overload 各自
            # 用自己的 http_method 精確查 boundary_index，不再是迴圈外
            # 只查一次、套用到「猜出來的那一個」多載——「同名、不同 HTTP
            # method」的多載（如 voice() 的 POST／GET）現在都能各自找到
            # 自己對應的 ApiMapping，不會有一個被硬套用不屬於它的
            # operation、另一個完全查不到而被誤判為非邊界方法。
            boundary = (
                boundary_index.get((module["module"], class_sig.class_name, sig.method_name, sig.http_method))
                if sig.http_method is not None
                else None
            )
            operation = (
                type_mapping.find_operation(boundary["endpoint"], boundary["http_method"], openapi_spec)
                if boundary is not None
                else None
            )
            if operation is not None:
                params, return_type = type_mapping.resolve_api_boundary_signature(sig, operation, openapi_spec)
                uncovered = type_mapping.find_uncovered_framework_params(sig, operation, openapi_spec)
                # ResponseEntity<T> 方法的 return_type 已被覆寫成
                # "Response"（見 resolve_api_boundary_signature()「見
                # 09b_bug_trace.md #30」），不需要、也不該為它收集 response
                # schema——這種情況下 openapi 的 response schema 不代表
                # 這個方法真正的回傳形狀，硬收集只會產生一個永遠不會被
                # 引用的多餘 Pydantic class。
                boundary_schemas = (
                    []
                    if type_mapping.is_response_entity_return_type(sig.return_type)
                    else type_mapping.collect_named_schemas(operation, openapi_spec)
                )
                # 05a 五章「router 層 API 邊界方法額外帶 http_method／route_path」：
                # 只有真正被選中對應這個 endpoint 的多載才帶這兩個值，其餘多載
                # （下面 else 分支）維持 None，跟 params/return_type 的處理方式一致。
                http_method = boundary["http_method"]
                route_path = boundary["endpoint"]
            else:
                params = [
                    ParamSpec(name=p.name, type=type_mapping.map_java_type(p.java_type, known_classes))
                    for p in sig.params
                ]
                return_type = type_mapping.map_java_type(sig.return_type or "void", known_classes)
                uncovered = []
                boundary_schemas = []
                http_method = None
                route_path = None

            base_name = _python_function_name(sig.method_name, sig.is_private)
            seen_names[base_name] = seen_names.get(base_name, 0) + 1
            function_name = base_name if seen_names[base_name] == 1 else f"{base_name}_{seen_names[base_name]}"

            contexts.append(
                _MethodContext(
                    signature_key=sig.signature_key,
                    java_method=sig.method_name,
                    class_name=class_sig.class_name,
                    complexity=method_info["complexity"],
                    function_name=function_name,
                    # 逐字沿用①既有的 parse_agent/types.py::method_id() 格式
                    # （"{file_path}::{class_name}::{method_name}"，不含參數
                    # 型別），供 [P] 六章「呼叫鏈範圍查找」直接查①的呼叫圖，
                    # 見 05a_design_agent_architecture.md 對應章節。
                    java_method_id=f"{class_sig.file_path}::{class_sig.class_name}::{sig.method_name}",
                    layer=layer,
                    params=params,
                    return_type=return_type,
                    uncovered_params=uncovered,
                    # 三層都問，不只 services/repositories：FastAPI+SQLAlchemy 的
                    # db session 慣例上由 router 層的 `Depends(get_db)` 建立，
                    # 再一路傳給它呼叫的 service/repository（見 _design_module()
                    # 合併階段對 layer=="routers" 的特殊處理），router 方法若會
                    # 呼叫到需要 db 的下游，同樣需要宣告這個參數，不能排除在外。
                    needs_db_session_decision=True,
                    boundary_schemas=boundary_schemas,
                    http_method=http_method,
                    route_path=route_path,
                )
            )
    return contexts


def _reorder_params_defaults_last(params: list[ParamSpec]) -> list[ParamSpec]:
    """Python 函式簽名要求「帶預設值的參數必須排在不帶預設值的參數之後」
    （否則 `SyntaxError: parameter without a default follows parameter
    with a default`，已用真實案例驗證：`def f(a: str = 1, b: int): ...`）。
    下方 `_design_module()` 組 `params` 的順序是業務參數（`ctx.params`，
    Java 原始簽名順序，一律不帶預設值）→ `extra_params`（六章 LLM 決定
    的框架注入參數，型別字串可能像 `User = Depends(get_current_user)`
    這樣自帶預設值，順序由 LLM 回應決定，沒有保證）→ `db`（機械附加，
    只有 routers 層帶 `= Depends(get_db)` 預設值，其餘層級不帶）。任何
    一個 `extra_params` 項目若帶預設值、且後面接著沒有預設值的其他項目
    （另一個 `extra_params`，或機械附加的 `db`），組裝出的簽名就會違反
    這條規則——這不是理論邊角案例，只要 LLM 判斷某個框架注入參數該用
    FastAPI 的 `Depends(...)` 慣例（05a 五章「框架注入物件」原文舉的
    例子就是這個），就有機會踩到。

    穩定分割（stable partition）：不帶預設值的參數維持原有相對順序排在
    前面，帶預設值的參數維持原有相對順序排在後面——不改變同一類別內部
    的順序，只調整「有沒有預設值」這一個維度，保證輸出永遠是合法 Python
    函式簽名，不需要要求 LLM 或後續哪個環節自己保證順序正確。用字面
    `"="` 子字串比對判斷「帶不帶預設值」：這個專案目前所有帶預設值的
    型別字串都來自這裡（`db` 的機械附加）或六章 LLM 的 `extra_params`
    （如 `Depends(...)` 慣例），`map_java_type()`／openapi 覆寫產出的
    型別字串從不含 `=`，比對不會誤判。
    """
    no_default = [p for p in params if "=" not in p["type"]]
    with_default = [p for p in params if "=" in p["type"]]
    return no_default + with_default


# 保留模組名稱，承接 parse_agent/summarize.py `_assemble_global_advice_
# draft()` 產出的全域生效類別（`@RestControllerAdvice`／
# `@ControllerAdvice`，見 09b_bug_trace.md #11/#12）。跟 summarize.py 那
# 邊的 `_GLOBAL_MODULE_NAME` 是同一個字面值，兩邊各自定義常數而不共用
# 匯入——`parse_agent`／`design_agent` 是各自獨立套件，不互相 import
# （01 二章「設計原則」），這個字面值本身極不可能變動，重複定義的維護
# 成本遠低於為了共用一個常數在兩個套件之間建立耦合。
_GLOBAL_MODULE_NAME = "_global"


def _exception_handler_targets(
    java_files: list[str], java_project_path: str
) -> tuple[dict[str, str], dict[str, str]]:
    """重新掃 `_global` module 的 `java_files`，逐 method 找
    `@ExceptionHandler(X.class)` 的目標例外類別名稱，回傳
    `({method_name: exception_class_name}, {class_name: file_path})`。
    **只收「單一 class-literal」形式**（javalang 把 `X.class`解析成
    `ClassReference(type=ReferenceType(name="X"))`）——
    `@ExceptionHandler({A.class, B.class})` 這種陣列形式的 `element`
    不是 `ClassReference`，不會被收進這份對照表，呼叫端
    （`_design_global_advice_module()`）對查不到的方法一律記警告略過，
    不嘗試處理（見 09b_bug_trace.md #11/#12「範圍刻意收斂」）。這是獨立
    於 `signature_scan.scan_java_files()` 之外的小型專用掃描——一般業務
    module 不需要 annotation 的字面值，只有這個特殊模組需要，不值得為了
    這一種用途替 `JavaMethodSignature` 加欄位。

    第二個回傳值（`class_name → file_path`）在同一次掃描順手收集，供
    `_design_global_advice_module()` 組 `java_method_id` 用，不需要
    另外再掃一次同一批檔案。
    """
    targets: dict[str, str] = {}
    class_file_paths: dict[str, str] = {}
    for rel_path in java_files:
        source = Path(java_project_path, rel_path).read_text(encoding="utf-8")
        tree = javalang.parse.parse(source)
        for decl in tree.types:
            if not isinstance(decl, javalang.tree.ClassDeclaration):
                continue
            class_file_paths[decl.name] = rel_path
            for method_decl in decl.methods:
                for ann in method_decl.annotations:
                    if ann.name != "ExceptionHandler":
                        continue
                    element = ann.element
                    if isinstance(element, javalang.tree.ClassReference):
                        targets[method_decl.name] = element.type.name
    return targets, class_file_paths


def _design_global_advice_module(module: ModuleInfo, java_project_path: str) -> ModuleDesignResult:
    """`_global` 保留模組的專屬處理路徑，完全繞過一般模組的
    `_build_method_contexts()`／`_call_design_llm()`（不查
    `_STEREOTYPE_LAYER`，這批方法不屬於 routers/services/repositories
    任何一層的既有分層慣例）。固定輸出 `layout.EXCEPTION_HANDLERS_FILE`、
    `class_name=None` 自由函式，簽名比照 FastAPI `@app.exception_
    handler(...)` 的呼叫慣例（`request: Request, exc: Exception) ->
    Response`），不是 Java 原始簽名的機械轉換——FastAPI 的例外處理器
    協定本來就要求這個固定形狀，跟 Java 端的方法簽名無關。

    **範圍刻意收斂**：只處理 `@ExceptionHandler(Exception.class)` 這種
    全域 catch-all case（見 `docs/09b_bug_trace.md` #11/#12 唯一有真實
    案例佐證的情況）。`module["methods"]` 裡若有方法不是
    `@ExceptionHandler`、或標註了 `Exception` 以外的例外類型（如真實
    案例裡同時存在的 `handleBaseException(BaseException e)`），只記
    警告、不產生任何 `InterfaceSpec`——這些方法因此不會流進 [P] 的
    task list，不會被實作，是已知、刻意接受的限制，不是遺漏。函式
    本體（哪個例外對應什麼 code/msg）不在這裡機械產生——比照一般
    service/repository 方法，經 [P] 產生 task、走⑤既有的
    `fill_function()` 流程翻譯 Java handler 方法本體，見
    `09b_bug_trace.md` #11/#12「① 已完成的前置修正」。
    """
    targets, class_file_paths = _exception_handler_targets(module["java_files"], java_project_path)

    interfaces: list[InterfaceSpec] = []
    for method_info in module["methods"]:
        java_method = method_info["java_method"]
        target = targets.get(java_method)
        if target != "Exception":
            logger.warning(
                "module %s 的方法 %s.%s 不是 @ExceptionHandler(Exception.class) 這種全域 "
                "catch-all case（實際目標：%s），範圍刻意收斂只處理 catch-all，略過（見 "
                "docs/09b_bug_trace.md #11/#12「範圍刻意收斂」）",
                module["module"], method_info["class_name"], java_method, target,
            )
            continue
        interfaces.append(
            InterfaceSpec(
                file_path=layout.EXCEPTION_HANDLERS_FILE,
                class_name=None,
                function_name=_python_function_name(java_method, is_private=False),
                params=[ParamSpec(name="request", type="Request"), ParamSpec(name="exc", type="Exception")],
                return_type="Response",
                http_method=None,
                route_path=None,
                # 全域例外處理由框架直接呼叫，語意上跟 routers 層同屬
                # Phase 2（見 refactor_plan.md 二章：不套用 _STEREOTYPE_LAYER
                # 也不是 utils／repository，機械歸為 Phase 2）。
                phase=2,
                java_method_id=f"{class_file_paths[method_info['class_name']]}::"
                                f"{method_info['class_name']}::{java_method}",
            )
        )

    return ModuleDesignResult(module=module["module"], interfaces=interfaces, directory_tree_fragment=None)


def _filter_skip_excluded_overloads(
    class_signatures: dict[str, JavaClassSignature],
    skip_excluded_overloads: list[tuple[str, str, str]],
) -> dict[str, JavaClassSignature]:
    """使用者填 skip，是人工判斷「這個 endpoint 整段不進翻譯流程」，
    不只是跳過自動化測試（見
    `docs/03a_spec_collection_agent_architecture.md`「Decision.SKIP 的
    語意」）。這裡在 `signature_scan.scan_java_files()` 重新掃描出**每個
    物理多載各自的 `http_method`** 之後，立刻用 ①
    `skip_filter.compute_skip_excluded_overloads()` 算出的 HTTP method
    精確排除清單，把該排除的多載從 `class_sig.methods` 濾掉，產生一份
    新的、乾淨的資料——`_build_method_contexts()` 後續展開 overloads／
    生成骨架的既有邏輯完全不用改，因為它讀到的資料本來就不會再包含
    這個多載了（見 `docs/refactor_bug_trace.md` #8 的討論）。

    只比對 `(class_name, method_name, http_method)`：`skip_excluded_
    overloads` 沒有帶檔案路徑那一段，同一次呼叫裡 `class_signatures`
    是逐檔案分開存的 dict（key 是 class 名稱），同一個 class_name 不會
    跨檔案重複出現在同一個 `_design_module()` 呼叫裡，不需要檔案路徑
    也能唯一定位。`JavaClassSignature` 是 frozen dataclass，過濾後的
    `methods` 用 `dataclasses.replace()` 產生新實例，不原地修改。
    """
    if not skip_excluded_overloads:
        return class_signatures
    excluded = set(skip_excluded_overloads)
    return {
        class_name: replace(
            sig,
            methods=[
                m for m in sig.methods
                if (class_name, m.method_name, m.http_method) not in excluded
            ],
        )
        for class_name, sig in class_signatures.items()
    }


# Spring Data JpaRepository/CrudRepository 內建的無過濾查詢方法，Java 原始碼
# 裡從不會顯式宣告（見 `_synthesize_inherited_repository_reads()` docstring），
# 目前只合成這一個、最常見也最安全的預設值，刻意不擴大到 save／findById／
# deleteById 等其他內建方法——那些方法的參數／回傳型別需要更多假設（如
# 主鍵型別），真的遇到再視真實案例擴充，不預先猜測。
_INHERITED_REPOSITORY_READ_METHOD = "findAll"


def _synthesize_inherited_repository_reads(
    class_signatures: dict[str, JavaClassSignature],
    module: ModuleInfo,
    layer_by_class: dict[str, str],
    covered_class_names: set[str],
    known_classes: frozenset[str],
) -> list[InterfaceSpec]:
    """對應 docs/refactor_bug_trace.md 真實案例：`QuestionRepository` 唯一
    顯式宣告的方法 `findByTypeAndPart()` 因為只被一個使用者標記 skip 的
    endpoint 呼叫（`GradingController.getRandomQuestions()`），被①的呼叫鏈
    排除機制正確地從 `module["methods"]` 移除；但另一個沒有被 skip 的方法
    `QuestionService.getList()` 仍然呼叫 `questionRepository.findAll()`——
    `findAll()` 繼承自 `JpaRepository`，Java 原始碼裡從來沒有顯式宣告，
    `signature_scan.scan_java_files()` 天生看不到它。

    `_design_module()` 既有的孤兒類別判斷（見該函式下方迴圈）以
    `sig.methods`（③對原始檔案的重新掃描，不受①的呼叫鏈排除影響）是否
    非空來判斷「這個類別是不是已經被某個 InterfaceSpec 涵蓋」——但
    `QuestionRepository.methods` 因為 `findByTypeAndPart` 還在（③沒有跟著
    ①排除），永遠是非空，導致這個類別被誤判成「已處理」而整個跳過孤兒
    判斷，連一行警告都不會留下：不是「產生了但缺方法」，是完全沒有任何
    痕跡。

    這裡在孤兒判斷之前先跑一輪：找出「③認為有方法、但一個方法都沒被
    `_build_method_contexts()` 實際收錄」的類別（`sig.methods` 非空、
    `name not in covered_class_names`），且這個類別仍被同模組另一個
    「存活」（有被收錄的方法）類別以欄位方式引用——這代表它不是真的沒人
    用，只是唯一的顯式方法被上游排除掉。若這個類別的層級解析為
    `repositories`，且 `sig.jpa_base_entity` 有值（見 `signature_scan.
    scan_java_files()`，直接從 `extends JpaRepository<Entity, Id>` 讀出，
    不是猜測），合成一筆 `find_all()` 的 InterfaceSpec，讓下游呼叫它的
    函式有真正的目標可以呼叫——其餘情況（層級不是 repositories，或這個
    interface 沒有繼承 Spring Data 基底介面）只記警告，不合成（無法確定
    安全預設值，見 `_INHERITED_REPOSITORY_READ_METHOD` docstring「刻意
    窄範圍」）。合成的 InterfaceSpec 帶上同一個 `jpa_base_entity`，供
    `translator_cli/scaffold.py` 渲染 `class X(BaseRepository[Entity])`
    繼承宣告用（對應 docs/refactor_bug_trace.md #10／#16 的統一修法）。

    刻意只在這裡新增警告訊息，不去更動既有孤兒判斷迴圈本身的邏輯（它對
    `sig.methods` 真的是空清單的案例運作正確，不需要跟著改)。
    """
    synthesized: list[InterfaceSpec] = []
    for name, sig in class_signatures.items():
        if not sig.methods or name in covered_class_names:
            continue
        still_referenced = any(
            f.java_type == name
            for other_name, other_sig in class_signatures.items()
            if other_name in covered_class_names
            for f in other_sig.fields
        )
        if not still_referenced:
            continue
        resolved_layer = layout.layer_for_class(sig.stereotype, sig.package) or layer_by_class.get(name)
        if resolved_layer != "repositories":
            logger.warning(
                "module %s 的類別 %s 的方法全部被上游排除（如呼叫鏈排除），"
                "但仍被其他存活類別以欄位方式引用，且不是 repositories 層，"
                "無法套用內建 CRUD 方法的安全預設值，需要人工檢查是否有下游"
                "呼叫會找不到目標",
                module["module"], name,
            )
            continue
        entity_type = sig.jpa_base_entity
        if entity_type is None:
            logger.warning(
                "module %s 的 repository 類別 %s 的方法全部被上游排除，"
                "但仍被其他存活類別以欄位方式引用，且這個 interface 沒有"
                "繼承 JpaRepository/CrudRepository/PagingAndSortingRepository，"
                "無法合成 %s()，需要人工檢查",
                module["module"], name, _INHERITED_REPOSITORY_READ_METHOD,
            )
            continue
        logger.warning(
            "module %s 的 repository 類別 %s 的方法全部因呼叫鏈排除被移除，"
            "但仍被其他存活類別依賴其繼承自 JpaRepository/CrudRepository 的"
            "內建方法，合成一個 find_all() 佔位介面",
            module["module"], name,
        )
        synthesized.append(
            InterfaceSpec(
                file_path=layout.file_path_for_layer(module["module"], "repositories"),
                class_name=sig.class_name,
                function_name="find_all",
                params=[ParamSpec(name="db", type="Session")],
                return_type=type_mapping.map_java_type(f"List<{entity_type}>", known_classes),
                phase=layout.phase_for_layer("repositories"),
                java_method_id=f"{sig.file_path}::{sig.class_name}::{_INHERITED_REPOSITORY_READ_METHOD}",
                jpa_base_entity=entity_type,
            )
        )
    return synthesized


def _design_module(
    module: ModuleInfo,
    boundary_index: dict[tuple[str, str, str, str], ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    interfaces_by_module: dict[str, list[InterfaceSpec]],
    skip_excluded_overloads: list[tuple[str, str, str]],
) -> ModuleDesignResult:
    """對應 05a 六章「單一 module 的 Claude 呼叫內容」全表格：組出這個
    module 的 `_MethodContext` 清單、決定這次呼叫真正需要 LLM 回答的
    問題（無 stereotype 類別的層級、框架注入參數、db session 判斷），
    視需要呼叫一次 Claude，合併回完整的 `InterfaceSpec`。

    **`_GLOBAL_MODULE_NAME` 走完全獨立的專屬路徑**（見
    `_design_global_advice_module()`）：這個保留模組的方法不屬於任何
    業務分層慣例，機械決策即可，不需要（也不該）套用下面一般模組的
    LLM 層級判斷／API 邊界覆寫邏輯。
    """
    if module["module"] == _GLOBAL_MODULE_NAME:
        return _design_global_advice_module(module, java_project_path)

    class_signatures = signature_scan.scan_java_files(module["java_files"], java_project_path)
    class_signatures = _filter_skip_excluded_overloads(class_signatures, skip_excluded_overloads)
    contexts = _build_method_contexts(module, class_signatures, boundary_index, openapi_spec)

    # 只問「有一般方法」、機械規則判斷不了層級的類別：一個 class 若
    # `methods` 是空清單（常見情況：只有建構子的自訂例外類別，見
    # JavaClassSignature.constructors docstring），永遠不會有任何
    # _MethodContext 引用到它（overloads 永遠篩不到東西），LLM 回答的
    # 層級因此永遠用不到——這批 class 改走下方 orphan class 處理，不需要
    # 也不該浪費一次 LLM 問答在一個沒有答案會被使用的問題上。**落在
    # utils package 下的類別也排除**：`layer_for_class()` 已經機械判定
    # 出 "utils"（不是 None），不屬於「機械規則判斷不了」的情況，見
    # `layout.is_utils_package()`、05a 三章「Utils 特例」。
    classes_needing_layer = [
        c for c in class_signatures.values()
        if c.stereotype is None and c.methods and not layout.is_utils_package(c.package)
    ]
    methods_needing_decision = [c for c in contexts if c.uncovered_params or c.needs_db_session_decision]

    if classes_needing_layer or methods_needing_decision:
        layer_by_class, extra_params_by_key, db_session_by_key = _call_design_llm(
            module, classes_needing_layer, methods_needing_decision, interfaces_by_module
        )
    else:
        layer_by_class, extra_params_by_key, db_session_by_key = {}, {}, {}

    interfaces: list[InterfaceSpec] = []
    schema_class_fields: list[tuple[str, list[tuple[str, str]]]] = []
    seen_schema_names: set[str] = set()

    for ctx in contexts:
        layer = ctx.layer or layer_by_class.get(ctx.class_name)
        if layer is None:
            logger.warning(
                "module %s 的類別 %s 沒有 stereotype、LLM 也沒有回答 class_layers，"
                "略過這個方法（05a 六章要求 class_layers 必須逐一回答，理論上走不到"
                "這裡，除非重試佇列已經放行——見 05a 十二章保守中止預設）",
                module["module"], ctx.class_name,
            )
            continue

        params = list(ctx.params)
        params.extend(extra_params_by_key.get(ctx.signature_key, []))
        if db_session_by_key.get(ctx.signature_key, False):
            # FastAPI 的 DI 只在「被 @router 裝飾的端點函式」這一層生效，
            # 只有 routers 層需要 `= Depends(get_db)` 讓 FastAPI 實際建立
            # session；services/repositories 層的 db 是呼叫端（router 或
            # 上一層）以一般引數往下傳，宣告成不帶預設值的 `db: Session`
            # 才不會被 FastAPI 誤判成一般參數硬要求呼叫端另外提供。這裡
            # 用「最終解析出的 layer」（可能來自 LLM 的 class_layers）
            # 判斷，不是 ctx.layer（那個在無 stereotype 類別時還沒定案）。
            db_type = "Session = Depends(get_db)" if layer == "routers" else "Session"
            params.append(ParamSpec(name="db", type=db_type))
        params = _reorder_params_defaults_last(params)

        # utils 不套用 {module}_{layer}.py 規則、不分 module，見
        # layout.file_path_for_utils() docstring、05a 三章「Utils 特例」；
        # class_name 比照 routers 層既有慣例設 None（Java @UtilityClass
        # 靜態方法，翻譯慣例上是模組層級函式，不是類別方法）。
        file_path = (
            layout.file_path_for_utils(ctx.class_name)
            if layer == "utils"
            else layout.file_path_for_layer(module["module"], layer)
        )
        interfaces.append(
            InterfaceSpec(
                file_path=file_path,
                class_name=None if layer in ("routers", "utils") else ctx.class_name,
                function_name=ctx.function_name,
                params=params,
                return_type=ctx.return_type,
                http_method=ctx.http_method,
                route_path=ctx.route_path,
                phase=layout.phase_for_layer(layer),
                java_method_id=ctx.java_method_id,
                jpa_base_entity=class_signatures[ctx.class_name].jpa_base_entity if ctx.class_name in class_signatures else None,
            )
        )

        for schema_name, resolved_schema in ctx.boundary_schemas:
            if schema_name in seen_schema_names:
                continue
            seen_schema_names.add(schema_name)
            schema_class_fields.append((schema_name, type_mapping.extract_schema_fields(resolved_schema)))

    # orphan class：class_signatures 裡沒有任何 InterfaceSpec 的 class_name
    # 指向它——這批 class 完全不會被 contexts 迴圈碰到（見上面
    # classes_needing_layer 的說明），若不另外處理，在 python_structure
    # 裡會完全沒有任何痕跡。依機械事實分六種情況處理（見 05a 三章「孤兒
    # 類別／資料容器占位」判斷優先序，理由見 common/java_annotations.py
    # docstring）：
    #   0. 這個類別名稱已經是 openapi_spec 裡的具名 schema（見下方
    #      globally_named_schemas）→ 跳過，不渲染任何東西，見下方詳述
    #   1. @Entity（含 @Embeddable/@MappedSuperclass）→ 跳過，DB schema
    #      欄位規格是④的職責（05a 九章），不是③要處理的範圍
    #   2. 有 Lombok/JPA 資料標記 → 渲染 dataclass（欄位為主，高信心）
    #   3. 有建構子（現有 AuthException 案例，順序與行為不變）→ 渲染
    #      建構子占位
    #   4. 有欄位、無標記（低信心推斷）→ 渲染 dataclass
    #   5. 什麼都沒有 → 只記警告，不渲染空段落
    # 已經出現在 seen_schema_names（這個模組自己的 API 邊界 openapi 展開
    # 產出的 schema 名稱）的 class 一併排除，避免同一個型別被兩種機制
    # 各渲染一次。
    #
    # **第 0 種情況對應 docs/refactor_bug_trace.md #25**：`openapi_spec
    # ['components']['schemas']` 是全專案唯一一份、04a 就已經產生好的
    # 具名 schema 字典——一個類別名稱若已經在裡面，代表它有一份「已經翻
    # 好、放進生成專案」的正確定義，只是可能是被**另一個**模組的 API
    # 邊界方法透過 `ctx.boundary_schemas`（見上面迴圈）登記進去的，不是
    # 這個模組自己的端點。修好之前，這裡完全沒有查過 openapi_spec，只憑
    # Java AST 上的 Lombok／建構子／欄位事實猜測，對這種「不是我的端點、
    # 但剛好是別人已經翻好的具名 schema」的類別一律誤判成孤兒、另外渲染
    # 一份 dataclass——真實案例：`CreaterandomRs` 同時被 `candidate` 模組
    # （openapi 具名 schema，正確渲染成 Pydantic BaseModel）跟 `exam`
    # 模組（誤判成孤兒，渲染成不相容的 dataclass）各自定義一份，兩者是
    # 不同的 class 物件，`candidate_router.py` 收到 `exam` 那份時 Pydantic
    # 直接判定驗證失敗。**只把「渲染」關掉，不需要另外處理 import**：
    # 這個類別不在這個模組渲染之後，`translator_cli/scaffold.py` 既有的
    # 全域 `custom_type_index`（#8／#17 已經建好的機制）會自動解析到唯一
    # 定義它的那個模組，不需要新增任何 import 解析邏輯——這正是「只翻譯
    # 一次、其他地方直接沿用同一份範本」該有的樣子。
    globally_named_schemas = frozenset((openapi_spec.get("components") or {}).get("schemas") or {})
    known_classes = frozenset(class_signatures)
    covered_class_names = {iface["class_name"] for iface in interfaces if iface["class_name"]}
    inherited_repository_reads = _synthesize_inherited_repository_reads(
        class_signatures, module, layer_by_class, covered_class_names, known_classes
    )
    interfaces.extend(inherited_repository_reads)
    covered_class_names |= {iface["class_name"] for iface in inherited_repository_reads if iface["class_name"]}
    orphan_classes: list[tuple[str, str, list[list[tuple[str, str]]]]] = []
    data_carrier_classes: list[tuple[str, str, bool, list[tuple[str, str]], str]] = []

    def _data_carrier_entry(
        sig: JavaClassSignature, confidence_note: str
    ) -> tuple[str, str, bool, list[tuple[str, str]], str]:
        frozen = bool(sig.fields) and all(f.is_final for f in sig.fields)
        fields = [(f.name, type_mapping.map_java_type(f.java_type, known_classes)) for f in sig.fields]
        return (sig.class_name, sig.file_path, frozen, fields, confidence_note)

    for name, sig in class_signatures.items():
        # 這整棵決策樹的前提是「methods 為空清單」（見本函式上方註解、
        # 05a 三章「孤兒類別與資料容器占位」開頭）。單靠 covered_class_
        # names 判斷「已處理過」不夠：routers 層的 InterfaceSpec.
        # class_name 一律是 None（05a 七章既有規則），covered_class_names
        # 因此永遠不會包含 router 類別的真實 class name，若不額外檢查
        # sig.methods，一個正常、方法齊全的 @RestController（如只是剛好
        # 沒有 constructor／field 的無狀態 Controller）會被誤判成孤兒
        # 類別，嚴重時甚至把它渲染成錯誤的 dataclass 段落。
        if sig.methods or name in covered_class_names or name in seen_schema_names:
            continue
        if name in globally_named_schemas:
            logger.warning(
                "module %s 的類別 %s 不是這個模組自己的 API 邊界方法用到的 schema，"
                "但已經是 openapi_spec 裡的具名 schema（另一個模組的端點登記的），"
                "略過本模組的孤兒類別渲染，交由那個模組的定義作為唯一來源"
                "（見 docs/refactor_bug_trace.md #25）",
                module["module"], name,
            )
            continue
        annotation_set = set(sig.annotations)
        if annotation_set & JPA_ENTITY_ANNOTATIONS:
            continue
        if annotation_set & DATA_CLASS_ANNOTATIONS:
            data_carrier_classes.append(_data_carrier_entry(sig, "偵測到 Lombok/JPA 資料標記"))
        elif sig.constructors:
            orphan_classes.append((
                name,
                sig.file_path,
                [
                    [(p.name, type_mapping.map_java_type(p.java_type, known_classes)) for p in ctor.params]
                    for ctor in sig.constructors
                ],
            ))
        elif sig.fields:
            data_carrier_classes.append(_data_carrier_entry(sig, "無 Lombok 標記，依欄位宣告推斷"))
        else:
            logger.warning(
                "module %s 的類別 %s 沒有方法、建構子、欄位，也沒有被任何 InterfaceSpec 覆蓋，"
                "略過（05a 三章孤兒類別判斷：沒有任何機械事實可渲染）",
                module["module"], name,
            )

    fragments: list[str] = []
    if schema_class_fields:
        fragments.append(layout.render_schema_section(layout.schema_file_path(module["module"]), schema_class_fields))
    if orphan_classes:
        fragments.append(
            layout.render_class_placeholder_section(layout.schema_file_path(module["module"]), orphan_classes)
        )
    if data_carrier_classes:
        fragments.append(
            layout.render_dataclass_section(layout.schema_file_path(module["module"]), data_carrier_classes)
        )
    directory_tree_fragment = "\n".join(fragments) if fragments else None

    return ModuleDesignResult(module=module["module"], interfaces=interfaces, directory_tree_fragment=directory_tree_fragment)


# --------------------------------------------------------------------------
# Claude API 呼叫（05a 六章）
# --------------------------------------------------------------------------


def _call_design_llm(
    module: ModuleInfo,
    classes_needing_layer: list[JavaClassSignature],
    methods_needing_decision: list[_MethodContext],
    interfaces_by_module: dict[str, list[InterfaceSpec]],
) -> tuple[dict[str, str], dict[str, list[ParamSpec]], dict[str, bool]]:
    """呼叫一次 Claude，回傳
    `(class_name -> layer, signature_key -> extra_params, signature_key -> needs_db_session)`。
    未涵蓋的 `classes_needing_layer`／`methods_needing_decision` 視為
    LLM 沒有完整回答，拋出 `LlmJsonError` 交給上層的待重試佇列（見
    `design_all_modules()` 模組 docstring「重試仍失敗則中止整條 design
    run」）。
    """
    descriptions = {(m["class_name"], m["java_method"]): m["description"] for m in module["methods"]}

    payload = {
        "module": {"module": module["module"], "summary": module["summary"]},
        "classes_needing_layer": [
            {
                "class_name": cls.class_name,
                "methods": [
                    {"method_name": m.method_name, "description": descriptions.get((cls.class_name, m.method_name), "")}
                    for m in cls.methods
                ],
            }
            for cls in classes_needing_layer
        ],
        "methods": [
            {
                "signature_key": ctx.signature_key,
                "java_method": ctx.java_method,
                "class_name": ctx.class_name,
                "description": descriptions.get((ctx.class_name, ctx.java_method), ""),
                "uncovered_params": [
                    {"name": u.param.name, "java_type": u.param.java_type} for u in ctx.uncovered_params
                ],
                "needs_db_session_decision": ctx.needs_db_session_decision,
            }
            for ctx in methods_needing_decision
        ],
        "upstream_interfaces": [iface for dep in module["depends_on"] for iface in interfaces_by_module.get(dep, [])],
    }
    user_prompt = json.dumps(payload, ensure_ascii=False)

    result = call_claude_for_json(
        system_prompt=DESIGN_SYSTEM_PROMPT, user_prompt=user_prompt, schema=DESIGN_OUTPUT_SCHEMA, model=DEFAULT_MODEL
    )

    expected_classes = {c.class_name for c in classes_needing_layer}
    returned_classes = {entry["class_name"] for entry in result["class_layers"]}
    missing_classes = expected_classes - returned_classes
    if missing_classes:
        raise LlmJsonError(f"module {module['module']} 的回應遺漏了 class_layers（視同呼叫失敗）: {sorted(missing_classes)}")

    expected_methods = {ctx.signature_key for ctx in methods_needing_decision}
    returned_methods = {entry["signature_key"] for entry in result["method_decisions"]}
    missing_methods = expected_methods - returned_methods
    if missing_methods:
        raise LlmJsonError(f"module {module['module']} 的回應遺漏了 method_decisions（視同呼叫失敗）: {sorted(missing_methods)}")

    layer_by_class = {entry["class_name"]: entry["layer"] for entry in result["class_layers"]}
    extra_params_by_key = {
        entry["signature_key"]: [ParamSpec(name=p["name"], type=p["type"]) for p in entry["extra_params"]]
        for entry in result["method_decisions"]
    }
    db_session_by_key = {entry["signature_key"]: entry["needs_db_session"] for entry in result["method_decisions"]}

    return layer_by_class, extra_params_by_key, db_session_by_key
```

**測試**：`tests/design_agent/test_design.py`（孤兒類別／資料容器占位決策樹、`ResponseEntity` 邊界方法、同名多載各自的 boundary、utils package 機械歸類與 `phase` 計算、repository/service 的 phase 對應）。

---

## 八、`route_mapping.py`——`route_to_file_mapping`／`route_to_module_mapping` 機械合併與落地寫入

完全是程式邏輯，不需要 LLM——所有需要的資訊（`api_to_python_target` 的 endpoint↔module 對應、每個 module 的 `interfaces` 檔案集合）在六章都已經產出完畢。

```python
# design_agent/route_mapping.py
from __future__ import annotations

import re
from pathlib import Path

import yaml

from design_agent.layout import schema_file_path
from graph.state import ApiMapping, InterfaceSpec

_PATH_PARAM_RE = re.compile(r"\{[^{}]+\}")


def normalize_path_key(http_method: str, endpoint: str) -> str:
    """必須跟 `02a` `RouteMapper.normalize_path_key()` 的正規化結果完全
    一致，否則 Harness 的比對會全部 miss。`endpoint` 是 openapi 的 path
    樣板（如 `/api/v1/users/{userId}`），逐字元處理，不是「切段、丟掉
    空字串、再重組」：

    1. 去除開頭 `/`（只去開頭這一個，不動其他任何 `/`）
    2. 所有（剩下的）`/` 換成 `_`
    3. 所有 `{paramName}` 樣板一律換成字面 `{id}`——`RouteMapper.
       normalize_path_key()` 是在實際 URL 上把純數字/UUID 換成
       `{id}`，並不知道原始參數名稱叫什麼，兩邊要能精確匹配，這裡也
       必須捨棄參數名稱、統一用 `{id}`

    ```
    /api/v1/users/{userId} + GET  → GET_api_v1_users_{id}
    /api/v1/users/{userId}/profiles + GET → GET_api_v1_users_{id}_profiles
    ```

    刻意不「切段、過濾空字串、重新 join」：那種寫法會把結尾多帶一個
    `/` 的路徑跟不帶結尾 `/` 的版本 normalize 成同一個 key。但
    `RouteMapper.normalize_path_key()` 拿到的 `url_parts` 是 Postman
    `request.url.path` 這個已經切好的陣列，逐段判斷數字/UUID、原樣
    `"_".join(...)`，不會主動丟掉空字串——這裡改成「只動開頭那一個
    `/`、其餘 `/` 逐一換成 `_`」的字面規則，才能保證跟 `RouteMapper`
    的正規化結果位元對位元一致。
    """
    path = endpoint.removeprefix("/")
    path = path.replace("/", "_")
    path = _PATH_PARAM_RE.sub("{id}", path)
    return f"{http_method.upper()}_{path}"


def build_route_to_module_mapping(api_to_python_target: list[ApiMapping]) -> dict[str, str]:
    """`route_to_module_mapping` 的值就是 `ApiMapping.module` 本尊，不是
    新的判斷，只需要①（parse）的輸出就能算出來，不依賴③才有的
    `interfaces`／`modules_with_schema_file`。

    獨立成這個函式，讓 `record_tests`／`run_tests` 這兩個 node 可以在
    ①完成、③還沒跑完（甚至根本不會跑，如 `run_tests` 在
    debug↔implement 重試迴圈裡重複呼叫）時，直接用
    `state["api_to_python_target"]` 就地算出當下這次 run 真正的 module
    對照，不用透過 `config/harness.yaml` 這個由③寫入、且跟②是平行
    分支、寫入時機不保證早於②讀取的中介檔案。
    """
    return {
        normalize_path_key(api["http_method"], api["endpoint"]): api["module"]
        for api in api_to_python_target
    }


def build_route_mappings(
    api_to_python_target: list[ApiMapping],
    interfaces: list[InterfaceSpec],
    modules_with_schema_file: set[str],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """回傳 `(route_to_file_mapping, route_to_module_mapping)`——
    `route_to_module_mapping` 直接委派給 `build_route_to_module_mapping()`
    （同一份邏輯，不重複維護兩份），這裡只另外組 `route_to_file_mapping`。

    `schemas/{module}.py` 是否存在的判定，必須用
    `modules_with_schema_file` 精確比對，不能用「這個 module 有沒有
    router 檔案」猜測：同一個 module 的 API 邊界方法可能全部只用
    inline schema，這種情況下即使有 router 檔案，也不會真的產出
    `schemas/{module}.py`。若改用猜測，會讓 `related_files` 指向一個
    directory_tree 裡實際上不存在的「幽靈檔案」。
    """
    files_by_module: dict[str, set[str]] = {}
    for iface in interfaces:
        # utils 不分 module，_module_of() 的 {module}_{layer}.py 逆運算
        # 對它不適用，也沒有任何 api_to_python_target.module 會拿 utils
        # 的檔名去查——排除，不讓它污染 files_by_module。
        if iface["file_path"].startswith("app/utils/"):
            continue
        module = _module_of(iface["file_path"])
        files_by_module.setdefault(module, set()).add(iface["file_path"])

    module_mapping = build_route_to_module_mapping(api_to_python_target)

    file_mapping: dict[str, list[str]] = {}
    for api in api_to_python_target:
        key = normalize_path_key(api["http_method"], api["endpoint"])
        related = set(files_by_module.get(api["module"], set()))
        if api["module"] in modules_with_schema_file:
            related.add(schema_file_path(api["module"]))
        file_mapping[key] = sorted(related)
    return file_mapping, module_mapping


def write_route_mappings(
    route_to_file_mapping: dict[str, list[str]],
    route_to_module_mapping: dict[str, str],
    config_path: str = "config/harness.yaml",
) -> None:
    """`route_to_file_mapping` 產出後直接寫入 `config/harness.yaml`，
    不需人工填寫——這裡是實際落地檔案 I/O 的地方，不只是回傳給 State
    就結束。

    兩個 key 必須在同一次讀寫回合裡一起覆寫：`safe_load`／`safe_dump`
    是整檔 round-trip，分兩次呼叫各自「讀取＋覆寫單一 key＋寫回」會讓
    後一次寫回的整份 config 蓋掉前一次剛寫入的那個 key。其餘段落
    （`databases`／`services`／`collections`／`diff_rules`，人工設定好
    的環境設定，不是③的產出）讀進來後原樣保留、寫回去。

    已知限制：用 PyYAML 的 `safe_load`／`safe_dump` 做「讀取＋覆寫＋
    寫回」，不是保留註解的 round-trip parser——`config/harness.yaml`
    裡原本給人看的註解在第一次被③寫入後會消失，機器可讀的 key/value
    不受影響。接受的取捨：專案目前唯一用到的 YAML 函式庫是 PyYAML，
    為了保留註解另外引入新函式庫，成本高於這個取捨的代價。
    """
    path = Path(config_path)
    config: dict = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    config["route_to_file_mapping"] = route_to_file_mapping
    config["route_to_module_mapping"] = route_to_module_mapping
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _module_of(file_path: str) -> str:
    """從 `InterfaceSpec.file_path`（如 `app/services/user_service.py`）
    反推 module 名稱——`layout.file_path_for_layer()` 的逆運算：檔名固定
    是 `{module}_{layer_singular}.py`，去掉這個後綴即為 module 名稱。
    """
    stem = file_path.rsplit("/", 1)[-1].removesuffix(".py")
    for suffix in ("_router", "_service", "_repository"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem  # 不應該發生，保底原樣回傳
```

**測試**：`tests/design_agent/test_route_mapping.py`（key 正規化、`related_files` 組成、`config/harness.yaml` 讀寫）。

---

## 九、`global_infra.py`——`@Value` 屬性注入、enum-backed interface

兩個獨立的全域基礎設施偵測與產出，都是「全域基礎設施檔案」既有機制（`app/core/database.py`／`app/main.py`）的延伸——不是逐 module 處理，是對整個 `module_list.java_files` 做一次獨立、輕量的全域掃描，因為這兩個模式本質上跨模組：

1. **`@Value("${key}")` 屬性注入欄位**：機械偵測＋機械產生 `app/core/config.py`，不需要 LLM——property key 已經是決定性事實，沒有語意判斷空間。
2. **只被 Enum 實作的 Java interface**：機械偵測（interface 有哪些實作者、是不是全部是 enum）＋ LLM 設計（怎麼用 Python 慣用寫法表示——Java 用 interface + enum 這套組合是 Java 慣用法，不能直接照搬，需要語意判斷才能決定功能對等的 Python 寫法）。

兩者都獨立於六章逐 module 的 `design_all_modules()` LLM 呼叫之外——③／④是平行分支，這裡的掃描不能依賴④已經算出的 `db_models`，因此有自己完全獨立的輕量 javalang 掃描，不重用 `signature_scan.py`（那是逐 module 呼叫的，範圍與時機都不對）。

```python
# design_agent/global_infra.py
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import javalang
import javalang.tree

from common.java_type_mapping import camel_to_snake, type_str as _type_str
from common.llm_client import LlmJsonError, call_claude_for_json
from design_agent import layout
from design_agent.exceptions import DesignAgentModuleError
from design_agent.llm import DEFAULT_MODEL
from graph.state import ModuleInfo

logger = logging.getLogger(__name__)

_RETRY_WAIT_SECONDS = 300.0


def _iter_project_files(module_list: list[ModuleInfo]) -> list[str]:
    """`module_list.java_files` 逐 module 各自一份清單，這裡的兩個掃描
    都是全域（不分 module），先攤平去重。
    """
    seen: dict[str, None] = {}
    for m in module_list:
        for f in m["java_files"]:
            seen.setdefault(f, None)
    return list(seen)


# ── @Value("${key}") 屬性注入欄位 → app/core/config.py（機械）──

_STEREOTYPES = frozenset({"RestController", "Controller", "Service", "Component", "Repository"})


@dataclass(frozen=True)
class ValueInjectedField:
    file_path: str  # Java 原始碼相對路徑，純記錄用
    class_name: str
    field_name: str
    property_key: str  # 已去掉 "${" "}" 包裝的原始 key，如 "language.code"
    # 這個欄位所屬 class 對應到的 Python 檔案路徑——render_config_py()
    # 產出的 config_field_mappings 要能被 [P]（只認得 Python
    # file_path）比對到，key 必須是 Python 路徑，不能是 Java file_path。
    # None 代表這個 class 沒有機械可判定的層級（無 Spring stereotype）
    # 或不屬於任何已知 module——這種邊界情況下 config.py 依然照常產生，
    # 只是沒有 task.context 提示可用，記警告即可，不中止。
    python_file_path: str | None


def scan_value_injected_fields(module_list: list[ModuleInfo], java_project_path: str) -> list[ValueInjectedField]:
    """逐 `module_list.java_files` 掃 `@Value("${key}")` 標註的欄位。

    Literal 規則：`@Value` 的 value 元素若不是 `javalang.tree.Literal`
    （如 `@Value(SomeConstants.KEY)` 這種常數參照），視同這個欄位缺席、
    記一筆 warning，不猜。`${...}` 包裝格式外的寫法（SpEL 表達式、純
    字面常數）一律跳過、記警告，不嘗試解析 SpEL。

    `python_file_path` 的計算：`@Value` 欄位所屬 class 的 Spring
    stereotype 決定它落在 Python 哪一層（沿用
    `layout.layer_for_stereotype()`／`file_path_for_layer()`），`module`
    則從 `module_list.java_files` 反查這個 class 所在檔案屬於哪個
    module。兩者缺一都會讓 `python_file_path` 是 `None`，記一筆
    warning，不中止。
    """
    file_to_module: dict[str, str] = {f: m["module"] for m in module_list for f in m["java_files"]}

    result: list[ValueInjectedField] = []
    for rel_path in _iter_project_files(module_list):
        source = Path(java_project_path, rel_path).read_text(encoding="utf-8")
        tree = javalang.parse.parse(source)
        for decl in tree.types:
            if not isinstance(decl, javalang.tree.ClassDeclaration):
                continue
            value_fields_in_class = [
                (field_decl, var_decl)
                for field_decl in decl.fields
                for var_decl in field_decl.declarators
                if any(a.name == "Value" for a in field_decl.annotations)
            ]
            if not value_fields_in_class:
                continue

            stereotype = next((a.name for a in decl.annotations if a.name in _STEREOTYPES), None)
            layer = layout.layer_for_stereotype(stereotype)
            module = file_to_module.get(rel_path)
            if layer is not None and module is not None:
                python_file_path = layout.file_path_for_layer(module, layer)
            else:
                logger.warning(
                    "%s 有 @Value 欄位，但無法機械判定對應的 Python 檔案（stereotype=%r, module=%r），"
                    "config_field_mappings 不會有這個 class 的條目",
                    decl.name, stereotype, module,
                )
                python_file_path = None

            for field_decl, var_decl in value_fields_in_class:
                value_ann = next(a for a in field_decl.annotations if a.name == "Value")
                element = value_ann.element
                if not isinstance(element, javalang.tree.Literal):
                    logger.warning(
                        "%s.%s 的 @Value 元素不是字面字串（可能是常數參照），視同缺席，不產生 config.py 對應項目",
                        decl.name, var_decl.name,
                    )
                    continue
                raw = element.value.strip('"')
                if not (raw.startswith("${") and raw.endswith("}")):
                    logger.warning(
                        "%s.%s 的 @Value(%r) 不是 ${key} 格式（可能是 SpEL 或字面常數），視同缺席，不產生 config.py 對應項目",
                        decl.name, var_decl.name, raw,
                    )
                    continue
                property_key = raw[2:-1]
                result.append(
                    ValueInjectedField(
                        file_path=rel_path,
                        class_name=decl.name,
                        field_name=var_decl.name,
                        property_key=property_key,
                        python_file_path=python_file_path,
                    )
                )
    return result


CONFIG_PY_FILE = "app/core/config.py"


def property_key_to_constant_name(property_key: str) -> str:
    """`language.code` → `LANGUAGE_CODE`、`language.displayName` →
    `LANGUAGE_DISPLAY_NAME`：逐 `.` 分段各自套用 `camel_to_snake()`，
    再以 `_` 接續、轉大寫——跟 `app/core/database.py` 的
    `DATABASE_URL` 同一種全大寫環境變數命名慣例。
    """
    return "_".join(camel_to_snake(seg) for seg in property_key.split(".")).upper()


def render_config_py(
    value_fields: list[ValueInjectedField],
) -> tuple[str | None, dict[str, dict[str, str]], list[dict[str, str]]]:
    """機械組出 `app/core/config.py` 的完整檔案內容，跟
    `layout.render_database_py()` 同一種固定樣板、不經過 LLM 的性質。

    回傳 `(檔案內容或 None, config_field_mappings, config_env_vars)`：
    `value_fields` 為空時回傳 `(None, {}, [])`，呼叫端不渲染這個檔案。

    `config_field_mappings`：`python_file_path -> {java_field_name:
    python_reference}`，供 [P] 折進 `task.context`——key 必須是 Python
    檔案路徑，不是 Java 原始碼路徑。`python_file_path` 是 `None` 的
    欄位仍然會產生環境變數常數，只是不會出現在
    `config_field_mappings` 裡。同一個 property key 若被多個欄位/檔案
    引用，只產生一個常數（依 property_key 去重），但
    `config_field_mappings` 仍逐一記錄每個檔案/欄位對應到哪個常數。

    `config_env_vars`：`property_key -> constant_name` 對照本來就是這個
    函式已經算好、拿去產生 `content` 字串的中間結果，這裡另外攤平成
    `[{"property_key": ..., "constant_name": ...}, ...]`（依首次出現
    順序，已去重）一併回傳，供容器啟動前讀取 Java 端
    `application-{profile}.properties` 實際值、解析出
    `{constant_name: value}` 當額外 `-e` 環境變數注入。
    """
    if not value_fields:
        return None, {}, []

    constants: dict[str, str] = {}  # property_key -> constant_name，依首次出現順序
    for vf in value_fields:
        constants.setdefault(vf.property_key, property_key_to_constant_name(vf.property_key))

    lines = ["import os", ""]
    for property_key, constant_name in constants.items():
        lines.append(f'{constant_name} = os.environ["{constant_name}"]')
    content = "\n".join(lines) + "\n"

    config_field_mappings: dict[str, dict[str, str]] = {}
    for vf in value_fields:
        if vf.python_file_path is None:
            continue
        constant_name = constants[vf.property_key]
        config_field_mappings.setdefault(vf.python_file_path, {})[vf.field_name] = f"app.core.config.{constant_name}"

    config_env_vars = [
        {"property_key": property_key, "constant_name": constant_name}
        for property_key, constant_name in constants.items()
    ]

    return content, config_field_mappings, config_env_vars


# ── 只被 Enum 實作的 interface → LLM 設計 Python 對等寫法 ──


@dataclass(frozen=True)
class _EnumImplementorInfo:
    class_name: str
    file_path: str
    fields: list[tuple[str, str]]  # (name, java_type)，來自 EnumBody.declarations 的 FieldDeclaration


@dataclass(frozen=True)
class EnumBackedInterface:
    """一個「只被 enum 實作」的 interface，交給 LLM 設計 Python 對等
    寫法用的完整輸入。"""

    interface_name: str
    file_path: str
    method_signatures: list[str]  # 如 "int getCode()"，純文字，LLM 判讀用，不需要結構化
    implementors: list[_EnumImplementorInfo] = field(default_factory=list)


def scan_enum_backed_interfaces(module_list: list[ModuleInfo], java_project_path: str) -> list[EnumBackedInterface]:
    """全域掃一次 `module_list.java_files`，找出「至少被一個 enum
    實作、且零個 class 實作」的 interface——這個條件本身是機械可判定
    的，不需要 LLM 介入；需要 LLM 判斷的只有「找到之後該怎麼翻譯成
    Python」，見 `design_enum_backed_interfaces()`。只掃頂層宣告，不用
    會遞迴進 inner class 的 filter，避免同一份原始碼被重複列入。
    """
    interfaces: dict[str, tuple[str, list[str]]] = {}  # name -> (file_path, method signatures)
    class_implementors: dict[str, list[str]] = {}
    enum_implementors: dict[str, list[_EnumImplementorInfo]] = {}

    for rel_path in _iter_project_files(module_list):
        source = Path(java_project_path, rel_path).read_text(encoding="utf-8")
        tree = javalang.parse.parse(source)
        for decl in tree.types:
            if isinstance(decl, javalang.tree.InterfaceDeclaration):
                sigs = [
                    f"{_type_str(m.return_type) if m.return_type is not None else 'void'} {m.name}()"
                    for m in decl.methods
                ]
                interfaces[decl.name] = (rel_path, sigs)
            elif isinstance(decl, javalang.tree.ClassDeclaration):
                for iface_name in decl.implements or []:
                    class_implementors.setdefault(iface_name.name, []).append(decl.name)
            elif isinstance(decl, javalang.tree.EnumDeclaration):
                if not decl.implements:
                    continue
                fields = [
                    (var_decl.name, _type_str(member.type))
                    for member in decl.body.declarations
                    if isinstance(member, javalang.tree.FieldDeclaration)
                    for var_decl in member.declarators
                ]
                info = _EnumImplementorInfo(class_name=decl.name, file_path=rel_path, fields=fields)
                for iface_ref in decl.implements:
                    enum_implementors.setdefault(iface_ref.name, []).append(info)

    result: list[EnumBackedInterface] = []
    for iface_name, (file_path, sigs) in interfaces.items():
        implementors = enum_implementors.get(iface_name)
        if not implementors:
            continue  # 沒有任何 enum 實作它，不是這個機制要處理的模式
        if class_implementors.get(iface_name):
            continue  # 還有 class 也實作它，是一般共用 interface，走既有路徑，不歸這裡管
        result.append(
            EnumBackedInterface(
                interface_name=iface_name,
                file_path=file_path,
                method_signatures=sigs,
                implementors=implementors,
            )
        )
    return result


_ENUM_INTERFACE_SYSTEM_PROMPT = """\
你是協助把 Java（Spring Boot）專案改寫成 Python（FastAPI）專案的助手。
你會收到一個 Java interface，這個 interface 只被若干個 enum 實作（沒有
任何一般 class 實作它），這是 Java 常見的「用 interface 定義契約、
enum 提供具體常數值」寫法（例如錯誤碼列舉）。

Java 的 interface + enum 這套組合是 Java 語言慣用法，不是 Python 的
慣用寫法——你的任務是判斷「在 Python 裡，什麼寫法能達到相同的功能：
讓每個實作 enum 的成員可以當作這個型別使用、可以呼叫這個 interface
宣告的方法（或等價存取對應資料）」，並直接輸出完整、可以直接寫入
檔案、可以被 Python `ast.parse()` 解析成功的原始碼。

輸出的程式碼會被寫進獨立的 Python 檔案，這個檔案之外還會有其他檔案
各自定義每個 enum 本身（用 Python `enum.Enum` 表示，成員名稱、數值
與 Java enum 一致，這部分已經有其他機制處理，不需要你重複輸出）——
你只需要輸出這個 interface 本身該怎麼表示，讓其他程式碼可以：
1. 把它當作型別標註使用（例如某個函式參數型別是這個 interface）
2. 存取到 Java interface 方法對應的資料（如 code／msg）

不要重新定義任何一個 enum 本身的完整成員清單，那不是你的任務。
不要輸出任何解釋文字、不要用 markdown code fence 包裹，只輸出 Python
原始碼本身。
"""

_ENUM_INTERFACE_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "python_module_path": {
            "type": "string",
            "description": "建議的檔案路徑，如 app/core/error_code.py（snake_case，放在 app/core/ 底下，理由：這是跨 module 的全域基礎設施，不屬於任何單一業務模組）",
        },
        "python_source": {"type": "string", "description": "完整檔案原始碼，UTF-8 純文字"},
    },
    "required": ["python_module_path", "python_source"],
    "additionalProperties": False,
}


def _enum_interface_user_prompt(iface: EnumBackedInterface) -> str:
    lines = [
        f"interface 名稱：{iface.interface_name}",
        f"Java 原始碼路徑：{iface.file_path}",
        "方法簽名：",
        *[f"  - {sig}" for sig in iface.method_signatures],
        "被以下 enum 實作：",
    ]
    for impl in iface.implementors:
        field_str = ", ".join(f"{name}: {jtype}" for name, jtype in impl.fields) or "（無欄位）"
        lines.append(f"  - {impl.class_name}（{field_str}）")
    return "\n".join(lines)


def design_enum_backed_interfaces(
    interfaces: list[EnumBackedInterface],
) -> list[tuple[str, str]]:
    """對每一個命中的 interface 各自呼叫一次 Claude（互相獨立，不是
    批次一次問完——每個 interface 的語意判斷彼此無關，分開呼叫的失敗
    互相隔離），回傳 `[(file_path, python_source), ...]`。

    失敗處理：比照六章既有的「單一呼叫失敗」節奏——失敗的 interface
    列入待重試清單，全部呼叫完後等待 `_RETRY_WAIT_SECONDS` 秒統一重試
    一次；仍失敗中止整條 design run（`DesignAgentModuleError`）。
    """
    if not interfaces:
        return []

    def _call(iface: EnumBackedInterface) -> tuple[str, str]:
        response = call_claude_for_json(
            system_prompt=_ENUM_INTERFACE_SYSTEM_PROMPT,
            user_prompt=_enum_interface_user_prompt(iface),
            schema=_ENUM_INTERFACE_OUTPUT_SCHEMA,
            model=DEFAULT_MODEL,
            target_file=iface.file_path,
            class_name=iface.interface_name,
        )
        return response["python_module_path"], response["python_source"]

    results: list[tuple[str, str]] = []
    failed: list[EnumBackedInterface] = []
    for iface in interfaces:
        try:
            results.append(_call(iface))
        except LlmJsonError as exc:
            logger.warning("interface %s 的全域 enum-backed interface 設計呼叫失敗，列入待重試清單: %s", iface.interface_name, exc)
            failed.append(iface)

    if not failed:
        return results

    logger.warning(
        "%d 個 enum-backed interface 的設計呼叫失敗，等待 %.0f 秒後統一重試一次: %s",
        len(failed), _RETRY_WAIT_SECONDS, [i.interface_name for i in failed],
    )
    time.sleep(_RETRY_WAIT_SECONDS)

    still_failed: list[EnumBackedInterface] = []
    for iface in failed:
        try:
            results.append(_call(iface))
        except LlmJsonError as exc:
            logger.warning("interface %s 重試仍失敗: %s", iface.interface_name, exc)
            still_failed.append(iface)

    if still_failed:
        raise DesignAgentModuleError(
            f"{len(still_failed)} 個全域 enum-backed interface 的設計呼叫重試後仍失敗，中止整個 design run"
            f"（見本函式 docstring）: {[i.interface_name for i in still_failed]}"
        )
    return results
```

**測試**：`tests/design_agent/test_global_infra.py`（`@Value` 掃描、`config.py` 產出、`config_field_mappings`／`config_env_vars`）、`tests/design_agent/test_global_advice_module.py`（`_global` 模組專屬路徑、`render_main_py()` 的例外處理器註冊）。

---

## 十、`__init__.py`——對外唯一入口

```python
# design_agent/__init__.py
"""③ 架構設計 Agent 對外唯一入口，`graph/nodes/design_node.py` 只呼叫
這裡的函式（見 05a 十章）。串接順序：六章逐波設計（`design.
design_all_modules()`，內部依序完成四章簽名掃描、五章型別對應／
openapi 覆寫、六章 LLM 呼叫）→ 八章機械合併 `route_to_file_mapping`。
"""
from __future__ import annotations

from design_agent import design, route_mapping
from graph.state import ApiMapping, ModuleInfo, PythonStructure


def run_design_agent(
    *,
    module_list: list[ModuleInfo],
    api_to_python_target: list[ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    skip_excluded_overloads: list[tuple[str, str, str]],
    harness_config_path: str = "config/harness.yaml",
) -> tuple[PythonStructure, dict]:
    """對應 05a 全文：設計 Python 專案結構，輸出
    `(python_structure, route_to_file_mapping)`，直接對應 `RefactorState`
    的 `python_structure`／`route_to_file_mapping` 兩個欄位（見 05a 九章）
    ——同時把 `route_to_file_mapping` 與 `route_to_module_mapping` 一併
    實際寫入 `harness_config_path`（見 `route_mapping.write_route_
    mappings()`），這不是可選的附加行為，是 05a 九章、00 八章明訂的③
    職責本身。`route_to_module_mapping` 不進回傳值、也不進 State（05a
    八章已定案，只寫 yaml）。

    `skip_excluded_overloads`：`RefactorState` 同名欄位，① `parse_agent.
    skip_filter.compute_skip_excluded_overloads()` 算出的 HTTP method
    精確排除清單——使用者填 skip，是人工判斷「這個 endpoint 整段不進
    翻譯流程」，不只是跳過自動化測試，見
    `docs/03a_spec_collection_agent_architecture.md`「Decision.SKIP 的
    語意」。
    """
    interfaces, directory_tree, modules_with_schema_file, config_field_mappings, config_env_vars, java_index = (
        design.design_all_modules(
            module_list, api_to_python_target, openapi_spec, java_project_path, skip_excluded_overloads,
        )
    )
    python_structure = PythonStructure(directory_tree=directory_tree, interfaces=interfaces, java_index=java_index)
    if config_field_mappings:
        python_structure["config_field_mappings"] = config_field_mappings
    if config_env_vars:
        python_structure["config_env_vars"] = config_env_vars
    route_to_file_mapping, route_to_module_mapping = route_mapping.build_route_mappings(
        api_to_python_target, interfaces, modules_with_schema_file
    )
    route_mapping.write_route_mappings(
        route_to_file_mapping, route_to_module_mapping, config_path=harness_config_path
    )
    return python_structure, route_to_file_mapping
```

---

## 十一、`graph/nodes/design_node.py`——LangGraph node

`design` 是平行分支 node：`parse` 完成後與 `record_tests`（②）同時進入就緒狀態（③不依賴 `golden_output`），因此只回傳自己實際更動的 key（`python_structure`／`route_to_file_mapping`），不能用 `{**state, ...}` 整包展開，避免跟 `record_tests` 同一個 superstep 對同一個 key 各自寫入。

```python
"""
③ 架構設計 Agent（Claude API）
輸入：Agent ① 的解析結果（module_list／api_to_python_target）+ openapi.json
輸出：python_structure, route_to_file_mapping
見 05a_design_agent_architecture.md、05b_design_agent_code.md

平行分支 node：parse 完成後與 record_tests（②）同時觸發（見 00 一章流程圖、
01 五章、05a 十一章——③ 不依賴 golden_output），因此只回傳自己實際更動的
key，不能用 `{**state, ...}` 整包展開，避免跟 record_tests 同一個
superstep 對同一個 key 各自寫入。
"""
from __future__ import annotations

import asyncio

from design_agent import run_design_agent
from graph.state import RefactorState


async def run(state: RefactorState) -> dict:
    # run_design_agent() 內部是同步阻塞呼叫（javalang 掃描＋多次 Claude
    # API 呼叫，且六章重試佇列的 5 分鐘等待，見 05a 六章），丟到執行緒
    # 跑，避免卡住事件迴圈（與 parse_node.py／spec_node.py／collection_node.py
    # 做法一致）。
    python_structure, route_to_file_mapping = await asyncio.to_thread(
        run_design_agent,
        module_list=state["module_list"],
        api_to_python_target=state["api_to_python_target"],
        openapi_spec=state["openapi_spec"],
        java_project_path=state["java_project_path"],
        skip_excluded_overloads=state["skip_excluded_overloads"],
    )

    return {
        "python_structure": python_structure,
        "route_to_file_mapping": route_to_file_mapping,
    }
```

---

## 十二、已知限制

- **`_build_boundary_index()` 的多載碰撞（索引層級）**：`ApiMapping.java_controller` 不含參數簽名，同一 class 內若有多載方法各自掛不同 endpoint 且 HTTP method 也相同，索引只保留最後一筆 `ApiMapping`。真實專案若出現這種碰撞，需要索引升級成 `dict[key, list[ApiMapping]]` 並想辦法消歧。
- **`_classify_params()` 的 requestBody 型別比對退化情形**：Java DTO 類別名稱與 springdoc 產生的 schema 名稱不一致、且同一方法有多個型別比對不到的參數時，全部落入 `uncovered_params`、交給六章 LLM 依描述語境判斷，沒有更精確的機械手段。
- **`route_mapping._module_of()` 的檔名慣例耦合**：從 `file_path` 反推 module 名稱依賴 `layout.file_path_for_layer()` 的命名慣例，兩邊若日後各自演化，需要同步維護。
- **`write_route_mappings()` 不保留 `config/harness.yaml` 既有註解**：PyYAML 的 `safe_load`／`safe_dump` 不是 round-trip parser，③第一次寫入後，檔案裡原本給人看的註解會消失（機器可讀的 key/value 不受影響）。接受的取捨，不是遺漏。
- **`X | None` 語法要求目標 Python 服務 ≥ 3.10**：`extract_schema_fields()`／`map_java_type()` 的 `Optional` 對應都用 PEP 604 union 語法。若這個前提未來改變，兩處都需要一併改成 `typing.Optional[T]`。
- **`BigDecimal` → `Decimal` 只在非 API 邊界方法生效**：API 邊界方法改用 openapi_spec 決定型別，springdoc 對 `BigDecimal` 欄位通常序列化成通用 `number` type，沒有訊號能標示「這原本是 BigDecimal」，這個欄位的 API 邊界型別仍是 `float`。
- **`from __future__ import annotations` 只解決同檔案內的循環參照**：跨 `schemas/{module}.py` 檔案的循環 import 不會被這一行解決，需要 `TYPE_CHECKING` guard＋`model_rebuild()`，屬於④如何實際生成、串接檔案間 import 的問題。
- **三處 LLM 判斷點的 prompt 品質未經真實專案校準**：無 stereotype 類別的層級歸屬、框架注入物件轉換、`db: Session` 慣例注入，目前只用合成範例驗證過契約可以正確跑通，實際判斷品質待接上真實專案輸出後校準。
- **`InterfaceSpec.phase` 欄位、Utils 特例尚未落地成程式碼**：⚠️ 已於本輪落地（見二、五、七章）——`graph/state.py::InterfaceSpec`、`design_agent/layout.py`、`design_agent/signature_scan.py`、`design_agent/design.py` 均已更新，823 個測試通過。`[P] Plan Agent`／⑤功能改寫 Agent／`graph/scheduler.py` 尚未依 `refactor_plan.md` 更新，見 `00_refactor_architecture.md` 十章待決定事項。
