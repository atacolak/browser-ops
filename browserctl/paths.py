"""Path layout for browserctl control plane."""

from __future__ import annotations

import os
from pathlib import Path

# browser-ops root (parent of browserctl package)
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_STATE_ROOT = ROOT / "state"
CONTROL_ROOT_NAME = "control"
LEASES_DIR_NAME = "leases"
INDEX_NAME = "index.json"
WORKER_LOCK_NAME = "worker.lock"
TARGET_REGISTRY_NAME = "targets.json"


def resolve_root(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    env = os.environ.get("BROWSER_OPS_ROOT", "").strip()
    if env:
        return Path(env).resolve()
    return ROOT


def resolve_state_root(
    state_root: Path | str | None = None,
    *,
    root: Path | str | None = None,
) -> Path:
    if state_root is not None:
        return Path(state_root).resolve()
    env = os.environ.get("BROWSERCTL_STATE_ROOT", "").strip()
    if env:
        return Path(env).resolve()
    return resolve_root(root) / "state"


def control_root(state_root: Path | str) -> Path:
    return Path(state_root) / CONTROL_ROOT_NAME


def leases_dir(state_root: Path | str) -> Path:
    return control_root(state_root) / LEASES_DIR_NAME


def lease_path(state_root: Path | str, lease_id: str) -> Path:
    return leases_dir(state_root) / f"{lease_id}.json"


def index_path(state_root: Path | str) -> Path:
    return control_root(state_root) / INDEX_NAME


def worker_lock_path(state_root: Path | str, worker_id: str) -> Path:
    return control_root(state_root) / "workers" / worker_id / WORKER_LOCK_NAME


def worker_control_dir(state_root: Path | str, worker_id: str) -> Path:
    """Per-worker control dir (also hosts active-target.json via daemon)."""
    return Path(state_root) / worker_id / "control"


def active_target_path(state_root: Path | str, worker_id: str) -> Path:
    return worker_control_dir(state_root, worker_id) / "active-target.json"


def target_registry_path(state_root: Path | str, worker_id: str) -> Path:
    return worker_control_dir(state_root, worker_id) / TARGET_REGISTRY_NAME


def scratch_profiles_root(root: Path | str | None = None) -> Path:
    return resolve_root(root) / "profiles" / "scratch"


def profiles_dir(root: Path | str | None = None) -> Path:
    return resolve_root(root) / "profiles"


def profiles_registry_path(root: Path | str | None = None) -> Path:
    """Named profile → launch selector registry (no secrets)."""
    return profiles_dir(root) / "PROFILES.json"
