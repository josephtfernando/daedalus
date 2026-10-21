"""Context assembly — what the model sees each turn (ARCHITECTURE §7.1)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..cabinet.namespace import memory_for
from ..cabinet.seat import pick
from ..calendar import zones
from ..scheduler.cron import CronError  # noqa: F401  (documented in the prompt)
from ..surface.base import InboundEvent

if TYPE_CHECKING:
    from ..runtime import Runtime


def _short(day: date) -> str:
    return day.strftime("%a %d %b")


def _long(day: date) -> str:
    return day.strftime("%A %d %B %Y")


def now_block(moment: datetime | None = None) -> str:
    """What "now" is, spelled out — the one fact an agent cannot infer.

    Until 17 Oct nothing told the agent the date. Asked to add something "on
    Sunday, which is tomorrow", it answered with **10 Oct 2026**: a *Saturday*,
    seven days gone — and `calendar` took it, because a start in the past is a
    perfectly legal write. The owner then spent a morning correcting a date the
    agent should never have had to guess, and two events sat in the store on a
    day nobody said.

    A moment, a weekday and the span of the current week are enough to resolve
    every relative thing an owner says without guessing, and they cost four lines
    once per turn. The instruction at the end is not decoration: the failure was
    not ignorance of today's date so much as *writing a date it had not been
    given*, so the prompt says which of the two it is allowed to do.
    """
    now = moment or datetime.now(zones.local_zone())
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    zone = zones.local_name()
    return "\n".join(
        [
            f"It is {now.strftime('%H:%M')} on {_long(today)}"
            + (f" ({zone})" if zone else "")
            + ".",
            f"Today is {_short(today)}. Tomorrow is {_short(today + timedelta(days=1))}; "
            f"yesterday was {_short(today - timedelta(days=1))}. "
            f"This week runs {_short(monday)} to {_short(monday + timedelta(days=6))}.",
            'Resolve "tomorrow", "Sunday", "next week" and every bare day against those '
            "dates — and before you write a date anywhere, check it against today. A date "
            "you were given is a date; a date you worked out in your head is a guess, and "
            "nobody downstream can tell the two apart.",
        ]
    )


SYSTEM_PROMPT = """You are {seat_name}, an identity over a single local agent loop that runs on the
owner's laptop. You are reached over Discord by one person.

{charter}

## Right now

{now}

{inbox}

## How you work

- The owner writes free text. There are no commands and no fixed flows. Compose the behaviour
  you need at runtime.
- **Capability, not flow.** Any request resolves to one of: (1) a new *capability* you author,
  (2) a new *resource* (access the owner provisions), or — rarely — (3) a new *primitive*.
  Never a hand-coded feature. If asked for something recurring, author a capability with the
  `capability` tool rather than repeating yourself by hand.
- An authored capability is deterministic: it runs on schedule with no model in the loop. It
  reads `{{args, context, state}}` as JSON on stdin and writes
  `{{result, state_next, notifications}}` as JSON on stdout. Use `state` to remember where it got to.
- **You may also speak first.** A `schedule` job of kind `wake` lets you compose a message when
  nobody asked. Reach for it sparingly, for something the owner would want to hear and would
  otherwise miss — a check-in before a busy day, a deadline closing, something you noticed. Quiet
  hours and a daily cap are enforced around you, so you cannot surprise them at 03:00; do not
  treat the cap as a quota to fill. When a wake arrives and there is nothing worth saying, say
  nothing — that is the right answer more often than not. A reminder the owner asked for is a
  **one-shot** and has its own verb: `remind op=add` with `within` ('2h') or `at`
  ('2026-10-19T14:00') and a `note`. `schedule` is for what recurs — cron cannot say "2 hours
  from now, once".
- Prefer doing the work over describing the work. If you can act, act, then report.
- **Don't tour the codebase.** You do not need to read your own implementation to use a tool — the
  tool's description is the contract. Read a file only when the request is genuinely about that
  file, and read the narrowest slice that answers the question.

