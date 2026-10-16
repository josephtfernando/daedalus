"""A small iCalendar (RFC 5545) reader/writer — the ``VEVENT`` slice.

The internal calendar is file-first (SPEC §5.5): ``events.ics`` has to open in
the owner's real calendar apps, and survive being edited there. So it is
written as valid iCalendar rather than a private format, and this module is
deliberately *lossless where it must be* — property lines it does not model are
carried through verbatim (:attr:`Event.extra`), and non-``VEVENT`` components
(``VTIMEZONE``, ``VTODO``, …) are passed back out untouched.

It is not a general iCalendar implementation. It covers what the agent writes
and reads, and says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from ..security.provenance import DataClass
from .event import INTERNAL, Event

PRODID = "-//Daedalus//agent calendar//EN"

#: Property lines this module models. Anything else in a VEVENT round-trips
#: through ``Event.extra``.
_KNOWN = frozenset(
    {
        "UID",
        "DTSTART",
        "DTEND",
        "SUMMARY",
        "LOCATION",
        "DESCRIPTION",
        "DTSTAMP",
        "LAST-MODIFIED",
        "X-DAEDALUS-CALENDAR",
        "X-DAEDALUS-AUTHORITY",
        "X-DAEDALUS-DATA-CLASS",
        "X-DAEDALUS-CREATED",
    }
)


@dataclass(slots=True)
class ParsedCalendar:
    events: list[Event] = field(default_factory=list)
    #: Component blocks that are not VEVENTs, kept as raw lines so a caller that
    #: rewrites the file does not delete them.
    passthrough: list[list[str]] = field(default_factory=list)


# -- text escaping (RFC 5545 §3.3.11) --------------------------------------


def escape(text: str) -> str:
    out = str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return out.replace("\r\n", "\\n").replace("\n", "\\n")


def unescape(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            out.append("\n" if nxt in ("n", "N") else nxt)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


# -- line folding (RFC 5545 §3.1) ------------------------------------------


def fold(line: str) -> str:
    """Break a content line at 75 octets, continuations starting with a space."""
    if len(line.encode("utf-8")) <= 75:
        return line
    chunks: list[str] = []
    current = ""
    width = 0
    for char in line:
        size = len(char.encode("utf-8"))
        # 73 leaves room for the space that introduces the next line.
        if width + size > 73:
            chunks.append(current)
            current, width = char, size
        else:
            current += char
            width += size
    chunks.append(current)
    return "\r\n ".join(chunks)


def unfold(text: str) -> list[str]:
    """Join folded continuations; blank lines are dropped."""
    lines: list[str] = []
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    for raw in normalised.split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return [line for line in lines if line.strip()]


# -- rendering --------------------------------------------------------------


def render_calendar(events: Iterable[Event], passthrough: Iterable[list[str]] = ()) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
    ]
    for event in events:
        lines.extend(_event_lines(event))
    for block in passthrough:
        lines.extend(block)
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def _event_lines(event: Event) -> list[str]:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{_utc_stamp(event.updated_at)}",
        _date_line("DTSTART", event.start, event.all_day),
    ]
    if event.end:
        lines.append(_date_line("DTEND", event.end, event.all_day))
    lines.append(f"SUMMARY:{escape(event.summary)}")
    if event.location:
        lines.append(f"LOCATION:{escape(event.location)}")
    if event.description:
        lines.append(f"DESCRIPTION:{escape(event.description)}")
    lines.append(f"X-DAEDALUS-CALENDAR:{event.calendar}")
    lines.append(f"X-DAEDALUS-AUTHORITY:{event.authority}")
    lines.append(f"X-DAEDALUS-DATA-CLASS:{event.data_class.value}")
    lines.append(f"X-DAEDALUS-CREATED:{_local_stamp(event.created_at)}")
    lines.append(f"LAST-MODIFIED:{_utc_stamp(event.updated_at)}")
    lines.extend(f"{name}:{value}" for name, value in event.extra)
    lines.append("END:VEVENT")
    return lines


def _date_line(name: str, value: str, all_day: bool) -> str:
    if all_day:
        return f"{name};VALUE=DATE:{value.replace('-', '')}"
    return f"{name}:{datetime.fromisoformat(value).strftime('%Y%m%dT%H%M%S')}"


def _utc_stamp(epoch: float) -> str:
    moment = datetime.fromtimestamp(epoch or 0.0, tz=timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _local_stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch or 0.0).strftime("%Y-%m-%dT%H:%M:%S")


# -- parsing ----------------------------------------------------------------


def parse_calendar(text: str) -> ParsedCalendar:
    """Read a calendar document. Raises ``ValueError`` on an unreadable time.

    Raising is deliberate: a store that rewrites the file must not drop the one
    event it could not read.
    """
    result = ParsedCalendar()
    lines = unfold(text)
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.upper().startswith("BEGIN:"):
            index += 1
            continue
        name = line[6:].strip().upper()
        if name == "VCALENDAR":
            # The wrapper is not a component we keep; step inside it.
            index += 1
            continue
        depth = 1
        block: list[str] = [line]
        cursor = index + 1
        while cursor < len(lines) and depth > 0:
            candidate = lines[cursor]
            upper = candidate.upper()
            if upper.startswith("BEGIN:"):
                depth += 1
            elif upper.startswith("END:"):
                depth -= 1
            # The BEGIN/END pair is part of the block: a component written back
            # without them is a corrupt file, not a preserved one.
            block.append(candidate)
            if depth == 0:
                break
            cursor += 1
        if name == "VEVENT":
            event = _from_block(block[1:-1])
            if event is not None:
                result.events.append(event)
        else:
            result.passthrough.append(block)
        index = cursor + 1
    return result


def _from_block(block: list[str]) -> Event | None:
    fields: dict[str, str] = {}
    extra: list[tuple[str, str]] = []
    for line in block:
        name, _, value = line.partition(":")
        if name.split(";", 1)[0].upper() in _KNOWN:
            fields[name.split(";", 1)[0].upper()] = value
        else:
            extra.append((name, value))
    start_raw = fields.get("DTSTART", "").strip()
    if not start_raw:
        return None
    start, all_day = _parse_stamp(start_raw)
    end = _parse_stamp(fields["DTEND"])[0] if fields.get("DTEND") else ""
    return Event(
        uid=fields.get("UID", ""),
        summary=unescape(fields.get("SUMMARY", "")),
        start=start,
        end=end,
        all_day=all_day,
        location=unescape(fields.get("LOCATION", "")),
        description=unescape(fields.get("DESCRIPTION", "")),
        calendar=fields.get("X-DAEDALUS-CALENDAR") or INTERNAL,
        authority=fields.get("X-DAEDALUS-AUTHORITY") or "internal",
        data_class=_data_class(fields.get("X-DAEDALUS-DATA-CLASS")),
        created_at=_stamp_epoch(fields.get("X-DAEDALUS-CREATED")),
        updated_at=_stamp_epoch(fields.get("LAST-MODIFIED")) or _stamp_epoch(fields.get("DTSTAMP")),
        extra=extra,
    )


def _data_class(raw: str | None) -> DataClass:
    try:
        return DataClass((raw or "private").strip())
    except ValueError:
        return DataClass.PRIVATE


def _parse_stamp(value: str) -> tuple[str, bool]:
    text = value.strip()
    if len(text) == 8 and text.isdigit():  # YYYYMMDD
        return f"{text[:4]}-{text[4:6]}-{text[6:]}", True
    bare = text.rstrip("Zz")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(bare, fmt).strftime("%Y-%m-%dT%H:%M"), False
        except ValueError:
            continue
    raise ValueError(f"unreadable date in the calendar: {value!r}")


def _stamp_epoch(value: str | None) -> float:
    if not value:
        return 0.0
    text = value.strip()
    is_utc = text.endswith(("Z", "z"))
    bare = text.rstrip("Zz")
    for fmt in ("%Y%m%dT%H%M%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            moment = datetime.strptime(bare, fmt)
        except ValueError:
            continue
        if is_utc:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return 0.0
