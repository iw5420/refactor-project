"""manual_fill.py 的假資料單元測試（見 docs/03a 三章「人工填值機制」）。
不呼叫真實 Claude API、不需要真實 seed.sql。
"""
import json

import pytest

from spec_collection_agent.manual_fill import (
    Decision,
    FillMode,
    ManualFillEntry,
    apply_manual_fill,
    generate_manual_fill_templates,
    list_pending,
    list_skipped,
    read_entry,
    record_apply_result,
)


class TestManualFillEntryIsResolved:
    def test_skip_always_resolved(self):
        entry = ManualFillEntry(endpoint="POST /a", param_schema={}, decision=Decision.SKIP)
        assert entry.is_resolved is True

    def test_fields_mode_needs_nonempty_values(self):
        entry = ManualFillEntry(endpoint="POST /a", param_schema={}, fill_mode=FillMode.FIELDS)
        assert entry.is_resolved is False
        entry.values = {"id": 1}
        assert entry.is_resolved is True

    def test_raw_body_mode_needs_non_none(self):
        entry = ManualFillEntry(endpoint="POST /a", param_schema={}, fill_mode=FillMode.RAW_BODY)
        assert entry.is_resolved is False
        entry.raw_body = [{"id": 1}]
        assert entry.is_resolved is True

    def test_file_upload_mode_needs_nonempty_file_paths(self):
        entry = ManualFillEntry(endpoint="POST /a", param_schema={}, fill_mode=FillMode.FILE_UPLOAD)
        assert entry.is_resolved is False
        entry.file_paths = {"file": "fixtures/file_upload_samples/sample_image.png"}
        assert entry.is_resolved is True

    def test_last_apply_error_overrides_filled_values(self):
        # 套用失敗跟「尚未填值」是同一種 retry 狀態——即使 values 有填，
        # last_apply_error 有值時 is_resolved 仍要是 False（見 03a 三章
        # 「套用失敗的重填機制」）。
        entry = ManualFillEntry(
            endpoint="POST /a",
            param_schema={},
            fill_mode=FillMode.FIELDS,
            values={"id": 1},
            last_apply_error="巢狀路徑衝突",
        )
        assert entry.is_resolved is False

    def test_skip_resolved_regardless_of_last_apply_error(self):
        # decision=skip 的判斷順序在 last_apply_error 之前——就算欄位裡
        # 還留著上一輪的錯誤訊息，只要是 skip 就一律已解決。
        entry = ManualFillEntry(
            endpoint="POST /a",
            param_schema={},
            decision=Decision.SKIP,
            last_apply_error="上一輪的錯誤",
        )
        assert entry.is_resolved is True


def _spec(paths: dict) -> dict:
    return {"paths": paths}