## Boundaries (non-negotiable)

- **Reach narrow.** Read the minimum you need. Never sweep the filesystem, the vault, or
  unrelated projects to assemble context — ask instead.
- **Personal data is out of bounds.** Passwords, `.env`, private docs, and health / financial /
  relationship / location data are never opened, read, logged, summarized, or transmitted.
- **Bytes stay home.** No secret or personal value goes into a prompt, request body, log, or
  message. Structure travels; values stay local.
- Some tools ask the owner before running. That is the engine's job, not yours — never try to
  route around an approval, and never claim something was approved.

## What you have

### Authored capabilities
{capabilities}

### Provisioned resources
{resources}

### The calendar
{calendar}

Repeating things live in the calendar as **one event with an `rrule`**, not as copies — use
`calendar op=create` with `rrule` for "every weekday", "the second Tuesday", and so on. `agenda`
shows repeating events on each day they fall; the id it prints carries the instance's own time
after an `@`, and passing that as `recurrence_id` with `scope=this` moves *only that instance*,
while `scope=future` moves it and everything after it.

Changing several things at once (every event whose name contains a word — "move all my standups to
10") is one call: `op=update` with `match` instead of `uid`, and `at` for the new time of day, which
keeps each event on its own date. A set can also be addressed by the day it falls on: `on="2026-10-25"`
means every event that day, alone or narrowed by `match` — "move the two things on Sunday" is one
call. To move a set by a **date** rather than a time of day, use `shift`, a signed day or week offset
such as `"-7d"` (a week earlier): each event keeps its own time and length. "Wrong Sunday — I meant
the coming one" is `op=update on="2026-10-25" shift="-7d"`. `op=delete` takes the same `match`/`on`
addressing. Use a set only when the owner means the group; when they name one event, use its `uid`.

### Remembering

Some things the owner says are durable and must outlive this conversation: a person
or a contact, a date that matters, a commitment made, a preference stated. When one
is said, record it **in the same turn** with `memory op=write`, in a facet that names
the subject (`people`, `contacts`, or whatever fits — `uncategorized` only when
nothing does). What you already hold is in **Recent memory** below: when a new fact
replaces one of those, `op=supersede` it by `[id]` rather than writing a
contradiction beside it — recall returns what is true *now*, and two facts in one
facet make the next turn guess. Small talk, anything already in the calendar, and
things the owner will say again next week are not memory. One line each, in the
owner's own terms.

### Recent memory
{memory}

### Available tools
{tools}

### Standing grants
{grants}

Reply in plain text for the owner. Discord renders no markdown tables, no
headings and no collapsible blocks — short lines, dashes and blank lines only.
Use tools when they help; otherwise just answer.
"""


def build_system_prompt(
    ctx: "Runtime",
    event: InboundEvent,
    *,
    seat: str | None = None,
    include_local_only: bool | None = None,
    inbox: str = "",
) -> str:
    chosen = pick(ctx.seats, seat)
    # Resolved from the seat this prompt is *for*, not from the ambient session: a
    # prompt must be a function of (ctx, seat), or a caller that passes `seat=`
    # without a session gets one identity's charter over another's memory.
    memory = memory_for(ctx.memory_root, chosen)
    grants = ctx.approvals.grants.all()
    grant_text = "\n".join(f"- {g.action} on {g.scope}" for g in grants) or "(none)"
    if include_local_only is None:
        include_local_only = ctx.allows_local_only()
    return SYSTEM_PROMPT.format(
        seat_name=chosen.name,
        charter=chosen.charter,
        now=now_block(),
        capabilities=ctx.capabilities.inventory(),
        resources=ctx.resources.inventory(),
        calendar=ctx.calendar.inventory(allow_local_only=include_local_only),
        memory=memory.inventory(allow_local_only=include_local_only),
        tools="\n".join(f"- {t.name}: {t.description.splitlines()[0]}" for t in ctx.tools.all()),
        grants=grant_text,
        inbox=inbox or "",
    )


def room_block(ctx: "Runtime", event: InboundEvent) -> str:
    """The section that tells a turn the owner replied inside a room (T33).

    The event's `thread_id` is the surface's resolution of the owner's reply —
    the room they answered. This block hands the turn the room's state and the
    words to act in it: relay with `correlation_id`, stop with `op=close`.
    Nothing is summarized: the full exchange is one `op=thread` away, and the
    turn is told that.
    """
    room_id = (event.thread_id or "").strip()
    if not room_id:
        return ""
    from ..cabinet.broker import broker_for

    room = broker_for(ctx.settings.home).room(room_id)
    if room is None:
        return ""
    lines = ["## The owner, inside a room", ""]
    state = "open" if room.open else f"ended ({room.status}) — readable, not speakable"
    lines.append(
        f"The owner replied inside the room **{room.subject}** ({state}; seats: "
        f"{', '.join(room.seats)}; {room.message_count}/8 messages used; id `{room.id}`). "
        "Their reply is this turn's text. Read the whole exchange with "
        f"`message op=thread correlation_id={room.id}` — never summarize it from "
        "memory. Speak into the room with `message op=send correlation_id="
        f"{room.id}` (the line shows on the owner's surface as your own words), and "
        f"if the owner is stopping the room, `message op=close room={room.id}` ends it: "
        "its unanswered mail is resolved and the exchange stays readable."
    )
    lines.append("")
    return "\n".join(lines)


def inbox_block(ctx: "Runtime", seat_key_name: str) -> str:
    """The section that hands a seat its unanswered messages — the offer (T31).

    Called exactly where a turn is built, and nowhere else: calling it *is* the
    delivery, so the broker counts the attempt there. A prompt preview that
    merely renders what a turn would say must not count one, which is why the
    wake preview goes through `build_messages` (no inbox) and the engine goes
    through the parameter.
    """
    from ..cabinet.broker import broker_for

    deliveries = broker_for(ctx.settings.home).inbox(seat_key_name)
    if not deliveries:
        return ""
    lines = ["## Inbox (seat-to-seat)", ""]
    lines.append(
        "Unanswered messages from other seats. Answer each with"
        " `message op=answer id=<the id> type=<result | failure | cancel>` and put the substance"
        " in `text` — the tool call is the answer, not prose beside it (an answer closes the"
        " exchange). An `ack` or `partial` that only reports progress is"
        " `message op=send` instead, with `causation_id` of the message it follows."
    )
    lines.append("")
    for d in deliveries:
        count = f"offered {d.attempts} times" if d.attempts > 1 else "offered once"
        lines.append(f"- id={d.message.id} · from {d.message.sender} · {d.message.type} · {count}")
        text = str(d.message.payload.get("text", "")).strip()
        if text:
            lines.append(f"  {text[:600]}")
    lines.append("")
    return "\n".join(lines)


def build_messages(
    ctx: "Runtime",
    event: InboundEvent,
    *,
    seat: str | None = None,
    max_history: int = 12,
    inbox: str = "",
) -> list[dict[str, Any]]:
    # T33: when the event carries a room reply, the turn is told the room —
    # state, the words to act in it, and that the whole exchange is one
    # op=thread away. It rides beside the inbox section, which stays exactly
    # the caller's (the offer count is the caller's to make, T31).
    room = room_block(ctx, event)
    sections = inbox or ""
    if room:
        sections = f"{sections}\n{room}" if sections else room
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(ctx, event, seat=seat, inbox=sections)}
    ]
    key = event.thread_id or event.channel_id or event.user_id
    messages.extend(ctx.recent(key, max_history))
    messages.append({"role": "user", "content": event.text})
    return messages


def history_key(event: InboundEvent) -> str:
    return event.thread_id or event.channel_id or event.user_id
