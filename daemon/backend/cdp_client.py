"""
CDPClient — minimal WebSocket client for the Chrome DevTools Protocol.

Zero external dependencies: uses only ``asyncio`` + ``json`` + standard library
for the WebSocket handshake and frame parsing.

Critical invariant
------------------
The receive loop must **never** await user event handlers. Command responses
and events share one WebSocket; awaiting a slow/blocking handler starves
``send()`` futures (live symptom: Runtime.evaluate / page_info timeouts after
navigation event storms). Events are dispatched via a bounded queue and a
single ordered worker task.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

# Cap in-flight CDP events. Excess are dropped (oldest-preserving put_nowait fail).
DEFAULT_EVENT_QUEUE_SIZE = 256

EventHandler = Callable[[str, dict], Any]


class CDPClient:
    """Minimal CDP WebSocket client — no external dependencies.

    Usage::

        cdp = CDPClient("ws://127.0.0.1:9222/devtools/page/…")
        await cdp.connect()
        result = await cdp.send("Page.navigate", {"url": "https://example.com"})
        await cdp.close()
    """

    def __init__(
        self,
        ws_url: str,
        *,
        event_queue_size: int = DEFAULT_EVENT_QUEUE_SIZE,
    ):
        self.ws_url = ws_url
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._event_handler: EventHandler | None = None
        self._recv_task: asyncio.Task | None = None
        self._event_queue_size = max(1, int(event_queue_size))
        self._event_queue: asyncio.Queue[tuple[str, dict] | None] | None = None
        self._event_worker_task: asyncio.Task | None = None
        self._event_dropped = 0
        self._event_dispatched = 0
        self._closed = False

    @property
    def event_dropped(self) -> int:
        return self._event_dropped

    @property
    def event_dispatched(self) -> int:
        return self._event_dispatched

    async def connect(self, timeout: float = 10.0):
        """Connect to the CDP WebSocket endpoint."""
        self._closed = False
        parsed = urlparse(self.ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9222
        path = parsed.path or "/"

        # Open TCP connection
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )

        # WebSocket upgrade handshake
        key = os.urandom(16).hex()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self._writer.write(request.encode())
        await self._writer.drain()

        response = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
        if b"101" not in response:
            raise RuntimeError(f"WebSocket upgrade failed: {response.decode().strip()}")

        # Consume remaining headers
        while True:
            line = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
            if line == b"\r\n":
                break

        self._ensure_event_pump()
        # Start receive loop
        self._recv_task = asyncio.create_task(self._recv_loop())

    def _ensure_event_pump(self) -> None:
        """Start bounded event queue + single worker if needed."""
        if self._event_queue is None:
            self._event_queue = asyncio.Queue(maxsize=self._event_queue_size)
        if self._event_worker_task is None or self._event_worker_task.done():
            self._event_worker_task = asyncio.create_task(
                self._event_worker(), name="cdp-event-worker"
            )

    async def _event_worker(self) -> None:
        """Serially run user handlers; never runs on the recv path."""
        assert self._event_queue is not None
        while True:
            item = await self._event_queue.get()
            try:
                if item is None:
                    return
                method, params = item
                handler = self._event_handler
                if handler is None:
                    continue
                try:
                    result = handler(method, params)
                    if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                        await result  # type: ignore[misc]
                    self._event_dispatched += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(
                        f"[cdp] event handler error method={method!r}: {exc}",
                        file=sys.stderr,
                    )
            finally:
                self._event_queue.task_done()

    def _enqueue_event(self, method: str, params: dict) -> None:
        """Non-blocking enqueue; drop when saturated (bounds storm)."""
        self._ensure_event_pump()
        assert self._event_queue is not None
        try:
            self._event_queue.put_nowait((method, params if params is not None else {}))
        except asyncio.QueueFull:
            self._event_dropped += 1

    def dispatch_message(self, msg: dict[str, Any]) -> None:
        """
        Handle one decoded CDP message (responses + events).

        Safe to call from tests without a live socket. Responses resolve
        pending futures immediately; events are queued never awaited here.
        """
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending:
            fut = self._pending.pop(msg_id, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        # Unsolicited id (late/unknown) — ignore
        if msg_id is not None:
            return
        method = msg.get("method") or ""
        if not method:
            return
        if self._event_handler is None:
            return
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        self._enqueue_event(method, params)

    async def _recv_loop(self):
        """Read WebSocket frames and dispatch responses/events."""
        buffer = bytearray()
        try:
            while True:
                chunk = await self._reader.read(8192)
                if not chunk:
                    break
                buffer.extend(chunk)

                while len(buffer) >= 2:
                    opcode = buffer[0] & 0x0F
                    if opcode == 8:  # Close
                        return
                    if opcode == 9:  # Ping
                        await self._send_frame(10, b"")  # Pong
                        del buffer[:2]
                        continue

                    # Parse frame length (byte 1: MASK bit + 7-bit length)
                    masked = (buffer[1] & 0x80) != 0
                    payload_len = buffer[1] & 0x7F
                    header_len = 2
                    if payload_len == 126:
                        if len(buffer) < 4:
                            break
                        payload_len = int.from_bytes(buffer[2:4], "big")
                        header_len = 4
                    elif payload_len == 127:
                        if len(buffer) < 10:
                            break
                        payload_len = int.from_bytes(buffer[2:10], "big")
                        header_len = 10

                    # Mask key (4 bytes, only present when MASK bit is set)
                    payload_start = header_len
                    if masked:
                        payload_start += 4
                    if len(buffer) < payload_start + payload_len:
                        break

                    if masked:
                        mask = buffer[header_len:header_len + 4]
                        payload = bytes(
                            b ^ mask[i % 4]
                            for i, b in enumerate(
                                buffer[payload_start:payload_start + payload_len]
                            )
                        )
                    else:
                        payload = bytes(buffer[payload_start:payload_start + payload_len])

                    del buffer[:payload_start + payload_len]

                    try:
                        msg = json.loads(payload.decode("utf-8"))
                    except Exception:
                        continue

                    # NEVER await user handlers here — see module docstring.
                    self.dispatch_message(msg)
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def _send_frame(self, opcode: int, payload: bytes):
        """Send a WebSocket frame (client-to-server, MUST be masked per RFC 6455 §5.1)."""
        length = len(payload)
        # Byte 0: FIN + opcode; Byte 1: MASK bit + length
        if length < 126:
            header = bytearray([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytearray([0x80 | opcode, 0x80 | 126])
            header.extend(length.to_bytes(2, "big"))
        else:
            header = bytearray([0x80 | opcode, 0x80 | 127])
            header.extend(length.to_bytes(8, "big"))

        # Client-to-server frames MUST include a 4-byte masking key
        mask_key = os.urandom(4)
        header.extend(mask_key)

        # Mask the payload: payload[i] ^= mask_key[i % 4]
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        self._writer.write(bytes(header) + masked)
        await self._writer.drain()

    async def send(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
    ) -> dict:
        """Send a CDP command and wait for response.

        Returns the full CDP response dict. Raises RuntimeError on timeout
        or when Chrome replies with an error object (no silent KeyError later).
        """
        self._msg_id += 1
        msg_id = self._msg_id
        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[msg_id] = future
        await self._send_frame(1, json.dumps(msg).encode("utf-8"))

        try:
            reply = await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"CDP command timed out: {method}")
        err = reply.get("error") if isinstance(reply, dict) else None
        if isinstance(err, dict):
            text = err.get("message") or str(err)
            raise RuntimeError(f"CDP {method} failed: {text}")
        return reply

    def on_event(self, handler: EventHandler):
        """Register event handler (sync or async).

        The handler is invoked as ``handler(method: str, params: dict)`` on a
        dedicated worker — **not** on the WebSocket receive loop. Slow handlers
        cannot block command response dispatch. Both sync and async callables
        are supported; exceptions are logged and swallowed.
        """
        self._event_handler = handler
        # Pump may already be running from connect(); safe if not.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if not self._closed:
            self._ensure_event_pump()

    async def close(self):
        """Close the WebSocket connection and stop event pump."""
        self._closed = True
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        q = self._event_queue
        if q is not None:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                    q.task_done()
                except Exception:
                    pass
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
        if self._event_worker_task is not None:
            try:
                await asyncio.wait_for(self._event_worker_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._event_worker_task.cancel()
                try:
                    await self._event_worker_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._event_worker_task = None
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        # Fail any pending commands
        for mid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(RuntimeError("CDP connection closed"))
            self._pending.pop(mid, None)
