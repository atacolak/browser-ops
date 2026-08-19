"""Active-target publication contract tests."""

from __future__ import annotations

import json
import multiprocessing
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from daemon.target_state import (  # noqa: E402
    build_target_state,
    clear_active_target,
    default_target_path,
    publish_active_target,
    read_json,
)


def test_build_target_state_schema():
    doc = build_target_state(
        worker_id="scratch-demo",
        active_target_id="T1",
        cdp_url="http://127.0.0.1:9333/",
        browser_generation="g1",
        seq=3,
        page={"url": "https://example.com", "title": "Ex", "secret": "nope"},
    )
    assert doc["version"] == 1
    assert doc["worker_id"] == "scratch-demo"
    assert doc["active_target_id"] == "T1"
    assert doc["cdp_url"] == "http://127.0.0.1:9333"
    assert doc["seq"] == 3
    assert doc["page"]["url"] == "https://example.com"
    assert "secret" not in doc["page"]


def test_publish_atomic_and_seq(tmp_path: Path):
    path = tmp_path / "active-target.json"
    d1 = publish_active_target(
        path,
        worker_id="w1",
        active_target_id="A",
        cdp_url="http://127.0.0.1:1",
    )
    assert d1["seq"] == 1
    d2 = publish_active_target(
        path,
        worker_id="w1",
        active_target_id="B",
    )
    assert d2["seq"] == 2
    assert d2["active_target_id"] == "B"
    assert d2["cdp_url"] == "http://127.0.0.1:1"
    on_disk = json.loads(path.read_text())
    assert on_disk["seq"] == 2


def test_clear_active_target(tmp_path: Path):
    path = tmp_path / "active-target.json"
    publish_active_target(path, worker_id="w1", active_target_id="A", seq=4)
    clear_active_target(path, worker_id="w1")
    doc = read_json(path)
    assert doc["active_target_id"] is None
    assert doc["seq"] == 5


def test_concurrent_publish_monotonic_threads(tmp_path: Path):
    """Thread concurrency: final seq must equal total successful publishes."""
    path = tmp_path / "active-target.json"
    n_threads = 8
    per_thread = 25
    errors: list[BaseException] = []
    barrier = threading.Barrier(n_threads)

    def worker(_i: int):
        try:
            barrier.wait(timeout=5)
            for n in range(per_thread):
                publish_active_target(
                    path,
                    worker_id="w",
                    active_target_id=f"T{_i}-{n}",
                    seq=None,
                )
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    doc = read_json(path)
    assert doc is not None
    assert doc["worker_id"] == "w"
    # With lock, every publish increments → exact total
    assert doc["seq"] == n_threads * per_thread


def _mp_publisher(path_str: str, n: int, q: multiprocessing.Queue) -> None:
    try:
        path = Path(path_str)
        for i in range(n):
            publish_active_target(
                path,
                worker_id="w",
                active_target_id=f"P-{i}",
                seq=None,
            )
        q.put(("ok", n))
    except BaseException as e:  # noqa: BLE001
        q.put(("err", str(e)))


def test_concurrent_publish_monotonic_processes(tmp_path: Path):
    """Cross-process lock: final seq equals total publishes (no lost increments)."""
    path = tmp_path / "active-target.json"
    n_procs = 4
    per = 15
    q: multiprocessing.Queue = multiprocessing.Queue()
    procs = [
        multiprocessing.Process(target=_mp_publisher, args=(str(path), per, q))
        for _ in range(n_procs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0
    results = [q.get(timeout=2) for _ in range(n_procs)]
    assert all(r[0] == "ok" for r in results)
    doc = read_json(path)
    assert doc is not None
    assert doc["seq"] == n_procs * per


def test_default_path_layout(tmp_path: Path):
    p = default_target_path(tmp_path, "scratch-demo")
    assert p == tmp_path / "scratch-demo" / "control" / "active-target.json"
