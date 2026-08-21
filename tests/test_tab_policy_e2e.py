"""Live two-client tab policy: peek / mint / close. Steal is operator-only.

Uses a unique scratch worker so operator profiles are untouched.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.errors import AdapterError, BrowserctlError  # noqa: E402
from browserctl.manager import Manager  # noqa: E402


def _rpc(sock: Path, payload: dict, timeout: float = 30.0) -> dict:
    data = (json.dumps(payload) + "\n").encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(sock))
        client.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        client.close()
    if not buf:
        raise AssertionError(f"empty daemon response for {payload.get('action')}")
    return json.loads(buf.split(b"\n", 1)[0].decode())


def test_two_clients_peek_mint_close():
    worker = f"scratch-tab-e2e-{uuid.uuid4().hex[:8]}"
    m = Manager(root=ROOT)
    a = b = None
    try:
        try:
            a = m.launch(
                {
                    "kind": "scratch",
                    "worker_id": worker,
                    "owner": "nav-a",
                    "mode": "one_shot",
                    "ttl": 180,
                    "headless": True,
                    "label": "tab-e2e",
                }
            )
            b = m.launch(
                {
                    "kind": "scratch",
                    "worker_id": worker,
                    "owner": "nav-b",
                    "mode": "one_shot",
                    "ttl": 180,
                    "headless": True,
                }
            )
        except (AdapterError, BrowserctlError) as e:
            pytest.skip(f"scratch launch failed: {e}")

        assert a["lease"]["worker_id"] == b["lease"]["worker_id"] == worker
        assert a["lease"]["target_id"] != b["lease"]["target_id"]
        sock = Path(a["lease"]["resources"]["socket"])
        assert sock.exists(), f"daemon.sock missing after launch: {sock}"

        ping = _rpc(sock, {"action": "ping"})
        assert ping.get("ok") is True

        ta = a["lease"]["target_id"]
        tb = b["lease"]["target_id"]
        la = a["lease"]["lease_id"]
        lb = b["lease"]["lease_id"]

        nav_a = _rpc(
            sock,
            {
                "action": "navigate",
                "url": "https://example.com",
                "target_id": ta,
                "lease_id": la,
                "held_lease_ids": [la],
            },
            timeout=60,
        )
        assert nav_a.get("code") is None, nav_a
        nav_b = _rpc(
            sock,
            {
                "action": "navigate",
                "url": "https://example.org",
                "target_id": tb,
                "lease_id": lb,
                "held_lease_ids": [lb],
            },
            timeout=60,
        )
        assert nav_b.get("code") is None, nav_b

        listed = _rpc(sock, {"action": "tabs", "lease_id": la, "held_lease_ids": [la]})
        by_id = {
            t.get("targetId") or t.get("target_id"): t.get("ownership")
            for t in listed.get("tabs") or []
        }
        assert by_id.get(ta) == "owned_by_me"
        assert by_id.get(tb) == "owned_by"

        peek = _rpc(
            sock,
            {
                "action": "switch_tab",
                "lease_id": la,
                "held_lease_ids": [la],
                "dest_target_id": tb,
            },
        )
        assert peek.get("mode") == "peek", peek
        assert peek.get("activated") is False
        page = peek.get("page") or {}
        assert "example.org" in (page.get("url") or "")

        still_b = _rpc(
            sock,
            {"action": "page_info", "target_id": tb, "lease_id": lb, "held_lease_ids": [lb]},
        )
        assert "example.org" in (still_b.get("url") or "")
        still_a = _rpc(
            sock,
            {"action": "page_info", "target_id": ta, "lease_id": la, "held_lease_ids": [la]},
        )
        assert "example.com" in (still_a.get("url") or "")

        conflict = _rpc(
            sock,
            {
                "action": "navigate",
                "url": "https://example.net",
                "target_id": tb,
                "lease_id": la,
                "held_lease_ids": [la],
            },
        )
        assert conflict.get("code") == "TARGET_CONFLICT"

        steal = _rpc(
            sock,
            {
                "action": "switch_tab",
                "lease_id": la,
                "held_lease_ids": [la],
                "dest_target_id": tb,
                "steal": True,
            },
        )
        assert steal.get("code") == "STEAL_FORBIDDEN", steal

        minted = _rpc(
            sock,
            {
                "action": "new_tab",
                "url": "https://example.net",
                "lease_id": la,
                "held_lease_ids": [la],
                "target_id": ta,
            },
            timeout=60,
        )
        assert minted.get("ownership") == "owned_by_me", minted
        new_tid = minted.get("target_id")
        new_lid = minted.get("lease_id")
        assert new_tid and new_lid and new_tid not in {ta, tb}

        b_close = _rpc(
            sock,
            {
                "action": "close_tab",
                "lease_id": lb,
                "held_lease_ids": [lb],
                "dest_target_id": ta,
            },
        )
        assert b_close.get("code") == "TARGET_CONFLICT", b_close

        closed = _rpc(
            sock,
            {
                "action": "close_tab",
                "lease_id": new_lid,
                "held_lease_ids": [la, new_lid],
                "dest_target_id": new_tid,
            },
        )
        assert closed.get("closed") == new_tid, closed
    finally:
        for row in (a, b):
            if not row:
                continue
            try:
                m.release(lease_id=row["lease"]["lease_id"], force=True)
            except Exception:
                pass
        try:
            m.release(worker_id=worker, force=True)
        except Exception:
            pass
        # last release may already have stopped chrome; give the sock a beat
        time.sleep(0.2)
