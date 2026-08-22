"""GoldenVerifier._process_executions()，之前完全沒有專屬測試檔案。

補上是因為 09b 端對端驗證發現 comparator.py 讀 actual_response 的方式
跟 golden_writer.py 犯了同一種 bug（讀不存在的 response["body"]，永遠
拿到 None），且從沒有任何單元測試會發現這件事——之前只靠真實環境跑過
才暴露，見 docs/09b_bug_trace.md。這裡直接用真實 newman 6.2.2 的
response 形狀（stream Buffer）建構 mock execution，確認 body 比對真的
會生效：golden 與 actual 內容一致時要 passed=True，不一致時要抓出
body_diff，不能永遠因為兩邊 body 都被誤讀成 None 而「意外」通過。
"""
import json

from refactor_harness.verifier.comparator import GoldenVerifier


def _stream_response(status_code: int, body: dict | None) -> dict:
    response = {
        "code": status_code,
        "header": [{"key": "Content-Type", "value": "application/json"}],
    }
    if body is not None:
        response["stream"] = {"type": "Buffer", "data": list(json.dumps(body).encode("utf-8"))}
    return response


def _execution(name: str, method: str, url_parts: list[str], status: int, body: dict | None) -> dict:
    return {
        "item": {
            "name": name,
            "request": {"method": method, "url": {"path": url_parts}},
        },
        "response": _stream_response(status, body),
    }


