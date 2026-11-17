"""One-shot wakes: `remind` — the verb the 22:52 turn could not find.

On 18 Oct 22:52 Calliope wanted a reminder two hours out, and `schedule`'s only
answer was a 5-field cron — which cannot say "2 hours from now, once"; an
absolute-date one would fire yearly forever. What is asserted here: a reminder
fires once, delivers, and is gone; a missed moment is caught up with its lateness
**announced**, never silently dropped, never recurring; a failed compose keeps the
job rather than losing the owner's alarm to a transient outage; and `schedule
op=add` names `remind` as the way out, since the error is the one rung of the
correction ladder this can make followable.
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


async def test_a_reminder_fires_once_composes_from_its_note_and_retires(runtime: Runtime) -> None:
    moment = datetime.now() + timedelta(hours=2)
    runtime.scheduler.add_reminder(
        name="send_your_writing", when=moment, note="Remind the owner to send a piece of his own writing"
    )
    calls: list[str] = []

    async def wake(prompt: str, *, seat: str | None = None) -> str:
        calls.append(prompt)
        return f"(composed from: {prompt})"

    results = await runtime.scheduler.dispatch_tick(runtime, when=moment, wake_handler=wake)

    # The named moment arrives, the agent composes from the note, the owner reads.
    assert calls == ["Remind the owner to send a piece of his own writing"], (
        "the reminder composes from its own note, not from the job name"
    )
    assert runtime.surface.sent, "a reminder that fired but delivered nothing is silence"
    assert any("send a piece of his own writing" in s for s in runtime.surface.sent)
    detail = "\n".join(r.detail for r in results if r.job.startswith("send_your_writing"))
    assert "reminded" in detail, f"an on-time delivery says so, plainly — got {detail!r}"
    # A reminder that has fired is gone, not dormant.
    assert runtime.scheduler.get("send_your_writing") is None

    # And the next tick has literally nothing to see: it cannot fire again.
    again = await runtime.scheduler.dispatch_tick(
        runtime, when=moment + timedelta(minutes=1), wake_handler=wake
    )
    assert all(not r.output for r in again), "the second tick must not redeliver"
    assert len(runtime.surface.sent) == 1


async def test_a_missed_reminder_is_delivered_by_the_due_pass_and_announces_its_lateness(
    runtime: Runtime,
) -> None:
    async def wake(_prompt: str, *, seat: str | None = None) -> str:
        return "here it is"

    # Two minutes late is inside the window: delivered without a prefix scold.
    moment = datetime.now() + timedelta(hours=2)
    runtime.scheduler.add_reminder(name="soon", when=moment, note="remind him")
    await runtime.scheduler.dispatch_tick(
        runtime, when=moment + timedelta(minutes=2), wake_handler=wake
    )
    delivered = "\n".join(runtime.surface.sent)
    assert "here it is" in delivered
    assert "(this reminder was set for" not in delivered

    # Delivered three hours late, it does not pretend the morning did not happen.
    later = datetime.now() + timedelta(hours=1)
    runtime.scheduler.add_reminder(name="late", when=later, note="remind him")
    await runtime.scheduler.dispatch_tick(
        runtime, when=later + timedelta(hours=3), wake_handler=wake
    )
    delivered = "\n".join(runtime.surface.sent)
    assert f"(this reminder was set for {later:%a %d %b %H:%M}" in delivered, (
        "a delivery far past its named moment must carry the lateness on its face"
    )
    assert runtime.scheduler.get("late") is None

    # A `run_at` job has no cron to scan backwards with: the due pass owns it.
    past = pinned(datetime.now(), hours=-1)
    job = Job(name="r", kind="remind", target="missing", run_at=past.timestamp(), last_run=None)
    assert catchup.plan([job], datetime.now()) == []


async def test_an_alarm_the_agent_could_not_compose_is_kept_and_never_delivered_twice(
    runtime: Runtime,
) -> None:
    logging.disable(logging.CRITICAL)
    try:
        moment = datetime.now() + timedelta(hours=2)
        runtime.scheduler.add_reminder(name="keep", when=moment, note="remind him")

        with pytest.raises(RuntimeError):

            async def broken(_prompt: str, *, seat: str | None = None) -> str:
                raise RuntimeError("model down")

            await runtime.scheduler.dispatch_tick(runtime, when=moment, wake_handler=broken)

        # Losing the alarm to a transient outage is the T41 sin.
        assert runtime.scheduler.get("keep") is not None

        async def working(_prompt: str, *, seat: str | None = None) -> str:
            return "recovered"

        await runtime.scheduler.dispatch_tick(
            runtime, when=moment + timedelta(seconds=30), wake_handler=working
        )
        assert runtime.scheduler.get("keep") is None, "the retry delivers, then it retires"
    finally:
        logging.disable(logging.NOTSET)

    # A crash after the compose but before the removal cannot redeliver: the
    # idempotency key is anchored to the named moment, not the tick's minute.
    later = datetime.now() + timedelta(hours=3)
    job = runtime.scheduler.add_reminder(name="crashy", when=later, note="remind him")
    runtime.scheduler.log.record(
        Wake(key=job.idempotency_key(later), job=job.name, ran_at=0.0, ok=True, detail="fire")
    )

    async def wake(_prompt: str, *, seat: str | None = None) -> str:
        return "should never be reached"

    delivered = len(runtime.surface.sent)
    results = await runtime.scheduler.dispatch_tick(
        runtime, when=later + timedelta(seconds=30), wake_handler=wake
    )
    assert all(r.job != "crashy" or "already fired" in (r.detail or "") for r in results), (
        "the logged one-shot is the crash-recovery case: retired, not redelivered"
    )
    assert runtime.scheduler.get("crashy") is None
    assert len(runtime.surface.sent) == delivered


async def test_the_reminder_verb_validates_its_shape_and_names_itself_in_refusals(
    runtime: Runtime,
) -> None:
    # The scheduler refuses a past moment, a nameless job, an empty note, and a
    # tolerance it cannot read.
    with pytest.raises(ValueError, match="already past"):
        runtime.scheduler.add_reminder(name="r", when=datetime.now() - timedelta(hours=1), note="n")
    with pytest.raises(ValueError, match="needs a name"):
        runtime.scheduler.add_reminder(name="", when=pinned(datetime.now()), note="n")
    with pytest.raises(ValueError, match="needs a note"):
        runtime.scheduler.add_reminder(name="r", when=pinned(datetime.now()), note="")
    with pytest.raises(ValueError, match="late_within"):
        runtime.scheduler.add_reminder(
            name="r", when=pinned(datetime.now()), note="n", late_within="soon"
        )

    # A valid one round-trips through the file: run_at exactly, and no cron.
    job = runtime.scheduler.add_reminder(
        name="keep", when=pinned(datetime.now()), note="n", late_within="45m"
    )
    loaded = runtime.scheduler.get("keep")
    assert loaded is not None and loaded.run_at == job.run_at
    assert loaded.cron == "" and job.cron == ""

    # The tool accepts exactly one of `within` / `at`, and refuses a date with no
    # moment or a moment already gone.
    tool = RemindTool()
    r = await tool.run({"op": "add", "name": "r", "note": "n"}, runtime)
    assert not r.ok and "exactly one of `within`" in r.output
    r = await tool.run(
        {"op": "add", "name": "r", "note": "n", "within": "2h", "at": "2026-10-19T14:00"}, runtime
    )
    assert not r.ok and "exactly one of `within`" in r.output
    r = await tool.run({"op": "add", "name": "r", "note": "n", "within": "2h"}, runtime)
    assert r.ok, r.output
    jobs = [j for j in runtime.scheduler.jobs() if j.kind == "remind" and j.name == "r"]
    assert len(jobs) == 1 and jobs[0].target == "n"
    r = await tool.run({"op": "add", "name": "r3", "note": "n", "at": "2026-10-19"}, runtime)
    assert not r.ok and "time" in r.output, "a date with no moment cannot be kept"
    r = await tool.run(
        {
            "op": "add",
            "name": "r2",
            "note": "n",
            "at": (datetime.now() - timedelta(hours=1)).strftime(FMT),
        },
        runtime,
    )
    assert not r.ok and "already past" in r.output
    r = await tool.run({"op": "list"}, runtime)
    assert "r" in r.output and "due=" in r.output
    r = await tool.run({"op": "remove", "name": "r"}, runtime)
    assert r.ok and r.output == "removed 'r'"

    # The 22:52 error names `remind`, or a model that cannot express its intent
    # changes nothing and the turn burns.
    result = await ScheduleTool().run(
        {
            "op": "add",
            "kind": "wake",
            "late_within": "2h",
            "name": "cmrlsim_voice_sample",
            "target": "p",
        },
        runtime,
    )
    assert not result.ok
    assert "remind op=add" in result.output
    assert "no cron expression can say '2 hours from now, once'" in result.output

    # And the model knows the verb exists before it fails into `schedule`.
    prompt = build_system_prompt(runtime, InboundEvent(text="hi", user_id="owner"))
    assert "remind op=add" in prompt
    assert "cron cannot say" in prompt
