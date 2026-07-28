"""Integration-like: navigate/load events must refresh active-target url/title/seq.

Mirrors the live smoke failure where toolbar stayed about:blank after harness
navigate because only a provisional publish ran and no Page.* event republished
live page_info.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

from daemon.target_state import read_json  # noqa: E402


def _make_backend(tmp_path: Path, monkeypatch, worker_id: str = "nav-pub"):
    target = tmp_path / "state" / worker_id / "control" / "active-target.json"
    target.parent.mkdir(parents=True)
    monkeypatch.setenv("BROWSER_TARGET_STATE", str(target))
    monkeypatch.delenv("BROWSER_OPS_STATE", raising=False)
    from backend.cloak import CloakBackend  # type: ignore

    b = CloakBackend(worker_id, cdp_port=9333)
    b._connected = True
    b.session_id = "sess-1"
    b.target_id = "TID-1"
    b._cdp_http_url = "http://127.0.0.1:9333"
    b._main_frame_id = "FRAME-MAIN"

    # Tests assert on-disk state immediately; force sync publish (production
    # event path still offloads to executor so the CDP loop stays free).
    _orig = b._publish_active_target

    def _sync_publish(*args, **kwargs):
        kwargs["sync"] = True
        return _orig(*args, **kwargs)

    b._publish_active_target = _sync_publish  # type: ignore[method-assign]
    return b, target


async def _flush_executor() -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: None)


class FakeCDP:
    """Minimal CDP stand-in: records sends, supports on_event dispatch."""

    def __init__(self):
        self.handler = None
        self.sent: list[tuple[str, dict | None, str | None]] = []
        self._nav_url = "about:blank"
        self._title = ""
        self._ready = "complete"

    def on_event(self, handler):
        self.handler = handler

    async def send(self, method: str, params: dict | None = None, session_id: str | None = None):
        self.sent.append((method, params, session_id))
        if method == "Page.navigate":
            self._nav_url = (params or {}).get("url") or self._nav_url
            return {"result": {"frameId": "FRAME-MAIN", "loaderId": "L1"}}
        if method == "Page.getFrameTree":
            return {
                "result": {
                    "frameTree": {
                        "frame": {
                            "id": "FRAME-MAIN",
                            "url": self._nav_url,
                        }
                    }
                }
            }
        if method == "Runtime.evaluate":
            expr = (params or {}).get("expression", "")
            if "readyState" in expr:
                return {"result": {"result": {"value": self._ready}}}
            if "location.href" in expr or "JSON.stringify" in expr:
                payload = json.dumps(
                    {
                        "url": self._nav_url,
                        "title": self._title,
                        "w": 800,
                        "h": 600,
                        "sx": 0,
                        "sy": 0,
                        "pw": 800,
                        "ph": 600,
                    }
                )
                return {"result": {"result": {"value": payload}}}
            return {"result": {"result": {"value": None}}}
        return {"result": {}}

    async def emit(self, method: str, params: dict):
        if self.handler:
            await self.handler(method, params)


def test_navigate_publishes_live_url_and_title_after_load(tmp_path, monkeypatch):
    async def _run():
        b, target = _make_backend(tmp_path, monkeypatch)
        cdp = FakeCDP()
        b.cdp = cdp

        b._publish_active_target({"url": "about:blank", "target_id": b.target_id}, force=True)
        doc0 = read_json(target)
        assert doc0 is not None
        assert doc0["seq"] == 1
        assert doc0["page"]["url"] == "about:blank"

        cdp._nav_url = "https://example.com/"
        cdp._title = "Example Domain"
        cdp._ready = "complete"

        result = await b.navigate("https://example.com/")
        assert isinstance(result, dict)

        doc = read_json(target)
        assert doc is not None
        assert doc["page"]["url"] == "https://example.com/"
        assert doc["page"]["title"] == "Example Domain"
        assert doc["seq"] > doc0["seq"]
        assert doc["active_target_id"] == "TID-1"

    asyncio.run(_run())


def test_frame_navigated_and_load_events_refresh_target_state(tmp_path, monkeypatch):
    async def _run():
        b, target = _make_backend(tmp_path, monkeypatch)
        cdp = FakeCDP()
        b.cdp = cdp
        # Wire the same event tap shape as connect()
        async def _event_tap(method: str, params: dict):
            b._events.append({"method": method, "params": params})
            b._on_page_event(method, params or {})

        cdp.on_event(_event_tap)

        b._publish_active_target({"url": "about:blank", "target_id": b.target_id}, force=True)
        seq_blank = read_json(target)["seq"]

        cdp._nav_url = "https://example.com/"
        cdp._title = "Example Domain"

        await cdp.emit(
            "Page.frameNavigated",
            {
                "frame": {
                    "id": "FRAME-MAIN",
                    "url": "https://example.com/",
                    "name": "",
                }
            },
        )
        mid = read_json(target)
        assert mid["page"]["url"] == "https://example.com/"
        assert mid["seq"] > seq_blank

        await cdp.emit("Page.loadEventFired", {"timestamp": 1.0})
        await asyncio.sleep(0.2)

        final = read_json(target)
        assert final is not None
        assert final["page"]["url"] == "https://example.com/"
        assert final["page"]["title"] == "Example Domain"
        assert final["seq"] >= mid["seq"]

    asyncio.run(_run())


def test_iframe_frame_navigated_does_not_clobber_main_url(tmp_path, monkeypatch):
    async def _run():
        b, target = _make_backend(tmp_path, monkeypatch)
        cdp = FakeCDP()
        b.cdp = cdp

        async def _event_tap(method: str, params: dict):
            b._on_page_event(method, params or {})

        cdp.on_event(_event_tap)

        b._publish_active_target(
            {
                "url": "https://example.com/",
                "title": "Example Domain",
                "target_id": b.target_id,
            },
            force=True,
        )
        before = read_json(target)

        await cdp.emit(
            "Page.frameNavigated",
            {
                "frame": {
                    "id": "FRAME-IFRAME",
                    "parentId": "FRAME-MAIN",
                    "url": "https://ads.example/tracker",
                }
            },
        )
        await asyncio.sleep(0.05)
        after = read_json(target)
        assert after["page"]["url"] == "https://example.com/"
        assert after["seq"] == before["seq"]

    asyncio.run(_run())


def test_navigated_within_document_updates_url(tmp_path, monkeypatch):
    async def _run():
        b, target = _make_backend(tmp_path, monkeypatch)
        cdp = FakeCDP()
        b.cdp = cdp

        async def _event_tap(method: str, params: dict):
            b._on_page_event(method, params or {})

        cdp.on_event(_event_tap)

        b._publish_active_target(
            {
                "url": "https://example.com/",
                "title": "Example Domain",
                "target_id": b.target_id,
            },
            force=True,
        )
        cdp._nav_url = "https://example.com/#section"
        cdp._title = "Example Domain"

        await cdp.emit(
            "Page.navigatedWithinDocument",
            {"frameId": "FRAME-MAIN", "url": "https://example.com/#section"},
        )
        await asyncio.sleep(0.2)
        doc = read_json(target)
        assert doc["page"]["url"] == "https://example.com/#section"
        assert doc["seq"] > 1

    asyncio.run(_run())


def test_wait_for_load_schedules_refresh(tmp_path, monkeypatch):
    async def _run():
        b, target = _make_backend(tmp_path, monkeypatch)
        cdp = FakeCDP()
        b.cdp = cdp
        b._publish_active_target({"url": "about:blank", "target_id": b.target_id}, force=True)
        cdp._nav_url = "https://example.com/done"
        cdp._title = "Done"
        cdp._ready = "complete"

        await b.wait_for_load(timeout=2.0)
        await asyncio.sleep(0.15)
        doc = read_json(target)
        assert doc["page"]["url"] == "https://example.com/done"
        assert doc["page"]["title"] == "Done"

    asyncio.run(_run())


def test_on_page_event_frame_navigated_sync_publish(tmp_path, monkeypatch):
    """_on_page_event must publish without requiring a running event loop task first."""
    b, target = _make_backend(tmp_path, monkeypatch)
    b._publish_active_target({"url": "about:blank", "target_id": b.target_id}, force=True)
    # No cdp/session → schedule is no-op, but provisional sync publish still works
    b.cdp = None
    b._on_page_event(
        "Page.frameNavigated",
        {"frame": {"id": "F1", "url": "https://example.com/"}},
    )
    doc = read_json(target)
    assert doc["page"]["url"] == "https://example.com/"
    assert doc["seq"] >= 2
