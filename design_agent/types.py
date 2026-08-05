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
    """

    file_path: str
    class_name: str
    stereotype: str | None  # "RestController"/"Controller"/"Service"/"Component"/"Repository"/None
    methods: list[JavaMethodSignature] = field(default_factory=list)
    constructors: list[JavaMethodSignature] = field(default_factory=list)


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
