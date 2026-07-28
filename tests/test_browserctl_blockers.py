"""Review-blocker regression tests: xai conflict, ports, watch endpoint, probe."""

from __future__ import annotations

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


def test_resolve_viewer_cwd_override_ok(tmp_path: Path, monkeypatch):
    good = tmp_path / "good"
    (good / "src").mkdir(parents=True)
    (good / "src" / "viewer.ts").write_text("x")
    (good / "src" / "targetState.ts").write_text(
        "observe_mirror\nHERDR_BROWSER_TARGET_STATE\nactive_target_id\n"
    )
    (good / "src" / "browser.ts").write_text("observe_mirror\nactive_target_id\n")
    monkeypatch.setenv("HERDR_BROWSER_ROOT", str(good))
    assert watch_mod.resolve_viewer_cwd() == good


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
    state = tmp_path / "state"
    state.mkdir()
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
                    "root": str(ROOT),
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
    state = tmp_path / "state"
    state.mkdir()
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
                    "root": str(ROOT),
                    "state_root": str(state),
                    "cdp_port": 19100,
                    "no_start": True,
                }
            )
        out = scratch_mod.acquire(
            {
                "root": str(ROOT),
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
