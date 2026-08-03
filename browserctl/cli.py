#!/usr/bin/env python3
"""
browserctl — thin human+agent CLI for browser session leases.

Commands:
  list | status | acquire | release | watch | unwatch | reap | launch | spawn
  navigator spawn | navigator cleanup | navigator status
  profiles list|show|register|associate|resolve

JSON mode: pass --json (stable for agents/orchestrators).

Orchestrator-facing navigator lifecycle is resource-oriented:
  browserctl navigator spawn … --json
  browserctl navigator cleanup --lease ID|--name NAME --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure browser-ops root on path for identity_ops + daemon.*
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from browserctl.errors import BrowserctlError  # noqa: E402
from browserctl.manager import Manager  # noqa: E402
from browserctl.profiles import ProfileRegistry  # noqa: E402


def _print(data: Any, *, as_json: bool, text_fn=None) -> None:
    if as_json or text_fn is None:
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
    else:
        text_fn(data)


def _global_parent() -> argparse.ArgumentParser:
    """Shared flags; subparsers must inherit so `--json` works after the subcommand."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "--root",
        default=None,
        help="browser-ops root (default: repo root or BROWSER_OPS_ROOT)",
    )
    p.add_argument(
        "--state-root",
        default=None,
        help="state root override (default: <root>/state or BROWSERCTL_STATE_ROOT)",
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    return p


def _mgr(args: argparse.Namespace) -> Manager:
    return Manager(root=args.root, state_root=args.state_root)


def _profiles(args: argparse.Namespace) -> ProfileRegistry:
    return ProfileRegistry(root=args.root)


def cmd_list_fixed(args: argparse.Namespace) -> int:
    m = _mgr(args)
    rows = m.list_leases(include_terminal=args.all)
    if args.json:
        print(json.dumps({"ok": True, "leases": rows}, indent=2, sort_keys=True, default=str))
        return 0
    if not rows:
        print("(no active leases)")
        return 0
    for r in rows:
        exp = " expired" if r.get("expired") else ""
        print(
            f"{r.get('lease_id')}  {str(r.get('worker_id') or '-'):28}  "
            f"{str(r.get('kind') or '-'):8}  {r.get('status')}{exp}  "
            f"owner={r.get('owner')}"
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    m = _mgr(args)
    out = m.status(lease_id=args.lease, worker_id=args.worker)
    print(json.dumps({"ok": True, **out} if args.json else out, indent=2, sort_keys=True, default=str))
    return 0


_SELECTOR_DESTS = (
    "kind", "email", "worker", "label", "cdp_port",
    "country", "city", "headed", "no_start", "attach_only",
)


def _request_from_launch_args(args: argparse.Namespace, *, for_launch: bool = False) -> dict[str, Any]:
    """Build acquire/launch request (unset selector flags use argparse.SUPPRESS)."""
    req: dict[str, Any] = {
        "owner": args.owner,
        "mode": args.mode,
        "ttl": args.ttl,
    }
    if getattr(args, "auto_reap", False):
        req["auto_reap"] = True
    if getattr(args, "profile", None):
        req["profile_name"] = args.profile
    else:
        for dest in _SELECTOR_DESTS:
            if getattr(args, dest, None) is None:
                continue
            val = getattr(args, dest)
            req[dest] = val
            if dest == "worker":
                req["worker_id"] = val
        # Legacy default when --headed omitted: headless browser.
        if "headed" in req:
            req["headed"] = bool(req["headed"])
        else:
            req["headless"] = True
        for flag in ("no_start", "attach_only"):
            if flag in req:
                req[flag] = bool(req[flag])

    if for_launch:
        req["watch"] = bool(getattr(args, "watch", False))
        req["agent_pane"] = getattr(args, "agent_pane", None)
        req["ratio"] = getattr(args, "ratio", None)
        req["ready_timeout"] = getattr(args, "ready_timeout", None)
        req["herdr_session"] = getattr(args, "herdr_session", None)
        req["herdr_socket"] = getattr(args, "herdr_socket", None)
        # Viewport modes are normalized by watch.py; CLI only passes values.
        if getattr(args, "viewport", None) is not None:
            req["viewport"] = args.viewport
        if getattr(args, "viewport_width", None) is not None:
            req["viewport_width"] = args.viewport_width
        if getattr(args, "viewport_height", None) is not None:
            req["viewport_height"] = args.viewport_height
    return req


def cmd_acquire(args: argparse.Namespace) -> int:
    m = _mgr(args)
    req = _request_from_launch_args(args, for_launch=False)
    out = m.acquire(req)
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True, default=str))
    else:
        lease = out["lease"]
        print(f"lease_id:  {lease['lease_id']}")
        print(f"worker_id: {lease['worker_id']}")
        print(f"kind:      {lease['kind']}")
        print(f"status:    {lease['status']}")
        print(f"expires:   {lease.get('expires_at_iso')}")
        print("env:")
        for k, v in (out.get("env") or {}).items():
            print(f"  {k}={v}")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    m = _mgr(args)
    out = m.release(
        lease_id=args.lease,
        worker_id=args.worker,
        force=args.force,
        keep_watch=args.keep_watch,
    )
    print(json.dumps(out, indent=2, sort_keys=True, default=str) if args.json else json.dumps(out, indent=2, default=str))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    m = _mgr(args)
    out = m.watch(
        lease_id=args.lease,
        worker_id=args.worker,
        agent_pane=args.agent_pane,
        ratio=args.ratio,
        direction=args.direction,
        herdr_session=args.herdr_session,
        herdr_socket=args.herdr_socket,
        ready_timeout_s=getattr(args, "ready_timeout", None),
        viewport=getattr(args, "viewport", None),
        viewport_width=getattr(args, "viewport_width", None),
        viewport_height=getattr(args, "viewport_height", None),
    )
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


