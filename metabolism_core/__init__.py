"""Pure terminal-quota classification and strike-ledger semantics.

Library only — no network, no CPA calls, no keys, no filesystem paths, and no
deletion. Callers own persistence of strike state and any explicit follow-up
actions when ``action_required`` is true.
"""

from __future__ import annotations

from .classify import (
    QUOTA_EXHAUSTION_CANDIDATE_STRINGS,
    TERMINAL_STRIKE_REQUIRED,
    TERMINAL_WEEKLY_QUOTA_CLASS,
    TERMINAL_WEEKLY_QUOTA_STATUS_CODES,
    TERMINAL_WEEKLY_QUOTA_STRINGS,
    classify_terminal_probe_response,
    extract_auth_file_signal_text,
    is_terminal_quota_probe,
    match_quota_exhaustion_candidate_strings,
    redact_probe_text,
    select_probe_candidate_from_auth_file,
)
from .strikes import (
    apply_terminal_strike_transition,
    empty_strike_state,
    normalize_strike_state,
)

__all__ = [
    "QUOTA_EXHAUSTION_CANDIDATE_STRINGS",
    "TERMINAL_STRIKE_REQUIRED",
    "TERMINAL_WEEKLY_QUOTA_CLASS",
    "TERMINAL_WEEKLY_QUOTA_STATUS_CODES",
    "TERMINAL_WEEKLY_QUOTA_STRINGS",
    "apply_terminal_strike_transition",
    "classify_terminal_probe_response",
    "empty_strike_state",
    "extract_auth_file_signal_text",
    "is_terminal_quota_probe",
    "match_quota_exhaustion_candidate_strings",
    "normalize_strike_state",
    "redact_probe_text",
    "select_probe_candidate_from_auth_file",
]

__version__ = "0.1.0"
