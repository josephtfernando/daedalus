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


def test_quiet_hours_cover_the_window_and_nothing_else() -> None:
    """The window is inclusive at its ends, handles midnight, and fails open."""
    # case: a 22:30–07:30 window crossing midnight, on the minute boundaries.
    for when, covered in [
        (datetime(2026, 10, 16, 22, 29), False),
        (datetime(2026, 10, 16, 22, 30), True),
        (datetime(2026, 10, 16, 23, 59), True),
        (datetime(2026, 10, 17, 3, 0), True),
        (datetime(2026, 10, 17, 7, 29), True),
        (datetime(2026, 10, 17, 7, 30), False),
        (datetime(2026, 10, 17, 12, 0), False),
    ]:
        assert quiet_hours_cover("22:30", "07:30", when) is covered, when

    # case: a window inside one day.
    assert quiet_hours_cover("09:00", "17:00", datetime(2026, 10, 16, 12, 0)) is True
    assert quiet_hours_cover("09:00", "17:00", datetime(2026, 10, 16, 18, 0)) is False

    # case: unset or unreadable — nothing is covered, so a typo cannot silence it.
    for start, end in [("", ""), ("22:30", ""), ("", "07:30"), ("bedtime", "07:30"), ("25:00", "07:30")]:
        assert quiet_hours_cover(start, end, NIGHT) is False, (start, end)

    # case: an equal window reads as not configured. "Never speak" is `enabled=false`.
    assert quiet_hours_cover("09:00", "09:00", datetime(2026, 10, 16, 9, 0)) is False


def test_the_gate_refuses_quiet_hours_off_and_the_cap_and_keeps_the_ledger_honest(
    settings: Settings, ledger: ProactiveState
) -> None:
    """`may_speak` is the whole policy: four reasons to stay silent, and no more.

    A corrupt ledger must not disarm the gate, the cap resets with the day, and
    the payload keeps the last message (so a wake can avoid repeating it) and
    the day's count (so the cap is not re-derived).
    """
    # case: quiet hours.
    allowed, why = ledger.may_speak(settings, NIGHT)
    assert allowed is False
    assert "quiet hours" in why

    # case: the feature is switched off.
    assert ledger.may_speak(replace(settings, proactive_enabled=False), NOON) == (
        False,
        "proactive messages are off",
    )

    # case: the daily cap bites.
    capped = replace(settings, proactive_max_per_day=2)
    ledger.note_spoken(NOON, "one")
    ledger.note_spoken(NOON, "two")
    allowed, why = ledger.may_speak(capped, NOON)
    assert allowed is False
    assert "cap" in why

    # case: the cap resets with the day.
    yesterday = replace(settings, proactive_max_per_day=1)
    ledger.note_spoken(NOON, "hello")
    assert ledger.may_speak(yesterday, NOON)[0] is False
    assert ledger.may_speak(yesterday, datetime(2026, 10, 17, 12, 0))[0] is True

    # case: the last message and the count are readable.
    ledger.note_spoken(NOON, "Standup in ten minutes.")
    assert ledger.last_text() == "Standup in ten minutes."
    assert ledger.spoken_today(NOON) == 4, "one, two, hello, then the standup line"

    # case: a hand-edited, unparseable file must not silence the agent.
    corrupt = ProactiveState(ledger.path)
    corrupt.path.parent.mkdir(parents=True, exist_ok=True)
    corrupt.path.write_text("{not json", encoding="utf-8")
    assert corrupt.spoken_today(NOON) == 0
    assert corrupt.last_text() == ""
    assert corrupt.may_speak(settings, NOON)[0] is True


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


async def test_the_gate_binds_a_wake_and_never_a_capability_alert(runtime: Runtime) -> None:
    """Quiet hours and the cap are the *agent's* discipline, not a gag on the owner.

    A wake held by the gate records why it stayed silent; an alert the owner
    explicitly asked for fires when it fires.
    """
    # case: a wake in quiet hours never reaches the owner, and says why.
    await _add_wake(runtime, "night")

    async def handler(prompt: str, *, seat: str | None = None) -> str:
        return "you should be asleep"

    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=NIGHT)
    assert runtime.surface.sent == [], "the agent spoke during quiet hours"
    woke = next(r for r in results if r.job == "night")
    assert woke.ran is False
    assert "quiet" in woke.detail, "the reason for the silence was not recorded"

    # case: the daily cap stops a wake once the day is spent.
    await _add_wake(runtime, "cadence")
    for index in range(runtime.settings.proactive_max_per_day):
        runtime.scheduler.proactive.note_spoken(NOON, f"message {index}")
    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=NOON)
    assert runtime.surface.sent == []
    spent = next(r for r in results if r.job == "cadence")
    assert spent.ran is False
    assert "cap" in spent.detail

    # case: a capability's alert is not a wake and is not silenced.
    await _add_alert(runtime)
    await runtime.scheduler.dispatch_tick(runtime, when=NIGHT)
    assert runtime.surface.sent, "quiet hours silenced a capability's alert"


