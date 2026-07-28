"""
EventBus — durable JSONL spool of primitive executions.

Every primitive execution emits a JSONL line to
``state/<worker>/events/YYYY-MM-DD.jsonl``.  This provides an audit trail
for debugging, replay, and observability.

Screenshot events reference the file path; base64 data is never inlined.
"""

import json
import time
from pathlib import Path


class EventBus:
    """Append-only event spool to a date-partitioned JSONL file.

    Usage::

        bus = EventBus("worker-b", Path("state"))
        await bus.emit("goto_url", {"url": "https://..."}, "ok", 1200)
    """

    def __init__(self, worker_id: str, state_dir: Path):
        self.worker_id = worker_id
        self.state_dir = Path(state_dir)
        self._file: Path | None = None
        self._handle = None

    def _ensure_file(self) -> Path:
        """Return (and lazily create) the JSONL file for today."""
        today = time.strftime("%Y-%m-%d")
        path = self.state_dir / self.worker_id / "events" / f"{today}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def emit(
        self,
        method: str,
        params: dict,
        result_summary: str,
        duration_ms: float,
    ) -> None:
        """Write one event line to the JSONL spool.

        Args:
            method: The primitive method name (e.g. ``"goto_url"``).
            params: The parameters passed to the method.
            result_summary: A short string summarising the result.
                           (e.g. ``"loaded"``, ``"error: NAV_TIMEOUT"``).
            duration_ms: Execution duration in milliseconds.
        """
        event = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "worker": self.worker_id,
            "method": method,
            "params": params,
            "result_summary": result_summary,
            "duration_ms": round(duration_ms, 1),
        }
        path = self._ensure_file()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
