"""
GoldenRecorder 的錄製異常偵測測試（見 docs/02a_harness_architecture.md 三章
「Mutation 錄製異常偵測」）。

DbEnvironment.apply_seed 與 core.postman_runner.run_newman／
list_top_level_folders 全部 mock 掉，不連真實 DB、不執行真實 newman，
只驗證 GoldenRecorder 內部的分類與寫檔邏輯。
"""
import json

from refactor_harness.fixtures.db_env import DbEnvironment
from refactor_harness.recorder import golden_writer
from refactor_harness.recorder.golden_writer import GoldenRecorder


def _execution(name: str, method: str, url_parts: list[str], status: int, body: dict) -> dict:
    return {
        "item": {
            "name": name,
            "request": {
                "method": method,
                "url": {"path": url_parts},
            },
        },
        "response": {
            "code": status,
            "headers": {"members": [{"key": "Content-Type", "value": "application/json"}]},
            "body": json.dumps(body),
        },
    }


# 健康 folder：兩個 request 都是 2xx
_HEALTHY_FOLDER = "order_lifecycle_ok"
_HEALTHY_EXECUTIONS = [
    _execution("Create Order", "POST", ["api", "v1", "orders"], 201, {"id": 1, "status": "pending"}),
    _execution("Get Order", "GET", ["api", "v1", "orders", "1"], 200, {"id": 1, "status": "pending"}),
]
_HEALTHY_CASE_IDS = {
    "create_order_POST_api_v1_orders",
    "get_order_GET_api_v1_orders_1",
}

# 異常 folder：第二個 request status code 是 400
_TAINTED_FOLDER = "order_lifecycle_bad"
_TAINTED_EXECUTIONS = [
    _execution("Create Order Bad", "POST", ["api", "v1", "orders"], 201, {"id": 2, "status": "pending"}),
    _execution("Get Order Bad", "GET", ["api", "v1", "orders", "2"], 400, {"error": "not found"}),
]
_TAINTED_CASE_IDS = {
    "create_order_bad_POST_api_v1_orders",
    "get_order_bad_GET_api_v1_orders_2",
}

# readonly 回歸測試用：4xx 也照樣要寫入 golden
_READONLY_EXECUTIONS = [
    _execution("Get Missing User", "GET", ["api", "v1", "users", "999"], 404, {"error": "not found"}),
]


def _fake_run_newman_mutation(collection_path, base_url, folder=None):
    if folder == _HEALTHY_FOLDER:
        return {"run": {"executions": _HEALTHY_EXECUTIONS}}
    if folder == _TAINTED_FOLDER:
        return {"run": {"executions": _TAINTED_EXECUTIONS}}
    raise AssertionError(f"未預期呼叫 run_newman(folder={folder!r})")


def _fake_run_newman_readonly(collection_path, base_url, folder=None):
    assert folder is None
    return {"run": {"executions": _READONLY_EXECUTIONS}}


def test_record_mutation_excludes_tainted_folder(tmp_path, monkeypatch):
    """情境 a：異常 folder 整批不寫入 golden，健康 folder 正常寫入。"""
    monkeypatch.setattr(DbEnvironment, "apply_seed", lambda self, *a, **kw: None)
    monkeypatch.setattr(
        golden_writer, "list_top_level_folders",
        lambda collection_path: [_HEALTHY_FOLDER, _TAINTED_FOLDER],
    )
    monkeypatch.setattr(golden_writer, "run_newman", _fake_run_newman_mutation)

    recorder = GoldenRecorder(
        java_base_url="http://localhost:8080",
        golden_dir=str(tmp_path),
        test_dsn="postgresql://fake/db",
    )
    result = recorder.record_mutation("postman/collection_mutation.json")

    # 健康 folder 的 case 正常寫入
    assert set(result["cases"]) == _HEALTHY_CASE_IDS
    for case_id in _HEALTHY_CASE_IDS:
        assert (tmp_path / "orders" / f"{case_id}.json").exists()

    # 異常 folder 的兩個 case 都沒有寫入任何 golden 檔案（含原本正常的那個）
    for case_id in _TAINTED_CASE_IDS:
        assert not (tmp_path / "orders" / f"{case_id}.json").exists()

    # tainted_folders 有一筆，excluded_case_ids 包含該 folder 全部 case
    assert len(result["tainted_folders"]) == 1
    tainted = result["tainted_folders"][0]
    assert tainted["folder"] == _TAINTED_FOLDER
    assert set(tainted["excluded_case_ids"]) == _TAINTED_CASE_IDS


def test_record_readonly_writes_golden_even_on_4xx(tmp_path, monkeypatch):
    """
    情境 b（回歸測試）：record()（readonly）完全不受異常偵測影響，
    即使 status code 是 4xx，golden 仍照樣寫入——判定只在
    context="mutation" 生效。
    """
    monkeypatch.setattr(DbEnvironment, "apply_seed", lambda self, *a, **kw: None)
    monkeypatch.setattr(golden_writer, "run_newman", _fake_run_newman_readonly)

    recorder = GoldenRecorder(
        java_base_url="http://localhost:8080",
        golden_dir=str(tmp_path),
        test_dsn="postgresql://fake/db",
    )
    result = recorder.record("postman/collection_readonly.json")

    case_id = "get_missing_user_GET_api_v1_users_999"
    assert result["cases"] == [case_id]
    golden_path = tmp_path / "users" / f"{case_id}.json"
    assert golden_path.exists()

    with open(golden_path, encoding="utf-8") as f:
        golden = json.load(f)
    assert golden["response"]["status_code"] == 404
