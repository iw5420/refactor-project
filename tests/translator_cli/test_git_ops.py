"""translator_cli/git_ops.py，對應 07a 八、九章。用真實 git repo（tmp_path
＋ subprocess 呼叫）驗證，不 mock subprocess——git 指令契約（`-C`、
`--porcelain` 輸出格式）本身就是這個模組唯一要保證正確的行為。
"""
import subprocess

import pytest

from translator_cli.exceptions import TranslatorCliDirtyWorkingTreeError, TranslatorCliError, TranslatorCliNotGitRepoError
from translator_cli.git_ops import (
    check_clean_working_tree,
    commit_fill,
    commit_scaffold,
    diff_for_file,
    discard_file_changes,
    discard_written_files,
    ensure_git_repo,
    reset_python_project_dir,
    rollback_python_project_dir,
)


def _init_repo(path):
    subprocess.run(["git", "init"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, capture_output=True, text=True, check=True)


class TestResetPythonProjectDir:
    """對應 docs/refactor_bug_trace.md #28／#29：每輪 pipeline 開始前
    重置生成專案目錄，避免上一輪殘留檔案在模組切法改變時變成孤兒或
    真正衝突。"""

    def test_creates_fresh_git_repo_when_target_does_not_exist_yet(self, tmp_path):
        target = tmp_path / "exam-platform-api"
        backup_path = reset_python_project_dir(str(target), run_id="20260906_000000_abcdef")

        assert target.is_dir()
        assert backup_path is None  # 沒有東西可備份
        ensure_git_repo(str(target))  # 不應拋出

    def test_existing_dir_is_renamed_to_timestamped_backup_not_deleted(self, tmp_path):
        target = tmp_path / "exam-platform-api"
        target.mkdir()
        (target / "leftover.py").write_text("stale = True\n", encoding="utf-8")

        backup_path = reset_python_project_dir(str(target), run_id="20260906_010203_abcdef")

        backup = tmp_path / "exam-platform-api.bak-20260906_010203_abcdef"
        # 舊內容原封不動保留在備份資料夾，沒有被刪除。
        assert backup.is_dir()
        assert (backup / "leftover.py").read_text(encoding="utf-8") == "stale = True\n"
        assert backup_path == backup
        # 這一輪拿到的是全新、乾淨的空目錄，看不到任何舊檔案。
        assert target.is_dir()
        assert not (target / "leftover.py").exists()
        ensure_git_repo(str(target))  # 新目錄已經 git init 過，不應拋出

    def test_backup_folder_name_uses_the_run_id_passed_in(self, tmp_path):
        """備份資料夾命名沿用呼叫端傳入的 run_id，不在函式內部另外
        產生一個新的時間戳記——確保跟同一輪其他 log／report 用同一個
        run_id 互相對應。"""
        target = tmp_path / "exam-platform-api"
        target.mkdir()

        reset_python_project_dir(str(target), run_id="20260906_161715_4b2116")

        assert (tmp_path / "exam-platform-api.bak-20260906_161715_4b2116").is_dir()


class TestRollbackPythonProjectDir:
    """對應 docs/refactor_bug_trace.md #31：這一輪 pipeline 若整個
    crash（連 write_run_report() 都沒執行到），reset_python_project_dir()
    建立的新目錄只是半成品，不該取代上一輪的真實產物——刪掉半成品、把
    備份還原回來。"""

    def test_restores_backup_and_removes_half_finished_new_dir(self, tmp_path):
        target = tmp_path / "exam-platform-api"
        target.mkdir()
        (target / "real_work.py").write_text("real = True\n", encoding="utf-8")

        backup_path = reset_python_project_dir(str(target), run_id="20260906_010203_abcdef")
        # 模擬這一輪 pipeline 才剛起步就 crash，新目錄裡只有極少量半成品內容。
        (target / "half_finished.py").write_text("half = True\n", encoding="utf-8")

        rollback_python_project_dir(str(target), backup_path)

        # 半成品不見了，目錄內容是上一輪的真實產物，不是半成品也不是空的。
        assert target.is_dir()
        assert (target / "real_work.py").read_text(encoding="utf-8") == "real = True\n"
        assert not (target / "half_finished.py").exists()
        assert not backup_path.exists()  # 備份已經改名還原回來，原路徑不再存在

    def test_no_backup_to_restore_just_removes_half_finished_dir(self, tmp_path):
        """這一輪開始前 python_project_path 本來就不存在（第一次跑），
        reset_python_project_dir() 回傳 None——rollback 時只需要刪掉
        這次的半成品，沒有東西可還原。"""
        target = tmp_path / "exam-platform-api"
        backup_path = reset_python_project_dir(str(target), run_id="20260906_000000_abcdef")
        (target / "half_finished.py").write_text("half = True\n", encoding="utf-8")

        rollback_python_project_dir(str(target), backup_path)

        assert not target.exists()

    def test_no_op_when_neither_target_nor_backup_exists(self, tmp_path):
        target = tmp_path / "exam-platform-api"
        rollback_python_project_dir(str(target), None)  # 不應拋出
        assert not target.exists()

    def test_restore_succeeds_even_if_deleting_half_finished_dir_fails(self, tmp_path, monkeypatch, caplog):
        """對應真實案例 20260906_152648_d36540：Windows 上剛 git init
        產生的 .git 內部檔案短暫鎖定，shutil.rmtree() 丟出
        PermissionError——還原備份不能因此連帶失敗，半成品目錄改名
        讓出路徑之後，刪不掉只記警告即可。"""
        target = tmp_path / "exam-platform-api"
        target.mkdir()
        (target / "real_work.py").write_text("real = True\n", encoding="utf-8")

        backup_path = reset_python_project_dir(str(target), run_id="20260906_152648_d36540")
        (target / "half_finished.py").write_text("half = True\n", encoding="utf-8")

        def fail_rmtree(path, *args, **kwargs):
            raise PermissionError(f"[WinError 5] simulated lock on {path}")

        monkeypatch.setattr("translator_cli.git_ops.shutil.rmtree", fail_rmtree)

        with caplog.at_level("WARNING"):
            rollback_python_project_dir(str(target), backup_path)

        assert target.is_dir()
        assert (target / "real_work.py").read_text(encoding="utf-8") == "real = True\n"
        assert not backup_path.exists()
        assert "刪除半成品目錄" in caplog.text


def test_ensure_git_repo_raises_on_missing_dir(tmp_path):
    with pytest.raises(TranslatorCliNotGitRepoError):
        ensure_git_repo(str(tmp_path / "does-not-exist"))


def test_ensure_git_repo_raises_on_non_repo_dir(tmp_path):
    with pytest.raises(TranslatorCliNotGitRepoError):
        ensure_git_repo(str(tmp_path))


def test_ensure_git_repo_passes_on_real_repo(tmp_path):
    _init_repo(tmp_path)
    ensure_git_repo(str(tmp_path))  # 不應拋出


def test_check_clean_working_tree_passes_on_clean_repo(tmp_path):
    _init_repo(tmp_path)
    check_clean_working_tree(str(tmp_path))  # 不應拋出


def test_check_clean_working_tree_raises_on_untracked_file(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "stray.txt").write_text("oops", encoding="utf-8")
    with pytest.raises(TranslatorCliDirtyWorkingTreeError):
        check_clean_working_tree(str(tmp_path))


def test_check_clean_working_tree_raises_plain_error_when_git_status_itself_fails(tmp_path):
    # `git status` 指令本身跑不動（repo 損毀等，跟「working tree 有變更」
    # 或「不是 git repo」都不同）——用真實損毀的 index 觸發（rev-parse
    # 仍會成功，只有 status 失敗），驗證不會誤用
    # TranslatorCliNotGitRepoError 這個字面上不準確的型別。
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, text=True, check=True)
    (tmp_path / ".git" / "index").write_text("garbage", encoding="utf-8")

    with pytest.raises(TranslatorCliError) as exc_info:
        check_clean_working_tree(str(tmp_path))
    assert not isinstance(exc_info.value, TranslatorCliNotGitRepoError)
    assert not isinstance(exc_info.value, TranslatorCliDirtyWorkingTreeError)


