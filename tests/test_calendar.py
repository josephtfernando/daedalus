"""The internal calendar (SPEC §5.5).

The property under test throughout: ``events.ics`` is the truth and the store
is a guest in it. A tool write must not flatten a hand edit, and an edit made
outside the tool must show up on the next read — otherwise "file-first" is a
claim about the *format*, not about who owns the data.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from daedalus.calendar.store import CalendarStore
from daedalus.calendar import zones
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
    assert reopened.summary == "Dentist"
    assert reopened.start == "2026-10-20T09:00"
    assert reopened.end == "2026-10-20T09:30"
    assert reopened.location == "Bridge St"
    assert reopened.all_day is False


def test_a_date_is_all_day_and_a_moment_is_not(store: CalendarStore) -> None:
    day = store.add(summary="Launch day", start="2026-11-02")
    hour = store.add(summary="Standup", start="2026-11-02T09:00")

    assert day.all_day is True
    assert hour.all_day is False
    assert day.when().endswith("(all day)")


def test_text_with_structural_characters_survives_the_format(
    store: CalendarStore, tmp_path: Path
) -> None:
    title = "Lunch, with Ana; re: budget"
    event = store.add(summary=title, start="2026-10-21T12:00", description="line one\nline two")

    raw = _raw(tmp_path / "calendar" / "events.ics")
    assert "SUMMARY:Lunch\\, with Ana\\; re: budget" in raw, "commas and semicolons must be escaped"

    back = CalendarStore(tmp_path / "calendar").get(event.uid)
    assert back is not None
    assert back.summary == title
    assert back.description == "line one\nline two"


def test_a_hand_edit_survives_the_next_tool_write(
    store: CalendarStore, tmp_path: Path
) -> None:
    """An event added by hand is not flattened by the next tool write."""
    store.add(summary="Standup", start="2026-10-16T09:00")
    path = tmp_path / "calendar" / "events.ics"
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


def test_a_hand_edit_is_visible_to_the_very_next_read(
    store: CalendarStore, tmp_path: Path
) -> None:
    """The index must not shadow the file it derives from."""
    store.add(summary="Standup", start="2026-10-16T09:00")
    path = tmp_path / "calendar" / "events.ics"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "END:VCALENDAR",
            "BEGIN:VEVENT\r\nUID:late\r\nDTSTAMP:20261016T090000Z\r\n"
            "DTSTART:20261019T110000\r\nSUMMARY:Added later\r\nEND:VEVENT\r\nEND:VCALENDAR",
        ),
        encoding="utf-8",
    )

    assert any(e.uid == "late" for e in store.all()), "the stale index hid a hand edit"


def test_a_component_we_do_not_model_is_kept(store: CalendarStore, tmp_path: Path) -> None:
    path = tmp_path / "calendar" / "events.ics"
    store.add(summary="Standup", start="2026-10-16T09:00")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "BEGIN:VEVENT",
            "BEGIN:VTIMEZONE\r\nTZID:Europe/London\r\nEND:VTIMEZONE\r\nBEGIN:VEVENT",
            1,
        ),
        encoding="utf-8",
    )

    store.add(summary="Another", start="2026-10-18T10:00")

    assert "BEGIN:VTIMEZONE" in _raw(path)


def test_the_index_rebuilds_from_the_file(store: CalendarStore) -> None:
    store.add(summary="A", start="2026-10-16T09:00")
    store.add(summary="B", start="2026-10-17T09:00")

    assert store.rebuild_index() == 2
    assert store.count() == 2


def test_delete_rewrites_the_file(store: CalendarStore, tmp_path: Path) -> None:
    event = store.add(summary="Scratch", start="2026-10-16T09:00")

    assert store.delete(event.uid) is True
    assert store.delete(event.uid) is False
    assert store.get(event.uid) is None
    assert "Scratch" not in _raw(tmp_path / "calendar" / "events.ics")


# -- the agenda -------------------------------------------------------------


def test_the_agenda_holds_a_whole_day_and_excludes_the_future_and_past(
    store: CalendarStore,
) -> None:
    store.add(summary="Yesterday", start="2026-10-15T09:00")
    store.add(summary="All day today", start="2026-10-16")
    store.add(summary="Later today", start="2026-10-16T18:00")
    store.add(summary="Next month", start="2026-11-16T09:00")

    titles = [e.summary for e in store.agenda(days=7, now=datetime(2026, 10, 16, 12, 0))]

    assert titles == ["All day today", "Later today"]


def test_update_only_touches_what_was_given(store: CalendarStore) -> None:
    event = store.add(summary="Standup", start="2026-10-16T09:00", description="daily")

    moved = store.update(event.uid, start="2026-10-16T10:00")

    assert moved is not None
    assert moved.start == "2026-10-16T10:00"
    assert moved.description == "daily", "a move blanked a field it was not asked to change"


def test_update_says_so_when_the_event_does_not_exist(store: CalendarStore) -> None:
    assert store.update("nope", summary="x") is None


def test_update_with_nothing_to_change_is_an_error(store: CalendarStore) -> None:
    event = store.add(summary="Standup", start="2026-10-16T09:00")

    with pytest.raises(ValueError):
        store.update(event.uid)


# -- the egress gate --------------------------------------------------------


def test_a_local_only_event_stays_out_of_a_remote_prompt(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Therapy", start=_soon(2), data_class=DataClass.LOCAL_ONLY)
    runtime.calendar.add(summary="Standup", start=_soon(3))

    event = InboundEvent(text="what's today?", user_id="owner")
    remote = build_system_prompt(runtime, event, include_local_only=False)
    assert "Therapy" not in remote, "a local_only event reached a remote-bound prompt"
    assert "Standup" in remote

    local = build_system_prompt(runtime, event, include_local_only=True)
    assert "Therapy" in local


async def test_the_tool_hides_a_local_only_event_from_a_remote_turn(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Therapy", start=_soon(2), data_class=DataClass.LOCAL_ONLY)

    result = await CalendarTool().run({"op": "agenda", "summary": "", "start": ""}, runtime)

    assert result.ok
    assert "Therapy" not in result.output


# -- the tool ---------------------------------------------------------------


async def test_the_tool_creates_reads_and_removes(runtime: Runtime) -> None:
    tool = CalendarTool()
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


async def test_the_tool_refuses_a_create_without_a_title_or_time(runtime: Runtime) -> None:
    result = await CalendarTool().run({"op": "create", "summary": "", "start": _soon()}, runtime)

    assert not result.ok
    assert "summary" in result.output


async def test_the_tool_reports_an_unreadable_time(runtime: Runtime) -> None:
    result = await CalendarTool().run(
        {"op": "create", "summary": "Nope", "start": "next tuesday"}, runtime
    )

    assert not result.ok
    assert "not a time I can read" in result.output


async def test_the_tool_refuses_an_update_with_nothing_to_change(runtime: Runtime) -> None:
    created = await CalendarTool().run(
        {"op": "create", "summary": "Standup", "start": _soon(6)}, runtime
    )

    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "", "uid": created.data["uid"]}, runtime
    )

    assert not result.ok
    assert "at least one field" in result.output


async def test_the_tool_tells_the_model_to_supply_a_uid(runtime: Runtime) -> None:
    result = await CalendarTool().run({"op": "delete", "summary": "", "start": ""}, runtime)

    assert not result.ok
    assert "uid" in result.output


def test_the_file_is_a_valid_calendar_document(store: CalendarStore, tmp_path: Path) -> None:
    store.add(summary="Standup", start="2026-10-16T09:00")

    raw = _raw(tmp_path / "calendar" / "events.ics")

    assert raw.startswith("BEGIN:VCALENDAR\r\n")
    assert "VERSION:2.0" in raw
    assert raw.rstrip().endswith("END:VCALENDAR")
    assert "X-DAEDALUS-AUTHORITY:internal" in raw


def test_sync_state_is_an_empty_seam_until_gcal_exists(store: CalendarStore) -> None:
    """gcal is provisioned, not implemented: the slot exists and says nothing."""
    assert store.sync_state()["cursor"] is None
    assert not (store.root / "sync_state.json").exists(), "an empty cursor file is not state"


# -- recurrence -------------------------------------------------------------


def test_a_repeating_event_shows_up_on_the_day_it_repeats(store: CalendarStore) -> None:
    """The point of the whole feature: "every Monday" answers "what is on the 19th"."""
    store.add(summary="Standup", start="2026-10-12T09:10", rrule="FREQ=WEEKLY;BYDAY=MO")

    monday = store.occurrences(datetime(2026, 10, 19), datetime(2026, 10, 20))
    other = store.occurrences(datetime(2026, 10, 20), datetime(2026, 10, 21))

    assert [e.summary for e in monday] == ["Standup"]
    assert monday[0].start == "2026-10-19T09:10"
    assert other == []


def test_one_definition_is_one_row_however_often_it_repeats(store: CalendarStore) -> None:
    event = store.add(summary="Standup", start="2026-10-12T09:10", rrule="FREQ=DAILY")

    assert store.count() == 1
    assert [e.uid for e in store.series()] == [event.uid]
    assert store.agenda(days=7, now=datetime(2026, 10, 12, 7, 0))[0].line(show_id=True).startswith(
        f"[{event.uid}@2026-10-12T09:10]"
    )


def test_the_agenda_stops_at_the_rule_s_end(store: CalendarStore) -> None:
    store.add(
        summary="Sprint", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T090000"
    )

    titles = [e.start for e in store.agenda(days=21, now=datetime(2026, 10, 12, 0, 0))]

    assert titles == ["2026-10-12T09:00", "2026-10-19T09:00"]


def test_an_excluded_date_is_left_out(store: CalendarStore) -> None:
    store.add(
        summary="Standup",
        start="2026-10-12T09:00",
        rrule="FREQ=DAILY;COUNT=3",
        exdates=["2026-10-13T09:00"],
    )

    starts = [e.start for e in store.agenda(days=7, now=datetime(2026, 10, 12, 0, 0))]

    assert starts == ["2026-10-12T09:00", "2026-10-14T09:00"]


def test_moving_one_instance_leaves_the_series_alone(store: CalendarStore) -> None:
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")

    moved = store.update(
        series.uid,
        recurrence_id="2026-10-19T09:00",
        scope="this",
        start="2026-10-19T11:00",
        end="2026-10-19T11:30",
    )

    assert moved is not None
    week = store.agenda(days=14, now=datetime(2026, 10, 19, 0, 0))
    by_day = {occ.start: occ.summary for occ in week}
    assert by_day["2026-10-19T11:00"] == "Standup"
    assert "2026-10-19T09:00" not in by_day, "the original slot was left behind"
    assert "2026-10-26T09:00" in by_day, "later occurrences must keep the series' time"


def test_moving_from_a_date_onward_splits_the_series(store: CalendarStore) -> None:
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")

    tail = store.update(
        series.uid, recurrence_id="2026-11-02T09:00", scope="future", start="2026-11-02T10:00"
    )

    assert tail is not None and tail.uid != series.uid
    starts = [e.start for e in store.agenda(days=35, now=datetime(2026, 10, 12, 0, 0))]
    assert starts == [
        "2026-10-12T09:00",
        "2026-10-19T09:00",
        "2026-10-26T09:00",
        "2026-11-02T10:00",
        "2026-11-09T10:00",
    ]


def test_dropping_one_instance_leaves_the_rest(store: CalendarStore) -> None:
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=DAILY;COUNT=3")

    assert store.delete(series.uid, recurrence_id="2026-10-13T09:00", scope="this") is True

    starts = [e.start for e in store.agenda(days=7, now=datetime(2026, 10, 12, 0, 0))]
    assert starts == ["2026-10-12T09:00", "2026-10-14T09:00"]


def test_dropping_a_series_takes_its_moved_instances_with_it(store: CalendarStore) -> None:
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    store.update(series.uid, recurrence_id="2026-10-19T09:00", scope="this", start="2026-10-19T11:00")

    assert store.delete(series.uid) is True

    assert store.count() == 0
    assert store.agenda(days=28, now=datetime(2026, 10, 12, 0, 0)) == []


def test_a_rule_that_cannot_be_read_is_refused_at_the_door(store: CalendarStore) -> None:
    with pytest.raises(ValueError):
        store.add(summary="Nope", start="2026-10-12T09:00", rrule="FREQ=HOURLY")


# -- the file speaks iCalendar ---------------------------------------------


def test_a_rule_and_its_exceptions_round_trip(store: CalendarStore, tmp_path: Path) -> None:
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


def test_an_override_is_written_as_its_own_vevent_sharing_the_uid(
    store: CalendarStore, tmp_path: Path
) -> None:
    """gcal reads an override as a second VEVENT with the same UID."""
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    store.update(series.uid, recurrence_id="2026-10-19T09:00", scope="this", start="2026-10-19T11:00")

    raw = _raw(tmp_path / "calendar" / "events.ics")
    assert raw.count("BEGIN:VEVENT") == 2
    assert raw.count(f"UID:{series.uid}") == 2
    assert "RECURRENCE-ID:20261019T090000" in raw

    reread = CalendarStore(tmp_path / "calendar")
    override = reread.overrides(series.uid)[0]
    assert override.start == "2026-10-19T11:00"
    assert reread.get(series.uid).start == "2026-10-12T09:00", "the series was rewritten"


def test_a_reminder_round_trips_and_does_not_touch_the_event(
    store: CalendarStore, tmp_path: Path
) -> None:
    store.add(summary="Standup", start="2026-10-12T09:00", reminders=[15])

    raw = _raw(tmp_path / "calendar" / "events.ics")
    assert "BEGIN:VALARM" in raw and "TRIGGER:-PT15M" in raw

    back = CalendarStore(tmp_path / "calendar").series()[0]
    assert back.reminders[0].offset_minutes == 15
    assert back.reminders[0].lead_text() == "15m before"


def test_a_reminder_s_own_description_does_not_overwrite_the_event_s() -> None:
    """A VALARM carries a DESCRIPTION of its own; lifting it would be a data loss."""
    from daedalus.calendar import ical

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


def test_a_zone_is_kept_with_the_time_it_qualifies(store: CalendarStore, tmp_path: Path) -> None:
    path = tmp_path / "calendar" / "events.ics"
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

    raw = _raw(path)
    assert "DTSTART;TZID=Europe/London:20261017T090000" in raw, "the zone was flattened away"
    back = CalendarStore(tmp_path / "calendar").get("zoned")
    assert back is not None and back.tzid == "Europe/London"


def test_a_multi_day_event_is_on_every_day_it_covers(store: CalendarStore) -> None:
    """A conference from Thursday to Saturday is on Thursday, Friday and Saturday."""
    store.add(summary="Conference", start="2026-10-15", end="2026-10-18")

    friday = store.occurrences(datetime(2026, 10, 16), datetime(2026, 10, 17))

    assert [e.summary for e in friday] == ["Conference"]
    assert friday[0].when() == "Thu 15 Oct – Sat 17 Oct (all day)"


# -- clocks other than this one ---------------------------------------------


def test_a_rule_on_another_clock_keeps_its_hour_across_a_change(store: CalendarStore) -> None:
    """A 09:00 call in London is 09:00 in London either side of the clocks changing."""
    store.add(
        summary="Call", start="2026-03-27T09:00", rrule="FREQ=DAILY", tzid="Europe/London"
    )

    starts = [e.start for e in store.occurrences(datetime(2026, 3, 27), datetime(2026, 3, 31))]

    assert starts == [
        "2026-03-27T09:00",
        "2026-03-28T09:00",
        "2026-03-29T09:00",  # the day the London clocks moved
        "2026-03-30T09:00",
    ]


def test_another_clock_is_named_only_when_it_is_not_this_one(store: CalendarStore) -> None:
    from daedalus.calendar import zones

    if zones.is_local("Asia/Tokyo"):
        pytest.skip("the host clock is Tokyo; the label would rightly be absent")
    foreign = store.add(summary="Call", start="2026-10-20T09:00", tzid="Asia/Tokyo")
    local = store.add(summary="Dentist", start="2026-10-20T09:00")

    assert "Tokyo" in foreign.line(), "a foreign clock must say which clock it is"
    assert "Tokyo" not in local.line()
    assert local.when() == "Tue 20 Oct 09:00"


def test_a_zone_name_tells_the_truth_about_the_instant() -> None:
    """Two clocks, one moment: the conversion the whole layer exists for."""
    from daedalus.calendar import zones

    london = zones.zone_for("Europe/London")
    tokyo = zones.zone_for("Asia/Tokyo")

    assert london is not None and tokyo is not None
    nine_in_london = datetime(2026, 10, 16, 9, 0, tzinfo=london)
    assert nine_in_london.astimezone(tokyo).strftime("%H:%M") == "17:00"


def test_an_unreadable_zone_falls_back_to_local_rather_than_guessing(
    store: CalendarStore, tmp_path: Path
) -> None:
    """A wrong hour delivered confidently is worse than one that is merely local."""
    path = tmp_path / "calendar" / "events.ics"
    store.add(summary="Anchor", start="2026-10-12T09:00")
    path.write_text(
        _raw(path).replace(
            "END:VCALENDAR",
            "BEGIN:VEVENT\r\nUID:weird\r\nDTSTAMP:20261016T090000Z\r\n"
            "DTSTART;TZID=Mars/Olympus:20261020T090000\r\nSUMMARY:Oympiad\r\n"
            "END:VEVENT\r\nEND:VCALENDAR",
        ),
        encoding="utf-8",
    )

    event = CalendarStore(tmp_path / "calendar").get("weird")

    assert event is not None
    assert event.start == "2026-10-20T09:00", "the wall time is kept, not shifted by a guess"
    assert zones.zone_for("Mars/Olympus") is None, "an unknown zone resolves to nothing"


# -- free, busy and doubles -------------------------------------------------


def test_busy_merges_overlapping_events_into_one_stretch(store: CalendarStore) -> None:
    store.add(summary="A", start="2026-10-20T09:00", end="2026-10-20T10:00")
    store.add(summary="B", start="2026-10-20T09:30", end="2026-10-20T10:30")

    blocks = store.busy(datetime(2026, 10, 20), datetime(2026, 10, 21))

    assert [(b[0].strftime("%H:%M"), b[1].strftime("%H:%M")) for b in blocks] == [("09:00", "10:30")]


def test_a_double_booking_is_reported_as_one(store: CalendarStore) -> None:
    store.add(summary="A", start="2026-10-20T09:00", end="2026-10-20T10:00")
    store.add(summary="B", start="2026-10-20T09:30", end="2026-10-20T10:30")

    pairs = store.conflicts(datetime(2026, 10, 20), datetime(2026, 10, 21))

    assert [(first.summary, second.summary) for first, second in pairs] == [("A", "B")]


def test_an_event_marked_free_does_not_block_the_day(store: CalendarStore) -> None:
    store.add(
        summary="Focus time",
        start="2026-10-20T09:00",
        end="2026-10-20T10:00",
        transparency="transparent",
    )

    assert store.busy(datetime(2026, 10, 20), datetime(2026, 10, 21)) == []
    assert store.free(datetime(2026, 10, 20), datetime(2026, 10, 21), minutes=30)


def test_free_finds_the_gaps_between_what_is_booked(store: CalendarStore) -> None:
    store.add(summary="A", start="2026-10-20T09:00", end="2026-10-20T10:00")
    store.add(summary="B", start="2026-10-20T11:00", end="2026-10-20T12:00")

    gaps = store.free(datetime(2026, 10, 20), datetime(2026, 10, 21), minutes=30)

    assert [(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in gaps] == [
        ("10:00", "11:00"),
        ("12:00", "18:00"),
    ]


def test_a_gap_too_short_to_use_is_not_offered(store: CalendarStore) -> None:
    store.add(summary="A", start="2026-10-20T09:00", end="2026-10-20T10:00")
    store.add(summary="B", start="2026-10-20T10:15", end="2026-10-20T18:00")

    assert store.free(datetime(2026, 10, 20), datetime(2026, 10, 21), minutes=30) == []


def test_a_full_day_leaves_no_room(store: CalendarStore) -> None:
    store.add(summary="Conference", start="2026-10-21")

    assert store.free(datetime(2026, 10, 21), datetime(2026, 10, 22), minutes=30) == []


def test_a_repeating_meeting_blocks_its_slot_every_day(store: CalendarStore) -> None:
    store.add(
        summary="Standup", start="2026-10-20T09:00", end="2026-10-20T09:30", rrule="FREQ=DAILY"
    )

    gaps = store.free(datetime(2026, 10, 20), datetime(2026, 10, 23), minutes=30)

    assert [(s.day, s.strftime("%H:%M")) for s, _ in gaps] == [
        (20, "09:30"),
        (21, "09:30"),
        (22, "09:30"),
    ]


# -- search -----------------------------------------------------------------


def test_search_finds_an_event_by_any_of_its_text(store: CalendarStore) -> None:
    store.add(summary="Dentist", start="2026-11-02T09:00", description="Bridge Street clinic")
    store.add(summary="Standup", start="2026-11-02T09:10", location="Zoom")

    assert [e.summary for e in store.search("bridge")] == ["Dentist"]
    assert [e.summary for e in store.search("zoom")] == ["Standup"]
    assert store.search("nothing like this") == []


def test_search_finds_the_series_not_each_instance(store: CalendarStore) -> None:
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    store.update(series.uid, recurrence_id="2026-10-19T09:00", scope="this", start="2026-10-19T11:00")

    rows = store.search("standup")

    assert [e.uid for e in rows] == [series.uid]
    assert rows[0].rrule == "FREQ=WEEKLY;BYDAY=MO"


def test_search_does_not_leak_a_local_only_event(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Therapy", start="2026-11-02T09:00", data_class=DataClass.LOCAL_ONLY)

    assert runtime.calendar.search("therapy", allow_local_only=False) == []
    assert len(runtime.calendar.search("therapy", allow_local_only=True)) == 1


async def test_the_tool_answers_when_the_owner_is_free(runtime: Runtime) -> None:
    runtime.calendar.add(summary="A", start="2026-11-03T09:00", end="2026-11-03T10:00")

    result = await CalendarTool().run(
        {"op": "free", "summary": "", "start": "2026-11-03", "query": "", "days": 1, "minutes": 60},
        runtime,
    )

    assert result.ok, result.output
    assert "10:00" in result.output and "18:00" in result.output


async def test_the_tool_reports_a_double_booking(runtime: Runtime) -> None:
    runtime.calendar.add(summary="A", start="2026-11-04T09:00", end="2026-11-04T10:00")
    runtime.calendar.add(summary="B", start="2026-11-04T09:30", end="2026-11-04T10:30")

    result = await CalendarTool().run(
        {"op": "busy", "summary": "", "start": "2026-11-04", "query": "", "days": 1}, runtime
    )

    assert result.ok
    assert "double-booked" in result.output


async def test_the_tool_searches(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Dentist", start="2026-11-05T09:00", location="Bridge St")

    result = await CalendarTool().run(
        {"op": "search", "summary": "", "start": "", "query": "bridge"}, runtime
    )

    assert result.ok
    assert "Dentist" in result.output


async def test_the_tool_says_so_when_a_search_has_no_words(runtime: Runtime) -> None:
    result = await CalendarTool().run(
        {"op": "search", "summary": "", "start": "", "query": ""}, runtime
    )

    assert not result.ok
    assert "query" in result.output


# -- changing a whole set at once -------------------------------------------


def test_a_bulk_retime_keeps_each_event_s_own_day(store: CalendarStore) -> None:
    """Moving all my standups to 10 must not pile a week onto one morning."""
    first = store.add(summary="Standup", start="2026-10-12T09:00", end="2026-10-12T09:15")
    second = store.add(summary="Standup", start="2026-10-13T09:00", end="2026-10-13T09:15")
    store.add(summary="Dentist", start="2026-10-12T09:00")

    changed = store.bulk_update("standup", at="10:00")

    assert {e.uid for e in changed} == {first.uid, second.uid}
    assert store.get(first.uid).start == "2026-10-12T10:00"  # type: ignore[union-attr]
    assert store.get(second.uid).start == "2026-10-13T10:00"  # type: ignore[union-attr]
    assert store.get(first.uid).end == "2026-10-12T10:15"  # type: ignore[union-attr]
    assert store.get(first.uid).summary == "Standup"  # type: ignore[union-attr]


def test_a_bulk_change_matches_the_name_and_not_the_notes(store: CalendarStore) -> None:
    store.add(summary="Standup", start="2026-10-12T09:00")
    store.add(summary="Dentist", start="2026-10-12T11:00", description="before the standup")

    assert [e.summary for e in store.matching("standup")] == ["Standup"]


def test_a_bulk_retime_leaves_a_whole_day_event_alone(store: CalendarStore) -> None:
    holiday = store.add(summary="Standup holiday", start="2026-10-12")

    assert store.bulk_update("holiday", at="10:00") == []
    assert store.get(holiday.uid).all_day is True  # type: ignore[union-attr]


def test_a_bulk_change_can_rename_the_set(store: CalendarStore) -> None:
    store.add(summary="Standup", start="2026-10-12T09:00")
    store.add(summary="Standup", start="2026-10-13T09:00")

    changed = store.bulk_update("standup", summary="Daily sync")

    assert [e.summary for e in changed] == ["Daily sync", "Daily sync"]
    assert store.matching("standup") == []


def test_a_bulk_retime_carries_the_rule_with_it(store: CalendarStore) -> None:
    store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")

    store.bulk_update("standup", at="10:00")

    rows = store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 13))
    assert [e.start for e in rows] == ["2026-10-12T10:00"]


def test_a_bulk_change_that_matches_nothing_changes_nothing(store: CalendarStore) -> None:
    store.add(summary="Dentist", start="2026-10-12T09:00")

    assert store.bulk_update("standup", at="10:00") == []
    assert store.bulk_delete("standup") == 0
    assert store.get(store.matching("dentist")[0].uid) is not None


def test_a_bulk_change_with_nothing_to_say_is_refused(store: CalendarStore) -> None:
    store.add(summary="Standup", start="2026-10-12T09:00")

    with pytest.raises(ValueError):
        store.bulk_update("standup")


def test_a_bulk_delete_takes_every_match(store: CalendarStore) -> None:
    store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY")
    store.add(summary="Standup", start="2026-10-13T09:00")
    keeper = store.add(summary="Dentist", start="2026-10-14T09:00")

    assert store.bulk_delete("standup") == 2
    assert [e.uid for e in store.series()] == [keeper.uid]


async def test_the_tool_moves_a_whole_set(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Standup", start="2026-10-12T09:00", end="2026-10-12T09:15")

    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "", "query": "", "match": "standup", "at": "10:00"},
        runtime,
    )

    assert result.ok, result.output
    assert "10:00" in result.output
    assert runtime.calendar.matching("standup")[0].start == "2026-10-12T10:00"


async def test_the_tool_refuses_a_bulk_edit_with_one_date(runtime: Runtime) -> None:
    """A shared `start` would move every match onto that day; `at` is the shape."""
    runtime.calendar.add(summary="Standup", start="2026-10-12T09:00")

    result = await CalendarTool().run(
        {
            "op": "update",
            "summary": "",
            "start": "2026-10-20T10:00",
            "query": "",
            "match": "standup",
        },
        runtime,
    )

    assert not result.ok
    assert "at" in result.output
    assert runtime.calendar.matching("standup")[0].start == "2026-10-12T09:00"


async def test_the_tool_deletes_a_whole_set(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Standup", start="2026-10-12T09:00")
    runtime.calendar.add(summary="Standup", start="2026-10-13T09:00")
    runtime.calendar.add(summary="Dentist", start="2026-10-14T09:00")

    result = await CalendarTool().run(
        {"op": "delete", "summary": "", "start": "", "query": "", "match": "standup"}, runtime
    )

    assert result.ok
    assert "2" in result.output
    assert [e.summary for e in runtime.calendar.series()] == ["Dentist"]


async def test_the_approval_prompt_names_the_set(runtime: Runtime) -> None:
    assessment = CalendarTool().assess({"op": "delete", "match": "standup"})

    assert "standup" in assessment.summary
    assert "every" in assessment.summary


# -- addressing a set by the day it falls on, and moving it by a date --------


def test_a_bulk_shift_moves_each_event_by_a_day_and_keeps_its_clock(
    store: CalendarStore,
) -> None:
    """A `shift` is a date delta: every match keeps its own hour and length."""
    first = store.add(summary="Alpha", start="2026-10-25T09:00", end="2026-10-25T10:00")
    second = store.add(summary="Beta", start="2026-10-25T19:00", end="2026-10-25T21:30")
    store.add(summary="Gamma", start="2026-10-26T09:00")

    changed = store.bulk_update("", on="2026-10-25", shift="-7d")

    assert {e.uid for e in changed} == {first.uid, second.uid}
    assert store.get(first.uid).start == "2026-10-18T09:00"  # type: ignore[union-attr]
    assert store.get(first.uid).end == "2026-10-18T10:00"  # type: ignore[union-attr]
    assert store.get(second.uid).start == "2026-10-18T19:00"  # type: ignore[union-attr]
    assert store.get(second.uid).end == "2026-10-18T21:30"  # type: ignore[union-attr]
    assert [e.summary for e in store.occurrences_on("2026-10-25")] == []


def test_a_bulk_shift_moves_a_whole_day_event(store: CalendarStore) -> None:
    """`at` skips an all-day event; a day offset is exactly its shape."""
    holiday = store.add(summary="Market", start="2026-10-25", end="2026-10-27")

    changed = store.bulk_update("", on="2026-10-25", shift="-7d")

    assert [e.uid for e in changed] == [holiday.uid]
    assert store.get(holiday.uid).start == "2026-10-18"  # type: ignore[union-attr]
    assert store.get(holiday.uid).end == "2026-10-20"  # type: ignore[union-attr]
    assert store.get(holiday.uid).all_day is True  # type: ignore[union-attr]


def test_a_day_narrows_a_named_set(store: CalendarStore) -> None:
    """`match` and `on` compose: the name picks the kind, the day picks which."""
    here = store.add(summary="Standup", start="2026-10-25T09:00")
    later = store.add(summary="Standup", start="2026-10-26T09:00")

    changed = store.bulk_update("standup", on="2026-10-25", shift="2d")

    assert [e.uid for e in changed] == [here.uid]
    assert store.get(here.uid).start == "2026-10-27T09:00"  # type: ignore[union-attr]
    assert store.get(later.uid).start == "2026-10-26T09:00"  # type: ignore[union-attr]


def test_a_week_shift_is_spelled_w(store: CalendarStore) -> None:
    event = store.add(summary="Review", start="2026-10-25T09:00")

    store.bulk_update("review", shift="-1w")

    assert store.get(event.uid).start == "2026-10-18T09:00"  # type: ignore[union-attr]


def test_an_unreadable_shift_is_refused_rather_than_guessed(store: CalendarStore) -> None:
    store.add(summary="Review", start="2026-10-25T09:00")

    with pytest.raises(ValueError, match="not a shift I can read"):
        store.bulk_update("review", shift="next tuesday")


# -- a shift carries the series' exceptions ---------------------------------
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


def test_a_shift_moves_the_whole_series_including_what_it_excepts(
    store: CalendarStore,
) -> None:
    """Every instance that was on is still on, a week earlier — none left behind.

    The window is fixed and a weekly rule is not, so the far edge gains the one
    occurrence the rule now reaches into it. What must not change is the rest:
    not one stranded exception (reads as an extra event) and not one skipped day
    come back.
    """
    uid = _weekly_with_exceptions(store)
    before = {
        event.start for event in store.occurrences(datetime(2026, 9, 1), datetime(2026, 12, 1))
    }

    store.update(uid, shift="-7d")

    after = {
        event.start for event in store.occurrences(datetime(2026, 9, 1), datetime(2026, 12, 1))
    }
    earlier = {_by_days(start, -7) for start in before}

    assert earlier <= after, "an instance or an exception was left behind"
    assert after - earlier == {"2026-11-30T09:00"}, "the window gained more than its edge"
    assert "2026-10-12T09:00" not in after, "the skipped day came back"
    assert "2026-10-13T11:00" not in after, "the moved instance stayed at its old date"


def _by_days(start: str, days: int) -> str:
    """`start` moved by whole days, in the shape it arrived in."""
    moment = datetime.fromisoformat(start) + timedelta(days=days)
    return moment.strftime("%Y-%m-%d" if len(start) == 10 else "%Y-%m-%dT%H:%M")


def test_a_shift_keeps_the_skipped_day_skipped(store: CalendarStore) -> None:
    uid = _weekly_with_exceptions(store)

    store.update(uid, shift="-7d")

    assert store.occurrences_on("2026-10-12") == []
    assert [e.start for e in store.occurrences_on("2026-10-19")] == ["2026-10-19T09:00"]


def test_a_shift_moves_a_moved_instances_recurrence_id_with_it(
    store: CalendarStore,
) -> None:
    """The override says *which* occurrence it replaces, so that moves too.

    Its own dates are what the owner edited; the id is what ties it to the rule.
    Moving one without the other detaches it, and the rule then produces its own
    instance at the date the owner had moved away from.
    """
    uid = _weekly_with_exceptions(store)

    store.update(uid, shift="-7d")

    moved = [e for e in store.overrides(uid) if e.start == "2026-10-06T11:00"]
    assert len(moved) == 1
    assert moved[0].recurrence_id == "2026-10-05T09:00"
    assert moved[0].end == "2026-10-06T11:30", "the instance's length was not kept"


def test_a_shifted_series_keeps_the_clock_it_was_given(store: CalendarStore) -> None:
    """A delta moves dates; it is not a chance to re-time anything."""
    uid = _weekly_with_exceptions(store)

    store.update(uid, shift="2d")

    assert [e.start for e in store.occurrences_on("2026-10-07")] == ["2026-10-07T09:00"]
    # The 12th's instance, which the owner had put on the 13th at 11:00.
    assert [e.start for e in store.occurrences_on("2026-10-15")] == ["2026-10-15T11:00"]


def test_an_all_day_series_carries_its_exceptions_as_dates(store: CalendarStore) -> None:
    """The keys of an all-day series are bare dates, and must stay bare."""
    series = store.add(summary="Market", start="2026-10-05", end="2026-10-07", rrule="FREQ=WEEKLY")
    store.update(series.uid, scope="this", recurrence_id="2026-10-12", summary="Big market")

    store.update(series.uid, shift="-7d")

    assert store.get(series.uid).start == "2026-09-28"  # type: ignore[union-attr]
    assert store.get(series.uid).end == "2026-09-30"  # type: ignore[union-attr]
    override = store.overrides(series.uid)[0]
    assert override.recurrence_id == "2026-10-05"
    assert override.start == "2026-10-05"
    assert override.all_day is True


def test_an_explicit_new_start_translates_the_exceptions_too(store: CalendarStore) -> None:
    """Putting a series on a different date moves what it excepts, by definition."""
    uid = _weekly_with_exceptions(store)

    store.update(uid, start="2026-10-07T09:00")

    starts = [e.start for e in store.occurrences(datetime(2026, 10, 1), datetime(2026, 11, 15))]
    assert "2026-10-07T09:00" in starts
    assert "2026-10-15T11:00" in starts, "the moved instance did not follow the series"
    assert "2026-10-13T11:00" not in starts
    assert store.occurrences_on("2026-10-21") == [], "the skipped day did not move"


def test_an_explicit_start_keeps_the_length_the_owner_chose(store: CalendarStore) -> None:
    """Moving an event is a translation, so its end goes with it.

    Before this the end stayed where it was, which silently turned a half-hour
    standup into one that ends before it starts. The same delta is what the
    exceptions of a series are moved by, so it cannot be one rule for the rule
    and another for the instances.
    """
    event = store.add(summary="Review", start="2026-10-05T09:00", end="2026-10-05T09:30")

    store.update(event.uid, start="2026-10-20T14:00")

    assert store.get(event.uid).start == "2026-10-20T14:00"  # type: ignore[union-attr]
    assert store.get(event.uid).end == "2026-10-20T14:30"  # type: ignore[union-attr]


def test_a_shift_touches_only_its_own_series_exceptions(store: CalendarStore) -> None:
    """The uid is the join, so a shift must not reach a sibling series' instances
    just because their dates look similar."""
    uid = _weekly_with_exceptions(store)
    other = store.add(summary="Review", start="2026-10-05T15:00", rrule="FREQ=WEEKLY")
    store.update(other.uid, scope="this", recurrence_id="2026-10-12T15:00", start="2026-10-12T16:00")

    store.update(uid, shift="-7d")

    assert store.get(other.uid).start == "2026-10-05T15:00"  # type: ignore[union-attr]
    assert store.overrides(other.uid)[0].recurrence_id == "2026-10-12T15:00"


def test_shifting_one_instance_leaves_the_series_rule_alone(store: CalendarStore) -> None:
    """The other half of the same rule: `scope=this` moves *a* standup, not the rule."""
    uid = _weekly_with_exceptions(store)

    store.update(uid, scope="this", recurrence_id="2026-10-26T09:00", start="2026-10-27T10:00")

    assert store.get(uid).start == "2026-10-05T09:00"  # type: ignore[union-attr]
    assert [e.start for e in store.occurrences_on("2026-10-27")] == ["2026-10-27T10:00"]


def test_a_tail_carries_only_the_exceptions_after_the_cut(store: CalendarStore) -> None:
    """`future` splits a series; each half keeps the exceptions on its own side."""
    uid = _weekly_with_exceptions(store)

    store.update(uid, scope="future", recurrence_id="2026-10-19T09:00", shift="2d")

    # The instance moved to the 13th stayed with the master, and did not move.
    assert [e.start for e in store.occurrences_on("2026-10-13")] == ["2026-10-13T11:00"]
    # The skipped 19th went with the tail and moved with it — and stayed skipped.
    assert store.occurrences_on("2026-10-21") == []
    starts = [e.start for e in store.occurrences(datetime(2026, 10, 1), datetime(2026, 11, 15))]
    assert "2026-10-28T09:00" in starts, "the tail did not carry on from its new day"
    assert "2026-10-21T09:00" not in starts, "a day the owner had skipped came back"


def test_a_split_hands_its_overrides_to_the_series_that_inherits_them(
    store: CalendarStore,
) -> None:
    """An override belongs to the rule it excepts, and the split moves that line.

    Left pointing at the master — whose rule now stops before the override —
    nothing supersedes the tail's own instance on that day, so one moved standup
    turns into two.
    """
    series = store.add(summary="Standup", start="2026-10-05T09:00", rrule="FREQ=WEEKLY")
    store.update(
        series.uid,
        scope="this",
        recurrence_id="2026-10-26T09:00",
        start="2026-10-27T10:00",
    )

    store.update(series.uid, scope="future", recurrence_id="2026-10-19T09:00", summary="Retro")

    # One standup on the 27th, where the owner had put it, and none left over on
    # the 26th: the tail's own instance there is superseded by the override.
    assert store.occurrences_on("2026-10-26") == []
    assert [e.start for e in store.occurrences_on("2026-10-27")] == ["2026-10-27T10:00"]
    assert sum(
        len(store.occurrences_on(day)) for day in ("2026-10-26", "2026-10-27")
    ) == 1, "the split duplicated a day"
    # And the master does not keep a copy of a day it no longer reaches.
    assert [e.summary for e in store.occurrences_on("2026-10-05")] == ["Standup"]
    assert store.occurrences_on("2026-10-13") == []


def test_a_shift_moves_an_until_with_the_rule_it_ends(store: CalendarStore) -> None:
    """`UNTIL` is a date inside the rule. Leave it and the series is truncated."""
    series = store.add(
        summary="Course",
        start="2026-10-05T18:00",
        rrule="FREQ=WEEKLY;UNTIL=20261102T230000",
    )
    before = [e.start for e in store.occurrences(datetime(2026, 9, 1), datetime(2027, 1, 1))]
    assert before[-1] == "2026-11-02T18:00", "the fixture drifted"

    store.update(series.uid, shift="-7d")

    after = [e.start for e in store.occurrences(datetime(2026, 9, 1), datetime(2027, 1, 1))]
    assert len(after) == len(before), "the series lost or gained an instance"
    assert after[0] == "2026-09-28T18:00"
    assert after[-1] == "2026-10-26T18:00", "the series was cut short at the old UNTIL"


def test_a_count_rule_moves_without_being_recounted(store: CalendarStore) -> None:
    """`COUNT` counts from a start that is moving too, so it needs nothing."""
    series = store.add(
        summary="Course", start="2026-10-05T18:00", rrule="FREQ=WEEKLY;COUNT=3"
    )

    store.update(series.uid, shift="3d")

    starts = [e.start for e in store.occurrences(datetime(2026, 9, 1), datetime(2027, 1, 1))]
    assert starts == ["2026-10-08T18:00", "2026-10-15T18:00", "2026-10-22T18:00"]


def test_a_bulk_shift_carries_the_exceptions_of_every_series_it_moves(
    store: CalendarStore,
) -> None:
    """The tool's `on=… shift=…` form is the one the model reaches for."""
    uid = _weekly_with_exceptions(store)
    on_day = [e.start for e in store.occurrences_on("2026-10-05")]

    store.bulk_update("standup", on="2026-10-05", shift="-7d")

    assert on_day == ["2026-10-05T09:00"], "the fixture drifted"
    assert store.get(uid).start == "2026-09-28T09:00"  # type: ignore[union-attr]
    starts = [
        e.start for e in store.occurrences(datetime(2026, 9, 21), datetime(2026, 11, 15))
    ]
    assert "2026-10-06T11:00" in starts, "the moved instance did not come with it"