def _write_golden(tmp_path, module: str, case_id: str, status_code: int, body: dict | None) -> None:
    golden_dir = tmp_path / module
    golden_dir.mkdir(parents=True, exist_ok=True)
    (golden_dir / f"{case_id}.json").write_text(
        json.dumps({"response": {"status_code": status_code, "body": body}}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_process_executions_passes_when_body_genuinely_matches(tmp_path, monkeypatch):
    case_id = "get_version_GET_api_v1_version"
    _write_golden(tmp_path, "version", case_id, 200, {"version": "1.0.1"})

    execution = _execution("get version", "GET", ["api", "v1", "version"], 200, {"version": "1.0.1"})
    # comparator.py 用 `from ... import run_newman` 直接匯入這個名稱，
    # 必須 patch 它自己命名空間裡的那個綁定，patch 來源模組
    # postman_runner.run_newman 對它的呼叫點不生效。
    monkeypatch.setattr(
        "refactor_harness.verifier.comparator.run_newman",
        lambda *a, **kw: {"run": {"executions": [execution]}},
    )

    verifier = GoldenVerifier(python_base_url="http://localhost:8000", golden_dir=str(tmp_path))

    raw = verifier.verify_raw("postman/collection_readonly.json")

    assert len(raw) == 1
    assert raw[0]["case_id"] == case_id
    assert raw[0]["passed"] is True
    assert raw[0]["body_diff"] is None
    # module 沒有 config/harness.yaml 對應（GET_api_v1_version 不在
    # route_to_module_mapping 裡），falls back 回 get_module() 的 URL
    # 推斷：跳過 api/v1，取下一段。
    assert raw[0]["module"] == "version"


def test_verify_excludes_case_recorded_as_skipped_non_json(tmp_path, monkeypatch):
    """對應真實案例：/api/general/version 這種端點在錄製當下就被
    Recorder 判定為非 JSON（text/plain）而跳過，從未寫入 golden——
    這不是 golden 遺失或 route_to_file_mapping 設定錯誤，驗證端不該
    把它算成 golden_not_found 失敗。GoldenVerifier 要讀
    {golden_dir}/_metadata.json 的 skipped 清單，把這類 case 從
    summary／failures 排除，改放進 excluded_cases。
    """
    case_id = "get_version_GET_api_general_version"
    (tmp_path / "_metadata.json").write_text(
        json.dumps({"skipped": [case_id]}), encoding="utf-8"
    )
    # 刻意不寫這個 case 的 golden 檔案——正是「錄製時就跳過」的真實狀態。

    execution = _execution("get version", "GET", ["api", "general", "version"], 200, None)
    monkeypatch.setattr(
        "refactor_harness.verifier.comparator.run_newman",
        lambda *a, **kw: {"run": {"executions": [execution]}},
    )

    verifier = GoldenVerifier(python_base_url="http://localhost:8000", golden_dir=str(tmp_path))
    report = verifier.verify("postman/collection_readonly.json")

    assert report["summary"]["total"] == 0
    assert report["failures"] == []
    assert report["excluded_cases"] == [case_id]
    # 沒有其他真正的失敗時，被排除的 case 不該讓整體 status 變成 fail。
    assert report["status"] == "pass"


def test_golden_not_found_includes_module_and_related_files(tmp_path, monkeypatch):
    """golden_not_found 分支：⑦ Debug Agent 要靠 module／related_files
    才能把這類失敗歸到對應模組，不用自己從 case_id 反推。故意不寫任何
    golden 檔案，也不放進 skipped 清單，觸發真正的 golden_not_found。"""
    execution = _execution("get version", "GET", ["api", "general", "version"], 200, {"version": "1.0.1"})
    monkeypatch.setattr(
        "refactor_harness.verifier.comparator.run_newman",
        lambda *a, **kw: {"run": {"executions": [execution]}},
    )

    verifier = GoldenVerifier(python_base_url="http://localhost:8000", golden_dir=str(tmp_path))
    raw = verifier.verify_raw("postman/collection_readonly.json")

    assert len(raw) == 1
    assert raw[0]["error"] == "golden_not_found"
    assert raw[0]["module"] == "school"
    assert raw[0]["related_files"] == [
        "app/repositories/school_repository.py",
        "app/routers/school_router.py",
        "app/schemas/school.py",
    ]


def test_response_not_json_includes_module_and_related_files(tmp_path, monkeypatch):
    """response_not_json 分支：Python 服務回傳非 JSON（如未處理例外的
    HTML error page）時，module／related_files 一樣要附上，理由同上。"""
    _write_golden(tmp_path, "school", "get_version_GET_api_general_version", 200, {"version": "1.0.1"})

    execution = {
        "item": {
            "name": "get version",
            "request": {"method": "GET", "url": {"path": ["api", "general", "version"]}},
        },
        "response": {
            "code": 200,
            "header": [{"key": "Content-Type", "value": "text/html"}],
            "stream": {"type": "Buffer", "data": list(b"<html>Internal Server Error</html>")},
        },
    }
    monkeypatch.setattr(
        "refactor_harness.verifier.comparator.run_newman",
        lambda *a, **kw: {"run": {"executions": [execution]}},
    )

    verifier = GoldenVerifier(python_base_url="http://localhost:8000", golden_dir=str(tmp_path))
    raw = verifier.verify_raw("postman/collection_readonly.json")

    assert len(raw) == 1
    assert raw[0]["error"] == "response_not_json"
    assert raw[0]["module"] == "school"
    assert raw[0]["related_files"] == [
        "app/repositories/school_repository.py",
        "app/routers/school_router.py",
        "app/schemas/school.py",
    ]


def test_process_executions_catches_real_body_mismatch(tmp_path, monkeypatch):
    case_id = "get_version_GET_api_v1_version"
    _write_golden(tmp_path, "version", case_id, 200, {"version": "1.0.1"})

    # actual 回傳的版本號跟 golden 不一樣——body 比對必須真的抓出這個差異，
    # 不能因為 body 被誤讀成 None（兩邊都是 None 時 DiffEngine 會判定相同）
    # 而被錯誤地判定通過。
    execution = _execution("get version", "GET", ["api", "v1", "version"], 200, {"version": "2.0.0"})
    monkeypatch.setattr(
        "refactor_harness.verifier.comparator.run_newman",
        lambda *a, **kw: {"run": {"executions": [execution]}},
    )

    verifier = GoldenVerifier(python_base_url="http://localhost:8000", golden_dir=str(tmp_path))
    raw = verifier.verify_raw("postman/collection_readonly.json")

    assert len(raw) == 1
    assert raw[0]["passed"] is False
    assert raw[0]["body_diff"] is not None
