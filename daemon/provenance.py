"""
Provenance recorder — structured audit trail of browser operations.

Extends the existing EventBus with semantic records that capture:
- What skill ran, on what domain, with what result
- What variables were extracted from the page
- What verify assertions passed/failed
- Entity/triple extraction for knowledge graph mapping

Records go to ``state/<worker>/provenance/YYYY-MM-DD.jsonl``.

Usage::

    from provenance import record_procedure_run, query_provenance

    await record_procedure_run(
        worker_id="worker-b",
        skill_id="search-repos",
        domain="github.com",
        url="https://github.com/search?q=rust",
        procedure_result={"procedure": True, "steps_executed": 5, ...},
        duration_ms=1234.5,
    )

    records = query_provenance("worker-b", skill_id="search-repos", limit=10)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class ProvenanceRecord:
    """A single semantic provenance event."""

    ts: str  # ISO timestamp
    worker: str  # Worker ID
    event_type: str  # "procedure_run" | "validation" | "extraction" | "navigation"
    skill_id: str | None  # Skill identifier if applicable
    domain: str | None  # Domain (e.g. "online-fines-vic-gov-au")
    url: str | None  # Page URL at time of event
    success: bool  # Did the operation succeed?
    summary: str  # Human-readable one-liner
    variables: dict | None  # Extracted variables (fine_stage, repos, etc.)
    entities: list[dict] | None  # Extracted entities: [{type, name, value}]
    error: str | None  # Error message if failed
    duration_ms: float  # How long it took


# ── Helpers ──────────────────────────────────────────────────────────────────


def _state_dir() -> str:
    """Resolve the state directory from env or default."""
    return os.environ.get(
        "BROWSER_OPS_STATE",
        os.path.join(os.path.dirname(__file__), "..", "state"),
    )


def _ensure_dir(path: str) -> None:
    """Ensure the parent directory of a file path exists."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _provenance_path(worker_id: str, day: str | None = None) -> str:
    """Return the path to the provenance JSONL file for a worker and day.

    If *day* is omitted, today's date is used.
    """
    if day is None:
        day = time.strftime("%Y-%m-%d")
    return os.path.join(_state_dir(), worker_id, "provenance", f"{day}.jsonl")


def _extract_entities(variables: dict[str, Any] | None) -> list[dict]:
    """Extract a flat list of entity dicts from a variables namespace.

    Uses simple structural heuristics — no NLP, no ML.

    Heuristics:
    - String values → entity with type guessed from variable name
      (e.g. ``fine_stage`` → type="status", ``error`` → type="error",
      anything else → type="value")
    - List values → entity with type="list" and a ``count`` field
    - Nested dicts → one entity per key, key name as type
    - ``None`` / numbers → entity with type="value"
    """
    if not variables:
        return []

    entities: list[dict] = []

    for key, val in variables.items():
        # Skip internal / metadata keys
        if key.startswith("_") or key in ("page_text",):
            continue

        if isinstance(val, str):
            # Guess type from variable name patterns
            guessed_type = "value"
            lower_key = key.lower()

            if any(pattern in lower_key for pattern in ("status", "stage", "state")):
                guessed_type = "status"
            elif any(pattern in lower_key for pattern in ("error", "failure")):
                guessed_type = "error"
            elif any(pattern in lower_key for pattern in ("url", "link", "href")):
                guessed_type = "url"
            elif any(pattern in lower_key for pattern in ("name", "title", "label")):
                guessed_type = "label"

            entities.append({
                "type": guessed_type,
                "name": key,
                "value": val,
            })

        elif isinstance(val, list):
            entities.append({
                "type": "list",
                "name": key,
                "count": len(val),
                "value": val if len(val) <= 5 else f"[{len(val)} items]",
            })

        elif isinstance(val, dict):
            for sub_key, sub_val in val.items():
                entities.append({
                    "type": sub_key,
                    "name": f"{key}.{sub_key}",
                    "value": str(sub_val) if not isinstance(sub_val, (list, dict))
                             else (sub_val if isinstance(sub_val, list) else f"<nested {type(sub_val).__name__}>"),
                })

        elif val is not None:
            # Numbers, booleans, etc.
            entities.append({
                "type": "value",
                "name": key,
                "value": str(val),
            })

    return entities


def _procedure_outcome_summary(procedure_result: dict) -> str:
    """Build a short human-readable summary of a procedure run."""
    executed = procedure_result.get("steps_executed", 0)
    total = procedure_result.get("steps_total", 0)
    passed = len(procedure_result.get("passed", []))
    failed = len(procedure_result.get("failed", []))

    if failed:
        errors = []
        for f in procedure_result.get("failed", [])[:3]:
            errors.append(f.get("action", "?") + ":" + (f.get("error", "?")[:50]))
        return f"{executed}/{total} steps ({passed} passed, {failed} failed): {', '.join(errors)}"
    return f"{executed}/{total} steps ({passed} passed)"


