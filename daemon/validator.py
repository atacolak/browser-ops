"""
Independent Validator VLM — judges browser action completion from screenshots.

Implements the Validator role from the Policy-Localizer-Validator pattern.
The validator model MUST be different from the planner/author model to prevent
self-grading (anti-gaming).

Two validation paths:

1. **Deterministic (default)** — extracts page text via CDP's
   ``document.body.innerText`` and evaluates verify assertions using
   string-based checks (no ``eval()``, no LLM).

2. **VLM** (stub) — if ``VALIDATOR_MODEL`` env var is set **and** differs
   from ``NAVIGATOR_MODEL``, logs intent and falls back to deterministic.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Anti-gaming config ──────────────────────────────────────────────────────

VALIDATOR_MODEL = os.environ.get("VALIDATOR_MODEL", "gpt-4o-mini")
NAVIGATOR_MODEL = os.environ.get("NAVIGATOR_MODEL", "claude-sonnet-4-20250514")


def _check_anti_gaming() -> None:
    """Log a warning if validator and navigator share the same model.

    In deterministic mode this is irrelevant (no LLM self-grading).
    In VLM mode a single model would allow the planner to grade its own
    work, breaking the anti-gaming guarantee.
    """
    if VALIDATOR_MODEL == NAVIGATOR_MODEL:
        logger.warning(
            "VALIDATOR_MODEL (%s) == NAVIGATOR_MODEL (%s). "
            "In VLM mode this would allow self-grading. "
            "Deterministic mode is unaffected.",
            VALIDATOR_MODEL,
            NAVIGATOR_MODEL,
        )


# ── Result dataclass ────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Result of a single validation check.

    Attributes:
        complete: Is the task complete according to the assertions?
        confidence: 0.0 (no confidence) to 1.0 (fully confident).
        reasoning: Human-readable explanation of the result.
        assertions_passed: Which verify assertions passed.
        assertions_failed: Which verify assertions failed.
    """

    complete: bool = False
    confidence: float = 0.0
    reasoning: str = ""
    assertions_passed: list[str] = field(default_factory=list)
    assertions_failed: list[str] = field(default_factory=list)


# ── Assertion parser (string-based, no eval()) ──────────────────────────────


def _resolve_variable(
    var_name: str,
    variables: dict[str, Any],
) -> tuple[Any, bool]:
    """Resolve *var_name* from *variables*, returning ``(value, found)``.

    When a variable is not in the namespace, returns ``(None, False)`` so
    callers can distinguish "missing variable" from "value is None".
    """
    if var_name in variables:
        return variables[var_name], True
    return None, False


