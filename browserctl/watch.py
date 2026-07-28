"""
Watch / unwatch: split navigator herdr pane and run observe_mirror viewer.

Contract with herdr-browser:
  HERDR_BROWSER_MODE=observe_mirror
  HERDR_BROWSER_TARGET_STATE=<active-target.json path>
  HERDR_BROWSER_CDP_URL=http://127.0.0.1:<port>

Viewer root (prefer/require observe_mirror capability):
  HERDR_BROWSER_ROOT override — required for non-default trees; pre-merge
  worktrees (e.g. demiurge.mirror) are acceptable when they pass the probe.

Herd session targeting (fail closed if ambiguous):
  --herdr-session / HERDR_SESSION
  --herdr-socket  / HERDR_SOCKET_PATH
  Persisted on the lease watch record; used for every herdr call.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from browserctl.errors import AdapterError, InvalidRequest
from browserctl.paths import active_target_path, resolve_root

DEFAULT_RATIO = 0.42

# Preferred viewer roots — only used when they pass observe_mirror capability probe.
# HERDR_BROWSER_ROOT override always wins when set.
VIEWER_CANDIDATES = [
    # additional roots via HERDR_BROWSER_ROOT only (no hardcoded host paths)
    Path("/tmp/herdr-browser"),
]

# Files that prove observe_mirror support in a herdr-browser tree.
_OBSERVE_MIRROR_MARKERS = (
    "src/targetState.ts",
    "src/browser.ts",
)


def _herdr_bin() -> str:
    return os.environ.get("HERDR_BIN") or shutil.which("herdr") or "herdr"


def resolve_herdr_endpoint(
    *,
    herdr_session: str | None = None,
    herdr_socket: str | None = None,
    lease_watch: dict[str, Any] | None = None,
) -> dict[str, str]:
    """
    Resolve exact herdr session/socket. Fail closed if ambiguous.

    Priority: explicit args → lease watch record → env → fail if neither.
    """
    watch = lease_watch or {}
    session = (
        (herdr_session or "").strip()
        or str(watch.get("herdr_session") or "").strip()
        or os.environ.get("HERDR_SESSION", "").strip()
    )
    socket = (
        (herdr_socket or "").strip()
        or str(watch.get("herdr_socket") or "").strip()
        or os.environ.get("HERDR_SOCKET_PATH", "").strip()
    )

    if not socket and not session:
        raise InvalidRequest(
            "herdr endpoint ambiguous: pass --herdr-socket/--herdr-session "
            "or set HERDR_SOCKET_PATH / HERDR_SESSION "
            "(required so watch targets the navigator's exact herdr server)"
        )

    # If only session is set, still require socket when env has none — session
    # alone can be ambiguous across concurrent herdr servers.
    if session and not socket:
        # allow session-only when herdr supports --session without socket, but
        # prefer fail-closed: require socket path for multi-server safety.
        env_sock = os.environ.get("HERDR_SOCKET_PATH", "").strip()
        if env_sock:
            socket = env_sock
        else:
            raise InvalidRequest(
                "herdr session set but socket missing; pass --herdr-socket "
                "or HERDR_SOCKET_PATH (fail closed — multiple herdr servers possible)"
            )

    out: dict[str, str] = {}
    if socket:
        out["herdr_socket"] = socket
    if session:
        out["herdr_session"] = session
    return out


def herdr_prefix(endpoint: dict[str, str]) -> list[str]:
    """Build herdr CLI global flags for a bound endpoint."""
    args: list[str] = []
    # Prefer socket (unique server) then session name.
    if endpoint.get("herdr_socket"):
        # herdr accepts HERDR_SOCKET_PATH via env; also try --socket if present.
        # We always inject env; flags for session when known.
        pass
    if endpoint.get("herdr_session"):
        args.extend(["--session", endpoint["herdr_session"]])
    return args


def _run_herdr(
    args: list[str],
    *,
    endpoint: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    endpoint = endpoint or {}
    cmd = [_herdr_bin(), *herdr_prefix(endpoint), *args]
    env = os.environ.copy()
    if endpoint.get("herdr_socket"):
        env["HERDR_SOCKET_PATH"] = endpoint["herdr_socket"]
    if endpoint.get("herdr_session"):
        env.setdefault("HERDR_SESSION", endpoint["herdr_session"])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as e:
        raise AdapterError("herdr binary not found", err=str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise AdapterError("herdr command timed out", cmd=cmd) from e

    text = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise AdapterError(
            f"herdr {' '.join(args[:3])} failed ({proc.returncode})",
            stderr=(proc.stderr or "")[-1500:],
            stdout=text[-1500:],
            endpoint=endpoint,
        )
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _extract_pane_id(resp: dict[str, Any]) -> str | None:
    """Best-effort extract pane_id from herdr JSON envelopes."""
    if not resp:
        return None
    result = resp.get("result") if isinstance(resp.get("result"), dict) else resp
    for key in ("pane", "new_pane", "created_pane"):
        node = result.get(key) if isinstance(result, dict) else None
        if isinstance(node, dict) and node.get("pane_id"):
            return str(node["pane_id"])
    if isinstance(result, dict) and result.get("pane_id"):
        return str(result["pane_id"])
    if isinstance(result, dict):
        for v in result.values():
            if isinstance(v, dict) and v.get("pane_id"):
                return str(v["pane_id"])
    return None


def resolve_agent_pane(
    agent_pane: str | None,
    *,
    endpoint: dict[str, str],
) -> str:
    if agent_pane:
        return agent_pane.strip()
    env_pane = os.environ.get("HERDR_PANE_ID", "").strip()
    if env_pane:
        return env_pane
    resp = _run_herdr(["pane", "current"], endpoint=endpoint)
    pid = _extract_pane_id(resp)
    if not pid:
        result = resp.get("result") or {}
        pane = result.get("pane") if isinstance(result, dict) else None
        if isinstance(pane, dict):
            pid = pane.get("pane_id")
    if not pid:
        raise InvalidRequest(
            "cannot resolve agent pane; pass --agent-pane or set HERDR_PANE_ID"
        )
    return str(pid)


def probe_observe_mirror(root: Path) -> dict[str, Any]:
    """
    Capability probe: require observe_mirror-capable herdr-browser tree.

    Pass if viewer entry exists and source mentions observe_mirror / target state.
    Fail closed otherwise.
    """
    root = Path(root)
    reasons: list[str] = []
    if not root.is_dir():
        return {"ok": False, "root": str(root), "reasons": ["not a directory"]}

    viewer = root / "src" / "viewer.ts"
    if not viewer.is_file():
        reasons.append("missing src/viewer.ts")

    has_mode = False
    has_target_state = False
    for rel in _OBSERVE_MIRROR_MARKERS:
        p = root / rel
        if not p.is_file():
            reasons.append(f"missing {rel}")
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            reasons.append(f"unreadable {rel}: {e}")
            continue
        if "observe_mirror" in text or "observe-mirror" in text:
            has_mode = True
        if "HERDR_BROWSER_TARGET_STATE" in text or "active_target_id" in text:
            has_target_state = True

    # Also accept package-level README markers if sources are minified-absent
    readme = root / "README.md"
    if readme.is_file() and not has_mode:
        try:
            rtext = readme.read_text(encoding="utf-8", errors="replace")
            if "observe_mirror" in rtext:
                has_mode = True
        except OSError:
            pass

    if not has_mode:
        reasons.append("no observe_mirror marker in sources")
    if not has_target_state:
        reasons.append("no HERDR_BROWSER_TARGET_STATE / active_target_id marker")

    ok = viewer.is_file() and has_mode and has_target_state and not any(
        r.startswith("missing src/") for r in reasons
    )
    # tighten: must have viewer + mode + target state
    ok = bool(viewer.is_file() and has_mode and has_target_state)
    return {
        "ok": ok,
        "root": str(root),
        "has_viewer": viewer.is_file(),
        "has_observe_mirror": has_mode,
        "has_target_state": has_target_state,
        "reasons": reasons if not ok else [],
    }


def resolve_viewer_cwd(*, require_capable: bool = True) -> Path:
    """
    Resolve herdr-browser root that supports observe_mirror.

    Prefer HERDR_BROWSER_ROOT (override; required for non-default / pre-merge
    worktrees in production docs). Fall back to known candidates that pass
    the capability probe. Fail closed if none capable.
    """
    tried: list[dict[str, Any]] = []
    env = os.environ.get("HERDR_BROWSER_ROOT", "").strip()
    if env:
        probe = probe_observe_mirror(Path(env))
        tried.append(probe)
        if probe["ok"]:
            return Path(env)
        if require_capable:
            raise AdapterError(
                "HERDR_BROWSER_ROOT is set but is not observe_mirror-capable "
                "(fail closed). Point it at a tree with observe_mirror support "
                "(pre-merge worktree is OK if capable).",
                probe=probe,
            )

    for cand in VIEWER_CANDIDATES:
        probe = probe_observe_mirror(cand)
        tried.append(probe)
        if probe["ok"]:
            return cand

    raise AdapterError(
        "no observe_mirror-capable herdr-browser root found; set "
        "HERDR_BROWSER_ROOT to a capable tree (pre-merge worktrees acceptable "
        "when they pass the capability probe)",
        tried=tried,
    )


def viewer_command(cwd: Path) -> str:
    """Shell command to run observe_mirror viewer in a pane."""
    viewer = cwd / "src" / "viewer.ts"
    bun = shutil.which("bun") or "bun"
    return f"{shlex.quote(bun)} run {shlex.quote(str(viewer))}"


def split_watch_pane(
    *,
    agent_pane: str,
    endpoint: dict[str, str],
    direction: str = "right",
    ratio: float = DEFAULT_RATIO,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Split agent pane and return new pane_id."""
    args = [
        "pane",
        "split",
        agent_pane,
        "--direction",
        direction,
        "--ratio",
        str(ratio),
        "--no-focus",
    ]
    if cwd is not None:
        args.extend(["--cwd", str(cwd)])
    if env:
        for k, v in env.items():
            args.extend(["--env", f"{k}={v}"])
    resp = _run_herdr(args, endpoint=endpoint)
    new_id = _extract_pane_id(resp)
    if not new_id:
        raise AdapterError(
            "herdr pane split did not return pane_id",
            response=resp,
            endpoint=endpoint,
        )
    return new_id


