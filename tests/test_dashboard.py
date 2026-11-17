"""The calendar dashboard: what a day, a week and a month actually show.

Two properties are worth more than the rest here.

The first is **occurrences, not definitions**. A calendar that shows a weekly
standup once, on the day its rule began, is wrong on every other day of the year
— so the repeating-event test asks about *days*, and the month test asks about
days that are not the first of anything.

The second is that the page and the **spec** are different objects. The spec says
what to draw and holds keys; the owner's data is bound in at render time (§6).
A test that renders the page and finds the summary proves the second half only,
so it is paired with an assertion that the *spec* does not contain it.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime
from pathlib import Path

import pytest

from daedalus.calendar.store import CalendarStore
from daedalus.genui.dashboard import VIEWS, calendar_page, write_page
from daedalus.genui.server import LocalServer
from daedalus.security.provenance import DataClass

#: Friday 16 October 2026. The week around it runs Mon 12 → Sun 18.
FRIDAY = "2026-10-16"
NOW = datetime(2026, 10, 16, 12, 0)
MONDAY = "2026-10-12"


@pytest.fixture
def store(tmp_path: Path) -> CalendarStore:
    return CalendarStore(tmp_path / "calendar")


@pytest.fixture
def week(store: CalendarStore) -> CalendarStore:
    """A week with a one-off, a daily repeat, an all-day, and a withheld event."""
    store.add(summary="standup", start=f"{FRIDAY}T09:00", end=f"{FRIDAY}T09:15")
    store.add(summary="morning walk", start="2026-10-14T08:00", rrule="FREQ=DAILY")
    store.add(summary="trip", start="2026-10-17")
    store.add(
        summary="therapy",
        start=f"{FRIDAY}T16:00",
        end=f"{FRIDAY}T17:00",
        data_class=DataClass.LOCAL_ONLY,
    )
    return store


def page_of(store: CalendarStore, view: str = "day", anchor: str | None = FRIDAY):
    return calendar_page(store, view=view, anchor=anchor, now=NOW)


def at(html: str, clock: str, what: str) -> int:
    """Where a timeline entry sits. Time and title are separate spans, so a
    substring search for the two together would pass with them in the wrong
    order — or in the wrong block."""
    return html.index(
        f'timeline__when">{clock}</span><span class="timeline__what">{what}'
    )


#: The markup a marked day carries. The *stylesheet* names this class too, twice,
#: so counting the bare class would count the theme.
MARKED = 'class="calendar__day calendar__day--today"'


# -- the right events, on the right days -------------------------------------


def test_the_day_view_lists_the_right_day_in_time_order(
    week: CalendarStore, tmp_path: Path
) -> None:
    page = page_of(week)

    assert page.view == "day"
    assert page.anchor.isoformat() == FRIDAY
    assert "Friday 16 October" in page.html()
    assert "standup" in page.html()

    # "A schedule" means in time order. Anything else is a pile of entries.
    html = page.html()
    assert at(html, "08:00", "morning walk") < at(html, "09:00", "standup")
    assert at(html, "09:00", "standup") < at(html, "16:00", "therapy")

    # A timed event shows its clock; an all-day one says so instead.
    saturday = page_of(week, anchor="2026-10-17").html()
    assert "all day" in saturday
    assert "trip" in saturday

    # Events from another day are not on this one.
    tuesday = page_of(week, anchor="2026-10-13").html()
    assert "standup" not in tuesday
    assert "therapy" not in tuesday

    # An empty day still draws the day, and says it is empty rather than blank.
    blank = calendar_page(
        CalendarStore(tmp_path / "empty"), view="day", anchor=FRIDAY, now=NOW
    ).html()
    assert "Nothing on." in blank
    assert "calendar__day" in blank, "the day itself should still be drawn"


def test_a_repeating_event_appears_on_every_day_it_falls(week: CalendarStore) -> None:
    """The whole reason the view reads occurrences: a rule is not an event."""
    for day in ("2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"):
        assert "morning walk" in page_of(week, anchor=day).html(), f"missing on {day}"

    # A day before the rule begins is honestly empty rather than showing the
    # definition.
    assert "morning walk" not in page_of(week, anchor="2026-10-13").html()


def test_the_week_and_month_are_drawn_as_whole_weeks_and_counted(
    week: CalendarStore,
) -> None:
    here = page_of(week, view="week").html()

    assert "calendar--week" in here
    assert f">{MONDAY}<" in here, "the week did not start on Monday"
    assert ">2026-10-18<" in here, "the week did not end on Sunday"
    assert "morning walk" in here and "standup" in here
    # `1 standup + a week of walks + a trip + therapy` — counted, not listed.
    assert "This week" in here
    assert "8 things" in here, "the week's total was not reported"

    month = page_of(week, view="month").html()

    assert "calendar--month" in month
    # October 2026 begins on a Thursday and ends on a Saturday, so the grid runs
    # from Monday 28 September to Sunday 1 November.
    assert ">2026-09-28<" in month, "the first row is not a whole week"
    assert ">2026-11-01<" in month, "the last row is not a whole week"
    assert ">2026-10-01<" in month

    # The day that spills in from the neighbouring month exists; it is simply
    # not this month's. Neither hidden nor faked: a date, and nothing beside it.
    cell = month[month.index(">2026-09-28<") : month.index(">2026-09-28<") + 400]
    assert "calendar__date" in cell
    assert "calendar__what\"></span>" in cell

    assert "This month" in month
    assert " things" in month


def test_today_and_the_mood_come_from_the_clock_that_was_passed_in(
    week: CalendarStore,
) -> None:
    here = page_of(week, view="week").html()
    elsewhere = page_of(week, view="week", anchor="2026-11-02").html()

    assert here.count(MARKED) == 1
    assert elsewhere.count(MARKED) == 0

    # Otherwise this page would be a different page tomorrow, and untestable.
    pinned = calendar_page(week, view="week", anchor=FRIDAY, now=datetime(2020, 1, 1, 9, 0))
    assert pinned.values["page.today"] == "2020-01-01"
    assert MARKED not in pinned.html()

    # The standing dashboard composes by the hour — midnight is dark, noon is not.
    night = calendar_page(week, view="day", anchor=FRIDAY, now=datetime(2026, 10, 16, 22, 30))
    noon = page_of(week)
    assert night.mood == "midnight-focus" and noon.mood == "calm"
    assert "--d-scheme: dark;" in night.html(), "the page itself goes dark at 22:30"
    assert "--d-scheme: light;" in noon.html()

    # And a crowded day reads as dense ops.
    for i in range(4):
        week.add(summary=f"call {i}", start=f"{FRIDAY}T1{i}:00", end=f"{FRIDAY}T1{i}:30")
    crowded = calendar_page(
        week, view="day", anchor=FRIDAY, now=datetime(2026, 10, 16, 11, 0)
    )
    assert crowded.mood == "dense-ops"


def test_the_owners_data_reaches_the_page_but_never_the_spec(week: CalendarStore) -> None:
    page = page_of(week)
    spec = json.dumps(page.spec.to_dict())

    assert "standup" not in spec and "therapy" not in spec
    assert "bind" in spec, "the spec resolved its keys too early to be a spec"
    assert "standup" in page.html() and "therapy" in page.html()

    # §6 withholds from *egress*; hiding the owner's own day from the owner's own
    # surface would be the rule turned inside out.
    assert "therapy" in page_of(week, view="week").html()

    # And the page reaches for nothing: no script, no network, no import.
    html = page_of(week, view="week").html()
    for forbidden in ("<script", "http://", "https://", "url(", "@import"):
        assert forbidden not in html


# -- writing it out, and the server serving it -------------------------------


def test_each_view_is_written_under_its_own_name_and_the_front_page_is_chosen(
    week: CalendarStore, tmp_path: Path
) -> None:
    page = page_of(week, view="week")

    written = write_page(page, tmp_path)

    assert written == tmp_path / "ui" / "week-2026-10-16.html"
    assert written.read_text(encoding="utf-8") == page.html()
    assert not (tmp_path / "ui" / "index.html").exists(), "a view was made the front page"

    front = page_of(week)
    write_page(front, tmp_path, front=True)
    assert (tmp_path / "ui" / "index.html").read_text(encoding="utf-8") == front.html()


def test_the_server_serves_each_view_at_its_own_address(
    week: CalendarStore, tmp_path: Path
) -> None:
    # `/dashboard` draws *today*, whatever day the suite runs on, so the event
    # that proves the owner's data reached the page goes on today.
    week.add(summary="the plan for today", start=f"{date.today().isoformat()}T12:00")
    server = LocalServer(tmp_path, token="tok", calendar=week)

    dashboard = server.get("/dashboard")
    assert dashboard.status == 200
    assert "the plan for today" in dashboard.text
    index = tmp_path / "ui" / "index.html"
    assert index.read_text(encoding="utf-8") == dashboard.text

    # A view may be pinned to a day, and that address is its own page — it does
    # not quietly become the front page.
    pinned = server.get("/day/2026-10-17")
    assert pinned.status == 200
    assert "trip" in pinned.text
    assert (tmp_path / "ui" / "day-2026-10-17.html").exists()
    assert index.read_text(encoding="utf-8") == dashboard.text

    # Each view has an address of its own.
    for view in VIEWS:
        response = server.get(f"/{view}")
        assert response.status == 200
        assert f"calendar--{view}" in response.text

    # The dashboard is built; the writes the approval split owns still say what
    # they will be.
    assert server.post("/tune", b"{}", token="tok").status == 501

    # A server built without a calendar says exactly that, not "not built yet".
    plain = LocalServer(tmp_path, token="tok").get("/dashboard")
    assert plain.status == 501
    assert "without a calendar" in plain.text

    # And it answers over a real socket, which is the half a unit test of `get()`
    # cannot prove. The policy travels as a header as well as the <meta>: a meta
    # policy starts applying where it is parsed, leaving the head unprotected.
    live = LocalServer(tmp_path, token="tok", port=0, calendar=week)
    url = live.start()
    try:
        with urllib.request.urlopen(f"{url}/dashboard", timeout=5) as reply:
            body = reply.read().decode("utf-8")
            assert reply.status == 200
            assert reply.headers["Content-Security-Policy"].startswith("default-src 'none'")
    finally:
        live.stop()

    assert "the plan for today" in body
    assert (tmp_path / "ui" / "index.html").exists()


# -- refusals name the way out ----------------------------------------------


def test_a_bad_view_anchor_or_address_is_refused_rather_than_rendered(
    week: CalendarStore, tmp_path: Path
) -> None:
    with pytest.raises(ValueError) as unknown_view:
        calendar_page(week, view="year")
    assert "day | week | month" in str(unknown_view.value)

    # "I asked for a day and got today" is worse than a refusal — it looks right.
    with pytest.raises(ValueError) as bad_anchor:
        calendar_page(week, view="day", anchor="16th of October")
    assert "YYYY-MM-DD" in str(bad_anchor.value)

    # An empty anchor, by contrast, means today.
    assert calendar_page(week, view="day", anchor="", now=NOW).anchor.isoformat() == FRIDAY

    server = LocalServer(tmp_path, token="tok", calendar=week)

    bad_date = server.get("/day/2026-13-45")
    assert bad_date.status == 400
    assert "YYYY-MM-DD" in bad_date.text

    assert server.get("/day/not-a-date").status == 400
    assert server.get("/fortnight").status == 404


# -- the roots editor rides the authenticated strip (T35) --------------------


def test_the_control_strip_only_rides_an_authenticated_page(week: CalendarStore) -> None:
    page = calendar_page(
        week,
        view="day",
        anchor=FRIDAY,
        now=NOW,
        with_controls=True,
        roots=[str(Path.home() / "vault"), "/srv/web"],
    )

    assert 'value="settings.roots-add"' in page.controls
    assert 'value="settings.roots-remove"' in page.controls
    assert "/srv/web" in page.controls
    assert "asks on Discord before it stands" in page.controls
    assert page_of(week).controls == "", "an unauthenticated page proposes nothing"

    bare = calendar_page(week, view="day", anchor=FRIDAY, now=NOW, with_controls=True)
    assert "No folders beyond the always-allowed ones" in bare.controls
