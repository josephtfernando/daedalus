"""Bringing an outside calendar in (SPEC §5.5).

The owner's gcal export is the reason this exists, so the sample below is shaped
like one: an ``X-WR-CALNAME``, a ``VTIMEZONE`` per zone, ``TZID``s, a rule, an
alarm, and an override that shares its series' ``uid``. The properties under
test are the ones that make an import *usable* — provenance that does not lie,
an override that lands with its series, and a second run that changes nothing.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from daedalus.calendar.store import CalendarStore
from daedalus.security.provenance import DataClass

EXPORT = """\
BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
CALSCALE:GREGORIAN
METHOD:PUBLISH
X-WR-CALNAME:owner@gmail.com
X-WR-TIMEZONE:Europe/London
BEGIN:VTIMEZONE
TZID:Europe/London
BEGIN:STANDARD
TZOFFSETFROM:+0100
TZOFFSETTO:+0000
DTSTART:19701025T020000
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
DTSTART;TZID=Europe/London:20261104T093000
DTEND;TZID=Europe/London:20261104T100000
DTSTAMP:20261001T120000Z
UID:standup@google.com
SUMMARY:Standup
RRULE:FREQ=WEEKLY;BYDAY=WE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:This is a reminder
TRIGGER:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Europe/London:20261111T110000
DTEND;TZID=Europe/London:20261111T113000
DTSTAMP:20261001T120000Z
UID:standup@google.com
RECURRENCE-ID;TZID=Europe/London:20261111T093000
SUMMARY:Standup (moved)
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261225
DTSTAMP:20261001T120000Z
UID:christmas@google.com
SUMMARY:Christmas
END:VEVENT
BEGIN:VTODO
UID:not-an-event@google.com
SUMMARY:Buy milk
END:VTODO
END:VCALENDAR
"""


@pytest.fixture
def export(tmp_path: Path) -> Path:
    path = tmp_path / "owner.ics"
    path.write_text(EXPORT, encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path: Path) -> CalendarStore:
    return CalendarStore(tmp_path / "calendar")


def test_the_events_arrive(store: CalendarStore, export: Path) -> None:
    report = store.import_ics(export)

    assert report.events == 3
    assert report.added == 3
    assert [e.summary for e in store.series()] == ["Standup", "Christmas"]


def test_an_import_keeps_the_name_of_the_calendar_it_came_from(
    store: CalendarStore, export: Path
) -> None:
    """Provenance that lies is worse than none: nothing here was written by us."""
    store.import_ics(export)
    adopted = store.get([e.uid for e in store.series() if e.summary == "Standup"][0])

    assert adopted is not None
    assert adopted.calendar == "owner@gmail.com"
    assert adopted.authority == "imported"


def test_a_zone_travels_with_its_events(store: CalendarStore, export: Path, tmp_path: Path) -> None:
    report = store.import_ics(export)

    assert report.zones == 1
    assert store.ics_path.read_text(encoding="utf-8").count("BEGIN:VTIMEZONE") == 1
    event = store.get([e.uid for e in store.series() if e.summary == "Standup"][0])
    assert event is not None and event.tzid == "Europe/London"


def test_a_rule_an_alarm_and_an_override_survive_the_trip(
    store: CalendarStore, export: Path
) -> None:
    store.import_ics(export)
    series = store.get([e.uid for e in store.series() if e.summary == "Standup"][0])

    assert series is not None
    assert series.rrule == "FREQ=WEEKLY;BYDAY=WE"
    assert [r.offset_minutes for r in series.reminders] == [10]
    # The override shares the uid: it is a moved instance, not a second event.
    moved = store.overrides(series.uid)
    assert [(e.summary, e.start) for e in moved] == [("Standup (moved)", "2026-11-11T11:00")]


def test_the_moved_instance_replaces_only_its_own_week(
    store: CalendarStore, export: Path
) -> None:
    store.import_ics(export)

    rows = store.occurrences(datetime(2026, 11, 4), datetime(2026, 11, 12))

    assert [(e.start, e.summary) for e in rows] == [
        ("2026-11-04T09:30", "Standup"),
        ("2026-11-11T11:00", "Standup (moved)"),
    ]


def test_an_all_day_event_keeps_its_shape(store: CalendarStore, export: Path) -> None:
    store.import_ics(export)
    holiday = store.get([e.uid for e in store.series() if e.summary == "Christmas"][0])

    assert holiday is not None
    assert holiday.all_day is True
    assert holiday.start == "2026-12-25"


def test_importing_the_same_file_twice_changes_nothing(
    store: CalendarStore, export: Path
) -> None:
    store.import_ics(export)
    before = store.ics_path.read_text(encoding="utf-8")

    report = store.import_ics(export)

    assert (report.added, report.replaced, report.skipped) == (0, 0, 3)
    assert store.ics_path.read_text(encoding="utf-8") == before
    assert store.ics_path.read_text(encoding="utf-8").count("BEGIN:VTIMEZONE") == 1


def test_replacing_lets_a_later_export_win(store: CalendarStore, tmp_path: Path) -> None:
    first = tmp_path / "first.ics"
    first.write_text(EXPORT.replace("SUMMARY:Standup", "SUMMARY:Standup (old)"), encoding="utf-8")
    store.import_ics(first)

    report = store.import_ics(_write(tmp_path / "second.ics", EXPORT), replace=True)

    assert report.replaced == 3
    assert sorted(e.summary for e in store.series()) == ["Christmas", "Standup"]


def test_import_can_keep_the_history_out_of_prompts(
    store: CalendarStore, export: Path
) -> None:
    report = store.import_ics(export, data_class=DataClass.LOCAL_ONLY)

    assert report.local_only == 3
    assert store.search("standup", allow_local_only=False) == []
    assert len(store.search("standup", allow_local_only=True)) == 1


def test_a_daedalus_export_keeps_its_own_provenance(
    store: CalendarStore, tmp_path: Path
) -> None:
    """Round-tripping our own file must not relabel the owner's own events."""
    store.add(summary="Dentist", start="2026-11-02T09:00")

    fresh = CalendarStore(tmp_path / "fresh")
    report = fresh.import_ics(store.ics_path)

    assert report.added == 1
    assert fresh.series()[0].authority == "internal"
    assert fresh.series()[0].calendar == "Daedalus"


def test_something_that_is_not_a_calendar_imports_nothing(store: CalendarStore, tmp_path: Path) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_text("just some notes, no calendar here\n", encoding="utf-8")

    report = store.import_ics(junk)

    assert report.events == 0
    assert "no events" in report.render()
    assert store.series() == []


def test_a_missing_file_is_reported_not_guessed(store: CalendarStore, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        store.import_ics(tmp_path / "nowhere.ics")


def test_a_component_the_store_cannot_read_is_counted(store: CalendarStore, export: Path) -> None:
    """A VTODO is not silently swallowed and not silently copied either."""
    report = store.import_ics(export)

    assert report.ignored == 1
    assert "VTODO" not in store.ics_path.read_text(encoding="utf-8")


def test_the_report_says_what_happened(store: CalendarStore, export: Path) -> None:
    text = store.import_ics(export).render()

    assert "3 events" in text
    assert "owner@gmail.com" in text
    assert "added 3" in text
    assert "1 time zone rule" in text


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path
