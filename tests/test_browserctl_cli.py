"""CLI smoke tests (no real browser)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.cli import main  # noqa: E402
from browserctl.manager import Manager  # noqa: E402


class FakeAdapter:
    def acquire(self, request):
        wid = request.get("worker_id") or "scratch-cli-1"
        return {
            "worker_id": wid,
            "kind": "scratch",
            "adapter": "scratch",
            "resources": {
                "worker_id": wid,
                "cdp_port": 9311,
                "cdp_url": "http://127.0.0.1:9311",
                "profile_dir": f"/tmp/{wid}",
                "state_dir": f"/tmp/state/{wid}",
                "daemon": "stopped",
            },
            "env": {
                "BROWSER_HARNESS_WORKER": wid,
                "PYTHONPATH": str(ROOT),
                "BROWSER_ALLOW_EVALUATE": "1",
                "BROWSER_OPS_ROOT": str(ROOT),
            },
            "meta": {},
        }

    def release(self, lease, *, force=False):
        return {"status": "stopped"}

    def status(self, lease):
        return {"cdp_alive": False, "daemon": "stopped"}


def test_cli_launch_json(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    fake = FakeAdapter()
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        code = main(
            [
                "--state-root",
                str(state),
                "--root",
                str(ROOT),
                "launch",
                "--kind",
                "scratch",
                "--owner",
                "cli-test",
                "--no-start",
                "--json",
            ]
        )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["env"]["BROWSER_HARNESS_WORKER"] == "scratch-cli-1"
    assert out["spawn"]["lease_id"] == out["lease"]["lease_id"]


def test_cli_duplicate_lease_json_error(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    fake = FakeAdapter()
    args_base = [
        "--state-root",
        str(state),
        "--root",
        str(ROOT),
        "acquire",
        "--kind",
        "scratch",
        "--worker",
        "scratch-cli-dup",
        "--no-start",
        "--json",
    ]
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        assert main(args_base) == 0
        capsys.readouterr()  # drop first success payload
        code = main(args_base)
    assert code == 3
    err = json.loads(capsys.readouterr().out)
    assert err["ok"] is False
    assert err["error"]["code"] == "LEASE_CONFLICT"


def test_cli_list_and_reap(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    fake = FakeAdapter()
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        assert (
            main(
                [
                    "--state-root",
                    str(state),
                    "acquire",
                    "--kind",
                    "scratch",
                    "--ttl",
                    "1",
                    "--no-start",
                    "--json",
                ]
            )
            == 0
        )
        # force expire via manager
        m = Manager(root=ROOT, state_root=state)
        leases = m.list_leases()
        assert len(leases) == 1
        from browserctl.store import load_lease, save_lease
        import time

        lease = load_lease(state, leases[0]["lease_id"])
        lease["expires_at"] = time.time() - 5
        save_lease(state, lease)

        code = main(["--state-root", str(state), "reap", "--json"])
    assert code == 0
    # drain capsys: last json is reap result — parse last object
    text = capsys.readouterr().out.strip()
    # may contain multiple JSON docs; take last
    chunks = text.split("\n{\n")
    if len(chunks) > 1:
        last = "{\n" + chunks[-1]
    else:
        last = text
    # find last complete json
    decoder = json.JSONDecoder()
    idx = 0
    objs = []
    text2 = text
    while text2.strip():
        text2 = text2.lstrip()
        obj, end = decoder.raw_decode(text2)
        objs.append(obj)
        text2 = text2[end:]
    reap = objs[-1]
    assert reap["ok"] is True
    assert reap["count"] >= 1
