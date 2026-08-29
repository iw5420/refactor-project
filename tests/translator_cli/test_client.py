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
from translator_cli.exceptions import (
    TranslatorCliError,
    TranslatorCliNetworkError,
    TranslatorCliScaffoldMismatchError,
    TranslatorCliUpstreamDegradedError,
)
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


def test_fill_function_with_fixed_body_skips_ollama_call(tmp_path, monkeypatch):
    """對應 10a 八章「⑦ 直接產生修正後程式碼」：`fixed_body` 給定時，
    完全不呼叫 ollama_client.get_function_body()（monkeypatch 成會炸的
    版本，確認真的沒被叫到），直接用 fixed_body 當本體寫入。
    """
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    async def _boom(**kwargs):
        raise AssertionError("fixed_body 給定時不應該呼叫 ollama_client.get_function_body()")

    monkeypatch.setattr(ollama_client, "get_function_body", _boom)

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
            fixed_body="return user_id * 3\n",
        )
    )

    assert result.success is True
    assert "user_id * 3" in result.diff

    written = (tmp_path / "app" / "repositories" / "user_repository.py").read_text(encoding="utf-8")
    assert "return user_id * 3" in written


def test_fill_function_with_fixed_body_invalid_syntax_returns_failure(tmp_path, monkeypatch):
    """⑦ 給的 fixed_body 一樣要通過既有的語法驗證關卡（見
    extract_body_statements()），不因為來源是 Claude 而放寬。"""
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    async def _boom(**kwargs):
        raise AssertionError("fixed_body 給定時不應該呼叫 ollama_client.get_function_body()")

    monkeypatch.setattr(ollama_client, "get_function_body", _boom)

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
            fixed_body="return user_id *\n",
        )
    )

    assert result.success is False


def test_fill_function_with_fixed_body_nested_same_name_def_rejected_and_logged(tmp_path, monkeypatch, caplog):
    """對應 docs/09b_bug_trace.md #52 真實案例：⑦ 給的 fixed_body 混雜了
    import／裝飾器等其他陳述式，同名巢狀函式定義只是其中一筆——這種
    fixed_body 必須被 extract_body_statements() 的巢狀同名函式檢查擋
    下來（見 translator_cli/python_adapter.py 的放寬版判斷），不能被
    當成合法陳述式插入，且要在 log 留下明確紀錄（見 client.py 新增的
    logger.warning，這條記錄跟一般 fill_failed 的路徑分開，明講是
    fixed_body 格式契約被違反，不是本地模型的一般格式錯誤）。"""
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    async def _boom(**kwargs):
        raise AssertionError("fixed_body 給定時不應該呼叫 ollama_client.get_function_body()")

    monkeypatch.setattr(ollama_client, "get_function_body", _boom)

    bad_fixed_body = (
        "import os\n"
        "\n"
        "def get_by_id(user_id: int) -> int:\n"
        "    return user_id * 2\n"
    )

    with caplog.at_level("WARNING"):
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
                fixed_body=bad_fixed_body,
            )
        )

    assert result.success is False
    assert "模型重複輸出函式簽名" in result.error
    assert any("task_001" in record.message and "#52" in record.message for record in caplog.records)


def test_apply_file_fix_replaces_unique_snippet_and_commits(tmp_path):
    """對應 10a 八章「phase 2：檔案層級修正」：⑦ 給的 file_fix 精確
    字串替換，不經過 fill_function() 的 AST 函式定位機制。
    """
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    result = asyncio.run(
        client.apply_file_fix(
            str(tmp_path),
            task_id="task_r2",
            target_file="app/core/database.py",
            old_snippet="DATABASE_URL = 1",
            new_snippet="DATABASE_URL = 2",
        )
    )

    assert result.success is True
    assert "DATABASE_URL = 2" in result.diff

    written = (tmp_path / "app" / "core" / "database.py").read_text(encoding="utf-8")
    assert "DATABASE_URL = 2" in written

    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert log.stdout.strip() == "debug: task_r2 apply file-level fix in app/core/database.py"


