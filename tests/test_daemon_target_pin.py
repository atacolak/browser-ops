"""Daemon pin: concurrent RPCs with distinct target_ids must not cross-mutate.

CloakBackend has one current session_id/target_id. handle_client is one-shot
but two sockets can run concurrently on the event loop. Drive actions that
pass target_id must pin then run under the backend drive lock so navigate
never executes while a sibling target is current.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

from rpc import _execute_action, handle_client  # noqa: E402


class RecordingBackend:
    """Fake backend that records pin/navigate order and yields mid-navigate."""

    worker_id = "pin-test"

    def __init__(self) -> None:
        self.drive_lock = asyncio.Lock()
        self.target_id = "T-legacy"
        self.session_id = "S-legacy"
        self._sessions: dict[str, str] = {"T-legacy": "S-legacy"}
        self.log: list[tuple] = []
        self.switch_calls: list[str] = []

    async def pin_target(self, target_id: str) -> dict:
        self.log.append(("pin", target_id, self.target_id))
        self.switch_calls.append(target_id)
        self.target_id = target_id
        self.session_id = self._sessions.setdefault(target_id, f"S-{target_id}")
        return {"target_id": self.target_id, "session_id": self.session_id}

    async def switch_tab(self, target_id: str) -> dict:
        return await self.pin_target(target_id)

    async def navigate(self, url: str) -> dict:
        pinned = self.target_id
        self.log.append(("nav_begin", pinned, url))
        # Yield so a racer without the lock would pin a sibling mid-navigate.
        await asyncio.sleep(0.05)
        current = self.target_id
        self.log.append(("nav_end", pinned, url, current))
        if current != pinned:
            raise AssertionError(
                f"navigate({url!r}) began on {pinned} but current is {current}"
            )
        return {"navigated": url, "target_id": pinned}

    async def page_info(self) -> dict:
        return {"url": f"https://{self.target_id}.example", "title": self.target_id}


class _BufWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_omitted_target_id_keeps_current_session():
    async def _run():
        backend = RecordingBackend()
        result = await _execute_action(
            backend, "navigate", {"url": "https://legacy.example"}
        )
        assert result["navigated"] == "https://legacy.example"
        assert result["target_id"] == "T-legacy"
        assert backend.switch_calls == []
        assert backend.target_id == "T-legacy"
        assert ("pin", "T-legacy", "T-legacy") not in backend.log

    asyncio.run(_run())


def test_rpc_accepts_target_id_and_pins_before_navigate():
    async def _run():
        backend = RecordingBackend()
        result = await _execute_action(
            backend,
            "navigate",
            {"url": "https://a.example", "target_id": "TA"},
        )
        assert result["target_id"] == "TA"
        assert backend.target_id == "TA"
        assert backend.switch_calls == ["TA"]
        assert backend.log[0][0] == "pin"
        assert backend.log[0][1] == "TA"
        assert backend.log[1] == ("nav_begin", "TA", "https://a.example")

    asyncio.run(_run())


def test_interleaved_rpcs_never_navigate_on_sibling_target():
    """Two concurrent navigates with distinct target_ids must not cross-mutate."""

    async def _run():
        backend = RecordingBackend()
        results = await asyncio.gather(
            _execute_action(
                backend,
                "navigate",
                {"url": "https://a.example", "target_id": "TA"},
            ),
            _execute_action(
                backend,
                "navigate",
                {"url": "https://b.example", "target_id": "TB"},
            ),
        )
        by_url = {r["navigated"]: r["target_id"] for r in results}
        assert by_url["https://a.example"] == "TA"
        assert by_url["https://b.example"] == "TB"

        # Each nav_begin..nav_end pair must stay on one target; no pin of a
        # sibling may land between them.
        stack: list[str] = []
        for event in backend.log:
            kind = event[0]
            if kind == "pin":
                assert stack == [], (
                    f"pin {event[1]} while navigate on {stack} still in flight: "
                    f"{backend.log}"
                )
            elif kind == "nav_begin":
                stack.append(event[1])
            elif kind == "nav_end":
                pinned, _url, current = event[1], event[2], event[3]
                assert pinned == current
                assert stack and stack[-1] == pinned
                stack.pop()
        assert stack == []

        pins = [e[1] for e in backend.log if e[0] == "pin"]
        assert set(pins) == {"TA", "TB"}

    asyncio.run(_run())


def test_handle_client_json_line_passes_target_id():
    async def _run():
        backend = RecordingBackend()
        reader = asyncio.StreamReader()
        cmd = {"action": "navigate", "url": "https://c.example", "target_id": "TC"}
        reader.feed_data((json.dumps(cmd) + "\n").encode())
        reader.feed_eof()
        writer = _BufWriter()
        await handle_client(reader, writer, backend)
        body = json.loads(bytes(writer.buf).decode().strip())
        assert body.get("code") is None
        assert body["target_id"] == "TC"
        assert body["navigated"] == "https://c.example"
        assert backend.target_id == "TC"

    asyncio.run(_run())
