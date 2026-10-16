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
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any, Mapping

from ..resources.secrets import SecretStore
from .actions import Controls
from .renderer import CSP, FORM_CSP
from .tuners import TuneError, Tuners

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
#: A pending tune's preview (T9): `/draft/<id>`.
_DRAFT_PATH = re.compile(r"^/draft/([A-Za-z0-9-]+)$")

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
#: its view paths (`/day/`, `/week/`, `/month/`) and a pending tune's preview
#: (`/draft/<id>`, T9). `POST /action` is built (T8) and answered when this
#: server was started **with** the daemon's `Controls` — the approval split lives
#: with the daemon, which owns the surface an approval needs. `/tune` and
#: `/approve` are built (T9) and answered when the server was started with its
#: `Tuners`, which is everywhere the theme can be seen.
ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/dashboard"): "the generated dashboard — day, week or month",
    ("GET", "/draft"): "a live preview of a pending theme or component",
    ("POST", "/tune"): "set pending token or knob values",
    ("POST", "/approve"): "commit the pending set into the owner-approved store",
    ("POST", "/action"): "run a control through the approval split — started with the daemon",
}

_PLAIN = "text/plain; charset=utf-8"


@dataclass(frozen=True, slots=True)
class Response:
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    body: bytes = b""
    #: Extra headers — `Location` for a redirect, `Set-Cookie` for the one
    #: bootstrap that hands the browser its session.
    headers: tuple[tuple[str, str], ...] = ()
    #: This response's own policy. `None` means the document default.
    csp: str | None = None

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
        controls: Controls | None = None,
        tuners: Tuners | None = None,
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
        #: The page's side of the approval split. Absent means this server was
        #: started without a daemon, and its controls say so rather than acting.
        self.controls = controls
        #: The pending → approved path for the theme (T9). Absent means `/tune`,
        #: `/approve` and `/draft/` answer as not built.
        self.tuners = tuners
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- routing -----------------------------------------------------------

    def get(self, path: str, *, cookie: str = "") -> Response:
        route, query = _split_query(path)
        wanted = _query_token(query)
        if wanted:
            # The one bootstrap: the owner opens the printed URL, the token is
            # verified, and the browser is handed a cookie. The token itself
            # never enters a document, and the redirect takes it out of the bar.
            if not self._authorised(wanted):
                return Response(
                    403, _PLAIN, b"that control token is not this install's\n"
                )
            return Response(
                303,
                _PLAIN,
                b"",
                headers=(
                    ("Location", route or "/dashboard"),
                    ("Set-Cookie", f"{COOKIE}={wanted}; HttpOnly; SameSite=Strict; Path=/; Max-Age={COOKIE_MAX_AGE}"),
                ),
            )
        if route == "/healthz":
            return _json({"ok": True, "artifacts": len(self.artifact_names())})
        if route in ("/", "/index.html"):
            return self._entry()
        view = _view_route(route)
        if view is not None:
            return self._dashboard(*view, session=self._authorised(_cookie_token(cookie)))
        draft = _DRAFT_PATH.match(route)
        if draft is not None:
            return self._draft(draft.group(1), session=self._authorised(_cookie_token(cookie)))
        if ("GET", route) in ROUTES:
            return _not_built(route, ROUTES[("GET", route)])
        return self._artifact(route.lstrip("/"))

    def post(
        self, path: str, body: bytes = b"", *, token: str = "", cookie: str = "", content_type: str = ""
    ) -> Response:
        route = _route(path)
        # A browser cannot set a header, so a control's session may travel as
        # the cookie the bootstrap handed it. Either credential is the same
        # secret, checked the same way.
        if not self._authorised(token or _cookie_token(cookie)):
            return Response(401, _PLAIN, b"a control token is required\n")
        if ("POST", route) in ROUTES:
            if route == "/action" and self.controls is not None:
                return self._action(body, content_type)
            if route == "/tune" and self.tuners is not None:
                return self._tune(body, content_type)
            if route == "/approve" and self.tuners is not None:
                return self._approve(body, content_type)
            return _not_built(route, ROUTES[("POST", route)])
        return Response(404, _PLAIN, b"no such control\n")

    def _authorised(self, offered: str) -> bool:
        """Whether a write may proceed. Fails **closed** — see the module note."""
        if not self.token:
            return False
        return hmac.compare_digest(self.token, offered or "")

    # -- the dashboard -----------------------------------------------------

    def _dashboard(self, view: str, anchor: str, *, session: bool = False) -> Response:
        """Draw a day, week or month from the internal calendar (SPEC §5.5).

        The page is generated *and written*: it lands in `ui/` like every other
        artifact, so the file is there after the request that made it, and the
        front page becomes today's view. Reading the calendar on every request is
        the point — a cached dashboard is a dashboard that lies about today.

        `session` is whether the request carries a valid control credential. An
        authenticated view carries the controls; a read-only one does not, so a
        page fetched without the token has nothing on it that can act.
        """
        from .dashboard import calendar_page, write_page
        from .renderer import FORM_CSP

        if self.calendar is None:
            return Response(
                501,
                _PLAIN,
                b"/dashboard has nothing to draw: this server was built without a calendar\n",
            )
        try:
            page = calendar_page(
                self.calendar,
                view=view,
                anchor=anchor or None,
                with_controls=session,
                overrides=self.tuners.approved() if self.tuners is not None else None,
            )
        except ValueError as exc:
            return Response(400, _PLAIN, f"{exc}\n".encode("utf-8"))
        try:
            write_page(page, self.home, front=view == "day" and not anchor)
        except OSError as exc:
            # Serving the page matters more than keeping the copy: the owner asked
            # to *see* the calendar, and a full disk is not a reason to show them
            # nothing.
            logger.warning("could not write the dashboard artifact: %s", exc)
        return Response(
            200,
            _TYPES[".html"],
            page.html().encode("utf-8"),
            csp=FORM_CSP if session else None,
        )

    # -- the control route -------------------------------------------------

    def _action(self, body: bytes, content_type: str) -> Response:
        """`POST /action` — one control, through the approval split (SPEC §3.6)."""
        from .actions import Outcome

        try:
            control, args = _parse_control(body, content_type)
        except ValueError as exc:
            return Response(400, "application/json; charset=utf-8", f'{{"error": {json.dumps(str(exc))}}}\n'.encode("utf-8"))

        outcome = self.controls.perform(control, args)
        logger.info("control %s -> %s", control, outcome.status)
        if _wants_json(content_type):
            payload = {"status": outcome.status, "message": outcome.message}
            if outcome.detail:
                payload["detail"] = outcome.detail
            return Response(outcome.code, "application/json; charset=utf-8", (json.dumps(payload) + "\n").encode("utf-8"))
        # A form came from a browser, so the answer is a page the browser shows.
        return Response(
            outcome.code,
            _TYPES[".html"],
            _RESULT_PAGE.format(status=outcome.status, message=_escape(outcome.message)).encode("utf-8"),
        )

    # -- the theme's pending → approved (T9) --------------------------------

    def _draft(self, id: str, *, session: bool = False) -> Response:
        """`GET /draft/<id>` — the real page, rendered with a **pending** set.

        Seeing is the most accurate way to say yes (SPEC §3.6), so the preview is
        not a swatch: it is today's dashboard as the tune would render it, with
        the two choices right under it. The live theme does not move until one of
        them is taken.
        """
        from .dashboard import calendar_page, draft_strip

        if self.tuners is None or self.calendar is None:
            return _not_built("/draft", ROUTES[("GET", "/draft")])
        try:
            tokens = self.tuners.preview(id)
        except TuneError as exc:
            return Response(404, _PLAIN, f"{exc}\n".encode("utf-8"))
        page = calendar_page(self.calendar, view="day", tokens=tokens)
        if session:
            page = replace(page, controls=draft_strip(id), csp=FORM_CSP)
        return Response(200, _TYPES[".html"], page.html().encode("utf-8"), csp=page.csp)

    def _tune(self, body: bytes, content_type: str) -> Response:
        """`POST /tune` — one partial token set, into the **pending** set."""
        try:
            values, note = _parse_tune(body, content_type)
            draft = self.tuners.tune(values, note=note)
        except TuneError as exc:
            return _tune_answer(content_type, 400, "refused", str(exc))
        return _tune_answer(
            content_type,
            201,
            "pending",
            f"{draft.describes()} — see it at /draft/{draft.id}; the live theme is "
            "unchanged until you adopt it",
            draft=draft,
        )

    def _approve(self, body: bytes, content_type: str) -> Response:
        """`POST /approve` — adopt one pending tune, or discard it."""
        try:
            id, decision = _parse_approve(body, content_type)
            if decision not in ("approve", "deny"):
                raise TuneError(
                    f"decision is 'approve' or 'deny'; it is {decision!r} — the two "
                    "must never be one default apart"
                )
        except TuneError as exc:
            return _tune_answer(content_type, 400, "refused", str(exc))
        try:
            if decision == "deny":
                self.tuners.deny(id)
                return _tune_answer(content_type, 200, "discarded", f"{id} discarded — the theme is as it was")
            theme = self.tuners.approve(id)
        except TuneError as exc:
            return _tune_answer(content_type, 404, "refused", str(exc))
        return _tune_answer(
            content_type,
            200,
            "adopted",
            f"{id} adopted — the live theme is {theme.accent} on {theme.bg} now",
        )


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
            self._emit(app.get(self.path, cookie=self.headers.get("Cookie", "")))

        def do_HEAD(self) -> None:  # noqa: N802
            self._emit(app.get(self.path), body=False)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            self._emit(
                app.post(
                    self.path,
                    body,
                    token=self.headers.get(TOKEN_HEADER, ""),
                    cookie=self.headers.get("Cookie", ""),
                    content_type=self.headers.get("Content-Type", ""),
                )
            )

        def _emit(self, response: Response, *, body: bool = True) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in response.headers:
                self.send_header(name, value)
            if response.content_type == _TYPES[".html"]:
                # The policy is *in* the document, as a <meta> the renderer writes
                # — but a meta policy only begins to apply where it is parsed, so
                # the bytes before it are a document with no policy at all. Sent as
                # a header it covers the whole response, from the first byte.
                self.send_header("Content-Security-Policy", response.csp or CSP)
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

        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            # The query string is where a one-time control token travels; a log
            # is not a place for a secret, so the request is logged without it.
            stripped = self.requestline.split("?", 1)[0]
            self.log_message('"%s" %s %s', stripped, str(code), str(size))

    return Handler


