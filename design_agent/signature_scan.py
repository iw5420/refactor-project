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

from design_agent.types import JavaClassSignature, JavaMethodSignature, JavaParam

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
                    if isinstance(decl, javalang.tree.ClassDeclaration)
                    else []
                ),
            )
    return result


def _stereotype_of(annotations: list) -> str | None:
    for ann in annotations:
        if ann.name in _STEREOTYPES:
            return ann.name
    return None


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
