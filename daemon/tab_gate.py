"""Lease-aware tab policy for daemon rpc.

One request, one capability: ``lease_id`` must be an active *target* lease
on this worker, and for mutating drive it must own ``target_id``.
Looking is free. Steal is not a socket verb — operator recovery is
``browserctl --steal``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rpc import DaemonError

READ_ONLY = frozenset({
    "tabs",
    "list_tabs",
    "ping",
    "health",
    "drain_events",
    "peek_tab",
})


def cmd_lease_id(cmd: dict) -> str:
    return str(cmd.get("lease_id") or "").strip()


def dest_target_id(cmd: dict, *, fallback: str | None = None) -> str:
    return str(
        cmd.get("dest_target_id") or cmd.get("switch_to") or fallback or ""
    ).strip()


def unmanaged(backend: Any) -> bool:
    return bool(getattr(backend, "unmanaged", False))


def state_root_for(backend: Any) -> Path:
    override = getattr(backend, "_state_root", None)
    if override:
        return Path(override)
    env = (os.environ.get("BROWSER_OPS_STATE") or os.environ.get("BROWSERCTL_STATE_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "state"


def ops_root() -> Path:
    env = (os.environ.get("BROWSER_OPS_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def classify(backend: Any, cmd: dict, target_id: str) -> dict[str, Any]:
    from browserctl.tab_policy import classify_target

    return classify_target(
        state_root_for(backend),
        str(getattr(backend, "worker_id", "") or ""),
        target_id,
        caller_lease_id=cmd_lease_id(cmd),
    )


def annotate(backend: Any, cmd: dict, tabs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    from browserctl.tab_policy import annotate_tabs

    return annotate_tabs(
        tabs,
        state_root=state_root_for(backend),
        worker_id=str(getattr(backend, "worker_id", "") or ""),
        caller_lease_id=cmd_lease_id(cmd),
    )


def require_live_lease(backend: Any, cmd: dict) -> dict[str, Any]:
    """Fail closed: mutating drive needs an active target lease on this worker.

    Occupancy (expiring) is not enough. Tab match is a separate check.
    """
    from browserctl.tab_policy import lease_can_mutate

    if unmanaged(backend):
        return {}
    lid = cmd_lease_id(cmd)
    if not lid:
        raise DaemonError(
            "TARGET_LEASE_REQUIRED",
            "mutating drive requires an active target lease",
            retryable=False,
        )
    worker = str(getattr(backend, "worker_id", "") or "") or None
    row = lease_can_mutate(
        state_root_for(backend),
        lid,
        worker_id=worker,
    )
    if not row:
        raise DaemonError(
            "TARGET_LEASE_REQUIRED",
            "stored target lease is missing, inactive, wrong worker, or not target-scoped",
            retryable=False,
        )
    return row


def retarget_live_lease(backend: Any, cmd: dict, new_target_id: str) -> dict[str, Any] | None:
    """Rewrite this live target lease onto a replacement CDP page after chrome death."""
    from browserctl.store import load_lease, save_lease, touch_lease
    from browserctl.target_registry import release_target, upsert_target

    if unmanaged(backend):
        return None
    lid = cmd_lease_id(cmd)
    if not lid or not new_target_id:
        return None
    root = state_root_for(backend)
    row = load_lease(root, lid)
    if not row or row.get("status") != "active":
        return None
    old = str(row.get("target_id") or "")
    worker = str(getattr(backend, "worker_id", "") or row.get("worker_id") or "")
    if old == new_target_id:
        return row
    row = touch_lease(row)
    row["target_id"] = new_target_id
    env = dict(row.get("env") or {})
    env["BROWSERCTL_TARGET_ID"] = new_target_id
    row["env"] = env
    save_lease(root, row)
    if worker:
        if old:
            try:
                release_target(root, worker, old)
            except Exception:
                pass
        upsert_target(
            root,
            worker,
            target_id=new_target_id,
            owner=str(row.get("owner") or "omp-nav"),
            lease_id=lid,
            url=None,
            make_active=True,
        )
    cmd["target_id"] = new_target_id
    return row


def _conflict(tid: str, ownership: str | None) -> None:
    raise DaemonError(
        "TARGET_CONFLICT",
        f"tab {tid} is {ownership}; peek with switch_tab, or operator recovery via browserctl --steal",
        retryable=False,
        page={"target_id": tid, "ownership": ownership},
    )


def _parent_lease(backend: Any, cmd: dict) -> dict[str, Any] | None:
    row = require_live_lease(backend, cmd)
    return row or None


def mint_target_lease(backend: Any, cmd: dict, target_id: str) -> dict[str, Any]:
    from browserctl.errors import BrowserctlError
    from browserctl.manager import Manager

    parent = _parent_lease(backend, cmd)
    worker = str(getattr(backend, "worker_id", "") or "")
    req: dict[str, Any] = {
        "kind": (parent or {}).get("kind") or "scratch",
        "worker_id": worker,
        "owner": (parent or {}).get("owner") or "omp-nav",
        "mode": (parent or {}).get("mode") or "persistent",
        "target_id": target_id,
    }
    ttl = (parent or {}).get("ttl_seconds")
    if ttl is not None:
        req["ttl"] = ttl
    try:
        return Manager(root=ops_root(), state_root=state_root_for(backend)).acquire(req)
    except BrowserctlError as e:
        raise DaemonError(e.code, e.message, retryable=False, page=e.details) from e


def release_lease(backend: Any, lease_id: str) -> dict[str, Any] | None:
    from browserctl.errors import BrowserctlError
    from browserctl.manager import Manager

    if not lease_id:
        return None
    try:
        return Manager(root=ops_root(), state_root=state_root_for(backend)).release(
            lease_id=lease_id, force=True
        )
    except BrowserctlError as e:
        raise DaemonError(e.code, e.message, retryable=False, page=e.details) from e


async def gate_mutate(backend: Any, action: str, cmd: dict) -> None:
    """Refuse mutating drive unless this lease owns the tab."""
    if action in READ_ONLY:
        return
    if unmanaged(backend):
        return
    row = require_live_lease(backend, cmd)
    tid = str(cmd.get("target_id") or getattr(backend, "target_id", "") or "").strip()
    if not tid:
        return
    if str(row.get("target_id") or "") == tid:
        return
    meta = classify(backend, cmd, tid)
    _conflict(tid, str(meta.get("ownership") or "owned_by"))


async def handle_tabs(backend: Any, cmd: dict) -> dict:
    result = dict(await backend.list_tabs())
    result["tabs"] = annotate(backend, cmd, result.get("tabs") or [])
    return result


async def handle_new_tab(backend: Any, cmd: dict) -> dict:
    if not unmanaged(backend):
        require_live_lease(backend, cmd)
    url = cmd.get("url") or "about:blank"
    created = dict(await backend.new_tab(url))
    tid = str(created.get("target_id") or "")
    if not tid:
        return created
    if unmanaged(backend):
        created["ownership"] = "unowned"
        created["mode"] = "drive"
        return created
    try:
        minted = mint_target_lease(backend, cmd, tid)
    except Exception:
        close = getattr(backend, "close_tab", None)
        if callable(close):
            try:
                await close(tid)
            except Exception:
                pass
        raise
    lease = minted.get("lease") or {}
    created["mode"] = "drive"
    created["ownership"] = "owned_by_me"
    created["lease_id"] = lease.get("lease_id")
    return created


async def handle_switch_tab(backend: Any, cmd: dict) -> dict:
    dest = dest_target_id(cmd, fallback=cmd.get("target_id"))
    if not dest:
        raise DaemonError("MISSING_PARAM", "switch_tab requires dest_target_id")
    if bool(cmd.get("steal")):
        raise DaemonError(
            "STEAL_FORBIDDEN",
            "steal is operator recovery via browserctl launch --steal --target-id, not a socket verb",
            retryable=False,
        )
    if unmanaged(backend):
        result = dict(await backend.switch_tab(dest))
        result["mode"] = "drive"
        result["ownership"] = "unowned"
        return result
    row = require_live_lease(backend, cmd)
    if str(row.get("target_id") or "") == dest:
        result = dict(await backend.switch_tab(dest))
        result["mode"] = "drive"
        result["ownership"] = "owned_by_me"
        result["lease_id"] = cmd_lease_id(cmd)
        return result
    peek = getattr(backend, "peek_tab", None)
    if callable(peek):
        result = dict(await peek(dest, path=cmd.get("path")))
    else:
        result = {"mode": "peek", "target_id": dest, "activated": False}
    result["ownership"] = classify(backend, cmd, dest).get("ownership")
    result["hint"] = "read-only peek; operator recovery is browserctl launch --steal --target-id"
    return result


async def handle_close_tab(backend: Any, cmd: dict) -> dict:
    dest = dest_target_id(cmd, fallback=cmd.get("target_id") or getattr(backend, "target_id", None))
    if not dest:
        raise DaemonError("MISSING_PARAM", "close_tab requires dest_target_id")
    if unmanaged(backend):
        return dict(await backend.close_tab(dest))
    row = require_live_lease(backend, cmd)
    if str(row.get("target_id") or "") != dest:
        meta = classify(backend, cmd, dest)
        _conflict(dest, str(meta.get("ownership") or "owned_by"))
    result = dict(await backend.close_tab(dest))
    lid = str(row.get("lease_id") or cmd_lease_id(cmd) or "")
    if lid:
        released = release_lease(backend, lid)
        result["released_lease"] = lid
        result["release"] = released
    return result
