"""design_agent/type_mapping.py 的 map_java_type() 假資料單元測試。

對應 05a 五章「基礎型別對應表」＋十三章「型別對應根因修正」（回應
07a 十四章「建議修正 05a 型別對應表（根因）」）：Layer 1（未知泛型的
符號轉換 fallback）與 Layer 2（JDK functional interface 對照表）。
"""
import ast

import pytest

from design_agent.type_mapping import map_java_type


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
