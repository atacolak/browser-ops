"""Cheap cloak tool units: factory metadata, sidecar fields, json-line rpc.

Does not boot omp. Uses bun to import omp/cloak.ts against a fake unix socket.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUN = os.environ.get("BUN", "bun")


def _bun_available() -> bool:
    try:
        r = subprocess.run([BUN, "--version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not _bun_available(), reason="bun required to load omp/cloak.ts")


def _run_bun(script: str, *, env: dict[str, str] | None = None) -> dict:
    r = subprocess.run(
        [BUN, "-e", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, **(env or {})},
    )
    if r.returncode != 0:
        raise AssertionError(f"bun failed ({r.returncode}): {r.stderr or r.stdout}")
    line = (r.stdout or "").strip().splitlines()[-1]
    return json.loads(line)


def test_cloak_factory_is_hidden_discoverable_named_cloak():
    out = _run_bun(
        """
        import cloakTool from "./omp/cloak.ts";
        const tool = cloakTool({ exec: async () => ({ stdout: "", stderr: "", code: 0 }) });
        console.log(JSON.stringify({
          name: tool.name,
          hidden: tool.hidden,
          defaultInactive: tool.defaultInactive,
          loadMode: tool.loadMode,
        }));
        """
    )
    assert out == {"name": "cloak", "hidden": True, "defaultInactive": True, "loadMode": "discoverable"}



def test_state_from_launch_sidecar_has_socket_worker_target():
    out = _run_bun(
        """
        import { stateFromLaunch, sidecarComplete } from "./omp/bind-profile.ts";
        const built = stateFromLaunch({
          env: {
            BROWSERCTL_LEASE_ID: "L1",
            BROWSER_CDP_URL: "http://127.0.0.1:9333",
            BROWSER_HARNESS_WORKER: "scratch-demo",
            BROWSERCTL_TARGET_ID: "T1",
          },
          lease: {
            lease_id: "L1",
            worker_id: "scratch-demo",
            target_id: "T1",
            resources: { socket: "/tmp/state/scratch-demo/daemon.sock" },
          },
        }, "/ops", "scratch-demo", true);
        console.log(JSON.stringify({ complete: sidecarComplete(built), state: built }));
        """
    )
    assert out["complete"] is True
    state = out["state"]
    assert state["leaseId"] == "L1"
    assert state["targetId"] == "T1"
    assert state["worker"] == "scratch-demo"
    assert state["socket"] == "/tmp/state/scratch-demo/daemon.sock"
    assert state["cdp"] == "http://127.0.0.1:9333"


def test_state_from_launch_derives_socket_from_worker():
    out = _run_bun(
        """
        import { stateFromLaunch } from "./omp/bind-profile.ts";
        const built = stateFromLaunch({
          env: {
            BROWSERCTL_LEASE_ID: "L1",
            BROWSER_CDP_URL: "http://127.0.0.1:9333",
            BROWSER_HARNESS_WORKER: "w1",
            BROWSERCTL_TARGET_ID: "T9",
          },
          lease: { lease_id: "L1", worker_id: "w1", target_id: "T9" },
        }, "/ops", "w1", false);
        console.log(JSON.stringify(built));
        """
    )
    assert out["socket"] == "/ops/state/w1/daemon.sock"
    assert out["worker"] == "w1"
    assert out["targetId"] == "T9"


def test_build_drive_request_pins_target_and_maps_fill():
    out = _run_bun(
        """
        import { buildDriveRequest } from "./omp/cloak.ts";
        const sidecar = {
          leaseId: "L1", targetId: "TAB", worker: "w", socket: "/s", cdp: "http://127.0.0.1:9", root: "/ops",
        };
        const nav = buildDriveRequest("navigate", sidecar, { action: "navigate", url: "https://ex.test" });
        const fill = buildDriveRequest("fill", sidecar, { action: "fill", selector: "#q", text: "hi" });
        const ping = buildDriveRequest("ping", sidecar, { action: "ping" });
        const sw = buildDriveRequest("switch_tab", sidecar, { action: "switch_tab", target_id: "OTHER" });
        console.log(JSON.stringify({ nav, fill, ping, sw }));
        """
    )
    extra = {"lease_id": "L1"}
    assert out["nav"] == {"action": "navigate", "target_id": "TAB", "url": "https://ex.test", **extra}
    assert out["fill"] == {"action": "fill_input", "target_id": "TAB", "text": "hi", "selector": "#q", **extra}
    assert out["ping"] == {"action": "ping", "target_id": "TAB", **extra}
    assert out["sw"]["dest_target_id"] == "OTHER"
    assert "steal" not in out["sw"]
    assert out["sw"]["target_id"] == "TAB"
    assert out["sw"]["lease_id"] == "L1"
    assert "held_lease_ids" not in out["nav"]
    assert "held_lease_ids" not in out["sw"]


def test_apply_tab_result_rewrites_sidecar():
    out = _run_bun(
        """
        import { applyTabResult } from "./omp/cloak.ts";
        const sidecar = {
          leaseId: "L1", targetId: "TAB", worker: "w", socket: "/s", cdp: "http://127.0.0.1:9", root: "/ops",
          held: { TAB: "L1" },
        };
        const minted = applyTabResult(sidecar, "new_tab", { target_id: "NEW", lease_id: "L2", mode: "drive" });
        const peek = applyTabResult(sidecar, "switch_tab", { target_id: "SIB", mode: "peek", ownership: "owned_by" });
        const closed = applyTabResult(minted, "close_tab", { closed: "NEW", released_lease: "L2" });
        console.log(JSON.stringify({ minted, peek, closed }));
        """
    )
    assert out["minted"]["targetId"] == "NEW"
    assert out["minted"]["leaseId"] == "L2"
    assert out["minted"]["held"] == {"TAB": "L1", "NEW": "L2"}
    assert out["peek"]["targetId"] == "TAB"
    assert out["peek"]["leaseId"] == "L1"
    assert out["closed"]["targetId"] == "TAB"
    assert out["closed"]["leaseId"] == "L1"
    assert "NEW" not in out["closed"]["held"]




def test_drive_without_bind_errors_no_cdp():
    out = _run_bun(
        """
        import cloakTool from "./omp/cloak.ts";
        const tool = cloakTool({ exec: async () => ({ stdout: "", stderr: "", code: 0 }) });
        const ctx = { sessionManager: { getSessionFile: () => null } };
        const result = await tool.execute("id", { action: "navigate", url: "https://ex.test" }, undefined, ctx);
        console.log(JSON.stringify({ text: result.content[0].text, isError: !!result.isError }));
        """
    )
    assert out["isError"] is True
    assert "without bind" in out["text"]
    assert "cdp" in out["text"].lower() or "CDP" in out["text"]


def test_bind_then_navigate_sends_json_line(tmp_path: Path):
    sock_path = tmp_path / "daemon.sock"
    received: list[dict] = []
    ready = threading.Event()

    def server():
        if sock_path.exists():
            sock_path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(1)
        ready.set()
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n", 1)[0]
            received.append(json.loads(line.decode()))
            conn.sendall(b'{"ok":true,"url":"https://ex.test"}\n')
        srv.close()

    t = threading.Thread(target=server, daemon=True)
    t.start()
    assert ready.wait(2)

    session = tmp_path / "session.jsonl"
    session.write_text("")
    sidecar = Path(str(session) + ".bind-profile.json")
    sidecar.write_text(
        json.dumps(
            {
                "leaseId": "L1",
                "targetId": "TAB-9",
                "worker": "scratch-demo",
                "socket": str(sock_path),
                "cdp": "http://127.0.0.1:9333",
                "root": str(ROOT),
            }
        )
    )

    script = f"""
        import cloakTool from "./omp/cloak.ts";
        const tool = cloakTool({{ exec: async () => ({{ stdout: "", stderr: "", code: 0 }}) }});
        const ctx = {{ sessionManager: {{ getSessionFile: () => {json.dumps(str(session))} }} }};
        const result = await tool.execute("id", {{ action: "navigate", url: "https://ex.test" }}, undefined, ctx);
        console.log(JSON.stringify({{ text: result.content[0].text, isError: !!result.isError, details: result.details }}));
    """
    out = _run_bun(script)
    t.join(2)
    assert out["isError"] is not True
    assert received[0]["action"] == "navigate"
    assert received[0]["target_id"] == "TAB-9"
    assert received[0]["url"] == "https://ex.test"
    assert received[0]["lease_id"] == "L1"
    assert "lease_id" not in (out.get("details") or {})
    assert "lease_id" not in out["text"]


def test_bind_reuses_alive_sidecar_without_launch(tmp_path: Path):
    session = tmp_path / "session.jsonl"
    session.write_text("")
    sock = tmp_path / "daemon.sock"
    sock.write_text("")
    sidecar = Path(str(session) + ".bind-profile.json")
    sidecar.write_text(
        json.dumps(
            {
                "leaseId": "L1",
                "targetId": "TAB-9",
                "worker": "scratch-demo",
                "socket": str(sock),
                "cdp": "http://127.0.0.1:9333",
                "root": str(ROOT),
            }
        )
    )
    script = """
        import { createBinder } from "./omp/bind-profile.ts";
        const calls = [];
        const exec = async (cmd, args) => {
          calls.push(args[0]);
          if (args[0] === "status") {
            return { stdout: JSON.stringify({ ok: true, status: "active", scope: "target", target_id: "TAB-9" }), stderr: "", code: 0 };
          }
          return { stdout: "", stderr: "should not launch", code: 1 };
        };
        const binder = createBinder(exec, { isAlive: async () => true });
        const ctx = { sessionManager: { getSessionFile: () => SESSION } };
        const result = await binder.bind(ctx, { scratch: true });
        console.log(JSON.stringify({ result, calls }));
    """.replace("SESSION", json.dumps(str(session)))
    out = _run_bun(script)
    assert out["result"]["ok"] is True
    assert out["result"]["reused"] is True
    assert out["result"]["state"]["targetId"] == "TAB-9"
    assert "launch" not in out["calls"]
    assert "status" in out["calls"]


def test_bind_does_not_reuse_expiring_target_lease(tmp_path: Path):
    session = tmp_path / "session.jsonl"
    session.write_text("")
    sock = tmp_path / "daemon.sock"
    sock.write_text("")
    sidecar = Path(str(session) + ".bind-profile.json")
    sidecar.write_text(
        json.dumps(
            {
                "leaseId": "L1",
                "targetId": "TAB-9",
                "worker": "scratch-demo",
                "socket": str(sock),
                "cdp": "http://127.0.0.1:9333",
                "root": str(ROOT),
            }
        )
    )
    launch = {
        "ok": True,
        "env": {
            "BROWSERCTL_LEASE_ID": "L2",
            "BROWSER_CDP_URL": "http://127.0.0.1:9444",
            "BROWSER_HARNESS_WORKER": "scratch-nav",
            "BROWSERCTL_TARGET_ID": "TAB-new",
        },
        "lease": {
            "lease_id": "L2",
            "worker_id": "scratch-nav",
            "target_id": "TAB-new",
            "resources": {"socket": "/ops/state/scratch-nav/daemon.sock"},
        },
    }
    script = """
        import { createBinder, readSidecar } from "./omp/bind-profile.ts";
        const calls = [];
        const exec = async (_cmd, args) => {
          calls.push(args[0]);
          if (args[0] === "status") {
            return { stdout: JSON.stringify({ ok: true, status: "expiring", scope: "target", target_id: "TAB-9" }), stderr: "", code: 0 };
          }
          if (args[0] === "release") {
            return { stdout: JSON.stringify({ ok: true }), stderr: "", code: 0 };
          }
          if (args[0] === "launch") {
            return { stdout: LAUNCH, stderr: "", code: 0 };
          }
          return { stdout: "", stderr: "unexpected", code: 1 };
        };
        const binder = createBinder(exec, { isAlive: async () => true });
        const ctx = { sessionManager: { getSessionFile: () => SESSION } };
        const result = await binder.bind(ctx, { scratch: true });
        const sc = readSidecar(ctx);
        console.log(JSON.stringify({ result, sidecar: sc, calls }));
    """.replace("SESSION", json.dumps(str(session))).replace("LAUNCH", json.dumps(json.dumps(launch)))
    out = _run_bun(script)
    assert out["result"]["ok"] is True
    assert out["result"]["reused"] is False
    assert out["sidecar"]["targetId"] == "TAB-new"
    assert "status" in out["calls"]
    assert "release" in out["calls"]
    assert "launch" in out["calls"]


def test_switch_owned_tab_sends_that_tab_lease():
    out = _run_bun(
        """
        import { buildDriveRequest } from "./omp/cloak.ts";
        const sidecar = {
          leaseId: "L1", targetId: "TAB", worker: "w", socket: "/s", cdp: "http://127.0.0.1:9", root: "/ops",
          held: { TAB: "L1", OTHER: "L2" },
        };
        const sw = buildDriveRequest("switch_tab", sidecar, { action: "switch_tab", target_id: "OTHER" });
        const peek = buildDriveRequest("switch_tab", sidecar, { action: "switch_tab", target_id: "SIB" });
        const close = buildDriveRequest("close_tab", sidecar, { action: "close_tab", target_id: "OTHER" });
        console.log(JSON.stringify({ sw, peek, close }));
        """
    )
    assert out["sw"]["lease_id"] == "L2"
    assert out["sw"]["target_id"] == "OTHER"
    assert out["sw"]["dest_target_id"] == "OTHER"
    assert "held_lease_ids" not in out["sw"]
    assert out["peek"]["lease_id"] == "L1"
    assert out["peek"]["dest_target_id"] == "SIB"
    assert out["close"]["lease_id"] == "L2"
    assert out["close"]["dest_target_id"] == "OTHER"


def test_remint_tabs_ownership_from_sidecar_map():
    out = _run_bun(
        """
        import { redactForModel, remintTabsOwnership } from "./omp/cloak.ts";
        const sidecar = {
          leaseId: "L1", targetId: "TAB", worker: "w", socket: "/s", cdp: "http://127.0.0.1:9", root: "/ops",
          held: { TAB: "L1", NEW: "L2" },
        };
        const raw = {
          tabs: [
            { targetId: "TAB", ownership: "owned_by_me" },
            { targetId: "NEW", ownership: "owned_by" },
            { targetId: "SIB", ownership: "owned_by" },
          ],
        };
        console.log(JSON.stringify(redactForModel(remintTabsOwnership(raw, sidecar))));
        """
    )
    by_id = {t["targetId"]: t["ownership"] for t in out["tabs"]}
    assert by_id == {"TAB": "owned_by_me", "NEW": "owned_by_me", "SIB": "owned_by"}


def test_bind_does_not_reuse_revoked_target_lease(tmp_path: Path):
    session = tmp_path / "session.jsonl"
    session.write_text("")
    sock = tmp_path / "daemon.sock"
    sock.write_text("")
    sidecar = Path(str(session) + ".bind-profile.json")
    sidecar.write_text(
        json.dumps(
            {
                "leaseId": "L1",
                "targetId": "TAB-9",
                "worker": "scratch-demo",
                "socket": str(sock),
                "cdp": "http://127.0.0.1:9333",
                "root": str(ROOT),
            }
        )
    )
    launch = {
        "ok": True,
        "env": {
            "BROWSERCTL_LEASE_ID": "L2",
            "BROWSER_CDP_URL": "http://127.0.0.1:9444",
            "BROWSER_HARNESS_WORKER": "scratch-nav",
            "BROWSERCTL_TARGET_ID": "TAB-new",
        },
        "lease": {
            "lease_id": "L2",
            "worker_id": "scratch-nav",
            "target_id": "TAB-new",
            "resources": {"socket": "/ops/state/scratch-nav/daemon.sock"},
        },
    }
    script = """
        import { createBinder, readSidecar } from "./omp/bind-profile.ts";
        const calls = [];
        const exec = async (_cmd, args) => {
          calls.push(args[0]);
          if (args[0] === "status") {
            return { stdout: JSON.stringify({ ok: false, error: { code: "LEASE_NOT_FOUND" } }), stderr: "", code: 2 };
          }
          if (args[0] === "release") {
            return { stdout: JSON.stringify({ ok: true }), stderr: "", code: 0 };
          }
          if (args[0] === "launch") {
            return { stdout: LAUNCH, stderr: "", code: 0 };
          }
          return { stdout: "", stderr: "unexpected", code: 1 };
        };
        const binder = createBinder(exec, { isAlive: async () => true });
        const ctx = { sessionManager: { getSessionFile: () => SESSION } };
        const result = await binder.bind(ctx, { scratch: true });
        const sc = readSidecar(ctx);
        console.log(JSON.stringify({ result, sidecar: sc, calls }));
    """.replace("SESSION", json.dumps(str(session))).replace("LAUNCH", json.dumps(json.dumps(launch)))
    out = _run_bun(script)
    assert out["result"]["ok"] is True
    assert out["result"]["reused"] is False
    assert out["sidecar"]["targetId"] == "TAB-new"
    assert "status" in out["calls"]
    assert "release" in out["calls"]
    assert "launch" in out["calls"]


def test_stale_bind_releases_every_held_lease(tmp_path: Path):
    session = tmp_path / "session.jsonl"
    session.write_text("")
    sock = tmp_path / "daemon.sock"
    sock.write_text("")
    sidecar = Path(str(session) + ".bind-profile.json")
    sidecar.write_text(
        json.dumps(
            {
                "leaseId": "LA",
                "targetId": "A",
                "worker": "scratch-demo",
                "socket": str(sock),
                "cdp": "http://127.0.0.1:9333",
                "root": str(ROOT),
                "held": {"A": "LA", "B": "LB", "C": "LC"},
            }
        )
    )
    launch = {
        "ok": True,
        "env": {
            "BROWSERCTL_LEASE_ID": "L2",
            "BROWSER_CDP_URL": "http://127.0.0.1:9444",
            "BROWSER_HARNESS_WORKER": "scratch-nav",
            "BROWSERCTL_TARGET_ID": "TAB-new",
        },
        "lease": {
            "lease_id": "L2",
            "worker_id": "scratch-nav",
            "target_id": "TAB-new",
            "resources": {"socket": "/ops/state/scratch-nav/daemon.sock"},
        },
    }
    script = """
        import { createBinder, readSidecar } from "./omp/bind-profile.ts";
        const released = [];
        const exec = async (_cmd, args) => {
          if (args[0] === "status") {
            return { stdout: JSON.stringify({ ok: false, error: { code: "LEASE_NOT_FOUND" } }), stderr: "", code: 2 };
          }
          if (args[0] === "release") {
            const i = args.indexOf("--lease");
            released.push(args[i + 1]);
            return { stdout: JSON.stringify({ ok: true }), stderr: "", code: 0 };
          }
          if (args[0] === "launch") {
            return { stdout: LAUNCH, stderr: "", code: 0 };
          }
          return { stdout: "", stderr: "unexpected", code: 1 };
        };
        const binder = createBinder(exec, { isAlive: async () => true });
        const ctx = { sessionManager: { getSessionFile: () => SESSION } };
        const result = await binder.bind(ctx, { scratch: true });
        const sc = readSidecar(ctx);
        console.log(JSON.stringify({ result, sidecar: sc, released }));
    """.replace("SESSION", json.dumps(str(session))).replace("LAUNCH", json.dumps(json.dumps(launch)))
    out = _run_bun(script)
    assert out["result"]["ok"] is True
    assert out["result"]["reused"] is False
    assert set(out["released"]) == {"LA", "LB", "LC"}
    assert out["sidecar"]["held"] == {"TAB-new": "L2"}


def test_target_lease_required_releases_every_held_lease(tmp_path: Path):
    sock_path = tmp_path / "daemon.sock"
    received: list[dict] = []
    ready = threading.Event()

    def server():
        if sock_path.exists():
            sock_path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(1)
        ready.set()
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n", 1)[0]
            received.append(json.loads(line.decode()))
            conn.sendall(b'{"code":"TARGET_LEASE_REQUIRED","message":"stored target lease is missing, inactive, wrong worker, or not target-scoped"}\n')
        srv.close()

    t = threading.Thread(target=server, daemon=True)
    t.start()
    assert ready.wait(2)

    session = tmp_path / "session.jsonl"
    session.write_text("")
    sidecar = Path(str(session) + ".bind-profile.json")
    sidecar.write_text(
        json.dumps(
            {
                "leaseId": "LA",
                "targetId": "A",
                "worker": "scratch-demo",
                "socket": str(sock_path),
                "cdp": "http://127.0.0.1:9333",
                "root": str(ROOT),
                "held": {"A": "LA", "B": "LB"},
            }
        )
    )

    script = f"""
        import cloakTool from "./omp/cloak.ts";
        import {{ readSidecar }} from "./omp/bind-profile.ts";
        const released = [];
        const exec = async (_cmd, args) => {{
          if (args[0] === "release") {{
            const i = args.indexOf("--lease");
            released.push(args[i + 1]);
            return {{ stdout: JSON.stringify({{ ok: true }}), stderr: "", code: 0 }};
          }}
          return {{ stdout: "", stderr: "unexpected", code: 1 }};
        }};
        const tool = cloakTool({{ exec }});
        const ctx = {{ sessionManager: {{ getSessionFile: () => {json.dumps(str(session))} }} }};
        const result = await tool.execute("id", {{ action: "navigate", url: "https://ex.test" }}, undefined, ctx);
        const sc = readSidecar(ctx);
        console.log(JSON.stringify({{ text: result.content[0].text, isError: !!result.isError, released, sidecar: sc ?? null }}));
    """
    out = _run_bun(script)
    t.join(2)
    assert out["isError"] is True
    assert "TARGET_LEASE_REQUIRED" in out["text"]
    assert set(out["released"]) == {"LA", "LB"}
    assert out["sidecar"] is None
    assert received[0]["action"] == "navigate"


def test_redact_for_model_strips_lease_tokens():
    out = _run_bun(
        """
        import { redactForModel } from "./omp/cloak.ts";
        const raw = {
          target_id: "T",
          ownership: "owned_by_me",
          lease_id: "SECRET",
          lease: { lease_id: "SECRET", browser_lease_id: "B" },
          held_lease_ids: ["SECRET"],
          page: { url: "https://ex.test", lease_id: "NESTED" },
        };
        console.log(JSON.stringify(redactForModel(raw)));
        """
    )
    assert out == {"target_id": "T", "ownership": "owned_by_me", "page": {"url": "https://ex.test"}}


def test_bind_writes_sidecar_socket_worker_target(tmp_path: Path):
    session = tmp_path / "session.jsonl"
    session.write_text("")
    payload = {
        "ok": True,
        "env": {
            "BROWSERCTL_LEASE_ID": "L2",
            "BROWSER_CDP_URL": "http://127.0.0.1:9444",
            "BROWSER_HARNESS_WORKER": "scratch-nav",
            "BROWSERCTL_TARGET_ID": "TAB-new",
        },
        "lease": {
            "lease_id": "L2",
            "worker_id": "scratch-nav",
            "target_id": "TAB-new",
            "resources": {"socket": "/ops/state/scratch-nav/daemon.sock"},
        },
    }
    out = _run_bun(
        f"""
        import {{ createBinder, readSidecar }} from "./omp/bind-profile.ts";
        const exec = async (_cmd, args) => {{
          if (args[0] === "launch") {{
            return {{ stdout: {json.dumps(json.dumps(payload))}, stderr: "", code: 0 }};
          }}
          return {{ stdout: "", stderr: "unexpected " + args.join(" "), code: 1 }};
        }};
        const binder = createBinder(exec, {{ isAlive: async () => false }});
        const ctx = {{ sessionManager: {{ getSessionFile: () => {json.dumps(str(session))} }} }};
        const result = await binder.bind(ctx, {{ scratch: true }});
        const sidecar = readSidecar(ctx);
        console.log(JSON.stringify({{ result, sidecar }}));
        """
    )
    assert out["result"]["ok"] is True
    assert out["result"]["reused"] is False
    sidecar = out["sidecar"]
    assert sidecar["leaseId"] == "L2"
    assert sidecar["worker"] == "scratch-nav"
    assert sidecar["targetId"] == "TAB-new"
    assert sidecar["socket"] == "/ops/state/scratch-nav/daemon.sock"
    assert sidecar["cdp"] == "http://127.0.0.1:9444"
    assert sidecar["held"] == {"TAB-new": "L2"}

