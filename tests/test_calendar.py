"""The internal calendar (SPEC §5.5).

``events.ics`` is the truth and the store is a guest in it: a tool write must not
flatten a hand edit, and an edit made outside the tool must show up on the next
read — otherwise "file-first" is a claim about the *format*, not about who owns
the data. Reads answer with occurrences; writes address a series, one instance,
or a set by name or day; and a local_only event never reaches a remote turn.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from daedalus.calendar import ical, zones
from daedalus.calendar.store import CalendarStore
from daedalus.loop.context import build_system_prompt
from daedalus.runtime import Runtime
from daedalus.security.provenance import DataClass
from daedalus.surface.base import InboundEvent
from daedalus.tools.calendar import CalendarTool


@pytest.fixture
def store(tmp_path: Path) -> CalendarStore:
    return CalendarStore(tmp_path / "calendar")


def _soon(hours: int = 2) -> str:
    """A time inside the agenda window, whenever the suite happens to run."""
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def _raw(path: Path) -> str:
    """The file as written — ``read_text`` translates CRLF away by default."""
    return path.read_text(encoding="utf-8", newline="")


# -- the file is the truth --------------------------------------------------


def test_an_event_round_trips_through_the_file(store: CalendarStore, tmp_path: Path) -> None:
    event = store.add(
        summary="Dentist",
        start="2026-10-20T09:00",
        end="2026-10-20T09:30",
        location="Bridge St",
    )

    reopened = CalendarStore(tmp_path / "calendar").get(event.uid)
    assert reopened is not None
    assert (
        reopened.summary,
        reopened.start,
        reopened.end,
        reopened.location,
    ) == ("Dentist", "2026-10-20T09:00", "2026-10-20T09:30", "Bridge St")
    assert reopened.all_day is False

    # A bare date is all day; a moment is not.
    day = store.add(summary="Launch day", start="2026-11-02")
    hour = store.add(summary="Standup", start="2026-11-02T09:00")
    assert (day.all_day, hour.all_day) == (True, False)
    assert day.when().endswith("(all day)")

    # Structural characters are escaped on the way out and survive the way back.
    title = "Lunch, with Ana; re: budget"
    escaped = store.add(summary=title, start="2026-10-21T12:00", description="line one\nline two")
    raw = _raw(tmp_path / "calendar" / "events.ics")
    assert "SUMMARY:Lunch\\, with Ana\\; re: budget" in raw, "commas and semicolons must be escaped"
    back = CalendarStore(tmp_path / "calendar").get(escaped.uid)
    assert back is not None
    assert back.summary == title
    assert back.description == "line one\nline two"

    # The file is a valid calendar document, carrying our own authority.
    assert raw.startswith("BEGIN:VCALENDAR\r\n")
    assert "VERSION:2.0" in raw
    assert raw.rstrip().endswith("END:VCALENDAR")
    assert "X-DAEDALUS-AUTHORITY:internal" in raw

    # The index derives from the file: it rebuilds, counts, and a delete rewrites
    # the file rather than hiding the event in a cache.
    assert store.rebuild_index() == 4
    assert store.count() == 4
    assert store.delete(event.uid) is True
    assert store.delete(event.uid) is False
    assert store.get(event.uid) is None
    assert "Dentist" not in _raw(tmp_path / "calendar" / "events.ics")


def test_a_hand_edit_or_unmodelled_component_survives_and_the_index_follows_the_file(
    store: CalendarStore, tmp_path: Path
) -> None:
    path = store.ics_path
    store.add(summary="Standup", start="2026-10-16T09:00")

    # An event added by hand is not flattened by the next tool write, and the
    # properties only the hand wrote — a rule, an unmodelled CATEGORIES — are kept.
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "END:VCALENDAR",
            "BEGIN:VEVENT\r\nUID:byhand\r\nDTSTAMP:20261016T090000Z\r\n"
            "DTSTART:20261017T140000\r\nSUMMARY:Added by hand\r\n"
            "RRULE:FREQ=WEEKLY\r\nCATEGORIES:work,deep\r\nEND:VEVENT\r\nEND:VCALENDAR",
        ),
        encoding="utf-8",
    )
    store.add(summary="Another", start="2026-10-18T10:00")
    reread = CalendarStore(tmp_path / "calendar")
    byhand = next((e for e in reread.all() if e.uid == "byhand"), None)
    assert byhand is not None, "a hand-added event was dropped"
    assert byhand.summary == "Added by hand"
    assert byhand.rrule == "FREQ=WEEKLY", "a rule the hand wrote was lost"
    assert ("CATEGORIES", "work,deep") in byhand.extra, "an unmodelled property was dropped"

    # A component we do not model is kept verbatim.
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "BEGIN:VEVENT",
            "BEGIN:VTIMEZONE\r\nTZID:Europe/London\r\nEND:VTIMEZONE\r\nBEGIN:VEVENT",
            1,
        ),
        encoding="utf-8",
    )
    store.add(summary="Third", start="2026-10-19T10:00")
    assert "BEGIN:VTIMEZONE" in _raw(path)

    # The index must not shadow the file it derives from: a hand edit is visible
    # to the very next read.
    path.write_text(
        _raw(path).replace(
            "END:VCALENDAR",
            "BEGIN:VEVENT\r\nUID:late\r\nDTSTAMP:20261016T090000Z\r\n"
            "DTSTART:20261019T110000\r\nSUMMARY:Added later\r\nEND:VEVENT\r\nEND:VCALENDAR",
        ),
        encoding="utf-8",
    )
    assert any(e.uid == "late" for e in store.all()), "the stale index hid a hand edit"

    # There is no second source of truth: gcal is a slot that has not spoken yet.
    assert store.sync_state()["cursor"] is None
    assert not (store.root / "sync_state.json").exists(), "an empty cursor file is not state"

    # A cache emptied without clearing its recorded revision must rebuild rather
    # than serve an empty calendar.
    fresh = CalendarStore(tmp_path / "schema")
    fresh.add(summary="Dentist", start="2026-11-02T09:00")
    with sqlite3.connect(fresh.index.path) as conn:  # as if built by an older shape
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema'")
    assert [e.summary for e in CalendarStore(tmp_path / "schema").series()] == ["Dentist"]


# -- the agenda and a single edit ------------------------------------------


def test_the_agenda_holds_whole_days_and_only_the_window_it_covers(store: CalendarStore) -> None:
    store.add(summary="Yesterday", start="2026-10-15T09:00")
    store.add(summary="All day today", start="2026-10-16")
    store.add(summary="Later today", start="2026-10-16T18:00")
    store.add(summary="Next month", start="2026-11-16T09:00")

    titles = [e.summary for e in store.agenda(days=7, now=datetime(2026, 10, 16, 12, 0))]

    assert titles == ["All day today", "Later today"]

    # A multi-day event is on every day it covers: Thursday to Saturday is all three.
    store.add(summary="Conference", start="2026-12-03", end="2026-12-06")
    friday = store.occurrences(datetime(2026, 12, 4), datetime(2026, 12, 5))
    assert [e.summary for e in friday] == ["Conference"]
    assert friday[0].when() == "Thu 03 Dec – Sat 05 Dec (all day)"


def test_an_update_touches_only_what_it_was_given(store: CalendarStore) -> None:
    event = store.add(summary="Standup", start="2026-10-16T09:00", description="daily")

    moved = store.update(event.uid, start="2026-10-16T10:00")

    assert moved is not None
    assert moved.start == "2026-10-16T10:00"
    assert moved.description == "daily", "a move blanked a field it was not asked to change"

    # A move is a translation, so the end goes with it: a half-hour standup stays
    # half an hour instead of ending before it starts.
    review = store.add(summary="Review", start="2026-10-05T09:00", end="2026-10-05T09:30")
    store.update(review.uid, start="2026-10-20T14:00")
    assert store.get(review.uid).start == "2026-10-20T14:00"
    assert store.get(review.uid).end == "2026-10-20T14:30"

    # An unknown uid is answered, not invented; nothing to change is refused.
    assert store.update("nope", summary="x") is None
    with pytest.raises(ValueError):
        store.update(event.uid)


# -- the egress gate --------------------------------------------------------


async def test_a_local_only_event_never_reaches_a_remote_prompt_or_tool_result(
    runtime: Runtime,
) -> None:
    runtime.calendar.add(summary="Therapy", start=_soon(2), data_class=DataClass.LOCAL_ONLY)
    runtime.calendar.add(summary="Standup", start=_soon(3))

    event = InboundEvent(text="what's today?", user_id="owner")
    remote = build_system_prompt(runtime, event, include_local_only=False)
    assert "Therapy" not in remote, "a local_only event reached a remote-bound prompt"
    assert "Standup" in remote
    assert "Therapy" in build_system_prompt(runtime, event, include_local_only=True)

    result = await CalendarTool().run({"op": "agenda", "summary": "", "start": ""}, runtime)
    assert result.ok
    assert "Therapy" not in result.output, "the tool leaked a local_only event into a turn"


# -- the tool ---------------------------------------------------------------


async def test_the_tool_creates_reads_deletes_and_refuses_an_incomplete_request(
    runtime: Runtime,
) -> None:
    tool = CalendarTool()
    # create → agenda → delete → the id no longer resolves
    created = await tool.run(
        {"op": "create", "summary": "Dentist", "start": _soon(4), "end": _soon(5)}, runtime
    )
    assert created.ok, created.output
    uid = created.data["uid"]
    agenda = await tool.run({"op": "agenda", "summary": "", "start": "", "days": 7}, runtime)
    assert agenda.ok
    assert "Dentist" in agenda.output and uid in agenda.output
    gone = await tool.run({"op": "delete", "summary": "", "start": "", "uid": uid}, runtime)
    assert gone.ok
    missing = await tool.run({"op": "get", "summary": "", "start": "", "uid": uid}, runtime)
    assert not missing.ok
    assert "no event" in missing.output

    # A create needs a title...
    no_title = await tool.run({"op": "create", "summary": "", "start": _soon()}, runtime)
    assert not no_title.ok and "summary" in no_title.output
    # ...and a time it can read.
    bad_time = await tool.run(
        {"op": "create", "summary": "Nope", "start": "next tuesday"}, runtime
    )
    assert not bad_time.ok and "not a time I can read" in bad_time.output
    # An update needs something to change...
    standup = await tool.run({"op": "create", "summary": "Standup", "start": _soon(6)}, runtime)
    nothing = await tool.run(
        {"op": "update", "summary": "", "start": "", "uid": standup.data["uid"]}, runtime
    )
    assert not nothing.ok and "at least one field" in nothing.output
    # ...and an addressing op needs its target.
    no_uid = await tool.run({"op": "delete", "summary": "", "start": ""}, runtime)
    assert not no_uid.ok and "uid" in no_uid.output


# -- recurrence -------------------------------------------------------------


def test_a_repeating_event_is_one_definition_that_appears_on_the_days_it_repeats(
    store: CalendarStore, tmp_path: Path
) -> None:
    event = store.add(summary="Standup", start="2026-10-12T09:10", rrule="FREQ=WEEKLY;BYDAY=MO")

    monday = store.occurrences(datetime(2026, 10, 19), datetime(2026, 10, 20))
    assert [e.summary for e in monday] == ["Standup"]
    assert monday[0].start == "2026-10-19T09:10"
    assert store.occurrences(datetime(2026, 10, 20), datetime(2026, 10, 21)) == []

    # One definition is one row however often it repeats, printed as uid@instance.
    assert store.count() == 1
    assert [e.uid for e in store.series()] == [event.uid]
    assert store.agenda(days=7, now=datetime(2026, 10, 12, 7, 0))[0].line(show_id=True).startswith(
        f"[{event.uid}@2026-10-12T09:10]"
    )

    # The rule's own end is where the set stops.
    stopped = CalendarStore(tmp_path / "until")
    stopped.add(
        summary="Sprint", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T090000"
    )
    assert [e.start for e in stopped.agenda(days=21, now=datetime(2026, 10, 12, 0, 0))] == [
        "2026-10-12T09:00",
        "2026-10-19T09:00",
    ]

    # An excluded date is left out (COUNT counts what the rule produced).
    store.add(
        summary="Daily",
        start="2026-10-12T09:00",
        rrule="FREQ=DAILY;COUNT=3",
        exdates=["2026-10-13T09:00"],
    )
    daily = [
        e.start
        for e in store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 16))
        if e.summary == "Daily"
    ]
    assert daily == ["2026-10-12T09:00", "2026-10-14T09:00"]

    # A rule that cannot be read is refused at the door.
    with pytest.raises(ValueError):
        store.add(summary="Nope", start="2026-10-12T09:00", rrule="FREQ=HOURLY")


def test_moving_or_dropping_one_instance_leaves_the_series_and_its_tail_intact(
    store: CalendarStore, tmp_path: Path
) -> None:
    # `scope=this` moves one instance; later occurrences keep the series' time.
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    moved = store.update(
        series.uid,
        recurrence_id="2026-10-19T09:00",
        scope="this",
        start="2026-10-19T11:00",
        end="2026-10-19T11:30",
    )
    assert moved is not None
    by_day = {occ.start: occ.summary for occ in store.agenda(days=14, now=datetime(2026, 10, 19, 0, 0))}
    assert by_day["2026-10-19T11:00"] == "Standup"
    assert "2026-10-19T09:00" not in by_day, "the original slot was left behind"
    assert "2026-10-26T09:00" in by_day, "later occurrences must keep the series' time"

    # Dropping one instance leaves the rest.
    drops = CalendarStore(tmp_path / "drops")
    daily = drops.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=DAILY;COUNT=3")
    assert drops.delete(daily.uid, recurrence_id="2026-10-13T09:00", scope="this") is True
    assert [e.start for e in drops.agenda(days=7, now=datetime(2026, 10, 12, 0, 0))] == [
        "2026-10-12T09:00",
        "2026-10-14T09:00",
    ]

    # `scope=future` splits the series; everything before the cut is untouched.
    split = CalendarStore(tmp_path / "split")
    original = split.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    tail = split.update(
        original.uid, recurrence_id="2026-11-02T09:00", scope="future", start="2026-11-02T10:00"
    )
    assert tail is not None and tail.uid != original.uid
    assert [e.start for e in split.agenda(days=35, now=datetime(2026, 10, 12, 0, 0))] == [
        "2026-10-12T09:00",
        "2026-10-19T09:00",
        "2026-10-26T09:00",
        "2026-11-02T10:00",
        "2026-11-09T10:00",
    ]

    # Dropping the series takes its moved instances with it.
    assert store.delete(series.uid) is True
    assert store.count() == 0
    assert store.agenda(days=28, now=datetime(2026, 10, 12, 0, 0)) == []


# -- the file speaks iCalendar ---------------------------------------------


def test_a_rule_and_its_exceptions_round_trip_as_icalendar(
    store: CalendarStore, tmp_path: Path
) -> None:
    store.add(
        summary="Standup",
        start="2026-10-12T09:00",
        rrule="FREQ=WEEKLY;BYDAY=MO,WE,FR",
        exdates=["2026-10-14T09:00"],
    )

    raw = _raw(tmp_path / "calendar" / "events.ics")
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR" in raw
    assert "EXDATE:20261014T090000" in raw

    back = CalendarStore(tmp_path / "calendar").series()[0]
    assert back.rrule == "FREQ=WEEKLY;BYDAY=MO,WE,FR"
    assert back.exdates == ["2026-10-14T09:00"]

    # gcal reads an override as a second VEVENT with the same UID; the series
    # itself must not be rewritten to make that happen.
    override_store = CalendarStore(tmp_path / "override")
    series = override_store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    override_store.update(
        series.uid, recurrence_id="2026-10-19T09:00", scope="this", start="2026-10-19T11:00"
    )

    raw = _raw(tmp_path / "override" / "events.ics")
    assert raw.count("BEGIN:VEVENT") == 2
    assert raw.count(f"UID:{series.uid}") == 2
    assert "RECURRENCE-ID:20261019T090000" in raw

    reread = CalendarStore(tmp_path / "override")
    assert reread.overrides(series.uid)[0].start == "2026-10-19T11:00"
    assert reread.get(series.uid).start == "2026-10-12T09:00", "the series was rewritten"


def test_a_reminder_round_trips_without_touching_the_event(
    store: CalendarStore, tmp_path: Path
) -> None:
    store.add(summary="Standup", start="2026-10-12T09:00", reminders=[15])

    raw = _raw(store.ics_path)
    assert "BEGIN:VALARM" in raw and "TRIGGER:-PT15M" in raw

    back = CalendarStore(tmp_path / "calendar").series()[0]
    assert back.reminders[0].offset_minutes == 15
    assert back.reminders[0].lead_text() == "15m before"

    # A VALARM carries a DESCRIPTION of its own; lifting it would be a data loss.
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n"
        "BEGIN:VEVENT\r\nUID:a1\r\nDTSTAMP:20261016T090000Z\r\n"
        "DTSTART:20261016T090000\r\nSUMMARY:Dentist\r\nDESCRIPTION:Bring the referral\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder!\r\nTRIGGER:-PT30M\r\n"
        "END:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    event = ical.parse_calendar(text).events[0]
    assert event.description == "Bring the referral"
    assert event.reminders[0].offset_minutes == 30
    assert event.reminders[0].description == "Reminder!"


# -- clocks other than this one ---------------------------------------------


def test_a_zone_keeps_the_wall_time_it_qualifies(store: CalendarStore, tmp_path: Path) -> None:
    # A TZID survives a rewrite and a reload rather than being flattened away.
    path = store.ics_path
    store.add(summary="Anchor", start="2026-10-12T09:00")
    path.write_text(
        _raw(path).replace(
            "END:VCALENDAR",
            "BEGIN:VEVENT\r\nUID:zoned\r\nDTSTAMP:20261016T090000Z\r\n"
            "DTSTART;TZID=Europe/London:20261017T090000\r\nSUMMARY:Call\r\n"
            "END:VEVENT\r\nEND:VCALENDAR",
        ),
        encoding="utf-8",
    )
    store.add(summary="Another", start="2026-10-18T10:00")  # forces a rewrite
    assert "DTSTART;TZID=Europe/London:20261017T090000" in _raw(path), "the zone was flattened away"
    zoned = store.get("zoned")
    assert zoned is not None and zoned.tzid == "Europe/London"

    # A 09:00 call in London is 09:00 in London either side of the clocks changing.
    clock = CalendarStore(tmp_path / "clock")
    clock.add(summary="Call", start="2026-03-27T09:00", rrule="FREQ=DAILY", tzid="Europe/London")
    assert [e.start for e in clock.occurrences(datetime(2026, 3, 27), datetime(2026, 3, 31))] == [
        "2026-03-27T09:00",
        "2026-03-28T09:00",
        "2026-03-29T09:00",  # the day the London clocks moved
        "2026-03-30T09:00",
    ]

    # A clock that is not this one is named; the local one stays uncluttered.
    outside = next(z for z in ("Asia/Tokyo", "Europe/London") if not zones.is_local(z))
    foreign = store.add(summary="Call", start="2026-10-20T09:00", tzid=outside)
    local = store.add(summary="Dentist", start="2026-10-20T09:00")
    assert outside.split("/")[-1] in foreign.line(), "a foreign clock must say which clock it is"
    assert outside.split("/")[-1] not in local.line()
    assert local.when() == "Tue 20 Oct 09:00"

    # Two clocks, one moment: the conversion the whole layer exists for.
    london = zones.zone_for("Europe/London")
    tokyo = zones.zone_for("Asia/Tokyo")
    assert london is not None and tokyo is not None
    nine_in_london = datetime(2026, 10, 16, 9, 0, tzinfo=london)
    assert nine_in_london.astimezone(tokyo).strftime("%H:%M") == "17:00"

    # An unreadable zone falls back to local rather than guessing an offset: a
    # wrong hour delivered confidently is worse than one that is merely local.
    weird = CalendarStore(tmp_path / "weird")
    weird.add(summary="Anchor", start="2026-10-12T09:00")
    weird.ics_path.write_text(
        _raw(weird.ics_path).replace(
            "END:VCALENDAR",
            "BEGIN:VEVENT\r\nUID:weird\r\nDTSTAMP:20261016T090000Z\r\n"
            "DTSTART;TZID=Mars/Olympus:20261020T090000\r\nSUMMARY:Olympiad\r\n"
            "END:VEVENT\r\nEND:VCALENDAR",
        ),
        encoding="utf-8",
    )
    event = CalendarStore(tmp_path / "weird").get("weird")
    assert event is not None
    assert event.start == "2026-10-20T09:00", "the wall time is kept, not shifted by a guess"
    assert zones.zone_for("Mars/Olympus") is None, "an unknown zone resolves to nothing"


# -- free, busy and doubles -------------------------------------------------


async def test_busy_merges_overlaps_and_a_double_booking_is_reported(
    runtime: Runtime,
) -> None:
    runtime.calendar.add(summary="A", start="2026-11-04T09:00", end="2026-11-04T10:00")
    runtime.calendar.add(summary="B", start="2026-11-04T09:30", end="2026-11-04T10:30")

    blocks = runtime.calendar.busy(datetime(2026, 11, 4), datetime(2026, 11, 5))
    assert [(b[0].strftime("%H:%M"), b[1].strftime("%H:%M")) for b in blocks] == [("09:00", "10:30")]

    pairs = runtime.calendar.conflicts(datetime(2026, 11, 4), datetime(2026, 11, 5))
    assert [(first.summary, second.summary) for first, second in pairs] == [("A", "B")]

    result = await CalendarTool().run(
        {"op": "busy", "summary": "", "start": "2026-11-04", "query": "", "days": 1}, runtime
    )
    assert result.ok
    assert "double-booked" in result.output


async def test_free_offers_only_gaps_long_enough_to_use(runtime: Runtime) -> None:
    cal = runtime.calendar

    # The gaps between what is booked, to the end of the working day.
    cal.add(summary="A", start="2026-10-20T09:00", end="2026-10-20T10:00")
    cal.add(summary="B", start="2026-10-20T11:00", end="2026-10-20T12:00")
    gaps = cal.free(datetime(2026, 10, 20), datetime(2026, 10, 21), minutes=30)
    assert [(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in gaps] == [
        ("10:00", "11:00"),
        ("12:00", "18:00"),
    ]

    # A gap too short to use is not offered.
    cal.add(summary="A", start="2026-10-22T09:00", end="2026-10-22T10:00")
    cal.add(summary="B", start="2026-10-22T10:15", end="2026-10-22T18:00")
    assert cal.free(datetime(2026, 10, 22), datetime(2026, 10, 23), minutes=30) == []

    # A full day leaves no room.
    cal.add(summary="Conference", start="2026-10-21")
    assert cal.free(datetime(2026, 10, 21), datetime(2026, 10, 22), minutes=30) == []

    # An event marked free does not block the day.
    cal.add(
        summary="Focus time",
        start="2026-10-23T09:00",
        end="2026-10-23T10:00",
        transparency="transparent",
    )
    assert cal.busy(datetime(2026, 10, 23), datetime(2026, 10, 24)) == []
    assert cal.free(datetime(2026, 10, 23), datetime(2026, 10, 24), minutes=30)

    # A repeating meeting blocks its slot every day it repeats.
    cal.add(summary="Standup", start="2026-10-26T09:00", end="2026-10-26T09:30", rrule="FREQ=DAILY")
    gaps = cal.free(datetime(2026, 10, 26), datetime(2026, 10, 29), minutes=30)
    assert [(s.day, s.strftime("%H:%M")) for s, _ in gaps] == [
        (26, "09:30"),
        (27, "09:30"),
        (28, "09:30"),
    ]

    # The tool answers the same question.
    cal.add(summary="C", start="2026-11-03T09:00", end="2026-11-03T10:00")
    result = await CalendarTool().run(
        {"op": "free", "summary": "", "start": "2026-11-03", "query": "", "days": 1, "minutes": 60},
        runtime,
    )
    assert result.ok, result.output
    assert "10:00" in result.output and "18:00" in result.output


# -- search -----------------------------------------------------------------


async def test_search_finds_an_event_by_any_of_its_text_and_never_leaks_a_local_only_one(
    runtime: Runtime,
) -> None:
    cal = runtime.calendar
    cal.add(summary="Dentist", start="2026-11-02T09:00", description="Bridge Street clinic")
    cal.add(summary="Standup", start="2026-11-02T09:10", location="Zoom")

    assert [e.summary for e in cal.search("bridge")] == ["Dentist"]
    assert [e.summary for e in cal.search("zoom")] == ["Standup"]
    assert cal.search("nothing like this") == []

    # A series is one hit, not one per instance.
    series = cal.add(summary="Weekly retro", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    cal.update(series.uid, recurrence_id="2026-10-19T09:00", scope="this", start="2026-10-19T11:00")
    rows = cal.search("retro")
    assert [e.uid for e in rows] == [series.uid]
    assert rows[0].rrule == "FREQ=WEEKLY;BYDAY=MO"

    # A local-only event is not searchable from a remote turn.
    cal.add(summary="Therapy", start="2026-11-02T09:00", data_class=DataClass.LOCAL_ONLY)
    assert cal.search("therapy", allow_local_only=False) == []
    assert len(cal.search("therapy", allow_local_only=True)) == 1

    # The tool searches, and says so when a search has no words.
    result = await CalendarTool().run(
        {"op": "search", "summary": "", "start": "", "query": "bridge"}, runtime
    )
    assert result.ok
    assert "Dentist" in result.output
    empty = await CalendarTool().run(
        {"op": "search", "summary": "", "start": "", "query": ""}, runtime
    )
    assert not empty.ok and "query" in empty.output


# -- changing a whole set at once -------------------------------------------


async def test_a_bulk_retime_moves_the_whole_set_and_keeps_each_event_s_own_day(
    runtime: Runtime,
) -> None:
    cal = runtime.calendar
    first = cal.add(summary="Standup", start="2026-10-12T09:00", end="2026-10-12T09:15")
    second = cal.add(summary="Standup", start="2026-10-13T09:00", end="2026-10-13T09:15")
    cal.add(summary="Dentist", start="2026-10-12T11:00", description="before the standup")

    # "Move all my standups to 10" keeps each event's own day and length.
    changed = cal.bulk_update("standup", at="10:00")
    assert {e.uid for e in changed} == {first.uid, second.uid}
    assert cal.get(first.uid).start == "2026-10-12T10:00"
    assert cal.get(second.uid).start == "2026-10-13T10:00"
    assert cal.get(first.uid).end == "2026-10-12T10:15"
    assert cal.get(first.uid).summary == "Standup"

    # The name is what is matched, not a mention of it in the notes.
    assert [e.summary for e in cal.matching("standup")] == ["Standup", "Standup"]

    # An all-day event has no hour to move and is left alone.
    holiday = cal.add(summary="Market holiday", start="2026-10-12")
    assert cal.bulk_update("holiday", at="10:00") == []
    assert cal.get(holiday.uid).all_day is True

    # A bulk change can retitle the set; the old name then matches nothing.
    renamed = cal.bulk_update("standup", summary="Daily sync")
    assert [e.summary for e in renamed] == ["Daily sync", "Daily sync"]
    assert cal.matching("standup") == []

    # A retime carries the rule with it.
    cal.add(summary="Review", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    cal.bulk_update("review", at="10:00")
    rows = cal.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 13))
    assert [e.start for e in rows if e.summary == "Review"] == ["2026-10-12T10:00"]

    # A change that matches nothing changes nothing; a bulk call with nothing to
    # say is refused.
    keeper = cal.add(summary="Dentist follow-up", start="2026-10-14T09:00")
    assert cal.bulk_update("nothing-here", at="10:00") == []
    assert cal.bulk_delete("nothing-here") == 0
    assert cal.get(keeper.uid) is not None
    with pytest.raises(ValueError):
        cal.bulk_update("review")

    # A bulk delete takes every match and leaves the rest.
    cal.add(summary="Climbing", start="2026-10-12T09:00", rrule="FREQ=WEEKLY")
    cal.add(summary="Climbing", start="2026-10-15T09:00")
    assert cal.bulk_delete("climbing") == 2
    summaries = [e.summary for e in cal.series()]
    assert "Climbing" not in summaries
    assert "Dentist follow-up" in summaries

    # The tool reaches the same set, and the approval prompt names it.
    cal.add(summary="Guitar practice", start="2026-10-20T09:00", end="2026-10-20T09:15")
    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "", "query": "", "match": "guitar", "at": "11:00"},
        runtime,
    )
    assert result.ok, result.output
    assert "11:00" in result.output
    assert cal.matching("guitar")[0].start == "2026-10-20T11:00"

    # A shared `start` would move every match onto that day; `at` is the shape.
    refused = await CalendarTool().run(
        {"op": "update", "start": "2026-10-20T10:00", "query": "", "match": "guitar"},
        runtime,
    )
    assert not refused.ok
    assert "at" in refused.output
    assert cal.matching("guitar")[0].start == "2026-10-20T11:00"

    # The tool deletes a whole set too.
    cal.add(summary="Climbing", start="2026-10-21T09:00")
    cal.add(summary="Climbing", start="2026-10-22T09:00")
    cal.add(summary="Dentist", start="2026-10-23T09:00")
    deleted = await CalendarTool().run(
        {"op": "delete", "summary": "", "start": "", "query": "", "match": "climbing"}, runtime
    )
    assert deleted.ok
    assert "2" in deleted.output
    assert [e.summary for e in cal.series() if e.summary == "Climbing"] == []

    assessment = CalendarTool().assess({"op": "delete", "match": "standup"})
    assert "standup" in assessment.summary
    assert "every" in assessment.summary


# -- addressing a set by the day it falls on, and moving it by a date --------


async def test_a_shift_moves_each_event_by_a_date_delta_and_the_tool_can_address_a_day(
    runtime: Runtime, tmp_path: Path
) -> None:
    # A `shift` is a date delta: every match keeps its own hour and length.
    store = CalendarStore(tmp_path / "shift")
    first = store.add(summary="Alpha", start="2026-10-25T09:00", end="2026-10-25T10:00")
    second = store.add(summary="Beta", start="2026-10-25T19:00", end="2026-10-25T21:30")
    store.add(summary="Gamma", start="2026-10-26T09:00")
    changed = store.bulk_update("", on="2026-10-25", shift="-7d")
    assert {e.uid for e in changed} == {first.uid, second.uid}
    assert store.get(first.uid).start == "2026-10-18T09:00"
    assert store.get(first.uid).end == "2026-10-18T10:00"
    assert store.get(second.uid).start == "2026-10-18T19:00"
    assert store.get(second.uid).end == "2026-10-18T21:30"
    assert [e.summary for e in store.occurrences_on("2026-10-25")] == []

    # `at` skips an all-day event; a day offset is exactly its shape.
    holidays = CalendarStore(tmp_path / "holidays")
    holiday = holidays.add(summary="Market", start="2026-10-25", end="2026-10-27")
    assert [e.uid for e in holidays.bulk_update("", on="2026-10-25", shift="-7d")] == [holiday.uid]
    assert holidays.get(holiday.uid).start == "2026-10-18"
    assert holidays.get(holiday.uid).end == "2026-10-20"
    assert holidays.get(holiday.uid).all_day is True

    # `match` and `on` compose: the name picks the kind, the day picks which.
    named = CalendarStore(tmp_path / "named")
    here = named.add(summary="Standup", start="2026-10-25T09:00")
    later = named.add(summary="Standup", start="2026-10-26T09:00")
    assert [e.uid for e in named.bulk_update("standup", on="2026-10-25", shift="2d")] == [here.uid]
    assert named.get(here.uid).start == "2026-10-27T09:00"
    assert named.get(later.uid).start == "2026-10-26T09:00"

    # A week is spelled `w`, and an unreadable shift is refused rather than guessed.
    weekly = CalendarStore(tmp_path / "weekly")
    event = weekly.add(summary="Review", start="2026-10-25T09:00")
    weekly.bulk_update("review", shift="-1w")
    assert weekly.get(event.uid).start == "2026-10-18T09:00"
    with pytest.raises(ValueError, match="not a shift I can read"):
        weekly.bulk_update("review", shift="next tuesday")

    cal = runtime.calendar
    # A delta is not only for a set: `uid` + `shift` moves one event by days.
    review = cal.add(summary="Review", start="2026-10-25T09:00", end="2026-10-25T10:00")
    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "", "query": "", "uid": review.uid, "shift": "-7d"},
        runtime,
    )
    assert result.ok, result.output
    assert cal.get(review.uid).start == "2026-10-18T09:00"
    assert cal.get(review.uid).end == "2026-10-18T10:00"

    # The set the model addresses is not always *only* a rule: the standup the
    # owner already moved to another day is still one of the standups.
    series = cal.add(
        summary="Standup", start="2026-10-05T09:00", end="2026-10-05T09:15", rrule="FREQ=WEEKLY"
    )
    cal.update(
        series.uid,
        scope="this",
        recurrence_id="2026-10-12T09:00",
        start="2026-10-13T11:00",
        end="2026-10-13T11:30",
    )
    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "", "query": "", "on": "2026-10-05", "shift": "-7d"},
        runtime,
    )
    assert result.ok, result.output
    assert [e.start for e in cal.occurrences_on("2026-10-06")] == [
        "2026-10-06T11:00"
    ], "the instance the owner had moved was left behind"
    assert cal.occurrences_on("2026-10-13") == []
    assert [e.start for e in cal.occurrences_on("2026-09-28")] == ["2026-09-28T09:00"]

    # Two things on Sunday that share no word, moved in one call — the owner's
    # own correction ("wrong Sunday — I meant the coming one").
    practice = cal.add(summary="Guitar practice", start="2026-10-25T09:00", end="2026-10-25T10:00")
    club = cal.add(summary="Book club", start="2026-10-25T19:00", end="2026-10-25T21:30")
    other = cal.add(summary="Market run", start="2026-10-24T08:00", end="2026-10-24T09:00")
    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "", "query": "", "on": "2026-10-25", "shift": "-7d"},
        runtime,
    )
    assert result.ok, result.output
    assert cal.get(practice.uid).start == "2026-10-18T09:00"
    assert cal.get(practice.uid).end == "2026-10-18T10:00"
    assert cal.get(club.uid).start == "2026-10-18T19:00"
    assert cal.get(other.uid).start == "2026-10-24T08:00"
    assert cal.occurrences_on("2026-10-25") == []

    # A day is a delete target too.
    alpha = cal.add(summary="Alpha", start="2026-10-28T09:00")
    beta = cal.add(summary="Beta", start="2026-10-28T19:00")
    keeper = cal.add(summary="Gamma", start="2026-10-29T09:00")
    result = await CalendarTool().run(
        {"op": "delete", "summary": "", "start": "", "query": "", "on": "2026-10-28"}, runtime
    )
    assert result.ok, result.output
    assert "2" in result.output
    remaining = [e.uid for e in cal.series()]
    assert keeper.uid in remaining
    assert alpha.uid not in remaining and beta.uid not in remaining

    # An unreadable shift is reported, and the approval prompt names the day.
    unreadable = await CalendarTool().run(
        {
            "op": "update",
            "summary": "",
            "start": "",
            "query": "",
            "on": "2026-10-29",
            "shift": "next tuesday",
        },
        runtime,
    )
    assert not unreadable.ok
    assert "not a shift I can read" in unreadable.output

    assessment = CalendarTool().assess({"op": "update", "on": "2026-10-25", "shift": "-7d"})
    assert "2026-10-25" in assessment.summary
    assert "every" in assessment.summary

    # The model's only vocabulary for the calendar is the context prompt.
    prompt = build_system_prompt(runtime, InboundEvent(text="move my Sunday", user_id="owner"))
    assert 'on="2026-10-25"' in prompt
    assert 'shift="-7d"' in prompt


# -- a change to the rule carries the series' exceptions --------------------
#
# A series is several entries in the file, and only one of them is the rule.
# Moving the rule alone does not move the instance the owner moved (it is a
# VEVENT with its own DTSTART) and does not move the day the owner skipped (it is
# an EXDATE naming a moment on the old rule). Left like that the calendar is
# wrong twice at once, in opposite directions — a skipped day reappears and a
# moved instance is stranded — and both look like ordinary events.


def _weekly_with_exceptions(store: CalendarStore) -> str:
    """A Monday standup whose 12th was moved to the 13th and whose 19th is off."""
    series = store.add(
        summary="Standup",
        start="2026-10-05T09:00",
        end="2026-10-05T09:15",
        rrule="FREQ=WEEKLY",
    )
    store.update(
        series.uid,
        scope="this",
        recurrence_id="2026-10-12T09:00",
        start="2026-10-13T11:00",
        end="2026-10-13T11:30",
    )
    store.update(series.uid, exdates=["2026-10-19T09:00"])
    return series.uid


def _by_days(start: str, days: int) -> str:
    """`start` moved by whole days, in the shape it arrived in."""
    moment = datetime.fromisoformat(start) + timedelta(days=days)
    return moment.strftime("%Y-%m-%d" if len(start) == 10 else "%Y-%m-%dT%H:%M")


def test_a_change_to_a_series_carries_the_exceptions_it_produced(tmp_path: Path) -> None:
    shift = CalendarStore(tmp_path / "shift")
    uid = _weekly_with_exceptions(shift)
    before = {e.start for e in shift.occurrences(datetime(2026, 9, 1), datetime(2026, 12, 1))}

    # A refusal must not have half-moved anything on its way out.
    with pytest.raises(ValueError, match="not a shift I can read"):
        shift.update(uid, shift="next tuesday")
    assert shift.get(uid).start == "2026-10-05T09:00"
    assert [e.start for e in shift.occurrences_on("2026-10-13")] == ["2026-10-13T11:00"]

    # A day shift moves the whole series: nothing stranded and no skipped day
    # back. The window is fixed and a weekly rule is not, so its far edge gains
    # the one occurrence the rule now reaches into it.
    shift.update(uid, shift="-7d")
    after = {e.start for e in shift.occurrences(datetime(2026, 9, 1), datetime(2026, 12, 1))}
    earlier = {_by_days(start, -7) for start in before}
    assert earlier <= after, "an instance or an exception was left behind"
    assert after - earlier == {"2026-11-30T09:00"}, "the window gained more than its edge"
    assert "2026-10-12T09:00" not in after, "the skipped day came back"
    assert "2026-10-13T11:00" not in after, "the moved instance stayed at its old date"

    # The skipped day stays skipped, and the day before it does not move.
    assert shift.occurrences_on("2026-10-12") == []
    assert [e.start for e in shift.occurrences_on("2026-10-19")] == ["2026-10-19T09:00"]

    # The override's own dates and its RECURRENCE-ID move together, and its
    # length is kept: the id is what ties it to the rule.
    moved = [e for e in shift.overrides(uid) if e.start == "2026-10-06T11:00"]
    assert len(moved) == 1
    assert moved[0].recurrence_id == "2026-10-05T09:00"
    assert moved[0].end == "2026-10-06T11:30", "the instance's length was not kept"

    # A translation is reversible: the file is the truth, and it round-trips.
    shift.update(uid, shift="1w")
    assert sorted(
        e.start for e in shift.occurrences(datetime(2026, 9, 1), datetime(2026, 12, 1))
    ) == sorted(before)

    # A delta moves dates; it is not a chance to re-time anything.
    clock = CalendarStore(tmp_path / "clock")
    clock_uid = _weekly_with_exceptions(clock)
    clock.update(clock_uid, shift="2d")
    assert [e.start for e in clock.occurrences_on("2026-10-07")] == ["2026-10-07T09:00"]
    assert [e.start for e in clock.occurrences_on("2026-10-15")] == ["2026-10-15T11:00"]

    # The keys of an all-day series are bare dates, and must stay bare.
    market = CalendarStore(tmp_path / "market")
    series = market.add(summary="Market", start="2026-10-05", end="2026-10-07", rrule="FREQ=WEEKLY")
    market.update(series.uid, scope="this", recurrence_id="2026-10-12", summary="Big market")
    market.update(series.uid, shift="-7d")
    assert market.get(series.uid).start == "2026-09-28"
    assert market.get(series.uid).end == "2026-09-30"
    override = market.overrides(series.uid)[0]
    assert override.recurrence_id == "2026-10-05"
    assert override.start == "2026-10-05"
    assert override.all_day is True

    # The uid is the join: a shift must not reach a sibling series' instances.
    siblings = CalendarStore(tmp_path / "siblings")
    mine = _weekly_with_exceptions(siblings)
    other = siblings.add(summary="Review", start="2026-10-05T15:00", rrule="FREQ=WEEKLY")
    siblings.update(other.uid, scope="this", recurrence_id="2026-10-12T15:00", start="2026-10-12T16:00")
    siblings.update(mine, shift="-7d")
    assert siblings.get(other.uid).start == "2026-10-05T15:00"
    assert siblings.overrides(other.uid)[0].recurrence_id == "2026-10-12T15:00"

    # `scope=this` moves *a* standup, not the rule.
    instance = CalendarStore(tmp_path / "instance")
    instance_uid = _weekly_with_exceptions(instance)
    instance.update(instance_uid, scope="this", recurrence_id="2026-10-26T09:00", start="2026-10-27T10:00")
    assert instance.get(instance_uid).start == "2026-10-05T09:00"
    assert [e.start for e in instance.occurrences_on("2026-10-27")] == ["2026-10-27T10:00"]

    # UNTIL is a date inside the rule; left behind, the series is truncated.
    until = CalendarStore(tmp_path / "until")
    course = until.add(
        summary="Course", start="2026-10-05T18:00", rrule="FREQ=WEEKLY;UNTIL=20261102T230000"
    )
    was = [e.start for e in until.occurrences(datetime(2026, 9, 1), datetime(2027, 1, 1))]
    assert was[-1] == "2026-11-02T18:00", "the fixture drifted"
    until.update(course.uid, shift="-7d")
    now = [e.start for e in until.occurrences(datetime(2026, 9, 1), datetime(2027, 1, 1))]
    assert len(now) == len(was), "the series lost or gained an instance"
    assert now[0] == "2026-09-28T18:00"
    assert now[-1] == "2026-10-26T18:00", "the series was cut short at the old UNTIL"

    # COUNT counts from a start that is moving too, so it needs nothing.
    counted = CalendarStore(tmp_path / "counted")
    short = counted.add(summary="Course", start="2026-10-05T18:00", rrule="FREQ=WEEKLY;COUNT=3")
    counted.update(short.uid, shift="3d")
    assert [e.start for e in counted.occurrences(datetime(2026, 9, 1), datetime(2027, 1, 1))] == [
        "2026-10-08T18:00",
        "2026-10-15T18:00",
        "2026-10-22T18:00",
    ]

    # An explicit new start translates the exceptions too, by definition.
    explicit = CalendarStore(tmp_path / "explicit")
    explicit_uid = _weekly_with_exceptions(explicit)
    explicit.update(explicit_uid, start="2026-10-07T09:00")
    starts = [e.start for e in explicit.occurrences(datetime(2026, 10, 1), datetime(2026, 11, 15))]
    assert "2026-10-07T09:00" in starts
    assert "2026-10-15T11:00" in starts, "the moved instance did not follow the series"
    assert "2026-10-13T11:00" not in starts
    assert explicit.occurrences_on("2026-10-21") == [], "the skipped day did not move"

    # An explicit move keeps the length the owner chose.
    length = CalendarStore(tmp_path / "length")
    review = length.add(summary="Review", start="2026-10-05T09:00", end="2026-10-05T09:30")
    length.update(review.uid, start="2026-10-20T14:00")
    assert length.get(review.uid).start == "2026-10-20T14:00"
    assert length.get(review.uid).end == "2026-10-20T14:30"

    # `future` splits a series; each half keeps the exceptions on its own side.
    tailed = CalendarStore(tmp_path / "tailed")
    tail_uid = _weekly_with_exceptions(tailed)
    tailed.update(tail_uid, scope="future", recurrence_id="2026-10-19T09:00", shift="2d")
    assert [e.start for e in tailed.occurrences_on("2026-10-13")] == ["2026-10-13T11:00"]
    assert tailed.occurrences_on("2026-10-21") == []
    starts = [e.start for e in tailed.occurrences(datetime(2026, 10, 1), datetime(2026, 11, 15))]
    assert "2026-10-28T09:00" in starts, "the tail did not carry on from its new day"
    assert "2026-10-21T09:00" not in starts, "a day the owner had skipped came back"

    # A split hands its overrides to the series that inherits them: left pointing
    # at the truncated master, one moved standup would become two.
    inherited = CalendarStore(tmp_path / "inherited")
    split = inherited.add(summary="Standup", start="2026-10-05T09:00", rrule="FREQ=WEEKLY")
    inherited.update(split.uid, scope="this", recurrence_id="2026-10-26T09:00", start="2026-10-27T10:00")
    inherited.update(split.uid, scope="future", recurrence_id="2026-10-19T09:00", summary="Retro")
    assert inherited.occurrences_on("2026-10-26") == []
    assert [e.start for e in inherited.occurrences_on("2026-10-27")] == ["2026-10-27T10:00"]
    assert sum(len(inherited.occurrences_on(day)) for day in ("2026-10-26", "2026-10-27")) == 1
    assert [e.summary for e in inherited.occurrences_on("2026-10-05")] == ["Standup"]
    assert inherited.occurrences_on("2026-10-13") == []

    # The tool's `on=… shift=…` form is the one the model reaches for, and it
    # carries the exceptions of every series it moves.
    bulk = CalendarStore(tmp_path / "bulk")
    bulk_uid = _weekly_with_exceptions(bulk)
    on_day = [e.start for e in bulk.occurrences_on("2026-10-05")]
    bulk.bulk_update("standup", on="2026-10-05", shift="-7d")
    assert on_day == ["2026-10-05T09:00"], "the fixture drifted"
    assert bulk.get(bulk_uid).start == "2026-09-28T09:00"
    starts = [e.start for e in bulk.occurrences(datetime(2026, 9, 21), datetime(2026, 11, 15))]
    assert "2026-10-06T11:00" in starts, "the moved instance did not come with it"


# -- dates the rule does not produce ----------------------------------------


def test_a_date_the_rule_does_not_produce_is_still_an_occurrence(tmp_path: Path) -> None:
    # The recurrence set is the rule *plus* RDATE: "every Monday, and also Friday".
    store = CalendarStore(tmp_path / "rdate")
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    store.update(series.uid, rdates=["2026-10-16T09:00"])
    rows = store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 20))
    assert [(e.start, e.summary) for e in rows] == [
        ("2026-10-12T09:00", "Standup"),
        ("2026-10-16T09:00", "Standup"),
        ("2026-10-19T09:00", "Standup"),
    ]

    # A property the store parses but does not write back is a silent data loss.
    assert "RDATE:" in _raw(store.ics_path).replace("\r\n ", "")
    reopened = CalendarStore(tmp_path / "rdate").get(series.uid)
    assert reopened is not None and reopened.rdates == ["2026-10-16T09:00"]

    # An added date that lands on the rule does not double the occurrence.
    store.update(series.uid, rdates=["2026-10-19T09:00"])  # a Monday the rule already has
    assert [e.start for e in store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 20))] == [
        "2026-10-12T09:00",
        "2026-10-19T09:00",
    ]

    # Dropping an added date leaves the rule alone.
    store.update(series.uid, rdates=["2026-10-16T09:00"])
    assert store.delete(series.uid, recurrence_id="2026-10-16T09:00", scope="this") is True
    assert [e.start for e in store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 20))] == [
        "2026-10-12T09:00",
        "2026-10-19T09:00",
    ]
    assert store.get(series.uid).rrule == "FREQ=WEEKLY;BYDAY=MO"

    # No rule at all: "this, and also these" is a legal event, not a broken one.
    workshop_store = CalendarStore(tmp_path / "workshop")
    workshop = workshop_store.add(summary="Workshop", start="2026-10-12T09:00")
    workshop_store.update(workshop.uid, rdates=["2026-10-20T09:00", "2026-11-03T09:00"])
    rows = workshop_store.occurrences(datetime(2026, 10, 12), datetime(2026, 11, 4))
    assert [e.start for e in rows] == ["2026-10-12T09:00", "2026-10-20T09:00", "2026-11-03T09:00"]
    assert all(e.recurrence_id for e in rows[1:])

    # An all-day added date keeps its shape, in the file and in the expansion.
    market_store = CalendarStore(tmp_path / "market")
    market = market_store.add(summary="Market", start="2026-10-12")
    market_store.update(market.uid, rdates=["2026-10-20"])
    reloaded = CalendarStore(tmp_path / "market").get(market.uid)
    rows = market_store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 21))
    assert reloaded is not None and reloaded.rdates == ["2026-10-20"]
    assert [e.start for e in rows] == ["2026-10-12", "2026-10-20"]
    assert all(e.all_day for e in rows)

    # Splitting a series keeps each added date on its own side.
    split_store = CalendarStore(tmp_path / "split")
    split = split_store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    split_store.update(split.uid, rdates=["2026-10-14T09:00", "2026-10-21T09:00"])
    tail = split_store.update(
        split.uid, recurrence_id="2026-10-19T09:00", scope="future", summary="Standup (late)"
    )
    assert tail is not None
    assert split_store.get(split.uid).rdates == ["2026-10-14T09:00"]
    assert tail.rdates == ["2026-10-21T09:00"]
    assert [
        (e.start, e.summary)
        for e in split_store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 26))
    ] == [
        ("2026-10-12T09:00", "Standup"),
        ("2026-10-14T09:00", "Standup"),
        ("2026-10-19T09:00", "Standup (late)"),
        ("2026-10-21T09:00", "Standup (late)"),
    ]

    # Moving one added date leaves the rule and the rest alone.
    moved_store = CalendarStore(tmp_path / "moved")
    moved = moved_store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    moved_store.update(moved.uid, rdates=["2026-10-16T09:00"])
    moved_store.update(
        moved.uid, recurrence_id="2026-10-16T09:00", scope="this", start="2026-10-16T14:00"
    )
    assert [
        e.start for e in moved_store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 20))
    ] == ["2026-10-12T09:00", "2026-10-16T14:00", "2026-10-19T09:00"]

    # The definition view admits to the extra dates, or `list` would contradict
    # what `agenda` then shows.
    view = CalendarStore(tmp_path / "view")
    viewed = view.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    view.update(viewed.uid, rdates=["2026-10-16T09:00", "2026-10-23T09:00"])
    assert "+2 extra dates" in view.series()[0].line()
    one = view.add(summary="Workshop", start="2026-11-02T09:00")
    view.update(one.uid, rdates=["2026-11-05T09:00"])
    assert "+1 extra date" in view.get(one.uid).line()


# -- a refusal that answers the question the model was asking ---------------


async def test_a_write_without_a_target_hands_back_the_id_and_the_call_that_works(
    runtime: Runtime,
) -> None:
    # The acceptance run: the model had the event's name, not its uid, and kept
    # re-sending the same call. The refusal now carries the id and the shape.
    college = runtime.calendar.add(summary="College", start="2026-10-21")
    result = await CalendarTool().run(
        {"op": "update", "summary": "College", "start": "", "query": "", "uid": ""}, runtime
    )
    assert not result.ok
    assert college.uid in result.output, "the id the model needed was not handed back"
    assert "`summary`" in result.output, "the field that was misused was not named"
    assert 'match="College"' in result.output, "the call that would work was not spelled out"

    # The same for a delete that named only the event.
    dentist = runtime.calendar.add(summary="Dentist", start="2026-10-20T09:00")
    result = await CalendarTool().run(
        {"op": "delete", "summary": "Dentist", "start": "", "query": "", "uid": ""}, runtime
    )
    assert not result.ok
    assert dentist.uid in result.output

    # An id under an alias must not read as "you gave me nothing".
    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "2026-10-25", "query": "", "id": "abc123"},
        runtime,
    )
    assert not result.ok
    assert "uid" in result.output and "on" in result.output
    assert "`id`" in result.output, "the wrong key the model sent was not echoed back"
    assert "`start`" in result.output

    # A delete naming no event names every field it misread, plus the calls that
    # would work (the 08:05 turn: name, hour and the verb in `search`'s field).
    contest = runtime.calendar.add(summary="LeetCode Weekly Contest 524", start="2026-10-18T08:00")
    result = await CalendarTool().run(
        {
            "data_class": "public",
            "op": "delete",
            "query": "delete",
            "start": "2026-10-18T08:00",
            "summary": "LeetCode Weekly Contest 524",
        },
        runtime,
    )
    assert not result.ok
    for field in ("`summary`", "`start`", "`query`"):
        assert field in result.output, f"{field} was misused as an address and not named"
    assert f"op=delete uid={contest.uid}" in result.output, "the call that works was not spelled out"
    assert 'op=delete on="2026-10-18"' in result.output, "the day was not offered as a selector"
    assert 'op=delete match="LeetCode Weekly Contest 524"' in result.output

    # "Remove the 8am thing" is `on`, not an hour — and the day is derivable.
    runtime.calendar.add(summary="Optician", start="2026-10-20T09:00")
    result = await CalendarTool().run(
        {"op": "delete", "start": "2026-10-20T09:00", "summary": "", "query": "", "uid": ""},
        runtime,
    )
    assert not result.ok
    assert '`op=delete on="2026-10-20"`' in result.output
    assert "`start`" in result.output and "`query`" not in result.output

    # A recovery one call away travels with the refusal: the ids that do exist.
    keeper = runtime.calendar.add(summary="Dentist follow-up", start="2026-10-20T15:00")
    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "", "query": "", "on": "2026-10-25", "shift": "-7d"},
        runtime,
    )
    assert not result.ok
    assert "2026-10-25" in result.output
    assert keeper.uid in result.output, "the ids that do exist were not handed back"


async def test_an_ambiguous_target_asks_the_owner_and_tells_the_model_how_to_name_one(
    runtime: Runtime,
) -> None:
    first = runtime.calendar.add(summary="Standup", start="2026-10-20T09:00")
    second = runtime.calendar.add(summary="Standup", start="2026-10-21T09:00")

    result = await CalendarTool().run({"op": "delete", "summary": "Standup"}, runtime)

    assert not result.ok
    # The model is told how to point at one.
    assert "Any of these would name it" in result.output
    assert f"op=delete uid={first.uid}" in result.output
    assert 'op=delete match="Standup"' in result.output
    # The owner is asked the one thing the model cannot answer for them.
    question = (result.data or {}).get("question", "")
    assert "which one to remove" in question, "the owner was not asked which event"
    assert first.uid in question and second.uid in question, (
        "two identical-looking events were offered without the ids that tell them apart"
    )
    # And the owner's question never reaches the model as its own instruction.
    assert "which one to remove" not in str(result.for_model())

    # "Which one?" is a false question when only one answers to the name — but
    # the write is still the owner's to confirm.
    dentist = runtime.calendar.add(summary="Dentist", start="2026-10-20T09:00")
    result = await CalendarTool().run({"op": "delete", "summary": "Dentist"}, runtime)
    assert not result.ok
    question = (result.data or {}).get("question", "")
    assert "which one" not in question.lower(), "a false choice was offered"
    assert dentist.uid in question
    assert "Say the word" in question


async def test_a_read_that_picked_the_wrong_op_is_pointed_at_the_right_one(
    runtime: Runtime,
) -> None:
    runtime.calendar.add(summary="Standup", start="2026-10-12T09:00")

    # `get` names an id; naming an event it does not have still gets an answer.
    result = await CalendarTool().run(
        {"op": "get", "summary": "Nonexistent", "start": "", "query": "", "uid": ""}, runtime
    )
    assert not result.ok
    assert "Standup" in result.output, "what does exist was not shown"

    # A read with a window was asking "what is coming", which is `agenda`.
    result = await CalendarTool().run(
        {"op": "get", "summary": "", "start": "", "query": "", "uid": "", "days": 7}, runtime
    )
    assert not result.ok
    assert "op=agenda" in result.output
    assert "days=7" in result.output

    # With nothing at all, it still names a way in.
    result = await CalendarTool().run(
        {"op": "get", "summary": "", "start": "", "query": "", "uid": ""}, runtime
    )
    assert not result.ok
    assert "op=search" in result.output and "op=list" in result.output


# -- a date that has already gone --------------------------------------------


async def test_a_past_one_off_is_refused_with_today_named_and_a_way_out(
    runtime: Runtime,
) -> None:
    # "Sunday, which is tomorrow" came back a week gone; refusing it is the
    # difference between a wrong date the owner sees and one nobody ever sees.
    gone = (date.today() - timedelta(days=7)).isoformat()
    result = await CalendarTool().run(
        {"op": "create", "summary": "Partner's house", "start": f"{gone}T08:00"}, runtime
    )
    assert not result.ok
    assert gone in result.output, "the refusal did not name the date it read"
    assert "past" in result.output
    assert runtime.calendar.series() == [], "a past event was written anyway"

    # A refusal that only says "past" leaves the model guessing again, so it says
    # which day it is now.
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    result = await CalendarTool().run(
        {"op": "create", "summary": "Friends", "start": f"{yesterday}T18:00"}, runtime
    )
    assert not result.ok
    assert date.today().strftime("%a %d %b") in result.output, (
        "the model was told a date had passed but not which day it is now"
    )

    # "Add the dentist visit I had last Tuesday" is a real request: `allow_past`
    # is how a caller that knows the owner's words answers.
    older = (date.today() - timedelta(days=3)).isoformat()
    result = await CalendarTool().run(
        {
            "op": "create",
            "summary": "Dentist",
            "start": f"{older}T09:00",
            "allow_past": True,
        },
        runtime,
    )
    assert result.ok, result.output
    assert [e.summary for e in runtime.calendar.series()] == ["Dentist"]

    # A series is the one shape where a past start is the normal case, so the
    # guard must not touch it.
    long_ago = (date.today() - timedelta(days=60)).isoformat()
    result = await CalendarTool().run(
        {
            "op": "create",
            "summary": "Standup",
            "start": f"{long_ago}T09:00",
            "rrule": "FREQ=WEEKLY;BYDAY=FR",
        },
        runtime,
    )
    assert result.ok, result.output
