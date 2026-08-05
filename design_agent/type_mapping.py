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
