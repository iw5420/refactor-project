# translator_cli/git_ops.py
"""git snapshot、commit 顆粒度、衝突偵測，對應 07a 八、九章。所有 git
指令一律以呼叫端傳入的 `python_project_path` 為 repo 根目錄
（`git -C {python_project_path} ...`），不是 translator-cli 自己執行時
的 cwd（見 07a 八章）。用列表形式呼叫 `subprocess.run`（不經 shell），
比照 `refactor_harness/core/postman_runner.py` 既有的 `run_newman()`
寫法，跨平台一致（見 01 八章「跨平台注意事項」`shell=False` 建議）。

**不需要鎖機制**：見 07a 八章「決策：每個 task 一個 commit，不需要鎖
機制」——`MODEL_SEMAPHORE(1)`（`implement_node.py`）與 scaffold/implement
的圖結構先後順序，已經從結構上保證任何時刻最多一個呼叫端在寫入這個
git repo，這裡不重複實作一層檔案鎖。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from translator_cli.exceptions import TranslatorCliDirtyWorkingTreeError, TranslatorCliError, TranslatorCliNotGitRepoError


def _run_git(python_project_path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", python_project_path, *args], capture_output=True, text=True, encoding="utf-8"
    )


def ensure_git_repo(python_project_path: str) -> None:
    """對應 07a 二章「一次性前置準備」：`generate_scaffold()` 執行前
    檢查目標目錄是不是一個 git repo，不是則直接中止並回報明確錯誤——
    這是輸入端環境沒準備好，不是可以自動補救的情況。

    不另外檢查目錄是否存在：`git -C {不存在的路徑}` 本身就會乾淨地
    失敗（`fatal: cannot change to '...': No such file or directory`，
    returncode 128），不需要在呼叫 git 之前自己先判斷一次，那只是重複
    git 已經會做的事。
    """
    result = _run_git(python_project_path, "rev-parse", "--is-inside-work-tree")
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise TranslatorCliNotGitRepoError(
            f"{python_project_path} 不是一個 git repo（見 07a 二章「一次性前置準備」，"
            f"需先在目標目錄執行 git init）：{result.stderr.strip()}"
        )


def check_clean_working_tree(python_project_path: str) -> None:
    """對應 07a 九章「衝突偵測」：`generate_scaffold()`／`fill_function()`
    動筆寫任何檔案之前的 precondition 檢查。輸出非空 → 不寫入任何內容，
    直接回傳失敗，交由人工核對這份意外的變更是什麼。
    """
    result = _run_git(python_project_path, "status", "--porcelain")
    if result.returncode != 0:
        # 指令本身跑不動（權限問題、repo 損毀等）跟「不是 git repo」是
        # 不同語意，不套用 TranslatorCliNotGitRepoError 這個字面說法，
        # 避免誤導排查方向（`ensure_git_repo()` 已經在更早的
        # precondition 檢查過一次是不是 git repo，這裡若還失敗代表狀況
        # 更複雜）。
        raise TranslatorCliError(f"git status 執行失敗：{result.stderr.strip()}")

    if result.stdout.strip():
        raise TranslatorCliDirtyWorkingTreeError(
            f"working tree 不乾淨，拒絕寫入（見 07a 九章）：{result.stdout.strip()}"
        )


def diff_for_file(python_project_path: str, target_file: str) -> str:
    """對應 07a 八章：在 `git add` 之前擷取，此時檔案已寫入磁碟但尚未
    進 staging area，working tree 依九章 precondition 保證動筆前是
    乾淨的，這個 diff 精確反映這次 `fill_function()` 造成的變更。
    """
    result = _run_git(python_project_path, "diff", "--", target_file)
    return result.stdout


def commit_scaffold(python_project_path: str) -> None:
    """對應 07a 八章：`generate_scaffold()` 成功寫入所有骨架檔案後的
    一次性 commit，作為後續所有 `fill_function()` commit 的共同基礎。
    """
    _run_git(python_project_path, "add", "-A")
    result = _run_git(python_project_path, "commit", "-m", "scaffold: initial skeleton from python_structure")
    if result.returncode != 0:
        raise TranslatorCliError(f"scaffold commit 失敗：{result.stderr.strip()}")


def commit_fill(
    python_project_path: str,
    *,
    task_id: str,
    target_file: str,
    class_name: str | None,
    function_name: str,
) -> None:
    """對應 07a 八章：只 add 這次實際寫入的那一個檔案，不用 `-A`——即使
    working tree 因為某種原因存在其他未預期的變更（理論上不該發生，見
    九章），也不會被這次 commit 意外一起帶走。`task_id` 是必要引數
    （見 07a 二章），保留 `class_name.function_name` 是為了讓 `git log`
    一眼看出這個 commit 改的是哪個函式。
    """
    label = f"{class_name}.{function_name}" if class_name else function_name
    _run_git(python_project_path, "add", target_file)
    result = _run_git(
        python_project_path, "commit", "-m", f"implement: {task_id} fill {label} in {target_file}"
    )
    if result.returncode != 0:
        raise TranslatorCliError(f"fill_function commit 失敗（task {task_id}）：{result.stderr.strip()}")


def commit_file_fix(python_project_path: str, *, task_id: str, target_file: str) -> None:
    """對應 `client.apply_file_fix()`（10a 八章「phase 2：檔案層級
    修正」）：⑦ Debug Agent 給的修正若碰的是函式本體以外的內容（如
    import 敘述），不是「填某個函式」，用跟 `commit_fill()` 不同的訊息
    格式，讓 `git log` 能區分「⑤ 生成函式」跟「⑦ 直接修正檔案層級
    內容」這兩種性質不同的變更。
    """
    _run_git(python_project_path, "add", target_file)
    result = _run_git(
        python_project_path, "commit", "-m", f"debug: {task_id} apply file-level fix in {target_file}"
    )
    if result.returncode != 0:
        raise TranslatorCliError(f"apply_file_fix commit 失敗（task {task_id}）：{result.stderr.strip()}")


def discard_file_changes(python_project_path: str, target_file: str) -> bool:
    """`commit_fill()` 失敗後的復原路徑用（見 `client.fill_function()`）：
    把 `target_file` 還原回目前 HEAD 的內容，撤銷這次失敗的 commit 之前
    寫入磁碟的新內容。`git commit` 若因不可抗力（index lock、權限問題）
    失敗，`write_text()` 已經落地的變更會讓 working tree 卡在「不乾淨」
    狀態，之後每一次 `check_clean_working_tree()` precondition 檢查都會
    連帶失敗——這對這一個 task 而言是可歸因、已知成因的變更（就是我們
    自己剛寫入、但沒能進版控的那份內容），不是九章「衝突偵測」要攔的
    那種「來源不明的意外變更」，因此可以安全地自動撤銷，只把這一個
    task 標記失敗，不讓後續所有 task 被這次 commit 失敗拖累卡住。

    **`git reset` 必須先於 `git checkout`**：`commit_fill()` 失敗前已經
    執行過 `git add {target_file}`，內容已經寫進 index，不是只留在
    working tree——`git checkout -- <path>` 預設是「用 index 內容覆蓋
    working tree」，並不會動 index 本身，若 index 裡已經是這次沒能
    commit 成功的新內容，`checkout` 只會把 working tree 覆蓋回同一份
    「壞」內容，等於什麼都沒還原（已用真實 git 行為驗證過：`add` 後
    只執行 `checkout` 不執行 `reset`，working tree 內容完全不變）。
    `git reset -- <path>` 把 index 還原回目前 HEAD 的狀態（沒有 HEAD
    的情況下也能安全執行），之後 `checkout` 才能真正把 working tree
    也拉回 HEAD 的內容。

    回傳是否成功還原（透過檢查最終 `git status` 是否乾淨，而不是任一
    個別指令的 returncode）。
    """
    _run_git(python_project_path, "reset", "--", target_file)
    _run_git(python_project_path, "checkout", "--", target_file)
    status = _run_git(python_project_path, "status", "--porcelain", "--", target_file)
    return status.returncode == 0 and not status.stdout.strip()


def discard_written_files(python_project_path: str, file_paths: list[str]) -> bool:
    """`commit_scaffold()` 失敗（或 `write_files()` 寫到一半發生
    `OSError`）後的復原路徑用（見 `client.generate_scaffold()`），道理
    跟 `discard_file_changes()` 一致，只是骨架階段一次寫入多個檔案。
    `file_paths` 是 `scaffold.build_files()` 實際算出、這次真正打算
    寫入的檔案清單（`client.py` 呼叫時傳 `list(files.keys())`）——**只**
    對這批路徑執行 `reset`／`checkout`／`clean`，不對整個 repo 做無
    差別還原。

    **範圍刻意限縮到這次寫入的精確檔案清單，不是整個 repo**：
    `python_project_path` 依 07a 二章設計是 translator-cli 專屬 repo，
    但這個前提依賴 `.env` 的 `PYTHON_PROJECT_PATH` 設定正確——萬一設定
    錯誤、誤指到一個帶有其他重要內容的目錄，整個 repo 範圍的 `clean`
    會有無法挽回的風險。既然 `build_files()` 當下就已經精確知道這次要
    寫哪些檔案，只對這批路徑動作，不論 `python_project_path` 實際指向
    哪裡，這個函式的破壞範圍都嚴格限制在「這次呼叫自己打算寫入的
    檔案」，不依賴任何關於目錄內容的外部假設。

    **`git reset` 必須先於 `checkout`／`clean`**：`commit_scaffold()`
    失敗前已經執行過 `git add -A`，這批檔案的內容已經寫進 index——
    `checkout` 只會用 index 內容覆蓋 working tree，`clean -fd` 也只
    處理未追蹤的檔案，兩者都不會動 index 本身。若 index 裡已經是這次
    沒能 commit 成功的新內容，光靠 `checkout`＋`clean` 等於什麼都沒
    還原、index 依然停在「已 add」的髒狀態（已用真實 git 行為驗證
    過）。`git reset -- p1 p2 p3` 先把這批路徑的 index 還原回目前 HEAD
    的狀態——不論這個 repo 有沒有任何 commit，`git reset` 都能安全
    執行（沒有 HEAD 時等同「把這批路徑從 index 整個移除」），之後
    `checkout`／`clean` 才能真正發揮作用。`git reset` 對多個 pathspec
    不是「全有全無」（跟下面 `checkout` 的限制不同，已驗證混合「已
    追蹤＋從未追蹤」的路徑一次呼叫就能正確處理），不需要逐一呼叫。

    **`git checkout` 對多個 pathspec 是「全有全無」，逐一呼叫**：若把
    `file_paths` 一次全部傳給同一個 `git checkout -- p1 p2 p3` 指令，
    只要其中一個路徑是全新、從未被追蹤過的檔案（`git` 眼中「不存在的
    pathspec」），整個指令會直接失敗、**其餘合法路徑也不會被還原**
    （已用真實 git 行為驗證過，多路徑不是逐一嘗試、是整批放棄）。
    因此改成逐一對每個路徑呼叫 `checkout`，個別失敗（代表這個路徑是
    全新檔案、本來就沒有舊版本可還原）直接忽略，交給下一步 `git clean`
    處理；`git clean -fd` 支援多個 pathspec 一次處理、不需要逐一呼叫。

    **判斷成功與否看最終狀態，不是每個指令各自的 returncode**：`git
    status --porcelain -- p1 p2 p3` 把檢查範圍限定在這批路徑，只要
    這批路徑都乾淨（不論是成功還原、或是被 `clean` 移除）就算成功。

    **`clean` 之後額外呼叫 `_prune_empty_parent_dirs()`**：`git clean
    -fd` 只刪未追蹤的檔案本身，不處理它留下的空目錄——`write_files()`
    幫全新檔案建的巢狀目錄（`app/models/` 這類）被清空後會原樣留在
    磁碟上，`git status` 判定乾淨（空目錄本來就不受 git 追蹤），但磁碟
    狀態沒有完全回到執行前，這裡補一步清掉。
    """
    if not file_paths:
        return True

    _run_git(python_project_path, "reset", "--", *file_paths)
    for path in file_paths:
        _run_git(python_project_path, "checkout", "--", path)
    _run_git(python_project_path, "clean", "-fd", "--", *file_paths)
    _prune_empty_parent_dirs(python_project_path, file_paths)
    status = _run_git(python_project_path, "status", "--porcelain", "--", *file_paths)
    return status.returncode == 0 and not status.stdout.strip()


def _prune_empty_parent_dirs(python_project_path: str, file_paths: list[str]) -> None:
    """`write_files()` 用 `parent.mkdir(parents=True, exist_ok=True)`
    幫全新檔案建立巢狀目錄（見 `scaffold.write_files()` docstring）；
    `git clean -fd` 只刪未追蹤的檔案本身，不處理它留下的空目錄（已用
    真實 git 行為驗證過：對單一檔案路徑執行 `clean -fd` 後，該檔案剛
    建立的父目錄若因此變空，會原樣留在磁碟上）。這裡對每個 file_path
    的父目錄逐層往上嘗試 `rmdir()`，只在目錄確實是空的才會成功、遇到
    非空或不存在就直接停止（`OSError` 忽略），並且不會往上超出
    `python_project_path` 這個 repo 根目錄，避免動到 rollback 範圍以外
    的任何東西。
    """
    root = Path(python_project_path).resolve()
    for file_path in file_paths:
        parent = (root / file_path).parent.resolve()
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
