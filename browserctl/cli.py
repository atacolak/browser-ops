#!/usr/bin/env python3
"""
browserctl — lease + named-profile CLI.

  list | status | acquire | release | reap | mark-exit | launch
  profiles list|show|register|associate|resolve|cards|card|stamp

JSON: --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

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
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "--root",
        default=None,
        help="browser-ops root (default: this repo, or BROWSER_OPS_ROOT)",
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
    "kind", "worker", "label", "cdp_port",
    "country", "city", "headed", "no_start", "attach_only",
)


def _request_from_launch_args(args: argparse.Namespace) -> dict[str, Any]:
    req: dict[str, Any] = {
        "owner": args.owner,
        "mode": args.mode,
        "ttl": args.ttl,
    }
    if getattr(args, "auto_reap", False):
        req["auto_reap"] = True
    if getattr(args, "profile", None):
        req["profile_name"] = args.profile
        return req
    for dest in _SELECTOR_DESTS:
        if getattr(args, dest, None) is None:
            continue
        val = getattr(args, dest)
        req[dest] = val
        if dest == "worker":
            req["worker_id"] = val
    if "headed" in req:
        req["headed"] = bool(req["headed"])
    else:
        req["headless"] = True
    for flag in ("no_start", "attach_only"):
        if flag in req:
            req[flag] = bool(req[flag])
    return req


def cmd_acquire(args: argparse.Namespace) -> int:
    m = _mgr(args)
    req = _request_from_launch_args(args)
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
    req = _request_from_launch_args(args)
    out = m.launch(req)
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


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
    print(json.dumps({"ok": True, "profile": out}, indent=2, sort_keys=True, default=str))
    return 0


def cmd_profiles_register(args: argparse.Namespace) -> int:
    reg = _profiles(args)
    launch: dict[str, Any] = {"kind": args.kind}
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
    egress = _egress_from_register_args(args)
    if egress is not None:
        launch["egress"] = egress
    out = reg.register(
        args.name,
        launch=launch,
        replace=args.replace,
        description=args.description,
        notes=args.notes,
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


def cmd_profiles_cards(args: argparse.Namespace) -> int:
    md = _profiles(args).cards()
    if args.json:
        print(json.dumps({"ok": True, "markdown": md}, indent=2, sort_keys=True, default=str))
    else:
        sys.stdout.write(md)
        if md and not md.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def cmd_profiles_card(args: argparse.Namespace) -> int:
    md = _profiles(args).card(args.name)
    if args.json:
        print(json.dumps({"ok": True, "markdown": md}, indent=2, sort_keys=True, default=str))
    else:
        sys.stdout.write(md)
        if md and not md.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def cmd_profiles_stamp(args: argparse.Namespace) -> int:
    out = _profiles(args).stamp_verified(args.name)
    print(json.dumps({"ok": True, "profile": out}, indent=2, sort_keys=True, default=str))
    return 0


def _egress_from_register_args(args: argparse.Namespace) -> Any | None:
    raw = getattr(args, "egress", None)
    if raw is None:
        return None
    v = str(raw).strip()
    low = v.lower()
    if low == "direct":
        return "direct"
    if low == "vpn":
        obj: dict[str, str] = {"type": "vpn"}
        if getattr(args, "country", None):
            obj["country"] = args.country
        if getattr(args, "city", None):
            obj["city"] = args.city
        return obj
    if v.startswith("{"):
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError as e:
            raise SystemExit(f"invalid --egress JSON: {e}") from e
        return parsed
    return v


def _add_launch_selector_flags(
    p: argparse.ArgumentParser,
    *,
    kind_required: bool = True,
    suppress_defaults: bool = False,
) -> None:
    d: dict[str, Any] = {"default": argparse.SUPPRESS} if suppress_defaults else {}
    kind_kw: dict[str, Any] = {
        "choices": ["scratch", "adhoc", "vpn"],
        "help": "scratch (default face) or vpn",
        **d,
    }
    if kind_required:
        kind_kw["required"] = True
        kind_kw.pop("default", None)
    p.add_argument("--kind", **kind_kw)
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
        help="named profile; exclusive with selector flags",
    )


def _add_lease_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--owner", default="operator")
    p.add_argument(
        "--mode",
        default="persistent",
        choices=["persistent", "one_shot"],
        help="one_shot: 15m TTL + auto-reap",
    )
    p.add_argument("--ttl", type=float, default=None)
    p.add_argument(
        "--auto-reap",
        action="store_true",
        help="mark persistent lease eligible for scheduled reap",
    )


def build_parser() -> argparse.ArgumentParser:
    shared = _global_parent()
    p = argparse.ArgumentParser(
        prog="browserctl",
        description="Lease a Cloak browser. Named profiles live in profiles/PROFILES.json.",
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

    ac = sub.add_parser("acquire", help="acquire a mutation lease + start adapter", parents=[shared])
    _add_profile_flag(ac)
    _add_launch_selector_flags(ac, kind_required=False, suppress_defaults=True)
    _add_lease_flags(ac)
    ac.set_defaults(func=cmd_acquire)

    rel = sub.add_parser("release", help="release lease and stop resources", parents=[shared])
    rel.add_argument("--lease")
    rel.add_argument("--worker")
    rel.add_argument("--force", action="store_true")
    rel.set_defaults(func=cmd_release)

    rp = sub.add_parser(
        "reap",
        help="reap expired auto-reap-eligible leases; --lease forces one id",
        parents=[shared],
    )
    rp.add_argument("--lease", help="force-reap one lease id")
    rp.add_argument("--dry-run", action="store_true")
    rp.set_defaults(func=cmd_reap)

    mx = sub.add_parser("mark-exit", help="mark lease expiring for TTL reap", parents=[shared])
    mx.add_argument("--lease", required=True)
    mx.add_argument("--ttl", type=float, default=None)
    mx.set_defaults(func=cmd_mark_exit)

    for name in ("launch", "spawn"):
        lp = sub.add_parser(
            name,
            help="acquire and print env JSON (cdp_url + lease id)",
            parents=[shared],
        )
        _add_profile_flag(lp)
        _add_launch_selector_flags(lp, kind_required=False, suppress_defaults=True)
        _add_lease_flags(lp)
        lp.set_defaults(func=cmd_launch)

    prof = sub.add_parser(
        "profiles",
        help="named profile registry (launch selector + site associations)",
        parents=[shared],
    )
    psub = prof.add_subparsers(dest="profiles_cmd", required=True)

    pl = psub.add_parser("list", help="list named profiles", parents=[shared])
    pl.set_defaults(func=cmd_profiles_list)

    ps = psub.add_parser("show", help="show one named profile", parents=[shared])
    ps.add_argument("name")
    ps.set_defaults(func=cmd_profiles_show)

    pr = psub.add_parser("register", help="register/update a named face", parents=[shared])
    pr.add_argument("name")
    _add_launch_selector_flags(pr, kind_required=True)
    pr.add_argument("--description", default=None, help="required on new register")
    pr.add_argument("--notes", default=None)
    pr.add_argument("--egress", default=None, help="direct | vpn | JSON {type:vpn,…}")
    pr.add_argument("--replace", action="store_true")
    pr.set_defaults(func=cmd_profiles_register)

    pa = psub.add_parser("associate", help="associate profile with site[/account]", parents=[shared])
    pa.add_argument("name")
    pa.add_argument("site")
    pa.add_argument("account", nargs="?", default=None)
    pa.set_defaults(func=cmd_profiles_associate)

    pv = psub.add_parser("resolve", help="exact resolve site[/account] (no start)", parents=[shared])
    pv.add_argument("site")
    pv.add_argument("--account", default=None)
    pv.set_defaults(func=cmd_profiles_resolve)

    pcards = psub.add_parser("cards", help="markdown face cards", parents=[shared])
    pcards.set_defaults(func=cmd_profiles_cards)

    pcard = psub.add_parser("card", help="one face card", parents=[shared])
    pcard.add_argument("name")
    pcard.set_defaults(func=cmd_profiles_card)

    pst = psub.add_parser("stamp", help="set last_verified_at = now", parents=[shared])
    pst.add_argument("name")
    pst.set_defaults(func=cmd_profiles_stamp)

    return p


def _normalize_argv(argv: list[str]) -> list[str]:
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
    if rest[0] == "profiles" and len(rest) >= 2 and not rest[1].startswith("-"):
        return [rest[0], rest[1], *globals_opts, *rest[2:]]
    return [rest[0], *globals_opts, *rest[1:]]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(_normalize_argv(raw))
    if getattr(args, "func", None) in (cmd_acquire, cmd_launch):
        has_profile = bool(getattr(args, "profile", None))
        has_kind = getattr(args, "kind", None) is not None
        if not has_profile and not has_kind:
            parser.error("acquire/launch require --kind or --profile NAME")
        if has_profile:
            used = [
                d
                for d in _SELECTOR_DESTS
                if getattr(args, d, None) is not None
            ]
            if used:
                flags = ", ".join("--" + d.replace("_", "-") for d in used)
                parser.error(f"--profile excludes launch selector flags ({flags})")
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
