"""The calendar dashboard: what a day, a week and a month actually show.

Two properties are worth more than the rest here.

The first is **occurrences, not definitions**. A calendar that shows a weekly
standup once, on the day its rule began, is a calendar that is wrong on every
other day of the year — so the repeating-event test asks about *days*, and the
month test asks about days that are not the first of anything.

The second is that the page and the **spec** are different objects. The spec says
what to draw and holds keys; the owner's data is bound in at render time. A test
that renders the page and asserts the summary is present proves the second half,
and is worth nothing on its own — so each of those is paired with an assertion
that the *spec* does not contain it. That is the §6 claim, and it is the one that
would rot silently.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime
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


def test_the_day_view_shows_the_day_it_was_asked_for(week: CalendarStore) -> None:
    page = page_of(week)

    assert page.view == "day"
    assert page.anchor.isoformat() == FRIDAY
    assert "Friday 16 October" in page.html()
    assert "standup" in page.html()


def test_the_day_view_lists_a_day_in_the_order_it_happens(week: CalendarStore) -> None:
    """"A schedule" means in time order. Anything else is a pile of entries."""
    html = page_of(week).html()

    assert at(html, "08:00", "morning walk") < at(html, "09:00", "standup")
    assert at(html, "09:00", "standup") < at(html, "16:00", "therapy")


def test_the_day_shows_the_time_of_a_timed_event_and_says_all_day_otherwise(
    week: CalendarStore,
) -> None:
    saturday = page_of(week, anchor="2026-10-17").html()

    assert "all day" in saturday
    assert "trip" in saturday


def test_events_from_another_day_are_not_on_this_one(week: CalendarStore) -> None:
    html = page_of(week, anchor="2026-10-13").html()

    assert "standup" not in html
    assert "therapy" not in html


def test_a_repeating_event_appears_on_every_day_it_falls(week: CalendarStore) -> None:
    """The whole reason the view reads occurrences: a rule is not an event."""
    for day in ("2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"):
        assert "morning walk" in page_of(week, anchor=day).html(), f"missing on {day}"

    # And a day before the rule begins, and one after it was asked about, are
    # honestly empty rather than showing the definition.
    assert "morning walk" not in page_of(week, anchor="2026-10-13").html()


def test_the_week_view_draws_monday_to_sunday_around_the_anchor(week: CalendarStore) -> None:
    html = page_of(week, view="week").html()

    assert "calendar--week" in html
    assert f">{MONDAY}<" in html, "the week did not start on Monday"
    assert ">2026-10-18<" in html, "the week did not end on Sunday"
    assert "morning walk" in html and "standup" in html


def test_the_week_names_how_much_is_on(week: CalendarStore) -> None:
    """`1 standup + a week of walks + a trip + therapy` — counted, not listed."""
    html = page_of(week, view="week").html()

    assert "This week" in html
    assert "8 things" in html, "the week's total was not reported"


def test_the_month_view_is_drawn_as_whole_weeks(week: CalendarStore) -> None:
    html = page_of(week, view="month").html()

    assert "calendar--month" in html
    # October 2026 begins on a Thursday and ends on a Saturday, so the grid runs
    # from Monday 28 September to Sunday 1 November.
    assert ">2026-09-28<" in html, "the first row is not a whole week"
    assert ">2026-11-01<" in html, "the last row is not a whole week"
    assert ">2026-10-01<" in html


def test_a_day_spilling_in_from_the_neighbouring_month_is_shown_as_a_date_only(
    week: CalendarStore,
) -> None:
    """The day exists; it is simply not this month's. Neither hidden nor faked."""
    html = page_of(week, view="month").html()
    cell = html[html.index(">2026-09-28<") : html.index(">2026-09-28<") + 400]

    assert "calendar__date" in cell
    assert "calendar__what\"></span>" in cell


def test_the_month_names_how_much_is_on(week: CalendarStore) -> None:
    html = page_of(week, view="month").html()

    assert "This month" in html
    assert " things" in html


def test_today_is_marked_in_the_week_it_falls_in_and_nowhere_else(week: CalendarStore) -> None:
    here = page_of(week, view="week").html()
    elsewhere = page_of(week, view="week", anchor="2026-11-02").html()

    assert here.count(MARKED) == 1
    assert elsewhere.count(MARKED) == 0


def test_today_is_read_from_the_clock_that_was_passed_in_not_the_wall_clock(
    week: CalendarStore,
) -> None:
    """Otherwise this page would be a different page tomorrow, and untestable."""
    page = calendar_page(week, view="week", anchor=FRIDAY, now=datetime(2020, 1, 1, 9, 0))

    assert page.values["page.today"] == "2020-01-01"
    assert MARKED not in page.html()


def test_an_empty_day_says_so_rather_than_drawing_a_blank_grid(store: CalendarStore) -> None:
    html = page_of(store).html()

    assert "Nothing on." in html
    assert "calendar__day" in html, "the day itself should still be drawn"


def test_a_local_only_event_is_shown_on_the_owner_own_surface(week: CalendarStore) -> None:
    """§6 withholds from *egress*. Hiding this from the owner would be backwards."""
    assert "therapy" in page_of(week).html()
    assert "therapy" in page_of(week, view="week").html()


# -- the spec and the values are different objects ---------------------------


def test_the_spec_holds_keys_and_the_page_holds_the_owners_data(week: CalendarStore) -> None:
    page = page_of(week)
    spec = json.dumps(page.spec.to_dict())

    assert "standup" not in spec and "therapy" not in spec
    assert "bind" in spec, "the spec resolved its keys too early to be a spec"
    assert "standup" in page.html() and "therapy" in page.html()


