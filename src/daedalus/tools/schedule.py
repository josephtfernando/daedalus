"""``schedule`` primitive — run anything on a period (SPEC §3.5 Layer 1)."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from .base import Tool, ToolResult, params, sent_args

if TYPE_CHECKING:
    from ..runtime import Runtime


def seat_in_cabinet(ctx: "Runtime", name: str) -> bool:
    """Whether a job's `seat` names a charter the cabinet actually holds.

    Refused here, at the tool's door, rather than discovered at fire time —
    a job that could only fail every occurrence should not be creatable.
    """
    from ..cabinet.seat import UnknownSeat, resolve

    try:
        resolve(ctx.seats, name)
    except UnknownSeat:
        return False
    return True


class ScheduleTool(Tool):
    name = "schedule"
    title = "Schedule a job"
    description = (
        "Add, list, or remove scheduled jobs. `op=add` always needs `name` and `cron`: "
        "a job here is something that *recurs* — cron is the only way to say when. A job "
        "either runs an authored capability (no model) or wakes the agent to compose a "
        "message. For a one-shot reminder — a single moment, not a pattern — use the "
        "`remind` tool instead; no cron expression can say '2 hours from now, once', and "
        "an absolute-date one would recur yearly forever."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["state"]}
    parameters = params(
        op=({"type": "string", "enum": ["add", "list", "remove"]}, True),
        name=({"type": "string", "description": "the job's handle — required for op=add and op=remove"}, False),
        cron=(
            {
                "type": "string",
                "description": (
                    "5-field cron, e.g. '0 7 * * *' — required for op=add; omit for list/remove. "
                    "A wakeup that should happen once, not on a pattern, belongs to `remind`."
                ),
            },
            False,
        ),
        kind=({"type": "string", "enum": ["capability", "wake"], "default": "capability"}, False),
        target=(
            {
                "type": "string",
                "description": (
                    "capability name, or a wake prompt — required for op=add"
                ),
            },
            False,
        ),
        late_within=(
            {
                "type": "string",
                "description": (
                    "The grace window for a run missed while the daemon was down, still worth "
                    "catching up (e.g. '45m', '6h', '2d'). This is not when the job runs — cron "
                    "is. Defaults to 1h for a capability and 12h for a wake."
                ),
            },
            False,
        ),
        seat=(
            {
                "type": "string",
                "description": (
                    "Optional, wake jobs only: the cabinet seat this job belongs to "
                    "('odysseus', 'athena'...). A seated wake composes from that seat's "
                    "charter and namespace, and delivers under its name. Omit for the "
                    "coordinator's own work."
                ),
            },
            False,
        ),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", ""))
        name = str(args.get("name", "")) or "a job"
        if op == "list":
            return Assessment(level=Level.OK, summary="list the scheduled jobs")
        if op == "remove":
            return Assessment(
                level=Level.DESTRUCTIVE,
                summary=f"remove the scheduled job {name!r}",
                reasons=("that automation stops running, silently",),
            )
        return Assessment(level=Level.CAUTION, summary=f"add the scheduled job {name!r}")

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        scheduler = ctx.scheduler
        if op == "add":
            name = str(args.get("name", "")).strip()
            cron = str(args.get("cron", "")).strip()
            seat = str(args.get("seat", "")).strip()
            if seat and str(args.get("kind", "capability")) != "wake":
                return ToolResult.error(
                    "`seat` belongs to a wake — a capability job runs headless and has no "
                    f"identity to carry. You sent: {sent_args(args)}"
                )
            if seat and not seat_in_cabinet(ctx, seat):
                return ToolResult.error(
                    f"`seat` {seat!r} names nobody in the cabinet — the seats are "
                    + ", ".join(sorted(key for key in ctx.seats if key != "daedalus"))
                    + ". A job that names no seat would silently answer as someone else, "
                    f"which is worse than no seat at all. You sent: {sent_args(args)}"
                )
            if not name or not cron:
                missing = " and ".join(
                    label for label, value in (("`name`", name), ("`cron`", cron)) if not value
                )
                return ToolResult.error(
                    f"add needs {missing} — `name` is the job's handle in this list, and `cron` "
                    'is when it runs ("0 7 * * *" is daily at 07:00; "0 8,20 * * *" is twice a '
                    f"day). Kind defaults to `capability`, so give `target` too. "
                    "A one-shot reminder is not this tool: if the moment should happen "
                    "once, use `remind op=add` with `within` ('2h') or `at` ('2026-10-19T14:00') "
                    "and a `note` — no cron expression can say '2 hours from now, once'. "
                    f"You sent: {sent_args(args)}"
                )
            try:
                job = scheduler.add_job(
                    name=name,
                    cron=cron,
                    kind=str(args.get("kind", "capability")),
                    target=str(args.get("target", "")),
                    late_within=str(args.get("late_within", "")),
                    seat=str(args.get("seat", "")).strip(),
                )
            except ValueError as exc:
                return ToolResult.error(str(exc))
            seat_note = f" (as {job.seat})" if job.seat else ""
            return ToolResult(output=f"scheduled {job.name!r}: {job.cron} → {job.kind}:{job.target}{seat_note}")
        if op == "list":
            jobs = scheduler.jobs()
            rows = [
                f"{j.name}\t{j.cron or j.next_run_text()}\t{j.kind}:{j.target}"
                + (f" as {j.seat}" if j.seat else "")
                + f"\tnext={j.next_run_text()}"
                + (f"\tlate_within={j.late_within}" if j.late_within else "")
                for j in jobs
            ]
            return ToolResult(output="\n".join(rows) or "(no jobs)")
        if op == "remove":
            name = str(args.get("name", "")).strip()
            ok = scheduler.remove_job(name)
            return ToolResult(ok=ok, output=f"removed {name!r}" if ok else f"no job {name!r}")
        return ToolResult.error(f"unknown op {op!r}")
