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
4. `run_newman()` 改用 `Popen`（不是 `subprocess.run()`）+ 逾時後
   `_kill_process_tree()`——單純的 `subprocess.run(timeout=...)` 在
   Windows 上砍不乾淨 `newman.cmd` 底下真正在跑的 `node.exe` 子行程，
   真實重跑卡了 24 分鐘以上都沒被逾時機制擋下，見 09b_bug_trace.md #36。
"""
import json
import subprocess

import pytest

from refactor_harness.core import postman_runner


def _fake_popen(communicate_impl, captured_cmds=None, captured_kwargs=None):
    """建構一個假的 `subprocess.Popen` 類別：呼叫端只需要提供
    `communicate_impl(fake_self, timeout)` 決定 `communicate()` 的行為
    （回傳 (stdout, stderr) 或拋 `TimeoutExpired`），不用真的啟動任何
    行程——`run_newman()` 已經改用 `Popen` 而不是 `subprocess.run()`
    （見 docs/09b_bug_trace.md #36），這裡的假物件要提供 `.pid`／
    `.returncode`／`.communicate()`，比照真實 `Popen` 的介面。
    """
    class _FakePopen:
        def __init__(self, cmd, stdout=None, stderr=None, text=None, **kwargs):
            self.cmd = cmd
            self.pid = 4242
            self.returncode = 0
            if captured_cmds is not None:
                captured_cmds.append(cmd)
            if captured_kwargs is not None:
                captured_kwargs.update(kwargs)

        def communicate(self, timeout=None):
            return communicate_impl(self, timeout)

    return _FakePopen


def test_run_newman_uses_camelcase_base_url_variable_name(monkeypatch, tmp_path):
    captured_cmds = []

    def _fake_which(name):
        assert name == "newman"
        return "/usr/local/bin/newman"

    def _communicate(self, timeout):
        output_path = self.cmd[self.cmd.index("--reporter-json-export") + 1]
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({"run": {"executions": []}}, f)
        self.returncode = 0
        return ("", "")

    monkeypatch.setattr(postman_runner.shutil, "which", _fake_which)
    monkeypatch.setattr(postman_runner.subprocess, "Popen", _fake_popen(_communicate, captured_cmds))

    postman_runner.run_newman("postman/collection_readonly.json", "http://localhost:8000")

    cmd = captured_cmds[0]
    assert "--env-var" in cmd
    env_var_value = cmd[cmd.index("--env-var") + 1]
    assert env_var_value == "baseUrl=http://localhost:8000"
    # 回歸測試：不能是底線寫法，那是這次要修正的 bug 本身
    assert not env_var_value.startswith("base_url=")


def test_run_newman_raises_clear_error_when_newman_not_found(monkeypatch):
    monkeypatch.setattr(postman_runner.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="newman"):
        postman_runner.run_newman("postman/collection_readonly.json", "http://localhost:8000")


def test_run_newman_returns_report_despite_nonzero_exit_when_report_is_valid(monkeypatch):
    """對應真實案例：鏈式依賴注入的 capture test script 斷言失敗時，
    newman exit code 是 1，但 JSON reporter 仍正常寫出完整報表——這種
    「collection 內部斷言失敗」不該被當成「執行本身失敗」，見
    docs/09b_bug_trace.md #32 的真實重現記錄。呼叫端（GoldenRecorder 的
    tainted-folder 機制等）要能拿到報表內容自己判斷，不能整個被
    RuntimeError 擋下來。"""
    def _fake_which(name):
        return "/usr/local/bin/newman"

    def _communicate(self, timeout):
        output_path = self.cmd[self.cmd.index("--reporter-json-export") + 1]
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({"run": {"executions": [{"item": {"name": "x"}}]}}, f)
        self.returncode = 1
        return ("", "")

    monkeypatch.setattr(postman_runner.shutil, "which", _fake_which)
    monkeypatch.setattr(postman_runner.subprocess, "Popen", _fake_popen(_communicate))

    result = postman_runner.run_newman("postman/collection_mutation.json", "http://localhost:8000")

    assert len(result["run"]["executions"]) == 1


def test_run_newman_passes_a_bounded_timeout_to_communicate(monkeypatch):
    """對應真實案例：目標服務容器內的 app 掛掉但 uvicorn --reload 的
    socket 還開著（不是乾脆的 connection refused），newman 打這種
    「看起來活著、實際上永遠不回應」的服務會卡住不結束——`communicate()`
    沒設 `timeout` 時，Python 會永遠等下去。這裡鎖住呼叫合約：
    `process.communicate()` 一定要帶一個非 None 的 `timeout`，不能重新
    退回無界等待。"""
    captured = {}

    def _fake_which(name):
        return "/usr/local/bin/newman"

    def _communicate(self, timeout):
        captured["timeout"] = timeout
        output_path = self.cmd[self.cmd.index("--reporter-json-export") + 1]
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({"run": {"executions": []}}, f)
        self.returncode = 0
        return ("", "")

    monkeypatch.setattr(postman_runner.shutil, "which", _fake_which)
    monkeypatch.setattr(postman_runner.subprocess, "Popen", _fake_popen(_communicate))

    postman_runner.run_newman("postman/collection_readonly.json", "http://localhost:8000")

    assert captured["timeout"] is not None
    assert captured["timeout"] > 0


def test_run_newman_raises_clear_error_on_timeout_instead_of_hanging(monkeypatch):
    """跟上面那個測試互補：即使真的逾時，也要拋出明確、有界的
    RuntimeError，不能讓 `subprocess.TimeoutExpired` 原始往上炸穿、
    也不能整個卡住不回應。"""
    def _fake_which(name):
        return "/usr/local/bin/newman"

    calls = {"n": 0}

    def _communicate(self, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd=self.cmd, timeout=timeout)
        return ("", "")  # 逾時後 _kill_process_tree() 已砍乾淨，收尾正常返回

    monkeypatch.setattr(postman_runner.shutil, "which", _fake_which)
    monkeypatch.setattr(postman_runner.subprocess, "Popen", _fake_popen(_communicate))
    monkeypatch.setattr(postman_runner, "_kill_process_tree", lambda pid: None)

    with pytest.raises(RuntimeError, match="逾時|timeout"):
        postman_runner.run_newman("postman/collection_readonly.json", "http://localhost:8000")


def test_run_newman_kills_full_process_tree_on_timeout_not_just_the_wrapper(monkeypatch):
    """對應 docs/09b_bug_trace.md #36 的真實根因：Windows 上單純的
    `subprocess.run(timeout=...)` 只會砍掉 `newman.cmd` 這個 wrapper，
    砍不到它底下真正在跑的 `node.exe`，讓收尾等待永遠等不到 EOF——
    真實重跑卡了 24 分鐘以上才被查出來。這裡驗證：逾時發生時，一定會
    呼叫 `_kill_process_tree()`（對整棵樹下手，不是只呼叫
    `process.kill()` 那種只砍得到最上層的做法），而且逾時後的收尾
    `communicate()` 真的會被呼叫第二次、正常結束（因為樹已經砍乾淨），
    不會自己也卡住。"""
    killed_pids = []
    communicate_calls = {"n": 0}

    def _fake_which(name):
        return "/usr/local/bin/newman"

    def _communicate(self, timeout):
        communicate_calls["n"] += 1
        if communicate_calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd=self.cmd, timeout=timeout)
        # 第二次呼叫是逾時後的收尾：因為 _kill_process_tree() 已經把整棵
        # 樹砍乾淨，管線會正常收到 EOF，不會卡住。
        return ("", "")

    monkeypatch.setattr(postman_runner.shutil, "which", _fake_which)
    monkeypatch.setattr(postman_runner.subprocess, "Popen", _fake_popen(_communicate))
    monkeypatch.setattr(postman_runner, "_kill_process_tree", lambda pid: killed_pids.append(pid))

    with pytest.raises(RuntimeError, match="逾時"):
        postman_runner.run_newman("postman/collection_readonly.json", "http://localhost:8000")

    assert killed_pids == [4242]
    assert communicate_calls["n"] == 2


def test_run_newman_raises_when_nonzero_exit_and_no_report_produced(monkeypatch):
    """跟上面那個測試相反的情境：exit code 非 0，且報表根本沒產生
    （服務沒起來、collection 路徑錯誤等真正的執行失敗）——這才是應該
    硬性擋下的錯誤，不能被「trust 報表內容」的邏輯誤放行。"""
    def _fake_which(name):
        return "/usr/local/bin/newman"

    def _communicate(self, timeout):
        # 故意不寫任何報表檔案，模擬 newman 連跑都沒跑起來
        self.returncode = 1
        return ("", "connect ECONNREFUSED 127.0.0.1:8000")

    monkeypatch.setattr(postman_runner.shutil, "which", _fake_which)
    monkeypatch.setattr(postman_runner.subprocess, "Popen", _fake_popen(_communicate))

    with pytest.raises(RuntimeError, match="ECONNREFUSED"):
        postman_runner.run_newman("postman/collection_readonly.json", "http://localhost:8000")


class TestKillProcessTree:
    """對應 docs/09b_bug_trace.md #36 根因：Windows 上單純砍掉
    `newman.cmd` wrapper 本身，砍不到它底下真正在跑的 `node.exe`，
    必須用 `taskkill /T`（整棵樹）才砍得乾淨。"""

    def test_windows_uses_taskkill_with_tree_flag(self, monkeypatch):
        monkeypatch.setattr(postman_runner.os, "name", "nt")
        captured = {}

        def _fake_run(cmd, capture_output, text):
            captured["cmd"] = cmd

            class _Result:
                returncode = 0

            return _Result()

        monkeypatch.setattr(postman_runner.subprocess, "run", _fake_run)

        postman_runner._kill_process_tree(4242)

        # /T 是關鍵：整棵行程樹一起砍，不是只砍最上層那個 wrapper
        assert captured["cmd"] == ["taskkill", "/F", "/T", "/PID", "4242"]

    def test_posix_uses_killpg(self, monkeypatch):
        monkeypatch.setattr(postman_runner.os, "name", "posix")
        # signal.SIGKILL 在 Windows 上的 Python 建置根本不存在這個屬性
        # （這個測試檔案本身跑在 Windows 開發機上）——這裡只是為了能在
        # Windows 上模擬 POSIX 分支，用 raising=False 補上這個常數，不影響
        # 真正在 POSIX 系統上執行時的行為（那邊本來就有這個屬性）。
        monkeypatch.setattr(postman_runner.signal, "SIGKILL", 9, raising=False)
        killed = {}

        def _fake_killpg(pid, sig):
            killed["pid"] = pid

        monkeypatch.setattr(postman_runner.os, "killpg", _fake_killpg, raising=False)

        postman_runner._kill_process_tree(4242)

        assert killed["pid"] == 4242

    def test_posix_ignores_process_already_gone(self, monkeypatch):
        # 行程樹自己剛好也結束了（正常收尾情境），不該讓 _kill_process_tree()
        # 自己拋例外擋下原本要往外傳的 RuntimeError。
        monkeypatch.setattr(postman_runner.os, "name", "posix")
        monkeypatch.setattr(postman_runner.signal, "SIGKILL", 9, raising=False)

        def _fake_killpg(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(postman_runner.os, "killpg", _fake_killpg, raising=False)

        postman_runner._kill_process_tree(4242)  # 不應該拋例外


class TestSpawn:
    """`_spawn()` 要讓子行程自成一個獨立的行程群組／session，
    `_kill_process_tree()` 才砍得到整棵樹。"""

    def test_windows_creates_new_process_group(self, monkeypatch):
        monkeypatch.setattr(postman_runner.os, "name", "nt")
        captured = {}

        def _fake_popen(cmd, **kwargs):
            captured.update(kwargs)

            class _P:
                pid = 1

            return _P()

        monkeypatch.setattr(postman_runner.subprocess, "Popen", _fake_popen)

        postman_runner._spawn(["newman", "run"])

        assert captured["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
        assert "start_new_session" not in captured

    def test_posix_starts_new_session(self, monkeypatch):
        monkeypatch.setattr(postman_runner.os, "name", "posix")
        captured = {}

        def _fake_popen(cmd, **kwargs):
            captured.update(kwargs)

            class _P:
                pid = 1

            return _P()

        monkeypatch.setattr(postman_runner.subprocess, "Popen", _fake_popen)

        postman_runner._spawn(["newman", "run"])

        assert captured["start_new_session"] is True
        assert "creationflags" not in captured


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