# -- helpers ----------------------------------------------------------------


def _route(path: str) -> str:
    route = str(path or "/").split("?", 1)[0].split("#", 1)[0]
    if not route.startswith("/"):
        route = "/" + route
    return route if route == "/" else route.rstrip("/")


def _split_query(path: str) -> tuple[str, str]:
    """The route and the raw query, so a one-time token can be read and dropped."""
    whole = str(path or "/").split("#", 1)[0]
    route, _, query = whole.partition("?")
    if not route.startswith("/"):
        route = "/" + route
    return (route if route == "/" else route.rstrip("/")), query


def _query_token(query: str) -> str:
    """The control token a bootstrap URL carried, or nothing.

    Only ``t`` is read; anything else in the query is not this server's business.
    """
    from urllib.parse import parse_qs

    for value in parse_qs(query, keep_blank_values=True).get("t", []):
        if value.strip():
            return value.strip()
    return ""


def _parse_control(body: bytes, content_type: str) -> tuple[str, dict[str, Any]]:
    """A control and its arguments, from JSON or from a browser form.

    A form cannot set a header or nest objects, so its fields are flat: the
    control is the ``control`` field and every other field is an argument. The
    two spellings reach the same middleware.
    """
    text = body.decode("utf-8", "replace")
    if "json" in (content_type or "").lower():
        raw = json.loads(text or "{}")
        if not isinstance(raw, Mapping):
            raise ValueError("a control is an object with `control` and `args`")
        control = raw.get("control", "")
        args = raw.get("args", {})
        if not isinstance(control, str) or not isinstance(args, Mapping):
            raise ValueError("`control` is a string like 'calendar.delete' and `args` an object")
        return control.strip(), dict(args)
    from urllib.parse import parse_qs

    fields = {key: values[-1] for key, values in parse_qs(text, keep_blank_values=True).items()}
    control = str(fields.pop("control", "")).strip()
    if not control:
        raise ValueError("a control needs a `control` field, e.g. control=calendar.delete")
    return control, fields


