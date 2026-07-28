"""
Evaluation primitives: evaluate, extract.

Security note
-------------
Unrestricted page JS evaluation is a high-risk capability.  The RPC
``evaluate`` action is gated in ``rpc.py`` by:

* ``BROWSER_ALLOW_EVALUATE=1`` — master switch (default: disabled)
* ``BROWSER_EVALUATE_ALLOWLIST`` — optional comma-separated URL prefixes

Internal callers (page_info, extract, wait_for_*, self_heal, etc.) use this
primitive directly and are *not* subject to those gates.  Do not expose this
function over untrusted IPC without the RPC-level checks.
"""

import json as _json
from typing import Any


async def evaluate(cdp: Any, session_id: str | None, expression: str) -> dict:
    """Evaluate JavaScript *expression* in the page context.

    ``returnByValue`` and ``awaitPromise`` are always ``True``, so the result
    is a deserialized JSON value (or an error object).

    RPC exposure of this primitive is gated by ``BROWSER_ALLOW_EVALUATE``
    (see ``rpc._gate_evaluate``).  Internal daemon helpers may call this
    directly without the gate.
    """
    result = await cdp.send(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        },
        session_id=session_id,
    )
    return result.get("result", {}).get("result", {})


async def extract(
    cdp: Any, session_id: str | None, selector: str, attribute: str | None = None
) -> dict:
    """Extract text content or an attribute value from the first match of *selector*.

    Returns ``{"value": …}`` (the value may be ``None`` if the element is not found).
    """
    if attribute:
        expr = (
            f"document.querySelector({_json.dumps(selector)})"
            f"?.getAttribute({_json.dumps(attribute)}) || null"
        )
    else:
        expr = (
            f"document.querySelector({_json.dumps(selector)})"
            f"?.innerText || null"
        )
    return await evaluate(cdp, session_id, expr)
