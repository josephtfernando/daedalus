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
    async def handler(prompt: str) -> str:  # noqa: ARG001 — the prompt is the wake's reason
        return text

    return handler


# -- the window the job is judged against -----------------------------------


def test_lateness_is_judged_by_tense_not_by_size() -> None:
    """A capability reports a condition; a wake talks about a day.

    Six hours late is a *false* report from a disk check and an ordinary briefing:
    "the disk is low" was true at 04:00 and may be a lie by 10:00, while a morning
    briefing is still about today at nine. So the default is per kind, and it is
    the difference between the two that matters, not the numbers.
    """
    assert parse_window(DEFAULT_LATE_WITHIN["capability"]) < parse_window(DEFAULT_LATE_WITHIN["wake"])
    assert window_for(Job(name="probe", cron="0 * * * *", kind="capability")) == timedelta(hours=1)
    assert window_for(Job(name="briefing", cron="0 8 * * *", kind="wake")) == timedelta(hours=12)


def test_a_job_may_state_its_own_tolerance() -> None:
    stated = Job(name="slow_mail", cron="0 8 * * *", kind="capability", late_within="2d")

    assert window_for(stated) == timedelta(days=2)


def test_an_unreadable_tolerance_falls_back_rather_than_failing_open() -> None:
    """A typo in a hand-edited `jobs.toml` must not turn a probe into a stale alert."""
    assert parse_window("six hours") is None
    assert parse_window("") is None
    assert parse_window("-1h") is None
    assert window_for(Job(name="probe", cron="0 * * * *", kind="capability", late_within="6h!")) == timedelta(
        hours=1
    )


def test_a_duration_reads_the_way_the_owner_would_write_it() -> None:
    assert parse_window("45m") == timedelta(minutes=45)
    assert parse_window("6h") == timedelta(hours=6)
    assert parse_window("2d") == timedelta(days=2)
    assert parse_window("90s") == timedelta(seconds=90)
    assert parse_window(" 6H ") == timedelta(hours=6)


def test_a_duration_says_itself_the_way_the_owner_would() -> None:
    assert human(timedelta(seconds=30)) == "30s"
    assert human(timedelta(minutes=45)) == "45m"
    assert human(timedelta(hours=6, minutes=20)) == "6h20m"
    assert human(timedelta(hours=2)) == "2h"
    assert human(timedelta(days=3, hours=4)) == "3d4h"


# -- what the queue holds, before anything runs -----------------------------


def test_a_job_that_just_ran_has_missed_nothing(runtime: Runtime) -> None:
    job = _job(runtime, "briefing", "0 8 * * *", kind="wake", last_run=NOW - timedelta(seconds=30))

    assert plan([job], NOW) == []


def test_a_job_that_has_never_run_is_judged_from_its_creation(runtime: Runtime) -> None:
    """A job added while the daemon was down has no `last_run` to start from."""
    job = _job(runtime, "briefing", "0 8 * * *", kind="wake", last_run=None)

    missed = plan([job], NOW)

    assert [m.when for m in missed] == [datetime(2026, 10, 17, 8, 0)]


def test_only_the_latest_missed_moment_is_a_candidate(runtime: Runtime) -> None:
    """Two days of an hourly job is one thing to say, not forty-eight."""
    job = _job(runtime, "hourly", "0 * * * *", kind="wake", last_run=datetime(2026, 10, 15, 9, 0))

    [missed] = plan([job], NOW)

    assert missed.when == datetime(2026, 10, 17, 14, 0)
    assert missed.superseded == 52, "the earlier occurrences were not folded into one"
    assert missed.worth_it


def test_an_occurrence_whose_moment_has_passed_is_not_worth_running(runtime: Runtime) -> None:
    job = _job(
        runtime,
        "cap-disk_free_check",
        "0 8 * * *",
        kind="capability",
        last_run=datetime(2026, 10, 15, 8, 0),
    )

    [missed] = plan([job], NOW)

    assert missed.worth_it is False
    assert missed.when == datetime(2026, 10, 17, 8, 0)
    assert "6h20m late" in missed.reason
    assert "1h window" in missed.reason


def test_a_disabled_job_is_left_alone(runtime: Runtime) -> None:
    job = _job(runtime, "briefing", "0 8 * * *", kind="wake", last_run=datetime(2026, 10, 16, 8, 0))
    job.enabled = False

    assert plan([job], NOW) == []


# -- the tick ---------------------------------------------------------------


async def test_a_run_missed_while_the_daemon_was_down_happens_after_startup(runtime: Runtime) -> None:
    """T18 (1/3): the missed morning reaches the owner instead of vanishing."""
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


