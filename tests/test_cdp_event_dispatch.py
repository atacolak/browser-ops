"""Regression: CDP recv path must not await user event handlers.

Live demo failure: with multi-navigator + observe mirrors, navigation event
storms blocked the CDP recv loop (await handler), so Runtime.evaluate /
page_info / tabs timed out. Events must be queued; command responses resolve
immediately even when handlers are slow; storms are bounded.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

from backend.cdp_client import CDPClient  # noqa: E402


def test_slow_event_handler_does_not_block_command_response():
    """Inject events then a response; send() must complete while handler sleeps."""

    async def _run():
        cdp = CDPClient("ws://127.0.0.1:9/devtools/browser", event_queue_size=64)
        # Avoid real connect — drive dispatch_message + send path manually.
        cdp._closed = False
        cdp._ensure_event_pump()

        started = asyncio.Event()
        released = asyncio.Event()
        handler_calls = 0

        async def slow_handler(method: str, params: dict):
            nonlocal handler_calls
            handler_calls += 1
            started.set()
            # Block for longer than a healthy command RTT would ever need.
            await released.wait()

        cdp.on_event(slow_handler)

        # Patch _send_frame so send() does not need a live socket.
        async def _fake_send_frame(opcode: int, payload: bytes):
            return None

        cdp._send_frame = _fake_send_frame  # type: ignore[method-assign]

        # Queue a slow event first (fills worker).
        cdp.dispatch_message(
            {"method": "Network.requestWillBeSent", "params": {"requestId": "1"}}
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # Start a command while handler is still blocked.
        send_task = asyncio.create_task(cdp.send("Runtime.evaluate", {"expression": "1"}))
        # Allow send to register pending + "write"
        await asyncio.sleep(0.02)
        assert not send_task.done()
        msg_id = cdp._msg_id
        assert msg_id in cdp._pending

        # Response arrives on the "wire" while handler still sleeping.
        t0 = time.monotonic()
        cdp.dispatch_message(
            {
                "id": msg_id,
                "result": {"result": {"type": "number", "value": 1}},
            }
        )
        result = await asyncio.wait_for(send_task, timeout=0.5)
        elapsed = time.monotonic() - t0
        assert result["result"]["result"]["value"] == 1
        # Must not wait for the slow handler (would be multi-second if awaited).
        assert elapsed < 0.4, f"command blocked by handler: {elapsed:.3f}s"
        assert handler_calls >= 1
        assert not released.is_set()

        released.set()
        await cdp.close()

    asyncio.run(_run())


def test_event_storm_is_bounded_no_task_explosion():
    async def _run():
        qsize = 32
        cdp = CDPClient("ws://127.0.0.1:9/devtools/browser", event_queue_size=qsize)
        cdp._closed = False
        cdp._ensure_event_pump()

        gate = asyncio.Event()
        seen: list[str] = []

        async def slow_handler(method: str, params: dict):
            await gate.wait()
            seen.append(method)

        cdp.on_event(slow_handler)

        # Flood far beyond queue capacity while worker blocked on first event.
        n = 500
        for i in range(n):
            cdp.dispatch_message(
                {"method": "Network.dataReceived", "params": {"n": i}}
            )

        # Queue must stay bounded; drops recorded.
        assert cdp._event_queue is not None
        assert cdp._event_queue.qsize() <= qsize
        assert cdp.event_dropped > 0
        assert cdp.event_dropped >= n - qsize - 5  # allow tiny race slack

        # Only one worker task — no per-event create_task explosion.
        assert cdp._event_worker_task is not None
        assert not cdp._event_worker_task.done()

        gate.set()
        # Drain
        for _ in range(50):
            if cdp._event_queue.qsize() == 0 and cdp.event_dispatched > 0:
                break
            await asyncio.sleep(0.01)
        await cdp.close()
        # Dispatched at most queue capacity (+ in-flight), not all 500.
        assert cdp.event_dispatched <= qsize + 2
        assert len(seen) == cdp.event_dispatched

    asyncio.run(_run())


def test_handler_exception_does_not_kill_worker_or_commands():
    async def _run():
        cdp = CDPClient("ws://127.0.0.1:9/devtools/browser", event_queue_size=16)
        cdp._closed = False
        cdp._ensure_event_pump()

        calls = 0

        def bad_handler(method: str, params: dict):
            nonlocal calls
            calls += 1
            raise RuntimeError("boom")

        cdp.on_event(bad_handler)

        async def _fake_send_frame(opcode: int, payload: bytes):
            return None

        cdp._send_frame = _fake_send_frame  # type: ignore[method-assign]

        for i in range(5):
            cdp.dispatch_message({"method": "Page.loadEventFired", "params": {"i": i}})

        await asyncio.sleep(0.1)
        assert calls == 5
        assert cdp._event_worker_task is not None
        assert not cdp._event_worker_task.done()

        send_task = asyncio.create_task(cdp.send("Page.enable"))
        await asyncio.sleep(0.01)
        cdp.dispatch_message({"id": cdp._msg_id, "result": {}})
        out = await asyncio.wait_for(send_task, timeout=0.5)
        assert "result" in out
        await cdp.close()

    asyncio.run(_run())


def test_cdp_error_reply_raises_instead_of_keyerror():
    async def _run():
        cdp = CDPClient("ws://127.0.0.1:9/devtools/browser", event_queue_size=8)
        cdp._closed = False
        cdp._ensure_event_pump()

        async def _fake_send_frame(opcode: int, payload: bytes):
            return None

        cdp._send_frame = _fake_send_frame  # type: ignore[method-assign]
        send_task = asyncio.create_task(cdp.send("Target.attachToTarget", {"targetId": "gone"}))
        await asyncio.sleep(0.01)
        cdp.dispatch_message({"id": cdp._msg_id, "error": {"message": "No target with given id"}})
        try:
            await send_task
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "No target with given id" in str(e)
            assert "KeyError" not in str(e)
        await cdp.close()

    asyncio.run(_run())


def test_sync_handler_supported():
    async def _run():
        cdp = CDPClient("ws://127.0.0.1:9/devtools/browser", event_queue_size=8)
        cdp._closed = False
        cdp._ensure_event_pump()
        got: list[str] = []

        def sync_handler(method: str, params: dict):
            got.append(method)

        cdp.on_event(sync_handler)
        cdp.dispatch_message({"method": "Page.frameNavigated", "params": {"frame": {}}})
        await asyncio.sleep(0.05)
        assert got == ["Page.frameNavigated"]
        assert cdp.event_dispatched == 1
        await cdp.close()

    asyncio.run(_run())


def test_ordered_dispatch_for_enqueued_events():
    async def _run():
        cdp = CDPClient("ws://127.0.0.1:9/devtools/browser", event_queue_size=64)
        cdp._closed = False
        cdp._ensure_event_pump()
        order: list[int] = []

        async def handler(method: str, params: dict):
            order.append(int(params["n"]))
            await asyncio.sleep(0)  # yield but preserve order via single worker

        cdp.on_event(handler)
        for i in range(20):
            cdp.dispatch_message({"method": "Page.lifecycleEvent", "params": {"n": i}})
        await asyncio.sleep(0.2)
        assert order == list(range(20))
        await cdp.close()

    asyncio.run(_run())
