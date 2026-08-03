"""Navigator lifecycle seam: spawn/cleanup/status + pane close truth."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.cli import main  # noqa: E402
from browserctl.errors import AdapterError, InvalidRequest  # noqa: E402
from browserctl.manager import Manager  # noqa: E402
from browserctl.navigator import (  # noqa: E402
    NavigatorLifecycle,
    find_binding_by_name,
    load_binding,
    save_binding,
)
from browserctl.store import load_lease  # noqa: E402
from browserctl import watch as watch_mod  # noqa: E402


class FakeAdapter:
    def __init__(self, worker_id: str = "scratch-nav-1"):
        self.worker_id = worker_id
        self.starts: list[dict[str, Any]] = []
        self.stops: list[dict[str, Any]] = []
        self._n = 0

    def acquire(self, request: dict[str, Any]) -> dict[str, Any]:
        self._n += 1
        wid = request.get("worker_id") or self.worker_id
        if request.get("label") and not request.get("worker_id"):
            wid = f"scratch-{request['label']}-{self._n}"
        port = 9400 + self._n
        self.starts.append({"worker_id": wid})
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
        return {"worker_id": lease.get("worker_id"), "cdp_alive": True, "daemon": "running"}


@pytest.fixture
def mgr(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    fake = FakeAdapter()
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        m = Manager(root=ROOT, state_root=state)
        yield m, fake, state


def _fake_spawn_payload(name: str = "nav-demo", pane: str = "w1:nav1") -> dict[str, Any]:
    return {
        "status": "success",
        "name": name,
        "pane_id": pane,
        "workspace_id": "ws-1",
        "tab_id": "tab-1",
        "lifecycle": "persistent",
        "profile": "navigator",
        "cwd": str(ROOT),
        "orchestrator_id": "orch-pane",
        "receipt": {
            "pane_id": pane,
            "workspace_id": "ws-1",
            "tab_id": "tab-1",
            "name": name,
            "orchestrator_id": "orch-pane",
        },
    }


# ── close_pane / stop_watch truth ────────────────────────────────────────────


def test_close_pane_does_not_swallow_errors(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, *, endpoint=None, timeout=15.0):
        calls.append(list(args))
        if args[:2] == ["pane", "close"]:
            raise AdapterError("herdr pane close failed (1)", stderr="boom")
        if args[:2] == ["pane", "get"]:
            # Still present after failed close
            return {"result": {"pane": {"pane_id": args[2]}}}
        return {}

    monkeypatch.setattr(watch_mod, "_run_herdr", fake_run)
    out = watch_mod.close_pane("w1:p9", endpoint={"herdr_socket": "/tmp/s.sock"})
    assert out["closed"] is False
    assert out["close_submitted"] is False
    assert out["error"]
    assert "boom" in (out.get("close_stderr") or out["error"] or "")
    assert out["verify"]["absent"] is False


def test_close_pane_verified_absent_on_not_found(monkeypatch):
    def fake_run(args, *, endpoint=None, timeout=15.0):
        if args[:2] == ["pane", "close"]:
            raise AdapterError(
                "herdr pane close failed (1)",
                stderr="Error: pane not found",
            )
        if args[:2] == ["pane", "get"]:
            raise AdapterError(
                "herdr pane get failed (1)",
                stderr="Error: pane not found",
            )
        return {}

    monkeypatch.setattr(watch_mod, "_run_herdr", fake_run)
    out = watch_mod.close_pane("w1:gone", endpoint={"herdr_socket": "/tmp/s.sock"})
    assert out["closed"] is True
    assert out["already_absent"] is True
    assert out["evidence"] == "not_found"


def test_close_pane_success_requires_verify(monkeypatch):
    def fake_run(args, *, endpoint=None, timeout=15.0):
        if args[:2] == ["pane", "close"]:
            return {}
        if args[:2] == ["pane", "get"]:
            raise AdapterError(
                "herdr pane get failed (1)",
                stderr="unknown pane",
            )
        return {}

    monkeypatch.setattr(watch_mod, "_run_herdr", fake_run)
    out = watch_mod.close_pane("w1:p1", endpoint={"herdr_socket": "/tmp/s.sock"})
    assert out["closed"] is True
    assert out["close_submitted"] is True
    assert out["evidence"] == "not_found"


def test_stop_watch_does_not_claim_closed_without_evidence(monkeypatch):
    lease = {
        "watch": {
            "watch_pane_id": "w1:p2",
            "agent_pane_id": "w1:p1",
            "herdr_socket": "/tmp/s.sock",
        }
    }

    def fake_close(pane_id, *, endpoint=None, verify=True):
        return {
            "pane_id": pane_id,
            "closed": False,
            "close_submitted": True,
            "error": "still present",
            "evidence": "still_present",
            "verify": {"absent": False, "evidence": "still_present"},
        }

    monkeypatch.setattr(watch_mod, "close_pane", fake_close)
    out = watch_mod.stop_watch(lease, close=True)
    assert out["closed"] is False
    assert out["error"]
    assert out["close"]["evidence"] == "still_present"


def test_stop_watch_claims_closed_only_with_evidence(monkeypatch):
    lease = {
        "watch": {
            "watch_pane_id": "w1:p2",
            "herdr_socket": "/tmp/s.sock",
        }
    }

    def fake_close(pane_id, *, endpoint=None, verify=True):
        return {
            "pane_id": pane_id,
            "closed": True,
            "close_submitted": True,
            "evidence": "not_found",
            "verify": {"absent": True, "evidence": "not_found"},
        }

    monkeypatch.setattr(watch_mod, "close_pane", fake_close)
    out = watch_mod.stop_watch(lease, close=True)
    assert out["closed"] is True
    assert out["error"] is None


def test_release_keeps_watch_binding_when_close_unconfirmed(mgr, monkeypatch):
    m, fake, state = mgr
    out = m.acquire({"kind": "scratch", "owner": "orch", "ttl": 60})
    lid = out["lease"]["lease_id"]
    lease = load_lease(state, lid)
    assert lease is not None
    lease["watch"] = {
        "watch_pane_id": "w1:p2",
        "agent_pane_id": "w1:p1",
        "herdr_socket": "/tmp/s.sock",
    }
    from browserctl.store import save_lease

    save_lease(state, lease)

    monkeypatch.setattr(
        watch_mod,
        "stop_watch",
        lambda lease, close=True: {
            "closed": False,
            "watch_pane_id": "w1:p2",
            "error": "still present",
            "evidence": "still_present",
        },
    )
    rel = m.release(lease_id=lid)
    assert rel["ok"] is False
    assert rel["retryable"] is True
    lease2 = load_lease(state, lid)
    assert lease2 is not None
    assert lease2["status"] == "expiring"
    assert lease2.get("watch") is not None  # binding preserved for retry
    assert lease2["meta"].get("watch_close_incomplete") is True
    # Adapter release still ran (browser cleanup progresses).
    assert fake.stops


def test_release_clears_watch_when_close_confirmed(mgr, monkeypatch):
    m, fake, state = mgr
    out = m.acquire({"kind": "scratch", "owner": "orch", "ttl": 60})
    lid = out["lease"]["lease_id"]
    lease = load_lease(state, lid)
    assert lease is not None
    lease["watch"] = {
        "watch_pane_id": "w1:p2",
        "herdr_socket": "/tmp/s.sock",
    }
    from browserctl.store import save_lease

    save_lease(state, lease)
    monkeypatch.setattr(
        watch_mod,
        "stop_watch",
        lambda lease, close=True: {
            "closed": True,
            "watch_pane_id": "w1:p2",
            "evidence": "not_found",
        },
    )
    rel = m.release(lease_id=lid)
    assert rel["ok"] is True
    assert rel.get("retryable") is False
    lease2 = load_lease(state, lid)
    assert lease2 is not None
    assert lease2["status"] == "released"
    assert lease2.get("watch") is None


# ── navigator spawn / cleanup ────────────────────────────────────────────────


def test_navigator_spawn_one_shot_is_auto_reap_eligible(mgr, monkeypatch):
    m, fake, state = mgr
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/orch.sock")
    monkeypatch.setenv("HERDR_PANE_ID", "w0:orch")
    monkeypatch.setattr(
        "browserctl.navigator.resolve_agent_ctl_bin",
        lambda explicit=None: "/bin/fake-agent-ctl",
    )
    monkeypatch.setattr(
        "browserctl.navigator.invoke_herdr_agent_ctl_spawn",
        lambda **k: _fake_spawn_payload(name=k["name"], pane="w1:os"),
    )
    receipt = m.navigator_spawn(
        {
            "kind": "scratch",
            "owner": "orch",
            "mode": "one_shot",
            "ttl": 90,
            "name": "nav-os",
        }
    )
    assert receipt["lease"]["mode"] == "one_shot"
    assert receipt["lease"]["auto_reap_eligible"] is True
    lease = load_lease(state, receipt["lease_id"])
    assert lease is not None
    from browserctl.store import is_auto_reap_eligible

    assert is_auto_reap_eligible(lease) is True


def test_navigator_spawn_persistent_auto_reap_flag(mgr, monkeypatch):
    m, fake, state = mgr
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/orch.sock")
    monkeypatch.setenv("HERDR_PANE_ID", "w0:orch")
    monkeypatch.setattr(
        "browserctl.navigator.resolve_agent_ctl_bin",
        lambda explicit=None: "/bin/fake-agent-ctl",
    )
    monkeypatch.setattr(
        "browserctl.navigator.invoke_herdr_agent_ctl_spawn",
        lambda **k: _fake_spawn_payload(name=k["name"], pane="w1:ar"),
    )
    receipt = m.navigator_spawn(
        {
            "kind": "scratch",
            "owner": "orch",
            "mode": "persistent",
            "auto_reap": True,
            "name": "nav-ar",
        }
    )
    assert receipt["lease"]["auto_reap"] is True
    assert receipt["lease"]["auto_reap_eligible"] is True
    lease = load_lease(state, receipt["lease_id"])
    assert lease is not None
    assert lease.get("auto_reap") is True


def test_navigator_spawn_receipt_and_binding(mgr, monkeypatch):
    m, fake, state = mgr
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/orch.sock")
    monkeypatch.setenv("HERDR_PANE_ID", "w0:orch")

    def fake_invoke(**kwargs):
        assert kwargs["profile"] == "navigator"
        assert kwargs["env"]["BROWSER_HARNESS_WORKER"]
        assert "BROWSERCTL_LEASE_ID" in kwargs["env"]
        return _fake_spawn_payload(name=kwargs["name"], pane="w1:navA")

    monkeypatch.setattr(
        "browserctl.navigator.invoke_herdr_agent_ctl_spawn", fake_invoke
    )
    monkeypatch.setattr(
        "browserctl.navigator.resolve_agent_ctl_bin",
        lambda explicit=None: "/bin/fake-agent-ctl",
    )

    receipt = m.navigator_spawn(
        {
            "kind": "scratch",
            "owner": "orch",
            "mode": "one_shot",
            "ttl": 90,
            "name": "nav-demo",
            "label": "demo",
        }
    )
    assert receipt["ok"] is True
    assert receipt["lease_id"]
    # default persistent without --auto-reap is NOT timer-eligible
    assert receipt["lease"]["mode"] == "one_shot"
    assert receipt["lease"]["auto_reap_eligible"] is True
    assert receipt["navigator"]["pane_id"] == "w1:navA"
    assert receipt["navigator"]["name"] == "nav-demo"
    assert receipt["next"]["run_target"] == "w1:navA"
    assert "navigator cleanup --lease" in receipt["next"]["cleanup"]
    assert receipt["env"]["BROWSER_HARNESS_WORKER"]

    binding = load_binding(state, receipt["lease_id"])
    assert binding is not None
    assert binding["pane_id"] == "w1:navA"
    assert binding["name"] == "nav-demo"
    assert binding["lease_id"] == receipt["lease_id"]

    lease = load_lease(state, receipt["lease_id"])
    assert lease is not None
    assert lease["meta"]["navigator"]["pane_id"] == "w1:navA"
    assert len(fake.starts) == 1


def test_navigator_spawn_rolls_back_lease_on_agent_failure(mgr, monkeypatch):
    m, fake, state = mgr
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/orch.sock")
    monkeypatch.setenv("HERDR_PANE_ID", "w0:orch")
    monkeypatch.setattr(
        "browserctl.navigator.resolve_agent_ctl_bin",
        lambda explicit=None: "/bin/fake-agent-ctl",
    )

    def boom(**kwargs):
        raise AdapterError("boot_failed", reason="boot_failed")

    monkeypatch.setattr(
        "browserctl.navigator.invoke_herdr_agent_ctl_spawn", boom
    )

    with pytest.raises(AdapterError) as ei:
        m.navigator_spawn(
            {"kind": "scratch", "owner": "orch", "ttl": 60, "name": "nav-x"}
        )
    assert ei.value.details.get("lease_id")
    assert ei.value.details.get("lease_release")
    # Lease should be released (not left active).
    lid = ei.value.details["lease_id"]
    lease = load_lease(state, lid)
    assert lease is not None
    assert lease["status"] in ("released", "expiring")
    assert fake.stops


def test_navigator_spawn_optional_watch_after_pane(mgr, monkeypatch):
    m, fake, state = mgr
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/orch.sock")
    monkeypatch.setenv("HERDR_PANE_ID", "w0:orch")
    monkeypatch.setattr(
        "browserctl.navigator.resolve_agent_ctl_bin",
        lambda explicit=None: "/bin/fake-agent-ctl",
    )
    monkeypatch.setattr(
        "browserctl.navigator.invoke_herdr_agent_ctl_spawn",
        lambda **k: _fake_spawn_payload(name=k["name"], pane="w1:navW"),
    )

    watch_calls: list[dict[str, Any]] = []

    def fake_watch(**kwargs):
        watch_calls.append(kwargs)
        return {
            "ok": True,
            "watch": {
                "agent_pane_id": kwargs.get("agent_pane"),
                "watch_pane_id": "w1:mirror",
                "herdr_socket": "/tmp/orch.sock",
            },
            "mirror_env": {"HERDR_BROWSER_MODE": "observe_mirror"},
        }

    monkeypatch.setattr(m, "watch", fake_watch)

    receipt = m.navigator_spawn(
        {
            "kind": "scratch",
            "owner": "orch",
            "ttl": 60,
            "name": "nav-w",
            "watch": True,
            "herdr_socket": "/tmp/orch.sock",
        }
    )
    assert receipt["watch"]["watch_pane_id"] == "w1:mirror"
    assert watch_calls and watch_calls[0]["agent_pane"] == "w1:navW"
    binding = load_binding(state, receipt["lease_id"])
    assert binding is not None
    assert binding.get("watch_pane_id") == "w1:mirror"


def test_navigator_cleanup_settles_and_releases(mgr, monkeypatch):
    m, fake, state = mgr
    # Seed lease + binding as if spawn already happened.
    acq = m.acquire({"kind": "scratch", "owner": "orch", "ttl": 60})
    lid = acq["lease"]["lease_id"]
    save_binding(
        state,
        {
            "version": 1,
            "lease_id": lid,
            "worker_id": acq["lease"]["worker_id"],
            "name": "nav-c",
            "pane_id": "w1:navC",
            "workspace_id": "ws-1",
            "tab_id": "tab-1",
            "agent_ctl": "/bin/fake-agent-ctl",
            "herdr_socket": "/tmp/orch.sock",
            "status": "active",
        },
    )
    lease = load_lease(state, lid)
    assert lease is not None
    lease["meta"] = {
        "navigator": {"name": "nav-c", "pane_id": "w1:navC"},
    }
    lease["watch"] = {
        "watch_pane_id": "w1:mirror",
        "agent_pane_id": "w1:navC",
        "herdr_socket": "/tmp/orch.sock",
    }
    from browserctl.store import save_lease

    save_lease(state, lease)

    monkeypatch.setattr(
        "browserctl.navigator.resolve_agent_ctl_bin",
        lambda explicit=None: "/bin/fake-agent-ctl",
    )
    monkeypatch.setattr(
        "browserctl.navigator.invoke_herdr_agent_ctl_close",
        lambda **k: {
            "ok": True,
            "status": "success",
            "target": k["target"],
            "pane_id": "w1:navC",
        },
    )
    monkeypatch.setattr(
        "browserctl.navigator.verify_navigator_pane_settled",
        lambda **k: {"settled": True, "evidence": "not_found", "pane_id": k.get("pane_id")},
    )
    monkeypatch.setattr(
        watch_mod,
        "stop_watch",
        lambda lease, close=True: {
            "closed": True,
            "watch_pane_id": "w1:mirror",
            "evidence": "not_found",
        },
    )

    out = m.navigator_cleanup(lease_id=lid)
    assert out["ok"] is True
    assert out["settled"] is True
    assert out["retryable"] is False
    assert out["proof"]["binding_cleared"] is True
    assert load_binding(state, lid) is None
    lease2 = load_lease(state, lid)
    assert lease2 is not None
    assert lease2["status"] == "released"
    assert fake.stops


def test_navigator_cleanup_fail_closed_when_pane_lingers(mgr, monkeypatch):
    m, fake, state = mgr
    acq = m.acquire({"kind": "scratch", "owner": "orch", "ttl": 60})
    lid = acq["lease"]["lease_id"]
    save_binding(
        state,
        {
            "version": 1,
            "lease_id": lid,
            "name": "nav-linger",
            "pane_id": "w1:linger",
            "herdr_socket": "/tmp/orch.sock",
            "status": "active",
        },
    )
    monkeypatch.setattr(
        "browserctl.navigator.resolve_agent_ctl_bin",
        lambda explicit=None: "/bin/fake-agent-ctl",
    )
    monkeypatch.setattr(
        "browserctl.navigator.invoke_herdr_agent_ctl_close",
        lambda **k: {"ok": True, "status": "success", "target": k["target"]},
    )
    monkeypatch.setattr(
        "browserctl.navigator.verify_navigator_pane_settled",
        lambda **k: {
            "settled": False,
            "evidence": "still_present",
            "pane_id": k.get("pane_id"),
            "error": "still there",
        },
    )

    out = m.navigator_cleanup(lease_id=lid)
    assert out["ok"] is False
    assert out["retryable"] is True
    assert out["error"]["code"] == "NAVIGATOR_CLOSE_INCOMPLETE"
    # Browser lease must NOT be released when navigator ownership uncertain.
    assert not fake.stops
    lease2 = load_lease(state, lid)
    assert lease2 is not None
    assert lease2["status"] == "active"
    assert load_binding(state, lid) is not None


def test_navigator_cleanup_force_releases_despite_pane(mgr, monkeypatch):
    m, fake, state = mgr
    acq = m.acquire({"kind": "scratch", "owner": "orch", "ttl": 60})
    lid = acq["lease"]["lease_id"]
    save_binding(
        state,
        {
            "version": 1,
            "lease_id": lid,
            "name": "nav-force",
            "pane_id": "w1:force",
            "herdr_socket": "/tmp/orch.sock",
            "status": "active",
        },
    )
    monkeypatch.setattr(
        "browserctl.navigator.resolve_agent_ctl_bin",
        lambda explicit=None: "/bin/fake-agent-ctl",
    )
    monkeypatch.setattr(
        "browserctl.navigator.invoke_herdr_agent_ctl_close",
        lambda **k: {"ok": False, "status": "error", "target": k["target"]},
    )
    monkeypatch.setattr(
        "browserctl.navigator.verify_navigator_pane_settled",
        lambda **k: {"settled": False, "evidence": "still_present", "pane_id": "w1:force"},
    )
    monkeypatch.setattr(
        watch_mod,
        "stop_watch",
        lambda lease, close=True: {"closed": True, "evidence": "no_watch_pane"},
    )

    out = m.navigator_cleanup(lease_id=lid, force=True)
    # force allows release path even if navigator unsettled; overall settled
    # still requires nav_settled which force does not flip — release runs.
    assert fake.stops
    assert out["proof"]["release"] is not None


def test_navigator_cleanup_idempotent(mgr, monkeypatch):
    m, fake, state = mgr
    acq = m.acquire({"kind": "scratch", "owner": "orch", "ttl": 60})
    lid = acq["lease"]["lease_id"]
    # Fully release first.
    rel = m.release(lease_id=lid)
    assert rel["ok"] is True
    out = m.navigator_cleanup(lease_id=lid, skip_navigator_close=True)
    assert out["ok"] is True
    assert out.get("idempotent") is True or out.get("settled") is True


def test_navigator_cleanup_by_name(mgr, monkeypatch):
    m, fake, state = mgr
    acq = m.acquire({"kind": "scratch", "owner": "orch", "ttl": 60})
    lid = acq["lease"]["lease_id"]
    save_binding(
        state,
        {
            "version": 1,
            "lease_id": lid,
            "name": "unique-nav",
            "pane_id": "w1:u",
            "herdr_socket": "/tmp/orch.sock",
            "status": "active",
        },
    )
    monkeypatch.setattr(
        "browserctl.navigator.resolve_agent_ctl_bin",
        lambda explicit=None: "/bin/fake-agent-ctl",
    )
    monkeypatch.setattr(
        "browserctl.navigator.invoke_herdr_agent_ctl_close",
        lambda **k: {"ok": True, "status": "gone", "target": k["target"]},
    )
    monkeypatch.setattr(
        "browserctl.navigator.verify_navigator_pane_settled",
        lambda **k: {"settled": True, "evidence": "not_found", "pane_id": "w1:u"},
    )

    out = m.navigator_cleanup(name="unique-nav")
    assert out["ok"] is True
    assert out["lease_id"] == lid


def test_navigator_status(mgr):
    m, fake, state = mgr
    acq = m.acquire({"kind": "scratch", "owner": "orch", "ttl": 60})
    lid = acq["lease"]["lease_id"]
    save_binding(
        state,
        {
            "version": 1,
            "lease_id": lid,
            "name": "st-nav",
            "pane_id": "w1:st",
            "status": "active",
        },
    )
    st = m.navigator_status(lease_id=lid)
    assert st["ok"] is True
    assert st["binding"]["name"] == "st-nav"
    assert "cleanup" in (st.get("next") or {})


def test_find_binding_ambiguous(mgr):
    m, fake, state = mgr
    save_binding(
        state,
        {"version": 1, "lease_id": "aaa", "name": "dup", "pane_id": "p1"},
    )
    save_binding(
        state,
        {"version": 1, "lease_id": "bbb", "name": "dup", "pane_id": "p2"},
    )
    with pytest.raises(InvalidRequest):
        find_binding_by_name(state, "dup")


def test_cli_navigator_spawn_and_cleanup(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    fake = FakeAdapter()
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/orch.sock")
    monkeypatch.setenv("HERDR_PANE_ID", "w0:orch")

    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        with mock.patch(
            "browserctl.navigator.resolve_agent_ctl_bin",
            return_value="/bin/fake-agent-ctl",
        ):
            with mock.patch(
                "browserctl.navigator.invoke_herdr_agent_ctl_spawn",
                return_value=_fake_spawn_payload(name="cli-nav", pane="w1:cli"),
            ):
                code = main(
                    [
                        "--state-root",
                        str(state),
                        "--root",
                        str(ROOT),
                        "navigator",
                        "spawn",
                        "--kind",
                        "scratch",
                        "--owner",
                        "cli",
                        "--name",
                        "cli-nav",
                        "--json",
                    ]
                )
    assert code == 0
    spawn_out = json.loads(capsys.readouterr().out)
    assert spawn_out["ok"] is True
    lid = spawn_out["lease_id"]
    assert spawn_out["next"]["run_target"] == "w1:cli"

    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        with mock.patch(
            "browserctl.navigator.resolve_agent_ctl_bin",
            return_value="/bin/fake-agent-ctl",
        ):
            with mock.patch(
                "browserctl.navigator.invoke_herdr_agent_ctl_close",
                return_value={"ok": True, "status": "success", "target": "cli-nav"},
            ):
                with mock.patch(
                    "browserctl.navigator.verify_navigator_pane_settled",
                    return_value={
                        "settled": True,
                        "evidence": "not_found",
                        "pane_id": "w1:cli",
                    },
                ):
                    code2 = main(
                        [
                            "--state-root",
                            str(state),
                            "--root",
                            str(ROOT),
                            "navigator",
                            "cleanup",
                            "--lease",
                            lid,
                            "--json",
                        ]
                    )
    assert code2 == 0
    clean_out = json.loads(capsys.readouterr().out)
    assert clean_out["ok"] is True
    assert clean_out["settled"] is True


def test_cli_navigator_cleanup_missing_selector_exits():
    with pytest.raises(SystemExit) as ei:
        main(["navigator", "cleanup", "--json"])
    assert ei.value.code == 2


def test_invoke_spawn_requires_herdr_env(monkeypatch):
    from browserctl.navigator import invoke_herdr_agent_ctl_spawn

    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    with pytest.raises(InvalidRequest):
        invoke_herdr_agent_ctl_spawn(
            agent_ctl="/bin/true",
            name="n",
            cwd=str(ROOT),
            env={"A": "1"},
        )
