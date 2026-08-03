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


def _fake_spawn_payload(
    name: str = "nav-demo",
    pane: str = "w1:nav1",
    *,
    tab_id: str = "ws-1:t-nav",
    workspace_id: str = "ws-1",
) -> dict[str, Any]:
    return {
        "status": "success",
        "name": name,
        "pane_id": pane,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "lifecycle": "persistent",
        "profile": "navigator",
        "cwd": str(ROOT),
        "orchestrator_id": "orch-pane",
        "receipt": {
            "pane_id": pane,
            "workspace_id": workspace_id,
            "tab_id": tab_id,
            "name": name,
            "orchestrator_id": "orch-pane",
        },
    }


def _fake_tab_create(**kwargs) -> dict[str, Any]:
    ws = kwargs.get("workspace") or "ws-1"
    return {
        "tab_id": f"{ws}:t-created",
        "workspace_id": ws,
        "root_pane_id": f"{ws}:p-root",
        "label": kwargs.get("label"),
        "endpoint": {},
    }


def _patch_spawn_substrate(monkeypatch, *, invoke=None, tab_create=None):
    """Common mocks for dedicated-tab spawn path (no live herdr)."""
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/orch.sock")
    monkeypatch.setenv("HERDR_PANE_ID", "w0:orch")
    monkeypatch.setattr(
        "browserctl.navigator.resolve_agent_ctl_bin",
        lambda explicit=None: "/bin/fake-agent-ctl",
    )
    monkeypatch.setattr(
        "browserctl.navigator.create_dedicated_tab",
        tab_create or _fake_tab_create,
    )
    monkeypatch.setattr(
        "browserctl.navigator.close_owned_tab",
        lambda tab_id, endpoint=None, timeout=15.0: {
            "closed": True,
            "settled": True,
            "evidence": "tested",
            "tab_id": tab_id,
        },
    )
    if invoke is None:
        def invoke(**k):
            return _fake_spawn_payload(
                name=k["name"],
                pane="w1:nav1",
                tab_id=k.get("tab") or "ws-1:t-nav",
            )
    monkeypatch.setattr("browserctl.navigator.invoke_herdr_agent_ctl_spawn", invoke)


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
    _patch_spawn_substrate(
        monkeypatch,
        invoke=lambda **k: _fake_spawn_payload(
            name=k["name"], pane="w1:os", tab_id=k.get("tab") or "t"
        ),
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
    _patch_spawn_substrate(
        monkeypatch,
        invoke=lambda **k: _fake_spawn_payload(
            name=k["name"], pane="w1:ar", tab_id=k.get("tab") or "t"
        ),
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
    seen: dict[str, Any] = {}

    def fake_invoke(**kwargs):
        seen.update(kwargs)
        assert kwargs["profile"] == "navigator"
        assert kwargs["env"]["BROWSER_HARNESS_WORKER"]
        assert "BROWSERCTL_LEASE_ID" in kwargs["env"]
        assert kwargs["split"] == "none"
        assert kwargs["tab"] == "ws-1:t-created"
        return _fake_spawn_payload(
            name=kwargs["name"],
            pane="w1:navA",
            tab_id=kwargs["tab"],
            workspace_id="ws-1",
        )

    _patch_spawn_substrate(monkeypatch, invoke=fake_invoke)

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
    assert receipt["lease"]["mode"] == "one_shot"
    assert receipt["lease"]["auto_reap_eligible"] is True
    assert receipt["navigator"]["pane_id"] == "w1:navA"
    assert receipt["navigator"]["name"] == "nav-demo"
    assert receipt["navigator"]["owns_tab"] is True
    assert receipt["navigator"]["tab_created"] is True
    assert receipt["navigator"]["geometry_guaranteed"] is True
    assert receipt["navigator"]["tab_id"] == "ws-1:t-created"
    assert receipt["navigator"]["split"] == "none"
    assert receipt["next"]["run_target"] == "w1:navA"
    assert "navigator cleanup --lease" in receipt["next"]["cleanup"]
    assert "1920x1080" in receipt["next"]["geometry"]
    assert receipt["env"]["BROWSER_HARNESS_WORKER"]
    assert "watch_error" not in receipt or receipt.get("watch_error") is None

    binding = load_binding(state, receipt["lease_id"])
    assert binding is not None
    assert binding["pane_id"] == "w1:navA"
    assert binding["name"] == "nav-demo"
    assert binding["owns_tab"] is True
    assert binding["tab_id"] == "ws-1:t-created"
    assert binding["lease_id"] == receipt["lease_id"]

    lease = load_lease(state, receipt["lease_id"])
    assert lease is not None
    assert lease["meta"]["navigator"]["pane_id"] == "w1:navA"
    assert lease["meta"]["navigator"]["owns_tab"] is True
    assert len(fake.starts) == 1


def test_navigator_spawn_explicit_tab_caller_owned(mgr, monkeypatch):
    m, fake, state = mgr
    seen: dict[str, Any] = {}
    tabs_created: list[Any] = []

    def no_create(**kwargs):
        tabs_created.append(kwargs)
        raise AssertionError("must not create tab when --tab provided")

    def fake_invoke(**kwargs):
        seen.update(kwargs)
        return _fake_spawn_payload(
            name=kwargs["name"],
            pane="w1:navT",
            tab_id=kwargs.get("tab") or "caller-tab",
        )

    _patch_spawn_substrate(monkeypatch, invoke=fake_invoke, tab_create=no_create)
    receipt = m.navigator_spawn(
        {
            "kind": "scratch",
            "owner": "orch",
            "name": "nav-tab",
            "tab": "ws-9:caller",
            "workspace": "ws-9",
            "split": "none",
        }
    )
    assert receipt["ok"] is True
    assert receipt["navigator"]["owns_tab"] is False
    assert receipt["navigator"]["geometry_guaranteed"] is False
    assert "NOT guaranteed" in receipt["navigator"]["geometry_note"]
    assert seen.get("tab") == "ws-9:caller"
    assert tabs_created == []
    binding = load_binding(state, receipt["lease_id"])
    assert binding["owns_tab"] is False
    assert binding["tab_id"] in ("ws-9:caller", "caller-tab", seen.get("tab"))


def test_navigator_spawn_rejects_non_none_split_without_tab(mgr, monkeypatch):
    m, fake, state = mgr
    _patch_spawn_substrate(monkeypatch)
    with pytest.raises(InvalidRequest) as ei:
        m.navigator_spawn(
            {
                "kind": "scratch",
                "owner": "orch",
                "name": "nav-split",
                "split": "right",
            }
        )
    assert "split none" in str(ei.value).lower() or "dedicated tab" in str(ei.value).lower()
    assert not fake.starts  # fail before acquire


def test_navigator_spawn_name_collision_fail_closed(mgr, monkeypatch):
    m, fake, state = mgr
    _patch_spawn_substrate(monkeypatch)
    save_binding(
        state,
        {
            "version": 1,
            "lease_id": "other-lease",
            "name": "nav-taken",
            "pane_id": "w1:x",
            "status": "active",
        },
    )
    with pytest.raises(InvalidRequest) as ei:
        m.navigator_spawn(
            {"kind": "scratch", "owner": "orch", "name": "nav-taken"}
        )
    assert ei.value.details.get("code") == "NAVIGATOR_NAME_COLLISION" or "already active" in str(ei.value)
    assert not fake.starts


def test_navigator_spawn_rolls_back_lease_on_agent_failure(mgr, monkeypatch):
    m, fake, state = mgr

    def boom(**kwargs):
        raise AdapterError("boot_failed", reason="boot_failed")

    closed_tabs: list[str] = []

    def fake_close_tab(tab_id, endpoint=None, timeout=15.0):
        closed_tabs.append(tab_id)
        return {"closed": True, "settled": True, "evidence": "tested", "tab_id": tab_id}

    _patch_spawn_substrate(monkeypatch, invoke=boom)
    monkeypatch.setattr("browserctl.navigator.close_owned_tab", fake_close_tab)

    with pytest.raises(AdapterError) as ei:
        m.navigator_spawn(
            {"kind": "scratch", "owner": "orch", "ttl": 60, "name": "nav-x"}
        )
    assert ei.value.details.get("lease_id")
    assert ei.value.details.get("lease_release")
    assert closed_tabs  # owned tab rolled back
    lid = ei.value.details["lease_id"]
    lease = load_lease(state, lid)
    assert lease is not None
    assert lease["status"] in ("released", "expiring")
    assert fake.stops
    assert load_binding(state, lid) is None


def test_navigator_spawn_watch_failure_rolls_back(mgr, monkeypatch):
    m, fake, state = mgr
    _patch_spawn_substrate(
        monkeypatch,
        invoke=lambda **k: _fake_spawn_payload(
            name=k["name"], pane="w1:navW", tab_id=k.get("tab") or "t"
        ),
    )
    closed: list[str] = []
    monkeypatch.setattr(
        "browserctl.navigator.invoke_herdr_agent_ctl_close",
        lambda **k: closed.append(k["target"]) or {"ok": True, "status": "success", "target": k["target"]},
    )
    monkeypatch.setattr(
        "browserctl.navigator.verify_navigator_pane_settled",
        lambda **k: {"settled": True, "evidence": "tested", "pane_id": k.get("pane_id")},
    )
    tabs: list[str] = []
    monkeypatch.setattr(
        "browserctl.navigator.close_owned_tab",
        lambda tab_id, endpoint=None, timeout=15.0: tabs.append(tab_id)
        or {"closed": True, "settled": True, "evidence": "tested", "tab_id": tab_id},
    )

    def boom_watch(**kwargs):
        raise AdapterError("watch_failed", reason="ready_timeout")

    monkeypatch.setattr(m, "watch", boom_watch)

    with pytest.raises(AdapterError) as ei:
        m.navigator_spawn(
            {
                "kind": "scratch",
                "owner": "orch",
                "name": "nav-wf",
                "watch": True,
            }
        )
    assert "watch failed" in str(ei.value).lower() or ei.value.details.get("watch_error")
    lid = ei.value.details["lease_id"]
    lease = load_lease(state, lid)
    assert lease is not None
    assert lease["status"] in ("released", "expiring")
    assert fake.stops
    assert closed or tabs
    assert load_binding(state, lid) is None


def test_navigator_spawn_optional_watch_after_pane(mgr, monkeypatch):
    m, fake, state = mgr
    _patch_spawn_substrate(
        monkeypatch,
        invoke=lambda **k: _fake_spawn_payload(
            name=k["name"], pane="w1:navW", tab_id=k.get("tab") or "t"
        ),
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
            "name": "nav-w",
            "watch": True,
            "ratio": 0.37,
        }
    )
    assert receipt["ok"] is True
    assert receipt["watch"]["watch_pane_id"] == "w1:mirror"
    assert watch_calls
    assert watch_calls[0]["agent_pane"] == "w1:navW"
    assert watch_calls[0].get("direction") == "right"
    binding = load_binding(state, receipt["lease_id"])
    assert binding["watch_pane_id"] == "w1:mirror"


def test_navigator_cleanup_settles_and_releases(mgr, monkeypatch):
    m, fake, state = mgr
    acq = m.acquire({"kind": "scratch", "owner": "orch", "ttl": 60})
    lid = acq["lease"]["lease_id"]
    save_binding(
        state,
        {
            "version": 1,
            "lease_id": lid,
            "name": "nav-c",
            "pane_id": "w1:navC",
            "tab_id": "ws-1:tC",
            "owns_tab": True,
            "herdr_socket": "/tmp/orch.sock",
            "agent_ctl": "/bin/fake-agent-ctl",
            "status": "active",
        },
    )
    # stamp meta
    from browserctl.store import require_lease, save_lease, worker_mutex, touch_lease

    lease = require_lease(state, lid)
    with worker_mutex(state, lease["worker_id"]):
        lease = require_lease(state, lid)
        lease = touch_lease(lease)
        meta = dict(lease.get("meta") or {})
        meta["navigator"] = {"name": "nav-c", "pane_id": "w1:navC", "tab_id": "ws-1:tC", "owns_tab": True}
        lease["meta"] = meta
        save_lease(state, lease)

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
        lambda **k: {"settled": True, "evidence": "absent", "pane_id": k.get("pane_id")},
    )
    tabs: list[str] = []
    monkeypatch.setattr(
        "browserctl.navigator.close_owned_tab",
        lambda tab_id, endpoint=None, timeout=15.0: tabs.append(tab_id)
        or {"closed": True, "settled": True, "evidence": "tested", "tab_id": tab_id},
    )
    monkeypatch.setattr(
        watch_mod,
        "stop_watch",
        lambda lease, close=True: {"closed": True, "evidence": "no_watch_pane"},
    )

    out = m.navigator_cleanup(lease_id=lid)
    assert out["ok"] is True
    assert out["settled"] is True
    assert out["forced"] is False
    assert out["proof"]["binding_cleared"] is True
    assert tabs == ["ws-1:tC"]
    assert fake.stops
    assert load_binding(state, lid) is None


