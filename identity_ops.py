#!/usr/bin/env python3
"""
Browser identity CLI — bind one account to one Cloak worker/profile/port.

  identity_ops.py ensure --email USER@host [--json] [--no-start] [--revive]
  identity_ops.py stop --email USER@host | --worker ID | --all [--json]
  identity_ops.py retire --email USER@host [--json] [--keep-profile] [--reason …]
  identity_ops.py stage-get --email USER@host [--json]
  identity_ops.py stage-set --email USER@host --stage S0..S6 […] [--json]
  identity_ops.py stage-list [--json]
  identity_ops.py list [--json]
  identity_ops.py show --email USER@host [--json]

Infrastructure + API risk stage ledger (S0–S6). Does not deposit/reauth by itself.
Orchestrator: ensure before navigator; **stop after the job** (browsers lag the host).
After every deposit/reauth/heal probe: stage-set from hard verify (see bot-flag-lifecycle).

Identity law:
  - active ∩ retired is impossible
  - ensure on a retired email requires --revive (or a clean supersession after retire)
  - retire stops daemon, drops active row, appends retired_identities, updates stage ledger
  - registry paths are repo-relative when under ROOT (no absolute host paths committed)
  - missing IDENTITIES.json / API_STAGES.json are bootstrapped empty in-code on first load
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
IDENTITIES_PATH = ROOT / "profiles" / "IDENTITIES.json"
API_STAGES_PATH = ROOT / "profiles" / "API_STAGES.json"
PROFILES_XAI = ROOT / "profiles" / "xai"
STATE_ROOT = ROOT / "state"

VALID_STAGES = {"S0", "S1", "S2", "S3", "S4", "S5", "S6"}

# Prefer high ports; 9222 retired for coal (was default).
PORT_MIN = 9223
PORT_MAX = 9299

RETIRED_WORKERS = {"default"}

EMPTY_REGISTRY: dict[str, Any] = {
    "version": 2,
    "rule": (
        "one xAI account ↔ one Cloak user-data-dir ↔ one daemon worker_id "
        "↔ one CDP port ↔ one navigator BROWSER_HARNESS_WORKER"
    ),
    "identities": [],
    "retired": {"default": "Do not use for xAI coal."},
    "retired_identities": [],
    "port_policy": {
        "9222": "retired/free (was default)",
        "allocation": "assign next free port per identity; never share CDP ports",
    },
}

EMPTY_STAGES: dict[str, Any] = {
    "version": 1,
    "canon": "skills/domains/x.ai/bot-flag-lifecycle.md",
    "accounts": {},
}


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _rel_under_root(path: str | Path) -> str:
    """Store paths repo-relative when under ROOT; otherwise keep absolute."""
    p = Path(path)
    try:
        resolved = p.resolve() if p.is_absolute() else (ROOT / p).resolve()
    except OSError:
        return str(path)
    try:
        return str(resolved.relative_to(ROOT.resolve()))
    except ValueError:
        return str(resolved)


def _abs_from_registry(path: str | Path | None, *, default: Path) -> Path:
    if not path:
        return default
    p = Path(path)
    if p.is_absolute():
        return p
    return (ROOT / p).resolve()


def _load_registry() -> dict[str, Any]:
    """Load identities registry; bootstrap empty file when absent."""
    if not IDENTITIES_PATH.exists():
        reg = json.loads(json.dumps(EMPTY_REGISTRY))  # deep copy via json
        _save_registry(reg)
        return reg
    reg = json.loads(IDENTITIES_PATH.read_text())
    # normalize required keys
    reg.setdefault("version", 2)
    reg.setdefault("identities", [])
    reg.setdefault("retired", {"default": "Do not use for xAI coal."})
    reg.setdefault("retired_identities", [])
    return reg


def _save_registry(reg: dict[str, Any]) -> None:
    IDENTITIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    # enforce invariant before every write
    _assert_active_retired_disjoint(reg)
    tmp = IDENTITIES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2) + "\n")
    tmp.replace(IDENTITIES_PATH)


def _active_emails(reg: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for row in reg.get("identities") or []:
        em = str(row.get("email") or "").lower().strip()
        if em:
            out.add(em)
    return out


def _retired_emails(reg: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for row in reg.get("retired_identities") or []:
        em = str(row.get("email") or "").lower().strip()
        if em:
            out.add(em)
    return out


def _assert_active_retired_disjoint(reg: dict[str, Any]) -> None:
    active = _active_emails(reg)
    retired = _retired_emails(reg)
    overlap = active & retired
    if overlap:
        _die(
            "identity invariant violated: active ∩ retired non-empty: "
            + ", ".join(sorted(overlap)),
            3,
        )


def _find_retired_by_email(reg: dict[str, Any], email: str) -> dict[str, Any] | None:
    target = email.lower()
    for row in reg.get("retired_identities") or []:
        if str(row.get("email", "")).lower() == target:
            return row
    return None


def _drop_retired_email(reg: dict[str, Any], email: str) -> None:
    """Remove all retired_identities entries for email (used on --revive)."""
    target = email.lower()
    reg["retired_identities"] = [
        r
        for r in (reg.get("retired_identities") or [])
        if str(r.get("email", "")).lower() != target
    ]


def _norm_email(email: str) -> str:
    e = (email or "").strip()
    if "@" not in e or e.startswith("@") or e.endswith("@"):
        _die(f"invalid email: {email!r}")
    return e


def _slug_from_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    slug = re.sub(r"[^a-z0-9]+", "", local)
    if not slug:
        _die(f"cannot derive slug from email: {email}")
    return slug[:40]


def _worker_id_from_slug(slug: str) -> str:
    return f"xai-{slug}"


def _find_by_email(reg: dict[str, Any], email: str) -> dict[str, Any] | None:
    target = email.lower()
    for row in reg.get("identities") or []:
        if str(row.get("email", "")).lower() == target:
            return row
    return None


def _used_ports(reg: dict[str, Any]) -> set[int]:
    ports: set[int] = set()
    for row in reg.get("identities") or []:
        p = row.get("cdp_port")
        if isinstance(p, int):
            ports.add(p)
    return ports


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


def _allocate_port(reg: dict[str, Any]) -> int:
    used = _used_ports(reg)
    for port in range(PORT_MIN, PORT_MAX + 1):
        if port in used:
            continue
        if _port_free(port):
            return port
    _die(f"no free CDP port in {PORT_MIN}-{PORT_MAX}")


def _socket_path(worker_id: str) -> Path:
    return STATE_ROOT / worker_id / "daemon.sock"


def _cdp_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=2
        ) as r:
            return r.status == 200
    except Exception:
        return False


def _daemon_pids_for_worker(worker_id: str) -> list[int]:
    """Best-effort: pids for daemon main with --worker <id>."""
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


def _daemon_pid_for_worker(worker_id: str) -> int | None:
    pids = _daemon_pids_for_worker(worker_id)
    return pids[0] if pids else None


def _chrome_pids_for_profile(profile_dir: str | Path) -> list[int]:
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


def _kill_pids(pids: list[int], *, sig: int = signal.SIGTERM) -> list[int]:
    alive: list[int] = []
    for pid in sorted(set(pids)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError:
            alive.append(pid)
    if not pids:
        return []
    time.sleep(0.6)
    for pid in sorted(set(pids)):
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            alive.append(pid)
    return sorted(set(alive))


def _stop_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Stop daemon + Cloak for one registry row. Profile data kept on disk."""
    worker_id = str(row["worker_id"])
    slug = row.get("slug") or worker_id.removeprefix("xai-")
    profile_dir = _abs_from_registry(
        row.get("profile_dir"), default=PROFILES_XAI / slug
    )
    port = int(row.get("cdp_port") or 0)

    daemon_pids = _daemon_pids_for_worker(worker_id)
    chrome_pids = _chrome_pids_for_profile(profile_dir)

    still = _kill_pids(daemon_pids, sig=signal.SIGTERM)
    if still:
        _kill_pids(still, sig=signal.SIGKILL)

    chrome_left = _chrome_pids_for_profile(profile_dir)
    still_c = _kill_pids(chrome_left, sig=signal.SIGTERM)
    if still_c:
        _kill_pids(still_c, sig=signal.SIGKILL)

    if port and not _cdp_alive(port):
        sock = _socket_path(worker_id)
        if sock.exists():
            try:
                sock.unlink()
            except OSError:
                pass
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            p = profile_dir / name
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
            except OSError:
                pass

    cdp_up = bool(port and _cdp_alive(port))
    daemon_left = _daemon_pids_for_worker(worker_id)
    chrome_left = _chrome_pids_for_profile(profile_dir)
    status = "stopped"
    if cdp_up or daemon_left or chrome_left:
        status = "partial"

    return {
        "email": row.get("email"),
        "worker_id": worker_id,
        "cdp_port": port,
        "profile_dir": str(profile_dir),
        "daemon_pids_signaled": daemon_pids,
        "chrome_pids_remaining": chrome_left,
        "daemon_pids_remaining": daemon_left,
        "cdp_alive": cdp_up,
        "status": status,
    }


