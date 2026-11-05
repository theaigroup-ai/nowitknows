from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import config as C


def content_security_policy(css: str, js: str) -> str:
    def digest(text: str) -> str:
        return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")

    return (
        "default-src 'none'; "
        f"style-src 'sha256-{digest(css)}'; script-src 'sha256-{digest(js)}'; "
        "connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def _document(cfg: C.Config) -> tuple[bytes, str]:
    from .ui_data import TooLarge, snapshot
    from .ui_page import CSS, JS, render

    try:
        data = snapshot(cfg)
    except TooLarge as error:
        raise PageTooLarge(str(error)) from None
    body = render(data).encode("utf-8")
    if len(body) > C.UI_PAGE_MAX_BYTES:
        raise PageTooLarge("The snapshot is larger than the page size limit, so it was not served.")
    return body, content_security_policy(CSS, JS)


class PageTooLarge(Exception):
    pass


USED = b"This one-use link was already opened. Restart nowitknows ui for a fresh link and snapshot.\n"


class SnapshotServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cfg: C.Config):
        self.cfg = cfg
        self.token = secrets.token_urlsafe(C.UI_TOKEN_BYTES)
        self.token_used = False
        self.token_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    @property
    def url(self) -> str:
        return f"{self.origin}/?token={self.token}"

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(C.UI_REQUEST_TIMEOUT_S)
        return connection, address

    def handle_error(self, request, client_address) -> None:
        # Connection failures must never print the request URL or data from a rendered snapshot.
        pass


class _Handler(BaseHTTPRequestHandler):
    server: SnapshotServer

    def log_message(self, format, *args) -> None:
        # The default access log includes the one-use URL.
        pass

    def _reply(self, status: int, body: bytes, policy: str = "default-src 'none'; frame-ancestors 'none'") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if status == 200 else "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Connection", "close")
        if status == 405:
            self.send_header("Allow", "GET")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _valid_request(self) -> bool:
        if self.headers.get_all("Host", []) != [f"127.0.0.1:{self.server.server_port}"]:
            return False
        if self.headers.get_all("Origin", []) not in ([], [self.server.origin]):
            return False
        if self.headers.get_all("Sec-Fetch-Site", []) not in ([], ["none"], ["same-origin"]):
            return False
        try:
            target = urlsplit(self.path)
            query = parse_qs(target.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return False
        if target.scheme or target.netloc or target.fragment or not self.path.startswith("/"):
            return False
        values = query.get("token", [])
        return (
            set(query) == {"token"}
            and len(values) == 1
            and secrets.compare_digest(values[0].encode("utf-8"), self.server.token.encode("ascii"))
        )

    def _respond(self) -> None:
        if not self._valid_request():
            self._reply(403, b"Forbidden\n")
            return
        with self.server.token_lock:
            if self.server.token_used:
                self._reply(403, USED)
                return
            if self.command != "GET":
                self._reply(405, b"Only GET is supported\n")
                return
            if urlsplit(self.path).path != "/":
                self._reply(404, b"Not found\n")
                return
            try:
                body, policy = _document(self.server.cfg)
            except PageTooLarge as error:
                self._reply(500, f"{error}\n".encode())
                return
            except Exception:
                self._reply(500, b"Could not read the local snapshot. Check the data folder and restart the command.\n")
                return
            self.server.token_used = True
        self._reply(200, body, policy)

    def do_GET(self) -> None:
        self._respond()

    def __getattr__(self, name: str):
        # BaseHTTPRequestHandler dispatches every method through do_<method>, including unknown verbs.
        if name.startswith("do_"):
            return self._respond
        raise AttributeError(name)


def create_server(cfg: C.Config) -> SnapshotServer:
    return SnapshotServer(cfg)


def run(cfg: C.Config, *, open_browser: bool = False) -> int:
    try:
        with create_server(cfg) as server:
            print(f"nowitknows UI: {server.url}", flush=True)
            print(
                "One-use link. Keep this terminal open. Press Ctrl-C to stop; restart for a fresh snapshot.", flush=True
            )
            if open_browser:
                webbrowser.open(server.url)
            try:
                server.serve_forever(poll_interval=C.UI_POLL_INTERVAL_S)
            except KeyboardInterrupt:
                pass
    except OSError:
        print("nowitknows UI could not start on 127.0.0.1.")
        return 1
    return 0
