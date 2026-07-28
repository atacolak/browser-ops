#!/usr/bin/env python3
"""
spawn_vpn_browser.py — reductionist geo-located browser capability.

Spin up a CloakBrowser whose traffic egresses through a Mullvad WireGuard
tunnel in a chosen region. Browser runs headed on a virtual X display
mirrored to NoVNC over the tailscale IP. Optionally loads a Chrome
extension so content-script interception (e.g. the Cassia mock) boots at
document_start without any manual paste.

Designed for agents/operators to invoke when a browser needs to appear
from a particular geography. No cpa-farm entanglement, no provider auth
state, no standing topology — start, use, stop.

Usage:
    spawn_vpn_browser.py start <worker_id> <country> <city> \\
        [--extension <dir>] [--cdp-port <port>] [--socks-port <port>] \\
        [--novnc-port <port>] [--display :N]
    spawn_vpn_browser.py stop <worker_id>
    spawn_vpn_browser.py status

Output (start): JSON with socks5_url, novnc_magic_url, display,
cdp_port, profile_dir, worker_id, egress_info.

Setup:
  browser-ops/vpn/.wireguard.env  (gitignored) — holds
  WIREGUARD_PRIVATE_KEY and WIREGUARD_ADDRESSES. Template written on
  first run. Reuse one Mullvad key across regions: account-scoped,
  region-free.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BROWSER_OPS_ROOT = Path(__file__).resolve().parents[1]
VPN_DIR = BROWSER_OPS_ROOT / "vpn"
WG_ENV_FILE = VPN_DIR / ".wireguard.env"
WG_ENV_TEMPLATE = """\
# Mullvad WireGuard credentials (account-scoped, region-free).
# Get values from `mullvad account get` + /etc/mullvad-vpn/device.json,
# OR reuse cpa-farm .env MULLVAD_WORKER_B_* values (valid through 2026-09).
WIREGUARD_PRIVATE_KEY=
WIREGUARD_ADDRESSES=
"""
PROFILE_ROOT = BROWSER_OPS_ROOT / "profiles" / "vpn"
STATE_ROOT = BROWSER_OPS_ROOT / "state"
EXTENSIONS_ROOT = BROWSER_OPS_ROOT / "extensions"
RUNTIME_FILE = STATE_ROOT / "vpn_runtime.json"
TAILSCALE_IP_FALLBACK = "100.68.60.39"
LAUNCH_BROWSER_PY = VPN_DIR / "_launch_browser.py"
BROWSER_LOG_DIR = STATE_ROOT / "vpn_browser_logs"


def _die(msg: str, code: int = 1) -> "NoReturn":
    print(f"spawn_vpn_browser: {msg}", file=sys.stderr)
    sys.exit(code)


def _load_runtime() -> dict[str, Any]:
    if RUNTIME_FILE.exists():
        try:
            return json.loads(RUNTIME_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_runtime(data: dict[str, Any]) -> None:
    RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_FILE.write_text(json.dumps(data, indent=2))


def _ensure_wg_env() -> None:
    if WG_ENV_FILE.exists():
        text = WG_ENV_FILE.read_text()
        for key in ("WIREGUARD_PRIVATE_KEY", "WIREGUARD_ADDRESSES"):
            for line in text.splitlines():
                if line.startswith(f"{key}=") and line.split("=", 1)[1].strip():
                    return
    WG_ENV_FILE.write_text(WG_ENV_TEMPLATE)
    _die(
        f"wrote skeleton {WG_ENV_FILE}. Fill in WIREGUARD_PRIVATE_KEY and "
        f"WIREGUARD_ADDRESSES, then re-run. "
        f"(Hint: reuse cpa-farm .env MULLVAD_WORKER_B_* values.)"
    )


def _read_wg_env() -> dict[str, str]:
    _ensure_wg_env()
    env: dict[str, str] = {}
    for line in WG_ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    if not env.get("WIREGUARD_PRIVATE_KEY") or not env.get(
        "WIREGUARD_ADDRESSES"
    ):
        _die(f"missing WIREGUARD_PRIVATE_KEY/ADDRESSES in {WG_ENV_FILE}")
    return env


def _tailscale_ip() -> str:
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, check=True
        )
        ip = out.stdout.strip()
        if ip:
            return ip
    except Exception:
        pass
    return TAILSCALE_IP_FALLBACK


def _kill_pid(pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    time.sleep(0.5)
    try:
        if os.path.exists(f"/proc/{pid}"):
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _stop_vpn_compose(worker_id: str) -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(VPN_DIR / "docker-compose.vpn.yml"),
            "-p",
            f"browser-ops-vpn-{worker_id}",
            "down",
            "--remove-orphans",
        ],
        capture_output=True,
        text=True,
    )


def _cleanup_runtime(worker_id: str, runtime: dict[str, Any]) -> None:
    """Kill procs, drop display locks, remove compose stack, clear JSON row."""
    inst = runtime.get(worker_id)
    if not inst:
        return
    for pid in (inst.get("browser_pid"), inst.get("xvfb_pid"),
                inst.get("x11vnc_pid"), inst.get("websockify_pid")):
        if pid:
            _kill_pid(int(pid))
    display = inst.get("display")
    if display:
        lock = Path(f"/tmp/.X{display.lstrip(':')}-lock")
        if lock.exists():
            try:
                lock.unlink()
            except OSError:
                pass
    _stop_vpn_compose(worker_id)
    runtime.pop(worker_id, None)
    _save_runtime(runtime)


def cmd_start(args: argparse.Namespace) -> int:
    worker_id = args.worker_id
    country = args.country
    city = args.city
    socks_port = args.socks_port or 10801
    display = args.display or ":97"
    cdp_port = args.cdp_port or 9320
    novnc_port = args.novnc_port or 6080
    vnc_port = novnc_port - 1
    ext_dir = args.extension
    novnc_password = args.novnc_password or secrets.token_urlsafe(8)
    profile_dir = PROFILE_ROOT / worker_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    BROWSER_LOG_DIR.mkdir(parents=True, exist_ok=True)

    display_num = display.lstrip(":")
    x_lock = Path(f"/tmp/.X{display_num}-lock")
    if x_lock.exists():
        _die(
            f"X lock {x_lock} already exists — display {display} busy. "
            f"Pass --display :<N> for a different number."
        )

    runtime = _load_runtime()
    if worker_id in runtime:
        print(
            f"spawn_vpn_browser: worker {worker_id} already running. stop it first.",
            file=sys.stderr,
        )
        return 1

    wg_env = _read_wg_env()

    # 1. VPN — gluetun compose up
    print(f"[{worker_id}] starting vpn ({country}/{city})…", file=sys.stderr)
    vpn_port_capture = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(VPN_DIR / "docker-compose.vpn.yml"),
            "-p",
            f"browser-ops-vpn-{worker_id}",
            "up",
            "-d",
        ],
        env={
            **os.environ,
            "COUNTRY": country,
            "CITY": city,
            "SOCKS_PORT": str(socks_port),
            "WIREGUARD_PRIVATE_KEY": wg_env["WIREGUARD_PRIVATE_KEY"],
            "WIREGUARD_ADDRESSES": wg_env["WIREGUARD_ADDRESSES"],
        },
        capture_output=True,
        text=True,
    )
    if vpn_port_capture.returncode != 0:
        print(vpn_port_capture.stdout, file=sys.stderr)
        print(vpn_port_capture.stderr, file=sys.stderr)
        _die(f"vpn compose up failed")

    # 1b. Verify SOCKS5 actually reaches the chosen region
    print(f"[{worker_id}] verifying egress via socks5://127.0.0.1:{socks_port}…", file=sys.stderr)
    probe_ok = False
    egress_info: Any = None
    for attempt in range(8):
        probe = subprocess.run(
            [
                "curl",
                "-sS",
                "-m",
                "8",
                "-x",
                f"socks5h://127.0.0.1:{socks_port}",
                "https://am.i.mullvad.net/json",
            ],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            try:
                egress_info = json.loads(probe.stdout)
                probe_ok = True
                break
            except Exception:
                pass
        time.sleep(2)
    if not probe_ok:
        _cleanup_runtime(worker_id, runtime)
        _die(
            f"socks5 probe failed after retries: "
            f"{probe.stderr.strip() or probe.stdout.strip()}"
        )
    egress_country = str(egress_info.get("country", "?"))
    if (
        country.lower() not in egress_country.lower()
        and not args.skip_egress_check
    ):
        _cleanup_runtime(worker_id, runtime)
        _die(
            f"egress country mismatch: wanted {country}, got {egress_country}. "
            f"info={egress_info}. abort. "
            f"(pass --skip-egress-check to override.)"
        )
    print(
        f"[{worker_id}] egress verified: {egress_country} via "
        f"{egress_info.get('ip','?')} ({egress_info.get('city','?')})",
        file=sys.stderr,
    )

    # 2. virtual X display
    print(f"[{worker_id}] starting Xvfb on display {display}…", file=sys.stderr)
    xvfb_proc = subprocess.Popen(
        [
            "Xvfb",
            display,
            "-screen",
            "0",
            "1600x1200x24",
            "-ac",
            "+extension",
            "RANDR",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    time.sleep(1.5)
    if xvfb_proc.poll() is not None:
        _cleanup_runtime(worker_id, runtime)
        _die(f"Xvfb failed to start on {display}")

    # 3. x11vnc — mirror the display, password-protected, localhost-only
    print(f"[{worker_id}] starting x11vnc on {vnc_port}…", file=sys.stderr)
    x11vnc_proc = subprocess.Popen(
        [
            "x11vnc",
            "-display",
            display,
            "-rfbport",
            str(vnc_port),
            "-passwd",
            novnc_password,
            "-shared",
            "-forever",
            "-localhost",
            "-bg",
            "-quiet",
        ],
        env={**os.environ, "DISPLAY": display},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    time.sleep(2.0)

    # 4. websockify — serve NoVNC, bridge to VNC raw port
    print(f"[{worker_id}] starting websockify on {novnc_port}…", file=sys.stderr)
    websockify_proc = subprocess.Popen(
        [
            "websockify",
            "--web",
            "/usr/share/novnc",
            str(novnc_port),
            f"localhost:{vnc_port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(1.5)

    ts_ip = _tailscale_ip()
    magic_url = (
        f"http://{ts_ip}:{novnc_port}/vnc.html?"
        f"autoconnect=1&password={novnc_password}&"
        f"resize=scale&reconnect=1"
    )

    # 5. CloakBrowser via the helper script (detached, logs to file)
    print(f"[{worker_id}] launching CloakBrowser…", file=sys.stderr)
    proxy_url = f"socks5://127.0.0.1:{socks_port}"
    browser_log = BROWSER_LOG_DIR / f"{worker_id}.log"
    browser_log_fp = browser_log.open("w")
    browser_cmd = [
        sys.executable,
        str(LAUNCH_BROWSER_PY),
        "--profile-dir",
        str(profile_dir),
        "--proxy-url",
        proxy_url,
        "--cdp-port",
        str(cdp_port),
        "--worker-id",
        worker_id,
    ]
    if ext_dir:
        ext_abs = str(Path(ext_dir).resolve())
        if not Path(ext_abs, "manifest.json").is_file():
            _cleanup_runtime(worker_id, runtime)
            _die(f"extension dir {ext_abs} has no manifest.json")
        browser_cmd += ["--extension", ext_abs]
    browser_proc = subprocess.Popen(
        browser_cmd,
        env={
            **os.environ,
            "DISPLAY": display,
            "BROWSER_ALLOW_EVALUATE": "1",
            "PYTHONPATH": str(BROWSER_OPS_ROOT),
        },
        stdout=browser_log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    runtime[worker_id] = {
        "country": country,
        "city": city,
        "socks_port": socks_port,
        "display": display,
        "cdp_port": cdp_port,
        "novnc_port": novnc_port,
        "vnc_port": vnc_port,
        "novnc_password": novnc_password,
        "profile_dir": str(profile_dir),
        "egress_info": egress_info,
        "xvfb_pid": xvfb_proc.pid,
        "x11vnc_pid": x11vnc_proc.pid,
        "websockify_pid": websockify_proc.pid,
        "browser_pid": browser_proc.pid,
        "browser_log": str(browser_log),
        "started_at_unix": int(time.time()),
        "novnc_magic_url": magic_url,
    }
    _save_runtime(runtime)

    print(
        f"\n=== {worker_id} ready ===\n"
        f"region:      {country}/{city} ({egress_info.get('country','?')})\n"
        f"socks5:      socks5://127.0.0.1:{socks_port}\n"
        f"cdp:         http://127.0.0.1:{cdp_port}\n"
        f"profile:     {profile_dir}\n"
        f"NoVNC URL:   {magic_url}\n"
        f"VNC pass:    {novnc_password}\n"
        f"browser log: {browser_log}\n",
        file=sys.stderr,
    )
    print(json.dumps(runtime[worker_id], indent=2))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    runtime = _load_runtime()
    if args.worker_id not in runtime:
        # check if user meant a different existing worker
        print(
            f"spawn_vpn_browser: no runtime row for {args.worker_id}; "
            f"running: {list(runtime.keys()) or 'none'}",
            file=sys.stderr,
        )
        return 1
    print(f"[{args.worker_id}] stopping…", file=sys.stderr)
    _cleanup_runtime(args.worker_id, runtime)
    print(f"[{args.worker_id}] stopped.", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    runtime = _load_runtime()
    if not runtime:
        print("no vpn browsers running")
        return 0
    for wid, inst in runtime.items():
        live = "yes" if (
            inst.get("browser_pid") and Path(f"/proc/{inst['browser_pid']}").exists()
        ) else "dead"
        print(
            f"{wid:24s} region={inst.get('country'):8s}/{inst.get('city','?'):14s} "
            f"novnc_port={inst.get('novnc_port')} cdp={inst.get('cdp_port')} "
            f"browser={live}"
        )
        print(f"    URL: {inst.get('novnc_magic_url')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="spin up vpn browser stack")
    s.add_argument("worker_id", help="e.g. claude-eu")
    s.add_argument("country", help="e.g. germany, sweden")
    s.add_argument("city", help="e.g. frankfurt, stockholm")
    s.add_argument("--extension", help="path to a Chrome MV3 extension dir")
    s.add_argument("--cdp-port", type=int)
    s.add_argument("--socks-port", type=int)
    s.add_argument("--novnc-port", type=int)
    s.add_argument("--display", help="X display e.g. :97")
    s.add_argument("--skip-egress-check", action="store_true")
    s.add_argument("--novnc-password", help="override random VNC password")
    s.set_defaults(func=cmd_start)

    st = sub.add_parser("stop", help="tear down vpn browser stack")
    st.add_argument("worker_id")
    st.set_defaults(func=cmd_stop)

    q = sub.add_parser("status")
    q.set_defaults(func=cmd_status)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())