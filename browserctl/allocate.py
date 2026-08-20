"""Allocate or claim a page target on an existing browser CDP endpoint."""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen


def new_target_id() -> str:
    return f"tgt-{uuid.uuid4().hex[:12]}"


def create_page_target(cdp_url: str | None, *, url: str = "about:blank") -> str:
    """Create a page via Chrome's HTTP ``/json/new`` when CDP is live.

    Falls back to a synthetic id when the endpoint is unavailable (no_start /
    unit tests). Callers must still record exclusive ownership in the registry.
    """
    if cdp_url:
        endpoint = cdp_url.rstrip("/") + "/json/new?" + quote(url, safe=":/")
        try:
            with urlopen(endpoint, timeout=5) as resp:  # noqa: S310 — localhost CDP
                data = json.loads(resp.read().decode("utf-8") or "{}")
            if isinstance(data, dict):
                tid = data.get("id")
                if tid:
                    return str(tid)
        except Exception:
            pass
    return new_target_id()


def target_from_request_or_create(req: dict[str, Any], *, cdp_url: str | None) -> str:
    requested = str(req.get("target_id") or "").strip()
    if requested:
        return requested
    return create_page_target(cdp_url)
