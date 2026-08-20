"""Allocate or claim a page target on an existing browser CDP endpoint."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def new_target_id() -> str:
    return f"tgt-{uuid.uuid4().hex[:12]}"


def create_page_target(cdp_url: str | None, *, url: str = "about:blank") -> str:
    """Create a page via Chrome HTTP ``PUT /json/new`` when CDP is live.

    Falls back to a synthetic id when the endpoint is unreachable
    (``no_start`` / unit tests). Live Chrome rejects GET with 405; PUT first.
    """
    if not cdp_url:
        return new_target_id()
    endpoint = cdp_url.rstrip("/") + "/json/new?" + quote(url, safe=":/")
    for method in ("PUT", "GET"):
        try:
            req = Request(endpoint, method=method)
            with urlopen(req, timeout=2) as resp:  # noqa: S310 — localhost CDP
                data = json.loads(resp.read().decode("utf-8") or "{}")
            if isinstance(data, dict):
                tid = data.get("id")
                if tid:
                    return str(tid)
        except HTTPError as e:
            if e.code == 405:
                continue
            return new_target_id()
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            return new_target_id()
    return new_target_id()


def target_from_request_or_create(req: dict[str, Any], *, cdp_url: str | None) -> str:
    requested = str(req.get("target_id") or "").strip()
    if requested:
        return requested
    return create_page_target(cdp_url)
