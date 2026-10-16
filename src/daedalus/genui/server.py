"""The loopback server — how a rendered thing reaches the phone (SPEC §3.5, §3.6).

Two properties matter more than the routing, and both are the kind that fail
silently if they are only assumed:

1. **Loopback, always.** The bind address is ``127.0.0.1``: there is no public
   Funnel and no code path that opens one. Publishing is a *separate* act
   (Tailscale Serve, ticket T2) which puts an authenticated tailnet in front of
   an already-private port. A server that bound ``0.0.0.0`` "temporarily" would
   be the whole security model, gone.
2. **No anonymous controls.** Reads are open to anything already on the loopback
   (that is what loopback means). Writes are not: every ``POST`` carries a
   per-install token from the owner-only store, and a server with no token
   configured accepts **no** writes at all. Failing open here would turn "the
   dashboard" into "an unauthenticated remote control for the owner's calendar",
   reachable by anything that can curl.

The routing table is written down (``ROUTES``) rather than inferred, so a control
that exists in the design but is not built yet answers *"not built yet"* instead
of a 404 — the difference between "I have not done that" and "that is not a
thing", which the owner is entitled to be told apart.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any

from ..resources.secrets import SecretStore
from .renderer import CSP

if TYPE_CHECKING:
    from ..calendar.store import CalendarStore

logger = logging.getLogger("daedalus.genui")

#: The only address this server ever binds. Not a default to be overridden.
LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8787
#: The header a control request carries its token in.
TOKEN_HEADER = "X-Daedalus-Token"
#: Artifact names become file names, so they are *matched* before they are joined.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: The dashboard's views, as read paths: `/day/2026-10-16`, `/week/…`, `/month/…`.
#: The day is optional and defaults to today, so the shortest address is the one
#: the owner would type.
_VIEW_PATH = re.compile(r"^/(day|week|month)(?:/(\d{4}-\d{2}-\d{2}))?$")

_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
}

#: The control API (ARCHITECTURE §9.5), by route. Listed so that a control that is
#: designed but is not built yet can say so; each is implemented by the ticket
#: that owns it. `GET /dashboard` is built (T7) and answered below, together with
#: its view paths (`/day/`, `/week/`, `/month/`); the four that remain unbuilt are
#: the writes the approval split owns (T8, T9).
ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/dashboard"): "the generated dashboard — day, week or month",
    ("GET", "/draft"): "a live preview of a pending theme or component",
    ("POST", "/tune"): "set pending token or knob values",
    ("POST", "/approve"): "commit the pending set into the owner-approved store",
    ("POST", "/action"): "route an action through the approval split",
}

_PLAIN = "text/plain; charset=utf-8"


@dataclass(frozen=True, slots=True)
class Response:
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    body: bytes = b""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class LocalServer:
    """Serves the owner's generated artifacts on the loopback, and gates writes.

    Loopback-only is asserted, not merely intended: :meth:`start` binds
    :data:`LOOPBACK` and the tests check the address it actually came up on.
    """

    def __init__(
        self,
        home: Path,
        *,
        token: str = "",
        port: int = DEFAULT_PORT,
        artifacts: Path | None = None,
        calendar: "CalendarStore | None" = None,
    ) -> None:
        self.home = Path(home)
        self.artifacts = Path(artifacts) if artifacts is not None else self.home / "ui"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.port = int(port)
        self.host = LOOPBACK
        #: What `/dashboard` draws. Absent means there is nothing to draw, which
        #: the route says plainly rather than pretending to be unbuilt.
        self.calendar = calendar
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- routing -----------------------------------------------------------

    def get(self, path: str) -> Response:
        route = _route(path)
        if route == "/healthz":
            return _json({"ok": True, "artifacts": len(self.artifact_names())})
        if route in ("/", "/index.html"):
            return self._entry()
        view = _view_route(route)
        if view is not None:
            return self._dashboard(*view)
        if ("GET", route) in ROUTES:
            return _not_built(route, ROUTES[("GET", route)])
        return self._artifact(route.lstrip("/"))

    def post(self, path: str, body: bytes = b"", *, token: str = "") -> Response:
        route = _route(path)
        if not self._authorised(token):
            return Response(401, _PLAIN, b"a control token is required\n")
        if ("POST", route) in ROUTES:
            return _not_built(route, ROUTES[("POST", route)])
        return Response(404, _PLAIN, b"no such control\n")

    def _authorised(self, offered: str) -> bool:
        """Whether a write may proceed. Fails **closed** — see the module note."""
        if not self.token:
            return False
        return hmac.compare_digest(self.token, offered or "")

    # -- the dashboard -----------------------------------------------------

    def _dashboard(self, view: str, anchor: str) -> Response:
        """Draw a day, week or month from the internal calendar (SPEC §5.5).

        The page is generated *and written*: it lands in `ui/` like every other
        artifact, so the file is there after the request that made it, and the
        front page becomes today's view. Reading the calendar on every request is
        the point — a cached dashboard is a dashboard that lies about today.
        """
        from .dashboard import calendar_page, write_page

        if self.calendar is None:
            return Response(
                501,
                _PLAIN,
                b"/dashboard has nothing to draw: this server was built without a calendar\n",
            )
        try:
            page = calendar_page(self.calendar, view=view, anchor=anchor or None)
        except ValueError as exc:
            return Response(400, _PLAIN, f"{exc}\n".encode("utf-8"))
        try:
            write_page(page, self.home, front=view == "day" and not anchor)
        except OSError as exc:
            # Serving the page matters more than keeping the copy: the owner asked
            # to *see* the calendar, and a full disk is not a reason to show them
            # nothing.
            logger.warning("could not write the dashboard artifact: %s", exc)
        return Response(200, _TYPES[".html"], page.html().encode("utf-8"))

    # -- artifacts ---------------------------------------------------------

    def artifact_names(self) -> list[str]:
        return sorted(
            path.name for path in self.artifacts.glob("*") if path.is_file() and _SAFE_NAME.match(path.name)
        )

    def _artifact(self, name: str) -> Response:
        if not _SAFE_NAME.match(name):
            return _refused(name)
        path = self.artifacts / name
        try:
            if path.resolve().parent != self.artifacts.resolve():
                return _refused(name)
        except OSError:
            return _refused(name)
        if not path.is_file():
            return Response(404, _PLAIN, f"nothing served at /{name}\n".encode("utf-8"))
        return Response(200, _content_type(name), path.read_bytes())

    def _entry(self) -> Response:
        """The front page: the newest artifact, or a plain list of them."""
        index = self.artifacts / "index.html"
        if index.is_file():
            return Response(200, _TYPES[".html"], index.read_bytes())
        names = self.artifact_names()
        if not names:
            return Response(
                200,
                _TYPES[".html"],
                _EMPTY_PAGE.encode("utf-8"),
            )
        items = "\n".join(
            f'<li><a href="/{_escape(name)}">{_escape(name)}</a></li>' for name in names
        )
        page = (
            "<!doctype html><meta charset=\"utf-8\">"
            "<title>Daedalus</title>"
            "<style>body{font:15px/1.5 system-ui,sans-serif;margin:3rem auto;max-width:38rem;"
            "color:#e8e8ea;background:#141416}h1{font-size:1.1rem}a{color:#8ab4f8}</style>"
            f"<h1>Daedalus — generated</h1><ul>{items}</ul>"
        )
        return Response(200, _TYPES[".html"], page.encode("utf-8"))

    # -- lifecycle ---------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def start(self) -> str:
        """Bind and serve in a background thread. Returns the loopback URL."""
        if self._http is not None:
            return self.url
        httpd = ThreadingHTTPServer((LOOPBACK, self.port), _handler_for(self))
        # Port 0 means "whatever is free" — take the port the OS actually gave.
        self.port = int(httpd.server_address[1])
        self.host = str(httpd.server_address[0])
        self._http = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="daedalus-serve", daemon=True
        )
        self._thread.start()
        logger.info("serving %s on %s (loopback only)", self.artifacts, self.url)
        return self.url

    def stop(self) -> None:
        if self._http is None:
            return
        self._http.shutdown()
        self._http.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._http = None
        self._thread = None

    def __enter__(self) -> "LocalServer":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()


# -- the token --------------------------------------------------------------


def control_token(store: SecretStore, *, name: str = "dashboard-token") -> str:
    """The per-install control token, minted once and kept encrypted at rest.

    "It is only loopback" is not authentication: anything running as this user can
    reach the loopback, and the dashboard's writes are real actions against the
    owner's calendar. So the token is a real secret, in the same store as the
    others, and its absence is a refusal rather than an open door.
    """
    existing = store.get(name)
    if existing:
        return existing
    minted = token_urlsafe(32)
    store.put(name, minted)
    return minted


# -- the HTTP shell ---------------------------------------------------------


def _handler_for(app: LocalServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Daedalus/0.1"
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - the BaseHTTPRequestHandler name
            self._emit(app.get(self.path))

        def do_HEAD(self) -> None:  # noqa: N802
            self._emit(app.get(self.path), body=False)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            self._emit(app.post(self.path, body, token=self.headers.get(TOKEN_HEADER, "")))

        def _emit(self, response: Response, *, body: bool = True) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            if response.content_type == _TYPES[".html"]:
                # The policy is *in* the document, as a <meta> the renderer writes
                # — but a meta policy only begins to apply where it is parsed, so
                # the bytes before it are a document with no policy at all. Sent as
                # a header it covers the whole response, from the first byte.
                self.send_header("Content-Security-Policy", CSP)
            # Whatever is served here is the owner's own data, in a document
            # nothing else should hold onto or be able to embed.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if body and response.body:
                self.wfile.write(response.body)

        def log_message(self, fmt: str, *args: Any) -> None:
            # A request log on the owner's surface is noise; loopback chatter
            # belongs in the debug log, where it can be turned on deliberately.
            logger.debug("serve %s", fmt % args)

    return Handler


# -- helpers ----------------------------------------------------------------


def _route(path: str) -> str:
    route = str(path or "/").split("?", 1)[0].split("#", 1)[0]
    if not route.startswith("/"):
        route = "/" + route
    return route if route == "/" else route.rstrip("/")


def _view_route(route: str) -> tuple[str, str] | None:
    """The dashboard view a path asks for, or ``None`` for any other path.

    `/dashboard` is the day the owner is having; a view may be named on its own
    (`/week`) or pinned to a day (`/week/2026-10-16`), which is what makes a
    rendered cell linkable.
    """
    if route == "/dashboard":
        return ("day", "")
    found = _VIEW_PATH.match(route)
    return (found.group(1), found.group(2) or "") if found else None


def _content_type(name: str) -> str:
    return _TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _json(payload: dict[str, Any]) -> Response:
    return Response(
        200, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8")
    )


def _not_built(route: str, what: str) -> Response:
    return Response(
        501,
        _PLAIN,
        f"{route} is not built yet — it will {what}.\n".encode("utf-8"),
    )


def _refused(name: str) -> Response:
    logger.warning("refused a request for %r", name)
    return Response(400, _PLAIN, f"{name!r} is not a name this server serves\n".encode("utf-8"))


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_EMPTY_PAGE = (
    '<!doctype html><meta charset="utf-8"><title>Daedalus</title>'
    "<style>body{font:15px/1.5 system-ui,sans-serif;margin:3rem auto;max-width:38rem;"
    "color:#e8e8ea;background:#141416}</style>"
    "<h1>Daedalus</h1><p>Nothing generated yet.</p>"
)
