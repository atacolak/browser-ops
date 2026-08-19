"""Lifecycle adapters: scratch and vpn."""

from __future__ import annotations

from typing import Any, Protocol

from browserctl.adapters import scratch as scratch_adapter
from browserctl.adapters import vpn as vpn_adapter
from browserctl.errors import InvalidRequest


class Adapter(Protocol):
    name: str

    def acquire(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def release(self, lease: dict[str, Any], *, force: bool = False) -> dict[str, Any]: ...

    def status(self, lease: dict[str, Any]) -> dict[str, Any]: ...


ADAPTERS: dict[str, Any] = {
    "scratch": scratch_adapter,
    "adhoc": scratch_adapter,
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
