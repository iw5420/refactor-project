"""跨 Agent 共用的 Java → Python 型別對應與命名慣例轉換，見 00 六章
「Java 型別／命名對應（共用工具）」。

`map_java_type()`／`camel_to_snake()` 原本只有 ③ 架構設計 Agent
（`design_agent/type_mapping.py`）需要，用於方法簽名的型別/命名轉換。
④ 骨架實作 Agent（`scaffold_agent/`，見 08a_scaffold_agent_architecture.md
四章）需要對 JPA entity 欄位做同一件事——把 Java 欄位型別轉成 Python
型別、把 camelCase 欄位名轉成 SQLAlchemy 慣用的 snake_case 欄位名——是
同一個機械轉換，不是恰好想法一致，因此比照 `common/openapi_ref_resolver.py`
先例（見 05a 十三章「已解決事項」）搬到這裡，`design_agent/type_mapping.py`
改為從這裡 import，不再自己維護一份。

`type_str()` 原本是 `design_agent/signature_scan.py` 的私有函式
`_type_str()`，同一種情況：③把 javalang 型別節點還原成含泛型的 Java
型別字面字串，供 `map_java_type()` 解析；④（`scaffold_agent`）對 JPA
entity 欄位需要同一個還原動作，只是額外多了陣列型別（`byte[]` 這類，
原函式的 `.name`／`.arguments` 組合處理不到）這一種原本沒遇過的形狀
——這是同一個還原邏輯的擴充，不是另一個獨立函式，因此連同陣列處理
一起搬到這裡，`design_agent/signature_scan.py` 改為 import，不再自己
維護一份。

搬遷時保持函式行為逐字不變（`type_str()` 新增的 `.dimensions` 陣列
處理除外，這是純增補），只是換了檔案位置——`design_agent/type_mapping.py`
其餘 API 邊界／openapi_spec 相關函式（`openapi_type_to_python()`、
`resolve_api_boundary_signature()` 等）只有③需要，不搬。
"""
from __future__ import annotations

import re

_CAMEL_RE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_RE_2 = re.compile(r"([a-z0-9])([A-Z])")


