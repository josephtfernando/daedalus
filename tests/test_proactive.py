"""Proactive speech: quiet hours, a daily cap, and the choice to stay silent.

The two ways a proactive agent is *worse* than a silent one: it speaks at 03:00,
or it speaks every time the timer fires. Both gates are deterministic — SPEC §6
puts the decision in code, never in the model — and both are tested against a
fixed clock, because a test that depends on the hour it happens to run at is a
test that fails at breakfast.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.loop.context import build_system_prompt
from daedalus.model.providers import ChatResult, ScriptedProvider
from daedalus.runtime import Runtime, Session, build_runtime
from daedalus.scheduler.proactive import SILENT, ProactiveState, quiet_hours_cover
from daedalus.security.provenance import DataClass
from daedalus.surface.base import InboundEvent
from daedalus.surface.cli import ScriptedSurface
from daedalus.tools.capability import CapabilityTool
from daedalus.tools.schedule import ScheduleTool

NOON = datetime(2026, 10, 16, 12, 0)  # a Friday, mid-day
NIGHT = datetime(2026, 10, 16, 3, 0)  # inside the default quiet window


@pytest.fixture
def ledger(tmp_path: Path) -> ProactiveState:
    return ProactiveState(tmp_path / "state" / "proactive.json")


# -- quiet hours ------------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "covered"),
    [
        (datetime(2026, 10, 16, 22, 29), False),
        (datetime(2026, 10, 16, 22, 30), True),
        (datetime(2026, 10, 16, 23, 59), True),
        (datetime(2026, 10, 17, 3, 0), True),
        (datetime(2026, 10, 17, 7, 29), True),
        (datetime(2026, 10, 17, 7, 30), False),
        (datetime(2026, 10, 17, 12, 0), False),
    ],
)
def test_quiet_hours_cross_midnight(when: datetime, covered: bool) -> None:
    assert quiet_hours_cover("22:30", "07:30", when) is covered


def test_quiet_hours_within_one_day() -> None:
    assert quiet_hours_cover("09:00", "17:00", datetime(2026, 10, 16, 12, 0)) is True
    assert quiet_hours_cover("09:00", "17:00", datetime(2026, 10, 16, 18, 0)) is False


@pytest.mark.parametrize(
    ("start", "end"), [("", ""), ("22:30", ""), ("", "07:30"), ("bedtime", "07:30"), ("25:00", "07:30")]
)
def test_an_unset_or_unreadable_window_covers_nothing(start: str, end: str) -> None:
    assert quiet_hours_cover(start, end, NIGHT) is False


def test_an_equal_window_is_read_as_not_configured() -> None:
    """A typo must not silently switch the agent off all day.

    The honest way to say "never speak" is `enabled = false`, and a config that
    reads as *always quiet* would be indistinguishable from a broken daemon.
    """
    assert quiet_hours_cover("09:00", "09:00", datetime(2026, 10, 16, 9, 0)) is False


# -- the ledger -------------------------------------------------------------


def test_quiet_hours_stop_a_wake(settings: Settings, ledger: ProactiveState) -> None:
    allowed, why = ledger.may_speak(settings, NIGHT)

    assert allowed is False
    assert "quiet hours" in why


def test_proactive_can_be_switched_off(settings: Settings, ledger: ProactiveState) -> None:
    assert ledger.may_speak(replace(settings, proactive_enabled=False), NOON) == (
        False,
        "proactive messages are off",
    )


def test_the_daily_cap_bites(settings: Settings, ledger: ProactiveState) -> None:
    capped = replace(settings, proactive_max_per_day=2)
    ledger.note_spoken(NOON, "one")
    ledger.note_spoken(NOON, "two")

    allowed, why = ledger.may_speak(capped, NOON)

    assert allowed is False
    assert "cap" in why


def test_the_cap_resets_with_the_day(settings: Settings, ledger: ProactiveState) -> None:
    capped = replace(settings, proactive_max_per_day=1)
    ledger.note_spoken(NOON, "hello")

    assert ledger.may_speak(capped, NOON)[0] is False
    assert ledger.may_speak(capped, datetime(2026, 10, 17, 12, 0))[0] is True


def test_the_last_message_is_remembered_for_the_next_one(ledger: ProactiveState) -> None:
    ledger.note_spoken(NOON, "Standup in ten minutes.")

    assert ledger.last_text() == "Standup in ten minutes."
    assert ledger.spoken_today(NOON) == 1


def test_a_corrupt_ledger_does_not_silence_the_agent(
    settings: Settings, ledger: ProactiveState
) -> None:
    """A hand-edited file that no longer parses must not disarm the gate."""
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text("{not json", encoding="utf-8")

    assert ledger.spoken_today(NOON) == 0
    assert ledger.last_text() == ""
    assert ledger.may_speak(settings, NOON)[0] is True


# -- the scheduler's last hop ----------------------------------------------


async def _add_wake(runtime: Runtime, name: str, target: str = "the daily check-in") -> None:
    result = await ScheduleTool().run(
        {"op": "add", "name": name, "cron": "* * * * *", "kind": "wake", "target": target},
        runtime,
    )
    assert result.ok, result.output


async def _add_alert(runtime: Runtime, name: str = "alert") -> None:
    run_py = (
        "import json, sys\n"
        'json.dump({"result": {"ok": True}, "state_next": {},'
        ' "notifications": [{"text": "Disk / is at 4.1% free."}]}, sys.stdout)\n'
    )
    result = await CapabilityTool().run(
        {
            "op": "author",
            "name": name,
            "description": "an alerting capability",
            "run_py": run_py,
            "schedule": "* * * * *",
        },
        runtime,
    )
    assert result.ok, result.output
    assert result.data["verified"] is True, "it must clear the Melinoë gate before it may run"


async def test_a_wake_in_quiet_hours_never_reaches_the_owner(runtime: Runtime) -> None:
    await _add_wake(runtime, "night")

    async def handler(prompt: str) -> str:
        return "you should be asleep"

    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=NIGHT)

    assert runtime.surface.sent == [], "the agent spoke during quiet hours"
    woke = next(r for r in results if r.job == "night")
    assert woke.ran is False
    assert "quiet" in woke.detail, "the reason for the silence was not recorded"


async def test_a_wake_at_a_reasonable_hour_is_sent_and_counted(runtime: Runtime) -> None:
    await _add_wake(runtime, "midday")

    async def handler(prompt: str) -> str:
        return "Standup in ten minutes."

    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=NOON)

    assert runtime.surface.sent == ["Standup in ten minutes."]
    assert runtime.scheduler.proactive.spoken_today(NOON) == 1
    assert runtime.scheduler.proactive.last_text() == "Standup in ten minutes."
    assert next(r for r in results if r.job == "midday").detail == "spoke"


async def test_a_second_pinned_tick_is_judged_by_the_pinned_clock_not_the_wall_clock(
    runtime: Runtime,
) -> None:
    """A tick given a time must be governed by *that* time.

    Each run used to record the job's `last_run` from the wall clock, which makes
    a pinned sequence contradict itself: the selftest probe ticks a silent 03:00
    and then a noon that has to speak. It passed all morning and went red at
    12:01, because noon had become "already run". A health gate that turns red at
    lunchtime is a gate the owner learns to ignore.

    Both times here are deliberately in the *past*: an implementation that reads
    the wall clock judges the second tick already-run whenever this suite happens
    to run, so the test fails on the old behaviour rather than only sometimes.
    """
    await _add_wake(runtime, "midday")

    async def handler(prompt: str) -> str:
        return "Standup in ten minutes."

    night = datetime(2020, 1, 6, 3, 0)
    noon = datetime(2020, 1, 6, 12, 0)

    await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=night)
    assert runtime.surface.sent == [], "the night tick should have stayed quiet"

    await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=noon)

    assert runtime.surface.sent == ["Standup in ten minutes."], (
        "a pinned tick was judged already-run against the wall clock"
    )


async def test_a_job_does_not_run_twice_for_the_same_pinned_minute(runtime: Runtime) -> None:
    """The other half: pinning must not defeat the dedup it was hiding behind."""
    await _add_wake(runtime, "midday")

    async def handler(prompt: str) -> str:
        return "Standup in ten minutes."

    when = datetime(2020, 1, 6, 12, 0)
    await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=when)
    second = await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=when)

    assert runtime.surface.sent == ["Standup in ten minutes."], "the same minute spoke twice"
    # Recording the tick's own time makes the job not-due for that minute, which
    # is the first line of defence; the wake log is the second, for the sleep and
    # double-tick cases where `last_run` never got written.
    assert "midday" not in [result.job for result in second]
    assert runtime.scheduler.log.seen(f"midday:{when.strftime('%Y%m%dT%H%M')}")


async def test_the_prompt_reaches_the_wake_handler(runtime: Runtime) -> None:
    """The job's `target` is *what brought it here*, and it must survive the hop."""
    await _add_wake(runtime, "midday", target="watch the disk")
    seen: list[str] = []

    async def handler(prompt: str) -> str:
        seen.append(prompt)
        return "ok"

    await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=NOON)

    assert seen == ["watch the disk"]