def test_apply_file_fix_snippet_not_found_returns_failure(tmp_path):
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    result = asyncio.run(
        client.apply_file_fix(
            str(tmp_path),
            task_id="task_r2",
            target_file="app/core/database.py",
            old_snippet="THIS_DOES_NOT_EXIST = 1",
            new_snippet="DATABASE_URL = 2",
        )
    )

    assert result.success is False
    assert "找不到" in result.error


def test_apply_file_fix_snippet_appears_multiple_times_returns_failure(tmp_path):
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))
    (tmp_path / "app" / "core" / "database.py").write_text(
        "DATABASE_URL = 1\nDATABASE_URL = 1\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "setup duplicate"], cwd=tmp_path, capture_output=True, text=True, check=True)

    result = asyncio.run(
        client.apply_file_fix(
            str(tmp_path),
            task_id="task_r2",
            target_file="app/core/database.py",
            old_snippet="DATABASE_URL = 1",
            new_snippet="DATABASE_URL = 2",
        )
    )

    assert result.success is False
    assert "2 次" in result.error


def test_apply_file_fix_invalid_syntax_returns_failure(tmp_path):
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    result = asyncio.run(
        client.apply_file_fix(
            str(tmp_path),
            task_id="task_r2",
            target_file="app/core/database.py",
            old_snippet="DATABASE_URL = 1",
            new_snippet="DATABASE_URL = ***",
        )
    )

    assert result.success is False


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


def test_fill_function_marks_upstream_degraded_on_that_specific_error(tmp_path, monkeypatch):
    """對應 docs/09b_bug_trace.md #35：get_function_body() 拋出
    TranslatorCliUpstreamDegradedError（連續多個 task 都在傳輸層失敗）
    時，fill_function() 除了照常轉成 FillResult(success=False)，還要把
    upstream_degraded=True 一併帶出來，讓 implement_node.py 能提早停止
    重試，不是每個 task 各自燒完重試預算才發現同一個根因。"""
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    async def _raise_upstream_degraded(**kwargs):
        raise TranslatorCliUpstreamDegradedError("連續 3 次都在傳輸層失敗")

    monkeypatch.setattr(ollama_client, "get_function_body", _raise_upstream_degraded)

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_001",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="d",
            context="",
            context_files=[],
        )
    )

    assert result.success is False
    assert result.upstream_degraded is True


def test_fill_function_does_not_mark_upstream_degraded_on_plain_network_error(tmp_path, monkeypatch):
    """普通的 TranslatorCliNetworkError（單次呼叫的網路層重試耗盡，還沒
    累積到跨 task 的連續失敗門檻）不該被誤標成 upstream_degraded。"""
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    async def _raise_plain_network_error(**kwargs):
        raise TranslatorCliNetworkError("這次剛好連不上")

    monkeypatch.setattr(ollama_client, "get_function_body", _raise_plain_network_error)

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_001",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="d",
            context="",
            context_files=[],
        )
    )

    assert result.success is False
    assert result.upstream_degraded is False


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


