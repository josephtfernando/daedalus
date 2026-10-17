"""The typed message one seat sends another (ARCHITECTURE §9.2, SPEC §7).

A seat is identity + charter + memory namespace — *not a process* — so seats do
not address each other over a socket. They leave messages in one durable queue
and the loop delivers them. That puts the interface in the **message**, and every
guarantee somewhere a restart can keep it: a queue that lives in memory is a
conversation that ends when the daemon does.

```
{ id, correlation_id, causation_id, from, to, type, payload, created_at }
type ∈ request | ack | partial | result | verify | failure | cancel
```

Three things this module decides, each because getting it wrong is invisible
until the day it matters:

- **The id is minted before the attempt, not inside it.** The queue is
  at-least-once: a sender may write the same message twice — it crashed between
  the write and hearing back, or the write landed and the ack was lost. The only
  thing that makes a retry safe is that the retry carries *the same id*, and the
  only way that can be true is if the id existed before the first attempt. An id
  minted inside the retry loop is a brand-new message every time, and no dedup
  downstream can tell it was ever one message.
- **The set of types is closed.** `ack` and `result` are different promises to a
  seat that is waiting, and an unrecognised type stored as an opaque row is a
  request nobody answers and nobody can explain.
- **A reply carries the exchange it belongs to.** :meth:`Message.reply` sets
  `causation_id` to the message it answers and *inherits* `correlation_id` from
  it. So "what is this about" is one query, "what caused it" is one hop, and no
  seat has to remember either across a restart.

Nothing here reaches the network. Seat-to-seat is a conversation inside one
home; this is the *structure* that moves, and the values stay where they are.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..memory.schema import ulid

__all__ = [
    "ACK",
    "CANCEL",
    "FAILURE",
    "PARTIAL",
    "REQUEST",
    "RESULT",
    "TERMINAL",
    "TYPES",
    "VERIFY",
    "Message",
    "MessageError",
    "seat_key",
]

REQUEST = "request"
ACK = "ack"
PARTIAL = "partial"
RESULT = "result"
VERIFY = "verify"
FAILURE = "failure"
CANCEL = "cancel"

#: Every type a message may carry, in the order §9.2 names them.
TYPES: tuple[str, ...] = (REQUEST, ACK, PARTIAL, RESULT, VERIFY, FAILURE, CANCEL)

#: Types that *end* an exchange. `ack` and `partial` are progress — a seat that
#: treats an `ack` as an answer replies to nothing and waits for a result that
#: has already arrived.
TERMINAL: frozenset[str] = frozenset({RESULT, FAILURE, CANCEL})

#: A seat key: lower case, digits, `-` and `_`. The same shape as a namespace,
#: because a key is the name of the file the charter lives in.
_SEAT = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class MessageError(ValueError):
    """A message that cannot be believed — refused where it would be misread."""


def seat_key(name: str, *, field_name: str = "seat") -> str:
    """A seat **key** — the handle, not the name written on the charter.

    Both spellings reach a message: the prompt says "Melinoë", the charter file
    is `melinoe.toml`, and the owner will use either. Only one of them addresses
    anything. A display name stored as a recipient is a letter to an empty room —
    it sits in the queue forever looking exactly like a seat that never answers —
    so it is refused here, with the key spelled out, rather than accepted and
    never read.
    """
    text = str(name or "").strip().lower()
    if not _SEAT.match(text):
        raise MessageError(
            f"{field_name}={name!r} is not a seat key — use the lower-case handle, "
            "which is the charter file's name without `.toml` (`melinoe`), not the "
            "display name (`Melinoë`)"
        )
    return text


@dataclass(slots=True)
class Message:
    """One seat's message to another. ``sender`` and ``recipient`` are §9.2's
    `from` and `to`: `from` is a Python keyword, so the attribute is named for
    what it is and the mapping is one line to read."""

    sender: str
    recipient: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    #: This message's own identity. Chosen *before* the first send — see the
    #: module docstring — so a retry is the same message and not a second one.
    #: Empty means "mint one now", which is the right thing for a first attempt.
    id: str = field(default_factory=ulid)
    #: The exchange this belongs to. Empty means this message is the one that
    #: opened it, and :attr:`correlation` then answers with its own id.
    correlation_id: str = ""
    #: The message this one answers or follows from. One hop back, not a thread.
    causation_id: str = ""
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.sender = seat_key(self.sender, field_name="sender")
        self.recipient = seat_key(self.recipient, field_name="recipient")
        self.type = str(self.type or "").strip().lower()
        if self.type not in TYPES:
            raise MessageError(
                f"unknown message type {self.type!r} — a seat message is one of "
                f"{', '.join(TYPES)}"
            )
        if not isinstance(self.payload, dict):
            raise MessageError(
                f"payload must be a mapping of the message's facts, not {type(self.payload).__name__} "
                "— `type` says what the message is, `payload` carries what it knows"
            )
        # Copied, so the sender's dict is not how the stored message changes.
        self.payload = dict(self.payload)
        self.id = str(self.id or "").strip() or ulid()
        self.correlation_id = str(self.correlation_id or "").strip()
        self.causation_id = str(self.causation_id or "").strip()
        if self.sender == self.recipient:
            raise MessageError(
                f"{self.sender} cannot send itself a {self.type!r} — a seat's notes to "
                "itself are memory (a fact, supersedable) or a scheduled job, not a "
                "message. This is almost always a routing slip: the sender's own key "
                "ended up as the recipient."
            )

    @property
    def correlation(self) -> str:
        """The exchange this belongs to — its own id when it started one."""
        return self.correlation_id or self.id

    @property
    def terminal(self) -> bool:
        return self.type in TERMINAL

    @property
    def answers(self) -> str:
        """The id of the message this one answers, or ``""``."""
        return self.causation_id

    def reply(self, *, type: str, payload: dict[str, Any] | None = None) -> Message:
        """The message that answers this one, from the seat it was addressed to.

        Correlation is **inherited**, not re-minted: an answer belongs to the
        exchange the request opened, so one query finds both. Causation points at
        the request itself, which is what makes "why did you say this" a hop
        rather than a reconstruction. The route is not a parameter and cannot be
        got wrong — the answer goes back the way the message came.
        """
        return Message(
            sender=self.recipient,
            recipient=self.sender,
            type=type,
            payload=dict(payload or {}),
            correlation_id=self.correlation,
            causation_id=self.id,
        )