def cmd_unwatch(args: argparse.Namespace) -> int:
    m = _mgr(args)
    out = m.unwatch(
        lease_id=args.lease,
        worker_id=args.worker,
        close=not args.keep_pane,
    )
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    m = _mgr(args)
    out = m.reap(force_lease_id=args.lease, dry_run=args.dry_run)
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


def cmd_mark_exit(args: argparse.Namespace) -> int:
    m = _mgr(args)
    out = m.mark_expiring(lease_id=args.lease, ttl_seconds=args.ttl)
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    m = _mgr(args)
    req = _request_from_launch_args(args, for_launch=True)
    out = m.launch(req)
    # launch always prefers JSON-shaped env for orchestrators when --json
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


# ── navigator lifecycle (orchestrator-only seam) ─────────────────────────────


def cmd_navigator_spawn(args: argparse.Namespace) -> int:
    m = _mgr(args)
    req = _request_from_launch_args(args, for_launch=True)
    # Navigator-specific knobs (not part of bare launch contract).
    if getattr(args, "name", None):
        req["name"] = args.name
    if getattr(args, "model", None):
        req["model"] = args.model
    if getattr(args, "thinking", None):
        req["thinking"] = args.thinking
    if getattr(args, "split", None):
        req["split"] = args.split
    if getattr(args, "navigator_lifecycle", None):
        req["navigator_lifecycle"] = args.navigator_lifecycle
    if getattr(args, "workspace", None):
        req["workspace"] = args.workspace
    if getattr(args, "tab", None):
        req["tab"] = args.tab
    if getattr(args, "agent_ctl", None):
        req["agent_ctl"] = args.agent_ctl
    out = m.navigator_spawn(req)
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


def cmd_navigator_cleanup(args: argparse.Namespace) -> int:
    m = _mgr(args)
    out = m.navigator_cleanup(
        lease_id=args.lease,
        name=args.name,
        force=bool(getattr(args, "force", False)),
        keep_watch=bool(getattr(args, "keep_watch", False)),
        skip_navigator_close=bool(getattr(args, "skip_navigator_close", False)),
    )
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    # Non-zero when not settled so finally/retry loops can detect incomplete cleanup.
    return 0 if out.get("ok") else 1


