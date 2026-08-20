"""Allocate or claim a page target on an existing browser CDP endpoint."""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_CDP_TIMEOUT = 2.0


def new_target_id() -> str:
    return f"tgt-{uuid.uuid4().hex[:12]}"


def _cdp_json(cdp_url: str, path: str, *, method: str = "GET") -> Any:
    endpoint = cdp_url.rstrip("/") + path
    req = Request(endpoint, method=method)
    with urlopen(req, timeout=_CDP_TIMEOUT) as resp:  # noqa: S310 — localhost CDP
        raw = resp.read().decode("utf-8") or ""
    if not raw:
        return None
    return json.loads(raw)


def list_page_targets(cdp_url: str | None) -> list[dict[str, Any]]:
    """Live ``type=page`` targets from ``GET /json/list``. Empty when CDP is down."""
    if not cdp_url:
        return []
    try:
        data = _cdp_json(cdp_url, "/json/list")
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        if row.get("type") != "page":
            continue
        tid = row.get("id")
        if tid:
            out.append(row)
    return out


def close_page_target(cdp_url: str | None, target_id: str) -> bool:
    """Best-effort ``GET /json/close/<id>``. False when CDP is down."""
    if not cdp_url or not target_id:
        return False
    try:
        _cdp_json(cdp_url, "/json/close/" + quote(target_id, safe=""))
        return True
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False


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
            with urlopen(req, timeout=_CDP_TIMEOUT) as resp:  # noqa: S310 — localhost CDP
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


def claim_unowned_or_create(
    cdp_url: str | None,
    *,
    owned_ids: set[str] | frozenset[str] | None = None,
    url: str = "about:blank",
) -> str:
    """Reuse an unowned live page, else ``PUT /json/new``.

    Crash-restore and Cloak's first-page attach leave leftover ``about:blank``
    tabs. Minting a new target on every join piles them up. Prefer a live page
    that no target lease currently owns (blank first, then any unowned page).
    """
    owned = owned_ids or set()
    pages = list_page_targets(cdp_url)
    unowned = [p for p in pages if str(p.get("id") or "") not in owned]

    def _is_blank(row: dict[str, Any]) -> bool:
        u = str(row.get("url") or "")
        return u in ("", "about:blank")

    pick = next((p for p in unowned if _is_blank(p)), None) or (unowned[0] if unowned else None)
    if pick and pick.get("id"):
        return str(pick["id"])
    return create_page_target(cdp_url, url=url)


def target_from_request_or_create(req: dict[str, Any], *, cdp_url: str | None) -> str:
    requested = str(req.get("target_id") or "").strip()
    if requested:
        return requested
    return create_page_target(cdp_url)