class TestGenerateManualFillTemplates:
    def test_creates_one_file_per_controller(self, tmp_path):
        spec = _spec({
            "/exam/update": {
                "put": {
                    "tags": ["exam-controller"],
                    "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                }
            },
            "/grading/score": {
                "post": {
                    "tags": ["grading-controller"],
                    "requestBody": {"content": {"application/json": {"schema": {"type": "array"}}}},
                }
            },
        })
        added = generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)

        assert sorted(added) == ["POST /grading/score", "PUT /exam/update"]
        assert (tmp_path / "exam_controller.json").exists()
        assert (tmp_path / "grading_controller.json").exists()

    def test_skips_endpoint_without_params(self, tmp_path):
        spec = _spec({"/health": {"post": {"tags": ["misc-controller"]}}})
        added = generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)
        assert added == []
        assert not (tmp_path / "misc_controller.json").exists()

    def test_readonly_methods_excluded(self, tmp_path):
        spec = _spec({
            "/exam/{id}": {
                "get": {
                    "tags": ["exam-controller"],
                    "parameters": [{"name": "id", "in": "path"}],
                }
            }
        })
        added = generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)
        assert added == []

    def test_rerun_does_not_overwrite_existing_entry(self, tmp_path):
        spec = _spec({
            "/exam/update": {
                "put": {
                    "tags": ["exam-controller"],
                    "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                }
            }
        })
        generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)

        # 人工已經填好值
        path = tmp_path / "exam_controller.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["endpoints"][0]["values"] = {"id": 1}
        path.write_text(json.dumps(raw), encoding="utf-8")

        # 重跑：同一個 endpoint 不該被覆寫掉已填的值
        added_again = generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)
        assert added_again == []
        entry = read_entry("PUT /exam/update", tmp_path)
        assert entry.values == {"id": 1}

    def test_rerun_adds_only_new_endpoint(self, tmp_path):
        spec = _spec({
            "/exam/update": {
                "put": {
                    "tags": ["exam-controller"],
                    "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                }
            }
        })
        generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)

        spec["paths"]["/exam/create"] = {
            "post": {
                "tags": ["exam-controller"],
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
            }
        }
        added = generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)
        assert added == ["POST /exam/create"]
        entry = read_entry("PUT /exam/update", tmp_path)
        assert entry is not None  # 既有的沒有被動過

    def test_default_fill_mode_by_body_shape(self, tmp_path):
        spec = _spec({
            "/object": {
                "post": {
                    "tags": ["t"],
                    "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                }
            },
            "/array": {
                "post": {
                    "tags": ["t"],
                    "requestBody": {"content": {"application/json": {"schema": {"type": "array"}}}},
                }
            },
            "/upload": {
                "post": {
                    "tags": ["t"],
                    "requestBody": {
                        "content": {
                            "multipart/form-data": {
                                "schema": {"type": "object", "properties": {"file": {"type": "string", "format": "binary"}}}
                            }
                        }
                    },
                }
            },
        })
        generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)

        assert read_entry("POST /object", tmp_path).fill_mode == FillMode.FIELDS
        assert read_entry("POST /array", tmp_path).fill_mode == FillMode.RAW_BODY
        assert read_entry("POST /upload", tmp_path).fill_mode == FillMode.FILE_UPLOAD


