"""translator_cli/formatting.py：ruff format 硬性依賴的整合。"""
import pytest

from translator_cli import formatting
from translator_cli.exceptions import TranslatorCliError


def test_format_paths_empty_list_is_noop():
    formatting.format_paths("does-not-matter")  # 不應拋出，也不應嘗試呼叫子行程


def test_format_paths_missing_ruff_executable_raises(tmp_path, monkeypatch):
    # ruff 執行檔找不到（FileNotFoundError）時必須拋出，不能只記警告
    # 略過——ruff 是硬性依賴。用 monkeypatch 模擬，不依賴測試環境本身
    # 有沒有裝 ruff（ruff 現在是專案的真實依賴，不能假設環境沒裝）。
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")

    def fake_run(cmd, cwd, capture_output, text, encoding):
        raise FileNotFoundError("找不到 ruff 執行檔")

    monkeypatch.setattr(formatting.subprocess, "run", fake_run)

    with pytest.raises(TranslatorCliError, match="ruff"):
        formatting.format_paths(str(tmp_path), "a.py")


def test_format_paths_nonzero_returncode_raises(tmp_path, monkeypatch):
    # 模擬 ruff 存在、但格式化本身失敗的情況（第二步 ruff format）：
    # 一樣必須拋出，不能吞掉。
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")

    class _FakeCompletedProcess:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stderr = "模擬 ruff 格式化失敗" if returncode else ""

    def fake_run(cmd, cwd, capture_output, text, encoding):
        assert cmd[0] == "ruff"
        return _FakeCompletedProcess(0 if cmd[1] == "check" else 1)

    monkeypatch.setattr(formatting.subprocess, "run", fake_run)

    with pytest.raises(TranslatorCliError, match="模擬 ruff 格式化失敗"):
        formatting.format_paths(str(tmp_path), "a.py")


def test_format_paths_import_sort_failure_raises(tmp_path, monkeypatch):
    # 第一步 ruff check --select I --fix（import 排序）本身失敗時，一樣
    # 必須拋出，不能因為只檢查第二步 format 而放過這條路徑。
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")

    class _FakeCompletedProcess:
        returncode = 1
        stderr = "模擬 import 排序失敗"

    def fake_run(cmd, cwd, capture_output, text, encoding):
        assert cmd[0] == "ruff"
        assert cmd[1] == "check"
        return _FakeCompletedProcess()

    monkeypatch.setattr(formatting.subprocess, "run", fake_run)

    with pytest.raises(TranslatorCliError, match="模擬 import 排序失敗"):
        formatting.format_paths(str(tmp_path), "a.py")


def test_format_paths_success_does_not_raise(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")

    class _FakeCompletedProcess:
        returncode = 0
        stderr = ""

    calls = []

    def fake_run(cmd, cwd, capture_output, text, encoding):
        calls.append(cmd)
        return _FakeCompletedProcess()

    monkeypatch.setattr(formatting.subprocess, "run", fake_run)

    formatting.format_paths(str(tmp_path), "a.py", "b.py")  # 不應拋出
    assert calls == [
        ["ruff", "check", "--select", "I", "--fix", "a.py", "b.py"],
        ["ruff", "format", "a.py", "b.py"],
    ]
