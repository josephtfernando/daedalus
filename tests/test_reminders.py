"""Reminders: the alarm the owner set, delivered exactly once.

A reminder is the one kind of speech that is neither the owner talking nor the
agent choosing to. It is an instruction the owner left behind, so it must fire
when they said — including at 03:00 — and it must fire *once*, because a nudge
repeated every tick is worse than no nudge at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from daedalus.calendar.reminders import ReminderLedger, key_for, render
from daedalus.calendar.store import CalendarStore
from daedalus.runtime import Runtime

NIGHT = datetime(2026, 10, 16, 3, 0)  # inside the default quiet window


def _soon(minutes: int) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def test_a_due_alarm_is_found_once_per_instance_and_never_without_one(tmp_path: Path) -> None:
    # A reminder that has come due is found, with the moment it fires at.
    calendar = CalendarStore(tmp_path / "calendar")
    calendar.add(summary="Dentist", start=_soon(10), reminders=[15])
    now = datetime.now()
    due = calendar.due_reminders(now - timedelta(minutes=15), now)
    assert [event.summary for event, _reminder, _at in due] == ["Dentist"]
    fired_at = due[0][2]
    assert now - timedelta(minutes=15) < fired_at <= now

    # One whose time has not arrived, or one with no alarm at all, is left alone.
    quiet = CalendarStore(tmp_path / "quiet")
    quiet.add(summary="Later", start=_soon(180), reminders=[15])
    quiet.add(summary="No alarm", start=_soon(10))
    assert quiet.due_reminders(now - timedelta(minutes=15), now) == []

    # Each instance of a series is its own nudge: Monday and Tuesday differ.
    quiet.add(summary="Standup", start="2026-10-19T09:00", rrule="FREQ=DAILY", reminders=[5])
    monday = quiet.occurrences(datetime(2026, 10, 19), datetime(2026, 10, 20))[0]
    tuesday = quiet.occurrences(datetime(2026, 10, 20), datetime(2026, 10, 21))[0]
    assert key_for(monday, monday.reminders[0]) != key_for(tuesday, tuesday.reminders[0])


async def test_the_scheduler_delivers_a_reminder_exactly_once_whatever_the_hour(
    runtime: Runtime,
) -> None:
    runtime.calendar.add(summary="Dentist", start=_soon(5), reminders=[10])
    now = datetime.now()

    first = await runtime.scheduler.fire_reminders(runtime, now)
    second = await runtime.scheduler.fire_reminders(runtime, now + timedelta(seconds=30))

    assert [r.job for r in first] == ["reminder:Dentist"]
    assert second == [], "the same nudge was delivered twice"
    assert len(runtime.surface.sent) == 1
    assert "Dentist" in runtime.surface.sent[0]

    # The owner set the alarm: quiet hours have no say, and it spends no cap —
    # it is an instruction being passed on, not the agent choosing to speak.
    runtime.calendar.add(summary="Night flight", start="2026-10-16T03:10", reminders=[15])
    results = await runtime.scheduler.fire_reminders(runtime, NIGHT)
    assert [r.job for r in results] == ["reminder:Night flight"], "quiet hours silenced it"
    assert runtime.scheduler.proactive.spoken_today(NIGHT) == 0

    # Reminders fall between ticks; the later tick is the one that owes them.
    runtime.calendar.add(summary="Standup", start="2026-10-16T09:00", reminders=[15])
    late = await runtime.scheduler.fire_reminders(runtime, datetime(2026, 10, 16, 8, 50))
    assert [r.job for r in late] == ["reminder:Standup"]

    # A daemon that was down overnight must not wake the owner with yesterday.
    runtime.calendar.add(summary="Yesterday", start="2026-10-15T09:00", reminders=[15])
    delivered = len(runtime.surface.sent)
    stale = await runtime.scheduler.fire_reminders(runtime, datetime(2026, 10, 16, 8, 0))
    assert stale == []
    assert len(runtime.surface.sent) == delivered


def test_a_reminder_says_what_and_when(runtime: Runtime) -> None:
    event = runtime.calendar.add(summary="Dentist", start="2026-10-20T09:00", reminders=[15])

    text = render(event, event.reminders[0], datetime(2026, 10, 20, 8, 45))

    assert "Dentist" in text
    assert "in 15 minutes" in text
    assert "09:00" in text


def test_the_ledger_remembers_a_nudge_until_it_prunes_it(tmp_path: Path) -> None:
    ledger = ReminderLedger(tmp_path / "reminders.json")
    ledger.note("standup@2026-10-01T09:00|5|DISPLAY", datetime(2026, 10, 1, 8, 55))

    assert ledger.fired("standup@2026-10-01T09:00|5|DISPLAY") is True
    assert ledger.prune(datetime(2026, 10, 5)) == 1
    assert ledger.fired("standup@2026-10-01T09:00|5|DISPLAY") is False
