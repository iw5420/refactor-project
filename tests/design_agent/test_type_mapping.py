"""design_agent/type_mapping.py 的 map_java_type() 假資料單元測試。

對應 05a 五章「基礎型別對應表」＋十三章「型別對應根因修正」（回應
07a 十四章「建議修正 05a 型別對應表（根因）」）：Layer 1（未知泛型的
符號轉換 fallback）與 Layer 2（JDK functional interface 對照表）。
"""
import ast

import pytest

from design_agent.type_mapping import (
    _classify_params,
    collect_named_schemas,
    extract_schema_fields,
    is_response_entity_return_type,
    map_java_type,
    openapi_type_to_python,
    resolve_api_boundary_signature,
)
from design_agent.types import JavaMethodSignature, JavaParam


def _assert_valid_annotation(type_str: str) -> None:
    """驗證 map_java_type() 的輸出接在函式簽名裡是合法 Python 語法
    ——這是這次修正真正要保證的事，不是只比對字串。
    """
    ast.parse(f"def f(x: {type_str}) -> None:\n    pass\n")


class TestOpenapiTypeToPythonObjectSchema:
    """對應 docs/refactor_bug_trace.md #40：真實案例 `GetAllExamRs.allExams`
    對應 Java `Map<String, List<ExamkindEntity>>`，springdoc 產生的
    schema 是 `additionalProperties` 形狀，修復前這裡直接落到最後一行
    退回字面字串 `"object"`——語法合法但完全不做型別檢查，讓⑤合理但
    錯誤地推論「不用把 entity 轉成 schema 物件」。"""

    def test_additional_properties_object_becomes_dict(self, caplog):
        schema = {
            "type": "object",
            "additionalProperties": {"type": "array", "items": {"$ref": "#/components/schemas/ExamkindEntity"}},
        }
        with caplog.at_level("INFO"):
            result = openapi_type_to_python(schema)

        assert result == "dict[str, list[ExamkindEntity]]"
        _assert_valid_annotation(result)
        assert any("Map" in r.message for r in caplog.records)

    def test_object_with_title_and_no_additional_properties_uses_title(self):
        schema = {"type": "object", "title": "InlineNamedThing"}
        assert openapi_type_to_python(schema) == "InlineNamedThing"

    def test_object_with_properties_and_additional_properties_is_not_treated_as_map(self):
        """有 `properties`（固定形狀物件）時，即使剛好也有
        `additionalProperties`，不該被判定成 Map——這種情況目前落到
        「看不懂就 raise」分支，不是這次要修的既有已知案例，但也不能
        被新的 Map 分支誤判。"""
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "additionalProperties": {"type": "string"},
            "title": "HasBoth",
        }
        assert openapi_type_to_python(schema) == "HasBoth"

    def test_unrecognized_object_schema_still_falls_back_to_literal_object(self):
        """既沒有 $ref／additionalProperties，也沒有 title——這個共用
        函式本身刻意保留寬容 fallback（給 `_classify_params()` 這條
        參數分類路徑用，見 `TestMultipartFileBodyParam::
        test_non_multipart_body_param_unaffected`），不在這裡 raise，
        「遇到裸 object 就失敗」的更嚴格規則改放在
        `extract_schema_fields()` 自己身上，見
        `TestExtractSchemaFieldsRejectsBareObject`。"""
        schema = {"type": "object"}
        assert openapi_type_to_python(schema) == "object"


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

    def test_all_produce_valid_annotations(self):
        for java_type in [
            "ResponseResult<T>",
            "ResponseResult<Map<String, Object>>",
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


class TestSpecification:
    """`Specification<T>`（Spring Data JPA 動態查詢 pattern）需要特殊
    處理，不能落入 Layer 1 未知泛型 fallback（`Specification[ExamEntity]`
    在 Python 端從未被定義，任何用到它的方法一被呼叫就會 `NameError`）
    ——見 docs/refactor_bug_trace.md #14 真實案例。跟 `ResponseEntity`
    一樣丟棄內層型別參數，因為 T 只是「這個條件對哪個 entity 產生」，
    不是容器語意。
    """

    def test_specification_maps_to_column_element_or_none(self):
        assert map_java_type("Specification<ExamEntity>") == "ColumnElement | None"

    def test_specification_nested_generic_still_maps_to_column_element(self):
        assert map_java_type("Specification<Map<String, Object>>") == "ColumnElement | None"

    def test_output_is_valid_annotation(self):
        _assert_valid_annotation(map_java_type("Specification<ExamEntity>"))


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


class TestMultipartFileBodyParam:
    """docs/refactor_bug_trace.md #7：真實環境端對端重跑發現
    `file_router.py` 的 `image()`／`voice()` 簽名帶著裸字串型別 `object`
    ——springdoc 對 `MultipartFile` 參數幾乎不會產生具名 schema（多半是
    inline object、沒有 `$ref`／`title`），型別比對必然落空、退回「唯一
    剩餘參數」啟發式後，`openapi_type_to_python()` 對這種 inline schema
    沒有 `$ref`／`title` 可用，只能吐出裸字串 `"object"`，FastAPI 完全
    解不了 multipart 上傳。改成偵測 Java 型別本身是不是 `MultipartFile`
    ——這是 Spring MVC 檔案上傳的框架型別，跟方法名稱／路徑無關，直接
    對應 FastAPI 的 `UploadFile`，不再嘗試從 openapi schema 猜型別。
    """

    def test_multipart_file_param_maps_to_upload_file(self):
        method = JavaMethodSignature(
            class_name="FileController", method_name="image",
            params=[
                JavaParam(name="kind", java_type="String"),
                JavaParam(name="file", java_type="MultipartFile"),
            ],
            return_type="ResponseResult<Map<String,Object>>",
        )
        operation = {
            "parameters": [{"name": "kind", "schema": {"type": "string"}}],
            "requestBody": {
                "content": {
                    "multipart/form-data": {
                        "schema": {"type": "object", "properties": {"file": {"type": "string", "format": "binary"}}}
                    }
                }
            },
            "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
        }
        params, _ = resolve_api_boundary_signature(method, operation, {})
        assert {"name": "kind", "type": "str"} in params
        assert {"name": "file", "type": "UploadFile"} in params

    def test_non_multipart_body_param_unaffected(self):
        # 確認這個特判只對 Java 型別剛好是 MultipartFile 的參數生效，不會
        # 誤傷既有的「唯一剩餘參數」啟發式對其他型別的既有行為。
        method = JavaMethodSignature(
            class_name="OrderController", method_name="create",
            params=[JavaParam(name="req", java_type="OrderCreateRequest")],
            return_type="OrderRs",
        )
        operation = {
            "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
            "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}},
        }
        params, _ = resolve_api_boundary_signature(method, operation, {})
        assert params == [{"name": "req", "type": "object"}]


class TestMultipartFileWithSiblingFormFields:
    """docs/refactor_bug_trace.md #20：真實案例 `FileController.voice()`——
    Spring 用 4 個獨立的 `@RequestParam` 字串參數 + 1 個 `MultipartFile`
    宣告同一個 multipart 端點（不是單一 DTO body）。openapi 不會把這些
    欄位放進 `parameters`（它們是 multipart body 欄位，不是 path/query），
    `_classify_params()` 既有的 requestBody 邏輯只在「剩餘參數剛好只有
    一個」時才嘗試判定，5 個剩餘參數完全不會觸發，全部原封不動流向
    `find_uncovered_framework_params()` 交六章 LLM 逐一亂猜——真實生成
    結果證實除了 `file` 猜對成 `UploadFile`，其餘 4 個都猜成裸 `str`，
    FastAPI 因此把它們當成 query parameter，真實 multipart 請求被判定
    缺必填參數。逐行比對 Java 原始碼確認 `voice()` 本體邏輯翻譯完全正確，
    這是唯一擋住它成功執行的問題。
    """

    def _voice_like_method(self, extra_param: JavaParam | None = None) -> JavaMethodSignature:
        params = [
            JavaParam(name="kind", java_type="String"),
            JavaParam(name="randomId", java_type="String"),
            JavaParam(name="partNumber", java_type="String"),
            JavaParam(name="questionNumber", java_type="String"),
        ]
        if extra_param is not None:
            params.append(extra_param)
        params.append(JavaParam(name="file", java_type="MultipartFile"))
        return JavaMethodSignature(
            class_name="FileController", method_name="voice", params=params, return_type="ResponseResult<String>",
        )

    def _voice_like_operation(self) -> dict:
        # 真實案例：openapi 完全沒有把這 4 個欄位放進 parameters，
        # multipart/form-data 的 schema 也只列出 file（springdoc 對這種
        # 端點的既有限制，見 TestMultipartFileBodyParam docstring）。
        return {
            "requestBody": {
                "content": {
                    "multipart/form-data": {
                        "schema": {"type": "object", "properties": {"file": {"type": "string", "format": "binary"}}}
                    }
                }
            },
            "responses": {"200": {"content": {"application/json": {"schema": {"type": "string"}}}}},
        }

    def test_sibling_string_params_get_form_wrapper_and_upload_file_stays_bare(self):
        method = self._voice_like_method()
        params, _ = resolve_api_boundary_signature(method, self._voice_like_operation(), {})
        assert {"name": "kind", "type": "str = Form(...)"} in params
        assert {"name": "randomId", "type": "str = Form(...)"} in params
        assert {"name": "partNumber", "type": "str = Form(...)"} in params
        assert {"name": "questionNumber", "type": "str = Form(...)"} in params
        assert {"name": "file", "type": "UploadFile"} in params
        assert len(params) == 5

    def test_non_primitive_sibling_param_not_wrapped_in_form(self):
        """防呆：`HttpServletRequest` 這類框架注入物件型別若剛好也跟
        `MultipartFile` 出現在同一個簽名裡，不應該被誤包成 `Form(...)`
        ——那不是 multipart 表單欄位，維持交給六章 LLM 的既有框架注入
        物件判斷路徑（05a 五章），不在這裡處理。"""
        method = self._voice_like_method(extra_param=JavaParam(name="request", java_type="HttpServletRequest"))
        covered, remaining = _classify_params(method, self._voice_like_operation(), {})
        covered_names = {p.name for p, _ in covered}
        assert "request" not in covered_names
        assert any(p.name == "request" for p in remaining)
        assert dict(next(spec for p, spec in covered if p.name == "file")) == {"name": "file", "type": "UploadFile"}


def _assert_valid_field_declaration(name: str, declaration: str) -> None:
    """驗證 extract_schema_fields() 的輸出接在 Pydantic BaseModel 的
    class body 裡是合法 Python 語法。"""
    ast.parse(f"class _M:\n    {name}: {declaration}\n")


class TestExtractSchemaFields:
    """對應 docs/09b_bug_trace.md：非必填欄位若沒有驗證限制（不會走
    Field(...) 分支），舊版只有 `python_type = f'{python_type} | None'`
    純型別注記、沒有補 `= None` 預設值——Pydantic v2 對這種寫法一樣會
    把欄位當成必填，Java 端本來可以省略的欄位一旦真的被省略就會
    RequestValidationError。真實案例：CreaterandomRq.year 的 golden
    請求本體完全沒有帶 year，但生成出來的欄位是 `year: str | None`
    （沒有 `= None`），第三輪真實 pipeline 因此對 create_random 端點
    500。"""

    def test_optional_field_without_constraints_gets_none_default(self):
        raw_schema = {
            "properties": {"year": {"type": "string"}},
            "required": [],
        }
        fields = extract_schema_fields(raw_schema)
        assert fields == [("year", "str | None = None")]
        _assert_valid_field_declaration(*fields[0])

    def test_required_field_without_constraints_has_no_default(self):
        raw_schema = {
            "properties": {"card": {"type": "string"}},
            "required": ["card"],
        }
        fields = extract_schema_fields(raw_schema)
        assert fields == [("card", "str")]
        _assert_valid_field_declaration(*fields[0])

    def test_optional_field_with_constraints_still_uses_field_default_none(self):
        """既有行為（走 Field(...) 分支的既有邏輯）不能被這次修正動到。"""
        raw_schema = {
            "properties": {"name": {"type": "string", "maxLength": 50}},
            "required": [],
        }
        fields = extract_schema_fields(raw_schema)
        assert fields == [("name", "str | None = Field(default=None, max_length=50)")]
        _assert_valid_field_declaration(*fields[0])

    def test_required_field_with_constraints_uses_field_ellipsis(self):
        raw_schema = {
            "properties": {"name": {"type": "string", "maxLength": 50}},
            "required": ["name"],
        }
        fields = extract_schema_fields(raw_schema)
        assert fields == [("name", "str = Field(..., max_length=50)")]
        _assert_valid_field_declaration(*fields[0])

    def test_map_field_via_additional_properties_becomes_dict(self):
        """對應 docs/refactor_bug_trace.md #40 真實案例
        `GetAllExamRs.allExams`（Java `Map<String, List<ExamkindEntity>>`）
        ——修復前這裡會拿到裸 `"object"`（見下面的
        `TestExtractSchemaFieldsRejectsBareObject`），修復後要正確產生
        `dict[str, list[ExamkindEntity]]`。"""
        raw_schema = {
            "properties": {
                "allExams": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/ExamkindEntity"},
                    },
                },
            },
            "required": [],
        }
        fields = extract_schema_fields(raw_schema)
        assert fields == [("allExams", "dict[str, list[ExamkindEntity]] | None = None")]
        _assert_valid_field_declaration(*fields[0])


