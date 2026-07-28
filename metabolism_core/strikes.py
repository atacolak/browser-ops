"""Pure in-memory terminal strike-ledger transitions.

Caller supplies prior state, run_id, and auth identity. This module never
touches the filesystem, never deletes accounts, and never issues network
calls. When consecutive terminal strikes reach ``TERMINAL_STRIKE_REQUIRED``,
the returned state sets ``action_required=True``; the caller decides what to do.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .classify import (
    TERMINAL_STRIKE_REQUIRED,
    TERMINAL_WEEKLY_QUOTA_CLASS,
    is_terminal_quota_probe,
    redact_probe_text,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty_strike_state(
    *,
    email: str | None = None,
    auth_id: str | None = None,
    auth_index: str | None = None,
) -> dict[str, Any]:
    """Canonical empty strike state (no prior evidence)."""
    return {
        "email": email,
        "auth_id": auth_id,
        "auth_index": auth_index,
        "strikes": [],
        "count": 0,
        "recorded": False,
        "reason": "empty",
        "required": TERMINAL_STRIKE_REQUIRED,
        "all_terminal": False,
        "action_required": False,
        # Legacy alias: means "threshold met for terminal class", not "delete now".
        "delete_allowed": False,
        "latest_probe_classifier": None,
    }


def normalize_strike_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Return a shallow-normalized copy of caller-supplied strike state."""
    if not isinstance(state, dict):
        return empty_strike_state()
    strikes = [s for s in (state.get("strikes") or []) if isinstance(s, dict)]
    out = empty_strike_state(
        email=state.get("email"),
        auth_id=state.get("auth_id"),
        auth_index=state.get("auth_index"),
    )
    out["strikes"] = deepcopy(strikes)
    out["count"] = len(strikes)
    out["recorded"] = bool(state.get("recorded", False))
    out["reason"] = state.get("reason") or "loaded"
    out["latest_probe_classifier"] = state.get("latest_probe_classifier")
    out["updated_at"] = state.get("updated_at")
    out["last_classifier"] = state.get("last_classifier")
    return out


def _identity_changed(
    entry: dict[str, Any],
    *,
    auth_id: str | None,
    auth_index: str | None,
) -> bool:
    """True when auth binding no longer matches stored strike identity."""
    if not entry:
        return False
    prev_index = entry.get("auth_index")
    prev_id = entry.get("auth_id")
    if auth_index is not None and prev_index is not None and str(auth_index) != str(prev_index):
        return True
    if auth_id is not None and prev_id is not None and str(auth_id) != str(prev_id):
        return True
    return False


def _secret_safe_strike_from_probe(
    probe: dict[str, Any],
    *,
    auth_id: str | None,
    auth_index: str | None,
    run_id: str,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Build one strike record: timestamps + classifier evidence, never tokens."""
    return {
        "recorded_at": recorded_at or _now_iso(),
        "run_id": run_id,
        "classifier": probe.get("classifier"),
        "upstream_status_code": probe.get("upstream_status_code"),
        "matched_terminal_strings": list(probe.get("matched_terminal_strings") or []),
        "body_sha256": probe.get("body_sha256"),
        "body_excerpt": redact_probe_text(probe.get("body_excerpt"), max_len=500),
        "error_excerpt": redact_probe_text(probe.get("error_excerpt"), max_len=300),
        "auth_id": auth_id,
        "auth_index": auth_index,
    }


def _finalize(
    *,
    email: str | None,
    auth_id: str | None,
    auth_index: str | None,
    strikes: list[dict[str, Any]],
    recorded: bool,
    reason: str,
    probe: dict[str, Any] | None,
    identity_reset: bool = False,
) -> dict[str, Any]:
    clean = [s for s in strikes if isinstance(s, dict)]
    all_terminal = bool(clean) and all(
        s.get("classifier") == TERMINAL_WEEKLY_QUOTA_CLASS for s in clean
    )
    count = len(clean)
    terminal_now = is_terminal_quota_probe(probe)
    action_required = bool(terminal_now and all_terminal and count >= TERMINAL_STRIKE_REQUIRED)
    state: dict[str, Any] = {
        "email": email,
        "auth_id": auth_id,
        "auth_index": auth_index,
        "strikes": clean,
        "count": count,
        "recorded": recorded,
        "reason": reason,
        "required": TERMINAL_STRIKE_REQUIRED,
        "all_terminal": all_terminal,
        "action_required": action_required,
        # Compatibility with legacy strike semantics: threshold met, no side effects.
        "delete_allowed": action_required,
        "latest_probe_classifier": (probe or {}).get("classifier"),
        "identity_reset": identity_reset,
    }
    return state


def apply_terminal_strike_transition(
    prior_state: dict[str, Any] | None,
    probe: dict[str, Any],
    *,
    run_id: str,
    email: str | None = None,
    auth_id: str | None = None,
    auth_index: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Apply one pure strike transition from caller-supplied state.

    Rules:
    - terminal weekly-quota class → at most one strike for this ``run_id``
    - same ``run_id`` cannot increment twice
    - auth identity (``auth_id`` / ``auth_index``) changes reset prior strikes
    - non-terminal / unknown / success classifications reset immediately
    - ``count >= TERMINAL_STRIKE_REQUIRED`` (default 3) → ``action_required=True``
      but this function performs no deletion and no I/O

    Returns a new state dict. Caller owns persistence.
    """
    if not isinstance(probe, dict):
        probe = {"classifier": "keep_unknown_response", "delete_allowed": False}

    prior = normalize_strike_state(prior_state)
    email = email if email is not None else prior.get("email")
    # Prefer explicit caller identity; fall back to prior only when not provided.
    if auth_id is None and prior_state is not None:
        auth_id = prior.get("auth_id")
    if auth_index is None and prior_state is not None:
        auth_index = prior.get("auth_index")

    if not is_terminal_quota_probe(probe):
        return _finalize(
            email=email,
            auth_id=auth_id,
            auth_index=auth_index,
            strikes=[],
            recorded=False,
            reason=f"reset:{probe.get('classifier') or 'unknown'}",
            probe=probe,
            identity_reset=False,
        )

    identity_reset = False
    strikes = list(prior.get("strikes") or [])
    if prior_state is not None and _identity_changed(
        prior, auth_id=auth_id, auth_index=auth_index
    ):
        identity_reset = True
        strikes = []

    if any(isinstance(s, dict) and s.get("run_id") == run_id for s in strikes):
        return _finalize(
            email=email,
            auth_id=auth_id if auth_id is not None else prior.get("auth_id"),
            auth_index=auth_index if auth_index is not None else prior.get("auth_index"),
            strikes=strikes,
            recorded=False,
            reason="already_recorded_this_run",
            probe=probe,
            identity_reset=identity_reset,
        )

    strike = _secret_safe_strike_from_probe(
        probe,
        auth_id=auth_id,
        auth_index=auth_index,
        run_id=run_id,
        recorded_at=now,
    )
    strikes.append(strike)
    # Cap retained evidence slightly above threshold for audit without unbounded growth.
    cap = max(TERMINAL_STRIKE_REQUIRED + 2, TERMINAL_STRIKE_REQUIRED)
    strikes = strikes[-cap:]

    state = _finalize(
        email=email,
        auth_id=auth_id,
        auth_index=auth_index,
        strikes=strikes,
        recorded=True,
        reason="recorded" if not identity_reset else "recorded_after_identity_reset",
        probe=probe,
        identity_reset=identity_reset,
    )
    state["updated_at"] = now or _now_iso()
    state["last_classifier"] = probe.get("classifier")
    return state
