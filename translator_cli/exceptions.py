# translator_cli/exceptions.py
"""translator-cli 例外階層，對應 07a 十三章「錯誤處理範圍」。所有例外
共用 `TranslatorCliError` 基底類別，但呼叫端（`client.py`）一律逐一
`except` 特定子類別、轉成 `FillResult(success=False, ...)` 或
`{"success": False, ...}` 回傳，不讓例外往 `graph/nodes/` 洩漏——
translator-cli 本身的呼叫失敗不直接觸發 `retry_count` 迴圈（見 07a 十三
章），是否要重試、要不要把 task 標記失敗，由呼叫端（`implement_node.py`
／`scaffold_node.py`）決定。
"""
from __future__ import annotations


class TranslatorCliError(Exception):
    """所有 translator-cli 例外的共同基底類別。"""


class TranslatorCliNotGitRepoError(TranslatorCliError):
    """`python_project_path` 不是一個已初始化的 git repo（見 07a 二章
    「一次性前置準備」：目錄存在、`git init` 過、沒有任何 commit）。
    `generate_scaffold()` 執行前檢查，環境沒準備好，直接中止，不是可以
    自動補救的情況（見 07a 二章）。
    """


class TranslatorCliDirtyWorkingTreeError(TranslatorCliError):
    """`git status --porcelain` 非空（見 07a 九章「衝突偵測」）。
    `generate_scaffold()`／`fill_function()` 動筆寫任何檔案之前的
    precondition 檢查沒通過，不寫入任何內容，交由人工核對這份意外的
    變更是什麼——不重試，見 07a 十三章。
    """


class TranslatorCliScaffoldMismatchError(TranslatorCliError):
    """`fill_function()` 定位函式失敗（07a 六章步驟 1／4「scaffold/task
    不一致」）：`target_file` 不存在，或 `(class_name, function_name)`
    在該檔案裡找不到。這是防禦性的最後一道檢查——06a 已在上游用「05a
    對多載的消歧」＋「1:1 涵蓋率」兩道機制保證這個三元組理論上必然
    存在，這裡觸發代表更上游的資料有 bug，不重試，直接回報（見 07a
    六章、十三章）。
    """


class TranslatorCliNetworkError(TranslatorCliError):
    """跟 ollama（經 nginx）的連線失敗，對應 07a 七章「Timeout 與重試
    策略」表格的網路層重試耗盡（`ollama_client._call_ollama_once()`）。
    跟 `TranslatorCliModelOutputError`（模型有回應、但內容違反格式契約）
    是刻意分開的兩種錯誤語意——這裡代表根本沒收到可用回應，成因通常是
    另一台 Mac 的網路／nginx／ollama 服務本身有問題，不是模型輸出品質
    問題，混用會誤導事後排查方向（見 00 十章「錯誤訊息看起來像哪個
    Agent 的問題，根因其實在別處」這類風險）。不重試——網路層重試已經
    在 `_call_ollama_once()` 內部做過，這裡拋出代表重試已經耗盡。
    """


class TranslatorCliModelOutputError(TranslatorCliError):
    """ollama 回應違反格式契約，對應 07a 七章「模型輸出格式錯誤的修正
    重試」四種觸發情況：delimiter 抽取失敗、`body_text` 通不過
    `ast.parse()`、`body_text` 解析出的陳述式清單為空（六章步驟
    6a）、`body_text` 恰好是與目標函式同名的巢狀函式定義（六章步驟
    6b「模型重複輸出函式簽名」）。固定重試一次（`ollama_client.
    get_function_body()`），仍失敗才拋出，不無限重試。
    """


class TranslatorCliAssemblyError(TranslatorCliError):
    """`generate_scaffold()` 整檔組裝完成後的最終 `ast.parse()` 驗證
    失敗（07a 四章「語法驗證與寫入」步驟 4，或 Schema 定義段合併後的
    步驟 5）。代表組裝邏輯本身有 bug（如 import 合併沒去重乾淨），不是
    個別介面的型別字串問題——那種情況走 `skipped_interfaces`／
    `skipped_db_models` 隔離，不會走到這裡。不重試，直接中止（見 07a
    十三章）。
    """


class TranslatorCliConfigError(TranslatorCliError):
    """必要的環境變數缺失（`OLLAMA_BASE_URL`／`OLLAMA_API_KEY`，見 07a
    七章「連線方式」）。跟 `TranslatorCliNotGitRepoError` 是同一類「輸入
    端環境沒準備好，不是可以自動補救的情況」（見 07a 二章），只是分別
    對應 git 環境與 ollama 連線環境兩種前置準備。不重試——遺漏的環境
    變數不會因為重試而自己出現；`.env` 若漏了這兩個變數，這條 pipeline
    run 的第一個 task 就會踩到，需要人工補上環境變數後重跑。
    """