def test_navigator_cleanup_never_closes_caller_owned_tab(mgr, monkeypatch):
    m, fake, state = mgr
    acq = m.acquire({"kind": "scratch", "owner": "orch", "ttl": 60})
    lid = acq["lease"]["lease_id"]
    save_binding(
        state,
        {
            "version": 1,
            "lease_id": lid,
            "name": "nav-caller",
            "pane_id": "w1:navK",
            "tab_id": "ws-1:caller",
            "owns_tab": False,
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
        lambda **k: {"settled": True, "evidence": "absent", "pane_id": k.get("pane_id")},
    )
    tabs: list[str] = []
    monkeypatch.setattr(
        "browserctl.navigator.close_owned_tab",
        lambda tab_id, endpoint=None, timeout=15.0: tabs.append(tab_id)
        or {"closed": True, "settled": True, "tab_id": tab_id},
    )
    monkeypatch.setattr(
        watch_mod,
        "stop_watch",
        lambda lease, close=True: {"closed": True, "evidence": "no_watch_pane"},
    )
    out = m.navigator_cleanup(lease_id=lid)
    assert out["ok"] is True
    assert tabs == []
    assert out["proof"]["tab_close"]["evidence"] == "caller_owned_tab_not_closed"


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
            "owns_tab": True,
            "tab_id": "ws-1:tl",
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
        lambda **k: {"settled": False, "evidence": "still_present", "pane_id": "w1:linger"},
    )
    out = m.navigator_cleanup(lease_id=lid)
    assert out["ok"] is False
    assert out["retryable"] is True
    assert out["error"]["code"] == "NAVIGATOR_CLOSE_INCOMPLETE"
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
    assert fake.stops
    assert out["proof"]["release"] is not None
    assert out["ok"] is True
    assert out["settled"] is True
    assert out["forced"] is True
    assert out["retryable"] is False
    assert "navigator_unconfirmed" in (out.get("warnings") or [])
    assert out["proof"]["binding_cleared"] is True
    assert load_binding(state, lid) is None


