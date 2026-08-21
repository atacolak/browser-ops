"""Tab ownership labels.

A chrome page is either owned by an active target lease or unowned.
Clients are identified by lease id, not a shared owner string.

Occupancy (active | expiring) is not mutation authority. Only an
``active`` target lease can drive.
"""

from __future__ import annotations

from typing import Any

from browserctl.store import find_target_lease, lease_scope, load_lease
from browserctl.target_registry import active_owned_targets, load_registry

OWNED_BY_ME = "owned_by_me"
OWNED_BY = "owned_by"
UNOWNED = "unowned"


def _tid(tab: dict[str, Any]) -> str:
    return str(tab.get("targetId") or tab.get("target_id") or "")


def classify_target(
    state_root: Any,
    worker_id: str,
    target_id: str,
    *,
    caller_lease_id: str | None,
) -> dict[str, Any]:
    """Return ownership metadata for one chrome target.

    ``owned_by_me`` only when the live lease on that tab is exactly
    *caller_lease_id*. Client-asserted extra ids do not confer ownership.
    """
    tid = str(target_id or "")
    caller = str(caller_lease_id or "")
    row = find_target_lease(state_root, worker_id, tid)
    if row is None:
        registry = load_registry(state_root, worker_id)
        owned = active_owned_targets(registry)
        meta = owned.get(tid)
        if not meta:
            return {"target_id": tid, "ownership": UNOWNED}
        lease_id = str(meta.get("lease_id") or "")
        if caller and lease_id == caller:
            return {
                "target_id": tid,
                "ownership": OWNED_BY_ME,
                "lease_id": lease_id,
                "owner": meta.get("owner"),
            }
        return {
            "target_id": tid,
            "ownership": OWNED_BY,
            "lease_id": lease_id or None,
            "owner": meta.get("owner"),
        }
    lease_id = str(row.get("lease_id") or "")
    if caller and lease_id == caller:
        return {
            "target_id": tid,
            "ownership": OWNED_BY_ME,
            "lease_id": lease_id,
            "owner": row.get("owner"),
        }
    return {
        "target_id": tid,
        "ownership": OWNED_BY,
        "lease_id": lease_id or None,
        "owner": row.get("owner"),
    }


def annotate_tabs(
    tabs: list[dict[str, Any]] | None,
    *,
    state_root: Any,
    worker_id: str,
    caller_lease_id: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tab in tabs or []:
        item = dict(tab)
        tid = _tid(item)
        meta = classify_target(
            state_root,
            worker_id,
            tid,
            caller_lease_id=caller_lease_id,
        )
        item["ownership"] = meta["ownership"]
        out.append(item)
    return out


def lease_is_active(state_root: Any, lease_id: str | None) -> dict[str, Any] | None:
    """Occupancy: still holds a slot (active or expiring). Not mutation authority."""
    if not lease_id:
        return None
    row = load_lease(state_root, str(lease_id))
    if not row:
        return None
    if row.get("status") not in ("active", "expiring"):
        return None
    return row


def lease_can_mutate(
    state_root: Any,
    lease_id: str | None,
    *,
    worker_id: str | None = None,
    target_id: str | None = None,
) -> dict[str, Any] | None:
    """Authorization: active target lease, optional worker/tab match."""
    if not lease_id:
        return None
    row = load_lease(state_root, str(lease_id))
    if not row:
        return None
    if row.get("status") != "active":
        return None
    if lease_scope(row) != "target":
        return None
    if worker_id and str(row.get("worker_id") or "") != str(worker_id):
        return None
    if target_id is not None and str(row.get("target_id") or "") != str(target_id):
        return None
    return row
