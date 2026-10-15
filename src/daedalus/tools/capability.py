"""``capability`` primitive — the Layer-2 interface the model authors through.

This is the tool-making machine's front door: author an artifact, gate it
through Melinoë, register it, optionally schedule it. The engine owns
registration and gating; the model supplies only the artifact.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.risk import Risk
from ..capabilities.authoring import materialize
from ..capabilities.manifest import Caps, Manifest, Scopes
from ..capabilities.verify import verify_capability
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime


class CapabilityTool(Tool):
    name = "capability"
    description = (
        "Author, list, run, verify, or remove an authored capability. Authoring writes "
        "capabilities/<name>/{run.py,manifest.toml,README.md}, runs the Melinoë gate, and "
        "registers it. A capability reads {args,context,state} as JSON on stdin and writes "
        "{result,state_next,notifications} as JSON on stdout."
    )
    risk = Risk.ASK
    scopes = {"fs": ["capabilities"], "exec": ["capability"]}
    parameters = params(
        op=({"type": "string", "enum": ["author", "list", "get", "run", "verify", "remove", "schedule"]}, True),
        name=({"type": "string"}, False),
        description=({"type": "string"}, False),
        run_py=({"type": "string", "description": "the run.py source"}, False),
        why=({"type": "string", "description": "one line: why this exists"}, False),
        deps=({"type": "array", "items": {"type": "string"}, "description": "pip deps for its own venv"}, False),
        schedule=({"type": "string", "description": "5-field cron, optional"}, False),
        net=({"type": "array", "items": {"type": "string"}, "description": "allowed hosts"}, False),
        trust_tier=({"type": "string", "enum": ["auto", "ask", "block"], "default": "auto"}, False),
        args=({"type": "object", "description": "for op=run"}, False),
        time_s=({"type": "integer", "default": 120}, False),
    )

    def risk_for(self, args: dict[str, Any]) -> Risk:
        op = str(args.get("op", ""))
        if op in ("list", "get"):
            return Risk.AUTO
        return Risk.ASK

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        if op == "list":
            return ToolResult(output=ctx.capabilities.inventory())
        if op == "get":
            manifest = ctx.capabilities.get(str(args.get("name", "")))
            if manifest is None:
                return ToolResult.error("no such capability")
            return ToolResult(output=manifest.to_toml(), data={"manifest": manifest.to_dict()})
        if op == "author":
            return await self._author(args, ctx)
        if op == "run":
            return await self._run(args, ctx)
        if op == "verify":
            return await self._verify(args, ctx)
        if op == "remove":
            name = str(args.get("name", ""))
            ok = ctx.capabilities.remove(name)
            return ToolResult(ok=ok, output=f"removed {name!r}" if ok else f"no capability {name!r}")
        if op == "schedule":
            return self._schedule(args, ctx)
        return ToolResult.error(f"unknown op {op!r}")

    async def _author(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        name = str(args.get("name", "")).strip()
        run_py = str(args.get("run_py", "")).strip()
        if not name or not run_py:
            return ToolResult.error("author needs name and run_py")
        manifest = Manifest(
            name=name,
            description=str(args.get("description", "")),
            schedule=str(args.get("schedule") or "") or None,
            deps=list(args.get("deps", []) or []),
            scopes=Scopes(net=list(args.get("net", []) or [])),
            caps=Caps(time_s=int(args.get("time_s", 120))),
            trust_tier=str(args.get("trust_tier", "auto")),
            io={"input_schema": args.get("input_schema", {}), "output_schema": args.get("output_schema", {})},
        )
        materialize(ctx.capabilities.root, manifest, run_py=run_py, why=str(args.get("why", "")))

        # Melinoë gate. Egress needs an owner-approved grant before we exercise it.
        if manifest.scopes.needs_egress():
            grant = ctx.approvals.grants.find("capability", f"capability:{name}")
            allow = grant is not None
            report = await verify_capability(
                manifest, ctx.runner, sample_args=args.get("args") or {}, allow_egress=allow
            )
        else:
            report = await verify_capability(manifest, ctx.runner, sample_args=args.get("args") or {})

        ctx.capabilities.register(manifest)  # registered either way; verified flag decides scheduling
        lines = [report.summary()]
        if report.ok:
            manifest.mark_verified()
            ctx.capabilities.register(manifest)
            lines.append(f"registered {name!r} (verified)")
            if manifest.schedule:
                try:
                    ctx.scheduler.add_job(name=f"cap-{name}", cron=manifest.schedule, kind="capability", target=name)
                    lines.append(f"scheduled {name!r}: {manifest.schedule}")
                except ValueError as exc:
                    lines.append(f"schedule rejected: {exc}")
        else:
            lines.append(f"{name!r} registered but NOT verified — it will not be scheduled unattended")
        return ToolResult(ok=report.ok, output="\n".join(lines), data={"verified": report.ok, "name": name})

    async def _run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        manifest = ctx.capabilities.get(str(args.get("name", "")))
        if manifest is None:
            return ToolResult.error("no such capability")
        state = ctx.runner.read_state(manifest)
        run = await ctx.runner.run(manifest, args=args.get("args") or {}, state=state)
        if run.state_next is not None:
            ctx.runner.write_state(manifest, run.state_next)
        if not run.ok:
            return ToolResult.error(run.error or run.stderr or f"exit {run.exit_code}")
        import json

        return ToolResult(output=json.dumps(run.result, indent=2, default=str), data={"result": run.result})

    async def _verify(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        manifest = ctx.capabilities.get(str(args.get("name", "")))
        if manifest is None:
            return ToolResult.error("no such capability")
        allow = not manifest.scopes.needs_egress() or ctx.approvals.grants.find("capability", f"capability:{manifest.name}") is not None
        report = await verify_capability(manifest, ctx.runner, sample_args=args.get("args") or {}, allow_egress=bool(allow))
        if report.ok:
            manifest.mark_verified()
            ctx.capabilities.register(manifest)
        return ToolResult(ok=report.ok, output=report.summary())

    def _schedule(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        name = str(args.get("name", ""))
        manifest = ctx.capabilities.get(name)
        if manifest is None:
            return ToolResult.error("no such capability")
        expr = str(args.get("schedule") or manifest.schedule or "")
        if not expr:
            return ToolResult.error("no cron given and the manifest declares none")
        if manifest.verified_at is None:
            return ToolResult.error(f"{name!r} has not passed the Melinoë gate; verify it first")
        job = ctx.scheduler.add_job(name=f"cap-{name}", cron=expr, kind="capability", target=name)
        return ToolResult(output=f"scheduled {name!r}: {job.cron} (next {job.next_run_text()})")
