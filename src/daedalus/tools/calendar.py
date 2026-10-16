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

from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from ..calendar.event import normalise_when, parse_iso
from ..security.provenance import DataClass
from .base import Tool, ToolResult, params

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


class CalendarTool(Tool):
    name = "calendar"
    title = "Read or change the calendar"
    description = (
        "The owner's calendar. `agenda` shows what is coming (repeating events "
        "appear on each day they fall); `list` shows the events as defined; "
        "`busy` shows the stretches they are not free in (and anything that "
        "overlaps); `free` finds slots they are; `search` finds events by text; "
        "`get`, `create`, `update` and `delete` manage them — `update`/`delete` take "
        "one event by `uid`, or every event whose name contains `match` (with `at` to "
        "give a whole set a new time of day). Times are local: "
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
                    "what the event is, for op=create and op=update. Pass \"\" for the "
                    "read ops."
                ),
            },
            True,
        ),
        start=(
            {
                "type": "string",
                "description": (
                    "when it begins, for op=create and op=update — local "
                    "'2026-10-16T09:10', or '2026-10-16' for a whole-day event. Pass \"\" "
                    "for the read ops."
                ),
            },
            True,
        ),
        uid=(
            {
                "type": "string",
                "description": (
                    "the event to act on, for op=get/update/delete. `agenda` and `list` "
                    "print it."
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
                    "whole set's time at once."
                ),
            },
            False,
        ),
        at=(
            {
                "type": "string",
                "description": (
                    "for op=update with `match`: the time of day to set, 'HH:MM', keeping "
                    "each event's own date and length. This is what 'move all my standups "
                    "to 10' means — `start` would not do it."
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
                "description": (
                    "the words to look for, for op=search. Pass \"\" for every other op."
                ),
            },
            True,
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
        bulk = str(args.get("match", "")).strip() and not str(args.get("uid", "")).strip()
        if op == "delete":
            return Assessment(
                level=Level.CAUTION,
                summary=(
                    f"remove every event whose name matches {args.get('match')!r}"
                    if bulk
                    else "remove an event from the internal calendar"
                ),
                reasons=("it is a local store, so this only changes what Daedalus holds",),
            )
        return Assessment(
            level=Level.CAUTION,
            summary=(
                f"change every event whose name matches {args.get('match')!r}"
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
            return ToolResult.error("op=get needs `uid`; `agenda` or `list` prints it")
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
        at = str(args.get("at", "")).strip()
        if not uid and match:
            return self._bulk_update(args, ctx, match, at)
        if not uid:
            return ToolResult.error(
                "op=update needs `uid` (one event) or `match` (every event whose name "
                "contains the text); `list` prints the ids"
            )
        if match:
            return ToolResult.error("give either `uid` or `match`, not both")
        changes: dict[str, Any] = {
            key: str(args.get(key, "")).strip()
            for key in ("summary", "start", "end", "location", "description", "rrule")
        }
        if args.get("data_class"):
            changes["data_class"] = args["data_class"]
        if args.get("remind") is not None:
            changes["reminders"] = [int(args["remind"])]
        if not any(value not in ("", [], None) for value in changes.values()):
            return ToolResult.error(
                "op=update needs at least one field to change, alongside `uid` — e.g. "
                'uid="ab12cd34ef56", start="2026-10-20T10:00".'
            )
        event = ctx.calendar.update(
            uid,
            recurrence_id=str(args.get("recurrence_id", "")).strip(),
            scope=str(args.get("scope", "")).strip() or "all",
            **changes,
        )
        if event is None:
            return ToolResult.error(f"no event {uid!r}; `list` shows what exists")
        return ToolResult(output=f"updated {event.line(show_id=True)}", data={"uid": uid})

    def _bulk_update(
        self, args: dict[str, Any], ctx: "Runtime", match: str, at: str
    ) -> ToolResult:
        """One change, applied to every event whose name matches."""
        if str(args.get("start", "")).strip():
            return ToolResult.error(
                "with `match`, use `at` (a time of day) rather than `start`: the matching "
                "events fall on different days, and one shared date would pile them all "
                "onto it."
            )
        if str(args.get("recurrence_id", "")).strip():
            return ToolResult.error(
                "with `match`, a change applies to whole events — pick one instance with "
                "`uid` and `recurrence_id` instead."
            )
        changes: dict[str, Any] = {
            key: str(args.get(key, "")).strip()
            for key in ("summary", "location", "description", "rrule", "end")
        }
        if args.get("data_class"):
            changes["data_class"] = args["data_class"]
        if args.get("remind") is not None:
            changes["reminders"] = [int(args["remind"])]
        changed = ctx.calendar.bulk_update(match, at=at, **changes)
        if not changed:
            return ToolResult.error(
                f"nothing in the event names matched {match!r}; `list` shows what exists"
            )
        return ToolResult(
            output=f"changed {len(changed)}:\n"
            + "\n".join(f"- {e.line(show_id=True)}" for e in changed),
            data={"count": len(changed), "uids": [e.uid for e in changed]},
        )

    def _delete(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        uid = str(args.get("uid", "")).strip()
        match = str(args.get("match", "")).strip()
        if not uid and match:
            removed = ctx.calendar.bulk_delete(match)
            if not removed:
                return ToolResult.error(
                    f"nothing in the event names matched {match!r}; `list` shows what exists"
                )
            return ToolResult(
                output=f"removed {removed} event{'s' if removed != 1 else ''} matching {match!r}",
                data={"count": removed},
            )
        if not uid:
            return ToolResult.error(
                "op=delete needs `uid` (one event) or `match` (every event whose name "
                "contains the text); `list` prints the ids"
            )
        if not ctx.calendar.delete(
            uid,
            recurrence_id=str(args.get("recurrence_id", "")).strip(),
            scope=str(args.get("scope", "")).strip() or "all",
        ):
            return ToolResult.error(f"no event {uid!r}; nothing was removed")
        return ToolResult(output=f"removed {uid}", data={"uid": uid})
