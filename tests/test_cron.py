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