def test_commit_scaffold_creates_commit(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    commit_scaffold(str(tmp_path))
    check_clean_working_tree(str(tmp_path))  # commit 後 working tree 應恢復乾淨

    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "scaffold: initial skeleton" in log.stdout


def test_commit_fill_only_adds_target_file(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "app" / "b.py").write_text("y = 1\n", encoding="utf-8")
    commit_scaffold(str(tmp_path))

    (tmp_path / "app" / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "app" / "b.py").write_text("y = 2\n", encoding="utf-8")  # 模擬「不該被帶走」的意外變更

    diff = diff_for_file(str(tmp_path), "app/a.py")
    assert "x = 2" in diff

    commit_fill(str(tmp_path), task_id="task_001", target_file="app/a.py", class_name="UserRepository", function_name="get_by_id")

    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert log.stdout.strip() == "implement: task_001 fill UserRepository.get_by_id in app/a.py"

    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "b.py" in status.stdout  # b.py 的變更沒有被這次 commit 帶走，working tree 仍不乾淨


def test_commit_fill_without_class_name_omits_prefix(tmp_path):
    # class_name=None（routers 層自由函式，見 05a 七章）時 commit 訊息
    # 不該帶 "None." 前綴（見 07a 八章「class_name 為 None 時省略該段」）。
    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "user_router.py").write_text("x = 1\n", encoding="utf-8")
    commit_scaffold(str(tmp_path))

    (tmp_path / "app" / "user_router.py").write_text("x = 2\n", encoding="utf-8")
    commit_fill(str(tmp_path), task_id="task_002", target_file="app/user_router.py", class_name=None, function_name="search")

    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert log.stdout.strip() == "implement: task_002 fill search in app/user_router.py"


