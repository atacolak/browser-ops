"""Scratch wipe_v1 cleanup + named stable worker."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.adapters import scratch as S  # noqa: E402
from browserctl.errors import LeaseConflict  # noqa: E402
from browserctl.manager import Manager  # noqa: E402
from browserctl.profiles import ProfileRegistry  # noqa: E402
from browserctl.store import load_lease  # noqa: E402


@pytest.fixture
def ops(tmp_path: Path):
    root = tmp_path / "ops"
    (root / "profiles").mkdir(parents=True)
    state = root / "state"
    state.mkdir()
    return root, state


def _acq(root, state, **kw):
    return S.acquire({"root": str(root), "state_root": str(state), "no_start": True, **kw})


def _dead():
    return mock.patch.multiple(
        S,
        _daemon_pids=mock.Mock(return_value=[]),
        _chrome_pids=mock.Mock(return_value=[]),
        _cdp_alive=mock.Mock(return_value=False),
        _kill_pids=mock.Mock(),
    )


class _Ad:
    def acquire(self, r):
        return S.acquire({**r, "no_start": True})

    def release(self, lease, *, force=False):
        return S.release(lease, force=force)

    def status(self, lease):
        return {}


def test_token_worker_wipes(ops):
    root, state = ops
    out = _acq(root, state, label="demo")
    res = out["resources"]
    assert res["ephemeral_wipe_v1"] is True
    p, sd = Path(res["profile_dir"]), Path(res["state_dir"])
    (p / "x").write_text("1")
    (sd / "y").write_text("1")
    with _dead():
        rel = S.release({"worker_id": out["worker_id"], "resources": res})
    assert rel["status"] == "stopped" and rel["profile_wiped"] and rel["state_wiped"]
    assert not p.exists() and not sd.exists()


def test_explicit_and_request_override_never_wipe(ops):
    root, state = ops
    pinned = root / "profiles" / "scratch" / "pin"
    a = _acq(root, state, worker_id="scratch-stable")
    b = _acq(root, state, profile_dir=str(pinned))
    c = _acq(root, state, worker_id="scratch-forced", ephemeral_wipe_v1=True, ephemeral_profile=True)
    for out in (a, b, c):
        assert out["resources"]["ephemeral_wipe_v1"] is False
        p = Path(out["resources"]["profile_dir"])
        (p / "m").write_text("keep")
        with _dead():
            rel = S.release({"worker_id": out["worker_id"], "resources": out["resources"]})
        assert not rel["profile_wiped"] and p.exists()


def test_legacy_ephemeral_profile_without_wipe_v1_never_wipes(ops):
    """Main stamped ephemeral_profile=true on all scratches including --worker."""
    root, state = ops
    p = root / "profiles" / "scratch" / "scratch-legacy"
    p.mkdir(parents=True)
    (p / "Cookies").write_text("session")
    sd = root / "state" / "scratch-legacy"
    sd.mkdir(parents=True)
    res = {
        "worker_id": "scratch-legacy",
        "cdp_port": 0,
        "profile_dir": str(p),
        "state_dir": str(sd),
        "ops_root": str(root),
        "control_state_root": str(state),
        "ephemeral_profile": True,  # legacy only
    }
    with _dead():
        rel = S.release({"worker_id": "scratch-legacy", "resources": res})
    assert rel["status"] == "stopped" and not rel["ephemeral_wipe_v1"]
    assert not rel["profile_wiped"] and (p / "Cookies").read_text() == "session"


def test_process_alive_and_containment_partial(ops):
    root, state = ops
    out = _acq(root, state)
    p = Path(out["resources"]["profile_dir"])
    (p / "x").write_text("1")
    with mock.patch.object(S, "_daemon_pids", return_value=[9]):
        with mock.patch.object(S, "_chrome_pids", return_value=[]):
            with mock.patch.object(S, "_kill_pids"):
                with mock.patch.object(S, "_cdp_alive", return_value=False):
                    rel = S.release({"worker_id": out["worker_id"], "resources": out["resources"]})
    assert rel["status"] == "partial" and rel["cleanup_incomplete"] and p.exists()

    outside = root.parent / "evil"
    outside.mkdir()
    (outside / "s").write_text("n")
    ctl = root / "state"
    (ctl / "control" / "leases").mkdir(parents=True)
    marker = ctl / "control" / "leases" / "k.json"
    marker.write_text("{}")
    ok = root / "profiles" / "scratch" / "ok"
    ok.mkdir(parents=True)
    with _dead():
        r1 = S.release(
            {
                "worker_id": "e",
                "resources": {
                    "cdp_port": 0,
                    "ops_root": str(root),
                    "ephemeral_wipe_v1": True,
                    "profile_dir": str(outside),
                    "state_dir": str(root / "state" / "e"),
                    "control_state_root": str(state),
                },
            }
        )
        r2 = S.release(
            {
                "worker_id": "c",
                "resources": {
                    "cdp_port": 0,
                    "ops_root": str(root),
                    "ephemeral_wipe_v1": True,
                    "profile_dir": str(ok),
                    "state_dir": str(ctl),
                    "control_state_root": str(ctl),
                },
            }
        )
    assert r1["cleanup_incomplete"] and outside.exists()
    assert (r2["wipe_state"] or {}).get("reason") == "control_plane" and marker.exists()


def test_manager_retryable_and_reap_skip(ops):
    root, state = ops
    m = Manager(root=root, state_root=state)
    with mock.patch("browserctl.manager.get_adapter", return_value=_Ad()):
        out = m.acquire({"kind": "scratch", "owner": "t", "ttl": 60})
        lid = out["lease"]["lease_id"]
        p = Path(out["lease"]["resources"]["profile_dir"])
        with _dead():
            with mock.patch.object(
                S, "_wipe_dir", side_effect=lambda path, **kw: {"path": str(path), "wiped": False, "error": "E"}
            ):
                rel = m.release(lease_id=lid)
        assert not rel["ok"] and rel["retryable"] and load_lease(state, lid)["status"] == "expiring"
        with _dead():
            assert m.release(lease_id=lid)["lease"]["status"] == "released"
        assert not p.exists()

        out2 = m.acquire({"kind": "scratch", "owner": "t", "ttl": 9})
        lid2 = out2["lease"]["lease_id"]
        p2 = Path(out2["lease"]["resources"]["profile_dir"])
        with _dead():
            with mock.patch.object(
                S, "_wipe_dir", side_effect=lambda path, **kw: {"path": str(path), "wiped": False, "error": "E"}
            ):
                r = m.reap(force_lease_id=lid2)
        assert r["count"] == 0 and r["skipped"][0]["reason"] == "release_incomplete"
        assert load_lease(state, lid2)["status"] == "expiring" and p2.exists()


def test_named_scratch_stable_pool(ops):
    root, state = ops
    ProfileRegistry(root).register("lab", launch={"kind": "scratch", "label": "d"})
    ProfileRegistry(root).register("pin", launch={"kind": "scratch", "worker": "scratch-pin"})
    m = Manager(root=root, state_root=state)
    with mock.patch("browserctl.manager.get_adapter", return_value=_Ad()):
        o1 = m.acquire({"profile_name": "lab", "owner": "a", "ttl": 60})
        assert o1["lease"]["worker_id"] == "scratch-profile-lab"
        assert o1["lease"]["resources"]["ephemeral_wipe_v1"] is False
        p = Path(o1["lease"]["resources"]["profile_dir"])
        (p / "s").write_text("k")
        m.release(lease_id=o1["lease"]["lease_id"])
        assert (p / "s").read_text() == "k"
        o2 = m.acquire({"profile_name": "lab", "owner": "b", "ttl": 60})
        assert o2["lease"]["worker_id"] == "scratch-profile-lab"
        with pytest.raises(LeaseConflict):
            m.acquire({"profile_name": "lab", "owner": "c", "ttl": 60})
        m.release(lease_id=o2["lease"]["lease_id"])
        pin = m.acquire({"profile_name": "pin", "owner": "d", "ttl": 30})
        assert pin["lease"]["worker_id"] == "scratch-pin"
        assert pin["lease"]["resources"]["ephemeral_wipe_v1"] is False
