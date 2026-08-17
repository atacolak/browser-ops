"""
Named persistent browser profile registry.

Repo-local map: stable profile name → browserctl launch selector + site/account
associations. No secrets. No capability ontology.

Storage: profiles/PROFILES.json (schema v2)
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from browserctl.atomic import atomic_write_json, read_json
from browserctl.errors import BrowserctlError, InvalidRequest, LeaseConflict
from browserctl.locks import mkdir_lock
from browserctl.paths import profiles_registry_path, resolve_root

PROFILES_SCHEMA_VERSION = 2

# Stable operator names: lowercase start, alnum/_/- , 1–64 chars.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# Site keys: etld-ish or short slug (x.ai, cpa-manager, accounts.x.ai).
_SITE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$|^[a-z0-9]$")
# Account labels are non-secret identifiers (email or handle); block whitespace/control.
_ACCOUNT_RE = re.compile(r"^[^\s\x00-\x1f]{1,253}$")

_FORBIDDEN_FIELD_RE = re.compile(
    r"(password|passwd|secret|token|cookie|authorization|api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)

_LAUNCH_KINDS = frozenset({"xai", "scratch", "adhoc", "vpn"})

# Persistent launch selector only — runtime lease fields (owner/mode/ttl) stay on acquire/launch.
_LAUNCH_KEYS = frozenset(
    {
        "kind",
        "email",
        "worker",
        "label",
        "cdp_port",
        "country",
        "city",
        "headed",
        "attach_only",
        "no_start",
        "egress",
    }
)


def validate_description(description: str | None, *, required: bool = True) -> str | None:
    if description is None:
        if required:
            raise InvalidRequest("profile description is required")
        return None
    if not isinstance(description, str):
        raise InvalidRequest("profile description must be a string")
    d = description.strip()
    if not d:
        raise InvalidRequest("profile description must be a non-empty string")
    return d


def validate_notes(notes: str | None) -> str | None:
    if notes is None:
        return None
    if not isinstance(notes, str):
        raise InvalidRequest("profile notes must be a string")
    n = notes.strip()
    if not n:
        return None
    if _FORBIDDEN_FIELD_RE.search(n) and (":" in n or "=" in n):
        raise InvalidRequest("notes look like a secret field")
    return n


def validate_egress(raw: Any) -> str | dict[str, str]:
    """egress is a launch property, not a peer identity. omitted|'direct'|{type:vpn}."""
    if raw is None:
        raise InvalidRequest("launch.egress must be 'direct' or {type: vpn, ...}")
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v == "direct":
            return "direct"
        raise InvalidRequest(
            "launch.egress must be 'direct' or an object {type: vpn, country?, city?}",
            egress=raw,
        )
    if not isinstance(raw, dict):
        raise InvalidRequest(
            "launch.egress must be 'direct' or an object {type: vpn, country?, city?}",
            egress=raw,
        )
    _reject_secret_keys(raw, where="launch.egress")
    unknown = set(raw) - {"type", "country", "city"}
    if unknown:
        raise InvalidRequest(
            f"unknown launch.egress keys: {sorted(unknown)}",
            unknown=sorted(unknown),
        )
    typ = str(raw.get("type") or "").strip().lower()
    if typ != "vpn":
        raise InvalidRequest(
            "launch.egress object type must be vpn",
            type=raw.get("type"),
        )
    out: dict[str, str] = {"type": "vpn"}
    for key in ("country", "city"):
        if key not in raw or raw[key] is None:
            continue
        val = str(raw[key]).strip()
        if val:
            out[key] = val
    return out


class ProfileNotFound(BrowserctlError):
    def __init__(
        self,
        *,
        name: str | None = None,
        site: str | None = None,
        account: str | None = None,
        message: str | None = None,
        hint: str | None = None,
    ):
        details: dict[str, Any] = {}
        if name is not None:
            details["name"] = name
        if site is not None:
            details["site"] = site
        if account is not None:
            details["account"] = account
        if hint is not None:
            details["hint"] = hint
        who = name or site or "?"
        super().__init__(
            "PROFILE_NOT_FOUND",
            message or f"no profile found for {who}",
            details=details,
            exit_code=2,
        )


class ProfileAmbiguous(BrowserctlError):
    def __init__(self, *, site: str, account: str | None, matches: list[str]):
        super().__init__(
            "PROFILE_AMBIGUOUS",
            f"multiple profiles match site={site!r}"
            + (f" account={account!r}" if account else "")
            + f": {matches}",
            details={"site": site, "account": account, "matches": matches},
            exit_code=2,
        )


class ProfileLockTimeout(BrowserctlError):
    def __init__(self, lock: str | None = None):
        super().__init__(
            "PROFILE_LOCK_TIMEOUT",
            "timed out acquiring profile registry lock",
            details={"lock": lock} if lock else {},
            exit_code=1,
        )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextmanager
def _registry_lock(path: Path) -> Iterator[None]:
    lock = path.parent / f".{path.name}.lock"
    try:
        with mkdir_lock(
            lock, timeout=5.0, stale_after=30.0, conflict_worker="profiles"
        ):
            yield
    except LeaseConflict as e:
        raise ProfileLockTimeout(lock=str(lock)) from e


def validate_profile_name(name: str) -> str:
    n = (name or "").strip()
    if not _NAME_RE.match(n):
        raise InvalidRequest(
            "invalid profile name (want ^[a-z][a-z0-9_-]{0,63}$)",
            name=name,
        )
    return n


def validate_site(site: str) -> str:
    s = (site or "").strip().lower()
    if not s or not _SITE_RE.match(s):
        raise InvalidRequest(
            "invalid site (want lowercase domain/slug, e.g. x.ai or cpa-manager)",
            site=site,
        )
    return s


def validate_account(account: str | None) -> str | None:
    if account is None:
        return None
    a = account.strip()
    if not a:
        return None
    if not _ACCOUNT_RE.match(a):
        raise InvalidRequest("invalid account label", account=account)
    if _FORBIDDEN_FIELD_RE.search(a) and ":" in a:
        raise InvalidRequest("account label looks like a secret field", account=account)
    return a


def _reject_secret_keys(obj: dict[str, Any], *, where: str) -> None:
    for k in obj:
        if _FORBIDDEN_FIELD_RE.search(str(k)):
            raise InvalidRequest(
                f"forbidden secret-like field {k!r} in {where}",
                field=k,
            )


def validate_launch(launch: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(launch, dict) or not launch:
        raise InvalidRequest("launch selector must be a non-empty object")
    _reject_secret_keys(launch, where="launch")
    unknown = set(launch) - _LAUNCH_KEYS
    if unknown:
        raise InvalidRequest(
            f"unknown launch keys: {sorted(unknown)}",
            unknown=sorted(unknown),
        )
    kind = str(launch.get("kind") or "").strip().lower()
    if kind not in _LAUNCH_KINDS:
        raise InvalidRequest(
            "launch.kind must be one of xai|scratch|adhoc|vpn",
            kind=launch.get("kind"),
        )
    out: dict[str, Any] = {"kind": kind}

    def _opt_str(key: str) -> None:
        if key not in launch or launch[key] is None:
            return
        val = str(launch[key]).strip()
        if not val:
            return
        out[key] = val

    def _opt_bool(key: str) -> None:
        if key not in launch or launch[key] is None:
            return
        out[key] = bool(launch[key])

    def _opt_num(key: str, *, as_int: bool = False) -> None:
        if key not in launch or launch[key] is None:
            return
        try:
            out[key] = int(launch[key]) if as_int else float(launch[key])
        except (TypeError, ValueError) as e:
            raise InvalidRequest(f"launch.{key} must be a number", key=key) from e

    if kind == "xai":
        email = str(launch.get("email") or "").strip()
        if not email or "@" not in email:
            raise InvalidRequest("xai launch requires email", kind=kind)
        out["email"] = email
        _opt_str("worker")
        _opt_bool("no_start")  # identity_ops ensure supports bind-only
    elif kind in ("scratch", "adhoc"):
        _opt_str("label")
        _opt_str("worker")
        _opt_num("cdp_port", as_int=True)
        _opt_bool("headed")
        _opt_bool("no_start")
    elif kind == "vpn":
        _opt_str("worker")
        _opt_str("country")
        _opt_str("city")
        _opt_bool("attach_only")
        if not out.get("worker") and not out.get("country"):
            raise InvalidRequest(
                "vpn launch requires worker and/or country",
                kind=kind,
            )
    if "egress" in launch and launch["egress"] is not None:
        out["egress"] = validate_egress(launch["egress"])
    return out


def _empty_registry() -> dict[str, Any]:
    return {"version": PROFILES_SCHEMA_VERSION, "profiles": {}}


def _normalize_association(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise InvalidRequest("association must be an object with site")
    _reject_secret_keys(raw, where="association")
    site = validate_site(str(raw.get("site") or ""))
    account = validate_account(
        None if raw.get("account") is None else str(raw.get("account"))
    )
    assoc: dict[str, str] = {"site": site}
    if account is not None:
        assoc["account"] = account
    return assoc


def _public_profile(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": name,
        "launch": dict(entry.get("launch") or {}),
        "associations": list(entry.get("associations") or []),
    }
    if entry.get("description"):
        out["description"] = entry["description"]
    if entry.get("notes"):
        out["notes"] = entry["notes"]
    if entry.get("last_verified_at"):
        out["last_verified_at"] = entry["last_verified_at"]
    if entry.get("created_at"):
        out["created_at"] = entry["created_at"]
    if entry.get("updated_at"):
        out["updated_at"] = entry["updated_at"]
    return out


def _assoc_label(assoc: dict[str, Any]) -> str:
    site = assoc.get("site") or "?"
    account = assoc.get("account")
    return f"{site} / {account}" if account else str(site)


def _egress_label(launch: dict[str, Any]) -> str:
    egress = launch.get("egress")
    if egress is None or egress == "direct":
        return "direct"
    if isinstance(egress, dict):
        bits = [str(egress.get("type") or "vpn")]
        if egress.get("country"):
            bits.append(str(egress["country"]))
        if egress.get("city"):
            bits.append(str(egress["city"]))
        return " ".join(bits)
    return str(egress)


def card_markdown(name: str, entry: dict[str, Any]) -> str:
    """Navigator-inject face card. No secrets."""
    launch = dict(entry.get("launch") or {})
    lines = [f"## {name}", ""]
    desc = str(entry.get("description") or "").strip()
    if desc:
        lines.append(desc)
        lines.append("")
    lines.append(f"- kind: {launch.get('kind', '?')}")
    lines.append(f"- egress: {_egress_label(launch)}")
    if entry.get("last_verified_at"):
        lines.append(f"- last_verified_at: {entry['last_verified_at']}")
    if entry.get("notes"):
        lines.append(f"- notes: {entry['notes']}")
    assocs = []
    for raw in entry.get("associations") or []:
        try:
            assocs.append(_assoc_label(_normalize_association(raw)))
        except InvalidRequest:
            continue
    if assocs:
        lines.append(f"- associations: {', '.join(assocs)}")
    return "\n".join(lines).rstrip() + "\n"


def load_registry(root: Path | str | None = None) -> dict[str, Any]:
    path = profiles_registry_path(root)
    raw = read_json(path)
    if not isinstance(raw, dict):
        return _empty_registry()
    version = raw.get("version", PROFILES_SCHEMA_VERSION)
    if version != PROFILES_SCHEMA_VERSION:
        raise InvalidRequest(
            f"unsupported PROFILES.json version {version}",
            version=version,
            supported=PROFILES_SCHEMA_VERSION,
        )
    profiles = raw.get("profiles")
    if profiles is None:
        profiles = {}
    if not isinstance(profiles, dict):
        raise InvalidRequest("PROFILES.json profiles must be an object")
    return {"version": PROFILES_SCHEMA_VERSION, "profiles": profiles}


def save_registry(registry: dict[str, Any], root: Path | str | None = None) -> Path:
    path = profiles_registry_path(root)
    payload = {
        "version": PROFILES_SCHEMA_VERSION,
        "profiles": registry.get("profiles") or {},
    }
    return atomic_write_json(path, payload)


class ProfileRegistry:
    """CRUD + exact resolve over profiles/PROFILES.json."""

    def __init__(self, root: Path | str | None = None):
        self.root = resolve_root(root)
        self.path = profiles_registry_path(self.root)

    def list_profiles(self) -> list[dict[str, Any]]:
        reg = load_registry(self.root)
        names = sorted(reg["profiles"].keys())
        return [_public_profile(n, reg["profiles"][n]) for n in names]

    def show(self, name: str) -> dict[str, Any]:
        name = validate_profile_name(name)
        reg = load_registry(self.root)
        entry = reg["profiles"].get(name)
        if not isinstance(entry, dict):
            raise ProfileNotFound(name=name)
        return _public_profile(name, entry)

    def register(
        self,
        name: str,
        *,
        launch: dict[str, Any],
        replace: bool = False,
        description: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        name = validate_profile_name(name)
        launch_n = validate_launch(launch)
        desc_n = validate_description(description, required=False)
        notes_n = validate_notes(notes) if notes is not None else None
        with _registry_lock(self.path):
            reg = load_registry(self.root)
            existing = reg["profiles"].get(name)
            if isinstance(existing, dict) and not replace:
                raise InvalidRequest(
                    f"profile {name!r} already registered (pass --replace to overwrite launch)",
                    name=name,
                )
            now = _now_iso()
            if isinstance(existing, dict):
                entry = dict(existing)
                entry["launch"] = launch_n
                entry["updated_at"] = now
                entry.setdefault("associations", [])
                entry.setdefault("created_at", now)
                if desc_n is not None:
                    entry["description"] = desc_n
                if notes is not None:
                    if notes_n is None:
                        entry.pop("notes", None)
                    else:
                        entry["notes"] = notes_n
            else:
                if desc_n is None:
                    raise InvalidRequest(
                        "profile description is required",
                        name=name,
                    )
                entry = {
                    "description": desc_n,
                    "launch": launch_n,
                    "associations": [],
                    "created_at": now,
                    "updated_at": now,
                }
                if notes_n is not None:
                    entry["notes"] = notes_n
            if not str(entry.get("description") or "").strip():
                raise InvalidRequest(
                    "profile description is required",
                    name=name,
                )
            reg["profiles"][name] = entry
            save_registry(reg, self.root)
            return _public_profile(name, entry)

    def stamp_verified(self, name: str) -> dict[str, Any]:
        name = validate_profile_name(name)
        with _registry_lock(self.path):
            reg = load_registry(self.root)
            entry = reg["profiles"].get(name)
            if not isinstance(entry, dict):
                raise ProfileNotFound(name=name)
            entry = dict(entry)
            now = _now_iso()
            entry["last_verified_at"] = now
            entry["updated_at"] = now
            reg["profiles"][name] = entry
            save_registry(reg, self.root)
            return _public_profile(name, entry)

    def card(self, name: str) -> str:
        name = validate_profile_name(name)
        reg = load_registry(self.root)
        entry = reg["profiles"].get(name)
        if not isinstance(entry, dict):
            raise ProfileNotFound(name=name)
        return card_markdown(name, entry)

    def cards(self) -> str:
        reg = load_registry(self.root)
        names = sorted(reg["profiles"].keys())
        parts = [
            card_markdown(n, reg["profiles"][n])
            for n in names
            if isinstance(reg["profiles"].get(n), dict)
        ]
        return "\n".join(p.rstrip() for p in parts).rstrip() + ("\n" if parts else "")

    def associate(
        self,
        name: str,
        site: str,
        account: str | None = None,
    ) -> dict[str, Any]:
        name = validate_profile_name(name)
        site = validate_site(site)
        account = validate_account(account)
        assoc: dict[str, str] = {"site": site}
        if account is not None:
            assoc["account"] = account
        with _registry_lock(self.path):
            reg = load_registry(self.root)
            entry = reg["profiles"].get(name)
            if not isinstance(entry, dict):
                raise ProfileNotFound(name=name)
            entry = dict(entry)
            assocs = [
                _normalize_association(a) for a in (entry.get("associations") or [])
            ]
            if assoc not in assocs:
                assocs.append(assoc)
            entry["associations"] = assocs
            entry["updated_at"] = _now_iso()
            reg["profiles"][name] = entry
            save_registry(reg, self.root)
            return _public_profile(name, entry)

    def resolve(
        self,
        site: str,
        *,
        account: str | None = None,
    ) -> dict[str, Any]:
        """
        Exact, deterministic resolve. Never fuzzy.

        - site must match association.site exactly (after normalize).
        - with --account: only associations whose account equals it (accountless
          associations do not match).
        - without --account: only accountless associations match. Profiles that
          only have account-scoped rows for the site → PROFILE_NOT_FOUND with
          hint to pass --account. Never promote an account-scoped row when
          account is omitted.
        - multiple matching profiles → PROFILE_AMBIGUOUS.
        """
        site = validate_site(site)
        account = validate_account(account)
        reg = load_registry(self.root)
        matched_names: list[str] = []
        saw_account_scoped_only = False

        for pname, entry in reg["profiles"].items():
            if not isinstance(entry, dict):
                continue
            site_hit = False
            accountless_hit = False
            account_hit = False
            for raw in entry.get("associations") or []:
                try:
                    assoc = _normalize_association(raw)
                except InvalidRequest:
                    continue
                if assoc["site"] != site:
                    continue
                site_hit = True
                assoc_acct = assoc.get("account")
                if account is not None:
                    if assoc_acct is not None and assoc_acct == account:
                        account_hit = True
                else:
                    if assoc_acct is None:
                        accountless_hit = True
            if account is not None:
                if account_hit and pname not in matched_names:
                    matched_names.append(pname)
            else:
                if accountless_hit and pname not in matched_names:
                    matched_names.append(pname)
                elif site_hit and not accountless_hit:
                    saw_account_scoped_only = True

        matched_names.sort()
        if not matched_names:
            hint = None
            msg = None
            if account is None and saw_account_scoped_only:
                hint = "pass --account; only account-scoped associations exist for this site"
                msg = (
                    f"no accountless profile for site={site!r}; "
                    "use --account to resolve account-scoped associations"
                )
            raise ProfileNotFound(site=site, account=account, message=msg, hint=hint)
        if len(matched_names) > 1:
            raise ProfileAmbiguous(site=site, account=account, matches=matched_names)

        name = matched_names[0]
        profile = _public_profile(name, reg["profiles"][name])
        launch = dict(profile["launch"])
        return {
            "ok": True,
            "profile": profile,
            "name": name,
            "site": site,
            "account": account,
            "launch": launch,
            "launch_argv": launch_to_argv(launch),
        }


def launch_to_argv(launch: dict[str, Any]) -> list[str]:
    """Exact browserctl argv fragment after the program name (starts with 'launch').

    egress never switches kind. country/city flags are emitted only when
    kind is already vpn (from launch or egress geo). scratch+egress vpn
    stays a scratch argv; the object is for cards/harness.
    """
    launch = validate_launch(launch)
    kind = str(launch["kind"])
    argv: list[str] = ["launch", "--kind", kind]
    flag_map = [
        ("email", "--email"),
        ("worker", "--worker"),
        ("label", "--label"),
        ("cdp_port", "--cdp-port"),
    ]
    for key, flag in flag_map:
        if key in launch and launch[key] is not None:
            argv.extend([flag, str(launch[key])])
    if kind == "vpn":
        country = launch.get("country")
        city = launch.get("city")
        egress = launch.get("egress")
        if isinstance(egress, dict) and egress.get("type") == "vpn":
            country = country or egress.get("country")
            city = city or egress.get("city")
        if country:
            argv.extend(["--country", str(country)])
        if city:
            argv.extend(["--city", str(city)])
    if launch.get("headed"):
        argv.append("--headed")
    if launch.get("attach_only"):
        argv.append("--attach-only")
    if launch.get("no_start"):
        argv.append("--no-start")
    return argv
