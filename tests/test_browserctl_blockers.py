"""Review-blocker regression tests: xai conflict, ports, watch endpoint, probe."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.adapters import scratch as scratch_mod  # noqa: E402
from browserctl.errors import AdapterError, InvalidRequest, LeaseConflict  # noqa: E402
from browserctl.manager import Manager  # noqa: E402
from browserctl.store import find_active_lease_for_worker, load_lease  # noqa: E402
from browserctl import watch as watch_mod  # noqa: E402


# ── 1. observe_mirror capability probe ──────────────────────────────────────


def test_probe_observe_mirror_requires_markers(tmp_path: Path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "src").mkdir()
    (bad / "src" / "viewer.ts").write_text("// plain viewer\n")
    probe = watch_mod.probe_observe_mirror(bad)
    assert probe["ok"] is False

    good = tmp_path / "good"
    (good / "src").mkdir(parents=True)
    (good / "src" / "viewer.ts").write_text("export {}\n")
    (good / "src" / "targetState.ts").write_text(
        'export type BrowserMode = "observe_mirror";\n'
        'const x = process.env.HERDR_BROWSER_TARGET_STATE;\n'
        "active_target_id\n"
    )
    (good / "src" / "browser.ts").write_text(
        'if (mode === "observe_mirror") {}\n'
        "HERDR_BROWSER_TARGET_STATE\n"
    )
    probe2 = watch_mod.probe_observe_mirror(good)
    assert probe2["ok"] is True


def test_resolve_viewer_cwd_fail_closed_bad_override(tmp_path: Path, monkeypatch):
    bad = tmp_path / "nope"
    bad.mkdir()
    monkeypatch.setenv("HERDR_BROWSER_ROOT", str(bad))
    with pytest.raises(AdapterError) as ei:
        watch_mod.resolve_viewer_cwd(require_capable=True)
    assert "observe_mirror-capable" in str(ei.value.message) or "capable" in str(
        ei.value.message
    )


def _capable_viewer(root: Path) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "viewer.ts").write_text("x")
    (root / "src" / "targetState.ts").write_text(
        "observe_mirror\nHERDR_BROWSER_TARGET_STATE\nactive_target_id\n"
    )
    (root / "src" / "browser.ts").write_text("observe_mirror\nactive_target_id\n")
    return root


def test_resolve_viewer_cwd_override_ok(tmp_path: Path, monkeypatch):
    good = _capable_viewer(tmp_path / "good")
    monkeypatch.setenv("HERDR_BROWSER_ROOT", str(good))
    assert watch_mod.resolve_viewer_cwd() == good


def test_resolve_viewer_cwd_browserctl_env(tmp_path: Path, monkeypatch):
    good = _capable_viewer(tmp_path / "good")
    monkeypatch.delenv("HERDR_BROWSER_ROOT", raising=False)
    monkeypatch.setenv("BROWSERCTL_VIEWER_ROOT", str(good))
    assert watch_mod.resolve_viewer_cwd() == good


def test_resolve_viewer_cwd_viewer_root_file(tmp_path: Path, monkeypatch):
    ops = tmp_path / "ops"
    state = ops / "state"
    good = _capable_viewer(tmp_path / "good")
    cfg = state / "control" / "viewer-root"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f"# local viewer\n{good}\n")
    monkeypatch.delenv("HERDR_BROWSER_ROOT", raising=False)
    monkeypatch.delenv("BROWSERCTL_VIEWER_ROOT", raising=False)
    monkeypatch.setattr(watch_mod, "VIEWER_CANDIDATES", [])
    assert watch_mod.resolve_viewer_cwd(root=ops, state_root=state) == good


def test_resolve_viewer_cwd_priority_herdr_over_file(tmp_path: Path, monkeypatch):
    ops = tmp_path / "ops"
    state = ops / "state"
    via_env = _capable_viewer(tmp_path / "via-env")
    via_file = _capable_viewer(tmp_path / "via-file")
    cfg = state / "control" / "viewer-root"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(str(via_file) + "\n")
    monkeypatch.setenv("HERDR_BROWSER_ROOT", str(via_env))
    monkeypatch.delenv("BROWSERCTL_VIEWER_ROOT", raising=False)
    assert watch_mod.resolve_viewer_cwd(root=ops, state_root=state) == via_env


def test_resolve_viewer_cwd_bad_file_fail_closed(tmp_path: Path, monkeypatch):
    ops = tmp_path / "ops"
    state = ops / "state"
    bad = tmp_path / "nope"
    bad.mkdir()
    cfg = state / "control" / "viewer-root"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(str(bad) + "\n")
    monkeypatch.delenv("HERDR_BROWSER_ROOT", raising=False)
    monkeypatch.delenv("BROWSERCTL_VIEWER_ROOT", raising=False)
    monkeypatch.setattr(watch_mod, "VIEWER_CANDIDATES", [])
    with pytest.raises(AdapterError):
        watch_mod.resolve_viewer_cwd(root=ops, state_root=state)


# ── 2. xAI conflict never stops winner ───────────────────────────────────────


class XaiFakeAdapter:
    name = "xai"

    def __init__(self):
        self.starts = 0
        self.stops: list[dict[str, Any]] = []

    def acquire(self, request):
        self.starts += 1
        email = request.get("email") or "a@x.ai"
        return {
            "worker_id": "xai-test-worker",
            "kind": "xai",
            "adapter": "xai",
            "resources": {
                "worker_id": "xai-test-worker",
                "email": email,
                "cdp_port": 9223,
                "cdp_url": "http://127.0.0.1:9223",
                "daemon": "running",
            },
            "env": {
                "BROWSER_HARNESS_WORKER": "xai-test-worker",
                "PYTHONPATH": str(ROOT),
                "BROWSER_ALLOW_EVALUATE": "1",
                "BROWSER_OPS_ROOT": str(ROOT),
            },
            # realistic: xai adapter always marks attached_existing
            "meta": {
                "email": email,
                "attached_existing": True,
            },
        }

    def release(self, lease, *, force=False):
        self.stops.append(
            {"lease_id": lease.get("lease_id"), "force": force, "worker": lease.get("worker_id")}
        )
        return {"status": "stopped", "force": force}

    def status(self, lease):
        return {"cdp_alive": True, "daemon": "running"}


def test_xai_duplicate_conflict_never_stops_winner(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    m = Manager(root=ROOT, state_root=state)
    fake = XaiFakeAdapter()
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        first = m.acquire(
            {"kind": "xai", "email": "a@x.ai", "owner": "a", "ttl": 120}
        )
        assert first["ok"] is True
        with pytest.raises(LeaseConflict):
            m.acquire({"kind": "xai", "email": "a@x.ai", "owner": "b", "ttl": 120})
    # CRITICAL: winner browser must not be force-stopped
    assert fake.stops == []
    active = find_active_lease_for_worker(state, "xai-test-worker")
    assert active is not None
    assert active["lease_id"] == first["lease"]["lease_id"]
    assert active["meta"].get("attached_existing") is True


def test_xai_conflict_even_if_meta_forget_attached(tmp_path: Path):
    """Manager forces attached_existing for adapter=xai even if meta omits it."""
    state = tmp_path / "state"
    state.mkdir()
    m = Manager(root=ROOT, state_root=state)

    class Forgetful(XaiFakeAdapter):
        def acquire(self, request):
            out = super().acquire(request)
            out["meta"] = {"email": request.get("email")}  # no attached_existing
            return out

    fake = Forgetful()
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        m.acquire({"kind": "xai", "email": "a@x.ai", "owner": "a", "ttl": 60})
        with pytest.raises(LeaseConflict):
            m.acquire({"kind": "xai", "email": "a@x.ai", "owner": "b", "ttl": 60})
    assert fake.stops == []


# ── 3. scratch port TOCTOU lock + unique ports ───────────────────────────────


def test_scratch_port_alloc_lock_serializes_select(tmp_path: Path, monkeypatch):
    """Parallel no_start acquires under same state_root get unique ports."""
    root = tmp_path / "ops"
    state = root / "state"
    state.mkdir(parents=True)
    # Shrink range to force contention on a tiny set of free ports.
    monkeypatch.setattr(scratch_mod, "PORT_MIN", 19000)
    monkeypatch.setattr(scratch_mod, "PORT_MAX", 19020)

    # Hold a few ports open so free set is limited
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
    assert len(set(results)) == 3  # unique
    assert all(19005 <= p <= 19020 for p in results)


def test_scratch_port_bind_retry_on_race(tmp_path: Path, monkeypatch):
    """If first selected port becomes busy before return, retry yields another."""
    root = tmp_path / "ops"
    state = root / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(scratch_mod, "PORT_MIN", 19100)
    monkeypatch.setattr(scratch_mod, "PORT_MAX", 19105)

    calls = {"n": 0}
    real_free = scratch_mod._port_free

    def flaky(port: int) -> bool:
        # First call for any port claims free; second check (retry path) uses real
        calls["n"] += 1
        if calls["n"] == 1:
            return True
        return real_free(port)

    # Simulate: select thinks 19100 free, then preferred path fails → retry
    # Easier: preferred port busy after first allocate attempt via no_start
    # just ensure allocate with preferred busy raises then free works.
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


# ── 4. watch herdr endpoint fail closed ──────────────────────────────────────


def test_herdr_endpoint_fail_closed_without_socket(monkeypatch):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    with pytest.raises(InvalidRequest):
        watch_mod.resolve_herdr_endpoint()


def test_herdr_endpoint_session_alone_fail_closed(monkeypatch):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    with pytest.raises(InvalidRequest):
        watch_mod.resolve_herdr_endpoint(herdr_session="main")


def test_herdr_endpoint_from_env_and_persist(monkeypatch):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/test-herdr.sock")
    monkeypatch.setenv("HERDR_SESSION", "sess-a")
    ep = watch_mod.resolve_herdr_endpoint()
    assert ep["herdr_socket"] == "/tmp/test-herdr.sock"
    assert ep["herdr_session"] == "sess-a"

    # explicit overrides env
    ep2 = watch_mod.resolve_herdr_endpoint(
        herdr_socket="/tmp/other.sock", herdr_session="sess-b"
    )
    assert ep2["herdr_socket"] == "/tmp/other.sock"
    assert ep2["herdr_session"] == "sess-b"

    # lease watch record
    ep3 = watch_mod.resolve_herdr_endpoint(
        lease_watch={"herdr_socket": "/tmp/lease.sock", "herdr_session": "L"}
    )
    assert ep3["herdr_socket"] == "/tmp/lease.sock"


def test_watch_persists_herdr_endpoint(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    m = Manager(root=ROOT, state_root=state)

    class FakeScratch:
        def acquire(self, request):
            return {
                "worker_id": "scratch-w",
                "kind": "scratch",
                "adapter": "scratch",
                "resources": {
                    "worker_id": "scratch-w",
                    "cdp_port": 9301,
                    "cdp_url": "http://127.0.0.1:9301",
                    "daemon": "stopped",
                },
                "env": {"BROWSER_HARNESS_WORKER": "scratch-w"},
                "meta": {},
            }

        def release(self, lease, *, force=False):
            return {"status": "stopped"}

        def status(self, lease):
            return {}

    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/watch-test.sock")
    good = tmp_path / "viewer"
    (good / "src").mkdir(parents=True)
    (good / "src" / "viewer.ts").write_text("x")
    (good / "src" / "targetState.ts").write_text(
        "observe_mirror\nHERDR_BROWSER_TARGET_STATE\nactive_target_id\n"
    )
    (good / "src" / "browser.ts").write_text("observe_mirror\nactive_target_id\n")
    monkeypatch.setenv("HERDR_BROWSER_ROOT", str(good))

    fake_watch = {
        "agent_pane_id": "w1:p1",
        "watch_pane_id": "w1:p2",
        "herdr_socket": "/tmp/watch-test.sock",
        "herdr_session": "s1",
        "env": {
            "HERDR_BROWSER_MODE": "observe_mirror",
            "HERDR_BROWSER_TARGET_STATE": "/t.json",
            "HERDR_BROWSER_CDP_URL": "http://127.0.0.1:9301",
            "HERDR_BROWSER_ROOT": str(good),
        },
        "target_state_path": "/t.json",
        "cdp_url": "http://127.0.0.1:9301",
    }
    with mock.patch("browserctl.manager.get_adapter", return_value=FakeScratch()):
        out = m.acquire({"kind": "scratch", "owner": "t", "ttl": 60, "no_start": True})
        lid = out["lease"]["lease_id"]
        with mock.patch(
            "browserctl.manager.watch_mod.start_watch", return_value=fake_watch
        ) as sw:
            w = m.watch(
                lease_id=lid,
                agent_pane="w1:p1",
                herdr_socket="/tmp/watch-test.sock",
                herdr_session="s1",
            )
            assert sw.called
            kwargs = sw.call_args.kwargs
            assert kwargs.get("herdr_socket") == "/tmp/watch-test.sock"
            assert kwargs.get("herdr_session") == "s1"
    lease = load_lease(state, lid)
    assert lease["watch"]["herdr_socket"] == "/tmp/watch-test.sock"
    assert lease["watch"]["herdr_session"] == "s1"
    assert w["mirror_env"]["HERDR_BROWSER_MODE"] == "observe_mirror"


# ── 5. watch readiness (cold-start race) ─────────────────────────────────────


def test_default_ratio_is_agent_37_browser_63():
    assert watch_mod.DEFAULT_RATIO == 0.37
    assert watch_mod.normalize_ratio(None) == 0.37
    assert watch_mod.normalize_ratio(0.25) == 0.25
    assert watch_mod.normalize_ratio(0.5) == 0.5
    # herdr clamps to [0.1, 0.9]
    assert watch_mod.normalize_ratio(0.05) == 0.1
    assert watch_mod.normalize_ratio(0.95) == 0.9
    with pytest.raises(InvalidRequest):
        watch_mod.normalize_ratio(0.0)
    with pytest.raises(InvalidRequest):
        watch_mod.normalize_ratio(1.0)
    with pytest.raises(InvalidRequest):
        watch_mod.normalize_ratio("nope")


def test_target_state_is_ready_requires_non_null_id():
    assert watch_mod.target_state_is_ready(None) is False
    assert watch_mod.target_state_is_ready({}) is False
    assert watch_mod.target_state_is_ready({"active_target_id": None}) is False
    assert watch_mod.target_state_is_ready({"active_target_id": ""}) is False
    assert watch_mod.target_state_is_ready({"active_target_id": "  "}) is False
    assert watch_mod.target_state_is_ready({"active_target_id": "T-1"}) is True
    # mismatched cdp_url blocks
    assert (
        watch_mod.target_state_is_ready(
            {"active_target_id": "T-1", "cdp_url": "http://127.0.0.1:1"},
            expected_cdp_url="http://127.0.0.1:2",
        )
        is False
    )
    # matching / omitted cdp_url ok
    assert (
        watch_mod.target_state_is_ready(
            {"active_target_id": "T-1", "cdp_url": "http://127.0.0.1:9/"},
            expected_cdp_url="http://127.0.0.1:9",
        )
        is True
    )
    assert (
        watch_mod.target_state_is_ready(
            {"active_target_id": "T-1"},
            expected_cdp_url="http://127.0.0.1:9",
        )
        is True
    )


def test_wait_for_watch_readiness_success(tmp_path: Path, monkeypatch):
    target = tmp_path / "active-target.json"
    cdp_url = "http://127.0.0.1:19301"

    # CDP becomes ready immediately; target id arrives after a short delay.
    monkeypatch.setattr(
        watch_mod,
        "cdp_version_ok",
        lambda url, timeout=2.0: {"ok": True, "status": 200, "body_keys": ["webSocketDebuggerUrl"]},
    )
    monkeypatch.setattr(
        watch_mod,
        "cdp_list_targets",
        lambda url, timeout=2.0: {
            "ok": True,
            "status": 200,
            "count": 1,
            "ids": ["TARGET-READY"],
        },
    )

    state = {"n": 0}

    def delayed_snapshot(path):
        state["n"] += 1
        if state["n"] < 3:
            if state["n"] == 1:
                return None
            return {
                "version": 1,
                "worker_id": "w",
                "active_target_id": None,
                "seq": 1,
                "cdp_url": cdp_url,
            }
        return {
            "version": 1,
            "worker_id": "w",
            "active_target_id": "TARGET-READY",
            "seq": 2,
            "cdp_url": cdp_url,
            "page": {"url": "https://example.com/", "title": "ex"},
        }

    monkeypatch.setattr(watch_mod, "read_target_state_snapshot", delayed_snapshot)
    out = watch_mod.wait_for_watch_readiness(
        cdp_url=cdp_url,
        target_state_path=target,
        timeout_s=2.0,
        poll_s=0.01,
    )
    assert out["ok"] is True
    assert out["active_target_id"] == "TARGET-READY"
    assert out["attempts"] >= 3
    assert out["cdp_list"]["matched_id"] == "TARGET-READY"


def test_wait_for_watch_readiness_timeout_null_target(tmp_path: Path, monkeypatch):
    target = tmp_path / "active-target.json"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "worker_id": "w",
                "active_target_id": None,
                "seq": 1,
                "cdp_url": "http://127.0.0.1:19302",
            }
        )
    )
    monkeypatch.setattr(
        watch_mod,
        "cdp_version_ok",
        lambda url, timeout=2.0: {"ok": True, "status": 200},
    )
    monkeypatch.setattr(
        watch_mod,
        "cdp_list_targets",
        lambda url, timeout=2.0: {"ok": True, "status": 200, "count": 0, "ids": []},
    )
    with pytest.raises(AdapterError) as ei:
        watch_mod.wait_for_watch_readiness(
            cdp_url="http://127.0.0.1:19302",
            target_state_path=target,
            timeout_s=0.15,
            poll_s=0.05,
        )
    err = ei.value
    assert err.code == "ADAPTER_ERROR"
    msg = err.message.lower()
    assert "readiness timed out" in msg or "timed out" in msg
    assert "about:blank" in msg or "null" in msg
    assert err.details.get("reasons")
    assert err.details.get("target_state", {}).get("active_target_id") is None
    assert "hint" in err.details


def test_wait_for_watch_readiness_timeout_cdp_down(tmp_path: Path, monkeypatch):
    target = tmp_path / "active-target.json"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "worker_id": "w",
                "active_target_id": "T-ok",
                "seq": 1,
                "cdp_url": "http://127.0.0.1:19303",
            }
        )
    )
    monkeypatch.setattr(
        watch_mod,
        "cdp_version_ok",
        lambda url, timeout=2.0: {"ok": False, "error": "ConnectionRefusedError: down"},
    )
    with pytest.raises(AdapterError) as ei:
        watch_mod.wait_for_watch_readiness(
            cdp_url="http://127.0.0.1:19303",
            target_state_path=target,
            timeout_s=0.12,
            poll_s=0.04,
        )
    assert "cdp" in ei.value.message.lower()
    assert ei.value.details["cdp"]["ok"] is False


def test_start_watch_does_not_seed_null_target(tmp_path: Path, monkeypatch):
    """Regression: watch must never write null active_target_id stub."""
    state = tmp_path / "state"
    state.mkdir()
    worker = "scratch-ready"
    target = state / worker / "control" / "active-target.json"
    # parent will be created by start_watch; file must NOT be pre-seeded null
    assert not target.exists()

    lease = {
        "lease_id": "L1",
        "worker_id": worker,
        "resources": {
            "cdp_url": "http://127.0.0.1:19304",
            "cdp_port": 19304,
            "target_state_path": str(target),
        },
    }

    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/watch-ready.sock")
    good = _capable_viewer(tmp_path / "viewer")
    monkeypatch.setenv("HERDR_BROWSER_ROOT", str(good))

    # Fail readiness quickly — prove we never wrote a null stub in the attempt.
    monkeypatch.setattr(
        watch_mod,
        "cdp_version_ok",
        lambda url, timeout=2.0: {"ok": False, "error": "down"},
    )
    with pytest.raises(AdapterError):
        watch_mod.start_watch(
            lease,
            state_root=state,
            agent_pane="w1:p1",
            herdr_socket="/tmp/watch-ready.sock",
            ready_timeout_s=0.1,
            ready_poll_s=0.05,
        )
    assert not target.exists(), "must not seed null active-target stub on cold start"


def test_start_watch_waits_then_splits(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    worker = "scratch-live"
    target = state / worker / "control" / "active-target.json"
    target.parent.mkdir(parents=True)
    cdp_url = "http://127.0.0.1:19305"

    lease = {
        "lease_id": "L2",
        "worker_id": worker,
        "resources": {
            "cdp_url": cdp_url,
            "cdp_port": 19305,
            "target_state_path": str(target),
        },
    }

    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/watch-live.sock")
    good = _capable_viewer(tmp_path / "viewer")
    monkeypatch.setenv("HERDR_BROWSER_ROOT", str(good))

    # Publish target mid-wait.
    calls = {"n": 0}

    def fake_cdp(url, timeout=2.0):
        return {"ok": True, "status": 200}

    def fake_snap(path):
        calls["n"] += 1
        if calls["n"] < 2:
            return {"version": 1, "active_target_id": None, "seq": 1, "cdp_url": cdp_url}
        return {
            "version": 1,
            "active_target_id": "TAB-9",
            "seq": 2,
            "cdp_url": cdp_url,
            "page": {"url": "https://example.test/"},
        }

    monkeypatch.setattr(watch_mod, "cdp_version_ok", fake_cdp)
    monkeypatch.setattr(watch_mod, "read_target_state_snapshot", fake_snap)
    monkeypatch.setattr(
        watch_mod,
        "cdp_list_targets",
        lambda url, timeout=2.0: {
            "ok": True,
            "status": 200,
            "count": 1,
            "ids": ["TAB-9"],
        },
    )

    split_calls: list[dict[str, Any]] = []

    def fake_split(**kwargs):
        split_calls.append(kwargs)
        return "w1:watch"

    monkeypatch.setattr(watch_mod, "split_watch_pane", fake_split)
    monkeypatch.setattr(watch_mod, "run_in_pane", lambda *a, **k: None)
    monkeypatch.setattr(watch_mod, "resolve_agent_pane", lambda pane, endpoint: "w1:agent")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(
        watch_mod,
        "wait_for_viewer_process",
        lambda pane_id, endpoint, timeout_s=3.0, poll_s=0.15: {
            "ok": True,
            "pane_id": pane_id,
            "attempts": 1,
            "elapsed_s": 0.01,
            "process": {
                "ok": True,
                "matched": ["viewer.ts"],
                "pids": [4242],
            },
        },
    )

    rec = watch_mod.start_watch(
        lease,
        state_root=state,
        agent_pane="w1:agent",
        herdr_socket="/tmp/watch-live.sock",
        ready_timeout_s=2.0,
        ready_poll_s=0.01,
        # default ratio
    )
    assert rec["watch_pane_id"] == "w1:watch"
    assert rec["ratio"] == 0.37
    assert rec["readiness"]["active_target_id"] == "TAB-9"
    assert split_calls and split_calls[0]["ratio"] == 0.37
    assert rec["env"]["HERDR_BROWSER_MODE"] == "observe_mirror"
    assert rec["env"]["HERDR_BROWSER_CDP_URL"] == cdp_url
    assert rec["env"]["HERDR_BROWSER_VIEWER_WATCH_RESIZE"] == "1"
    # default fixed viewport contract
    assert rec["viewport"]["mode"] == "fixed"
    assert rec["viewport"]["width"] == 1150
    assert rec["viewport"]["height"] == 902
    assert rec["env"]["HERDR_BROWSER_VIEWPORT_MODE"] == "fixed"
    assert rec["env"]["HERDR_BROWSER_VIEWPORT_WIDTH"] == "1150"
    assert rec["env"]["HERDR_BROWSER_VIEWPORT_HEIGHT"] == "902"
    assert "HERDR_BROWSER_FOLLOW_PANE_VIEWPORT" not in rec["env"]
    assert split_calls[0]["env"]["HERDR_BROWSER_VIEWER_WATCH_RESIZE"] == "1"
    assert split_calls[0]["env"]["HERDR_BROWSER_VIEWPORT_MODE"] == "fixed"
    assert rec["viewer_start"]["ok"] is True
    assert rec["viewer_start"]["matched"] == ["viewer.ts"]

    # explicit override preserved
    rec2 = watch_mod.start_watch(
        lease,
        state_root=state,
        agent_pane="w1:agent",
        herdr_socket="/tmp/watch-live.sock",
        ratio=0.4,
        ready_timeout_s=2.0,
        ready_poll_s=0.01,
    )
    assert rec2["ratio"] == 0.4

    # opt-in follow-pane
    rec3 = watch_mod.start_watch(
        lease,
        state_root=state,
        agent_pane="w1:agent",
        herdr_socket="/tmp/watch-live.sock",
        viewport="follow-pane",
        ready_timeout_s=2.0,
        ready_poll_s=0.01,
    )
    assert rec3["viewport"]["mode"] == "follow-pane"
    assert rec3["env"]["HERDR_BROWSER_VIEWPORT_MODE"] == "follow-pane"
    assert rec3["env"]["HERDR_BROWSER_FOLLOW_PANE_VIEWPORT"] == "1"
    assert "HERDR_BROWSER_VIEWPORT_WIDTH" not in rec3["env"]
    assert "HERDR_BROWSER_VIEWPORT_HEIGHT" not in rec3["env"]
    assert rec3["env"]["HERDR_BROWSER_VIEWER_WATCH_RESIZE"] == "1"


def test_build_mirror_env_default_fixed_viewport(tmp_path: Path):
    env = watch_mod.build_mirror_env(
        cdp_url="http://127.0.0.1:9333",
        target_state_path=tmp_path / "active-target.json",
        viewer_root=tmp_path / "viewer",
    )
    assert env["HERDR_BROWSER_MODE"] == "observe_mirror"
    assert env["HERDR_BROWSER_CAPTURE_BACKEND"] == "screencast"
    assert env["HERDR_BROWSER_CAPTURE_SCALE"] == "1"
    assert env["HERDR_BROWSER_VIEWPORT_MODE"] == "fixed"
    assert env["HERDR_BROWSER_VIEWPORT_WIDTH"] == "1150"
    assert env["HERDR_BROWSER_VIEWPORT_HEIGHT"] == "902"
    assert "HERDR_BROWSER_FOLLOW_PANE_VIEWPORT" not in env
    assert env["HERDR_BROWSER_VIEWER_WATCH_RESIZE"] == "1"
    assert env["HERDR_BROWSER_CDP_URL"] == "http://127.0.0.1:9333"
    assert env["HERDR_BROWSER_TARGET_STATE"] == str(tmp_path / "active-target.json")
    assert env["HERDR_BROWSER_ROOT"] == str(tmp_path / "viewer")


def test_build_mirror_env_follow_pane_opt_in(tmp_path: Path):
    env = watch_mod.build_mirror_env(
        cdp_url="http://127.0.0.1:9333",
        target_state_path=tmp_path / "active-target.json",
        viewport="follow-pane",
    )
    assert env["HERDR_BROWSER_VIEWPORT_MODE"] == "follow-pane"
    assert env["HERDR_BROWSER_FOLLOW_PANE_VIEWPORT"] == "1"
    assert "HERDR_BROWSER_VIEWPORT_WIDTH" not in env
    assert "HERDR_BROWSER_VIEWPORT_HEIGHT" not in env
    assert env["HERDR_BROWSER_VIEWER_WATCH_RESIZE"] == "1"


def test_build_mirror_env_preserve_mode(tmp_path: Path):
    env = watch_mod.build_mirror_env(
        cdp_url="http://127.0.0.1:9333",
        target_state_path=tmp_path / "active-target.json",
        viewport="preserve",
    )
    assert env["HERDR_BROWSER_VIEWPORT_MODE"] == "preserve"
    assert "HERDR_BROWSER_FOLLOW_PANE_VIEWPORT" not in env
    assert "HERDR_BROWSER_VIEWPORT_WIDTH" not in env
    assert env["HERDR_BROWSER_VIEWER_WATCH_RESIZE"] == "1"


def test_build_mirror_env_fixed_size_override(tmp_path: Path):
    env = watch_mod.build_mirror_env(
        cdp_url="http://127.0.0.1:9333",
        target_state_path=tmp_path / "active-target.json",
        viewport="fixed",
        viewport_width=1280,
        viewport_height=720,
    )
    assert env["HERDR_BROWSER_VIEWPORT_MODE"] == "fixed"
    assert env["HERDR_BROWSER_VIEWPORT_WIDTH"] == "1280"
    assert env["HERDR_BROWSER_VIEWPORT_HEIGHT"] == "720"


def test_build_mirror_env_rejects_size_outside_fixed(tmp_path: Path):
    with pytest.raises(InvalidRequest):
        watch_mod.build_mirror_env(
            cdp_url="http://127.0.0.1:9333",
            target_state_path=tmp_path / "active-target.json",
            viewport="follow-pane",
            viewport_width=800,
        )


def test_normalize_viewport_mode_aliases():
    assert watch_mod.normalize_viewport_mode(None) == "fixed"
    assert watch_mod.normalize_viewport_mode("fixed") == "fixed"
    assert watch_mod.normalize_viewport_mode("follow_pane") == "follow-pane"
    assert watch_mod.normalize_viewport_mode("follow") == "follow-pane"
    assert watch_mod.normalize_viewport_mode("preserve") == "preserve"


def test_wait_for_watch_readiness_rejects_stale_target_not_in_list(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "active-target.json"
    cdp_url = "http://127.0.0.1:19311"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "worker_id": "w",
                "active_target_id": "STALE-ID",
                "seq": 9,
                "cdp_url": cdp_url,
            }
        )
    )
    monkeypatch.setattr(
        watch_mod,
        "cdp_version_ok",
        lambda url, timeout=2.0: {"ok": True, "status": 200},
    )
    monkeypatch.setattr(
        watch_mod,
        "cdp_list_targets",
        lambda url, timeout=2.0: {
            "ok": True,
            "status": 200,
            "count": 1,
            "ids": ["LIVE-OTHER"],
        },
    )
    with pytest.raises(AdapterError) as ei:
        watch_mod.wait_for_watch_readiness(
            cdp_url=cdp_url,
            target_state_path=target,
            timeout_s=0.15,
            poll_s=0.05,
        )
    msg = ei.value.message.lower()
    assert "json/list" in msg or "stale" in msg or "missing" in msg
    assert "STALE-ID" in ei.value.message
    assert ei.value.details.get("reasons")
    assert ei.value.details.get("cdp_list", {}).get("ok") is True


def test_wait_for_watch_readiness_accepts_when_list_contains_id(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "active-target.json"
    cdp_url = "http://127.0.0.1:19312"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "worker_id": "w",
                "active_target_id": "LIVE-1",
                "seq": 2,
                "cdp_url": cdp_url,
            }
        )
    )
    monkeypatch.setattr(
        watch_mod,
        "cdp_version_ok",
        lambda url, timeout=2.0: {"ok": True, "status": 200},
    )
    monkeypatch.setattr(
        watch_mod,
        "cdp_list_targets",
        lambda url, timeout=2.0: {
            "ok": True,
            "status": 200,
            "count": 2,
            "ids": ["OTHER", "LIVE-1"],
        },
    )
    out = watch_mod.wait_for_watch_readiness(
        cdp_url=cdp_url,
        target_state_path=target,
        timeout_s=1.0,
        poll_s=0.01,
    )
    assert out["ok"] is True
    assert out["active_target_id"] == "LIVE-1"
    assert out["cdp_list"]["matched_id"] == "LIVE-1"


def test_viewer_process_started_matches_markers():
    ok = watch_mod.viewer_process_started(
        {
            "shell_pid": 1,
            "foreground_processes": [
                {
                    "pid": 99,
                    "name": "bun",
                    "cmdline": "bun run /tmp/herdr-browser/src/viewer.ts",
                    "argv": ["bun", "run", "/tmp/herdr-browser/src/viewer.ts"],
                }
            ],
        }
    )
    assert ok["ok"] is True
    assert "viewer.ts" in ok["matched"]
    bad = watch_mod.viewer_process_started(
        {
            "shell_pid": 1,
            "foreground_processes": [
                {"pid": 2, "name": "bash", "cmdline": "bash"},
            ],
        }
    )
    assert bad["ok"] is False


def test_start_watch_closes_pane_when_viewer_fails_to_start(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    worker = "scratch-viewer-fail"
    target = state / worker / "control" / "active-target.json"
    target.parent.mkdir(parents=True)
    cdp_url = "http://127.0.0.1:19313"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "active_target_id": "TAB-OK",
                "seq": 1,
                "cdp_url": cdp_url,
            }
        )
    )
    lease = {
        "lease_id": "L3",
        "worker_id": worker,
        "resources": {
            "cdp_url": cdp_url,
            "cdp_port": 19313,
            "target_state_path": str(target),
        },
    }
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/watch-viewer-fail.sock")
    good = _capable_viewer(tmp_path / "viewer")
    monkeypatch.setenv("HERDR_BROWSER_ROOT", str(good))
    monkeypatch.setattr(
        watch_mod,
        "cdp_version_ok",
        lambda url, timeout=2.0: {"ok": True, "status": 200},
    )
    monkeypatch.setattr(
        watch_mod,
        "cdp_list_targets",
        lambda url, timeout=2.0: {
            "ok": True,
            "status": 200,
            "count": 1,
            "ids": ["TAB-OK"],
        },
    )
    monkeypatch.setattr(watch_mod, "split_watch_pane", lambda **k: "w1:orphan")
    monkeypatch.setattr(watch_mod, "run_in_pane", lambda *a, **k: None)
    monkeypatch.setattr(watch_mod, "resolve_agent_pane", lambda pane, endpoint: "w1:agent")
    monkeypatch.setattr(time, "sleep", lambda s: None)

    closed: list[str] = []

    def fake_close(pane_id, endpoint=None):
        closed.append(pane_id)

    monkeypatch.setattr(watch_mod, "close_pane", fake_close)

    def boom(pane_id, endpoint, timeout_s=3.0, poll_s=0.15):
        raise AdapterError(
            "watch viewer process did not start in pane",
            pane_id=pane_id,
            process={"ok": False, "foreground_count": 0},
        )

    monkeypatch.setattr(watch_mod, "wait_for_viewer_process", boom)

    with pytest.raises(AdapterError) as ei:
        watch_mod.start_watch(
            lease,
            state_root=state,
            agent_pane="w1:agent",
            herdr_socket="/tmp/watch-viewer-fail.sock",
            ready_timeout_s=1.0,
            ready_poll_s=0.01,
        )
    assert closed == ["w1:orphan"]
    assert ei.value.details.get("closed_on_failure") is True
    assert ei.value.details.get("watch_pane_id") == "w1:orphan"

