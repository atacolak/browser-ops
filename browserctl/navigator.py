"""
Orchestrator-only navigator lifecycle seam.

Public surface (resource-oriented):
  browserctl navigator spawn   — lease + env bridge + herdr-agent-ctl spawn
                                 + optional watch; one structured receipt
  browserctl navigator cleanup — close navigator + prove watch closed + release
  browserctl navigator status  — tiny binding lookup (optional)

Navigators are only used through orchestrators. This is not a generic agent
framework — it only bridges browserctl leases to the existing herdr-agent-ctl
substrate with exact env and a minimal lease↔navigator binding.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from browserctl.errors import AdapterError, InvalidRequest, LeaseNotFound
from browserctl.paths import (
    control_root,
    resolve_root,
    resolve_state_root,
)
from browserctl.store import (
    find_active_lease_for_worker,
    iter_leases,
    load_lease,
    require_lease,
    save_lease,
    touch_lease,
    worker_mutex,
)
from browserctl import watch as watch_mod

NAVIGATOR_PROFILE = "navigator"
DEFAULT_AGENT_CTL_CANDIDATES = (
    # Prefer PATH, then known workspace profile locations (orchestrator → scout).
    "herdr-agent-ctl",
)
_NAME_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def navigator_bindings_dir(state_root: Path | str) -> Path:
    return control_root(state_root) / "navigator-bindings"


def navigator_binding_path(state_root: Path | str, lease_id: str) -> Path:
    return navigator_bindings_dir(state_root) / f"{lease_id}.json"


def resolve_agent_ctl_bin(explicit: str | None = None) -> str:
    """Resolve herdr-agent-ctl binary. Prefer explicit → env → PATH → profile paths."""
    if explicit and str(explicit).strip():
        path = Path(str(explicit).strip()).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        raise InvalidRequest(
            f"herdr-agent-ctl not executable at {path}",
            path=str(path),
        )

    env = os.environ.get("BROWSERCTL_HERDR_AGENT_CTL", "").strip()
    if env:
        path = Path(env).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        raise InvalidRequest(
            "BROWSERCTL_HERDR_AGENT_CTL is set but not an executable file",
            path=env,
        )

    which = shutil.which("herdr-agent-ctl")
    if which:
        return which

    raise AdapterError(
        "herdr-agent-ctl not found; set BROWSERCTL_HERDR_AGENT_CTL or install it on PATH",
    )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _slug_name(raw: str | None, *, lease_id: str) -> str:
    if raw and str(raw).strip():
        base = str(raw).strip()
    else:
        base = f"nav-{lease_id[:8]}"
    cleaned = _NAME_SAFE.sub("-", base).strip("-._")
    if not cleaned:
        cleaned = f"nav-{uuid.uuid4().hex[:8]}"
    return cleaned[:48]


def _env_entries(env: dict[str, str]) -> list[str]:
    """Stable KEY=VALUE list for herdr-agent-ctl --env (exact lease env)."""
    out: list[str] = []
    for key in sorted(env.keys()):
        val = env[key]
        if val is None:
            continue
        out.append(f"{key}={val}")
    return out


def _parse_json_stdout(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    # herdr-agent-ctl may print a single JSON object; tolerate trailing noise.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Last non-empty line that looks like JSON object.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"raw": text[-2000:]}


def load_binding(state_root: Path | str, lease_id: str) -> dict[str, Any] | None:
    path = navigator_binding_path(state_root, lease_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def save_binding(state_root: Path | str, binding: dict[str, Any]) -> Path:
    lid = binding.get("lease_id")
    if not lid:
        raise InvalidRequest("navigator binding missing lease_id")
    path = navigator_binding_path(state_root, str(lid))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(binding, indent=2, sort_keys=True, default=str) + "\n"
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)
    return path


def delete_binding(state_root: Path | str, lease_id: str) -> bool:
    path = navigator_binding_path(state_root, lease_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def find_binding_by_name(
    state_root: Path | str, name: str
) -> dict[str, Any] | None:
    """Exact name match across binding files. Fail closed on ambiguity."""
    want = str(name).strip()
    if not want:
        return None
    d = navigator_bindings_dir(state_root)
    if not d.is_dir():
        return None
    matches: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        if str(raw.get("name") or "") == want:
            matches.append(raw)
        elif str(raw.get("pane_id") or "") == want:
            matches.append(raw)
    if not matches:
        # Fall back: lease.meta.navigator name on active leases.
        for lease in iter_leases(state_root):
            nav = (lease.get("meta") or {}).get("navigator") or {}
            if not isinstance(nav, dict):
                continue
            if str(nav.get("name") or "") == want or str(nav.get("pane_id") or "") == want:
                matches.append(
                    {
                        "lease_id": lease.get("lease_id"),
                        "worker_id": lease.get("worker_id"),
                        **nav,
                        "source": "lease_meta",
                    }
                )
    if len(matches) > 1:
        raise InvalidRequest(
            f"navigator name {want!r} is ambiguous",
            name=want,
            matches=[
                {
                    "lease_id": m.get("lease_id"),
                    "pane_id": m.get("pane_id"),
                    "name": m.get("name"),
                }
                for m in matches
            ],
        )
    return matches[0] if matches else None


def _binding_from_spawn_payload(
    *,
    lease: dict[str, Any],
    spawn_result: dict[str, Any],
    agent_ctl: str,
    cwd: str,
    profile: str,
    requested_name: str,
    herdr_socket: str | None,
    herdr_session: str | None,
) -> dict[str, Any]:
    receipt = spawn_result.get("receipt") if isinstance(spawn_result.get("receipt"), dict) else {}
    pane_id = (
        spawn_result.get("pane_id")
        or receipt.get("pane_id")
    )
    name = (
        spawn_result.get("name")
        or receipt.get("name")
        or requested_name
    )
    workspace_id = spawn_result.get("workspace_id") or receipt.get("workspace_id")
    tab_id = spawn_result.get("tab_id") or receipt.get("tab_id")
    return {
        "version": 1,
        "lease_id": lease["lease_id"],
        "worker_id": lease.get("worker_id"),
        "name": name,
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "profile": profile,
        "cwd": cwd,
        "agent_ctl": agent_ctl,
        "herdr_socket": herdr_socket
        or os.environ.get("HERDR_SOCKET_PATH")
        or receipt.get("herdr_socket"),
        "herdr_session": herdr_session
        or os.environ.get("HERDR_SESSION")
        or receipt.get("herdr_session"),
        "session_value": spawn_result.get("session_value") or receipt.get("session_value"),
        "session_kind": spawn_result.get("session_kind") or receipt.get("session_kind"),
        "terminal_id": spawn_result.get("terminal_id") or receipt.get("terminal_id"),
        "orchestrator_id": spawn_result.get("orchestrator_id")
        or receipt.get("orchestrator_id"),
        "lifecycle": spawn_result.get("lifecycle"),
        "receipt": receipt or None,
        "spawn_status": spawn_result.get("status"),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "status": "active",
    }


def invoke_herdr_agent_ctl_spawn(
    *,
    agent_ctl: str,
    name: str,
    cwd: str,
    env: dict[str, str],
    profile: str = NAVIGATOR_PROFILE,
    model: str | None = None,
    thinking: str | None = None,
    split: str = "right",
    lifecycle: str = "persistent",
    workspace: str | None = None,
    tab: str | None = None,
    timeout_ms: int = 30000,
    herdr_socket: str | None = None,
    herdr_session: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """
    Invoke herdr-agent-ctl spawn with exact lease env.

    Requires a live herdr ambient (HERDR_SOCKET_PATH + HERDR_PANE_ID) — same
    precondition as herdr-agent-ctl itself. Pass socket/session via env.
    """
    cmd: list[str] = [
        agent_ctl,
        "spawn",
        "--profile",
        profile,
        "--name",
        name,
        "--cwd",
        cwd,
        "--split",
        split,
        "--lifecycle",
        lifecycle,
        "--timeout",
        str(int(timeout_ms)),
        "--json",
    ]
    if model:
        cmd.extend(["--model", model])
    if thinking:
        cmd.extend(["--thinking", thinking])
    if workspace:
        cmd.extend(["--workspace", workspace])
    if tab:
        cmd.extend(["--tab", tab])
    for entry in _env_entries(env):
        cmd.extend(["--env", entry])

    run_env = os.environ.copy()
    if herdr_socket:
        run_env["HERDR_SOCKET_PATH"] = herdr_socket
    if herdr_session:
        run_env["HERDR_SESSION"] = herdr_session
    if extra_env:
        run_env.update(extra_env)

    # herdr-agent-ctl refuses to start without ambient herdr pane/socket.
    if not run_env.get("HERDR_SOCKET_PATH", "").strip():
        raise InvalidRequest(
            "navigator spawn requires HERDR_SOCKET_PATH (or --herdr-socket) "
            "so herdr-agent-ctl targets the orchestrator herdr server"
        )
    if not run_env.get("HERDR_PANE_ID", "").strip():
        raise InvalidRequest(
            "navigator spawn requires HERDR_PANE_ID in the environment "
            "(orchestrator pane) — herdr-agent-ctl is orchestrator-only"
        )

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=run_env,
        )
    except FileNotFoundError as e:
        raise AdapterError(
            "herdr-agent-ctl binary not found at invoke time",
            path=agent_ctl,
            err=str(e),
        ) from e
    except subprocess.TimeoutExpired as e:
        raise AdapterError(
            "herdr-agent-ctl spawn timed out",
            timeout_s=timeout_s,
            cmd=cmd[:12],
        ) from e

    payload = _parse_json_stdout(proc.stdout)
    if proc.returncode != 0:
        # Prefer structured failure from agent-ctl.
        reason = payload.get("reason") or payload.get("status") or "spawn_failed"
        raise AdapterError(
            f"herdr-agent-ctl spawn failed ({reason})",
            returncode=proc.returncode,
            reason=reason,
            payload=payload,
            stderr=(proc.stderr or "")[-2000:],
            stdout=(proc.stdout or "")[-2000:],
            cmd=cmd[:16],
        )
    status = payload.get("status")
    if status and status not in ("success", "ok"):
        raise AdapterError(
            f"herdr-agent-ctl spawn returned non-success status {status!r}",
            payload=payload,
            returncode=proc.returncode,
        )
    if not (payload.get("pane_id") or (payload.get("receipt") or {}).get("pane_id")):
        raise AdapterError(
            "herdr-agent-ctl spawn succeeded but returned no pane_id",
            payload=payload,
        )
    return payload


def invoke_herdr_agent_ctl_close(
    *,
    agent_ctl: str,
    target: str,
    herdr_socket: str | None = None,
    herdr_session: str | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Close navigator via herdr-agent-ctl close --target (orchestrator substrate)."""
    cmd = [agent_ctl, "close", "--target", target, "--json"]
    run_env = os.environ.copy()
    if herdr_socket:
        run_env["HERDR_SOCKET_PATH"] = herdr_socket
    if herdr_session:
        run_env["HERDR_SESSION"] = herdr_session

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=run_env,
        )
    except FileNotFoundError as e:
        raise AdapterError(
            "herdr-agent-ctl binary not found at close time",
            path=agent_ctl,
            err=str(e),
        ) from e
    except subprocess.TimeoutExpired as e:
        raise AdapterError(
            "herdr-agent-ctl close timed out",
            timeout_s=timeout_s,
            target=target,
        ) from e

    payload = _parse_json_stdout(proc.stdout)
    # close reports success|gone — both are acceptable settlement signals.
    status = str(payload.get("status") or "")
    if proc.returncode != 0 and status not in ("success", "gone", "skipped"):
        return {
            "ok": False,
            "status": status or "error",
            "target": target,
            "returncode": proc.returncode,
            "payload": payload,
            "stderr": (proc.stderr or "")[-1500:],
            "stdout": (proc.stdout or "")[-1500:],
        }
    return {
        "ok": status in ("success", "gone", "") or proc.returncode == 0,
        "status": status or ("success" if proc.returncode == 0 else "error"),
        "target": target,
        "pane_id": payload.get("pane_id"),
        "payload": payload,
        "returncode": proc.returncode,
    }


