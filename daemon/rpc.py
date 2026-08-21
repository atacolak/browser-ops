"""
rpc — ND-JSON Unix socket IPC server with request routing and error envelopes.

Every command comes as one JSON line on a Unix socket (or TCP).  The server
routes ``action`` keys to the appropriate backend method and wraps all errors
in the standard envelope.

Error envelope (always JSON, never bare strings)::

    {
      "code": "ELEMENT_NOT_FOUND",
      "message": "human-readable detail",
      "retryable": true,
      "screenshot": "state/worker-b/shots/8f3a.png",
      "page": {"url": "…", "title": "…"}
    }
"""

import asyncio
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from backend.base import BrowserBackend
from events import EventBus


# ── Error types ─────────────────────────────────────────────────────────────

class DaemonError(Exception):
    """Base error with structured envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        screenshot: str | None = None,
        page: dict | None = None,
    ):
        self.code = code
        self.message = message
        self.retryable = retryable
        self.screenshot = screenshot
        self.page = page or {}
        super().__init__(message)

    def to_envelope(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "screenshot": self.screenshot,
            "page": self.page,
        }


class UnknownActionError(DaemonError):
    def __init__(self, action: str):
        super().__init__(
            code="UNKNOWN_ACTION",
            message=f"unknown action: {action}",
        )


class CDPDisconnectedError(DaemonError):
    def __init__(self, detail: str = ""):
        super().__init__(
            code="CDP_DISCONNECTED",
            message=f"CDP connection lost: {detail}",
            retryable=True,
        )


class NavTimeoutError(DaemonError):
    def __init__(self, url: str = ""):
        super().__init__(
            code="NAV_TIMEOUT",
            message=f"Navigation timed out: {url}",
            retryable=True,
        )


class ElementNotFoundError(DaemonError):
    def __init__(self, selector: str):
        super().__init__(
            code="ELEMENT_NOT_FOUND",
            message=f"Element not found: {selector}",
            retryable=True,
        )


# ── Param extractors ────────────────────────────────────────────────────────
# Each maps the flat command dict from IPC into typed kwargs for the backend.

def _no_params(cmd: dict) -> dict:
    return {}


def _str_param(key: str, default: str | None = None):
    def extract(cmd: dict) -> dict:
        val = cmd.get(key, default)
        if val is None:
            raise DaemonError("MISSING_PARAM", f"missing required param: {key}")
        return {key: val}
    return extract


def _optional_str(key: str):
    def extract(cmd: dict) -> dict:
        v = cmd.get(key)
        return {key: v} if v is not None else {}
    return extract


def _int_param(key: str, default: int | None = None):
    def extract(cmd: dict) -> dict:
        val = cmd.get(key, default)
        if val is None:
            raise DaemonError("MISSING_PARAM", f"missing required param: {key}")
        return {key: int(val)}
    return extract


def _float_param(key: str, default: float | None = None):
    def extract(cmd: dict) -> dict:
        val = cmd.get(key, default)
        if val is None:
            raise DaemonError("MISSING_PARAM", f"missing required param: {key}")
        return {key: float(val)}
    return extract


def _click_params(cmd: dict) -> dict:
    x = cmd.get("x")
    y = cmd.get("y")
    if x is None or y is None:
        raise DaemonError("MISSING_PARAM", "click requires x and y")
    return {
        "x": int(x),
        "y": int(y),
        "button": cmd.get("button", "left"),
        "clicks": int(cmd.get("clicks", 1)),
    }


def _scroll_params(cmd: dict) -> dict:
    return {
        "x": int(cmd.get("x", 500)),
        "y": int(cmd.get("y", 500)),
        "dy": int(cmd.get("dy", -300)),
    }


def _screenshot_params(cmd: dict) -> dict:
    return {"path": cmd.get("path")}


def _http_get_params(cmd: dict) -> dict:
    url = cmd.get("url")
    if url is None:
        raise DaemonError("MISSING_PARAM", "http_get requires url")
    return {"url": url, "headers": cmd.get("headers")}


def _new_tab_params(cmd: dict) -> dict:
    return {"url": cmd.get("url", "about:blank")}


def _switch_tab_params(cmd: dict) -> dict:
    tid = cmd.get("target_id")
    if tid is None:
        raise DaemonError("MISSING_PARAM", "switch_tab requires target_id")
    return {"target_id": tid}


def _close_tab_params(cmd: dict) -> dict:
    return {"target_id": cmd.get("target_id")}


def _dialog_params(cmd: dict) -> dict:
    return {
        "accept": bool(cmd.get("accept", True)),
        "prompt_text": cmd.get("prompt_text", ""),
    }


def _fill_input_params(cmd: dict) -> dict:
    sel = cmd.get("selector")
    txt = cmd.get("text")
    if sel is None or txt is None:
        raise DaemonError("MISSING_PARAM", "fill_input requires selector and text")
    return {"selector": sel, "text": txt}


def _wait_for_load_params(cmd: dict) -> dict:
    return {"timeout": float(cmd.get("timeout", 15.0))}


def _wait_for_element_params(cmd: dict) -> dict:
    return {
        "selector": cmd["selector"],
        "timeout": float(cmd.get("timeout", 10.0)),
    }


def _type_params(cmd: dict) -> dict:
    text = cmd.get("text")
    if text is None:
        raise DaemonError("MISSING_PARAM", "type requires text")
    return {"text": text}


def _press_params(cmd: dict) -> dict:
    key = cmd.get("key")
    if key is None:
        raise DaemonError("MISSING_PARAM", "press requires key")
    return {"key": key}


def _extract_params(cmd: dict) -> dict:
    sel = cmd.get("selector")
    if sel is None:
        raise DaemonError("MISSING_PARAM", "extract requires selector")
    return {"selector": sel, "attribute": cmd.get("attribute")}


def _evaluate_params(cmd: dict) -> dict:
    expr = cmd.get("expression")
    if expr is None:
        raise DaemonError("MISSING_PARAM", "evaluate requires expression")
    return {"expression": expr}


def _upload_params(cmd: dict) -> dict:
    sel = cmd.get("selector")
    fp = cmd.get("file_path")
    if sel is None or fp is None:
        raise DaemonError("MISSING_PARAM", "upload_file requires selector and file_path")
    return {"selector": sel, "file_path": fp}


def _procedure_params(cmd: dict) -> dict:
    return {
        "steps": cmd.get("steps", []),
        "params": cmd.get("params"),
    }


def _report_outcome_params(cmd: dict) -> dict:
    return {
        "skill_id": cmd.get("skill_id", "unknown"),
        "success": bool(cmd.get("success", True)),
        "error": cmd.get("error"),
        "details": cmd.get("details"),
    }


# ── Build the action-to-backend method mapping ─────────────────────────────

# (action, backend_method_name, param_extractor)
_ROUTES: list[tuple[str, str, Callable[[dict], dict]]] = [
    ("ping", "ping", lambda _: {}),
    ("page_info", "page_info", _no_params),
    ("navigate", "navigate", _str_param("url")),
    ("screenshot", "screenshot", _screenshot_params),
    ("screenshot_base64", "screenshot_base64", _no_params),
    ("click", "click", _click_params),
    ("type", "type_text", _type_params),
    ("press", "press_key", _press_params),
    ("scroll", "scroll", _scroll_params),
    ("evaluate", "evaluate", _evaluate_params),
    ("extract", "extract", _extract_params),
    ("fill_input", "fill_input", _fill_input_params),
    ("tabs", "list_tabs", _no_params),
    ("list_tabs", "list_tabs", _no_params),
    ("new_tab", "new_tab", _new_tab_params),
    ("switch_tab", "switch_tab", _switch_tab_params),
    ("close_tab", "close_tab", _close_tab_params),
    ("dialog", "dialog_status", _no_params),
    ("dialog_dismiss", "handle_dialog", _dialog_params),
    ("wait_for_load", "wait_for_load", _wait_for_load_params),
    ("wait_for_element", "wait_for_element", _wait_for_element_params),
    ("http_get", "http_get", _http_get_params),
    ("upload_file", "upload_file", _upload_params),
    ("drain_events", "drain_events", _no_params),
    ("run_procedure", "run_procedure", _procedure_params),
    ("report_outcome", "report_outcome", _report_outcome_params),
    ("health", "health", _no_params),
]

# Build lookup
_ROUTE_MAP: dict[str, tuple[str, Callable[[dict], dict]]] = {
    action: (method_name, extractor)
    for action, method_name, extractor in _ROUTES
}

# Actions that the existing client may send (backward compat aliases):
_ACTION_ALIASES = {
    "tabs": "list_tabs",
}

# ── Client handler ─────────────────────────────────────────────────────────

async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    backend: BrowserBackend,
    event_bus: EventBus | None = None,
) -> None:
    """Handle one JSON-line command from an agent and send the response."""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=30.0)
        if not line:
            return
        cmd = json.loads(line.decode("utf-8"))
    except asyncio.TimeoutError:
        writer.write(b'{"code":"TIMEOUT","message":"read timeout","retryable":true}\n')
        await writer.drain()
        writer.close()
        return
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        writer.write(
            json.dumps({
                "code": "INVALID_JSON",
                "message": f"Invalid JSON on socket: {e}",
                "retryable": False,
            }).encode() + b"\n"
        )
        await writer.drain()
        writer.close()
        return

    action = cmd.get("action", "")
    start_ns = time.monotonic_ns()

    try:
        result = await _execute_action(backend, action, cmd)
        duration_ms = (time.monotonic_ns() - start_ns) / 1_000_000

        # Emit event (but not for drain_events or ping to avoid loops)
        if event_bus and action not in ("drain_events", "ping"):
            result_summary = _summarize_result(result, action)
            # Fire-and-forget: don't block the response on event I/O
            asyncio.ensure_future(
                event_bus.emit(action, cmd, result_summary, duration_ms)
            )

        writer.write((json.dumps(result) + "\n").encode())
        await writer.drain()

    except DaemonError as e:
        duration_ms = (time.monotonic_ns() - start_ns) / 1_000_000
        envelope = e.to_envelope()
        if event_bus and action not in ("drain_events", "ping"):
            asyncio.ensure_future(
                event_bus.emit(action, cmd, f"error: {e.code}", duration_ms)
            )
        writer.write((json.dumps(envelope) + "\n").encode())
        await writer.drain()

    except Exception as e:
        # Unexpected error — wrap in generic envelope
        traceback.print_exc(file=sys.stderr)
        envelope = {
            "code": "INTERNAL_ERROR",
            "message": str(e),
            "retryable": True,
        }
        try:
            writer.write((json.dumps(envelope) + "\n").encode())
            await writer.drain()
        except Exception:
            pass

    finally:
        writer.close()


def _summarize_result(result: dict, action: str) -> str:
    """Produce a short human-readable summary of the result."""
    if "error" in result:
        return f"error: {result['error']}"
    if action == "navigate":
        return "navigated"
    if action in ("screenshot", "screenshot_base64"):
        return "captured" if "path" in result else f"size={result.get('size', '?')}"
    if action == "page_info":
        return f"url={result.get('url', '?')[:60]}"
    if action == "click":
        return f"({result.get('x', '?')},{result.get('y', '?')})"
    if action == "type":
        return f"len={result.get('length', '?')}"
    if action == "press":
        return result.get("pressed", "?")
    if action in ("wait_for_load", "wait_for_element"):
        return "ready" if result.get("ready") or result.get("found") else "timeout"
    if action == "evaluate":
        val = result.get("value", "")
        return f"type={type(val).__name__}"
    if action == "extract":
        return f"found={result.get('value') is not None}"
    if action == "fill_input":
        return f"selector={result.get('selector', '?')}"
    if action == "http_get":
        return f"status={result.get('status', 'error')}"
    if action == "drain_events":
        return f"count={len(result.get('events', []))}"
    if action == "run_procedure":
        return f"exec={result.get('steps_executed', '?')}/{result.get('steps_total', '?')}"
    if action == "report_outcome":
        return f"skill={result.get('skill_id', '?')} success={result.get('success', '?')}"
    if action == "start_async":
        return f"handle={result.get('handle_id', '?')}"
    if action == "poll_async":
        return f"handle={result.get('handle_id', '?')} status={result.get('status', '?')}"
    if action == "list_asyncs":
        jobs = result.get('jobs', [])
        return f"count={len(jobs)}"
    if action == "read_skill_health":
        return f"records={len(result.get('records', []))} degraded={len(result.get('degraded', []))}"
    if action == "capture_debug_context":
        url = result.get('page_url', '?') or '?'
        return f"url={url[:60]} candidates={len(result.get('candidate_selectors', []))}"
    if action == "run_validator":
        c = result.get('complete', '?')
        conf = result.get('confidence', '?')
        return f"complete={c} confidence={conf}"
    if action == "extract_page_text":
        text = result.get('page_text', '')
        return f"len={len(text)}"
    return "ok"



async def _gate_evaluate(backend: BrowserBackend) -> None:
    """Fail-closed gate for unrestricted JS evaluate.

    Requires ``BROWSER_ALLOW_EVALUATE=1``.  When ``BROWSER_EVALUATE_ALLOWLIST``
    is set (comma-separated URL prefixes), evaluate is further restricted to
    pages whose URL starts with one of those prefixes.
    """
    if os.environ.get("BROWSER_ALLOW_EVALUATE") != "1":
        raise DaemonError(
            "EVALUATE_DISABLED",
            "evaluate is disabled. Set BROWSER_ALLOW_EVALUATE=1 to enable.",
            retryable=False,
        )

    allowlist_raw = os.environ.get("BROWSER_EVALUATE_ALLOWLIST", "").strip()
    if not allowlist_raw:
        return  # global evaluate allowed when only BROWSER_ALLOW_EVALUATE is set

    prefixes = [p.strip() for p in allowlist_raw.split(",") if p.strip()]
    if not prefixes:
        return

    try:
        page = await backend.page_info()
        url = page.get("url") or page.get("page", {}).get("url") or ""
    except Exception as e:
        raise DaemonError(
            "EVALUATE_URL_CHECK_FAILED",
            f"could not determine page URL for evaluate allowlist check: {e}",
            retryable=True,
        )

    if not any(url.startswith(prefix) for prefix in prefixes):
        raise DaemonError(
            "EVALUATE_DOMAIN_DENIED",
            f"evaluate denied for url={url!r}; allowed prefixes: {prefixes}",
            retryable=False,
        )


def _gate_upload_file(cmd: dict) -> None:
    """Fail-closed gate for upload_file.

    Requires ``BROWSER_UPLOAD_ALLOW_ROOT`` to be set to a directory path.
    Only files under that resolved root may be uploaded.
    """
    allow_root = os.environ.get("BROWSER_UPLOAD_ALLOW_ROOT", "").strip()
    if not allow_root:
        raise DaemonError(
            "UPLOAD_DISABLED",
            "upload_file is disabled. Set BROWSER_UPLOAD_ALLOW_ROOT to an allowlisted directory.",
            retryable=False,
        )

    file_path = cmd.get("file_path")
    if not file_path:
        # Let the normal param extractor raise MISSING_PARAM
        return

    try:
        resolved = Path(file_path).resolve()
        root = Path(allow_root).resolve()
    except Exception as e:
        raise DaemonError(
            "UPLOAD_PATH_INVALID",
            f"could not resolve upload path: {e}",
            retryable=False,
        )

    try:
        resolved.relative_to(root)
    except ValueError:
        raise DaemonError(
            "UPLOAD_PATH_DENIED",
            f"upload_file denied: {resolved} is outside allowlisted root {root}",
            retryable=False,
        )


def _ensure_drive_lock(backend: BrowserBackend) -> asyncio.Lock:
    """Return the backend drive lock, creating one if the backend has none."""
    lock = getattr(backend, "drive_lock", None)
    if isinstance(lock, asyncio.Lock):
        return lock
    lock = asyncio.Lock()
    try:
        setattr(backend, "drive_lock", lock)
    except Exception:
        pass
    return lock


async def _pin_session(backend: BrowserBackend, action: str, cmd: dict) -> None:
    """If ``target_id`` is set, pin the backend to that tab before the action.

    ``switch_tab`` already switches, so it is left to the route. Omitted
    ``target_id`` keeps the current session (legacy / doctor).
    """
    tid = cmd.get("target_id")
    if tid is None or tid == "":
        return
    if action == "switch_tab":
        return
    pin = getattr(backend, "pin_target", None)
    if callable(pin):
        await pin(str(tid))
        return
    switch = getattr(backend, "switch_tab", None)
    if callable(switch):
        await switch(str(tid))


async def _execute_action(backend: BrowserBackend, action: str, cmd: dict) -> dict:
    """Pin (optional), then dispatch. Serialized on ``backend.drive_lock``."""
    lock = _ensure_drive_lock(backend)
    async with lock:
        await _pin_session(backend, action, cmd)
        return await _execute_action_unlocked(backend, action, cmd)


async def _execute_action_unlocked(backend: BrowserBackend, action: str, cmd: dict) -> dict:
    """Look up the action and call the backend method with extracted params.

    Actions that don't map to a backend method (ping, run_procedure,
    report_outcome) are handled as special cases before the route dispatch.
    """

    # ── Special cases that don't map to backend methods ────────────────

    if action == "ping":
        return {"ok": True, "worker": getattr(backend, "worker_id", "unknown")}

    if action == "run_procedure":
        from daemon.primitives.replay import run_procedure
        from daemon.provenance import record_procedure_run
        import yaml as _yaml

        # Load from procedure file if specified, otherwise use inline steps
        procedure_name = cmd.get("procedure")
        if procedure_name:
            proc_dir = Path(__file__).resolve().parent.parent / "skills" / "procedures"
            proc_path = proc_dir / f"{procedure_name}.yaml"
            if not proc_path.exists():
                return {
                    "procedure": True,
                    "error": f"Procedure not found: {procedure_name} (looked in {proc_path})",
                    "steps_executed": 0,
                    "steps_total": 0,
                    "passed": [],
                    "failed": [],
                }
            proc = _yaml.safe_load(proc_path.read_text())
            steps = proc.get("steps", [])
            # Merge defaults from procedure with caller's params
            defaults = proc.get("params", {})
            default_values = {k: v.get("default", "") if isinstance(v, dict) else v
                              for k, v in defaults.items()}
            caller_params = cmd.get("params", {}) or {}
            params = {**default_values, **caller_params}
            skill_id = proc.get("name", procedure_name)
            domain = proc.get("domain", "unknown")
        else:
            steps = cmd.get("steps", [])
            params = cmd.get("params")
            skill_id = cmd.get("skill_id", "unknown")
            domain = cmd.get("domain", "unknown")

        cdp_client = getattr(backend, "cdp", backend)
        session_id = getattr(backend, "session_id", None)
        worker_id = getattr(backend, "worker_id", "default")
        t0 = time.monotonic_ns()
        result = await run_procedure(cdp_client, session_id, steps, params)
        duration_ms = (time.monotonic_ns() - t0) / 1_000_000

        # Get current page URL after procedure run
        url = None
        try:
            page = await backend.page_info()
            url = page.get("url") or page.get("page", {}).get("url")
        except Exception:
            pass
        # Fire-and-forget provenance recording
        asyncio.ensure_future(
            record_procedure_run(
                worker_id, skill_id, domain, url, result, duration_ms
            )
        )
        return result

    if action == "run_validator":
        from daemon.validator import validate_screenshot
        from daemon.provenance import record_validation
        screenshot_path = cmd.get("screenshot_path", "/tmp/browser-shot.png")
        assertions = cmd.get("assertions", [])
        task_description = cmd.get("task_description", "")
        cdp_client = getattr(backend, "cdp", backend)
        session_id = getattr(backend, "session_id", None)
        t0 = time.monotonic_ns()
        validation = await validate_screenshot(
            screenshot_path, assertions, task_description, cdp_client, session_id
        )
        duration_ms = (time.monotonic_ns() - t0) / 1_000_000
        result = {
            "validator": True,
            "complete": validation.complete,
            "confidence": validation.confidence,
            "reasoning": validation.reasoning,
            "assertions_passed": validation.assertions_passed,
            "assertions_failed": validation.assertions_failed,
        }
        worker_id = getattr(backend, "worker_id", "default")
        skill_id = cmd.get("skill_id", "unknown")
        domain = cmd.get("domain", "unknown")
        # Fire-and-forget provenance recording
        asyncio.ensure_future(
            record_validation(
                worker_id, skill_id, domain, validation, duration_ms
            )
        )
        return result

    if action == "extract_page_text":
        cdp_client = getattr(backend, "cdp", backend)
        session_id = getattr(backend, "session_id", None)
        if cdp_client is not None:
            eval_result = await cdp_client.send(
                "Runtime.evaluate",
                {
                    "expression": "document.body.innerText",
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                session_id=session_id,
            )
            raw = eval_result.get("result", {}).get("result", {}).get("value", "")
            page_text = str(raw) if raw is not None else ""
        else:
            page_text = ""
        return {"page_text": page_text}

    if action == "capture_debug_context":
        from daemon.primitives.self_heal import capture_failure_context
        failed_selector = cmd.get("selector")
        step = {"action": "debug", "selector": failed_selector} if failed_selector else {}
        cdp_client = getattr(backend, "cdp", backend)
        session_id = getattr(backend, "session_id", None)
        ctx = await capture_failure_context(
            cdp_client, session_id, step, "explicit debug request", 0
        )
        from dataclasses import asdict
        return asdict(ctx)

    if action == "navigate":
        from daemon.provenance import record_navigation
        url_param = cmd.get("url", "about:blank")
        t0 = time.monotonic_ns()
        method = getattr(backend, "navigate", None)
        if method is None:
            raise DaemonError(
                "NOT_IMPLEMENTED",
                f"backend {type(backend).__name__} does not implement navigate",
            )
        nav_result = await method(url_param)
        duration_ms = (time.monotonic_ns() - t0) / 1_000_000
        # Get actual URL after navigation (handles redirects)
        actual_url = url_param
        try:
            page = await backend.page_info()
            actual_url = page.get("url") or page.get("page", {}).get("url") or url_param
        except Exception:
            pass
        domain = cmd.get("domain", "unknown")
        if domain == "unknown" and actual_url:
            from urllib.parse import urlparse
            try:
                parsed = urlparse(actual_url)
                domain = parsed.netloc or "unknown"
            except Exception:
                pass
        worker_id = getattr(backend, "worker_id", "default")
        asyncio.ensure_future(
            record_navigation(
                worker_id, actual_url, domain, duration_ms
            )
        )
        if not isinstance(nav_result, dict):
            nav_result = {"result": nav_result}
        return nav_result

    if action == "query_provenance":
        from daemon.provenance import query_provenance
        from dataclasses import asdict
        worker_id = getattr(backend, "worker_id", "default")
        skill_id = cmd.get("skill_id")
        limit = cmd.get("limit", 50)
        records = query_provenance(worker_id, skill_id, limit)
        return {"records": [asdict(r) for r in records], "count": len(records)}

    if action == "report_outcome":
        from daemon.health import write_outcome
        skill_id = cmd.get("skill_id", "unknown")
        domain = cmd.get("domain", "unknown")
        success = bool(cmd.get("success", False))
        error = cmd.get("error")
        worker_id = getattr(backend, "worker_id", "default")

        record = write_outcome(worker_id, skill_id, domain, success, error)

        return {"reported": True, "skill_id": skill_id, "success": success, "degraded": record.degraded}

    if action == "start_async":
        from daemon.async_jobs import start_async as _start_async
        steps = cmd.get("steps", [])
        params = cmd.get("params")
        cdp_client = getattr(backend, "cdp", backend)
        session_id = getattr(backend, "session_id", None)
        handle_id = await _start_async(steps, params, cdp_client, session_id)
        return {"handle_id": handle_id, "status": "running"}

    if action == "poll_async":
        from daemon.async_jobs import poll_async as _poll_async
        handle_id = cmd.get("handle_id")
        job = _poll_async(handle_id)
        if job is None:
            return {"error": f"Unknown handle: {handle_id}"}
        return {
            "handle_id": job.handle_id,
            "status": job.status,
            "steps_executed": job.steps_executed,
            "steps_total": job.steps_total,
            "result": job.result if job.status in ("done", "failed") else None,
            "error": job.error,
        }

    if action == "list_asyncs":
        from daemon.async_jobs import list_asyncs as _list_asyncs
        jobs = _list_asyncs()
        return {
            "jobs": [
                {
                    "handle_id": j.handle_id,
                    "status": j.status,
                    "steps_executed": j.steps_executed,
                    "steps_total": j.steps_total,
                }
                for j in jobs
            ]
        }

    if action == "read_skill_health":
        from daemon.health import read_health, get_degraded_skills
        from dataclasses import asdict
        skill_id = cmd.get("skill_id")
        worker_id = getattr(backend, "worker_id", "default")
        records = read_health(worker_id, skill_id)
        degraded = get_degraded_skills(worker_id)
        return {
            "records": [asdict(r) for r in records],
            "degraded": [asdict(r) for r in degraded],
        }

    # ── Route-based dispatch for backend methods ───────────────────────

    # Resolve aliases
    resolved_action = _ACTION_ALIASES.get(action, action)

    # ── Security gates (fail-closed defaults) ────────────────────
    if resolved_action == "evaluate":
        await _gate_evaluate(backend)
    if resolved_action == "upload_file":
        _gate_upload_file(cmd)

    route = _ROUTE_MAP.get(resolved_action)
    if route is None:
        raise UnknownActionError(action)

    method_name, extractor = route
    method = getattr(backend, method_name, None)
    if method is None:
        raise DaemonError(
            "NOT_IMPLEMENTED",
            f"backend {type(backend).__name__} does not implement {method_name}",
        )

    params = extractor(cmd)

    # Call the method
    result = await method(**params)

    # Ensure result is a dict
    if not isinstance(result, dict):
        result = {"result": result}

    return result


# ── IPC Server ──────────────────────────────────────────────────────────────

async def run_ipc_server(
    backend: BrowserBackend,
    socket_path: str,
    event_bus: EventBus | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the Unix socket IPC server.

    Accepts one JSON-line command per connection, routes to the backend,
    and returns a JSON-line response.

    A single connection = one command.  This keeps the protocol stateless
    and trivially safe against partial writes.
    """
    # Remove stale socket
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    # Ensure parent directory exists
    Path(socket_path).parent.mkdir(parents=True, exist_ok=True)

    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, backend, event_bus),
        path=socket_path,
    )
    # Security: owner read/write only (0600). The socket accepts arbitrary
    # browser-control commands; world/group access would let other local
    # users drive the browser. Keep this mode tight.
    os.chmod(socket_path, 0o600)

    print(f"[{backend.worker_id}] listening on {socket_path}", file=sys.stderr)

    # Handle shutdown signals
    loop = asyncio.get_running_loop()
    stop = stop_event or asyncio.Event()

    async def _shutdown():
        print(f"[{backend.worker_id}] shutting down…", file=sys.stderr)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_shutdown()))
        except NotImplementedError:
            # Windows or environments without signal support
            pass

    async with server:
        await stop.wait()
