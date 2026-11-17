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
import html
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from daedalus.approvals.risk import Risk
from daedalus.genui.actions import Controls
from daedalus.genui.dashboard import calendar_page
from daedalus.genui.renderer import CSP, FORM_CSP
from daedalus.genui.server import COOKIE, LocalServer
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


def test_the_page_may_only_propose_the_writes_it_has_names_for(
    server: LocalServer, tmp_path: Path
) -> None:
    # The page *is* the read; a control is only ever a write.
    read = server.controls.perform("calendar.agenda", {})
    assert (read.status, read.code) == ("refused", 400)

    # The page cannot propose `shell` or `http` because it has no name for them.
    shell = server.controls.perform("shell.run", {"command": "rm -rf ~"})
    assert shell.status == "refused"
    assert shell.code == 400
    assert "not a control" in shell.message

    # A control naming a tool this install does not have is refused too.
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


def test_the_page_gets_exactly_the_decision_the_tool_gets(runtime: Runtime) -> None:
    tool = GateTool(tier=Risk.AUTO)
    runtime.tools.register(tool)
    controls = Controls(runtime)

    # Same middleware, same verdict — for each write the strip can propose.
    for op, args in (
        ("create", {"summary": "Dentist", "start": f"{FRIDAY}T09:30"}),
        ("update", {"uid": "whatever", "start": f"{FRIDAY}T10:30"}),
        ("delete", {"uid": "whatever"}),
    ):
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

    # The half of the acceptance that matters: the page cannot go around a *no*.
    ran_before = list(tool.ran)
    tool.tier = Risk.BLOCK
    from_tool = _tool_path_decision(runtime, tool, {"op": "delete", "uid": "whatever"})
    outcome = controls.perform("calendar.delete", {"uid": "whatever"})

    assert from_tool.blocked
    assert outcome.status == "refused" and outcome.code == 403
    assert "refused by policy" in outcome.message
    assert tool.ran == ran_before, "a blocked control must not run"

    # No surface attached: the honest answer is that nothing can ask, not a guess.
    tool.tier = Risk.ASK
    outcome = controls.perform("calendar.delete", {"uid": "whatever"})
    assert outcome.status == "refused" and outcome.code == 503
    assert tool.ran == ran_before


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
        _settle(lambda: bool(runtime.approvals.grants.all()))

    request = surface.requests[0]
    assert request.action == "calendar"
    assert request.args["op"] == "delete"
    assert tool.ran and tool.ran[0]["op"] == "delete", "approved, so it ran"
    assert runtime.approvals.grants.all(), "a yes on a button is a yes in the store"


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


# -- the real tool, end to end ----------------------------------------------


def test_the_real_calendar_tool_answers_a_control_the_way_it_answers_a_call(
    server: LocalServer, runtime: Runtime
) -> None:
    uid = _add(runtime, "Standup")

    # The args the control composes are a legal `calendar` tool call, and the
    # tool's own path allows it — the page is not asking for more than the loop.
    assert _tool_path_decision(
        runtime, runtime.tools.get("calendar"), {"op": "delete", "uid": uid}
    ).allow

    outcome = server.controls.perform("calendar.delete", {"uid": uid})

    assert outcome.status == "done"
    assert runtime.calendar.get(uid) is None

    # A series instance is removed by the address the picker offers — the same
    # address the agenda prints — and the rest of the series is untouched.
    series = _add(runtime, "Weekly", start=f"{FRIDAY}T09:00")
    runtime.calendar.update(series, rrule="FREQ=WEEKLY;BYDAY=FR")
    page = calendar_page(runtime.calendar, view="day", anchor=FRIDAY, with_controls=True)
    assert "Weekly" in page.controls

    instance = next(e for e in runtime.calendar.occurrences_on(FRIDAY) if e.recurrence_id)
    instance_outcome = Controls(runtime).perform(
        "calendar.delete", {"uid": instance.address}
    )
    assert instance_outcome.status == "done"
    assert runtime.calendar.occurrences_on(FRIDAY) == [], "that instance went"
    assert [e.address for e in runtime.calendar.occurrences_on(NEXT_FRIDAY)] == [
        f"{series}@{NEXT_FRIDAY}T09:00"
    ], "and only that instance — the series is untouched"

    # And a create from the page adds the event.
    created = server.controls.perform(
        "calendar.create", {"summary": "Dentist", "start": f"{NEXT_FRIDAY}T09:30"}
    )
    assert created.status == "done"
    assert any(e.summary == "Dentist" for e in runtime.calendar.occurrences_on(NEXT_FRIDAY))

    # A malformed control is the tool's own refusal, passed through.
    malformed = server.controls.perform("calendar.delete", {"uid": ""})
    assert malformed.status == "refused" and malformed.code == 400
    assert "uid" in malformed.message, "the tool's own message, passed through"


# -- over HTTP, and the session ---------------------------------------------


