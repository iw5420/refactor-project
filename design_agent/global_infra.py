# design_agent/global_infra.py
"""③ 架構設計 Agent：兩個獨立的「全域基礎設施」偵測與產出，都是三章
「全域基礎設施檔案」既有機制（`app/core/database.py`／`app/main.py`）
的延伸，比照既有先例（05a 十四章新增 `app/core/exception_handlers.py`
那次擴充）——**不是**逐 module 處理，是對整個 `module_list.java_files`
做一次獨立、輕量的全域掃描，因為這兩個模式本質上跨模組：

1. **`@Value("${key}")` 屬性注入欄位**（對應 `docs/09b_bug_trace.md`
   #45 根因）：機械偵測＋機械產生 `app/core/config.py`，不需要 LLM——
   跟 `layout.render_database_py()` 是同一種「純樣板」性質，Java 端的
   property key 已經是決定性事實，沒有語意判斷空間。

2. **只被 Enum 實作的 Java interface**（對應 #44 剩餘部分，`ErrorCode`
   本身）：機械偵測（interface 有哪些實作者、實作者是不是全部是 enum）
   ＋ **LLM 設計**（怎麼用 Python 慣用寫法表示，機械規則沒有答案——
   Java 用 interface + enum 這套組合本身就是 Java 慣用法，不能直接
   照搬，需要語意判斷才能決定功能對等的 Python 寫法，比照使用者對
   #44 的要求）。

兩者都刻意獨立於六章逐 module 的 `design_all_modules()` LLM 呼叫之外
——③／④ 是平行分支（05a 十一章），這裡的掃描不能依賴④已經算出的
`db_models`／`scan_index`（08a 六章），因此有自己完全獨立的輕量
javalang 掃描，不重用 `signature_scan.py`（那是逐 module 呼叫的，範圍
與時機都不對），也不重用 `scaffold_agent/entity_scan.py`（08a 明確
「獨立實作，不重用」的既有精神，見 08a 四章）。
"""
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

# 比照 design.py 六章既有的「單一呼叫失敗」重試節奏（05a 六章）。
_RETRY_WAIT_SECONDS = 300.0


def _iter_project_files(module_list: list[ModuleInfo]) -> list[str]:
    """`module_list.java_files` 逐 module 各自一份清單，這裡的兩個掃描
    都是全域（不分 module），先攤平去重，避免同一個檔案（理論上不會
    跨 module 重複，但防禦性去重不需要額外假設）被解析兩次。
    """
    seen: dict[str, None] = {}
    for m in module_list:
        for f in m["java_files"]:
            seen.setdefault(f, None)
    return list(seen)


# ── #45：@Value("${key}") 屬性注入欄位 → app/core/config.py（機械）──

# 跟 design_agent/signature_scan.py::_STEREOTYPES、parse_agent/
# call_graph.py::_stereotype_of() 各自維護同一份 5 個 Spring stereotype
# annotation 名稱的既有慣例一致（見 05a 四章「這不是重跑 04a 三章的呼叫
# 圖建構」同一個「獨立掃描、不共用」的既有精神）——這裡只需要判斷「有沒有
# stereotype」，實際層級對應交給 layout.layer_for_stereotype()。
_STEREOTYPES = frozenset({"RestController", "Controller", "Service", "Component", "Repository"})


@dataclass(frozen=True)
class ValueInjectedField:
    file_path: str  # Java 原始碼相對路徑，純記錄用（log／除錯）
    class_name: str
    field_name: str
    property_key: str  # 已去掉 "${" "}" 包裝的原始 key，如 "language.code"
    # 這個欄位所屬 class 對應到的 Python 檔案路徑（如
    # "app/routers/general_router.py"）——`render_config_py()` 產出的
    # `config_field_mappings` 要能被 [P]（plan_agent，只認得 Python
    # file_path，見 06a）比對到，key 必須是 Python 路徑，不能是這裡的
    # Java `file_path`，兩者是不同的路徑空間。`None` 代表這個 class 沒有
    # 機械可判定的層級（無 Spring stereotype，見 layout.
    # layer_for_stereotype() docstring）或不屬於任何已知 module——這種
    # 邊界情況下 config.py 依然照常產生（不影響 #45 的核心修復），只是
    # 沒有 task.context 提示可用，記警告即可，不中止。
    python_file_path: str | None


