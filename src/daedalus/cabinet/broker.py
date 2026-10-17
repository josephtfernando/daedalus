"""The durable queue between seats (ARCHITECTURE §7.5, §9.2).

A seat is not a process, so there is no socket to lose a message on — and no
in-memory list to keep one in either. ``cabinet/messages.db`` is the whole
transport: one table, one row per message, every guarantee stated in terms a
restart can keep.

**At-least-once, and honest about it.** A message is *offered* until it is
*resolved*. :meth:`Broker.inbox` hands a seat everything addressed to it that
nobody has answered, oldest first, and counts the offer — so a message that
arrives with ``attempts=2`` says plainly that someone held it before and never
finished. The broker cannot know whether a seat got to the end before it died;
pretending it can is the exactly-once lie §9.2 rules out. What it *can* do is make
the repeat visible, and make every side effect dedupable, by giving each message
a stable id.

**Dedup, in the two places it is decidable.** A retry is the same message when it
carries the same id — and the sender chose that id before sending, which is what
`protocol.py` is for — so the second write stores nothing new and reports
``duplicate=True`` without moving the row's place in the queue. An answer is the
same answer when it resolves a message that is already resolved, so the second
resolve reports ``False``. Neither needs a seat to remember anything.

Ordering is the row's own ``seq``, assigned by sqlite at insert. A retry cannot
jump the queue, and :meth:`thread` reads an exchange back the way it happened —
which is what correlation is *for*; a correlation id nobody queries is a field,
not a link.

The journal mode is deliberately left alone. This is a queue on one laptop with
one writing process: sqlite's default rollback journal with ``synchronous=FULL``
is the most crash-safe arrangement it offers, and the volume is a handful of rows
a day. WAL buys concurrent readers and writers a speed they do not need here.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import Message, MessageError, seat_key

__all__ = ["Broker", "Delivery", "Receipt", "UnknownMessage", "broker_for", "db_path"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seat_messages (
  seq            INTEGER PRIMARY KEY AUTOINCREMENT,
  id             TEXT    NOT NULL UNIQUE,
  correlation_id TEXT    NOT NULL,
  causation_id   TEXT    NOT NULL DEFAULT '',
  sender         TEXT    NOT NULL,
  recipient      TEXT    NOT NULL,
  type           TEXT    NOT NULL,
  payload        TEXT    NOT NULL DEFAULT '{}',
  created_at     REAL    NOT NULL,
  attempts       INTEGER NOT NULL DEFAULT 0,
  offered_at     REAL,
  resolved_at    REAL,
  outcome        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS seat_messages_inbox ON seat_messages (recipient, resolved_at, seq);
CREATE INDEX IF NOT EXISTS seat_messages_thread ON seat_messages (correlation_id, seq);
"""


class UnknownMessage(KeyError):
    """A message id nobody sent — refused, because "no" would read as "already done"."""


@dataclass(frozen=True, slots=True)
class Receipt:
    """What :meth:`Broker.send` did: the message as stored, and whether it was new."""

    message: Message
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class Delivery:
    """A message offered to a seat, and how many times it has been through one."""

    message: Message
    #: 1 the first time. More than 1 means a seat was handed this before — it was
    #: offered and never resolved, or its answer was put back — so whatever the
    #: earlier attempt did may be half-done. This is the only honesty the broker
    #: has about exactly-once, and it is deliberate that it is visible.
    attempts: int = 1
    seq: int = 0


def db_path(home: Path) -> Path:
    """Where seat messages live: ``<home>/cabinet/messages.db`` (ARCHITECTURE §8.5)."""
    return Path(home) / "cabinet" / "messages.db"


def broker_for(home: Path) -> Broker:
    """The broker for one home.

    The one place that knows the path, so a turn, a tool and a CLI probe cannot
    each spell it slightly differently — the same reason `cabinet/namespace.py`
    owns where a seat's facts live.
    """
    return Broker(db_path(home))