def test_a_shifted_and_put_back_series_reads_as_it_started(store: CalendarStore) -> None:
    """A translation is reversible: the file is the truth, and it round-trips."""
    uid = _weekly_with_exceptions(store)
    before = sorted(e.start for e in store.occurrences(datetime(2026, 9, 1), datetime(2026, 12, 1)))

    store.update(uid, shift="-7d")
    store.update(uid, shift="1w")

    after = sorted(e.start for e in store.occurrences(datetime(2026, 9, 1), datetime(2026, 12, 1)))
    assert after == before


def test_an_unreadable_shift_is_refused_after_the_fact_too(store: CalendarStore) -> None:
    """A refusal must not have half-moved anything on its way out."""
    uid = _weekly_with_exceptions(store)

    with pytest.raises(ValueError, match="not a shift I can read"):
        store.update(uid, shift="next tuesday")

    assert store.get(uid).start == "2026-10-05T09:00"  # type: ignore[union-attr]
    assert [e.start for e in store.occurrences_on("2026-10-13")] == ["2026-10-13T11:00"]


async def test_the_tool_shifts_one_event_by_its_uid(runtime: Runtime) -> None:
    """A delta is not only for a set: `uid` + `shift` moves one event by days."""
    event = runtime.calendar.add(
        summary="Review", start="2026-10-25T09:00", end="2026-10-25T10:00"
    )

    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "", "query": "", "uid": event.uid, "shift": "-7d"},
        runtime,
    )

    assert result.ok, result.output
    assert runtime.calendar.get(event.uid).start == "2026-10-18T09:00"  # type: ignore[union-attr]
    assert runtime.calendar.get(event.uid).end == "2026-10-18T10:00"  # type: ignore[union-attr]


