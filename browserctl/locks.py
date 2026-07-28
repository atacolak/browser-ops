"""Directory-based exclusive locks for control-plane mutations."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from browserctl.errors import LeaseConflict
from browserctl.paths import control_root, worker_lock_path


def _now() -> float:
    return time.time()


@contextmanager
def mkdir_lock(
    lock_dir: Path,
    *,
    timeout: float = 5.0,
    stale_after: float = 30.0,
    conflict_worker: str | None = None,
) -> Iterator[None]:
    """Exclusive lock via atomic ``mkdir`` (works across processes)."""
    lock_dir = Path(lock_dir)
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    deadline = _now() + timeout
    while True:
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            try:
                age = _now() - lock_dir.stat().st_mtime
            except OSError:
                age = 0.0
            if age > stale_after:
                try:
                    os.rmdir(lock_dir)
                    continue
                except OSError:
                    pass
            if _now() >= deadline:
                raise LeaseConflict(
                    conflict_worker or lock_dir.name,
                    {"reason": "mutex_timeout", "lock": str(lock_dir)},
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.rmdir(lock_dir)
        except OSError:
            pass


@contextmanager
def worker_mutex(
    state_root: Path | str,
    worker_id: str,
    *,
    timeout: float = 5.0,
) -> Iterator[None]:
    """Exclusive lock for lease mutations on one worker."""
    with mkdir_lock(
        worker_lock_path(state_root, worker_id),
        timeout=timeout,
        conflict_worker=worker_id,
    ):
        yield


@contextmanager
def index_mutex(state_root: Path | str, *, timeout: float = 5.0) -> Iterator[None]:
    """Serialize global lease index RMW."""
    lock = control_root(state_root) / "index.lock"
    with mkdir_lock(lock, timeout=timeout, conflict_worker="index"):
        yield


@contextmanager
def port_alloc_mutex(state_root: Path | str, *, timeout: float = 30.0) -> Iterator[None]:
    """Global lock around scratch CDP port select + daemon bind."""
    lock = control_root(state_root) / "ports.lock"
    with mkdir_lock(lock, timeout=timeout, stale_after=60.0, conflict_worker="ports"):
        yield


@contextmanager
def target_state_mutex(path: Path | str, *, timeout: float = 5.0) -> Iterator[None]:
    """Serialize active-target seq read-increment-write (process + thread)."""
    path = Path(path)
    lock = path.parent / f".{path.name}.lock"
    with mkdir_lock(lock, timeout=timeout, stale_after=15.0, conflict_worker="target-state"):
        yield
