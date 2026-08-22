"""graph/stream_watchdog.py 的單元測試。

對應 docs/09b_bug_trace.md #34：真實重跑卡了一個多小時，只能靠 py-spy
手動 dump 活行程才抓到卡點。main.py 改用 astream() 拿逐 node 進度、逾時
自動 dump 堆疊，但這個設計本身有一個容易犯的錯——若直接對
`stream.__anext__()` 套 `asyncio.wait_for(timeout=...)`，逾時時
`wait_for()` 會 cancel 被等待的 coroutine，等於卡住偵測機制自己把還在
合理執行中的 graph 執行本體砍斷。這裡直接用單元測試鎖住這個安全性質，
不是等真實重跑才發現。
"""
import asyncio

import pytest

from graph.stream_watchdog import pump_graph_stream


async def _quick_stream():
    yield {"node_a": {"x": 1}}
    yield {"node_b": {"y": 2}}


async def _slow_stream():
    yield {"node_a": {"x": 1}}
    await asyncio.sleep(0.3)  # 模擬一個合理地跑很久的 node
    yield {"node_b": {"y": 2}}


async def _failing_stream():
    yield {"node_a": {"x": 1}}
    raise RuntimeError("node_b 爆了")


def test_pump_graph_stream_forwards_chunks_then_done():
    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        await pump_graph_stream(_quick_stream(), queue)

        kind1, payload1 = queue.get_nowait()
        kind2, payload2 = queue.get_nowait()
        kind3, payload3 = queue.get_nowait()

        assert (kind1, payload1) == ("chunk", {"node_a": {"x": 1}})
        assert (kind2, payload2) == ("chunk", {"node_b": {"y": 2}})
        assert kind3 == "done"

    asyncio.run(scenario())


def test_pump_graph_stream_forwards_exception_as_error_sentinel():
    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        await pump_graph_stream(_failing_stream(), queue)

        kind1, _ = queue.get_nowait()
        kind2, exc = queue.get_nowait()

        assert kind1 == "chunk"
        assert kind2 == "error"
        assert isinstance(exc, RuntimeError)
        assert queue.empty()  # 例外發生後不會再有 "done"

    asyncio.run(scenario())


def test_stuck_detection_timeout_does_not_cancel_the_underlying_stream():
    """核心安全性質：外層對 `queue.get()` 套的 `wait_for(timeout=...)`
    逾時，只能取消「等 queue 有沒有新東西」這個無副作用的動作，
    不能連帶把 `pump_graph_stream()` 正在消費的 stream 一起砍斷——
    否則「卡住偵測」在合理的長任務（如 ⑤ 填空翻譯）上會誤判成卡住，
    還親手把還在正常執行的東西中斷掉。"""
    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        pump_task = asyncio.create_task(pump_graph_stream(_slow_stream(), queue))

        kind, payload = await queue.get()
        assert (kind, payload) == ("chunk", {"node_a": {"x": 1}})

        # 故意用很短的 timeout 讓它逾時，模擬「卡住偵測」的判斷時機，
        # 這時候底下的 stream 其實還在合理地跑（sleep 0.3s 還沒到）。
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)

        # 逾時之後，pump_task／底下的 stream 不該被砍斷，應該還活著。
        assert not pump_task.done()

        # 真正等到 stream 自然把第二個 chunk 送出來。
        kind, payload = await asyncio.wait_for(queue.get(), timeout=2)
        assert (kind, payload) == ("chunk", {"node_b": {"y": 2}})

        kind, _ = await queue.get()
        assert kind == "done"

        await pump_task  # 正常結束，不拋例外

    asyncio.run(scenario())
