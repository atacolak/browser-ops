"""
Capture primitives: capture_screenshot, http_get.
"""

import base64 as _b64
from pathlib import Path
from typing import Any


async def capture_screenshot(
    cdp: Any,
    session_id: str | None,
    path: str | None = None,
    base64: bool = False,
) -> dict:
    """Capture a page screenshot via CDP.

    * If *path* is provided → save PNG to disk, return ``{"path": …}``.
    * If *path* is ``None`` and *base64* is ``True`` → return ``{"base64": …}``.
    * Otherwise → return ``{"path": None, "size": …}``.
    """
    result = await cdp.send(
        "Page.captureScreenshot", {"format": "png"}, session_id=session_id
    )
    data = result["result"]["data"]

    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(_b64.b64decode(data))
        return {"path": path}

    if base64:
        return {"base64": data}

    return {"path": None, "size": len(data)}


async def http_get(url: str, headers: dict | None = None) -> dict:
    """Perform an HTTP GET **outside** the browser (via urllib).

    This is useful for fetching raw data without navigating the browser page.
    """
    import urllib.request as req

    h = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    if headers:
        h.update(headers)

    try:
        with req.urlopen(req.Request(url, headers=h), timeout=20) as r:
            body = r.read().decode("utf-8", errors="replace")
        return {"body": body, "status": r.status}
    except Exception as e:
        return {"error": str(e)}
