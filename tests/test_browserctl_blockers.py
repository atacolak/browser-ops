"""Review-blocker regression tests: attached conflict, scratch ports."""

from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.adapters import scratch as scratch_mod  # noqa: E402
from browserctl.errors import AdapterError, LeaseConflict  # noqa: E402
from browserctl.manager import Manager  # noqa: E402
from browserctl.store import find_active_lease_for_worker  # noqa: E402


class AttachedFakeAdapter:
    """VPN/attach-style adapter: existing runtime, never stop the winner."""

    name = "vpn"

    def __init__(self):
        self.starts = 0
        self.stops: list[dict[str, Any]] = []

    def acquire(self, request):
        self.starts += 1
        return {
            "worker_id": "vpn-test-worker",
            "kind": "vpn",
            "adapter": "vpn",
            "resources": {
                "worker_id": "vpn-test-worker",
                "cdp_port": 9400,
                "cdp_url": "http://127.0.0.1:9400",
                "daemon": "running",
            },
            "env": {
                "BROWSER_HARNESS_WORKER": "vpn-test-worker",
                "PYTHONPATH": str(ROOT),
                "BROWSER_ALLOW_EVALUATE": "1",
                "BROWSER_OPS_ROOT": str(ROOT),
            },
            "meta": {"attached_existing": True},
        }

    def release(self, lease, *, force=False):
        self.stops.append(
            {
                "lease_id": lease.get("lease_id"),
                "force": force,
                "worker": lease.get("worker_id"),
            }
        )
        return {"status": "stopped", "force": force}

    def status(self, lease):
        return {"cdp_alive": True, "daemon": "running"}


def test_attached_join_never_stops_winner(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    m = Manager(root=ROOT, state_root=state)
    fake = AttachedFakeAdapter()
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        first = m.acquire(
            {"kind": "vpn", "country": "SE", "owner": "a", "ttl": 120}
        )
        second = m.acquire({"kind": "vpn", "country": "SE", "owner": "b", "ttl": 120})
        assert first["ok"] is True and second["ok"] is True
        assert first["lease"]["target_id"] != second["lease"]["target_id"]
        assert first["browser_lease"]["lease_id"] == second["browser_lease"]["lease_id"]
        with pytest.raises(LeaseConflict):
            m.acquire(
                {
                    "kind": "vpn",
                    "country": "SE",
                    "owner": "c",
                    "ttl": 120,
                    "browser_exclusive": True,
                }
            )
    assert fake.stops == []
    active = find_active_lease_for_worker(state, "vpn-test-worker")
    assert active is not None
    assert active["lease_id"] == first["browser_lease"]["lease_id"]
    assert active["meta"].get("attached_existing") is True


def test_scratch_port_alloc_lock_serializes_select(tmp_path: Path, monkeypatch):
    """Parallel no_start acquires under same state_root get unique ports."""
    root = tmp_path / "ops"
    state = root / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(scratch_mod, "PORT_MIN", 19000)
    monkeypatch.setattr(scratch_mod, "PORT_MAX", 19020)

    holders: list[socket.socket] = []
    for p in range(19000, 19005):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
            s.listen(1)
            holders.append(s)
        except OSError:
            pass

    results: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(3)

    def one(i: int):
        try:
            barrier.wait(timeout=5)
            out = scratch_mod.acquire(
                {
                    "root": str(root),
                    "state_root": str(state),
                    "label": f"p{i}",
                    "no_start": True,
                    "headless": True,
                }
            )
            results.append(int(out["resources"]["cdp_port"]))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for s in holders:
        s.close()

    assert not errors, errors
    assert len(results) == 3
    assert len(set(results)) == 3
    assert all(19005 <= p <= 19020 for p in results)


def test_scratch_port_bind_retry_on_race(tmp_path: Path, monkeypatch):
    """Preferred busy port fails; unconstrained acquire picks another."""
    root = tmp_path / "ops"
    state = root / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(scratch_mod, "PORT_MIN", 19100)
    monkeypatch.setattr(scratch_mod, "PORT_MAX", 19105)

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 19100))
    holder.listen(1)
    try:
        with pytest.raises(AdapterError):
            scratch_mod.acquire(
                {
                    "root": str(root),
                    "state_root": str(state),
                    "cdp_port": 19100,
                    "no_start": True,
                }
            )
        out = scratch_mod.acquire(
            {
                "root": str(root),
                "state_root": str(state),
                "no_start": True,
                "label": "retry",
            }
        )
        assert out["resources"]["cdp_port"] != 19100
    finally:
        holder.close()
