"""Tab ownership labels for swarm peek/steal.

A chrome page is either owned by an active target lease or unowned.
Navigators are identified by lease id, not the shared owner string (omp-nav).
"""

from __future__ import annotations

from typing import Any

from browserctl.store import find_target_lease, load_lease
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
    held_lease_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Return ownership metadata for one chrome target."""
    tid = str(target_id or "")
    held = {str(x) for x in (held_lease_ids or set()) if x}
    if caller_lease_id:
        held.add(str(caller_lease_id))
    row = find_target_lease(state_root, worker_id, tid)
    if row is None:
        registry = load_registry(state_root, worker_id)
        owned = active_owned_targets(registry)
        meta = owned.get(tid)
        if not meta:
            return {"target_id": tid, "ownership": UNOWNED}
        lease_id = str(meta.get("lease_id") or "")
        if lease_id and lease_id in held:
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
    if lease_id and lease_id in held:
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
    held_lease_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    held = {str(x) for x in (held_lease_ids or []) if x}
    out: list[dict[str, Any]] = []
    for tab in tabs or []:
        item = dict(tab)
        tid = _tid(item)
        meta = classify_target(
            state_root,
            worker_id,
            tid,
            caller_lease_id=caller_lease_id,
            held_lease_ids=held,
        )
        item["ownership"] = meta["ownership"]
        if meta.get("lease_id"):
            item["lease_id"] = meta["lease_id"]
        if meta.get("owner"):
            item["owner"] = meta["owner"]
        out.append(item)
    return out


def lease_is_active(state_root: Any, lease_id: str | None) -> dict[str, Any] | None:
    if not lease_id:
        return None
    row = load_lease(state_root, str(lease_id))
    if not row:
        return None
    if row.get("status") not in ("active", "expiring"):
        return None
    return row
