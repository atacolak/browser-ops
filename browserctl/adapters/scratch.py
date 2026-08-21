"""Ad-hoc / scratch profile adapter — unique or stable worker, profile dir, CDP port.

Wipe is opt-in via resources.ephemeral_wipe_v1, set only when acquire mints a
unique token worker (no explicit worker/profile_dir). Legacy leases that only
have ephemeral_profile=true are never wiped. Named/explicit scratches are stable.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from browserctl.atomic import atomic_write_json, read_json
from browserctl.errors import AdapterError, InvalidRequest
from browserctl.locks import port_alloc_mutex
from browserctl.paths import ROOT, control_root, resolve_root, resolve_state_root, scratch_profiles_root

name = "scratch"

PORT_MIN = 9300
PORT_MAX = 9399
RETIRED = {"default"}
PORT_BIND_RETRIES = 8
PORT_RESERVE_TTL = 120.0  # seconds; covers daemon start + CDP wait


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _reservations_path(state_root: Path | str) -> Path:
    return control_root(state_root) / "ports.json"


def _load_reservations(state_root: Path | str) -> dict[str, Any]:
    raw = read_json(_reservations_path(state_root))
    if not isinstance(raw, dict):
        return {"version": 1, "ports": {}}
    raw.setdefault("version", 1)
    raw.setdefault("ports", {})
    return raw


def _save_reservations(state_root: Path | str, data: dict[str, Any]) -> None:
    atomic_write_json(_reservations_path(state_root), data)


def _active_reserved(state_root: Path | str, *, now: float | None = None) -> set[int]:
    """Ports reserved under ports.lock that have not expired."""
    now = now if now is not None else time.time()
    data = _load_reservations(state_root)
    ports = data.get("ports") or {}
    alive: set[int] = set()
    changed = False
    for key, meta in list(ports.items()):
        try:
            port = int(key)
            exp = float((meta or {}).get("expires_at") or 0)
        except (TypeError, ValueError):
            del ports[key]
            changed = True
            continue
        if exp <= now:
            del ports[key]
            changed = True
            continue
        alive.add(port)
    if changed:
        data["ports"] = ports
        _save_reservations(state_root, data)
    return alive


def _reserve_port(
    state_root: Path | str,
    port: int,
    *,
    worker_id: str,
    ttl: float = PORT_RESERVE_TTL,
) -> None:
    now = time.time()
    data = _load_reservations(state_root)
    ports = data.setdefault("ports", {})
    ports[str(port)] = {
        "worker_id": worker_id,
        "reserved_at": now,
        "expires_at": now + float(ttl),
    }
    data["ports"] = ports
    _save_reservations(state_root, data)


def _release_reserved_port(state_root: Path | str, port: int | None) -> None:
    if not port:
        return
    data = _load_reservations(state_root)
    ports = data.get("ports") or {}
    if str(port) in ports:
        del ports[str(port)]
        data["ports"] = ports
        _save_reservations(state_root, data)


def _select_free_port(
    preferred: int | None = None,
    *,
    reserved: set[int] | None = None,
) -> int:
    reserved = reserved or set()
    if preferred is not None:
        if preferred in (9222,):
            raise InvalidRequest("port 9222 is retired; choose another")
        if preferred in reserved:
            raise AdapterError(f"preferred port {preferred} already reserved")
        if _port_free(preferred):
            return preferred
        raise AdapterError(f"preferred port {preferred} not free")
    for port in range(PORT_MIN, PORT_MAX + 1):
        if port in reserved:
            continue
        if _port_free(port):
            return port
    raise AdapterError(f"no free scratch CDP port in {PORT_MIN}-{PORT_MAX}")


def _cdp_alive(port: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _slugify(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return (s or "scratch")[:40]


def _daemon_pids(worker_id: str) -> list[int]:
    pids: list[int] = []
    try:
        out = subprocess.check_output(["pgrep", "-af", "daemon"], text=True)
    except subprocess.CalledProcessError:
        return pids
    needle = f"--worker {worker_id}"
    for line in out.splitlines():
        if "pgrep" in line:
            continue
        if needle not in line:
            continue
        if "daemon/main.py" not in line and "daemon.main" not in line:
            continue
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            pids.append(int(parts[0]))
    return sorted(set(pids))


def _chrome_pids(profile_dir: str | Path) -> list[int]:
    profile = str(Path(profile_dir).resolve())
    pids: list[int] = []
    try:
        out = subprocess.check_output(["pgrep", "-af", "chrome"], text=True)
    except subprocess.CalledProcessError:
        return pids
    for line in out.splitlines():
        if "pgrep" in line:
            continue
        if profile not in line and str(profile_dir) not in line:
            continue
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            pids.append(int(parts[0]))
    return pids


def _kill_pids(pids: list[int]) -> None:
    for pid in sorted(set(pids)):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(0.4)
    for pid in sorted(set(pids)):
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _env_for(
    root: Path,
    worker_id: str,
    *,
    state_root: Path | str | None = None,
    target_state_path: Path | str | None = None,
) -> dict[str, str]:
    """Navigator + daemon env. Target/state paths must match lease publication."""
    from browserctl.paths import active_target_path as _atp

    sr = Path(state_root) if state_root is not None else (root / "state")
    tsp = (
        Path(target_state_path)
        if target_state_path is not None
        else _atp(sr, worker_id)
    )
    return {
        "BROWSER_HARNESS_WORKER": worker_id,
        "PYTHONPATH": str(root),
        "BROWSER_ALLOW_EVALUATE": "1",
        "BROWSER_OPS_ROOT": str(root),
        # Daemon cloak backend reads these for active-target publish location.
        "BROWSER_OPS_STATE": str(sr),
        "BROWSER_TARGET_STATE": str(tsp),
    }


def _start_daemon(
    *,
    root: Path,
    worker_id: str,
    port: int,
    profile_dir: Path,
    env: dict[str, str],
    headless: bool,
) -> None:
    log_path = Path(f"/tmp/daemon-{worker_id}.log")
    cmd = [
        sys.executable,
        "-m",
        "daemon.main",
        "--worker",
        worker_id,
        "--launch",
        "--cdp-port",
        str(port),
        "--profile-dir",
        str(profile_dir),
    ]
    if headless:
        cmd.append("--headless")
    full_env = os.environ.copy()
    full_env.update(env)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n--- scratch acquire {time.strftime('%Y-%m-%dT%H:%M:%SZ')} ---\n")
        log.flush()
        subprocess.Popen(
            cmd,
            cwd=str(root),
            env=full_env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _wait_cdp(port: int, *, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _cdp_alive(port):
            return True
        time.sleep(0.3)
    return False


def _wait_socket(path: Path, *, timeout: float = 10.0) -> bool:
    """Wait until the daemon unix socket exists (CDP can be up first)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _strict_child(path: Path, parent: Path) -> bool:
    try:
        r, pr = path.resolve(), parent.resolve()
        if r == pr:
            return False
        r.relative_to(pr)
        return True
    except (OSError, ValueError):
        return False


