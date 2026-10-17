"""``calendar`` primitive — the internal calendar (SPEC §5.5).

Reads are self-contained and writes stay on this machine, so the whole tool is
``auto``: this is the agent's own store, not an egress into someone else's
calendar. Reaching gcal is a separate, gated path that is not built yet.

The tool is deliberately the *whole* surface. A recurring event, a single moved
instance, a reminder and a one-off are the same primitive with different
arguments, because the model should not have to know which of five tools
"standup every Monday except next week" belongs to.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from ..calendar.event import normalise_when, parse_iso
from ..security.provenance import DataClass
from .base import Tool, ToolResult, params, sent_args

if TYPE_CHECKING:
    from ..runtime import Runtime

_RRULE_HELP = (
    "how it repeats, e.g. 'FREQ=WEEKLY;BYDAY=MO,WE,FR' for weekdays, "
    "'FREQ=DAILY', 'FREQ=MONTHLY;BYDAY=2TU' for the second Tuesday. Add "
    "';UNTIL=20261231T235959' or ';COUNT=10' to stop. Empty for a one-off."
)
_SCOPE_HELP = (
    "for a repeating event, what a change means: 'all' (the whole series, the "
    "default), 'this' (only the instance named by recurrence_id), 'future' "
    "(that instance and every one after it)."
)


def _window(args: dict[str, Any], *, default_days: int) -> tuple[datetime, datetime]:
    """The span a `busy`/`free` question is about, from ``start`` and ``days``.

    ``start`` is the day (or moment) to look from — today when it is empty, so
    "when am I free" needs no date.
    """
    raw = str(args.get("start", "")).strip()
    begin = parse_iso(normalise_when(raw)[0]) if raw else datetime.now()
    begin = datetime.combine(begin.date(), datetime.min.time())
    days = max(1, int(args.get("days") or default_days))
    return begin, begin + timedelta(days=days)


def _hours(value: Any) -> tuple[str, str]:
    """``"09:30-17:00"`` as a pair of clocks; ``"any"`` opens the whole day."""
    text = str(value or "").strip().lower()
    if text in ("any", "all", "anytime"):
        return "00:00", "23:59"
    if "-" in text:
        opens, _, closes = text.partition("-")
        return opens.strip(), closes.strip()
    return "09:00", "18:00"


def _slot(start: datetime, end: datetime) -> str:
    """A block of time, e.g. ``Fri 16 Oct 09:00–10:30``."""
    if start.date() == end.date():
        return f"{start:%a %d %b %H:%M}–{end:%H:%M}"
    return f"{start:%a %d %b %H:%M} – {end:%a %d %b %H:%M}"


def _set_phrase(match: str, on: str) -> str:
    """What a bulk target *is*, in the owner's words, for the approval prompt."""
    if match and on:
        return f"whose name matches {match!r} on {on}"
    if on:
        return f"on {on}"
    return f"whose name matches {match!r}"


def _scope(args: dict[str, Any]) -> str:
    """What a change to a repeating event means (SPEC §5.5).

    Naming one instance — `recurrence_id`, the address `agenda` prints after the
    ``@`` — while leaving the scope unset means **that instance**: the address
    already says which one, and defaulting to the series would turn "drop
    today's standup" into "drop the standup forever". A scope of `all` alongside
    an instance is a contradiction, so it is refused rather than resolved by
    quietly ignoring half of what was asked.
    """
    explicit = str(args.get("scope", "")).strip()
    named = str(args.get("recurrence_id", "")).strip()
    if explicit:
        if explicit == "all" and named:
            raise ValueError(
                "scope=all means the whole series, but `recurrence_id` names one "
                "instance — drop `recurrence_id` to change the series, or drop "
                "`scope` to change just that instance"
            )
        return explicit
    return "this" if named else "all"


def _sent(args: dict[str, Any]) -> str:
    """The argument names a call actually carried, so an error can echo them.

    Shared with ``schedule`` — a refusal that says only what is missing is how
    the same wrong key gets sent four times.
    """
    return sent_args(args)


