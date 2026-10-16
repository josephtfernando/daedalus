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
from daedalus.runtime import Runtime

NIGHT = datetime(2026, 10, 16, 3, 0)  # inside the default quiet window


def _soon(minutes: int) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


# -- finding the ones that are due -----------------------------------------


def test_a_reminder_that_has_come_due_is_found_with_its_own_time(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Dentist", start=_soon(10), reminders=[15])

    now = datetime.now()
    due = runtime.calendar.due_reminders(now - timedelta(minutes=15), now)

    assert [event.summary for event, _reminder, _at in due] == ["Dentist"]
    fired_at = due[0][2]
    assert now - timedelta(minutes=15) < fired_at <= now


def test_a_reminder_whose_time_has_not_arrived_is_left_alone(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Dentist", start=_soon(180), reminders=[15])

    now = datetime.now()

    assert runtime.calendar.due_reminders(now - timedelta(minutes=15), now) == []


def test_an_event_with_no_alarm_never_nudges(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Dentist", start=_soon(10))

    now = datetime.now()

    assert runtime.calendar.due_reminders(now - timedelta(minutes=15), now) == []


def test_each_instance_of_a_series_is_its_own_nudge(runtime: Runtime) -> None:
    """A daily standup reminds on Monday and again on Tuesday, not once for both."""
    series = runtime.calendar.add(
        summary="Standup", start="2026-10-19T09:00", rrule="FREQ=DAILY", reminders=[5]
    )
    assert series.rrule

    monday = runtime.calendar.occurrences(datetime(2026, 10, 19), datetime(2026, 10, 20))[0]
    tuesday = runtime.calendar.occurrences(datetime(2026, 10, 20), datetime(2026, 10, 21))[0]

    assert key_for(monday, monday.reminders[0]) != key_for(tuesday, tuesday.reminders[0])


# -- the last hop -----------------------------------------------------------


async def test_the_scheduler_delivers_a_reminder_once(runtime: Runtime) -> None:
    runtime.calendar.add(summary="Dentist", start=_soon(5), reminders=[10])
    now = datetime.now()

    first = await runtime.scheduler.fire_reminders(runtime, now)
    second = await runtime.scheduler.fire_reminders(runtime, now + timedelta(seconds=30))

    assert [r.job for r in first] == ["reminder:Dentist"]
    assert second == [], "the same nudge was delivered twice"
    assert len(runtime.surface.sent) == 1
    assert "Dentist" in runtime.surface.sent[0]


async def test_a_reminder_is_not_silenced_by_quiet_hours(runtime: Runtime) -> None:
    """The owner set the alarm; the agent's own quiet hours have no say in it."""
    runtime.calendar.add(summary="Night flight", start="2026-10-16T03:10", reminders=[15])

    results = await runtime.scheduler.fire_reminders(runtime, NIGHT)

    assert results, "the reminder did not fire"
    assert runtime.surface.sent, "quiet hours silenced a reminder the owner set"


async def test_a_reminder_does_not_spend_the_proactive_cap(runtime: Runtime) -> None:
    """It is an instruction being passed on, not the agent choosing to speak."""
    runtime.calendar.add(summary="Night flight", start="2026-10-16T03:10", reminders=[15])

    await runtime.scheduler.fire_reminders(runtime, NIGHT)

    assert runtime.scheduler.proactive.spoken_today(NIGHT) == 0


async def test_a_tick_that_is_too_late_rather_than_early_delivers_it(runtime: Runtime) -> None:
    """Reminders fall between ticks; the later tick is the one that owes them."""
    runtime.calendar.add(summary="Dentist", start="2026-10-16T09:00", reminders=[15])

    results = await runtime.scheduler.fire_reminders(runtime, datetime(2026, 10, 16, 8, 50))

    assert [r.job for r in results] == ["reminder:Dentist"]


async def test_a_reminder_long_past_is_not_dug_back_up(runtime: Runtime) -> None:
    """A daemon that was down overnight must not wake the owner with yesterday."""
    runtime.calendar.add(summary="Dentist", start="2026-10-15T09:00", reminders=[15])

    results = await runtime.scheduler.fire_reminders(runtime, datetime(2026, 10, 16, 8, 0))

    assert results == []
    assert runtime.surface.sent == []


# -- what it says, and what it remembers ------------------------------------


def test_a_reminder_says_what_and_when(runtime: Runtime) -> None:
    event = runtime.calendar.add(summary="Dentist", start="2026-10-20T09:00", reminders=[15])

    text = render(event, event.reminders[0], datetime(2026, 10, 20, 8, 45))

    assert "Dentist" in text
    assert "in 15 minutes" in text
    assert "09:00" in text


def test_the_ledger_forgets_nudges_it_no_longer_needs(tmp_path: Path) -> None:
    ledger = ReminderLedger(tmp_path / "reminders.json")
    ledger.note("standup@2026-10-01T09:00|5|DISPLAY", datetime(2026, 10, 1, 8, 55))

    assert ledger.fired("standup@2026-10-01T09:00|5|DISPLAY") is True
    assert ledger.prune(datetime(2026, 10, 5)) == 1
    assert ledger.fired("standup@2026-10-01T09:00|5|DISPLAY") is False