async def test_the_cap_stops_a_wake_once_the_day_is_spent(runtime: Runtime) -> None:
    await _add_wake(runtime, "cadence")
    for index in range(runtime.settings.proactive_max_per_day):
        runtime.scheduler.proactive.note_spoken(NOON, f"message {index}")

    async def handler(prompt: str) -> str:
        return "one more"

    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=NOON)

    assert runtime.surface.sent == []
    spent = next(r for r in results if r.job == "cadence")
    assert spent.ran is False
    assert "cap" in spent.detail


async def test_the_agent_may_look_and_decide_to_stay_silent(runtime: Runtime) -> None:
    await _add_wake(runtime, "quiet_check")

    async def handler(prompt: str) -> str:
        return ""

    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=NOON)

    assert runtime.surface.sent == [], "an empty wake produced a message"
    quiet = next(r for r in results if r.job == "quiet_check")
    assert quiet.ok is True, "choosing silence is a decision, not a failure"
    assert "nothing worth saying" in quiet.detail
    assert runtime.scheduler.proactive.spoken_today(NOON) == 0, "silence spent a message"


async def test_a_capability_alert_is_not_a_wake_and_is_not_silenced(runtime: Runtime) -> None:
    """An alert fires when it fires — the owner asked it to speak up."""
    await _add_alert(runtime)

    await runtime.scheduler.dispatch_tick(runtime, when=NIGHT)

    assert runtime.surface.sent, "quiet hours silenced a capability's alert"


