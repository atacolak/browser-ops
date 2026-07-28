"""Dataclasses for the verify block format (Step and Procedure)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class Step:
    """A single deterministic browser automation step."""

    action: str
    store_as: str | None = None
    assert_expr: str | None = None
    message: str | None = None
    # Capture all other kwargs as action-specific params
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Procedure:
    """A sequence of steps with a configurable failure policy."""

    steps: list[dict]  # list of raw step dicts
    on_failure: Literal["stop", "continue"] = "stop"

    @classmethod
    def from_yaml_frontmatter(cls, fm: dict) -> "Procedure":
        """Parse a YAML frontmatter procedure block into a Procedure.

        Expects *fm* to contain a ``procedure`` key with ``steps`` and
        optionally ``on_failure``::

            procedure:
              steps:
                - action: navigate
                  url: "https://example.com"
                - action: verify
                  assert: "len(repos) > 0"
              on_failure: stop
        """
        proc = fm.get("procedure", {})
        return cls(
            steps=proc.get("steps", []),
            on_failure=proc.get("on_failure", "stop"),
        )
