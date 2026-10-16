"""The internal calendar (SPEC §5.5).

The property under test throughout: ``events.ics`` is the truth and the store
is a guest in it. A tool write must not flatten a hand edit, and an edit made
outside the tool must show up on the next read — otherwise "file-first" is a
claim about the *format*, not about who owns the data.
"""

from __future__ import annotations

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