def _as_int(value: Any, default: int) -> int:
    """An integer from a model-supplied value, without raising on nonsense.

    The schema says ``days`` is an integer, but a model may send ``"7"`` — and a
    refusal that dies on ``int("next week")`` would replace one bad turn with a
    crashed one.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _named(args: dict[str, Any]) -> tuple[str, str]:
    """The text a call used to *name* an event, and the field it came in on.

    ``summary`` is the name a write **sets**; a model reaching for "the event
    called College" fills it in anyway, as if naming the event were the same
    thing as addressing it. ``query`` is the same mistake made through
    ``search``'s field. Both are read here so a refusal can answer the question
    the model was actually asking — *which* event — instead of only reporting
    what was missing, which is what leaves it repeating the same wrong call.
    """
    summary = str(args.get("summary", "")).strip()
    if summary:
        return summary, "summary"
    query = str(args.get("query", "")).strip()
    if query:
        return query, "query"
    return "", ""


#: The fields that *describe* an event rather than *choose* one. A write sets
#: them; none of them can address an event, however much a call wants one to.
_VALUE_FIELDS = (
    "summary",
    "start",
    "end",
    "location",
    "description",
    "rrule",
    "remind",
    "at",
    "shift",
    "query",
)


def _misused(args: dict[str, Any]) -> list[str]:
    """The *value* fields a call filled in as though they could choose an event.

    A model reaching for an event it can name but not address fills every value
    field it has: on 17 Oct one `delete` carried ``summary``, ``start`` **and**
    ``query="delete"``. A refusal that names only the first leaves it holding two
    more things it still believes are addresses.
    """
    return [f for f in _VALUE_FIELDS if str(args.get(f, "")).strip() not in ("", "None")]


def _phrase(names: list[str]) -> str:
    """``x``; ``x`` and ``y``; ``x``, ``y`` and ``z`` — a list the ear can follow."""
    quoted = [f"`{name}`" for name in names]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + f" and {quoted[-1]}"


def _suggested(op: str, uid: str) -> str:
    """The literal call that would work, in the model's own vocabulary.

    Describing the right call leaves the model to translate it — and its own
    wrong call is sitting in the context, one message back, to be copied
    instead. That is what happened four times. Handing it a line to copy gives
    it something else to copy.
    """
    if op == "get":
        return f"op=get uid={uid}"
    if op == "delete":
        return f"op=delete uid={uid}"
    return f"op=update uid={uid} <the fields to change>"


def _looks_like_day(text: str) -> bool:
    """Whether a model-supplied string is a bare ``YYYY-MM-DD`` — not the hour."""
    return len(text) == 10 and text[4] == "-" and text[7] == "-" and text.replace("-", "").isdigit()


class CalendarTool(Tool):
    name = "calendar"
    title = "Read or change the calendar"
    description = (
        "The owner's calendar. `agenda` shows what is coming (repeating events "
        "appear on each day they fall); `list` shows the events as defined; "
        "`busy` shows the stretches they are not free in (and anything that "
        "overlaps); `free` finds slots they are; `search` finds events by text; "
        "`get`, `create`, `update` and `delete` manage them. "
        "**Each op takes its own fields — send only those, and omit the rest.** "
        "`agenda` takes `days`; `list` takes `limit`; `get` takes `uid`; `busy` takes "
        "`days`; `free` takes `days`, `minutes`, `hours`; `search` takes `query`; "
        "`create` takes `summary` and `start` (plus any of `end`, `location`, "
        "`description`, `rrule`, `remind`); `update` takes a target *and* the fields to "
        "change; `delete` takes a target and nothing else. A field that belongs to "
        "another op is not a weaker way to say it — it is not read. "
        "**To change an existing event, `get`/`update`/`delete` must be told which "
        "one**: either `uid` (one event — `agenda`, `list` and `search` print it) or "
        "`match`/`on` (a set: every event whose name contains `match`, and/or every "
        "event that falls on the day `on`, with `at` to give a set a new time of day "
        "or `shift` to move a set by a date delta such as '-7d'). `summary` and `start` "
        "are what a write *sets* — neither can select an event, however much a call "
        "wants one. "
        "Times are local: "
        "'2026-10-16T09:10', or '2026-10-16' for a whole-day event. A repeating "
        "event is one event with an rrule, not many copies."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["state"]}
    parameters = params(
        op=(
            {
                "type": "string",
                "enum": [
                    "agenda", "list", "get", "busy", "free", "search",
                    "create", "update", "delete",
                ],
            },
            True,
        ),
        summary=(
            {
                "type": "string",
                "description": (
                    "for op=create (required) and op=update: the event's name — the title "
                    "a write *sets*. It can never select which event to change: for "
                    "op=update/delete, use `uid` or `match` for that."
                ),
            },
            False,
        ),
        start=(
            {
                "type": "string",
                "description": (
                    "for op=create (required) and op=update: when it begins, local "
                    "'2026-10-16T09:10', or '2026-10-16' for a whole-day event. It is "
                    "not a way to choose an event — to pick one by its time, use `on` "
                    "(the day). For op=update with `match`/`on`, use `at` or `shift`."
                ),
            },
            False,
        ),
        uid=(
            {
                "type": "string",
                "description": (
                    "the event to act on, for op=get/update/delete. `agenda`, `list` and "
                    "`search` print it. Required whenever one of those ops names an event "
                    "— `match`/`on` is the alternative for a set."
                ),
            },
            False,
        ),
        match=(
            {
                "type": "string",
                "description": (
                    "for op=update/delete *instead of* `uid`: act on every event whose "
                    "name contains this text, e.g. 'standup'. Use with `at` to change a "
                    "whole set's time, `shift` to move a set by days, or `on` to narrow "
                    "it to a day."
                ),
            },
            False,
        ),
        on=(
            {
                "type": "string",
                "description": (
                    "for op=update/delete: the day to act on, 'YYYY-MM-DD'. On its own it "
                    "means every event that falls on that day; with `match` it narrows the "
                    "set to events that day. This is 'move the two things on Sunday'."
                ),
            },
            False,
        ),
        at=(
            {
                "type": "string",
                "description": (
                    "for op=update with `match`/`on`: the time of day to set, 'HH:MM', keeping "
                    "each event's own date and length. This is what 'move all my standups "
                    "to 10' means — `start` would not do it."
                ),
            },
            False,
        ),
        shift=(
            {
                "type": "string",
                "description": (
                    "for op=update: move the event (or the `match`/`on` set) by a signed day or "
                    "week offset, e.g. '-7d' (a week earlier) or '3d'. Each keeps its own time "
                    "of day and length. This is what a date move needs — `start` names one "
                    "shared date, which would pile a set onto it."
                ),
            },
            False,
        ),
        end=({"type": "string", "description": "when it ends, optional; same shape as `start`."}, False),
        location=({"type": "string"}, False),
        description=({"type": "string"}, False),
        rrule=({"type": "string", "description": _RRULE_HELP}, False),
        remind=(
            {
                "type": "integer",
                "description": (
                    "for op=create/update: minutes before the start to nudge the owner. "
                    "10 means ten minutes before."
                ),
            },
            False,
        ),
        recurrence_id=(
            {
                "type": "string",
                "description": (
                    "which instance of a repeating event, for op=update/delete — the "
                    "instance's own start, as `agenda` prints it after the '@'."
                ),
            },
            False,
        ),
        scope=({"type": "string", "enum": ["all", "this", "future"], "description": _SCOPE_HELP}, False),
        query=(
            {
                "type": "string",
                "description": "for op=search (required): the words to look for.",
            },
            False,
        ),
        minutes=(
            {
                "type": "integer",
                "default": 45,
                "description": "for op=free: how long a gap the owner needs.",
            },
            False,
        ),
        hours=(
            {
                "type": "string",
                "description": (
                    "for op=free: the part of the day to consider, 'HH:MM-HH:MM' "
                    "(default '09:00-18:00'). Pass 'any' to consider the whole day."
                ),
            },
            False,
        ),
        days=(
            {
                "type": "integer",
                "default": 7,
                "description": "for op=agenda/busy/free: how many days ahead to look.",
            },
            False,
        ),
        limit=({"type": "integer", "default": 20}, False),
        data_class=(
            {
                "type": "string",
                "enum": ["public", "private", "local_only"],
                "description": (
                    "how sensitive this event is. 'local_only' keeps it out of any prompt "
                    "that could reach a remote model."
                ),
            },
            False,
        ),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", ""))
        if op in ("agenda", "list", "get", "busy", "free", "search"):
            return Assessment(level=Level.OK, summary="read the calendar")
        # A bulk write is one more thing than it looks: the prompt should say how
        # many events are about to move, in the owner's own words.
        uid = str(args.get("uid", "")).strip()
        match = str(args.get("match", "")).strip()
        on = str(args.get("on", "")).strip()
        bulk = not uid and (match or on)
        target = _set_phrase(match, on)
        if op == "delete":
            return Assessment(
                level=Level.CAUTION,
                summary=(
                    f"remove every event {target}"
                    if bulk
                    else "remove an event from the internal calendar"
                ),
                reasons=("it is a local store, so this only changes what Daedalus holds",),
            )
        return Assessment(
            level=Level.CAUTION,
            summary=(
                f"change every event {target}"
                if bulk
                else f"change the internal calendar ({op})"
            ),
        )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        handler = {
            "agenda": self._agenda,
            "list": self._list,
            "get": self._get,
            "busy": self._busy,
            "free": self._free,
            "search": self._search,
            "create": self._create,
            "update": self._update,
            "delete": self._delete,
        }.get(op)
        if handler is None:
            return ToolResult.error(f"unknown op {op!r}")
        try:
            return handler(args, ctx)
        except ValueError as exc:
            # An unreadable time, an unreadable rule, a file the owner
            # hand-edited into something invalid: report it rather than guess.
            return ToolResult.error(str(exc))

    # -- reads -------------------------------------------------------------

    def _agenda(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        days = int(args.get("days") or 7)
        events = ctx.calendar.agenda(
            days=days,
            limit=int(args.get("limit") or 20),
            allow_local_only=ctx.allows_local_only(),
        )
        if not events:
            return ToolResult(
                output=f"(nothing scheduled in the next {days} days)", data={"count": 0}
            )
        return ToolResult(
            output="\n".join(f"- {e.line(show_id=True)}" for e in events),
            data={"count": len(events)},
        )

    def _list(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        events = ctx.calendar.series(
            limit=int(args.get("limit") or 100), allow_local_only=ctx.allows_local_only()
        )
        if not events:
            return ToolResult(output="(the calendar is empty)", data={"count": 0})
        return ToolResult(
            output="\n".join(f"- {e.line(show_id=True)}" for e in events),
            data={"count": len(events)},
        )

    def _get(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        uid = str(args.get("uid", "")).strip()
        if not uid:
            return ToolResult.error(self._no_target(args, ctx, "get"))
        event = ctx.calendar.get(uid)
        if event is None:
            return ToolResult.error(f"no event {uid!r}; `list` shows what exists")
        lines = [event.line(show_id=True)]
        moved = ctx.calendar.overrides(uid)
        if moved:
            lines.append("moved instances:")
            lines.extend(f"- {e.line(show_id=True)}" for e in moved)
        return ToolResult(output="\n".join(lines), data={"uid": uid})

    def _busy(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        start, end = _window(args, default_days=1)
        allow = ctx.allows_local_only()
        blocks = ctx.calendar.busy(start, end, allow_local_only=allow)
        clashes = ctx.calendar.conflicts(start, end, allow_local_only=allow)
        if not blocks:
            return ToolResult(output="(nothing booked — the owner is free)", data={"blocks": 0})
        lines = [f"- {_slot(begin, finish)}" for begin, finish in blocks]
        if clashes:
            lines.append("double-booked:")
            lines.extend(f"- {a.line()}  ×  {b.line()}" for a, b in clashes)
        return ToolResult(
            output="\n".join(lines),
            data={"blocks": len(blocks), "conflicts": len(clashes)},
        )

    def _free(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        start, end = _window(args, default_days=1)
        minutes = int(args.get("minutes") or 45)
        gaps = ctx.calendar.free(
            start,
            end,
            minutes=minutes,
            hours=_hours(args.get("hours")),
            allow_local_only=ctx.allows_local_only(),
        )
        if not gaps:
            return ToolResult(
                output=f"(no {minutes}-minute gap between {start:%a %d %b} and {end:%a %d %b})",
                data={"count": 0},
            )
        return ToolResult(
            output="\n".join(f"- {_slot(begin, finish)}" for begin, finish in gaps),
            data={"count": len(gaps)},
        )

    def _search(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        query = str(args.get("query", "")).strip() or str(args.get("summary", "")).strip()
        if not query:
            return ToolResult.error("op=search needs `query` — the words to look for")
        rows = ctx.calendar.search(
            query,
            limit=int(args.get("limit") or 20),
            allow_local_only=ctx.allows_local_only(),
        )
        if not rows:
            return ToolResult(output=f"(nothing matching {query!r})", data={"count": 0})
        return ToolResult(
            output="\n".join(f"- {e.line(show_id=True)}" for e in rows),
            data={"count": len(rows)},
        )

    # -- writes ------------------------------------------------------------

    def _create(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        summary = str(args.get("summary", "")).strip()
        start = str(args.get("start", "")).strip()
        if not summary or not start:
            return ToolResult.error(
                "op=create needs `summary` (what it is) and `start` (when) — e.g. "
                'summary="Dentist", start="2026-10-20T09:00".'
            )
        remind = args.get("remind")
        event = ctx.calendar.add(
            summary=summary,
            start=start,
            end=str(args.get("end", "")).strip(),
            rrule=str(args.get("rrule", "")).strip(),
            reminders=[int(remind)] if remind else [],
            location=str(args.get("location", "")).strip(),
            description=str(args.get("description", "")).strip(),
            data_class=DataClass(args.get("data_class", "private")),
        )
        return ToolResult(
            output=f"added [{event.uid}] {event.line()}",
            data={"uid": event.uid, "start": event.start},
        )

    def _update(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        uid = str(args.get("uid", "")).strip()
        match = str(args.get("match", "")).strip()
        on = str(args.get("on", "")).strip()
        at = str(args.get("at", "")).strip()
        if uid and (match or on):
            return ToolResult.error(
                "give either `uid` (one event) or `match`/`on` (a set), not both. "
                f"You sent: {_sent(args)}"
            )
        if not uid and (match or on):
            return self._bulk_update(args, ctx, match, on, at)
        if not uid:
            return ToolResult.error(self._no_target(args, ctx, "update"))
        changes: dict[str, Any] = {
            key: str(args.get(key, "")).strip()
            for key in ("summary", "start", "end", "location", "description", "rrule")
        }
        # A delta works on one event too: `uid` + `shift` moves it by days and
        # keeps its clock, exactly as the matched form does for a set.
        shift = str(args.get("shift", "")).strip()
        if shift:
            changes["shift"] = shift
        if args.get("data_class"):
            changes["data_class"] = args["data_class"]
        if args.get("remind") is not None:
            changes["reminders"] = [int(args["remind"])]
        if not any(value not in ("", [], None) for value in changes.values()):
            return ToolResult.error(
                "op=update needs at least one field to change, alongside `uid` — e.g. "
                'uid="ab12cd34ef56", start="2026-10-20T10:00". '
                f"You sent: {_sent(args)}"
            )
        event = ctx.calendar.update(
            uid,
            recurrence_id=str(args.get("recurrence_id", "")).strip(),
            scope=_scope(args),
            **changes,
        )
        if event is None:
            return ToolResult.error(f"no event {uid!r}; `list` shows what exists")
        return ToolResult(output=f"updated {event.line(show_id=True)}", data={"uid": uid})

    def _bulk_update(
        self, args: dict[str, Any], ctx: "Runtime", match: str, on: str, at: str
    ) -> ToolResult:
        """One change, applied to every event that matches and/or falls on a day."""
        if str(args.get("start", "")).strip():
            return ToolResult.error(
                "with `match`/`on`, use `at` (a time of day) or `shift` (a day offset) "
                "rather than `start`: the matching events fall on different days, and "
                "one shared date would pile them all onto it. "
                f"You sent: {_sent(args)}"
            )
        if str(args.get("recurrence_id", "")).strip():
            return ToolResult.error(
                "with `match`/`on`, a change applies to whole events — pick one instance "
                "with `uid` and `recurrence_id` instead. "
                f"You sent: {_sent(args)}"
            )
        changes: dict[str, Any] = {
            key: str(args.get(key, "")).strip()
            for key in ("summary", "location", "description", "rrule", "end")
        }
        shift = str(args.get("shift", "")).strip()
        if shift:
            changes["shift"] = shift
        if args.get("data_class"):
            changes["data_class"] = args["data_class"]
        if args.get("remind") is not None:
            changes["reminders"] = [int(args["remind"])]
        changed = ctx.calendar.bulk_update(
            match, at=at, on=on, allow_local_only=ctx.allows_local_only(), **changes
        )
        if not changed:
            return ToolResult.error(self._no_match(match, on, ctx))
        return ToolResult(
            output=f"changed {len(changed)}:\n"
            + "\n".join(f"- {e.line(show_id=True)}" for e in changed),
            data={"count": len(changed), "uids": [e.uid for e in changed]},
        )

    def _no_target(self, args: dict[str, Any], ctx: "Runtime", op: str) -> str:
        """A call that named no event — answered with the call that would.

        `summary` is the name a write *sets*; it is not the address a write goes
        to, and `get`/`update`/`delete` take `uid`, or `match`/`on` for a set.
        Saying only *"needs a target"* cost the owner four turns in one evening:
        the model held the event's name — and, from a `search` that had just
        succeeded, its id — and still could not see how to point at it, so it
        re-sent the same call until the repeat guard ended the turn.

        So the refusal does three things instead: it restates the rule, it names
        every value field the call misused as an address, and it hands back the
        calls and ids that answer the question the model was actually asking. A
        `get` carrying nothing usable is answered the same way — with the op that
        was probably meant, because "what is coming in the next 7 days" is
        `agenda`, not `get`.
        """
        named, _field = _named(args)
        if not named:
            return self._no_name(args, op)
        said = _phrase(_misused(args))
        verb = "reads" if op == "get" else "changes"
        head = f"op={op} {verb} one event, and {said} cannot say which"
        rows = ctx.calendar.matching(
            named, limit=8, allow_local_only=ctx.allows_local_only()
        )
        quoted = json.dumps(named)
        if not rows:
            others = ctx.calendar.series(limit=8, allow_local_only=ctx.allows_local_only())
            if not others:
                return (
                    f"{head}: they are what a write *sets*, not the address it writes "
                    f"to. Nothing here is called {quoted}, and the calendar is empty."
                )
            listing = "\n".join(f"- {e.line(show_id=True)}" for e in others)
            return (
                f"{head}: they are what a write *sets*, not the address it writes to. "
                f"Nothing here is called {quoted}. What exists:\n{listing}"
            )
        listing = "\n".join(f"- {e.line(show_id=True)}" for e in rows)
        # Every form that would name it, spelled out. The model's wrong call is
        # one message back in its own context; the only way to win that contest
        # is to put the right call there instead, ready to copy.
        choices = [f"    {_suggested(op, rows[0].uid)}"]
        day = str(args.get("start", "")).strip()[:10]
        if _looks_like_day(day):
            choices.append(f'    op={op} on="{day}"')
        choices.append(f"    op={op} match={quoted}")
        return (
            f"{head}: they are what a write *sets*, not the address it writes to. "
            f"Any of these would name it:\n"
            + "\n".join(choices)
            + f"\nMatching events:\n{listing}"
        )

    def _no_name(self, args: dict[str, Any], op: str) -> str:
        """A call that named no event *and* offered nothing that could name one.

        Nothing here to hand back, so the useful answer is the op that was
        probably meant, plus the rule the call broke. A `get` carrying a window
        — `days`, or a `start` — is a read that picked the wrong op; "what is
        coming in the next 7 days" is `agenda`, and saying so is cheaper than
        the four turns it cost when the refusal only reported that `uid` was
        missing. A `delete` carrying a `start` is the same mistake on the write
        side: an hour is when an event is, not how to pick one.
        """
        if op == "get":
            if str(args.get("start", "")).strip() or args.get("days"):
                wanted = _as_int(args.get("days"), 7)
                return (
                    "op=get needs `uid` — one event, as `agenda` or `list` prints it. "
                    f'For "what is coming", that is `op=agenda days={wanted}`; for a word, '
                    '`op=search query="…"`. Both print the `uid` this op needs.'
                )
            return (
                "op=get needs `uid` — one event, as `agenda` or `list` prints it. "
                f'To find one: `op=list`, or `op=search query="…"`. You sent: {_sent(args)}'
            )
        head = (
            "op=delete needs a target and nothing else"
            if op == "delete"
            else f"op={op} needs a target"
        )
        lines = [
            f"{head}: `uid` (one event, as `list` prints it), or `match`/`on` for a set "
            '— `on="2026-10-25"` is a day, `shift="-7d"` moves the set by a week.'
        ]
        said = _misused(args)
        if said:
            lines.append(
                f"{_phrase(said)} cannot choose an event: they are what a write *sets*, "
                "not the address it writes to."
            )
        day = str(args.get("start", "")).strip()[:10]
        if _looks_like_day(day):
            lines.append(
                f'To choose by *time*, use `on` — the day, not the hour: `op={op} on="{day}"`.'
            )
        lines.append(f"You sent: {_sent(args)}")
        return " ".join(lines)

    def _no_match(self, match: str, on: str, ctx: "Runtime") -> str:
        """Say what did not match *and* hand back what is there, in one call.

        "Nothing matched" plus a second round trip to `list` is a turn spent on
        bookkeeping; the ids the model needs are one line away, so they travel
        with the refusal.
        """
        if match and on:
            said = f"nothing on {on} has a name containing {match!r}"
        elif on:
            said = f"nothing falls on {on}"
        else:
            said = f"nothing has a name containing {match!r}"
        rows = ctx.calendar.series(limit=8, allow_local_only=ctx.allows_local_only())
        if not rows:
            return f"{said}, and the calendar is empty"
        listing = "\n".join(f"- {e.line(show_id=True)}" for e in rows)
        return f"{said}. What exists:\n{listing}"

    def _delete(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        uid = str(args.get("uid", "")).strip()
        match = str(args.get("match", "")).strip()
        on = str(args.get("on", "")).strip()
        if uid and (match or on):
            return ToolResult.error(
                "give either `uid` (one event) or `match`/`on` (a set), not both. "
                f"You sent: {_sent(args)}"
            )
        if not uid and (match or on):
            removed = ctx.calendar.bulk_delete(
                match, on=on, allow_local_only=ctx.allows_local_only()
            )
            if not removed:
                return ToolResult.error(self._no_match(match, on, ctx))
            return ToolResult(
                output=f"removed {removed} event{'s' if removed != 1 else ''} {_set_phrase(match, on)}",
                data={"count": removed},
            )
        if not uid:
            return ToolResult.error(self._no_target(args, ctx, "delete"))
        if not ctx.calendar.delete(
            uid,
            recurrence_id=str(args.get("recurrence_id", "")).strip(),
            scope=_scope(args),
        ):
            return ToolResult.error(f"no event {uid!r}; nothing was removed")
        return ToolResult(output=f"removed {uid}", data={"uid": uid})
