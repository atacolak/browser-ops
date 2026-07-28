"""xAI coal adapter — delegates lifecycle to identity_ops (canonical)."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import identity_ops
from browserctl.errors import AdapterError, InvalidRequest
from browserctl.paths import active_target_path, resolve_root, resolve_state_root

name = "xai"


def _run_identity(fn, *args, **kwargs) -> dict[str, Any]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = fn(*args, **kwargs)
    text = buf.getvalue().strip()
    if code not in (0, None):
        raise AdapterError(
            f"identity_ops returned {code}",
            stdout=text[-2000:] if text else "",
        )
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _probe_cdp(port: int | None) -> bool:
    if not port:
        return False
    try:
        return bool(identity_ops._cdp_alive(int(port)))
    except Exception:
        return False


def acquire(request: dict[str, Any]) -> dict[str, Any]:
    """
    Ensure xAI identity worker.

    Always marks ``meta.attached_existing`` when the CDP endpoint was already
    live *before* ensure, or when ensure reports an already-running daemon.
    Callers MUST NOT force-stop this browser on lease conflict — the winner
    lease owns the live session.
    """
    email = (request.get("email") or request.get("identity") or "").strip()
    if not email:
        raise InvalidRequest("xai acquire requires --email")
    no_start = bool(request.get("no_start"))

    # Resolve publish paths *before* ensure so identity_ops daemon start inherits
    # BROWSER_OPS_STATE / BROWSER_TARGET_STATE when browserctl overrides state root.
    root = resolve_root(request.get("root"))
    state_root = resolve_state_root(request.get("state_root"), root=root)
    # worker_id unknown until ensure; use a provisional env with state root only,
    # then set precise BROWSER_TARGET_STATE after worker_id is known. For cold
    # start, identity_ops _env_for reads os.environ — set state root now.
    prev_ops = os.environ.get("BROWSER_OPS_STATE")
    prev_tgt = os.environ.get("BROWSER_TARGET_STATE")
    os.environ["BROWSER_OPS_STATE"] = str(state_root)
    try:
        payload = _run_identity(
            identity_ops.cmd_ensure,
            email,
            as_json=True,
            no_start=no_start,
        )
    finally:
        # restore caller env (payload env carries the effective values)
        if prev_ops is None:
            os.environ.pop("BROWSER_OPS_STATE", None)
        else:
            os.environ["BROWSER_OPS_STATE"] = prev_ops
        if prev_tgt is None:
            os.environ.pop("BROWSER_TARGET_STATE", None)
        else:
            os.environ["BROWSER_TARGET_STATE"] = prev_tgt

    worker_id = payload.get("worker_id")
    if not worker_id:
        raise AdapterError("identity_ops ensure missing worker_id", payload=payload)
    if worker_id == "default":
        raise AdapterError(
            "refusing managed default worker from identity_ops",
            payload=payload,
        )
    env = dict(payload.get("env") or {})
    cdp_port = payload.get("cdp_port")
    post_alive = _probe_cdp(int(cdp_port) if cdp_port else None)
    daemon = payload.get("daemon")
    # identity_ops is the shared lifecycle for this worker. Conflict paths MUST
    # NEVER stop the browser (would kill the winner lease mid-flight). Always
    # mark attached_existing so manager skips force-release on LEASE_CONFLICT.
    attached_existing = True

    target_state = active_target_path(state_root, worker_id)
    Path(target_state).parent.mkdir(parents=True, exist_ok=True)
    env.setdefault("BROWSER_OPS_ROOT", str(root))
    env["BROWSER_OPS_STATE"] = str(state_root)
    env["BROWSER_TARGET_STATE"] = str(target_state)

    resources = {
        "email": payload.get("email") or email,
        "slug": payload.get("slug"),
        "worker_id": worker_id,
        "cdp_port": cdp_port,
        "cdp_url": f"http://127.0.0.1:{cdp_port}" if cdp_port else None,
        "profile_dir": payload.get("profile_dir"),
        "state_dir": payload.get("state_dir"),
        "control_state_root": str(state_root),
        "target_state_path": str(target_state),
        "socket": payload.get("socket"),
        "daemon": daemon,
        "identity_created": payload.get("created"),
    }
    return {
        "worker_id": worker_id,
        "kind": "xai",
        "adapter": "xai",
        "resources": resources,
        "env": env,
        "meta": {
            "spawn_hint": payload.get("spawn_hint"),
            "email": resources["email"],
            "attached_existing": attached_existing,
            "post_cdp_alive": post_alive,
        },
    }


def release(lease: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    resources = lease.get("resources") or {}
    email = resources.get("email") or (lease.get("meta") or {}).get("email")
    worker_id = lease.get("worker_id") or resources.get("worker_id")
    if email:
        result = _run_identity(
            identity_ops.cmd_stop,
            email=email,
            worker=None,
            all_identities=False,
            as_json=True,
        )
    elif worker_id:
        result = _run_identity(
            identity_ops.cmd_stop,
            email=None,
            worker=worker_id,
            all_identities=False,
            as_json=True,
        )
    else:
        return {"status": "noop", "reason": "no email/worker on lease"}
    return {"status": "stopped", "identity_ops": result, "force": force}


def status(lease: dict[str, Any]) -> dict[str, Any]:
    resources = lease.get("resources") or {}
    email = resources.get("email") or (lease.get("meta") or {}).get("email")
    port = resources.get("cdp_port")
    alive = _probe_cdp(int(port) if port else None)
    return {
        "email": email,
        "worker_id": lease.get("worker_id"),
        "cdp_port": port,
        "cdp_alive": alive,
        "daemon": "running" if alive else "stopped",
    }