def cmd_navigator_status(args: argparse.Namespace) -> int:
    m = _mgr(args)
    out = m.navigator_status(lease_id=args.lease, name=args.name)
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


# ── named profile registry ───────────────────────────────────────────────────


def cmd_profiles_list(args: argparse.Namespace) -> int:
    reg = _profiles(args)
    rows = reg.list_profiles()
    if args.json:
        print(json.dumps({"ok": True, "profiles": rows}, indent=2, sort_keys=True, default=str))
        return 0
    if not rows:
        print("(no named profiles)")
        return 0
    for r in rows:
        launch = r.get("launch") or {}
        kind = launch.get("kind", "?")
        n_assoc = len(r.get("associations") or [])
        print(f"{r['name']:24}  kind={kind:8}  associations={n_assoc}")
    return 0


def cmd_profiles_show(args: argparse.Namespace) -> int:
    reg = _profiles(args)
    out = reg.show(args.name)
    payload = {"ok": True, "profile": out}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def cmd_profiles_register(args: argparse.Namespace) -> int:
    reg = _profiles(args)
    launch: dict[str, Any] = {"kind": args.kind}
    if args.email:
        launch["email"] = args.email
    if args.worker:
        launch["worker"] = args.worker
    if args.label:
        launch["label"] = args.label
    if args.cdp_port is not None:
        launch["cdp_port"] = args.cdp_port
    if args.country:
        launch["country"] = args.country
    if args.city:
        launch["city"] = args.city
    if args.headed:
        launch["headed"] = True
    if args.attach_only:
        launch["attach_only"] = True
    if args.no_start:
        launch["no_start"] = True
    out = reg.register(
        args.name,
        launch=launch,
        replace=args.replace,
    )
    print(json.dumps({"ok": True, "profile": out}, indent=2, sort_keys=True, default=str))
    return 0


def cmd_profiles_associate(args: argparse.Namespace) -> int:
    reg = _profiles(args)
    out = reg.associate(args.name, args.site, account=args.account)
    print(json.dumps({"ok": True, "profile": out}, indent=2, sort_keys=True, default=str))
    return 0


def cmd_profiles_resolve(args: argparse.Namespace) -> int:
    reg = _profiles(args)
    out = reg.resolve(args.site, account=args.account)
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True, default=str))
    else:
        print(f"name:   {out['name']}")
        print(f"launch: {' '.join(out['launch_argv'])}")
        print(json.dumps({"launch": out["launch"]}, indent=2, sort_keys=True, default=str))
    return 0


def _add_viewport_flags(
    p: argparse.ArgumentParser,
    *,
    with_watch_prefix: bool = False,
) -> None:
    """Shared --viewport / size flags for watch and launch --watch."""
    prefix = "with --watch: " if with_watch_prefix else ""
    p.add_argument(
        "--viewport",
        default=None,
        choices=["fixed", "follow-pane", "preserve"],
        help=(
            f"{prefix}observe_mirror layout policy "
            "(default fixed → HERDR_BROWSER_VIEWPORT_MODE=fixed at 1150x902; "
            "follow-pane reflows page with terminal; preserve never mutates)"
        ),
    )
    p.add_argument(
        "--viewport-width",
        type=int,
        default=None,
        help=f"{prefix}fixed mode width override (default 1150)",
    )
    p.add_argument(
        "--viewport-height",
        type=int,
        default=None,
        help=f"{prefix}fixed mode height override (default 902)",
    )


