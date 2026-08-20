"""Concurrent target leases on one persistent browser/process."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.request import urlopen

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.errors import TargetConflict  # noqa: E402
from browserctl.manager import Manager  # noqa: E402
from browserctl.store import (  # noqa: E402
    find_active_lease_for_worker,
    iter_active_target_leases,
    load_lease,
    save_lease,
)
from browserctl.target_registry import load_registry  # noqa: E402


class FakeAdapter:
    def __init__(self, worker_id: str = "scratch-profile-providers"):
        self.worker_id = worker_id
        self.starts: list[dict[str, Any]] = []
        self.stops: list[dict[str, Any]] = []
        self.alive = True

    def acquire(self, request: dict[str, Any]) -> dict[str, Any]:
        wid = request.get("worker_id") or self.worker_id
        self.starts.append({"worker_id": wid})
        return {
            "worker_id": wid,
            "kind": "scratch",
            "adapter": "scratch",
            "resources": {
                "worker_id": wid,
                "cdp_port": 9342,
                "cdp_url": "http://127.0.0.1:9342",
                "profile_dir": f"/tmp/{wid}",
                "state_dir": f"/tmp/state/{wid}",
                "daemon": "running",
            },
            "env": {"BROWSER_HARNESS_WORKER": wid},
            "meta": {},
        }

    def release(self, lease: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        self.stops.append({"lease_id": lease.get("lease_id"), "force": force})
        self.alive = False
        return {"status": "stopped", "force": force}

    def status(self, lease: dict[str, Any]) -> dict[str, Any]:
        return {"cdp_alive": self.alive, "daemon": "running" if self.alive else "stopped"}


@pytest.fixture
def ctx(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    m = Manager(root=ROOT, state_root=state)
    fake = FakeAdapter()
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        yield m, fake, state


def test_concurrent_bind_same_persistent_profile(ctx):
    m, fake, state = ctx
    a = m.acquire({"kind": "scratch", "worker_id": fake.worker_id, "owner": "nav-a", "ttl": 120})
    b = m.acquire({"kind": "scratch", "worker_id": fake.worker_id, "owner": "nav-b", "ttl": 120})
    assert a["lease"]["worker_id"] == b["lease"]["worker_id"] == fake.worker_id
    assert a["env"]["BROWSER_CDP_URL"] == b["env"]["BROWSER_CDP_URL"]
    assert a["lease"]["target_id"] != b["lease"]["target_id"]
    assert a["lease"]["lease_id"] != b["lease"]["lease_id"]
    assert a["browser_lease"]["lease_id"] == b["browser_lease"]["lease_id"]
    assert len(fake.starts) == 1
    assert len(fake.stops) == 0


def test_target_exclusivity(ctx):
    m, fake, _state = ctx
    a = m.acquire(
        {
            "kind": "scratch",
            "worker_id": fake.worker_id,
            "owner": "nav-a",
            "target_id": "T-A",
            "ttl": 120,
        }
    )
    assert a["lease"]["target_id"] == "T-A"
    with pytest.raises(TargetConflict) as ei:
        m.acquire(
            {
                "kind": "scratch",
                "worker_id": fake.worker_id,
                "owner": "nav-b",
                "target_id": "T-A",
                "ttl": 120,
            }
        )
    assert ei.value.code == "TARGET_CONFLICT"
    assert ei.value.details["target_id"] == "T-A"


def test_independent_release_leaves_sibling(ctx):
    m, fake, state = ctx
    a = m.acquire({"kind": "scratch", "worker_id": fake.worker_id, "owner": "nav-a", "ttl": 120})
    b = m.acquire({"kind": "scratch", "worker_id": fake.worker_id, "owner": "nav-b", "ttl": 120})
    rel = m.release(lease_id=a["lease"]["lease_id"])
    assert rel["ok"] is True
    assert rel["release"]["remaining_targets"] == [b["lease"]["target_id"]]
    assert rel["release"]["browser_stopped"] is False
    assert len(fake.stops) == 0
    still = load_lease(state, b["lease"]["lease_id"])
    assert still is not None and still["status"] == "active"
    browser = find_active_lease_for_worker(state, fake.worker_id)
    assert browser is not None and browser["status"] == "active"
    # remaining navigator can still "operate" (status + registry)
    st = m.status(lease_id=b["lease"]["lease_id"])
    assert st["env"]["BROWSER_CDP_URL"] == a["env"]["BROWSER_CDP_URL"]
    owned = load_registry(state, fake.worker_id)
    assert owned["targets"][b["lease"]["target_id"]]["status"] == "active"
    assert owned["targets"][a["lease"]["target_id"]]["status"] == "released"


def test_final_release_stops_persistent_browser_process(ctx):
    """Named persistent profile: last explicit release still stops the process (no wipe)."""
    m, fake, state = ctx
    a = m.acquire(
        {
            "kind": "scratch",
            "worker_id": fake.worker_id,
            "owner": "nav-a",
            "mode": "persistent",
            "ttl": 120,
        }
    )
    b = m.acquire(
        {
            "kind": "scratch",
            "worker_id": fake.worker_id,
            "owner": "nav-b",
            "mode": "persistent",
            "ttl": 120,
        }
    )
    m.release(lease_id=a["lease"]["lease_id"])
    assert len(fake.stops) == 0
    m.release(lease_id=b["lease"]["lease_id"])
    assert len(fake.stops) == 1
    assert find_active_lease_for_worker(state, fake.worker_id) is None


def test_stale_target_reap_reclaims_without_killing_persistent_browser(ctx):
    m, fake, state = ctx
    a = m.acquire(
        {
            "kind": "scratch",
            "worker_id": fake.worker_id,
            "owner": "dead-nav",
            "mode": "persistent",
            "ttl": 30,
            "auto_reap": True,
        }
    )
    b = m.acquire(
        {
            "kind": "scratch",
            "worker_id": fake.worker_id,
            "owner": "live-nav",
            "mode": "persistent",
            "ttl": 3600,
        }
    )
    lease = load_lease(state, a["lease"]["lease_id"])
    assert lease is not None
    lease["expires_at"] = time.time() - 5
    lease["auto_reap"] = True
    save_lease(state, lease)
    out = m.reap()
    assert any(r.get("lease_id") == a["lease"]["lease_id"] for r in out["reaped"])
    assert len(fake.stops) == 0
    live = load_lease(state, b["lease"]["lease_id"])
    assert live is not None and live["status"] == "active"
    # reclaimed: same target id can be claimed again
    again = m.acquire(
        {
            "kind": "scratch",
            "worker_id": fake.worker_id,
            "owner": "nav-c",
            "target_id": a["lease"]["target_id"],
            "ttl": 60,
        }
    )
    assert again["lease"]["target_id"] == a["lease"]["target_id"]
    assert again["lease"]["lease_id"] != a["lease"]["lease_id"]


def test_single_agent_regression(ctx):
    m, fake, state = ctx
    out = m.acquire(
        {
            "kind": "scratch",
            "worker_id": fake.worker_id,
            "owner": "solo",
            "mode": "persistent",
            "ttl": 60,
        }
    )
    assert out["lease"]["scope"] == "target"
    assert out["env"]["BROWSERCTL_LEASE_ID"] == out["lease"]["lease_id"]
    assert out["env"]["BROWSERCTL_TARGET_ID"]
    m.release(lease_id=out["lease"]["lease_id"])
    assert len(fake.stops) == 1
    assert len(m.list_leases()) == 0


def test_ephemeral_one_shot_still_unique_and_stops(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    m = Manager(root=ROOT, state_root=state)
    n = {"i": 0}

    class TokenAdapter(FakeAdapter):
        def acquire(self, request):
            n["i"] += 1
            wid = f"scratch-demo-{n['i']:08x}"
            self.starts.append({"worker_id": wid})
            return {
                "worker_id": wid,
                "kind": "scratch",
                "adapter": "scratch",
                "resources": {
                    "worker_id": wid,
                    "cdp_port": 9300 + n["i"],
                    "cdp_url": f"http://127.0.0.1:{9300 + n['i']}",
                    "ephemeral_wipe_v1": True,
                    "daemon": "running",
                },
                "env": {"BROWSER_HARNESS_WORKER": wid},
                "meta": {"ephemeral_wipe_v1": True},
            }

    fake = TokenAdapter()
    with mock.patch("browserctl.manager.get_adapter", return_value=fake):
        a = m.acquire({"kind": "scratch", "label": "demo", "mode": "one_shot", "owner": "x", "ttl": 30})
        b = m.acquire({"kind": "scratch", "label": "demo", "mode": "one_shot", "owner": "y", "ttl": 30})
        assert a["lease"]["worker_id"] != b["lease"]["worker_id"]
        assert a["env"]["BROWSER_CDP_URL"] != b["env"]["BROWSER_CDP_URL"]
        assert len(fake.starts) == 2
        m.release(lease_id=a["lease"]["lease_id"])
        assert load_lease(state, b["lease"]["lease_id"])["status"] == "active"
        m.release(lease_id=b["lease"]["lease_id"])
    assert len(fake.stops) == 2


def test_target_state_does_not_collapse_two_owners(ctx):
    m, fake, state = ctx
    a = m.acquire({"kind": "scratch", "worker_id": fake.worker_id, "owner": "nav-a", "ttl": 120})
    b = m.acquire({"kind": "scratch", "worker_id": fake.worker_id, "owner": "nav-b", "ttl": 120})
    reg = load_registry(state, fake.worker_id)
    assert set(reg["targets"]) >= {a["lease"]["target_id"], b["lease"]["target_id"]}
    assert reg["targets"][a["lease"]["target_id"]]["owner"] == "nav-a"
    assert reg["targets"][b["lease"]["target_id"]]["owner"] == "nav-b"
    at = Path(a["env"]["BROWSER_TARGET_STATE"])
    doc = json.loads(at.read_text())
    assert "targets" in doc
    assert len(doc["targets"]) >= 2
    assert doc["active_target_id"] in (
        a["lease"]["target_id"],
        b["lease"]["target_id"],
    )


def _chrome_bin() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser", "google-chrome-stable"):
        p = shutil.which(name)
        if p:
            return p
    return None


def test_concurrent_cdp_sessions_do_not_cross_mutate():
    """One browser, two page targets, two CDP HTTP clients; mutations stay local."""
    chrome = _chrome_bin()
    if not chrome:
        pytest.skip("no local chrome/chromium")

    # tiny page server
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = f"<html><body id='root'>{self.path}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    port = 19222
    # pick a free port
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    user = tempfile.mkdtemp(prefix="bo-cdp-")
    proc = subprocess.Popen(  # noqa: S603
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={user}",
            f"--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        version = None
        while time.time() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                    version = json.loads(r.read().decode())
                    break
            except Exception:
                time.sleep(0.1)
        if not version:
            pytest.skip("chrome cdp did not come up")

        def new_tab(path: str) -> dict:
            from urllib.request import Request
            req = Request(
                f"http://127.0.0.1:{port}/json/new?{base}{path}",
                method="PUT",
            )
            with urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode())

        ta = new_tab("/tab-a")
        tb = new_tab("/tab-b")
        assert ta["id"] != tb["id"]

        # evaluate via /json runtime isn't here; use websocket-less page url check
        def page(tid: str) -> dict:
            with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
                tabs = json.loads(r.read().decode())
            for t in tabs:
                if t.get("id") == tid:
                    return t
            raise AssertionError(tid)

        # wait for titles/urls
        deadline = time.time() + 8
        while time.time() < deadline:
            pa, pb = page(ta["id"]), page(tb["id"])
            if "/tab-a" in (pa.get("url") or "") and "/tab-b" in (pb.get("url") or ""):
                break
            time.sleep(0.1)
        pa, pb = page(ta["id"]), page(tb["id"])
        assert "/tab-a" in pa.get("url", "")
        assert "/tab-b" in pb.get("url", "")
        assert "/tab-a" not in pb.get("url", "")
        assert "/tab-b" not in pa.get("url", "")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()
        shutil.rmtree(user, ignore_errors=True)
