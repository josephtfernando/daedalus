"""The calendar event model, and how a time is read (SPEC §5.5).

Times are **floating local**: the owner's laptop is the only clock in the
system, and "9:10" means 9:10 here — not 9:10 in some calendar server's zone.
An event's shape decides whether it is all-day (``2026-10-16`` is a date,
``2026-10-16T09:10`` is a moment), so the flag can never contradict the time.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..security.provenance import DataClass
from . import recur, zones

#: The one calendar the agent owns and writes (SPEC §5.5). Every other
#: calendar the owner has is gcal-authoritative and, from here, read-only.
INTERNAL = "Daedalus"

#: The name given to events adopted from a file that did not say where they came
#: from — better an honest "Imported" than a false claim to be the agent's own.
IMPORTED = "Imported"

#: A timed event with no end is assumed to last this long. gcal defaults an hour
#: too, and "lunch at noon" meaning a zero-length blip would make every
#: free/busy answer wrong.
DEFAULT_TIMED_MINUTES = 60
#: An all-day event with no end covers its day.
DEFAULT_ALL_DAY_DAYS = 1

#: ``TRANSP``: whether an event consumes the time it occupies. An event marked
#: ``transparent`` is on the calendar but does not make the owner busy — gcal's
#: "Free" events, and the difference between "you have a thing" and "you are
#: unavailable".
OPAQUE = "opaque"
TRANSPARENT = "transparent"


def now_iso() -> str:
    """The current local minute, in the same shape an event stores."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M")


def parse_iso(value: str) -> datetime:
    """An event time as a datetime; a date-only value is midnight."""
    text = value.strip()
    if len(text) == 10:
        return datetime.combine(date.fromisoformat(text), datetime.min.time())
    return datetime.fromisoformat(text)


def normalise_when(value: str) -> tuple[str, bool]:
    """``(iso, all_day)`` for a time the model or the owner gave us.

    Accepts ``2026-10-16`` (all-day), ``2026-10-16T09:10``, and the same with a
    space in place of the ``T``. Seconds are dropped: a calendar is not a
    stopwatch. Anything else raises ``ValueError``, so a misreadable time is
    reported rather than silently filed at the wrong hour.
    """
    text = str(value).strip().replace(" ", "T")
    if not text:
        raise ValueError("no time given")
    try:
        if len(text) == 10:
            date.fromisoformat(text)  # raises if it is not a date
            return text, True
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"{value!r} is not a time I can read — use '2026-10-16T09:10', or "
            "'2026-10-16' for a whole day."
        ) from exc
    return moment.strftime("%Y-%m-%dT%H:%M"), False