async def test_the_tool_moves_a_set_and_the_moved_instance_inside_it(
    runtime: Runtime,
) -> None:
    """The set the model addresses is not always *only* a rule.

    "The standups move a week later" is one call on the day they fall — and the
    standup the owner had already moved to another day is still one of the
    standups. Moving the rule alone would leave the edited one where it was and
    bring back any day that had been skipped, which reads as two new events.
    """
    series = runtime.calendar.add(
        summary="Standup", start="2026-10-05T09:00", end="2026-10-05T09:15", rrule="FREQ=WEEKLY"
    )
    runtime.calendar.update(
        series.uid,
        scope="this",
        recurrence_id="2026-10-12T09:00",
        start="2026-10-13T11:00",
        end="2026-10-13T11:30",
    )

    result = await CalendarTool().run(
        {
            "op": "update",
            "summary": "",
            "start": "",
            "query": "",
            "on": "2026-10-05",
            "shift": "-7d",
        },
        runtime,
    )

    assert result.ok, result.output
    assert [e.start for e in runtime.calendar.occurrences_on("2026-10-06")] == [
        "2026-10-06T11:00"
    ], "the instance the owner had moved was left behind"
    assert runtime.calendar.occurrences_on("2026-10-13") == []
    assert [e.start for e in runtime.calendar.occurrences_on("2026-09-28")] == [
        "2026-09-28T09:00"
    ]