def test_discard_file_changes_reverts_tracked_file_to_head(tmp_path):
    # commit_fill() 失敗時的復原路徑（見 client.fill_function()）：
    # target_file 已在 scaffold 階段 commit 過，這裡驗證能正確還原。
    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    f = tmp_path / "app" / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    commit_scaffold(str(tmp_path))

    f.write_text("x = 999  # 未成功 commit 的變更\n", encoding="utf-8")
    assert discard_file_changes(str(tmp_path), "app/a.py") is True
    assert f.read_text(encoding="utf-8") == "x = 1\n"
    check_clean_working_tree(str(tmp_path))  # 應恢復乾淨，不拋出


def test_discard_written_files_cleans_untracked_files_on_first_scaffold(tmp_path):
    # commit_scaffold() 在「repo 完全沒有任何 commit」（07a 二章「一次性
    # 前置準備」的初始狀態）這個情境下失敗時的復原路徑——git checkout
    # 對這種全新、從未被追蹤過的路徑而言無事可做（會回傳非 0），必須
    # 靠 git clean -fd 才能清乾淨，這裡驗證最終判準（scoped status 是否
    # 為空）正確涵蓋這個情境（見 discard_written_files() docstring）。
    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert discard_written_files(str(tmp_path), ["app/a.py"]) is True
    assert not (tmp_path / "app" / "a.py").exists()
    check_clean_working_tree(str(tmp_path))  # 不應拋出


def test_discard_written_files_prunes_newly_created_empty_parent_dir(tmp_path):
    # write_files() 用 parent.mkdir(parents=True) 幫全新檔案建巢狀目錄
    # （見 scaffold.write_files() docstring）；git clean -fd 只刪檔案本身
    # 不處理它留下的空目錄，這裡驗證 discard_written_files() 補的
    # _prune_empty_parent_dirs() 把這個空目錄也清乾淨，磁碟狀態完全回到
    # 執行前（不只是 git status 判定乾淨）。
    _init_repo(tmp_path)
    nested = tmp_path / "app" / "models"
    nested.mkdir(parents=True)
    (nested / "user.py").write_text("class User: pass\n", encoding="utf-8")

    assert discard_written_files(str(tmp_path), ["app/models/user.py"]) is True
    assert not (tmp_path / "app" / "models").exists()  # 空目錄本身也被清掉
    assert not (tmp_path / "app").exists()  # 一路往上，app/ 也是這次才建的空目錄
    check_clean_working_tree(str(tmp_path))  # 不應拋出