@dataclass(slots=True, frozen=True)
class Reminder:
    """A ``VALARM``: a nudge this many minutes *before* the event starts.

    RFC 5545 writes the trigger as an offset from the start, usually negative
    (``TRIGGER:-PT10M``). A person thinks "10 minutes before", so that is what
    is stored: ``offset_minutes`` is positive when the reminder leads the event,
    and ``0`` means "as it starts". A calendar a person writes has no use for a
    reminder that fires *after* the thing it is about.
    """

    offset_minutes: int
    action: str = "DISPLAY"
    description: str = ""
    #: ``VALARM`` lines this module does not model (``REPEAT``, ``DURATION``,
    #: ``ATTACH``…), kept verbatim so a reminder gcal wrote is not stripped.
    extra: tuple[tuple[str, str], ...] = ()

    def lead_text(self) -> str:
        if self.offset_minutes <= 0:
            return "at the start"
        hours, minutes = divmod(self.offset_minutes, 60)
        if hours and minutes:
            return f"{hours}h {minutes}m before"
        if hours:
            return f"{hours}h before"
        return f"{minutes}m before"

    def to_dict(self) -> dict[str, Any]:
        return {
            "offset": self.offset_minutes,
            "action": self.action,
            "description": self.description,
            "extra": [list(pair) for pair in self.extra],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Reminder":
        if not isinstance(data, dict):
            return cls(0)
        try:
            offset = int(data.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        raw_extra = data.get("extra") or []
        extra = tuple(
            (str(pair[0]), str(pair[1]))
            for pair in raw_extra
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        )
        return cls(
            offset,
            action=str(data.get("action") or "DISPLAY"),
            description=str(data.get("description") or ""),
            extra=extra,
        )


@dataclass(slots=True)
class Event:
    """One VEVENT. ``uid`` is the identity; every other field is editable."""

    summary: str
    start: str
    uid: str = ""
    end: str = ""
    all_day: bool = False
    location: str = ""
    description: str = ""
    calendar: str = INTERNAL
    authority: str = "internal"
    data_class: DataClass = DataClass.PRIVATE
    created_at: float = 0.0
    updated_at: float = 0.0
    #: ``RRULE`` — the raw value, e.g. ``FREQ=WEEKLY;BYDAY=MO,WE,FR``. Empty for
    #: a one-off. Kept verbatim so the file says what the owner wrote.
    rrule: str = ""
    #: ``EXDATE`` — occurrence starts to leave out, in the shape times are stored.
    exdates: list[str] = field(default_factory=list)
    #: ``RDATE`` — extra occurrence starts the rule does not produce. The
    #: recurrence set is RRULE ∪ RDATE, minus EXDATE (RFC 5545 §3.8.5), which is
    #: also what makes "drop that one" work on an added date.
    rdates: list[str] = field(default_factory=list)
    #: ``RECURRENCE-ID`` — set on an event that replaces a single occurrence of
    #: the series sharing its ``uid``. Empty on the series itself.
    recurrence_id: str = ""
    #: ``RECURRENCE-ID;RANGE=`` — ``""`` or ``THISANDFUTURE``.
    recurrence_range: str = ""
    #: ``TZID`` — an IANA zone for ``start``/``end``. Empty means floating local:
    #: "09:00 here", the honest default for one laptop.
    tzid: str = ""
    #: ``TRANSP`` — ``opaque`` (busy, the default) or ``transparent`` (free).
    transparency: str = OPAQUE
    #: ``VALARM`` blocks, as offsets from the start (see :class:`Reminder`).
    reminders: list["Reminder"] = field(default_factory=list)
    #: Content lines this module does not own (``CATEGORIES``, ``X-*``, …), kept
    #: verbatim and in order so a hand-edited event survives a tool write.
    extra: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.uid:
            self.uid = uuid.uuid4().hex[:12]
        stamp = time.time()
        if not self.created_at:
            self.created_at = stamp
        if not self.updated_at:
            self.updated_at = self.created_at

    # -- shape -------------------------------------------------------------

    @property
    def recurring(self) -> bool:
        return bool(self.rrule)

    def span(self) -> tuple[datetime, datetime]:
        """``(start, end)`` as real instants, with defaults filled in.

        Aware where a zone is known, so two events on different clocks compare as
        moments rather than as strings. ``end`` is exclusive. Adding a duration
        keeps the *wall* time (a 09:00–10:00 meeting does not become 08:00–09:00
        across a clock change), because ``ZoneInfo`` recomputes its offset.
        """
        start = zones.aware(parse_iso(self.start), self.tzid)
        if self.end:
            end = zones.aware(parse_iso(self.end), self.tzid)
        elif self.all_day:
            end = start + timedelta(days=DEFAULT_ALL_DAY_DAYS)
        else:
            end = start + timedelta(minutes=DEFAULT_TIMED_MINUTES)
        if end <= start:
            # An end before its start is a typo, not a negative meeting.
            end = start + (
                timedelta(days=DEFAULT_ALL_DAY_DAYS)
                if self.all_day
                else timedelta(minutes=DEFAULT_TIMED_MINUTES)
            )
        return start, end

    def occupies(self) -> bool:
        """Whether this event makes the owner busy (``TRANSP``)."""
        return self.transparency != TRANSPARENT

    @property
    def address(self) -> str:
        """How the owner's agent names this event: a series, or one instance.

        A series is addressed by its ``uid``. One instance of it is
        ``uid@start``, which is what the model passes back to move or drop just
        that instance — the difference between "the standup moved" and "today's
        standup moved".
        """
        return f"{self.uid}@{self.recurrence_id}" if self.recurrence_id else self.uid

    def rule_text(self) -> str:
        """The ``RRULE`` in words, or the raw value if it is unreadable."""
        if not self.rrule:
            return ""
        try:
            return recur.describe(recur.parse_rule(self.rrule))
        except ValueError:
            return self.rrule

    # -- rendering --------------------------------------------------------

    def zone_label(self) -> str:
        """A short zone name, shown only when it is not the owner's own clock."""
        if not self.tzid or zones.is_local(self.tzid):
            return ""
        return f" {self.tzid.split('/')[-1].replace('_', ' ')}"

    def when(self) -> str:
        """A human time, e.g. ``Fri 16 Oct 09:10–09:30``, or a day span.

        Shown on the *event's* clock — "09:00 London" — because that is when the
        thing happens from the owner's side; the label appears only when it is
        not the laptop's own zone, so an ordinary local day stays uncluttered.
        """
        moment = parse_iso(self.start)
        label = self.zone_label()
        if self.all_day:
            first = moment.strftime("%a %d %b")
            if self.end and parse_iso(self.end) > moment + timedelta(days=1):
                last = parse_iso(self.end) - timedelta(days=1)
                return f"{first} – {last.strftime('%a %d %b')}{label} (all day)"
            return f"{first}{label} (all day)"
        text = moment.strftime("%a %d %b %H:%M")
        if self.end:
            text += "–" + parse_iso(self.end).strftime("%H:%M")
        return text + label

    def line(self, *, show_id: bool = False, show_rule: bool = True) -> str:
        parts = [self.when(), self.summary]
        if self.location:
            parts.append(f"@ {self.location}")
        if self.rrule and show_rule:
            parts.append(f"↻ {self.rule_text()}")
        if self.reminders:
            parts.append(f"⏰ {self.reminders[0].lead_text()}")
        text = " ".join(parts)
        return f"[{self.address}] {text}" if show_id else text


def overlaps(event: Event, window_start: datetime, window_end: datetime) -> bool:
    """Whether ``event``'s span touches ``[window_start, window_end)``.

    Overlap, not containment: a meeting that started yesterday and runs into
    today is on today, and a store that filtered on the start alone would drop
    it and tell the owner their day is clear. Both sides are compared as wall
    time on this laptop, so an event from another zone lands on the right day.
    """
    start, end = event.span()
    return zones.local_wall(start) < zones.local_wall(window_end) and zones.local_wall(
        end
    ) > zones.local_wall(window_start)
