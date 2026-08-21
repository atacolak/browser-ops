"""
Tab management primitives: list_tabs, new_tab, switch_tab, close_tab, handle_dialog.
"""

from typing import Any


async def list_tabs(cdp: Any, session_id: str | None = None) -> dict:
    """List all open page targets via CDP ``Target.getTargets``."""
    result = await cdp.send("Target.getTargets")
    targets = [
        t for t in result["result"]["targetInfos"] if t.get("type") == "page"
    ]
    return {"tabs": targets}


async def attach_session(
    cdp: Any,
    target_id: str,
    *,
    activate: bool = False,
) -> dict:
    """Attach a CDP session to *target_id*.

    ``activate=True`` brings the tab to the front (mutating drive).
    Peek attaches without ``Target.activateTarget``.
    """
    if activate:
        await cdp.send("Target.activateTarget", {"targetId": target_id})
    attach = await cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
    new_session_id = attach["result"]["sessionId"]
    return {"target_id": target_id, "session_id": new_session_id}


async def new_tab(cdp: Any, session_id: str | None, url: str = "about:blank") -> dict:
    """Create a new tab, attach to it, and optionally navigate."""
    tid = (await cdp.send("Target.createTarget", {"url": "about:blank"}))["result"]["targetId"]
    attached = await attach_session(cdp, tid, activate=True)
    new_session_id = attached["session_id"]
    if url and url != "about:blank":
        from .nav import goto_url
        await goto_url(cdp, new_session_id, url)
    return {"target_id": tid, "session_id": new_session_id}


async def switch_tab(cdp: Any, session_id: str | None, target_id: str) -> dict:
    """Activate an existing tab and attach a new session to it."""
    return await attach_session(cdp, target_id, activate=True)


async def close_tab(cdp: Any, session_id: str | None, target_id: str) -> dict:
    """Close the tab identified by *target_id*."""
    await cdp.send("Target.closeTarget", {"targetId": target_id})
    return {"closed": target_id}


async def handle_dialog(cdp: Any, session_id: str | None, accept: bool = True, prompt_text: str = "") -> dict:
    """Accept or dismiss a JavaScript dialog (alert/confirm/prompt)."""
    params: dict = {"accept": accept}
    if prompt_text:
        params["promptText"] = prompt_text
    await cdp.send("Page.handleJavaScriptDialog", params, session_id=session_id)
    return {"dismissed": True}
