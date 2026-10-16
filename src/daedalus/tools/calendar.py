"""``calendar`` primitive — the internal calendar (SPEC §5.5).

Reads are self-contained and writes stay on this machine, so the whole tool is
``auto``: this is the agent's own store, not an egress into someone else's
calendar. Reaching gcal is a separate, gated path that is not built yet.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from ..security.provenance import DataClass
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime


class CalendarTool(Tool):
    name = "calendar"
    title = "Read or change the calendar"
    description = (
        "The owner's calendar. `agenda` and `list` read it; `create`, `update` and "
        "`delete` change the Daedalus calendar, the one this agent owns. Times are "
        "local: '2026-10-16T09:10', or '2026-10-16' for a whole-day event."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["state"]}
    parameters = params(
        op=(
            {"type": "string", "enum": ["agenda", "list", "get", "create", "update", "delete"]},
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
            # An unreadable time, an empty title, a file the owner hand-edited
            # into something invalid: report it rather than guess.
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
        events = ctx.calendar.all(
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
        return ToolResult(output=event.line(show_id=True), data={"uid": uid})

    # -- writes ------------------------------------------------------------

    def _create(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        summary = str(args.get("summary", "")).strip()
        start = str(args.get("start", "")).strip()
        if not summary or not start:
            return ToolResult.error(
                "op=create needs `summary` (what it is) and `start` (when) — e.g. "
                'summary="Dentist", start="2026-10-20T09:00".'
            )
        event = ctx.calendar.add(
            summary=summary,
            start=start,
            end=str(args.get("end", "")).strip(),
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
            for key in ("summary", "start", "end", "location", "description")
        }
        if args.get("data_class"):
            changes["data_class"] = args["data_class"]
        if not any(changes.values()):
            return ToolResult.error(
                "op=update needs at least one field to change, alongside `uid` — e.g. "
                'uid="ab12cd34ef56", start="2026-10-20T10:00".'
            )
        event = ctx.calendar.update(uid, **changes)
        if event is None:
            return ToolResult.error(f"no event {uid!r}; `list` shows what exists")
        return ToolResult(output=f"updated [{event.uid}] {event.line()}", data={"uid": uid})

    def _delete(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        uid = str(args.get("uid", "")).strip()
        if not uid:
            return ToolResult.error("op=delete needs `uid`; `agenda` or `list` prints it")
        if not ctx.calendar.delete(uid):
            return ToolResult.error(f"no event {uid!r}; nothing was removed")
        return ToolResult(output=f"removed {uid}", data={"uid": uid})