def _env_for(worker_id: str) -> dict[str, str]:
    """Navigator + daemon env. identity_ops is canonical when no override env set."""
    env = {
        "BROWSER_HARNESS_WORKER": worker_id,
        "PYTHONPATH": str(ROOT),
        "BROWSER_ALLOW_EVALUATE": "1",
        "BROWSER_OPS_ROOT": str(ROOT),
    }
    ops_state = (os.environ.get("BROWSER_OPS_STATE") or "").strip()
    target_state = (os.environ.get("BROWSER_TARGET_STATE") or "").strip()
    if not ops_state:
        ops_state = str(STATE_ROOT)
    if not target_state:
        target_state = str(
            Path(ops_state) / worker_id / "control" / "active-target.json"
        )
    env["BROWSER_OPS_STATE"] = ops_state
    env["BROWSER_TARGET_STATE"] = target_state
    return env


# ── Stage ledger ──────────────────────────────────────────────────────────────


def _load_stages() -> dict[str, Any]:
    """Load API stage ledger; bootstrap empty file when absent."""
    if not API_STAGES_PATH.exists():
        doc = json.loads(json.dumps(EMPTY_STAGES))  # deep copy via json
        _save_stages(doc)
        return doc
    doc = json.loads(API_STAGES_PATH.read_text())
    doc.setdefault("version", 1)
    doc.setdefault("canon", EMPTY_STAGES["canon"])
    doc.setdefault("accounts", {})
    return doc


