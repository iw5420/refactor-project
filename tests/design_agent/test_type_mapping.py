"""design_agent/type_mapping.py 的 map_java_type() 假資料單元測試。

對應 05a 五章「基礎型別對應表」＋十三章「型別對應根因修正」（回應
07a 十四章「建議修正 05a 型別對應表（根因）」）：Layer 1（未知泛型的
符號轉換 fallback）與 Layer 2（JDK functional interface 對照表）。
"""
import ast

import pytest

from design_agent.type_mapping import (
    collect_named_schemas,
    is_response_entity_return_type,
    map_java_type,
    resolve_api_boundary_signature,
)
from design_agent.types import JavaMethodSignature, JavaParam


def _assert_valid_annotation(type_str: str) -> None:
    """驗證 map_java_type() 的輸出接在函式簽名裡是合法 Python 語法
    ——這是這次修正真正要保證的事，不是只比對字串。
    """
    ast.parse(f"def f(x: {type_str}) -> None:\n    pass\n")


class TestSimpleTypes:
    """既有行為，確認這次重構沒有動到基礎型別對應（回歸測試）。"""

    def test_primitive_and_wrapper_types(self):
        assert map_java_type("int") == "int"
        assert map_java_type("Integer") == "int"
        assert map_java_type("long") == "int"
        assert map_java_type("String") == "str"
        assert map_java_type("boolean") == "bool"
        assert map_java_type("double") == "float"
        assert map_java_type("void") == "None"

    def test_big_decimal_maps_to_decimal(self):
        assert map_java_type("BigDecimal") == "Decimal"

    def test_unrecognized_scalar_kept_as_is(self):
        assert map_java_type("Object") == "Object"

    def test_project_class_kept_as_is(self):
        assert map_java_type("UserDto", known_classes=frozenset({"UserDto"})) == "UserDto"


class TestJdkCollectionGenerics:
    """既有行為：List/Set/Collection/Optional/Map，確認重構後語法仍不變。"""

    def test_list_set_collection(self):
        assert map_java_type("List<String>") == "list[str]"
        assert map_java_type("Set<Integer>") == "list[int]"
        assert map_java_type("Collection<UserDto>") == "list[UserDto]"

    def test_optional(self):
        assert map_java_type("Optional<String>") == "str | None"

    def test_map(self):
        assert map_java_type("Map<String, Integer>") == "dict[str, int]"

    def test_map_wrong_arity_raises(self):
        # 依 05a 決策：機械規則解析不出來的輸入直接拋例外中止（04a／05a
        # 一貫的「輸入端問題，不嘗試自動修正」原則），不是這次修正的範圍。
        with pytest.raises(ValueError):
            map_java_type("Map<String>")

    def test_nested_generic(self):
        assert map_java_type("Map<String, List<Order>>") == "dict[str, list[Order]]"

    def test_all_produce_valid_annotations(self):
        for java_type in ["List<String>", "Optional<UserDto>", "Map<String, List<Order>>"]:
            _assert_valid_annotation(map_java_type(java_type))


class TestFunctionalInterfaces:
    """Layer 2：java.util.function 常見型別，有唯一正確答案，機械表格
    直接命中，不經過 Layer 1 的符號轉換 fallback。
    """

    def test_supplier(self):
        assert map_java_type("Supplier<String>") == "Callable[[], str]"

    def test_consumer(self):
        assert map_java_type("Consumer<String>") == "Callable[[str], None]"

    def test_bi_consumer(self):
        assert map_java_type("BiConsumer<String, Integer>") == "Callable[[str, int], None]"

    def test_function(self):
        assert map_java_type("Function<Long, String>") == "Callable[[int], str]"

    def test_bi_function(self):
        assert map_java_type("BiFunction<String, Integer, Boolean>") == "Callable[[str, int], bool]"

    def test_predicate(self):
        assert map_java_type("Predicate<UserDto>") == "Callable[[UserDto], bool]"

    def test_bi_predicate(self):
        assert map_java_type("BiPredicate<String, String>") == "Callable[[str, str], bool]"

    def test_inner_type_recursively_mapped(self):
        # inner 型別本身若是巢狀泛型，一併正規化，不是原樣塞進 Callable。
        assert map_java_type("Function<T, List<String>>") == "Callable[[T], list[str]]"

    def test_wrong_arity_falls_back_to_unknown_generic(self):
        # Function 理論上一定是 2 個型別引數；若輸入不符（理論上不該
        # 發生），退回 Layer 1 fallback，而不是 IndexError 崩潰。
        assert map_java_type("Function<String>") == "Function[str]"

    def test_all_produce_valid_annotations(self):
        for java_type in [
            "Supplier<String>",
            "Function<Long, String>",
            "BiFunction<String, Integer, Boolean>",
            "Function<T, List<String>>",
        ]:
            _assert_valid_annotation(map_java_type(java_type))


