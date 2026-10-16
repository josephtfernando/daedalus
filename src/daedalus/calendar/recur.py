"""Recurrence rules (RFC 5545 §3.3.10) and their expansion into real dates.

A recurring event is **one** ``VEVENT`` carrying an ``RRULE``. What the owner
lives by is not the rule but the *occurrences* it produces — and telling someone
"the standup, every weekday" is not the same as telling them "what is on
Thursday". gcal's defining trick is that it can answer the second question, so
this module turns a rule into concrete datetimes.

The algorithm is period-based, which is how RFC 5545 describes it: step the
frequency (a day, a week, a month, a year), generate the candidate days inside
that period from the ``BY*`` parts, and order them. That is what the standard
calls the "recurrence set", and following it is why ``FREQ=MONTHLY;BYDAY=MO``
means *every Monday of the month* rather than *the Mondays near DTSTART*.

Supported: ``FREQ`` ``DAILY`` / ``WEEKLY`` / ``MONTHLY`` / ``YEARLY``, plus
``INTERVAL``, ``COUNT``, ``UNTIL``, ``BYDAY`` (with ordinals, e.g. ``-1FR``),
``BYMONTHDAY``, ``BYMONTH``, ``BYSETPOS`` and ``WKST``. **Not** supported, and
refused rather than guessed, are the sub-daily frequencies (``SECONDLY`` /
``MINUTELY`` / ``HOURLY``): they are not things a person writes into a calendar,
and silently treating one as ``DAILY`` would be a wrong answer delivered
confidently.
"""

from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")

#: A hard ceiling on how far a rule will be walked. Every caller windows the
#: expansion, so this is a backstop against a mistyped rule, not a real limit.
_MAX_PERIODS = 200_000


class RuleError(ValueError):
    """An ``RRULE`` this module will not pretend to understand."""


@dataclass(slots=True, frozen=True)
class Recurrence:
    """A parsed ``RRULE``. ``raw`` is kept so the file round-trips verbatim."""

    freq: str
    raw: str
    interval: int = 1
    count: int | None = None
    until: datetime | None = None
    byday: tuple[tuple[int | None, int], ...] = ()
    bymonthday: tuple[int, ...] = ()
    bymonth: tuple[int, ...] = ()
    bysetpos: tuple[int, ...] = ()
    wkst: int = 0


# -- parsing ----------------------------------------------------------------


def parse_rule(text: str) -> Recurrence:
    """Read an ``RRULE`` value. Raises :class:`RuleError` on anything odd."""
    raw = str(text).strip()
    parts: dict[str, str] = {}
    for chunk in raw.split(";"):
        if not chunk.strip():
            continue
        key, _, value = chunk.partition("=")
        parts[key.strip().upper()] = value.strip()

    freq = parts.get("FREQ", "").upper()
    if not freq:
        raise RuleError(f"RRULE has no FREQ: {raw!r}")
    if freq not in FREQUENCIES:
        supported = ", ".join(FREQUENCIES)
        raise RuleError(
            f"RRULE FREQ={freq} is not supported (a person writes {supported}); "
            "I will not guess at its meaning."
        )

    return Recurrence(
        freq=freq,
        raw=raw,
        interval=_positive(parts.get("INTERVAL"), "INTERVAL", default=1),
        count=_positive(parts.get("COUNT"), "COUNT", default=None),
        until=_until(parts.get("UNTIL")),
        byday=_byday(parts.get("BYDAY", "")),
        bymonthday=_bymonthday(parts.get("BYMONTHDAY", "")),
        bymonth=_bymonth(parts.get("BYMONTH", "")),
        bysetpos=_by_setpos(parts.get("BYSETPOS", "")),
        wkst=_weekday(parts.get("WKST", ""), default=0, what="WKST"),
    )