class TestTrimContextFilesIfOversized:
    """對應 09b_bug_trace.md #37：context 過大時的裁減，只在超過門檻時
    才動手，門檻值取自真實環境量化資料（成功呼叫最高 12942 bytes、
    三次 attempt 全失敗的呼叫最低 15590 bytes）。
    """

    _BIG_FUNCTION = "\n".join(
        f"    def with_{name}(self, {name}: str) -> object:\n"
        f"        validator = ValidationUtil()\n"
        f"        if validator.is_valid_field({name}):\n"
        f"            return Specification(lambda x: x.{name} == {name})\n"
        f"        else:\n"
        f"            return conjunction()\n"
        for name in ("year", "grade", "classes", "kind", "status", "card", "random_id")
    )

    def test_below_threshold_returns_unchanged(self, monkeypatch):
        monkeypatch.setattr(client, "TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES", 100_000)
        context_files = [("app/services/foo.py", "class Foo:\n    def bar(self): pass\n")]
        result = client._trim_context_files_if_oversized(context_files, task_id="task_x")
        assert result == context_files

    def test_above_threshold_strips_function_bodies(self, monkeypatch):
        monkeypatch.setattr(client, "TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES", 100)
        source = "class ExamSpecification:\n" + self._BIG_FUNCTION
        context_files = [("app/repositories/exam_repository.py", source)]
        result = client._trim_context_files_if_oversized(context_files, task_id="task_x")
        assert len(result) == 1
        path, trimmed = result[0]
        assert path == "app/repositories/exam_repository.py"
        assert "return Specification(lambda x: x.year == year)" not in trimmed
        assert "def with_year(self, year: str) -> object:" in trimmed
        ast.parse(trimmed)  # 裁減後仍是合法 Python

    def test_threshold_measured_against_total_not_per_file(self, monkeypatch):
        # 五個各自不大的檔案，加總超過門檻時仍要觸發裁減——真實案例
        # 就是這種「單一檔案不算大，加總才過大」的情況。
        monkeypatch.setattr(client, "TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES", 150)
        small_file = "class Foo:\n    def bar(self): return 1\n"
        context_files = [(f"app/m{i}.py", small_file) for i in range(5)]
        total_before = sum(len(c.encode("utf-8")) for _, c in context_files)
        assert total_before > 150
        result = client._trim_context_files_if_oversized(context_files, task_id="task_x")
        assert all("return 1" not in content for _, content in result)

    def test_unparseable_file_falls_back_to_original_content(self, monkeypatch):
        monkeypatch.setattr(client, "TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES", 10)
        bad_source = "def (:\n"  # 不合法 Python，理論上不該發生，但裁減本身不能崩潰
        context_files = [("app/broken.py", bad_source)]
        result = client._trim_context_files_if_oversized(context_files, task_id="task_x")
        assert result == [("app/broken.py", bad_source)]

    def test_schema_path_uses_class_filtering_not_body_stripping(self, monkeypatch):
        """對應 09b_bug_trace.md #45：app/schemas/ 底下的檔案沒有函式
        本體可以剝，strip_all_function_bodies() 對這種檔案是空操作——
        改用 extract_referenced_classes() 依簽名/description 裡出現過的
        class 名稱過濾，真的能把無關的 class 砍掉。"""
        monkeypatch.setattr(client, "TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES", 10)
        source = (
            "class GetExamRq(BaseModel):\n    year: str | None\n\n\n"
            "class UnrelatedRs(BaseModel):\n    other: str | None\n"
        )
        context_files = [("app/schemas/exam.py", source)]
        result = client._trim_context_files_if_oversized(
            context_files, task_id="task_x", current_signature="def search(self, rq: GetExamRq) -> None:",
        )
        assert len(result) == 1
        _, trimmed = result[0]
        assert "GetExamRq" in trimmed
        assert "UnrelatedRs" not in trimmed

    def test_models_path_also_uses_class_filtering(self, monkeypatch):
        monkeypatch.setattr(client, "TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES", 10)
        source = "class ExamEntity(Base):\n    id: int\n\n\nclass OtherEntity(Base):\n    id: int\n"
        context_files = [("app/models/exam.py", source)]
        result = client._trim_context_files_if_oversized(
            context_files, task_id="task_x", description="更新至 ExamEntity 並儲存",
        )
        _, trimmed = result[0]
        assert "ExamEntity" in trimmed
        assert "OtherEntity" not in trimmed

    def test_schema_path_with_no_matching_signal_keeps_full_content(self, monkeypatch):
        # 抓不到任何候選名稱時（simple 的 str/int 簽名、description 也沒
        # 提到任何 class 名稱），extract_referenced_classes() 內建的
        # 「找不到交集就原樣回傳」保守處理會生效，不強行砍到空。
        monkeypatch.setattr(client, "TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES", 10)
        source = "class GetExamRq(BaseModel):\n    year: str | None\n"
        context_files = [("app/schemas/exam.py", source)]
        result = client._trim_context_files_if_oversized(
            context_files, task_id="task_x", current_signature="def f(self, x: str) -> str:",
        )
        assert result == context_files


