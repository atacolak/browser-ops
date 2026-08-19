"""BROWSERCTL_STATE_ROOT / BROWSER_TARGET_STATE alignment (live-like)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# daemon.main path bootstrap (primitives live under daemon/)
sys.path.insert(0, str(ROOT / "daemon"))

from browserctl.adapters import scratch as scratch_mod  # noqa: E402
from browserctl.manager import Manager  # noqa: E402
from browserctl.paths import active_target_path  # noqa: E402
from daemon.target_state import read_json  # noqa: E402


def _make_backend(worker_id: str, cdp_port: int = 9333):
    """Import CloakBackend after path bootstrap."""
    from backend.cloak import CloakBackend  # type: ignore

    return CloakBackend(worker_id, cdp_port=cdp_port)


def test_cloak_prefers_browser_target_state_env(tmp_path: Path, monkeypatch):
    override = tmp_path / "override" / "w1" / "control" / "active-target.json"
    override.parent.mkdir(parents=True)
    monkeypatch.setenv("BROWSER_TARGET_STATE", str(override))
    monkeypatch.delenv("BROWSER_OPS_STATE", raising=False)
    b = _make_backend("w1", 9333)
    assert b.active_target_path == override.resolve()
    b.target_id = "T-LIVE"
    b._cdp_http_url = "http://127.0.0.1:9333"
    b._publish_active_target({"url": "https://example.com", "title": "x"}, force=True)
    doc = read_json(override)
    assert doc is not None
    assert doc["active_target_id"] == "T-LIVE"
    assert doc["worker_id"] == "w1"
    repo_default = ROOT / "state" / "w1" / "control" / "active-target.json"
    assert not repo_default.exists() or read_json(repo_default) != doc


def test_cloak_browser_ops_state_root(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.delenv("BROWSER_TARGET_STATE", raising=False)
    monkeypatch.setenv("BROWSER_OPS_STATE", str(state))
    b = _make_backend("scratch-align", 9301)
    expected = (state / "scratch-align" / "control" / "active-target.json").resolve()
    assert b.active_target_path == expected
    b.target_id = "T2"
    b._publish_active_target(force=True)
    assert expected.exists()
    assert read_json(expected)["active_target_id"] == "T2"


def test_scratch_daemon_env_matches_lease_target(tmp_path: Path):
    root = tmp_path / "ops"
    state = root / "state"
    state.mkdir(parents=True)
    out = scratch_mod.acquire(
        {
            "root": str(root),
            "state_root": str(state),
            "label": "align",
            "no_start": True,
        }
    )
    wid = out["worker_id"]
    tsp = Path(out["resources"]["target_state_path"]).resolve()
    assert tsp == active_target_path(state, wid).resolve()
    assert str(state.resolve()) in str(tsp)
    assert Path(out["env"]["BROWSER_OPS_STATE"]).resolve() == state.resolve()
    assert Path(out["env"]["BROWSER_TARGET_STATE"]).resolve() == tsp

    m = Manager(root=root, state_root=state)

    class A:
        def acquire(self, req):
            return out

        def release(self, lease, *, force=False):
            return {"status": "stopped"}

        def status(self, lease):
            return {}

    with mock.patch("browserctl.manager.get_adapter", return_value=A()):
        leased = m.acquire(
            {
                "kind": "scratch",
                "owner": "t",
                "ttl": 60,
                "worker_id": wid,
                "no_start": True,
            }
        )
    lease_tsp = Path(leased["lease"]["resources"]["target_state_path"]).resolve()
    assert lease_tsp == tsp
    assert Path(leased["env"]["BROWSER_TARGET_STATE"]).resolve() == tsp


def test_live_like_publish_under_state_root_override(tmp_path: Path, monkeypatch):
    """
    browserctl --state-root /tmp/... then daemon child env:
    cloak publishes where lease.resources.target_state_path points.
    """
    root = tmp_path / "ops"
    state = root / "state"
    state.mkdir(parents=True)
    m = Manager(root=root, state_root=state)
    captured_env: dict[str, str] = {}

    class ScratchCapture:
        def acquire(self, request):
            out = scratch_mod.acquire({**request, "no_start": True, "label": "live"})
            captured_env.update(out["env"])
            return out

        def release(self, lease, *, force=False):
            return {"status": "stopped"}

        def status(self, lease):
            return {}

    with mock.patch("browserctl.manager.get_adapter", return_value=ScratchCapture()):
        result = m.acquire(
            {"kind": "scratch", "owner": "smoke", "ttl": 120, "label": "live"}
        )

    lease = result["lease"]
    tsp = Path(lease["resources"]["target_state_path"]).resolve()
    assert tsp == Path(result["env"]["BROWSER_TARGET_STATE"]).resolve()
    assert str(state.resolve()) in str(tsp)
    assert Path(captured_env["BROWSER_TARGET_STATE"]).resolve() == tsp
    assert "BROWSER_OPS_STATE" in captured_env

    monkeypatch.setenv("BROWSER_TARGET_STATE", captured_env["BROWSER_TARGET_STATE"])
    monkeypatch.setenv("BROWSER_OPS_STATE", captured_env["BROWSER_OPS_STATE"])
    backend = _make_backend(
        lease["worker_id"], int(lease["resources"]["cdp_port"])
    )
    assert backend.active_target_path == tsp
    backend.target_id = "SMOKE-TARGET"
    backend._cdp_http_url = lease["resources"]["cdp_url"]
    backend._publish_active_target(
        {"url": "https://example.com/", "title": "Example"}, force=True
    )
    doc = read_json(tsp)
    assert doc is not None
    assert doc["active_target_id"] == "SMOKE-TARGET"
    assert doc["page"]["url"] == "https://example.com/"
    assert Path(result["env"]["BROWSER_TARGET_STATE"]).exists()



def test_cloak_default_without_override_is_repo_state(monkeypatch):
    monkeypatch.delenv("BROWSER_TARGET_STATE", raising=False)
    monkeypatch.delenv("BROWSER_OPS_STATE", raising=False)
    b = _make_backend("default-path-check", 9333)
    expected = (ROOT / "state" / "default-path-check" / "control" / "active-target.json").resolve()
    assert b.active_target_path == expected
