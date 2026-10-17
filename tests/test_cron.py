from __future__ import annotations

from datetime import datetime

import pytest

from daedalus.scheduler import cron


def test_matches_every_minute() -> None:
    assert cron.matches("* * * * *", datetime(2026, 10, 15, 3, 7))


def test_matches_fixed_time() -> None:
    assert cron.matches("0 7 * * *", datetime(2026, 10, 15, 7, 0))
    assert not cron.matches("0 7 * * *", datetime(2026, 10, 15, 7, 1))


def test_step_and_list() -> None:
    assert cron.matches("*/15 * * * *", datetime(2026, 10, 15, 0, 45))
    assert not cron.matches("*/15 * * * *", datetime(2026, 10, 15, 0, 46))
    assert cron.matches("0 9,17 * * 1-5", datetime(2026, 10, 15, 17, 0))  # a Thursday


def test_rejects_bad_expressions() -> None:
    with pytest.raises(cron.CronError):
        cron.validate("0 7 * *")
    with pytest.raises(cron.CronError):
        cron.validate("99 * * * *")


def test_next_run_is_forward_and_correct() -> None:
    nxt = cron.next_run("30 6 * * *", datetime(2026, 10, 15, 23, 59))
    assert nxt == datetime(2026, 10, 16, 6, 30)


def test_previous_run_is_backward_and_correct() -> None:
    assert cron.previous_run("0 8 * * *", datetime(2026, 10, 15, 14, 20)) == datetime(2026, 10, 15, 8, 0)


def test_previous_run_will_not_walk_past_the_floor() -> None:
    """`after` is the job's own history: an occurrence it already answered for is
    not an occurrence it missed."""
    assert cron.previous_run("0 8 * * *", datetime(2026, 10, 15, 14, 20), after=datetime(2026, 10, 15, 8, 0)) is None
    assert cron.previous_run("0 8 * * *", datetime(2026, 10, 15, 14, 20), after=datetime(2026, 10, 14, 8, 0)) == datetime(
        2026, 10, 15, 8, 0
    )


def test_previous_run_gives_up_beyond_its_horizon() -> None:
    """A rule that fires once a year must not be walked back a year to find out.

    `None` and "too far back to matter" are the same answer to every caller: an
    occurrence past the horizon is stale by any window they could hold.
    """
    assert cron.previous_run("0 0 29 2 *", datetime(2026, 10, 15, 14, 0)) is None
    assert cron.previous_run("0 0 29 2 *", datetime(2026, 10, 15, 14, 0), horizon_days=4000) == datetime(
        2024, 2, 29, 0, 0
    )


def test_count_occurrences_counts_and_stops() -> None:
    after, before = datetime(2026, 10, 15, 9, 0), datetime(2026, 10, 17, 14, 0)

    assert cron.count_occurrences("0 * * * *", after, before) == 53
    assert cron.count_occurrences("0 8 * * *", after, before) == 2
    assert cron.count_occurrences("0 * * * *", after, before, limit=10) == 10
