"""
Watch / unwatch: split navigator herdr pane and run observe_mirror viewer.

Contract with herdr-browser:
  HERDR_BROWSER_MODE=observe_mirror
  HERDR_BROWSER_TARGET_STATE=<active-target.json path>
  HERDR_BROWSER_CDP_URL=http://127.0.0.1:<port>

Cold-start race:
  Do NOT seed a null active_target_id (that opens a permanent about:blank
  mirror). Wait until CDP answers /json/version, active-target.json has a
  non-null active_target_id from the harness daemon, AND that id appears in
  CDP /json/list (rejects stale published state). Bounded wait; fail with a
  diagnostic if readiness never settles.

Viewer live stream:
  Watch panes must set HERDR_BROWSER_VIEWER_WATCH_RESIZE=1 so observe_mirror
  enters the resize/graphics-stream loop (otherwise daemon metrics stay
  graphics_stream.active=false / frames=0).

Viewport modes (CLI --viewport → HERDR_BROWSER_VIEWPORT_MODE):
  fixed (default): lock real page layout once to DEFAULT_VIEWPORT_WIDTH x
    DEFAULT_VIEWPORT_HEIGHT (1150x902 — measured fullscreen 1920x1080 herdr
    37/63 browser pane). Sets HERDR_BROWSER_VIEWPORT_MODE=fixed plus
    HERDR_BROWSER_VIEWPORT_WIDTH/HEIGHT. herdr-browser applies
    Emulation.setDeviceMetricsOverride at that size on attach and does not
    derive new dims from terminal resize; pane resize contain-fits/scales
    the rendered frame only.
  follow-pane: HERDR_BROWSER_VIEWPORT_MODE=follow-pane (+ legacy
    HERDR_BROWSER_FOLLOW_PANE_VIEWPORT=1). Terminal/pane size reflows the
    page each resize (previous default behavior).
  preserve: HERDR_BROWSER_VIEWPORT_MODE=preserve — never mutate layout
    (debug/forensic; not the navigator default).

Viewer root (prefer/require observe_mirror capability), first match wins:
  1. HERDR_BROWSER_ROOT env
  2. BROWSERCTL_VIEWER_ROOT env
  3. repo-local state/control/viewer-root text file (gitignored under state/)
  4. VIEWER_CANDIDATES that pass the capability probe

  Pre-merge worktrees are acceptable when they pass the probe.
  Do not hardcode operator home paths in-repo.

Herd session targeting (fail closed if ambiguous):
  --herdr-session / HERDR_SESSION
  --herdr-socket  / HERDR_SOCKET_PATH
  Persisted on the lease watch record; used for every herdr call.

Split ratio (herdr pane split --ratio):
  ratio = first-child fraction. Direction right keeps the agent pane as first
  child (left) and the new watch pane as second (right). Default 0.37 → agent
  37% / browser 63%. Explicit --ratio always wins.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from browserctl.errors import AdapterError, InvalidRequest
from browserctl.paths import (
    active_target_path,
    control_root,
    resolve_root,
    resolve_state_root,
)

# herdr: ratio is first-child fraction. direction=right → agent left, watch right.
# 0.37 keeps the paired browser near 1150px on a 191-column terminal while
# leaving enough transcript width for useful navigator observation.
DEFAULT_RATIO = 0.37
VIEWER_ROOT_FILENAME = "viewer-root"

# Fixed default layout viewport: measured browser-pane CSS size at fullscreen
# 1920x1080 with the default herdr 37/63 split (agent left / browser right).
DEFAULT_VIEWPORT_MODE = "fixed"
DEFAULT_VIEWPORT_WIDTH = 1150
DEFAULT_VIEWPORT_HEIGHT = 902
VIEWPORT_MODES = frozenset({"fixed", "follow-pane", "preserve"})

# Bounded cold-start wait before failing watch with diagnostics.
DEFAULT_READY_TIMEOUT_S = 20.0
DEFAULT_READY_POLL_S = 0.25

# After pane run: bounded verify that the viewer process actually started.
DEFAULT_VIEWER_START_TIMEOUT_S = 3.0
DEFAULT_VIEWER_START_POLL_S = 0.15

# Substrings that indicate the observe_mirror viewer is running in the pane.
_VIEWER_PROCESS_MARKERS = (
    "viewer.ts",
    "herdr-browser",
    "observe_mirror",
)

# Preferred viewer roots — only used when they pass observe_mirror capability probe.
# Env / viewer-root file overrides always win when set (see resolve_viewer_cwd).
VIEWER_CANDIDATES = [
    # additional roots via env or state/control/viewer-root (no hardcoded host paths)
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


def viewer_root_config_path(
    *,
    state_root: Path | str | None = None,
    root: Path | str | None = None,
) -> Path:
    """Repo-local gitignored path for a stable viewer root override."""
    return control_root(resolve_state_root(state_root, root=root)) / VIEWER_ROOT_FILENAME


def _read_viewer_root_file(
    *,
    state_root: Path | str | None = None,
    root: Path | str | None = None,
) -> str | None:
    path = viewer_root_config_path(state_root=state_root, root=root)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return line
    return None


def _viewer_root_candidates(
    *,
    state_root: Path | str | None = None,
    root: Path | str | None = None,
) -> list[tuple[str, str]]:
    """Ordered (source, path) overrides before VIEWER_CANDIDATES."""
    out: list[tuple[str, str]] = []
    herdr = os.environ.get("HERDR_BROWSER_ROOT", "").strip()
    if herdr:
        out.append(("HERDR_BROWSER_ROOT", herdr))
    browserctl = os.environ.get("BROWSERCTL_VIEWER_ROOT", "").strip()
    if browserctl:
        out.append(("BROWSERCTL_VIEWER_ROOT", browserctl))
    file_root = _read_viewer_root_file(state_root=state_root, root=root)
    if file_root:
        out.append((f"file:{viewer_root_config_path(state_root=state_root, root=root)}", file_root))
    return out


def resolve_viewer_cwd(
    *,
    require_capable: bool = True,
    state_root: Path | str | None = None,
    root: Path | str | None = None,
) -> Path:
    """
    Resolve herdr-browser root that supports observe_mirror.

    Priority:
      1. HERDR_BROWSER_ROOT
      2. BROWSERCTL_VIEWER_ROOT
      3. state/control/viewer-root (repo-local, gitignored)
      4. VIEWER_CANDIDATES that pass the capability probe

    Fail closed if none capable. Explicit overrides that fail the probe error
    immediately (do not silently fall through).
    """
    tried: list[dict[str, Any]] = []
    for source, raw in _viewer_root_candidates(state_root=state_root, root=root):
        path = Path(raw).expanduser()
        probe = probe_observe_mirror(path)
        probe = {**probe, "source": source}
        tried.append(probe)
        if probe["ok"]:
            return path
        if require_capable:
            raise AdapterError(
                f"{source} is set but is not observe_mirror-capable "
                "(fail closed). Point it at a tree with observe_mirror support "
                "(pre-merge worktree is OK if capable).",
                probe=probe,
            )

    for cand in VIEWER_CANDIDATES:
        probe = probe_observe_mirror(cand)
        probe = {**probe, "source": "candidate"}
        tried.append(probe)
        if probe["ok"]:
            return cand

    raise AdapterError(
        "no observe_mirror-capable herdr-browser root found; set "
        "HERDR_BROWSER_ROOT, BROWSERCTL_VIEWER_ROOT, or write a path to "
        f"{viewer_root_config_path(state_root=state_root, root=root)} "
        "(pre-merge worktrees acceptable when they pass the capability probe)",
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


def _pane_missing_signal(message: str, *, stderr: str = "", stdout: str = "") -> bool:
    """True when herdr output indicates the pane id is already gone."""
    blob = " ".join(
        x for x in (message or "", stderr or "", stdout or "") if x
    ).lower()
    if not blob:
        return False
    needles = (
        "not found",
        "unknown pane",
        "no such pane",
        "invalid pane",
        "pane does not exist",
        "unknown pane_id",
    )
    return any(n in blob for n in needles)


def pane_get(
    pane_id: str,
    *,
    endpoint: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch herdr pane get envelope (raises AdapterError on failure)."""
    return _run_herdr(["pane", "get", str(pane_id)], endpoint=endpoint or {})


