"""translator_cli/client.py 端對端測試，對應 07a 二、四、五、八、九章。
用真實 tmp_path git repo，`ollama_client.get_function_body()` 用
monkeypatch 換成假實作（不需要真的連線到 ollama），驗證
generate_scaffold()／fill_function() 的完整流程串接正確。
"""
import ast
import asyncio
import subprocess
from pathlib import Path

import pytest

from translator_cli import client, formatting, git_ops, ollama_client, scaffold
from translator_cli.exceptions import TranslatorCliError, TranslatorCliScaffoldMismatchError
from translator_cli.types import FillResult


@pytest.fixture(autouse=True)
def _stub_formatting(monkeypatch):
    """`formatting.format_paths()` 現在是硬性依賴（找不到 ruff 執行檔會
    拋出），但這個測試環境本身沒有安裝 ruff（見 `test_formatting.py`
    對這條路徑的獨立驗證）。這裡的測試要驗證的是 `client.py` 自己的
    串接邏輯（git 操作、ollama 呼叫、rollback），不是 ruff 本身有沒有
    裝，因此對整個檔案的測試一律 stub 成 no-op，避免每個 happy-path
    測試都因為環境缺 ruff 而失敗。
    """
    monkeypatch.setattr(formatting, "format_paths", lambda *args, **kwargs: None)


def _init_repo(path):
    subprocess.run(["git", "init"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, capture_output=True, text=True, check=True)


def _python_structure():
    directory_tree = (
        "app/\n  app/repositories/user_repository.py\n  app/models/user.py\n\n"
        "### app/core/database.py\n```python\nDATABASE_URL = 1\n```\n\n"
        "### app/main.py\n```python\nfrom fastapi import FastAPI\n\napp = FastAPI()\n```\n"
    )
    interfaces = [
        {
            "file_path": "app/repositories/user_repository.py",
            "class_name": "UserRepository",
            "function_name": "get_by_id",
            "params": [{"name": "user_id", "type": "int"}],
            "return_type": "int",
        }
    ]
    return {"directory_tree": directory_tree, "interfaces": interfaces}


def test_generate_scaffold_requires_git_repo(tmp_path):
    result = asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    assert result["success"] is False
    assert "git repo" in result["error"]


def test_generate_scaffold_requires_clean_working_tree(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "stray.txt").write_text("oops", encoding="utf-8")
    result = asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    assert result["success"] is False
    assert "working tree" in result["error"]


def test_generate_scaffold_writes_files_and_commits(tmp_path):
    _init_repo(tmp_path)
    result = asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    assert result["success"] is True
    assert result["skipped_interfaces"] == []
    assert (tmp_path / "app" / "repositories" / "user_repository.py").exists()
    assert (tmp_path / "app" / "core" / "database.py").exists()

    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "scaffold: initial skeleton" in log.stdout

    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert status.stdout.strip() == ""  # commit 後 working tree 恢復乾淨


async def _fake_get_function_body(**kwargs):
    return "return user_id * 2\n"


def test_fill_function_end_to_end(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    monkeypatch.setattr(ollama_client, "get_function_body", _fake_get_function_body)

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_001",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="回傳 user_id 乘以 2",
            context="",
            context_files=["app/repositories/user_repository.py"],
        )
    )

    assert isinstance(result, FillResult)
    assert result.success is True
    assert "user_id * 2" in result.diff

    written = (tmp_path / "app" / "repositories" / "user_repository.py").read_text(encoding="utf-8")
    assert "return user_id * 2" in written

    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert log.stdout.strip() == "implement: task_001 fill UserRepository.get_by_id in app/repositories/user_repository.py"


