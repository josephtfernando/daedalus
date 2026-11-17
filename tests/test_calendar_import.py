"""Bringing an outside calendar in (SPEC §5.5).

The owner's gcal export is the reason this exists, so the sample below is shaped
like one: an ``X-WR-CALNAME``, a ``VTIMEZONE``, ``TZID``s, a rule, an alarm, and
an override that shares its series' ``uid``. The properties under test are the
ones that make an import *usable* — provenance that does not lie, an override
that lands with its series, and a second run that changes nothing.
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


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_an_export_arrives_whole_with_its_rules_alarms_and_overrides(
    store: CalendarStore, export: Path, tmp_path: Path
) -> None:
    report = store.import_ics(export)

    # The events arrive, and the unreadable component is counted, not copied.
    assert report.events == 3
    assert report.added == 3
    assert [e.summary for e in store.series()] == ["Standup", "Christmas"]
    assert report.ignored == 1
    assert "VTODO" not in store.ics_path.read_text(encoding="utf-8")

    # The rule, the alarm and the zone travel with the series.
    series = store.get(next(e.uid for e in store.series() if e.summary == "Standup"))
    assert series is not None
    assert series.rrule == "FREQ=WEEKLY;BYDAY=WE"
    assert [r.offset_minutes for r in series.reminders] == [10]
    assert series.tzid == "Europe/London"
    assert report.zones == 1
    assert store.ics_path.read_text(encoding="utf-8").count("BEGIN:VTIMEZONE") == 1

    # The override shares the uid: a moved instance, and only its own week is moved.
    assert [(e.summary, e.start) for e in store.overrides(series.uid)] == [
        ("Standup (moved)", "2026-11-11T11:00")
    ]
    rows = store.occurrences(datetime(2026, 11, 4), datetime(2026, 11, 12))
    assert [(e.start, e.summary) for e in rows] == [
        ("2026-11-04T09:30", "Standup"),
        ("2026-11-11T11:00", "Standup (moved)"),
    ]

    # An all-day event keeps its shape.
    holiday = store.get(next(e.uid for e in store.series() if e.summary == "Christmas"))
    assert holiday is not None
    assert holiday.all_day is True
    assert holiday.start == "2026-12-25"

    # RDATE is not decoration: the dates in the file arrive as occurrences.
    rdates = CalendarStore(tmp_path / "rdates")
    rdates.import_ics(
        _write(
            tmp_path / "rdate.ics",
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Google Inc//Google Calendar//EN\r\n"
            "BEGIN:VEVENT\r\nUID:rdate@google.com\r\nDTSTART:20261102T090000\r\n"
            "DTEND:20261102T093000\r\nSUMMARY:Workshop\r\nRRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
            "RDATE:20261105T090000,20261112T090000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        )
    )
    assert [e.start for e in rdates.occurrences(datetime(2026, 11, 2), datetime(2026, 11, 16))] == [
        "2026-11-02T09:00",
        "2026-11-05T09:00",
        "2026-11-09T09:00",
        "2026-11-12T09:00",
    ]
    assert rdates.ics_path.read_text(encoding="utf-8").replace("\r\n ", "").count("RDATE:") == 1


def test_the_import_reports_what_it_did_and_where_the_events_came_from(
    store: CalendarStore, export: Path, tmp_path: Path
) -> None:
    text = store.import_ics(export).render()
    assert "3 events" in text
    assert "owner@gmail.com" in text
    assert "added 3" in text
    assert "1 time zone rule" in text

    # Provenance that lies is worse than none: nothing here was written by us.
    adopted = store.get(next(e.uid for e in store.series() if e.summary == "Standup"))
    assert adopted is not None
    assert adopted.calendar == "owner@gmail.com"
    assert adopted.authority == "imported"

    # Round-tripping our own file must not relabel the owner's own events.
    own = CalendarStore(tmp_path / "own")
    own.add(summary="Dentist", start="2026-11-02T09:00")
    fresh = CalendarStore(tmp_path / "fresh")
    report = fresh.import_ics(own.ics_path)
    assert report.added == 1
    assert fresh.series()[0].authority == "internal"
    assert fresh.series()[0].calendar == "Daedalus"


def test_importing_the_same_file_twice_changes_nothing(store: CalendarStore, export: Path) -> None:
    store.import_ics(export)
    before = store.ics_path.read_text(encoding="utf-8")

    report = store.import_ics(export)

    assert (report.added, report.replaced, report.skipped) == (0, 0, 3)
    assert store.ics_path.read_text(encoding="utf-8") == before
    assert store.ics_path.read_text(encoding="utf-8").count("BEGIN:VTIMEZONE") == 1


def test_the_import_options_decide_who_wins_and_what_may_be_spoken(
    store: CalendarStore, tmp_path: Path
) -> None:
    # `replace` lets a later export win over the one imported before it.
    first = _write(
        tmp_path / "first.ics", EXPORT.replace("SUMMARY:Standup", "SUMMARY:Standup (old)")
    )
    store.import_ics(first)
    report = store.import_ics(_write(tmp_path / "second.ics", EXPORT), replace=True)
    assert report.replaced == 3
    assert sorted(e.summary for e in store.series()) == ["Christmas", "Standup"]

    # An import can be kept out of remote prompts entirely.
    local_only = CalendarStore(tmp_path / "local")
    report = local_only.import_ics(_write(tmp_path / "third.ics", EXPORT), data_class=DataClass.LOCAL_ONLY)
    assert report.local_only == 3
    assert local_only.search("standup", allow_local_only=False) == []
    assert len(local_only.search("standup", allow_local_only=True)) == 1


def test_something_that_is_not_a_calendar_or_does_not_exist_imports_nothing(
    store: CalendarStore, tmp_path: Path
) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_text("just some notes, no calendar here\n", encoding="utf-8")

    report = store.import_ics(junk)

    assert report.events == 0
    assert "no events" in report.render()
    assert store.series() == []

    with pytest.raises(ValueError):
        store.import_ics(tmp_path / "nowhere.ics")