# -- composing the message -------------------------------------------------


async def test_a_wake_that_chooses_silence_returns_nothing(settings: Settings) -> None:
    rt = build_runtime(
        settings,
        surface=ScriptedSurface(approve=True),
        provider=ScriptedProvider([ChatResult(content=SILENT)]),
    )

    assert await rt.wake("standup check") == ""


async def test_a_wake_with_something_to_say_returns_it(settings: Settings) -> None:
    rt = build_runtime(
        settings,
        surface=ScriptedSurface(approve=True),
        provider=ScriptedProvider([ChatResult(content="Standup in ten minutes.")]),
    )

    assert await rt.wake("standup check") == "Standup in ten minutes."


async def test_a_wake_cannot_inherit_a_previous_turns_local_only_permission(
    settings: Settings,
) -> None:
    """The briefing is composed before the wake's chain is resolved.

    So it must not borrow `local_only` rights from whatever turn ran last: a DM
    that happened to route through a local model would otherwise hand the next
    wake permission to put `local_only` values into a remotely-bound prompt.
    """
    rt = build_runtime(settings, surface=ScriptedSurface(approve=True))
    rt.session = Session(seat="Daedalus", local_only_ok=True)
    soon = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    rt.calendar.add(summary="Therapy", start=soon, data_class=DataClass.LOCAL_ONLY)

    assert "Therapy" not in rt.briefing("check in")


def test_the_briefing_carries_the_facts_a_fresh_message_needs(settings: Settings) -> None:
    """Not a template — but not an amnesiac either."""
    rt = build_runtime(settings, surface=ScriptedSurface(approve=True))
    rt.scheduler.proactive.note_spoken(NOON, "Standup in ten minutes.")
    soon = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    rt.calendar.add(summary="Dentist", start=soon)

    briefing = rt.briefing("the morning check-in")

    assert "speaking first" in briefing, "it was not told nobody asked for this"
    assert "Dentist" in briefing, "the wake has no idea what is on today"
    assert "Standup in ten minutes." in briefing, "it cannot know it may repeat itself"
    assert "Do not repeat it" in briefing
    assert SILENT in briefing, "it was never offered silence"
    assert "the morning check-in" in briefing, "the reason for the wake was dropped"


def test_the_prompt_tells_the_agent_it_may_speak_first(settings: Settings) -> None:
    """A feature the agent is never told about is not built.

    Free text is the only interface, so the wake path is reachable only if the
    system prompt explains it — including that it is bounded, and that silence is
    a legitimate answer.
    """
    rt = build_runtime(settings, surface=ScriptedSurface(approve=True))

    prompt = build_system_prompt(rt, InboundEvent(text="hi", user_id="owner"))
    flat = " ".join(prompt.split())  # the prompt is hard-wrapped

    assert "speak first" in flat
    assert "kind `wake`" in flat
    assert "Quiet hours and a daily cap are enforced around you" in flat, "the bounds were not stated"
    assert "say nothing" in flat, "silence was not offered as a legitimate answer"


def test_the_wake_verb_reports_silence_and_can_be_forced(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`daedalus wake` is a probe: it must show the reason for silence, and
    never spend the day's quota just because the owner looked."""
    from daedalus import main as main_module

    # `enabled = false` rather than a quiet window: the probe reads the real
    # clock, and a test that depends on what time it is fails at night.
    (settings.home / "config.toml").write_text("[proactive]\nenabled = false\n", encoding="utf-8")

    rt = build_runtime(
        settings,
        surface=ScriptedSurface(approve=True),
        provider=ScriptedProvider([ChatResult(content="Standup in ten minutes.")]),
    )
    monkeypatch.setattr("daedalus.runtime.build_runtime", lambda *a, **k: rt)

    def args(*, force: bool) -> argparse.Namespace:
        return argparse.Namespace(
            home=str(settings.home), verbose=False, text=["standup"], force=force
        )

    assert main_module.cmd_wake(args(force=False)) == 0
    assert "silent" in capsys.readouterr().out

    assert main_module.cmd_wake(args(force=True)) == 0
    assert "Standup in ten minutes." in capsys.readouterr().out
