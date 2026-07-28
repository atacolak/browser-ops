"""
``--doctor`` — offline diagnostics for the browser harness.

Checks:
  - Unix socket directory writable
  - CDP endpoint reachable (HTTP GET /json/version)
  - Browser type / version detected
  - Profile data directory exists
  - No stale lockfiles

Prints a structured JSON report to stdout and exits.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path


def run_doctor(
    cdp_url: str,
    socket_path: str | None,
    worker: str,
    state_dir: Path,
) -> None:
    """Execute all diagnostic checks and print a JSON report."""
    report: dict[str, object] = {
        "doctor": True,
        "worker": worker,
        "checks": [],
        "summary": {"passed": 0, "failed": 0, "warnings": 0},
    }

    def _check(name: str, passed: bool, detail: str, severity: str = "error"):
        entry = {"name": name, "passed": passed, "detail": detail}
        report["checks"].append(entry)  # type: ignore[arg-type]
        if passed:
            report["summary"]["passed"] += 1  # type: ignore[index]
        elif severity == "warning":
            entry["severity"] = "warning"
            report["summary"]["warnings"] += 1  # type: ignore[index]
        else:
            report["summary"]["failed"] += 1  # type: ignore[index]

    # ── 1. CDP endpoint reachable ──────────────────────────────────────
    try:
        with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=5) as r:
            data = json.loads(r.read())
            browser = data.get("Browser", "unknown")
            ua = data.get("User-Agent", "unknown")
            ws_url = data.get("webSocketDebuggerUrl", "unknown")
            _check(
                "cdp_endpoint",
                True,
                f"Browser={browser} WS={ws_url}",
            )
            _check(
                "browser_version",
                True,
                f"Browser: {browser[:80]} UA: {ua[:60]}...",
            )
    except Exception as e:
        _check("cdp_endpoint", False, f"CDP connection failed: {e}")

    # ── 2. Socket directory writable ───────────────────────────────────
    if socket_path:
        sock_dir = Path(socket_path).parent
    else:
        sock_dir = state_dir / worker
    try:
        sock_dir.mkdir(parents=True, exist_ok=True)
        test_file = sock_dir / ".doctor_writable_test"
        test_file.write_text("ok")
        test_file.unlink()
        _check("socket_dir_writable", True, str(sock_dir))
    except Exception as e:
        _check("socket_dir_writable", False, f"Cannot write to {sock_dir}: {e}")

    # ── 3. Profile directory ───────────────────────────────────────────
    profile_dir = Path(f"/tmp/pi-agent-browser-worker-{worker}")
    if profile_dir.exists():
        _check("profile_dir", True, str(profile_dir))
    else:
        _check(
            "profile_dir",
            False,
            f"Profile dir {profile_dir} not found (may be created on launch)",
            severity="warning",
        )

    # ── 4. Stale lockfiles ─────────────────────────────────────────────
    lock_path = sock_dir / "daemon.lock"
    if lock_path.exists():
        _check(
            "stale_lockfile",
            False,
            f"Stale lockfile at {lock_path} — will be cleaned on start",
            severity="warning",
        )
    else:
        _check("stale_lockfile", True, "No stale lockfile")

    # ── 5. Stale socket ────────────────────────────────────────────────
    sock_path = sock_dir / "daemon.sock"
    if sock_path.exists():
        _check(
            "stale_socket",
            False,
            f"Stale socket at {sock_path} — will be cleaned on start",
            severity="warning",
        )
    else:
        _check("stale_socket", True, "No stale socket")

    # ── Print report ───────────────────────────────────────────────────
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["summary"]["failed"] == 0 else 1)  # type: ignore[operator]
