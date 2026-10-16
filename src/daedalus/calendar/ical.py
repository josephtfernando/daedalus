"""A small iCalendar (RFC 5545) reader/writer — the ``VEVENT`` slice.

The internal calendar is file-first (SPEC §5.5): ``events.ics`` has to open in
the owner's real calendar apps, and survive being edited there. So it is
written as valid iCalendar rather than a private format, and this module is
deliberately *lossless where it must be* — property lines it does not model are
carried through verbatim (:attr:`Event.extra`), and non-``VEVENT`` components
(``VTIMEZONE``, ``VTODO``, …) are passed back out untouched.

It is not a general iCalendar implementation. It covers what the agent writes
and reads — including the parts that make a calendar *usable* rather than a
list: ``RRULE``, ``EXDATE``, ``RECURRENCE-ID`` overrides, ``TRANSP``, ``TZID``
and ``VALARM`` — and says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from ..security.provenance import DataClass
from .event import INTERNAL, OPAQUE, TRANSPARENT, Event, Reminder

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
        "RRULE",
        "EXDATE",
        "RDATE",
        "RECURRENCE-ID",
        "TRANSP",
        "DTSTAMP",
        "LAST-MODIFIED",
        "X-DAEDALUS-CALENDAR",
        "X-DAEDALUS-AUTHORITY",
        "X-DAEDALUS-DATA-CLASS",
        "X-DAEDALUS-CREATED",
    }
)

#: ``-P1DT2H30M`` and friends. The sub-daily units a person writes, no more.
_DURATION = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


@dataclass(slots=True)
class ParsedCalendar:
    events: list[Event] = field(default_factory=list)
    #: Component blocks that are not VEVENTs, kept as raw lines so a caller that
    #: rewrites the file does not delete them.
    passthrough: list[list[str]] = field(default_factory=list)
    #: The document's own top-level properties — ``X-WR-CALNAME``,
    #: ``X-WR-TIMEZONE``, ``VERSION`` and whatever else the writer put there.
    #: Read, not re-rendered: the store writes its own header.
    header: list[str] = field(default_factory=list)


def calendar_name(parsed: ParsedCalendar) -> str:
    """The name the document calls itself — ``X-WR-CALNAME:owner@gmail.com``.

    This is how a Google Calendar export says who it belongs to, and it is the
    one piece of provenance worth keeping when the owner brings a file in.
    """
    for line in parsed.header:
        name, _, value = line.partition(":")
        if name.strip().upper().endswith("X-WR-CALNAME"):
            return unescape(value).strip()
    return ""


def prodid(parsed: ParsedCalendar) -> str:
    """What wrote the document — our own ``PRODID``, or Google's.

    Used to decide whether an imported file's own provenance should be trusted
    (it is one of ours, and already says where each event belongs) or replaced
    wholesale (it is someone else's, and silence about a calendar is not a claim
    to be on ours).
    """
    for line in parsed.header:
        name, _, value = line.partition(":")
        if name.strip().upper() == "PRODID":
            return value.strip()
    return ""


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
    ]
    if event.recurrence_id:
        lines.append(_recurrence_line(event))
    lines.append(_stamp_line("DTSTART", event.start, event))
    if event.end:
        lines.append(_stamp_line("DTEND", event.end, event))
    lines.append(f"SUMMARY:{escape(event.summary)}")
    if event.location:
        lines.append(f"LOCATION:{escape(event.location)}")
    if event.description:
        lines.append(f"DESCRIPTION:{escape(event.description)}")
    if event.rrule:
        lines.append(f"RRULE:{event.rrule}")
    if event.exdates:
        lines.append(_date_list_line("EXDATE", event.exdates, event))
    if event.rdates:
        lines.append(_date_list_line("RDATE", event.rdates, event))
    lines.append(f"TRANSP:{'TRANSPARENT' if event.transparency == TRANSPARENT else 'OPAQUE'}")
    lines.append(f"X-DAEDALUS-CALENDAR:{event.calendar}")
    lines.append(f"X-DAEDALUS-AUTHORITY:{event.authority}")
    lines.append(f"X-DAEDALUS-DATA-CLASS:{event.data_class.value}")
    lines.append(f"X-DAEDALUS-CREATED:{_local_stamp(event.created_at)}")
    for reminder in event.reminders:
        lines.extend(_valarm_lines(reminder))
    lines.extend(f"{name}:{value}" for name, value in event.extra)
    lines.append(f"LAST-MODIFIED:{_utc_stamp(event.updated_at)}")
    lines.append("END:VEVENT")
    return lines


def _recurrence_line(event: Event) -> str:
    params: list[str] = []
    if event.all_day:
        params.append("VALUE=DATE")
        rendered = str(event.recurrence_id).replace("-", "")
    else:
        if event.tzid:
            params.append(f"TZID={event.tzid}")
        rendered = datetime.fromisoformat(str(event.recurrence_id)).strftime("%Y%m%dT%H%M%S")
    if event.recurrence_range:
        params.append(f"RANGE={event.recurrence_range}")
    return ";".join(["RECURRENCE-ID", *params]) + ":" + rendered


def _stamp_line(name: str, value: str, event: Event) -> str:
    params: list[str] = []
    if event.all_day:
        params.append("VALUE=DATE")
        rendered = str(value).replace("-", "")
    else:
        if event.tzid:
            params.append(f"TZID={event.tzid}")
        rendered = datetime.fromisoformat(str(value)).strftime("%Y%m%dT%H%M%S")
    if params:
        return f"{name};{';'.join(params)}:{rendered}"
    return f"{name}:{rendered}"


def _date_list_line(name: str, values: list[str], event: Event) -> str:
    """``EXDATE`` or ``RDATE``: the same shape, so the same writer."""
    if event.all_day:
        joined = ",".join(str(value).replace("-", "") for value in values)
        return f"{name};VALUE=DATE:{joined}"
    joined = ",".join(
        datetime.fromisoformat(str(value)).strftime("%Y%m%dT%H%M%S") for value in values
    )
    prefix = f"{name};TZID={event.tzid}" if event.tzid else name
    return f"{prefix}:{joined}"


def _valarm_lines(reminder: Reminder) -> list[str]:
    lines = ["BEGIN:VALARM", f"ACTION:{reminder.action}"]
    if reminder.description:
        lines.append(f"DESCRIPTION:{escape(reminder.description)}")
    lines.append(f"TRIGGER:{_trigger_text(reminder.offset_minutes)}")
    lines.extend(f"{name}:{value}" for name, value in reminder.extra)
    lines.append("END:VALARM")
    return lines


def _trigger_text(offset_minutes: int) -> str:
    """``offset_minutes`` (positive = before the start) as an RFC trigger."""
    if offset_minutes == 0:
        return "PT0M"
    if offset_minutes > 0:
        return f"-PT{offset_minutes}M"
    return f"PT{abs(offset_minutes)}M"


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
            # A top-level property: the version, the calendar's own name. Kept
            # because a caller importing a file needs to know where it came from.
            result.header.append(line)
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


def _params(text: str) -> dict[str, str]:
    """The ``;KEY=VALUE`` parameters of a content line."""
    out: dict[str, str] = {}
    for chunk in str(text).split(";"):
        if not chunk.strip():
            continue
        key, _, value = chunk.partition("=")
        out[key.strip().upper()] = value.strip().strip('"')
    return out


def _from_block(block: list[str]) -> Event | None:
    """One VEVENT from its inner lines. Nested components stay nested.

    A ``VALARM`` carries its own ``DESCRIPTION`` and ``SUMMARY``; a reader that
    lifted them would let a reminder overwrite the event it belongs to.
    """
    fields: dict[str, str] = {}
    params: dict[str, dict[str, str]] = {}
    extra: list[tuple[str, str]] = []
    alarms: list[list[str]] = []
    depth = 0
    current: list[str] = []
    for line in block:
        upper = line.upper()
        if upper.startswith("BEGIN:"):
            depth += 1
            if depth == 1:
                current = [line]
                continue
        if depth:
            current.append(line)
            if upper.startswith("END:"):
                depth -= 1
                if depth == 0:
                    alarms.append(current)
            continue
        name, _, value = line.partition(":")
        key = name.split(";", 1)[0].strip().upper()
        if key in _KNOWN:
            fields[key] = value
            params[key] = _params(name.split(";", 1)[1] if ";" in name else "")
        elif key:
            extra.append((name, value))

    start_raw = fields.get("DTSTART", "").strip()
    if not start_raw:
        return None
    start, all_day, tzid = _parse_stamp(start_raw, params.get("DTSTART"))
    event = Event(
        uid=fields.get("UID", ""),
        summary=unescape(fields.get("SUMMARY", "")),
        start=start,
        end=_parse_stamp(fields["DTEND"], params.get("DTEND"))[0] if fields.get("DTEND") else "",
        all_day=all_day,
        location=unescape(fields.get("LOCATION", "")),
        description=unescape(fields.get("DESCRIPTION", "")),
        calendar=fields.get("X-DAEDALUS-CALENDAR") or INTERNAL,
        authority=fields.get("X-DAEDALUS-AUTHORITY") or "internal",
        data_class=_data_class(fields.get("X-DAEDALUS-DATA-CLASS")),
        created_at=_stamp_epoch(fields.get("X-DAEDALUS-CREATED")),
        updated_at=_stamp_epoch(fields.get("LAST-MODIFIED")) or _stamp_epoch(fields.get("DTSTAMP")),
        rrule=fields.get("RRULE", "").strip(),
        exdates=_stamps(fields.get("EXDATE", ""), all_day),
        rdates=_stamps(fields.get("RDATE", ""), all_day),
        recurrence_id=_parse_stamp(fields["RECURRENCE-ID"], params.get("RECURRENCE-ID"))[0]
        if fields.get("RECURRENCE-ID")
        else "",
        recurrence_range=params.get("RECURRENCE-ID", {}).get("RANGE", "").upper(),
        tzid=tzid,
        transparency=TRANSPARENT if fields.get("TRANSP", "").strip().upper() == "TRANSPARENT" else OPAQUE,
        reminders=_reminders(alarms, start, all_day),
        extra=extra,
    )
    return event


def _reminders(alarms: list[list[str]], start: str, all_day: bool) -> list[Reminder]:
    out: list[Reminder] = []
    for block in alarms:
        action = "DISPLAY"
        description = ""
        trigger: str | None = None
        trigger_params: dict[str, str] = {}
        extra: list[tuple[str, str]] = []
        for line in block[1:-1]:
            name, _, value = line.partition(":")
            key = name.split(";", 1)[0].strip().upper()
            if key == "ACTION":
                action = value.strip().upper() or "DISPLAY"
            elif key == "DESCRIPTION":
                description = unescape(value)
            elif key == "TRIGGER":
                trigger = value.strip()
                trigger_params = _params(name.split(";", 1)[1] if ";" in name else "")
            elif key and key != "BEGIN" and key != "END":
                extra.append((name, value))
        if trigger is None:
            continue
        offset = _trigger_offset(trigger, trigger_params, start, all_day)
        if offset is None:
            continue
        out.append(Reminder(offset, action=action, description=description, extra=tuple(extra)))
    return out


def _trigger_offset(
    trigger: str, params: dict[str, str], start: str, all_day: bool
) -> int | None:
    """Minutes *before* the start, from a relative or absolute trigger."""
    if params.get("VALUE", "").upper() == "DATE-TIME" or _looks_like_stamp(trigger):
        moment = _parse_stamp(trigger, params)[0]
        delta = datetime.fromisoformat(start) - datetime.fromisoformat(moment)
        return int(round(delta.total_seconds() / 60))
    match = _DURATION.match(trigger.strip().upper())
    if match is None:
        return None
    parts = match.groupdict()
    total = (
        int(parts["weeks"] or 0) * 7 * 1440
        + int(parts["days"] or 0) * 1440
        + int(parts["hours"] or 0) * 60
        + int(parts["minutes"] or 0)
    )
    total += int(round(int(parts["seconds"] or 0) / 60))
    if parts["sign"] == "-":
        return total  # a negative duration leads the event
    return -total


def _looks_like_stamp(text: str) -> bool:
    stripped = text.strip()
    return len(stripped) >= 8 and stripped[:8].isdigit()


def _data_class(raw: str | None) -> DataClass:
    try:
        return DataClass((raw or "private").strip())
    except ValueError:
        return DataClass.PRIVATE


def _stamps(value: str, all_day: bool) -> list[str]:
    """A comma-separated ``EXDATE``/``RDATE`` list, in the stored shape."""
    out: list[str] = []
    for token in str(value).split(","):
        if not token.strip():
            continue
        moment, _flag, _tz = _parse_stamp(token)
        out.append(moment)
    return out


def _parse_stamp(value: str, params: dict[str, str] | None = None) -> tuple[str, bool, str]:
    """``(iso, all_day, tzid)`` for a ``DTSTART``/``DTEND``/``EXDATE`` value."""
    text = value.strip()
    params = params or {}
    if len(text) == 8 and text.isdigit():  # YYYYMMDD
        return f"{text[:4]}-{text[4:6]}-{text[6:]}", True, ""
    is_utc = text.endswith(("Z", "z"))
    bare = text.rstrip("Zz")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            moment = datetime.strptime(bare, fmt)
        except ValueError:
            continue
        tzid = str(params.get("TZID") or "").strip()
        if not tzid and is_utc:
            tzid = "UTC"
        return moment.strftime("%Y-%m-%dT%H:%M"), False, tzid
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
