"""core/postman_runner.py 的 run_newman()／extract_response_body()，鎖住
幾個實測發現的真實環境缺陷（見 09b_implement_agent_code.md 十章、
09b_bug_trace.md 的完整重現記錄）：

1. `--env-var` 傳的變數名稱必須是 `baseUrl`（駝峰式，對應
   postman/collection_*.json 頂層 "variable" 陣列的實際 key），不是
   `base_url`（底線）——名稱對不上時 newman 永遠 fallback 回 collection
   內建的預設值，錄製對 Java 端「碰巧」正確（預設值恰好等於 Java 網址），
   驗證對 Python 端則從未真正命中過 Python 服務。
2. 用 `shutil.which("newman")` 解析完整路徑，不是直接傳字面字串
   "newman"——Windows 上全域 npm 套件是 `newman.cmd`，
   `subprocess.run(["newman", ...], shell=False)` 不會自動嘗試附加
   副檔名，即使命令列打字執行沒問題，也會直接 FileNotFoundError。
3. `response["body"]` 這個欄位在真實 newman 6.2.2 根本不存在，body 內容
   序列化在 `response["stream"]`（Node.js Buffer 的 JSON 表示）——這個
   bug 讓錄製到的每一筆 golden output body 都是空的，直到用真實 Java／
   Python 服務跑完整條端對端驗證才被發現（見 09b_bug_trace.md #20）。
"""
import json

import pytest

from refactor_harness.core import postman_runner


def test_run_newman_uses_camelcase_base_url_variable_name(monkeypatch, tmp_path):
    captured_cmd = {}

    def _fake_which(name):
        assert name == "newman"
        return "/usr/local/bin/newman"

    def _fake_run(cmd, capture_output, text):
        captured_cmd["cmd"] = cmd
        output_path = cmd[cmd.index("--reporter-json-export") + 1]
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({"run": {"executions": []}}, f)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(postman_runner.shutil, "which", _fake_which)
    monkeypatch.setattr(postman_runner.subprocess, "run", _fake_run)

    postman_runner.run_newman("postman/collection_readonly.json", "http://localhost:8000")

    cmd = captured_cmd["cmd"]
    assert "--env-var" in cmd
    env_var_value = cmd[cmd.index("--env-var") + 1]
    assert env_var_value == "baseUrl=http://localhost:8000"
    # 回歸測試：不能是底線寫法，那是這次要修正的 bug 本身
    assert not env_var_value.startswith("base_url=")


def test_run_newman_raises_clear_error_when_newman_not_found(monkeypatch):
    monkeypatch.setattr(postman_runner.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="newman"):
        postman_runner.run_newman("postman/collection_readonly.json", "http://localhost:8000")


def test_extract_response_body_decodes_stream_buffer():
    # 對應真實案例：newman 6.2.2 的 response 物件把 body 序列化成
    # {"type": "Buffer", "data": [位元組陣列]}，不是純字串欄位。
    body_bytes = '{"code":200,"msg":"操作成功"}'.encode("utf-8")
    response = {"stream": {"type": "Buffer", "data": list(body_bytes)}}

    result = postman_runner.extract_response_body(response)

    assert result == '{"code":200,"msg":"操作成功"}'


def test_extract_response_body_none_when_stream_missing():
    # 對應真正沒有 body 的情況（如 204 No Content），response 裡完全
    # 沒有 "stream" 這個鍵。
    assert postman_runner.extract_response_body({}) is None


def test_extract_response_body_none_when_stream_data_empty():
    assert postman_runner.extract_response_body({"stream": {"type": "Buffer", "data": []}}) is None
