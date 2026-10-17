"""``schedule`` primitive — run anything on a period (SPEC §3.5 Layer 1)."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from .base import Tool, ToolResult, params, sent_args

if TYPE_CHECKING:
    from ..runtime import Runtime


class ScheduleTool(Tool):
    name = "schedule"
    title = "Schedule a job"
    description = (
        "Add, list, or remove scheduled jobs. A job either runs an authored capability "
        "(no model) or wakes the agent to compose a message."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["state"]}
    parameters = params(
        op=({"type": "string", "enum": ["add", "list", "remove"]}, True),
        name=({"type": "string"}, False),
        cron=({"type": "string", "description": "5-field cron, e.g. '0 7 * * *'"}, False),
        kind=({"type": "string", "enum": ["capability", "wake"], "default": "capability"}, False),
        target=({"type": "string", "description": "capability name, or a wake prompt"}, False),
        late_within=(
            {
                "type": "string",
                "description": (
                    "How late a run missed while the daemon was down is still worth running "
                    "(e.g. '45m', '6h', '2d'). Defaults to 1h for a capability and 12h for a wake."
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
            if not name or not cron:
                missing = " and ".join(
                    label for label, value in (("`name`", name), ("`cron`", cron)) if not value
                )
                return ToolResult.error(
                    f"add needs {missing} — `name` is the job's handle in this list, and `cron` "
                    'is when it runs ("0 7 * * *" is daily at 07:00; "0 8,20 * * *" is twice a '
                    f"day). Kind defaults to `capability`, so give `target` too. "
                    f"You sent: {sent_args(args)}"
                )
            try:
                job = scheduler.add_job(
                    name=name,
                    cron=cron,
                    kind=str(args.get("kind", "capability")),
                    target=str(args.get("target", "")),
                    late_within=str(args.get("late_within", "")),
                )
            except ValueError as exc:
                return ToolResult.error(str(exc))
            return ToolResult(output=f"scheduled {job.name!r}: {job.cron} → {job.kind}:{job.target}")
        if op == "list":
            jobs = scheduler.jobs()
            rows = [
                f"{j.name}\t{j.cron}\t{j.kind}:{j.target}\tnext={j.next_run_text()}"
                + (f"\tlate_within={j.late_within}" if j.late_within else "")
                for j in jobs
            ]
            return ToolResult(output="\n".join(rows) or "(no jobs)")
        if op == "remove":
            name = str(args.get("name", "")).strip()
            ok = scheduler.remove_job(name)
            return ToolResult(ok=ok, output=f"removed {name!r}" if ok else f"no job {name!r}")
        return ToolResult.error(f"unknown op {op!r}")
