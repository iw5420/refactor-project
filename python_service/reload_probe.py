"""一次性部署 `_reload_probe_wrapper.py`／`.gitignore` 到
`python_project_path`，對應 09a 三章「這個端點不寫進 `app/main.py`，
改用外掛的 ASGI wrapper 掛載真正的 app」與「`_reload_token.py`／
`_reload_probe_wrapper.py` 必須排除在 07a 九章的衝突偵測之外」。

**呼叫時機**：必須在 `generate_scaffold()`（④ 骨架實作 Agent）第一次
執行之前呼叫——`.gitignore` 的初始 commit 必須早於 `generate_scaffold()`
自己的骨架 commit，否則這兩個檔案會以未追蹤檔案的身分讓
`check_clean_working_tree()` precondition 檢查失敗（見 07a 九章）。
因此由 `main.py` 在 `graph.ainvoke()` 之前呼叫一次，見
`09b_implement_agent_code.md` 二章。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from translator_cli.git_ops import ensure_git_repo

WRAPPER_FILE_NAME = "_reload_probe_wrapper.py"
TOKEN_FILE_NAME = "_reload_token.py"

# __pycache__/ 不是 09a 三章原本列的兩個檔名，是實測時發現的必要追加：
# implement 期間容器（python_service/process.py）透過 bind mount 常駐
# 執行 uvicorn --reload，CPython 每次 import 都會在磁碟上寫出
# __pycache__/*.pyc——這些是容器裡的行程寫的，不是任何一次 fill_
# function()／generate_scaffold() 自己的動作，但一樣會讓
# check_clean_working_tree()（07a 九章）判定 working tree 不乾淨，
# 導致同一輪 implement 迴圈裡後續每一個 task 都直接被 precondition
# 檢查擋下（已用真實案例重現：exam-platform-api 對 72 個 task 跑
# implement_node.run() 時，前兩個 task 就因為這個原因失敗，見
# 09b_bug_trace.md）。
_IGNORED_ENTRIES = (WRAPPER_FILE_NAME, TOKEN_FILE_NAME, "__pycache__/")

# 對應 09a 三章「這個端點不寫進 app/main.py...」定案的完整內容。
# 註冊順序不能顛倒：/__reload_probe__ 必須先於 mount("/", app) 註冊，
# 否則會被 catch-all mount 攔截（見 09a 三章「這個順序不能顛倒」）。
WRAPPER_TEMPLATE = '''"""測試基礎設施用的固定樣板，不含任何業務邏輯，不由任何 Agent 產生。
啟動方式：uvicorn _reload_probe_wrapper:wrapper_app --reload
（不是 uvicorn app.main:app --reload），見 09a 三章「Python 服務只
啟動一次」。
"""
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from app.main import app as _real_app

try:
    from _reload_token import TOKEN
except ModuleNotFoundError:
    # pipeline 第一次啟動時這個檔案還沒被任何一輪 _wait_for_service_reload()
    # 寫過；退回一個不可能等於任何真實 token（UUID）的預設值。見 09a 三章
    # 「探測端點讀取 token 檔案時要容忍檔案還不存在」。
    TOKEN = ""


async def _probe(request):
    return PlainTextResponse(TOKEN)


wrapper_app = Starlette(routes=[
    Route("/__reload_probe__", _probe),
    Mount("/", app=_real_app),
])
'''


def _run_git(python_project_path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", python_project_path, *args], capture_output=True, text=True, encoding="utf-8"
    )


def ensure_reload_probe_infra(python_project_path: str) -> None:
    """冪等：main.py 每次啟動都可以呼叫。只在 `.gitignore` 缺少這兩個
    檔名時才新增一次 commit（見 09a 三章「這個 .gitignore 修正涵蓋的是
    每一輪覆寫的情況，不是只解決第一次寫入」）；wrapper 檔案內容固定，
    每次都覆寫、不做存在性判斷——這個檔案本來就被 `.gitignore` 排除，
    覆寫沒有版控風險，也讓樣板更新後舊環境能自動跟上。

    不自動 `git init`：`python_project_path` 是否已完成 07a 二章「一次性
    前置準備」（目錄存在、`git init` 過、沒有任何 commit）是輸入端環境
    問題，不是這裡可以自動補救的情況，比照 `generate_scaffold()` 對同一
    個前提的既有處理方式（`ensure_git_repo()` 檢查不通過就直接拋出明確
    錯誤）。
    """
    ensure_git_repo(python_project_path)

    root = Path(python_project_path)
    gitignore_path = root / ".gitignore"

    existing_lines = (
        gitignore_path.read_text(encoding="utf-8").splitlines() if gitignore_path.exists() else []
    )
    missing = [name for name in _IGNORED_ENTRIES if name not in existing_lines]

    if missing:
        with gitignore_path.open("a", encoding="utf-8") as f:
            for name in missing:
                f.write(f"{name}\n")

        add = _run_git(python_project_path, "add", ".gitignore")
        if add.returncode != 0:
            raise RuntimeError(f"git add .gitignore 失敗：{add.stderr.strip()}")

        commit = _run_git(python_project_path, "commit", "-m", "chore: ignore reload-probe infra files")
        if commit.returncode != 0:
            raise RuntimeError(f"git commit .gitignore 失敗：{commit.stderr.strip()}")

    (root / WRAPPER_FILE_NAME).write_text(WRAPPER_TEMPLATE, encoding="utf-8")