def run_in_pane(
    pane_id: str,
    command: str,
    *,
    endpoint: dict[str, str],
) -> None:
    _run_herdr(["pane", "run", pane_id, command], endpoint=endpoint)


def close_pane(pane_id: str, *, endpoint: dict[str, str] | None = None) -> None:
    try:
        _run_herdr(["pane", "close", pane_id], endpoint=endpoint or {})
    except AdapterError:
        pass


def build_mirror_env(
    *,
    cdp_url: str,
    target_state_path: str | Path,
    viewer_root: Path | str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = {
        "HERDR_BROWSER_MODE": "observe_mirror",
        "HERDR_BROWSER_TARGET_STATE": str(target_state_path),
        "HERDR_BROWSER_CDP_URL": str(cdp_url).rstrip("/"),
    }
    if viewer_root is not None:
        env["HERDR_BROWSER_ROOT"] = str(viewer_root)
    if extra:
        env.update(extra)
    return env


def start_watch(
    lease: dict[str, Any],
    *,
    state_root: Path | str,
    agent_pane: str | None = None,
    ratio: float = DEFAULT_RATIO,
    direction: str = "right",
    herdr_session: str | None = None,
    herdr_socket: str | None = None,
) -> dict[str, Any]:
    """
    Split the navigator's existing pane and run observe_mirror viewer.

    Binds watch pane ids + herdr endpoint onto the returned watch record.
    """
    resources = lease.get("resources") or {}
    worker_id = lease.get("worker_id")
    cdp_url = resources.get("cdp_url")
    cdp_port = resources.get("cdp_port")
    if not cdp_url and cdp_port:
        cdp_url = f"http://127.0.0.1:{cdp_port}"
    if not cdp_url:
        raise InvalidRequest("lease has no cdp_url/cdp_port; cannot watch")

    endpoint = resolve_herdr_endpoint(
        herdr_session=herdr_session,
        herdr_socket=herdr_socket,
        lease_watch=lease.get("watch") if isinstance(lease.get("watch"), dict) else None,
    )

    target_path = resources.get("target_state_path") or str(
        active_target_path(state_root, worker_id)
    )
    Path(target_path).parent.mkdir(parents=True, exist_ok=True)
    if not Path(target_path).exists():
        from daemon.target_state import publish_active_target

        publish_active_target(
            target_path,
            worker_id=str(worker_id),
            active_target_id=None,
            cdp_url=cdp_url,
            seq=1,
        )

    agent = resolve_agent_pane(agent_pane, endpoint=endpoint)
    viewer_cwd = resolve_viewer_cwd(require_capable=True)
    probe = probe_observe_mirror(viewer_cwd)
    if not probe["ok"]:
        raise AdapterError(
            "viewer root failed observe_mirror capability probe (fail closed)",
            probe=probe,
        )

    mirror_env = build_mirror_env(
        cdp_url=cdp_url,
        target_state_path=target_path,
        viewer_root=viewer_cwd,
    )
    # Keep herdr socket in pane env so child tools hit the same server
    if endpoint.get("herdr_socket"):
        mirror_env["HERDR_SOCKET_PATH"] = endpoint["herdr_socket"]
    if endpoint.get("herdr_session"):
        mirror_env["HERDR_SESSION"] = endpoint["herdr_session"]

    watch_pane = split_watch_pane(
        agent_pane=agent,
        endpoint=endpoint,
        direction=direction,
        ratio=ratio,
        cwd=viewer_cwd,
        env=mirror_env,
    )
    cmd = viewer_command(viewer_cwd)
    time.sleep(0.15)
    run_in_pane(watch_pane, cmd, endpoint=endpoint)

    return {
        "agent_pane_id": agent,
        "watch_pane_id": watch_pane,
        "direction": direction,
        "ratio": ratio,
        "viewer_cwd": str(viewer_cwd),
        "viewer_command": cmd,
        "viewer_probe": {
            "ok": True,
            "root": str(viewer_cwd),
            "has_observe_mirror": True,
        },
        "env": mirror_env,
        "target_state_path": target_path,
        "cdp_url": cdp_url,
        "herdr_socket": endpoint.get("herdr_socket"),
        "herdr_session": endpoint.get("herdr_session"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def stop_watch(lease: dict[str, Any], *, close: bool = True) -> dict[str, Any]:
    watch = lease.get("watch") or {}
    pane = watch.get("watch_pane_id")
    endpoint = resolve_herdr_endpoint(
        herdr_session=watch.get("herdr_session"),
        herdr_socket=watch.get("herdr_socket"),
        lease_watch=watch,
    ) if (watch.get("herdr_socket") or watch.get("herdr_session") or
          os.environ.get("HERDR_SOCKET_PATH") or os.environ.get("HERDR_SESSION")) else {}
    closed = False
    if pane and close:
        # Prefer bound endpoint; fall back to env-only close if unresolved
        try:
            if endpoint:
                close_pane(str(pane), endpoint=endpoint)
            else:
                close_pane(str(pane), endpoint={})
            closed = True
        except InvalidRequest:
            # last resort without fail — best-effort close on default env
            close_pane(str(pane), endpoint={})
            closed = True
    return {
        "watch_pane_id": pane,
        "closed": closed,
        "agent_pane_id": watch.get("agent_pane_id"),
        "herdr_socket": watch.get("herdr_socket"),
        "herdr_session": watch.get("herdr_session"),
    }