def scan_value_injected_fields(module_list: list[ModuleInfo], java_project_path: str) -> list[ValueInjectedField]:
    """逐 `module_list.java_files` 掃 `@Value("${key}")` 標註的欄位（05a
    四章既有的「javalang 輕量再掃描」目前只取方法簽名／class stereotype
    兩件事，這裡是機械擴充的第三件事，見本檔案 module docstring）。

    **Literal 規則**（沿用 08a 四章「Annotation 元素值萃取：只接受
    Literal」同一套精神，見 `scaffold_agent/entity_scan.py` 既有先例）：
    `@Value` 的 value 元素若不是 `javalang.tree.Literal`（如
    `@Value(SomeConstants.KEY)` 這種常數參照），視同這個欄位缺席、記
    一筆 warning，不猜——這種寫法在 Spring 專案裡極少見，且沒有字面值
    可用，猜測的風險遠高於漏收一個欄位。

    **`${...}` 包裝格式外的寫法一律跳過**：`@Value` 也能直接塞 SpEL
    表達式（如 `@Value("#{someBean.value}")`）或純字面常數（沒有
    `${...}` 包裝），這裡只處理「讀取 application.properties key」這種
    最常見、且能機械映射到環境變數的形式，其餘視同缺席、記警告，不
    嘗試解析 SpEL——那不是一個環境變數能表達的語意。

    **`python_file_path` 的計算**：`@Value` 欄位所屬 class 的 Spring
    stereotype 決定它落在 Python 哪一層（沿用 `layout.
    layer_for_stereotype()`／`file_path_for_layer()`，跟六章逐 module
    LLM 設計階段用的是同一套機械規則，不重新發明），`module` 則從
    `module_list.java_files` 反查這個 class 所在檔案屬於哪個 module。
    兩者缺一（無 stereotype，或這個檔案不屬於任何已知 module——理論上
    不該發生，`module_list.java_files` 應該涵蓋所有掃描到的檔案）都會
    讓 `python_file_path` 是 `None`，記一筆 warning，不中止——config.py
    本身仍然照常產生，只是這個欄位不會有 `config_field_mappings` 條目
    可用，見 `ValueInjectedField.python_file_path` docstring。
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
    `LANGUAGE_DISPLAY_NAME`：逐 `.` 分段各自套用既有 `camel_to_snake()`
    （跟 ③④ 共用同一份型別/命名轉換工具，見 05a 五章），再以 `_` 接續、
    轉大寫——跟 `app/core/database.py` 的 `DATABASE_URL` 同一種全大寫
    環境變數命名慣例。
    """
    return "_".join(camel_to_snake(seg) for seg in property_key.split(".")).upper()


