"""
Input primitives: click_at_xy, type_text, fill_input, press_key, scroll.
"""

import json as _json
from typing import Any


async def click_at_xy(
    cdp: Any,
    session_id: str | None,
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
) -> dict:
    """Dispatch a mouse-press + mouse-release at (x, y) via CDP."""
    await cdp.send(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": button, "clickCount": clicks},
        session_id=session_id,
    )
    await cdp.send(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x, "y": y, "button": button, "clickCount": clicks},
        session_id=session_id,
    )
    return {"clicked": True, "x": x, "y": y}


async def type_text(cdp: Any, session_id: str | None, text: str) -> dict:
    """Insert *text* at the focused element via CDP Input.insertText."""
    await cdp.send("Input.insertText", {"text": text}, session_id=session_id)
    return {"typed": True, "length": len(text)}


async def press_key(cdp: Any, session_id: str | None, key: str) -> dict:
    """Dispatch keyDown (+ char if printable) + keyUp for *key*."""
    vk_map = {
        "Enter": 13, "Tab": 9, "Backspace": 8, "Escape": 27,
        "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
        "Delete": 46, "Home": 36, "End": 35, " ": 32,
    }
    vk = vk_map.get(key, 0)
    base: dict = {
        "key": key,
        "code": key,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
    }
    await cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", **base}, session_id=session_id)
    if len(key) == 1:
        await cdp.send(
            "Input.dispatchKeyEvent", {"type": "char", "text": key, **base}, session_id=session_id
        )
    await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **base}, session_id=session_id)
    return {"pressed": True}


async def scroll(cdp: Any, session_id: str | None, x: int = 500, y: int = 500, dy: int = -300) -> dict:
    """Dispatch a mouse-wheel event at (x, y) with vertical delta *dy*."""
    await cdp.send(
        "Input.dispatchMouseEvent",
        {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": dy},
        session_id=session_id,
    )
    return {"scrolled": True}


async def fill_input(cdp: Any, session_id: str | None, selector: str, text: str) -> dict:
    """Fill an input field identified by *selector*.

    Focus → select all → clear → type character-by-character → dispatch
    ``input`` and ``change`` events so frameworks (React, Vue, …) react.
    """
    from .eval_mod import evaluate

    # Focus and select existing content
    await evaluate(
        cdp, session_id,
        f"(()=>{{const e=document.querySelector({_json.dumps(selector)}); if(e){{e.focus();e.select();}}}})()",
    )

    # Clear field
    await press_key(cdp, session_id, "Backspace")

    # Type each character
    for ch in text:
        await press_key(cdp, session_id, ch)

    # Synthesize framework events
    await evaluate(
        cdp, session_id,
        f"(()=>{{const e=document.querySelector({_json.dumps(selector)});"
        f"if(e){{e.dispatchEvent(new Event('input',{{bubbles:true}}));"
        f"e.dispatchEvent(new Event('change',{{bubbles:true}}));}}}})()",
    )
    return {"filled": True, "selector": selector}
