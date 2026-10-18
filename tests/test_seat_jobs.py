"""A scheduled job can belong to a seat (T21 — "one thing the seat owns that runs").

Before this, everything the scheduler fired spoke as whoever happened to be
active — Odysseus's weekly sweep would have been the coordinator's message
wearing a hat. The wash that makes a seat real is: its charter composes the
message, its namespace holds the writes, and the delivery arrives under its
name. A seat nobody can see owning a job is ownership as decoration.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.conftest import tool_call

from daedalus.runtime import Runtime
from daedalus.model.providers import ScriptedProvider
from daedalus.tools.schedule import ScheduleTool


MONDAY = datetime(2026, 10, 19, 9, 30)  # a Monday morning, after quiet hours


async def test_a_seat_owned_wake_composes_as_that_seat_and_arrives_named(runtime: Runtime) -> None:
    calls: list[tuple[str, str | None]] = []

    async def handler(prompt: str, *, seat: str | None = None) -> str:
        calls.append((prompt, seat))
        return "Three openings worth your time this week."

    runtime.scheduler.add_job(
        name="odysseus_sweep",
        cron="30 9 * * 1",
        kind="wake",
        target="Sweep the job market for the owner's stack. Report what changed, with sources.",
        seat="odysseus",
    )
    results = await runtime.scheduler.dispatch_tick(runtime, when=MONDAY, wake_handler=handler)

    assert calls == [(calls[0][0], "odysseus")], "the wake is Odysseus's work, by charter key"
    delivered = "\n".join(runtime.surface.sent)
    assert "Odysseus: Three openings" in delivered, (
        "a proactive message arrives under its seat's name — unsigned, the owner "
        "reads it as the coordinator's"
    )
    detail = "\n".join(r.detail for r in results if r.job == "odysseus_sweep")
    assert "Odysseus" in detail


async def test_a_seat_that_names_nobody_never_delivers_as_someone_else(runtime: Runtime) -> None:
    """A typo'd seat is a broken job, recorded — not a quiet substitution."""

    async def handler(prompt: str, *, seat: str | None = None) -> str:
        return "should never be reached"

    runtime.scheduler.add_job(
        name="bad_seat", cron="30 9 * * 1", kind="wake", target="sweep", seat="odyseus"
    )
    results = await runtime.scheduler.dispatch_tick(runtime, when=MONDAY, wake_handler=handler)

    assert not runtime.surface.sent
    detail = "\n".join(r.detail for r in results if r.job == "bad_seat")
    assert "names nobody" in detail


async def test_an_unsigned_wake_delivers_untouched(runtime: Runtime) -> None:
    """The coordinator's own work is not suddenly prefixed with its own name."""

    async def handler(prompt: str, *, seat: str | None = None) -> str:
        return "Quiet morning, nothing to flag."

    runtime.scheduler.add_job(name="plain", cron="30 9 * * 1", kind="wake", target="check the day")
    await runtime.scheduler.dispatch_tick(runtime, when=MONDAY, wake_handler=handler)

    assert runtime.surface.sent == ["Quiet morning, nothing to flag."], (
        "no seat, no signature — the prefix exists to name a *different* speaker"
    )


async def test_a_seat_owned_wake_writes_into_its_own_namespace(runtime: Runtime) -> None:
    """The message is composed *as* the seat: its charter frames, its memory holds.

    Driven through `rt.wake` with a scripted provider: a fact the turn writes
    must land in Odysseus's namespace, where the coordinator's own cannot recall
    it — that is the "its writes land only in its own namespace" clause of T21,
    and it is what makes the sweep prepping Athena's drills safe to remember.
    """
    from daedalus.cabinet.namespace import memory_for
    from daedalus.model import ModelClient
    from daedalus.model.providers import ChatResult

    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("memory", op="write", facet="opportunities", content="the openings"),
                    ChatResult(content="Sweep done, three openings noted."),
                ]
            )
        ],
    )
    text = await runtime.wake("Sweep the job market and report.", seat="odysseus")

    assert text == "Sweep done, three openings noted."
    assert runtime.session is not None and runtime.session.seat == "odysseus"
    odysseus = memory_for(runtime.memory_root, runtime.seats["odysseus"])
    coordinator = memory_for(runtime.memory_root, runtime.seats["daedalus"])
    assert odysseus.recall("openings"), "the sweep's finding is Odysseus's to keep"
    assert not coordinator.recall("openings"), (
        "the coordinator's namespace must not see a seat's fact — invisible here is "
        "the point (T23 repeats this for Athena)"
    )


async def test_the_schedule_tool_refuses_a_seat_that_names_nobody(runtime: Runtime) -> None:
    tool = ScheduleTool()
    result = await tool.run(
        {"op": "add", "name": "sweep", "cron": "30 9 * * 1", "kind": "wake", "target": "x", "seat": "odyseus"},
        runtime,
    )
    assert not result.ok and "names nobody" in result.output
    assert "athena" in result.output, "the refusal names the cabinet rather than leaving a guess"

    ok = await tool.run(
        {"op": "add", "name": "sweep", "cron": "30 9 * * 1", "kind": "wake", "target": "x", "seat": "athena"},
        runtime,
    )
    assert ok.ok, ok.output
    job = runtime.scheduler.get("sweep")
    assert job is not None and job.seat == "athena"


async def test_a_capability_job_cannot_carry_a_seat(runtime: Runtime) -> None:
    with pytest.raises(ValueError, match="cannot belong to a seat"):
        runtime.scheduler.add_job(
            name="cap_job", cron="0 8 * * *", kind="capability", target="codeforces_contest_alert",
            seat="athena",
        )


async def test_a_hand_edited_job_becomes_real_on_the_next_tick(runtime: Runtime) -> None:
    """jobs.toml is the source, not the store's cache (ARCHITECTURE §8.5)."""
    text = "\n".join(
        [
            '[[job]]',
            'name = "hand_added"',
            'cron = "30 9 * * 1"',
            'kind = "wake"',
            'target = "from the file"',
            "created_at = 1790000000.0",
            "",
        ]
    )
    runtime.scheduler.jobs_path.write_text(text, encoding="utf-8")

    seen: list[tuple[str, str | None]] = []

    async def handler(prompt: str, *, seat: str | None = None) -> str:
        seen.append((prompt, seat))
        return "noted"

    await runtime.scheduler.dispatch_tick(runtime, when=MONDAY, wake_handler=handler)

    assert seen and seen[0][0] == "from the file", (
        "a job added by editing the file fires without a restart — a daemon that "
        "only reads its files once made hand-editing a lie"
    )