class TestUnknownGenericFallback:
    """Layer 1：不在任何已知表裡的泛型包裝類別（專案自訂或其他框架
    型別）——這是 07a 四章「型別字串正規化」發現的真實案例（25/72 介面
    受影響），這裡驗證根因已在 map_java_type() 解決。
    """

    def test_project_own_generic_wrapper(self):
        assert map_java_type("ResponseResult<T>") == "ResponseResult[T]"

    def test_nested_unknown_generic(self):
        # 07a 四章原文案例：ResponseResult<Map<String, Object>>。
        # 內層 Map 遞迴正規化成 dict[...]，不是單純符號替換留下
        # Map[String, Object] 這種同樣不合法的殘留。
        assert map_java_type("ResponseResult<Map<String, Object>>") == "ResponseResult[dict[str, Object]]"

    def test_framework_internal_type(self):
        # 07a 四章原文案例：Specification<ExamEntity>（Spring Data JPA）。
        assert map_java_type("Specification<ExamEntity>") == "Specification[ExamEntity]"

    def test_all_produce_valid_annotations(self):
        for java_type in [
            "ResponseResult<T>",
            "ResponseResult<Map<String, Object>>",
            "Specification<ExamEntity>",
        ]:
            _assert_valid_annotation(map_java_type(java_type))


class TestResponseEntity:
    """`ResponseEntity<T>`（可在方法內動態控制 HTTP status／header）需要
    特殊處理，不能落入 Layer 1 未知泛型 fallback（`ResponseEntity[?]`
    不是合法 Python 型別）——見 docs/09b_bug_trace.md #30。
    """

    def test_response_entity_maps_to_response(self):
        assert map_java_type("ResponseEntity<FileRs>") == "Response"

    def test_response_entity_wildcard_maps_to_response(self):
        # 真實案例：FileController 的 ResponseEntity<?>。
        assert map_java_type("ResponseEntity<?>") == "Response"

    def test_response_entity_nested_generic_still_maps_to_response(self):
        # 內層型別不管多複雜都丟棄，不遞迴正規化它（跟其餘未知泛型 fallback
        # 刻意不同——見 map_java_type() ResponseEntity 分支說明）。
        assert map_java_type("ResponseEntity<Map<String, Object>>") == "Response"

    def test_output_is_valid_annotation(self):
        _assert_valid_annotation(map_java_type("ResponseEntity<FileRs>"))


class TestIsResponseEntityReturnType:
    """`is_response_entity_return_type()`：判斷 Java 方法簽名**原始**
    回傳型別字面字串是不是 ResponseEntity（給 `resolve_api_boundary_
    signature()`／`design.py` 用來決定要不要覆寫 return_type、要不要
    跳過收集 response schema，見 09b_bug_trace.md #30）。
    """

    def test_true_for_response_entity(self):
        assert is_response_entity_return_type("ResponseEntity<FileRs>") is True

    def test_true_for_response_entity_with_whitespace(self):
        assert is_response_entity_return_type("  ResponseEntity<FileRs>  ") is True

    def test_false_for_other_types(self):
        assert is_response_entity_return_type("FileRs") is False
        assert is_response_entity_return_type("List<FileRs>") is False

    def test_false_for_none(self):
        assert is_response_entity_return_type(None) is False


class TestResolveApiBoundarySignatureResponseEntity:
    """`resolve_api_boundary_signature()` 對 `ResponseEntity<T>` 方法的
    覆寫：不查 openapi response schema，直接回傳 "Response"（見
    docs/09b_bug_trace.md #30）。真實案例：FileController 的
    `GET /voice`／`GET /image` 用 ResponseEntity 顯式回 404／500，
    openapi_spec 只記錄了其中一個代表性 status 的 body 形狀。
    """

    def _method(self, return_type: str) -> JavaMethodSignature:
        return JavaMethodSignature(
            class_name="FileController", method_name="voice",
            params=[JavaParam(name="location", java_type="String")],
            return_type=return_type,
        )

    def test_response_entity_return_type_overridden_ignoring_openapi_response(self):
        # operation 裡故意放一個看起來正常的 200 response schema，驗證
        # ResponseEntity 覆寫真的完全不查它，不是只是「剛好結果一樣」。
        operation = {
            "parameters": [{"name": "location", "schema": {"type": "string"}}],
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/FileRs"}}}}},
        }
        params, return_type = resolve_api_boundary_signature(
            self._method("ResponseEntity<FileRs>"), operation, {}
        )
        assert return_type == "Response"
        assert params == [{"name": "location", "type": "str"}]

    def test_non_response_entity_still_uses_openapi_response(self):
        # 迴歸測試：非 ResponseEntity 方法的既有行為不受影響。
        operation = {
            "parameters": [{"name": "location", "schema": {"type": "string"}}],
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/FileRs"}}}}},
        }
        params, return_type = resolve_api_boundary_signature(
            self._method("FileRs"), operation, {}
        )
        assert return_type == "FileRs"