def test_fill_function_adds_missing_body_imports(tmp_path, monkeypatch):
    # 對應真實 pipeline 案例：qwen 生成的本體引用了簽名以外的名稱
    # （框架例外 HTTPException、跨檔案自訂類別 UserRepository），驗證
    # fill_function() 會補上這兩個 import，寫入的檔案語法合法、可以
    # 直接執行。
    _init_repo(tmp_path)
    structure = {
        "directory_tree": (
            "app/\n  app/routers/user_router.py\n"
            "  app/repositories/user_repository.py\n\n"
            "### app/core/database.py\n```python\nDATABASE_URL = 1\n```\n\n"
            "### app/main.py\n```python\nfrom fastapi import FastAPI\n\napp = FastAPI()\n```\n"
        ),
        "interfaces": [
            {
                "file_path": "app/routers/user_router.py",
                "class_name": None,
                "function_name": "get_user",
                "params": [{"name": "user_id", "type": "int"}],
                "return_type": "dict",
                "http_method": "GET",
                "route_path": "/api/v1/users/{user_id}",
            },
            {
                "file_path": "app/repositories/user_repository.py",
                "class_name": "UserRepository",
                "function_name": "get_by_id",
                "params": [{"name": "user_id", "type": "int"}],
                "return_type": "dict | None",
            },
        ],
    }
    asyncio.run(client.generate_scaffold(str(tmp_path), structure))

    async def fake_get_function_body(**kwargs):
        return (
            "user = UserRepository(None).get_by_id(user_id)\n"
            "if user is None:\n"
            "    raise HTTPException(status_code=404, detail=\"not found\")\n"
            "return user\n"
        )

    monkeypatch.setattr(ollama_client, "get_function_body", fake_get_function_body)

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_001",
            target_file="app/routers/user_router.py",
            class_name=None,
            function_name="get_user",
            description="d",
            context="",
            context_files=["app/routers/user_router.py"],
        )
    )

    assert result.success is True
    written = (tmp_path / "app" / "routers" / "user_router.py").read_text(encoding="utf-8")
    assert "from fastapi import" in written and "HTTPException" in written
    assert "from app.repositories.user_repository import UserRepository" in written
    ast.parse(written)  # 語法必須合法


def test_fill_function_idempotent_rerun_with_identical_body_succeeds(tmp_path, monkeypatch):
    # 07a 五章「冪等：允許對已有內容的函式重新填空」：同一個 task 因為
    # 某種原因被重複排程，LLM 這次生成的內容跟磁碟上已經 commit 的版本
    # 完全相同時，write_text() 之後 working tree 其實沒有變更，
    # `git commit`（不帶 --allow-empty）遇到空 staging area 會直接失敗
    # ——驗證 fill_function() 提前偵測到這個情況、視為成功，不會誤判成
    # commit 失敗並觸發不必要的 rollback。
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    monkeypatch.setattr(ollama_client, "get_function_body", _fake_get_function_body)

    kwargs = dict(
        python_project_path=str(tmp_path),
        target_file="app/repositories/user_repository.py",
        class_name="UserRepository",
        function_name="get_by_id",
        description="回傳 user_id 乘以 2",
        context="",
        context_files=["app/repositories/user_repository.py"],
    )

    first = asyncio.run(client.fill_function(task_id="task_001", **kwargs))
    assert first.success is True
    assert first.diff != ""

    log_before = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout

    second = asyncio.run(client.fill_function(task_id="task_001_retry", **kwargs))
    assert second.success is True
    assert second.diff == ""  # 沒有實質變更

    log_after = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    assert log_after == log_before  # 沒有建立新 commit

    git_ops.check_clean_working_tree(str(tmp_path))  # 不應拋出：working tree 仍乾淨


def test_fill_function_missing_target_file_returns_scaffold_mismatch(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "placeholder.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, text=True, check=True)

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_001",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="d",
            context="",
            context_files=["app/repositories/user_repository.py"],
        )
    )
    assert result.success is False
    assert "scaffold/task 不一致" in result.error


def test_fill_function_missing_function_returns_scaffold_mismatch(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_002",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="delete",  # 骨架裡不存在的函式
            description="d",
            context="",
            context_files=["app/repositories/user_repository.py"],
        )
    )
    assert result.success is False
    assert "scaffold/task 不一致" in result.error


def test_fill_function_context_files_missing_extra_file_does_not_abort(tmp_path, monkeypatch):
    # 07a 七章「context_files 讀取容錯」：context_files[0] 之外的檔案允許
    # 不存在（例如沒有對應 DB 表的模組），跳過不中斷。
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    monkeypatch.setattr(ollama_client, "get_function_body", _fake_get_function_body)

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_003",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="d",
            context="",
            context_files=["app/repositories/user_repository.py", "app/models/user.py"],  # user.py 不存在
        )
    )
    assert result.success is True


