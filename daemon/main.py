#!/usr/bin/env python3
"""
Browser Harness Daemon — persistent CDP WebSocket holder + IPC relay.

Usage::

    # Preferred ad-hoc start (debug only)
    PYTHONPATH=. BROWSER_ALLOW_EVALUATE=1 \
      python3 -m daemon.main --worker default --launch --headless --cdp-port 9333

    # Also works as a bare script (self-heals package path for from daemon.*)
    BROWSER_ALLOW_EVALUATE=1 python3 daemon/main.py --worker default --launch ...

    # Diagnostics (offline)
    python3 -m daemon.main --doctor

    # Leased work
    ./bin/browserctl launch --kind scratch --label demo --json

One daemon per worker.  The daemon holds a CDP connection to CloakBrowser,
listens on a Unix socket (or TCP) for agent commands, and relays them to
the browser.

Architecture::

    Pi Agent (TypeScript ext)
         │
         ▼  Unix socket IPC (JSON-line protocol)
    main.py ─── rpc.py (router + error envelopes)
         │
         ├── backend/cloak.py (CloakBackend → CDPClient → WebSocket)
         ├── primitives/ (standalone CDP building blocks)
         ├── events.py (JSONL spool to state/<worker>/events/)
         └── schema/rpc-v1.json (versioned RPC contract)
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Path bootstrap so both entry styles work:
#   python3 daemon/main.py ...
#   python3 -m daemon.main ...
# Sibling imports (from backend..., from rpc...) need daemon/ on sys.path.
# Absolute package imports (from daemon.provenance ...) need browser-ops root.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
# Keep env contract discoverable for tools/children even on bare script start.
os.environ.setdefault("BROWSER_OPS_ROOT", str(_ROOT))
# Prefer browser-ops root on PYTHONPATH without clobbering an existing value.
_existing_pp = os.environ.get("PYTHONPATH", "")
if str(_ROOT) not in _existing_pp.split(os.pathsep):
    os.environ["PYTHONPATH"] = (
        str(_ROOT) if not _existing_pp else str(_ROOT) + os.pathsep + _existing_pp
    )

from backend.cloak import CloakBackend
from doctor import run_doctor
from events import EventBus
from rpc import run_ipc_server

# Default socket dir: browser-ops/state
DEFAULT_SOCKET_DIR = _ROOT / "state"


async def main():
    parser = argparse.ArgumentParser(
        description="Browser Harness Daemon — persistent CDP + IPC relay"
    )
    parser.add_argument(
        "--worker",
        default="default",
        help="Worker ID (default: %(default)s)",
    )
    parser.add_argument(
        "--cdp-url",
        help="CDP endpoint URL (e.g. http://127.0.0.1:9222 or ws://...)",
    )
    parser.add_argument(
        "--socket",
        help="Unix socket path for IPC (default: state/<worker>/daemon.sock)",
    )
    parser.add_argument(
        "--tcp",
        type=int,
        default=0,
        help="TCP port for IPC (0 = Unix socket only, not yet implemented)",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run diagnostics and print JSON report to stdout",
    )
    parser.add_argument(
        "--no-events",
        action="store_true",
        help="Disable JSONL event spooling",
    )
    # ── Launch options (use cloakbrowser wrapper instead of raw subprocess) ──
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Launch the browser via cloakbrowser wrapper instead of connecting to an existing one",
    )
    parser.add_argument(
        "--profile-dir",
        help="Browser profile directory (for --launch mode)",
    )
    parser.add_argument(
        "--proxy-url",
        help="Proxy URL for browser traffic (for --launch mode)",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        help="CDP debugging port (for --launch mode; overrides manifest/default)",
    )
    parser.add_argument(
        "--human-preset",
        choices=["default", "careful"],
        default="default",
        help="Humanization preset (default: %(default)s)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (no visible window)",
    )
    parser.add_argument(
        "--no-humanize",
        action="store_true",
        help="Disable Bézier humanization (for debugging)",
    )
    parser.add_argument(
        "--no-geoip",
        action="store_true",
        help="Disable geoip auto-detection of timezone/locale",
    )
    parser.add_argument(
        "--unmanaged",
        action="store_true",
        help="Doctor/debug: skip target-lease enforcement on the socket (not for leased work)",
    )
    args = parser.parse_args()

    # ── Doctor mode ─────────────────────────────────────────────────────
    if args.doctor:
        cdp_url = args.cdp_url or "http://127.0.0.1:9222"
        socket_path = args.socket or str(DEFAULT_SOCKET_DIR / args.worker / "daemon.sock")
        run_doctor(
            cdp_url=cdp_url,
            socket_path=socket_path,
            worker=args.worker,
            state_dir=DEFAULT_SOCKET_DIR,
        )
        return  # run_doctor calls sys.exit()

    # ── Normal operation ────────────────────────────────────────────────
    cdp_url = args.cdp_url or "http://127.0.0.1:9222"
    socket_path = args.socket or str(DEFAULT_SOCKET_DIR / args.worker / "daemon.sock")

    # Ensure state directories exist
    Path(socket_path).parent.mkdir(parents=True, exist_ok=True)

    # Socket permissions: run_ipc_server (rpc.py) creates the Unix socket and
    # immediately chmods it to 0o600 (owner read/write only).  This prevents
    # other local users from sending browser control commands.  Do not relax
    # that mode without an explicit threat-model review.

    # Create backend (with optional profile/port for launch mode)
    backend = CloakBackend(
        args.worker,
        profile_dir=args.profile_dir,
        cdp_port=args.cdp_port,
        unmanaged=args.unmanaged,
    )

    if args.launch:
        await backend.launch(
            proxy_url=args.proxy_url,
            humanize=not args.no_humanize,
            geoip=not args.no_geoip,
            human_preset=args.human_preset,
            headless=args.headless,
        )
    else:
        await backend.connect(cdp_url)

    # Optionally create event bus
    event_bus = None
    if not args.no_events:
        event_bus = EventBus(args.worker, DEFAULT_SOCKET_DIR)

    # Run IPC server (blocking)
    await run_ipc_server(
        backend=backend,
        socket_path=socket_path,
        event_bus=event_bus,
    )


if __name__ == "__main__":
    asyncio.run(main())
