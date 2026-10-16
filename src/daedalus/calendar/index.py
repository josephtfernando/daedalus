"""SQLite index over the calendar's ``.ics`` — a derived cache (SPEC §5.5).

Range reads want an index, so this exists. But the ``.ics`` stays the truth:
the table records the ``.ics`` mtime it was built from, so a hand edit or an
external sync is noticed before the next read rather than shadowed by a stale
cache. Rebuild it at any time with :meth:`CalendarStore.rebuild_index`.

Two shapes are stored per event, on purpose. ``start``/``end`` are what the
event *says*; ``span_start``/``span_end`` are the same thing normalised to one
format with the duration defaults filled in. Queries compare the second pair —
comparing ``2026-10-16`` against ``2026-10-16T00:00`` as text would put an
all-day event on the wrong side of a boundary.

The key is ``(uid, recurrence_id)``, not ``uid``: RFC 5545 lets a moved
instance share its series' ``uid``, and a table keyed on ``uid`` alone would let
one occurrence of a recurring event evict the rest.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..security.provenance import DataClass
from . import zones
from .event import OPAQUE, Event, Reminder

#: Bumped when the table shape changes. The cache is derived, so a mismatch
#: means "rebuild", never "migrate".
SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
    uid TEXT NOT NULL,
    recurrence_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL DEFAULT '',
    span_start TEXT NOT NULL DEFAULT '',
    span_end TEXT NOT NULL DEFAULT '',
    all_day INTEGER NOT NULL DEFAULT 0,
    recurring INTEGER NOT NULL DEFAULT 0,
    location TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    calendar TEXT NOT NULL DEFAULT 'Daedalus',
    authority TEXT NOT NULL DEFAULT 'internal',
    data_class TEXT NOT NULL DEFAULT 'private',
    rrule TEXT NOT NULL DEFAULT '',
    exdates TEXT NOT NULL DEFAULT '[]',
    recurrence_range TEXT NOT NULL DEFAULT '',
    tzid TEXT NOT NULL DEFAULT '',
    transparency TEXT NOT NULL DEFAULT 'opaque',
    reminders TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    extra TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (uid, recurrence_id)
);
CREATE INDEX IF NOT EXISTS events_span ON events(span_start, span_end);
CREATE INDEX IF NOT EXISTS events_uid ON events(uid);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    uid UNINDEXED,
    recurrence_id UNINDEXED,
    summary,
    description,
    location
);
"""

_COLUMNS = (
    "uid, recurrence_id, summary, start, end, span_start, span_end, all_day,"
    " recurring, location, description, calendar, authority, data_class, rrule,"
    " exdates, recurrence_range, tzid, transparency, reminders, created_at,"
    " updated_at, extra"
)
_INSERT = f"INSERT OR REPLACE INTO events ({_COLUMNS}) VALUES ({','.join('?' * 23)})"


class CalendarIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            if self._version(conn) != SCHEMA_VERSION:
                # The table is a cache over the ``.ics``: rebuilding is always
                # cheaper and safer than migrating a shape nobody depends on.
                conn.executescript("DROP TABLE IF EXISTS events; DROP TABLE IF EXISTS search;")
                conn.executescript(SCHEMA)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                conn.executescript(SCHEMA)

    @staticmethod
    def _version(conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        except sqlite3.OperationalError:
            return 0
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    # -- the derived cache -------------------------------------------------

    def replace_all(self, events: list[Event], *, source_revision: int) -> None:
        """Rewrite the table, and record which ``.ics`` revision it reflects."""
        with self._conn() as conn:
            conn.execute("DELETE FROM events")
            conn.executemany(_INSERT, [_row(e) for e in events])
            conn.execute("DELETE FROM search")
            conn.executemany(
                "INSERT INTO search (uid, recurrence_id, summary, description, location)"
                " VALUES (?,?,?,?,?)",
                [
                    (e.uid, e.recurrence_id, e.summary, e.description, e.location)
                    for e in events
                    if not e.recurrence_id
                ],
            )
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
        """The series itself, not an override of it."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE uid = ? AND recurrence_id = ''", (uid,)
            ).fetchone()
        return _event(row) if row else None

    def overrides(self, uid: str) -> list[Event]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE uid = ? AND recurrence_id != ''"
                " ORDER BY recurrence_id",
                (uid,),
            )
            return [_event(r) for r in rows]

    def candidates(self, start: str, end: str, *, limit: int = 2000) -> list[Event]:
        """Every stored event that could contribute an occurrence to a window.

        A recurring series always qualifies: its own start may be years behind
        the window and it still produces today's standup. Every override
        qualifies too — an instance moved *out* of the window must still be
        found, or the day would keep showing a meeting that is no longer on it.
        Everything else is matched by overlap, which is why ``span_end`` exists.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events"
                " WHERE recurring = 1 OR recurrence_id != ''"
                " OR (span_start < ? AND span_end > ?)"
                " ORDER BY span_start LIMIT ?",
                (end, start, limit),
            )
            return [_event(r) for r in rows]

    def all(self, *, limit: int = 100) -> list[Event]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY span_start LIMIT ?", (limit,)
            )
            return [_event(r) for r in rows]

    def count(self) -> int:
        with self._conn() as conn:
            (total,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(total)

    # -- search ------------------------------------------------------------

    def search(self, query: str, *, limit: int = 20) -> list[Event]:
        """Definitions whose text matches, best first.

        Overrides are indexed under their series only: one hit should be one
        thing, not one thing and its moved instances.
        """
        rows = self._match(query, limit)
        if rows is None:
            rows = self._like(query, limit)
        return [_event(r) for r in rows]

    def _match(self, query: str, limit: int) -> list[sqlite3.Row] | None:
        text = _fts_query(query)
        if not text:
            return []
        try:
            with self._conn() as conn:
                return list(
                    conn.execute(
                        "SELECT e.* FROM search s JOIN events e"
                        " ON e.uid = s.uid AND e.recurrence_id = s.recurrence_id"
                        " WHERE search MATCH ? ORDER BY rank LIMIT ?",
                        (text, limit),
                    )
                )
        except sqlite3.OperationalError:
            # FTS5 unavailable or the query is not a query: fall back to LIKE
            # rather than reporting "nothing found" about something that exists.
            return None

    def _like(self, query: str, limit: int) -> list[sqlite3.Row]:
        needle = f"%{query}%"
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM events WHERE recurrence_id = '' AND"
                    " (summary LIKE ? OR description LIKE ? OR location LIKE ?)"
                    " ORDER BY span_start DESC LIMIT ?",
                    (needle, needle, needle, limit),
                )
            )


def _row(event: Event) -> tuple:
    start, end = event.span()
    return (
        event.uid,
        event.recurrence_id,
        event.summary,
        event.start,
        event.end,
        zones.local_wall(start).strftime("%Y-%m-%dT%H:%M"),
        zones.local_wall(end).strftime("%Y-%m-%dT%H:%M"),
        1 if event.all_day else 0,
        1 if event.rrule else 0,
        event.location,
        event.description,
        event.calendar,
        event.authority,
        event.data_class.value,
        event.rrule,
        json.dumps(event.exdates),
        event.recurrence_range,
        event.tzid,
        event.transparency,
        json.dumps([reminder.to_dict() for reminder in event.reminders]),
        event.created_at,
        event.updated_at,
        json.dumps(event.extra),
    )


def _event(row: sqlite3.Row) -> Event:
    try:
        data_class = DataClass(row["data_class"])
    except ValueError:
        data_class = DataClass.PRIVATE
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
        rrule=row["rrule"],
        exdates=_load_list(row["exdates"]),
        recurrence_id=row["recurrence_id"],
        recurrence_range=row["recurrence_range"],
        tzid=row["tzid"],
        transparency=row["transparency"] or OPAQUE,
        reminders=[Reminder.from_dict(item) for item in _load_json(row["reminders"])],
        extra=[tuple(pair) for pair in _load_json(row["extra"])],
    )


def _load_json(raw: str) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _load_list(raw: str) -> list[str]:
    return [str(item) for item in _load_json(raw)]


def _fts_query(query: str) -> str:
    """Free text as a safe FTS5 OR-query over its tokens."""
    tokens = [t for t in "".join(ch if ch.isalnum() else " " for ch in query).split() if t]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)