def _check_single_assertion(
    expr: str,
    variables: dict[str, Any],
) -> tuple[bool, str]:
    """Evaluate a single assertion expression against *variables*.

    All parsing is string-based.  No ``eval()`` is used anywhere.

    Supported patterns (checked in order):

    * ``len(varname) > N``       — sequence/dict length exceeds threshold
    * ``varname != 'value'``     — inequality (single or double quotes)
    * ``varname == 'value'``     — equality (single or double quotes)
    * ``varname != None``        — not-None check
    * ``varname`` (bare word)    — truthy check

    **Missing variables**: when the referenced variable is not in the
    *variables* namespace, the assertion **fails** with a diagnostic
    message.  This prevents false positives from ``str(None)`` accidental
    matches.

    Returns:
        ``(ok: bool, reason: str)`` — reason is a human-readable description
        of what was checked and the actual value.
    """
    expr = expr.strip()
    if not expr:
        return False, "Empty assertion expression"

    # ── len(varname) > N ────────────────────────────────────────────────
    m = re.match(r"^len\((\w+)\)\s*>\s*(\d+)$", expr)
    if m:
        var_name = m.group(1)
        threshold = int(m.group(2))
        val, found = _resolve_variable(var_name, variables)
        if not found:
            return False, (
                f"Variable '{var_name}' not found in validation namespace. "
                f"Available: {list(variables.keys())}"
            )
        if isinstance(val, (list, dict, str, tuple, set, frozenset)):
            actual_len = len(val)
            ok = actual_len > threshold
            return ok, (
                f"len({var_name}) = {actual_len} "
                f"({'≻' if ok else '≯'}) {threshold}"
            )
        return False, (
            f"len({var_name}) not applicable to "
            f"type {type(val).__name__}"
        )

    # ── varname != 'value' (single quotes) ──────────────────────────────
    m = re.match(r"^(\w+)\s*!=\s*'([^']*)'$", expr)
    if m:
        var_name = m.group(1)
        not_expected = m.group(2)
        val, found = _resolve_variable(var_name, variables)
        if not found:
            return False, (
                f"Variable '{var_name}' not found in validation namespace. "
                f"Available: {list(variables.keys())}"
            )
        actual = str(val)
        ok = actual != not_expected
        return ok, (
            f"{var_name} = {actual!r} "
            f"({'!=' if ok else '=='}) {not_expected!r}"
        )

    # ── varname != "value" (double quotes) ──────────────────────────────
    m = re.match(r'^(\w+)\s*!=\s*"([^"]*)"$', expr)
    if m:
        var_name = m.group(1)
        not_expected = m.group(2)
        val, found = _resolve_variable(var_name, variables)
        if not found:
            return False, (
                f"Variable '{var_name}' not found in validation namespace. "
                f"Available: {list(variables.keys())}"
            )
        actual = str(val)
        ok = actual != not_expected
        return ok, (
            f"{var_name} = {actual!r} "
            f"({'!=' if ok else '=='}) {not_expected!r}"
        )

    # ── varname == 'value' (single quotes) ──────────────────────────────
    m = re.match(r"^(\w+)\s*==\s*'([^']*)'$", expr)
    if m:
        var_name = m.group(1)
        expected = m.group(2)
        val, found = _resolve_variable(var_name, variables)
        if not found:
            return False, (
                f"Variable '{var_name}' not found in validation namespace. "
                f"Available: {list(variables.keys())}"
            )
        actual = str(val)
        ok = actual == expected
        return ok, (
            f"{var_name} = {actual!r} "
            f"({'==' if ok else '!='}) {expected!r}"
        )

    # ── varname == "value" (double quotes) ──────────────────────────────
    m = re.match(r'^(\w+)\s*==\s*"([^"]*)"$', expr)
    if m:
        var_name = m.group(1)
        expected = m.group(2)
        val, found = _resolve_variable(var_name, variables)
        if not found:
            return False, (
                f"Variable '{var_name}' not found in validation namespace. "
                f"Available: {list(variables.keys())}"
            )
        actual = str(val)
        ok = actual == expected
        return ok, (
            f"{var_name} = {actual!r} "
            f"({'==' if ok else '!='}) {expected!r}"
        )

    # ── varname != None ─────────────────────────────────────────────────
    m = re.match(r"^(\w+)\s*!=\s*None$", expr)
    if m:
        var_name = m.group(1)
        val, found = _resolve_variable(var_name, variables)
        if not found:
            return False, (
                f"Variable '{var_name}' not found in validation namespace. "
                f"Available: {list(variables.keys())}"
            )
        ok = val is not None
        return ok, (
            f"{var_name} is {'not None' if ok else 'None'}"
        )

    # ── Bare variable name — truthy check ───────────────────────────────
    m = re.match(r"^(\w+)$", expr)
    if m:
        var_name = m.group(1)
        val, found = _resolve_variable(var_name, variables)
        if not found:
            return False, (
                f"Variable '{var_name}' not found in validation namespace. "
                f"Available: {list(variables.keys())}"
            )
        ok = bool(val)
        return ok, (
            f"{var_name} = {val!r} ({'truthy' if ok else 'falsy'})"
        )

    return False, f"Unsupported assertion pattern: {expr!r}"


