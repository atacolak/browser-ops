"""Replay primitives: deterministic step-list procedure runner.

Executes a sequence of browser actions (navigate, click, type, extract,
verify, ...) with:

* ``{{variable}}`` interpolation from a ``params`` dict
* Result capture via ``store_as`` into a local variable namespace
* ``verify`` assertion evaluation using simple string-based checks (no ``eval()``)
* Configurable failure policy (stop / continue)
"""

from __future__ import annotations

import re as _re
from typing import Any

from . import capture as _capture
from . import eval_mod as _eval
from . import input as _input
from . import nav as _nav
from . import self_heal as _self_heal

# ── Template interpolation ──────────────────────────────────────────────────

_VAR_RE = _re.compile(r"\{\{(\w+)\}\}")


def _interpolate(value: str, params: dict[str, Any] | None) -> str:
    """Replace ``{{variable}}`` placeholders with values from *params*.

    Unknown placeholders are left unchanged so callers can see the gap.
    """
    if not params:
        return value

    def _replace(m: _re.Match) -> str:
        var_name = m.group(1)
        return str(params.get(var_name, m.group(0)))

    return _VAR_RE.sub(_replace, value)


def _interpolate_dict(
    data: dict[str, Any], params: dict[str, Any] | None
) -> dict[str, Any]:
    """Return a shallow copy of *data* with all string values interpolated."""
    if not params:
        return dict(data)
    out = {}
    for k, v in data.items():
        if isinstance(v, str):
            out[k] = _interpolate(v, params)
        else:
            out[k] = v
    return out


# ── Simple assertion parser (no eval) ───────────────────────────────────────


def _check_assertion(expr: str, variables: dict[str, Any]) -> bool:
    """Evaluate a simple assertion expression against *variables*.

    Supported patterns (in order of matching):

    * ``len(varname) > N``       — sequence/dict length exceeds threshold
    * ``varname != None``        — not-None check
    * ``varname == 'value'``     — equality check (single or double quotes)
    * ``varname`` (bare word)    — truthy check

    Everything else returns ``False``.
    """
    expr = expr.strip()

    # len(varname) > N
    m = _re.match(r"^len\((\w+)\)\s*>\s*(\d+)$", expr)
    if m:
        var_name = m.group(1)
        threshold = int(m.group(2))
        val = variables.get(var_name)
        if isinstance(val, (list, dict, str, tuple, set, frozenset)):
            return len(val) > threshold
        return False

    # varname != None
    m = _re.match(r"^(\w+)\s*!=\s*None$", expr)
    if m:
        var_name = m.group(1)
        return variables.get(var_name) is not None

    # varname == 'value' (single quotes)
    m = _re.match(r"^(\w+)\s*==\s*'([^']*)'$", expr)
    if m:
        var_name = m.group(1)
        expected = m.group(2)
        return str(variables.get(var_name)) == expected

    # varname == "value" (double quotes)
    m = _re.match(r'^(\w+)\s*==\s*"([^"]*)"$', expr)
    if m:
        var_name = m.group(1)
        expected = m.group(2)
        return str(variables.get(var_name)) == expected

    # varname != 'value' (single quotes)
    m = _re.match(r"^(\w+)\s*!=\s*'([^']*)'$", expr)
    if m:
        var_name = m.group(1)
        expected = m.group(2)
        return str(variables.get(var_name)) != expected

    # varname != "value" (double quotes)
    m = _re.match(r'^(\w+)\s*!=\s*"([^"]*)"$', expr)
    if m:
        var_name = m.group(1)
        expected = m.group(2)
        return str(variables.get(var_name)) != expected

    # Bare variable name — truthy check
    m = _re.match(r"^(\w+)$", expr)
    if m:
        var_name = m.group(1)
        return bool(variables.get(var_name))

    # Unsupported assertion
    return False


# ── Action result value extraction for store_as ─────────────────────────────


def _store_value(result: dict[str, Any], action: str) -> Any:
    """Extract the meaningful value from a step result for ``store_as``."""
    if action in ("extract", "evaluate"):
        return result.get("value")
    if action == "screenshot":
        return result.get("path")
    return result