def _save_stages(doc: dict[str, Any]) -> None:
    API_STAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = API_STAGES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(API_STAGES_PATH)


def _default_stage_row(email: str) -> dict[str, Any]:
    return {
        "email": email,
        "api_stage": "S0",
        "bot_flag_source": None,
        "last_billing_http": None,
        "last_probe_at": None,
        "s3_full_oauth_fails": 0,
        "s3_entered_at": None,
        "s3_next_retry_at": None,
        "s3_probe_only_at": None,
        "s4_entered_at": None,
        "s4_next_probe_at": None,
        "note": None,
        "history": [],
    }


def _get_stage_row(doc: dict[str, Any], email: str) -> dict[str, Any]:
    accounts = doc.setdefault("accounts", {})
    key = email.lower()
    for k, v in list(accounts.items()):
        if k.lower() == key:
            return v
    row = _default_stage_row(email)
    accounts[email] = row
    return row


def _mirror_stage_to_identities(email: str, stage: str, note: str | None = None) -> None:
    """Keep IDENTITIES.json api_stage in sync when row exists (best-effort)."""
    reg = _load_registry()
    row = _find_by_email(reg, email)
    if not row:
        return
    row["api_stage"] = stage
    if note:
        row["notes"] = note[:500]
    if stage == "S4":
        row["deposit_status"] = "s4_action_required"
    elif stage in ("S0", "S1"):
        row["deposit_status"] = "verified_active"
    elif stage == "S5":
        row["deposit_status"] = "terminal_quota_action_required"
    _save_registry(reg)


def _mark_stage_retired(email: str, *, reason: str) -> dict[str, Any] | None:
    """On retire: annotate stage ledger (does not invent destructive CPA calls)."""
    doc = _load_stages()
    key = email
    row = None
    for k, v in list((doc.get("accounts") or {}).items()):
        if k.lower() == email.lower():
            key = k
            row = v
            break
    if row is None:
        row = _default_stage_row(email)
    prev = row.get("api_stage")
    now = _utc_now()
    # S4 stays S4 with action_required annotation; others get retired marker in history
    if prev == "S4":
        row["api_stage"] = "S4"
        row["note"] = f"retired:{reason}"
        row["action_required"] = True
    else:
        # keep stage but flag retired
        row["note"] = f"retired:{reason}"
        row["action_required"] = True
    row["retired_at"] = now
    hist = row.setdefault("history", [])
    if not isinstance(hist, list):
        hist = []
        row["history"] = hist
    hist.append(
        {
            "at": now,
            "from": prev,
            "to": row.get("api_stage"),
            "billing": row.get("last_billing_http"),
            "bot_flag": row.get("bot_flag_source"),
            "s3_full_oauth_fails": row.get("s3_full_oauth_fails"),
            "note": f"identity_ops retire: {reason}",
        }
    )
    row["history"] = hist[-30:]
    doc.setdefault("accounts", {})[email] = row
    if key != email and key in doc["accounts"]:
        del doc["accounts"][key]
    _save_stages(doc)
    return row


def _payload(row: dict[str, Any], *, daemon: str, created: bool = False) -> dict[str, Any]:
    worker_id = row["worker_id"]
    email = row["email"]
    stages = _load_stages()
    stage_row = None
    for k, v in (stages.get("accounts") or {}).items():
        if k.lower() == str(email).lower():
            stage_row = v
            break
    slug = row["slug"]
    profile_abs = _abs_from_registry(
        row.get("profile_dir"), default=PROFILES_XAI / slug
    )
    state_abs = _abs_from_registry(
        row.get("state_dir"), default=STATE_ROOT / worker_id
    )
    return {
        "email": email,
        "slug": slug,
        "worker_id": worker_id,
        "cdp_port": row["cdp_port"],
        "profile_dir": str(profile_abs),
        "state_dir": str(state_abs),
        "socket": str(_socket_path(worker_id)),
        "env": _env_for(worker_id),
        "daemon": daemon,
        "created": created,
        "api_stage": (stage_row or {}).get("api_stage") or row.get("api_stage"),
        "api_stage_detail": stage_row,
        "spawn_hint": {
            "profile": "navigator",
            "cwd": str(ROOT),
            "env": _env_for(worker_id),
            "skill": {"domain": "cpa-manager", "skill_id": "xai-dashboard-deposit"},
        },
    }


