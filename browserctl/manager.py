"""
Lease / lifecycle manager.

Owns acquire → env publication → release/reap. Adapters start browsers;
this module enforces one mutation lease per worker and TTL.
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
    delete_lease_record,
    find_active_lease_for_worker,
    is_expired,
    iter_leases,
    load_lease,
    require_lease,
    save_lease,
    touch_lease,
    worker_mutex,
)
from browserctl import watch as watch_mod

DEFAULT_TTL = 3600.0  # 1h
DEFAULT_ONE_SHOT_TTL = 900.0  # 15m

# Exclusive with profile_name (headless included so API cannot override headed).
_SELECTOR_KEYS = frozenset(
    "kind adapter email worker worker_id label cdp_port country city "
    "headed headless no_start attach_only".split()
)


def _apply_headed_headless(req: dict[str, Any]) -> None:
    """Derive headless from headed; reject contradictions on every path."""
    if req.get("headed") is None:
        return
    headed = bool(req["headed"])
    if req.get("headless") is not None and bool(req["headless"]) != (not headed):
        raise InvalidRequest(
            "headed/headless contradict", headed=headed, headless=bool(req["headless"])
        )
    req["headless"] = not headed


def _public_lease(lease: dict[str, Any]) -> dict[str, Any]:
    """Return lease dict safe for agents (no secrets by construction)."""
    out = dict(lease)
    # never echo raw adapter dumps that might grow secrets
    resources = dict(out.get("resources") or {})
    resources.pop("egress_info", None)
    out["resources"] = resources
    return out


def _navigator_env(lease: dict[str, Any], *, state_root: Path) -> dict[str, str]:
    env = dict(lease.get("env") or {})
    resources = lease.get("resources") or {}
    worker_id = lease.get("worker_id")
    # Always publish target-state path for mirror coordination
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

    # ── queries ──────────────────────────────────────────────────────────

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
                # fall back to any lease for worker
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
        out["env"] = _navigator_env(lease, state_root=self.state_root)
        return out

    # ── acquire / release ────────────────────────────────────────────────

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

        _apply_headed_headless(req)

        kind = (req.get("kind") or req.get("adapter") or "").strip().lower()
        if not kind:
            raise InvalidRequest(
                "acquire requires --kind or --profile (xai|scratch|vpn)",
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

        # Fast path: if caller already knows worker_id, serialize under that
        # mutex and refuse before starting anything new.
        known_worker = (req.get("worker_id") or req.get("worker") or "").strip()
        if known_worker and known_worker != "default":
            with worker_mutex(self.state_root, known_worker):
                existing = find_active_lease_for_worker(self.state_root, known_worker)
                if existing:
                    raise LeaseConflict(
                        known_worker, holder=_public_lease(existing)
                    )

        # Adapters may start browsers. For xAI, acquire is always
        # attached_existing (shared identity_ops lifecycle) so conflict must
        # NEVER stop the winner browser.
        acquired = adapter.acquire(req)
        worker_id = acquired["worker_id"]
        if not worker_id or worker_id == "default":
            raise InvalidRequest(
                "adapter returned unmanaged/default worker_id",
                worker_id=worker_id,
            )

        meta = dict(acquired.get("meta") or {})
        kind_name = (acquired.get("kind") or kind or "").lower()
        adapter_name = (acquired.get("adapter") or kind_name).lower()
        # xAI identity_ops is shared; never force-stop on conflict.
        if adapter_name == "xai" or kind_name == "xai":
            meta["attached_existing"] = True
        attached = bool(meta.get("attached_existing"))

        with worker_mutex(self.state_root, worker_id):
            existing = find_active_lease_for_worker(self.state_root, worker_id)
            if existing:
                # NEVER stop winner / shared browsers. Only roll back resources
                # we uniquely started (scratch cold start without attach flag).
                if not attached and adapter_name not in ("xai",):
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
            Path(resources["target_state_path"]).parent.mkdir(
                parents=True, exist_ok=True
            )
            save_lease(self.state_root, lease)

        env = _navigator_env(lease, state_root=self.state_root)
        lease["env"] = env
        save_lease(self.state_root, lease)

        return {
            "ok": True,
            "lease": _public_lease(lease),
            "env": env,
            "spawn": {
                "profile": "navigator",
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
        keep_watch: bool = False,
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
            # reload under lock
            lease = require_lease(self.state_root, lease["lease_id"])
            if lease.get("status") in ("released", "reaped"):
                return {
                    "ok": True,
                    "idempotent": True,
                    "lease": _public_lease(lease),
                }

            watch_result = None
            if lease.get("watch") and not keep_watch:
                try:
                    watch_result = watch_mod.stop_watch(lease, close=True)
                except Exception as e:
                    watch_result = {"error": str(e)}
                lease["watch"] = None

            adapter = get_adapter(lease.get("adapter") or lease.get("kind") or "scratch")
            try:
                release_result = adapter.release(lease, force=force)
            except Exception as e:
                if not force:
                    raise
                release_result = {"status": "error", "error": str(e), "force": True}

            lease = touch_lease(lease)
            lease["status"] = "released"
            lease["released_at"] = lease["updated_at"]
            lease["release_result"] = release_result
            if watch_result is not None:
                lease["last_watch_stop"] = watch_result
            save_lease(self.state_root, lease)

        return {
            "ok": True,
            "lease": _public_lease(lease),
            "release": release_result,
            "watch_stop": watch_result,
        }

    # ── watch ────────────────────────────────────────────────────────────

    def watch(
        self,
        *,
        lease_id: str | None = None,
        worker_id: str | None = None,
        agent_pane: str | None = None,
        ratio: float | None = None,
        direction: str = "right",
        herdr_session: str | None = None,
        herdr_socket: str | None = None,
        ready_timeout_s: float | None = None,
    ) -> dict[str, Any]:
        if lease_id:
            lease = require_lease(self.state_root, lease_id)
        elif worker_id:
            lease = find_active_lease_for_worker(self.state_root, worker_id)
            if not lease:
                raise LeaseNotFound(worker_id=worker_id)
        else:
            raise InvalidRequest("watch requires --lease or --worker")

        if lease.get("status") not in ("active", "acquired", "expiring"):
            raise InvalidRequest(
                f"cannot watch lease in status {lease.get('status')!r}"
            )

        # idempotent: already watching
        existing = lease.get("watch") or {}
        if existing.get("watch_pane_id"):
            return {
                "ok": True,
                "idempotent": True,
                "watch": existing,
                "lease_id": lease["lease_id"],
            }

        ready_kwargs: dict[str, Any] = {}
        if ready_timeout_s is not None:
            ready_kwargs["ready_timeout_s"] = float(ready_timeout_s)

        watch_rec = watch_mod.start_watch(
            lease,
            state_root=self.state_root,
            agent_pane=agent_pane,
            ratio=watch_mod.normalize_ratio(ratio),
            direction=direction,
            herdr_session=herdr_session,
            herdr_socket=herdr_socket,
            **ready_kwargs,
        )
        lease = touch_lease(lease)
        lease["watch"] = watch_rec
        resources = dict(lease.get("resources") or {})
        resources["target_state_path"] = watch_rec.get(
            "target_state_path", resources.get("target_state_path")
        )
        lease["resources"] = resources
        save_lease(self.state_root, lease)
        return {
            "ok": True,
            "watch": watch_rec,
            "lease_id": lease["lease_id"],
            "worker_id": lease["worker_id"],
            "mirror_env": watch_rec.get("env"),
        }

    def unwatch(
        self,
        *,
        lease_id: str | None = None,
        worker_id: str | None = None,
        close: bool = True,
    ) -> dict[str, Any]:
        if lease_id:
            lease = require_lease(self.state_root, lease_id)
        elif worker_id:
            lease = find_active_lease_for_worker(self.state_root, worker_id)
            if not lease:
                raise LeaseNotFound(worker_id=worker_id)
        else:
            raise InvalidRequest("unwatch requires --lease or --worker")

        if not lease.get("watch"):
            return {
                "ok": True,
                "idempotent": True,
                "lease_id": lease["lease_id"],
                "watch": None,
            }

        result = watch_mod.stop_watch(lease, close=close)
        lease = touch_lease(lease)
        lease["watch"] = None
        lease["last_watch_stop"] = result
        save_lease(self.state_root, lease)
        return {"ok": True, "lease_id": lease["lease_id"], "unwatch": result}

    # ── reap / mark ──────────────────────────────────────────────────────

    def mark_expiring(
        self,
        *,
        lease_id: str,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Navigator exit without release: keep resources, shorten/mark TTL."""
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
            lease["meta"]["navigator_exited"] = True
            save_lease(self.state_root, lease)
        return {"ok": True, "lease": _public_lease(lease)}

    def reap(
        self,
        *,
        force_lease_id: str | None = None,
        now: float | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Reap expired leases (and optionally one forced id). Idempotent."""
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
                if is_expired(lease, now=now):
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
                result = self.release(
                    lease_id=lease["lease_id"],
                    force=True,
                )
                # mark reaped rather than merely released when via reap
                lid = lease["lease_id"]
                with worker_mutex(self.state_root, lease["worker_id"]):
                    cur = load_lease(self.state_root, lid)
                    if cur:
                        cur["status"] = "reaped"
                        cur["reaped_at"] = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
                        )
                        save_lease(self.state_root, cur)
                        reaped.append(_public_lease(cur))
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
        """
        Convenient acquire (+ optional watch) returning navigator spawn env.

        Alias surface for orchestrators: browserctl launch|spawn ...
        """
        result = self.acquire(request)
        if request.get("watch"):
            try:
                ready_timeout = request.get("ready_timeout")
                if ready_timeout is None:
                    ready_timeout = request.get("ready_timeout_s")
                w = self.watch(
                    lease_id=result["lease"]["lease_id"],
                    agent_pane=request.get("agent_pane"),
                    ratio=request.get("ratio"),
                    herdr_session=request.get("herdr_session"),
                    herdr_socket=request.get("herdr_socket"),
                    ready_timeout_s=(
                        float(ready_timeout) if ready_timeout is not None else None
                    ),
                )
                result["watch"] = w.get("watch")
                result["mirror_env"] = w.get("mirror_env")
            except Exception as e:
                result["watch_error"] = str(e)
        return result
