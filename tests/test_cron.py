"""Cron matching and navigation (SPEC §7).

One expression names a set of minutes, and everything derived from it — whether
a tick is due, the next and previous occurrence, how many were missed — must
agree with that set. The edges are where it goes wrong: a step that starts at
zero, a "previous" that is really the job's own history, and a rule so sparse
that walking back to it is pointless.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from daedalus.scheduler import cron


def test_a_rule_matches_exactly_the_minutes_it_names() -> None:
    # every minute
    assert cron.matches("* * * * *", datetime(2026, 10, 15, 3, 7))
    # a fixed hour, and the minute after it
    assert cron.matches("0 7 * * *", datetime(2026, 10, 15, 7, 0))
    assert not cron.matches("0 7 * * *", datetime(2026, 10, 15, 7, 1))
    # a step, and a list narrowed by a weekday range
    assert cron.matches("*/15 * * * *", datetime(2026, 10, 15, 0, 45))
    assert not cron.matches("*/15 * * * *", datetime(2026, 10, 15, 0, 46))
    assert cron.matches("0 9,17 * * 1-5", datetime(2026, 10, 15, 17, 0))  # a Thursday

    # Counting over a window uses the same rule, and honours its own limit.
    after, before = datetime(2026, 10, 15, 9, 0), datetime(2026, 10, 17, 14, 0)
    assert cron.count_occurrences("0 * * * *", after, before) == 53
    assert cron.count_occurrences("0 8 * * *", after, before) == 2
    assert cron.count_occurrences("0 * * * *", after, before, limit=10) == 10


def test_navigation_runs_forward_and_backward_but_not_past_its_bounds() -> None:
    # forward, across a day boundary
    assert cron.next_run("30 6 * * *", datetime(2026, 10, 15, 23, 59)) == datetime(2026, 10, 16, 6, 30)
    # backward, to the occurrence earlier today
    assert cron.previous_run("0 8 * * *", datetime(2026, 10, 15, 14, 20)) == datetime(2026, 10, 15, 8, 0)

    # `after` is the job's own history: an occurrence it already answered for is
    # not one it missed.
    assert (
        cron.previous_run(
            "0 8 * * *", datetime(2026, 10, 15, 14, 20), after=datetime(2026, 10, 15, 8, 0)
        )
        is None
    )
    assert cron.previous_run(
        "0 8 * * *", datetime(2026, 10, 15, 14, 20), after=datetime(2026, 10, 14, 8, 0)
    ) == datetime(2026, 10, 15, 8, 0)

    # A rule that fires once a year is not walked back a year to find out: an
    # occurrence past the horizon is stale by any window its callers could hold.
    assert cron.previous_run("0 0 29 2 *", datetime(2026, 10, 15, 14, 0)) is None
    assert cron.previous_run(
        "0 0 29 2 *", datetime(2026, 10, 15, 14, 0), horizon_days=4000
    ) == datetime(2024, 2, 29, 0, 0)


def test_a_malformed_expression_is_refused() -> None:
    with pytest.raises(cron.CronError):
        cron.validate("0 7 * *")  # four fields, not five
    with pytest.raises(cron.CronError):
        cron.validate("99 * * * *")  # no such minute
