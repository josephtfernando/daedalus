"""``remind`` primitive — one-shot wakes (T42).

The tool that the 22:52 turn needed and could not find: a reminder for one
named moment. ``schedule`` is for what *recurs*; this is for what happens
once. ``in`` ("2h") or ``at`` ("2026-10-19T14:00"), exactly one, plus a note —
the wake prompt the agent composes from when the moment arrives.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from ..scheduler import catchup
from .base import Tool, ToolResult, params, sent_args

if TYPE_CHECKING:
    from ..runtime import Runtime


class RemindTool(Tool):
    name = "remind"
    title = "Set a one-shot reminder"
    description = (
        "Wake yourself up once, at a named moment, to say something the owner asked for "
        "or would otherwise miss. `in` ('2h') or `at` ('2026-10-19T14:00') — exactly one — "
        "plus `note`, the prompt you will answer from when the moment comes. Recurring "
        "work belongs to `schedule`, not here; a reminder fires once and then is gone."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["state"]}
    parameters = params(
        op=({"type": "string", "enum": ["add", "list", "remove"]}, True),
        name=({"type": "string", "description": "the reminder's handle — required with op=add and op=remove"}, False),
        within=(
            {
                "type": "string",
                "description": (
                    "how long from now, e.g. '2h', '45m', '1d' — one of `within`/`at` is "
                    "required for op=add"
                ),
            },
            False,
        ),
        at=(
            {
                "type": "string",
                "description": (
                    "the moment itself, ISO with a time, e.g. '2026-10-19T14:00' (local time) — "
                    "one of `within`/`at` is required for op=add"
                ),
            },
            False,
        ),
        note=(
            {
                "type": "string",
                "description": "the wake prompt — what you should say or do when the moment arrives",
            },
            False,
        ),
        late_within=(
            {
                "type": "string",
                "description": (
                    "NOT the delay — a delivered-late reminder past this window announces "
                    "its lateness (e.g. '45m'). Defaults to 1h."
                ),
            },
            False,
        ),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", ""))
        name = str(args.get("name", "")) or "a reminder"
        if op == "list":
            return Assessment(level=Level.OK, summary="list the reminders")
        if op == "remove":
            return Assessment(
                level=Level.DESTRUCTIVE,
                summary=f"remove the reminder {name!r}",
                reasons=("the moment will arrive and nobody will speak",),
            )
        return Assessment(level=Level.CAUTION, summary=f"add the reminder {name!r}")

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        if op == "add":
            return self._add(args, ctx)
        if op == "list":
            rows = [
                f"{j.name}\tdue={j.next_run_text()}"
                + (f"\tlate_within={j.late_within}" if j.late_within else "")
                for j in ctx.scheduler.jobs()
                if j.kind == "remind"
            ]
            return ToolResult(output="\n".join(rows) or "(no reminders)")
        if op == "remove":
            name = str(args.get("name", "")).strip()
            ok = ctx.scheduler.remove_job(name)
            return ToolResult(ok=ok, output=f"removed {name!r}" if ok else f"no reminder {name!r}")
        return ToolResult.error(f"unknown op {op!r}")

    def _add(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        name = str(args.get("name", "")).strip()
        note = str(args.get("note", "")).strip()
        late_within = str(args.get("late_within", "")).strip()
        in_txt = str(args.get("within", "")).strip()
        at_txt = str(args.get("at", "")).strip()
        when: datetime | None = None
        which = [t for t, v in (("`within`", in_txt), ("`at`", at_txt)) if v]
        if not name or not note:
            return ToolResult.error(
                f"add needs `name` and `note` — `note` is the wake prompt when the "
                f"moment arrives. You sent: {sent_args(args)}"
            )
        if len(which) != 1:
            return ToolResult.error(
                "add needs exactly one of `within` (how long from now, e.g. '2h') or `at` (an "
                f"ISO moment, e.g. '2026-10-19T14:00'). You sent: {sent_args(args)}"
            )
        if in_txt:
            window = catchup.parse_window(in_txt)
            if window is None:
                return ToolResult.error(
                    f"`within` must read like 45m, 6h or 2d — got {in_txt!r}"
                )
            when = datetime.now() + window
        if at_txt:
            try:
                when = _parse_at(at_txt)
            except ValueError as exc:
                return ToolResult.error(str(exc))
        if when is None:  # unreachable by construction — the exclusivity check covers it
            return ToolResult.error("a reminder needs a moment: `within` or `at`")
        try:
            job = ctx.scheduler.add_reminder(
                name=name,
                when=when,
                note=note,
                late_within=late_within,
            )
        except ValueError as exc:
            return ToolResult.error(str(exc))
        return ToolResult(output=f"reminded {job.name!r}: at {when:%a %d %b %H:%M} → {note}")


def _parse_at(text: str) -> datetime:
    """An ISO moment (optionally with offsets), or a clean refusal."""
    from ..calendar import zones

    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(
            f"`at` must be an ISO moment, e.g. '2026-10-19T14:00' (local time) — got {text!r}; "
            "bare dates ('2026-10-19') need a time, because a reminder without a moment cannot be kept"
        ) from None
    if moment.tzinfo is not None:
        moment = moment.astimezone(zones.local_zone()).replace(tzinfo=None)
    if len(raw) <= 10:
        raise ValueError(
            f"`at` must carry a time, not just a date — got {text!r}: a reminder without a "
            "moment cannot be kept"
        )
    return moment