def cmd_stage_get(email: str, *, as_json: bool) -> int:
    email = _norm_email(email)
    doc = _load_stages()
    row = None
    for k, v in (doc.get("accounts") or {}).items():
        if k.lower() == email.lower():
            row = v
            break
    if row is None:
        row = {
            **_default_stage_row(email),
            "inferred": True,
            "note": "no ledger row yet — default S0",
        }
    if as_json:
        print(json.dumps(row, indent=2))
    else:
        print(
            f"{row.get('email', email):40}  stage={row.get('api_stage')}  "
            f"oauth_fails={row.get('s3_full_oauth_fails')}  "
            f"next_retry={row.get('s3_next_retry_at')}  "
            f"billing={row.get('last_billing_http')}  bot_flag={row.get('bot_flag_source')}"
        )
    return 0


def cmd_stage_list(*, as_json: bool) -> int:
    doc = _load_stages()
    accounts = doc.get("accounts") or {}
    reg = _load_registry()
    rows = []
    seen: set[str] = set()
    for email, st in accounts.items():
        rows.append(st)
        seen.add(email.lower())
    for ident in reg.get("identities") or []:
        em = ident.get("email")
        if not em or em.lower() in seen:
            continue
        r = _default_stage_row(em)
        if ident.get("api_stage") in VALID_STAGES:
            r["api_stage"] = ident["api_stage"]
            r["note"] = "from IDENTITIES.api_stage only"
        r["inferred"] = True
        rows.append(r)
    rows.sort(key=lambda r: (r.get("api_stage") or "S0", r.get("email") or ""))
    if as_json:
        print(json.dumps({"accounts": rows}, indent=2))
    else:
        if not rows:
            print("(no stage rows)")
        for r in rows:
            print(
                f"{r.get('email', '?'):40}  {r.get('api_stage', '?'):4}  "
                f"fails={r.get('s3_full_oauth_fails', 0)}  "
                f"next={r.get('s3_next_retry_at') or '-'}  "
                f"bill={r.get('last_billing_http')}  flag={r.get('bot_flag_source')}"
            )
    return 0


def cmd_stage_set(
    *,
    email: str,
    stage: str,
    as_json: bool,
    bot_flag_source: str | None,
    last_billing_http: int | None,
    full_oauth_fail: bool,
    full_oauth_ok: bool,
    note: str | None,
    force_s3_fails: int | None,
) -> int:
    """Update API risk stage after a probe/reauth. Mirrors to IDENTITIES when bound."""
    email = _norm_email(email)
    stage = (stage or "").upper().strip()
    if stage not in VALID_STAGES:
        _die(f"invalid stage {stage!r}; want one of {sorted(VALID_STAGES)}")

    doc = _load_stages()
    key = email
    for k in list((doc.get("accounts") or {}).keys()):
        if k.lower() == email.lower():
            key = k
            break
    row = _get_stage_row(doc, key)
    row["email"] = email
    prev = row.get("api_stage")
    now = _utc_now()

    if bot_flag_source is not None:
        if bot_flag_source.lower() in ("", "none", "null", "-", "absent"):
            row["bot_flag_source"] = None
        else:
            try:
                row["bot_flag_source"] = int(bot_flag_source)
            except ValueError:
                row["bot_flag_source"] = bot_flag_source

    if last_billing_http is not None:
        row["last_billing_http"] = int(last_billing_http)
        row["last_probe_at"] = now

    if force_s3_fails is not None:
        row["s3_full_oauth_fails"] = int(force_s3_fails)

    if full_oauth_fail:
        row["s3_full_oauth_fails"] = int(row.get("s3_full_oauth_fails") or 0) + 1
        nxt = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + 6 * 3600),
        )
        row["s3_next_retry_at"] = nxt
        row["s3_probe_only_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + 3600),
        )
        if not row.get("s3_entered_at"):
            row["s3_entered_at"] = now
        if int(row["s3_full_oauth_fails"]) >= 3 and stage in ("S3", "S2", "S4"):
            stage = "S4"

    if full_oauth_ok or stage in ("S0", "S1"):
        if full_oauth_ok or (
            last_billing_http is not None and int(last_billing_http) == 200
        ):
            row["s3_full_oauth_fails"] = 0
            row["s3_entered_at"] = None
            row["s3_next_retry_at"] = None
            row["s3_probe_only_at"] = None
            row["s4_entered_at"] = None
            row["s4_next_probe_at"] = None

    if stage == "S3" and not row.get("s3_entered_at"):
        row["s3_entered_at"] = now
        if not row.get("s3_next_retry_at"):
            row["s3_next_retry_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(time.time() + 6 * 3600),
            )

    if stage == "S4":
        if not row.get("s4_entered_at"):
            row["s4_entered_at"] = now
        row["s4_next_probe_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + 7 * 24 * 3600),
        )
        row["s3_next_retry_at"] = None
        # S4 is action_required — orchestrator must decide; not automatic teardown
        row["action_required"] = True

    row["api_stage"] = stage
    if note is not None:
        row["note"] = note

    hist = row.setdefault("history", [])
    if not isinstance(hist, list):
        hist = []
        row["history"] = hist
    hist.append(
        {
            "at": now,
            "from": prev,
            "to": stage,
            "billing": row.get("last_billing_http"),
            "bot_flag": row.get("bot_flag_source"),
            "s3_full_oauth_fails": row.get("s3_full_oauth_fails"),
            "note": note,
        }
    )
    row["history"] = hist[-30:]

    doc.setdefault("accounts", {})[key] = row
    if key != email:
        doc["accounts"][email] = row
        if key in doc["accounts"] and key != email:
            del doc["accounts"][key]
    _save_stages(doc)
    _mirror_stage_to_identities(email, stage, note=row.get("note"))

    if as_json:
        print(json.dumps(row, indent=2))
    else:
        print(
            f"{email:40}  {prev} → {stage}  fails={row.get('s3_full_oauth_fails')}  "
            f"next={row.get('s3_next_retry_at')}  bill={row.get('last_billing_http')}"
        )
    return 0