class TestExtractSchemaFieldsBareObjectHandling:
    """對應 docs/refactor_bug_trace.md #40／#41：`openapi_type_to_python()`
    對「猜不出來的 object」故意保留寬容 fallback（給 `_classify_params()`
    參數分類路徑用），但 `extract_schema_fields()` 是在產生真正會寫進
    Pydantic model 的欄位型別宣告——裸 `object` 完全不做型別檢查，語法
    合法但語意等於沒有型別，會讓⑤合理但錯誤地推論「這個欄位不用把
    entity 轉成 schema 物件」。**分兩種情況**（#41 真實重跑訂正）：
    schema 除了 `type` 真的沒有其他資訊時（真實案例 Java
    `ResponseResult<Void>` 的 `data` 欄位）代表本來就沒有更精確的型別
    可救，退回 `Any`，不 raise；schema 還帶著這個函式看不懂的其他鍵
    （代表可能真的漏接了一個能救回來的結構，跟 `GetAllExamRs.allExams`
    是同一種情況）才 raise。"""

    def test_bare_object_with_no_other_info_falls_back_to_any(self, caplog):
        """真實案例：Java `ResponseResult<Void>` 的 `data` 欄位，schema
        就只有 `{"type": "object"}`，除了 type 沒有任何其他資訊——這是
        泛型抹除後本來就沒有資料可以描述，不是漏掉的結構，退回 `Any`，
        不該讓整條 pipeline 崩潰。"""
        raw_schema = {
            "properties": {"data": {"type": "object"}},
            "required": [],
        }
        with caplog.at_level("INFO"):
            fields = extract_schema_fields(raw_schema)

        assert fields == [("data", "Any | None = None")]
        _assert_valid_field_declaration(*fields[0])
        assert any("Void" in r.message for r in caplog.records)

    def test_object_with_unexplained_extra_keys_still_raises(self, caplog):
        """跟上一個測試唯一的差異是多了一個這個函式看不懂的鍵
        （`oneOf`）——代表可能真的漏接了一個能救回來的結構，維持嚴格
        失敗，不要悄悄退回 Any 蓋掉一個真正的缺口。"""
        raw_schema = {
            "properties": {"mystery": {"type": "object", "oneOf": [{"type": "string"}]}},
            "required": [],
        }
        with caplog.at_level("WARNING"):
            with pytest.raises(ValueError, match="mystery"):
                extract_schema_fields(raw_schema)
        assert any("裸 object" in r.message for r in caplog.records)

    def test_field_with_title_does_not_raise(self):
        """有 `title` 可用時走既有的具名 inline 型別行為，不受這次修正
        影響。"""
        raw_schema = {
            "properties": {"named": {"type": "object", "title": "InlineNamedThing"}},
            "required": ["named"],
        }
        fields = extract_schema_fields(raw_schema)
        assert fields == [("named", "InlineNamedThing")]
