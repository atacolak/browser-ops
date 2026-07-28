"""VPN worker adapter — attaches to / manages vpn/spawn_vpn_browser workers."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from pathlib import Path

from browserctl.errors import AdapterError, InvalidRequest
from browserctl.paths import (
    ROOT,
    active_target_path,
    resolve_root,
    resolve_state_root,
)

name = "vpn"

SPAWN = ROOT / "vpn" / "spawn_vpn_browser.py"


def _target_env(
    root: Path,
    worker_id: str,
    *,
    state_root: Path | str,
) -> tuple[dict[str, str], str, Path]:
    sr = Path(state_root)
    tsp = active_target_path(sr, worker_id)
    Path(tsp).parent.mkdir(parents=True, exist_ok=True)
    env = {
        "BROWSER_HARNESS_WORKER": worker_id,
        "PYTHONPATH": str(root),
        "BROWSER_ALLOW_EVALUATE": "1",
        "BROWSER_OPS_ROOT": str(root),
        "BROWSER_OPS_STATE": str(sr),
        "BROWSER_TARGET_STATE": str(tsp),
    }
    return env, str(tsp), sr


def _run_spawn(args: list[str]) -> dict[str, Any]:
    if not SPAWN.exists():
        raise AdapterError(f"vpn spawn script missing: {SPAWN}")
    proc = subprocess.run(
        [sys.executable, str(SPAWN), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    text = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise AdapterError(
            f"spawn_vpn_browser failed ({proc.returncode})",
            stdout=text[-2000:],
            stderr=(proc.stderr or "")[-2000:],
        )
    if not text:
        return {}
    # spawn may print logs to stderr and JSON to stdout
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # last JSON object in output
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {"raw": text}


def _load_runtime() -> dict[str, Any]:
    path = ROOT / "state" / "vpn_runtime.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def acquire(request: dict[str, Any]) -> dict[str, Any]:
    """
    Acquire a VPN browser worker.

    Modes:
      - attach existing: --worker already in vpn_runtime (no country required)
      - start new: --worker + --country + --city
    """
    root = resolve_root(request.get("root"))
    state_root = resolve_state_root(request.get("state_root"), root=root)
    worker_id = (request.get("worker_id") or request.get("worker") or "").strip()
    if not worker_id:
        raise InvalidRequest("vpn acquire requires --worker")
    if worker_id == "default":
        raise InvalidRequest("refusing managed default worker")

    runtime = _load_runtime()
    country = (request.get("country") or "").strip()
    city = (request.get("city") or "").strip()
    attach_only = bool(request.get("attach_only"))

    if worker_id in runtime and not (country and city):
        inst = runtime[worker_id]
        cdp_port = inst.get("cdp_port")
        env, tsp, sr = _target_env(root, worker_id, state_root=state_root)
        resources = {
            "worker_id": worker_id,
            "cdp_port": cdp_port,
            "cdp_url": f"http://127.0.0.1:{cdp_port}" if cdp_port else None,
            "profile_dir": inst.get("profile_dir"),
            "state_dir": str(root / "state" / worker_id),
            "control_state_root": str(sr),
            "target_state_path": tsp,
            "socket": str(root / "state" / worker_id / "daemon.sock"),
            "novnc_url": inst.get("novnc_magic_url") or inst.get("novnc_url"),
            "display": inst.get("display"),
            "vpn_attached": True,
            "country": inst.get("country"),
            "city": inst.get("city"),
        }
        return {
            "worker_id": worker_id,
            "kind": "vpn",
            "adapter": "vpn",
            "resources": resources,
            "env": env,
            "meta": {"attached_existing": True},
        }

    if attach_only:
        raise AdapterError(
            f"vpn worker {worker_id!r} not running and attach_only set",
            runtime_keys=sorted(runtime.keys()),
        )

    if not country or not city:
        raise InvalidRequest(
            "vpn acquire of new worker requires --country and --city "
            "(or attach an already-running --worker)"
        )

    args = ["start", worker_id, country, city]
    if request.get("cdp_port"):
        args.extend(["--cdp-port", str(int(request["cdp_port"]))])
    if request.get("extension"):
        args.extend(["--extension", str(request["extension"])])

    payload = _run_spawn(args)
    cdp_port = payload.get("cdp_port")
    env, tsp, sr = _target_env(root, worker_id, state_root=state_root)
    resources = {
        "worker_id": worker_id,
        "cdp_port": cdp_port,
        "cdp_url": f"http://127.0.0.1:{cdp_port}" if cdp_port else None,
        "profile_dir": payload.get("profile_dir"),
        "state_dir": str(root / "state" / worker_id),
        "control_state_root": str(sr),
        "target_state_path": tsp,
        "socket": str(root / "state" / worker_id / "daemon.sock"),
        "novnc_url": payload.get("novnc_magic_url") or payload.get("novnc_url"),
        "socks5_url": payload.get("socks5_url"),
        "display": payload.get("display"),
        "vpn_attached": False,
        "country": country,
        "city": city,
        "egress_info": payload.get("egress_info"),
    }
    # strip potential secrets from egress if any
    if isinstance(resources.get("egress_info"), dict):
        safe = {
            k: resources["egress_info"].get(k)
            for k in ("ip", "country", "city", "mullvad_exit_ip")
            if k in resources["egress_info"]
        }
        resources["egress_info"] = safe

    return {
        "worker_id": worker_id,
        "kind": "vpn",
        "adapter": "vpn",
        "resources": resources,
        "env": env,
        "meta": {"attached_existing": False, "country": country, "city": city},
    }


def release(lease: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    resources = lease.get("resources") or {}
    worker_id = lease.get("worker_id") or resources.get("worker_id")
    if not worker_id:
        return {"status": "noop", "reason": "no worker_id"}

    # If we only attached to an existing runtime and not force, leave it up
    # unless mode is one_shot or meta says we started it.
    meta = lease.get("meta") or {}
    started_here = not meta.get("attached_existing", False)
    if not started_here and not force and lease.get("mode") != "one_shot":
        return {
            "status": "left_running",
            "reason": "attached existing vpn worker; use --force to stop",
            "worker_id": worker_id,
        }

    try:
        result = _run_spawn(["stop", worker_id])
    except AdapterError as e:
        if force:
            return {"status": "error", "error": e.message, "force": True}
        raise
    return {"status": "stopped", "vpn": result, "force": force}


def status(lease: dict[str, Any]) -> dict[str, Any]:
    resources = lease.get("resources") or {}
    worker_id = lease.get("worker_id") or resources.get("worker_id")
    runtime = _load_runtime()
    inst = runtime.get(worker_id or "") if worker_id else None
    port = (inst or {}).get("cdp_port") or resources.get("cdp_port")
    alive = False
    if port:
        import urllib.request

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{int(port)}/json/version", timeout=2
            ) as r:
                alive = r.status == 200
        except Exception:
            alive = False
    return {
        "worker_id": worker_id,
        "in_runtime": bool(inst),
        "cdp_port": port,
        "cdp_alive": alive,
        "country": (inst or {}).get("country") or resources.get("country"),
        "city": (inst or {}).get("city") or resources.get("city"),
    }