def _ensure_row(
    reg: dict[str, Any], email: str, *, revive: bool = False
) -> tuple[dict[str, Any], bool]:
    existing = _find_by_email(reg, email)
    if existing:
        wid = existing.get("worker_id") or ""
        if wid in RETIRED_WORKERS:
            _die(
                f"identity {email} is bound to retired worker {wid!r}; "
                "migrate to xai-<slug> before ensure"
            )
        existing.setdefault("slug", _slug_from_email(email))
        existing.setdefault(
            "profile_dir",
            _rel_under_root(PROFILES_XAI / existing["slug"]),
        )
        existing.setdefault(
            "state_dir",
            _rel_under_root(STATE_ROOT / existing["worker_id"]),
        )
        # normalize any absolute paths already stored
        existing["profile_dir"] = _rel_under_root(existing["profile_dir"])
        existing["state_dir"] = _rel_under_root(existing["state_dir"])
        return existing, False

    # retired → require explicit revive
    prior = _find_retired_by_email(reg, email)
    if prior is not None and not revive:
        _die(
            f"identity {email} is retired "
            f"(reason={prior.get('reason')!r}, at={prior.get('retired_at')!r}); "
            "pass --revive to re-bind, or leave retired",
            2,
        )
    if prior is not None and revive:
        _drop_retired_email(reg, email)

    slug = _slug_from_email(email)
    worker_id = _worker_id_from_slug(slug)
    for row in reg.get("identities") or []:
        if row.get("worker_id") == worker_id or row.get("slug") == slug:
            if str(row.get("email", "")).lower() != email.lower():
                _die(
                    f"slug/worker collision: {slug}/{worker_id} already used by {row.get('email')}"
                )

    port = _allocate_port(reg)
    row = {
        "email": email,
        "slug": slug,
        "worker_id": worker_id,
        "cdp_port": port,
        "profile_dir": _rel_under_root(PROFILES_XAI / slug),
        "state_dir": _rel_under_root(STATE_ROOT / worker_id),
        "bound_at": time.strftime("%Y-%m-%d"),
        "notes": (
            "revived by identity_ops ensure --revive"
            if prior is not None
            else "allocated by identity_ops ensure"
        ),
    }
    reg.setdefault("identities", []).append(row)
    unbound = reg.get("unbound_active_auths") or []
    reg["unbound_active_auths"] = [e for e in unbound if e.lower() != email.lower()]
    missing = reg.get("missing_shipment_accounts") or []
    reg["missing_shipment_accounts"] = [
        e for e in missing if e.lower() != email.lower()
    ]
    _save_registry(reg)
    return row, True


