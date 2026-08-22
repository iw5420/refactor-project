"""main.py 用來觀察 graph.astream() 進度、偵測卡住的輔助工具。

對應真實重跑卡了一個多小時、只能用 py-spy 手動 dump 活行程才抓到卡點的
教訓（見 docs/09b_bug_trace.md #34）：`graph.ainvoke()` 整條 graph 跑完
才回傳，中間完全黑箱，任何一個 node 卡住都無從得知。改用
`astream(stream_mode="updates")` 拿到逐 node 完成的進度事件；若太久沒有
任何 node 完成，代表當下正在跑的那個 node 可能卡住了（也可能只是合理地
在跑長時間任務，如 ⑤ 填空翻譯——這種情況下 `implement_node.py` 的 task
心跳 log 是更細緻的訊號），自動對自己這個 process 做一次 py-spy dump、
印出目前所有 thread 的 Python 呼叫堆疊，不中斷執行，只是把「卡住」從
必須手動介入才看得到的黑箱，變成自動、有紀錄的可觀測狀態。

拆成獨立模組（不放在 main.py 裡）：`_pump_graph_stream()` 本身不依賴任何
需要 .env／JAVA_BASE_URL 等環境變數才能匯入的模組（`main.py` 匯入
`graph.builder` 會連帶觸發這些環境變數檢查），拆開才能在不用準備完整
執行環境的情況下對這個核心邏輯寫單元測試。
"""
import asyncio
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

STUCK_REPORT_SECONDS = float(os.environ.get("STUCK_REPORT_SECONDS", "600"))


def dump_self_stack() -> None:
    """對目前這個 process 做一次 py-spy dump（需要先 `pip install py-spy`），
    印出所有 thread 目前卡在哪一行 Python 呼叫堆疊——不需要事後才手動對
    活著的行程 dump，直接內建成卡住偵測的一部分。"""
    pid = os.getpid()
    try:
        result = subprocess.run(
            ["py-spy", "dump", "--pid", str(pid)],
            capture_output=True, text=True, timeout=30,
        )
        logger.warning(
            "卡住偵測：目前所有 thread 的 Python 呼叫堆疊（pid=%s）\n%s",
            pid, result.stdout or result.stderr,
        )
    except Exception as exc:
        logger.warning(
            "卡住偵測：py-spy dump 失敗（%s），可能尚未安裝 py-spy"
            "（pip install py-spy）", exc,
        )


async def pump_graph_stream(stream, queue: asyncio.Queue) -> None:
    """把 astream() 的每個 node 完成事件丟進 queue，讓外層可以用
    `asyncio.wait_for(queue.get(), timeout=...)` 偵測卡住——**不能直接對
    `stream.__anext__()` 套 `asyncio.wait_for()`**：逾時時 `wait_for()`
    會 cancel 被等待的 coroutine，若那個 coroutine 正是目前卡在某個 node
    內部（例如卡在 `subprocess.run()`）的執行本體，等於卡住偵測機制自己
    把還在合理執行中的 node 砍斷。這裡把「消費 stream」跟「等多久算卡住」
    拆成兩個獨立的 coroutine，`wait_for()` 逾時取消的只是 `queue.get()`
    這個無副作用的操作，不會動到真正在跑的 graph 執行本體。
    """
    try:
        async for chunk in stream:
            await queue.put(("chunk", chunk))
    except BaseException as exc:  # noqa: BLE001 — 原樣轉交給外層處理／往上拋
        await queue.put(("error", exc))
        return
    await queue.put(("done", None))