def _control_blocked(path: Path, control_state_root: Path | str | None) -> bool:
    if not control_state_root:
        return False
    try:
        r = path.resolve()
        sr = Path(control_state_root).resolve()
        ctl = sr / "control"
        if ctl.exists():
            ctl = ctl.resolve()
    except OSError:
        return True
    if r in (sr, ctl):
        return True
    try:
        ctl.relative_to(r)
        return True
    except ValueError:
        pass
    try:
        r.relative_to(ctl)
        return True
    except ValueError:
        return False


def _wipe_dir(
    path: str | Path | None,
    *,
    allowed_root: Path,
    label: str,
    control_state_root: Path | str | None = None,
) -> dict[str, Any]:
    if not path:
        return {"path": None, "wiped": False, "skipped": True, "reason": f"no_{label}"}
    p = Path(path)
    try:
        resolved = p.resolve()
    except OSError as e:
        return {"path": str(path), "wiped": False, "error": str(e)}
    if _control_blocked(resolved, control_state_root):
        return {
            "path": str(resolved),
            "wiped": False,
            "reason": "control_plane",
            "error": f"refuse wipe {label} overlapping control plane",
        }
    if not _strict_child(resolved, allowed_root):
        return {
            "path": str(resolved),
            "wiped": False,
            "error": f"refuse wipe {label} outside {allowed_root}",
        }
    if not p.exists() and not resolved.exists():
        return {"path": str(resolved), "wiped": True, "missing": True}
    try:
        shutil.rmtree(resolved)
        return {"path": str(resolved), "wiped": True}
    except OSError as e:
        return {"path": str(resolved), "wiped": False, "error": str(e)}


def _wipe_ok(result: dict[str, Any] | None) -> bool:
    if not result:
        return False
    if result.get("wiped"):
        return True
    return bool(result.get("skipped") and str(result.get("reason") or "").startswith("no_"))


