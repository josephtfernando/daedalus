"""Recurrence expansion (RFC 5545 §3.3.10).

The property under test: a rule names a *set* of dates, and the set is the one
RFC 5545 describes — not an approximation that happens to work for "every
Monday". The awkward cases are where a naive implementation quietly differs:
the 31st in February, the second Tuesday, the last weekday of the month.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from daedalus.calendar.recur import RuleError, describe, occurrences, parse_rule


def _run(rule_text: str, start: str, *, stop: str | None = None, limit: int = 100):
    rule = parse_rule(rule_text)
    return occurrences(
        datetime.fromisoformat(start),
        rule,
        stop=datetime.fromisoformat(stop) if stop else None,
        limit=limit,
    )


def _days(moments) -> list[str]:
    return [m.strftime("%Y-%m-%d") for m in moments]


def test_the_start_seeds_the_set_even_when_it_does_not_match_the_rule() -> None:
    """DTSTART is the first occurrence even when it is not one the BY parts pick."""
    moments = _run("FREQ=WEEKLY;BYDAY=MO", "2026-10-14T09:00", stop="2026-11-01T00:00")

    assert moments[0] == datetime(2026, 10, 14, 9, 0)  # a Wednesday
    assert _days(moments)[1:] == ["2026-10-19", "2026-10-26"]


def test_a_frequency_walks_forward_from_the_start() -> None:
    # daily
    assert _days(_run("FREQ=DAILY", "2026-10-01T08:00", stop="2026-10-06T00:00")) == [
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "2026-10-04",
        "2026-10-05",
    ]
    # every named weekday, starting on the Monday
    assert _days(_run("FREQ=WEEKLY;BYDAY=MO,WE,FR", "2026-10-12T09:10", stop="2026-10-26T00:00")) == [
        "2026-10-12",  # Mon, the start
        "2026-10-14",
        "2026-10-16",
        "2026-10-19",
        "2026-10-21",
        "2026-10-23",
    ]
    # every other period
    assert _days(_run("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO", "2026-10-12T09:00", stop="2026-12-01T00:00")) == [
        "2026-10-12",
        "2026-10-26",
        "2026-11-09",
        "2026-11-23",
    ]
    # monthly, on the start's day of the month
    assert _days(_run("FREQ=MONTHLY", "2026-01-15T10:00", stop="2026-05-01T00:00")) == [
        "2026-01-15",
        "2026-02-15",
        "2026-03-15",
        "2026-04-15",
    ]
    # yearly, on a fixed date
    assert _days(_run("FREQ=YEARLY", "2026-06-01T10:00", stop="2030-01-01T00:00")) == [
        "2026-06-01",
        "2027-06-01",
        "2028-06-01",
        "2029-06-01",
    ]
    # yearly, over the chosen months
    assert _days(
        _run("FREQ=YEARLY;BYMONTH=MAR,SEP;BYMONTHDAY=1", "2026-03-01T09:00", stop="2028-01-01T00:00")
    ) == ["2026-03-01", "2026-09-01", "2027-03-01", "2027-09-01"]


def test_a_bound_ends_the_set_and_until_is_inclusive() -> None:
    # COUNT bounds the set
    assert _days(_run("FREQ=DAILY;COUNT=3", "2026-10-01T08:00")) == [
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
    ]
    # UNTIL includes the moment it names
    assert _days(_run("FREQ=DAILY;UNTIL=20261004T080000", "2026-10-01T08:00")) == [
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "2026-10-04",
    ]
    # the window is a bound too: a daily rule does not walk forever to answer it
    assert len(_run("FREQ=DAILY", "2020-01-01T08:00", stop="2020-01-08T00:00")) == 7


def test_a_by_part_selects_inside_the_period_instead_of_approximating() -> None:
    # the second Tuesday, not the 13th of every month
    assert _days(_run("FREQ=MONTHLY;BYDAY=2TU", "2026-01-13T09:00", stop="2026-05-01T00:00")) == [
        "2026-01-13",
        "2026-02-10",
        "2026-03-10",
        "2026-04-14",
    ]
    # the last Friday
    assert _days(_run("FREQ=MONTHLY;BYDAY=-1FR", "2026-01-30T17:00", stop="2026-05-01T00:00")) == [
        "2026-01-30",
        "2026-02-27",
        "2026-03-27",
        "2026-04-24",
    ]
    # every Monday of the month is not one Monday — the trap a naive expansion falls into
    assert _days(_run("FREQ=MONTHLY;BYDAY=MO", "2026-10-05T09:00", stop="2026-11-01T00:00")) == [
        "2026-10-05",
        "2026-10-12",
        "2026-10-19",
        "2026-10-26",
    ]
    # the last weekday of the month needs BYSETPOS
    assert _days(
        _run(
            "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1",
            "2026-10-30T16:00",
            stop="2027-01-01T00:00",
        )
    ) == ["2026-10-30", "2026-11-30", "2026-12-31"]
    # a negative month day counts back from the end
    assert _days(_run("FREQ=MONTHLY;BYMONTHDAY=-1", "2026-01-31T23:00", stop="2026-05-01T00:00")) == [
        "2026-01-31",
        "2026-02-28",
        "2026-03-31",
        "2026-04-30",
    ]
    # a weekday filter narrows a daily rule: Fri the 9th is the start, the weekend is skipped
    assert _days(
        _run("FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR", "2026-10-09T09:00", stop="2026-10-14T00:00")
    ) == ["2026-10-09", "2026-10-12", "2026-10-13"]
    # the 31st exists in January, not in February — the month is skipped, not clamped
    assert _days(_run("FREQ=MONTHLY", "2026-01-31T10:00", stop="2026-05-01T00:00")) == [
        "2026-01-31",
        "2026-03-31",
    ]


def test_an_unsupported_or_incomplete_rule_is_refused() -> None:
    with pytest.raises(RuleError) as caught:
        parse_rule("FREQ=HOURLY")
    assert "not supported" in str(caught.value)

    with pytest.raises(RuleError):
        parse_rule("INTERVAL=2")


def test_a_readable_rule_is_described_in_words() -> None:
    assert describe(parse_rule("FREQ=WEEKLY;BYDAY=MO,WE,FR")) == "weekly on MO, WE, FR"
    assert describe(parse_rule("FREQ=DAILY;COUNT=5")) == "daily, 5 times"
    assert describe(parse_rule("FREQ=MONTHLY;BYDAY=-1FR")).startswith("monthly on the")