class TestReferencedClassNames:
    """對應 docs/09b_bug_trace.md #45：從函式簽名／description／context
    這些文字裡粗略抓出可能是 class 名稱的候選字（大寫開頭的識別字）。"""

    def test_extracts_from_signature_type_hints(self):
        names = client._referenced_class_names("def create_random(self, card: str) -> Result[CreaterandomRs]:")
        assert "CreaterandomRs" in names
        assert "Result" in names

    def test_extracts_from_description_text(self):
        names = client._referenced_class_names("", "更新至 ExamEntity 並將狀態設為 '1'")
        assert "ExamEntity" in names

    def test_lowercase_words_not_matched(self):
        names = client._referenced_class_names("def create_random(self, card: str, db: Session):")
        assert "card" not in names
        assert "self" not in names

    def test_combines_multiple_text_sources(self):
        names = client._referenced_class_names("def f() -> FooRs:", "見 BarRq 的說明")
        assert {"FooRs", "BarRq"} <= names


def test_fill_function_trims_oversized_context_before_calling_ollama(tmp_path, monkeypatch):
    # 端對端驗證：真的超過門檻時，get_function_body() 收到的 context_files
    # 已經被裁減過，不是原始的巨大內容（見 09b_bug_trace.md #37）。
    monkeypatch.setattr(client, "TRANSLATOR_CLI_CONTEXT_TRIM_THRESHOLD_BYTES", 100)
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    bloated_source = (
        "class UserRepository:\n"
        "    def get_by_id(self, user_id: int) -> int:\n"
        "        pass\n"
        "    def get_all(self) -> list[int]:\n"
        "        return [x for x in range(1000) if x % 2 == 0 and x % 3 == 0]\n"
        "    def get_by_name(self, name: str) -> int | None:\n"
        "        return db.query(User).filter(User.name == name).one_or_none()\n"
    )
    (tmp_path / "app" / "repositories" / "user_repository.py").write_text(bloated_source, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "test: bloat user_repository.py"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )

    captured = {}

    async def fake_get_function_body(**kwargs):
        captured["context_files"] = kwargs["context_files"]
        return "return user_id\n"

    monkeypatch.setattr(ollama_client, "get_function_body", fake_get_function_body)

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

    assert result.success is True
    received_content = dict(captured["context_files"])["app/repositories/user_repository.py"]
    assert "range(1000)" not in received_content
    assert "def get_all(self) -> list[int]:" in received_content


