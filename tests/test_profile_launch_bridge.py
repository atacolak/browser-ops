"""--profile NAME: exclusive selector, show lookup, lease/env stamp."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.cli import main  # noqa: E402
from browserctl.errors import InvalidRequest  # noqa: E402
from browserctl.manager import Manager  # noqa: E402
from browserctl.profiles import ProfileNotFound, ProfileRegistry  # noqa: E402
from browserctl.store import load_lease  # noqa: E402


class FakeAdapter:
    def __init__(self):
        self.starts: list[dict] = []

    def acquire(self, request):
        self.starts.append(dict(request))
        kind = (request.get("kind") or "scratch").lower()
        wid = (
            request.get("worker_id")
            or request.get("worker")
            or (f"scratch-{request['label']}-1" if request.get("label") else None)
            or "scratch-prof-1"
        )
        return {
            "worker_id": wid,
            "kind": kind if kind != "adhoc" else "scratch",
            "adapter": "scratch",
            "resources": {
                "worker_id": wid,
                "cdp_port": 9310,
                "cdp_url": "http://127.0.0.1:9310",
                "daemon": "stopped",
            },
            "env": {"BROWSER_HARNESS_WORKER": wid},
            "meta": {},
        }

    def release(self, lease, *, force=False):
        return {"status": "stopped"}

    def status(self, lease):
        return {"cdp_alive": False}


@pytest.fixture
def env(tmp_path: Path):
    (tmp_path / "profiles").mkdir()
    state = tmp_path / "state"
    state.mkdir()
    return tmp_path, state


def _reg(root, name, launch):
    return ProfileRegistry(root).register(name, launch=launch, description=f"{name} test face")


def test_profile_stamp_and_headed(env):
    root, state = env
    _reg(root, "headed-lab", {"kind": "scratch", "label": "vis", "headed": True})
    fake = FakeAdapter()
    m = Manager(root=root, state_root=state)
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        out = m.acquire({"profile_name": "headed-lab", "owner": "orch", "ttl": 60})
    assert out["lease"]["profile_name"] == "headed-lab"
    assert out["env"]["BROWSERCTL_PROFILE_NAME"] == "headed-lab"
    assert out["spawn"]["env"]["BROWSERCTL_PROFILE_NAME"] == "headed-lab"
    assert fake.starts[0]["headed"] is True
    assert fake.starts[0]["headless"] is False
    loaded = load_lease(state, out["lease"]["lease_id"])
    assert loaded["profile_name"] == "headed-lab"
    assert loaded["env"]["BROWSERCTL_PROFILE_NAME"] == "headed-lab"


def test_manager_strict_exclusion_and_unknown(env):
    root, state = env
    _reg(root, "lab-demo", {"kind": "scratch", "label": "demo"})
    m = Manager(root=root, state_root=state)
    fake = FakeAdapter()
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        with pytest.raises(InvalidRequest) as ei:
            m.acquire(
                {"profile_name": "lab-demo", "label": "other", "owner": "o"}
            )
        assert "label" in ei.value.details["fields"]
        with pytest.raises(InvalidRequest):
            m.acquire(
                {
                    "kind": "scratch",
                    "headed": True,
                    "headless": True,
                    "owner": "o",
                    "ttl": 60,
                }
            )
        with pytest.raises(ProfileNotFound):
            m.acquire({"profile_name": "missing", "owner": "o"})
    assert fake.starts == []


def test_cli_profile_conflict_and_legacy(env, capsys):
    root, state = env
    assert (
        main(
            [
                "--root",
                str(root),
                "profiles",
                "register",
                "lab-demo",
                "--kind",
                "scratch",
                "--label",
                "demo",
                "--description",
                "lab-demo test face",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    fake = FakeAdapter()
    base = ["--root", str(root), "--state-root", str(state)]
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        assert (
            main([*base, "launch", "--profile", "lab-demo", "--owner", "t", "--json"])
            == 0
        )
        out = json.loads(capsys.readouterr().out)
        assert out["lease"]["profile_name"] == "lab-demo"
        assert out["env"]["BROWSERCTL_PROFILE_NAME"] == "lab-demo"
        assert fake.starts[-1]["label"] == "demo"

        with pytest.raises(SystemExit) as se:
            main(
                [
                    *base,
                    "launch",
                    "--profile",
                    "lab-demo",
                    "--label",
                    "other",
                    "--json",
                ]
            )
        assert se.value.code == 2

        assert (
            main(
                [
                    *base,
                    "launch",
                    "--kind",
                    "scratch",
                    "--label",
                    "legacy",
                    "--no-start",
                    "--json",
                ]
            )
            == 0
        )
        leg = json.loads(capsys.readouterr().out)
        assert not leg["lease"].get("profile_name")
        assert "BROWSERCTL_PROFILE_NAME" not in leg["env"]
        assert fake.starts[-1].get("headless") is True
        assert fake.starts[-1].get("no_start") is True
