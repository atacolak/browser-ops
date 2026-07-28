"""Lifecycle adapters: xai (identity_ops), scratch, vpn."""

from __future__ import annotations

from typing import Any, Protocol

from browserctl.adapters import scratch as scratch_adapter
from browserctl.adapters import vpn as vpn_adapter
from browserctl.adapters import xai as xai_adapter
from browserctl.errors import InvalidRequest


class Adapter(Protocol):
    name: str

    def acquire(self, request: dict[str, Any]) -> dict[str, Any]:
        """Start/bind resources. Returns resources + env (+ identity fields)."""
        ...

    def release(self, lease: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        """Stop resources for lease. Idempotent."""
        ...

    def status(self, lease: dict[str, Any]) -> dict[str, Any]:
        """Best-effort live status for resources."""
        ...


ADAPTERS: dict[str, Any] = {
    "xai": xai_adapter,
    "scratch": scratch_adapter,
    "adhoc": scratch_adapter,  # alias
    "vpn": vpn_adapter,
}


def get_adapter(name: str) -> Any:
    key = (name or "").strip().lower()
    if key not in ADAPTERS:
        raise InvalidRequest(
            f"unknown adapter/kind {name!r}",
            known=sorted(set(ADAPTERS)),
        )
    return ADAPTERS[key]