def _extract_variables_from_page_text(
    page_text: str,
    task_description: str = "",
) -> dict[str, Any]:
    """Extract a variable namespace from raw page text.

    Returns:
    * ``page_text`` — the full visible text content
    * ``text_length`` — character count of page_text
    * Any key-value pairs found in the page text matching common patterns
      like ``Key: value``, ``Key\nvalue``, etc.

    The goal is to make domain-specific variables (e.g. ``fine_stage``)
    available to the assertion checker even when only raw page text is
    available (no procedure-generated variables).
    """
    variables: dict[str, Any] = {
        "page_text": page_text,
        "text_length": len(page_text),
    }

    # ── Heuristic: key-value extraction from page text ──────────────────
    # Match lines like:  "Fine Stage: Infringement Notice"
    #                    "Fine Stage  Infringement Notice"
    # Produces variable: fine_stage = "Infringement Notice"
    for line in page_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # "Key: value" pattern
        m = re.match(r"^([A-Za-z][A-Za-z\s_-]{1,60}?)\s*:\s+(.+)$", line)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_").replace("-", "_")
            value = m.group(2).strip()
            # Only set if not already populated (first occurrence wins)
            if key not in variables:
                variables[key] = value
        else:
            # "Key  value" pattern (whitespace separator, no colon)
            m = re.match(r"^([A-Za-z][A-Za-z\s_-]{1,60}?)\s{2,}(.+)$", line)
            if m:
                key = m.group(1).strip().lower().replace(" ", "_").replace("-", "_")
                value = m.group(2).strip()
                if key not in variables:
                    variables[key] = value

    return variables


# ── Public validation functions ─────────────────────────────────────────────


def validate_from_page_state(
    page_text: str,
    assertions: list[str],
    task_description: str = "",
) -> ValidationResult:
    """Evaluate verify assertions against extracted page text.

    This is the **deterministic** (no-VLM) validation path.  It extracts
    values from *page_text* and evaluates each assertion using pure
    string-based checks.

    Args:
        page_text: The visible text content of the page
            (e.g. ``document.body.innerText``).
        assertions: List of assertion strings (e.g.
            ``"fine_stage != 'Infringement Notice'"``).
        task_description: Optional human-readable task description
            (reserved for future VLM use).

    Returns:
        A ``ValidationResult`` with per-assertion pass/fail details.
    """
    _check_anti_gaming()

    variables = _extract_variables_from_page_text(page_text, task_description)

    passed: list[str] = []
    failed: list[str] = []

    for expr in assertions:
        ok, reason = _check_single_assertion(expr, variables)
        if ok:
            passed.append(expr)
        else:
            failed.append(expr)

    total = len(assertions)
    passed_count = len(passed)

    if total == 0:
        return ValidationResult(
            complete=False,
            confidence=0.0,
            reasoning="No assertions provided to validate.",
            assertions_passed=[],
            assertions_failed=[],
        )

    complete = passed_count == total
    confidence = passed_count / total

    if complete:
        reasoning = (
            f"All {total} assertion(s) passed. "
            f"Task appears complete."
        )
    else:
        reasoning = (
            f"{passed_count}/{total} assertion(s) passed. "
            f"Failed: {', '.join(failed)}. "
            f"Task may not be complete."
        )

    return ValidationResult(
        complete=complete,
        confidence=confidence,
        reasoning=reasoning,
        assertions_passed=passed,
        assertions_failed=failed,
    )


