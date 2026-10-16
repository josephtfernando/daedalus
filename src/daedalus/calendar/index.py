"""SQLite index over the calendar's ``.ics`` — a derived cache (SPEC §5.5).

Range reads want an index, so this exists. But the ``.ics`` stays the truth:
the table records the ``.ics`` mtime it was built from, so a hand edit or an
external sync is noticed before the next read rather than shadowed by a stale
cache. Rebuild it at any time with :meth:`CalendarStore.rebuild_index`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..security.provenance import DataClass
from .event import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
    uid TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL DEFAULT '',
    all_day INTEGER NOT NULL DEFAULT 0,
    location TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    calendar TEXT NOT NULL DEFAULT 'Daedalus',
    authority TEXT NOT NULL DEFAULT 'internal',
    data_class TEXT NOT NULL DEFAULT 'private',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    extra TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS events_start ON events(start);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_INSERT = (
    "INSERT OR REPLACE INTO events (uid, summary, start, end, all_day, location,"
    " description, calendar, authority, data_class, created_at, updated_at, extra)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


class CalendarIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # -- the derived cache -------------------------------------------------

    def replace_all(self, events: list[Event], *, source_revision: int) -> None:
        """Rewrite the table, and record which ``.ics`` revision it reflects."""
        with self._conn() as conn:
            conn.execute("DELETE FROM events")
            conn.executemany(_INSERT, [_row(e) for e in events])
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('source_revision', ?)",
                (str(int(source_revision)),),
            )

    def source_revision(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'source_revision'").fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    # -- reads -------------------------------------------------------------

    def get(self, uid: str) -> Event | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM events WHERE uid = ?", (uid,)).fetchone()
        return _event(row) if row else None

    def between(self, start: str, end: str, *, limit: int = 50) -> list[Event]:
        """Events beginning in ``[start, end)``. ISO strings sort as time does."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE start >= ? AND start < ? ORDER BY start LIMIT ?",
                (start, end, limit),
            )
            return [_event(r) for r in rows]

    def all(self, *, limit: int = 100) -> list[Event]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY start LIMIT ?", (limit,))
            return [_event(r) for r in rows]

    def count(self) -> int:
        with self._conn() as conn:
            (total,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(total)


def _row(event: Event) -> tuple:
    return (
        event.uid,
        event.summary,
        event.start,
        event.end,
        1 if event.all_day else 0,
        event.location,
        event.description,
        event.calendar,
        event.authority,
        event.data_class.value,
        event.created_at,
        event.updated_at,
        json.dumps(event.extra),
    )


def _event(row: sqlite3.Row) -> Event:
    try:
        data_class = DataClass(row["data_class"])
    except ValueError:
        data_class = DataClass.PRIVATE
    try:
        extra = [tuple(pair) for pair in json.loads(row["extra"] or "[]")]
    except (TypeError, ValueError):
        extra = []
    return Event(
        uid=row["uid"],
        summary=row["summary"],
        start=row["start"],
        end=row["end"],
        all_day=bool(row["all_day"]),
        location=row["location"],
        description=row["description"],
        calendar=row["calendar"],
        authority=row["authority"],
        data_class=data_class,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        extra=extra,
    )
