"""
Atomic lease store under state/control.

Layout::

    state/control/
      index.json                 # lease_id → worker_id, status summary
      index.lock                 # global index RMW mutex
      ports.lock                 # scratch CDP port allocation
      leases/<lease_id>.json     # full lease record
      workers/<worker_id>/worker.lock  # one mutation lease per worker

Locking: directory ``mkdir`` as exclusive lock for mutations on a worker / index.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from browserctl.atomic import atomic_write_json, read_json
from browserctl.errors import LeaseNotFound
from browserctl.locks import index_mutex, worker_mutex  # re-export
from browserctl.paths import (
    index_path,
    lease_path,
    leases_dir,
)

LEASE_SCHEMA_VERSION = 1

__all__ = [
    "worker_mutex",
    "index_mutex",
    "load_index",
    "save_index",
    "load_lease",
    "save_lease",
    "delete_lease_record",
    "list_lease_ids",
    "find_active_lease_for_worker",
    "iter_leases",
    "is_expired",
    "touch_lease",
    "base_lease",
    "require_lease",
    "new_lease_id",
]


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else _now()))


def new_lease_id() -> str:
    return uuid.uuid4().hex[:12]


def load_index(state_root: Path | str) -> dict[str, Any]:
    raw = read_json(index_path(state_root))
    if not isinstance(raw, dict):
        return {"version": 1, "leases": {}}
    raw.setdefault("version", 1)
    raw.setdefault("leases", {})
    return raw


def save_index(state_root: Path | str, index: dict[str, Any]) -> None:
    atomic_write_json(index_path(state_root), index)


def load_lease(state_root: Path | str, lease_id: str) -> dict[str, Any] | None:
    data = read_json(lease_path(state_root, lease_id))
    return data if isinstance(data, dict) else None


def save_lease(state_root: Path | str, lease: dict[str, Any]) -> Path:
    lid = lease["lease_id"]
    path = lease_path(state_root, lid)
    atomic_write_json(path, lease)
    with index_mutex(state_root):
        index = load_index(state_root)
        index["leases"][lid] = {
            "lease_id": lid,
            "worker_id": lease.get("worker_id"),
            "kind": lease.get("kind"),
            "status": lease.get("status"),
            "owner": lease.get("owner"),
            "expires_at": lease.get("expires_at"),
            "updated_at": lease.get("updated_at"),
        }
        save_index(state_root, index)
    return path


def delete_lease_record(state_root: Path | str, lease_id: str) -> None:
    path = lease_path(state_root, lease_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    with index_mutex(state_root):
        index = load_index(state_root)
        if lease_id in index.get("leases", {}):
            del index["leases"][lease_id]
            save_index(state_root, index)


def list_lease_ids(state_root: Path | str) -> list[str]:
    d = leases_dir(state_root)
    if not d.exists():
        return sorted((load_index(state_root).get("leases") or {}).keys())
    ids = [p.stem for p in d.glob("*.json")]
    return sorted(ids)


def find_active_lease_for_worker(
    state_root: Path | str,
    worker_id: str,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Return non-terminal lease holding mutation on worker, if any."""
    _ = now  # reserved for future soft-expiry filtering
    for lid in list_lease_ids(state_root):
        lease = load_lease(state_root, lid)
        if not lease:
            continue
        if lease.get("worker_id") != worker_id:
            continue
        status = lease.get("status")
        if status in ("released", "reaped", "failed"):
            continue
        # expired still "active" until reap — blocks new acquire
        if status in ("active", "expiring", "acquired"):
            return lease
    return None


def iter_leases(state_root: Path | str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for lid in list_lease_ids(state_root):
        lease = load_lease(state_root, lid)
        if lease:
            out.append(lease)
    return out


def is_expired(lease: dict[str, Any], *, now: float | None = None) -> bool:
    now = now if now is not None else _now()
    exp = lease.get("expires_at")
    if exp is None:
        return False
    try:
        return float(exp) <= now
    except (TypeError, ValueError):
        return False


def touch_lease(
    lease: dict[str, Any],
    *,
    ttl_seconds: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now = now if now is not None else _now()
    lease = dict(lease)
    lease["updated_at"] = _iso(now)
    lease["updated_ts"] = now
    if ttl_seconds is not None:
        lease["ttl_seconds"] = float(ttl_seconds)
        lease["expires_at"] = now + float(ttl_seconds)
        lease["expires_at_iso"] = _iso(lease["expires_at"])
    return lease


def base_lease(
    *,
    lease_id: str | None = None,
    worker_id: str,
    kind: str,
    owner: str,
    ttl_seconds: float,
    mode: str = "persistent",
    adapter: str,
    resources: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    meta: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now = now if now is not None else _now()
    lid = lease_id or new_lease_id()
    exp = now + float(ttl_seconds)
    return {
        "version": LEASE_SCHEMA_VERSION,
        "lease_id": lid,
        "worker_id": worker_id,
        "kind": kind,
        "adapter": adapter,
        "owner": owner,
        "mode": mode,  # persistent | one_shot
        "status": "active",
        "created_at": _iso(now),
        "created_ts": now,
        "updated_at": _iso(now),
        "updated_ts": now,
        "ttl_seconds": float(ttl_seconds),
        "expires_at": exp,
        "expires_at_iso": _iso(exp),
        "resources": resources or {},
        "env": env or {},
        "watch": None,
        "meta": meta or {},
    }


def require_lease(state_root: Path | str, lease_id: str) -> dict[str, Any]:
    lease = load_lease(state_root, lease_id)
    if not lease:
        raise LeaseNotFound(lease_id=lease_id)
    return lease