def _start_daemon(row: dict[str, Any]) -> None:
    worker_id = row["worker_id"]
    slug = row.get("slug") or _slug_from_email(row["email"])
    profile_dir = _abs_from_registry(
        row.get("profile_dir"), default=PROFILES_XAI / slug
    )
    state_dir = _abs_from_registry(
        row.get("state_dir"), default=STATE_ROOT / worker_id
    )
    port = int(row["cdp_port"])

    if worker_id in RETIRED_WORKERS:
        _die(f"refusing to start retired worker {worker_id}")

    profile_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    if not _cdp_alive(port):
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            p = profile_dir / name
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
            except OSError:
                pass
        sock = _socket_path(worker_id)
        if sock.exists():
            try:
                sock.unlink()
            except OSError:
                pass

    if _cdp_alive(port) and _daemon_pid_for_worker(worker_id):
        return

    if _cdp_alive(port) and not _daemon_pid_for_worker(worker_id):
        _die(
            f"CDP {port} is up but daemon for {worker_id} not found — "
            "manual conflict; free the port or attach carefully"
        )

    log_path = Path(f"/tmp/daemon-{worker_id}.log")
    env = os.environ.copy()
    env.update(_env_for(worker_id))

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
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n--- ensure start {time.strftime('%Y-%m-%dT%H:%M:%SZ')} ---\n")
        log.flush()
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.time() + 20
    while time.time() < deadline:
        if _cdp_alive(port):
            return
        time.sleep(0.4)

    tail = ""
    try:
        tail = log_path.read_text(errors="replace")[-1500:]
    except OSError:
        pass
    _die(f"daemon for {worker_id} failed to expose CDP {port} within 20s\n{tail}")


def cmd_ensure(
    email: str, *, as_json: bool, no_start: bool, revive: bool = False
) -> int:
    email = _norm_email(email)
    reg = _load_registry()
    row, created = _ensure_row(reg, email, revive=revive)
    if not created:
        _save_registry(reg)

    slug = row.get("slug") or _slug_from_email(email)
    profile_abs = _abs_from_registry(
        row.get("profile_dir"), default=PROFILES_XAI / slug
    )
    state_abs = _abs_from_registry(
        row.get("state_dir"), default=STATE_ROOT / row["worker_id"]
    )
    profile_abs.mkdir(parents=True, exist_ok=True)
    state_abs.mkdir(parents=True, exist_ok=True)

    daemon = "stopped"
    if no_start:
        daemon = "running" if _cdp_alive(int(row["cdp_port"])) else "stopped"
    else:
        _start_daemon(row)
        daemon = "running" if _cdp_alive(int(row["cdp_port"])) else "unknown"

    payload = _payload(row, daemon=daemon, created=created)
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"email:      {payload['email']}")
        print(f"worker_id:  {payload['worker_id']}")
        print(f"cdp_port:   {payload['cdp_port']}")
        print(f"profile:    {payload['profile_dir']}")
        print(f"socket:     {payload['socket']}")
        print(f"daemon:     {payload['daemon']}")
        print(f"created:    {payload['created']}")
        print("env:")
        for k, v in payload["env"].items():
            print(f"  {k}={v}")
    return 0


def cmd_list(*, as_json: bool) -> int:
    reg = _load_registry()
    rows = []
    for row in reg.get("identities") or []:
        port = int(row.get("cdp_port") or 0)
        rows.append(
            {
                "email": row.get("email"),
                "worker_id": row.get("worker_id"),
                "cdp_port": port,
                "profile_dir": row.get("profile_dir"),
                "daemon": "running" if port and _cdp_alive(port) else "stopped",
            }
        )
    if as_json:
        print(
            json.dumps(
                {
                    "identities": rows,
                    "retired_count": len(reg.get("retired_identities") or []),
                },
                indent=2,
            )
        )
    else:
        if not rows:
            print("(no identities)")
        for r in rows:
            print(
                f"{r['email']:40}  {r['worker_id']:20}  :{r['cdp_port']}  {r['daemon']}"
            )
    return 0


def cmd_show(email: str, *, as_json: bool) -> int:
    email = _norm_email(email)
    reg = _load_registry()
    row = _find_by_email(reg, email)
    if not row:
        retired = _find_retired_by_email(reg, email)
        if retired:
            _die(
                f"identity {email} is retired (reason={retired.get('reason')!r}); "
                "not active — use ensure --revive to re-bind",
                2,
            )
        _die(f"no identity for {email}", 2)
    port = int(row.get("cdp_port") or 0)
    daemon = "running" if port and _cdp_alive(port) else "stopped"
    payload = _payload(row, daemon=daemon, created=False)
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(json.dumps(payload, indent=2))
    return 0


def _wipe_tree(path: Path) -> dict[str, Any]:
    """Remove a profile/state directory tree. Refuses paths outside ROOT."""
    try:
        resolved = path.resolve()
    except OSError as e:
        return {"path": str(path), "wiped": False, "error": str(e)}
    root = ROOT.resolve()
    if root not in resolved.parents and resolved != root:
        return {
            "path": str(path),
            "wiped": False,
            "error": f"refuse wipe outside {root}: {resolved}",
        }
    allowed_parents = {(root / "profiles").resolve(), (root / "state").resolve()}
    ok = False
    for ap in allowed_parents:
        try:
            resolved.relative_to(ap)
            ok = True
            break
        except ValueError:
            pass
    if not ok:
        return {
            "path": str(path),
            "wiped": False,
            "error": f"refuse wipe path not under profiles/ or state/: {resolved}",
        }
    if not path.exists():
        return {"path": str(path), "wiped": True, "missing": True}
    try:
        shutil.rmtree(path)
        return {"path": str(path), "wiped": True}
    except OSError as e:
        return {"path": str(path), "wiped": False, "error": str(e)}