async def test_the_tool_moves_the_two_things_on_sunday_in_one_call(runtime: Runtime) -> None:
    """The owner's own correction, end to end.

    "Nah, wrong Sunday — I meant the coming one" landed on two events that share
    no word to match on and fall on the same day. Addressing that day and
    shifting the set is one call; before this, the model had none to make.
    """
    practice = runtime.calendar.add(
        summary="Guitar practice", start="2026-10-25T09:00", end="2026-10-25T10:00"
    )
    club = runtime.calendar.add(
        summary="Book club", start="2026-10-25T19:00", end="2026-10-25T21:30"
    )
    other = runtime.calendar.add(
        summary="Market run", start="2026-10-24T08:00", end="2026-10-24T09:00"
    )

    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "", "query": "", "on": "2026-10-25", "shift": "-7d"},
        runtime,
    )

    assert result.ok, result.output
    assert runtime.calendar.get(practice.uid).start == "2026-10-18T09:00"  # type: ignore[union-attr]
    assert runtime.calendar.get(practice.uid).end == "2026-10-18T10:00"  # type: ignore[union-attr]
    assert runtime.calendar.get(club.uid).start == "2026-10-18T19:00"  # type: ignore[union-attr]
    assert runtime.calendar.get(other.uid).start == "2026-10-24T08:00"  # type: ignore[union-attr]
    assert runtime.calendar.occurrences_on("2026-10-25") == []


