"""
Lease manager: acquire → env → release/reap.

One browser/process lease per worker. Many target leases may share that
browser. At most one mutating owner per target.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from browserctl.adapters import get_adapter
from browserctl.allocate import target_from_request_or_create
from browserctl.errors import InvalidRequest, LeaseConflict, LeaseNotFound, TargetConflict
from browserctl.paths import (
    active_target_path,
    resolve_root,
    resolve_state_root,
    target_registry_path,
)
from browserctl.store import (
    base_lease,
    find_active_lease_for_worker,
    find_target_lease,
    is_auto_reap_eligible,
    is_expired,
    iter_active_target_leases,
    iter_leases,
    lease_scope,
    load_lease,
    require_lease,
    save_lease,
    touch_lease,
    worker_mutex,
)
from browserctl.target_registry import (
    load_registry,
    release_target as registry_release_target,
    upsert_target,
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
    if lease.get("target_id"):
        env["BROWSERCTL_TARGET_ID"] = str(lease["target_id"])
        env["BROWSERCTL_TARGET_LEASE_ID"] = str(lease["lease_id"])
    if lease.get("browser_lease_id"):
        env["BROWSERCTL_BROWSER_LEASE_ID"] = str(lease["browser_lease_id"])
    elif lease_scope(lease) == "browser":
        env["BROWSERCTL_BROWSER_LEASE_ID"] = str(lease["lease_id"])
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
    resources.setdefault(
        "target_registry_path",
        str(target_registry_path(state_root, worker_id)),
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

        wid = str(lease.get("worker_id") or "")
        registry = load_registry(self.state_root, wid) if wid else None

        out = _public_lease(lease)
        out["expired"] = is_expired(lease, now=now)
        out["live"] = live
        out["active_target"] = target
        out["target_registry"] = registry
        out["env"] = _lease_env(lease, state_root=self.state_root)
        return out

    def _claim_or_allocate_target(
        self,
        *,
        worker_id: str,
        req: dict[str, Any],
        cdp_url: str | None,
        owner: str,
    ) -> str:
        requested = str(req.get("target_id") or "").strip()
        if requested:
            held = find_target_lease(self.state_root, worker_id, requested)
            if held:
                raise TargetConflict(
                    worker_id, requested, holder=_public_lease(held)
                )
            return requested
        return target_from_request_or_create(req, cdp_url=cdp_url)

    def _mint_target_lease(
        self,
        *,
        browser: dict[str, Any],
        owner: str,
        ttl: float,
        mode: str,
        profile_name: str | None,
        auto_reap: bool,
        req: dict[str, Any],
    ) -> dict[str, Any]:
        worker_id = browser["worker_id"]
        resources = dict(browser.get("resources") or {})
        cdp_url = resources.get("cdp_url")
        target_id = self._claim_or_allocate_target(
            worker_id=worker_id,
            req=req,
            cdp_url=str(cdp_url) if cdp_url else None,
            owner=owner,
        )
        held = find_target_lease(self.state_root, worker_id, target_id)
        if held:
            raise TargetConflict(worker_id, target_id, holder=_public_lease(held))

        target = base_lease(
            worker_id=worker_id,
            kind=browser.get("kind") or "scratch",
            owner=owner,
            ttl_seconds=ttl,
            mode=mode,
            adapter=browser.get("adapter") or browser.get("kind") or "scratch",
            resources=resources,
            env=dict(browser.get("env") or {}),
            meta={"joined": True},
            scope="target",
            target_id=target_id,
            browser_lease_id=browser["lease_id"],
        )
        if profile_name:
            target["profile_name"] = profile_name
        if auto_reap:
            target["auto_reap"] = True
        save_lease(self.state_root, target)
        upsert_target(
            self.state_root,
            worker_id,
            target_id=target_id,
            owner=owner,
            lease_id=target["lease_id"],
        )
        env = _lease_env(target, state_root=self.state_root)
        target["env"] = env
        save_lease(self.state_root, target)
        return target

    def _acquire_result(self, target: dict[str, Any], browser: dict[str, Any]) -> dict[str, Any]:
        env = _lease_env(target, state_root=self.state_root)
        return {
            "ok": True,
            "lease": _public_lease(target),
            "browser_lease": _public_lease(browser),
            "env": env,
            "spawn": {
                "cwd": str(self.root),
                "env": env,
                "lease_id": target["lease_id"],
                "worker_id": target["worker_id"],
                "target_id": target.get("target_id"),
                "browser_lease_id": browser["lease_id"],
            },
        }

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
        auto_reap = req.get("auto_reap") is True
        exclusive = bool(req.get("browser_exclusive"))

        adapter = get_adapter(kind)
        req["root"] = str(self.root)
        req["state_root"] = str(self.state_root)

        known_worker = (req.get("worker_id") or req.get("worker") or "").strip()

        def _join_or_conflict(browser: dict[str, Any]) -> dict[str, Any]:
            if exclusive:
                raise LeaseConflict(browser["worker_id"], holder=_public_lease(browser))
            target = self._mint_target_lease(
                browser=browser,
                owner=owner,
                ttl=ttl,
                mode=mode,
                profile_name=profile_name,
                auto_reap=auto_reap,
                req=req,
            )
            return self._acquire_result(target, browser)

        if known_worker and known_worker != "default":
            with worker_mutex(self.state_root, known_worker):
                existing = find_active_lease_for_worker(self.state_root, known_worker)
                if existing:
                    return _join_or_conflict(existing)
                return self._start_browser(
                    req=req,
                    adapter=adapter,
                    kind=kind,
                    owner=owner,
                    ttl=ttl,
                    mode=mode,
                    profile_name=profile_name,
                    auto_reap=auto_reap,
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
                if exclusive:
                    raise LeaseConflict(worker_id, holder=_public_lease(existing))
                return _join_or_conflict(existing)
            return self._record_started_browser(
                acquired=acquired,
                kind=kind,
                owner=owner,
                ttl=ttl,
                mode=mode,
                profile_name=profile_name,
                auto_reap=auto_reap,
                req=req,
            )

    def _start_browser(
        self,
        *,
        req: dict[str, Any],
        adapter: Any,
        kind: str,
        owner: str,
        ttl: float,
        mode: str,
        profile_name: str | None,
        auto_reap: bool,
    ) -> dict[str, Any]:
        acquired = adapter.acquire(req)
        worker_id = acquired["worker_id"]
        if not worker_id or worker_id == "default":
            raise InvalidRequest(
                "adapter returned unmanaged/default worker_id",
                worker_id=worker_id,
            )
        existing = find_active_lease_for_worker(self.state_root, worker_id)
        if existing:
            raise LeaseConflict(worker_id, holder=_public_lease(existing))
        return self._record_started_browser(
            acquired=acquired,
            kind=kind,
            owner=owner,
            ttl=ttl,
            mode=mode,
            profile_name=profile_name,
            auto_reap=auto_reap,
            req=req,
        )

    def _record_started_browser(
        self,
        *,
        acquired: dict[str, Any],
        kind: str,
        owner: str,
        ttl: float,
        mode: str,
        profile_name: str | None,
        auto_reap: bool,
        req: dict[str, Any],
    ) -> dict[str, Any]:
        worker_id = acquired["worker_id"]
        meta = dict(acquired.get("meta") or {})
        resources = _enrich_resources(acquired, state_root=self.state_root)
        browser = base_lease(
            worker_id=worker_id,
            kind=acquired.get("kind") or kind,
            owner=owner,
            ttl_seconds=ttl,
            mode=mode,
            adapter=acquired.get("adapter") or kind,
            resources=resources,
            env=dict(acquired.get("env") or {}),
            meta=meta,
            scope="browser",
        )
        if profile_name:
            browser["profile_name"] = profile_name
        if auto_reap:
            browser["auto_reap"] = True
        Path(resources["target_state_path"]).parent.mkdir(parents=True, exist_ok=True)
        save_lease(self.state_root, browser)

        target = self._mint_target_lease(
            browser=browser,
            owner=owner,
            ttl=ttl,
            mode=mode,
            profile_name=profile_name,
            auto_reap=auto_reap,
            req=req,
        )
        return self._acquire_result(target, browser)

    def _stop_browser(self, browser: dict[str, Any], *, force: bool) -> dict[str, Any]:
        adapter = get_adapter(browser.get("adapter") or browser.get("kind") or "scratch")
        try:
            release_result = adapter.release(browser, force=force)
        except Exception as e:
            if not force:
                raise
            release_result = {"status": "error", "error": str(e), "force": True}

        release_status = str((release_result or {}).get("status") or "")
        incomplete = (
            bool((release_result or {}).get("cleanup_incomplete"))
            or release_status in ("partial", "error")
        )
        browser = touch_lease(browser)
        browser["release_result"] = release_result
        if incomplete:
            browser["status"] = "expiring"
            meta = dict(browser.get("meta") or {})
            meta["release_incomplete"] = True
            if (release_result or {}).get("cleanup_error"):
                meta["release_cleanup_error"] = release_result["cleanup_error"]
            browser["meta"] = meta
        else:
            browser["status"] = "released"
            browser["released_at"] = browser["updated_at"]
            meta = dict(browser.get("meta") or {})
            meta.pop("release_incomplete", None)
            meta.pop("release_cleanup_error", None)
            browser["meta"] = meta
        save_lease(self.state_root, browser)
        return {
            "ok": not incomplete,
            "lease": _public_lease(browser),
            "release": release_result,
            "retryable": incomplete,
        }

    def _release_target(
        self,
        lease: dict[str, Any],
        *,
        force: bool,
        stop_browser_if_last: bool,
    ) -> dict[str, Any]:
        wid = lease["worker_id"]
        tid = str(lease.get("target_id") or "")
        others = [
            t
            for t in iter_active_target_leases(self.state_root, wid)
            if t.get("lease_id") != lease.get("lease_id")
        ]
        browser = find_active_lease_for_worker(self.state_root, wid)
        stopped = None
        if stop_browser_if_last and not others and browser:
            stopped = self._stop_browser(browser, force=force)
            if stopped.get("retryable"):
                lease = touch_lease(lease)
                lease["status"] = "expiring"
                lease["release_result"] = stopped.get("release")
                save_lease(self.state_root, lease)
                return {
                    "ok": False,
                    "lease": _public_lease(lease),
                    "release": stopped.get("release"),
                    "retryable": True,
                    "browser_release": stopped,
                }

        lease = touch_lease(lease)
        lease["status"] = "released"
        lease["released_at"] = lease["updated_at"]
        save_lease(self.state_root, lease)
        if tid:
            registry_release_target(self.state_root, wid, tid)

        remaining = iter_active_target_leases(self.state_root, wid)
        return {
            "ok": True if not stopped else stopped.get("ok", True),
            "lease": _public_lease(lease),
            "release": {
                "status": "target_released",
                "target_id": tid,
                "remaining_targets": [t.get("target_id") for t in remaining],
                "browser_stopped": bool(stopped and not stopped.get("retryable")),
            },
            "retryable": bool(stopped and stopped.get("retryable")),
            "browser_release": stopped,
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
            targets = iter_active_target_leases(self.state_root, worker_id)
            lease = find_active_lease_for_worker(self.state_root, worker_id)
            if targets:
                last = None
                for t in targets:
                    last = self.release(lease_id=t["lease_id"], force=force)
                return last or {"ok": True, "lease": None}
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
            if lease_scope(lease) == "target":
                return self._release_target(
                    lease, force=force, stop_browser_if_last=True
                )
            remaining = iter_active_target_leases(self.state_root, wid)
            if remaining and not force:
                raise InvalidRequest(
                    "browser lease still has active target leases",
                    worker_id=wid,
                    targets=[t.get("target_id") for t in remaining],
                )
            for t in remaining:
                self._release_target(t, force=True, stop_browser_if_last=False)
            return self._stop_browser(lease, force=force)

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
                            "scope": lease_scope(lease),
                        }
                    )
                    continue
                filtered.append(lease)

        # targets first so last-target browser stop is coherent
        filtered.sort(key=lambda L: 0 if lease_scope(L) == "target" else 1)

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
                if lease_scope(lease) == "target" and not force_lease_id:
                    # stale target: drop ownership; stop browser only for one_shot last target
                    with worker_mutex(self.state_root, lease["worker_id"]):
                        cur = require_lease(self.state_root, lid)
                        if cur.get("status") in ("released", "reaped"):
                            continue
                        browser = find_active_lease_for_worker(
                            self.state_root, cur["worker_id"]
                        )
                        one_shot = str((browser or cur).get("mode") or "") == "one_shot"
                        stop_last = one_shot or cur.get("status") == "expiring" or (
                            cur.get("auto_reap") is True
                            or (browser or {}).get("auto_reap") is True
                        )
                        result = self._release_target(
                            cur,
                            force=True,
                            stop_browser_if_last=stop_last,
                        )
                else:
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
                    if lease_scope(lease) == "target":
                        br = result.get("browser_release") or {}
                        bid = (br.get("lease") or {}).get("lease_id")
                        if bid:
                            bcur = load_lease(self.state_root, bid)
                            if bcur and bcur.get("status") == "released":
                                bcur["status"] = "reaped"
                                bcur["reaped_at"] = time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
                                )
                                save_lease(self.state_root, bcur)
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
