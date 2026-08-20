"""CDP target allocation uses PUT /json/new, not first-tab discovery."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.allocate import create_page_target  # noqa: E402


def test_create_page_target_uses_put():
    methods: list[str] = []

    class H(BaseHTTPRequestHandler):
        def do_PUT(self):  # noqa: N802
            methods.append("PUT")
            if urlparse(self.path).path == "/json/new":
                body = json.dumps({"id": "REAL-TARGET-1", "type": "page"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def do_GET(self):  # noqa: N802
            methods.append("GET")
            self.send_error(405)

        def log_message(self, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        tid = create_page_target(f"http://127.0.0.1:{httpd.server_address[1]}")
        assert tid == "REAL-TARGET-1"
        assert methods[0] == "PUT"
        assert "GET" not in methods
    finally:
        httpd.shutdown()


def test_create_page_target_synthetic_when_down():
    tid = create_page_target("http://127.0.0.1:1")
    assert tid.startswith("tgt-")


def test_claim_unowned_prefers_blank_over_json_new():
    from browserctl.allocate import claim_unowned_or_create

    methods: list[str] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            methods.append(("GET", urlparse(self.path).path))
            if urlparse(self.path).path == "/json/list":
                body = json.dumps(
                    [
                        {"id": "OWNED", "type": "page", "url": "about:blank"},
                        {"id": "LIVE", "type": "page", "url": "https://example.com/"},
                        {"id": "BLANK", "type": "page", "url": "about:blank"},
                    ]
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def do_PUT(self):  # noqa: N802
            methods.append(("PUT", urlparse(self.path).path))
            self.send_error(500)

        def log_message(self, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        tid = claim_unowned_or_create(
            f"http://127.0.0.1:{httpd.server_address[1]}",
            owned_ids={"OWNED"},
        )
        assert tid == "BLANK"
        assert ("PUT", "/json/new") not in methods
    finally:
        httpd.shutdown()


def test_claim_unowned_creates_when_all_owned():
    from browserctl.allocate import claim_unowned_or_create

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if urlparse(self.path).path == "/json/list":
                body = json.dumps(
                    [{"id": "OWNED", "type": "page", "url": "about:blank"}]
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def do_PUT(self):  # noqa: N802
            if urlparse(self.path).path == "/json/new":
                body = json.dumps({"id": "FRESH", "type": "page"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def log_message(self, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        tid = claim_unowned_or_create(
            f"http://127.0.0.1:{httpd.server_address[1]}",
            owned_ids={"OWNED"},
        )
        assert tid == "FRESH"
    finally:
        httpd.shutdown()
