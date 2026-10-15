"""``schedule`` primitive — run anything on a period (SPEC §3.5 Layer 1)."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.risk import Risk
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime


class ScheduleTool(Tool):
    name = "schedule"
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
    )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        scheduler = ctx.scheduler
        if op == "add":
            name = str(args.get("name", "")).strip()
            cron = str(args.get("cron", "")).strip()
            if not name or not cron:
                return ToolResult.error("add needs name and cron")
            try:
                job = scheduler.add_job(
                    name=name,
                    cron=cron,
                    kind=str(args.get("kind", "capability")),
                    target=str(args.get("target", "")),
                )
            except ValueError as exc:
                return ToolResult.error(str(exc))
            return ToolResult(output=f"scheduled {job.name!r}: {job.cron} → {job.kind}:{job.target}")
        if op == "list":
            jobs = scheduler.jobs()
            rows = [f"{j.name}\t{j.cron}\t{j.kind}:{j.target}\tnext={j.next_run_text()}" for j in jobs]
            return ToolResult(output="\n".join(rows) or "(no jobs)")
        if op == "remove":
            name = str(args.get("name", "")).strip()
            ok = scheduler.remove_job(name)
            return ToolResult(ok=ok, output=f"removed {name!r}" if ok else f"no job {name!r}")
        return ToolResult.error(f"unknown op {op!r}")