def _parse_tune(body: bytes, content_type: str) -> tuple[dict[str, str], str]:
    """A tune and its note, from JSON (`values`) or from a flat form.

    A form cannot nest, so its fields *are* the token values, and every field
    that is not a known token is refused — a typo'd token name must not become a
    pending value nobody can see the meaning of.
    """
    from .theming import TOKEN_NAMES

    text = body.decode("utf-8", "replace")
    if "json" in (content_type or "").lower():
        raw = json.loads(text or "{}")
        if not isinstance(raw, Mapping):
            raise TuneError("a tune is an object with `values`")
        values = raw.get("values", {})
        note = str(raw.get("note", ""))
        if not isinstance(values, Mapping):
            raise TuneError("`values` is an object of token = value")
        return {str(k): v for k, v in values.items()}, note
    from urllib.parse import parse_qs

    fields = {k: v[-1] for k, v in parse_qs(text, keep_blank_values=True).items()}
    note = str(fields.pop("note", ""))
    unknown = sorted(set(fields) - set(TOKEN_NAMES))
    if unknown:
        raise TuneError(
            f"unknown token{'' if len(unknown) == 1 else 's'} {', '.join(map(repr, unknown))} — "
            f"a theme has: {', '.join(TOKEN_NAMES)}"
        )
    return fields, note