def _add_launch_selector_flags(
    p: argparse.ArgumentParser,
    *,
    kind_required: bool = True,
    suppress_defaults: bool = False,
) -> None:
    """Selector flags. suppress_defaults → argparse.SUPPRESS (acquire/launch)."""
    d: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress_defaults else {}
    kind_kw: dict[str, Any] = {
        "choices": ["xai", "scratch", "adhoc", "vpn"],
        "help": "lifecycle adapter / launch kind",
        **d,
    }
    if kind_required:
        kind_kw["required"] = True
        kind_kw.pop("default", None)
    p.add_argument("--kind", **kind_kw)
    p.add_argument("--email", help="xai identity email", **d)
    p.add_argument("--worker", help="worker id (vpn/scratch optional)", **d)
    p.add_argument("--label", help="scratch label", **d)
    p.add_argument("--cdp-port", type=int, **({} if suppress_defaults else {"default": None}), **d)
    p.add_argument("--country", help="vpn country", **d)
    p.add_argument("--city", help="vpn city", **d)
    p.add_argument("--headed", action="store_true", help="scratch: headed browser", **d)
    p.add_argument("--no-start", action="store_true", help="bind only; do not launch", **d)
    p.add_argument(
        "--attach-only", action="store_true", help="vpn: only attach existing runtime", **d
    )


def _add_profile_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--profile",
        default=None,
        metavar="NAME",
        help="named profile (profiles show); exclusive with selector flags",
    )