def render_config_py(
    value_fields: list[ValueInjectedField],
) -> tuple[str | None, dict[str, dict[str, str]], list[dict[str, str]]]:
    """機械組出 `app/core/config.py` 的完整檔案內容，跟
    `layout.render_database_py()` 同一種「固定樣板，不經過 LLM」性質
    ——property key 到環境變數名稱、到常數宣告，每一步都是決定性
    字串轉換，沒有語意判斷空間。

    回傳 `(檔案內容或 None, config_field_mappings, config_env_vars)`：
    `value_fields` 為空時回傳 `(None, {}, [])`，呼叫端不渲染這個檔案
    （比照 08a 十二章「內容真的存在才產生 key」的既有原則，不是每個專案
    都會用到 `@Value`）。

    `config_field_mappings`：`python_file_path -> {java_field_name:
    python_reference}`，供 [P]（06a）折進 `task.context`，見
    `graph/state.py PythonStructure.config_field_mappings`——key 必須是
    Python 檔案路徑（`TaskSpec.target_files[0]` 比對用的路徑空間），不是
    Java 原始碼路徑，見 `ValueInjectedField.python_file_path` docstring。
    `python_file_path` 是 `None` 的欄位（無法機械判定對應 Python 檔案）
    仍然會產生環境變數常數（config.py 本身不受影響），只是不會出現在
    `config_field_mappings` 裡，沒有 task.context 提示可用。同一個
    property key 若被多個欄位/多個檔案引用（如同一個 key 被兩個
    Controller 各自宣告一次 `@Value` 欄位），只產生一個常數（用 dict 對
    property_key 去重），但 `config_field_mappings` 仍然逐一記錄每個
    檔案/欄位各自對應到哪個常數。

    `config_env_vars`：對應 `docs/09b_bug_trace.md` #46「@Value 屬性注入
    機制只設計了怎麼命名/怎麼讀，沒有設計值從哪裡來」——`constants` 這份
    `property_key -> constant_name` 對照本來就是這個函式已經算好、拿去
    產生 `content` 的中間結果，這裡另外攤平成
    `[{"property_key": ..., "constant_name": ...}, ...]`（依首次出現
    順序，已經對 property_key 去重，跟 `content` 裡實際宣告的常數一一
    對應）一併回傳，供 `python_service`（⑤，見 `09a_implement_agent_
    architecture.md` 對應章節）啟動容器前讀取 Java 端 `application-
    {profile}.properties` 實際值、解析出 `{constant_name: value}` 當額外
    `-e` 環境變數注入——這不是新開一條資料管道，只是把這個函式本來就
    算過、之前只用來組 `content` 字串就丟棄的中間結果也回傳出去。
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


# ── #44：只被 Enum 實作的 interface → LLM 設計 Python 對等寫法 ──


@dataclass(frozen=True)
class _EnumImplementorInfo:
    class_name: str
    file_path: str
    fields: list[tuple[str, str]]  # (name, java_type)，來自 EnumBody.declarations 的 FieldDeclaration


@dataclass(frozen=True)
class EnumBackedInterface:
    """一個「只被 enum 實作」的 interface（見 `scan_enum_backed_
    interfaces()` 判定條件），交給 LLM 設計 Python 對等寫法用的完整
    輸入。"""

    interface_name: str
    file_path: str
    method_signatures: list[str]  # 如 "int getCode()"，純文字，LLM 判讀用，不需要結構化
    implementors: list[_EnumImplementorInfo] = field(default_factory=list)


def scan_enum_backed_interfaces(module_list: list[ModuleInfo], java_project_path: str) -> list[EnumBackedInterface]:
    """全域掃一次 `module_list.java_files`，找出「至少被一個 enum
    實作、且零個 class 實作」的 interface——這個條件本身是機械可判定
    的（見本檔案 module docstring 第 2 點），不需要 LLM 介入；需要 LLM
    判斷的只有「找到之後該怎麼翻譯成 Python」，見
    `design_enum_backed_interfaces()`。

    **只掃頂層宣告**（`tree.types`，不用會遞迴進 inner class 的
    `tree.filter()`），理由同 `parse_agent/call_graph.py::
    _extract_classes()` docstring——避免同一份原始碼被重複列入。
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
    互相隔離，比照 `design.py` 逐 module 呼叫的既有精神），回傳
    `[(file_path, python_source), ...]`。

    **失敗處理**：比照 05a 六章既有的「單一呼叫失敗」節奏——失敗的
    interface 列入待重試清單，全部呼叫完後等待 `_RETRY_WAIT_SECONDS`
    秒統一重試一次；仍失敗中止整條 design run（`DesignAgentModuleError`
    ——這裡缺失會讓下游多處 `NameError`，風險等級同六章逐 module 呼叫，
    沿用同一套例外型別與中止邏輯，不新發明一套，見本檔案 module
    docstring）。
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
            f"（05a 對應章節的保守預設，見本函式 docstring）: {[i.interface_name for i in still_failed]}"
        )
    return results