async def test_the_tool_deletes_a_day(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Alpha", start="2026-10-25T09:00")
    runtime.calendar.add(summary="Beta", start="2026-10-25T19:00")
    keeper = runtime.calendar.add(summary="Gamma", start="2026-10-26T09:00")

    result = await CalendarTool().run(
        {"op": "delete", "summary": "", "start": "", "query": "", "on": "2026-10-25"}, runtime
    )

    assert result.ok, result.output
    assert "2" in result.output
    assert [e.uid for e in runtime.calendar.series()] == [keeper.uid]


async def test_the_tool_says_what_it_received_when_it_has_no_target(runtime: Runtime) -> None:
    """An id under an alias must not read as "you gave me nothing"."""
    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "2026-10-25", "query": "", "id": "abc123"},
        runtime,
    )

    assert not result.ok
    assert "uid" in result.output and "on" in result.output
    assert "`id`" in result.output, "the wrong key the model sent was not echoed back"
    assert "`start`" in result.output


async def test_a_bulk_change_that_finds_nothing_hands_back_the_ids(runtime: Runtime) -> None:
    """A recovery one call away travels with the refusal, not after another call."""
    keeper = runtime.calendar.add(summary="Dentist", start="2026-10-20T09:00")

    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "", "query": "", "on": "2026-10-25", "shift": "-7d"},
        runtime,
    )

    assert not result.ok
    assert "2026-10-25" in result.output
    assert keeper.uid in result.output, "the ids that do exist were not handed back"


