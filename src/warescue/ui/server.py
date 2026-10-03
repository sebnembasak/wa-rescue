"""Local HTTP server behind `warescue ui`.

Security model (the process holds a full chat history and a backup key):

* listens on 127.0.0.1 only, on a random free port.
* every request must carry the server's own Host (blocks DNS rebinding) and,
  if the browser sends one, its own Origin (blocks other websites).
* every /api/ request must carry the session token in a header. The token
  reaches the page in the URL fragment, which browsers never send to servers.
* the pages are served with a strict Content Security Policy. No external
  resources at all, and only the page's own inline script may run.
"""
from __future__ import annotations

import hmac
import secrets
import sys
import threading
import traceback
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

from . import api
from .jobs import JobRunner
from .session import Session
from .web import ApiError, Request, Response

HOST = "127.0.0.1"
TOKEN_HEADER = "X-Warescue-Token"
IDLE_TIMEOUT_S = 30 * 60
WATCHDOG_INTERVAL_S = 15.0
NONCE_PLACEHOLDER = "__CSP_NONCE__"

PAGE_CSP = ("default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
            "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")
COMMON_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
}


# Static pages served without a token, they hold no session data.
PAGES = {"/": "index.html", "/how-it-works": "how-it-works.html"}


def load_page(name: str) -> str:
    return resources.files(__package__).joinpath("static", name).read_text(encoding="utf-8")


class WizardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, session: Session, *, port: int = 0,
                 idle_timeout: float = IDLE_TIMEOUT_S,
                 watchdog_interval: float = WATCHDOG_INTERVAL_S) -> None:
        super().__init__((HOST, port), RequestHandler)
        self.session = session
        self.jobs = JobRunner()
        self.idle_timeout = idle_timeout
        self.pages = {path: load_page(name) for path, name in PAGES.items()}
        self._stopping = threading.Event()
        self._watchdog = threading.Thread(target=self._watch_idle, args=(watchdog_interval,),
                                          name="warescue-idle", daemon=True)
        self._watchdog.start()

    # identity

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({f"{HOST}:{self.port}", f"localhost:{self.port}"})

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(f"http://{host}" for host in self.allowed_hosts)

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/#t={self.session.token}"

    # lifecycle

    def stop(self) -> None:
        """Forget the key and stop serving. Safe to call from any thread, once or more."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        self.session.clear_key()
        # shutdown() blocks until serve_forever() returns, so never run it on
        # the thread that is handling the request asking for it.
        threading.Thread(target=self.shutdown, name="warescue-stop", daemon=True).start()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def server_close(self) -> None:
        self._stopping.set()
        self.jobs.shutdown(wait=False, cancel_pending=True)
        self.session.close()
        super().server_close()

    def _watch_idle(self, interval: float) -> None:
        while not self._stopping.wait(interval):
            if self.session.idle_seconds() >= self.idle_timeout and not self.jobs.busy:
                print("warescue ui: idle for too long, shutting down", file=sys.stderr)
                self.stop()

    # routes

    def route(self, request: Request) -> Response:
        for method, pattern, handler in api.ROUTES:
            if method == request.method and (match := pattern.match(request.path)):
                request.match = match
                try:
                    return handler(self, request)
                except ApiError as exc:
                    return Response.error(exc.status, exc.code, exc.message)
                except Exception:  # never drop the connection without an answer
                    traceback.print_exc()
                    return Response.error(HTTPStatus.INTERNAL_SERVER_ERROR, "unexpected",
                                          "unexpected error; see the terminal")
        return Response.error(HTTPStatus.NOT_FOUND, "not_found", "no such endpoint")

    def render_page(self, path: str = "/") -> Response:
        nonce = secrets.token_urlsafe(16)
        body = self.pages[path].replace(NONCE_PLACEHOLDER, nonce).encode("utf-8")
        return Response(HTTPStatus.OK, body, "text/html; charset=utf-8",
                        (("Content-Security-Policy", PAGE_CSP.format(nonce=nonce)),))


class RequestHandler(BaseHTTPRequestHandler):
    server: WizardServer
    server_version = "warescue"
    sys_version = ""

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the terminal quiet; request lines carry nothing worth logging.
        pass

    def _handle(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        refusal = self._refusal(path)
        if refusal is None:
            self.server.session.touch()
        self._send(refusal or self._dispatch(method, path))

    def _refusal(self, path: str) -> Response | None:
        if self.headers.get("Host") not in self.server.allowed_hosts:
            return Response.error(HTTPStatus.FORBIDDEN, "bad_host", "unexpected Host header")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            return Response.error(HTTPStatus.FORBIDDEN, "bad_origin", "cross-origin request refused")
        if not path.startswith("/api/"):
            # The page holds no secret (the token is in the URL fragment), so
            # it may be opened by a link from anywhere.
            return None
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return Response.error(HTTPStatus.FORBIDDEN, "bad_origin", "cross-site request refused")
        token = self.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(token.encode(), self.server.session.token.encode()):
            return Response.error(HTTPStatus.UNAUTHORIZED, "bad_token", "missing or wrong session token")
        return None

    def _dispatch(self, method: str, path: str) -> Response:
        if path in PAGES and method == "GET":
            return self.server.render_page(path)
        if not path.startswith("/api/"):
            return Response.error(HTTPStatus.NOT_FOUND, "not_found", "not found")
        return self.server.route(Request(method, path, self.headers, self.rfile))

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(response.length))
        for name, value in (*COMMON_HEADERS.items(), *response.headers):
            self.send_header(name, value)
        self.end_headers()
        response.write_to(self.wfile)


def serve(session: Session, *, port: int = 0, open_browser: bool = True,
          announce: Callable[[WizardServer], None] | None = None) -> None:
    """Run the wizard until the user clicks Finish, the session goes idle,
    or the process is interrupted."""
    with WizardServer(session, port=port) as server:
        if announce:
            announce(server)
        if open_browser:
            webbrowser.open(server.url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
