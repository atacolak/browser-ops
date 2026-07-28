"""
Navigation primitives: goto_url, wait_for_load, wait_for_element, page_info.
"""

import asyncio
import json
from typing import Any


async def goto_url(cdp: Any, session_id: str | None, url: str) -> dict:
    """Navigate the current page to *url* via CDP Page.navigate."""
    result = await cdp.send("Page.navigate", {"url": url}, session_id=session_id)
    return result.get("result", {})


async def page_info(cdp: Any, session_id: str | None, dialog: dict | None = None) -> dict:
    """Return current page metadata (url, title, viewport, scroll, content dimensions).

    If *dialog* is truthy, return it immediately instead of evaluating JS
    (CDP blocks evaluation while a dialog is open).
    """
    if dialog:
        return {"dialog": dialog}

    from .eval_mod import evaluate
    expr = (
        "JSON.stringify({"
        "url:location.href,title:document.title,"
        "w:innerWidth,h:innerHeight,"
        "sx:scrollX,sy:scrollY,"
        "pw:document.documentElement.scrollWidth,"
        "ph:document.documentElement.scrollHeight"
        "})"
    )
    result = await evaluate(cdp, session_id, expr)
    return json.loads(result["value"])


async def wait_for_load(cdp: Any, session_id: str | None, timeout: float = 15.0) -> dict:
    """Poll ``document.readyState`` until ``complete`` or *timeout* expires."""
    from .eval_mod import evaluate
    for _ in range(int(timeout / 0.3)):
        result = await evaluate(cdp, session_id, "document.readyState")
        if result.get("value") == "complete":
            return {"ready": True}
        await asyncio.sleep(0.3)
    return {"ready": False, "timeout": True}


async def wait_for_element(cdp: Any, session_id: str | None, selector: str, timeout: float = 10.0) -> dict:
    """Poll for *selector* presence in the DOM up to *timeout* seconds."""
    from .eval_mod import evaluate
    import json as _json
    for _ in range(int(timeout / 0.3)):
        result = await evaluate(
            cdp, session_id,
            f"!!document.querySelector({_json.dumps(selector)})"
        )
        if result.get("value"):
            return {"found": True, "selector": selector}
        await asyncio.sleep(0.3)
    return {"found": False, "timeout": True, "selector": selector}