def test_navigator_cleanup_idempotent(mgr, monkeypatch):
    m, fake, state = mgr
    acq = m.acquire({"kind": "scratch", "owner": "orch", "ttl": 60})
    lid = acq["lease"]["lease_id"]
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
            "owns_tab": False,
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
        lambda **k: {"settled": True, "evidence": "absent", "pane_id": k.get("pane_id")},
    )
    monkeypatch.setattr(
        watch_mod,
        "stop_watch",
        lambda lease, close=True: {"closed": True, "evidence": "no_watch_pane"},
    )
    out = m.navigator_cleanup(name="unique-nav")
    assert out["ok"] is True
    assert out["lease_id"] == lid


def test_reap_delegates_to_navigator_cleanup(mgr, monkeypatch):
    m, fake, state = mgr
    acq = m.acquire(
        {"kind": "scratch", "owner": "orch", "mode": "one_shot", "ttl": 1}
    )
    lid = acq["lease"]["lease_id"]
    save_binding(
        state,
        {
            "version": 1,
            "lease_id": lid,
            "name": "nav-reap",
            "pane_id": "w1:reap",
            "owns_tab": True,
            "tab_id": "ws-1:tr",
            "herdr_socket": "/tmp/orch.sock",
            "status": "active",
        },
    )
    from browserctl.store import require_lease, save_lease, worker_mutex, touch_lease
    import time as _time

    lease = require_lease(state, lid)
    with worker_mutex(state, lease["worker_id"]):
        lease = require_lease(state, lid)
        lease = touch_lease(lease)
        # expire
        lease["expires_at"] = _time.time() - 10
        meta = dict(lease.get("meta") or {})
        meta["navigator"] = {
            "name": "nav-reap",
            "pane_id": "w1:reap",
            "tab_id": "ws-1:tr",
            "owns_tab": True,
        }
        lease["meta"] = meta
        save_lease(state, lease)

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
        lambda **k: {"settled": True, "evidence": "absent", "pane_id": k.get("pane_id")},
    )
    tabs: list[str] = []
    monkeypatch.setattr(
        "browserctl.navigator.close_owned_tab",
        lambda tab_id, endpoint=None, timeout=15.0: tabs.append(tab_id)
        or {"closed": True, "settled": True, "tab_id": tab_id},
    )
    monkeypatch.setattr(
        watch_mod,
        "stop_watch",
        lambda lease, close=True: {"closed": True, "evidence": "no_watch_pane"},
    )

    out = m.reap(now=_time.time())
    assert out["count"] >= 1, out
    assert any(r.get("lease_id") == lid for r in out["reaped"])
    assert tabs == ["ws-1:tr"]
    assert load_binding(state, lid) is None
    cur = load_lease(state, lid)
    assert cur is not None
    assert cur["status"] == "reaped"
    assert (cur.get("meta") or {}).get("reaped_via") == "navigator_cleanup"


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
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/orch.sock")
    monkeypatch.setenv("HERDR_PANE_ID", "w0:orch")
    with mock.patch("browserctl.manager.get_adapter", return_value=FakeAdapter()):
        with mock.patch(
            "browserctl.navigator.resolve_agent_ctl_bin",
            return_value="/bin/fake-agent-ctl",
        ):
            with mock.patch(
                "browserctl.navigator.create_dedicated_tab",
                side_effect=_fake_tab_create,
            ):
                with mock.patch(
                    "browserctl.navigator.close_owned_tab",
                    return_value={
                        "closed": True,
                        "settled": True,
                        "evidence": "tested",
                        "tab_id": "t",
                    },
                ):
                    with mock.patch(
                        "browserctl.navigator.invoke_herdr_agent_ctl_spawn",
                        return_value=_fake_spawn_payload(
                            name="cli-nav", pane="w1:cli", tab_id="ws-1:t-created"
                        ),
                    ):
                        with mock.patch(
                            "browserctl.navigator.invoke_herdr_agent_ctl_close",
                            return_value={
                                "ok": True,
                                "status": "success",
                                "target": "cli-nav",
                            },
                        ):
                            with mock.patch(
                                "browserctl.navigator.verify_navigator_pane_settled",
                                return_value={
                                    "settled": True,
                                    "evidence": "absent",
                                    "pane_id": "w1:cli",
                                },
                            ):
                                with mock.patch(
                                    "browserctl.watch.stop_watch",
                                    return_value={
                                        "closed": True,
                                        "evidence": "no_watch_pane",
                                    },
                                ):
                                    rc = main(
                                        [
                                            "--state-root",
                                            str(state),
                                            "navigator",
                                            "spawn",
                                            "--kind",
                                            "scratch",
                                            "--label",
                                            "cli",
                                            "--owner",
                                            "orch",
                                            "--name",
                                            "cli-nav",
                                            "--json",
                                        ]
                                    )
                                    assert rc == 0
                                    spawn_out = json.loads(capsys.readouterr().out)
                                    assert spawn_out["ok"] is True
                                    assert spawn_out["navigator"]["owns_tab"] is True
                                    lid = spawn_out["lease_id"]
                                    rc2 = main(
                                        [
                                            "--state-root",
                                            str(state),
                                            "navigator",
                                            "cleanup",
                                            "--lease",
                                            lid,
                                            "--json",
                                        ]
                                    )
                                    assert rc2 == 0
                                    clean_out = json.loads(capsys.readouterr().out)
                                    assert clean_out["ok"] is True
                                    assert clean_out["settled"] is True


def test_cli_navigator_cleanup_missing_selector_exits():
    with pytest.raises(SystemExit) as exc:
        main(["navigator", "cleanup", "--json"])
    assert exc.value.code == 2


def test_invoke_spawn_requires_herdr_env(monkeypatch):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    from browserctl.navigator import invoke_herdr_agent_ctl_spawn

    with pytest.raises(InvalidRequest):
        invoke_herdr_agent_ctl_spawn(
            agent_ctl="/bin/true",
            name="n",
            cwd=str(ROOT),
            env={"A": "1"},
        )