class Broker:
    """One home's seat-to-seat queue."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """A connection and a transaction: committed on the way out, rolled back
        on a raise, closed either way. ``with sqlite3.connect(...)`` does the first
        two and leaves the third to the garbage collector, which is fine for a
        one-shot write and not for a queue that is read while it is written."""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- writing ----------------------------------------------------------

    def send(self, message: Message) -> Receipt:
        """Queue a message. A retry carrying the same id stores nothing new.

        ``duplicate=True`` is not an error and not a no-op to ignore: it says the
        message is *already in the queue*, which is what a sender wants to know
        before it sends a third time. The insert is a single statement with
        ``ON CONFLICT DO NOTHING``, so two processes racing the same id cannot
        both win.
        """
        with self._db() as c:
            cur = c.execute(
                "INSERT INTO seat_messages"
                " (id, correlation_id, causation_id, sender, recipient, type, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO NOTHING",
                (
                    message.id,
                    message.correlation,
                    message.causation_id,
                    message.sender,
                    message.recipient,
                    message.type,
                    json.dumps(message.payload, ensure_ascii=False),
                    message.created_at,
                ),
            )
            if cur.rowcount:
                return Receipt(message=message)
            row = c.execute("SELECT * FROM seat_messages WHERE id = ?", (message.id,)).fetchone()
        return Receipt(message=self._message(row), duplicate=True)

    def inbox(self, seat: str, *, limit: int | None = None) -> list[Delivery]:
        """Offer a seat the messages it has not answered yet, oldest first.

        Offering is an attempt: this is where at-least-once becomes something a
        seat can *see*. A seat reads its inbox once when its turn begins, so a
        message that comes back with a higher count has been through an earlier
        turn that did not finish.
        """
        who = seat_key(seat, field_name="seat")
        sql = (
            "SELECT * FROM seat_messages WHERE recipient = ? AND resolved_at IS NULL"
            " ORDER BY seq"
        )
        params: tuple[Any, ...] = (who,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (int(limit),)
        now = time.time()
        with self._db() as c:
            rows = c.execute(sql, params).fetchall()
            for row in rows:
                c.execute(
                    "UPDATE seat_messages SET attempts = attempts + 1, offered_at = ? WHERE seq = ?",
                    (now, row["seq"]),
                )
        return [
            Delivery(message=self._message(row), attempts=row["attempts"] + 1, seq=row["seq"])
            for row in rows
        ]

    def resolve(self, message_id: str, *, outcome: str = "ok", note: str = "") -> bool:
        """Mark a message answered. ``False`` means it already was.

        The second resolution is the *duplicate* of the first, and this is the one
        place the broker can see that. It says which call did the work, so a seat
        can tell its own replay from first contact — and it never rewrites an
        answer, because the first one is the one that happened.
        """
        record = f"{outcome}: {note}" if note else str(outcome)
        with self._db() as c:
            cur = c.execute(
                "UPDATE seat_messages SET resolved_at = ?, outcome = ?"
                " WHERE id = ? AND resolved_at IS NULL",
                (time.time(), record, str(message_id)),
            )
            if cur.rowcount:
                return True
            known = c.execute("SELECT 1 FROM seat_messages WHERE id = ?", (str(message_id),)).fetchone()
        if known is None:
            raise UnknownMessage(message_id)
        return False

    def reopen(self, message_id: str) -> bool:
        """Put a resolved message back in the seat's inbox. ``False`` = it was open.

        Not how a crash recovers — a message that was never resolved is offered
        again by itself. This is for what the broker cannot see: the answer was
        sent and it was wrong. Clearing the resolution puts the message back, and
        the attempt count moves with it, because a *rejected* answer was still an
        attempt: the seat that sees it next has to know it is not first contact.
        """
        with self._db() as c:
            cur = c.execute(
                "UPDATE seat_messages SET resolved_at = NULL, outcome = '', attempts = attempts + 1"
                " WHERE id = ? AND resolved_at IS NOT NULL",
                (str(message_id),),
            )
            if cur.rowcount:
                return True
            known = c.execute("SELECT 1 FROM seat_messages WHERE id = ?", (str(message_id),)).fetchone()
        if known is None:
            raise UnknownMessage(message_id)
        return False

    # -- reading ----------------------------------------------------------

    def get(self, message_id: str) -> Message | None:
        with self._db() as c:
            row = c.execute("SELECT * FROM seat_messages WHERE id = ?", (str(message_id),)).fetchone()
        return self._message(row) if row is not None else None

    def thread(self, correlation_id: str) -> list[Message]:
        """One exchange, in the order it happened.

        Any member of the exchange answers with the same list, because a message
        that opened one carries its own id as the correlation (see
        `Message.correlation`) — so `thread(any_message.id)` is a thread and not a
        lookup that happens to work for the first message only.
        """
        with self._db() as c:
            rows = c.execute(
                "SELECT * FROM seat_messages WHERE correlation_id = ? ORDER BY seq",
                (str(correlation_id),),
            ).fetchall()
        return [self._message(row) for row in rows]

    def pending(self, seat: str | None = None) -> int:
        """How many messages are unanswered — for one seat, or for the home."""
        sql = "SELECT COUNT(*) AS n FROM seat_messages WHERE resolved_at IS NULL"
        params: tuple[Any, ...] = ()
        if seat is not None:
            sql += " AND recipient = ?"
            params = (seat_key(seat, field_name="seat"),)
        with self._db() as c:
            row = c.execute(sql, params).fetchone()
        return int(row["n"])

    # -- rows --------------------------------------------------------------

    @staticmethod
    def _message(row: sqlite3.Row) -> Message:
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError as exc:
            raise MessageError(
                f"message {row['id']} has an unreadable payload in {row['payload']!r} — "
                "the row is corrupt, and guessing at its contents would be worse"
            ) from exc
        return Message(
            sender=row["sender"],
            recipient=row["recipient"],
            type=row["type"],
            payload=payload,
            id=row["id"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            created_at=row["created_at"],
        )
