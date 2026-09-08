# design_agent/layout.py
"""③ 架構設計 Agent：Python 分層與命名慣例、module 依賴拓樸分波、
directory_tree 組裝、全域基礎設施檔案，對應 05a 三章全節。
"""
from __future__ import annotations

import re

from common.java_type_mapping import camel_to_snake
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

# Phase 1（entity/dto/repository/utils）／Phase 2（controller/service）
# 分階段翻譯設計（見 refactor_plan.md 一、二章）：repositories／utils
# 歸 Phase 1，routers／services 歸 Phase 2。entity／dto 不經過這張表——
# 它們從不產生一般 InterfaceSpec（entity 直接跳過，DTO 走 openapi 展開
# 進 directory_tree 文字，見九章、五章），phase 欄位的計算天然用不到。
_PHASE_1_LAYERS = {"repositories", "utils"}


def layer_for_stereotype(stereotype: str | None) -> str | None:
    """回傳 `stereotype` 機械對應到的層級目錄名稱；`None`（含未知
    stereotype）代表機械規則判斷不了，交給六章 LLM（見 05a 三章「無
    stereotype 的類別」，多半併入 services，但實際歸屬可能因業務語意
    而異）。

    **不含 utils 判斷**：這裡只看 stereotype，不看 package，維持
    `global_infra.py::scan_value_injected_fields()` 既有呼叫端的行為
    不變（`@Value` 欄位注入在 `@UtilityClass` 靜態方法類別上不是真實
    會出現的 Spring 模式，這條路徑不需要跟著改）。六章 `design.py` 的
    層級判定改呼叫 `layer_for_class()`（見下方），會先檢查 utils
    package 再退回這裡的 stereotype 對應。
    """
    return _STEREOTYPE_LAYER.get(stereotype) if stereotype else None


def is_utils_package(package: str | None) -> bool:
    """判斷 Java class 的 package 是否落在 `xxx.utils` 下（05a 三章
    「Utils 特例」，`refactor_plan.md` 二章已定案：幾乎所有 Java 專案
    都遵守這個慣例，比「無 stereotype + 全靜態方法」這種行為推斷簡單、
    可靠得多）。`package` 為 `None`（default package，Java 專案裡極
    罕見）一律回傳 `False`。
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
    六章 LLM（見 05a 三章）。
    """
    if is_utils_package(package):
        return "utils"
    return layer_for_stereotype(stereotype)


def phase_for_layer(layer: str) -> int:
    """`layer`（`layer_for_class()` 或 LLM `class_layers` 回傳的層級
    名稱）對應到 Phase 1 或 Phase 2（見 `refactor_plan.md` 二章）：
    `repositories`／`utils` → 1，其餘（`routers`／`services`） → 2。
    """
    return 1 if layer in _PHASE_1_LAYERS else 2


def file_path_for_layer(module: str, layer: str) -> str:
    """對應 05a 三章「檔名規則：{module}_{layer_singular}.py」，`module`
    沿用 `ModuleInfo.module`，已是 snake_case 慣例字串，不需要③額外
    轉換大小寫。**不適用於 utils 層**——見 `file_path_for_utils()`。
    """
    return f"app/{layer}/{module}_{_LAYER_SINGULAR[layer]}.py"


def file_path_for_utils(java_class_name: str) -> str:
    """對應 05a 三章「Utils 特例」：不套用 `{module}_{layer}.py` 規則
    （utils 橫跨多個 module，套用會出現歸屬假問題），直接複製 Java
    package 結構，一個 Java class 對一個 Python 檔案，不分 module——
    `ValidationUtil` → `app/utils/validation_util.py`。
    """
    return f"app/utils/{camel_to_snake(java_class_name)}.py"


def schema_file_path(module: str) -> str:
    return f"app/schemas/{module}.py"


def model_file_path(module: str) -> str:
    return f"app/models/{module}.py"


# 全域例外處理（`_global` 保留模組，見 design.py「全域生效類別」處理）
# 固定輸出的檔案路徑——這批方法不屬於任何 module 分層慣例
# （`_STEREOTYPE_LAYER`／`file_path_for_layer()`），對應
# docs/09b_bug_trace.md #11/#12。
EXCEPTION_HANDLERS_FILE = "app/core/exception_handlers.py"


