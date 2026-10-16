"""The internal calendar store (SPEC §5.5, ARCHITECTURE §8.5).

``$DAEDALUS_HOME/calendar/events.ics`` is the calendar. This class is the only
thing that writes it, and it does so by **re-reading first**: every mutation
loads the current file, changes it, and writes the whole document back. That is
what makes the store genuinely file-first — an event the owner added by hand, a
property this module does not model, a ``VTIMEZONE`` block, all survive a tool
write instead of being flattened by a private copy of the truth.

Reads answer with **occurrences**, not definitions: ask for today and a
recurring standup appears as itself on today's date (see :mod:`.occurrences`).
Writes can address either the whole series or one instance of it, which is the
difference between "the standup moved" and "today's standup moved".

Only the ``Daedalus`` calendar is writable here. The owner's other calendars
are gcal's, and gcal is not wired yet — see :meth:`sync_state`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ..security.provenance import DataClass
from . import ical, recur, zones
from .event import INTERNAL, OPAQUE, Event, Reminder, normalise_when, parse_iso
from .index import CalendarIndex
from .occurrences import expand as expand_occurrences

#: How an edit is scoped when an event repeats.
ALL = "all"
THIS = "this"
FUTURE = "future"


class CalendarStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.ics_path = root / "events.ics"
        self.sync_path = root / "sync_state.json"
        self.index = CalendarIndex(root / "index.sqlite")
        self._sync()

    # -- the file is the truth --------------------------------------------

    def _revision(self) -> int:
        """The file's identity, at nanosecond resolution.

        Coarse mtimes are not enough: a hand edit can land in the same second
        as the last tool write, and a store that missed it would quietly serve
        a stale cache over the owner's own edit.
        """
        try:
            return self.ics_path.stat().st_mtime_ns
        except OSError:
            return 0

    def _load(self) -> ical.ParsedCalendar:
        if not self.ics_path.exists():
            return ical.ParsedCalendar()
        return ical.parse_calendar(self.ics_path.read_text(encoding="utf-8"))

    def _sync(self) -> None:
        """Refresh the cache when ``events.ics`` has moved on since it was built."""
        if self.index.source_revision() != self._revision():
            self.rebuild_index()

    def rebuild_index(self) -> int:
        """Reconstruct the index from ``events.ics``. Returns events indexed."""
        parsed = self._load()
        self.index.replace_all(parsed.events, source_revision=self._revision())
        return len(parsed.events)

    def _write(self, parsed: ical.ParsedCalendar) -> None:
        ordered = sorted(
            parsed.events,
            key=lambda e: (zones.local_wall(e.span()[0]), e.recurrence_id, e.summary.lower()),
        )
        self.ics_path.write_text(
            ical.render_calendar(ordered, parsed.passthrough), encoding="utf-8"
        )
        self.index.replace_all(ordered, source_revision=self._revision())

    # -- writes ------------------------------------------------------------

    def add(
        self,
        *,
        summary: str,
        start: str,
        end: str = "",
        rrule: str = "",
        reminders: Iterable[Any] = (),
        transparency: str = OPAQUE,
        tzid: str = "",
        **fields: Any,
    ) -> Event:
        """Add an event. The shape of ``start`` decides whether it is all-day."""
        text = str(summary).strip()
        if not text:
            raise ValueError("an event needs a summary")
        rule = str(rrule).strip()
        if rule:
            recur.parse_rule(rule)  # raises RuleError (a ValueError) on nonsense
        start_iso, all_day = normalise_when(start)
        parsed = self._load()
        event = Event(
            summary=text,
            start=start_iso,
            end=normalise_when(end)[0] if end else "",
            all_day=all_day,
            rrule=rule,
            reminders=_reminders(reminders),
            transparency=transparency or OPAQUE,
            tzid=str(tzid).strip(),
            **fields,
        )
        parsed.events.append(event)
        self._write(parsed)
        return event

    def update(
        self,
        uid: str,
        *,
        recurrence_id: str = "",
        scope: str = ALL,
        **changes: Any,
    ) -> Event | None:
        """Change an event. ``None`` means there is no such event.

        ``scope`` decides what a change to a repeating event means:
        ``all`` edits the series, ``this`` moves or edits a single instance,
        ``future`` splits the series and leaves everything before it alone.
        Only the keys actually given are touched, so a move does not blank the
        description. Empty values are ignored rather than treated as erasures.
        """
        wanted = _wanted(changes)
        if not wanted:
            raise ValueError("nothing to change")
        parsed = self._load()
        master = _find(parsed.events, uid, "")
        if master is None:
            return None
        if not master.rrule or scope == ALL:
            _apply(master, wanted)
            master.updated_at = time.time()
            self._write(parsed)
            return master
        if scope == THIS:
            return self._update_instance(parsed, master, recurrence_id, wanted)
        if scope == FUTURE:
            return self._update_future(parsed, master, recurrence_id, wanted)
        raise ValueError(f"unknown scope {scope!r} — use 'all', 'this' or 'future'")

    def _update_instance(
        self, parsed: ical.ParsedCalendar, master: Event, occurrence: str, wanted: dict[str, Any]
    ) -> Event:
        if not occurrence:
            raise ValueError(
                "editing one occurrence needs `recurrence_id` — the instance's own "
                "start, as `agenda` prints it"
            )
        key = _key_for(master, occurrence)
        existing = _find(parsed.events, master.uid, key)
        if existing is None:
            # A new override: the *same* uid as its series — that is what makes
            # it an override rather than an unrelated event — with its own
            # DTSTART and a RECURRENCE-ID pinning the instance it replaces.
            existing = Event(
                uid=master.uid,
                summary=master.summary,
                start=key,
                end=master.end,
                all_day=master.all_day,
                location=master.location,
                description=master.description,
                calendar=master.calendar,
                authority=master.authority,
                data_class=master.data_class,
                tzid=master.tzid,
                recurrence_id=key,
            )
            _copy_span(master, existing, counter=_offset_for(master, key))
            parsed.events.append(existing)
        _apply(existing, wanted)
        existing.updated_at = time.time()
        self._write(parsed)
        return existing

    def _update_future(
        self, parsed: ical.ParsedCalendar, master: Event, occurrence: str, wanted: dict[str, Any]
    ) -> Event:
        """Cut the series at ``occurrence`` and carry on from there.

        Two clean series is easier to reason about than a chain of
        ``THISANDFUTURE`` overrides, and every calendar app reads it.
        """
        if not occurrence:
            raise ValueError(
                "editing everything from a date needs `recurrence_id` — the instance "
                "the change starts at, as `agenda` prints it"
            )
        key = _key_for(master, occurrence)
        tail_rrule = _truncate(master, key)
        start_iso, all_day = normalise_when(key)
        tail = Event(
            summary=master.summary,
            start=start_iso,
            end=master.end,
            all_day=all_day,
            location=master.location,
            description=master.description,
            calendar=master.calendar,
            authority=master.authority,
            data_class=master.data_class,
            rrule=tail_rrule,
            exdates=[value for value in master.exdates if str(value) >= key],
            tzid=master.tzid,
            transparency=master.transparency,
            reminders=list(master.reminders),
        )
        master.exdates = [value for value in master.exdates if str(value) < key]
        master.updated_at = time.time()
        parsed.events.append(tail)
        _apply(tail, wanted)
        self._write(parsed)
        return tail

    def delete(self, uid: str, *, recurrence_id: str = "", scope: str = ALL) -> bool:
        """Remove an event, or leave one instance out of a series."""
        parsed = self._load()
        master = _find(parsed.events, uid, "")
        if master is None:
            return False
        if not master.rrule or scope == ALL:
            keep = [e for e in parsed.events if e.uid != uid]
            changed = len(keep) != len(parsed.events)
            if not changed:
                return False
            parsed.events = keep
            self._write(parsed)
            return True
        if scope == THIS:
            key = _key_for(master, recurrence_id) if recurrence_id else ""
            if not key:
                raise ValueError(
                    "dropping one occurrence needs `recurrence_id` — the instance's "
                    "own start"
                )
            master.exdates = sorted({*master.exdates, key})
            master.updated_at = time.time()
            # An override of that instance is now moot; the exclusion speaks.
            parsed.events = [
                e
                for e in parsed.events
                if not (e.uid == uid and e.recurrence_id == key)
            ]
            self._write(parsed)
            return True
        if scope == FUTURE:
            key = _key_for(master, recurrence_id) if recurrence_id else ""
            if not key:
                raise ValueError(
                    "dropping the rest of a series needs `recurrence_id` — the "
                    "instance it should stop before"
                )
            moment = parse_iso(key) - timedelta(minutes=1)
            master.rrule = _with_until(master, moment)
            master.updated_at = time.time()
            parsed.events = [
                e
                for e in parsed.events
                if not (e.uid == uid and e.recurrence_id and e.recurrence_id >= key)
            ]
            self._write(parsed)
            return True
        raise ValueError(f"unknown scope {scope!r} — use 'all', 'this' or 'future'")

    # -- bulk, by name -----------------------------------------------------

    def matching(
        self, text: str, *, limit: int = 200, allow_local_only: bool = True
    ) -> list[Event]:
        """Series whose summary contains ``text``, case-insensitively.

        Matched on the *summary*, not the description: "move all my standups"
        means the things *called* standup. A bulk change that also caught every
        event merely mentioning the word in its notes would be a surprise in the
        worst place — someone else's meeting, retitled by proxy.
        """
        needle = str(text).strip().lower()
        if not needle:
            return []
        rows = self.series(limit=max(limit * 4, limit), allow_local_only=allow_local_only)
        return [event for event in rows if needle in event.summary.lower()][:limit]

    def bulk_update(self, match: str, *, at: str = "", **changes: Any) -> list[Event]:
        """Change every series whose summary matches. Returns what changed.

        ``at`` is a clock (``"10:00"``) applied to each event's **own** date. That
        is what "move all my standups to 10" actually means; handing the same
        full ``start`` to all of them would collapse a week of standups onto one
        day. The duration is preserved.

        Events are edited one at a time, so the file is re-read and rewritten per
        match. On a personal calendar that is the right trade — correctness and a
        legible file over one clever write.
        """
        wanted = _wanted(changes)
        clock = str(at or "").strip()
        if not wanted and not clock:
            raise ValueError("nothing to change — give `at` (a time) or a field to set")
        changed: list[Event] = []
        for event in self.matching(match):
            per_event = dict(wanted)
            if clock:
                moved = _retimed(event, clock)
                if not moved:
                    # An all-day event has no hour to move, and quietly turning
                    # one into a timed event is not a small edit.
                    continue
                per_event.update(moved)
            if not per_event:
                continue
            updated = self.update(event.uid, **per_event)
            if updated is not None:
                changed.append(updated)
        return changed

    def bulk_delete(self, match: str) -> int:
        """Remove every series whose summary matches. Returns how many went."""
        removed = 0
        for event in self.matching(match):
            if self.delete(event.uid):
                removed += 1
        return removed

    # -- reads -------------------------------------------------------------

    def get(self, uid: str) -> Event | None:
        """The series (or one-off) itself. Overrides are :meth:`overrides`."""
        self._sync()
        return self.index.get(uid)

    def overrides(self, uid: str) -> list[Event]:
        self._sync()
        return self.index.overrides(uid)

    def between(
        self,
        start: str,
        end: str,
        *,
        limit: int = 50,
        allow_local_only: bool = True,
    ) -> list[Event]:
        return self.occurrences(
            parse_iso(start),
            parse_iso(end),
            limit=limit,
            allow_local_only=allow_local_only,
        )

    def occurrences(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        limit: int = 50,
        allow_local_only: bool = True,
    ) -> list[Event]:
        """The dated events in a window — series expanded, overrides applied."""
        self._sync()
        rows = self.index.candidates(
            window_start.strftime("%Y-%m-%dT%H:%M"),
            window_end.strftime("%Y-%m-%dT%H:%M"),
        )
        expanded = expand_occurrences(
            rows, window_start, window_end, limit=max(limit * 4, 200)
        )
        return _visible(expanded, limit, allow_local_only)

    def agenda(
        self,
        *,
        days: int = 7,
        limit: int = 10,
        allow_local_only: bool = True,
        now: datetime | None = None,
    ) -> list[Event]:
        """What is coming, from today. Bounds are dates, so a day counts in full."""
        moment = now or datetime.now()
        return self.occurrences(
            datetime.combine(moment.date(), datetime.min.time()),
            datetime.combine(moment.date(), datetime.min.time()) + timedelta(days=days),
            limit=limit,
            allow_local_only=allow_local_only,
        )

    def series(self, *, limit: int = 100, allow_local_only: bool = True) -> list[Event]:
        """The definitions themselves — one row per event or series."""
        self._sync()
        rows = [e for e in self.index.all(limit=max(limit * 4, limit)) if not e.recurrence_id]
        return _visible(rows, limit, allow_local_only)

    def all(self, *, limit: int = 100, allow_local_only: bool = True) -> list[Event]:
        return self.series(limit=limit, allow_local_only=allow_local_only)

    def count(self) -> int:
        self._sync()
        return self.index.count()

    def search(
        self, query: str, *, limit: int = 20, allow_local_only: bool = True
    ) -> list[Event]:
        """Definitions whose text matches — "when did I last meet Ana"."""
        self._sync()
        rows = self.index.search(query, limit=max(limit * 4, limit))
        return _visible(rows, limit, allow_local_only)

    # -- reminders ---------------------------------------------------------

    def due_reminders(
        self,
        since: datetime,
        now: datetime,
        *,
        allow_local_only: bool = True,
    ) -> list[tuple[Event, Reminder, datetime]]:
        """``(event, reminder, when it should have fired)`` for ``(since, now]``.

        A reminder is relative to the *start*, so an alarm that leads a meeting
        is already due while the meeting itself is still ahead: the window
        reaches ``max_lead`` into the future to catch those, and a day back to
        catch an event that began just before the tick.

        ``local_only`` events are **included**: a reminder goes to the owner,
        over the surface they chose, with no model in the loop — so the gate
        that keeps those values out of a *prompt* has nothing to do here.
        """
        self._sync()
        lead = self._max_lead()
        rows = self.occurrences(
            since - timedelta(days=1),
            now + lead,
            limit=1000,
            allow_local_only=allow_local_only,
        )
        found: list[tuple[Event, Reminder, datetime]] = []
        for event in rows:
            start = zones.local_wall(event.span()[0])
            for reminder in event.reminders:
                fire_at = start - timedelta(minutes=reminder.offset_minutes)
                if since < fire_at <= now:
                    found.append((event, reminder, fire_at))
        return sorted(found, key=lambda item: item[2])

    def _max_lead(self) -> timedelta:
        """The longest reminder lead in the store, capped at a week."""
        top = 0
        for event in self.index.all(limit=5000):
            for reminder in event.reminders:
                top = max(top, reminder.offset_minutes)
        return timedelta(minutes=min(max(top, 0), 7 * 24 * 60))

    # -- time the owner has, and time they do not --------------------------

    def busy(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        allow_local_only: bool = True,
    ) -> list[tuple[datetime, datetime]]:
        """Merged blocks the owner is *not* free in, as local wall time.

        Merged, not listed: "when am I free" is a question about the shape of a
        day, and five overlapping events are one occupied stretch. Events marked
        ``TRANSP:TRANSPARENT`` are left out — on the calendar, not blocking it.
        """
        rows = self.occurrences(
            window_start, window_end, limit=1000, allow_local_only=allow_local_only
        )
        return merge_blocks([e.span() for e in rows if e.occupies()])

    def conflicts(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        allow_local_only: bool = True,
        limit: int = 200,
    ) -> list[tuple[Event, Event]]:
        """Pairs of events that claim the same stretch of time."""
        rows = [
            e
            for e in self.occurrences(
                window_start, window_end, limit=limit, allow_local_only=allow_local_only
            )
            if e.occupies()
        ]
        found: list[tuple[Event, Event]] = []
        for position, first in enumerate(rows):
            first_start, first_end = first.span()
            for second in rows[position + 1 :]:
                second_start, second_end = second.span()
                if second_start < first_end and second_end > first_start:
                    found.append((first, second))
        return found

    def free(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        minutes: int = 45,
        hours: tuple[str, str] = ("09:00", "18:00"),
        allow_local_only: bool = True,
        limit: int = 20,
    ) -> list[tuple[datetime, datetime]]:
        """Gaps of at least ``minutes`` inside each day's ``hours``.

        Bounded to waking hours on purpose: a schedule that happily books 03:00
        is technically correct and practically useless. Gaps are reported as
        naive local wall time, which is the frame the owner reads a day in.
        """
        needed = timedelta(minutes=max(1, int(minutes)))
        opens, closes = _clock(hours[0]), _clock(hours[1])
        if closes <= opens:
            opens, closes = _clock("09:00"), _clock("18:00")

        found: list[tuple[datetime, datetime]] = []
        day = window_start.date()
        last = window_end.date()
        while day <= last and len(found) < limit:
            day_open = datetime.combine(day, opens)
            day_close = datetime.combine(day, closes)
            lower = max(day_open, window_start)
            upper = min(day_close, window_end)
            if upper > lower:
                blocked = self.busy(lower, upper, allow_local_only=allow_local_only)
                found.extend(
                    (gap_start, gap_end)
                    for gap_start, gap_end in gaps_within(lower, upper, blocked)
                    if gap_end - gap_start >= needed
                )
            day += timedelta(days=1)
        return found[:limit]

    def inventory(self, limit: int = 6, *, days: int = 7, allow_local_only: bool = True) -> str:
        rows = self.agenda(days=days, limit=limit, allow_local_only=allow_local_only)
        if not rows:
            return "(nothing scheduled in the next 7 days)"
        return "\n".join(f"- {event.line()}" for event in rows)

    def sync_state(self) -> dict[str, Any]:
        """The gcal cursor/token slot (SPEC §5.5, ARCHITECTURE §8.5).

        Empty until gcal is wired: the internal calendar is authoritative and
        needs no cursor, so nothing writes this yet. The file is the seam.
        """
        if not self.sync_path.exists():
            return {"calendar": INTERNAL, "cursor": None, "last_sync": None}
        return json.loads(self.sync_path.read_text(encoding="utf-8"))


# -- helpers ----------------------------------------------------------------


def _clock(value: str) -> datetime.time:  # type: ignore[name-defined]
    """``"09:30"`` as a clock time; unreadable falls back to nine and six."""
    bits = str(value or "").strip().split(":")
    if len(bits) == 2:
        try:
            hours, minutes = int(bits[0]), int(bits[1])
            if 0 <= hours < 24 and 0 <= minutes < 60:
                return datetime(2000, 1, 1, hours, minutes).time()
        except ValueError:
            pass
    return datetime(2000, 1, 1, 9, 0).time()


def merge_blocks(
    intervals: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Overlapping intervals collapsed into the stretches they cover.

    Everything is compared as wall time on this laptop, so an event from another
    zone merges against a local one at the right moment rather than at the same
    digits on a clock.
    """
    normalised: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        local_start, local_end = zones.local_wall(start), zones.local_wall(end)
        if local_end > local_start:
            normalised.append((local_start, local_end))
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(normalised, key=lambda pair: pair[0]):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def gaps_within(
    start: datetime, end: datetime, blocked: Iterable[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """``[start, end)`` minus what is blocked — the free time inside it."""
    found: list[tuple[datetime, datetime]] = []
    cursor = start
    for block_start, block_end in merge_blocks(blocked):
        if block_start >= end:
            break
        if block_start > cursor:
            found.append((cursor, min(block_start, end)))
        cursor = max(cursor, block_end)
        if cursor >= end:
            break
    if cursor < end:
        found.append((cursor, end))
    return found


def _visible(rows: list[Event], limit: int, allow_local_only: bool) -> list[Event]:
    if not allow_local_only:
        rows = [r for r in rows if r.data_class is not DataClass.LOCAL_ONLY]
    return rows[:limit]


def _find(events: list[Event], uid: str, recurrence_id: str) -> Event | None:
    return next(
        (e for e in events if e.uid == uid and e.recurrence_id == recurrence_id), None
    )


def _reminders(values: Iterable[Any]) -> list[Reminder]:
    out: list[Reminder] = []
    for value in values:
        if isinstance(value, Reminder):
            out.append(value)
        elif isinstance(value, int):
            out.append(Reminder(value))
        elif isinstance(value, dict):
            out.append(Reminder.from_dict(value))
    return out


def _wanted(changes: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in changes.items() if value not in (None, "", [])}


def _apply(event: Event, wanted: dict[str, Any]) -> None:
    if "reminders" in wanted:
        event.reminders = _reminders(wanted["reminders"])
    for key, value in wanted.items():
        if key == "reminders":
            continue
        if key == "start":
            event.start, event.all_day = normalise_when(str(value))
        elif key == "end":
            event.end = normalise_when(str(value))[0] if value else ""
        elif key == "data_class":
            event.data_class = DataClass(str(value))
        elif key == "rrule":
            rule = str(value).strip()
            if rule:
                recur.parse_rule(rule)
            event.rrule = rule
        elif key == "exdates":
            event.exdates = [_key_from(value, event) for value in value]
        elif key == "summary":
            event.summary = str(value).strip()
        elif key in ("location", "description", "calendar", "authority", "tzid"):
            setattr(event, key, str(value))
        elif key == "transparency":
            event.transparency = str(value)


def _key_from(value: Any, event: Event) -> str:
    iso, _all_day = normalise_when(str(value))
    return iso


def _key_for(master: Event, occurrence: str) -> str:
    """An occurrence start in the same shape the series stores its own times."""
    iso, _all_day = normalise_when(occurrence)
    if master.all_day:
        return iso[:10]
    return iso


def _offset_for(master: Event, key: str) -> timedelta:
    start = parse_iso(master.start)
    moment = parse_iso(key)
    return moment - start


def _copy_span(master: Event, into: Event, *, counter: timedelta) -> None:
    if master.end:
        into.end = _shift(master.end, counter, master.all_day)


def _shift(value: str, delta: timedelta, all_day: bool) -> str:
    moment = parse_iso(value) + delta
    return moment.strftime("%Y-%m-%d" if all_day else "%Y-%m-%dT%H:%M")


def _retimed(event: Event, clock: str) -> dict[str, str]:
    """The event's own date, at ``clock`` — the ``start``/``end`` to set.

    Empty when there is nothing sensible to move: an all-day event has no hour
    to change, and a clock that cannot be read is not an instruction.
    """
    if event.all_day:
        return {}
    bits = str(clock).strip().split(":")
    if len(bits) != 2:
        return {}
    try:
        hours, minutes = int(bits[0]), int(bits[1])
    except ValueError:
        return {}
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return {}
    start = parse_iso(event.start)
    moved = start.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    changes = {"start": moved.strftime("%Y-%m-%dT%H:%M")}
    if event.end:
        # Keep the length the owner chose, not the length the old digits implied.
        changes["end"] = (moved + (parse_iso(event.end) - start)).strftime("%Y-%m-%dT%H:%M")
    return changes


def _truncate(master: Event, key: str) -> str:
    """End the master's rule just before ``key``; return the tail's rule."""
    moment = parse_iso(key) - timedelta(minutes=1)
    master.rrule = _with_until(master, moment)
    return _without_until(master.rrule)


def _with_until(master: Event, moment: datetime) -> str:
    stamp = moment.strftime("%Y%m%d" if master.all_day else "%Y%m%dT%H%M%S")
    parts = [
        chunk
        for chunk in master.rrule.split(";")
        if chunk and not chunk.upper().startswith(("UNTIL=", "COUNT="))
    ]
    parts.append(f"UNTIL={stamp}")
    return ";".join(parts)


def _without_until(rule: str) -> str:
    return ";".join(
        chunk
        for chunk in rule.split(";")
        if chunk and not chunk.upper().startswith(("UNTIL=", "COUNT="))
    )