def _parse_approve(body: bytes, content_type: str) -> tuple[str, str]:
    """A draft id and the decision, from JSON or from a flat form.

    The decision is spelled, not implied: a POST that only carries an id is
    refused, because "approve" and "discard" must never be one default apart.
    """
    text = body.decode("utf-8", "replace")
    if "json" in (content_type or "").lower():
        raw = json.loads(text or "{}")
        if not isinstance(raw, Mapping):
            raise TuneError("an approval is an object with `id` and `decision`")
        id, decision = raw.get("id", ""), raw.get("decision", "")
        if not isinstance(id, str) or not isinstance(decision, str):
            raise TuneError("`id` and `decision` are strings")
        return id.strip(), decision.strip().lower()
    from urllib.parse import parse_qs

    fields = {k: v[-1] for k, v in parse_qs(text, keep_blank_values=True).items()}
    return str(fields.get("id", "")).strip(), str(fields.get("decision", "")).strip().lower()


def _tune_answer(
    content_type: str, code: int, status: str, message: str, *, draft: Any = None
) -> Response:
    """JSON for a caller that asked for it; a readable page for a browser."""
    if _wants_json(content_type):
        payload: dict[str, Any] = {"status": status, "message": message}
        if draft is not None:
            payload["id"] = draft.id
            payload["values"] = dict(draft.values)
        return Response(code, "application/json; charset=utf-8", (json.dumps(payload) + "\n").encode("utf-8"))
    return Response(
        code,
        _TYPES[".html"],
        _RESULT_PAGE.format(status=status, message=_escape(message)).encode("utf-8"),
    )


def _wants_json(content_type: str) -> bool:
    """Whether the caller asked for JSON back. A form gets a page it can read."""
    return "json" in (content_type or "").lower()


#: The browser's session: the control token, handed over once by the bootstrap
#: URL and then carried by the browser itself, so no rendered document and no
#: logged URL ever has to hold it.
COOKIE = "daedalus_control"
COOKIE_MAX_AGE = 7 * 24 * 3600


def _cookie_token(header: str) -> str:
    """The control token inside a ``Cookie`` header, or nothing.

    A header may carry several cookies, and only this one is this server's
    business — the rest are passed over, unread.
    """
    for pair in str(header or "").split(";"):
        name, _, value = pair.strip().partition("=")
        if name.strip() == COOKIE:
            return value.strip()
    return ""

_RESULT_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Daedalus — {status}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;margin:3rem auto;max-width:38rem;color:#e8e8ea;background:#141416}}
h1{{font-size:1.1rem;text-transform:capitalize}}.message{{white-space:pre-wrap}}a{{color:#8ab4f8}}</style>
<h1>{status}</h1>
<p class="message">{message}</p>
<p><a href="/dashboard">Back to the dashboard</a></p>
"""


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
