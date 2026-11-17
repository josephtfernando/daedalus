"""Catch-up: work that fell due while nobody was listening (T18).

A tick is only ever *now*, and `cron.matches` asks whether a job's moment is the
present minute. So a job whose moment passed while the daemon was down was not
skipped and not recorded — it was never *asked about*. These tests pin all three
timestamps a catch-up is arithmetic over (`created_at`, `last_run`, and the tick's
`when`), because a test of "what should have run" that reads the wall clock is a
test that passes at 09:00 and fails at breakfast.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

from daedalus.runtime import Runtime
from daedalus.scheduler.catchup import (
    DEFAULT_LATE_WITHIN,
    human,
    parse_window,
    plan,
    window_for,
)
from daedalus.scheduler.jobs import Job

#: The pinned present: a Saturday, mid-afternoon.
NOW = datetime(2026, 10, 17, 14, 20)
#: When the jobs were created — long enough ago that anything is possible.
CREATED = datetime(2026, 10, 14, 9, 0)


def _job(
    runtime: Runtime,
    name: str,
    cron: str,
    *,
    kind: str = "capability",
    target: str = "",
    last_run: datetime | None = None,
    created_at: datetime = CREATED,
    late_within: str = "",
) -> Job:
    """Put a job straight into the store, with its whole history pinned.

    `add_job` stamps `created_at` from the wall clock, which is the one thing
    these tests must not depend on.
    """
    job = Job(
        name=name,
        cron=cron,
        kind=kind,
        target=target,
        last_run=last_run.timestamp() if last_run is not None else None,
        created_at=created_at.timestamp(),
        late_within=late_within,
    )
    runtime.scheduler.store.put(job)
    return job


def _speaks(text: str = "Standup in ten minutes."):
    async def handler(prompt: str, *, seat: str | None = None) -> str:  # noqa: ARG001 — the prompt is the wake's reason
        return text

    return handler


def test_the_window_is_judged_by_kind_and_can_be_stated_or_falls_back() -> None:
    # A capability reports a condition; a wake talks about a day. The difference
    # between the two is the guarantee, not the numbers.
    assert parse_window(DEFAULT_LATE_WITHIN["capability"]) < parse_window(DEFAULT_LATE_WITHIN["wake"])
    assert window_for(Job(name="probe", cron="0 * * * *", kind="capability")) == timedelta(hours=1)
    assert window_for(Job(name="briefing", cron="0 8 * * *", kind="wake")) == timedelta(hours=12)

    # A job may state its own tolerance.
    stated = Job(name="slow_mail", cron="0 8 * * *", kind="capability", late_within="2d")
    assert window_for(stated) == timedelta(days=2)

    # An unreadable tolerance falls back rather than failing open: a typo in a
    # hand-edited `jobs.toml` must not turn a probe into a stale alert.
    assert parse_window("six hours") is None
    assert parse_window("") is None
    assert parse_window("-1h") is None
    assert window_for(
        Job(name="probe", cron="0 * * * *", kind="capability", late_within="6h!")
    ) == timedelta(hours=1)

    # A duration reads — and says itself — the way the owner would write it.
    assert parse_window("45m") == timedelta(minutes=45)
    assert parse_window("6h") == timedelta(hours=6)
    assert parse_window("2d") == timedelta(days=2)
    assert parse_window("90s") == timedelta(seconds=90)
    assert parse_window(" 6H ") == timedelta(hours=6)
    assert human(timedelta(seconds=30)) == "30s"
    assert human(timedelta(minutes=45)) == "45m"
    assert human(timedelta(hours=6, minutes=20)) == "6h20m"
    assert human(timedelta(hours=2)) == "2h"
    assert human(timedelta(days=3, hours=4)) == "3d4h"


def test_the_queue_offers_only_the_latest_missed_moment_worth_running(runtime: Runtime) -> None:
    # A job that just ran has missed nothing.
    just_ran = _job(runtime, "briefing", "0 8 * * *", kind="wake", last_run=NOW - timedelta(seconds=30))
    assert plan([just_ran], NOW) == []

    # A job added while the daemon was down has no `last_run`: creation is the floor.
    never = _job(runtime, "new", "0 8 * * *", kind="wake", last_run=None)
    assert [m.when for m in plan([never], NOW)] == [datetime(2026, 10, 17, 8, 0)]

    # Two days of an hourly job is one thing to say, not forty-eight.
    hourly = _job(runtime, "hourly", "0 * * * *", kind="wake", last_run=datetime(2026, 10, 15, 9, 0))
    [missed] = plan([hourly], NOW)
    assert missed.when == datetime(2026, 10, 17, 14, 0)
    assert missed.superseded == 52, "the earlier occurrences were not folded into one"
    assert missed.worth_it

    # An occurrence whose moment has passed is not worth running, and says why.
    probe = _job(
        runtime,
        "cap-disk_free_check",
        "0 8 * * *",
        kind="capability",
        last_run=datetime(2026, 10, 15, 8, 0),
    )
    [stale] = plan([probe], NOW)
    assert stale.worth_it is False
    assert stale.when == datetime(2026, 10, 17, 8, 0)
    assert "6h20m late" in stale.reason
    assert "1h window" in stale.reason

    # A disabled job is left alone.
    disabled = _job(
        runtime, "disabled", "0 8 * * *", kind="wake", last_run=datetime(2026, 10, 16, 8, 0)
    )
    disabled.enabled = False
    assert plan([disabled], NOW) == []


async def test_a_queue_of_missed_runs_is_delivered_in_order_and_folded_into_one(
    runtime: Runtime,
) -> None:
    # T18 (1/3): the missed morning reaches the owner instead of vanishing.
    _job(
        runtime,
        "briefing",
        "0 8 * * *",
        kind="wake",
        target="the morning check-in",
        last_run=datetime(2026, 10, 16, 8, 0),
    )
    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=_speaks(), when=NOW)
    assert runtime.surface.sent == ["Standup in ten minutes."], "the missed morning was dropped"
    caught = next(result for result in results if result.job == "briefing")
    assert caught.ran is True and caught.ok is True
    assert "caught up the missed 08:00 run" in caught.detail
    assert "6h20m late" in caught.detail

    # T18 (2/3): a start-up is a queue, not a burst — one after the other, never
    # simultaneously, which is the failure a test can name exactly.
    _job(runtime, "eight", "0 8 * * *", kind="wake", target="eight", last_run=datetime(2026, 10, 16, 8, 0))
    _job(runtime, "nine", "0 9 * * *", kind="wake", target="nine", last_run=datetime(2026, 10, 16, 9, 0))
    runtime.scheduler.stagger_s = 0.05

    live = 0
    peak = 0
    stamps: list[float] = []

    async def handler(prompt: str, *, seat: str | None = None) -> str:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        stamps.append(time.monotonic())
        await asyncio.sleep(0.01)
        live -= 1
        return f"woke for {prompt}"

    await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=NOW)

    assert runtime.surface.sent[1:] == ["woke for eight", "woke for nine"], "the queue ran out of order"
    assert peak == 1, "two catch-up runs overlapped"
    assert stamps[1] - stamps[0] >= runtime.scheduler.stagger_s * 0.8, "the runs were not staggered"

    # And a long outage collapses to one message, not a pile.
    _job(runtime, "hourly", "0 * * * *", kind="wake", target="the check-in", last_run=datetime(2026, 10, 15, 9, 0))
    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=_speaks(), when=NOW)
    assert runtime.surface.sent[-1] == "Standup in ten minutes.", "the whole outage was replayed"
    caught = next(result for result in results if result.job == "hourly")
    assert "52 earlier run(s) folded in" in caught.detail


async def test_a_stale_probe_is_skipped_once_with_the_note_in_both_records(runtime: Runtime) -> None:
    # T18 (3/3): the queue's judgement, visible in the tick's result and in the
    # wake log that outlives it.
    job = _job(
        runtime,
        "cap-disk_free_check",
        "0 8 * * *",
        kind="capability",
        target="disk_free_check",
        last_run=datetime(2026, 10, 15, 8, 0),
    )
    results = await runtime.scheduler.dispatch_tick(runtime, when=NOW)

    assert runtime.surface.sent == [], "a stale probe reported a condition that has moved on"
    skipped = next(result for result in results if result.job == "cap-disk_free_check")
    assert skipped.ran is False
    assert skipped.ok is True, "a skip is a decision, not a failure"
    assert "not caught up" in skipped.detail and "past its 1h window" in skipped.detail
    assert runtime.scheduler.log.seen(job.idempotency_key(datetime(2026, 10, 17, 8, 0)))

    # The verdict moves `last_run`, so the next tick has nothing to re-litigate —
    # and re-read from the store, because the disk is the truth across a tick.
    again = await runtime.scheduler.dispatch_tick(runtime, when=NOW + timedelta(minutes=3))
    assert [result for result in again if result.job == "cap-disk_free_check"] == []
    assert runtime.scheduler.get("cap-disk_free_check").last_run == NOW.timestamp()


async def test_catch_up_is_spent_once_and_still_obeys_the_clock_it_speaks_at(
    runtime: Runtime,
) -> None:
    # Quiet hours do not stop applying because the daemon was down: the missed
    # moment decides *what* ran, the present moment whether it may be said.
    _job(
        runtime,
        "briefing",
        "0 22 * * *",
        kind="wake",
        target="the evening check-in",
        last_run=datetime(2026, 10, 16, 22, 0),
    )
    late = datetime(2026, 10, 17, 23, 40)
    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=_speaks(), when=late)
    assert runtime.surface.sent == [], "a caught-up wake ignored quiet hours"
    caught = next(result for result in results if result.job == "briefing")
    assert caught.ran is False
    assert "quiet" in caught.detail
    assert "caught up the missed 22:00 run" in caught.detail, "the run was not recorded as a catch-up"

    # Catch-up is spent once: after the first tick, the due pass governs again.
    _job(
        runtime,
        "morning",
        "0 8 * * *",
        kind="wake",
        target="the check-in",
        last_run=datetime(2026, 10, 16, 8, 0),
    )
    breath = datetime(2026, 10, 18, 14, 20)
    await runtime.scheduler.dispatch_tick(runtime, wake_handler=_speaks(), when=breath)
    assert len(runtime.surface.sent) == 1, "the missed run was not delivered"
    await runtime.scheduler.dispatch_tick(
        runtime, wake_handler=_speaks(), when=breath + timedelta(minutes=3)
    )
    assert len(runtime.surface.sent) == 1, "the missed run was delivered twice"

    # The two passes cannot both claim a minute: the due pass moves `last_run`,
    # which is what makes running the catch-up on every tick affordable.
    _job(runtime, "every_minute", "* * * * *", kind="wake", target="the check-in")
    tick = breath + timedelta(minutes=6)
    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=_speaks(), when=tick)
    assert len(runtime.surface.sent) == 2, "the same minute was delivered twice"
    assert [result.ran for result in results if result.job == "every_minute"] == [True]
    assert runtime.scheduler.get("every_minute").last_run == tick.timestamp()
