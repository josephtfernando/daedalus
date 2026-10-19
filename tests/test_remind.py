"""One-shot wakes: `remind` — the verb the 22:52 turn could not find.

On 18 Oct 22:52 Calliope wanted a reminder two hours out, and `schedule`'s
only answer was a 5-field cron — which cannot say "2 hours from now, once";
an absolute-date one would fire yearly forever. Four identical failures, a
correction ignored, and a turn handed back to the owner.

What is asserted here: a reminder fires once, delivers, and is gone; a missed
moment is caught up with its lateness **announced**, never silently dropped,
never recurring; a failed compose keeps the job rather than losing the owner's
alarm to a transient outage; and `schedule op=add` names `remind` as the way
out, since the error is the one rung of the correction ladder this can make
followable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from daedalus.loop.context import build_system_prompt
from daedalus.runtime import Runtime
from daedalus.scheduler import catchup
from daedalus.scheduler.jobs import Job
from daedalus.scheduler.wakes import Wake
from daedalus.surface.base import InboundEvent
from daedalus.tools.remind import RemindTool
from daedalus.tools.schedule import ScheduleTool

FMT = "%Y-%m-%d %H:%M"


def pinned(base: datetime, *, hours: float = 2) -> datetime:
    return base + timedelta(hours=hours)


async def test_a_reminder_fires_once_and_retires(runtime: Runtime) -> None:
    """The named moment arrives, the agent composes, the owner reads — once."""
    moment = datetime.now() + timedelta(hours=2)
    runtime.scheduler.add_reminder(
        name="send_your_writing", when=moment, note="Remind the owner to send a piece of his own writing"
    )
    calls: list[str] = []

    async def wake(prompt: str, *, seat: str | None = None) -> str:
        calls.append(prompt)
        return f"(composed from: {prompt})"

    results = await runtime.scheduler.dispatch_tick(runtime, when=moment, wake_handler=wake)

    assert calls == ["Remind the owner to send a piece of his own writing"], (
        "the reminder composes from its own note, not from the job name"
    )
    assert runtime.surface.sent, "a reminder that fired but delivered nothing is silence"
    assert any("send a piece of his own writing" in s for s in runtime.surface.sent)
    detail = "\n".join(r.detail for r in results if r.job.startswith("send_your_writing"))
    assert "reminded" in detail, f"an on-time delivery says so, plainly — got {detail!r}"
    assert runtime.scheduler.get("send_your_writing") is None, (
        "a reminder that has fired is gone, not dormant — nothing left to fire again"
    )


async def test_a_fired_reminder_cannot_fire_again(runtime: Runtime) -> None:
    """Fires once. The next tick has literally nothing to see."""
    moment = datetime.now() + timedelta(hours=2)
    runtime.scheduler.add_reminder(name="send_your_writing", when=moment, note="remind him")

    async def wake(_prompt: str, *, seat: str | None = None) -> str:
        return "done"

    await runtime.scheduler.dispatch_tick(runtime, when=moment, wake_handler=wake)
    again = await runtime.scheduler.dispatch_tick(runtime, when=moment + timedelta(minutes=1), wake_handler=wake)

    assert all(not r.output for r in again), "the second tick must not redeliver"
    assert runtime.surface.sent.count("done") == 1


async def test_a_reminder_missed_while_down_is_caught_up_and_says_so(runtime: Runtime) -> None:
    """The daemon was down at the moment; the owner still hears — with the lateness named."""
    moment = datetime.now() + timedelta(hours=2)
    runtime.scheduler.add_reminder(name="send_your_writing", when=moment, note="remind him")

    async def wake(_prompt: str, *, seat: str | None = None) -> str:
        return "here it is"

    await runtime.scheduler.dispatch_tick(runtime, when=moment + timedelta(minutes=2), wake_handler=wake)

    delivered = "\n".join(runtime.surface.sent)
    assert "here it is" in delivered
    # Two minutes late is inside the window: delivered without a prefix scold.
    assert "(this reminder was set for" not in delivered


async def test_a_reminder_delivered_far_late_announces_how_late(runtime: Runtime) -> None:
    """Delivered three hours late, it does not pretend the morning did not happen."""
    moment = datetime.now() + timedelta(hours=1)
    runtime.scheduler.add_reminder(name="send_your_writing", when=moment, note="remind him")

    async def wake(_prompt: str, *, seat: str | None = None) -> str:
        return "here it is"

    await runtime.scheduler.dispatch_tick(runtime, when=moment + timedelta(hours=3), wake_handler=wake)

    delivered = "\n".join(runtime.surface.sent)
    assert f"(this reminder was set for {moment:%a %d %b %H:%M}" in delivered, (
        "a delivery far past its named moment must carry the lateness on its face"
    )
    # And it is still gone afterwards — announced, delivered, never recurring.
    assert runtime.scheduler.get("send_your_writing") is None


async def test_a_failed_compose_keeps_the_reminder(runtime: Runtime) -> None:
    """A transient outage must not eat the owner's alarm.

    The lesson of the executable bit (T41): a capability that fails on its
    first run looks exactly like one with nothing to say. A reminder that
    cannot compose keeps trying — every failure journalled — rather than
    retiring into silence.
    """
    logging.disable(logging.CRITICAL)
    try:
        moment = datetime.now() + timedelta(hours=2)
        runtime.scheduler.add_reminder(name="send_your_writing", when=moment, note="remind him")

        with pytest.raises(RuntimeError):

            async def broken(_prompt: str, *, seat: str | None = None) -> str:
                raise RuntimeError("model down")

            await runtime.scheduler.dispatch_tick(runtime, when=moment, wake_handler=broken)

        assert runtime.scheduler.get("send_your_writing") is not None, (
            "a failed compose must keep the reminder — losing it silently is the T41 sin"
        )

        async def working(_prompt: str, *, seat: str | None = None) -> str:
            return "recovered"

        await runtime.scheduler.dispatch_tick(
            runtime, when=moment + timedelta(seconds=30), wake_handler=working
        )
        assert runtime.scheduler.get("send_your_writing") is None, (
            "the retry delivers, and then the reminder retires"
        )
    finally:
        logging.disable(logging.NOTSET)


async def test_a_crash_between_fire_and_retire_cannot_redeliver(runtime: Runtime) -> None:
    """The idempotency key is anchored to the named moment, not the tick's minute.

    A crash after the compose but before the removal would otherwise let the
    next tick redeliver — two copies of one reminder, the exact failure the
    duplicate-call guard exists for on the turn side.
    """
    moment = datetime.now() + timedelta(hours=2)
    job = runtime.scheduler.add_reminder(name="r", when=moment, note="remind him")

    # The compose succeeded and was logged; the retire never happened.
    runtime.scheduler.log.record(
        Wake(
            key=job.idempotency_key(moment),
            job=job.name,
            ran_at=0.0,
            ok=True,
            detail="fire",
        )
    )

    async def wake(_prompt: str, *, seat: str | None = None) -> str:
        return "should never be reached"

    results = await runtime.scheduler.dispatch_tick(runtime, when=moment + timedelta(seconds=30), wake_handler=wake)

    assert all(r.job != "r" or "already fired" in (r.detail or "") for r in results), (
        "the logged one-shot is the crash-recovery case: retired, not redelivered"
    )
    assert runtime.scheduler.get("r") is None
    assert not runtime.surface.sent


def test_catchup_never_scans_a_one_shot() -> None:
    """A `run_at` job has no cron to scan backwards with — the due pass owns it."""
    moment = pinned(datetime.now(), hours=-1)  # long past
    job = Job(name="r", kind="remind", target="missing", run_at=moment.timestamp(), last_run=None)
    assert catchup.plan([job], datetime.now()) == [], (
        "the cron scan must not touch a one-shot: it fires through the due pass"
    )


def test_add_reminder_refuses_past_and_broken_input(runtime: Runtime) -> None:
    fmt = "%Y-%m-%d %H:%M"
    past = datetime.now() - timedelta(hours=1)
    with pytest.raises(ValueError, match="already past"):
        runtime.scheduler.add_reminder(name="r", when=past, note="n")
    with pytest.raises(ValueError, match="needs a name"):
        runtime.scheduler.add_reminder(name="", when=pinned(datetime.now()), note="n")
    with pytest.raises(ValueError, match="needs a note"):
        runtime.scheduler.add_reminder(name="r", when=pinned(datetime.now()), note="")
    with pytest.raises(ValueError, match="late_within"):
        runtime.scheduler.add_reminder(name="r", when=pinned(datetime.now()), note="n", late_within="soon")
    # A valid one round-trips: run_at through the file, exactly.
    job = runtime.scheduler.add_reminder(
        name="keep", when=pinned(datetime.now()), note="n", late_within="45m"
    )
    loaded = runtime.scheduler.get("keep")
    assert loaded is not None and loaded.run_at == job.run_at
    assert loaded.cron == "" and job.cron == ""


async def test_the_remind_tool_validates_its_shape(runtime: Runtime) -> None:
    tool = RemindTool()
    r = await tool.run({"op": "add", "name": "r", "note": "n"}, runtime)
    assert not r.ok and "exactly one of `within`" in r.output
    r = await tool.run(
        {"op": "add", "name": "r", "note": "n", "within": "2h", "at": "2026-10-19T14:00"}, runtime
    )
    assert not r.ok and "exactly one of `within`" in r.output
    r = await tool.run({"op": "add", "name": "r", "note": "n", "within": "2h"}, runtime)
    assert r.ok, r.output
    jobs = [j for j in runtime.scheduler.jobs() if j.kind == "remind"]
    assert len(jobs) == 1 and jobs[0].target == "n"
    r = await tool.run({"op": "add", "name": "r3", "note": "n", "at": "2026-10-19"}, runtime)
    assert not r.ok and "time" in r.output, "a date with no moment cannot be kept"
    now = datetime.now()
    r = await tool.run(
        {"op": "add", "name": "r2", "note": "n", "at": (now - timedelta(hours=1)).strftime(FMT)},
        runtime,
    )
    assert not r.ok and "already past" in r.output
    r = await tool.run({"op": "list"}, runtime)
    assert "r" in r.output and "due=" in r.output
    r = await tool.run({"op": "remove", "name": "r"}, runtime)
    assert r.ok and r.output == "removed 'r'"


async def test_the_schedule_add_error_names_the_way_out(runtime: Runtime) -> None:
    """The 22:52 error must name `remind` — the correction's "do what the error
    says" branch needs a rung to land on, or a model that cannot express its
    intent changes nothing and the turn burns."""
    tool = ScheduleTool()
    result = await tool.run(
        {"op": "add", "kind": "wake", "late_within": "2h", "name": "cmrlsim_voice_sample", "target": "p"},
        runtime,
    )
    assert not result.ok
    assert "remind op=add" in result.output, (
        "the error must name the one-shot verb: a wake-less cron is the wall, not the answer"
    )
    assert "no cron expression can say '2 hours from now, once'" in result.output


def test_the_prompt_names_the_reminder_verb(runtime: Runtime) -> None:
    prompt = build_system_prompt(runtime, InboundEvent(text="hi", user_id="owner"))

    assert "remind op=add" in prompt, (
        "the model must know the one-shot verb exists before it fails into schedule"
    )
    assert "cron cannot say" in prompt