def _positive(value: str | None, what: str, *, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except ValueError as exc:
        raise RuleError(f"{what}={value!r} is not a number") from exc
    if number < 1:
        raise RuleError(f"{what}={value!r} must be positive")
    return number


def _weekday(token: str, *, default: int, what: str) -> int:
    text = token.strip().upper()
    if not text:
        return default
    if text not in WEEKDAYS:
        raise RuleError(f"{what}={token!r} is not a weekday (MO…SU)")
    return WEEKDAYS[text]


def _until(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().rstrip("Zz")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise RuleError(f"UNTIL={value!r} is not a date I can read")


def _byday(value: str) -> tuple[tuple[int | None, int], ...]:
    out: list[tuple[int | None, int]] = []
    for token in value.split(","):
        text = token.strip().upper()
        if not text:
            continue
        digits = ""
        index = 0
        while index < len(text) and (text[index].isdigit() or text[index] in "+-"):
            digits += text[index]
            index += 1
        name = text[index:]
        if name not in WEEKDAYS:
            raise RuleError(f"BYDAY={token!r} is not a weekday (MO…SU, optionally e.g. -1FR)")
        ordinal = None
        if digits not in ("", "+"):
            try:
                ordinal = int(digits)
            except ValueError as exc:
                raise RuleError(f"BYDAY={token!r} has an unreadable ordinal") from exc
            if ordinal == 0:
                raise RuleError(f"BYDAY={token!r}: an ordinal of 0 means nothing")
        out.append((ordinal, WEEKDAYS[name]))
    return tuple(out)


def _bymonthday(value: str) -> tuple[int, ...]:
    out: list[int] = []
    for token in value.split(","):
        text = token.strip()
        if not text:
            continue
        try:
            day = int(text)
        except ValueError as exc:
            raise RuleError(f"BYMONTHDAY={token!r} is not a number") from exc
        if day == 0 or abs(day) > 31:
            raise RuleError(f"BYMONTHDAY={token!r} is not a day of the month")
        out.append(day)
    return tuple(out)


def _bymonth(value: str) -> tuple[int, ...]:
    out: list[int] = []
    for token in value.split(","):
        text = token.strip().upper()
        if not text:
            continue
        if text in MONTHS:
            out.append(MONTHS[text])
            continue
        try:
            month = int(text)
        except ValueError as exc:
            raise RuleError(f"BYMONTH={token!r} is not a month") from exc
        if not 1 <= month <= 12:
            raise RuleError(f"BYMONTH={token!r} is not a month")
        out.append(month)
    return tuple(out)


def _by_setpos(value: str) -> tuple[int, ...]:
    out: list[int] = []
    for token in value.split(","):
        text = token.strip()
        if not text:
            continue
        try:
            position = int(text)
        except ValueError as exc:
            raise RuleError(f"BYSETPOS={token!r} is not a number") from exc
        if position == 0:
            raise RuleError("BYSETPOS=0 means nothing")
        out.append(position)
    return tuple(out)


# -- expansion --------------------------------------------------------------


def occurrences(
    start: datetime,
    rule: Recurrence,
    *,
    stop: datetime | None = None,
    limit: int = 100_000,
) -> list[datetime]:
    """Every occurrence of ``rule`` from ``start``, windowed.

    ``start`` (DTSTART) is always the first occurrence: RFC 5545 defines it that
    way, and it is the only reading under which a rule whose seed does not match
    its own ``BY`` parts is still well-defined. ``stop`` is an *exclusive*
    ceiling — expansion halts past it, so "the next week" does not walk a daily
    rule into the year 3000.
    """
    out: list[datetime] = [start]
    if limit <= 1:
        return out
    if rule.count is not None and rule.count <= 1:
        return out

    seen = {start}

    def done() -> bool:
        if len(out) >= limit:
            return True
        return rule.count is not None and len(out) >= rule.count

    for period in range(_MAX_PERIODS):
        if done():
            break
        floor = _period_floor(start, rule, period)
        if floor is not None:
            if rule.until is not None and floor > rule.until:
                break
            if stop is not None and floor >= stop:
                break
        for moment in _candidates(start, rule, period):
            if moment <= start or moment in seen:
                continue
            if rule.until is not None and moment > rule.until:
                return out
            if stop is not None and moment >= stop:
                return out
            seen.add(moment)
            out.append(moment)
            if done():
                return out
    return out


def _period_floor(start: datetime, rule: Recurrence, period: int) -> datetime | None:
    """The earliest possible moment of a period, for cheap early bail-out."""
    if rule.freq == "DAILY":
        return start + timedelta(days=rule.interval * period)
    if rule.freq == "WEEKLY":
        week_start = start.date() - timedelta(days=(start.weekday() - rule.wkst) % 7)
        return datetime.combine(week_start + timedelta(weeks=rule.interval * period), time())
    if rule.freq == "MONTHLY":
        year, month = _month_add(start.year, start.month, rule.interval * period)
        return datetime(year, month, 1)
    if rule.freq == "YEARLY":
        return datetime(start.year + rule.interval * period, 1, 1)
    return None


def _candidates(start: datetime, rule: Recurrence, period: int) -> list[datetime]:
    """The occurrence datetimes a single period contributes, ascending."""
    if rule.freq == "DAILY":
        day = start + timedelta(days=rule.interval * period)
        return [day] if _day_allowed(day.date(), rule) else []
    if rule.freq == "WEEKLY":
        week_start = start.date() - timedelta(days=(start.weekday() - rule.wkst) % 7)
        base = week_start + timedelta(weeks=rule.interval * period)
        weekdays = {wd for ordinal, wd in rule.byday if ordinal is None}
        if not weekdays:
            # A plain WEEKLY repeats on the weekday of DTSTART.
            weekdays = {start.weekday()}
        days: list[date] = []
        for weekday in sorted(weekdays):
            day = base + timedelta(days=(weekday - rule.wkst) % 7)
            if rule.bymonth and day.month not in rule.bymonth:
                continue
            days.append(day)
        return [datetime.combine(day, start.time()) for day in sorted(days)]
    if rule.freq == "MONTHLY":
        year, month = _month_add(start.year, start.month, rule.interval * period)
        return _month_candidates(year, month, start, rule)
    if rule.freq == "YEARLY":
        year = start.year + rule.interval * period
        if not rule.bymonth and rule.byday:
            # "every Monday of the year", "the first Monday of the year" — the
            # weekday BY parts expand over the year, not over one month.
            return _year_candidates(year, start, rule)
        out: list[datetime] = []
        for month in sorted(rule.bymonth or (start.month,)):
            out.extend(_month_candidates(year, month, start, rule))
        return sorted(out)
    return []


def _month_candidates(
    year: int, month: int, start: datetime, rule: Recurrence
) -> list[datetime]:
    if rule.bymonth and month not in rule.bymonth:
        return []
    days = _candidate_days(year, month, start, rule)
    moments = [datetime(year, month, day, start.hour, start.minute) for day in days]
    if rule.bysetpos:
        moments = _apply_setpos(moments, rule.bysetpos)
    return moments


def _year_candidates(year: int, start: datetime, rule: Recurrence) -> list[datetime]:
    plain = sorted({wd for ordinal, wd in rule.byday if ordinal is None})
    ordinals = [(ordinal, wd) for ordinal, wd in rule.byday if ordinal is not None]
    days: set[int] = set()
    if plain:
        days |= {d for d in _year_day_numbers(year) if date.fromordinal(d).weekday() in plain}
    for ordinal, weekday in ordinals:
        day_of_year = _nth_weekday_of_year(year, ordinal, weekday)
        if day_of_year is not None:
            days.add(day_of_year)
    moments = [
        datetime.combine(date.fromordinal(d), start.time()) for d in sorted(days)
    ]
    if rule.bysetpos:
        moments = _apply_setpos(moments, rule.bysetpos)
    return moments


def _candidate_days(year: int, month: int, start: datetime, rule: Recurrence) -> list[int]:
    """The days of a month a MONTHLY/YEARLY rule selects, ascending."""
    days_in = _calendar.monthrange(year, month)[1]
    wanted: set[int] = set()
    if rule.bymonthday:
        for day in rule.bymonthday:
            wanted.add(day if day > 0 else days_in + day + 1)
        wanted = {d for d in wanted if 1 <= d <= days_in}
    if rule.byday:
        selected: set[int] = set()
        for ordinal, weekday in rule.byday:
            if ordinal is None:
                selected |= {
                    d for d in range(1, days_in + 1) if date(year, month, d).weekday() == weekday
                }
            else:
                found = _nth_weekday(year, month, ordinal, weekday)
                if found is not None:
                    selected.add(found)
        wanted = selected if not rule.bymonthday else (wanted & selected)
    if not wanted:
        if rule.bymonthday or rule.byday:
            return []
        # A plain MONTHLY keeps the day of DTSTART — and skips a month whose
        # shortest form has no such day (the 31st in February), as RFC wants.
        if start.day <= days_in:
            wanted = {start.day}
    return sorted(wanted)


def _day_allowed(day: date, rule: Recurrence) -> bool:
    """Whether a DAILY period's day passes the ``BY*`` filters."""
    if rule.bymonth and day.month not in rule.bymonth:
        return False
    if rule.bymonthday:
        days_in = _calendar.monthrange(day.year, day.month)[1]
        allowed = {d if d > 0 else days_in + d + 1 for d in rule.bymonthday}
        if day.day not in allowed:
            return False
    weekdays = {wd for ordinal, wd in rule.byday if ordinal is None}
    if weekdays and day.weekday() not in weekdays:
        return False
    return True


def _apply_setpos(moments: list[datetime], positions: tuple[int, ...]) -> list[datetime]:
    kept: list[datetime] = []
    total = len(moments)
    for position in positions:
        index = position - 1 if position > 0 else total + position
        if 0 <= index < total and moments[index] not in kept:
            kept.append(moments[index])
    return sorted(kept)


def _month_add(year: int, month: int, delta: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def _nth_weekday(year: int, month: int, ordinal: int, weekday: int) -> int | None:
    days_in = _calendar.monthrange(year, month)[1]
    if ordinal > 0:
        delta = (weekday - date(year, month, 1).weekday()) % 7
        day = 1 + delta + (ordinal - 1) * 7
    else:
        delta = (date(year, month, days_in).weekday() - weekday) % 7
        day = days_in - delta - (abs(ordinal) - 1) * 7
    return day if 1 <= day <= days_in else None


def _year_day_numbers(year: int) -> range:
    first = date(year, 1, 1).toordinal()
    last = date(year, 12, 31).toordinal()
    return range(first, last + 1)


def _nth_weekday_of_year(year: int, ordinal: int, weekday: int) -> int | None:
    first = date(year, 1, 1)
    last = date(year, 12, 31)
    if ordinal > 0:
        delta = (weekday - first.weekday()) % 7
        moment = first + timedelta(days=delta + (ordinal - 1) * 7)
    else:
        delta = (last.weekday() - weekday) % 7
        moment = last - timedelta(days=delta + (abs(ordinal) - 1) * 7)
    return moment.toordinal() if first <= moment <= last else None


def describe(rule: Recurrence) -> str:
    """A short human phrase, for a listing the owner reads."""
    names = {number: name for name, number in WEEKDAYS.items()}
    if rule.freq == "DAILY":
        text = "daily" if rule.interval == 1 else f"every {rule.interval} days"
    elif rule.freq == "WEEKLY":
        text = "weekly" if rule.interval == 1 else f"every {rule.interval} weeks"
        days = ", ".join(names[wd] for ordinal, wd in rule.byday if ordinal is None)
        if days:
            text += f" on {days}"
    elif rule.freq == "MONTHLY":
        text = "monthly" if rule.interval == 1 else f"every {rule.interval} months"
        if rule.byday:
            days = ", ".join(
                names[wd] if ordinal is None else f"{_ordinal(ordinal)} {names[wd]}"
                for ordinal, wd in rule.byday
            )
            text += f" on the {days}"
    elif rule.freq == "YEARLY":
        text = "yearly" if rule.interval == 1 else f"every {rule.interval} years"
    else:
        text = rule.freq.lower()
    if rule.count is not None:
        text += f", {rule.count} times"
    elif rule.until is not None:
        text += f", until {rule.until.strftime('%d %b %Y')}"
    return text


def _ordinal(number: int) -> str:
    words = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}
    if number in words:
        return words[number]
    if number == -1:
        return "last"
    return f"{number}th" if number > 0 else f"{abs(number)}th from last"
