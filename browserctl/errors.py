"""Structured errors for browserctl."""

from __future__ import annotations

from typing import Any


class BrowserctlError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int = 1,
    ):
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.details:
            out["error"]["details"] = self.details
        return out


class LeaseConflict(BrowserctlError):
    def __init__(self, worker_id: str, holder: dict[str, Any] | None = None):
        super().__init__(
            "LEASE_CONFLICT",
            f"worker {worker_id!r} already has an active mutation lease",
            details={"worker_id": worker_id, "holder": holder or {}},
            exit_code=3,
        )


class LeaseNotFound(BrowserctlError):
    def __init__(self, lease_id: str | None = None, worker_id: str | None = None):
        who = lease_id or worker_id or "?"
        super().__init__(
            "LEASE_NOT_FOUND",
            f"no lease found for {who}",
            details={"lease_id": lease_id, "worker_id": worker_id},
            exit_code=2,
        )


class InvalidRequest(BrowserctlError):
    def __init__(self, message: str, **details: Any):
        super().__init__("INVALID_REQUEST", message, details=details, exit_code=2)


class AdapterError(BrowserctlError):
    def __init__(self, message: str, **details: Any):
        super().__init__("ADAPTER_ERROR", message, details=details, exit_code=1)
