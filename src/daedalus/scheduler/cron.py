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
    minutes, hours, doms, months, dows = parse(expr)
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
    while when < limit:
        if matches(expr, when):
            return when
        when += timedelta(minutes=1)
    return None


def validate(expr: str) -> None:
    parse(expr)
