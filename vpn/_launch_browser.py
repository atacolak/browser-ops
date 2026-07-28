#!/usr/bin/env python3
"""
_launch_browser.py — start a CloakBrowser with proxy + extension, keep it alive.

Internal helper invoked by spawn_vpn_browser.py. Runs in its own process
group, holds the browser context open, and parks until SIGTERM/SIGINT
arrives (then closes the context cleanly).
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path


async def main_async(args: argparse.Namespace) -> int:
    from cloakbrowser import (
        launch_persistent_context_async,
        ProxySettings,
    )

    profile_dir = Path(args.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # clean stale singleton lock from previous crashes
    singleton_lock = profile_dir / "SingletonLock"
    if singleton_lock.is_symlink() or singleton_lock.exists():
        try:
            singleton_lock.unlink()
        except OSError:
            pass

    proxy = ProxySettings(server=args.proxy_url) if args.proxy_url else None

    # NOTE: cloakbrowser's stealth args set --disable-extensions by default,
    # which silently nullifies --load-extension. The cloakbrowser API exposes
    # `extension_paths` which it loads through its own mechanism (it re-enables
    # loading for OUR supplied extensions while keeping the stealth posture on
    # everything else). Always prefer the API over raw args for extensions.
    extension_paths: list[str] | None = None
    if args.extension:
        ext_abs = str(Path(args.extension).resolve())
        if not Path(ext_abs, "manifest.json").is_file():
            raise SystemExit(f"extension dir {ext_abs} has no manifest.json")
        extension_paths = [ext_abs]

    extra_args = [
        "--fingerprint-webrtc-ip=auto",
        f"--remote-debugging-port={args.cdp_port}",
        # Allow script/agent CDP clients (navigator daemon + ad-hoc ws tools)
        # to drive this browser. Without this, modern chromium rejects
        # incoming websocket connections from non-devtools frontends
        # with HTTP 403.
        "--remote-allow-origins=*",
    ]

    print(
        f"[{args.worker_id}] launching cloakbrowser\n"
        f"  profile: {profile_dir}\n"
        f"  proxy:   {args.proxy_url}\n"
        f"  cdp:     {args.cdp_port}\n"
        f"  ext:     {args.extension or 'none'}",
        file=sys.stderr,
        flush=True,
    )

    context = await launch_persistent_context_async(
        user_data_dir=str(profile_dir),
        headless=False,  # headed; rendered on the Xvfb display provided via $DISPLAY
        proxy=proxy,
        args=extra_args,
        extension_paths=extension_paths,
        humanize=True,
        geoip=False,  # we already know the proxy geography; DNS-over-https handled by socks5h
        human_preset="default",
        viewport={"width": 1440, "height": 900},
    )

    # open an initial blank page so the user has something to see
    pages = context.pages
    page = pages[0] if pages else await context.new_page()
    if not pages:
        await page.goto("about:blank")

    stop_event = asyncio.Event()

    def _stop(*_a: object) -> None:
        stop_event.set()

    # the parent process sends SIGTERM on teardown
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    print(
        f"[{args.worker_id}] browser ready on display + cdp port {args.cdp_port}; "
        f"parking until SIGTERM.",
        file=sys.stderr,
        flush=True,
    )

    try:
        await stop_event.wait()
    finally:
        print(f"[{args.worker_id}] closing browser…", file=sys.stderr)
        try:
            await context.close()
        except Exception as e:
            print(f"[{args.worker_id}] close error: {e}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile-dir", required=True)
    p.add_argument("--proxy-url", required=True)
    p.add_argument("--cdp-port", type=int, required=True)
    p.add_argument("--worker-id", required=True)
    p.add_argument("--extension", help="path to MV3 extension dir to load via cloakbrowser API")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())