def verify_pane_absent(
    pane_id: str,
    *,
    endpoint: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Evidence-based absence check for a pane id.

    Returns {absent, evidence, error?} — absent is True only when herdr
    clearly reports the pane is gone. Ambiguous herdr failures → absent False
    (fail closed).
    """
    endpoint = endpoint or {}
    try:
        resp = pane_get(pane_id, endpoint=endpoint)
    except AdapterError as e:
        stderr = str((e.details or {}).get("stderr") or "")
        stdout = str((e.details or {}).get("stdout") or "")
        if _pane_missing_signal(e.message, stderr=stderr, stdout=stdout):
            return {
                "absent": True,
                "evidence": "not_found",
                "pane_id": str(pane_id),
                "error": e.message,
            }
        return {
            "absent": False,
            "evidence": "verify_error",
            "pane_id": str(pane_id),
            "error": e.message,
            "stderr": stderr[-500:] if stderr else None,
        }

    found_id = _extract_pane_id(resp if isinstance(resp, dict) else {})
    if found_id and str(found_id) == str(pane_id):
        return {
            "absent": False,
            "evidence": "still_present",
            "pane_id": str(pane_id),
            "found_pane_id": str(found_id),
        }
    # Successful get without matching pane_id is ambiguous — fail closed.
    if found_id:
        return {
            "absent": False,
            "evidence": "unexpected_pane",
            "pane_id": str(pane_id),
            "found_pane_id": str(found_id),
        }
    return {
        "absent": False,
        "evidence": "unparseable_get",
        "pane_id": str(pane_id),
        "response_keys": sorted(resp.keys()) if isinstance(resp, dict) else [],
    }


def close_pane(
    pane_id: str,
    *,
    endpoint: dict[str, str] | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """
    Close a herdr pane and return structured proof.

    Does **not** swallow errors. ``closed`` is True only when absence is
    verified (or close is skipped because the pane was already absent).
    Callers must inspect the return value — never assume success.
    """
    endpoint = endpoint or {}
    pid = str(pane_id)
    result: dict[str, Any] = {
        "pane_id": pid,
        "close_submitted": False,
        "closed": False,
        "already_absent": False,
        "error": None,
        "verify": None,
    }

    close_error: str | None = None
    close_details: dict[str, Any] = {}
    try:
        _run_herdr(["pane", "close", pid], endpoint=endpoint)
        result["close_submitted"] = True
    except AdapterError as e:
        close_error = e.message
        close_details = dict(e.details or {})
        stderr = str(close_details.get("stderr") or "")
        stdout = str(close_details.get("stdout") or "")
        # If herdr says the pane is already gone, treat as successful no-op
        # only after verify (below) confirms absence.
        result["close_error"] = close_error
        result["close_stderr"] = stderr[-500:] if stderr else None
        if not verify and _pane_missing_signal(close_error, stderr=stderr, stdout=stdout):
            result["closed"] = True
            result["already_absent"] = True
            result["evidence"] = "close_not_found"
            return result

    if not verify:
        # Explicit no-verify path still refuses to claim closed without evidence.
        result["closed"] = False
        result["error"] = close_error or "close_submitted_unverified"
        result["evidence"] = "unverified"
        return result

    absence = verify_pane_absent(pid, endpoint=endpoint)
    result["verify"] = {
        "absent": absence.get("absent"),
        "evidence": absence.get("evidence"),
        "error": absence.get("error"),
    }
    if absence.get("absent"):
        result["closed"] = True
        result["already_absent"] = not result["close_submitted"]
        result["evidence"] = absence.get("evidence")
        result["error"] = None
        return result

    # Still present or verify ambiguous — fail closed, surface errors.
    result["closed"] = False
    result["evidence"] = absence.get("evidence") or "still_present"
    err_parts = [p for p in (close_error, absence.get("error")) if p]
    if absence.get("evidence") == "still_present":
        err_parts.append(f"pane {pid!r} still present after close")
    result["error"] = "; ".join(err_parts) if err_parts else (
        f"pane {pid!r} close not confirmed"
    )
    return result


def pane_process_info(
    pane_id: str,
    *,
    endpoint: dict[str, str],
) -> dict[str, Any]:
    """Fetch herdr pane process-info envelope (best-effort structured)."""
    return _run_herdr(["pane", "process-info", "--pane", pane_id], endpoint=endpoint)


def _process_info_blob(resp: dict[str, Any]) -> dict[str, Any]:
    """Normalize herdr process-info JSON into a flat process_info dict."""
    if not isinstance(resp, dict):
        return {}
    result = resp.get("result") if isinstance(resp.get("result"), dict) else resp
    if not isinstance(result, dict):
        return {}
    info = result.get("process_info")
    if isinstance(info, dict):
        return info
    # Some envelopes put fields at the result root.
    if "foreground_processes" in result or "shell_pid" in result:
        return result
    return {}


def _viewer_markers_in_text(text: str) -> list[str]:
    low = text.lower()
    return [m for m in _VIEWER_PROCESS_MARKERS if m.lower() in low]


def viewer_process_started(process_info: dict[str, Any] | None) -> dict[str, Any]:
    """
    Decide whether pane process-info shows the observe_mirror viewer running.

    Matches bun/node running viewer.ts / herdr-browser markers in foreground
    process name/argv/cmdline. Returns {ok, matched?, pids?, sample?}.
    """
    info = process_info if isinstance(process_info, dict) else {}
    procs = info.get("foreground_processes") or []
    if not isinstance(procs, list):
        procs = []
    matched: list[str] = []
    pids: list[int] = []
    samples: list[str] = []
    for proc in procs:
        if not isinstance(proc, dict):
            continue
        parts: list[str] = []
        for key in ("name", "argv0", "cmdline"):
            val = proc.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val)
        argv = proc.get("argv")
        if isinstance(argv, list):
            parts.extend(str(a) for a in argv if a is not None)
        blob = " ".join(parts)
        if blob:
            samples.append(blob[:200])
        hits = _viewer_markers_in_text(blob)
        if hits:
            matched.extend(hits)
            pid = proc.get("pid")
            if isinstance(pid, int):
                pids.append(pid)
    # Deduplicate while preserving order
    seen: set[str] = set()
    matched_u = []
    for m in matched:
        if m not in seen:
            seen.add(m)
            matched_u.append(m)
    return {
        "ok": bool(matched_u),
        "matched": matched_u,
        "pids": pids,
        "shell_pid": info.get("shell_pid"),
        "sample": samples[:3],
        "foreground_count": len(procs),
    }


def wait_for_viewer_process(
    pane_id: str,
    *,
    endpoint: dict[str, str],
    timeout_s: float = DEFAULT_VIEWER_START_TIMEOUT_S,
    poll_s: float = DEFAULT_VIEWER_START_POLL_S,
) -> dict[str, Any]:
    """
    Poll herdr pane process-info until the viewer appears or timeout.

    Raises AdapterError with last process snapshot on failure.
    """
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    poll = max(0.05, float(poll_s))
    attempts = 0
    started = time.monotonic()
    last_check: dict[str, Any] = {"ok": False, "error": "not probed"}
    last_error: str | None = None

    while True:
        attempts += 1
        try:
            last_resp = pane_process_info(pane_id, endpoint=endpoint)
            info = _process_info_blob(last_resp)
            last_check = viewer_process_started(info)
            if last_check.get("ok"):
                return {
                    "ok": True,
                    "pane_id": pane_id,
                    "attempts": attempts,
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "process": last_check,
                }
            last_error = None
        except AdapterError as e:
            last_error = e.message
            last_check = {"ok": False, "error": e.message, "details": e.details}

        if time.monotonic() >= deadline:
            break
        time.sleep(poll)

    raise AdapterError(
        "watch viewer process did not start in pane "
        f"{pane_id!r} within {float(timeout_s):g}s",
        pane_id=pane_id,
        timeout_s=float(timeout_s),
        attempts=attempts,
        elapsed_s=round(time.monotonic() - started, 3),
        process=last_check,
        last_error=last_error,
        hint=(
            "confirm herdr pane run launched bun src/viewer.ts with "
            "HERDR_BROWSER_VIEWER_WATCH_RESIZE=1; close orphan split panes"
        ),
    )


def normalize_viewport_mode(mode: str | None) -> str:
    """Accept fixed|follow-pane|preserve; default fixed."""
    if mode is None or str(mode).strip() == "":
        return DEFAULT_VIEWPORT_MODE
    value = str(mode).strip().lower().replace("_", "-")
    # Aliases from earlier flag naming / boolean follow opt-in.
    if value in ("follow", "follow-pane-viewport", "dynamic", "pane"):
        value = "follow-pane"
    if value in ("forensic", "readonly-layout", "keep"):
        value = "preserve"
    if value not in VIEWPORT_MODES:
        raise InvalidRequest(
            f"viewport must be one of {sorted(VIEWPORT_MODES)} (got {mode!r})",
            viewport=mode,
        )
    return value


def normalize_viewport_dim(value: int | float | str | None, *, name: str) -> int | None:
    """Optional positive int override for fixed viewport width/height."""
    if value is None or value == "":
        return None
    try:
        dim = int(value)
    except (TypeError, ValueError) as e:
        raise InvalidRequest(f"invalid {name}: {value!r}") from e
    if dim <= 0:
        raise InvalidRequest(f"{name} must be a positive integer (got {dim})")
    return dim


def build_mirror_env(
    *,
    cdp_url: str,
    target_state_path: str | Path,
    viewer_root: Path | str | None = None,
    viewport: str | None = None,
    viewport_width: int | float | str | None = None,
    viewport_height: int | float | str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """
    Env for observe_mirror watch panes.

    Default viewport=fixed → HERDR_BROWSER_VIEWPORT_MODE=fixed with
    WIDTH/HEIGHT 1150x902 so herdr-browser applies device metrics once and
    keeps layout stable while WATCH_RESIZE contain-fits the frame on pane
    resize. follow-pane omits fixed dims, sets MODE=follow-pane and legacy
    FOLLOW_PANE_VIEWPORT=1 so the page reflows with the terminal. preserve
    never mutates layout. WATCH_RESIZE stays on in all modes for the graphics
    stream loop.
    """
    mode = normalize_viewport_mode(viewport)
    width = normalize_viewport_dim(viewport_width, name="viewport_width")
    height = normalize_viewport_dim(viewport_height, name="viewport_height")
    if mode != "fixed" and (width is not None or height is not None):
        raise InvalidRequest(
            "viewport-width/height only apply to --viewport fixed",
            viewport=mode,
            viewport_width=width,
            viewport_height=height,
        )
    if mode == "fixed":
        width = width if width is not None else DEFAULT_VIEWPORT_WIDTH
        height = height if height is not None else DEFAULT_VIEWPORT_HEIGHT

    env: dict[str, str] = {
        "HERDR_BROWSER_MODE": "observe_mirror",
        "HERDR_BROWSER_TARGET_STATE": str(target_state_path),
        "HERDR_BROWSER_CDP_URL": str(cdp_url).rstrip("/"),
        # Bound frames to the capture raster. Unbounded screenshots preserve the
        # external browser's larger viewport and Herdr clips them on small panes.
        "HERDR_BROWSER_CAPTURE_BACKEND": "screencast",
        "HERDR_BROWSER_CAPTURE_SCALE": "1",
        # Canonical viewport policy consumed by herdr-browser observe_mirror.
        "HERDR_BROWSER_VIEWPORT_MODE": mode,
        # Required for live resize + graphics stream loop in herdr-browser
        # viewer (shouldWatchResize). Without this, daemon metrics stay
        # graphics_stream.active=false / frames=0 after a one-shot render.
        # Placement contain-fits; layout mutation is governed by VIEWPORT_MODE.
        "HERDR_BROWSER_VIEWER_WATCH_RESIZE": "1",
    }
    if mode == "fixed":
        env["HERDR_BROWSER_VIEWPORT_WIDTH"] = str(width)
        env["HERDR_BROWSER_VIEWPORT_HEIGHT"] = str(height)
    elif mode == "follow-pane":
        # Legacy companion flag: older herdr-browser builds only understood
        # FOLLOW_PANE_VIEWPORT. New builds prefer VIEWPORT_MODE=follow-pane.
        env["HERDR_BROWSER_FOLLOW_PANE_VIEWPORT"] = "1"
    if viewer_root is not None:
        env["HERDR_BROWSER_ROOT"] = str(viewer_root)
    if extra:
        env.update(extra)
    return env


def normalize_ratio(ratio: float | None) -> float:
    """Clamp herdr first-child ratio into the valid (0.1, 0.9) band."""
    if ratio is None:
        return DEFAULT_RATIO
    try:
        value = float(ratio)
    except (TypeError, ValueError) as e:
        raise InvalidRequest(f"invalid watch ratio: {ratio!r}") from e
    if not (value == value) or value <= 0 or value >= 1:  # NaN / out of range
        raise InvalidRequest(
            f"watch ratio must be between 0 and 1 exclusive (got {value})",
            ratio=value,
        )
    # Match herdr valid_split_ratio clamp so CLI and runtime agree.
    return max(0.1, min(0.9, value))


def cdp_version_ok(cdp_url: str, *, timeout: float = 2.0) -> dict[str, Any]:
    """Probe CDP HTTP /json/version. Returns {ok, status?, error?, body_keys?}."""
    url = str(cdp_url).rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
            status = getattr(resp, "status", 200) or 200
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return {
                    "ok": False,
                    "status": status,
                    "error": "cdp /json/version returned non-JSON",
                }
            if status != 200:
                return {"ok": False, "status": status, "error": f"HTTP {status}"}
            return {
                "ok": True,
                "status": status,
                "body_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}




def cdp_list_targets(cdp_url: str, *, timeout: float = 2.0) -> dict[str, Any]:
    """Fetch CDP HTTP /json/list. Returns {ok, targets?, ids?, error?, status?}."""
    url = str(cdp_url).rstrip("/") + "/json/list"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
            status = getattr(resp, "status", 200) or 200
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return {
                    "ok": False,
                    "status": status,
                    "error": "cdp /json/list returned non-JSON",
                }
            if status != 200:
                return {"ok": False, "status": status, "error": f"HTTP {status}"}
            if not isinstance(payload, list):
                return {
                    "ok": False,
                    "status": status,
                    "error": "cdp /json/list returned non-list",
                }
            ids: list[str] = []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                tid = item.get("id")
                if isinstance(tid, str) and tid.strip():
                    ids.append(tid.strip())
            return {
                "ok": True,
                "status": status,
                "count": len(payload),
                "ids": ids,
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def cdp_list_has_target(
    list_probe: dict[str, Any] | None,
    target_id: str | None,
) -> bool:
    """True when /json/list succeeded and contains target_id."""
    if not isinstance(list_probe, dict) or not list_probe.get("ok"):
        return False
    if not isinstance(target_id, str) or not target_id.strip():
        return False
    ids = list_probe.get("ids") or []
    if not isinstance(ids, list):
        return False
    want = target_id.strip()
    return any(isinstance(i, str) and i.strip() == want for i in ids)


def read_target_state_snapshot(path: Path | str) -> dict[str, Any] | None:
    """Best-effort read of active-target.json (None if missing/unreadable)."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def target_state_is_ready(
    doc: dict[str, Any] | None,
    *,
    expected_cdp_url: str | None = None,
) -> bool:
    """
    Ready when harness published a non-null active_target_id.

    A null id is the cold-start / cleared state — opening observe_mirror on it
    yields a permanent detached/about:blank mirror. Optional CDP URL match is
    soft (warn via diagnostics) so path-only mismatches do not block forever.
    """
    if not isinstance(doc, dict):
        return False
    tid = doc.get("active_target_id")
    if not isinstance(tid, str) or not tid.strip():
        return False
    if expected_cdp_url:
        published = str(doc.get("cdp_url") or "").rstrip("/")
        expect = str(expected_cdp_url).rstrip("/")
        # If publisher omitted cdp_url, still accept non-null target id.
        if published and expect and published != expect:
            return False
    return True