def verify_navigator_pane_settled(
    *,
    pane_id: str | None,
    endpoint: dict[str, str] | None,
) -> dict[str, Any]:
    """Evidence that navigator pane is gone (or never existed)."""
    if not pane_id:
        return {
            "settled": True,
            "evidence": "no_pane_id",
            "pane_id": None,
        }
    absence = watch_mod.verify_pane_absent(str(pane_id), endpoint=endpoint or {})
    return {
        "settled": bool(absence.get("absent")),
        "evidence": absence.get("evidence"),
        "pane_id": str(pane_id),
        "error": absence.get("error"),
        "verify": absence,
    }


class NavigatorLifecycle:
    """Manager-facing navigator spawn/cleanup/status."""

    def __init__(self, manager: Any):
        self.manager = manager
        self.root = manager.root
        self.state_root = manager.state_root

    # ── spawn ────────────────────────────────────────────────────────────

    def spawn(self, request: dict[str, Any]) -> dict[str, Any]:
        """
        Acquire lease, spawn navigator with exact env, optional watch, bind.

        Returns one structured receipt with next-step fields for the
        orchestrator (run target = pane_id / name).
        """
        req = dict(request or {})
        watch = bool(req.pop("watch", False))
        name_req = req.pop("name", None)
        model = req.pop("model", None)
        thinking = req.pop("thinking", None)
        split = str(req.pop("split", "right") or "right")
        nav_lifecycle = str(req.pop("navigator_lifecycle", "persistent") or "persistent")
        workspace = req.pop("workspace", None)
        tab = req.pop("tab", None)
        agent_ctl_path = req.pop("agent_ctl", None)
        herdr_socket = req.pop("herdr_socket", None) or os.environ.get(
            "HERDR_SOCKET_PATH"
        )
        herdr_session = req.pop("herdr_session", None) or os.environ.get("HERDR_SESSION")
        ready_timeout = req.pop("ready_timeout", None)
        ratio = req.pop("ratio", None)
        agent_pane_override = req.pop("agent_pane", None)

        # 1) Lease acquire (browser resources).
        launch_req = dict(req)
        # Never let nested launch open watch before navigator pane exists.
        launch_req.pop("watch", None)
        launch_req.pop("agent_pane", None)
        acquired = self.manager.acquire(launch_req)
        lease = acquired["lease"]
        lease_id = lease["lease_id"]
        env = dict(acquired.get("env") or {})
        spawn_contract = dict(acquired.get("spawn") or {})
        cwd = str(spawn_contract.get("cwd") or self.root)
        profile = str(spawn_contract.get("profile") or NAVIGATOR_PROFILE)

        agent_ctl = resolve_agent_ctl_bin(
            agent_ctl_path or os.environ.get("BROWSERCTL_HERDR_AGENT_CTL")
        )
        name = _slug_name(name_req, lease_id=lease_id)

        # 2) herdr-agent-ctl spawn with exact env.
        try:
            spawn_payload = invoke_herdr_agent_ctl_spawn(
                agent_ctl=agent_ctl,
                name=name,
                cwd=cwd,
                env=env,
                profile=profile,
                model=model,
                thinking=thinking,
                split=split,
                lifecycle=nav_lifecycle,
                workspace=workspace,
                tab=tab,
                herdr_socket=herdr_socket,
                herdr_session=herdr_session,
            )
        except Exception as e:
            # Roll back lease so spawn failure does not leak browser resources.
            release_out = None
            try:
                release_out = self.manager.release(lease_id=lease_id, force=True)
            except Exception as re:  # pragma: no cover - nested failure path
                release_out = {"ok": False, "error": str(re)}
            if isinstance(e, (AdapterError, InvalidRequest)):
                details = dict(e.details or {})
                details["lease_id"] = lease_id
                details["lease_release"] = release_out
                details["navigator_name"] = name
                raise type(e)(e.message, **details) from e
            raise AdapterError(
                f"navigator spawn failed: {e}",
                lease_id=lease_id,
                lease_release=release_out,
                navigator_name=name,
            ) from e

        binding = _binding_from_spawn_payload(
            lease=lease,
            spawn_result=spawn_payload,
            agent_ctl=agent_ctl,
            cwd=cwd,
            profile=profile,
            requested_name=name,
            herdr_socket=herdr_socket,
            herdr_session=herdr_session,
        )
        save_binding(self.state_root, binding)

        # Stamp lease.meta.navigator (minimal; binding file is canonical).
        full_lease = require_lease(self.state_root, lease_id)
        with worker_mutex(self.state_root, full_lease["worker_id"]):
            full_lease = require_lease(self.state_root, lease_id)
            full_lease = touch_lease(full_lease)
            meta = dict(full_lease.get("meta") or {})
            meta["navigator"] = {
                "name": binding.get("name"),
                "pane_id": binding.get("pane_id"),
                "workspace_id": binding.get("workspace_id"),
                "tab_id": binding.get("tab_id"),
                "profile": profile,
                "binding_path": str(navigator_binding_path(self.state_root, lease_id)),
            }
            full_lease["meta"] = meta
            save_lease(self.state_root, full_lease)
            lease = full_lease

        watch_rec = None
        watch_error = None
        if watch:
            nav_pane = agent_pane_override or binding.get("pane_id")
            try:
                # Forward optional watch extras (viewport*) when present on req
                # without hardcoding Manager.watch signature.
                watch_kwargs: dict[str, Any] = {
                    "lease_id": lease_id,
                    "agent_pane": nav_pane,
                    "ratio": ratio,
                    "herdr_session": herdr_session or binding.get("herdr_session"),
                    "herdr_socket": herdr_socket or binding.get("herdr_socket"),
                    "ready_timeout_s": (
                        float(ready_timeout) if ready_timeout is not None else None
                    ),
                }
                for key in ("viewport", "viewport_width", "viewport_height", "direction"):
                    if key in req and req.get(key) is not None:
                        watch_kwargs[key] = req.get(key)
                w = self.manager.watch(**watch_kwargs)
                watch_rec = w.get("watch")
                # Refresh binding with watch pane id if present.
                if watch_rec and watch_rec.get("watch_pane_id"):
                    binding = dict(binding)
                    binding["watch_pane_id"] = watch_rec.get("watch_pane_id")
                    binding["agent_pane_id"] = watch_rec.get("agent_pane_id")
                    binding["updated_at"] = _now_iso()
                    save_binding(self.state_root, binding)
            except Exception as e:
                watch_error = str(e)

        # Auto-reap eligibility (crash backstop for finite jobs).
        from browserctl.store import is_auto_reap_eligible

        auto_reap_eligible = is_auto_reap_eligible(lease)

        run_target = binding.get("pane_id") or binding.get("name")
        receipt = {
            "ok": True,
            "lease_id": lease_id,
            "worker_id": lease.get("worker_id"),
            "lease": {
                "lease_id": lease_id,
                "worker_id": lease.get("worker_id"),
                "kind": lease.get("kind"),
                "status": lease.get("status"),
                "mode": lease.get("mode"),
                "auto_reap": bool(lease.get("auto_reap")),
                "auto_reap_eligible": auto_reap_eligible,
                "profile_name": lease.get("profile_name"),
            },
            "env": env,
            "navigator": {
                "name": binding.get("name"),
                "pane_id": binding.get("pane_id"),
                "workspace_id": binding.get("workspace_id"),
                "tab_id": binding.get("tab_id"),
                "profile": profile,
                "cwd": cwd,
                "lifecycle": binding.get("lifecycle"),
            },
            "binding": {
                "lease_id": lease_id,
                "name": binding.get("name"),
                "pane_id": binding.get("pane_id"),
                "workspace_id": binding.get("workspace_id"),
                "tab_id": binding.get("tab_id"),
                "path": str(navigator_binding_path(self.state_root, lease_id)),
                "herdr_socket": binding.get("herdr_socket"),
                "herdr_session": binding.get("herdr_session"),
                "watch_pane_id": binding.get("watch_pane_id"),
            },
            "watch": watch_rec,
            "watch_error": watch_error,
            # Exact next-step fields for orchestrator dispatch between spawn and cleanup.
            "next": {
                "run_target": run_target,
                "run_via": "herdr-agent-ctl run --target <run_target> --prompt ...",
                "cleanup": f"browserctl navigator cleanup --lease {lease_id} --json",
                "cleanup_by_name": (
                    f"browserctl navigator cleanup --name {binding.get('name')} --json"
                    if binding.get("name")
                    else None
                ),
                "status": f"browserctl navigator status --lease {lease_id} --json",
                "note": (
                    "normal path: cleanup in finally. scheduled browserctl reap is "
                    "crash backstop only for auto-reap-eligible leases "
                    "(one_shot|expiring|auto_reap)"
                    if auto_reap_eligible
                    else (
                        "persistent lease: not scheduled-reap eligible; "
                        "always navigator cleanup in finally "
                        "(or pass --mode one_shot / --auto-reap for finite jobs)"
                    )
                ),
            },
            "spawn_agent": {
                "status": spawn_payload.get("status"),
                "receipt": spawn_payload.get("receipt"),
                "agent_ctl": agent_ctl,
            },
        }
        return receipt

    # ── status ───────────────────────────────────────────────────────────

    def status(
        self,
        *,
        lease_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        binding, lease = self._resolve_binding(lease_id=lease_id, name=name)
        lid = (binding or {}).get("lease_id") or lease_id
        lease_pub = None
        if lease:
            lease_pub = {
                "lease_id": lease.get("lease_id"),
                "worker_id": lease.get("worker_id"),
                "status": lease.get("status"),
                "kind": lease.get("kind"),
                "watch": bool(lease.get("watch")),
            }
        elif lid:
            try:
                lease = require_lease(self.state_root, lid)
                lease_pub = {
                    "lease_id": lease.get("lease_id"),
                    "worker_id": lease.get("worker_id"),
                    "status": lease.get("status"),
                    "kind": lease.get("kind"),
                    "watch": bool(lease.get("watch")),
                }
            except LeaseNotFound:
                lease_pub = None
        return {
            "ok": True,
            "lease_id": lid or (lease or {}).get("lease_id"),
            "binding": binding,
            "lease": lease_pub,
            "next": {
                "cleanup": (
                    f"browserctl navigator cleanup --lease {lid} --json"
                    if lid
                    else None
                ),
            },
        }

    # ── cleanup ──────────────────────────────────────────────────────────

    def cleanup(
        self,
        *,
        lease_id: str | None = None,
        name: str | None = None,
        force: bool = False,
        keep_watch: bool = False,
        skip_navigator_close: bool = False,
    ) -> dict[str, Any]:
        """
        Unified cleanup: settle navigator pane, prove watch closed, release lease.

        Idempotent and safe for ``finally``. Fail closed when ownership/binding
        is uncertain (ambiguous name, missing both selectors).
        """
        if not lease_id and not name:
            raise InvalidRequest("navigator cleanup requires --lease or --name")

        binding, lease = self._resolve_binding(lease_id=lease_id, name=name)
        if lease is None and binding is None:
            raise LeaseNotFound(lease_id=lease_id, worker_id=None)

        lid = (lease or {}).get("lease_id") or (binding or {}).get("lease_id")
        if not lid:
            raise InvalidRequest(
                "cannot determine lease_id for navigator cleanup",
                binding=binding,
            )

        # Reload authoritative lease when present.
        try:
            lease = require_lease(self.state_root, lid)
        except LeaseNotFound:
            lease = None

        proof: dict[str, Any] = {
            "lease_id": lid,
            "navigator_close": None,
            "navigator_pane": None,
            "watch_stop": None,
            "release": None,
            "binding_cleared": False,
            "steps": [],
        }

        # Already fully settled?
        if lease and lease.get("status") in ("released", "reaped") and not lease.get("watch"):
            if binding:
                delete_binding(self.state_root, lid)
                proof["binding_cleared"] = True
            return {
                "ok": True,
                "idempotent": True,
                "settled": True,
                "lease_id": lid,
                "lease": {
                    "lease_id": lid,
                    "status": lease.get("status"),
                    "worker_id": lease.get("worker_id"),
                },
                "proof": proof,
                "retryable": False,
            }

        nav_name = (binding or {}).get("name") or (
            ((lease or {}).get("meta") or {}).get("navigator") or {}
        ).get("name")
        nav_pane = (binding or {}).get("pane_id") or (
            ((lease or {}).get("meta") or {}).get("navigator") or {}
        ).get("pane_id")
        herdr_socket = (binding or {}).get("herdr_socket") or os.environ.get(
            "HERDR_SOCKET_PATH"
        )
        herdr_session = (binding or {}).get("herdr_session") or os.environ.get(
            "HERDR_SESSION"
        )
        agent_ctl = (binding or {}).get("agent_ctl")

        endpoint: dict[str, str] = {}
        if herdr_socket or herdr_session:
            try:
                endpoint = watch_mod.resolve_herdr_endpoint(
                    herdr_session=herdr_session,
                    herdr_socket=herdr_socket,
                    lease_watch=(lease or {}).get("watch")
                    if isinstance((lease or {}).get("watch"), dict)
                    else None,
                )
            except InvalidRequest:
                endpoint = {}
                if herdr_socket:
                    endpoint["herdr_socket"] = str(herdr_socket)
                if herdr_session:
                    endpoint["herdr_session"] = str(herdr_session)

        # 1) Close navigator pane via herdr-agent-ctl (preferred) + verify.
        nav_settled = False
        nav_close: dict[str, Any] | None = None
        if skip_navigator_close:
            nav_close = {"ok": True, "skipped": True, "status": "skipped"}
            nav_settled = True
            proof["steps"].append("navigator_close_skipped")
        elif not nav_name and not nav_pane:
            # No navigator binding — treat navigator step as N/A (lease-only cleanup).
            nav_close = {
                "ok": True,
                "status": "no_navigator",
                "evidence": "no_binding",
            }
            nav_settled = True
            proof["steps"].append("navigator_absent")
        else:
            target = nav_name or nav_pane
            try:
                ctl = resolve_agent_ctl_bin(agent_ctl)
            except (InvalidRequest, AdapterError) as e:
                ctl = None
                nav_close = {
                    "ok": False,
                    "status": "agent_ctl_missing",
                    "error": str(e),
                    "target": target,
                }
            if ctl:
                nav_close = invoke_herdr_agent_ctl_close(
                    agent_ctl=ctl,
                    target=str(target),
                    herdr_socket=herdr_socket,
                    herdr_session=herdr_session,
                )
            # Evidence: pane must be absent (or agent-ctl said gone).
            pane_proof = verify_navigator_pane_settled(
                pane_id=nav_pane, endpoint=endpoint
            )
            proof["navigator_pane"] = pane_proof
            if pane_proof.get("settled"):
                nav_settled = True
            elif nav_close and nav_close.get("status") == "gone" and not nav_pane:
                nav_settled = True
            elif nav_close and nav_close.get("ok") and pane_proof.get("settled"):
                nav_settled = True
            proof["navigator_close"] = nav_close
            proof["steps"].append(
                "navigator_settled" if nav_settled else "navigator_unsettled"
            )

            if not nav_settled and not force:
                # Fail closed — do not release browser while navigator ownership
                # is uncertain / pane still present.
                if binding:
                    binding = dict(binding)
                    binding["status"] = "cleanup_incomplete"
                    binding["updated_at"] = _now_iso()
                    binding["last_cleanup"] = {
                        "navigator_close": nav_close,
                        "navigator_pane": pane_proof,
                    }
                    save_binding(self.state_root, binding)
                return {
                    "ok": False,
                    "settled": False,
                    "lease_id": lid,
                    "error": {
                        "code": "NAVIGATOR_CLOSE_INCOMPLETE",
                        "message": (
                            "navigator pane close not confirmed; "
                            "refusing lease release (fail closed). "
                            "retry cleanup or pass --force to release browser anyway"
                        ),
                    },
                    "proof": proof,
                    "retryable": True,
                    "next": {
                        "retry": f"browserctl navigator cleanup --lease {lid} --json",
                        "force": f"browserctl navigator cleanup --lease {lid} --force --json",
                    },
                }

        # 2) Watch close with evidence (via manager.release or explicit unwatch).
        # Prefer full release which also stops adapter; if watch fails, release
        # stays retryable.
        if lease and lease.get("status") not in ("released", "reaped"):
            release_out = self.manager.release(
                lease_id=lid,
                force=force,
                keep_watch=keep_watch,
            )
            proof["release"] = {
                "ok": release_out.get("ok"),
                "retryable": release_out.get("retryable"),
                "release": release_out.get("release"),
                "watch_stop": release_out.get("watch_stop"),
                "idempotent": release_out.get("idempotent"),
            }
            proof["watch_stop"] = release_out.get("watch_stop")
            proof["steps"].append("lease_release")
            release_ok = bool(release_out.get("ok")) and not release_out.get("retryable")
        else:
            # Lease already terminal — still try to stop leftover watch if any.
            release_ok = True
            if lease and lease.get("watch") and not keep_watch:
                try:
                    uw = self.manager.unwatch(lease_id=lid, close=True)
                    proof["watch_stop"] = uw.get("unwatch")
                    release_ok = bool(uw.get("ok"))
                    proof["steps"].append("watch_unwatch")
                except Exception as e:
                    proof["watch_stop"] = {"closed": False, "error": str(e)}
                    release_ok = False
            proof["release"] = {
                "ok": True,
                "idempotent": True,
                "status": (lease or {}).get("status") or "absent",
            }
            proof["steps"].append("lease_already_terminal")

        settled = bool(nav_settled and release_ok)
        if settled:
            delete_binding(self.state_root, lid)
            proof["binding_cleared"] = True
            proof["steps"].append("binding_cleared")
            # Clear navigator meta on terminal lease if still present.
            try:
                cur = load_lease(self.state_root, lid)
                if cur and (cur.get("meta") or {}).get("navigator"):
                    with worker_mutex(self.state_root, cur["worker_id"]):
                        cur = require_lease(self.state_root, lid)
                        meta = dict(cur.get("meta") or {})
                        meta["navigator_closed_at"] = _now_iso()
                        meta.pop("navigator", None)
                        cur["meta"] = meta
                        save_lease(self.state_root, cur)
            except LeaseNotFound:
                pass
        else:
            if binding:
                binding = dict(binding)
                binding["status"] = "cleanup_incomplete"
                binding["updated_at"] = _now_iso()
                binding["last_cleanup"] = proof
                save_binding(self.state_root, binding)

        return {
            "ok": settled,
            "settled": settled,
            "lease_id": lid,
            "worker_id": (lease or {}).get("worker_id") or (binding or {}).get("worker_id"),
            "navigator": {
                "name": nav_name,
                "pane_id": nav_pane,
                "settled": nav_settled,
            },
            "proof": proof,
            "retryable": not settled,
            "next": {
                "retry": (
                    None
                    if settled
                    else f"browserctl navigator cleanup --lease {lid} --json"
                ),
            },
        }

    # ── resolve helpers ──────────────────────────────────────────────────

    def _resolve_binding(
        self,
        *,
        lease_id: str | None,
        name: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if lease_id and name:
            # Both provided — require consistency.
            binding = load_binding(self.state_root, lease_id)
            by_name = find_binding_by_name(self.state_root, name)
            if binding and by_name and binding.get("lease_id") != by_name.get("lease_id"):
                raise InvalidRequest(
                    "navigator --lease and --name refer to different bindings",
                    lease_id=lease_id,
                    name=name,
                    lease_binding=binding.get("lease_id"),
                    name_binding=by_name.get("lease_id"),
                )
            binding = binding or by_name
            try:
                lease = require_lease(self.state_root, lease_id)
            except LeaseNotFound:
                lease = None
            return binding, lease

        if lease_id:
            binding = load_binding(self.state_root, lease_id)
            try:
                lease = require_lease(self.state_root, lease_id)
            except LeaseNotFound:
                lease = None
            if binding is None and lease is not None:
                nav = (lease.get("meta") or {}).get("navigator")
                if isinstance(nav, dict) and nav:
                    binding = {
                        "lease_id": lease_id,
                        "worker_id": lease.get("worker_id"),
                        **nav,
                        "source": "lease_meta",
                    }
            return binding, lease

        # name only
        binding = find_binding_by_name(self.state_root, str(name))
        if not binding:
            raise InvalidRequest(
                f"no navigator binding for name {name!r}",
                name=name,
            )
        lid = binding.get("lease_id")
        lease = None
        if lid:
            try:
                lease = require_lease(self.state_root, str(lid))
            except LeaseNotFound:
                lease = None
        return binding, lease
