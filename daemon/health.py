"""
Skill health ledger — tracks success/failure counts per skill.

Stores aggregate health data at ``state/<worker>/skill-health.jsonl``.
Each line is a JSON record with per-skill aggregate stats.

Usage::

    from health import write_outcome, read_health, get_degraded_skills

    # After a skill run, record the outcome
    write_outcome("worker-b", "search-repos", "github.com", True)

    # Read all health records for a worker
    records = read_health("worker-b")

    # Check which skills are degraded
    bad = get_degraded_skills("worker-b")
"""

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class SkillHealthRecord:
    """Aggregate health stats for a single skill."""

    skill_id: str
    domain: str
    total_runs: int = 0
    successes: int = 0
    failures: int = 0
    success_rate: float = 0.0  # 0.0–1.0
    last_success: Optional[str] = None  # ISO timestamp
    last_failure: Optional[str] = None  # ISO timestamp
    last_error: Optional[str] = None  # Most recent error message
    consecutive_failures: int = 0  # Reset on success
    degraded: bool = False  # True if success_rate < 0.8 or consecutive_failures >= 3
    updated_at: str = ""  # ISO timestamp


# ── Helpers ──────────────────────────────────────────────────────────────────


def _state_dir() -> str:
    """Resolve the state directory from env or default."""
    return os.environ.get(
        "BROWSER_OPS_STATE",
        os.path.join(os.path.dirname(__file__), "..", "state"),
    )


def _health_path(worker_id: str) -> str:
    """Return the path to the skill-health.jsonl file for a worker."""
    return os.path.join(_state_dir(), worker_id, "skill-health.jsonl")


def _compute_degraded(record: SkillHealthRecord) -> bool:
    """Return True if the skill is considered degraded."""
    return record.success_rate < 0.8 or record.consecutive_failures >= 3


def _ensure_dir(path: str) -> None:
    """Ensure the parent directory of a file path exists."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


# ── Public API ───────────────────────────────────────────────────────────────


def read_health(
    worker_id: str, skill_id: Optional[str] = None
) -> list[SkillHealthRecord]:
    """Read health records from state/<worker>/skill-health.jsonl.

    If *skill_id* is given, return only records matching that skill.
    Otherwise return all records.
    """
    path = _health_path(worker_id)
    try:
        with open(path, "r") as f:
            records = [
                SkillHealthRecord(**json.loads(line))
                for line in f
                if line.strip()
            ]
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    if skill_id:
        records = [r for r in records if r.skill_id == skill_id]

    return records


def write_outcome(
    worker_id: str,
    skill_id: str,
    domain: str,
    success: bool,
    error: Optional[str] = None,
) -> SkillHealthRecord:
    """Update the health record for a skill after a run.

    If no record exists, create one.  Otherwise update aggregate stats.
    Rewrites the entire JSONL file (small files, simpler than append+compact).

    Returns the updated ``SkillHealthRecord``.
    """
    now = datetime.now(timezone.utc).isoformat()
    path = _health_path(worker_id)
    _ensure_dir(path)

    # Read existing records
    records = read_health(worker_id)

    # Find or create the record for this domain/skill
    record: Optional[SkillHealthRecord] = None
    for r in records:
        if r.domain == domain and r.skill_id == skill_id:
            record = r
            break

    if record is None:
        record = SkillHealthRecord(
            skill_id=skill_id,
            domain=domain,
        )
        records.append(record)

    # Update aggregate stats
    record.total_runs += 1

    if success:
        record.successes += 1
        record.last_success = now
        record.consecutive_failures = 0
    else:
        record.failures += 1
        record.last_failure = now
        record.consecutive_failures += 1
        if error:
            record.last_error = error

    # Recalculate derived fields
    record.success_rate = (
        round(record.successes / record.total_runs, 4) if record.total_runs > 0 else 0.0
    )
    record.degraded = _compute_degraded(record)
    record.updated_at = now

    # Rewrite the entire file
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    return record


def get_degraded_skills(worker_id: str) -> list[SkillHealthRecord]:
    """Return all skills marked as degraded."""
    return [r for r in read_health(worker_id) if r.degraded]