def wait_for_watch_readiness(
    *,
    cdp_url: str,
    target_state_path: str | Path,
    timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    poll_s: float = DEFAULT_READY_POLL_S,
    require_target_id: bool = True,
    require_cdp_list_match: bool = True,
) -> dict[str, Any]:
    """
    Block until CDP is up and (by default) active-target has a live target id.

    Ready when:
      1. CDP /json/version is 200
      2. active-target.json has a non-null active_target_id (and optional cdp match)
      3. that id exists in CDP /json/list (rejects stale published state)

    Returns a readiness record. Raises AdapterError with bounded diagnostics
    when the deadline expires without settling.
    """
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    poll = max(0.05, float(poll_s))
    target_path = Path(target_state_path)
    last_cdp: dict[str, Any] = {"ok": False, "error": "not probed"}
    last_list: dict[str, Any] = {"ok": False, "error": "not probed"}
    last_doc: dict[str, Any] | None = None
    attempts = 0
    started = time.monotonic()

    while True:
        attempts += 1
        last_cdp = cdp_version_ok(cdp_url)
        last_doc = read_target_state_snapshot(target_path)
        cdp_ready = bool(last_cdp.get("ok"))
        target_ready = (
            target_state_is_ready(last_doc, expected_cdp_url=cdp_url)
            if require_target_id
            else last_doc is not None
        )
        list_ready = True
        active_id = (
            (last_doc or {}).get("active_target_id")
            if isinstance(last_doc, dict)
            else None
        )
        if require_cdp_list_match and require_target_id and target_ready:
            last_list = cdp_list_targets(cdp_url)
            list_ready = cdp_list_has_target(
                last_list, active_id if isinstance(active_id, str) else None
            )
        elif not require_cdp_list_match:
            last_list = {"ok": True, "skipped": True, "ids": []}

        if cdp_ready and target_ready and list_ready:
            return {
                "ok": True,
                "cdp": last_cdp,
                "cdp_list": {
                    "ok": bool(last_list.get("ok")),
                    "count": last_list.get("count"),
                    "matched_id": active_id if isinstance(active_id, str) else None,
                    "skipped": bool(last_list.get("skipped")),
                },
                "target_state": last_doc,
                "target_state_path": str(target_path),
                "attempts": attempts,
                "elapsed_s": round(time.monotonic() - started, 3),
                "active_target_id": active_id,
            }

        if time.monotonic() >= deadline:
            break
        time.sleep(poll)

    # Build a compact diagnostic — never dump secrets (target state has none).
    reasons: list[str] = []
    if not last_cdp.get("ok"):
        reasons.append(
            f"cdp not ready ({last_cdp.get('error') or last_cdp.get('status') or 'unknown'})"
        )
    if require_target_id and not target_state_is_ready(last_doc, expected_cdp_url=cdp_url):
        if last_doc is None:
            if target_path.exists():
                reasons.append(
                    f"active-target unreadable or invalid at {target_path}"
                )
            else:
                reasons.append(
                    f"active-target missing at {target_path} "
                    "(daemon has not published yet)"
                )
        else:
            tid = last_doc.get("active_target_id")
            if tid is None or tid == "":
                reasons.append(
                    "active_target_id is null/empty — refusing permanent "
                    "about:blank observe_mirror; wait for harness publish"
                )
            else:
                reasons.append(
                    f"active-target not ready (id={tid!r}, "
                    f"cdp_url={last_doc.get('cdp_url')!r})"
                )
    elif require_cdp_list_match and require_target_id:
        tid = (last_doc or {}).get("active_target_id") if isinstance(last_doc, dict) else None
        if not last_list.get("ok"):
            reasons.append(
                "cdp /json/list not ready ("
                f"{last_list.get('error') or last_list.get('status') or 'unknown'})"
            )
        elif not cdp_list_has_target(last_list, tid if isinstance(tid, str) else None):
            sample = last_list.get("ids") or []
            sample_s = ",".join(str(x) for x in sample[:5]) if isinstance(sample, list) else ""
            reasons.append(
                f"active_target_id {tid!r} missing from cdp /json/list "
                f"(stale target state?; list_count={last_list.get('count')}"
                + (f", sample=[{sample_s}]" if sample_s else "")
                + ")"
            )

    raise AdapterError(
        "watch readiness timed out: "
        + ("; ".join(reasons) if reasons else "target/CDP never settled"),
        timeout_s=float(timeout_s),
        attempts=attempts,
        elapsed_s=round(time.monotonic() - started, 3),
        cdp_url=str(cdp_url).rstrip("/"),
        cdp=last_cdp,
        cdp_list={
            "ok": bool(last_list.get("ok")),
            "error": last_list.get("error"),
            "status": last_list.get("status"),
            "count": last_list.get("count"),
            "ids_sample": (last_list.get("ids") or [])[:8]
            if isinstance(last_list.get("ids"), list)
            else [],
        },
        target_state_path=str(target_path),
        target_state={
            "present": last_doc is not None,
            "active_target_id": (last_doc or {}).get("active_target_id"),
            "seq": (last_doc or {}).get("seq"),
            "cdp_url": (last_doc or {}).get("cdp_url"),
            "worker_id": (last_doc or {}).get("worker_id"),
            "page_url": ((last_doc or {}).get("page") or {}).get("url")
            if isinstance((last_doc or {}).get("page"), dict)
            else None,
        },
        reasons=reasons,
        hint=(
            "ensure the lease adapter started the daemon and it published "
            "state/<worker>/control/active-target.json with a non-null "
            "active_target_id that still exists in CDP /json/list; "
            "do not seed a null stub"
        ),
    )