class TestReadContextFilesReferencedFunctions:
    """對應 06a 七章新設計「referenced_interfaces 函式層級抽取」，見
    docs/09b_bug_trace.md #37 根因。"""

    _EXAM_REPO_SOURCE = (
        "class ExamRepository:\n"
        "    def find_by_kind(self, kind: str) -> object:\n"
        "        pass\n"
        "    def find_by_card(self, card: str) -> object | None:\n"
        "        return db.query(1).filter(2).one_or_none()\n\n\n"
        "class ExamSpecification:\n"
        "    def with_year(self, year: str) -> object:\n"
        "        return Specification(lambda x: x.year == year)\n"
    )

    def test_file_with_referenced_functions_entry_gets_precisely_extracted(self, tmp_path):
        (tmp_path / "exam_repository.py").write_text(self._EXAM_REPO_SOURCE, encoding="utf-8")
        result = client._read_context_files(
            tmp_path, ["exam_repository.py"], task_id="task_x",
            referenced_functions=[("exam_repository.py", "ExamRepository", "find_by_card")],
        )
        content = dict(result)["exam_repository.py"]
        assert "find_by_card" in content
        assert "find_by_kind" not in content
        assert "ExamSpecification" not in content

    def test_file_without_referenced_functions_entry_stays_full(self, tmp_path):
        # 沒有對應項目的檔案（例如 target_files[0] 自己、schemas／models）
        # 維持整份帶入，這是「沒問題的 prompts 還是沒問題」的核心保證。
        (tmp_path / "exam_repository.py").write_text(self._EXAM_REPO_SOURCE, encoding="utf-8")
        result = client._read_context_files(
            tmp_path, ["exam_repository.py"], task_id="task_x",
            referenced_functions=[("some_other_file.py", "Foo", "bar")],
        )
        content = dict(result)["exam_repository.py"]
        assert "find_by_kind" in content
        assert "find_by_card" in content
        assert "ExamSpecification" in content

    def test_none_referenced_functions_behaves_like_before(self, tmp_path):
        (tmp_path / "exam_repository.py").write_text(self._EXAM_REPO_SOURCE, encoding="utf-8")
        result = client._read_context_files(tmp_path, ["exam_repository.py"], task_id="task_x")
        content = dict(result)["exam_repository.py"]
        assert content == self._EXAM_REPO_SOURCE

    def test_multiple_files_only_matching_ones_get_extracted(self, tmp_path):
        (tmp_path / "exam_repository.py").write_text(self._EXAM_REPO_SOURCE, encoding="utf-8")
        (tmp_path / "schemas_exam.py").write_text("class ExamRs(object):\n    id: int\n", encoding="utf-8")
        result = client._read_context_files(
            tmp_path, ["exam_repository.py", "schemas_exam.py"], task_id="task_x",
            referenced_functions=[("exam_repository.py", "ExamRepository", "find_by_card")],
        )
        by_path = dict(result)
        assert "find_by_kind" not in by_path["exam_repository.py"]
        assert by_path["schemas_exam.py"] == "class ExamRs(object):\n    id: int\n"

    def test_unparseable_file_falls_back_to_full_content(self, tmp_path):
        (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")
        result = client._read_context_files(
            tmp_path, ["broken.py"], task_id="task_x",
            referenced_functions=[("broken.py", None, "whatever")],
        )
        assert dict(result)["broken.py"] == "def (:\n"


def test_fill_function_passes_referenced_functions_through_to_context_extraction(tmp_path, monkeypatch):
    # 端對端驗證：referenced_functions 真的從 fill_function() 一路傳到
    # get_function_body() 收到的 context_files，且精準抽取生效。
    _init_repo(tmp_path)
    asyncio.run(client.generate_scaffold(str(tmp_path), _python_structure()))

    exam_repo_source = (
        "class ExamRepository:\n"
        "    def find_by_kind(self, kind: str) -> object:\n"
        "        pass\n"
        "    def find_by_card(self, card: str) -> object | None:\n"
        "        return db.query(1).filter(2).one_or_none()\n"
    )
    (tmp_path / "app" / "repositories").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "repositories" / "exam_repository.py").write_text(exam_repo_source, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "test: add exam_repository.py"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )

    captured = {}

    async def fake_get_function_body(**kwargs):
        captured["context_files"] = kwargs["context_files"]
        return "return user_id\n"

    monkeypatch.setattr(ollama_client, "get_function_body", fake_get_function_body)

    result = asyncio.run(
        client.fill_function(
            str(tmp_path),
            task_id="task_001",
            target_file="app/repositories/user_repository.py",
            class_name="UserRepository",
            function_name="get_by_id",
            description="d",
            context="",
            context_files=["app/repositories/user_repository.py", "app/repositories/exam_repository.py"],
            referenced_functions=[("app/repositories/exam_repository.py", "ExamRepository", "find_by_card")],
        )
    )

    assert result.success is True
    received = dict(captured["context_files"])
    assert "find_by_card" in received["app/repositories/exam_repository.py"]
    assert "find_by_kind" not in received["app/repositories/exam_repository.py"]
    # target_files[0]（自己的檔案）不受影響，維持完整內容。
    assert "def get_by_id" in received["app/repositories/user_repository.py"]