def acquire(request: dict[str, Any]) -> dict[str, Any]:
    root = resolve_root(request.get("root"))
    state_root = resolve_state_root(request.get("state_root"), root=root)
    label = (request.get("label") or request.get("name") or "").strip()
    explicit_worker = bool(
        (request.get("worker_id") or request.get("worker") or "").strip()
    )
    explicit_profile_dir = bool(request.get("profile_dir"))
    token = uuid.uuid4().hex[:8]
    if explicit_worker:
        worker_id = (request.get("worker_id") or request.get("worker") or "").strip()
    elif label:
        worker_id = f"scratch-{_slugify(label)}-{token}"
    else:
        worker_id = f"scratch-{token}"

    if worker_id in RETIRED or worker_id == "default":
        raise InvalidRequest("refusing managed default worker for scratch")

    # Wipe marker only for newly minted token workers. Never accept request-side
    # ephemeral_profile / ephemeral_wipe_v1 as an enablement switch (legacy safe).
    ephemeral_wipe_v1 = not explicit_worker and not explicit_profile_dir
    ephemeral_profile = ephemeral_wipe_v1  # informational; wipe gates on wipe_v1

    preferred = request.get("cdp_port")
    if preferred is not None:
        preferred = int(preferred)

    profiles = scratch_profiles_root(root)
    profile_dir = Path(request.get("profile_dir") or (profiles / worker_id))
    state_dir = root / "state" / worker_id
    # when state_root overridden (tests), still place worker state under ops root
    # unless request forces state_dir
    if request.get("state_dir"):
        state_dir = Path(request["state_dir"])
    profile_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    headless = bool(request.get("headless", True))
    no_start = bool(request.get("no_start"))
    # Align daemon publish path with lease/manager target_state_path (override-safe).
    from browserctl.paths import active_target_path as _atp

    target_state = Path(
        request.get("target_state_path") or _atp(state_root, worker_id)
    )
    target_state.parent.mkdir(parents=True, exist_ok=True)
    env = _env_for(
        root,
        worker_id,
        state_root=state_root,
        target_state_path=target_state,
    )

    daemon = "stopped"
    port: int | None = None
    last_err: str | None = None

    # Global lock around select+reserve(+daemon start handoff) so parallel
    # acquires cannot TOCTOU the same free port. Reservation registry covers
    # no_start and the window before CDP bind is observable.
    for attempt in range(PORT_BIND_RETRIES):
        with port_alloc_mutex(state_root, timeout=30.0):
            reserved = _active_reserved(state_root)
            try:
                candidate = _select_free_port(
                    preferred if attempt == 0 else None,
                    reserved=reserved,
                )
            except (AdapterError, InvalidRequest):
                if preferred is not None and attempt == 0:
                    raise
                last_err = "no free port"
                time.sleep(0.05 * (attempt + 1))
                continue
            port = candidate
            _reserve_port(state_root, port, worker_id=worker_id)
            if no_start:
                break
            try:
                _start_daemon(
                    root=root,
                    worker_id=worker_id,
                    port=port,
                    profile_dir=profile_dir,
                    env=env,
                    headless=headless,
                )
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                _release_reserved_port(state_root, port)
                port = None
                time.sleep(0.05 * (attempt + 1))
                continue
        # outside lock: wait for CDP, then IPC socket (listen is after CDP)
        if no_start:
            break
        assert port is not None
        sock = state_dir / "daemon.sock"
        if _wait_cdp(port, timeout=20.0) and _wait_socket(sock, timeout=10.0):
            daemon = "running"
            break
        # bind failed or daemon died — kill stray, drop reservation, retry
        last_err = (
            f"CDP {port} not live after start"
            if not _cdp_alive(port)
            else f"daemon.sock missing after CDP {port} ({sock})"
        )
        _kill_pids(_daemon_pids(worker_id) + _chrome_pids(profile_dir))
        with port_alloc_mutex(state_root, timeout=30.0):
            _release_reserved_port(state_root, port)
        preferred = None  # drop sticky preferred after first failure
        port = None
        time.sleep(0.1 * (attempt + 1))

    if port is None:
        raise AdapterError(
            f"scratch failed to allocate unique CDP port after {PORT_BIND_RETRIES} tries",
            last_error=last_err,
            worker_id=worker_id,
        )
    if not no_start and daemon != "running":
        with port_alloc_mutex(state_root, timeout=30.0):
            _release_reserved_port(state_root, port)
        tail = ""
        try:
            tail = Path(f"/tmp/daemon-{worker_id}.log").read_text(errors="replace")[-1500:]
        except OSError:
            pass
        raise AdapterError(
            f"scratch daemon failed to expose CDP {port}",
            log_tail=tail,
            worker_id=worker_id,
            last_error=last_err,
        )

    resources = {
        "worker_id": worker_id,
        "cdp_port": port,
        "cdp_url": f"http://127.0.0.1:{port}",
        "profile_dir": str(profile_dir),
        "state_dir": str(state_dir),
        "ops_root": str(root),
        "control_state_root": str(state_root),
        "target_state_path": str(target_state),
        "socket": str(state_dir / "daemon.sock"),
        "daemon": daemon,
        "headless": headless,
        # legacy informational flag (also true only for wipe_v1 token workers)
        "ephemeral_profile": ephemeral_profile,
        # sole wipe enablement marker — never present on pre-upgrade leases
        "ephemeral_wipe_v1": ephemeral_wipe_v1,
    }
    return {
        "worker_id": worker_id,
        "kind": "scratch",
        "adapter": "scratch",
        "resources": resources,
        "env": env,
        "meta": {
            "label": label or None,
            "ephemeral_wipe_v1": ephemeral_wipe_v1,
            "stable": not ephemeral_wipe_v1,
        },
    }