def start_watch(
    lease: dict[str, Any],
    *,
    state_root: Path | str,
    agent_pane: str | None = None,
    ratio: float = DEFAULT_RATIO,
    direction: str = "right",
    herdr_session: str | None = None,
    herdr_socket: str | None = None,
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    ready_poll_s: float = DEFAULT_READY_POLL_S,
    viewport: str | None = None,
    viewport_width: int | float | str | None = None,
    viewport_height: int | float | str | None = None,
) -> dict[str, Any]:
    """
    Split the navigator's existing pane and run observe_mirror viewer.

    Waits for CDP + non-null active_target_id present in /json/list before
    splitting. Sets HERDR_BROWSER_VIEWER_WATCH_RESIZE=1 on the mirror pane.
    Default viewport=fixed locks layout at 1150x902; follow-pane reflows with
    the terminal. Verifies the viewer process started; closes the new pane on
    failure. Never seeds a null target stub. Binds watch pane ids + herdr
    endpoint onto the returned watch record.
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
    # Ensure parent exists for the daemon publisher, but do NOT write a null
    # active_target_id stub — that freezes observe_mirror on about:blank.
    Path(target_path).parent.mkdir(parents=True, exist_ok=True)

    readiness = wait_for_watch_readiness(
        cdp_url=str(cdp_url),
        target_state_path=target_path,
        timeout_s=ready_timeout_s,
        poll_s=ready_poll_s,
        require_target_id=True,
    )

    ratio = normalize_ratio(ratio)

    agent = resolve_agent_pane(agent_pane, endpoint=endpoint)
    viewer_cwd = resolve_viewer_cwd(require_capable=True)
    probe = probe_observe_mirror(viewer_cwd)
    if not probe["ok"]:
        raise AdapterError(
            "viewer root failed observe_mirror capability probe (fail closed)",
            probe=probe,
        )

    viewport_mode = normalize_viewport_mode(viewport)
    fixed_width = normalize_viewport_dim(viewport_width, name="viewport_width")
    fixed_height = normalize_viewport_dim(viewport_height, name="viewport_height")
    mirror_env = build_mirror_env(
        cdp_url=cdp_url,
        target_state_path=target_path,
        viewer_root=viewer_cwd,
        viewport=viewport_mode,
        viewport_width=fixed_width,
        viewport_height=fixed_height,
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
    try:
        run_in_pane(watch_pane, cmd, endpoint=endpoint)
        viewer_start = wait_for_viewer_process(
            watch_pane,
            endpoint=endpoint,
            timeout_s=DEFAULT_VIEWER_START_TIMEOUT_S,
            poll_s=DEFAULT_VIEWER_START_POLL_S,
        )
    except AdapterError as e:
        # Roll back the newly split pane so failures do not leave orphans.
        close_result = close_pane(watch_pane, endpoint=endpoint)
        raise AdapterError(
            f"watch pane started then failed: {e.message}",
            watch_pane_id=watch_pane,
            closed_on_failure=bool(close_result.get("closed")),
            close_on_failure=close_result,
            **(e.details or {}),
        ) from e

    viewport_meta: dict[str, Any] = {"mode": viewport_mode}
    if viewport_mode == "fixed":
        viewport_meta["width"] = int(
            mirror_env.get("HERDR_BROWSER_VIEWPORT_WIDTH", DEFAULT_VIEWPORT_WIDTH)
        )
        viewport_meta["height"] = int(
            mirror_env.get("HERDR_BROWSER_VIEWPORT_HEIGHT", DEFAULT_VIEWPORT_HEIGHT)
        )

    return {
        "agent_pane_id": agent,
        "watch_pane_id": watch_pane,
        "direction": direction,
        "ratio": ratio,
        "viewport": viewport_meta,
        "viewer_cwd": str(viewer_cwd),
        "viewer_command": cmd,
        "viewer_probe": {
            "ok": True,
            "root": str(viewer_cwd),
            "has_observe_mirror": True,
        },
        "viewer_start": {
            "ok": True,
            "elapsed_s": viewer_start.get("elapsed_s"),
            "attempts": viewer_start.get("attempts"),
            "matched": (viewer_start.get("process") or {}).get("matched"),
            "pids": (viewer_start.get("process") or {}).get("pids"),
        },
        "env": mirror_env,
        "target_state_path": target_path,
        "cdp_url": cdp_url,
        "readiness": {
            "ok": True,
            "elapsed_s": readiness.get("elapsed_s"),
            "attempts": readiness.get("attempts"),
            "active_target_id": readiness.get("active_target_id"),
            "cdp_list": readiness.get("cdp_list"),
        },
        "herdr_socket": endpoint.get("herdr_socket"),
        "herdr_session": endpoint.get("herdr_session"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def stop_watch(lease: dict[str, Any], *, close: bool = True) -> dict[str, Any]:
    """
    Stop/unbind watch for a lease.

    ``closed`` is True only with evidence the watch pane is gone (or there was
    no pane to close). Never claims closed on a swallowed herdr error.
    """
    watch = lease.get("watch") or {}
    pane = watch.get("watch_pane_id")
    endpoint: dict[str, str] = {}
    endpoint_error: str | None = None
    if watch.get("herdr_socket") or watch.get("herdr_session") or os.environ.get(
        "HERDR_SOCKET_PATH"
    ) or os.environ.get("HERDR_SESSION"):
        try:
            endpoint = resolve_herdr_endpoint(
                herdr_session=watch.get("herdr_session"),
                herdr_socket=watch.get("herdr_socket"),
                lease_watch=watch,
            )
        except InvalidRequest as e:
            endpoint_error = e.message
            endpoint = {}

    result: dict[str, Any] = {
        "watch_pane_id": pane,
        "closed": False,
        "close_requested": bool(close),
        "agent_pane_id": watch.get("agent_pane_id"),
        "herdr_socket": watch.get("herdr_socket") or endpoint.get("herdr_socket"),
        "herdr_session": watch.get("herdr_session") or endpoint.get("herdr_session"),
        "close": None,
        "error": None,
    }

    if not pane:
        # Nothing bound — vacuously settled.
        result["closed"] = True
        result["evidence"] = "no_watch_pane"
        return result

    if not close:
        # Unbind-only: do not claim the pane was closed.
        result["closed"] = False
        result["evidence"] = "unbind_only"
        return result

    if endpoint_error and not endpoint:
        # Try env-only close still, but never claim success without verify.
        result["endpoint_error"] = endpoint_error

    close_result = close_pane(str(pane), endpoint=endpoint or {})
    result["close"] = {
        "closed": close_result.get("closed"),
        "close_submitted": close_result.get("close_submitted"),
        "already_absent": close_result.get("already_absent"),
        "evidence": close_result.get("evidence"),
        "error": close_result.get("error"),
        "verify": close_result.get("verify"),
    }
    if close_result.get("closed"):
        result["closed"] = True
        result["evidence"] = close_result.get("evidence") or "verified_absent"
        result["error"] = None
        return result

    result["closed"] = False
    result["evidence"] = close_result.get("evidence") or "close_unconfirmed"
    result["error"] = close_result.get("error") or (
        f"watch pane {pane!r} close not confirmed"
    )
    return result
