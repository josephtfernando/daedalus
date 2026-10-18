"""Dashboard controls and the approval split (SPEC §3.6, ticket T8).

*"Visual → dashboard, action → Discord."* The page may propose; the middleware
disposes. Two ways to get this wrong are worth a test each:

- **The page is a bypass.** A control that computes its own answer to "may I"
  would be a second approval policy, and the weaker one wins by being the one a
  browser can reach. So the acceptance is a *comparison*: the same call driven
  through the tool's own path and through the page, and the two decisions must
  agree — including when the answer is *no*.
- **Waiting is not hanging.** A control that needs the owner must not hold an
  HTTP request open for the ten minutes a person takes to press a button: the
  answer is "waiting", and the buttons are surfaced where the owner already is.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from daedalus.approvals.risk import Risk
from daedalus.genui.actions import CONTROLS, Controls
from daedalus.genui.dashboard import calendar_page
from daedalus.genui.server import COOKIE, ROUTES, LocalServer
from daedalus.genui.renderer import CSP, FORM_CSP
from daedalus.runtime import Runtime
from daedalus.tools.base import Tool, ToolResult

TOKEN = "a-token-the-owner-never-sees"
#: The Friday these tests are set on — the **next** one, so that a `calendar.create`
#: from the page is a write into the future and the suite does not mean something
#: different on 17 Oct than it did on 10 Oct. Hard-coding `2026-10-16` is how a
#: create into the past sat here unremarked until a guard with every right to
#: refuse it arrived.
_TODAY = date.today()
FRIDAY_DATE = _TODAY + timedelta(days=(4 - _TODAY.weekday()) % 7 or 7)
FRIDAY = FRIDAY_DATE.isoformat()
MONDAY = (FRIDAY_DATE - timedelta(days=4)).isoformat()
NEXT_FRIDAY = (FRIDAY_DATE + timedelta(days=7)).isoformat()


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path, runtime: Runtime) -> LocalServer:
    from daedalus.genui.actions import Controls

    return LocalServer(
        tmp_path,
        token=TOKEN,
        port=0,
        calendar=runtime.calendar,
        controls=Controls(runtime),
    )


def _add(runtime: Runtime, summary: str, start: str = f"{FRIDAY}T09:00") -> str:
    return runtime.calendar.add(summary=summary, start=start).uid


# -- the closed vocabulary ---------------------------------------------------


def test_the_controls_are_the_three_writes_and_nothing_else() -> None:
    assert CONTROLS == frozenset(
        {
            "calendar.create",
            "calendar.update",
            "calendar.delete",
            "settings.roots-add",
            "settings.roots-remove",
        }
    )


def test_a_read_is_not_a_control(server: LocalServer) -> None:
    """The page *is* the read; a control is only ever a write."""
    outcome = server.controls.perform("calendar.agenda", {})

    assert outcome.status == "refused"
    assert outcome.code == 400


def test_another_tool_is_not_reachable_as_a_control(server: LocalServer) -> None:
    """The page cannot propose `shell` or `http` because it has no name for them."""
    outcome = server.controls.perform("shell.run", {"command": "rm -rf ~"})

    assert outcome.status == "refused"
    assert outcome.code == 400
    assert "not a control" in outcome.message


def test_a_control_that_names_a_tool_nobody_has_is_refused(tmp_path: Path) -> None:
    from daedalus.genui.actions import Controls

    app = LocalServer(tmp_path, token=TOKEN, port=0, controls=Controls(_bare(tmp_path)))

    assert app.controls.perform("calendar.delete", {"uid": "x"}).status == "refused"


def _bare(tmp_path: Path) -> Any:
    """A stand-in runtime with the middleware but no tools, for the refusal path."""

    class Bare:
        from daedalus.approvals.middleware import ApprovalMiddleware
        from daedalus.config import Settings

        settings = Settings(home=tmp_path, env={}, heavy=[], cheap=None)
        approvals = ApprovalMiddleware(settings)
        tools = type("R", (), {"get": staticmethod(lambda name: None)})()
        calendar = None
        surface = None

    return Bare()


# -- the acceptance: both paths, compared ------------------------------------


@dataclass
class GateTool(Tool):
    """A calendar-shaped tool with a risk tier the test chooses."""

    name = "calendar"
    title = "Read or change the calendar"
    risk = Risk.AUTO
    scopes: dict[str, Any] = field(default_factory=lambda: {"fs": ["state"]})
    tier: Risk = Risk.AUTO
    ran: list[dict[str, Any]] = field(default_factory=list)

    def risk_for(self, args: dict[str, Any]) -> Risk:
        return self.tier

    async def run(self, args: dict[str, Any], ctx: Runtime) -> ToolResult:
        self.ran.append(dict(args))
        return ToolResult(output=f"did {args.get('op')}")


def _tool_path_decision(runtime: Runtime, tool: Tool, args: dict[str, Any]) -> Any:
    """Exactly what `Engine._execute` does before running a tool call."""
    return runtime.approvals.authorize(
        tool.name,
        args,
        risk=tool.risk_for(args),
        scopes=tool.scopes,
        assessment=tool.assess(args),
    )


@pytest.mark.parametrize(
    "op,args",
    [
        ("create", {"summary": "Dentist", "start": f"{FRIDAY}T09:30"}),
        ("update", {"uid": "whatever", "start": f"{FRIDAY}T10:30"}),
        ("delete", {"uid": "whatever"}),
    ],
)
def test_the_page_gets_the_decision_the_tool_gets(runtime: Runtime, op: str, args: dict) -> None:
    """The acceptance, driven both ways: same middleware, same verdict."""
    tool = GateTool(tier=Risk.AUTO)
    runtime.tools.register(tool)
    controls = Controls(runtime)

    from_tool = _tool_path_decision(runtime, tool, {**args, "op": op})
    outcome = controls.perform(f"calendar.{op}", args)

    if from_tool.allow:
        expected = ("done", 200)
    elif from_tool.ask:
        expected = ("waiting", 202)
    else:
        expected = ("refused", 403)
    assert (outcome.status, outcome.code) == expected
    assert from_tool.action == "calendar"


def test_a_refused_tool_is_a_refused_control(runtime: Runtime) -> None:
    """The half of the acceptance that matters: the page cannot go around a *no*."""
    tool = GateTool(tier=Risk.BLOCK)
    runtime.tools.register(tool)
    controls = Controls(runtime)

    from_tool = _tool_path_decision(runtime, tool, {"op": "delete", "uid": "whatever"})
    outcome = controls.perform("calendar.delete", {"uid": "whatever"})

    assert from_tool.blocked
    assert outcome.status == "refused" and outcome.code == 403
    assert "refused by policy" in outcome.message
    assert tool.ran == [], "a blocked control must not run"


def test_an_ask_on_the_tool_path_is_a_refusal_without_a_surface(runtime: Runtime) -> None:
    """No surface attached: the honest answer is that nothing can ask, not a guess."""
    tool = GateTool(tier=Risk.ASK)
    runtime.tools.register(tool)

    outcome = Controls(runtime).perform("calendar.delete", {"uid": "whatever"})

    assert outcome.status == "refused" and outcome.code == 503
    assert tool.ran == []


# -- the loop: waiting, not hanging ------------------------------------------


class Recorder:
    """A surface that records what was asked, and answers on demand."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.sent: list[str] = []
        self.answer = True

    async def send(self, text: str, *, thread: str | None = None) -> None:
        self.sent.append(text)

    async def ask(self, question: str, *, options: list[str] | None = None) -> str:
        return ""

    async def approve(self, request: Any) -> bool:
        self.requests.append(request)
        return self.answer