class TestCollectNamedSchemasRecursion:
    """`collect_named_schemas()` 遞迴收集巢狀具名 schema，對應
    docs/09b_bug_trace.md #28：真實案例 `registration.py` 引用了只在
    `school.py` 渲染過的 `GetAllGradeRs`（頂層 response schema 的欄位
    指向另一個具名 schema，過去只有頂層才會被收集）。
    """

    def _spec(self, schemas: dict) -> dict:
        return {"components": {"schemas": schemas}}

    def test_nested_ref_in_response_property_is_collected(self):
        operation = {
            "responses": {
                "200": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/ResponseResultGetAllGradeRs"}}
                    }
                }
            }
        }
        spec = self._spec(
            {
                "ResponseResultGetAllGradeRs": {
                    "properties": {
                        "code": {"type": "integer"},
                        "data": {"$ref": "#/components/schemas/GetAllGradeRs"},
                    },
                },
                "GetAllGradeRs": {"properties": {"grades": {"type": "array", "items": {"type": "string"}}}},
            }
        )
        results = collect_named_schemas(operation, spec)
        assert [name for name, _ in results] == ["ResponseResultGetAllGradeRs", "GetAllGradeRs"]

    def test_array_wrapped_nested_ref_is_collected(self):
        operation = {
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/OrderListRs"}}}}}
        }
        spec = self._spec(
            {
                "OrderListRs": {
                    "properties": {"items": {"type": "array", "items": {"$ref": "#/components/schemas/OrderDto"}}},
                },
                "OrderDto": {"properties": {"id": {"type": "integer"}}},
            }
        )
        results = collect_named_schemas(operation, spec)
        assert {name for name, _ in results} == {"OrderListRs", "OrderDto"}

    def test_mutually_referencing_schemas_do_not_infinite_loop(self):
        operation = {
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/UserRs"}}}}}
        }
        spec = self._spec(
            {
                "UserRs": {"properties": {"orders": {"type": "array", "items": {"$ref": "#/components/schemas/OrderRs"}}}},
                "OrderRs": {"properties": {"user": {"$ref": "#/components/schemas/UserRs"}}},
            }
        )
        results = collect_named_schemas(operation, spec)
        assert {name for name, _ in results} == {"UserRs", "OrderRs"}
        assert len(results) == 2  # 互相引用不會被重複收集

    def test_no_duplicate_when_multiple_fields_reference_same_schema(self):
        operation = {
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/PairRs"}}}}}
        }
        spec = self._spec(
            {
                "PairRs": {
                    "properties": {
                        "first": {"$ref": "#/components/schemas/ItemDto"},
                        "second": {"$ref": "#/components/schemas/ItemDto"},
                    },
                },
                "ItemDto": {"properties": {"name": {"type": "string"}}},
            }
        )
        results = collect_named_schemas(operation, spec)
        assert [name for name, _ in results].count("ItemDto") == 1

    def test_request_body_and_response_both_recursed(self):
        operation = {
            "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CreateReq"}}}},
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CreateRs"}}}}},
        }
        spec = self._spec(
            {
                "CreateReq": {"properties": {"detail": {"$ref": "#/components/schemas/DetailDto"}}},
                "DetailDto": {"properties": {"label": {"type": "string"}}},
                "CreateRs": {"properties": {"result": {"$ref": "#/components/schemas/ResultDto"}}},
                "ResultDto": {"properties": {"ok": {"type": "boolean"}}},
            }
        )
        results = collect_named_schemas(operation, spec)
        assert {name for name, _ in results} == {"CreateReq", "DetailDto", "CreateRs", "ResultDto"}

    def test_no_nested_refs_behaves_like_before(self):
        operation = {
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Simple"}}}}}
        }
        spec = self._spec({"Simple": {"properties": {"name": {"type": "string"}}}})
        results = collect_named_schemas(operation, spec)
        assert [name for name, _ in results] == ["Simple"]


class TestResolveApiBoundarySignatureRequestBody:
    """回歸測試：真實端對端測試發現 `_classify_params()` 對有
    requestBody 的方法（`_simple_type_name()` 依型別比對 body 參數）
    會直接 `NameError: name '_GENERIC_RE' is not defined`——
    `design_agent/type_mapping.py` 沒有 import `_GENERIC_RE`
    （只在 `common/java_type_mapping.py` 定義），這是既有缺陷、不是
    這輪四項架構修正的一部分，但擋住了真實環境驗證，順手修正並補測試。
    """

    def test_post_endpoint_with_request_body_does_not_crash(self):
        method = JavaMethodSignature(
            class_name="OrderController", method_name="create",
            params=[JavaParam(name="req", java_type="Optional<OrderCreateRequest>")],
            return_type="OrderRs",
        )
        operation = {
            "requestBody": {
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OrderCreateRequest"}}}
            },
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/OrderRs"}}}}},
        }
        params, return_type = resolve_api_boundary_signature(method, operation, {})
        assert params == [{"name": "req", "type": "OrderCreateRequest"}]
        assert return_type == "OrderRs"