async def test_a_wake_is_sent_and_counted_or_silent_and_free(runtime: Runtime) -> None:
    """The scheduler's last hop: the reason arrives, "spoke" is counted, "" is not."""
    # case: a wake with something to say is sent, counted, and given its reason.
    await _add_wake(runtime, "midday", target="watch the disk")
    seen: list[str] = []

    async def handler(prompt: str, *, seat: str | None = None) -> str:
        seen.append(prompt)
        return "Standup in ten minutes."

    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=NOON)
    assert seen == ["watch the disk"], "the job's `target` did not survive the hop"
    assert runtime.surface.sent == ["Standup in ten minutes."]
    assert runtime.scheduler.proactive.spoken_today(NOON) == 1
    assert runtime.scheduler.proactive.last_text() == "Standup in ten minutes."
    assert next(r for r in results if r.job == "midday").detail == "spoke"

    # case: the agent may look and decide to stay silent — a decision, not a
    # failure, and it does not spend the day's quota.
    await _add_wake(runtime, "quiet_check")

    async def quiet(prompt: str, *, seat: str | None = None) -> str:
        return ""

    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=quiet, when=NOON)
    assert runtime.surface.sent == ["Standup in ten minutes."], "an empty wake produced a message"
    silent = next(r for r in results if r.job == "quiet_check")
    assert silent.ok is True, "choosing silence is a decision, not a failure"
    assert "nothing worth saying" in silent.detail
    assert runtime.scheduler.proactive.spoken_today(NOON) == 1, "silence spent a message"


async def test_a_pinned_tick_is_judged_by_its_own_clock_and_still_dedups(runtime: Runtime) -> None:
    """A tick given a time is governed by *that* time — and runs once.

    Each run used to record `last_run` from the wall clock, so a pinned sequence
    contradicted itself: a silent 03:00 followed by a noon that was "already
    run". Recording the tick's own time fixes that and becomes the first line of
    dedup; the wake log is the second, for sleeps and double ticks.
    """
    await _add_wake(runtime, "midday")

    async def handler(prompt: str, *, seat: str | None = None) -> str:
        return "Standup in ten minutes."

    # Both times are deliberately in the past: reading the wall clock fails here.
    night = datetime(2020, 1, 6, 3, 0)
    noon = datetime(2020, 1, 6, 12, 0)

    await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=night)
    assert runtime.surface.sent == [], "the night tick should have stayed quiet"

    await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=noon)
    assert runtime.surface.sent == ["Standup in ten minutes."], (
        "a pinned tick was judged already-run against the wall clock"
    )

    # case: the same pinned minute does not speak twice.
    second = await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=noon)
    assert runtime.surface.sent == ["Standup in ten minutes."], "the same minute spoke twice"
    assert "midday" not in [result.job for result in second]
    assert runtime.scheduler.log.seen(f"midday:{noon.strftime('%Y%m%dT%H%M')}")


async def test_a_wake_is_composed_fresh_with_the_facts_it_needs(settings: Settings) -> None:
    """The message is neither a template nor an amnesiac.

    Silence is a first-class answer; the briefing says nobody asked for this,
    what is on today, what was just said and why it is here; and the system
    prompt tells the agent the wake path exists — and that it is bounded.
    """
    # case: the model choosing silence yields no message.
    silent = build_runtime(
        settings,
        surface=ScriptedSurface(approve=True),
        provider=ScriptedProvider([ChatResult(content=SILENT)]),
    )
    assert await silent.wake("standup check") == ""

    # case: something to say is returned as said.
    spoken = build_runtime(
        settings,
        surface=ScriptedSurface(approve=True),
        provider=ScriptedProvider([ChatResult(content="Standup in ten minutes.")]),
    )
    assert await spoken.wake("standup check") == "Standup in ten minutes."

    # case: the briefing carries the reason, the day, the last message and silence.
    soon = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    spoken.calendar.add(summary="Dentist", start=soon)
    spoken.scheduler.proactive.note_spoken(NOON, "Standup in ten minutes.")
    briefing = spoken.briefing("the morning check-in")
    assert "speaking first" in briefing, "it was not told nobody asked for this"
    assert "Dentist" in briefing, "the wake has no idea what is on today"
    assert "Standup in ten minutes." in briefing, "it cannot know it may repeat itself"
    assert "Do not repeat it" in briefing
    assert SILENT in briefing, "it was never offered silence"
    assert "the morning check-in" in briefing, "the reason for the wake was dropped"

    # case: the system prompt names the verb, the bounds and the exit.
    prompt = build_system_prompt(spoken, InboundEvent(text="hi", user_id="owner"))
    flat = " ".join(prompt.split())  # the prompt is hard-wrapped
    assert "speak first" in flat
    assert "kind `wake`" in flat
    assert "Quiet hours and a daily cap are enforced around you" in flat, "the bounds were not stated"
    assert "say nothing" in flat, "silence was not offered as a legitimate answer"


def test_a_wake_cannot_inherit_a_previous_turns_local_only_permission(settings: Settings) -> None:
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


def test_the_wake_verb_reports_silence_and_can_be_forced(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`daedalus wake` is a probe: it shows the reason for silence, and never
    spends the day's quota just because the owner looked."""
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
