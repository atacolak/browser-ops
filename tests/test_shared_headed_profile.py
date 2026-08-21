"""shared-headed-demo is a stable headed scratch profile."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.profiles import ProfileRegistry, launch_to_argv  # noqa: E402


def test_shared_headed_demo_in_tracked_registry():
    raw = json.loads((ROOT / "profiles" / "PROFILES.example.json").read_text())
    launch = raw["profiles"]["shared-headed-demo"]["launch"]
    assert launch["kind"] == "scratch"
    assert launch.get("headed") is True
    assert "--headed" in launch_to_argv(launch)


def test_shared_headed_demo_show(tmp_path: Path):
    dest = tmp_path / "profiles"
    dest.mkdir()
    (dest / "PROFILES.json").write_text(
        (ROOT / "profiles" / "PROFILES.example.json").read_text()
    )
    shown = ProfileRegistry(tmp_path).show("shared-headed-demo")
    assert shown["launch"]["headed"] is True
    assert "--headed" in launch_to_argv(shown["launch"])
