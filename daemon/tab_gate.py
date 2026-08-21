"""Lease-aware tab policy for daemon rpc.

Navigators are identified by lease id. Looking is free. Mutating a tab
you do not hold requires steal=true (which transfers the target lease).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rpc import DaemonError


def cmd_lease_id(cmd: dict) -> str:
    return str(cmd.get("lease_id") or "").strip()


def cmd_held_leases(cmd: dict) -> list[str]:
    raw = cmd.get("held_lease_ids")
    if isinstance(raw, list):
        out = [str(x) for x in raw if x]
        if out:
            return out
    lid = cmd_lease_id(cmd)
    return [lid] if lid else []


def dest_target_id(cmd: dict, *, fallback: str | None = None) -> str:
    return str(
        cmd.get("dest_target_id") or cmd.get("switch_to") or fallback or ""
    ).strip()


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
        held_lease_ids=set(cmd_held_leases(cmd)),
    )


def annotate(backend: Any, cmd: dict, tabs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    from browserctl.tab_policy import annotate_tabs

    return annotate_tabs(
        tabs,
        state_root=state_root_for(backend),
        worker_id=str(getattr(backend, "worker_id", "") or ""),
        caller_lease_id=cmd_lease_id(cmd),
        held_lease_ids=cmd_held_leases(cmd),
    )


def _parent_lease(backend: Any, cmd: dict) -> dict[str, Any] | None:
    from browserctl.store import load_lease

    lid = cmd_lease_id(cmd)
    if not lid:
        return None
    row = load_lease(state_root_for(backend), lid)
    if not row or row.get("status") in ("released", "reaped"):
        return None
    return row


def mint_target_lease(backend: Any, cmd: dict, target_id: str, *, steal: bool = False) -> dict[str, Any]:
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
        "steal": steal,
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
    """Refuse mutating drive on a tab this sidecar does not hold."""
    if action in {
        "tabs",
        "list_tabs",
        "ping",
        "health",
        "drain_events",
        "peek_tab",
        "new_tab",
        "switch_tab",
        "close_tab",
    }:
        return
    if not cmd_lease_id(cmd):
        return
    tid = str(cmd.get("target_id") or getattr(backend, "target_id", "") or "").strip()
    if not tid:
        return
    meta = classify(backend, cmd, tid)
    if meta.get("ownership") == "owned_by_me":
        return
    raise DaemonError(
        "TARGET_CONFLICT",
        f"tab {tid} is {meta.get('ownership')}; switch_tab steal=true to drive it",
        retryable=False,
        page=meta,
    )


async def handle_tabs(backend: Any, cmd: dict) -> dict:
    result = dict(await backend.list_tabs())
    result["tabs"] = annotate(backend, cmd, result.get("tabs") or [])
    return result


async def handle_new_tab(backend: Any, cmd: dict) -> dict:
    url = cmd.get("url") or "about:blank"
    created = dict(await backend.new_tab(url))
    tid = str(created.get("target_id") or "")
    if not tid:
        return created
    if not cmd_lease_id(cmd):
        created["ownership"] = "unowned"
        created["mode"] = "drive"
        return created
    try:
        minted = mint_target_lease(backend, cmd, tid, steal=False)
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
    created["lease"] = lease
    return created


async def handle_switch_tab(backend: Any, cmd: dict) -> dict:
    dest = dest_target_id(cmd, fallback=cmd.get("target_id"))
    if not dest:
        raise DaemonError("MISSING_PARAM", "switch_tab requires dest_target_id")
    steal = bool(cmd.get("steal"))
    if not cmd_lease_id(cmd):
        result = dict(await backend.switch_tab(dest))
        result["mode"] = "drive"
        result["ownership"] = "unowned"
        return result
    meta = classify(backend, cmd, dest)
    own = meta.get("ownership")
    if own == "owned_by_me":
        result = dict(await backend.switch_tab(dest))
        result["mode"] = "drive"
        result["ownership"] = own
        result["lease_id"] = meta.get("lease_id") or cmd_lease_id(cmd)
        return result
    if steal:
        minted = mint_target_lease(backend, cmd, dest, steal=own == "owned_by")
        result = dict(await backend.switch_tab(dest))
        lease = minted.get("lease") or {}
        result["mode"] = "steal" if own == "owned_by" else "drive"
        result["ownership"] = "owned_by_me"
        result["lease_id"] = lease.get("lease_id")
        result["lease"] = lease
        result["stolen_from"] = meta.get("lease_id")
        return result
    peek = getattr(backend, "peek_tab", None)
    if callable(peek):
        result = dict(await peek(dest, path=cmd.get("path")))
    else:
        result = {"mode": "peek", "target_id": dest, "activated": False}
    result["ownership"] = own
    if meta.get("lease_id"):
        result["lease_id"] = meta.get("lease_id")
    result["hint"] = "read-only peek; pass steal=true to take mutating ownership"
    return result


async def handle_close_tab(backend: Any, cmd: dict) -> dict:
    dest = dest_target_id(cmd, fallback=cmd.get("target_id") or getattr(backend, "target_id", None))
    if not dest:
        raise DaemonError("MISSING_PARAM", "close_tab requires dest_target_id")
    if cmd_lease_id(cmd):
        meta = classify(backend, cmd, dest)
        if meta.get("ownership") != "owned_by_me":
            raise DaemonError(
                "TARGET_CONFLICT",
                f"close_tab only on your leases (tab {dest} is {meta.get('ownership')})",
                retryable=False,
                page=meta,
            )
        result = dict(await backend.close_tab(dest))
        if meta.get("lease_id"):
            released = release_lease(backend, str(meta["lease_id"]))
            result["released_lease"] = meta["lease_id"]
            result["release"] = released
        return result
    return dict(await backend.close_tab(dest))