def build_parser() -> argparse.ArgumentParser:
    shared = _global_parent()
    p = argparse.ArgumentParser(
        prog="browserctl",
        description="Browser session lease manager (identity_ops remains canonical for xAI)",
        parents=[shared],
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="list leases", parents=[shared])
    ls.add_argument("--all", action="store_true", help="include released/reaped")
    ls.set_defaults(func=cmd_list_fixed)

    st = sub.add_parser("status", help="status for one lease/worker", parents=[shared])
    st.add_argument("--lease")
    st.add_argument("--worker")
    st.set_defaults(func=cmd_status)

    ac = sub.add_parser(
        "acquire", help="acquire a mutation lease + start adapter", parents=[shared]
    )
    _add_profile_flag(ac)
    _add_launch_selector_flags(ac, kind_required=False, suppress_defaults=True)
    ac.add_argument("--owner", default="operator")
    ac.add_argument(
        "--mode",
        default="persistent",
        choices=["persistent", "one_shot"],
        help="persistent: orchestrator releases; one_shot: shorter TTL + auto-reap",
    )
    ac.add_argument("--ttl", type=float, default=None)
    ac.add_argument(
        "--auto-reap",
        action="store_true",
        help=(
            "mark lease auto-reap eligible for scheduled browserctl reap "
            "(one_shot and expiring leases are already eligible)"
        ),
    )
    ac.set_defaults(func=cmd_acquire)

    rel = sub.add_parser(
        "release", help="release lease and stop resources", parents=[shared]
    )
    rel.add_argument("--lease")
    rel.add_argument("--worker")
    rel.add_argument("--force", action="store_true")
    rel.add_argument(
        "--keep-watch",
        action="store_true",
        help="do not close watch pane",
    )
    rel.set_defaults(func=cmd_release)

    w = sub.add_parser(
        "watch",
        help="split agent pane and run observe_mirror viewer",
        parents=[shared],
    )
    w.add_argument("--lease")
    w.add_argument("--worker")
    w.add_argument(
        "--agent-pane",
        help="navigator herdr pane id (default: HERDR_PANE_ID / pane current)",
    )
    w.add_argument(
        "--herdr-session",
        default=None,
        help="exact herdr session name (or HERDR_SESSION); required with socket if ambiguous",
    )
    w.add_argument(
        "--herdr-socket",
        default=None,
        help="exact herdr socket path (or HERDR_SOCKET_PATH); fail closed if missing",
    )
    w.add_argument(
        "--ratio",
        type=float,
        default=None,
        help=(
            "herdr first-child fraction (direction=right → agent left). "
            "Default 0.37 (agent 37%% / browser 63%%)."
        ),
    )
    w.add_argument(
        "--ready-timeout",
        type=float,
        default=None,
        help="seconds to wait for CDP + non-null active_target_id (default 20)",
    )
    w.add_argument(
        "--direction",
        default="right",
        choices=["right", "down"],
    )
    _add_viewport_flags(w)
    w.set_defaults(func=cmd_watch)

    uw = sub.add_parser(
        "unwatch", help="close watch pane; keep lease", parents=[shared]
    )
    uw.add_argument("--lease")
    uw.add_argument("--worker")
    uw.add_argument(
        "--keep-pane",
        action="store_true",
        help="unbind only; do not close pane",
    )
    uw.set_defaults(func=cmd_unwatch)

    rp = sub.add_parser(
        "reap",
        help=(
            "reap expired auto-reap-eligible leases "
            "(one_shot|expiring|auto_reap); --lease forces one id"
        ),
        parents=[shared],
    )
    rp.add_argument(
        "--lease",
        help="force-reap one lease id (bypasses auto-reap eligibility)",
    )
    rp.add_argument("--dry-run", action="store_true")
    rp.set_defaults(func=cmd_reap)

    mx = sub.add_parser(
        "mark-exit",
        help="navigator exited without release — mark expiring for TTL reap",
        parents=[shared],
    )
    mx.add_argument("--lease", required=True)
    mx.add_argument("--ttl", type=float, default=None)
    mx.set_defaults(func=cmd_mark_exit)

    for name in ("launch", "spawn"):
        lp = sub.add_parser(
            name,
            help=(
                "acquire (+ optional --watch) and print navigator env JSON "
                "(env contract only; prefer 'navigator spawn' to start a pane)"
            ),
            parents=[shared],
        )
        _add_profile_flag(lp)
        _add_launch_selector_flags(lp, kind_required=False, suppress_defaults=True)
        lp.add_argument("--owner", default="operator")
        lp.add_argument(
            "--mode",
            default="persistent",
            choices=["persistent", "one_shot"],
            help="persistent: orchestrator releases; one_shot: shorter TTL + auto-reap",
        )
        lp.add_argument("--ttl", type=float, default=None)
        lp.add_argument(
            "--auto-reap",
            action="store_true",
            help=(
                "mark lease auto-reap eligible for scheduled browserctl reap "
                "(one_shot and expiring leases are already eligible)"
            ),
        )
        lp.add_argument(
            "--watch",
            action="store_true",
            help="also split watch pane for observe_mirror",
        )
        lp.add_argument("--agent-pane")
        lp.add_argument("--herdr-session", default=None)
        lp.add_argument("--herdr-socket", default=None)
        lp.add_argument(
            "--ratio",
            type=float,
            default=None,
            help=(
                "with --watch: herdr first-child fraction "
                "(default 0.37 → agent 37%% / browser 63%%)"
            ),
        )
        lp.add_argument(
            "--ready-timeout",
            type=float,
            default=None,
            help="with --watch: readiness wait seconds (default 20)",
        )
        _add_viewport_flags(lp, with_watch_prefix=True)
        lp.set_defaults(func=cmd_launch)

    # navigator <subcommand> — orchestrator-only resource lifecycle
    nav = sub.add_parser(
        "navigator",
        help=(
            "orchestrator navigator lifecycle: spawn (lease+pane+bind), "
            "cleanup (close+release), status"
        ),
        parents=[shared],
    )
    nsub = nav.add_subparsers(dest="navigator_cmd", required=True)

    nsp = nsub.add_parser(
        "spawn",
        help=(
            "acquire lease, spawn navigator via herdr-agent-ctl with exact env, "
            "persist binding, optional watch; one structured receipt"
        ),
        parents=[shared],
    )
    _add_profile_flag(nsp)
    _add_launch_selector_flags(nsp, kind_required=False, suppress_defaults=True)
    nsp.add_argument("--owner", default="operator")
    nsp.add_argument(
        "--mode",
        default="persistent",
        choices=["persistent", "one_shot"],
        help=(
            "browser lease mode; one_shot → shorter TTL and scheduled-reap "
            "eligible (crash backstop). Prefer one_shot for finite navigator jobs"
        ),
    )
    nsp.add_argument("--ttl", type=float, default=None)
    nsp.add_argument(
        "--auto-reap",
        action="store_true",
        help=(
            "mark lease auto-reap eligible for scheduled browserctl reap "
            "(one_shot already eligible; use with persistent for finite jobs)"
        ),
    )
    nsp.add_argument(
        "--name",
        default=None,
        help="navigator agent name (default: nav-<lease>)",
    )
    nsp.add_argument(
        "--model",
        default=None,
        help="optional model override for navigator pane (default: profile default)",
    )
    nsp.add_argument(
        "--thinking",
        default=None,
        choices=["high", "xhigh", "medium", "low", "minimal"],
    )
    nsp.add_argument(
        "--split",
        default="right",
        choices=["none", "right", "down"],
        help="herdr-agent-ctl pane placement (default right)",
    )
    nsp.add_argument(
        "--navigator-lifecycle",
        default="persistent",
        choices=["one_shot", "persistent"],
        help="navigator agent lifecycle passed to herdr-agent-ctl",
    )
    nsp.add_argument("--workspace", default=None, help="target workspace id")
    nsp.add_argument("--tab", default=None, help="target tab id/label (needs --workspace)")
    nsp.add_argument(
        "--agent-ctl",
        default=None,
        help="path to herdr-agent-ctl (or set BROWSERCTL_HERDR_AGENT_CTL)",
    )
    nsp.add_argument(
        "--watch",
        action="store_true",
        help="open observe_mirror watch after navigator pane exists",
    )
    nsp.add_argument(
        "--agent-pane",
        default=None,
        help="override pane to split for --watch (default: navigator pane)",
    )
    nsp.add_argument("--herdr-session", default=None)
    nsp.add_argument("--herdr-socket", default=None)
    nsp.add_argument(
        "--ratio",
        type=float,
        default=None,
        help="with --watch: herdr first-child fraction (default 0.37)",
    )
    nsp.add_argument(
        "--ready-timeout",
        type=float,
        default=None,
        help="with --watch: readiness wait seconds (default 20)",
    )
    _add_viewport_flags(nsp, with_watch_prefix=True)
    nsp.set_defaults(func=cmd_navigator_spawn)

    ncl = nsub.add_parser(
        "cleanup",
        help=(
            "close navigator + prove watch closed + release lease; "
            "idempotent, safe for finally"
        ),
        parents=[shared],
    )
    ncl.add_argument("--lease", default=None, help="lease id")
    ncl.add_argument(
        "--name",
        default=None,
        help="navigator binding name or pane id",
    )
    ncl.add_argument(
        "--force",
        action="store_true",
        help="release browser even if navigator pane close is unconfirmed",
    )
    ncl.add_argument(
        "--keep-watch",
        action="store_true",
        help="do not close watch pane during release",
    )
    ncl.add_argument(
        "--skip-navigator-close",
        action="store_true",
        help="only release lease/watch (navigator already closed)",
    )
    ncl.set_defaults(func=cmd_navigator_cleanup)

    nst = nsub.add_parser(
        "status",
        help="show lease↔navigator binding",
        parents=[shared],
    )
    nst.add_argument("--lease", default=None)
    nst.add_argument("--name", default=None)
    nst.set_defaults(func=cmd_navigator_status)

    # profiles <subcommand>
    prof = sub.add_parser(
        "profiles",
        help="named persistent profile registry (launch selector + site associations)",
        parents=[shared],
    )
    psub = prof.add_subparsers(dest="profiles_cmd", required=True)

    pl = psub.add_parser("list", help="list named profiles", parents=[shared])
    pl.set_defaults(func=cmd_profiles_list)

    ps = psub.add_parser("show", help="show one named profile", parents=[shared])
    ps.add_argument("name", help="stable profile name")
    ps.set_defaults(func=cmd_profiles_show)

    pr = psub.add_parser(
        "register",
        help="register/update launch selector for a profile name",
        parents=[shared],
    )
    pr.add_argument("name", help="stable profile name (e.g. coal-hattie)")
    _add_launch_selector_flags(pr, kind_required=True)
    pr.add_argument(
        "--replace",
        action="store_true",
        help="overwrite launch if name already exists (keeps associations)",
    )
    pr.set_defaults(func=cmd_profiles_register)

    pa = psub.add_parser(
        "associate",
        help="associate profile with site and optional account label",
        parents=[shared],
    )
    pa.add_argument("name", help="registered profile name")
    pa.add_argument("site", help="site key (e.g. x.ai, cpa-manager)")
    pa.add_argument(
        "account",
        nargs="?",
        default=None,
        help="optional account label (email/handle; not a secret)",
    )
    pa.set_defaults(func=cmd_profiles_associate)

    pv = psub.add_parser(
        "resolve",
        help="exact resolve site[/account] → launch argv (never fuzzy; no browser start)",
        parents=[shared],
    )
    pv.add_argument("site", help="site key to resolve")
    pv.add_argument(
        "--account",
        default=None,
        help="optional exact account label",
    )
    pv.set_defaults(func=cmd_profiles_resolve)

    return p