# ── Action dispatcher ───────────────────────────────────────────────────────


async def _execute_step(
    cdp: Any,
    session_id: str | None,
    step: dict[str, Any],
    variables: dict[str, Any],  # noqa: ARG001 (unused, available for future use)
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Execute a single action step and return its result dict.

    *cdp* is the CDP client.
    *session_id* is the current CDP session ID.
    *step* is the raw step dict (already interpolated for string fields).
    *variables* is the mutable variable namespace (unused by dispatcher, kept
    for API consistency).
    *params* is the original params dict for ``{{variable}}`` interpolation.

    Returns a dict with at least a boolean ``"step"`` key indicating success.
    """
    action = step.get("action", "")
    result: dict[str, Any] = {"step": True}

    if action == "navigate":
        url = step["url"]
        nav_result = await _nav.goto_url(cdp, session_id, url)
        result.update(nav_result)

    elif action in ("wait_for_load",):
        timeout = step.get("timeout", 15.0)
        nav_result = await _nav.wait_for_load(cdp, session_id, timeout)
        result.update(nav_result)

    elif action == "screenshot":
        path = step.get("path")
        cap_result = await _capture.capture_screenshot(cdp, session_id, path=path)
        result.update(cap_result)

    elif action == "click":
        x = int(step["x"])
        y = int(step["y"])
        button = step.get("button", "left")
        clicks = int(step.get("clicks", 1))
        click_result = await _input.click_at_xy(cdp, session_id, x, y, button, clicks)
        result.update(click_result)

    elif action in ("type", "type_text"):
        text = step["text"]
        type_result = await _input.type_text(cdp, session_id, text)
        result.update(type_result)

    elif action in ("press", "press_key"):
        key = step["key"]
        press_result = await _input.press_key(cdp, session_id, key)
        result.update(press_result)

    elif action in ("fill", "fill_input"):
        selector = step["selector"]
        text = step["text"]
        fill_result = await _input.fill_input(cdp, session_id, selector, text)
        result.update(fill_result)

    elif action == "scroll":
        x = int(step.get("x", 500))
        y = int(step.get("y", 500))
        dy = int(step.get("dy", -300))
        scroll_result = await _input.scroll(cdp, session_id, x, y, dy)
        result.update(scroll_result)

    elif action == "extract":
        selector = step["selector"]
        attribute = step.get("attribute")
        ext_result = await _eval.extract(cdp, session_id, selector, attribute)
        result.update(ext_result)

    elif action == "evaluate":
        expression = step["expression"]
        eval_result = await _eval.evaluate(cdp, session_id, expression)
        result.update(eval_result)

    else:
        result = {"step": False, "error": f"Unknown action: {action}"}

    return result


# ── Public runner ───────────────────────────────────────────────────────────


async def run_procedure(
    cdp: Any,
    session_id: str | None,
    steps: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a sequence of browser action steps.

    Args:
        cdp: CDP client instance.
        session_id: Current CDP session ID (may be ``None`` for tab-less ops).
        steps: List of step dicts, each with at minimum ``action`` and
            action-specific fields.
        params: Optional dict of ``{{variable}}`` values for interpolation.

    Returns:
        A result dict with the following keys::

            {
                "procedure": True,
                "steps_executed": 5,
                "steps_total": 6,
                "passed": [{"step": 0, "action": "navigate"}, ...],
                "failed": [{"step": 3, "action": "verify",
                            "assert": "len(repos) > 0",
                            "message": "..."}, ...],
                "variables": {"repos": ["/org/repo1", ...]},
                "on_failure": "stop",
                "stopped_early": False,
            }
    """
    if not isinstance(steps, list):
        return {
            "procedure": True,
            "steps_executed": 0,
            "steps_total": 0,
            "passed": [],
            "failed": [],
            "variables": {},
            "error": "steps must be a list",
        }

    # Determine failure policy from the first step that has one, or default
    on_failure: str = "stop"
    for s in steps:
        if "on_failure" in s:
            val = s["on_failure"]
            if val in ("stop", "continue"):
                on_failure = val
            break

    variables: dict[str, Any] = {}
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    stopped_early = False
    steps_executed = 0

    # Filter out non-executable entries (no action key)
    executable = [s for s in steps if "action" in s]
    steps_total = len(steps)

    # Fail closed: never report success for a procedure with zero executable steps.
    # Empty / mis-parsed frontmatter previously produced passed=[], failed=[],
    # steps_executed=0 which callers treated as success.
    if not executable:
        return {
            "procedure": True,
            "steps_executed": 0,
            "steps_total": 0,
            "passed": [],
            "failed": [{
                "step": 0,
                "action": "none",
                "error": (
                    "No executable steps found in procedure. "
                    "Check that procedure steps have 'action' keys and the "
                    "frontmatter parser handles nested YAML."
                ),
            }],
            "stopped_early": True,
        }

    for idx, raw_step in enumerate(executable):
        if stopped_early:
            continue

        # Interpolate string fields in the step dict
        step = _interpolate_dict(raw_step, params)
        action = step.get("action", "")

        # ── Verify (special-cased — no CDP call) ──────────────────────
        if action == "verify":
            expr = step.get("assert", "")
            message = step.get("message", "")

            if not expr:
                failed.append({
                    "step": idx,
                    "action": "verify",
                    "assert": expr,
                    "message": message,
                    "error": "No assert expression provided",
                })
                steps_executed += 1
                if on_failure == "stop":
                    stopped_early = True
                continue

            ok = _check_assertion(expr, variables)

            if ok:
                passed.append({
                    "step": idx,
                    "action": "verify",
                    "assert": expr,
                    "message": message,
                })
            else:
                failed.append({
                    "step": idx,
                    "action": "verify",
                    "assert": expr,
                    "message": message,
                    "error": f"Assertion failed: {expr}",
                })
                if on_failure == "stop":
                    stopped_early = True

            steps_executed += 1
            continue

        # ── Regular action step ────────────────────────────────────────
        store_as = step.get("store_as")

        try:
            result = await _execute_step(cdp, session_id, step, variables, params)
        except Exception as e:
            # Capture failure context for self-healing
            try:
                from dataclasses import asdict
                ctx = await _self_heal.capture_failure_context(
                    cdp, session_id, step, str(e), idx
                )
                failure_entry = {
                    "step": idx,
                    "action": action,
                    "error": str(e),
                    "store_as": store_as,
                    "failure_context": asdict(ctx),
                }
            except Exception:
                failure_entry = {
                    "step": idx,
                    "action": action,
                    "error": str(e),
                    "store_as": store_as,
                }
            failed.append(failure_entry)
            steps_executed += 1
            if on_failure == "stop":
                stopped_early = True
            continue

        if result.get("step") is False:
            # Capture failure context for self-healing on step-level failures
            try:
                from dataclasses import asdict
                ctx = await _self_heal.capture_failure_context(
                    cdp, session_id, step,
                    result.get("error", "Unknown error"), idx
                )
                failure_entry = {
                    "step": idx,
                    "action": action,
                    "error": result.get("error", "Unknown error"),
                    "store_as": store_as,
                    "failure_context": asdict(ctx),
                }
            except Exception:
                failure_entry = {
                    "step": idx,
                    "action": action,
                    "error": result.get("error", "Unknown error"),
                    "store_as": store_as,
                }
            failed.append(failure_entry)
            steps_executed += 1
            if on_failure == "stop":
                stopped_early = True
            continue

        # Capture result via store_as
        if store_as:
            variables[store_as] = _store_value(result, action)

        passed.append({
            "step": idx,
            "action": action,
            "store_as": store_as,
        })
        steps_executed += 1

    return {
        "procedure": True,
        "steps_executed": steps_executed,
        "steps_total": steps_total,
        "passed": passed,
        "failed": failed,
        "variables": variables,
        "on_failure": on_failure,
        "stopped_early": stopped_early,
    }