def test_discard_written_files_keeps_nonempty_parent_dir(tmp_path):
    # 剪空目錄的範圍要精確：父目錄底下若還有這次 rollback 清單以外的
    # 其他內容（例如另一個既有檔案），不能被連帶刪除。
    _init_repo(tmp_path)
    (tmp_path / "app" / "models").mkdir(parents=True)
    (tmp_path / "app" / "models" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    commit_scaffold(str(tmp_path))

    (tmp_path / "app" / "models" / "user.py").write_text("class User: pass\n", encoding="utf-8")

    assert discard_written_files(str(tmp_path), ["app/models/user.py"]) is True
    assert not (tmp_path / "app" / "models" / "user.py").exists()
    assert (tmp_path / "app" / "models").exists()  # 目錄裡還有 existing.py，不該被刪
    assert (tmp_path / "app" / "models" / "existing.py").exists()
    check_clean_working_tree(str(tmp_path))  # 不應拋出


def test_discard_written_files_reverts_tracked_and_removes_untracked(tmp_path):
    # commit_scaffold() 在「重新產生、覆蓋既有骨架」情境下失敗時的復原
    # 路徑：既有已追蹤檔案的修改要被還原，新增的未追蹤檔案要被移除。
    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("x = 1\n", encoding="utf-8")
    commit_scaffold(str(tmp_path))

    (tmp_path / "app" / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "app" / "b.py").write_text("y = 1\n", encoding="utf-8")

    assert discard_written_files(str(tmp_path), ["app/a.py", "app/b.py"]) is True
    assert (tmp_path / "app" / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (tmp_path / "app" / "b.py").exists()
    check_clean_working_tree(str(tmp_path))  # 不應拋出


def test_discard_written_files_ignores_unrelated_dirty_content(tmp_path):
    # 「爆炸半徑」測試：若 PYTHON_PROJECT_PATH 誤指到一個帶有其他重要
    # 未追蹤內容的目錄，discard_written_files() 只能動這次呼叫自己
    # 傳入的檔案清單，不能連帶清掉清單以外、剛好也存在於同一個 repo
    # 裡的其他未追蹤檔案——這是取代舊版 discard_all_changes()（對整個
    # repo 做 checkout -- . ＋ clean -fd）的核心理由，見 git_ops.py
    # docstring「範圍刻意限縮」。
    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("x = 1\n", encoding="utf-8")

    # 模擬「其他重要、與這次 translator-cli 呼叫完全無關」的未追蹤檔案
    important = tmp_path / "important_unrelated_work.py"
    important.write_text("do_not_delete_me = True\n", encoding="utf-8")

    assert discard_written_files(str(tmp_path), ["app/a.py"]) is True
    assert not (tmp_path / "app" / "a.py").exists()  # 這次呼叫自己寫的檔案被清掉
    assert important.exists()  # 清單以外的檔案完全不受影響
    assert important.read_text(encoding="utf-8") == "do_not_delete_me = True\n"


def test_discard_written_files_empty_list_is_noop(tmp_path):
    _init_repo(tmp_path)
    assert discard_written_files(str(tmp_path), []) is True


def test_discard_file_changes_reverts_staged_but_uncommitted_change(tmp_path):
    # 模擬 commit_fill() 真實的失敗時序：write_text() 已經落地，
    # git add 也已經執行（進了 index），但 git commit 本身失敗（例如
    # index lock）——`checkout` 不會動 index，若沒有先 `reset` 清掉
    # index 裡的新內容，`checkout` 只會把 working tree 覆蓋回同一份
    # 還沒 commit 成功的內容，等於沒有真的還原。
    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    f = tmp_path / "app" / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    commit_scaffold(str(tmp_path))

    f.write_text("x = 999  # 未成功 commit 的變更\n", encoding="utf-8")
    subprocess.run(["git", "add", "app/a.py"], cwd=tmp_path, capture_output=True, text=True, check=True)

    assert discard_file_changes(str(tmp_path), "app/a.py") is True
    assert f.read_text(encoding="utf-8") == "x = 1\n"
    check_clean_working_tree(str(tmp_path))  # index 也必須恢復乾淨，不只是 working tree


def test_discard_written_files_reverts_staged_but_uncommitted_change_on_first_scaffold(tmp_path):
    # 模擬 commit_scaffold() 在「repo 完全沒有任何 commit」（初次骨架）
    # 情境下真實的失敗時序：write_files() 已落地、git add -A 也已執行
    # （進了 index），但 commit 本身失敗——同上，沒有 `reset` 的話
    # `checkout`／`clean` 都不會動 index，rollback 會完全失效。
    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "app" / "b.py").write_text("y = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True, check=True)

    assert discard_written_files(str(tmp_path), ["app/a.py", "app/b.py"]) is True
    assert not (tmp_path / "app" / "a.py").exists()
    assert not (tmp_path / "app" / "b.py").exists()
    check_clean_working_tree(str(tmp_path))  # index 也必須恢復乾淨


def test_discard_written_files_reverts_staged_but_uncommitted_change_on_existing_repo(tmp_path):
    # 同上，但這次是「重新產生、覆蓋既有骨架」情境（repo 已有 HEAD）：
    # git add -A 之後，既有已追蹤檔案的修改跟新增的未追蹤檔案都進了
    # index，reset 必須能同時正確處理這兩種狀態。
    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("x = 1\n", encoding="utf-8")
    commit_scaffold(str(tmp_path))

    (tmp_path / "app" / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "app" / "b.py").write_text("y = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True, check=True)

    assert discard_written_files(str(tmp_path), ["app/a.py", "app/b.py"]) is True
    assert (tmp_path / "app" / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (tmp_path / "app" / "b.py").exists()
    check_clean_working_tree(str(tmp_path))
