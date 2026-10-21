"""``message`` — seat-to-seat: what gets sent, what answers it, what it left behind.

T31 is where T14's queue stops being substrate. The engine offers a seat its
inbox on every turn (``loop.context.inbox_block``); this tool is how the seat
does something about it. Three acts, one rule behind all of them — *the exchange
is the unit*:

- **`op=answer`** closes an incoming message: it resolves the message in the
  queue and sends the answer *as a message* back to the sender, caused by the
  one it answers and carrying the same correlation — so `thread()` reads the
  exchange whole, from either side, forever. A second answer to an already
  resolved message is refused with "the first answer stands" — refusing is not
  an error state, it is the dedup the protocol asks of every side effect.
- **`op=send`** opens or continues an exchange *without* closing anything: a
  new exchange's ``request``, or the progress types (`ack`, `partial`,
  `verify`) that ride in the middle of one. A **retry is a chosen id**: the
  same id sent twice is one message in the queue, reported as a duplicate
  rather than stored twice.
- **`op=thread`** reads one exchange back in the order it happened.

Reading never deletes: resolution is an answered timestamp, not a tombstone.
What this tool does not build is the *room* (T32) — no hops, no budget, no
convening. That decision is the owner's and SPEC has not had it yet.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from ..cabinet.broker import RoomError, UnknownMessage, broker_for
from ..cabinet.protocol import TERMINAL, Message, MessageError, TYPES
from ..cabinet.seat import DEFAULT_SEAT
from .base import Tool, ToolResult, params, sent_args

if TYPE_CHECKING:
    from ..runtime import Runtime


class MessageTool(Tool):
    name = "message"
    title = "Message another seat"
    description = (
        "Send to, answer, or read back seat-to-seat exchanges (your inbox is "
        "handed to you each turn). op=answer id=<message id> type=<result|failure|"
        "cancel> text=... closes the message it answers and replies to its sender "
        "within the same exchange — a second answer to a resolved message is "
        "refused: the first answer stands. op=send to=<seat key> type=<request|ack|"
        "partial|verify|result|failure|cancel> text=... opens or continues an "
        "exchange; pass correlation_id to keep one going, causation_id for the "
        "message it follows, and id when retrying — the same id is the same "
        "message, not a second one. op=list shows what is unanswered for your "
        "seat, with its offer count; asking counts as another offer, which is the "
        "point. op=thread correlation_id=... reads the whole exchange back. "
        "op=convene seats=<seat keys, comma-separated> text=<the subject> opens a "
        "bounded room (only the coordinator's turns may: convening is a spend) — "
        "its bounds are the owner's: 8 messages total, at most 2 rooms a day per "
        "seat, one convening a day, a 24-hour-unanswered room ends as a named "
        "dead end, and an answer must be verify-ed before it leaves the room; "
        "send into it with correlation_id=<room id>. op=rooms lists the rooms and "
        "their state."
    )
    risk = Risk.AUTO
    scopes = {}
    parameters = params(
        op=(
            {
                "type": "string",
                "enum": ["answer", "send", "list", "thread", "convene", "rooms"],
                "description": "answer closes an incoming message; send opens or continues one; convene opens a bounded room.",
            },
            True,
        ),
        id={"type": "string", "description": "The message id (for answer; a chosen id makes a send retry-safe)."},
        to={
            "type": "string",
            "description": "The seat key to send to — the charter file's name ('athena'), not the display name.",
        },
        type=(
            {
                "type": "string",
                "enum": list(TYPES),
                "description": "Message type. answer takes a closing type; send takes any.",
            },
            False,
        ),
        text=(
            {
                "type": "string",
                "description": "The substance of the message — its payload, not a gesture. "
                "For the read ops (`list`, `thread`, `rooms`) send an empty string; "
                "for `convene` it is the room's subject.",
            },
            True,
        ),
        correlation_id={"type": "string", "description": "The exchange a send continues."},
        causation_id={"type": "string", "description": "The message this one follows from."},
        seats={
            "type": "string",
            "description": "For op=convene: the seats to convene, comma-separated keys ('athena, icarus').",
        },
    )

    def title_for(self, args: dict[str, Any]) -> str:
        op = str(args.get("op", ""))
        if op == "answer":
            return "Answer a seat's message"
        if op == "send":
            return f"Message seat {args.get('to', '')!r}"
        return f"{self.title} ({op})"

    def assess(self, args: dict[str, Any]) -> Assessment:
        return Assessment(level=Level.OK, summary=f"seat messaging ({args.get('op')})")

    def risk_for(self, args: dict[str, Any]) -> Risk:
        return Risk.AUTO

    # ------------------------------------------------------------------

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        sent = sent_args(args)
        who = ctx.session.seat if ctx.session is not None else ""
        broker = broker_for(ctx.settings.home)
        text = str(args.get("text", "")).strip()
        try:
            if op == "list":
                mine = broker.inbox(who)
                rows = [
                    f"id={d.message.id} · from {d.message.sender} · {d.message.type}"
                    f" · offered {d.attempts} time(s): {str(d.message.payload.get('text', ''))[:200]}"
                    for d in mine
                ]
                return ToolResult(output="\n".join(rows) or "your inbox is clear")
            if op == "thread":
                correlation = str(args.get("correlation_id", "")).strip()
                if not correlation:
                    return ToolResult.error(f"op=thread needs correlation_id. You sent: {sent}")
                rows = [
                    f"{m.sender} → {m.recipient} · {m.type} · id={m.id}"
                    + (f" (follows {m.causation_id})" if m.causation_id else "")
                    + f"\n    {str(m.payload.get('text', ''))[:400]}"
                    for m in broker.thread(correlation)
                ]
                return ToolResult(output="\n".join(rows) or f"no exchange under {correlation!r}")
            if op == "rooms":
                known = broker.rooms()
                rows = [
                    f"{r.status.upper()} · {r.subject} · seats: {', '.join(r.seats)}"
                    f" · {r.message_count}/8 messages · room {r.id}"
                    for r in known
                ]
                return ToolResult(output="\n".join(rows) or "no rooms have been convened")
            if op == "convene":
                return self._convene(args, broker, who, list(ctx.seats.keys()), text, sent)
            if op not in ("send", "answer"):
                return ToolResult.error(
                    f"unknown message op {op!r} — answer, send, list, thread, convene, rooms. You sent: {sent}"
                )
            if not text:
                return ToolResult.error(
                    f"op={op} needs `text` — the message's substance is its payload, "
                    f"not a gesture. You sent: {sent}"
                )
            if op == "answer":
                return self._answer(args, broker, who, list(ctx.seats.keys()), text, sent)
            return self._send(args, broker, who, list(ctx.seats.keys()), text, sent)
        except (MessageError, RoomError, UnknownMessage) as exc:
            return ToolResult.error(f"{exc} You sent: {sent}")

    def _convene(
        self, args: dict[str, Any], broker: Any, who: str, seat_keys: list[str], text: str, sent: str
    ) -> ToolResult:
        """Open a bounded room — the spend the owner capped, so the coordinator's."""
        if who != DEFAULT_SEAT:
            return ToolResult.error(
                f"only the coordinator convenes rooms ({DEFAULT_SEAT!r} is the seat that "
                f"owns the spend — this turn is {who or 'nobody'!r}). The owner adopted "
                "that bound on 19 Oct; a seat that wants a room asks the coordinator. "
                f"You sent: {sent}"
            )
        raw = str(args.get("seats", "")).strip()
        wanted = [s for s in (part.strip() for part in raw.split(",")) if s]
        if not wanted:
            return ToolResult.error(
                f"op=convene needs `seats` — the keys to convene, comma-separated "
                f"('athena, icarus'). You sent: {sent}"
            )
        known = sorted(set(seat_keys))
        unknown = [s for s in wanted if s not in known]
        if unknown:
            return ToolResult.error(
                f"not seats in this cabinet: {', '.join(unknown)} (the cabinet holds "
                f"{', '.join(known) or 'nobody'}) — a room for nobody is a queue that "
                f"never drains. You sent: {sent}"
            )
        try:
            room = broker.convene(convener=who, seats=wanted, subject=text or "a room")
        except RoomError as exc:
            return ToolResult.error(f"{exc} You sent: {sent}")
        return ToolResult(
            ok=True,
            output=(
                f"room {room.id} convened — {room.subject!r} with {', '.join(room.seats)}. "
                "Its bounds are the owner's: 8 messages total, at most 2 rooms a day per "
                "seat, and a room nobody answers for 24 hours ends as a named dead end. "
                f"Send into it with correlation_id={room.id}, and an answer must be "
                "verify-ed before it leaves."
            ),
            data={"room": room.id, "seats": list(room.seats), "subject": room.subject},
        )

    def _answer(
        self, args: dict[str, Any], broker: Any, who: str, seat_keys: list[str], text: str, sent: str
    ) -> ToolResult:
        message_id = str(args.get("id", "")).strip()
        if not message_id:
            return ToolResult.error(f"op=answer needs `id` — the message it closes. You sent: {sent}")
        inbound = broker.get(message_id)
        if inbound is None:
            return ToolResult.error(
                f"no message {message_id!r} — op=list shows your mail. You sent: {sent}"
            )
        if inbound.recipient != who:
            return ToolResult.error(
                f"{message_id!r} is addressed to {inbound.recipient!r}, not to you — "
                "a seat answers its own mail"
            )
        reply_type = str(args.get("type", "")).strip() or "result"
        if reply_type not in TERMINAL:
            return ToolResult.error(
                f"an answer closes the exchange: one of {', '.join(sorted(TERMINAL))} — "
                f"{reply_type!r} leaves it open. If this is progress, use op=send. "
                f"You sent: {sent}"
            )
        resolved = broker.resolve(message_id, outcome=reply_type, note=text[:120])
        if not resolved:
            return ToolResult.error(
                f"{message_id!r} is already resolved — the first answer stands "
                "(answering it again is the duplicate the protocol refuses)"
            )
        # The answer's route is not a parameter: `reply()` sends it back the way
        # the message came, correlation inherited, causation pointed at the
        # message it answers.
        reply = inbound.reply(type=reply_type, payload={"text": text})
        try:
            receipt = broker.send(reply)
        except Exception:
            # The resolve already happened; a queue failure between the two acts
            # would drop the answer silently. `reopen` puts the message back (its
            # attempt count follows) — at-least-once survives even its own bug.
            broker.reopen(message_id)
            raise
        return ToolResult(
            ok=True,
            output=(
                f"answered {message_id!r} — {reply_type} sent to {inbound.sender!r}; "
                f"the exchange is {reply.correlation_id}"
                + (" (the answer was already queued)" if receipt.duplicate else "")
            ),
            data={
                "correlation_id": reply.correlation_id,
                "message_id": receipt.message.id,
                "first_answer_stood": not receipt.duplicate,
            },
        )

    def _send(
        self, args: dict[str, Any], broker: Any, who: str, seat_keys: list[str], text: str, sent: str
    ) -> ToolResult:
        to = str(args.get("to", "")).strip()
        if not to:
            return ToolResult.error(f"op=send needs `to` — a seat key (e.g. 'athena'). You sent: {sent}")
        seat_keys = sorted(set(seat_keys))
        if to not in seat_keys:
            return ToolResult.error(
                f"{to!r} is not a seat in this cabinet ({', '.join(seat_keys) or 'none'}) — "
                f"a message to nobody is a queue that never drains. You sent: {sent}"
            )
        mtype = str(args.get("type", "")).strip() or "request"
        message = Message(
            sender=who,
            recipient=to,
            type=mtype,
            payload={"text": text},
            correlation_id=str(args.get("correlation_id", "")).strip(),
            causation_id=str(args.get("causation_id", "")).strip(),
            id=str(args.get("id", "")).strip(),
        )
        receipt = broker.send(message)
        return ToolResult(
            ok=True,
            output=(
                f"sent to {to!r} ({message.type}) — id {message.id}"
                + (" (a retry: it was already queued)" if receipt.duplicate else "")
                + f"; exchange {message.correlation}"
            ),
            data={"id": message.id, "correlation_id": message.correlation},
        )
