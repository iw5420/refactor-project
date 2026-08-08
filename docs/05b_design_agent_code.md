# ③ 架構設計 Agent 程式碼實作

> 05a 是設計面文件，本文件是實作面文件，一一對應、不重複設計理由——每節開頭註明對應 05a 章節，這裡只講怎麼落地成程式碼。對應 `00_refactor_architecture.md` 十一章文件索引的 `05b_design_agent_code.md`。

05a 十章定義的套件結構列了 6 個檔案（`signature_scan.py`／`type_mapping.py`／`layout.py`／`design.py`／`route_mapping.py`／`prompts.py`）。比照 04b 新增 `types.py`／`exceptions.py`／`llm.py`／`__init__.py` 的先例，本文件同樣補上這四個檔案——都是落地時必然需要、但不屬於 05a 設計決策範圍的基礎設施檔案。另外，05a 五章已定案「抽為共用工具 `common/openapi_ref_resolver.py`」，這次一併新增（00 六章「OpenAPI `$ref` 展開（共用工具）」的落地）。

## 目錄

| 檔案 | 對應章節 | 說明 |
|---|---|---|
| `common/openapi_ref_resolver.py` | 05a 五章 | 共用 `$ref` 展開（新增，00 六章已定案的共用介面） |
| `design_agent/exceptions.py` | 05a 十二章 | 例外階層 |
| `design_agent/types.py` | 05a 四～六章 | 內部資料結構（`JavaClassSignature`／`ModuleDesignResult` 等） |
| `design_agent/signature_scan.py` | 05a 四章 | javalang 輕量再掃描 |
| `design_agent/type_mapping.py` | 05a 五章 | Java→Python 型別對應、openapi_spec 覆寫、框架注入偵測 |
| `design_agent/layout.py` | 05a 三章 | 分層/命名規則、module 依賴拓樸分波、directory_tree 組裝、全域基礎設施檔案 |
| `design_agent/prompts.py` | 05a 六章 | Claude system prompt 與 output schema |
| `design_agent/llm.py` | 05a 六章 | ③ 專屬的模型選擇 |
| `design_agent/design.py` | 05a 六章 | 逐波呼叫 Claude API、組裝 InterfaceSpec |
| `design_agent/route_mapping.py` | 05a 八、九章 | `route_to_file_mapping`／`route_to_module_mapping` 機械合併，並寫入 `config/harness.yaml` |
| `design_agent/__init__.py` | 05a 十章 | 對外唯一入口 `run_design_agent()` |
| `graph/nodes/design_node.py` | 05a 十一章 | LangGraph node（取代原本的 stub） |

---

## 零、`common/openapi_ref_resolver.py`——共用 `$ref` 展開

對應 05a 五章「決策：抽為共用工具 `common/openapi_ref_resolver.py`」、00 六章「OpenAPI `$ref` 展開（共用工具）」。③ 是第一個直接對齊這個介面的 Agent；`spec_collection_agent/openapi_refs.py` 目前仍是內部各自的展開實作，尚未遷移過來共用，是已知技術債（見十一章「已知限制」），不在這次範圍內處理。

```python
# common/openapi_ref_resolver.py
"""跨 Agent 共用的 OpenAPI `$ref` 展開，見 00 六章「OpenAPI `$ref` 展開
（共用工具）」、05a 五章。[B] Collection Agent（03a/03c）與 ③ 架構設計
Agent（05a 五章）都需要對 openapi_spec 的 operation/schema 片段做同一件
事：遞迴展開 `$ref`，只展開這次任務相關的片段（不整包攤平
`components.schemas`），遞迴展開到底，不處理 `allOf`／`oneOf`／`anyOf`
組合語法（等真的遇到再處理）。

**現況**：③ 是第一個直接對齊這個共用介面的 Agent（見 05a 五章「決策：
抽為共用工具 common/openapi_ref_resolver.py」）；`spec_collection_agent/
openapi_refs.py` 目前仍是內部各自的展開實作，尚未遷移過來共用，是已知
技術債（見 05a 十三章待決定事項），不在③本次設計範圍內處理——這裡的
演算法沿用同一套邏輯，但物件上是獨立檔案，不是把舊檔案搬過來改名。
"""
from __future__ import annotations

from typing import Any


def resolve_refs(fragment: dict, full_spec: dict) -> dict:
    """遞迴展開 `fragment` 內出現的 `$ref`（JSON Pointer，如
    `"#/components/schemas/UserCreateRequest"`），只展開 `fragment` 實際
    用到的部分，不會把整份 `full_spec["components"]["schemas"]` 攤平塞
    進來。

    循環參照時停止繼續展開，回傳一個帶 `_circular` 標記的殘留 `$ref`，
    不無限遞迴。找不到指標對應的節點時原樣保留該 `$ref`，不拋例外中止
    ——展開失敗不該讓呼叫端（③ 的型別對應、[B] 的填值/鏈式依賴偵測）
    連帶整個失敗，寧可讓呼叫端看到一個沒展開的指標，也不要讓這個函式
    本身變成新的硬性失敗點。
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

對應 05a 十二章「錯誤處理範圍」：四章重新掃描失敗直接往上拋、中止整條 run；六章 LLM 呼叫失敗先走重試佇列緩衝，重試仍失敗才拋出硬性失敗（見七章 `design.py`）；`depends_on` 循環依賴、`depends_on` 引用不存在的 module 名稱，各是另外兩個獨立的硬性失敗（刻意分開成不同例外型別，見下方 `DesignAgentUnknownDependencyError`）；`module_list.methods` 對不到任何真實 Java 方法是第四種硬性失敗（`DesignAgentCoverageError`，見下方類別 docstring）。

```python
# design_agent/exceptions.py
"""③ 架構設計 Agent 例外階層，對應 05a 十二章「錯誤處理範圍」。Java
原始碼再掃描（四章）失敗一律視為輸入端問題，直接往上拋、中止整條
LangGraph run，不在這裡額外包裝——javalang 拋出的 `JavaSyntaxError`／
檔案 I/O 例外原樣往上傳即可。這裡定義六章 LLM 設計階段專屬的四種硬性
失敗：單一 module 呼叫重試仍失敗、depends_on 出現循環依賴、depends_on
引用不存在的 module 名稱、`module_list.methods` 對不到任何真實 Java
方法（`DesignAgentCoverageError`，見該類別 docstring「與 05b 原始設計
的差異」）。
"""
from __future__ import annotations


class DesignAgentModuleError(Exception):
    """單一 module 的六章 Claude API 呼叫，在 design.py 的重試佇列機制
    （待重試清單、5 分鐘後統一重試一次，比照 04a 四章）跑完仍失敗時
    拋出，中止整條 design run（見 05a 十二章：`python_structure` 是
    [P]／④ 唯一的權威規格，任何一個 module 的介面缺失都會讓兩者在不
    知情狀況下對著不完整規格工作，風險遠高於重新執行一次——比 04a
    允許缺摘要繼續跑更嚴格，見 05a 六章「單一 module 呼叫失敗時」）。
    """


class DesignAgentCycleError(Exception):
    """`module_list.depends_on` 拓樸排序時偵測到循環依賴時拋出，中止
    整條 design run，交由人工排查（見 05a 六章「循環依賴」）——不嘗試
    自動打斷環或猜測合理順序：`depends_on` 理論上應為 DAG（04a Reduce
    階段、⑤ 排程器都依賴同一假設），真的出現循環代表上游輸入資料本身
    有錯誤，不是這裡能安全修復的情況。

    只在`depends_on` 引用的 module 名稱**都確實存在**於 `module_list`
    的前提下才會拋出——引用不存在的 module 名稱是另一種輸入錯誤（見
    `DesignAgentUnknownDependencyError`），不會走到這裡：`layout.
    build_waves()` 會先做過一輪存在性檢查，避免「依賴引用了拼錯或不
    存在的 module 名稱」被誤判成循環依賴，讓除錯方向對不上真正的
    根因（缺依賴 vs. 真的循環）。
    """


class DesignAgentUnknownDependencyError(Exception):
    """`module_list.depends_on` 引用了不存在於 `module_list` 裡任何一個
    `module` 名稱時拋出（見 `layout.build_waves()`）——常見成因是①的
    Reduce 階段拼錯依賴的 module 名稱（04a 四章「missing_classes 只檢查
    一個方向」的姊妹情況，這裡換成 module 名稱層級）。跟循環依賴
    （`DesignAgentCycleError`）刻意分開成兩種例外：兩者的根因（缺依賴
    vs. 依賴形成環）不同，共用同一個例外訊息會讓人工排查時先入為主
    地往「循環」方向找，實際上問題出在①的輸出資料本身。
    """


class DesignAgentCoverageError(Exception):
    """`_build_method_contexts()` 在 `module_list.methods` 裡的一筆方法
    找不到對應的真實 Java class／method 時拋出（`class_signatures.
    get(method_info["class_name"])` 是 `None`，或該 class 底下找不到
    同名方法），中止整條 design run，不進 LLM 呼叫重試佇列。

    **不是 LLM 呼叫失敗，不該走 `LlmJsonError` 那條重試路線**：這個
    情況發生在呼叫 Claude API 之前（`_build_method_contexts()` 是純
    機械比對），根因是①的輸出（`module_list.methods`）跟③這次重新
    掃描 Java 原始碼的結果對不上——同一份 `java_project_path`，理論上
    應該永遠對得上，對不上代表某處有結構性錯誤（例如 `signature_scan.
    scan_java_files()` 漏掃了某種 Java 語法結構，見之前修過的 interface
    掃描缺漏案例）。重跑同一次 Claude API 呼叫解決不了「程式碼掃描邏輯
    有 bug」這種問題，5 分鐘後重試只是白等，因此刻意用獨立的例外型別，
    確保不會被 `_run_wave_batch()` 的 `except LlmJsonError` 攔截、送進
    重試佇列。

    **與 05b 原始設計的差異**：05b 原本的決定是「記警告並跳過，不中止
    整條 design run」（理由：「理論上不該發生」）。實測對真實
    `lang-exam-api-refactor` 專案跑出這個情況真的會發生，且發生時
    影響幅度不小（一次涵蓋 26% 的方法）——根本原因後來確認是
    `signature_scan.py` 沒有掃描 Java `interface` 宣告，已經修好；但
    「找不到對應」這個分支本身沒有變，若之後又出現另一種目前沒遇過的
    Java 語法造成同樣的落空，還是會被原本的「跳過＋警告」悄悄吃掉，
    只留在容易被忽略的 log 裡。因此改成立即中止＋在錯誤訊息裡列出
    精確的 module／class／method 清單，讓這類問題無法再悄悄流入不完整
    的 `python_structure`。
    """