# ── Public API ──────────────────────────────────────────────────────────────


async def record_procedure_run(
    worker_id: str,
    skill_id: str,
    domain: str,
    url: str,
    procedure_result: dict,
    duration_ms: float,
) -> ProvenanceRecord:
    """Record a full procedure run with its outcome.

    Extracts entities from the procedure's stored variables:
    - ``fine_stage="Infringement Notice"`` → entity with type="status",
      name="Fine Stage", value="Infringement Notice"
    - ``repos=["/org/a", "/org/b"]`` → entity with type="list",
      name="Repositories", count=2

    Writes to ``state/<worker>/provenance/YYYY-MM-DD.jsonl``.
    """
    success = len(procedure_result.get("failed", [])) == 0
    error = None
    if not success:
        failed = procedure_result.get("failed", [])
        if failed:
            error = failed[0].get("error", "Unknown failure")

    variables = procedure_result.get("variables", {})
    summary = _procedure_outcome_summary(procedure_result)

    # Event type is "procedure_run" always for this function, but if there
    # was a specific extraction step we note it.
    event_type = "procedure_run"
    if variables:
        event_type = "extraction" if any(
            step.get("action") in ("extract", "evaluate")
            for step in procedure_result.get("passed", [])
        ) else "procedure_run"

    record = ProvenanceRecord(
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        worker=worker_id,
        event_type=event_type,
        skill_id=skill_id,
        domain=domain,
        url=url,
        success=success,
        summary=summary,
        variables=variables if variables else None,
        entities=_extract_entities(variables) if variables else None,
        error=error,
        duration_ms=round(duration_ms, 1),
    )

    path = _provenance_path(worker_id)
    _ensure_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    return record


async def record_navigation(
    worker_id: str,
    url: str,
    domain: str,
    duration_ms: float,
) -> ProvenanceRecord:
    """Record a page navigation event."""
    record = ProvenanceRecord(
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        worker=worker_id,
        event_type="navigation",
        skill_id=None,
        domain=domain,
        url=url,
        success=True,
        summary=f"Navigated to {url[:80]}" if len(url) > 80 else f"Navigated to {url}",
        variables=None,
        entities=None,
        error=None,
        duration_ms=round(duration_ms, 1),
    )

    path = _provenance_path(worker_id)
    _ensure_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    return record


async def record_validation(
    worker_id: str,
    skill_id: str,
    domain: str,
    validation_result,
    duration_ms: float,
) -> ProvenanceRecord:
    """Record a validator run with its pass/fail verdict.

    *validation_result* can be a ``ValidationResult`` dataclass or a dict
    with keys ``complete``, ``confidence``, ``assertions_passed``,
    ``assertions_failed``, ``reasoning``.
    """
    if hasattr(validation_result, "complete"):
        complete = validation_result.complete
        confidence = getattr(validation_result, "confidence", 0.0)
        passed = getattr(validation_result, "assertions_passed", [])
        failed = getattr(validation_result, "assertions_failed", [])
        reasoning = getattr(validation_result, "reasoning", "")
    else:
        complete = validation_result.get("complete", False)
        confidence = validation_result.get("confidence", 0.0)
        passed = validation_result.get("assertions_passed", [])
        failed = validation_result.get("assertions_failed", [])
        reasoning = validation_result.get("reasoning", "")

    total = len(passed) + len(failed)
    summary_parts = [f"confidence={confidence}"]

    if total > 0:
        summary_parts.append(f"{len(passed)}/{total} assertions passed")
    if not complete:
        summary_parts.append("INCOMPLETE")

    record = ProvenanceRecord(
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        worker=worker_id,
        event_type="validation",
        skill_id=skill_id,
        domain=domain,
        url=None,
        success=complete,
        summary=" | ".join(summary_parts),
        variables=None,
        entities=None,
        error=reasoning if not complete else None,
        duration_ms=round(duration_ms, 1),
    )

    path = _provenance_path(worker_id)
    _ensure_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    return record


def query_provenance(
    worker_id: str,
    skill_id: str | None = None,
    limit: int = 50,
) -> list[ProvenanceRecord]:
    """Read recent provenance records, optionally filtered by skill.

    Scans today's file plus yesterday's if needed to reach *limit*.
    Returns records in reverse chronological order (newest first).
    """
    today = time.strftime("%Y-%m-%d")
    yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))

    records: list[ProvenanceRecord] = []

    for day in (today, yesterday):
        path = _provenance_path(worker_id, day)
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(ProvenanceRecord(**json.loads(line)))
                    except (json.JSONDecodeError, TypeError):
                        continue
        except FileNotFoundError:
            continue

    # Filter
    if skill_id:
        records = [r for r in records if r.skill_id == skill_id]

    # Reverse chronological (newest first)
    records.sort(key=lambda r: r.ts, reverse=True)

    # Limit
    return records[:limit]