def test_fill_function_context_files_permission_error_returns_failure(tmp_path, monkeypatch):
    # 07a 七章「context_files 讀取容錯」只涵蓋 FileNotFoundError；其餘
    # OSError（權限問題等）是真實環境錯誤，_read_context_files() 不吞，
    # 這裡驗證 fill_function() 把它轉成 FillResult(success=False)，不
    # 讓原生例外洩漏。
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    monkeypatch.setattr(ollama_client, "get_function_body", _fake_get_function_body)

    original_read_text = Path.read_text

    def failing_read_text(self, encoding=None, **kwargs):
        if self.name == "user.py":
            raise PermissionError("模擬權限不足")
        return original_read_text(self, encoding=encoding, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_009",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="d",
            context="",
            context_files=["app/repositories/user_repository.py", "app/models/user.py"],
        )
    )

    assert result.success is False
    assert "context_files" in result.error


def test_fill_function_detects_dirty_tree_that_appeared_during_llm_wait(tmp_path, monkeypatch):
    # 模擬人工在「等待模型回應」這段漫長 I/O 空窗期間，手動改了目標
    # 檔案（尚未 commit）——precondition 檢查若只在函式入口做一次，這
    # 個修改會在模型回應後被無聲覆蓋掉。client.py 在 write_text() 前
    # 補了第二次 check_clean_working_tree()，這裡驗證它確實生效：
    # 呼叫失敗、且人工的修改沒有被蓋掉。
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    target = tmp_path / "app" / "repositories" / "user_repository.py"

    async def fake_get_function_body_with_concurrent_human_edit(**kwargs):
        target.write_text(
            target.read_text(encoding="utf-8").replace("pass", "return 999  # 人工緊急修正"),
            encoding="utf-8",
        )
        return "return user_id * 2\n"

    monkeypatch.setattr(ollama_client, "get_function_body", fake_get_function_body_with_concurrent_human_edit)

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_005",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="回傳 user_id 乘以 2",
            context="",
            context_files=["app/repositories/user_repository.py"],
        )
    )

    assert result.success is False
    assert "working tree" in result.error
    assert "人工緊急修正" in target.read_text(encoding="utf-8")  # 人工修改沒有被覆蓋


def test_fill_function_dirty_working_tree_refuses_to_write(tmp_path):
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    (tmp_path / "stray.txt").write_text("oops", encoding="utf-8")

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_004",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="d",
            context="",
            context_files=["app/repositories/user_repository.py"],
        )
    )
    assert result.success is False
    assert "working tree" in result.error
    # 不寫入任何內容：目標檔案內容應維持骨架階段的 pass
    written = (tmp_path / "app" / "repositories" / "user_repository.py").read_text(encoding="utf-8")
    assert "pass" in written


def test_generate_scaffold_rolls_back_on_commit_failure(tmp_path, monkeypatch):
    # commit_scaffold() 因不可抗力（index lock 等）失敗時，write_files()
    # 已經落地的內容不能留在磁碟上讓 working tree 卡住——驗證會自動
    # 呼叫 discard_written_files() 還原，下一次呼叫的 precondition 檢查
    # 才不會被這次失敗永久卡住。
    _init_repo(tmp_path)

    def fake_commit_scaffold(python_project_path):
        raise TranslatorCliError("模擬 git commit 失敗（例如 index lock）")

    monkeypatch.setattr(git_ops, "commit_scaffold", fake_commit_scaffold)

    result = asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    assert result["success"] is False
    assert "commit" in result["error"]
    assert not (tmp_path / "app" / "repositories" / "user_repository.py").exists()
    git_ops.check_clean_working_tree(str(tmp_path))  # 不應拋出：已還原乾淨


def test_generate_scaffold_rolls_back_on_write_oserror(tmp_path, monkeypatch):
    # write_files() 寫到一半遇到 OSError（模擬磁碟空間不足／權限問題）
    # 時，已經寫入一半的檔案同樣不能留下讓 working tree 卡住。
    _init_repo(tmp_path)

    def fake_write_files(python_project_path, files):
        partial_path = Path(python_project_path) / "app" / "core" / "database.py"
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path.write_text("DATABASE_URL = 1\n", encoding="utf-8")
        raise OSError("模擬磁碟寫入失敗")

    monkeypatch.setattr(scaffold, "write_files", fake_write_files)

    result = asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    assert result["success"] is False
    assert "磁碟寫入失敗" in result["error"]
    git_ops.check_clean_working_tree(str(tmp_path))  # 不應拋出：已還原乾淨


