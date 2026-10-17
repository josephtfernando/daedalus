"""Who takes this message (SPEC §7).

The owner should not have to name a seat. Daedalus coordinates, and part of
coordinating is knowing that "drill me on graphs" is Athena's and "who is hiring"
is Odysseus's — so a message is routed to the seat it belongs to, and that seat
answers as itself: its own charter, its own memory namespace, its own voice.

**The menu is the cabinet.** A charter already says what a seat owns, so triage
reads the charters themselves rather than a second list of responsibilities
living in code — a second list would drift the day a charter was edited, and it
would be the copy the model believed. A hand-written ninth charter is routable
the moment it exists.

Four rules keep routing from becoming a second, worse owner:

- **A name the owner wrote wins, and no model is asked.** "athena: drill me" is a
  command, not a subject for judgement. (A name that is *not* a seat is not
  addressing — SPEC §7 says an unrecognised name lands on the identity already
  answering, rather than failing the request.)
- **The answer must be a seat that exists.** The model's reply is validated
  against the charters; it cannot invent an identity.
- **Anything unreadable falls back rather than guessing** — no answer, an
  unknown name, an unreachable provider, a message too short to be about
  anything. The seat that was already handling this conversation keeps it; else
  the active seat does.
- **A chatty reply is not a routed reply.** Reading is deliberately strict: a
  leading name before a separator, or nothing. Prose that merely mentions a seat
  is not an answer, because "not Athena" also contains "Athena".

Triage is a *preference*, never a gate. A routed turn is the same turn run as a
different identity; nothing is delegated, nothing is hidden, and the reply
carries the name of whoever took it — which is what makes a wrong route
correctable rather than mysterious.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from .seat import DEFAULT_SEAT, Seat, UnknownSeat, active_seat, resolve

__all__ = [
    "MAX_REASON",
    "Decision",
    "addressed",
    "menu",
    "prompt",
    "read",
    "triage",
]

#: A reason is shown to the owner next to a name; a paragraph is not a reason.
MAX_REASON = 140

#: Asking the model. Injected so triage can be tested without one, and so the
#: caller keeps control of which provider a routing question goes to.
Ask = Callable[[str], Awaitable[str]]

#: Addressing a seat. Three forms, each of which has to be unambiguous *before*
#: it is checked against the cabinet:
#:
#: - `@athena …` — the `@` is the address, so no separator is needed;
#: - `athena: …`, `athena, …`, `athena — …` — a leading name and a separator, so
#:   an ordinary sentence that happens to start with a seat's name ("Home is
#:   where…") is not read as addressing;
#: - `to athena: …` — an explicit hand-off.
#:
#: A name that survives all three still has to *be* a seat: `resolve` decides,
#: and a leading word that is not one is an ordinary sentence.
_AT = re.compile(r"^\s*@\s*([^\W\d_][\w'\u00C0-\u024f-]*)", re.UNICODE)
_TO = re.compile(
    r"^\s*to\s+([^\W\d_][\w'\u00C0-\u024f-]*)\s*(?:[:,]|\u2014|-{1,2})?\s+",
    re.UNICODE,
)
_NAME = re.compile(
    r"^\s*([^\W\d_][\w'\u00C0-\u024f-]*)\s*(?:[:,]|\u2014|-{1,2})\s+",
    re.UNICODE,
)

#: Below this, the message is a pleasantry rather than a subject ("ok", "yes").
#: A routing call for it would be spent to learn what the fallback already knows.
_MIN_CHARS = 4


@dataclass(frozen=True, slots=True)
class Decision:
    """The seat a message belongs to, and how that was decided."""

    seat: Seat
    #: "named" — the owner addressed it; "model" — triage chose;
    #: "previous" — the seat already handling this conversation; "default" — the
    #: active seat, because nothing usable came back.
    by: str = "default"
    reason: str = ""
    #: The raw answer, kept so a bad route can be diagnosed from the log or a test.
    raw: str = field(default="")

    @property
    def routed(self) -> bool:
        """Whether this was a *decision* rather than a fallback."""
        return self.by in ("named", "model")


def menu(seats: Mapping[str, Seat]) -> str:
    """The cabinet as a list for the prompt: key, name, and the charter."""
    lines = []
    for key in sorted(seats):
        seat = seats[key]
        lines.append(f"- {key} ({seat.name}): {seat.charter}")
    return "\n".join(lines)


def addressed(text: str, seats: Mapping[str, Seat]) -> Seat | None:
    """The seat the owner addressed at the start of a message, if any."""
    body = str(text or "")
    for pattern in (_AT, _TO, _NAME):
        found = pattern.match(body)
        if not found:
            continue
        try:
            return resolve(seats, found.group(1))
        except UnknownSeat:
            # A leading word that is not a seat is an ordinary sentence
            # ("Right, so…"), not a failed address. SPEC §7: an unrecognised
            # name lands on the identity already answering.
            return None
    return None


def read(raw: str, seats: Mapping[str, Seat]) -> tuple[Seat, str] | None:
    """A model answer as (seat, reason), or ``None`` if it cannot be believed.

    Strict on purpose. The first line must *lead* with a seat — before a
    separator, or as the whole line — because a mention in prose is not a choice:
    "not Athena, this is Odysseus's" contains both names.
    """
    line = ""
    for candidate in str(raw or "").splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    if not line:
        return None
    stripped = line.lstrip("-*# ").strip()
    head, reason = stripped, ""
    for sep in (":", ",", " — ", " – ", " - ", "\t"):
        if sep in stripped:
            before, _, after = stripped.partition(sep)
            # Only a *leading* name counts: the separator must come early.
            if len(before.strip()) <= 24:
                head, reason = before.strip(), after.strip()
            break
    if not head:
        return None
    try:
        seat = resolve(seats, head.strip("\"'`*.").strip())
    except UnknownSeat:
        return None
    return seat, reason[:MAX_REASON].replace("\n", " ").strip()


def prompt(
    text: str,
    seats: Mapping[str, Seat],
    *,
    previous: str = "",
    active: str = DEFAULT_SEAT,
) -> str:
    """The routing question. Short, closed, and with the charters in it."""
    last = ""
    if previous and previous in seats:
        last = (
            f"\nThe previous message in this conversation was handled by {previous}. "
            "A follow-up with no subject of its own belongs to the same seat.\n"
        )
    return (
        "One message arrived for the owner's agent, which has the seats below. "
        "Name the single seat it belongs to.\n\n"
        f"Seats:\n{menu(seats)}\n"
        f"{last}"
        f"\nAnswer with the seat's key, then a colon, then one short clause saying why. "
        f"Nothing else — no preamble, no alternatives.\n"
        f"If the message spans seats, is about priorities or the week, or you are unsure, "
        f"answer {active}: the coordinator keeps what does not clearly belong elsewhere.\n"
        f"\nMessage: {text}"
    )


async def triage(
    text: str,
    seats: Mapping[str, Seat],
    *,
    ask: Ask | None = None,
    previous: str = "",
    active: str = "",
) -> Decision:
    """Which seat takes this message.

    Never raises on account of the model: a routing question that fails is a
    routing question that falls back, because the owner's request must not be
    lost to a classifier's bad day.
    """
    body = str(text or "").strip()
    fallback, fallback_by = _fallback(seats, previous, active)

    named = addressed(body, seats)
    if named is not None:
        return Decision(named, "named", f"addressed as {named.name}")

    if ask is None or len(body) < _MIN_CHARS:
        return Decision(fallback, fallback_by, "nothing to triage")

    try:
        raw = await ask(prompt(body, seats, previous=previous, active=active_seat(seats).key))
    except Exception as exc:  # noqa: BLE001 — a classifier must not cost a turn
        return Decision(fallback, fallback_by, f"triage could not run ({type(exc).__name__})")

    parsed = read(raw, seats)
    if parsed is None:
        return Decision(
            fallback,
            fallback_by,
            "no usable answer from triage",
            raw=str(raw or ""),
        )
    seat, reason = parsed
    return Decision(seat, "model", reason, raw=str(raw or ""))


def _fallback(
    seats: Mapping[str, Seat], previous: str, active: str
) -> tuple[Seat, str]:
    """The seat already handling this conversation, else the active one.

    Returned with *why*, because "we fell back" and "the owner addressed it"
    are different answers to the owner's next question: who is this, and why.
    """
    for name, by in ((previous, "previous"), (active, "default")):
        wanted = str(name or "").strip().lower()
        if not wanted:
            continue
        for key, seat in seats.items():
            if wanted in (key.lower(), seat.name.lower()):
                return seat, by
    return active_seat(seats), "default"
