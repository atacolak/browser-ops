"""
Lease manager: acquire → env → release/reap.

One mutation lease per worker. Adapters start browsers.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from browserctl.adapters import get_adapter
from browserctl.errors import InvalidRequest, LeaseConflict, LeaseNotFound
from browserctl.paths import (
    active_target_path,
    resolve_root,
    resolve_state_root,
)
from browserctl.store import (
    base_lease,
    find_active_lease_for_worker,
    is_auto_reap_eligible,
    is_expired,
    iter_leases,
    load_lease,
    require_lease,
    save_lease,
    touch_lease,
    worker_mutex,
)

DEFAULT_TTL = 3600.0  # 1h
DEFAULT_ONE_SHOT_TTL = 900.0  # 15m

_SELECTOR_KEYS = frozenset(
    "kind adapter worker worker_id label cdp_port country city "
    "headed headless no_start attach_only".split()
)


def _apply_headed_headless(req: dict[str, Any]) -> None:
    if req.get("headed") is None:
        return
    headed = bool(req["headed"])
    if req.get("headless") is not None and bool(req["headless"]) != (not headed):
        raise InvalidRequest(
            "headed/headless contradict", headed=headed, headless=bool(req["headless"])
        )
    req["headless"] = not headed


def _public_lease(lease: dict[str, Any]) -> dict[str, Any]:
    out = dict(lease)
    resources = dict(out.get("resources") or {})
    resources.pop("egress_info", None)
    out["resources"] = resources
    return out


def _lease_env(lease: dict[str, Any], *, state_root: Path) -> dict[str, str]:
    env = dict(lease.get("env") or {})
    resources = lease.get("resources") or {}
    worker_id = lease.get("worker_id")
    tsp = resources.get("target_state_path") or str(
        active_target_path(state_root, worker_id)
    )
    env.setdefault("BROWSER_HARNESS_WORKER", str(worker_id))
    env.setdefault("BROWSER_TARGET_STATE", tsp)
    env.setdefault("BROWSERCTL_LEASE_ID", lease["lease_id"])
    profile_name = lease.get("profile_name")
    if profile_name:
        env["BROWSERCTL_PROFILE_NAME"] = str(profile_name)
    if resources.get("cdp_url"):
        env.setdefault("BROWSER_CDP_URL", str(resources["cdp_url"]))
    return env


def _enrich_resources(
    acquired: dict[str, Any],
    *,
    state_root: Path,
) -> dict[str, Any]:
    resources = dict(acquired.get("resources") or {})
    worker_id = acquired["worker_id"]
    resources.setdefault(
        "target_state_path",
        str(active_target_path(state_root, worker_id)),
    )
    if resources.get("cdp_port") and not resources.get("cdp_url"):
        resources["cdp_url"] = f"http://127.0.0.1:{resources['cdp_port']}"
    return resources


class Manager:
    def __init__(
        self,
        *,
        root: Path | str | None = None,
        state_root: Path | str | None = None,
    ):
        self.root = resolve_root(root)
        self.state_root = resolve_state_root(state_root, root=self.root)

    def list_leases(
        self,
        *,
        include_terminal: bool = False,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        now = now if now is not None else time.time()
        rows = []
        for lease in iter_leases(self.state_root):
            status = lease.get("status")
            if not include_terminal and status in ("released", "reaped", "failed"):
                continue
            row = _public_lease(lease)
            row["expired"] = is_expired(lease, now=now)
            rows.append(row)
        rows.sort(key=lambda r: r.get("created_ts") or 0, reverse=True)
        return rows

    def status(
        self,
        *,
        lease_id: str | None = None,
        worker_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = now if now is not None else time.time()
        if lease_id:
            lease = require_lease(self.state_root, lease_id)
        elif worker_id:
            lease = find_active_lease_for_worker(
                self.state_root, worker_id, now=now
            )
            if not lease:
                matches = [
                    L
                    for L in iter_leases(self.state_root)
                    if L.get("worker_id") == worker_id
                ]
                if not matches:
                    raise LeaseNotFound(worker_id=worker_id)
                matches.sort(key=lambda r: r.get("updated_ts") or 0, reverse=True)
                lease = matches[0]
        else:
            raise InvalidRequest("status requires --lease or --worker")

        adapter = get_adapter(lease.get("adapter") or lease.get("kind") or "scratch")
        live = {}
        try:
            live = adapter.status(lease)
        except Exception as e:
            live = {"error": str(e)}

        target = None
        tsp = (lease.get("resources") or {}).get("target_state_path")
        if tsp:
            from daemon.target_state import read_json

            target = read_json(tsp)

        out = _public_lease(lease)
        out["expired"] = is_expired(lease, now=now)
        out["live"] = live
        out["active_target"] = target
        out["env"] = _lease_env(lease, state_root=self.state_root)
        return out

    def acquire(self, request: dict[str, Any]) -> dict[str, Any]:
        req = dict(request)
        raw_profile = req.pop("profile_name", None) or req.pop("profile", None)
        profile_name = (
            str(raw_profile).strip() or None if raw_profile is not None else None
        )

        if profile_name:
            from browserctl.profiles import ProfileRegistry, validate_launch

            present = sorted(
                k for k in _SELECTOR_KEYS if k in req and req.get(k) is not None
            )
            if present:
                raise InvalidRequest(
                    "profile excludes launch selector fields "
                    f"({', '.join(present)})",
                    profile=profile_name,
                    fields=present,
                )
            for k in _SELECTOR_KEYS:
                req.pop(k, None)
            profile = ProfileRegistry(root=self.root).show(profile_name)
            profile_name = profile["name"]
            launch = validate_launch(dict(profile.get("launch") or {}))
            req.update(launch)
            if "worker" in launch:
                req["worker_id"] = launch["worker"]
            kind_from_profile = str(launch.get("kind") or "").strip().lower()
            if kind_from_profile in ("scratch", "adhoc"):
                if not (req.get("worker_id") or req.get("worker")):
                    req["worker_id"] = f"scratch-profile-{profile_name}"

        _apply_headed_headless(req)

        kind = (req.get("kind") or req.get("adapter") or "").strip().lower()
        if not kind:
            raise InvalidRequest(
                "acquire requires --kind or --profile (scratch|vpn)",
            )
        if kind in ("default",):
            raise InvalidRequest("refusing managed default kind")

        owner = (req.get("owner") or "operator").strip()
        mode = (req.get("mode") or "persistent").strip()
        if mode not in ("persistent", "one_shot"):
            raise InvalidRequest("mode must be persistent|one_shot")

        ttl = req.get("ttl")
        if ttl is None:
            ttl = DEFAULT_ONE_SHOT_TTL if mode == "one_shot" else DEFAULT_TTL
        ttl = float(ttl)

        adapter = get_adapter(kind)
        req["root"] = str(self.root)
        req["state_root"] = str(self.state_root)

        known_worker = (req.get("worker_id") or req.get("worker") or "").strip()
        if known_worker and known_worker != "default":
            with worker_mutex(self.state_root, known_worker):
                existing = find_active_lease_for_worker(self.state_root, known_worker)
                if existing:
                    raise LeaseConflict(
                        known_worker, holder=_public_lease(existing)
                    )

        acquired = adapter.acquire(req)
        worker_id = acquired["worker_id"]
        if not worker_id or worker_id == "default":
            raise InvalidRequest(
                "adapter returned unmanaged/default worker_id",
                worker_id=worker_id,
            )

        meta = dict(acquired.get("meta") or {})
        attached = bool(meta.get("attached_existing"))

        with worker_mutex(self.state_root, worker_id):
            existing = find_active_lease_for_worker(self.state_root, worker_id)
            if existing:
                if not attached:
                    try:
                        adapter.release(
                            {
                                "worker_id": worker_id,
                                "resources": acquired.get("resources") or {},
                                "meta": meta,
                                "mode": mode,
                            },
                            force=True,
                        )
                    except Exception:
                        pass
                raise LeaseConflict(worker_id, holder=_public_lease(existing))

            resources = _enrich_resources(acquired, state_root=self.state_root)
            lease = base_lease(
                worker_id=worker_id,
                kind=acquired.get("kind") or kind,
                owner=owner,
                ttl_seconds=ttl,
                mode=mode,
                adapter=acquired.get("adapter") or kind,
                resources=resources,
                env=dict(acquired.get("env") or {}),
                meta=meta,
            )
            if profile_name:
                lease["profile_name"] = profile_name
            if req.get("auto_reap") is True:
                lease["auto_reap"] = True
            Path(resources["target_state_path"]).parent.mkdir(
                parents=True, exist_ok=True
            )
            save_lease(self.state_root, lease)

        env = _lease_env(lease, state_root=self.state_root)
        lease["env"] = env
        save_lease(self.state_root, lease)

        return {
            "ok": True,
            "lease": _public_lease(lease),
            "env": env,
            "spawn": {
                "cwd": str(self.root),
                "env": env,
                "lease_id": lease["lease_id"],
                "worker_id": worker_id,
            },
        }

    def release(
        self,
        *,
        lease_id: str | None = None,
        worker_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if lease_id:
            lease = require_lease(self.state_root, lease_id)
        elif worker_id:
            lease = find_active_lease_for_worker(self.state_root, worker_id)
            if not lease:
                raise LeaseNotFound(worker_id=worker_id)
        else:
            raise InvalidRequest("release requires --lease or --worker")

        wid = lease["worker_id"]
        with worker_mutex(self.state_root, wid):
            lease = require_lease(self.state_root, lease["lease_id"])
            if lease.get("status") in ("released", "reaped"):
                return {
                    "ok": True,
                    "idempotent": True,
                    "lease": _public_lease(lease),
                }

            adapter = get_adapter(lease.get("adapter") or lease.get("kind") or "scratch")
            try:
                release_result = adapter.release(lease, force=force)
            except Exception as e:
                if not force:
                    raise
                release_result = {"status": "error", "error": str(e), "force": True}

            release_status = str((release_result or {}).get("status") or "")
            incomplete = (
                bool((release_result or {}).get("cleanup_incomplete"))
                or release_status in ("partial", "error")
            )

            lease = touch_lease(lease)
            lease["release_result"] = release_result
            if incomplete:
                lease["status"] = "expiring"
                meta = dict(lease.get("meta") or {})
                meta["release_incomplete"] = True
                if (release_result or {}).get("cleanup_error"):
                    meta["release_cleanup_error"] = release_result["cleanup_error"]
                lease["meta"] = meta
            else:
                lease["status"] = "released"
                lease["released_at"] = lease["updated_at"]
                meta = dict(lease.get("meta") or {})
                meta.pop("release_incomplete", None)
                meta.pop("release_cleanup_error", None)
                lease["meta"] = meta
            save_lease(self.state_root, lease)

        return {
            "ok": not incomplete,
            "lease": _public_lease(lease),
            "release": release_result,
            "retryable": incomplete,
        }

    def mark_expiring(
        self,
        *,
        lease_id: str,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        lease = require_lease(self.state_root, lease_id)
        if lease.get("status") in ("released", "reaped"):
            return {"ok": True, "idempotent": True, "lease": _public_lease(lease)}
        ttl = float(ttl_seconds if ttl_seconds is not None else min(
            float(lease.get("ttl_seconds") or DEFAULT_ONE_SHOT_TTL),
            DEFAULT_ONE_SHOT_TTL,
        ))
        with worker_mutex(self.state_root, lease["worker_id"]):
            lease = require_lease(self.state_root, lease_id)
            lease = touch_lease(lease, ttl_seconds=ttl)
            lease["status"] = "expiring"
            lease["meta"] = dict(lease.get("meta") or {})
            lease["meta"]["exited"] = True
            save_lease(self.state_root, lease)
        return {"ok": True, "lease": _public_lease(lease)}

    def reap(
        self,
        *,
        force_lease_id: str | None = None,
        now: float | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        now = now if now is not None else time.time()
        reaped: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        filtered: list[dict[str, Any]] = []
        if force_lease_id:
            lease = load_lease(self.state_root, force_lease_id)
            if not lease:
                raise LeaseNotFound(lease_id=force_lease_id)
            if lease.get("status") in ("released", "reaped"):
                return {
                    "ok": True,
                    "reaped": [],
                    "skipped": [
                        {"lease_id": force_lease_id, "reason": "terminal"}
                    ],
                    "count": 0,
                    "dry_run": dry_run,
                    "idempotent": True,
                }
            filtered = [lease]
        else:
            for lease in iter_leases(self.state_root):
                if lease.get("status") in ("released", "reaped"):
                    continue
                if not is_expired(lease, now=now):
                    continue
                if not is_auto_reap_eligible(lease):
                    skipped.append(
                        {
                            "lease_id": lease.get("lease_id"),
                            "worker_id": lease.get("worker_id"),
                            "reason": "not_auto_reap_eligible",
                            "mode": lease.get("mode"),
                            "status": lease.get("status"),
                            "auto_reap": bool(lease.get("auto_reap")),
                        }
                    )
                    continue
                filtered.append(lease)

        for lease in filtered:
            if dry_run:
                reaped.append(
                    {
                        "lease_id": lease["lease_id"],
                        "worker_id": lease.get("worker_id"),
                        "dry_run": True,
                    }
                )
                continue
            try:
                lid = lease["lease_id"]
                result = self.release(lease_id=lid, force=True)
                if result.get("retryable") or not result.get("ok", True):
                    skipped.append(
                        {
                            "lease_id": lid,
                            "reason": "release_incomplete",
                            "release": result.get("release"),
                        }
                    )
                    continue
                with worker_mutex(self.state_root, lease["worker_id"]):
                    cur = load_lease(self.state_root, lid)
                    if cur and cur.get("status") == "released":
                        cur["status"] = "reaped"
                        cur["reaped_at"] = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
                        )
                        save_lease(self.state_root, cur)
                        reaped.append(_public_lease(cur))
                    elif cur:
                        skipped.append(
                            {
                                "lease_id": lid,
                                "reason": "not_released",
                                "status": cur.get("status"),
                            }
                        )
                    else:
                        reaped.append(result.get("lease") or {"lease_id": lid})
            except Exception as e:
                skipped.append(
                    {
                        "lease_id": lease.get("lease_id"),
                        "error": str(e),
                    }
                )

        return {
            "ok": True,
            "reaped": reaped,
            "skipped": skipped,
            "count": len(reaped),
            "dry_run": dry_run,
        }

    def launch(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.acquire(request)
