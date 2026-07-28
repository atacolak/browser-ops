"""
Async job queue — fire-and-forget procedure execution with handle-based polling.

Jobs are in-memory only.  Daemon restart clears the queue.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class AsyncJob:
    """Represents a single async procedure execution."""

    handle_id: str
    status: str  # "running" | "done" | "failed"
    created_at: str  # ISO timestamp
    finished_at: str | None = None
    steps_total: int = 0
    steps_executed: int = 0
    result: dict | None = None  # Final procedure result
    error: str | None = None


# ── In-memory store ────────────────────────────────────────────────────────

_jobs: dict[str, AsyncJob] = {}
_lock = asyncio.Lock()


def _now_iso() -> str:
    """Return current UTC timestamp as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ── Public API ─────────────────────────────────────────────────────────────


async def start_async(
    steps: list[dict[str, Any]],
    params: dict[str, Any] | None,
    cdp: Any,
    session_id: str | None,
) -> str:
    """Begin executing a procedure in the background.

    Spawns an ``asyncio.create_task`` that runs ``run_procedure`` and
    captures the result or error on the job record.

    Returns ``handle_id`` immediately.  The caller polls with ``poll_async()``.
    """
    from .primitives.replay import run_procedure

    handle_id = "job_" + uuid.uuid4().hex[:12]

    job = AsyncJob(
        handle_id=handle_id,
        status="running",
        created_at=_now_iso(),
        steps_total=len(steps) if isinstance(steps, list) else 0,
    )

    async with _lock:
        _jobs[handle_id] = job

    async def _run():
        try:
            result = await run_procedure(cdp, session_id, steps, params)
            steps_exec = result.get("steps_executed", 0)
            failed = result.get("failed", [])
            async with _lock:
                job.status = "done"
                job.finished_at = _now_iso()
                job.steps_executed = steps_exec
                job.result = result
                if failed:
                    # Attach a summary error if there were failures
                    job.error = (
                        f"{len(failed)} step(s) failed: "
                        + "; ".join(
                            f.get("error", "unknown")[:200] for f in failed[:5]
                        )
                    )
        except Exception as exc:
            async with _lock:
                job.status = "failed"
                job.finished_at = _now_iso()
                job.error = str(exc)

    asyncio.create_task(_run())

    return handle_id


def poll_async(handle_id: str) -> AsyncJob | None:
    """Check the status of an async job.

    Returns ``None`` if the handle is unknown.
    """
    return _jobs.get(handle_id)


def list_asyncs() -> list[AsyncJob]:
    """Return all jobs (active + completed)."""
    return list(_jobs.values())


def cleanup_old_jobs(max_age_minutes: int = 60) -> int:
    """Remove completed jobs older than *max_age_minutes*.

    Returns the number of jobs removed.
    """
    import time

    now = time.time()
    cutoff = now - max_age_minutes * 60
    to_remove: list[str] = []

    for handle_id, job in _jobs.items():
        if job.status in ("done", "failed") and job.finished_at:
            # Parse ISO timestamp into epoch seconds
            try:
                finished = datetime.fromisoformat(job.finished_at).timestamp()
                if finished < cutoff:
                    to_remove.append(handle_id)
            except (ValueError, TypeError):
                continue

    for handle_id in to_remove:
        del _jobs[handle_id]

    return len(to_remove)
