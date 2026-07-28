"""
Active-target publication for browser harness ↔ observe_mirror.

Atomic JSON at ``state/<worker>/control/active-target.json`` (default).
Schema is the shared contract with herdr-browser observe_mirror mode:

  HERDR_BROWSER_MODE=observe_mirror
  HERDR_BROWSER_TARGET_STATE=<path>
  HERDR_BROWSER_CDP_URL=http://127.0.0.1:<port>

No secrets. Callers may attach optional page metadata (url/title only).

Seq increments are serialized with a process/thread lock dir so concurrent
publishers cannot lose monotonicity via TOCTOU on read-increment-write.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Filename under state/<worker>/control/
ACTIVE_TARGET_NAME = "active-target.json"

# Thread-local coordination for same-process publishers (mkdir is process-level)
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def control_dir(state_root: Path | str, worker_id: str) -> Path:
    return Path(state_root) / worker_id / "control"


def default_target_path(state_root: Path | str, worker_id: str) -> Path:
    return control_dir(state_root, worker_id) / ACTIVE_TARGET_NAME


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve()) if path.parent.exists() else str(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _process_lock_dir(path: Path) -> Path:
    return path.parent / f".{path.name}.lock"


def _acquire_process_lock(lock_dir: Path, *, timeout: float = 5.0) -> None:
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            os.mkdir(lock_dir)
            return
        except FileExistsError:
            try:
                age = time.time() - lock_dir.stat().st_mtime
            except OSError:
                age = 0.0
            if age > 15.0:
                try:
                    os.rmdir(lock_dir)
                    continue
                except OSError:
                    pass
            if time.time() >= deadline:
                raise TimeoutError(f"target-state lock timeout: {lock_dir}")
            time.sleep(0.01)


def _release_process_lock(lock_dir: Path) -> None:
    try:
        os.rmdir(lock_dir)
    except OSError:
        pass


def atomic_write_json(path: Path | str, payload: dict[str, Any]) -> Path:
    """Write JSON atomically (temp in same dir + replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def read_json(path: Path | str) -> dict[str, Any] | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build_target_state(
    *,
    worker_id: str,
    active_target_id: str | None,
    cdp_url: str | None = None,
    browser_generation: str | int | None = None,
    seq: int = 1,
    page: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a target-state document (no I/O)."""
    doc: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "worker_id": worker_id,
        "active_target_id": active_target_id,
        "seq": int(seq),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if cdp_url:
        doc["cdp_url"] = str(cdp_url).rstrip("/")
    if browser_generation is not None:
        doc["browser_generation"] = browser_generation
    if page:
        clean: dict[str, Any] = {}
        for key in ("url", "title", "target_id"):
            if key in page and page[key] is not None:
                clean[key] = page[key]
        if clean:
            doc["page"] = clean
    if extra:
        for k, v in extra.items():
            if k in doc:
                continue
            doc[k] = v
    return doc


def publish_active_target(
    path: Path | str,
    *,
    worker_id: str,
    active_target_id: str | None,
    cdp_url: str | None = None,
    browser_generation: str | int | None = None,
    seq: int | None = None,
    page: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Atomically publish active-target state.

    If *seq* is None, read existing file and increment (or start at 1) under
    a process + thread lock so concurrent publishers stay monotonic.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_dir = _process_lock_dir(path)
    tlock = _thread_lock_for(path)
    with tlock:
        _acquire_process_lock(lock_dir)
        try:
            if seq is None:
                prev = read_json(path) or {}
                try:
                    seq = int(prev.get("seq") or 0) + 1
                except (TypeError, ValueError):
                    seq = 1
                if browser_generation is None and "browser_generation" in prev:
                    browser_generation = prev.get("browser_generation")
                if cdp_url is None and prev.get("cdp_url"):
                    cdp_url = prev.get("cdp_url")

            doc = build_target_state(
                worker_id=worker_id,
                active_target_id=active_target_id,
                cdp_url=cdp_url,
                browser_generation=browser_generation,
                seq=int(seq),
                page=page,
                extra=extra,
            )
            atomic_write_json(path, doc)
            return doc
        finally:
            _release_process_lock(lock_dir)


def clear_active_target(path: Path | str, *, worker_id: str | None = None) -> None:
    """Mark target cleared (seq bump, null active_target_id) under the same lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_dir = _process_lock_dir(path)
    tlock = _thread_lock_for(path)
    with tlock:
        _acquire_process_lock(lock_dir)
        try:
            prev = read_json(path) or {}
            wid = worker_id or prev.get("worker_id") or "unknown"
            try:
                seq = int(prev.get("seq") or 0) + 1
            except (TypeError, ValueError):
                seq = 1
            doc = build_target_state(
                worker_id=str(wid),
                active_target_id=None,
                cdp_url=prev.get("cdp_url"),
                browser_generation=prev.get("browser_generation"),
                seq=seq,
            )
            atomic_write_json(path, doc)
        finally:
            _release_process_lock(lock_dir)
