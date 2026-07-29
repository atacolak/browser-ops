"""Lease manager: acquire/release/reap/duplicate/concurrency tests."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.errors import LeaseConflict, LeaseNotFound  # noqa: E402
from browserctl.manager import Manager  # noqa: E402
from browserctl.store import (  # noqa: E402
    find_active_lease_for_worker,
    is_expired,
    load_lease,
    save_lease,
)


class FakeAdapter:
    name = "scratch"
    starts: list[dict[str, Any]]
    stops: list[dict[str, Any]]

    def __init__(self, worker_id: str = "scratch-test-1"):
        self.worker_id = worker_id
        self.starts = []
        self.stops = []
        self._n = 0

    def acquire(self, request: dict[str, Any]) -> dict[str, Any]:
        self._n += 1
        wid = request.get("worker_id") or self.worker_id
        # allow unique workers when label provided
        if request.get("label") and not request.get("worker_id"):
            wid = f"scratch-{request['label']}-{self._n}"
        port = 9300 + self._n
        self.starts.append({"worker_id": wid, "request": dict(request)})
        return {
            "worker_id": wid,
            "kind": "scratch",
            "adapter": "scratch",
            "resources": {
                "worker_id": wid,
                "cdp_port": port,
                "cdp_url": f"http://127.0.0.1:{port}",
                "profile_dir": f"/tmp/{wid}",
                "state_dir": f"/tmp/state/{wid}",
                "socket": f"/tmp/state/{wid}/daemon.sock",
                "daemon": "running",
            },
            "env": {
                "BROWSER_HARNESS_WORKER": wid,
                "PYTHONPATH": str(ROOT),
                "BROWSER_ALLOW_EVALUATE": "1",
                "BROWSER_OPS_ROOT": str(ROOT),
            },
            "meta": {"fake": True},
        }

    def release(self, lease: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        self.stops.append({"lease_id": lease.get("lease_id"), "force": force})
        return {"status": "stopped", "force": force}

    def status(self, lease: dict[str, Any]) -> dict[str, Any]:
        return {
            "worker_id": lease.get("worker_id"),
            "cdp_alive": True,
            "daemon": "running",
        }


@pytest.fixture
def mgr(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    m = Manager(root=ROOT, state_root=state)
    fake = FakeAdapter()
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        yield m, fake, state


def test_acquire_returns_env_and_lease(mgr):
    m, fake, state = mgr
    out = m.acquire(
        {"kind": "scratch", "owner": "test", "mode": "persistent", "ttl": 60}
    )
    assert out["ok"] is True
    lease = out["lease"]
    assert lease["status"] == "active"
    assert lease["worker_id"] == "scratch-test-1"
    assert out["env"]["BROWSER_HARNESS_WORKER"] == "scratch-test-1"
    assert out["env"]["BROWSERCTL_LEASE_ID"] == lease["lease_id"]
    assert "BROWSER_TARGET_STATE" in out["env"]
    assert Path(out["env"]["BROWSER_TARGET_STATE"]).name == "active-target.json"
    assert len(fake.starts) == 1
    # persisted
    loaded = load_lease(state, lease["lease_id"])
    assert loaded is not None
    assert loaded["worker_id"] == "scratch-test-1"


def test_duplicate_lease_same_worker_conflicts(mgr):
    m, fake, state = mgr
    first = m.acquire({"kind": "scratch", "owner": "a", "ttl": 120})
    with pytest.raises(LeaseConflict) as ei:
        m.acquire({"kind": "scratch", "owner": "b", "ttl": 120})
    err = ei.value
    assert err.code == "LEASE_CONFLICT"
    assert err.details["worker_id"] == "scratch-test-1"
    # second acquire's adapter start was rolled back via force release
    assert len(fake.starts) == 2
    assert len(fake.stops) >= 1
    # first still active
    active = find_active_lease_for_worker(state, "scratch-test-1")
    assert active is not None
    assert active["lease_id"] == first["lease"]["lease_id"]


def test_release_idempotent(mgr):
    m, fake, _state = mgr
    out = m.acquire({"kind": "scratch", "owner": "a", "ttl": 60})
    lid = out["lease"]["lease_id"]
    r1 = m.release(lease_id=lid)
    assert r1["ok"] is True
    assert r1["lease"]["status"] == "released"
    r2 = m.release(lease_id=lid)
    assert r2["ok"] is True
    assert r2.get("idempotent") is True
    # adapter release called once for real work (second is no-op path)
    assert len(fake.stops) == 1


def test_reap_expired(mgr):
    m, fake, state = mgr
    out = m.acquire({"kind": "scratch", "owner": "a", "ttl": 1})
    lid = out["lease"]["lease_id"]
    # backdate expiry
    lease = load_lease(state, lid)
    assert lease is not None
    lease["expires_at"] = time.time() - 10
    save_lease(state, lease)
    assert is_expired(lease, now=time.time())

    result = m.reap()
    assert result["count"] == 1
    assert result["reaped"][0]["lease_id"] == lid
    assert result["reaped"][0]["status"] == "reaped"
    assert len(fake.stops) == 1

    # idempotent reap
    result2 = m.reap()
    assert result2["count"] == 0


def test_reap_force_lease(mgr):
    m, fake, _state = mgr
    out = m.acquire({"kind": "scratch", "owner": "a", "ttl": 9999})
    lid = out["lease"]["lease_id"]
    result = m.reap(force_lease_id=lid)
    assert result["count"] == 1
    assert result["reaped"][0]["status"] == "reaped"


def test_mark_expiring_shortens_ttl(mgr):
    m, _fake, state = mgr
    out = m.acquire({"kind": "scratch", "owner": "a", "ttl": 3600})
    lid = out["lease"]["lease_id"]
    marked = m.mark_expiring(lease_id=lid, ttl_seconds=30)
    assert marked["lease"]["status"] == "expiring"
    lease = load_lease(state, lid)
    assert lease is not None
    assert lease["ttl_seconds"] == 30
    assert lease["meta"].get("navigator_exited") is True


def test_list_hides_terminal(mgr):
    m, _fake, _state = mgr
    out = m.acquire({"kind": "scratch", "owner": "a", "ttl": 60})
    assert len(m.list_leases()) == 1
    m.release(lease_id=out["lease"]["lease_id"])
    assert len(m.list_leases()) == 0
    assert len(m.list_leases(include_terminal=True)) == 1


def test_concurrent_acquire_one_winner(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    m = Manager(root=ROOT, state_root=state)
    fake = FakeAdapter(worker_id="scratch-race")
    results: list[Any] = []
    errors: list[Any] = []

    def attempt(i: int):
        try:
            with mock.patch("browserctl.manager.get_adapter", return_value=fake):
                out = m.acquire(
                    {
                        "kind": "scratch",
                        "owner": f"t{i}",
                        "ttl": 120,
                        "worker_id": "scratch-race",
                    }
                )
                results.append(out)
        except LeaseConflict as e:
            errors.append(e)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1
    assert len(errors) == 7
    assert all(isinstance(e, LeaseConflict) for e in errors)
    active = find_active_lease_for_worker(state, "scratch-race")
    assert active is not None
    assert active["lease_id"] == results[0]["lease"]["lease_id"]


def test_no_managed_default(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    m = Manager(root=ROOT, state_root=state)

    class DefaultAdapter(FakeAdapter):
        def acquire(self, request):
            out = super().acquire(request)
            out["worker_id"] = "default"
            out["resources"]["worker_id"] = "default"
            return out

    with mock.patch("browserctl.manager.get_adapter", return_value=DefaultAdapter()):
        from browserctl.errors import InvalidRequest

        with pytest.raises(InvalidRequest):
            m.acquire({"kind": "scratch", "owner": "x", "ttl": 60})


def test_status_missing_lease(mgr):
    m, _fake, _state = mgr
    with pytest.raises(LeaseNotFound):
        m.status(lease_id="does-not-exist")


def test_launch_contract_json_shape(mgr):
    m, _fake, _state = mgr
    out = m.launch(
        {"kind": "scratch", "owner": "orch", "mode": "one_shot", "ttl": 90}
    )
    assert out["ok"] is True
    assert "env" in out
    assert "spawn" in out
    assert out["spawn"]["profile"] == "navigator"
    assert out["spawn"]["env"]["BROWSER_HARNESS_WORKER"]
    assert out["lease"]["mode"] == "one_shot"


def test_watch_binds_pane_ids(mgr):
    m, _fake, state = mgr
    out = m.acquire({"kind": "scratch", "owner": "a", "ttl": 120})
    lid = out["lease"]["lease_id"]

    fake_watch = {
        "agent_pane_id": "w1:p1",
        "watch_pane_id": "w1:p2",
        "direction": "right",
        "ratio": 0.25,
        "env": {
            "HERDR_BROWSER_MODE": "observe_mirror",
            "HERDR_BROWSER_TARGET_STATE": "/tmp/t.json",
            "HERDR_BROWSER_CDP_URL": "http://127.0.0.1:9301",
        },
        "target_state_path": "/tmp/t.json",
        "cdp_url": "http://127.0.0.1:9301",
    }
    with mock.patch(
        "browserctl.manager.watch_mod.start_watch", return_value=fake_watch
    ):
        w = m.watch(lease_id=lid, agent_pane="w1:p1")
    assert w["ok"] is True
    assert w["watch"]["watch_pane_id"] == "w1:p2"
    assert w["mirror_env"]["HERDR_BROWSER_MODE"] == "observe_mirror"
    lease = load_lease(state, lid)
    assert lease is not None
    assert lease["watch"]["agent_pane_id"] == "w1:p1"

    # idempotent second watch
    with mock.patch(
        "browserctl.manager.watch_mod.start_watch",
        side_effect=AssertionError("should not call"),
    ):
        w2 = m.watch(lease_id=lid)
    assert w2.get("idempotent") is True

    with mock.patch(
        "browserctl.manager.watch_mod.stop_watch",
        return_value={"closed": True, "watch_pane_id": "w1:p2"},
    ):
        u = m.unwatch(lease_id=lid)
    assert u["ok"] is True
    lease2 = load_lease(state, lid)
    assert lease2 is not None
    assert lease2["watch"] is None
