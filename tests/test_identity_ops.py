"""Identity law: active∩retired, revive, stage ledger on retire, bootstrap."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import identity_ops as io  # noqa: E402


@pytest.fixture
def iso_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate identity_ops filesystem roots under tmp_path."""
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "profiles" / "xai").mkdir()
    monkeypatch.setattr(io, "ROOT", tmp_path)
    monkeypatch.setattr(io, "IDENTITIES_PATH", profiles / "IDENTITIES.json")
    monkeypatch.setattr(io, "API_STAGES_PATH", profiles / "API_STAGES.json")
    monkeypatch.setattr(io, "PROFILES_XAI", profiles / "xai")
    monkeypatch.setattr(io, "STATE_ROOT", tmp_path / "state")
    return tmp_path


def _reg(iso_root: Path) -> dict:
    return json.loads((iso_root / "profiles" / "IDENTITIES.json").read_text())


def test_bootstrap_empty_registry(iso_root: Path):
    assert not (iso_root / "profiles" / "IDENTITIES.json").exists()
    reg = io._load_registry()
    assert reg["identities"] == []
    assert (iso_root / "profiles" / "IDENTITIES.json").is_file()
    assert reg.get("retired_identities") == []


def test_ensure_no_start_creates_relative_paths(iso_root: Path):
    code = io.cmd_ensure("coal@example.com", as_json=True, no_start=True)
    assert code == 0
    reg = _reg(iso_root)
    assert len(reg["identities"]) == 1
    row = reg["identities"][0]
    assert row["email"] == "coal@example.com"
    assert row["worker_id"] == "xai-coal"
    assert not str(row["profile_dir"]).startswith("/")
    assert row["profile_dir"].startswith("profiles/")
    assert "retired_identities" in reg
    assert io._active_emails(reg).isdisjoint(io._retired_emails(reg))


def test_retire_removes_active_and_blocks_ensure(iso_root: Path, capsys):
    io.cmd_ensure("a@example.com", as_json=True, no_start=True)
    capsys.readouterr()  # drop ensure stdout
    code = io.cmd_retire(
        email="a@example.com",
        as_json=True,
        keep_profile=True,
        reason="test_retire",
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "retired"
    assert out["stage"]["action_required"] is True

    reg = _reg(iso_root)
    assert reg["identities"] == []
    assert len(reg["retired_identities"]) == 1
    assert reg["retired_identities"][0]["reason"] == "test_retire"
    assert io._active_emails(reg).isdisjoint(io._retired_emails(reg))

    with pytest.raises(SystemExit) as ei:
        io.cmd_ensure("a@example.com", as_json=True, no_start=True)
    assert ei.value.code == 2


def test_ensure_revive_after_retire(iso_root: Path):
    io.cmd_ensure("b@example.com", as_json=True, no_start=True)
    io.cmd_retire(
        email="b@example.com",
        as_json=True,
        keep_profile=True,
        reason="cycle",
    )
    code = io.cmd_ensure(
        "b@example.com", as_json=True, no_start=True, revive=True
    )
    assert code == 0
    reg = _reg(iso_root)
    assert len(reg["identities"]) == 1
    assert reg["identities"][0]["email"] == "b@example.com"
    # revive drops retired entries for that email
    assert all(
        r.get("email", "").lower() != "b@example.com"
        for r in reg.get("retired_identities") or []
    )
    assert io._active_emails(reg).isdisjoint(io._retired_emails(reg))


def test_active_retired_invariant_on_save(iso_root: Path):
    reg = io._load_registry()
    reg["identities"] = [
        {
            "email": "x@example.com",
            "slug": "x",
            "worker_id": "xai-x",
            "cdp_port": 9223,
            "profile_dir": "profiles/xai/x",
            "state_dir": "state/xai-x",
        }
    ]
    reg["retired_identities"] = [
        {"email": "x@example.com", "reason": "bad", "retired_at": "t"}
    ]
    with pytest.raises(SystemExit) as ei:
        io._save_registry(reg)
    assert ei.value.code == 3


def test_stage_set_s4_marks_action_required(iso_root: Path, capsys):
    io.cmd_ensure("s@example.com", as_json=True, no_start=True)
    capsys.readouterr()
    code = io.cmd_stage_set(
        email="s@example.com",
        stage="S4",
        as_json=True,
        bot_flag_source="1",
        last_billing_http=403,
        full_oauth_fail=False,
        full_oauth_ok=False,
        note="probe",
        force_s3_fails=3,
    )
    assert code == 0
    row = json.loads(capsys.readouterr().out)
    assert row["api_stage"] == "S4"
    assert row.get("action_required") is True

    stages = json.loads((iso_root / "profiles" / "API_STAGES.json").read_text())
    assert "s@example.com" in stages["accounts"]


def test_retire_updates_stage_ledger(iso_root: Path):
    io.cmd_ensure("t@example.com", as_json=True, no_start=True)
    io.cmd_stage_set(
        email="t@example.com",
        stage="S1",
        as_json=True,
        bot_flag_source="1",
        last_billing_http=200,
        full_oauth_fail=False,
        full_oauth_ok=True,
        note="ok",
        force_s3_fails=None,
    )
    io.cmd_retire(
        email="t@example.com",
        as_json=True,
        keep_profile=True,
        reason="done",
    )
    stages = json.loads((iso_root / "profiles" / "API_STAGES.json").read_text())
    row = stages["accounts"]["t@example.com"]
    assert row.get("action_required") is True
    assert "retired:done" in (row.get("note") or "")
    assert any("retire" in (h.get("note") or "") for h in row.get("history") or [])


def test_show_retired_fails_closed(iso_root: Path):
    io.cmd_ensure("z@example.com", as_json=True, no_start=True)
    io.cmd_retire(
        email="z@example.com",
        as_json=True,
        keep_profile=True,
        reason="x",
    )
    with pytest.raises(SystemExit) as ei:
        io.cmd_show("z@example.com", as_json=True)
    assert ei.value.code == 2
