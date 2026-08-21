"""Tab ownership: peek vs steal vs mint. Fake manager + fake backend."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

from browserctl.errors import TargetConflict  # noqa: E402
from browserctl.manager import Manager  # noqa: E402
from browserctl.tab_policy import annotate_tabs, classify_target  # noqa: E402
from rpc import DaemonError, _execute_action  # noqa: E402


class FakeAdapter:
    def __init__(self, worker_id: str = "scratch-profile-providers"):
        self.worker_id = worker_id
        self.starts: list[dict[str, Any]] = []
        self.stops: list[dict[str, Any]] = []
        self.alive = True

    def acquire(self, request: dict[str, Any]) -> dict[str, Any]:
        wid = request.get("worker_id") or self.worker_id
        self.starts.append({"worker_id": wid})
        return {
            "worker_id": wid,
            "kind": "scratch",
            "adapter": "scratch",
            "resources": {
                "worker_id": wid,
                "cdp_port": 9342,
                "cdp_url": "http://127.0.0.1:9342",
                "profile_dir": f"/tmp/{wid}",
                "state_dir": f"/tmp/state/{wid}",
                "daemon": "running",
            },
            "env": {"BROWSER_HARNESS_WORKER": wid},
            "meta": {},
        }

    def release(self, lease: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        self.stops.append({"lease_id": lease.get("lease_id"), "force": force})
        self.alive = False
        return {"status": "stopped", "force": force}

    def status(self, lease: dict[str, Any]) -> dict[str, Any]:
        return {"cdp_alive": self.alive, "daemon": "running" if self.alive else "stopped"}


@pytest.fixture
def ctx(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    m = Manager(root=ROOT, state_root=state)
    fake = FakeAdapter()
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        yield m, fake, state


def test_classify_owned_by_lease_id_not_owner_string(ctx):
    m, _fake, state = ctx
    a = m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "omp-nav", "ttl": 120, "target_id": "TA"})
    b = m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "omp-nav", "ttl": 120, "target_id": "TB"})
    mine = classify_target(state, "scratch-profile-providers", "TA", caller_lease_id=a["lease"]["lease_id"])
    sib = classify_target(state, "scratch-profile-providers", "TB", caller_lease_id=a["lease"]["lease_id"])
    junk = classify_target(state, "scratch-profile-providers", "NOPE", caller_lease_id=a["lease"]["lease_id"])
    assert mine["ownership"] == "owned_by_me"
    assert sib["ownership"] == "owned_by"
    assert sib["lease_id"] == b["lease"]["lease_id"]
    assert junk["ownership"] == "unowned"
    tabs = annotate_tabs(
        [{"targetId": "TA"}, {"targetId": "TB"}, {"targetId": "NOPE"}],
        state_root=state,
        worker_id="scratch-profile-providers",
        caller_lease_id=a["lease"]["lease_id"],
    )
    by_id = {t["targetId"]: t["ownership"] for t in tabs}
    assert by_id == {"TA": "owned_by_me", "TB": "owned_by", "NOPE": "unowned"}
    mine_row = next(t for t in tabs if t["targetId"] == "TA")
    sib_row = next(t for t in tabs if t["targetId"] == "TB")
    assert "lease_id" not in mine_row
    assert "lease_id" not in sib_row


def test_steal_transfers_target_lease(ctx):
    m, _fake, state = ctx
    a = m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "nav-a", "ttl": 120, "target_id": "T-A"})
    with pytest.raises(TargetConflict):
        m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "nav-b", "ttl": 120, "target_id": "T-A"})
    stolen = m.acquire(
        {
            "kind": "scratch",
            "worker_id": "scratch-profile-providers",
            "owner": "nav-b",
            "ttl": 120,
            "target_id": "T-A",
            "steal": True,
        }
    )
    assert stolen["lease"]["target_id"] == "T-A"
    assert stolen["lease"]["lease_id"] != a["lease"]["lease_id"]
    held = classify_target(
        state, "scratch-profile-providers", "T-A", caller_lease_id=stolen["lease"]["lease_id"]
    )
    old = classify_target(
        state, "scratch-profile-providers", "T-A", caller_lease_id=a["lease"]["lease_id"]
    )
    assert held["ownership"] == "owned_by_me"
    assert old["ownership"] == "owned_by"


class RecordingTabs:
    worker_id = "scratch-profile-providers"

    def __init__(self, state_root: Path):
        self._state_root = state_root
        self.drive_lock = asyncio.Lock()
        self.target_id = "TA"
        self.session_id = "SA"
        self.activated: list[str] = []
        self.created: list[str] = []
        self.closed: list[str] = []
        self.peeked: list[str] = []
        self.switched: list[str] = []
        self.tabs = [
            {"targetId": "TA", "url": "https://a.example", "type": "page"},
            {"targetId": "TB", "url": "https://b.example", "type": "page"},
        ]

    async def list_tabs(self) -> dict:
        return {"tabs": list(self.tabs)}

    async def new_tab(self, url: str = "about:blank") -> dict:
        tid = f"TNEW{len(self.created)}"
        self.created.append(tid)
        self.activated.append(tid)
        self.target_id = tid
        self.tabs.append({"targetId": tid, "url": url, "type": "page"})
        return {"target_id": tid, "session_id": f"S-{tid}"}

    async def switch_tab(self, target_id: str) -> dict:
        self.switched.append(target_id)
        self.activated.append(target_id)
        self.target_id = target_id
        return {"target_id": target_id, "session_id": f"S-{target_id}"}

    async def peek_tab(self, target_id: str, *, path: str | None = None) -> dict:
        self.peeked.append(target_id)
        return {
            "mode": "peek",
            "target_id": target_id,
            "activated": False,
            "page": {"url": f"https://{target_id}.example", "title": target_id},
            "screenshot": {"path": path or f"/tmp/peek-{target_id}.png"},
        }

    async def close_tab(self, target_id: str | None = None) -> dict:
        tid = target_id or self.target_id
        self.closed.append(tid)
        return {"closed": tid}

    async def navigate(self, url: str) -> dict:
        return {"navigated": url, "target_id": self.target_id}

    async def pin_target(self, target_id: str) -> dict:
        self.target_id = target_id
        return {"target_id": target_id}


def _run(coro):
    return asyncio.run(coro)


def test_tabs_annotated_and_sibling_switch_is_peek(ctx):
    m, _fake, state = ctx
    a = m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "omp-nav", "ttl": 120, "target_id": "TA"})
    b = m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "omp-nav", "ttl": 120, "target_id": "TB"})
    backend = RecordingTabs(state)
    listed = _run(
        _execute_action(
            backend,
            "tabs",
            {"lease_id": a["lease"]["lease_id"], "held_lease_ids": [a["lease"]["lease_id"]]},
        )
    )
    by_id = {t["targetId"]: t["ownership"] for t in listed["tabs"]}
    assert by_id["TA"] == "owned_by_me"
    assert by_id["TB"] == "owned_by"

    peek = _run(
        _execute_action(
            backend,
            "switch_tab",
            {
                "lease_id": a["lease"]["lease_id"],
                "held_lease_ids": [a["lease"]["lease_id"]],
                "dest_target_id": "TB",
            },
        )
    )
    assert peek["mode"] == "peek"
    assert peek["activated"] is False
    assert backend.peeked == ["TB"]
    assert backend.activated == []
    assert backend.target_id == "TA"

    with pytest.raises(DaemonError) as ei:
        _run(
            _execute_action(
                backend,
                "switch_tab",
                {
                    "lease_id": a["lease"]["lease_id"],
                    "held_lease_ids": [a["lease"]["lease_id"]],
                    "dest_target_id": "TB",
                    "steal": True,
                },
            )
        )
    assert ei.value.code == "STEAL_FORBIDDEN"
    assert backend.activated == []
    still = classify_target(state, "scratch-profile-providers", "TB", caller_lease_id=b["lease"]["lease_id"])
    assert still["ownership"] == "owned_by_me"


def test_new_tab_mints_lease_and_close_only_own(ctx):
    m, _fake, state = ctx
    a = m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "omp-nav", "ttl": 120, "target_id": "TA"})
    b = m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "omp-nav", "ttl": 120, "target_id": "TB"})
    backend = RecordingTabs(state)
    created = _run(
        _execute_action(
            backend,
            "new_tab",
            {
                "url": "https://new.example",
                "lease_id": a["lease"]["lease_id"],
                "held_lease_ids": [a["lease"]["lease_id"]],
            },
        )
    )
    assert created["ownership"] == "owned_by_me"
    assert created["lease_id"]
    assert created["target_id"] in backend.created
    mine = classify_target(
        state, "scratch-profile-providers", created["target_id"], caller_lease_id=created["lease_id"]
    )
    assert mine["ownership"] == "owned_by_me"

    with pytest.raises(DaemonError) as ei:
        _run(
            _execute_action(
                backend,
                "close_tab",
                {
                    "lease_id": a["lease"]["lease_id"],
                    "held_lease_ids": [a["lease"]["lease_id"], created["lease_id"]],
                    "dest_target_id": "TB",
                },
            )
        )
    assert ei.value.code == "TARGET_CONFLICT"
    assert backend.closed == []

    closed = _run(
        _execute_action(
            backend,
            "close_tab",
            {
                "lease_id": created["lease_id"],
                "held_lease_ids": [a["lease"]["lease_id"], created["lease_id"]],
                "dest_target_id": created["target_id"],
            },
        )
    )
    assert closed["closed"] == created["target_id"]
    gone = classify_target(
        state, "scratch-profile-providers", created["target_id"], caller_lease_id=created["lease_id"]
    )
    assert gone["ownership"] == "unowned"


def test_navigate_on_sibling_target_is_conflict(ctx):
    m, _fake, state = ctx
    a = m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "omp-nav", "ttl": 120, "target_id": "TA"})
    m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "omp-nav", "ttl": 120, "target_id": "TB"})
    backend = RecordingTabs(state)
    with pytest.raises(DaemonError) as ei:
        _run(
            _execute_action(
                backend,
                "navigate",
                {
                    "url": "https://evil.example",
                    "target_id": "TB",
                    "lease_id": a["lease"]["lease_id"],
                    "held_lease_ids": [a["lease"]["lease_id"]],
                },
            )
        )
    assert ei.value.code == "TARGET_CONFLICT"


def test_mutate_without_lease_id_is_required(ctx):
    m, _fake, state = ctx
    m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "omp-nav", "ttl": 120, "target_id": "TA"})
    backend = RecordingTabs(state)
    with pytest.raises(DaemonError) as ei:
        _run(_execute_action(backend, "navigate", {"url": "https://evil.example", "target_id": "TA"}))
    assert ei.value.code == "TARGET_LEASE_REQUIRED"

    backend.unmanaged = True
    out = _run(_execute_action(backend, "navigate", {"url": "https://ok.example", "target_id": "TA"}))
    assert out["navigated"] == "https://ok.example"


def test_browser_scope_lease_cannot_mutate(ctx):
    from browserctl.store import find_active_lease_for_worker

    m, _fake, state = ctx
    a = m.acquire({"kind": "scratch", "worker_id": "scratch-profile-providers", "owner": "omp-nav", "ttl": 120, "target_id": "TA"})
    browser = find_active_lease_for_worker(state, "scratch-profile-providers")
    assert browser is not None
    backend = RecordingTabs(state)
    with pytest.raises(DaemonError) as ei:
        _run(
            _execute_action(
                backend,
                "navigate",
                {"url": "https://evil.example", "target_id": "TA", "lease_id": browser["lease_id"]},
            )
        )
    assert ei.value.code == "TARGET_LEASE_REQUIRED"
    with pytest.raises(DaemonError) as ei2:
        _run(
            _execute_action(
                backend,
                "new_tab",
                {"url": "https://evil.example", "lease_id": browser["lease_id"]},
            )
        )
    assert ei2.value.code == "TARGET_LEASE_REQUIRED"
    # real target lease still works
    ok = _run(
        _execute_action(
            backend,
            "navigate",
            {"url": "https://ok.example", "target_id": "TA", "lease_id": a["lease"]["lease_id"]},
        )
    )
    assert ok["navigated"] == "https://ok.example"
