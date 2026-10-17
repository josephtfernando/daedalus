"""A small 5-field cron matcher. No dependency, no surprises.

Fields: minute hour day-of-month month day-of-week.
Supports ``*``, ``*/n``, ``a-b``, ``a-b/n``, lists, and names are not accepted
(numbers only; day-of-week 0 and 7 both mean Sunday).
"""

from __future__ import annotations

from datetime import datetime, timedelta

_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class CronError(ValueError):
    pass


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError("empty cron field")
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError as exc:
                raise CronError(f"bad step {step_s!r}") from exc
            if step <= 0:
                raise CronError("step must be positive")
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start < lo or end > hi:
            raise CronError(f"{part!r} out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    if hi == 7:  # day-of-week: fold 7 → 0
        if 7 in values:
            values.discard(7)
            values.add(0)
    return values


def parse(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    fields = expr.split()
    if len(fields) != 5:
        raise CronError("cron must have exactly 5 fields")
    return tuple(_parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _RANGES))  # type: ignore[return-value]


def matches(expr: str, when: datetime) -> bool:
    return _matches(expr, parse(expr), when)


def _matches(expr: str, fields: tuple[set[int], ...], when: datetime) -> bool:
    """``matches`` against an already-parsed expression.

    Split out because the callers that scan a range of minutes — `next_run`,
    `previous_run`, `count_occurrences` — would otherwise re-parse the expression
    once per minute, which is the difference between a scan being affordable and
    not.
    """
    minutes, hours, doms, months, dows = fields
    if when.minute not in minutes or when.hour not in hours or when.month not in months:
        return False
    dom_restricted = expr.split()[2] != "*"
    dow_restricted = expr.split()[4] != "*"
    dom_ok = when.day in doms
    dow_ok = (when.weekday() + 1) % 7 in dows
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    if dom_restricted:
        return dom_ok
    if dow_restricted:
        return dow_ok
    return True


def next_run(expr: str, after: datetime | None = None, *, horizon_days: int = 400) -> datetime | None:
    when = (after or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = when + timedelta(days=horizon_days)
    fields = parse(expr)
    while when < limit:
        if _matches(expr, fields, when):
            return when
        when += timedelta(minutes=1)
    return None


def previous_run(
    expr: str,
    before: datetime | None = None,
    *,
    after: datetime | None = None,
    horizon_days: int = 8,
) -> datetime | None:
    """The most recent occurrence strictly before ``before`` — and after ``after``.

    Walks backwards a minute at a time, which is affordable only because of how
    it is asked: a caller with nothing to look for (a job that just ran) never
    reaches it. ``after`` bounds the walk to the gap being explained, and
    ``horizon_days`` bounds it when the gap is enormous — an occurrence older
    than the horizon is stale by any standard the caller could hold, so ``None``
    and "too far back to matter" are the same answer.
    """
    end = (before or datetime.now()).replace(second=0, microsecond=0) - timedelta(minutes=1)
    floor = end - timedelta(days=horizon_days)
    if after is not None:
        floor = max(floor, after.replace(second=0, microsecond=0) + timedelta(minutes=1))
    fields = parse(expr)
    when = end
    while when >= floor:
        if _matches(expr, fields, when):
            return when
        when -= timedelta(minutes=1)
    return None


def count_occurrences(
    expr: str, after: datetime, before: datetime, *, limit: int = 500
) -> int:
    """How many occurrences fall in ``(after, before]``, stopped at ``limit``.

    The cap is the point: this exists so a catch-up run can say *how much* it is
    standing in for, and a number that has already passed "this is a lot" does not
    need to be exact.
    """
    fields = parse(expr)
    when = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    total = 0
    while when <= before:
        if _matches(expr, fields, when):
            total += 1
            if total >= limit:
                return limit
        when += timedelta(minutes=1)
    return total


def validate(expr: str) -> None:
    parse(expr)
