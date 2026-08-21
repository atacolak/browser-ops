"""
CloakBackend — production backend wrapping CloakBrowser via CDP.

Every method delegates to the corresponding standalone primitive in
``primitives/``, adding state management (session tracking, dialog state,
event buffering).

This is the refactored core of the original ``BrowserDaemon`` class from
``main.py`` — all CDP logic was extracted into ``primitives/`` modules, and
this class provides the stateful wrapper + capability flags + event tap.
"""

import asyncio
import json
import os
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

from .base import BrowserBackend
from .cdp_client import CDPClient

from primitives import nav as _nav
from primitives import input as _input
from primitives import capture as _capture
from primitives import tabs as _tabs
from primitives import eval_mod as _eval
from primitives import files as _files
from primitives import replay as _replay

EVENT_BUFFER_SIZE = 500


class CloakBackend(BrowserBackend):
    """Backend that drives CloakBrowser (stealth Chromium) via CDP.

    Can either connect to an already-running browser (``connect()``) or
    launch one itself via the ``cloakbrowser`` Python wrapper — which
    provides Bézier mouse humanization, geoip locale sync, WebRTC
    spoofing, and stealth fingerprinting (``launch()``).

    Capabilities:
        - Full JS evaluation and DOM traversal
        - Screenshot capture (PNG, file or base64)
        - Compositor-level click via CDP Input domain
        - Humanized input via character-by-character typing
        - Tab management (create, switch, close)
        - Dialog handling (accept/dismiss)
        - Event buffering with ``drain_events``
        - ``humanize`` — Bézier mouse paths and human-like delays
        - ``geoip`` — auto-detect timezone/locale from proxy IP
        - ``webrtc_spoof`` — WebRTC IP leaks patched at startup
    """

    capability_flags: frozenset[str] = frozenset({
        "js",
        "screenshots",
        "compositor_click",
        "humanize_input",
        "tab_management",
        "dialog_handling",
        "event_buffering",
        "http_get",
        "humanize",
        "geoip",
        "webrtc_spoof",
    })

    # ── default paths (relative to browser-ops root) ────────────────────
    _DEFAULT_PROFILES = Path(__file__).resolve().parent.parent.parent / "profiles"

    def __init__(
        self,
        worker_id: str,
        profile_dir: str | None = None,
        cdp_port: int | None = None,
        *,
        unmanaged: bool = False,
    ):
        self.worker_id = worker_id
        self.profile_dir = profile_dir or str(self._DEFAULT_PROFILES / worker_id)
        self.cdp_port = cdp_port
        self.unmanaged = unmanaged
        self.cdp: CDPClient | None = None
        self.session_id: str | None = None
        self.target_id: str | None = None
        self.dialog: dict | None = None
        self._events: deque = deque(maxlen=EVENT_BUFFER_SIZE)
        self._connected = False
        self._playwright_context: Any = None  # set by launch()
        # Active-target publication (observe_mirror contract).
        # Prefer explicit path / state-root env so browserctl BROWSERCTL_STATE_ROOT
        # overrides align lease target_state_path with daemon publishes.
        #   BROWSER_TARGET_STATE — absolute path to active-target.json
        #   BROWSER_OPS_STATE    — state root (…/state); default <repo>/state
        self._active_target_path_override = self._resolve_target_path_env(worker_id)
        self._state_root = self._resolve_state_root_env()
        self._browser_generation = f"{worker_id}-{int(time.time())}"
        self._cdp_http_url: str | None = (
            f"http://127.0.0.1:{cdp_port}" if cdp_port else None
        )
        self._last_published_target: str | None = None
        self._last_published_url: str | None = None
        self._last_published_title: str | None = None
        self._main_frame_id: str | None = None
        self._refresh_task: asyncio.Task | None = None
        self._refresh_generation = 0
        # Serialize drive RPCs so concurrent sockets cannot interleave
        # switch_tab + action across sibling targets.
        self.drive_lock = asyncio.Lock()
        self._sessions: dict[str, str] = {}

    @staticmethod
    def _repo_state_root() -> Path:
        return Path(__file__).resolve().parent.parent.parent / "state"

    @classmethod
    def _resolve_state_root_env(cls) -> Path:
        raw = (os.environ.get("BROWSER_OPS_STATE") or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return cls._repo_state_root()

    @classmethod
    def _resolve_target_path_env(cls, worker_id: str) -> Path | None:
        """Return explicit BROWSER_TARGET_STATE path if set, else None."""
        raw = (os.environ.get("BROWSER_TARGET_STATE") or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return None

    def _wipe_session_restore(self) -> None:
        """Drop Chromium Session/Current Tabs so a kill cannot restore a tab storm."""
        root = Path(self.profile_dir)
        for rel in ("Default/Sessions", "Default/Session Storage"):
            d = root / rel
            if not d.is_dir():
                continue
            for child in d.iterdir():
                try:
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                except OSError as exc:
                    print(
                        f"[{self.worker_id}] session restore wipe skipped {child}: {exc}",
                        file=sys.stderr,
                    )
        for name in ("Default/Current Session", "Default/Current Tabs", "Default/Last Session", "Default/Last Tabs"):
            p = root / name
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
            except OSError as exc:
                print(
                    f"[{self.worker_id}] session restore wipe skipped {p}: {exc}",
                    file=sys.stderr,
                )

    @property
    def active_target_path(self) -> Path:
        if self._active_target_path_override is not None:
            return self._active_target_path_override
        from target_state import default_target_path

        return default_target_path(self._state_root, self.worker_id)

    def _publish_active_target(
        self,
        page: dict | None = None,
        *,
        force: bool = False,
        sync: bool = False,
    ) -> None:
        """Atomically publish the current CDP target for observe_mirror.

        File lock + atomic write is synchronous I/O. By default we offload to
        a thread-pool executor so event workers / the asyncio loop stay free.
        Pass ``sync=True`` for tests or disconnect teardown.
        """
        url = None
        title = None
        if isinstance(page, dict):
            url = page.get("url")
            title = page.get("title")
        if (
            not force
            and self.target_id == self._last_published_target
            and url == self._last_published_url
            and title == self._last_published_title
            and self.target_id is not None
        ):
            return
        # Snapshot for background thread (avoid racing self.* mutations).
        path = self.active_target_path
        worker_id = self.worker_id
        target_id = self.target_id
        cdp_url = self._cdp_http_url
        generation = self._browser_generation
        page_snap = dict(page) if isinstance(page, dict) else page

        def _do_publish() -> None:
            try:
                from target_state import publish_active_target

                publish_active_target(
                    path,
                    worker_id=worker_id,
                    active_target_id=target_id,
                    cdp_url=cdp_url,
                    browser_generation=generation,
                    page=page_snap,
                )
            except Exception as exc:
                print(
                    f"[{worker_id}] active-target publish failed: {exc}",
                    file=sys.stderr,
                )

        # Optimistic dedupe keys updated on the calling thread so rapid
        # duplicate events collapse before the executor runs.
        self._last_published_target = target_id
        self._last_published_url = url
        self._last_published_title = title

        if sync:
            _do_publish()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _do_publish()
            return
        loop.run_in_executor(None, _do_publish)

    def _schedule_target_refresh(self, *, delay: float = 0.05, force: bool = True) -> None:
        """Debounce page_info + publish off the CDP recv loop (no await in event tap)."""
        if not self._connected or self.cdp is None or self.session_id is None:
            return
        self._refresh_generation += 1
        gen = self._refresh_generation
        prev = self._refresh_task
        if prev is not None and not prev.done():
            prev.cancel()

        async def _run() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                if gen != self._refresh_generation:
                    return
                if not self._connected or self.cdp is None or self.session_id is None:
                    return
                if self.dialog:
                    return
                info = await _nav.page_info(self.cdp, self.session_id, self.dialog)
                if gen != self._refresh_generation:
                    return
                if not isinstance(info, dict) or info.get("dialog"):
                    return
                self._publish_active_target(
                    {
                        "url": info.get("url"),
                        "title": info.get("title"),
                        "target_id": self.target_id,
                    },
                    force=force,
                    sync=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    f"[{self.worker_id}] target refresh failed: {exc}",
                    file=sys.stderr,
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._refresh_task = loop.create_task(_run())

    def _on_page_event(self, method: str, params: dict) -> None:
        """React to navigation/load CDP events so mirror toolbar tracks the page."""
        if method == "Page.frameNavigated":
            frame = params.get("frame") if isinstance(params, dict) else None
            if not isinstance(frame, dict):
                return
            frame_id = frame.get("id")
            parent_id = frame.get("parentId")
            # Main frame has no parentId.
            if parent_id:
                return
            if frame_id:
                self._main_frame_id = frame_id
            url = frame.get("url")
            if url:
                # Provisional URL immediately (seq bump); title refresh follows load.
                self._publish_active_target(
                    {"url": url, "target_id": self.target_id}
                )
            self._schedule_target_refresh(delay=0.05, force=True)
            return

        if method == "Page.navigatedWithinDocument":
            # SPA / history API — same document, URL changes.
            url = params.get("url") if isinstance(params, dict) else None
            frame_id = params.get("frameId") if isinstance(params, dict) else None
            if self._main_frame_id and frame_id and frame_id != self._main_frame_id:
                return
            if url:
                self._publish_active_target(
                    {"url": url, "target_id": self.target_id}
                )
            self._schedule_target_refresh(delay=0.05, force=True)
            return

        if method in ("Page.loadEventFired", "Page.domContentEventFired"):
            # Prefer load; still refresh on DOMContent for slow assets.
            delay = 0.0 if method == "Page.loadEventFired" else 0.1
            self._schedule_target_refresh(delay=delay, force=True)
            return

        if method == "Page.frameStoppedLoading":
            frame_id = params.get("frameId") if isinstance(params, dict) else None
            if self._main_frame_id and frame_id and frame_id != self._main_frame_id:
                return
            self._schedule_target_refresh(delay=0.05, force=True)

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def launch(
        self,
        proxy_url: str | None = None,
        *,
        humanize: bool = True,
        geoip: bool = True,
        human_preset: str = "default",
        headless: bool = False,
    ) -> None:
        """Launch a browser via the ``cloakbrowser`` Python wrapper.

        Uses ``launch_persistent_context_async()`` which configures the
        stealth Chromium with:

        * Humanized mouse/keyboard/scroll (Bézier curves, variable delays)
        * GeoIP-based timezone & locale detection (when ``geoip=True``)
        * WebRTC IP spoofing (``--fingerprint-webrtc-ip=auto``)
        * Persistent profile at ``self.profile_dir``
        * Proxy routing via ``ProxySettings``
        * Exposed CDP port for the backend to connect

        Args:
            proxy_url: Proxy URL string (e.g. ``socks5://127.0.0.1:10999``).
            humanize: Enable human-like mouse/keyboard/scroll. Default True.
            geoip: Auto-detect timezone/locale from proxy IP. Default True.
            human_preset: ``'default'`` or ``'careful'`` (slower).
            headless: Run headless (no visible window). Default False.
        """
        # Lazy-import cloakbrowser so the backend is importable even if
        # the package is not installed (e.g. on CI / dev machines).
        from cloakbrowser import launch_persistent_context_async, ProxySettings

        profile_path = Path(self.profile_dir)
        profile_path.mkdir(parents=True, exist_ok=True)

        proxy = None
        if proxy_url:
            proxy = ProxySettings(server=proxy_url)

        # Build extra Chromium CLI args
        args = [
            "--fingerprint-webrtc-ip=auto",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
        ]
        if self.cdp_port:
            args.append(f"--remote-debugging-port={self.cdp_port}")
        self._wipe_session_restore()

        print(
            f"[{self.worker_id}] launching browser via cloakbrowser wrapper …",
            file=sys.stderr,
        )
        print(f"  profile: {self.profile_dir}", file=sys.stderr)
        print(f"  cdp port: {self.cdp_port}", file=sys.stderr)
        print(f"  proxy: {proxy_url or 'none'}", file=sys.stderr)
        print(f"  humanize: {humanize}, geoip: {geoip}, preset: {human_preset}", file=sys.stderr)

        self._playwright_context = await launch_persistent_context_async(
            user_data_dir=self.profile_dir,
            headless=headless,
            proxy=proxy,
            args=args,
            humanize=humanize,
            geoip=geoip,
            human_preset=human_preset,
        )

        # Now connect our CDP client to the launched browser
        cdp_url = f"http://127.0.0.1:{self.cdp_port}" if self.cdp_port else "http://127.0.0.1:9222"
        await self.connect(cdp_url)

    async def connect(self, cdp_url: str) -> None:
        """Resolve the CDP WebSocket URL, connect, attach to a page, and enable domains."""
        # Prefer explicit HTTP endpoint for target-state / mirror consumers.
        if cdp_url.startswith("http://") or cdp_url.startswith("https://"):
            self._cdp_http_url = cdp_url.rstrip("/")
        elif self.cdp_port and not self._cdp_http_url:
            self._cdp_http_url = f"http://127.0.0.1:{self.cdp_port}"

        ws_url = self._resolve_ws_url(cdp_url)
        print(f"[{self.worker_id}] connecting to {ws_url}", file=sys.stderr)

        self.cdp = CDPClient(ws_url)
        await self.cdp.connect()

        # Attach to first real (non-chrome) page
        targets = (await self.cdp.send("Target.getTargets"))["result"]["targetInfos"]
        pages = [
            t for t in targets
            if t.get("type") == "page"
            and not t.get("url", "").startswith(("chrome://", "devtools://"))
        ]

        if not pages:
            tid = (await self.cdp.send("Target.createTarget", {"url": "about:blank"}))["result"]["targetId"]
            pages = [{"targetId": tid, "url": "about:blank", "type": "page"}]

        attach_result = await self.cdp.send("Target.attachToTarget", {
            "targetId": pages[0]["targetId"],
            "flatten": True,
        })
        self.session_id = attach_result["result"]["sessionId"]
        self.target_id = pages[0]["targetId"]
        self._sessions[self.target_id] = self.session_id

        # Enable CDP domains
        await asyncio.gather(
            self.cdp.send("Page.enable", session_id=self.session_id),
            self.cdp.send("DOM.enable", session_id=self.session_id),
            self.cdp.send("Runtime.enable", session_id=self.session_id),
            self.cdp.send("Network.enable", session_id=self.session_id),
        )

        # Sync event tap: CDPClient runs handlers off the recv loop on a
        # bounded worker. Keep this path CPU-only / schedule work — never
        # await CDP (would stall the single event worker + starve page_info).
        # Ignore high-volume Network.* noise for the ring buffer.
        def _event_tap(method: str, params: dict) -> None:
            if not method.startswith("Network."):
                self._events.append({"method": method, "params": params})
            if method == "Page.javascriptDialogOpening":
                self.dialog = params
            elif method == "Page.javascriptDialogClosed":
                self.dialog = None
            try:
                self._on_page_event(method, params or {})
            except Exception as exc:
                print(
                    f"[{self.worker_id}] page event handler failed: {exc}",
                    file=sys.stderr,
                )

        self.cdp.on_event(_event_tap)
        self._connected = True
        # Seed main frame id when possible (about:blank attach).
        try:
            tree = await self.cdp.send("Page.getFrameTree", session_id=self.session_id)
            frame = (
                (tree.get("result") or {})
                .get("frameTree", {})
                .get("frame", {})
            )
            if isinstance(frame, dict) and frame.get("id"):
                self._main_frame_id = frame.get("id")
        except Exception:
            pass
        self._publish_active_target(
            {"url": pages[0].get("url"), "target_id": self.target_id},
            sync=True,
        )
        # Title may arrive after first paint.
        self._schedule_target_refresh(delay=0.1, force=True)
        print(
            f"[{self.worker_id}] attached to {pages[0].get('url', '?')[:80]}",
            file=sys.stderr,
        )

    async def disconnect(self) -> None:
        """Close the CDP WebSocket and, if we launched the browser, the Playwright context."""
        self._connected = False
        self._refresh_generation += 1
        task = self._refresh_task
        self._refresh_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            from target_state import clear_active_target

            clear_active_target(self.active_target_path, worker_id=self.worker_id)
        except Exception:
            pass
        if self.cdp:
            await self.cdp.close()
        if self._playwright_context is not None:
            print(f"[{self.worker_id}] closing playwright context …", file=sys.stderr)
            try:
                await self._playwright_context.close()
            except Exception:
                pass
            self._playwright_context = None

    def _resolve_ws_url(self, cdp_url: str) -> str:
        """Resolve an HTTP CDP endpoint to a ``ws://`` WebSocket URL."""
        if cdp_url.startswith("ws://") or cdp_url.startswith("wss://"):
            return cdp_url

        base = cdp_url.rstrip("/")
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base}/json/version", timeout=3) as r:
                    return json.loads(r.read())["webSocketDebuggerUrl"]
            except Exception:
                time.sleep(0.5)

        raise RuntimeError(f"Could not resolve CDP WebSocket URL from {cdp_url}")

    # ── page / navigation ──────────────────────────────────────────────────

    async def navigate(self, url: str) -> dict:
        # Provisional URL so mirror reacts immediately; load/events correct it.
        self._publish_active_target(
            {"url": url, "target_id": self.target_id}, sync=True
        )
        result = await _nav.goto_url(self.cdp, self.session_id, url)
        # Wait for document load then publish live url/title (redirects etc.).
        load = await _nav.wait_for_load(self.cdp, self.session_id, timeout=15.0)
        try:
            info = await _nav.page_info(self.cdp, self.session_id, self.dialog)
            if isinstance(info, dict) and not info.get("dialog"):
                self._publish_active_target(
                    {
                        "url": info.get("url") or url,
                        "title": info.get("title"),
                        "target_id": self.target_id,
                    },
                    force=True,
                    sync=True,
                )
            else:
                self._schedule_target_refresh(delay=0.05, force=True)
        except Exception:
            self._schedule_target_refresh(delay=0.05, force=True)
        if isinstance(result, dict):
            out = dict(result)
            out["load"] = load
            return out
        return {"result": result, "load": load}

    async def page_info(self) -> dict:
        info = await _nav.page_info(self.cdp, self.session_id, self.dialog)
        if isinstance(info, dict) and not info.get("dialog"):
            self._publish_active_target(
                {
                    "url": info.get("url"),
                    "title": info.get("title"),
                    "target_id": self.target_id,
                },
                force=True,
                sync=True,
            )
            info = dict(info)
            info["target_id"] = self.target_id
        return info

    async def wait_for_load(self, timeout: float = 15.0) -> dict:
        result = await _nav.wait_for_load(self.cdp, self.session_id, timeout)
        # Ensure toolbar catches up even if caller only waited for load.
        self._schedule_target_refresh(delay=0.0, force=True)
        return result

    async def wait_for_element(self, selector: str, timeout: float = 10.0) -> dict:
        return await _nav.wait_for_element(self.cdp, self.session_id, selector, timeout)

    # ── input ──────────────────────────────────────────────────────────────

    async def click(
        self, x: int, y: int, button: str = "left", clicks: int = 1
    ) -> dict:
        return await _input.click_at_xy(self.cdp, self.session_id, x, y, button, clicks)

    async def type_text(self, text: str) -> dict:
        return await _input.type_text(self.cdp, self.session_id, text)

    async def press_key(self, key: str) -> dict:
        return await _input.press_key(self.cdp, self.session_id, key)

    async def scroll(self, x: int = 500, y: int = 500, dy: int = -300) -> dict:
        return await _input.scroll(self.cdp, self.session_id, x, y, dy)

    async def fill_input(self, selector: str, text: str) -> dict:
        return await _input.fill_input(self.cdp, self.session_id, selector, text)

    # ── capture / network ──────────────────────────────────────────────────

    async def screenshot(self, path: str | None = None) -> dict:
        return await _capture.capture_screenshot(self.cdp, self.session_id, path=path, base64=False)

    async def screenshot_base64(self) -> dict:
        return await _capture.capture_screenshot(self.cdp, self.session_id, path=None, base64=True)

    async def http_get(self, url: str, headers: dict | None = None) -> dict:
        return await _capture.http_get(url, headers)

    # ── tab management ─────────────────────────────────────────────────────

    async def list_tabs(self) -> dict:
        return await _tabs.list_tabs(self.cdp, self.session_id)

    async def new_tab(self, url: str = "about:blank") -> dict:
        result = await _tabs.new_tab(self.cdp, self.session_id, url)
        self.session_id = result["session_id"]
        self.target_id = result["target_id"]
        self._sessions[self.target_id] = self.session_id
        self._main_frame_id = None
        await self._enable_session_domains(self.session_id)
        self._publish_active_target({"url": url, "target_id": self.target_id})
        self._schedule_target_refresh(delay=0.1, force=True)
        return result

    async def pin_target(self, target_id: str) -> dict:
        """Pin the drive session to *target_id* for a mutating act.

        Reuses a mapped CDP session when already attached. Activates the
        tab — this is drive, not peek. If chrome was relaunched and the
        stored id is gone, mint a replacement page (caller rewrites the lease).
        """
        if not target_id:
            return {"target_id": self.target_id, "session_id": self.session_id}
        try:
            return await self.switch_tab(target_id)
        except RuntimeError as e:
            if "No target with given id" not in str(e):
                raise
            minted = await self.new_tab("about:blank")
            minted = dict(minted)
            minted["replaced_target_id"] = target_id
            minted["retargeted"] = True
            return minted

    async def _enable_session_domains(self, session_id: str | None) -> None:
        if self.cdp is None or not session_id:
            return
        await asyncio.gather(
            self.cdp.send("Page.enable", session_id=session_id),
            self.cdp.send("DOM.enable", session_id=session_id),
            self.cdp.send("Runtime.enable", session_id=session_id),
            self.cdp.send("Network.enable", session_id=session_id),
        )

    async def switch_tab(self, target_id: str) -> dict:
        existing = self._sessions.get(target_id)
        if existing is not None:
            if self.target_id == target_id and self.session_id == existing:
                return {"target_id": target_id, "session_id": existing}
            if self.cdp is not None:
                await self.cdp.send("Target.activateTarget", {"targetId": target_id})
            self.session_id = existing
            self.target_id = target_id
            self._main_frame_id = None
            self._publish_active_target({"target_id": self.target_id})
            self._schedule_target_refresh(delay=0.05, force=True)
            return {"target_id": target_id, "session_id": existing}

        result = await _tabs.switch_tab(self.cdp, self.session_id, target_id)
        self.session_id = result["session_id"]
        self.target_id = result["target_id"]
        self._sessions[self.target_id] = self.session_id
        self._main_frame_id = None
        await self._enable_session_domains(self.session_id)
        try:
            tree = await self.cdp.send("Page.getFrameTree", session_id=self.session_id)
            frame = (
                (tree.get("result") or {})
                .get("frameTree", {})
                .get("frame", {})
            )
            if isinstance(frame, dict) and frame.get("id"):
                self._main_frame_id = frame.get("id")
                if frame.get("url"):
                    self._publish_active_target(
                        {
                            "url": frame.get("url"),
                            "target_id": self.target_id,
                        }
                    )
        except Exception:
            self._publish_active_target({"target_id": self.target_id})
        self._schedule_target_refresh(delay=0.05, force=True)
        return result

    async def peek_tab(self, target_id: str, *, path: str | None = None) -> dict:
        """Read a tab without becoming its driver and without activateTarget."""
        if not target_id:
            raise ValueError("peek_tab requires target_id")
        prev_tid = self.target_id
        prev_sid = self.session_id
        attached = await _tabs.attach_session(self.cdp, target_id, activate=False)
        sid = attached["session_id"]
        self._sessions[target_id] = sid
        await self._enable_session_domains(sid)
        self.session_id = sid
        self.target_id = target_id
        try:
            info = await _nav.page_info(self.cdp, sid, self.dialog)
            if isinstance(info, dict):
                info = dict(info)
                info["target_id"] = target_id
            if not path:
                shot_dir = Path(self._state_root) / self.worker_id / "shots"
                shot_dir.mkdir(parents=True, exist_ok=True)
                path = str(shot_dir / f"peek-{target_id[:16]}.png")
            shot = await _capture.capture_screenshot(
                self.cdp, sid, path=path, base64=False
            )
            return {
                "mode": "peek",
                "target_id": target_id,
                "activated": False,
                "page": info,
                "screenshot": shot,
            }
        finally:
            self.session_id = prev_sid
            self.target_id = prev_tid

    async def close_tab(self, target_id: str | None = None) -> dict:
        tid = target_id or self.target_id
        result = await _tabs.close_tab(self.cdp, self.session_id, tid)
        if tid:
            self._sessions.pop(tid, None)
        if tid and self.target_id and tid == self.target_id:
            self.target_id = None
            self.session_id = None
            self._publish_active_target(None, force=True)
        return result

    # ── evaluation ─────────────────────────────────────────────────────────

    async def evaluate(self, expression: str) -> dict:
        return await _eval.evaluate(self.cdp, self.session_id, expression)

    async def extract(self, selector: str, attribute: str | None = None) -> dict:
        return await _eval.extract(self.cdp, self.session_id, selector, attribute)

    # ── dialogs ────────────────────────────────────────────────────────────

    async def handle_dialog(self, accept: bool = True, prompt_text: str = "") -> dict:
        result = await _tabs.handle_dialog(self.cdp, self.session_id, accept, prompt_text)
        self.dialog = None
        return result

    async def dialog_status(self) -> dict:
        return {"dialog": self.dialog}

    # ── events ─────────────────────────────────────────────────────────────

    async def drain_events(self) -> dict:
        out = list(self._events)
        self._events.clear()
        return {"events": out}

    # ── files ──────────────────────────────────────────────────────────────

    async def upload_file(self, selector: str, file_path: str) -> dict:
        return await _files.upload_file(self.cdp, self.session_id, selector, file_path)

    # ── procedures ─────────────────────────────────────────────────────────

    async def run_procedure(self, steps: list[dict], params: dict | None = None) -> dict:
        return await _replay.run_procedure(self.cdp, self.session_id, steps, params=params)

    async def report_outcome(self, skill_id: str, success: bool, error: str | None = None, details: dict | None = None) -> dict:
        """Append a line to the skill-health JSONL log.

        The log is written to ``state/<worker>/skill-health.jsonl`` relative to
        the project root (``browser-ops/``).
        """
        import datetime
        # Derive state dir from the module location: cloak.py -> ../../state
        state_dir = Path(__file__).resolve().parent.parent.parent / "state" / self.worker_id
        state_dir.mkdir(parents=True, exist_ok=True)
        log_path = state_dir / "skill-health.jsonl"
        entry: dict[str, Any] = {
            "skill_id": skill_id,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "success": success,
        }
        if error:
            entry["error"] = error
        if details:
            entry["details"] = details
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return {"report_outcome": True, "skill_id": skill_id, "success": success}

    # ── health ─────────────────────────────────────────────────────────────

    async def health(self) -> dict:
        return {
            "connected": self._connected,
            "worker": self.worker_id,
            "target_id": self.target_id,
            "browser_generation": self._browser_generation,
            "cdp_url": self._cdp_http_url,
            "active_target_path": str(self.active_target_path),
            "dialog_active": self.dialog is not None,
            "events_buffered": len(self._events),
            "capabilities": sorted(self.capability_flags),
        }
