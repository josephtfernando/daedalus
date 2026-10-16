"""Occurrences — the dated events a (possibly recurring) series produces.

A stored event is a *definition*: one row that may say "every weekday at 9:10".
What the owner reads is the calendar, which is the set of dated things that
happen. This module is that translation, and it is deliberately the only place
that decides it, so ``agenda``, ``list``, free/busy and search all agree about
what exists.

Overrides (``RECURRENCE-ID``) are resolved here too: a moved single instance
replaces exactly one occurrence and leaves the series alone. That is the
difference between "the standup moved today" and "the standup moved", and
getting it wrong silently rewrites the owner's week.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Iterable

from . import recur, zones
from .event import Event, overlaps, parse_iso


def occurrence_key(moment: datetime, all_day: bool) -> str:
    """The identity of one occurrence, in the same shape events store times.

    ``EXDATE`` and ``RECURRENCE-ID`` both name an occurrence by its start, so
    this is the string they are matched against.
    """
    return moment.strftime("%Y-%m-%d" if all_day else "%Y-%m-%dT%H:%M")


def expand(
    events: Iterable[Event],
    window_start: datetime,
    window_end: datetime,
    *,
    limit: int = 200,
) -> list[Event]:
    """Every dated occurrence in ``[window_start, window_end)``, ascending.

    One-off events pass through untouched. A series is expanded from its
    ``DTSTART`` up to the window's end, its ``RDATE``s added and its ``EXDATE``s
    dropped, and its overrides substituted in place.
    """
    masters: list[Event] = []
    overrides: dict[str, dict[str, Event]] = {}
    for event in events:
        if event.recurrence_id:
            overrides.setdefault(event.uid, {})[event.recurrence_id] = event
        else:
            masters.append(event)

    found: list[Event] = []
    for master in masters:
        pending = dict(overrides.get(master.uid, {}))
        for detached in _occurrences_of(master, window_start, window_end, pending):
            found.append(detached)
        # An override whose original start the rule never produced (a hand edit
        # beyond COUNT, a rule since shortened) still means something the owner
        # put on the calendar. It is kept rather than silently dropped.
        for leftover in pending.values():
            if overlaps(leftover, window_start, window_end):
                found.append(leftover)

    # A dangling override whose master is gone: the owner's row still exists.
    for uid, group in overrides.items():
        if any(master.uid == uid for master in masters):
            continue
        for detached in group.values():
            if overlaps(detached, window_start, window_end):
                found.append(detached)

    found.sort(
        key=lambda e: (zones.local_wall(e.span()[0]), e.summary.lower())
    )
    return found[:limit]


def _occurrences_of(
    master: Event,
    window_start: datetime,
    window_end: datetime,
    pending: dict[str, Event],
) -> list[Event]:
    """The recurrence set of one series, in the window.

    RFC 5545 defines it as ``RRULE ∪ RDATE − EXDATE``, and it is worth being
    literal about: an added date is a real occurrence, and leaving one out is the
    same act whether the rule produced it or a person typed it. A series with no
    rule still contributes its own start plus any ``RDATE`` — "this, and also
    these three" is a legal event.
    """
    moments: list[datetime] = []
    if master.rrule:
        rule = recur.parse_rule(master.rrule)
        # The rule is pure wall-clock arithmetic (recur.py never sees a zone), so
        # expand on the *event's own* clock: a weekday 09:00 is 09:00 in the zone
        # it was written in and stays 09:00 there across a clock change. Only the
        # window bound has to be translated into that clock to be comparable.
        zone = None if master.all_day else zones.zone_for(master.tzid)
        start = parse_iso(master.start)
        stop = zones.wall_in(window_end, zone) if zone is not None else window_end
        moments.extend(recur.occurrences(start, rule, stop=stop))
    else:
        moments.append(parse_iso(master.start))
    moments.extend(parse_iso(str(value)) for value in master.rdates)

    excluded = {str(value) for value in master.exdates}
    end = parse_iso(master.end) if master.end else None
    length = (end - parse_iso(master.start)) if end is not None else None

    out: list[Event] = []
    seen: set[str] = set()
    for moment in sorted(moments):
        key = occurrence_key(moment, master.all_day)
        # `seen` dedupes an RDATE that lands on a date the rule already produced:
        # one day, one standup. `excluded` is the EXDATE.
        if key in seen or key in excluded:
            continue
        seen.add(key)
        override = pending.pop(key, None)
        if override is not None:
            if overlaps(override, window_start, window_end):
                out.append(override)
            continue
        instance = _instance(master, moment, length)
        if overlaps(instance, window_start, window_end):
            out.append(instance)
    return out


def _instance(master: Event, moment: datetime, length: timedelta | None) -> Event:
    """One occurrence of ``master`` at ``moment``, as an event in its own right."""
    if master.all_day:
        start = moment.strftime("%Y-%m-%d")
        end = (moment + length).strftime("%Y-%m-%d") if length else ""
    else:
        start = moment.strftime("%Y-%m-%dT%H:%M")
        end = (moment + length).strftime("%Y-%m-%dT%H:%M") if length else ""
    return replace(
        master,
        start=start,
        end=end,
        rrule="",
        exdates=[],
        recurrence_id=occurrence_key(moment, master.all_day),
    )
