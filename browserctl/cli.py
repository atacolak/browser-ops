#!/usr/bin/env python3
"""
browserctl — thin human+agent CLI for browser session leases.

Commands:
  list | status | acquire | release | watch | unwatch | reap | launch | spawn
  profiles list|show|register|associate|resolve

JSON mode: pass --json (stable for agents/orchestrators).
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


def cmd_acquire(args: argparse.Namespace) -> int:
    m = _mgr(args)
    req = {
        "kind": args.kind,
        "owner": args.owner,
        "mode": args.mode,
        "ttl": args.ttl,
        "email": args.email,
        "worker_id": args.worker,
        "worker": args.worker,
        "label": args.label,
        "cdp_port": args.cdp_port,
        "country": args.country,
        "city": args.city,
        "headless": not args.headed,
        "no_start": args.no_start,
        "attach_only": args.attach_only,
        "force": args.force if hasattr(args, "force") else False,
    }
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
    req = {
        "kind": args.kind,
        "owner": args.owner,
        "mode": args.mode,
        "ttl": args.ttl,
        "email": args.email,
        "worker_id": args.worker,
        "worker": args.worker,
        "label": args.label,
        "cdp_port": args.cdp_port,
        "country": args.country,
        "city": args.city,
        "headless": not args.headed,
        "no_start": args.no_start,
        "attach_only": args.attach_only,
        "watch": args.watch,
        "agent_pane": args.agent_pane,
        "ratio": args.ratio,
        "herdr_session": getattr(args, "herdr_session", None),
        "herdr_socket": getattr(args, "herdr_socket", None),
    }
    out = m.launch(req)
    # launch always prefers JSON-shaped env for orchestrators when --json
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


def _add_launch_selector_flags(p: argparse.ArgumentParser, *, kind_required: bool = True) -> None:
    """Persistent launch selector flags for profile register (not lease runtime)."""
    p.add_argument(
        "--kind",
        required=kind_required,
        choices=["xai", "scratch", "adhoc", "vpn"],
        help="lifecycle adapter / launch kind",
    )
    p.add_argument("--email", help="xai identity email")
    p.add_argument("--worker", help="worker id (vpn/scratch optional)")
    p.add_argument("--label", help="scratch label")
    p.add_argument("--cdp-port", type=int, default=None)
    p.add_argument("--country", help="vpn country")
    p.add_argument("--city", help="vpn city")
    p.add_argument("--headed", action="store_true", help="scratch: headed browser")
    p.add_argument("--no-start", action="store_true", help="bind only; do not launch")
    p.add_argument(
        "--attach-only",
        action="store_true",
        help="vpn: only attach existing runtime",
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
    ac.add_argument(
        "--kind",
        required=True,
        choices=["xai", "scratch", "adhoc", "vpn"],
        help="lifecycle adapter",
    )
    ac.add_argument("--owner", default="operator")
    ac.add_argument(
        "--mode",
        default="persistent",
        choices=["persistent", "one_shot"],
        help="persistent: orchestrator releases; one_shot: shorter TTL",
    )
    ac.add_argument("--ttl", type=float, default=None)
    ac.add_argument("--email", help="xai identity email")
    ac.add_argument("--worker", help="worker id (vpn/scratch optional)")
    ac.add_argument("--label", help="scratch label")
    ac.add_argument("--cdp-port", type=int, default=None)
    ac.add_argument("--country", help="vpn country")
    ac.add_argument("--city", help="vpn city")
    ac.add_argument("--headed", action="store_true", help="scratch: headed browser")
    ac.add_argument("--no-start", action="store_true", help="bind only; do not launch")
    ac.add_argument(
        "--attach-only",
        action="store_true",
        help="vpn: only attach existing runtime",
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
    w.add_argument("--ratio", type=float, default=0.42)
    w.add_argument(
        "--direction",
        default="right",
        choices=["right", "down"],
    )
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

    rp = sub.add_parser("reap", help="reap expired leases", parents=[shared])
    rp.add_argument("--lease", help="force-reap one lease id")
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
            help="acquire (+ optional --watch) and print navigator env JSON",
            parents=[shared],
        )
        lp.add_argument(
            "--kind",
            required=True,
            choices=["xai", "scratch", "adhoc", "vpn"],
        )
        lp.add_argument("--owner", default="operator")
        lp.add_argument(
            "--mode",
            default="persistent",
            choices=["persistent", "one_shot"],
        )
        lp.add_argument("--ttl", type=float, default=None)
        lp.add_argument("--email")
        lp.add_argument("--worker")
        lp.add_argument("--label")
        lp.add_argument("--cdp-port", type=int, default=None)
        lp.add_argument("--country")
        lp.add_argument("--city")
        lp.add_argument("--headed", action="store_true")
        lp.add_argument("--no-start", action="store_true")
        lp.add_argument("--attach-only", action="store_true")
        lp.add_argument(
            "--watch",
            action="store_true",
            help="also split watch pane for observe_mirror",
        )
        lp.add_argument("--agent-pane")
        lp.add_argument("--herdr-session", default=None)
        lp.add_argument("--herdr-socket", default=None)
        lp.add_argument("--ratio", type=float, default=0.42)
        lp.set_defaults(func=cmd_launch)

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

    For nested ``profiles <sub>``, globals attach after the profiles subcommand
    token (second position).
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
    # Nested: profiles <sub> … → insert globals after both tokens when present
    if rest[0] == "profiles" and len(rest) >= 2 and not rest[1].startswith("-"):
        return [rest[0], rest[1], *globals_opts, *rest[2:]]
    return [rest[0], *globals_opts, *rest[1:]]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(_normalize_argv(raw))
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