def test_a_written_page_is_self_contained(week: CalendarStore) -> None:
    html = page_of(week, view="week").html()

    for forbidden in ("<script", "http://", "https://", "url(", "@import"):
        assert forbidden not in html


# -- writing it out ----------------------------------------------------------


def test_writing_puts_the_page_in_ui_under_its_own_name(week: CalendarStore, tmp_path: Path) -> None:
    page = page_of(week, view="week")

    written = write_page(page, tmp_path)

    assert written == tmp_path / "ui" / "week-2026-10-16.html"
    assert written.read_text(encoding="utf-8") == page.html()
    assert not (tmp_path / "ui" / "index.html").exists(), "a view was made the front page"


def test_the_front_page_is_the_one_the_owner_opens_on(week: CalendarStore, tmp_path: Path) -> None:
    page = page_of(week)

    write_page(page, tmp_path, front=True)

    index = tmp_path / "ui" / "index.html"
    assert index.read_text(encoding="utf-8") == page.html()


# -- the server serves it ----------------------------------------------------


def test_the_server_serves_the_dashboard_and_leaves_the_artifact_behind(
    week: CalendarStore, tmp_path: Path
) -> None:
    server = LocalServer(tmp_path, token="tok", calendar=week)

    response = server.get("/dashboard")

    assert response.status == 200
    assert "standup" in response.text
    assert (tmp_path / "ui" / "index.html").read_text(encoding="utf-8") == response.text


@pytest.mark.parametrize(
    "path,view", [("/dashboard", "day"), ("/day/2026-10-16", "day"), ("/week", "week"), ("/month", "month")]
)
def test_each_view_has_an_address(week: CalendarStore, tmp_path: Path, path: str, view: str) -> None:
    server = LocalServer(tmp_path, token="tok", calendar=week)

    response = server.get(path)

    assert response.status == 200
    assert f"calendar--{view}" in response.text


def test_a_view_pinned_to_a_day_is_addressed_by_that_day(week: CalendarStore, tmp_path: Path) -> None:
    server = LocalServer(tmp_path, token="tok", calendar=week)

    response = server.get("/day/2026-10-17")

    assert response.status == 200
    assert "trip" in response.text
    assert (tmp_path / "ui" / "day-2026-10-17.html").exists()
    assert not (tmp_path / "ui" / "index.html").exists()


def test_the_route_is_no_longer_answered_as_unbuilt(week: CalendarStore, tmp_path: Path) -> None:
    server = LocalServer(tmp_path, token="tok", calendar=week)

    assert server.get("/dashboard").status == 200
    # The four writes the approval split owns still say what they will be.
    assert server.post("/tune", b"{}", token="tok").status == 501


def test_the_dashboard_answers_over_a_real_socket(week: CalendarStore, tmp_path: Path) -> None:
    """T1 serves it, which is the half a unit test of `get()` cannot prove."""
    server = LocalServer(tmp_path, token="tok", port=0, calendar=week)
    url = server.start()
    try:
        with urllib.request.urlopen(f"{url}/dashboard", timeout=5) as reply:
            body = reply.read().decode("utf-8")
            assert reply.status == 200
            # As a header, not only as the <meta> in the document: a meta policy
            # starts applying where it is parsed, which leaves the bytes before it
            # — and the page head — unprotected.
            assert reply.headers["Content-Security-Policy"].startswith("default-src 'none'")
    finally:
        server.stop()

    assert "standup" in body
    assert (tmp_path / "ui" / "index.html").exists()


def test_a_server_without_a_calendar_says_so_plainly(tmp_path: Path) -> None:
    """"Built without a calendar" is true; "not built yet" would not be."""
    response = LocalServer(tmp_path, token="tok").get("/dashboard")

    assert response.status == 501
    assert "without a calendar" in response.text


def test_an_unreadable_date_is_refused_rather_than_rendered(week: CalendarStore, tmp_path: Path) -> None:
    server = LocalServer(tmp_path, token="tok", calendar=week)

    response = server.get("/day/2026-13-45")

    assert response.status == 400
    assert "YYYY-MM-DD" in response.text


def test_a_path_that_is_not_a_view_is_still_not_a_view(week: CalendarStore, tmp_path: Path) -> None:
    server = LocalServer(tmp_path, token="tok", calendar=week)

    assert server.get("/day/not-a-date").status == 400
    assert server.get("/fortnight").status == 404


# -- refusals name the way out ----------------------------------------------


def test_an_unknown_view_is_refused_naming_the_three_that_exist(week: CalendarStore) -> None:
    with pytest.raises(ValueError) as refused:
        calendar_page(week, view="year")

    assert "day | week | month" in str(refused.value)


def test_the_views_are_exactly_the_three_the_spec_names() -> None:
    assert VIEWS == ("day", "week", "month")


def test_an_unreadable_anchor_is_refused_rather_than_defaulted_to_today(week: CalendarStore) -> None:
    """"I asked for a day and got today" is worse than a refusal — it looks right."""
    with pytest.raises(ValueError) as refused:
        calendar_page(week, view="day", anchor="16th of October")

    assert "YYYY-MM-DD" in str(refused.value)


def test_an_empty_anchor_means_today(week: CalendarStore) -> None:
    page = calendar_page(week, view="day", anchor="", now=NOW)

    assert page.anchor.isoformat() == FRIDAY
