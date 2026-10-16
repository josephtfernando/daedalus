"""The internal calendar store (SPEC §5.5, ARCHITECTURE §8.5).

``$DAEDALUS_HOME/calendar/events.ics`` is the calendar. This class is the only
thing that writes it, and it does so by **re-reading first**: every mutation
loads the current file, changes it, and writes the whole document back. That is
what makes the store genuinely file-first — an event the owner added by hand, a
property this module does not model, a ``VTIMEZONE`` block, all survive a tool
write instead of being flattened by a private copy of the truth.

The SQLite index beside it exists for range reads and can be rebuilt from the
``.ics`` at any moment (:meth:`rebuild_index`).

Only the ``Daedalus`` calendar is writable here. The owner's other calendars
are gcal's, and gcal is not wired yet — see :meth:`sync_state`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..security.provenance import DataClass
from . import ical
from .event import INTERNAL, Event, normalise_when
from .index import CalendarIndex


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
        ordered = sorted(parsed.events, key=lambda e: (e.start, e.summary.lower()))
        self.ics_path.write_text(
            ical.render_calendar(ordered, parsed.passthrough), encoding="utf-8"
        )
        self.index.replace_all(ordered, source_revision=self._revision())

    # -- writes ------------------------------------------------------------

    def add(self, *, summary: str, start: str, end: str = "", **fields: Any) -> Event:
        """Add an event. The shape of ``start`` decides whether it is all-day."""
        text = str(summary).strip()
        if not text:
            raise ValueError("an event needs a summary")
        start_iso, all_day = normalise_when(start)
        parsed = self._load()
        event = Event(
            summary=text,
            start=start_iso,
            end=normalise_when(end)[0] if end else "",
            all_day=all_day,
            **fields,
        )
        parsed.events.append(event)
        self._write(parsed)
        return event

    def update(self, uid: str, **changes: Any) -> Event | None:
        """Change an event's fields. ``None`` means there is no such event.

        Only the keys actually given are touched, so a move does not blank the
        description. Empty values are ignored rather than treated as erasures.
        """
        wanted = {k: v for k, v in changes.items() if v not in (None, "")}
        if not wanted:
            raise ValueError("nothing to change")
        parsed = self._load()
        event = next((e for e in parsed.events if e.uid == uid), None)
        if event is None:
            return None
        for key, value in wanted.items():
            if key == "start":
                event.start, event.all_day = normalise_when(str(value))
            elif key == "end":
                event.end = normalise_when(str(value))[0]
            elif key == "data_class":
                event.data_class = DataClass(str(value))
            elif key == "summary":
                event.summary = str(value).strip()
            elif key in ("location", "description", "calendar", "authority"):
                setattr(event, key, str(value))
        event.updated_at = time.time()
        self._write(parsed)
        return event

    def delete(self, uid: str) -> bool:
        parsed = self._load()
        keep = [e for e in parsed.events if e.uid != uid]
        if len(keep) == len(parsed.events):
            return False
        parsed.events = keep
        self._write(parsed)
        return True

    # -- reads -------------------------------------------------------------

    def get(self, uid: str) -> Event | None:
        self._sync()
        return self.index.get(uid)

    def between(
        self,
        start: str,
        end: str,
        *,
        limit: int = 50,
        allow_local_only: bool = True,
    ) -> list[Event]:
        self._sync()
        rows = self.index.between(start, end, limit=max(limit * 4, limit))
        return _visible(rows, limit, allow_local_only)

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
        return self.between(
            moment.strftime("%Y-%m-%d"),
            (moment + timedelta(days=days)).strftime("%Y-%m-%d"),
            limit=limit,
            allow_local_only=allow_local_only,
        )

    def all(self, *, limit: int = 100, allow_local_only: bool = True) -> list[Event]:
        self._sync()
        rows = self.index.all(limit=max(limit * 4, limit))
        return _visible(rows, limit, allow_local_only)

    def count(self) -> int:
        self._sync()
        return self.index.count()

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


def _visible(rows: list[Event], limit: int, allow_local_only: bool) -> list[Event]:
    if not allow_local_only:
        rows = [r for r in rows if r.data_class is not DataClass.LOCAL_ONLY]
    return rows[:limit]