async def test_the_tool_reports_an_unreadable_shift(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Review", start="2026-10-25T09:00")

    result = await CalendarTool().run(
        {"op": "update", "summary": "", "start": "", "query": "", "on": "2026-10-25", "shift": "next tuesday"},
        runtime,
    )

    assert not result.ok
    assert "not a shift I can read" in result.output


def test_the_approval_prompt_names_the_day(runtime: Runtime) -> None:
    assessment = CalendarTool().assess({"op": "update", "on": "2026-10-25", "shift": "-7d"})

    assert "2026-10-25" in assessment.summary
    assert "every" in assessment.summary


def test_the_prompt_teaches_the_day_and_the_delta(runtime: Runtime) -> None:
    """The model's only vocabulary for the calendar is the context prompt."""
    prompt = build_system_prompt(runtime, InboundEvent(text="move my Sunday", user_id="owner"))

    assert 'on="2026-10-25"' in prompt
    assert 'shift="-7d"' in prompt


# -- dates the rule does not produce ----------------------------------------


def test_an_added_date_is_an_occurrence(store: CalendarStore) -> None:
    """The recurrence set is the rule *plus* RDATE: "every Monday, and also Friday"."""
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    store.update(series.uid, rdates=["2026-10-16T09:00"])

    rows = store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 20))

    assert [(e.start, e.summary) for e in rows] == [
        ("2026-10-12T09:00", "Standup"),
        ("2026-10-16T09:00", "Standup"),
        ("2026-10-19T09:00", "Standup"),
    ]


