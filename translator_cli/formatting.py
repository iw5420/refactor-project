# translator_cli/formatting.py
"""寫入後的格式化，統一用 `ruff check --select I --fix`＋`ruff format`
正規化 `PythonAdapter.render()`（見 `python_adapter.py`）產出的內容。
`ast.unparse()` 保證語法合法，但不保證符合 PEP8／專案 lint 規則（引號
慣例、空行數量、import 排序等）。

**`ruff format` 不處理 import 排序**：`ruff format`（格式器）只管排版
（引號、空行、縮排），不含 isort 功能——`scaffold._merge_schema_blocks()`
合併多個 Schema 定義段的 import 陳述式時用清單／字典去重，只保證語法
合法與不重複，不保證排序符合慣例。因此先跑一次
`ruff check --select I --fix`（isort 對應的 lint 規則，開 `--fix` 自動
排序整理），再跑 `ruff format` 正規化其餘排版——兩者職責不同，缺一個
都無法達到「風格完全一致」的目標。

**`ruff` 是硬性依賴，不是 best-effort**：07a 六章「`ast.unparse()` 會
重新格式化整個檔案」承諾「格式風格天生一致」，讓 `git diff` 只集中在
被填的那個函式——這個承諾建立在「每次寫入都經過同一條格式化流程」
這個前提上。若格式化只在部分環境／部分次執行才生效，這個前提就不成
立：`fill_function()` 每次都會先用 `ast.unparse()` 把整個目標檔案重新
序列化（例如把雙引號改成 `ast.unparse()` 固定使用的單引號、壓縮多餘
空行），若這次執行剛好沒有 `ruff` 可以把格式排回去，這個「整檔改形」
會原樣寫入 diff／commit，讓 07a 承諾的「diff 範圍必須乾淨」失效——
`scaffold` 若在有裝 `ruff` 的機器上先跑過一次，之後只要有任何一次
`fill_function()` 在沒有 `ruff` 的環境執行，那次的 diff 就會整檔改形。

因此找不到 `ruff` 執行檔、或 `ruff format` 本身執行失敗，都直接拋出
`TranslatorCliError` 中止這次呼叫（由 `client.py` 轉成
`FillResult(success=False)`／`ScaffoldResult` 失敗並觸發 rollback），
不允許「這次沒排版」的檔案流入 commit——寧可讓這個 task 失敗、交給
排程器記一次失敗重試，也不要讓不一致的格式風格污染 git 歷史。
"""
from __future__ import annotations

import subprocess

from translator_cli.exceptions import TranslatorCliError


def _run_ruff(python_project_path: str, args: list[str]) -> None:
    """執行單一 ruff 指令，找不到執行檔或指令本身失敗都拋出
    `TranslatorCliError`，不吞掉。
    """
    try:
        result = subprocess.run(
            ["ruff", *args],
            cwd=python_project_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise TranslatorCliError(
            "找不到 ruff 執行檔——ruff 是硬性依賴（見 07b 文件「formatting.py」），"
            "請先 `pip install ruff`（或 `pip install -r requirements.txt`）"
        ) from exc

    if result.returncode != 0:
        raise TranslatorCliError(f"ruff {' '.join(args)} 執行失敗：{result.stderr.strip()}")


def format_paths(python_project_path: str, *relative_paths: str) -> None:
    """對 `python_project_path` 底下的一或多個相對路徑（檔案，通常是
    `scaffold.write_files()`／`fill_function()` 剛寫入的那批路徑）依序
    呼叫 `ruff check --select I --fix`（import 排序）與 `ruff format`
    （其餘排版），就地格式化。任一步找不到 `ruff` 執行檔、或本身失敗
    都拋出 `TranslatorCliError`，不吞掉——呼叫端需要這個訊號才能觸發
    rollback，避免半格式化的內容進入 commit。
    """
    if not relative_paths:
        return
    _run_ruff(python_project_path, ["check", "--select", "I", "--fix", *relative_paths])
    _run_ruff(python_project_path, ["format", *relative_paths])