def cmd_retire(
    *,
    email: str,
    as_json: bool,
    keep_profile: bool,
    reason: str | None,
) -> int:
    """Full coal teardown: stop daemon, retire IDENTITIES row, wipe Cloak profile.

    Call after CPA auth DELETE + discard receipt. Does not call CPA itself.
    Updates stage ledger. Enforces active∩retired == ∅.
    """
    email = _norm_email(email)
    reg = _load_registry()
    row = _find_by_email(reg, email)
    if not row:
        # already retired is idempotent success
        prior = _find_retired_by_email(reg, email)
        payload = {
            "email": email,
            "status": "already_retired" if prior else "not_bound",
            "note": (
                "already in retired_identities"
                if prior
                else "no IDENTITIES row — nothing to retire (CPA delete may still have happened)"
            ),
        }
        if as_json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"{email:40}  {payload['status']}")
        return 0

    stop_info = _stop_identity(row)
    slug = row.get("slug") or _slug_from_email(email)
    profile_dir = _abs_from_registry(
        row.get("profile_dir"), default=PROFILES_XAI / slug
    )
    state_dir = _abs_from_registry(
        row.get("state_dir"), default=STATE_ROOT / row["worker_id"]
    )

    if keep_profile:
        wipe_profile: dict[str, Any] = {
            "path": str(profile_dir),
            "wiped": False,
            "kept": True,
        }
        wipe_state: dict[str, Any] = {
            "path": str(state_dir),
            "wiped": False,
            "kept": True,
        }
    else:
        wipe_profile = _wipe_tree(profile_dir)
        wipe_state = _wipe_tree(state_dir)

    # remove from active first, then append retired (disjoint)
    reg["identities"] = [
        r
        for r in (reg.get("identities") or [])
        if str(r.get("email", "")).lower() != email.lower()
    ]
    # ensure no stale retired dup before append
    _drop_retired_email(reg, email)
    retired_list = reg.setdefault("retired_identities", [])
    if not isinstance(retired_list, list):
        retired_list = []
        reg["retired_identities"] = retired_list
    retire_reason = reason or "operator_retire"
    retired_list.append(
        {
            "email": email,
            "slug": row.get("slug"),
            "worker_id": row.get("worker_id"),
            "cdp_port": row.get("cdp_port"),
            "profile_dir": row.get("profile_dir"),
            "retired_at": _utc_now(),
            "reason": retire_reason,
            "profile_wiped": bool(wipe_profile.get("wiped")) and not keep_profile,
            "prior_notes": row.get("notes"),
        }
    )
    reg["retired_identities"] = retired_list[-50:]
    _save_registry(reg)

    stage_row = _mark_stage_retired(email, reason=retire_reason)

    payload = {
        "email": email,
        "status": "retired",
        "stop": stop_info,
        "wipe_profile": wipe_profile,
        "wipe_state": wipe_state,
        "reason": retire_reason,
        "stage": {
            "api_stage": (stage_row or {}).get("api_stage"),
            "action_required": (stage_row or {}).get("action_required"),
            "note": (stage_row or {}).get("note"),
        },
    }
    ok = stop_info.get("status") == "stopped" and (
        keep_profile or (wipe_profile.get("wiped") and wipe_state.get("wiped"))
    )
    if as_json:
        print(json.dumps({**payload, "ok": ok}, indent=2))
    else:
        print(
            f"{email:40}  {row.get('worker_id'):20}  "
            f"retired wipe_profile={wipe_profile.get('wiped')} "
            f"wipe_state={wipe_state.get('wiped')}"
        )
        if not ok:
            print(json.dumps(payload, indent=2), file=sys.stderr)
    return 0 if ok else 1