async def test_two_missed_jobs_run_one_after_the_other_and_never_together(runtime: Runtime) -> None:
    """T18 (2/3): a start-up is a queue, not a burst.

    Three notifications inside one second is what the owner asked us to avoid;
    *simultaneous* is the failure a test can name exactly.
    """
    _job(runtime, "eight", "0 8 * * *", kind="wake", target="eight", last_run=datetime(2026, 10, 16, 8, 0))
    _job(runtime, "nine", "0 9 * * *", kind="wake", target="nine", last_run=datetime(2026, 10, 16, 9, 0))
    runtime.scheduler.stagger_s = 0.05

    live = 0
    peak = 0
    stamps: list[float] = []

    async def handler(prompt: str) -> str:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        stamps.append(time.monotonic())
        await asyncio.sleep(0.01)
        live -= 1
        return f"woke for {prompt}"

    await runtime.scheduler.dispatch_tick(runtime, wake_handler=handler, when=NOW)

    assert runtime.surface.sent == ["woke for eight", "woke for nine"], "the queue ran out of order"
    assert peak == 1, "two catch-up runs overlapped"
    assert stamps[1] - stamps[0] >= runtime.scheduler.stagger_s * 0.8, "the runs were not staggered"


async def test_a_missed_probe_whose_moment_has_passed_is_skipped_with_a_note(runtime: Runtime) -> None:
    """T18 (3/3): the queue's judgement, and it is visible in both places.

    Skipping is the right answer here, so the record has to say so — in the tick's
    own result, which the journal prints, and in the wake log that outlives it.
    """
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


async def test_a_skipped_run_is_decided_once_and_not_re_asked(runtime: Runtime) -> None:
    """The verdict moves `last_run`, so the next tick has nothing to re-litigate.

    Without this, a stale job would be re-examined — and re-noted — on every tick
    for the rest of the day.
    """
    job = _job(
        runtime,
        "cap-disk_free_check",
        "0 8 * * *",
        kind="capability",
        last_run=datetime(2026, 10, 15, 8, 0),
    )

    await runtime.scheduler.dispatch_tick(runtime, when=NOW)
    again = await runtime.scheduler.dispatch_tick(runtime, when=NOW + timedelta(minutes=3))

    assert [result for result in again if result.job == "cap-disk_free_check"] == []
    assert job.last_run == NOW.timestamp()


async def test_a_long_outage_collapses_to_one_message_not_a_pile(runtime: Runtime) -> None:
    _job(runtime, "hourly", "0 * * * *", kind="wake", target="the check-in", last_run=datetime(2026, 10, 15, 9, 0))

    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=_speaks(), when=NOW)

    assert runtime.surface.sent == ["Standup in ten minutes."], "the whole outage was replayed"
    caught = next(result for result in results if result.job == "hourly")
    assert "52 earlier run(s) folded in" in caught.detail


async def test_a_caught_up_wake_is_gated_by_the_clock_it_speaks_at_not_the_one_it_missed(runtime: Runtime) -> None:
    """Quiet hours do not stop applying because the daemon was down.

    The missed moment decides *what* ran; the present moment decides whether it
    may be said. A briefing caught up at 23:40 belongs to the next morning.
    """
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


async def test_a_normal_tick_a_breath_later_does_not_replay_the_queue(runtime: Runtime) -> None:
    """Catch-up is spent once: after the first tick, the due pass governs again."""
    _job(runtime, "briefing", "0 8 * * *", kind="wake", target="the check-in", last_run=datetime(2026, 10, 16, 8, 0))

    await runtime.scheduler.dispatch_tick(runtime, wake_handler=_speaks(), when=NOW)
    assert len(runtime.surface.sent) == 1

    await runtime.scheduler.dispatch_tick(runtime, wake_handler=_speaks(), when=NOW + timedelta(minutes=3))

    assert len(runtime.surface.sent) == 1, "the missed run was delivered twice"


async def test_a_job_that_missed_exactly_now_is_the_due_pass_s_business(runtime: Runtime) -> None:
    """The two passes cannot both claim a minute: the due pass moves `last_run`.

    This is what makes running the catch-up every tick affordable.
    """
    job = _job(runtime, "every_minute", "* * * * *", kind="wake", target="the check-in")

    results = await runtime.scheduler.dispatch_tick(runtime, wake_handler=_speaks(), when=NOW)

    assert len(runtime.surface.sent) == 1, "the same minute was delivered twice"
    assert [result.ran for result in results if result.job == "every_minute"] == [True]
    assert job.last_run == NOW.timestamp()