def test_fill_function_rolls_back_on_commit_failure(tmp_path, monkeypatch):
    # commit_fill() 因不可抗力失敗時，write_text() 已經落地的新內容
    # 不能留在磁碟上讓 working tree 卡住——驗證會自動還原成骨架階段的
    # 內容，且只讓這一個 task 失敗，不影響後續呼叫的 precondition 檢查。
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    monkeypatch.setattr(ollama_client, "get_function_body", _fake_get_function_body)

    def fake_commit_fill(python_project_path, **kwargs):
        raise TranslatorCliError("模擬 git commit 失敗")

    monkeypatch.setattr(git_ops, "commit_fill", fake_commit_fill)

    target = tmp_path / "app" / "repositories" / "user_repository.py"
    original_content = target.read_text(encoding="utf-8")

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_006",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="回傳 user_id 乘以 2",
            context="",
            context_files=["app/repositories/user_repository.py"],
        )
    )

    assert result.success is False
    assert "commit" in result.error
    assert target.read_text(encoding="utf-8") == original_content  # 已還原回骨架內容
    git_ops.check_clean_working_tree(str(tmp_path))  # 不應拋出：已還原乾淨


def test_fill_function_rolls_back_on_format_failure(tmp_path, monkeypatch):
    # format_paths() 是硬性依賴，失敗時必須還原 write_text() 已經落地的
    # 內容，不能讓半格式化的檔案留在磁碟上——驗證走的是 discard_file_changes()
    # 而不是 commit 失敗那條路徑（commit_fill() 根本不會被呼叫到）。
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    monkeypatch.setattr(ollama_client, "get_function_body", _fake_get_function_body)

    def failing_format_paths(python_project_path, *relative_paths):
        raise TranslatorCliError("模擬 ruff format 失敗")

    monkeypatch.setattr(formatting, "format_paths", failing_format_paths)

    target = tmp_path / "app" / "repositories" / "user_repository.py"
    original_content = target.read_text(encoding="utf-8")

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_008",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="回傳 user_id 乘以 2",
            context="",
            context_files=["app/repositories/user_repository.py"],
        )
    )

    assert result.success is False
    assert "格式化" in result.error
    assert target.read_text(encoding="utf-8") == original_content  # 已還原回骨架內容
    git_ops.check_clean_working_tree(str(tmp_path))  # 不應拋出：已還原乾淨


def test_fill_function_write_oserror_returns_failure(tmp_path, monkeypatch):
    # write_text() 拋出 OSError（模擬磁碟空間不足／檔案被鎖定）時，
    # 不能讓底層例外直接洩漏出 fill_function() 的 FillResult 合約。
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    monkeypatch.setattr(ollama_client, "get_function_body", _fake_get_function_body)

    original_write_text = Path.write_text

    def failing_write_text(self, content, encoding=None, **kwargs):
        if self.name == "user_repository.py":
            raise OSError("模擬磁碟寫入失敗")
        return original_write_text(self, content, encoding=encoding, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write_text)

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_007",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="d",
            context="",
            context_files=["app/repositories/user_repository.py"],
        )
    )

    assert result.success is False
    assert "寫入" in result.error


def test_read_target_file_raises_scaffold_mismatch_on_missing_file(tmp_path):
    # 對應 07a 六章步驟 1「scaffold/task 不一致」：驗證 _read_target_file()
    # 內部確實 raise TranslatorCliScaffoldMismatchError，不是繞過例外
    # 階層直接組字串（見一章「TranslatorCliScaffoldMismatchError」對這個
    # 一致性的說明）。
    with pytest.raises(TranslatorCliScaffoldMismatchError, match="不存在"):
        client._read_target_file(tmp_path, "app/repositories/does_not_exist.py")


def test_fill_function_missing_ollama_env_vars_returns_failure_not_crash(tmp_path, monkeypatch):
    # OLLAMA_BASE_URL／OLLAMA_API_KEY 缺失時，ollama_client._call_ollama_once()
    # 會拋 TranslatorCliConfigError（見該模組），這裡驗證 fill_function()
    # 正確接住、轉成 FillResult(success=False)，不是原生例外洩漏擊穿合約。
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_008",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="d",
            context="",
            context_files=["app/repositories/user_repository.py"],
        )
    )

    assert result.success is False
    assert "OLLAMA_BASE_URL" in result.error
