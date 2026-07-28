"""Pure classifiers for management candidacy and pinned probe responses.

No network I/O. Billing / used_pct is never a candidacy trigger. Only the
pinned weekly-quota class is terminal; deletion is never performed here.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

TERMINAL_STRIKE_REQUIRED = 3
TERMINAL_WEEKLY_QUOTA_STATUS_CODES = frozenset({400, 403})
TERMINAL_WEEKLY_QUOTA_CLASS = "terminal_weekly_quota_exhausted"
TERMINAL_WEEKLY_QUOTA_STRINGS = (
    "weekly quota exhausted",
    "weekly quota has been exhausted",
    "weekly quota is exhausted",
    "weekly limit exhausted",
    "weekly limit has been exhausted",
    "weekly response limit",
    "weekly message limit",
    "weekly usage limit",
    "you have reached your weekly",
    "you've reached your weekly",
    "you have exceeded your weekly",
    "you've exceeded your weekly",
    "exceeded the weekly limit",
    "reached the weekly limit",
)

# Auth-file metadata candidacy (fail-closed): only these strings in
# status/message/error fields nominate a probe. Counters alone never nominate.
# Broader than the terminal probe class so management shorthand still surfaces
# candidates; the pinned probe remains the terminal oracle.
QUOTA_EXHAUSTION_CANDIDATE_STRINGS = TERMINAL_WEEKLY_QUOTA_STRINGS + (
    "quota exhausted",
    "quota has been exhausted",
    "quota is exhausted",
    "out of quota",
    "no remaining quota",
    "weekly quota",
    "weekly limit",
)

_SECRET_PATTERNS = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.I), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[JWT_REDACTED]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL_REDACTED]"),
)


def redact_probe_text(value: object, *, max_len: int = 500) -> str:
    """Return a bounded, secret-safe string for classifier evidence."""
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:max_len]


def _extract_error_text(parsed_body: object) -> str:
    """Extract non-secret classifier text from known upstream error shapes."""
    if isinstance(parsed_body, dict):
        parts: list[str] = []
        for key in ("error", "message", "detail", "reason", "code", "type"):
            val = parsed_body.get(key)
            if isinstance(val, (str, int, float)):
                parts.append(str(val))
            elif isinstance(val, dict):
                parts.append(_extract_error_text(val))
        return " ".join(p for p in parts if p)
    if isinstance(parsed_body, list):
        return " ".join(_extract_error_text(item) for item in parsed_body)
    if isinstance(parsed_body, (str, int, float)):
        return str(parsed_body)
    return ""


def _extract_assistant_content(parsed_body: object) -> str:
    """Extract assistant content from OpenAI-compatible chat completions."""
    if not isinstance(parsed_body, dict):
        return ""
    choices = parsed_body.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else {}
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def _terminal_weekly_match(status_code: int | None, classifier_text: str) -> list[str]:
    """Return matched terminal weekly quota strings for terminal-class responses."""
    if status_code not in TERMINAL_WEEKLY_QUOTA_STATUS_CODES:
        return []
    lower = classifier_text.lower()
    return [needle for needle in TERMINAL_WEEKLY_QUOTA_STRINGS if needle in lower]


def classify_terminal_probe_response(resp: dict[str, Any]) -> dict[str, Any]:
    """Classify a pinned chat probe response.

    Fail-closed: only ``terminal_weekly_quota_exhausted`` is terminal-class.
    HTTP 200 with nonempty assistant content always keeps the account.
    This function never deletes anything; ``delete_allowed`` is retained as a
    terminal-class flag for compatibility with legacy tests/callers and means
    "probe is terminal-class", not "caller should delete".
    """
    status_code = resp.get("status_code")
    try:
        status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code = None

    body = resp.get("body") or ""
    evidence: dict[str, Any] = {
        "probe": "pinned_chat_completions",
        "upstream_status_code": status_code,
        "classifier": "keep_unknown_response",
        "delete_allowed": False,
        "body_sha256": (
            hashlib.sha256(str(body).encode("utf-8", "replace")).hexdigest() if body else None
        ),
        "body_excerpt": redact_probe_text(body),
        "terminal_status_codes": sorted(TERMINAL_WEEKLY_QUOTA_STATUS_CODES),
        "terminal_strings": list(TERMINAL_WEEKLY_QUOTA_STRINGS),
    }

    if status_code is None:
        return evidence

    try:
        parsed = json.loads(body) if body else {}
    except (TypeError, json.JSONDecodeError) as e:
        evidence.update(
            {
                "classifier": "keep_parse_error",
                "parse_error": redact_probe_text(e, max_len=180),
            }
        )
        return evidence

    if status_code == 200:
        content = _extract_assistant_content(parsed)
        evidence["assistant_content_present"] = bool(content.strip())
        evidence["assistant_content_excerpt"] = redact_probe_text(content, max_len=120)
        if content.strip():
            evidence["classifier"] = "keep_success_200_assistant_content"
        else:
            evidence["classifier"] = "keep_200_empty_assistant_content"
        return evidence

    classifier_text = _extract_error_text(parsed)
    evidence["error_excerpt"] = redact_probe_text(classifier_text, max_len=300)

    if status_code == 429 or re.search(
        r"\b(rate limit|too many requests|temporarily rate limited)\b",
        classifier_text,
        re.I,
    ):
        evidence["classifier"] = "keep_rate_limit"
        return evidence

    if status_code in {401, 419} or re.search(
        r"\b(expired|bad credentials|bad credential|invalid[_ -]?grant|"
        r"invalid[_ -]?token|unauthori[sz]ed|auth(?:entication)? failed)\b",
        classifier_text,
        re.I,
    ):
        evidence["classifier"] = "keep_auth_expiry_or_bad_credentials"
        return evidence

    if re.search(
        r"\b(access denied|permission[-_ ]denied|permission denied|forbidden|bot_flag_source)\b",
        classifier_text,
        re.I,
    ):
        evidence["classifier"] = "keep_access_denied"
        return evidence

    matches = _terminal_weekly_match(status_code, classifier_text)
    if matches:
        evidence.update(
            {
                "classifier": TERMINAL_WEEKLY_QUOTA_CLASS,
                "delete_allowed": True,
                "matched_terminal_strings": matches,
            }
        )
        return evidence

    if status_code in {408, 425} or 500 <= status_code <= 599:
        evidence["classifier"] = "keep_transient_or_server_error"
    elif 400 <= status_code <= 499:
        evidence["classifier"] = "keep_unknown_4xx"
    else:
        evidence["classifier"] = "keep_unknown_response"
    return evidence


def is_terminal_quota_probe(probe: dict[str, Any] | None) -> bool:
    """True when probe is the terminal weekly-quota class."""
    return bool(
        probe
        and probe.get("delete_allowed")
        and probe.get("classifier") == TERMINAL_WEEKLY_QUOTA_CLASS
    )


def _walk_metadata_strings(value: object, *, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out.extend(_walk_metadata_strings(v, depth=depth + 1))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_walk_metadata_strings(item, depth=depth + 1))
        return out
    return []


def extract_auth_file_signal_text(auth_file: dict[str, Any]) -> str:
    """Build classifier text from management auth-file fields (no counters alone).

    Prefer explicit error/status/message-like keys. Fail closed: unknown shapes
    that lack textual error/status do not produce quota-like matches.
    Billing fields such as ``used_pct`` are ignored even if present.
    """
    if not isinstance(auth_file, dict):
        return ""

    primary_keys = (
        "status",
        "status_message",
        "message",
        "error",
        "error_message",
        "last_error",
        "detail",
        "reason",
        "unavailable_reason",
        "state",
    )
    parts: list[str] = []
    for key in primary_keys:
        if key not in auth_file:
            continue
        parts.extend(_walk_metadata_strings(auth_file.get(key)))

    for key in ("errors", "last_error_detail", "diagnostics", "meta", "metadata"):
        if key not in auth_file:
            continue
        parts.extend(_walk_metadata_strings(auth_file.get(key)))

    if auth_file.get("unavailable") is True and "unavailable" not in {p.lower() for p in parts}:
        parts.append("unavailable")

    seen: set[str] = set()
    ordered: list[str] = []
    for p in parts:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(p)
    return " ".join(ordered)


def match_quota_exhaustion_candidate_strings(text: str) -> list[str]:
    """Return quota-exhaustion-like needles found in management metadata text.

    Underscores/hyphens are treated like spaces so status tokens such as
    ``weekly_quota_exhausted`` still match the spaced needle list.
    """
    if not text:
        return []
    lower = text.lower()
    normalized = re.sub(r"[_\-]+", " ", lower)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    haystacks = (lower, normalized)
    return [
        needle
        for needle in QUOTA_EXHAUSTION_CANDIDATE_STRINGS
        if any(needle in h for h in haystacks)
    ]


def select_probe_candidate_from_auth_file(auth_file: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed candidacy from one management auth-file row.

    Nominates only when status/message/error text indicates possible quota
    exhaustion. Counters (failed/success/recent_requests), disabled alone,
    billing %, and bare non-quota error statuses do NOT nominate.
    """
    signal_text = extract_auth_file_signal_text(
        auth_file if isinstance(auth_file, dict) else {}
    )
    matches = match_quota_exhaustion_candidate_strings(signal_text)
    reasons: list[str] = []
    if matches:
        reasons.append("quota_like_metadata")
        for m in matches:
            tag = "meta_match:" + re.sub(r"[^a-z0-9]+", "_", m.lower()).strip("_")
            if tag not in reasons:
                reasons.append(tag)
    return {
        "discard_candidate": bool(matches),
        "candidate_reasons": reasons,
        "matched_quota_strings": matches,
        "signal_text": redact_probe_text(signal_text, max_len=400),
        "signal_text_raw_len": len(signal_text),
    }