async def validate_screenshot(
    screenshot_path: str,
    assertions: list[str],
    task_description: str = "",
    cdp=None,
    session_id: str | None = None,
) -> ValidationResult:
    """Validate browser state from a screenshot.

    Two paths:

    1. **VLM** — if ``VALIDATOR_MODEL`` env var is set **and** differs from
       ``NAVIGATOR_MODEL``, attempts vision-language model validation
       (currently a stub that logs and falls through).

    2. **Deterministic (fallback)** — extracts page text via CDP
       ``Runtime.evaluate("document.body.innerText")``, then runs assertion
       checks.

    Args:
        screenshot_path: Path to the screenshot PNG file.
        assertions: List of assertion strings.
        task_description: Optional task description (used by VLM path).
        cdp: CDP client instance (needed for deterministic fallback).
        session_id: Current CDP session ID.

    Returns:
        A ``ValidationResult``.
    """
    _check_anti_gaming()

    # ── VLM path (stub) ────────────────────────────────────────────────
    vlm_configured = (
        VALIDATOR_MODEL != "gpt-4o-mini"
        or bool(os.environ.get("VALIDATOR_MODEL"))
    )
    if vlm_configured and VALIDATOR_MODEL != NAVIGATOR_MODEL:
        logger.info(
            "VLM configured: VALIDATOR_MODEL=%s, NAVIGATOR_MODEL=%s. "
            "Stub — falling back to deterministic path.",
            VALIDATOR_MODEL,
            NAVIGATOR_MODEL,
        )
        # TODO: Implement actual VLM call with screenshot image.
        # For now, fall through to deterministic.

    # ── Deterministic fallback: extract page text via CDP ───────────────
    page_text = ""
    if cdp is not None:
        try:
            result = await cdp.send(
                "Runtime.evaluate",
                {
                    "expression": "document.body.innerText",
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                session_id=session_id,
            )
            raw = result.get("result", {}).get("result", {}).get("value", "")
            if isinstance(raw, str):
                page_text = raw
            elif raw is not None:
                page_text = str(raw)
        except Exception as exc:
            logger.warning(
                "Failed to extract page text via CDP: %s", exc
            )

    if not page_text:
        return ValidationResult(
            complete=False,
            confidence=0.0,
            reasoning=(
                "Could not extract page text from the browser. "
                "No CDP client available or page has no text content."
            ),
            assertions_passed=[],
            assertions_failed=list(assertions),
        )

    return validate_from_page_state(page_text, assertions, task_description)


async def validate_procedure_result(
    procedure_result: dict,
    assertions: list[str],
    cdp=None,
    session_id: str | None = None,
) -> ValidationResult:
    """Validate the overall completion of a procedure run.

    Takes the output of ``run_procedure()`` plus verify assertions and
    judges overall completion.

    Strategy:
    1. If the procedure had **failed steps**, return early with zero
       confidence (can't trust any assertions).
    2. If the procedure produced ``variables`` with meaningful text,
       validate against those.
    3. Otherwise, fall back to screenshot-based validation.

    Args:
        procedure_result: The dict returned by ``run_procedure()``.
        assertions: List of assertion strings to validate.
        cdp: CDP client (used for fallback screenshot path).
        session_id: Current CDP session ID.

    Returns:
        A ``ValidationResult``.
    """
    # ── Check for procedure-level failure ───────────────────────────────
    failed_steps = procedure_result.get("failed", [])
    steps_executed = procedure_result.get("steps_executed", 0)
    steps_total = procedure_result.get("steps_total", 0)

    if failed_steps:
        return ValidationResult(
            complete=False,
            confidence=0.0,
            reasoning=(
                f"Procedure had {len(failed_steps)} failed step(s). "
                f"Executed {steps_executed}/{steps_total} steps. "
                "Cannot validate assertions on a failed procedure."
            ),
            assertions_passed=[],
            assertions_failed=list(assertions),
        )

    # ── Try to validate from procedure variables ────────────────────────
    variables = procedure_result.get("variables", {})

    if assertions and variables:
        # Build a synthetic page text from stored variable values
        parts: list[str] = []
        for key, val in variables.items():
            if isinstance(val, str) and len(val) > 10:
                parts.append(val)
        if parts:
            page_text = "\n".join(parts)
            return validate_from_page_state(page_text, assertions, "")

    # ── Fall back to screenshot-based validation ────────────────────────
    return await validate_screenshot(
        "/tmp/browser-shot.png",
        assertions,
        "",
        cdp,
        session_id,
    )