def camel_to_snake(name: str) -> str:
    """Java 慣例的 camelCase 轉 Python 慣例的 snake_case（如 `userId`
    → `user_id`、`getHTTPStatus` → `get_http_status`）。連續大寫（縮寫，
    如 `HTTP`）視為單一詞界，不逐字元插入底線。
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
    # java.time／java.util 常見日期時間型別（新增，見
    # 08a_scaffold_agent_architecture.md 七章「Python 型別 →
    # SQLAlchemy Column 型別」——④渲染 JPA entity 欄位需要，原表沒有
    # 涵蓋，屬於純增補，不改動任何既有 key 的對應行為）。`Date`／
    # `Instant` 都沒有時區以外的獨立語意保留必要，一律對到
    # `datetime`，跟 `BigDecimal` 一樣是「刻意選擇損失一點精度資訊，
    # 換取單一穩定對應」的決策，不逐一区分 java.util.Date 與
    # java.time.Instant 的細微差異。
    "LocalDate": "date",
    "LocalDateTime": "datetime",
    "LocalTime": "time",
    "Date": "datetime",
    "Instant": "datetime",
    # java.util.UUID → uuid.UUID（見 08a_scaffold_agent_architecture.md
    # 七章）：企業級專案常用 UUID 當主鍵或對外暴露的唯一識別碼，原表
    # 遺漏會讓這類欄位落入 08a 七章「其他」分支被整個跳過——UUID 若剛好
    # 是主鍵欄位，被跳過的後果比一般欄位嚴重得多（整個 entity 沒有可用
    # 的主鍵）。同樣是純增補，不影響既有 key 的對應行為。
    "UUID": "UUID",
    # byte[]／Byte[]（二進位資料，如檔案內容、簽章）→ bytes（見 08a 七章）：
    # 這是這張表唯一的陣列語法特例——`_GENERIC_RE` 只認得 `Foo<Bar>` 這種
    # 角括號泛型，不認得 `Foo[]` 陣列語法，因此直接用完整字面字串當 key
    # 精確比對，不需要另外寫陣列語法的解析規則。呼叫端（`scaffold_agent`）
    # 對 javalang 的 ArrayType 節點重建型別字串時，只有「單一維度、元素
    # 型別是 byte/Byte」這個組合會產生剛好對得上這裡 key 的字面字串，
    # 其餘陣列形狀（多維、非 byte 元素型別）呼叫端不會嘗試對到這裡，
    # 見 08a 四章。
    "byte[]": "bytes",
    "Byte[]": "bytes",
}
# 單一型別參數容器：解開一層取內層型別。Map 需要兩個型別參數，另外處理。
_UNWRAP_SINGLE_PARAM = {"List": "list[{0}]", "Set": "list[{0}]", "Collection": "list[{0}]", "Optional": "{0} | None"}
_GENERIC_RE = re.compile(r"^(\w+)<(.+)>$")

# java.util.function 常見 functional interface，有唯一、明確的 Python
# 對應（Callable 的各種形式），屬於機械規則能決定的範疇，不需要交給
# LLM 判斷（00 二章）。value 是 `(預期型別引數個數, 組出 Callable 字串的
# 函式)`；型別引數個數對不上時視為理論上不該發生的情況（Java 編譯器本來
# 就會擋掉數量錯誤的泛型引數），不硬套模板，退回下面的未知泛型 fallback。
_FUNCTIONAL_INTERFACE_TEMPLATES = {
    "Supplier": (1, lambda ts: f"Callable[[], {ts[0]}]"),
    "Consumer": (1, lambda ts: f"Callable[[{ts[0]}], None]"),
    "BiConsumer": (2, lambda ts: f"Callable[[{ts[0]}, {ts[1]}], None]"),
    "Function": (2, lambda ts: f"Callable[[{ts[0]}], {ts[1]}]"),
    "BiFunction": (3, lambda ts: f"Callable[[{ts[0]}, {ts[1]}], {ts[2]}]"),
    "Predicate": (1, lambda ts: f"Callable[[{ts[0]}], bool]"),
    "BiPredicate": (2, lambda ts: f"Callable[[{ts[0]}, {ts[1]}], bool]"),
}


def map_java_type(java_type: str, known_classes: frozenset[str] = frozenset()) -> str:
    """05a 五章基礎型別對應表的程式化版本，遞迴處理泛型容器。
    `known_classes`：專案內自訂 class 名稱集合，命中時原樣沿用（假設
    同名 Python 類別存在，見 05a 五章表格最後一列）。

    **`BigDecimal` → `Decimal` 只在這個函式的路徑上生效**，③ 的 API 邊界
    方法改走 `design_agent.type_mapping.resolve_api_boundary_signature()`，
    覆蓋不到（理由見 05a 五章「型別對應」）。

    **未知泛型包裝類別的處理**（回應 07a 十四章「建議修正 05a 型別對應
    表（根因）」）：不在 `_UNWRAP_SINGLE_PARAM`／`_FUNCTIONAL_INTERFACE_
    TEMPLATES` 裡的泛型外層類別（專案自訂泛型如 `ResponseResult<T>`，
    或其他框架型別如 `Specification<T>`）——遞迴正規化內層型別參數，
    外層符號轉換 `Foo<Bar>` → `Foo[Bar]`，不猜測外層類別本身的語意（是
    否該用 `Generic[T]`、該不該整個從簽名拿掉，這些判斷不是型別字串
    層級能決定的事，留給更上游的職責範圍，見 05a 十三章「型別對應根因
    修正：為什麼不做 LLM 判斷」）。這保證回傳的字串永遠是合法 Python
    泛型 subscript 語法，07a 四章「型別字串正規化」的符號轉換防線因此
    對這個函式的輸出而言恆為 no-op（純防禦，不再是實際承接轉換工作的
    那一層）。
    """
    java_type = java_type.strip()
    if java_type in _SIMPLE_JAVA_TYPES:
        return _SIMPLE_JAVA_TYPES[java_type]

    match = _GENERIC_RE.match(java_type)
    if match:
        outer, inner = match.group(1), match.group(2)
        if outer == "Map":
            parts = _split_top_level_commas(inner)
            if len(parts) != 2:
                raise ValueError(f"無法解析 Map 泛型參數（預期 2 個型別參數，實際 {len(parts)} 個）: {inner}")
            key_type, value_type = parts
            return f"dict[{map_java_type(key_type, known_classes)}, {map_java_type(value_type, known_classes)}]"
        if outer in _UNWRAP_SINGLE_PARAM:
            return _UNWRAP_SINGLE_PARAM[outer].format(map_java_type(inner, known_classes))
        if outer in _FUNCTIONAL_INTERFACE_TEMPLATES:
            arity, render = _FUNCTIONAL_INTERFACE_TEMPLATES[outer]
            type_args = _split_top_level_commas(inner)
            if len(type_args) == arity:
                return render([map_java_type(t, known_classes) for t in type_args])
        # 未知的泛型包裝類別：遞迴正規化內層型別參數＋符號轉換
        # <...> -> [...]，不猜測外層類別本身的語意（見 docstring）。
        mapped_args = [map_java_type(t, known_classes) for t in _split_top_level_commas(inner)]
        return f"{outer}[{', '.join(mapped_args)}]"

    return java_type  # 命中 known_classes 或無法辨識的純量型別，原樣沿用（見 docstring）


def _split_top_level_commas(inner: str) -> list[str]:
    """依最外層逗號切分泛型型別引數列（不在巢狀 `<...>` 內的逗號才算數，
    如 `Map<String, List<Order>>` 的 `inner` 是 `"String, List<Order>"`，
    只切最外層那個逗號）。沒有頂層逗號時回傳單一元素清單（單一型別
    引數的泛型，如 `Optional<T>`）。供 Map（固定 2 個引數）、JDK
    functional interface（依各自 arity）、未知泛型 fallback（引數個數
    不定）共用同一份切分邏輯，不再各自維護。
    """
    depth = 0
    parts: list[str] = []
    start = 0
    for i, ch in enumerate(inner):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:i].strip())
            start = i + 1
    parts.append(inner[start:].strip())
    return parts


def type_str(java_type) -> str:
    """把 javalang 型別節點還原成含泛型參數的原始 Java 型別字面字串
    （如 `List<UserDto>`），供 `map_java_type()` 解析——跟
    `parse_agent/call_graph.py` 只取 `.name`（外層型別，供依賴解析用）
    的需求不同，這裡需要完整還原含泛型的字面字串。多層巢狀泛型（如
    `Map<String, List<Order>>`）遞迴處理。

    **陣列型別（`.dimensions`）**：javalang **不是**用獨立的節點類別表示
    陣列型別（`javalang.tree` 裡沒有 `ArrayType` 這個類別，實測直接
    `AttributeError`），而是在同一個 `BasicType`／`ReferenceType` 節點上
    多帶一個 `.dimensions` 屬性（`byte[]` 是 `BasicType(name="byte",
    dimensions=[None])`，`int[][]` 的 `dimensions` 長度是 2，型別宣告
    情境下每個維度的值固定是 `None`，不是陣列建立運算式那種帶長度的
    情境）。因此不需要另外遞迴解一層「元素型別」節點，直接對同一個
    節點的 `.name`／`.arguments` 組出基底型別字串，依 `len(dimensions)`
    接上對應層數的 `[]`。這個分支目前只被 `scaffold_agent`（見
    08a_scaffold_agent_architecture.md 四章「陣列型別的還原」）實際
    觸發需求（`byte[]`／`Byte[]` 對到 `bytes`，見本檔 `_SIMPLE_JAVA_
    TYPES`），但方法簽名理論上同樣可能出現陣列參數，因此在共用層一併
    處理，不是只做④用得到的那一半。
    """
    name = java_type.name
    arguments = getattr(java_type, "arguments", None)
    dimensions = getattr(java_type, "dimensions", None) or []

    if arguments:
        inner_types = []
        for arg in arguments:
            inner = getattr(arg, "type", None)
            inner_types.append(type_str(inner) if inner is not None else "?")
        base = f"{name}<{', '.join(inner_types)}>"
    else:
        base = name

    return base + "[]" * len(dimensions)