@dataclass
class Loop:
    """A real running loop in another thread, as the daemon has."""

    def __enter__(self) -> asyncio.AbstractEventLoop:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *_: Any) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def _settle(condition: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("the daemon never caught up")


def test_a_control_that_needs_the_owner_says_waiting_and_asks_in_discord(
    runtime: Runtime,
) -> None:
    surface = Recorder()
    runtime.set_surface(surface)  # type: ignore[arg-type]
    tool = GateTool(tier=Risk.ASK)
    runtime.tools.register(tool)

    with Loop() as loop:
        outcome = Controls(runtime, loop=loop).perform(
            "calendar.delete", {"uid": "whatever"}
        )
        assert outcome.status == "waiting" and outcome.code == 202
        assert "waiting" in outcome.message
        assert tool.ran == [], "a control waits for the owner; it does not run first"
        _settle(lambda: bool(surface.requests))

    request = surface.requests[0]
    assert request.action == "calendar"
    assert request.args["op"] == "delete"
    assert tool.ran and tool.ran[0]["op"] == "delete", "approved, so it ran"


def test_a_denied_control_runs_nothing_and_says_so(runtime: Runtime) -> None:
    surface = Recorder()
    surface.answer = False
    runtime.set_surface(surface)  # type: ignore[arg-type]
    tool = GateTool(tier=Risk.ASK)
    runtime.tools.register(tool)

    with Loop() as loop:
        Controls(runtime, loop=loop).perform("calendar.delete", {"uid": "whatever"})
        _settle(lambda: bool(surface.sent))

    assert tool.ran == [], "a denial is a no"
    assert "nothing ran" in surface.sent[0].lower()


def test_an_approved_control_is_remembered_like_a_conversation_approval(
    runtime: Runtime,
) -> None:
    surface = Recorder()
    runtime.set_surface(surface)  # type: ignore[arg-type]
    tool = GateTool(tier=Risk.ASK)
    runtime.tools.register(tool)

    with Loop() as loop:
        Controls(runtime, loop=loop).perform("calendar.delete", {"uid": "whatever"})
        _settle(lambda: bool(runtime.approvals.grants.all()))

    assert runtime.approvals.grants.all(), "a yes on a button is a yes in the store"


# -- the real tool, end to end ----------------------------------------------


def test_a_delete_from_the_page_removes_the_event(server: LocalServer, runtime: Runtime) -> None:
    uid = _add(runtime, "Standup")

    outcome = server.controls.perform("calendar.delete", {"uid": uid})

    assert outcome.status == "done"
    assert runtime.calendar.get(uid) is None


def test_an_instance_is_removed_by_its_address(runtime: Runtime) -> None:
    """The picker offers `uid@start` — the same address the agenda prints."""
    uid = _add(runtime, "Standup", start=f"{FRIDAY}T09:00")
    runtime.calendar.update(uid, rrule="FREQ=WEEKLY;BYDAY=FR")
    page = calendar_page(runtime.calendar, view="day", anchor=FRIDAY, with_controls=True)
    assert "Standup" in page.controls

    instance = next(e for e in runtime.calendar.occurrences_on(FRIDAY) if e.recurrence_id)
    outcome = Controls(runtime).perform("calendar.delete", {"uid": instance.address})

    assert outcome.status == "done"
    assert runtime.calendar.occurrences_on(FRIDAY) == [], "that instance went"
    next_week = runtime.calendar.occurrences_on(NEXT_FRIDAY)
    assert [e.address for e in next_week] == [
        f"{uid}@{NEXT_FRIDAY}T09:00"
    ], "and only that instance — the series is untouched"


def test_a_malformed_control_is_the_tool_s_own_refusal(
    server: LocalServer, runtime: Runtime
) -> None:
    outcome = server.controls.perform("calendar.delete", {"uid": ""})

    assert outcome.status == "refused" and outcome.code == 400
    assert "uid" in outcome.message, "the tool's own message, passed through"


def test_a_create_from_the_page_adds_the_event(server: LocalServer, runtime: Runtime) -> None:
    outcome = server.controls.perform(
        "calendar.create", {"summary": "Dentist", "start": f"{FRIDAY}T09:30"}
    )

    assert outcome.status == "done"
    assert any(e.summary == "Dentist" for e in runtime.calendar.occurrences_on(FRIDAY))


# -- over HTTP ---------------------------------------------------------------


def test_a_delete_without_the_token_is_refused_and_removes_nothing(
    server: LocalServer, runtime: Runtime
) -> None:
    uid = _add(runtime, "Standup")
    body = json.dumps({"control": "calendar.delete", "args": {"uid": uid}}).encode()

    assert server.post("/action", body).status == 401
    assert runtime.calendar.get(uid) is not None, "an unauthorised control changed nothing"


def test_a_delete_with_the_token_runs_and_answers_json(
    server: LocalServer, runtime: Runtime
) -> None:
    uid = _add(runtime, "Standup")
    body = json.dumps({"control": "calendar.delete", "args": {"uid": uid}}).encode()

    response = server.post("/action", body, token=TOKEN, content_type="application/json")

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["status"] == "done"
    assert runtime.calendar.get(uid) is None


def test_a_form_control_is_parsed_and_answered_with_a_page(
    server: LocalServer, runtime: Runtime
) -> None:
    """A browser cannot set a header; its flat form reaches the same middleware."""
    uid = _add(runtime, "Standup")

    response = server.post(
        "/action",
        f"control=calendar.delete&uid={uid}".encode(),
        token=TOKEN,
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status == 200
    assert runtime.calendar.get(uid) is None
    assert "done" in response.text and "/dashboard" in response.text


def test_a_form_that_names_no_control_is_refused(server: LocalServer) -> None:
    response = server.post(
        "/action",
        b"uid=whatever",
        token=TOKEN,
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status == 400


def test_a_control_with_a_wrong_token_is_refused(server: LocalServer) -> None:
    body = json.dumps({"control": "calendar.delete", "args": {"uid": "x"}}).encode()

    assert server.post("/action", body, token="not-the-token").status == 401


def test_a_server_without_controls_says_so_rather_than_guessing(tmp_path: Path) -> None:
    """`daedalus serve` is a viewer; the split lives with the daemon."""
    app = LocalServer(tmp_path, token=TOKEN, port=0)
    body = json.dumps({"control": "calendar.delete", "args": {"uid": "x"}}).encode()

    response = app.post("/action", body, token=TOKEN, content_type="application/json")

    assert response.status == 501
    assert "daemon" in response.text


def test_the_route_table_still_answers_for_a_server_without_controls(tmp_path: Path) -> None:
    assert ("POST", "/action") in ROUTES


# -- the session: one bootstrap, then a cookie ------------------------------


def test_the_bootstrap_sets_a_cookie_and_leaves_the_token_out_of_the_page(
    server: LocalServer,
) -> None:
    response = server.get(f"/dashboard?t={TOKEN}")

    assert response.status == 303
    headers = dict(response.headers)
    assert headers["Location"] == "/dashboard"
    assert f"{COOKIE}={TOKEN}" in headers["Set-Cookie"]
    assert "HttpOnly" in headers["Set-Cookie"]


def test_a_wrong_bootstrap_token_is_refused(server: LocalServer) -> None:
    assert server.get("/dashboard?t=nope").status == 403


def test_a_page_without_a_session_has_no_controls_and_the_strict_policy(
    server: LocalServer, runtime: Runtime
) -> None:
    _add(runtime, "Standup")

    response = server.get("/dashboard")

    assert response.status == 200
    assert "<form" not in response.text
    assert response.csp is None, "the server sends the strict default"


def test_a_page_with_a_session_carries_the_controls_and_the_form_policy(
    server: LocalServer, runtime: Runtime
) -> None:
    _add(runtime, "Standup")

    response = server.get("/dashboard", cookie=f"{COOKIE}={TOKEN}")

    assert response.status == 200
    assert "<form" in response.text
    assert response.csp == FORM_CSP
    assert "calendar.delete" in response.text
    assert TOKEN not in response.text, "the token never enters a document"


def test_a_control_ride_on_the_cookie(server: LocalServer, runtime: Runtime) -> None:
    uid = _add(runtime, "Standup")
    body = json.dumps({"control": "calendar.delete", "args": {"uid": uid}}).encode()

    response = server.post(
        "/action", body, cookie=f"{COOKIE}={TOKEN}", content_type="application/json"
    )

    assert response.status == 200
    assert runtime.calendar.get(uid) is None


def test_the_written_copy_is_the_view_and_the_token_is_nowhere_in_it(
    server: LocalServer, runtime: Runtime, tmp_path: Path
) -> None:
    _add(runtime, "Standup")
    server.get("/dashboard", cookie=f"{COOKIE}={TOKEN}")

    copies = list((tmp_path / "ui").glob("*.html"))
    assert copies, "the dashboard was not written"
    for copy in copies:
        assert TOKEN not in copy.read_text(encoding="utf-8")


# -- the page the model did not compose --------------------------------------


def test_the_control_strip_is_outside_the_spec(server: LocalServer, runtime: Runtime) -> None:
    """`page.spec.to_dict()` must stay free of controls: the model never sees them."""
    _add(runtime, "Standup")
    page = calendar_page(runtime.calendar, view="day", anchor=FRIDAY, with_controls=True)

    assert page.controls
    assert "control" not in json.dumps(page.spec.to_dict())


def test_a_week_view_offers_adding_but_not_a_picker(runtime: Runtime) -> None:
    _add(runtime, "Standup", start=f"{MONDAY}T09:00")
    page = calendar_page(runtime.calendar, view="week", anchor=FRIDAY, with_controls=True)

    assert "calendar.create" in page.controls
    assert "calendar.delete" not in page.controls
    assert "Open a day" in page.controls


def test_a_read_only_page_has_no_strip_at_all(runtime: Runtime) -> None:
    _add(runtime, "Standup")
    page = calendar_page(runtime.calendar, view="day", anchor=FRIDAY)

    assert page.controls == ""
    assert page.csp == CSP


def test_the_strip_escapes_what_the_calendar_holds(runtime: Runtime) -> None:
    """A summary is the owner's words, but it is still rendered into a form."""
    import html

    raw = "<script>alert(1)</script>"
    _add(runtime, raw, start=f"{FRIDAY}T09:00")
    page = calendar_page(runtime.calendar, view="day", anchor=FRIDAY, with_controls=True)

    assert "<script>" not in page.controls
    assert html.escape(raw, quote=True) in page.controls, (
        "escaped, not stripped - the words still read"
    )


def test_a_control_tool_call_fits_the_engine_shape(runtime: Runtime) -> None:
    """The same args the control composes are a legal `calendar` tool call."""
    uid = _add(runtime, "Standup")
    args = {"op": "delete", "uid": uid}
    tool = runtime.tools.get("calendar")

    from_tool = _tool_path_decision(runtime, tool, args)

    assert from_tool.allow