```

---

## 二、`types.py`——內部型別定義

對應 05a 四～六章。輸出面（`python_structure`／`route_to_file_mapping`）直接沿用 `graph/state.py` 既有的 `PythonStructure`／`InterfaceSpec`／`ParamSpec`（見 05a 九章「不重新定義結構」），這裡只定義 05b 處理過程需要、不跨模組交接的中間資料結構。

```python
# design_agent/types.py
"""③ 架構設計 Agent 內部型別定義，對應 05a 四～六章。輸出面
（python_structure／route_to_file_mapping）直接用 graph/state.py 既有的
PythonStructure／InterfaceSpec／ParamSpec（見 05a 九章「不重新定義
結構」），這裡只放 05b 處理過程內部使用、不跨模組交接的中間資料結構。
04a 七章的模組清單沒有點名這個檔案，是比照 03c/04b 新增共用型別檔的
先例補上的。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class JavaParam:
    name: str
    java_type: str  # 原始 Java 型別字面字串，含泛型，如 "List<UserDto>"、"int"


@dataclass(frozen=True)
class JavaMethodSignature:
    """單一 Java 方法的完整簽名，對應 05a 四章「方法簽名的機械基礎
    資料」。多載方法（同名不同參數）各自是獨立的一筆，不因同名合併
    （見 05a 四章「多載方法的處理」）——`signature_key` 因此含參數型別，
    天生不會有多載碰撞，比 04a 的 method_id（只含方法名）精確度更高。
    """

    class_name: str
    method_name: str
    params: list[JavaParam]
    return_type: str | None  # None 代表 void
    is_private: bool = False  # 供 design.py 決定 Python function_name 是否加底線前綴（05a 七章）

    @property
    def signature_key(self) -> str:
        param_types = ",".join(p.java_type for p in self.params)
        return f"{self.class_name}::{self.method_name}({param_types})"


@dataclass(frozen=True)
class JavaField:
    """單一欄位宣告，供孤兒類別（無方法、可能是 Lombok 資料類別）的
    dataclass 渲染用（見 `design.py` orphan class 決策樹、
    `layout.render_dataclass_section()`）。`java_type` 是含泛型的完整
    型別字面字串（同 `JavaParam.java_type` 慣例，由 `signature_scan.
    _type_str()` 產生），不是 `parse_agent.types.FieldInfo.type_name`
    那種為了 DI 依賴解析而展開集合取內層型別的簡化版——兩者目的不同，
    這裡需要的是能直接餵給 `type_mapping.map_java_type()` 的精確型別。
    `is_final` 供判斷這批欄位該渲染成 `@dataclass(frozen=True)` 還是
    一般 `@dataclass`：全部欄位都 `final` 時視為不可變。
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


@dataclass(frozen=True)
class UncoveredParam:
    """API 邊界方法裡，Java 簽名有、但 openapi_spec 業務參數（path/query/
    requestBody 展開後的欄位）裡找不到對應的參數——框架注入物件候選，見
    05a 五章「框架注入物件：openapi_spec 覆寫的例外」。這份清單是機械
    比對出來的，六章必須明確列給 LLM，不能只給 openapi_spec 展開結果、
    指望 LLM 自己發現漏了什麼。
    """

    method_signature_key: str  # 對回 JavaMethodSignature.signature_key
    param: JavaParam


@dataclass(frozen=True)
class ModuleDesignResult:
    """六章單一 module 處理完的結果：這個 module 的 InterfaceSpec 清單 +
    directory_tree 文字片段（含 schemas/{module}.py 欄位描述，若這個
    module 有 API 邊界方法）。`directory_tree_fragment` 為 `None` 代表
    這個 module 沒有 API 邊界方法、不需要 schema 段落。`interfaces` 的
    元素是符合 `graph/state.py` `InterfaceSpec` 的 dict。
    """

    module: str
    interfaces: list[dict]
    directory_tree_fragment: str | None
```

---

## 三、`signature_scan.py`——javalang 輕量再掃描（四章）

`scan_java_files()` 是這個檔案唯一的對外函式，逐 module 呼叫（見 05a 四章「決策」）。取方法完整簽名（參數清單＋回傳型別）、建構子簽名、class stereotype、annotation 清單與欄位宣告，不建呼叫圖——跟 `parse_agent/call_graph.py` 各自獨立、不共用。`ClassDeclaration` 與 `InterfaceDeclaration` 都會建立 `JavaClassSignature`（Spring Data Repository 慣例上寫成 interface，見下方模組 docstring），但只有 `ClassDeclaration` 才有建構子／annotations／fields 可讀。

```python
# design_agent/signature_scan.py
"""③ 架構設計 Agent：javalang 輕量再掃描，對應 05a 四章。只取方法完整
簽名（參數清單＋回傳型別）、建構子簽名（供 orphan class 處理，見
`JavaClassSignature.constructors` docstring／`design.py`）與 class
stereotype，不建呼叫圖——跟
`parse_agent/call_graph.py` 各自獨立、不共用（見 05a 四章「這不是重跑
04a 三章的呼叫圖建構」：呼叫圖需要欄位依賴、method invocation 解析、
`@Qualifier`/`@Primary` 消歧，這裡的範圍窄得多，不需要建立呼叫關係）。

沿用 04a 三章已驗證過的同一顆 `javalang`（同一個 `lang-exam-api-refactor`
專案已證實 100% 解析成功），不需要重新驗證解析器可行性，只是抽取的
欄位不同。

**Interface 宣告也要建 `JavaClassSignature`**（比照 `parse_agent/
call_graph.py` 的 `_extract_interfaces()`，理由相同）：Spring Data JPA
Repository 慣例上寫成 `interface XxxRepository extends JpaRepository<...>`，
沒有手寫實作類別。若只掃 `ClassDeclaration`，這類 interface 完全不會
建立 `JavaClassSignature`，`design.py` 的 `_build_method_contexts()` 對
`module_list.methods` 裡屬於這個 class 的每一筆都會落入「找不到所屬
類別」分支而整批略過——不是「這個方法簽名解析不出來」，是這個 class
在 `class_signatures` 裡根本不存在，實測對真實 `lang-exam-api-refactor`
專案這一個缺口就佔了 05a 七章強制規則違反案例的多數（Repository 衍生
查詢方法＋ interface 形式的常數存取類別，如 `ErrorCode`）。無 stereotype
的 interface（Spring Data Repository 慣例上不標註 `@Repository`，靠
`extends JpaRepository` 讓 Spring 結構性識別）比照既有規則交給六章 LLM
判斷歸屬層級（見 05a 三章「無 stereotype 的類別」），這裡不額外猜測。
"""
from __future__ import annotations

from pathlib import Path

import javalang
import javalang.tree

from design_agent.types import JavaClassSignature, JavaField, JavaMethodSignature, JavaParam