def build_waves(module_list: list[ModuleInfo]) -> list[list[ModuleInfo]]:
    """對 `module_list` 依 `depends_on` 建拓樸順序、分波，對應 05a 六章
    「處理單位：依模組取代整包 Map-Reduce」步驟 1。第一波是沒有
    `depends_on` 的 module，之後每一波是「`depends_on` 全部落在前面
    已完成波次」的 module。

    循環依賴視為上游輸入資料錯誤，直接中止（見 05a 六章「循環依賴」）
    ——不嘗試自動打斷環或猜測合理順序，交由人工排查 `module_list` 本身
    的問題。

    **依賴完整性檢查先於拓樸排序**：`depends_on` 若引用了不存在於
    `module_list` 的 module 名稱（上游拼字錯誤等輸入問題），這個依賴
    永遠無法被滿足，拓樸排序迴圈最終還是會因為 `ready` 沒有新成員而
    卡住——但那樣拋出的會是 `DesignAgentCycleError`，把「缺依賴」誤導
    成「循環依賴」，兩種根因完全不同，除錯方向會被誤導。因此在進入
    排序迴圈前先做一輪存在性檢查，缺依賴用專屬的
    `DesignAgentUnknownDependencyError` 明確標示。
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
    """`app/core/database.py` 固定樣板，落實 00 五章「Python 服務統一讀
    環境變數 DATABASE_URL」的規範——寫死讀取 `DATABASE_URL`、建立
    SQLAlchemy engine／`SessionLocal`／`Base`／`get_db()` dependency，
    字面模板（欄位、變數名固定）不經過 LLM，避免④骨架生成呼叫（本地
    模型 qwen）自行臆測別的環境變數名稱或連線寫法（見 05a 三章）。

    **維持同步（`create_engine`／`Session`），不改非同步**：這是重構
    專案，目標是跟 Java 服務（傳統 blocking JDBC）行為對齊，不是從零
    打造高併發 API，見 00 三章「Python 目標技術棧」的明確決策。改非
    同步會讓 ④／⑤ 產生的每一支碰到 DB 的函式（repository／service／
    router 三層）都要正確加上 `async`/`await`，這個正確性負擔會落在
    ⑤ 呼叫的本地模型（qwen2.5-coder:32b）身上——本地模型的可靠度本來
    就是這個專案風險最高的環節（見 00 三章「硬體限制」），不該再加大
    它的出錯面。

    **`except Exception: db.rollback()` 是明確補上的清理步驟，不是修
    一個既有的髒資料 bug**：SQLAlchemy 的 `Session.close()` 本身就會
    在關閉前把還沒 commit 的交易 rollback 掉（見官方文件 `Session.
    close()` 說明），所以就算沒有這段 `except`，`finally: db.close()`
    也不會真的留下沒 rollback 的髒交易。加上這段的理由是**明確性與
    即時性**：例外發生當下立刻 rollback，不是隱含依賴 `close()` 的
    內部行為，讓④／⑤產生的程式碼、以及日後接手的人都能一眼看懂交易
    邊界，也是業界常見寫法。
    """
    return _DATABASE_PY_TEMPLATE


def render_main_py(interfaces: list[InterfaceSpec]) -> str:
    """`app/main.py`：對每一個「`interfaces` 裡出現過
    `app/routers/{module}_router.py` 這個 `file_path`」的 module，機械
    產生一行 import 與一行 `include_router()`（見 05a 三章）。沒有產出
    router 層檔案的 module（例如整個 module 只有 service/repository）
    不會出現在這份清單裡——純粹檢查 `interfaces` 的 `file_path` 是否落
    在 `app/routers/` 底下，不需要 LLM 介入。

    **全域例外處理註冊（見 `EXCEPTION_HANDLERS_FILE`、
    `docs/09b_bug_trace.md` #11/#12／#57）**：`interfaces` 裡若有
    `file_path == EXCEPTION_HANDLERS_FILE` 的項目（`design.py` 的
    `_design_global_advice_module()` 產生，範圍刻意收斂只涵蓋
    `@ExceptionHandler(Exception.class)` 這種全域 catch-all case，見該
    函式），為每一個這樣的函式機械產生 import＋**三行**
    `app.add_exception_handler()` 註冊行：`Exception`／`HTTPException`／
    `RequestValidationError`。**只註冊 `Exception` 不夠**——FastAPI 會替
    `HTTPException`／`RequestValidationError` 這兩種型別預先註冊自己的
    內建預設處理器，Starlette 分派例外時是精確型別優先比對，不是找 MRO
    最近的祖先類別，內建的具體型別處理器會贏過這裡只註冊 `Exception`
    的泛用處理器——只註冊 `Exception` 時，這兩種例外會被 FastAPI 自己的
    預設處理器接走，回應是 FastAPI 的內建格式（如 `{"detail": "..."}`），
    不是這個專案統一的 `ResponseResult` 回應慣例，等於「全域 catch-all」
    這個設計目標在這兩種常見例外型別上完全落空（已用隔離測試證實，見
    #57）。三行都指向同一個 Python 函式（Java 端 `@ExceptionHandler
    (Exception.class)` 本來就是設計成涵蓋所有情況的單一 catch-all，
    Python 這裡對應到三個型別但仍是同一份邏輯，不是三種不同處理方式）。
    純粹檢查 `file_path`，不需要 LLM 介入，跟上面 router 的機械判斷同一種
    精神。
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