def _normalize_argv(argv: list[str]) -> list[str]:
    """Allow global flags before or after the subcommand.

    argparse + parents does not reliably accept ``browserctl --json launch …``
    when the same options also live on subparsers. Peel known globals and
    re-attach them after the subcommand token.

    For nested ``profiles <sub>`` / ``navigator <sub>``, globals attach after
    the nested subcommand token (second position).
    """
    if not argv:
        return argv
    globals_opts: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            globals_opts.append(a)
            i += 1
            continue
        if a in ("--root", "--state-root") and i + 1 < len(argv):
            globals_opts.extend([a, argv[i + 1]])
            i += 2
            continue
        if a.startswith("--root=") or a.startswith("--state-root="):
            globals_opts.append(a)
            i += 1
            continue
        rest.append(a)
        i += 1
    if not rest:
        return globals_opts
    # Nested: profiles|navigator <sub> … → insert globals after both tokens
    if (
        rest[0] in ("profiles", "navigator")
        and len(rest) >= 2
        and not rest[1].startswith("-")
    ):
        return [rest[0], rest[1], *globals_opts, *rest[2:]]
    return [rest[0], *globals_opts, *rest[1:]]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(_normalize_argv(raw))
    if getattr(args, "func", None) in (cmd_acquire, cmd_launch, cmd_navigator_spawn):
        has_profile = bool(getattr(args, "profile", None))
        has_kind = getattr(args, "kind", None) is not None
        if not has_profile and not has_kind:
            parser.error(
                "acquire/launch/navigator spawn require --kind or --profile NAME"
            )
        if has_profile:
            used = [
                d
                for d in _SELECTOR_DESTS
                if getattr(args, d, None) is not None
            ]
            if used:
                flags = ", ".join(
                    "--" + d.replace("_", "-") for d in used
                )
                parser.error(
                    f"--profile excludes launch selector flags ({flags})"
                )
    if getattr(args, "func", None) is cmd_navigator_cleanup:
        if not getattr(args, "lease", None) and not getattr(args, "name", None):
            parser.error("navigator cleanup requires --lease or --name")
    if getattr(args, "func", None) is cmd_navigator_status:
        if not getattr(args, "lease", None) and not getattr(args, "name", None):
            parser.error("navigator status requires --lease or --name")
    try:
        return int(args.func(args) or 0)
    except BrowserctlError as e:
        payload = e.to_dict()
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return e.exit_code
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