def test_an_event_can_be_its_own_date_and_three_others(store: CalendarStore) -> None:
    """No rule at all: "this, and also these" is a legal event, not a broken one."""
    series = store.add(summary="Workshop", start="2026-10-12T09:00")
    store.update(series.uid, rdates=["2026-10-20T09:00", "2026-11-03T09:00"])

    rows = store.occurrences(datetime(2026, 10, 12), datetime(2026, 11, 4))

    assert [e.start for e in rows] == ["2026-10-12T09:00", "2026-10-20T09:00", "2026-11-03T09:00"]
    assert all(e.recurrence_id for e in rows[1:])


def test_an_added_date_survives_the_file(store: CalendarStore, tmp_path: Path) -> None:
    """A property the store parses but does not write back is a silent data loss."""
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    store.update(series.uid, rdates=["2026-10-16T09:00"])

    raw = _raw(store.ics_path)
    reopened = CalendarStore(tmp_path / "calendar").get(series.uid)

    assert "RDATE:" in raw.replace("\r\n ", "")
    assert reopened is not None and reopened.rdates == ["2026-10-16T09:00"]


def test_an_added_date_that_lands_on_the_rule_does_not_double(store: CalendarStore) -> None:
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    store.update(series.uid, rdates=["2026-10-19T09:00"])  # a Monday the rule already has

    rows = store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 20))

    assert [e.start for e in rows] == ["2026-10-12T09:00", "2026-10-19T09:00"]


def test_dropping_an_added_date_leaves_the_rule_alone(store: CalendarStore) -> None:
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    store.update(series.uid, rdates=["2026-10-16T09:00"])

    assert store.delete(series.uid, recurrence_id="2026-10-16T09:00", scope="this") is True

    rows = store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 20))
    assert [e.start for e in rows] == ["2026-10-12T09:00", "2026-10-19T09:00"]
    assert store.get(series.uid).rrule == "FREQ=WEEKLY;BYDAY=MO"  # type: ignore[union-attr]


def test_an_all_day_added_date_keeps_its_shape(store: CalendarStore, tmp_path: Path) -> None:
    series = store.add(summary="Market", start="2026-10-12")
    store.update(series.uid, rdates=["2026-10-20"])

    reopened = CalendarStore(tmp_path / "calendar").get(series.uid)
    rows = store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 21))

    assert reopened is not None and reopened.rdates == ["2026-10-20"]
    assert [e.start for e in rows] == ["2026-10-12", "2026-10-20"]
    assert all(e.all_day for e in rows)


def test_splitting_a_series_keeps_each_added_date_on_its_own_side(store: CalendarStore) -> None:
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    store.update(series.uid, rdates=["2026-10-14T09:00", "2026-10-21T09:00"])

    tail = store.update(
        series.uid, recurrence_id="2026-10-19T09:00", scope="future", summary="Standup (late)"
    )

    assert tail is not None
    assert store.get(series.uid).rdates == ["2026-10-14T09:00"]  # type: ignore[union-attr]
    assert tail.rdates == ["2026-10-21T09:00"]
    rows = store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 26))
    assert [(e.start, e.summary) for e in rows] == [
        ("2026-10-12T09:00", "Standup"),
        ("2026-10-14T09:00", "Standup"),
        ("2026-10-19T09:00", "Standup (late)"),
        ("2026-10-21T09:00", "Standup (late)"),
    ]


def test_a_schema_bump_rebuilds_rather_than_serving_an_empty_calendar(
    store: CalendarStore, tmp_path: Path
) -> None:
    """A dropped cache whose old revision still matches is a calendar read as empty.

    The index records the ``.ics`` revision it was built from, so a cache that
    has been emptied *without* clearing that revision looks current — and the
    owner opens their calendar to nothing. Survivable in a test, not on the
    laptop where the events live.
    """
    store.add(summary="Dentist", start="2026-11-02T09:00")
    with sqlite3.connect(store.index.path) as conn:  # as if built by an older shape
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema'")

    reopened = CalendarStore(tmp_path / "calendar")

    assert [e.summary for e in reopened.series()] == ["Dentist"]