class TestReadEntry:
    def test_not_found_returns_none(self, tmp_path):
        assert read_entry("GET /nope", tmp_path) is None

    def test_finds_across_multiple_controller_files(self, tmp_path):
        spec = _spec({
            "/a": {"post": {"tags": ["controller-a"], "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}}},
            "/b": {"post": {"tags": ["controller-b"], "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}}},
        })
        generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)
        assert read_entry("POST /b", tmp_path) is not None
        assert read_entry("POST /b", tmp_path).endpoint == "POST /b"


class TestListPendingAndSkipped:
    def test_persisted_resolved_entry_not_pending(self, tmp_path):
        # 模擬模板不刪除之後，已解決的項目應該一直留在檔案裡但不算 pending
        spec = _spec({
            "/a": {"post": {"tags": ["c"], "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}}},
            "/b": {"post": {"tags": ["c"], "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}}},
        })
        generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)

        path = tmp_path / "c.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        for e in raw["endpoints"]:
            if e["endpoint"] == "POST /a":
                e["values"] = {"id": 1}
            elif e["endpoint"] == "POST /b":
                e["decision"] = "skip"
        path.write_text(json.dumps(raw), encoding="utf-8")

        assert list_pending(tmp_path) == []
        assert list_skipped(tmp_path) == ["POST /b"]
        # 檔案本身仍然存在、兩筆項目都還在（模板不刪除）
        assert path.exists()
        assert len(json.loads(path.read_text(encoding="utf-8"))["endpoints"]) == 2

    def test_unresolved_entry_is_pending(self, tmp_path):
        spec = _spec({
            "/a": {"post": {"tags": ["c"], "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}}},
        })
        generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)
        assert list_pending(tmp_path) == ["POST /a"]

    def test_empty_dir_returns_empty(self, tmp_path):
        assert list_pending(tmp_path) == []
        assert list_skipped(tmp_path) == []

    def test_last_apply_error_counts_as_pending(self, tmp_path):
        # 值有填、但帶著上一輪的 last_apply_error：跟「還沒填」是同一種
        # pending（retry）狀態，list_pending() 要抓得到。
        spec = _spec({
            "/a": {"post": {"tags": ["c"], "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}}},
        })
        generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)
        path = tmp_path / "c.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["endpoints"][0]["values"] = {"id": 1}
        raw["endpoints"][0]["last_apply_error"] = "巢狀路徑衝突"
        path.write_text(json.dumps(raw), encoding="utf-8")

        assert list_pending(tmp_path) == ["POST /a"]


class TestRecordApplyResult:
    def _entry_path(self, tmp_path):
        spec = _spec({
            "/a": {"post": {"tags": ["c"], "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}}},
        })
        generate_manual_fill_templates(openapi_spec=spec, manual_fill_dir=tmp_path)
        return tmp_path / "c.json"

    def test_records_error_message(self, tmp_path):
        self._entry_path(tmp_path)
        record_apply_result("POST /a", tmp_path, error="巢狀路徑衝突")
        entry = read_entry("POST /a", tmp_path)
        assert entry.last_apply_error == "巢狀路徑衝突"

    def test_clears_previous_error_on_success(self, tmp_path):
        self._entry_path(tmp_path)
        record_apply_result("POST /a", tmp_path, error="巢狀路徑衝突")
        record_apply_result("POST /a", tmp_path, error=None)
        entry = read_entry("POST /a", tmp_path)
        assert entry.last_apply_error is None

    def test_does_not_write_file_when_unchanged(self, tmp_path):
        path = self._entry_path(tmp_path)
        mtime_before = path.stat().st_mtime_ns
        # 預設就是 last_apply_error=None，記錄「成功」(error=None) 沒有
        # 任何變化，不該去重寫檔案。
        record_apply_result("POST /a", tmp_path, error=None)
        assert path.stat().st_mtime_ns == mtime_before

    def test_missing_endpoint_is_noop(self, tmp_path):
        self._entry_path(tmp_path)
        record_apply_result("GET /nope", tmp_path, error="不該發生")
        assert read_entry("GET /nope", tmp_path) is None

    def test_missing_dir_is_noop(self, tmp_path):
        record_apply_result("POST /a", tmp_path / "does-not-exist", error="x")


class TestApplyManualFill:
    def test_fields_mode_dispatches_to_apply_values(self):
        item = {"request": {"method": "POST", "url": {"path": ["a"], "variable": [{"key": "id", "value": "0"}], "query": []}}}
        entry = ManualFillEntry(
            endpoint="POST /a", param_schema={}, fill_mode=FillMode.FIELDS, values={"id": "5"}
        )
        apply_manual_fill(item, entry)
        assert item["request"]["url"]["variable"][0]["value"] == "5"

    def test_raw_body_mode_dispatches_to_apply_raw_body(self):
        item = {"request": {"method": "POST", "url": {"path": ["a"], "variable": [], "query": []}}}
        entry = ManualFillEntry(
            endpoint="POST /a", param_schema={}, fill_mode=FillMode.RAW_BODY, raw_body=[{"id": 1}]
        )
        apply_manual_fill(item, entry)
        assert json.loads(item["request"]["body"]["raw"]) == [{"id": 1}]

    def test_raw_body_mode_also_fills_url_query(self):
        # 迴歸測試：raw_body 模式的 endpoint 旁邊常常還有 query 參數（跟
        # body 是什麼格式無關），過去 fill_mode 分派只會呼叫
        # _apply_raw_body()，entry.values 完全被忽略——query 參數永遠只
        # 留著 openapi-to-postmanv2 產生的預設佔位值，是靜默發生的 False
        # Pass 風險（見 03a 三章「檔案上傳」更正）。
        item = {
            "request": {
                "method": "POST",
                "url": {"path": ["a"], "variable": [], "query": [{"key": "kind", "value": "string"}]},
            }
        }
        entry = ManualFillEntry(
            endpoint="POST /a",
            param_schema={},
            fill_mode=FillMode.RAW_BODY,
            raw_body=[{"id": 1}],
            values={"kind": "real-kind"},
        )
        apply_manual_fill(item, entry)
        assert item["request"]["url"]["query"][0]["value"] == "real-kind"
        assert json.loads(item["request"]["body"]["raw"]) == [{"id": 1}]

    def test_file_upload_mode_also_fills_url_query(self):
        # 迴歸測試：同樣的問題也發生在 file_upload 模式——
        # POST /api/file/image 的 kind/randomId/side 是必填 query 參數，
        # file 才是 multipart body，過去 fill_mode="file_upload" 只會呼叫
        # _apply_file_upload()，這三個必填參數完全沒有管道被填。
        item = {
            "request": {
                "method": "POST",
                "url": {
                    "path": ["file", "image"],
                    "variable": [],
                    "query": [
                        {"key": "kind", "value": "string"},
                        {"key": "randomId", "value": "string"},
                        {"key": "side", "value": "string"},
                    ],
                },
                "body": {
                    "mode": "formdata",
                    "formdata": [{"key": "file", "type": "file"}],
                },
            }
        }
        entry = ManualFillEntry(
            endpoint="POST /api/file/image",
            param_schema={},
            fill_mode=FillMode.FILE_UPLOAD,
            file_paths={"file": "fixtures/file_upload_samples/sample_image.png"},
            values={"kind": "photo", "randomId": "abc123", "side": "front"},
        )
        apply_manual_fill(item, entry)
        query = {q["key"]: q["value"] for q in item["request"]["url"]["query"]}
        assert query == {"kind": "photo", "randomId": "abc123", "side": "front"}
        assert item["request"]["body"]["formdata"][0]["src"] == "fixtures/file_upload_samples/sample_image.png"

    def test_unsupported_fill_mode_raises(self):
        item = {"request": {}}
        entry = ManualFillEntry(endpoint="POST /a", param_schema={})
        entry.fill_mode = "not_a_real_mode"  # type: ignore[assignment]
        with pytest.raises(ValueError):
            apply_manual_fill(item, entry)


class TestApplyFileUpload:
    def _formdata_item(self):
        return {
            "request": {
                "method": "POST",
                "url": {"path": ["upload"], "variable": [], "query": []},
                "body": {"mode": "formdata", "formdata": [{"key": "file", "type": "file"}]},
            }
        }

    def test_sets_src_to_provided_path(self):
        item = self._formdata_item()
        entry = ManualFillEntry(
            endpoint="POST /upload",
            param_schema={},
            fill_mode=FillMode.FILE_UPLOAD,
            file_paths={"file": "fixtures/file_upload_samples/sample_image.png"},
        )
        apply_manual_fill(item, entry)
        assert item["request"]["body"]["formdata"][0]["src"] == "fixtures/file_upload_samples/sample_image.png"

    def test_missing_formdata_key_raises(self):
        item = self._formdata_item()
        entry = ManualFillEntry(
            endpoint="POST /upload",
            param_schema={},
            fill_mode=FillMode.FILE_UPLOAD,
            file_paths={"nonexistent_key": "fixtures/file_upload_samples/sample_image.png"},
        )
        with pytest.raises(ValueError, match="找不到 file 型別的欄位"):
            apply_manual_fill(item, entry)

    def test_nonexistent_file_raises(self):
        item = self._formdata_item()
        entry = ManualFillEntry(
            endpoint="POST /upload",
            param_schema={},
            fill_mode=FillMode.FILE_UPLOAD,
            file_paths={"file": "fixtures/file_upload_samples/does_not_exist.png"},
        )
        with pytest.raises(ValueError, match="指向的檔案不存在"):
            apply_manual_fill(item, entry)

    def test_non_formdata_body_raises(self):
        item = {"request": {"method": "POST", "url": {"path": ["a"], "variable": [], "query": []}, "body": {"mode": "raw", "raw": "{}"}}}
        entry = ManualFillEntry(
            endpoint="POST /a",
            param_schema={},
            fill_mode=FillMode.FILE_UPLOAD,
            file_paths={"file": "fixtures/file_upload_samples/sample_image.png"},
        )
        with pytest.raises(ValueError, match="不是 formdata 模式"):
            apply_manual_fill(item, entry)
