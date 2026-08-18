"""python_service/reload_probe.py，對應 09a 三章「這個 .gitignore 修正」
「探測端點讀取 token 檔案時要容忍檔案還不存在」。用真實 git repo
（tmp_path + subprocess 呼叫）驗證，比照 tests/translator_cli/test_git_ops.py
既有慣例。
"""
import subprocess

import pytest

from translator_cli.exceptions import TranslatorCliNotGitRepoError
from python_service.reload_probe import (
    TOKEN_FILE_NAME,
    WRAPPER_FILE_NAME,
    ensure_reload_probe_infra,
)


def _init_repo(path):
    subprocess.run(["git", "init"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, capture_output=True, text=True, check=True)


def _git_log_count(path) -> int:
    result = subprocess.run(
        ["git", "-C", str(path), "log", "--oneline"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return 0  # 尚無任何 commit
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _git_status_porcelain(path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True, check=True
    )
    return result.stdout


def test_raises_when_not_a_git_repo(tmp_path):
    with pytest.raises(TranslatorCliNotGitRepoError):
        ensure_reload_probe_infra(str(tmp_path))


def test_first_call_creates_gitignore_and_first_commit(tmp_path):
    _init_repo(tmp_path)

    ensure_reload_probe_infra(str(tmp_path))

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert WRAPPER_FILE_NAME in gitignore.splitlines()
    assert TOKEN_FILE_NAME in gitignore.splitlines()
    assert "__pycache__/" in gitignore.splitlines()
    assert _git_log_count(tmp_path) == 1
    # wrapper 檔案本身已寫入磁碟，但因為被 .gitignore 排除，working tree 仍乾淨
    assert (tmp_path / WRAPPER_FILE_NAME).exists()
    assert _git_status_porcelain(tmp_path) == ""


def test_idempotent_second_call_does_not_create_extra_commit(tmp_path):
    _init_repo(tmp_path)

    ensure_reload_probe_infra(str(tmp_path))
    first_wrapper_content = (tmp_path / WRAPPER_FILE_NAME).read_text(encoding="utf-8")

    ensure_reload_probe_infra(str(tmp_path))

    assert _git_log_count(tmp_path) == 1  # 沒有因為第二次呼叫多出一個 commit
    assert _git_status_porcelain(tmp_path) == ""
    # wrapper 內容固定，每次都覆寫，第二次呼叫後內容仍然相同
    assert (tmp_path / WRAPPER_FILE_NAME).read_text(encoding="utf-8") == first_wrapper_content


def test_precedes_scaffold_commit_working_tree_stays_clean_for_precondition_check(tmp_path):
    """對應 09a 三章：這個初始 commit 必須早於 generate_scaffold() 的骨架
    commit，且完成後 working tree 必須乾淨，否則 07a 九章的
    check_clean_working_tree() precondition 檢查會誤判成「working tree
    不乾淨」而拒絕寫入。這裡直接複用 translator_cli 自己的檢查函式驗證。
    """
    from translator_cli.git_ops import check_clean_working_tree

    _init_repo(tmp_path)
    ensure_reload_probe_infra(str(tmp_path))

    check_clean_working_tree(str(tmp_path))  # 不應拋出


def test_pycache_from_running_container_does_not_dirty_working_tree(tmp_path):
    """對應真實案例：implement 期間容器（python_service/process.py）透過
    bind mount 常駐執行 uvicorn --reload，CPython import 時會在磁碟上
    寫出 __pycache__/*.pyc——這些是容器裡的行程寫的，不是任何一次
    fill_function()／generate_scaffold() 自己的動作。若 .gitignore 沒
    排除它，check_clean_working_tree() 會誤判 working tree 不乾淨，導致
    同一輪 implement 迴圈裡後續每一個 task 都直接失敗（已用真實案例
    重現：exam-platform-api 對 72 個 task 跑 implement_node.run() 時，
    前兩個 task 就因為這個原因失敗，見 docs/09b_bug_trace.md）。
    """
    from translator_cli.git_ops import check_clean_working_tree

    _init_repo(tmp_path)
    ensure_reload_probe_infra(str(tmp_path))

    # 模擬容器 import 模組時，在磁碟上寫出 __pycache__（不透過任何
    # translator-cli 或 python_service 自己的 API，直接模擬外部行程
    # 造成的磁碟變化）。
    pycache_dir = tmp_path / "app" / "__pycache__"
    pycache_dir.mkdir(parents=True)
    (pycache_dir / "main.cpython-312.pyc").write_bytes(b"\x00\x01\x02")

    check_clean_working_tree(str(tmp_path))  # 不應拋出
