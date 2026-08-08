"""design_agent/design.py 的 _reorder_params_defaults_last() 假資料單元
測試。回應真實案例：六章 LLM 決定的 extra_params 若帶 `Depends(...)`
這類自帶預設值的型別、且順序落在其他不帶預設值的參數之前，組出的函式
簽名會直接是 SyntaxError（見 design.py 該函式 docstring 的完整說明）。
"""
import ast

from design_agent.design import _reorder_params_defaults_last


def _assert_valid_signature(params: list[dict]) -> None:
    param_str = ", ".join(f"{p['name']}: {p['type']}" for p in params)
    ast.parse(f"def f({param_str}) -> None:\n    pass\n")


class TestReorderParamsDefaultsLast:
    def test_no_default_params_unchanged(self):
        params = [{"name": "user_id", "type": "int"}, {"name": "name", "type": "str"}]
        assert _reorder_params_defaults_last(params) == params

    def test_default_param_moved_after_non_default(self):
        # 真實觸發場景：db（機械附加，只有 routers 層帶預設值）排在
        # extra_params 決定的一個不帶預設值的框架參數之前。
        params = [
            {"name": "user_id", "type": "int"},
            {"name": "db", "type": "Session = Depends(get_db)"},
            {"name": "request", "type": "Request"},
        ]
        result = _reorder_params_defaults_last(params)
        assert result == [
            {"name": "user_id", "type": "int"},
            {"name": "request", "type": "Request"},
            {"name": "db", "type": "Session = Depends(get_db)"},
        ]
        _assert_valid_signature(result)

    def test_original_order_without_fix_would_be_syntax_error(self):
        # 確認這確實是個真實問題：不重排的話，這個組合本身就是非法簽名。
        params = [
            {"name": "user_id", "type": "int"},
            {"name": "db", "type": "Session = Depends(get_db)"},
            {"name": "request", "type": "Request"},
        ]
        try:
            _assert_valid_signature(params)
            raised = False
        except SyntaxError:
            raised = True
        assert raised

    def test_multiple_default_params_preserve_relative_order(self):
        params = [
            {"name": "current_user", "type": "User = Depends(get_current_user)"},
            {"name": "user_id", "type": "int"},
            {"name": "db", "type": "Session = Depends(get_db)"},
        ]
        result = _reorder_params_defaults_last(params)
        assert result == [
            {"name": "user_id", "type": "int"},
            {"name": "current_user", "type": "User = Depends(get_current_user)"},
            {"name": "db", "type": "Session = Depends(get_db)"},
        ]
        _assert_valid_signature(result)

    def test_empty_params(self):
        assert _reorder_params_defaults_last([]) == []

    def test_all_default_params_unchanged_relative_order(self):
        params = [
            {"name": "current_user", "type": "User = Depends(get_current_user)"},
            {"name": "db", "type": "Session = Depends(get_db)"},
        ]
        assert _reorder_params_defaults_last(params) == params
