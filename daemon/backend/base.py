"""
BrowserBackend — abstract protocol that all browser backends implement.

Every method returns a ``dict`` suitable for JSON-serialization.  Methods that
interact with CDP return the CDP result envelope ``{"result": …}`` or an
error-adjacent structure.

Capability flags allow the RPC layer / agents to discover what a backend
supports without probing each method.
"""

import abc
from typing import Any


class BrowserBackend(abc.ABC):
    """Protocol that all browser backends implement.

    Capability flags are declared as a frozenset on each concrete subclass.
    The flags listed here represent the union of capabilities a production
    backend *may* provide:

    * ``js`` — Full JavaScript evaluation and DOM traversal
    * ``screenshots`` — PNG screenshot capture (file or base64)
    * ``compositor_click`` — Compositor-level click via CDP Input domain
    * ``humanize_input`` — Character-by-character typing with delays
    * ``tab_management`` — Create, switch, close tabs
    * ``dialog_handling`` — Accept/dismiss JavaScript dialogs
    * ``event_buffering`` — CDP event drain endpoint
    * ``http_get`` — HTTP GET outside the browser
    * ``humanize`` — Bézier mouse paths and human-like interaction delays
    * ``geoip`` — Auto-detect timezone/locale from proxy IP
    * ``webrtc_spoof`` — WebRTC IP leak patched at startup
    """

    worker_id: str
    capability_flags: frozenset[str] = frozenset()

    # ── lifecycle ──────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def connect(self, cdp_url: str) -> None:
        """Connect to the browser's CDP endpoint, attach to a page, enable domains."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Close the CDP connection gracefully."""

    # ── page / navigation ──────────────────────────────────────────────────

    @abc.abstractmethod
    async def navigate(self, url: str) -> dict:
        """Navigate the current page to ``url``."""

    @abc.abstractmethod
    async def page_info(self) -> dict:
        """Return current page metadata (url, title, viewport, …)."""

    @abc.abstractmethod
    async def wait_for_load(self, timeout: float = 15.0) -> dict:
        """Wait for ``document.readyState === 'complete'``."""

    @abc.abstractmethod
    async def wait_for_element(self, selector: str, timeout: float = 10.0) -> dict:
        """Wait for a DOM element matching ``selector`` to appear."""

    # ── input ──────────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def click(
        self, x: int, y: int, button: str = "left", clicks: int = 1
    ) -> dict:
        """Click at pixel coordinates (``x``, ``y``)."""

    @abc.abstractmethod
    async def type_text(self, text: str) -> dict:
        """Insert ``text`` at the focused element."""

    @abc.abstractmethod
    async def press_key(self, key: str) -> dict:
        """Press a single key (e.g. ``"Enter"``, ``"Tab"``)."""

    @abc.abstractmethod
    async def scroll(self, x: int = 500, y: int = 500, dy: int = -300) -> dict:
        """Scroll the page by ``dy`` pixels at (x, y)."""

    @abc.abstractmethod
    async def fill_input(self, selector: str, text: str) -> dict:
        """Fill an input field, dispatching framework events."""

    # ── capture / network ──────────────────────────────────────────────────

    @abc.abstractmethod
    async def screenshot(self, path: str | None = None) -> dict:
        """Capture a screenshot. Returns ``{"path": …}`` when a path is provided."""

    @abc.abstractmethod
    async def screenshot_base64(self) -> dict:
        """Capture a screenshot and return the base64-encoded PNG data."""

    @abc.abstractmethod
    async def http_get(self, url: str, headers: dict | None = None) -> dict:
        """Perform an HTTP GET outside the browser."""

    # ── tab management ─────────────────────────────────────────────────────

    @abc.abstractmethod
    async def list_tabs(self) -> dict:
        """List all open page tabs."""

    @abc.abstractmethod
    async def new_tab(self, url: str = "about:blank") -> dict:
        """Create and attach a new tab, optionally navigating to ``url``."""

    @abc.abstractmethod
    async def switch_tab(self, target_id: str) -> dict:
        """Activate an existing tab and attach a session to it."""

    @abc.abstractmethod
    async def close_tab(self, target_id: str | None = None) -> dict:
        """Close a tab (defaults to current tab)."""

    # ── evaluation ─────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def evaluate(self, expression: str) -> dict:
        """Evaluate JavaScript ``expression`` in the page context."""

    @abc.abstractmethod
    async def extract(self, selector: str, attribute: str | None = None) -> dict:
        """Extract text content or attribute from the first matching element."""

    # ── dialogs ────────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def handle_dialog(self, accept: bool = True, prompt_text: str = "") -> dict:
        """Accept or dismiss a JavaScript dialog (alert/confirm/prompt)."""

    @abc.abstractmethod
    async def dialog_status(self) -> dict:
        """Return the current dialog state (``{"dialog": …}`` or ``None``)."""

    # ── events ─────────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def drain_events(self) -> dict:
        """Drain buffered CDP events and return them."""

    # ── files ──────────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def upload_file(self, selector: str, file_path: str) -> dict:
        """Upload a file via a file input element."""

    # ── procedures ─────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def run_procedure(self, steps: list[dict], params: dict | None = None) -> dict:
        """Execute a deterministic step list."""

    @abc.abstractmethod
    async def report_outcome(self, skill_id: str, success: bool, error: str | None = None, details: dict | None = None) -> dict:
        """Report the outcome of a skill execution to the health log."""

    # ── health ─────────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def health(self) -> dict:
        """Return health/status info about the backend connection."""
