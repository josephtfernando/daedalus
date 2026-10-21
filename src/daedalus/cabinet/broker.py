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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .protocol import Message, MessageError, seat_key, ulid

__all__ = [
    "Broker",
    "Delivery",
    "Receipt",
    "Room",
    "RoomError",
    "ROOM_CONVENE_CAP",
    "ROOM_DEAD_AFTER_H",
    "ROOM_HOP_CAP",
    "ROOM_JOIN_CAP",
    "UnknownMessage",
    "broker_for",
    "db_path",
]

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
CREATE TABLE IF NOT EXISTS rooms (
  id            TEXT PRIMARY KEY,
  convener      TEXT NOT NULL,
  seats         TEXT NOT NULL,
  subject       TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'open',
  created_at    REAL NOT NULL,
  last_activity REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rooms_status ON rooms (status, last_activity);
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


@dataclass(frozen=True, slots=True)
class Room:
    """A bounded multi-seat exchange (T32) — the room the owner's decision defines.

    The room *is* its exchange: the id is the correlation every message in it
    carries, so `thread(room.id)` reads a room whole, from any seat. The bounds
    are the owner's adopted decision of 19 Oct, and every one of them is enforced
    at the broker, where the counting is true — not in a prompt, where it would
    be a hope.
    """

    id: str
    convener: str
    seats: tuple[str, ...]
    subject: str
    status: str
    created_at: float
    last_activity: float
    message_count: int = 0

    @property
    def open(self) -> bool:
        return self.status == "open"


#: The bounds, as the owner adopted them (T32, 19 Oct). Named here because they
#: are a decision, not a heuristic — changing one is changing the owner's word,
#: and the docstring above each says what it keeps honest.
ROOM_HOP_CAP = 8
#: Eight messages is request → partial → verify → result, twice over. A room that
#: needs more is two rooms, and the second should say why the first was not enough.
ROOM_CONVENE_CAP = 1
#: Convening is a spend of several seats' turns; the coordinator owns the budget.
ROOM_JOIN_CAP = 2
#: Rooms are the expensive form of talking; the cap keeps one from becoming the
#: drip §9's daily cap exists to prevent.
ROOM_DEAD_AFTER_H = 24
#: An exchange nobody will answer ends as a *named* dead end — reported, removed
#: from the queue — rather than a queue row that never drains, the one failure
#: at-least-once delivery guarantees (T32's acceptance).


class RoomError(ValueError):
    """A room action the bounds refuse — the error says the bound it hit."""


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

        A message whose correlation names a **room** (T32) passes the room's
        bounds first — participant, hop cap, status, and verify-before-leaving —
        and each refusal says the bound it hit, because a cap that stops
        silently is a cap nobody can trust.
        """
        self._room_send_checks(message)
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
                self._touch_room(c, message)
                return Receipt(message=message)
            row = c.execute("SELECT * FROM seat_messages WHERE id = ?", (message.id,)).fetchone()
        return Receipt(message=self._message(row), duplicate=True)

    def _room_send_checks(self, message: Message) -> None:
        """The room bounds, held where the counting is true (T32, adopted 19 Oct).

        Only a message that names a room's exchange is checked — ordinary
        point-to-point mail (T31) is untouched, and the seam is the correlation:
        a room's id *is* its exchange.
        """
        if not message.correlation_id:
            return
        with self._db() as c:
            room = self._room(c, message.correlation_id)
            if room is None:
                return
            if not room.open:
                raise RoomError(
                    f"the room {room.subject!r} has ended ({room.status}) — its exchange "
                    "is readable with op=thread, but nothing new may be said in it"
                )
            if message.sender not in room.seats:
                raise RoomError(
                    f"{message.sender!r} is not in the room {room.subject!r} "
                    f"(its seats are {', '.join(room.seats)}) — a room is convened, "
                    "not overheard"
                )
            if room.message_count >= ROOM_HOP_CAP:
                raise RoomError(
                    f"the room {room.subject!r} has spent its {ROOM_HOP_CAP} messages — "
                    "a room that needs more is two rooms, and the second should say "
                    "why the first was not enough"
                )
            if (
                message.type == "result"
                and message.sender in room.seats
                and message.recipient not in room.seats
            ):
                # The adopted verify rule: an answer must be verified before it
                # *leaves* the room — into the owner's view or another seat's
                # work. Intermediate chatter inside the room needs no verify.
                verified = c.execute(
                    "SELECT COUNT(*) FROM seat_messages"
                    " WHERE correlation_id = ? AND type = 'verify' AND sender != ?",
                    (room.id, message.sender),
                ).fetchone()[0]
                if not verified:
                    raise RoomError(
                        f"an answer leaves the room {room.subject!r} only after it is "
                        "verified — the exchange holds no verify yet, and an "
                        "unverified result leaving is exactly what the rule exists "
                        "to stop"
                    )

    def _touch_room(self, c: sqlite3.Connection, message: Message) -> None:
        """A message landed in a room: the room's clock moves (dead-end sweep reads it)."""
        c.execute(
            "UPDATE rooms SET last_activity = MAX(last_activity, ?) WHERE id = ?",
            (message.created_at, message.correlation_id),
        )

    # -- rooms (T32) -------------------------------------------------------

    def convene(
        self,
        convener: str,
        seats: Sequence[str],
        subject: str,
        *,
        now: float | None = None,
    ) -> Room:
        """Convene a bounded room. The caps are the owner's adopted decision.

        Every refusal names the bound it hit and the seat it counts, because the
        cap is a decision the owner made about *spending* — and a spend that
        stops silently is a spend nobody can account for. The "only the
        coordinator may convene" half of the decision is enforced one layer up,
        in the tool that knows whose turn is asking.
        """
        convener = seat_key(convener, field_name="convener")
        subject = str(subject or "").strip() or "a room"
        others: list[str] = []
        for name in seats:
            key = seat_key(name, field_name="seats")
            if key != convener and key not in others:
                others.append(key)
        if not others:
            raise RoomError(
                "a room needs at least one seat besides the convener — a room of "
                "one is a note to self, which is memory, not a message"
            )
        moment = time.time() if now is None else now
        day_start = datetime.fromtimestamp(moment).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        with self._db() as c:
            convened_today = c.execute(
                "SELECT COUNT(*) FROM rooms WHERE convener = ? AND created_at >= ?",
                (convener, day_start),
            ).fetchone()[0]
            if convened_today >= ROOM_CONVENE_CAP:
                raise RoomError(
                    f"{convener!r} already convened a room today (the cap is "
                    f"{ROOM_CONVENE_CAP}) — convening is a spend of several seats' "
                    "turns, and the owner bounded it"
                )
            # The join cap counts OPEN rooms a seat is in, and it deliberately
            # skips the convener: he is in every room he convenes, so a join cap
            # that counted him could only ever fire on him first and the
            # per-seat bound would be dead code. His bound is the convene cap
            # above; this one is the other seats'. An open room is a room that
            # is still spending the seat's attention — which is what "per day"
            # was always honest about meaning.
            for seat in others:
                joined_open = c.execute(
                    "SELECT COUNT(*) FROM rooms WHERE seats LIKE ? AND status = 'open'",
                    (f'%"{seat}"%',),
                ).fetchone()[0]
                if joined_open >= ROOM_JOIN_CAP:
                    raise RoomError(
                        f"{seat!r} is already in {joined_open} open rooms (the cap "
                        f"is {ROOM_JOIN_CAP}) — rooms are the expensive form of "
                        "talking, and the cap is what keeps one from becoming a drip"
                    )
            room = Room(
                id=ulid(),
                convener=convener,
                seats=(convener, *others),
                subject=subject,
                status="open",
                created_at=moment,
                last_activity=moment,
            )
            c.execute(
                "INSERT INTO rooms (id, convener, seats, subject, status, created_at, last_activity)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    room.id,
                    room.convener,
                    json.dumps(list(room.seats)),
                    room.subject,
                    room.status,
                    room.created_at,
                    room.last_activity,
                ),
            )
        return room

    def sweep_dead_rooms(self, *, cutoff_hours: float = ROOM_DEAD_AFTER_H, now: float | None = None) -> list[Room]:
        """End the rooms nobody answered (T32's acceptance clause).

        An open room with no activity past the cutoff is a **dead end**: it is
        marked, its unresolved messages are resolved as dead — so no seat is
        offered mail from an exchange that is over — and the room is returned so
        the tick that swept it can *name* it. A queue that never drains is the
        one failure at-least-once delivery guarantees; this is where the room
        refuses to become one.
        """
        moment = time.time() if now is None else now
        horizon = moment - cutoff_hours * 3600
        with self._db() as c:
            rows = c.execute(
                "SELECT * FROM rooms WHERE status = 'open' AND last_activity < ?",
                (horizon,),
            ).fetchall()
            dead: list[Room] = []
            for row in rows:
                c.execute(
                    "UPDATE rooms SET status = 'dead' WHERE id = ?", (row["id"],)
                )
                c.execute(
                    "UPDATE seat_messages SET resolved_at = ?, outcome = 'dead'"
                    " WHERE correlation_id = ? AND resolved_at IS NULL",
                    (moment, row["id"]),
                )
                fresh = c.execute("SELECT * FROM rooms WHERE id = ?", (row["id"],)).fetchone()
                dead.append(self._room_from(c, fresh))
            return dead

    def rooms(self, *, status: str | None = None) -> list[Room]:
        """The rooms this home has convened, newest first — the digest's query."""
        with self._db() as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM rooms WHERE status = ? ORDER BY created_at DESC", (status,)
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM rooms ORDER BY created_at DESC").fetchall()
            return [self._room_from(c, row) for row in rows]

    def room(self, room_id: str) -> Room | None:
        """One room by its id — the exchange's own address."""
        with self._db() as c:
            return self._room(c, str(room_id).strip())

    def _room(self, c: sqlite3.Connection, room_id: str) -> Room | None:
        row = c.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if row is None:
            return None
        return self._room_from(c, row)

    def _room_from(self, c: sqlite3.Connection, row: sqlite3.Row) -> Room:
        seats = tuple(json.loads(row["seats"]))
        count = c.execute(
            "SELECT COUNT(*) FROM seat_messages WHERE correlation_id = ?", (row["id"],)
        ).fetchone()[0]
        return Room(
            id=row["id"],
            convener=row["convener"],
            seats=seats,
            subject=row["subject"],
            status=row["status"],
            created_at=row["created_at"],
            last_activity=row["last_activity"],
            message_count=count,
        )

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
