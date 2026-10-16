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
from datetime import date, datetime

from ..security.provenance import DataClass

#: The one calendar the agent owns and writes (SPEC §5.5). Every other
#: calendar the owner has is gcal-authoritative and, from here, read-only.
INTERNAL = "Daedalus"


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
    #: Content lines this module does not own (``RRULE``, ``VALARM``, …), kept
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

    # -- rendering --------------------------------------------------------

    def when(self) -> str:
        """A human time, e.g. ``Fri 16 Oct 09:10–09:30``."""
        moment = parse_iso(self.start)
        if self.all_day:
            return moment.strftime("%a %d %b (all day)")
        text = moment.strftime("%a %d %b %H:%M")
        if self.end:
            text += "–" + parse_iso(self.end).strftime("%H:%M")
        return text

    def line(self, *, show_id: bool = False) -> str:
        parts = [self.when(), self.summary]
        if self.location:
            parts.append(f"@ {self.location}")
        text = " ".join(parts)
        return f"[{self.uid}] {text}" if show_id else text