def test_a_control_over_http_needs_the_token_and_a_session_is_handed_a_cookie(
    server: LocalServer, runtime: Runtime, tmp_path: Path
) -> None:
    # The one bootstrap: the token is verified and the browser gets a cookie, so
    # no document and no logged URL has to hold it.
    bootstrapped = server.get(f"/dashboard?t={TOKEN}")
    assert bootstrapped.status == 303
    headers = dict(bootstrapped.headers)
    assert headers["Location"] == "/dashboard"
    assert f"{COOKIE}={TOKEN}" in headers["Set-Cookie"]
    assert "HttpOnly" in headers["Set-Cookie"]
    assert server.get("/dashboard?t=nope").status == 403

    uid = _add(runtime, "Standup")
    body = json.dumps({"control": "calendar.delete", "args": {"uid": uid}}).encode()

    assert server.post("/action", body).status == 401
    assert runtime.calendar.get(uid) is not None, "an unauthorised control changed nothing"
    assert server.post("/action", body, token="not-the-token").status == 401

    done = server.post("/action", body, token=TOKEN, content_type="application/json")
    assert done.status == 200
    assert json.loads(done.text)["status"] == "done"
    assert runtime.calendar.get(uid) is None

    # A browser cannot set a header; its flat form reaches the same middleware
    # and gets a page back.
    uid = _add(runtime, "Standup")
    form = server.post(
        "/action",
        f"control=calendar.delete&uid={uid}".encode(),
        token=TOKEN,
        content_type="application/x-www-form-urlencoded",
    )
    assert form.status == 200
    assert runtime.calendar.get(uid) is None
    assert "done" in form.text and "/dashboard" in form.text

    # A form that names no control is refused.
    assert (
        server.post(
            "/action",
            b"uid=whatever",
            token=TOKEN,
            content_type="application/x-www-form-urlencoded",
        ).status
        == 400
    )

    # `daedalus serve` is a viewer; the split lives with the daemon, and a server
    # without controls says so rather than guessing.
    viewer = LocalServer(tmp_path, token=TOKEN, port=0)
    unbuilt = viewer.post("/action", body, token=TOKEN, content_type="application/json")
    assert unbuilt.status == 501
    assert "daemon" in unbuilt.text


def test_a_page_has_controls_only_with_a_session_and_a_token_never_enters_it(
    server: LocalServer, runtime: Runtime, tmp_path: Path
) -> None:
    _add(runtime, "Standup")

    read_only = server.get("/dashboard")
    assert read_only.status == 200
    assert "<form" not in read_only.text
    assert read_only.csp is None, "the server sends the strict default"

    signed_in = server.get("/dashboard", cookie=f"{COOKIE}={TOKEN}")
    assert signed_in.status == 200
    assert "<form" in signed_in.text
    assert signed_in.csp == FORM_CSP
    assert "calendar.delete" in signed_in.text
    assert TOKEN not in signed_in.text, "the token never enters a document"

    # The written copy is the view, and the token is nowhere in it.
    copies = list((tmp_path / "ui").glob("*.html"))
    assert copies, "the dashboard was not written"
    for copy in copies:
        assert TOKEN not in copy.read_text(encoding="utf-8")

    # A control may ride the cookie the bootstrap handed the browser.
    uid = _add(runtime, "Standup")
    body = json.dumps({"control": "calendar.delete", "args": {"uid": uid}}).encode()
    rode = server.post(
        "/action", body, cookie=f"{COOKIE}={TOKEN}", content_type="application/json"
    )
    assert rode.status == 200
    assert runtime.calendar.get(uid) is None


# -- the page the model did not compose --------------------------------------


def test_the_strip_lives_outside_the_spec_and_escapes_the_calendar(
    server: LocalServer, runtime: Runtime
) -> None:
    _add(runtime, "Standup")
    page = calendar_page(runtime.calendar, view="day", anchor=FRIDAY, with_controls=True)

    assert page.controls
    assert "control" not in json.dumps(page.spec.to_dict()), "the model never sees them"

    # A week view offers adding but not a picker over a grid of summaries.
    _add(runtime, "Morning standup", start=f"{MONDAY}T09:00")
    week = calendar_page(runtime.calendar, view="week", anchor=FRIDAY, with_controls=True)
    assert "calendar.create" in week.controls
    assert "calendar.delete" not in week.controls
    assert "Open a day" in week.controls

    # A read-only page has no strip at all, and keeps the strict policy.
    plain = calendar_page(runtime.calendar, view="day", anchor=FRIDAY)
    assert plain.controls == ""
    assert plain.csp == CSP

    # A summary is the owner's words, but it is still rendered into a form.
    raw = "<script>alert(1)</script>"
    _add(runtime, raw, start=f"{FRIDAY}T09:00")
    escaped = calendar_page(runtime.calendar, view="day", anchor=FRIDAY, with_controls=True)
    assert "<script>" not in escaped.controls
    assert html.escape(raw, quote=True) in escaped.controls, (
        "escaped, not stripped - the words still read"
    )
