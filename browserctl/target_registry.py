"""Authoritative per-worker target ownership (not the mirrored active tab).

Layout: ``state/<worker>/control/targets.json``

Schema version is independent of package version and of ``active-target.json``.
"""

from __future__ import annotations

import time
from typing import Any

from browserctl.atomic import atomic_write_json, read_json
from browserctl.locks import target_state_mutex
from browserctl.paths import active_target_path, target_registry_path

REGISTRY_SCHEMA_VERSION = 1


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def empty_registry(worker_id: str) -> dict[str, Any]:
    return {
        "version": REGISTRY_SCHEMA_VERSION,
        "worker_id": worker_id,
        "targets": {},
        "active_target_id": None,
        "updated_at": _iso(),
    }


def load_registry(state_root: str | Any, worker_id: str) -> dict[str, Any]:
    raw = read_json(target_registry_path(state_root, worker_id))
    if not isinstance(raw, dict):
        return empty_registry(worker_id)
    raw.setdefault("version", REGISTRY_SCHEMA_VERSION)
    raw.setdefault("worker_id", worker_id)
    raw.setdefault("targets", {})
    raw.setdefault("active_target_id", None)
    return raw


def _project_active_target(state_root: str | Any, registry: dict[str, Any]) -> None:
    """Keep v1 active-target.json as a presentation projection."""
    from daemon.target_state import publish_active_target

    worker_id = str(registry.get("worker_id") or "")
    path = active_target_path(state_root, worker_id)
    active = registry.get("active_target_id")
    targets = registry.get("targets") or {}
    page = None
    row = targets.get(active) if active else None
    if isinstance(row, dict) and row.get("url"):
        page = {"url": row.get("url"), "target_id": active}
    publish_active_target(
        path,
        worker_id=worker_id,
        active_target_id=active,
        page=page,
        extra={"targets": targets},
    )


def upsert_target(
    state_root: str | Any,
    worker_id: str,
    *,
    target_id: str,
    owner: str,
    lease_id: str,
    status: str = "active",
    url: str | None = None,
    make_active: bool = True,
) -> dict[str, Any]:
    path = target_registry_path(state_root, worker_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with target_state_mutex(path):
        reg = load_registry(state_root, worker_id)
        targets = dict(reg.get("targets") or {})
        prev = dict(targets.get(target_id) or {})
        row = {
            "target_id": target_id,
            "worker_id": worker_id,
            "owner": owner,
            "lease_id": lease_id,
            "status": status,
            "created_at": prev.get("created_at") or _iso(),
            "updated_at": _iso(),
        }
        if url or prev.get("url"):
            row["url"] = url or prev.get("url")
        targets[target_id] = row
        reg["targets"] = targets
        if make_active:
            reg["active_target_id"] = target_id
        reg["updated_at"] = _iso()
        atomic_write_json(path, reg)
    _project_active_target(state_root, reg)
    return reg


def release_target(
    state_root: str | Any,
    worker_id: str,
    target_id: str,
) -> dict[str, Any]:
    path = target_registry_path(state_root, worker_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with target_state_mutex(path):
        reg = load_registry(state_root, worker_id)
        targets = dict(reg.get("targets") or {})
        row = dict(targets.get(target_id) or {})
        if row:
            row["status"] = "released"
            row["updated_at"] = _iso()
            row.pop("owner", None)
            row.pop("lease_id", None)
            targets[target_id] = row
        reg["targets"] = targets
        if reg.get("active_target_id") == target_id:
            nxt = next(
                (
                    tid
                    for tid, meta in targets.items()
                    if isinstance(meta, dict) and meta.get("status") == "active"
                ),
                None,
            )
            reg["active_target_id"] = nxt
        reg["updated_at"] = _iso()
        atomic_write_json(path, reg)
    _project_active_target(state_root, reg)
    return reg


def active_owned_targets(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for tid, row in (registry.get("targets") or {}).items():
        if isinstance(row, dict) and row.get("status") == "active" and row.get("lease_id"):
            out[str(tid)] = row
    return out