def release(lease: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    resources = lease.get("resources") or {}
    worker_id = lease.get("worker_id") or resources.get("worker_id")
    profile_dir = resources.get("profile_dir")
    state_dir = resources.get("state_dir")
    port = int(resources.get("cdp_port") or 0)
    # ONLY the v1 marker enables wipe. legacy ephemeral_profile alone never wipes.
    wipe_enabled = bool(resources.get("ephemeral_wipe_v1"))
    if resources.get("ops_root"):
        root = Path(str(resources["ops_root"]))
    else:
        root = resolve_root(None)
        if profile_dir:
            try:
                pd = Path(str(profile_dir)).resolve()
                if pd.parent.name == "scratch" and pd.parent.parent.name == "profiles":
                    root = pd.parent.parent.parent
            except OSError:
                pass

    try:
        sr = resources.get("control_state_root") or str(root / "state")
        with port_alloc_mutex(sr, timeout=5.0):
            _release_reserved_port(sr, port)
    except Exception:
        pass

    daemon_pids = _daemon_pids(worker_id) if worker_id else []
    chrome_pids = _chrome_pids(profile_dir) if profile_dir else []
    _kill_pids(daemon_pids + chrome_pids)

    if worker_id:
        sock = Path(resources["socket"]) if resources.get("socket") else (
            root / "state" / worker_id / "daemon.sock"
        )
        try:
            if sock.exists():
                sock.unlink()
        except OSError:
            pass

    if profile_dir and not wipe_enabled:
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            p = Path(profile_dir) / name
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
            except OSError:
                pass

    daemon_left = _daemon_pids(worker_id) if worker_id else []
    chrome_left = _chrome_pids(profile_dir) if profile_dir else []
    still_cdp = bool(port and _cdp_alive(port))
    cleared = not daemon_left and not chrome_left and not still_cdp
    wipe_profile: dict[str, Any] | None = None
    wipe_state: dict[str, Any] | None = None
    control_sr = resources.get("control_state_root")

    if wipe_enabled and cleared:
        wipe_profile = _wipe_dir(
            profile_dir,
            allowed_root=scratch_profiles_root(root),
            label="profile_dir",
            control_state_root=control_sr,
        )
        wipe_state = _wipe_dir(
            state_dir,
            allowed_root=root / "state",
            label="state_dir",
            control_state_root=control_sr,
        )
    elif wipe_enabled and not cleared:
        wipe_profile = {
            "wiped": False,
            "skipped": True,
            "reason": "processes_or_cdp_alive",
            "daemon_pids_left": daemon_left,
            "chrome_pids_left": chrome_left,
            "cdp_alive": still_cdp,
        }
        wipe_state = dict(wipe_profile)

    cleanup_incomplete = False
    cleanup_error: str | None = None
    if wipe_enabled:
        if not cleared:
            cleanup_incomplete = True
            cleanup_error = "processes_or_cdp_alive"
        elif not (_wipe_ok(wipe_profile) and _wipe_ok(wipe_state)):
            cleanup_incomplete = True
            parts = []
            for w, fallback in (
                (wipe_profile, "profile_wipe_failed"),
                (wipe_state, "state_wipe_failed"),
            ):
                if not _wipe_ok(w):
                    parts.append((w or {}).get("error") or (w or {}).get("reason") or fallback)
            cleanup_error = "; ".join(parts)

    status = "partial" if (still_cdp or cleanup_incomplete) else "stopped"
    return {
        "status": status,
        "daemon_pids": daemon_pids,
        "chrome_pids": chrome_pids,
        "cdp_alive": still_cdp,
        "force": force,
        "ephemeral_wipe_v1": wipe_enabled,
        "ephemeral_profile": bool(resources.get("ephemeral_profile")),
        "cleanup_incomplete": cleanup_incomplete,
        "cleanup_error": cleanup_error,
        "profile_wiped": bool((wipe_profile or {}).get("wiped")),
        "state_wiped": bool((wipe_state or {}).get("wiped")),
        "wipe_profile": wipe_profile,
        "wipe_state": wipe_state,
    }


def status(lease: dict[str, Any]) -> dict[str, Any]:
    resources = lease.get("resources") or {}
    port = resources.get("cdp_port")
    alive = bool(port and _cdp_alive(int(port)))
    return {
        "worker_id": lease.get("worker_id"),
        "cdp_port": port,
        "cdp_alive": alive,
        "daemon": "running" if alive else "stopped",
        "profile_dir": resources.get("profile_dir"),
    }