def cmd_stop(
    *,
    email: str | None,
    worker: str | None,
    all_identities: bool,
    as_json: bool,
) -> int:
    reg = _load_registry()
    rows: list[dict[str, Any]] = list(reg.get("identities") or [])

    if all_identities:
        targets = rows
    elif email:
        row = _find_by_email(reg, _norm_email(email))
        if not row:
            _die(f"no identity for {email}", 2)
        targets = [row]
    elif worker:
        targets = [r for r in rows if r.get("worker_id") == worker]
        if not targets:
            targets = [
                {
                    "email": None,
                    "worker_id": worker,
                    "cdp_port": 0,
                    "profile_dir": str(PROFILES_XAI / worker.removeprefix("xai-")),
                    "slug": worker.removeprefix("xai-"),
                }
            ]
    else:
        _die("stop requires --email, --worker, or --all")

    results = [_stop_identity(r) for r in targets]
    failed = [r for r in results if r.get("status") != "stopped"]

    if as_json:
        print(json.dumps({"stopped": results, "ok": not failed}, indent=2))
    else:
        for r in results:
            print(
                f"{r.get('email') or '-':40}  {r['worker_id']:20}  "
                f":{r['cdp_port']}  {r['status']}"
            )
            if r["status"] != "stopped":
                print(
                    f"  remaining daemon={r['daemon_pids_remaining']} "
                    f"chrome={r['chrome_pids_remaining']} cdp={r['cdp_alive']}",
                    file=sys.stderr,
                )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Browser identity binding CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("ensure", help="allocate/bind identity and start daemon")
    e.add_argument("--email", required=True)
    e.add_argument("--json", action="store_true")
    e.add_argument(
        "--no-start",
        action="store_true",
        help="only bind registry paths; do not launch daemon",
    )
    e.add_argument(
        "--revive",
        action="store_true",
        help="re-bind an email present in retired_identities (explicit)",
    )

    st = sub.add_parser(
        "stop",
        help="stop Cloak+daemon for an identity (keep profile on disk)",
    )
    st.add_argument("--email")
    st.add_argument("--worker", help="worker_id e.g. xai-<slug>")
    st.add_argument(
        "--all",
        action="store_true",
        help="stop every identity in the registry",
    )
    st.add_argument("--json", action="store_true")

    rt = sub.add_parser(
        "retire",
        help="stop + remove IDENTITIES row + wipe Cloak profile (after CPA discard)",
    )
    rt.add_argument("--email", required=True)
    rt.add_argument("--json", action="store_true")
    rt.add_argument(
        "--keep-profile",
        action="store_true",
        help="retire registry row but leave profiles/xai/<slug> on disk",
    )
    rt.add_argument(
        "--reason",
        default="operator_retire",
        help="audit reason stored in retired_identities",
    )

    l = sub.add_parser("list", help="list bound identities")
    l.add_argument("--json", action="store_true")

    s = sub.add_parser("show", help="show one identity")
    s.add_argument("--email", required=True)
    s.add_argument("--json", action="store_true")

    sg = sub.add_parser("stage-get", help="get API risk stage S0–S6 for an email")
    sg.add_argument("--email", required=True)
    sg.add_argument("--json", action="store_true")

    ss = sub.add_parser(
        "stage-set",
        help="set API risk stage after probe/reauth (updates API_STAGES.json + IDENTITIES)",
    )
    ss.add_argument("--email", required=True)
    ss.add_argument("--stage", required=True, help="S0..S6")
    ss.add_argument("--json", action="store_true")
    ss.add_argument(
        "--bot-flag",
        dest="bot_flag_source",
        help="JWT bot_flag_source value, or 'absent'",
    )
    ss.add_argument(
        "--billing-http",
        dest="last_billing_http",
        type=int,
        help="HTTP status from cli-chat-proxy billing probe",
    )
    ss.add_argument(
        "--full-oauth-fail",
        action="store_true",
        help="count one full OAuth that still 403'd (+6h next retry; auto S4 at 3)",
    )
    ss.add_argument(
        "--full-oauth-ok",
        action="store_true",
        help="full OAuth + probe succeeded; clear S3 counters",
    )
    ss.add_argument("--note", help="short audit note")
    ss.add_argument(
        "--s3-fails",
        dest="force_s3_fails",
        type=int,
        help="force-set s3_full_oauth_fails counter",
    )

    sl = sub.add_parser("stage-list", help="list API risk stages for coal identities")
    sl.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "ensure":
        return cmd_ensure(
            args.email,
            as_json=args.json,
            no_start=args.no_start,
            revive=args.revive,
        )
    if args.cmd == "stop":
        return cmd_stop(
            email=args.email,
            worker=args.worker,
            all_identities=args.all,
            as_json=args.json,
        )
    if args.cmd == "retire":
        return cmd_retire(
            email=args.email,
            as_json=args.json,
            keep_profile=args.keep_profile,
            reason=args.reason,
        )
    if args.cmd == "stage-get":
        return cmd_stage_get(args.email, as_json=args.json)
    if args.cmd == "stage-set":
        return cmd_stage_set(
            email=args.email,
            stage=args.stage,
            as_json=args.json,
            bot_flag_source=args.bot_flag_source,
            last_billing_http=args.last_billing_http,
            full_oauth_fail=args.full_oauth_fail,
            full_oauth_ok=args.full_oauth_ok,
            note=args.note,
            force_s3_fails=args.force_s3_fails,
        )
    if args.cmd == "stage-list":
        return cmd_stage_list(as_json=args.json)
    if args.cmd == "list":
        return cmd_list(as_json=args.json)
    if args.cmd == "show":
        return cmd_show(args.email, as_json=args.json)
    _die(f"unknown cmd {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
