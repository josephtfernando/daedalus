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

from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
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


class CalendarTool(Tool):
    name = "calendar"
    title = "Read or change the calendar"
    description = (
        "The owner's calendar. `agenda` shows what is coming (repeating events "
        "appear on each day they fall); `list` shows the events as defined; "
        "`get`, `create`, `update` and `delete` manage them. Times are local: "
        "'2026-10-16T09:10', or '2026-10-16' for a whole-day event. A repeating "
        "event is one event with an rrule, not many copies."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["state"]}
    parameters = params(
        op=(
            {
                "type": "string",
                "enum": ["agenda", "list", "get", "create", "update", "delete"],
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
        days=(
            {"type": "integer", "default": 7, "description": "for op=agenda: how many days ahead to look."},
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
        if op in ("agenda", "list", "get"):
            return Assessment(level=Level.OK, summary="read the calendar")
        if op == "delete":
            return Assessment(
                level=Level.CAUTION,
                summary="remove an event from the internal calendar",
                reasons=("it is a local store, so this only changes what Daedalus holds",),
            )
        return Assessment(level=Level.CAUTION, summary=f"change the internal calendar ({op})")

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        handler = {
            "agenda": self._agenda,
            "list": self._list,
            "get": self._get,
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
        if not uid:
            return ToolResult.error("op=update needs `uid`; `agenda` or `list` prints it")
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

    def _delete(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        uid = str(args.get("uid", "")).strip()
        if not uid:
            return ToolResult.error("op=delete needs `uid`; `agenda` or `list` prints it")
        if not ctx.calendar.delete(
            uid,
            recurrence_id=str(args.get("recurrence_id", "")).strip(),
            scope=str(args.get("scope", "")).strip() or "all",
        ):
            return ToolResult.error(f"no event {uid!r}; nothing was removed")
        return ToolResult(output=f"removed {uid}", data={"uid": uid})