def test_moving_one_added_date_leaves_the_rule_and_the_rest_alone(store: CalendarStore) -> None:
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    store.update(series.uid, rdates=["2026-10-16T09:00"])

    store.update(series.uid, recurrence_id="2026-10-16T09:00", scope="this", start="2026-10-16T14:00")

    rows = store.occurrences(datetime(2026, 10, 12), datetime(2026, 10, 20))
    assert [e.start for e in rows] == ["2026-10-12T09:00", "2026-10-16T14:00", "2026-10-19T09:00"]


def test_the_definition_view_admits_to_the_extra_dates(store: CalendarStore) -> None:
    """`list` shows definitions; one that hides its RDATEs would contradict `agenda`."""
    series = store.add(summary="Standup", start="2026-10-12T09:00", rrule="FREQ=WEEKLY;BYDAY=MO")
    store.update(series.uid, rdates=["2026-10-16T09:00", "2026-10-23T09:00"])

    assert "+2 extra dates" in store.series()[0].line()
    one = store.add(summary="Workshop", start="2026-11-02T09:00")
    store.update(one.uid, rdates=["2026-11-05T09:00"])
    assert "+1 extra date" in store.get(one.uid).line()  # type: ignore[union-attr]


# -- a refusal that answers the question the model was asking ---------------


async def test_update_with_only_a_summary_hands_back_the_uid(runtime: Runtime) -> None:
    """The exact loop from the acceptance run, answered in one call.

    The owner said "college is from 8am to 11am". The model had already found
    the event — a `search` had printed `[9eeeb38d18ed] … College` — and then
    sent `op=update summary="College"` with no `uid`, because a name is what an
    event *is* and nothing said that `summary` is what a write *sets*. Told only
    "needs a target", it sent the same call three more times until the guard
    ended the turn. The refusal now carries the id.
    """
    college = runtime.calendar.add(summary="College", start="2026-10-21")

    result = await CalendarTool().run(
        {"op": "update", "summary": "College", "start": "", "query": "", "uid": ""}, runtime
    )

    assert not result.ok
    assert college.uid in result.output, "the id the model needed was not handed back"
    assert "`summary`" in result.output, "the field that was misused was not named"
    assert 'match="College"' in result.output, "the call that would work was not spelled out"


async def test_delete_with_only_a_summary_hands_back_the_uid(runtime: Runtime) -> None:
    dentist = runtime.calendar.add(summary="Dentist", start="2026-10-20T09:00")

    result = await CalendarTool().run(
        {"op": "delete", "summary": "Dentist", "start": "", "query": "", "uid": ""}, runtime
    )

    assert not result.ok
    assert dentist.uid in result.output


async def test_a_get_for_an_event_that_does_not_exist_lists_what_does(runtime: Runtime) -> None:
    """`get` names an id; naming an event it does not have still gets an answer."""
    runtime.calendar.add(summary="Standup", start="2026-10-12T09:00")

    result = await CalendarTool().run(
        {"op": "get", "summary": "Nonexistent", "start": "", "query": "", "uid": ""}, runtime
    )

    assert not result.ok
    assert "Standup" in result.output, "what does exist was not shown"


async def test_a_get_with_a_window_is_pointed_at_agenda(runtime: Runtime) -> None:
    """A read that picked the wrong op is told the right one.

    The failing turn sent `op=get` with `days=7` — it was asking "what is
    coming", which is `agenda`. Telling it that `uid` is missing answers a
    question it did not ask.
    """
    runtime.calendar.add(summary="Standup", start="2026-10-12T09:00")

    result = await CalendarTool().run(
        {"op": "get", "summary": "", "start": "", "query": "", "uid": "", "days": 7}, runtime
    )

    assert not result.ok
    assert "op=agenda" in result.output
    assert "days=7" in result.output


async def test_a_get_with_nothing_at_all_still_names_the_way_in(runtime: Runtime) -> None:
    result = await CalendarTool().run(
        {"op": "get", "summary": "", "start": "", "query": "", "uid": ""}, runtime
    )

    assert not result.ok
    assert "op=search" in result.output and "op=list" in result.output


async def test_a_delete_naming_no_event_names_every_field_it_misread(
    runtime: Runtime,
) -> None:
    """The exact call from the 08:05 turn, and everything it got wrong told back.

    The model had just created "LeetCode Weekly Contest 524" (twice) and wanted
    one of them gone. It sent `op=delete` with `summary` (the name), `start` (the
    hour) and `query="delete"` (the verb, in `search`'s field) — three fields that
    describe an event, none of which can choose one. The old refusal named only
    `summary`, so it re-sent the call four times and the guard ended the turn.
    """
    event = runtime.calendar.add(summary="LeetCode Weekly Contest 524", start="2026-10-18T08:00")

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
    assert f"op=delete uid={event.uid}" in result.output, "the call that works was not spelled out"
    assert 'op=delete on="2026-10-18"' in result.output, "the day was not offered as a selector"
    assert 'op=delete match="LeetCode Weekly Contest 524"' in result.output


async def test_a_delete_that_names_nothing_at_all_is_told_the_day(
    runtime: Runtime,
) -> None:
    """"Remove the 8am thing" is `on`, not an hour — and the day is derivable.

    The model sent `start` as an address twice with no name at all. Rather than
    say only "a target is required", the refusal reads the day out of the hour
    it was given and hands back the call that uses it.
    """
    runtime.calendar.add(summary="Dentist", start="2026-10-20T09:00")

    result = await CalendarTool().run(
        {"op": "delete", "start": "2026-10-20T09:00", "summary": "", "query": "", "uid": ""},
        runtime,
    )

    assert not result.ok
    assert '`op=delete on="2026-10-20"`' in result.output
    assert "`start`" in result.output and "`query`" not in result.output


async def test_an_ambiguous_target_gives_the_model_the_rule_and_the_owner_the_question(
    runtime: Runtime,
) -> None:
    """When the choice is the owner's, the refusal says so — to each of them.

    The model has to be told how to *name* an event, and the owner has to be told
    that two events answer to the name it used. Asking the model the owner's
    question is how a turn ends with "I got stuck, tell me what to try instead"
    while the agent is holding both ids.
    """
    first = runtime.calendar.add(summary="Standup", start="2026-10-20T09:00")
    second = runtime.calendar.add(summary="Standup", start="2026-10-21T09:00")

    result = await CalendarTool().run({"op": "delete", "summary": "Standup"}, runtime)

    assert not result.ok
    # The model is told how to point at one.
    assert "Any of these would name it" in result.output
    assert f"op=delete uid={first.uid}" in result.output
    assert f"op=delete match=\"Standup\"" in result.output
    # The owner is asked the one thing the model cannot answer for them.
    question = (result.data or {}).get("question", "")
    assert "which one to remove" in question, "the owner was not asked which event"
    assert first.uid in question and second.uid in question, (
        "two identical-looking events were offered without the ids that tell them apart"
    )
    # And the owner's question never reaches the model as its own instruction.
    assert "which one to remove" not in str(result.for_model())


async def test_one_match_is_a_confirmation_not_a_choice(runtime: Runtime) -> None:
    """"Which one?" is a false question when only one answers to the name.

    There is nothing to choose, so the refusal must not pretend otherwise -- but
    the write is still the owner's to confirm, and that is the half the loop can
    hand over when the model cannot get there itself.
    """
    dentist = runtime.calendar.add(summary="Dentist", start="2026-10-20T09:00")

    result = await CalendarTool().run({"op": "delete", "summary": "Dentist"}, runtime)

    assert not result.ok
    question = (result.data or {}).get("question", "")
    assert "which one" not in question.lower(), "a false choice was offered"
    assert dentist.uid in question
    assert "Say the word" in question
