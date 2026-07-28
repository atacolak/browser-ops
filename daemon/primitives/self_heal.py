"""
Self-healing helpers — capture failure context so a fixer agent can
diagnose and repair broken procedure steps.

The daemon NEVER calls an LLM directly.  This module only captures:
  - screenshot of the current page
  - page URL
  - visible page text (first 2000 chars)
  - candidate selectors via fuzzy DOM searching
  - a DOM snippet around the target area

The navigator agent consumes this context to reason about fixes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Failure context dataclass ───────────────────────────────────────────────

@dataclass
class FailureContext:
    """Context captured at the point of a step failure."""

    step_index: int
    action: str
    error: str
    screenshot_path: str | None = None       # Screenshot at the failure point
    page_url: str | None = None              # Current URL
    page_text_snippet: str | None = None     # First 2000 chars of visible text
    candidate_selectors: list[dict] = field(default_factory=list)  # Nearby elements
    dom_snippet: str | None = None           # Relevant DOM subtree (if applicable)
    selector_used: str | None = None         # The selector that was attempted
    timestamp: str = ""


# ── Path helpers ────────────────────────────────────────────────────────────

def _failure_screenshot_path() -> str:
    """Return a timestamped screenshot path under /tmp/browser-ops/."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    os.makedirs("/tmp/browser-ops", exist_ok=True)
    return f"/tmp/browser-ops/failure-{ts}.png"


# ── Candidate finder ────────────────────────────────────────────────────────

_CANDIDATE_JS = r"""
(function() {
    const selector = %(selector)s;
    const candidates = [];
    const seen = new Set();

    // Helper: extract key attributes from an element
    function describe(el) {
        const tag = (el.tagName || '').toLowerCase();
        const id = el.id || '';
        const name = el.getAttribute('name') || '';
        const type = el.getAttribute('type') || '';
        const placeholder = el.getAttribute('placeholder') || '';
        const ariaLabel = el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || '';
        const text = (el.innerText || el.textContent || '').trim().substring(0, 100);
        const className = (el.className || '').toString().substring(0, 200);
        const selectorStr = id ? '#' + CSS.escape(id) :
            name ? '[name="' + CSS.escape(name) + '"]' :
            '';
        return {tag, id, name, type, placeholder, ariaLabel, text, className, selector: selectorStr};
    }

    // If no selector, return empty
    if (!selector) return JSON.stringify(candidates);

    // Strategy 1: try the exact selector (it might work even if the step thinks it failed)
    try {
        const exact = document.querySelectorAll(selector);
        for (const el of exact) {
            const d = describe(el);
            const key = d.tag + '|' + d.id + '|' + d.name + '|' + d.text;
            if (!seen.has(key)) { seen.add(key); candidates.push(d); }
        }
    } catch(e) {}

    // Strategy 2: try partial id match (e.g. "input[name=q]" → search by partial name)
    const idMatch = selector.match(/#([\w-]+)/);
    const nameMatch = selector.match(/\[name=["']?([^"'\]]+)["']?\]/);
    const placeholderMatch = selector.match(/\[placeholder=["']?([^"'\]]+)["']?\]/);
    const ariaMatch = selector.match(/\[aria-label=["']?([^"'\]]+)["']?\]/);

    // Partial id search
    if (idMatch) {
        const partial = idMatch[1].replace(/[-_]/g, '').toLowerCase();
        const all = document.querySelectorAll('[id]');
        for (const el of all) {
            const elId = (el.id || '').replace(/[-_]/g, '').toLowerCase();
            if (partial.length >= 3 && elId.includes(partial)) {
                const d = describe(el);
                const key = d.tag + '|' + d.id + '|' + d.name + '|' + d.text;
                if (!seen.has(key)) { seen.add(key); candidates.push(d); }
            }
        }
    }

    // Partial name search
    if (nameMatch) {
        const partial = nameMatch[1].toLowerCase();
        const all = document.querySelectorAll('[name]');
        for (const el of all) {
            const elName = (el.getAttribute('name') || '').toLowerCase();
            if (partial.length >= 2 && elName.includes(partial)) {
                const d = describe(el);
                const key = d.tag + '|' + d.id + '|' + d.name + '|' + d.text;
                if (!seen.has(key)) { seen.add(key); candidates.push(d); }
            }
        }
    }

    // Placeholder text match
    if (placeholderMatch) {
        const partial = placeholderMatch[1].toLowerCase();
        const all = document.querySelectorAll('[placeholder]');
        for (const el of all) {
            const ph = (el.getAttribute('placeholder') || '').toLowerCase();
            if (ph.includes(partial) || partial.includes(ph.substring(0, 10))) {
                const d = describe(el);
                const key = d.tag + '|' + d.id + '|' + d.name + '|' + d.text;
                if (!seen.has(key)) { seen.add(key); candidates.push(d); }
            }
        }
    }

    // Aria-label match
    if (ariaMatch) {
        const partial = ariaMatch[1].toLowerCase();
        const all = document.querySelectorAll('[aria-label], [aria-labelledby]');
        for (const el of all) {
            const al = (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || '').toLowerCase();
            if (al.includes(partial) || partial.includes(al.substring(0, 10))) {
                const d = describe(el);
                const key = d.tag + '|' + d.id + '|' + d.name + '|' + d.text;
                if (!seen.has(key)) { seen.add(key); candidates.push(d); }
            }
        }
    }

    // Strategy 3: tag + text content match — find elements with tag matching
    // the first part of the selector and text containing the action context
    const tagMatch = selector.match(/^(\w+)/);
    if (tagMatch) {
        const tag = tagMatch[1].toLowerCase();
        const all = document.querySelectorAll(tag);
        for (const el of all) {
            const d = describe(el);
            const key = d.tag + '|' + d.id + '|' + d.name + '|' + d.text;
            if (!seen.has(key)) { seen.add(key); candidates.push(d); }
        }
    }

    // Limit to 15 candidates
    return JSON.stringify(candidates.slice(0, 15));
})()
"""


