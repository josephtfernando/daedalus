"""A scheduled job can belong to a seat (T21 — "one thing the seat owns that runs").

Before this, everything the scheduler fired spoke as whoever happened to be
active. The wash that makes a seat real is: its charter composes the message, its
namespace holds the writes, and the delivery arrives under its name. A seat
nobody can see owning a job is ownership as decoration.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.conftest import tool_call

from daedalus.model.providers import ScriptedProvider
from daedalus.runtime import Runtime
from daedalus.tools.schedule import ScheduleTool


MONDAY = datetime(2026, 10, 19, 9, 30)  # a Monday morning, after quiet hours


async def test_a_seat_owned_wake_composes_and_arrives_as_that_seat(runtime: Runtime) -> None:
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

    assert calls and calls[0][1] == "odysseus", "the wake is Odysseus's work, by charter key"
    delivered = "\n".join(runtime.surface.sent)
    assert "Odysseus: Three openings" in delivered, (
        "a proactive message arrives under its seat's name — unsigned, the owner "
        "reads it as the coordinator's"
    )
    detail = "\n".join(r.detail for r in results if r.job == "odysseus_sweep")
    assert "Odysseus" in detail

    # An unsigned wake is the coordinator's own work: no seat, no signature.
    async def plain_handler(prompt: str, *, seat: str | None = None) -> str:
        return "Quiet morning, nothing to flag."

    runtime.scheduler.add_job(name="plain", cron="30 9 * * 1", kind="wake", target="check the day")
    await runtime.scheduler.dispatch_tick(runtime, when=MONDAY, wake_handler=plain_handler)
    assert runtime.surface.sent[-1] == "Quiet morning, nothing to flag.", (
        "no seat, no signature — the prefix exists to name a *different* speaker"
    )

    # A typo'd seat is a broken job, recorded — not a quiet substitution.
    runtime.scheduler.add_job(
        name="bad_seat", cron="30 9 * * 1", kind="wake", target="sweep", seat="odyseus"
    )
    before = len(runtime.surface.sent)
    results = await runtime.scheduler.dispatch_tick(runtime, when=MONDAY, wake_handler=handler)
    assert len(runtime.surface.sent) == before
    detail = "\n".join(r.detail for r in results if r.job == "bad_seat")
    assert "names nobody" in detail

    # The message is composed *as* the seat: driven through `rt.wake` with a
    # scripted provider, a fact the turn writes lands in Odysseus's namespace.
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
        "the coordinator's namespace must not see a seat's fact — invisible here is the point"
    )


async def test_the_schedule_tool_admits_only_real_seats_and_the_file_is_the_source(
    runtime: Runtime,
) -> None:
    tool = ScheduleTool()

    # A typo'd seat is refused by name, and the refusal names the cabinet.
    result = await tool.run(
        {
            "op": "add",
            "name": "sweep",
            "cron": "30 9 * * 1",
            "kind": "wake",
            "target": "x",
            "seat": "odyseus",
        },
        runtime,
    )
    assert not result.ok and "names nobody" in result.output
    assert "athena" in result.output, "the refusal names the cabinet rather than leaving a guess"

    ok = await tool.run(
        {
            "op": "add",
            "name": "sweep",
            "cron": "30 9 * * 1",
            "kind": "wake",
            "target": "x",
            "seat": "athena",
        },
        runtime,
    )
    assert ok.ok, ok.output
    job = runtime.scheduler.get("sweep")
    assert job is not None and job.seat == "athena"

    # A capability job cannot carry a seat.
    with pytest.raises(ValueError, match="cannot belong to a seat"):
        runtime.scheduler.add_job(
            name="cap_job",
            cron="0 8 * * *",
            kind="capability",
            target="codeforces_contest_alert",
            seat="athena",
        )

    # jobs.toml is the source, not the store's cache (ARCHITECTURE §8.5): a job
    # added by editing the file fires on the next tick, without a restart.
    text = "\n".join(
        [
            "[[job]]",
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
        "a daemon that only reads its files once made hand-editing a lie"
    )


def test_the_coordinators_habit_wake_is_live() -> None:
    """T36, decided by the owner 19 Oct: the habit tracker is the coordinator's.

    The decision is real only while its wiring is: a daily wake, seated
    `daedalus`, that reads the tracker's report and carries one line. The
    scheduler re-reads the file every tick, so the job being in the file IS it
    being live.
    """
    from pathlib import Path

    from daedalus.scheduler.jobs import JobStore

    store = JobStore(Path.home() / ".daedalus" / "state" / "jobs.toml")
    jobs = {j.name: j for j in store.all()}
    job = jobs.get("habit_report")

    assert job is not None, "the owner named the coordinator — his morning wake must exist"
    assert job.seat == "daedalus" and job.kind == "wake"
    assert job.cron == "45 8 * * *", "inside the wake window, after Athena's practice plan"
    assert "habit_tracker" in job.target and "action=report" in job.target
    assert "ONE line" in job.target, "a habit digest is a line, not a lecture"
    assert "never from memory" in job.target, "the report is the stored record, not a recollection"