_STEREOTYPES = {"RestController", "Controller", "Service", "Component", "Repository"}


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
        for decl in tree.types:
            if not isinstance(decl, (javalang.tree.ClassDeclaration, javalang.tree.InterfaceDeclaration)):
                continue
            is_class_decl = isinstance(decl, javalang.tree.ClassDeclaration)
            result[decl.name] = JavaClassSignature(
                file_path=rel_path,
                class_name=decl.name,
                stereotype=_stereotype_of(decl.annotations),
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
    到完整清單，不能只看 stereotype。
    """
    return [ann.name for ann in annotations]


def _field_signature(field_decl: javalang.tree.FieldDeclaration) -> list[JavaField]:
    """一個 `FieldDeclaration` 可能一次宣告多個變數（如
    `private int a, b;`），逐一展開成獨立的 `JavaField`，共用同一個
    型別與 modifiers。回傳 list 供呼叫端用 list comprehension 攤平
    （見 `scan_java_files()`），對稱於 `_method_signature()`／
    `_constructor_signature()` 都是「一筆宣告對一筆輸出」的慣例，這裡
    因為 javalang 的 `FieldDeclaration` 天生可能包含多個宣告，才需要
    回傳 list 而非單一 `JavaField`。
    """
    java_type = _type_str(field_decl.type)
    is_final = "final" in field_decl.modifiers
    return [
        JavaField(name=decl.name, java_type=java_type, is_final=is_final)
        for decl in field_decl.declarators
    ]


def _method_signature(class_name: str, method_decl: javalang.tree.MethodDeclaration) -> JavaMethodSignature:
    """多載（同名不同參數）方法各自對應輸入 `class_decl.methods` 裡
    獨立的一筆宣告，這裡逐筆轉換、不做任何去重或合併——見 05a 四章
    「多載方法的處理」：這裡直接讀完整 AST 節點（含參數型別），不像
    04a 的 `method_id` 只用方法名不含簽名，天生不會有多載碰撞問題。
    """
    return JavaMethodSignature(
        class_name=class_name,
        method_name=method_decl.name,
        params=[JavaParam(name=p.name, java_type=_type_str(p.type)) for p in method_decl.parameters],
        return_type=_type_str(method_decl.return_type) if method_decl.return_type is not None else None,
        is_private="private" in method_decl.modifiers,
    )


def _constructor_signature(class_name: str, ctor_decl: javalang.tree.ConstructorDeclaration) -> JavaMethodSignature:
    """建構子沒有回傳型別（`return_type=None`，跟 `void` 方法用同一個
    字面表示，但呼叫端不會混淆——建構子只會出現在 `JavaClassSignature.
    constructors`，不會混進 `methods`）。多載建構子（如 `AuthException`
    的兩個建構子）各自對應輸入 `class_decl.constructors` 裡獨立的一筆，
    比照 `_method_signature()` 逐筆轉換、不去重合併，理由同「多載方法
    的處理」。`method_name` 沿用 class 名稱（Java 建構子語法本來就跟
    class 同名），供 `design.py` 渲染建構子簽名時識別用。
    """
    return JavaMethodSignature(
        class_name=class_name,
        method_name=ctor_decl.name,
        params=[JavaParam(name=p.name, java_type=_type_str(p.type)) for p in ctor_decl.parameters],
        return_type=None,
        is_private="private" in ctor_decl.modifiers,
    )


def _type_str(java_type) -> str:
    """把 javalang 型別節點還原成含泛型參數的原始 Java 型別字面字串
    （如 `List<UserDto>`），供 `type_mapping.map_java_type()` 解析——跟
    `parse_agent/call_graph.py` 只取 `.name`（外層型別，供依賴解析用）
    的需求不同，這裡需要完整還原含泛型的字面字串，才能做五章的型別
    對應。多層巢狀泛型（如 `Map<String, List<Order>>`）遞迴處理。
    """
    name = java_type.name
    arguments = getattr(java_type, "arguments", None)
    if not arguments:
        return name
    inner_types = []
    for arg in arguments:
        inner = getattr(arg, "type", None)
        inner_types.append(_type_str(inner) if inner is not None else "?")
    return f"{name}<{', '.join(inner_types)}>"
```

**已驗證**：對照一段合成的 `UserController.java`（`@RestController`，一個帶 `@PathVariable`／框架物件參數的 GET、一個帶 `@RequestBody` 的 POST、一個 `private` 方法）跑過 `scan_java_files()`，正確取出 stereotype、參數清單（含型別）、回傳型別、`is_private` 旗標，多載場景（若有）也會各自成一筆。`annotations`／`fields` 擷取另外對照合成的 Lombok `@Data`／`@Value` 類別、無標記純欄位類別、`InterfaceDeclaration` 三種情境驗證過（`tests/design_agent/test_signature_scan.py`），確認 `InterfaceDeclaration` 的 `annotations`／`fields` 一律是空清單、一次宣告多個變數的欄位（`private int a, b;`）會正確展開成獨立的 `JavaField`。

---

## 四、`type_mapping.py`——Java→Python 型別對應、openapi_spec 覆寫（五章）

```python
# design_agent/type_mapping.py
"""③ 架構設計 Agent：Java→Python 型別對應、openapi_spec 覆寫，對應
05a 五章全節。
"""
from __future__ import annotations

import logging
import re

from common.openapi_ref_resolver import resolve_refs
from design_agent.types import JavaMethodSignature, JavaParam, UncoveredParam
from graph.state import ParamSpec

logger = logging.getLogger(__name__)

_CAMEL_RE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_RE_2 = re.compile(r"([a-z0-9])([A-Z])")


def camel_to_snake(name: str) -> str:
    """Java 慣例的 camelCase 轉 Python 慣例的 snake_case（如 `userId`
    → `user_id`、`getHTTPStatus` → `get_http_status`），供 requestBody
    參數命名（見 `resolve_api_boundary_signature()`）與 design.py 的
    方法名稱轉換共用，不各自重寫一份。連續大寫（縮寫，如 `HTTP`）視為
    單一詞界，不逐字元插入底線。
    """
    s1 = _CAMEL_RE_1.sub(r"\1_\2", name)
    return _CAMEL_RE_2.sub(r"\1_\2", s1).lower()


# 05a 五章「基礎型別對應表（機械，非 LLM）」，純字串對應。
# BigDecimal 刻意獨立映射到 Decimal，不跟 double／float 共用——Java 生態
# 系統選 BigDecimal 通常就是為了避開 IEEE 754 浮點誤差（金額、需要精確
# 小數運算的場景），對到 Python float 會直接把這個精度保證丟掉。
# decimal.Decimal 是 Pydantic 原生支援的型別，語意對應才正確。
_SIMPLE_JAVA_TYPES = {
    "int": "int", "Integer": "int", "short": "int", "Short": "int",
    "long": "int", "Long": "int",
    "String": "str",
    "boolean": "bool", "Boolean": "bool",
    "double": "float", "Double": "float", "float": "float", "Float": "float",
    "BigDecimal": "Decimal",
    "void": "None",
}
# 單一型別參數容器：解開一層取內層型別。Map 需要兩個型別參數，另外處理。
_UNWRAP_SINGLE_PARAM = {"List": "list[{0}]", "Set": "list[{0}]", "Collection": "list[{0}]", "Optional": "{0} | None"}
_GENERIC_RE = re.compile(r"^(\w+)<(.+)>$")


def map_java_type(java_type: str, known_classes: frozenset[str] = frozenset()) -> str:
    """05a 五章基礎型別對應表的程式化版本，遞迴處理泛型容器。
    `known_classes`：專案內自訂 class 名稱集合，命中時原樣沿用（假設
    同名 Python 類別存在，見 05a 五章表格最後一列）；不在表裡、也不在
    `known_classes` 的型別原樣保留字串，交由六章 LLM 判斷（如專案內
    少見的第三方型別，見 05a 五章表格「無法辨識的型別」）。

    **`BigDecimal` → `Decimal` 只在這個函式的路徑上生效**，API 邊界方法
    改走 `resolve_api_boundary_signature()`、覆蓋不到（理由見 05a 五章
    「型別對應」）。
    """
    java_type = java_type.strip()
    if java_type in _SIMPLE_JAVA_TYPES:
        return _SIMPLE_JAVA_TYPES[java_type]

    match = _GENERIC_RE.match(java_type)
    if match:
        outer, inner = match.group(1), match.group(2)
        if outer == "Map":
            key_type, value_type = _split_top_level_comma(inner)
            return f"dict[{map_java_type(key_type, known_classes)}, {map_java_type(value_type, known_classes)}]"
        if outer in _UNWRAP_SINGLE_PARAM:
            return _UNWRAP_SINGLE_PARAM[outer].format(map_java_type(inner, known_classes))
        return java_type  # 未知的單參數泛型容器：不硬猜，原樣保留交給六章 LLM

    return java_type  # 命中 known_classes 或無法辨識，兩種情況都原樣沿用（見 docstring）


def _split_top_level_comma(inner: str) -> tuple[str, str]:
    """`Map<K, V>` 的 `inner` 是 `"K, V"`，只在最外層逗號（不在巢狀
    `<...>` 內）切分——K/V 本身仍可能是巢狀泛型（如 `Map<String,
    List<Order>>`）。
    """
    depth = 0
    for i, ch in enumerate(inner):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            return inner[:i].strip(), inner[i + 1 :].strip()
    raise ValueError(f"無法解析 Map 泛型參數（找不到最外層逗號）: {inner}")


_OPENAPI_SCALAR_TYPE = {"integer": "int", "number": "float", "string": "str", "boolean": "bool"}


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
    """
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    schema_type = schema.get("type")
    if schema_type == "array":
        return f"list[{openapi_type_to_python(schema.get('items', {}))}]"
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
    """
    covered, _ = _classify_params(method, operation, openapi_spec)
    params = [param_spec for _, param_spec in covered]

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


def collect_named_schemas(operation: dict, openapi_spec: dict) -> list[tuple[str, dict]]:
    """回傳這個 operation 用到的具名 schema（requestBody + 第一個 2xx
    response），每個是 `(schema_name, raw_schema)`，供
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
    """
    results: list[tuple[str, dict]] = []

    body_name = schema_name_for(operation, part="requestBody")
    if body_name:
        results.append((body_name, _raw_named_schema(body_name, openapi_spec)))

    responses = operation.get("responses", {})
    status = next((s for s in sorted(responses) if s.startswith("2")), None)
    if status:
        response_name = schema_name_for(operation, part=status)
        if response_name:
            results.append((response_name, _raw_named_schema(response_name, openapi_spec)))
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
    """
    properties = raw_schema.get("properties", {})
    required = set(raw_schema.get("required", []))
    fields: list[tuple[str, str]] = []
    for name, prop_schema in properties.items():
        python_type = openapi_type_to_python(prop_schema)
        is_required = name in required
        if not is_required:
            python_type = f"{python_type} | None"

        constraint_kwargs = _field_constraint_kwargs(prop_schema)
        if constraint_kwargs:
            default_arg = "..." if is_required else "default=None"
            declaration = f"{python_type} = Field({', '.join([default_arg, *constraint_kwargs])})"
        else:
            declaration = python_type
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

**已驗證**：用一段合成的 openapi_spec（GET 帶 path 參數＋回傳具名 schema、POST 帶具名 requestBody）驗證過 `resolve_api_boundary_signature()`／`find_uncovered_framework_params()`／`collect_named_schemas()`：path 參數正確對應、`HttpServletRequest` 這類框架物件正確落入 uncovered、`req: UserCreateRequest` 這類 body 參數正確用型別比對抓到（沒有被誤判成 uncovered）、回傳型別與 schema 欄位都正確展開。另外針對 requestBody 是 **inline schema**（沒有 `$ref`，`body_schema_name` 為 `None`）、退回「唯一剩餘參數」啟發式的情況額外驗證過：body 參數型別正確產出 `str`（`openapi_type_to_python()` 直接對 inline schema 的 `type: "string"` 判斷），不是把 Java 型別字面字串 `"String"` 原封不動當成 Python 型別。`map_java_type('BigDecimal')` 正確產出 `Decimal`（不是 `float`）；`extract_schema_fields()` 對帶 `maxLength`／`pattern`／`minimum`／`maximum` 的欄位正確產出 `Field(...)` 形狀的宣告字串（必填欄位用 `Field(...)`、非必填欄位用 `Field(default=None, ...)`），無限制的欄位維持原本的裸型別字串。

**巢狀 `$ref` 型別**：額外驗證過 `User` 帶一個 `orders: List[Order]` 欄位（`Order` 是另一個具名 schema）的情況，`extract_schema_fields()` 正確產出 `"list[Order]"`，不是展開後退化成的 `"list[object]"`。

**陣列包裝的 requestBody／response**：額外驗證過 requestBody 是 `List<UserCreateRequest>`（openapi 的 `requestBody` schema 是 `type: array, items: {$ref: UserCreateRequest}`）的情況——`schema_name_for()` 正確取到 `"UserCreateRequest"` 供 `_simple_type_name()` 比對（確認 `_simple_type_name("List<UserCreateRequest>")` 正確剝到 `"UserCreateRequest"`，不是誤取外層的 `"List"`），但最終寫進 `InterfaceSpec` 的參數型別是 `"list[UserCreateRequest]"`，不是弄丟容器語意的裸 `"UserCreateRequest"`；response 是同樣的陣列包裝時，`return_type` 也正確是 `"list[UserCreateRequest]"`。`collect_named_schemas()` 對這兩種陣列包裝的情況都正確收到 `("UserCreateRequest", ...)`，不會因為外層是陣列就漏收，Schema 定義段照樣會有 `class UserCreateRequest(BaseModel):`。

---

## 五、`layout.py`——分層、拓樸分波、directory_tree 組裝（三章）

```python
# design_agent/layout.py
"""③ 架構設計 Agent：Python 分層與命名慣例、module 依賴拓樸分波、
directory_tree 組裝、全域基礎設施檔案，對應 05a 三章全節。
"""
from __future__ import annotations

from design_agent.exceptions import DesignAgentCycleError, DesignAgentUnknownDependencyError
from graph.state import InterfaceSpec, ModuleInfo

# 05a 三章「層級判定（機械，非 LLM）」：Java class 的 stereotype
# annotation 決定歸屬層級，無 stereotype 的類別（None）留給六章 LLM 判斷
# （見 layer_for_stereotype() docstring）。
_STEREOTYPE_LAYER = {
    "RestController": "routers",
    "Controller": "routers",
    "Service": "services",
    "Component": "services",
    "Repository": "repositories",
}
_LAYER_SINGULAR = {"routers": "router", "services": "service", "repositories": "repository"}


def layer_for_stereotype(stereotype: str | None) -> str | None:
    """回傳 `stereotype` 機械對應到的層級目錄名稱；`None`（含未知
    stereotype）代表機械規則判斷不了，交給六章 LLM（見 05a 三章「無
    stereotype 的類別」，多半併入 services，但實際歸屬可能因業務語意
    而異）。
    """
    return _STEREOTYPE_LAYER.get(stereotype) if stereotype else None


def file_path_for_layer(module: str, layer: str) -> str:
    """對應 05a 三章「檔名規則：{module}_{layer_singular}.py」，`module`
    沿用 `ModuleInfo.module`，已是 snake_case 慣例字串，不需要③額外
    轉換大小寫。
    """
    return f"app/{layer}/{module}_{_LAYER_SINGULAR[layer]}.py"


def schema_file_path(module: str) -> str:
    return f"app/schemas/{module}.py"


def model_file_path(module: str) -> str:
    return f"app/models/{module}.py"


def build_waves(module_list: list[ModuleInfo]) -> list[list[ModuleInfo]]:
    """對 `module_list` 依 `depends_on` 建拓樸順序、分波，對應 05a 六章
    「處理單位：依模組取代整包 Map-Reduce」步驟 1。第一波是沒有
    `depends_on` 的 module，之後每一波是「`depends_on` 全部落在前面
    已完成波次」的 module。循環依賴中止並拋 `DesignAgentCycleError`
    （見 05a 六章「循環依賴」）。

    **依賴完整性檢查先於拓樸排序**：先做一輪存在性檢查、拋
    `DesignAgentUnknownDependencyError`，避免「缺依賴」被拓樸排序迴圈
    誤判成 `DesignAgentCycleError`（見 05a 六章「依賴完整性檢查與循環
    依賴分開判定」）。
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
                f"module_list.depends_on 偵測到循環依賴，無法拓樸排序（見 05a 六章）: {sorted(remaining)}"
            )
        waves.append([by_name[name] for name in sorted(ready)])
        done |= ready
        remaining -= ready
    return waves


# ── 全域基礎設施檔案（05a 三章「全域基礎設施檔案」，機械組裝，不需 LLM）──

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
    """`app/core/database.py` 固定樣板，不經過 LLM 生成。維持同步（不改
    asyncpg/AsyncSession）與 `except Exception: db.rollback()` 的理由見
    05a 三章「database.py」樣板段落，這裡不重複。
    """
    return _DATABASE_PY_TEMPLATE


def render_main_py(interfaces: list[InterfaceSpec]) -> str:
    """`app/main.py`：對每一個「`interfaces` 裡出現過
    `app/routers/{module}_router.py` 這個 `file_path`」的 module，機械
    產生一行 import 與一行 `include_router()`（見 05a 三章）。沒有產出
    router 層檔案的 module（例如整個 module 只有 service/repository）
    不會出現在這份清單裡——純粹檢查 `interfaces` 的 `file_path` 是否落
    在 `app/routers/` 底下，不需要 LLM 介入。
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

    lines = ["from fastapi import FastAPI", "", *imports, "", "app = FastAPI()", *includes]
    return "\n".join(lines) + "\n"


def render_schema_section(file_path: str, class_fields: list[tuple[str, list[tuple[str, str]]]]) -> str:
    """`### {file_path}` + python code block 段落（05a 三章「Schema
    定義段」），`class_fields` 是 `(class_name, [(field_name,
    python_type), ...])` 清單。純機械字串組裝，不需要 LLM 生成這段文字
    本身。一律標成 `(BaseModel)`、`from __future__ import annotations`
    的理由見 05a 三章「格式慣例」，這裡不重複。

    **`Field` import 視內容需要才加**：`class_fields` 裡任一欄位的型別
    字串含 `"Field("`（`type_mapping.extract_schema_fields()` 對有驗證
    限制的欄位會產出的形狀）才在 import 行加上 `Field`，沒有任何欄位帶
    限制時維持只 import `BaseModel`。
    """
    needs_field_import = any("Field(" in python_type for _, fields in class_fields for _, python_type in fields)
    import_line = "from pydantic import BaseModel, Field" if needs_field_import else "from pydantic import BaseModel"
    lines = [f"### {file_path}", "```python", "from __future__ import annotations", "", import_line, ""]
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
    """`### {file_path}` + python code block 段落，延伸自 05a 三章
    「Schema 定義段」同一種機制——`InterfaceSpec` 只能表達函式簽名，
    遇到「這個 class 在 `module_list.methods` 裡完全沒有被追蹤到任何
    一般方法」的情況，同樣沒有結構化欄位可以表達，比照既有 Schema
    定義段的作法用文字渲染進 directory_tree，不是新機制。

    **常見成因**：自訂例外類別（如 `AuthException`）在 Java 端只有
    建構子、沒有一般方法，javalang 的 `class_decl.methods` 不含建構子
    （見 `JavaClassSignature.constructors` docstring），這批 class
    因此永遠不會被 `_build_method_contexts()` 追蹤到、不會產生任何
    `InterfaceSpec`——若不另外處理，這批 class 在 `python_structure`
    裡會完全沒有任何痕跡，④／⑤下游不會被告知要建立對應的 Python 定義
    （見 `design.py` orphan class 偵測邏輯）。

    **刻意不假設 Python 基底類別**（不像 `render_schema_section()`
    固定套用 `(BaseModel)`）：這批 class 可能是例外類別，也可能是其他
    用途，實際該對應 `Exception` 子類別、`dataclass`、或其他寫法，這裡
    只列出建構子簽名這個機械事實＋原始 Java 檔案路徑，交由④／⑤依
    Java 原始碼自行判斷，不在③這裡越界猜測——呼應 05a 三章「機械規則
    判斷不了的部分才問 LLM」，這裡連「要問 LLM 什麼」都不成立（沒有
    一般方法可供 LLM 判斷業務語意），只能誠實列出機械事實。

    `classes`：`(class_name, java_file_path, constructor_param_lists)`
    清單，`constructor_param_lists` 是「每個多載建構子各自的參數清單」
    的清單（一個 class 可能有多個多載建構子，見 05a 四章「多載方法的
    處理」同一種精神），元素是 `(param_name, python_type)`——型別已經
    由呼叫端（`design.py`）透過 `type_mapping.map_java_type()` 轉換過，
    這裡純粹是字串組裝。
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
    `render_class_placeholder_section()` 是同一種「`InterfaceSpec` 表達
    不了、只能用文字渲染進 directory_tree」機制的另一個分支——差別在於
    這裡的 class 有欄位可以列（不是只有建構子簽名），對應到的是純粹
    當籃子傳接值用的 Python `dataclass`，不是 `render_schema_section()`
    的 `(BaseModel)`（那是留給 API 邊界契約用的，見 05a 五章）。

    **觸發情境**（見 `design.py` orphan class 決策樹）：這批 class 沒有
    被任何 `InterfaceSpec` 覆蓋、也不是 `@Entity`（`@Entity` 直接跳過，
    DB schema 欄位規格是④的職責，見 05a 九章），可能因為原始碼裡有
    Lombok annotation（`@Data`／`@Value` 等，javalang 看不到 annotation
    processor 生成的 getter/setter，`methods` 因此是空清單）、也可能
    完全沒有 Lombok 標記、單純是沒寫存取方法的欄位容器（信心較低的
    機械推斷，見 `confidence_note`）。兩種情況的 Python 對應寫法相同，
    差別只在渲染出來的註解要不要提一句「這是怎麼判斷出來的」，供人工
    核對時追溯。

    `classes`：`(class_name, java_file_path, frozen, field_list,
    confidence_note)` 清單。`frozen`：這個 class 的欄位是否全部
    `final`（見 `JavaField.is_final`），決定渲染成
    `@dataclass(frozen=True)` 還是一般 `@dataclass`。`field_list`：
    `(field_name, python_type)`，型別已經由呼叫端透過
    `type_mapping.map_java_type()` 轉換過，這裡純粹是字串組裝，跟
    `render_class_placeholder_section()` 的既有慣例一致。
    `confidence_note`：一句話說明判斷來源（有無 Lombok 標記），寫進
    渲染出來的註解裡。
    """
    lines = [
        f"### {file_path}",
        "```python",
        "from dataclasses import dataclass",
        "",
        "# 以下類別在 Java 端沒有被 module_list 追蹤到任何一般方法、也不是",
        "# @Entity（DB schema 欄位規格是④的職責，這裡不重複）。依欄位宣告",
        "# 機械推斷為單純傳接值用的資料容器，對應 Python dataclass，不是",
        "# API 邊界的 BaseModel（那類契約走 openapi_spec 展開，見 05a 五章）。",
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
    `render_database_py()`／`render_main_py()` 這類全域基礎設施檔案用，
    跟 `render_schema_section()` 只列欄位宣告不同——見 05a 三章「格式
    慣例」第 3 點「基礎設施段...格式與 Schema 定義段相同」）。
    """
    return f"### {file_path}\n```python\n{code}```\n"


def render_directory_tree(
    directory_lines: list[str], schema_fragments: list[str], infra_sections: list[str]
) -> str:
    """把三段（05a 三章「格式慣例」）組成最終
    `PythonStructure.directory_tree` 字串：目錄結構段（純文字樹狀圖）
    ＋ Schema 定義段（各 module 的片段，六章逐波處理完成後依序附加）
    ＋ 基礎設施段（`database.py`／`main.py`，機械產生，見上方兩個
    render 函式）。
    """
    parts = ["\n".join(directory_lines), "", *schema_fragments, *infra_sections]
    return "\n".join(parts)
```

**已驗證**：`build_waves()` 對「order 依賴 user」的兩模組清單正確分成 `[[user], [order]]` 兩波；對 `a→b→a` 的循環輸入正確拋出 `DesignAgentCycleError`；對 `depends_on` 引用不存在 module 名稱（拼字錯誤）的輸入正確拋出 `DesignAgentUnknownDependencyError`，不會被誤判成循環依賴。`render_schema_section()` 對含 `Field(...)` 的欄位正確在 import 行加上 `Field`，不含限制的欄位維持只 import `BaseModel`；`render_database_py()` 產出的 `get_db()` 正確含 `except Exception: db.rollback()` 區塊。`render_dataclass_section()` 對全欄位 `final` 的案例正確渲染 `@dataclass(frozen=True)`，非全 `final` 維持一般 `@dataclass`，無欄位時渲染 `pass`，`confidence_note` 正確出現在註解裡（`tests/design_agent/test_layout.py`）。

---

## 六、`prompts.py` / `llm.py`——LLM 契約與模型選擇（六章）

```python
# design_agent/llm.py
"""③ 架構設計 Agent 專屬的 Claude API 模型選擇。實際呼叫邏輯（client
初始化、Structured Outputs、log_usage() 整合、錯誤處理）在
common/llm_client.py，所有需要呼叫 Claude API 的 Agent 共用同一份（見
00 六章）。這個檔案只負責一件事：③ 用哪個模型，不跟其他 Agent 共用
同一個環境變數。
"""
from __future__ import annotations

import os

from common.llm_client import DEFAULT_MODEL_FALLBACK

DEFAULT_MODEL = os.environ.get("DESIGN_AGENT_MODEL", DEFAULT_MODEL_FALLBACK)
```

```python
# design_agent/prompts.py
"""③ 架構設計 Agent 用到的 Claude API system prompt 與對應 output
schema，對應 05a 六章「LLM 設計階段」。schema 跟 prompt 放同一個檔案的
理由比照 parse_agent/prompts.py 的說明。

輸出契約只問「機械規則判斷不了的部分」（無 stereotype 類別的層級、
找不到 openapi 對應的參數怎麼翻譯成 FastAPI 寫法、要不要加
`db: Session`），不要求 LLM 吐出完整 `InterfaceSpec`——理由見 05a 六章
「單一 module 的 Claude 呼叫內容」，這裡不重複。
"""
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

---

## 七、`design.py`——逐波呼叫 Claude API、組裝 InterfaceSpec（六章）

`design_all_modules()` 是對外唯一入口：依 `layout.build_waves()` 分波、波內平行呼叫 `_design_module()`；每個 module 內部先用三～五章的機械規則組出 `_MethodContext` 骨架，只有真正需要判斷的部分才進 `_call_design_llm()`。

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
from dataclasses import dataclass, field

from common.concurrency import default_concurrency
from common.java_annotations import DATA_CLASS_ANNOTATIONS, JPA_ENTITY_ANNOTATIONS
from common.llm_client import LlmJsonError, call_claude_for_json
from design_agent import layout, signature_scan, type_mapping
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
    layer: str | None  # None 代表機械規則判斷不了，等 LLM 的 class_layers 決定
    params: list[ParamSpec]  # 已知的業務參數，不含 LLM 決定的框架注入/db session 參數
    return_type: str
    uncovered_params: list[UncoveredParam]
    needs_db_session_decision: bool
    boundary_schemas: list[tuple[str, dict]] = field(default_factory=list)  # (schema_name, resolved_schema)，見五章
    http_method: str | None = None  # 僅 boundary（selected_overload）非 None，見五章「router 層 API 邊界方法額外帶」
    route_path: str | None = None   # 同上，直接是 boundary["endpoint"] 原始字面值，不做轉換


def design_all_modules(
    module_list: list[ModuleInfo],
    api_to_python_target: list[ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
) -> tuple[list[InterfaceSpec], str, set[str]]:
    """對外入口，對應 05a 六章全節。回傳
    `(全部 module 攤平的 InterfaceSpec 清單, 組裝完成的 directory_tree 字串,
    實際產出過 schemas/{module}.py 的 module 名稱集合)`。

    第三個回傳值供 `route_mapping.build_route_mappings()` 判斷
    `related_files` 該不該納入 schema 檔案——不能只憑「這個 module 有沒有
    router 檔案」猜測，同一個 module 的 API 邊界方法若全部只用 inline
    schema（沒有 `$ref`，見 `type_mapping.schema_name_for()`），這個
    module 就不會真的產出 `schemas/{module}.py`，猜測會讓
    `route_to_file_mapping` 指向一個 directory_tree 裡實際上不存在的
    「幽靈檔案」，Debug Agent（⑦）跟著這個路徑去讀檔會撲空。
    """
    waves = layout.build_waves(module_list)
    boundary_index = _build_boundary_index(api_to_python_target)

    all_interfaces: list[InterfaceSpec] = []
    schema_fragments: list[str] = []
    modules_with_schema_file: set[str] = set()
    interfaces_by_module: dict[str, list[InterfaceSpec]] = {}

    for wave in waves:
        wave_results = _design_wave_with_retry(wave, boundary_index, openapi_spec, java_project_path, interfaces_by_module)
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
    directory_tree = layout.render_directory_tree(directory_lines, schema_fragments, infra_sections)

    return all_interfaces, directory_tree, modules_with_schema_file


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
    boundary_index: dict[tuple[str, str, str], ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    interfaces_by_module: dict[str, list[InterfaceSpec]],
) -> list[ModuleDesignResult]:
    """對一整波（同一波內彼此不依賴）module 平行呼叫 Claude，失敗的
    module 列入待重試清單，這一波其餘 module 跑完後等待
    `_RETRY_WAIT_SECONDS` 秒統一重試一次；重試仍失敗則中止整條
    design run（見 05a 六章：`python_structure` 是 [P]／④ 唯一的權威
    規格，任何一個 module 的介面缺失風險遠高於重新執行一次）。
    """
    results, failed = _run_wave_batch(wave, boundary_index, openapi_spec, java_project_path, interfaces_by_module)
    if not failed:
        return results

    logger.warning(
        "%d 個 module 的六章設計呼叫失敗，等待 %.0f 秒後統一重試一次: %s",
        len(failed),
        _RETRY_WAIT_SECONDS,
        [m["module"] for m in failed],
    )
    time.sleep(_RETRY_WAIT_SECONDS)

    retry_results, still_failed = _run_wave_batch(failed, boundary_index, openapi_spec, java_project_path, interfaces_by_module)
    results.extend(retry_results)

    if still_failed:
        raise DesignAgentModuleError(
            f"{len(still_failed)} 個 module 的六章設計呼叫重試後仍失敗，中止整個 design run"
            f"（05a 六章的保守預設，見本函式 docstring）: {[m['module'] for m in still_failed]}"
        )
    return results


def _run_wave_batch(
    modules: list[ModuleInfo],
    boundary_index: dict[tuple[str, str, str], ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    interfaces_by_module: dict[str, list[InterfaceSpec]],
) -> tuple[list[ModuleDesignResult], list[ModuleInfo]]:
    """平行處理一批 module（同一波內），回傳
    (成功結果清單, 失敗待重試的 ModuleInfo 清單)。每個 module 的失敗
    互相隔離，不取消其他 module（同一波內彼此本來就互不依賴）。
    """
    results: list[ModuleDesignResult] = []
    failed: list[ModuleInfo] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_WAVE_WORKERS, len(modules))) as pool:
        futures = {
            pool.submit(_design_module, m, boundary_index, openapi_spec, java_project_path, interfaces_by_module): m
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


def _build_boundary_index(api_to_python_target: list[ApiMapping]) -> dict[tuple[str, str, str], ApiMapping]:
    """`(module, class_name, method_name) -> ApiMapping`，供
    `_build_method_contexts()` 判斷一個方法是不是 API 邊界方法。
    `ApiMapping.java_controller` 是 `"ClassName.method_name"`（見 04a
    六章 `assemble_api_mapping()`），這裡拆開重組成 key。

    **已知限制**：`java_controller` 不含參數簽名，若同一個 class 內有
    多載方法且剛好都是不同 endpoint 的 Controller 方法（比 04a 五章
    `voice`/`image` 那種 skip 案例更少見，但理論上可能發生），這裡的
    索引只保留最後一筆 `ApiMapping`。`_build_method_contexts()` 那邊
    再進一步限定：這唯一一筆 operation 只會套用到 `overloads` 清單裡
    `_select_boundary_overload()` 依參數個數選出的那一個多載，不是宣告
    順序第一個（見該函式 docstring），其餘多載一律退回機械型別對應。
    這仍然是猜測，不是精確消歧，留待接上真實專案規模評估是否需要升級
    成 list（並讓 `ApiMapping` 帶參數簽名徹底消歧）。
    """
    index: dict[tuple[str, str, str], ApiMapping] = {}
    for api in api_to_python_target:
        class_name, _, method_name = api["java_controller"].partition(".")
        index[(api["module"], class_name, method_name)] = api
    return index


def _python_function_name(java_method: str, is_private: bool) -> str:
    """05a 七章「私有／內部方法的命名慣例」：camelCase → snake_case，
    Java `private` 方法加底線前綴，保留在同一個 class／檔案內，不特別
    切出獨立檔案。只算單一 method 的基礎名稱，不處理多載消歧——多載時
    同一組 `overloads` 內每個 `sig` 呼叫這裡都會拿到相同字串，消歧邏輯
    在呼叫端 `_build_method_contexts()`（見該函式與 05a 四章「多載方法
    的處理」）。
    """
    name = type_mapping.camel_to_snake(java_method)
    return f"_{name}" if is_private else name


def _select_boundary_overload(overloads: list[JavaMethodSignature], operation: dict) -> JavaMethodSignature:
    """`boundary_index` 對同名多載只能保留一筆 `ApiMapping`（見
    `_build_boundary_index()` 已知限制），這裡要在 `overloads`
    （javalang 掃描的宣告順序，見 05a 四章「多載方法的處理」）裡挑一個
    最可能對應這個 operation 的多載——不挑宣告順序第一個：Java 原始碼
    裡的宣告順序跟哪個多載才是真正的 Controller 端點無關，單純調整
    程式碼排版就可能悄悄換掉綁定結果。改用「Java 參數個數」跟
    operation 的 `parameters` + `requestBody`（算一個）比對，取參數
    個數差距最小的一個；並列時退回宣告順序，讓結果穩定可重現。這仍然
    是猜測，不是精確消歧（`ApiMapping` 本身不含參數型別，見
    `_build_boundary_index()` docstring），只是比「永遠挑宣告順序第一
    個」更貼近實際簽名形狀。
    """
    expected_count = len(operation.get("parameters") or []) + (1 if operation.get("requestBody") else 0)
    return min(overloads, key=lambda sig: (abs(len(sig.params) - expected_count), overloads.index(sig)))


def _build_method_contexts(
    module: ModuleInfo,
    class_signatures: dict[str, JavaClassSignature],
    boundary_index: dict[tuple[str, str, str], ApiMapping],
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

        layer = layout.layer_for_stereotype(class_sig.stereotype)
        boundary = boundary_index.get((module["module"], class_sig.class_name, method_info["java_method"]))
        operation = (
            type_mapping.find_operation(boundary["endpoint"], boundary["http_method"], openapi_spec)
            if boundary is not None
            else None
        )
        # 這個 operation 只屬於「一個」物理方法，boundary_index 對同名
        # 多載本來就只能保留一筆 ApiMapping（見 _build_boundary_index()
        # docstring）。用 _select_boundary_overload() 依參數個數挑出最可能
        # 的那一個多載，而不是宣告順序第一個——不能讓迴圈裡剩下的其他
        # 多載也套用同一份 operation，那會把同一個 endpoint 的參數/回傳
        # 型別錯誤地複製到不相干的多載方法上。
        selected_overload = _select_boundary_overload(overloads, operation) if operation is not None else None

        # 多載消歧（05a 四章「多載方法的處理」）：同一組 overloads 的
        # sig.method_name 完全相同，_python_function_name() 對每個 sig
        # 都會算出同一個基礎名稱，必須在這裡依宣告順序疊加計數器消歧，
        # 否則同一個 class／檔案會產出兩個同名 InterfaceSpec（Python 不
        # 支援多載，這是非法輸出，不只是精準度問題）。第一次出現保留
        # 原始名稱，第二次起加 `_2`、`_3`……——比照 04a 三章引用過的
        # springdoc voice/voice_1 前例，用「從 2 開始」單純是對人類讀者
        # 更直覺，不是唯一合法編號方式。
        seen_names: dict[str, int] = {}
        for sig in overloads:
            if sig is selected_overload:
                params, return_type = type_mapping.resolve_api_boundary_signature(sig, operation, openapi_spec)
                uncovered = type_mapping.find_uncovered_framework_params(sig, operation, openapi_spec)
                boundary_schemas = type_mapping.collect_named_schemas(operation, openapi_spec)
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


def _design_module(
    module: ModuleInfo,
    boundary_index: dict[tuple[str, str, str], ApiMapping],
    openapi_spec: dict,
    java_project_path: str,
    interfaces_by_module: dict[str, list[InterfaceSpec]],
) -> ModuleDesignResult:
    """對應 05a 六章「單一 module 的 Claude 呼叫內容」全表格：組出這個
    module 的 `_MethodContext` 清單、決定這次呼叫真正需要 LLM 回答的
    問題（無 stereotype 類別的層級、框架注入參數、db session 判斷），
    視需要呼叫一次 Claude，合併回完整的 `InterfaceSpec`。
    """
    class_signatures = signature_scan.scan_java_files(module["java_files"], java_project_path)
    contexts = _build_method_contexts(module, class_signatures, boundary_index, openapi_spec)

    # 只問「有一般方法」的無 stereotype 類別要歸哪一層：一個 class 若
    # `methods` 是空清單（常見情況：只有建構子的自訂例外類別，見
    # JavaClassSignature.constructors docstring），永遠不會有任何
    # _MethodContext 引用到它（overloads 永遠篩不到東西），LLM 回答的
    # 層級因此永遠用不到——這批 class 改走下方 orphan class 處理，不需要
    # 也不該浪費一次 LLM 問答在一個沒有答案會被使用的問題上。
    classes_needing_layer = [c for c in class_signatures.values() if c.stereotype is None and c.methods]
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

        interfaces.append(
            InterfaceSpec(
                file_path=layout.file_path_for_layer(module["module"], layer),
                class_name=None if layer == "routers" else ctx.class_name,
                function_name=ctx.function_name,
                params=params,
                return_type=ctx.return_type,
                http_method=ctx.http_method,
                route_path=ctx.route_path,
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
    # 裡會完全沒有任何痕跡。依機械事實分五種情況處理（見 05a 三章「孤兒
    # 類別／資料容器占位」判斷優先序，理由見 common/java_annotations.py
    # docstring）：
    #   1. @Entity（含 @Embeddable/@MappedSuperclass）→ 跳過，DB schema
    #      欄位規格是④的職責（05a 九章），不是③要處理的範圍
    #   2. 有 Lombok/JPA 資料標記 → 渲染 dataclass（欄位為主，高信心）
    #   3. 有建構子（現有 AuthException 案例，順序與行為不變）→ 渲染
    #      建構子占位
    #   4. 有欄位、無標記（低信心推斷）→ 渲染 dataclass
    #   5. 什麼都沒有 → 只記警告，不渲染空段落
    # 已經出現在 seen_schema_names（API 邊界 openapi 展開產出的 schema
    # 名稱）的 class 一併排除，避免同一個型別被兩種機制各渲染一次。
    known_classes = frozenset(class_signatures)
    covered_class_names = {iface["class_name"] for iface in interfaces if iface["class_name"]}
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

**已驗證**（`unittest.mock.patch` 掉 `call_claude_for_json`，跑幾組含 GET/POST/private 方法的合成 module）：
- `get_by_id` 正確拿到 `userId: int` 業務參數 + LLM 決定的 `request: Request` 框架參數、回傳型別 `UserResponse`；`create` 正確拿到 `req: UserCreateRequest` 業務參數（型別比對修正後，沒有被誤判成 uncovered，見四章「已驗證」）；`helper`（private）正確產出 `function_name="_helper"`。
- `directory_tree` 正確組出 Schema 定義段（`from pydantic import BaseModel` + `UserResponse(BaseModel)`／`UserCreateRequest(BaseModel)` 欄位）＋ `database.py`／`main.py`（含 `include_router()`）；`route_to_file_mapping` 正確帶上 `app/schemas/user.py`。
- **db session 依層級產生不同宣告**：router 層方法標記 `needs_db_session=True` 時，正確產出 `db: Session = Depends(get_db)`；repository 層方法標記同樣的旗標時，正確產出不帶預設值的 `db: Session`（呼叫端往下傳，不是 FastAPI DI 進入點）。
- **`related_files` 不含幽靈檔案**：一個只有 inline response schema（無 `$ref`）的 module，`directory_tree` 不會產出 `schemas/{module}.py`，`route_to_file_mapping` 也正確不把這個檔案路徑塞進 `related_files`；換成另一個有具名 schema 的 module 則正常帶上——`modules_with_schema_file` 精確追蹤到「這個 module 六章實際有沒有產出 schema 檔案」，不是用「有沒有 router 檔案」猜測。
- **目錄結構段（`_render_directory_lines()`）涵蓋 `schemas`／`models`**：驗證過同一組多 module 輸入，目錄結構段正確列出所有 `all_module_names` 的 `app/models/{module}.py`，以及僅 `modules_with_schema_file` 裡 module 的 `app/schemas/{module}.py`（沒有具名 schema 的 module 不會出現）——跟上一條 `related_files` 的判斷邏輯共用同一個集合，兩處不會對不上。
- **多載方法不會共用同一份 openapi operation，且挑選依參數個數而非宣告順序**：一個 class 內兩個同名多載方法（`save(String)`／`save(String, Integer)`），只有一個掛了實際 endpoint 時，驗證過 `_select_boundary_overload()` 正確挑中參數個數跟 operation 更接近的那一個拿到 openapi 覆寫的參數/回傳型別，另一個正確退回機械型別對應（`String`→`str`／`Integer`→`int`），不會兩個都套用同一份 operation；刻意把兩個多載在 `overloads` 清單裡的宣告順序對調後重跑，選中的仍是參數個數較接近的那一個，確認不是「永遠選第一個」。
- **多載方法的 `function_name` 消歧**（見 05a 四章）：直接呼叫 `_build_method_contexts()`（不 mock LLM，這段不涉及 Claude API）餵入一個 class 內三個同名多載方法（`voice(String)`／`voice(String, int)`／`voice(String, int, int)`），確認產出 `function_name` 依宣告順序分別是 `voice`／`voice_2`／`voice_3`，三者互不相同。
- **孤兒類別／資料容器占位五分支判斷**（見 05a 三章「孤兒類別與資料容器占位」）：直接呼叫 `_design_module()`（`module["methods"]` 留空避免觸發 `_call_design_llm()`，不涉及真實 Claude API）餵入六種合成 Java 檔案，逐一驗證五個分支——`@Entity` 標記的類別完全不出現在 `directory_tree_fragment`、也不記警告（分支 1）；`@Data` 且欄位部分非 `final` 的類別正確渲染成一般 `@dataclass`、註解帶「偵測到 Lombok/JPA 資料標記」（分支 2）；`@Value` 且全部欄位 `final` 的類別正確渲染成 `@dataclass(frozen=True)`（分支 2）；既有的自訂例外類別（有建構子、無 Lombok 標記）確認優先序調整後仍正確走建構子占位段落、沒有被誤判進 dataclass 分支（分支 3，迴歸測試）；無 Lombok 標記但有欄位的類別正確渲染成 `@dataclass`、註解帶「無 Lombok 標記，依欄位宣告推斷」（分支 4）；無方法、無建構子、無欄位的類別只記一筆警告、不渲染任何段落（分支 5）；另外驗證 `methods` 非空的前置判斷本身——一個方法齊全、但剛好沒有欄位也沒有建構子的 `@RestController`（`unittest.mock.patch` 掉 `_call_design_llm()`，因為 router 方法一律 `needs_db_session_decision=True`，光留空 `module["methods"]` 無法避開 LLM 呼叫），確認其方法正確產出 `InterfaceSpec`、且不會被誤判進孤兒類別任何一個分支。詳見 `tests/design_agent/test_design.py`。

---

## 八、`route_mapping.py`——`route_to_file_mapping`／`route_to_module_mapping` 機械合併與落地寫入（八、九章）

除了機械合併 `route_to_file_mapping`／`route_to_module_mapping`（05a 八章，設計理由詳見該章，這裡不重複），這個檔案也負責把結果實際寫入 `config/harness.yaml`（05a 九章、00 八章明訂的③職責），不是只回傳給 State 就結束——`config/harness.yaml` 是 Harness（Agent ⑥）驗證階段直接讀檔案的來源，State 裡的值到不了那裡；`route_to_module_mapping` 不進 `RefactorState`，只寫入 yaml（見 05a 八章）。

```python
# design_agent/route_mapping.py
"""③ 架構設計 Agent：route_to_file_mapping／route_to_module_mapping 機械
合併，對應 05a 八章全節（設計理由詳見該章，這裡不重複）。完全是程式
邏輯，不需要 LLM——所有需要的資訊（`api_to_python_target` 的
endpoint↔module 對應、每個 module 的 `interfaces` 檔案集合）在六章都
已經產出完畢。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from design_agent.layout import schema_file_path
from graph.state import ApiMapping, InterfaceSpec

_PATH_PARAM_RE = re.compile(r"\{[^{}]+\}")


def normalize_path_key(http_method: str, endpoint: str) -> str:
    """對應 05a 八章「Key 格式」：**必須跟 02a `RouteMapper.
    normalize_path_key()` 的正規化結果完全一致**，否則 Harness 的比對
    會全部 miss。`endpoint` 是 openapi 的 path 樣板（如
    `/api/v1/users/{userId}`），逐字元處理，不是「切段、丟掉空字串、
    再重組」：

    1. 去除開頭 `/`（只去開頭這一個，不動其他任何 `/`）
    2. 所有（剩下的）`/` 換成 `_`
    3. 所有 `{paramName}` 樣板，不論原始參數名稱是什麼，一律換成字面
       `{id}`——因為 `RouteMapper.normalize_path_key()` 是在**實際 URL**
       上把「純數字/UUID」換成 `{id}`，並不知道原始參數名稱叫什麼，
       兩邊要能精確匹配，這裡也必須捨棄參數名稱、統一用 `{id}`

    ```
    /api/v1/users/{userId} + GET  → GET_api_v1_users_{id}
    /api/v1/users/{userId}/profiles + GET → GET_api_v1_users_{id}_profiles
    ```

    **刻意不「切段、過濾空字串、重新 join」**：那種寫法會把結尾多帶
    一個 `/` 的路徑（如 `/api/v1/users/`）跟不帶結尾 `/` 的版本
    （`/api/v1/users`）normalize 成同一個 key（都是 `..._users`）。
    但 `RouteMapper.normalize_path_key()`（`refactor_harness/core/
    route_mapper.py`）拿到的 `url_parts` 是 Postman `request.url.path`
    這個**已經切好的陣列**，它的邏輯只是逐段判斷數字/UUID、原樣
    `"_".join(...)`，不會主動丟掉空字串——若 Postman 對一個結尾帶 `/`
    的 URL 產出帶結尾空字串的 `path` 陣列，`RouteMapper` 那邊 join 出來
    的 key 會帶著結尾底線（如 `GET_api_v1_users_`），若這裡先把它
    filter 掉會兩邊對不上。因此這裡改成「只動開頭那一個 `/`、其餘
    `/` 逐一換成 `_`」的字面規則，結尾/中間是否有多餘 `/` 兩邊都會
    留下同樣的痕跡，才能保證跟 `RouteMapper` 的正規化結果位元對位元
    一致——不是靠事後幫兩邊的 key 都做 trim 去湊出一致（trim 只在兩邊
    都真的一致地被 trim 過才安全，這裡沒有把握 `RouteMapper` 那側也
    會 trim，貿然單邊 trim 反而製造新的不一致）。
    """
    path = endpoint.removeprefix("/")
    path = path.replace("/", "_")
    path = _PATH_PARAM_RE.sub("{id}", path)
    return f"{http_method.upper()}_{path}"


def build_route_mappings(
    api_to_python_target: list[ApiMapping],
    interfaces: list[InterfaceSpec],
    modules_with_schema_file: set[str],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """對應 05a 八章全節（`related_files` 的組成、route_to_module_mapping
    的存在理由，詳見該章，這裡不重複）。回傳
    `(route_to_file_mapping, route_to_module_mapping)`——共用同一個
    `normalize_path_key()` 迴圈一次產出，`route_to_module_mapping` 的值
    就是這筆 `ApiMapping.module` 本尊，不是新的判斷。

    **`schemas/{module}.py` 是否存在的判定，必須用 `modules_with_schema_
    file` 精確比對，不能用「這個 module 有沒有 router 檔案」猜測**：同一
    個 module 的 API 邊界方法可能全部只用 inline schema（沒有具名
    `$ref`，見 `type_mapping.schema_name_for()`），這種情況下即使有
    router 檔案，`design.py` 也不會真的產出 `schemas/{module}.py`（見
    `design_all_modules()` 的 `modules_with_schema_file` 集合，只在
    `ModuleDesignResult.directory_tree_fragment` 非空時才會收錄這個
    module）。若這裡改用「有 router 檔案就假設有 schema 檔案」的猜測，
    會讓 `related_files` 指向一個 directory_tree 裡實際上不存在的「幽靈
    檔案」，Debug Agent 跟著這個路徑去讀檔會撲空。
    """
    files_by_module: dict[str, set[str]] = {}
    for iface in interfaces:
        module = _module_of(iface["file_path"])
        files_by_module.setdefault(module, set()).add(iface["file_path"])

    file_mapping: dict[str, list[str]] = {}
    module_mapping: dict[str, str] = {}
    for api in api_to_python_target:
        key = normalize_path_key(api["http_method"], api["endpoint"])
        related = set(files_by_module.get(api["module"], set()))
        if api["module"] in modules_with_schema_file:
            related.add(schema_file_path(api["module"]))
        file_mapping[key] = sorted(related)
        module_mapping[key] = api["module"]
    return file_mapping, module_mapping


def write_route_mappings(
    route_to_file_mapping: dict[str, list[str]],
    route_to_module_mapping: dict[str, str],
    config_path: str = "config/harness.yaml",
) -> None:
    """對應 05a 九章、00 八章「`route_to_file_mapping` 產出後直接寫入
    `config/harness.yaml`，不需人工填寫」，以及 05a 八章
    `route_to_module_mapping` 的落地——這裡是實際落地檔案 I/O 的地方，
    不只是回傳給 State 就結束（`RouteMapper` 是純讀檔案的類別，不吃
    LangGraph State，見 02a 十一章）。跟 `spec_node.py`／
    `collection_node.py` 呼叫的 `run_spec_agent()`／
    `run_collection_agent()` 一樣，檔案 I/O 副作用放在 Agent 自己的執行
    過程裡完成，不留到流程末端另外用一個 Orchestrator 步驟去沖刷。

    **兩個 key 必須在同一次讀寫回合裡一起覆寫**：`safe_load`／
    `safe_dump` 是整檔 round-trip，分兩次呼叫各自「讀取＋覆寫單一 key
    ＋寫回」會讓後一次寫回的整份 config 蓋掉前一次剛寫入的那個 key。
    其餘段落（`databases`／`services`／`collections`／`diff_rules`，這些
    是人工在 00 五章、02a 設定好的環境設定，不是③的產出）讀進來後原樣
    保留、寫回去。

    **已知限制**：用 PyYAML 的 `safe_load`／`safe_dump` 做「讀取＋覆寫＋
    寫回」，不是保留註解的 round-trip parser（如 `ruamel.yaml`）——
    `config/harness.yaml` 裡原本給人看的註解在第一次被③寫入後會消失，
    其餘機器可讀的 key/value 不受影響，只有註解會不見。這是接受的取捨，
    不是遺漏：專案目前唯一用到的 YAML 函式庫是 PyYAML
    （`refactor_harness/core/route_mapper.py` 已經在用），為了保留註解
    另外引入一個新函式庫，成本高於這個取捨的代價。
    """
    path = Path(config_path)
    config: dict = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    config["route_to_file_mapping"] = route_to_file_mapping
    config["route_to_module_mapping"] = route_to_module_mapping
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _module_of(file_path: str) -> str:
    """從 `InterfaceSpec.file_path`（如 `app/services/user_service.py`）
    反推 module 名稱——`layout.file_path_for_layer()` 的逆運算：檔名固定
    是 `{module}_{layer_singular}.py`，`layer_singular` 只有三種
    （router／service／repository，見 05a 三章「檔名規則」），去掉這個
    後綴即為 module 名稱。
    """
    stem = file_path.rsplit("/", 1)[-1].removesuffix(".py")
    for suffix in ("_router", "_service", "_repository"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem  # 不應該發生（見 05a 三章檔名規則），保底原樣回傳
```

**已驗證**（`route_to_file_mapping` 部分，`route_to_module_mapping` 是本輪新增、尚未跑過同等驗證，見十一章）：對一份沿用既有格式的 `config/harness.yaml`（含 `databases`／`services`／`collections`／`diff_rules`）跑過舊版 `write_route_to_file_mapping()`，其餘段落原封不動、只有 `route_to_file_mapping` 被換成這次產出的內容，`route_mapper.RouteMapper` 讀取端的資料格式不受影響。`normalize_path_key()` 對一般 endpoint 與結尾多帶 `/` 的 endpoint 都驗證過——後者正確保留結尾底線（`GET_api_v1_users_`），不會被悄悄 filter 掉。

---

## 九、`__init__.py`——對外唯一入口

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
    """
    interfaces, directory_tree, modules_with_schema_file = design.design_all_modules(
        module_list, api_to_python_target, openapi_spec, java_project_path
    )
    python_structure = PythonStructure(directory_tree=directory_tree, interfaces=interfaces)
    route_to_file_mapping, route_to_module_mapping = route_mapping.build_route_mappings(
        api_to_python_target, interfaces, modules_with_schema_file
    )
    route_mapping.write_route_mappings(
        route_to_file_mapping, route_to_module_mapping, config_path=harness_config_path
    )
    return python_structure, route_to_file_mapping
```

---

## 十、`graph/nodes/design_node.py`——LangGraph node 實作

取代原本回傳假資料的 stub。對應 05a 十一章：`design` 是平行分支 node，`parse` 完成後與 `record_tests`（②）同時進入就緒狀態，只回傳自己實際更動的 key（`python_structure`／`route_to_file_mapping`），不能用 `{**state, ...}` 整包展開。

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
    )

    return {
        "python_structure": python_structure,
        "route_to_file_mapping": route_to_file_mapping,
    }
```

---

## 十一、已知限制與待驗證事項

- **`_build_boundary_index()` 的多載碰撞（索引層級）**：`ApiMapping.java_controller` 不含參數簽名，同一 class 內若有多載方法各自掛不同 endpoint，索引本身只保留最後一筆 `ApiMapping`（見 `design.py` 該函式 docstring）——這一層的限制還在；但**消費端已經比宣告順序更好一步**：`_select_boundary_overload()` 依 Java 參數個數跟 operation 的 `parameters`＋`requestBody` 個數比對，挑參數個數最接近的那個多載套用這筆 operation，不會讓多個多載共用同一份 operation，也不再是「宣告順序第一個就中」（見七章「已驗證」）。這仍是啟發式、不是精確消歧——若兩個多載參數個數剛好相同，還是可能選錯。真實專案若真的出現這種多載碰撞，需要索引升級成 `dict[key, list[ApiMapping]]` 並想辦法消歧（可能需要 [B] Collection Agent 在 `ApiMapping` 額外帶入 Java 參數型別資訊）。
- **`_classify_params()` 的 requestBody 型別比對退化情形**：Java DTO 類別名稱與 springdoc 產生的 schema 名稱不一致、且同一方法有多個型別比對不到的參數時，全部落入 `uncovered_params`、交給六章 LLM 依描述語境判斷，沒有更精確的機械手段（見 `type_mapping._classify_params()` docstring「已知限制」，這點 05a 十三章本來就列為「待接上真實專案輸出後校準」）。
- **`route_mapping._module_of()` 的檔名慣例耦合**：從 `file_path` 反推 module 名稱依賴 `layout.file_path_for_layer()` 的命名慣例（`{module}_{layer_singular}.py`），兩邊若日後各自演化，需要同步維護；沒有另外傳一份 `file_path -> module` 對照表的原因是這份資訊在六章當下就有（`_design_module()` 內部知道），但 `route_mapping.py` 刻意設計成純函式、只吃 `interfaces`，不額外要求呼叫端多傳一份輔助結構。
- **`common/openapi_ref_resolver.py` 與 `spec_collection_agent/openapi_refs.py` 尚未合併**：延續 05a 十三章的既有技術債，這次只新增了共用介面、讓③直接對齊，03a/03c 遷移過去共用是後續一個獨立的小重構。
- **`write_route_mappings()` 不保留 `config/harness.yaml` 既有註解**：PyYAML 的 `safe_load`／`safe_dump` 不是 round-trip parser，③第一次寫入後，檔案裡原本給人看的註解會消失（機器可讀的 key/value 不受影響）。接受的取捨，不是遺漏（見 `route_mapping.write_route_mappings()` docstring）。
- **`route_to_module_mapping`（`build_route_mappings()`／`write_route_mappings()`）是本輪新增、尚未跑過「已驗證」段落那組真實 config/harness.yaml 測試**：邏輯上與既有的 `route_to_file_mapping` 共用同一個迴圈與 key，理論上不會有獨立的失效模式，但實際跑過 `RouteMapper.resolve_module()`（02b）讀取這份新段落之前，仍算未驗證，留待接上真實③輸出後補測。
- **`X | None` 語法要求目標 Python 服務 ≥ 3.10**：`extract_schema_fields()`／`map_java_type()` 的 `Optional` 對應都用 PEP 604 union 語法。這項前提已經明訂進 `00_refactor_architecture.md` 三章「Python 目標技術棧」，不再是隱含假設；若這個前提未來改變，`extract_schema_fields()` 跟 `map_java_type()` 都需要一併改成 `typing.Optional[T]`，不是只改其中一處（見 `type_mapping.py` 相關函式 docstring「環境前提」）。
- **`BigDecimal` → `Decimal` 只在非 API 邊界方法生效**：API 邊界方法改用 openapi_spec 決定型別，springdoc 對 `BigDecimal` 欄位通常序列化成通用 `number` type，沒有訊號能標示「這原本是 BigDecimal」，這個欄位的 API 邊界型別仍是 `float`，是既有設計原則（openapi_spec 為邊界方法唯一權威來源）下的既知落差（見 `type_mapping.map_java_type()` docstring）。
- **`from __future__ import annotations` 只解決同檔案內的循環參照**：跨 `schemas/{module}.py` 檔案的循環 import（`user.py` 直接 `import` `order.py`、反之亦然）不會被這一行解決，需要 `TYPE_CHECKING` guard＋`model_rebuild()`，屬於④如何實際生成、串接檔案間 import 的問題，留給 07a／08a（皆待建立）處理（見 `layout.render_schema_section()` docstring）。
- **巢狀具名 schema 只保證型別字串正確，不保證一定有對應的 class 定義**：`extract_schema_fields()` 對巢狀 `$ref`（如 `User` 的某個欄位是 `List[Order]`）能正確產出 `Order` 這個型別名稱（見 `type_mapping.openapi_type_to_python()` docstring「`$ref` 一律優先於其他判斷」），但 `collect_named_schemas()` 只收集 requestBody／2xx response 這兩個**頂層**具名 schema，不遞迴收集巢狀關聯到的 schema——若 `Order` 從未自己是任何 operation 的頂層 requestBody/response，`directory_tree` 的 Schema 定義段就不會有 `class Order(BaseModel):`，④只能看著一個沒有定義的型別名稱。刻意不做遞迴收集的原因：巢狀關聯到的 schema 可能屬於**另一個 module**（如 `Order` 屬於 order module、被 user module 的 `User` 引用），遞迴收集會把 `class Order` 錯誤地渲染進 user module 的 `schemas/user.py`，造成跨 module 定義重複或位置錯誤——這需要先決定「巢狀具名 schema 該歸哪個 module」（05a 沒有規範這件事），不是單純的機械收集問題，留待接上真實專案輸出、確認是否真的出現跨 module 巢狀引用後再設計。
- **三處 LLM 判斷點的 prompt 品質未經真實專案校準**：無 stereotype 類別的層級歸屬、框架注入物件轉換、`db: Session` 慣例注入，這三個 `prompts.py` 裡的判斷點目前只用合成範例驗證過契約可以正確跑通（見七章「已驗證」），實際判斷品質待接上真實 `lang-exam-api-refactor` 輸出後校準（延續 05a 十三章）。

---

*各 Agent 的實作細節、演算法、程式碼一律留在對應細節文件，避免重複維護；本文件隨實作推進持續更新。*