async def find_candidate_elements(
    cdp: Any,
    session_id: str | None,
    failed_selector: str | None,
) -> list[dict[str, str]]:
    """Search the DOM for elements similar to the failed selector.

    Uses JS evaluation to try:
      - exact selector match
      - partial id attribute match
      - partial name attribute match
      - placeholder text match
      - aria-label / aria-labelledby match
      - tag name match

    Returns a list of ``{tag, id, name, type, placeholder, ariaLabel, text,
    className, selector}`` dicts (max 15).
    """
    if not failed_selector:
        return []

    from .eval_mod import evaluate

    js_code = _CANDIDATE_JS % {"selector": json.dumps(failed_selector)}
    result = await evaluate(cdp, session_id, js_code)
    value = result.get("value", "")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


# ── DOM snippet capture ─────────────────────────────────────────────────────

_DOM_SNIPPET_JS = r"""
(function() {
    const selector = %(selector)s;
    if (!selector) return null;
    try {
        const el = document.querySelector(selector);
        if (!el) {
            // Try to find closest parent context
            return null;
        }
        // Return outerHTML of the element and its immediate parent for context
        const parent = el.parentElement;
        return JSON.stringify({
            element: el.outerHTML ? el.outerHTML.substring(0, 2000) : null,
            parent: parent && parent.outerHTML ? parent.outerHTML.substring(0, 2000) : null
        });
    } catch(e) {
        return null;
    }
})()
"""


async def _capture_dom_snippet(
    cdp: Any,
    session_id: str | None,
    selector: str | None,
) -> str | None:
    """Capture a DOM snippet around the target selector."""
    if not selector:
        return None

    from .eval_mod import evaluate

    js_code = _DOM_SNIPPET_JS % {"selector": json.dumps(selector)}
    result = await evaluate(cdp, session_id, js_code)
    return result.get("value") if result else None


# ── Main capture function ───────────────────────────────────────────────────

async def capture_failure_context(
    cdp: Any,
    session_id: str | None,
    step: dict[str, Any],
    error: str,
    step_index: int,
) -> FailureContext:
    """After a step fails, capture everything a fixer needs.

    For selector-based failures (fill, extract, click with selector):
    - Screenshot the current page
    - Extract page URL
    - Extract page text (first 2000 chars)
    - Try to find similar elements on the page (fuzzy selector matching)
    - Capture DOM subtree around the expected target area

    Returns a rich FailureContext for the fixer agent.
    """
    from .capture import capture_screenshot
    from .eval_mod import evaluate

    action = step.get("action", "")
    selector = step.get("selector", "")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # ── Screenshot ────────────────────────────────────────────────────
    screenshot_path = None
    try:
        path = _failure_screenshot_path()
        result = await capture_screenshot(cdp, session_id, path=path)
        screenshot_path = result.get("path")
    except Exception:
        screenshot_path = None

    # ── Page URL ──────────────────────────────────────────────────────
    page_url = None
    try:
        url_result = await evaluate(cdp, session_id, "location.href")
        page_url = url_result.get("value") if url_result else None
    except Exception:
        page_url = None

    # ── Page text snippet ─────────────────────────────────────────────
    page_text_snippet = None
    try:
        text_result = await evaluate(
            cdp, session_id,
            "document.body ? document.body.innerText.substring(0, 2000) : ''",
        )
        page_text_snippet = text_result.get("value") if text_result else None
    except Exception:
        page_text_snippet = None

    # ── Candidate selectors ───────────────────────────────────────────
    candidate_selectors: list[dict] = []
    if selector:
        try:
            candidate_selectors = await find_candidate_elements(cdp, session_id, selector)
        except Exception:
            candidate_selectors = []

    # ── DOM snippet ───────────────────────────────────────────────────
    dom_snippet = None
    if selector:
        try:
            dom_snippet = await _capture_dom_snippet(cdp, session_id, selector)
        except Exception:
            dom_snippet = None

    return FailureContext(
        step_index=step_index,
        action=action,
        error=error,
        screenshot_path=screenshot_path,
        page_url=page_url,
        page_text_snippet=page_text_snippet,
        candidate_selectors=candidate_selectors,
        dom_snippet=dom_snippet,
        selector_used=selector or None,
        timestamp=timestamp,
    )
