"""Live abuse: concurrency, death, steal, stale sidecar, 20+ tabs, many clients.

One unique scratch chrome. Skip if launch fails. Always force-release in finally.
"""

from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.adapters.scratch import _chrome_pids, _daemon_pids, _kill_pids, _start_daemon  # noqa: E402
from browserctl.errors import AdapterError, BrowserctlError, LeaseNotFound  # noqa: E402
from browserctl.manager import Manager  # noqa: E402
from browserctl.store import load_lease  # noqa: E402
from browserctl.tab_policy import lease_can_mutate  # noqa: E402

BUN = os.environ.get("BUN", "bun")


def _rpc(sock: Path, payload: dict, timeout: float = 30.0) -> dict:
    data = (json.dumps(payload) + "\n").encode()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(sock))
        client.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError as e:
        return {"code": "SOCKET_DEAD", "message": str(e)}
    finally:
        client.close()
    if not buf:
        return {"code": "EMPTY", "message": "empty daemon response"}
    try:
        return json.loads(buf.split(b"\n", 1)[0].decode())
    except json.JSONDecodeError as e:
        return {"code": "BAD_JSON", "message": str(e)}


def _wait_sock(sock: Path, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sock.exists():
            ping = _rpc(sock, {"action": "ping"}, timeout=2.0)
            if ping.get("ok") is True:
                return True
        time.sleep(0.1)
    return False


def _launch_pair(m: Manager, worker: str) -> tuple[dict, dict]:
    a = m.launch(
        {
            "kind": "scratch",
            "worker_id": worker,
            "owner": "abuse-a",
            "mode": "one_shot",
            "ttl": 240,
            "headless": True,
            "label": "abuse",
        }
    )
    b = m.launch(
        {
            "kind": "scratch",
            "worker_id": worker,
            "owner": "abuse-b",
            "mode": "one_shot",
            "ttl": 240,
            "headless": True,
        }
    )
    return a, b


def _mint(sock: Path, parent: dict, url: str = "about:blank") -> dict:
    return _rpc(
        sock,
        {
            "action": "new_tab",
            "url": url,
            "lease_id": parent["lease"]["lease_id"],
            "target_id": parent["lease"]["target_id"],
        },
        timeout=60,
    )


def _nav(sock: Path, lease_id: str, target_id: str, url: str) -> dict:
    return _rpc(
        sock,
        {
            "action": "navigate",
            "url": url,
            "lease_id": lease_id,
            "target_id": target_id,
        },
        timeout=60,
    )


def _bun_available() -> bool:
    try:
        r = subprocess.run([BUN, "--version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_bun(script: str) -> dict:
    r = subprocess.run(
        [BUN, "-e", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=40,
        env={**os.environ},
    )
    if r.returncode != 0:
        raise AssertionError(f"bun failed ({r.returncode}): {r.stderr or r.stdout}")
    line = (r.stdout or "").strip().splitlines()[-1]
    return json.loads(line)


@pytest.fixture(scope="module")
def live():
    worker = f"scratch-abuse-{uuid.uuid4().hex[:8]}"
    m = Manager(root=ROOT)
    a = b = None
    try:
        try:
            a, b = _launch_pair(m, worker)
        except (AdapterError, BrowserctlError) as e:
            pytest.skip(f"scratch launch failed: {e}")
        sock = Path(a["lease"]["resources"]["socket"])
        if not _wait_sock(sock):
            pytest.skip(f"daemon.sock never answered ping: {sock}")
        yield {
            "m": m,
            "worker": worker,
            "a": a,
            "b": b,
            "sock": sock,
            "state": m.state_root,
            "profile": a["lease"]["resources"]["profile_dir"],
            "port": int(a["lease"]["resources"]["cdp_port"]),
            "cdp": a["lease"]["resources"]["cdp_url"],
            "root": ROOT,
        }
    finally:
        seen = set()
        for row in (a, b):
            if not row:
                continue
            lid = row["lease"]["lease_id"]
            if lid in seen:
                continue
            seen.add(lid)
            try:
                m.release(lease_id=lid, force=True)
            except Exception:
                pass
        try:
            m.release(worker_id=worker, force=True)
        except Exception:
            pass
        time.sleep(0.2)


def test_concurrency(live):
    sock = live["sock"]
    a, b = live["a"], live["b"]
    la, ta = a["lease"]["lease_id"], a["lease"]["target_id"]
    lb, tb = b["lease"]["lease_id"], b["lease"]["target_id"]
    errors: list[str] = []

    def go(lid, tid, n):
        out = _nav(sock, lid, tid, f"https://example.com/#{n}-{tid[-4:]}")
        if out.get("code"):
            errors.append(f"{tid}:{out}")
        return out

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(go, la, ta, i) for i in range(8)] + [
            ex.submit(go, lb, tb, i) for i in range(8)
        ]
        for f in as_completed(futs):
            f.result()
    assert not errors, errors
    pa = _rpc(sock, {"action": "page_info", "target_id": ta, "lease_id": la})
    pb = _rpc(sock, {"action": "page_info", "target_id": tb, "lease_id": lb})
    assert ta[-4:] in (pa.get("url") or "") or "example.com" in (pa.get("url") or "")
    assert tb[-4:] in (pb.get("url") or "") or "example.com" in (pb.get("url") or "")
    assert (pa.get("url") or "") != (pb.get("url") or "") or ta == tb  # last hash may collide only if same tab
    listed = _rpc(sock, {"action": "tabs", "lease_id": la})
    by = {t.get("targetId") or t.get("target_id"): t.get("ownership") for t in listed.get("tabs") or []}
    assert by.get(ta) == "owned_by_me"
    assert by.get(tb) == "owned_by"


def test_operator_steal_races(live):
    m, sock, state = live["m"], live["sock"], live["state"]
    a, b = live["a"], live["b"]
    worker = live["worker"]
    la, ta = a["lease"]["lease_id"], a["lease"]["target_id"]
    lb, tb = b["lease"]["lease_id"], b["lease"]["target_id"]

    stolen = m.acquire(
        {
            "kind": "scratch",
            "worker_id": worker,
            "owner": "thief",
            "ttl": 240,
            "target_id": tb,
            "steal": True,
        }
    )
    lc = stolen["lease"]["lease_id"]
    assert lc != lb
    assert stolen["lease"]["target_id"] == tb
    assert lease_can_mutate(state, lb) is None
    assert lease_can_mutate(state, lc, worker_id=worker, target_id=tb) is not None

    conflict = _nav(sock, lb, tb, "https://example.net/stolen")
    assert conflict.get("code") in {"TARGET_LEASE_REQUIRED", "TARGET_CONFLICT"}, conflict

    ok = _nav(sock, lc, tb, "https://example.org/thief")
    assert ok.get("code") is None, ok

    peek = _rpc(sock, {"action": "switch_tab", "lease_id": la, "dest_target_id": tb})
    assert peek.get("mode") == "peek", peek

    still_a = _nav(sock, la, ta, "https://example.com/still-a")
    assert still_a.get("code") is None, still_a

    # restore b as a live sibling for later tests: steal back
    restored = m.acquire(
        {
            "kind": "scratch",
            "worker_id": worker,
            "owner": "abuse-b",
            "ttl": 240,
            "target_id": tb,
            "steal": True,
        }
    )
    live["b"] = restored
    try:
        m.release(lease_id=lc, force=True)
    except Exception:
        pass


def test_stale_sidecars(live, tmp_path):
    if not _bun_available():
        pytest.skip("bun required")
    sock = live["sock"]
    a, b = live["a"], live["b"]
    la, ta = a["lease"]["lease_id"], a["lease"]["target_id"]
    lb, tb = b["lease"]["lease_id"], b["lease"]["target_id"]

    session = tmp_path / "session.jsonl"
    session.write_text("")
    sidecar = Path(str(session) + ".bind-profile.json")
    sidecar.write_text(
        json.dumps(
            {
                "leaseId": la,
                "targetId": ta,
                "worker": live["worker"],
                "socket": str(sock),
                "cdp": live["cdp"],
                "root": str(ROOT),
                "held": {ta: la, tb: lb},
            }
        )
    )
    # steal B so sidecar's held[B] is a lie
    stolen = live["m"].acquire(
        {
            "kind": "scratch",
            "worker_id": live["worker"],
            "owner": "stale-thief",
            "ttl": 240,
            "target_id": tb,
            "steal": True,
        }
    )
    bin_path = json.dumps(str(ROOT / "bin" / "browserctl"))
    root_js = json.dumps(str(ROOT))
    script = f"""
        import cloakTool from "./omp/cloak.ts";
        import {{ readSidecar }} from "./omp/bind-profile.ts";
        const exec = async (_cmd, args) => {{
          const proc = Bun.spawn([ {bin_path}, ...args, "--json", "--root", {root_js} ], {{ stdout: "pipe", stderr: "pipe" }});
          const stdout = await new Response(proc.stdout).text();
          const stderr = await new Response(proc.stderr).text();
          const code = await proc.exited;
          return {{ stdout, stderr, code }};
        }};
        const tool = cloakTool({{ exec }});
        const ctx = {{ sessionManager: {{ getSessionFile: () => {json.dumps(str(session))} }} }};
        const result = await tool.execute("id", {{ action: "tabs" }}, undefined, ctx);
        const sc = readSidecar(ctx);
        const text = result.content[0].text;
        let body = {{}};
        try {{ body = JSON.parse(text); }} catch {{}}
        console.log(JSON.stringify({{
          isError: !!result.isError,
          text,
          byId: Object.fromEntries((body.tabs || []).map(t => [t.targetId || t.target_id, t.ownership])),
          held: sc?.held || {{}},
          target: sc?.targetId,
        }}));
    """
    try:
        out = _run_bun(script)
        assert out["isError"] is not True, out
        assert out["byId"].get(ta) == "owned_by_me"
        assert out["byId"].get(tb) in {"owned_by", "unowned"}
        assert tb not in (out["held"] or {})
        assert out["target"] == ta
        assert (out["held"] or {}).get(ta) == la
        still = _nav(sock, la, ta, "https://example.com/after-prune")
        assert still.get("code") is None, still
    finally:
        restored = live["m"].acquire(
            {
                "kind": "scratch",
                "worker_id": live["worker"],
                "owner": "abuse-b",
                "ttl": 240,
                "target_id": tb,
                "steal": True,
            }
        )
        live["b"] = restored
        try:
            live["m"].release(lease_id=stolen["lease"]["lease_id"], force=True)
        except Exception:
            pass


def test_release_failures(live):
    sock = live["sock"]
    a = live["a"]
    minted = _mint(sock, a, "https://example.net/to-close")
    assert minted.get("ownership") == "owned_by_me", minted
    new_tid = minted["target_id"]
    new_lid = minted["lease_id"]
    closed = _rpc(
        sock,
        {"action": "close_tab", "lease_id": new_lid, "dest_target_id": new_tid},
    )
    assert closed.get("closed") == new_tid, closed
    again = _rpc(
        sock,
        {"action": "close_tab", "lease_id": new_lid, "dest_target_id": new_tid},
    )
    try:
        ghost = live["m"].release(lease_id=new_lid, force=True)
        assert ghost.get("ok") is True or ghost.get("idempotent") is True
    except LeaseNotFound:
        pass
    sibling = _nav(sock, a["lease"]["lease_id"], a["lease"]["target_id"], "https://example.com/after-ghost")
    assert sibling.get("code") is None, sibling


def test_twenty_plus_tabs(live):
    sock = live["sock"]
    a = live["a"]
    parent = a
    minted: list[dict] = []
    for i in range(22):
        out = _mint(sock, parent, f"https://example.com/t{i}")
        assert out.get("code") is None, out
        assert out.get("target_id") and out.get("lease_id")
        minted.append(out)
        parent = {"lease": {"lease_id": out["lease_id"], "target_id": out["target_id"]}}
    listed = _rpc(sock, {"action": "tabs", "lease_id": a["lease"]["lease_id"]})
    n = len(listed.get("tabs") or [])
    assert n >= 24, n  # original A+B plus 22
    # close extras so later death tests aren't drowning
    for row in minted:
        _rpc(
            sock,
            {
                "action": "close_tab",
                "lease_id": row["lease_id"],
                "dest_target_id": row["target_id"],
            },
            timeout=60,
        )
    leftover = _rpc(sock, {"action": "tabs", "lease_id": a["lease"]["lease_id"]})
    ids = {t.get("targetId") or t.get("target_id") for t in leftover.get("tabs") or []}
    assert a["lease"]["target_id"] in ids
    assert live["b"]["lease"]["target_id"] in ids


def test_many_simultaneous_omps(live):
    sock = live["sock"]
    a, b = live["a"], live["b"]
    clients = []
    for i in range(6):
        src = a if i % 2 == 0 else b
        out = _mint(sock, src, f"https://example.com/omp{i}")
        assert out.get("code") is None, out
        clients.append(out)

    def hammer(row, n):
        return _nav(sock, row["lease_id"], row["target_id"], f"https://example.com/omp-{n}")

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(hammer, row, i) for i, row in enumerate(clients)]
        results = [f.result() for f in as_completed(futs)]
    bad = [r for r in results if r.get("code")]
    assert not bad, bad
    for row in clients:
        _rpc(
            sock,
            {
                "action": "close_tab",
                "lease_id": row["lease_id"],
                "dest_target_id": row["target_id"],
            },
        )


def test_random_action_sequences(live):
    sock = live["sock"]
    a, b = live["a"], live["b"]
    rng = random.Random(0xA50E)
    pool = [
        (a["lease"]["lease_id"], a["lease"]["target_id"]),
        (b["lease"]["lease_id"], b["lease"]["target_id"]),
    ]
    extras: list[tuple[str, str]] = []
    fatal = []
    for i in range(40):
        op = rng.choice(["nav", "tabs", "peek", "mint", "close", "page", "steal_forbidden"])
        lid, tid = rng.choice(pool + extras) if (pool + extras) else pool[0]
        if op == "nav":
            out = _nav(sock, lid, tid, f"https://example.com/r{i}")
        elif op == "tabs":
            out = _rpc(sock, {"action": "tabs", "lease_id": lid})
        elif op == "peek":
            other = b["lease"]["target_id"] if tid != b["lease"]["target_id"] else a["lease"]["target_id"]
            out = _rpc(sock, {"action": "switch_tab", "lease_id": lid, "dest_target_id": other})
        elif op == "mint":
            out = _rpc(
                sock,
                {"action": "new_tab", "url": f"https://example.com/m{i}", "lease_id": lid, "target_id": tid},
                timeout=60,
            )
            if out.get("lease_id") and out.get("target_id"):
                extras.append((out["lease_id"], out["target_id"]))
        elif op == "close":
            if not extras:
                continue
            xlid, xtid = extras.pop()
            out = _rpc(sock, {"action": "close_tab", "lease_id": xlid, "dest_target_id": xtid})
        elif op == "page":
            out = _rpc(sock, {"action": "page_info", "target_id": tid, "lease_id": lid})
        else:
            other = b["lease"]["target_id"] if tid != b["lease"]["target_id"] else a["lease"]["target_id"]
            out = _rpc(
                sock,
                {"action": "switch_tab", "lease_id": lid, "dest_target_id": other, "steal": True},
            )
            if out.get("code") != "STEAL_FORBIDDEN":
                fatal.append(("steal", out))
            continue
        if out.get("code") in {"SOCKET_DEAD", "EMPTY", "BAD_JSON"}:
            fatal.append((op, out))
    ping = _rpc(sock, {"action": "ping"})
    assert ping.get("ok") is True, ping
    assert not fatal, fatal
    for xlid, xtid in extras:
        _rpc(sock, {"action": "close_tab", "lease_id": xlid, "dest_target_id": xtid})


def test_daemon_death(live):
    worker = live["worker"]
    sock = live["sock"]
    a = live["a"]
    pids = _daemon_pids(worker)
    assert pids, "no daemon pids to kill"
    _kill_pids(pids)
    time.sleep(0.3)
    dead = _rpc(sock, {"action": "ping"}, timeout=2.0)
    assert dead.get("code") in {"SOCKET_DEAD", "EMPTY", "BAD_JSON"} or dead.get("ok") is not True, dead

    res = a["lease"]["resources"]
    # playwright child chrome usually dies with the daemon. relaunch the pair.
    _start_daemon(
        root=ROOT,
        worker_id=worker,
        port=int(res["cdp_port"]),
        profile_dir=Path(res["profile_dir"]),
        env={
            "BROWSER_HARNESS_WORKER": worker,
            "PYTHONPATH": str(ROOT),
            "BROWSER_ALLOW_EVALUATE": "1",
            "BROWSER_OPS_ROOT": str(ROOT),
            "BROWSER_OPS_STATE": str(live["state"]),
            "BROWSER_TARGET_STATE": res.get("target_state_path") or "",
        },
        headless=True,
    )
    assert _wait_sock(sock, timeout=25), "daemon did not come back"
    ping = _rpc(sock, {"action": "ping"})
    assert ping.get("ok") is True, ping
    nav = _nav(sock, a["lease"]["lease_id"], a["lease"]["target_id"], "https://example.com/after-daemon-death")
    assert nav.get("code") is None, nav


def test_chrome_death(live):
    worker = live["worker"]
    sock = live["sock"]
    a = live["a"]
    profile = live["profile"]
    chrome = _chrome_pids(profile)
    if chrome:
        _kill_pids(chrome)
        time.sleep(0.5)
        out = _nav(sock, a["lease"]["lease_id"], a["lease"]["target_id"], "https://example.com/chrome-dead")
        assert out.get("code") or out.get("ok") is not True or "error" in out, out
    # restart the whole worker via force release + relaunch of A/B
    try:
        live["m"].release(worker_id=worker, force=True)
    except Exception:
        _kill_pids(_daemon_pids(worker) + _chrome_pids(profile))
    time.sleep(0.4)
    a2, b2 = _launch_pair(live["m"], worker)
    live["a"], live["b"] = a2, b2
    live["sock"] = Path(a2["lease"]["resources"]["socket"])
    live["profile"] = a2["lease"]["resources"]["profile_dir"]
    live["port"] = int(a2["lease"]["resources"]["cdp_port"])
    live["cdp"] = a2["lease"]["resources"]["cdp_url"]
    assert _wait_sock(live["sock"], timeout=25)
    nav = _nav(
        live["sock"],
        a2["lease"]["lease_id"],
        a2["lease"]["target_id"],
        "https://example.com/after-chrome-death",
    )
    assert nav.get("code") is None, nav


def test_process_death(live):
    """Kill daemon+chrome together (the process pair). Relaunch must work."""
    worker = live["worker"]
    profile = live["profile"]
    _kill_pids(_daemon_pids(worker) + _chrome_pids(profile))
    time.sleep(0.4)
    dead = _rpc(live["sock"], {"action": "ping"}, timeout=2.0)
    assert dead.get("ok") is not True
    try:
        live["m"].release(worker_id=worker, force=True)
    except Exception:
        pass
    time.sleep(0.3)
    a2, b2 = _launch_pair(live["m"], worker)
    live["a"], live["b"] = a2, b2
    live["sock"] = Path(a2["lease"]["resources"]["socket"])
    live["profile"] = a2["lease"]["resources"]["profile_dir"]
    live["port"] = int(a2["lease"]["resources"]["cdp_port"])
    live["cdp"] = a2["lease"]["resources"]["cdp_url"]
    assert _wait_sock(live["sock"], timeout=25)
    ping = _rpc(live["sock"], {"action": "ping"})
    assert ping.get("ok") is True, ping


def test_resume_restart(live, tmp_path):
    if not _bun_available():
        pytest.skip("bun required")
    sock = live["sock"]
    a = live["a"]
    la, ta = a["lease"]["lease_id"], a["lease"]["target_id"]
    session = tmp_path / "resume.jsonl"
    session.write_text("")
    sidecar = Path(str(session) + ".bind-profile.json")
    sidecar.write_text(
        json.dumps(
            {
                "leaseId": la,
                "targetId": ta,
                "worker": live["worker"],
                "socket": str(sock),
                "cdp": live["cdp"],
                "root": str(ROOT),
                "held": {ta: la},
            }
        )
    )
    bin_path = json.dumps(str(ROOT / "bin" / "browserctl"))
    root_js = json.dumps(str(ROOT))
    script = f"""
        import {{ createBinder, readSidecar }} from "./omp/bind-profile.ts";
        const calls = [];
        const exec = async (cmd, args) => {{
          calls.push(typeof cmd === "string" && cmd.endsWith("browserctl") ? args[0] : cmd);
          const argv = cmd === "python3" || cmd.endsWith("python3")
            ? [cmd, ...args]
            : [ {bin_path}, ...args, "--json", "--root", {root_js} ];
          const proc = Bun.spawn(argv, {{ stdout: "pipe", stderr: "pipe" }});
          const stdout = await new Response(proc.stdout).text();
          const stderr = await new Response(proc.stderr).text();
          const code = await proc.exited;
          return {{ stdout, stderr, code }};
        }};
        const binder = createBinder(exec);
        const ctx = {{ sessionManager: {{ getSessionFile: () => {json.dumps(str(session))} }} }};
        const result = await binder.bind(ctx, {{ scratch: true }});
        const sc = readSidecar(ctx);
        console.log(JSON.stringify({{ ok: result.ok, reused: result.reused, target: sc?.targetId, lease: sc?.leaseId, calls }}));
    """
    out = _run_bun(script)
    assert out["ok"] is True, out
    assert out["reused"] is True, out
    assert out["target"] == ta
    assert out["lease"] == la
    assert "launch" not in out["calls"]
    nav = _nav(sock, la, ta, "https://example.com/after-resume")
    assert nav.get("code") is None, nav
    row = load_lease(live["state"], la)
    assert row and row.get("status") == "active"
