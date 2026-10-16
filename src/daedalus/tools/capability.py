"""``capability`` primitive — the Layer-2 interface the model authors through.

This is the tool-making machine's front door: author an artifact, gate it
through Melinoë, register it, optionally schedule it. The engine owns
registration and gating; the model supplies only the artifact.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from ..approvals.classify import Assessment, Level
from ..approvals.grants import Grant
from ..approvals.risk import Risk
from ..capabilities.authoring import materialize
from ..capabilities.manifest import Caps, Manifest, Scopes
from ..capabilities.verify import verify_capability
from ..calendar.event import normalise_when, parse_iso
from ..security.provenance import DataClass
from ..surface.base import ApprovalRequest
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime


class CapabilityTool(Tool):
    name = "capability"
    title = "Author a capability"
    description = (
        "The tool-making machine: author, list, get, run, verify, remove, or schedule a capability.\n"
        "`op=author` ALWAYS needs `name` and `run_py`. `run_py` is the complete Python source of "
        "run.py, and YOU write it. The tool stores exactly what you pass, runs the Melinoë gate over "
        "it, and registers it — the tool never writes code for you, so a call without `run_py` "
        "creates nothing.\n"
        "The contract run.py must honour: it runs as a standalone script in its own venv, reads one "
        "JSON object {args,context,state} on stdin, and prints one JSON object "
        "{result,state_next,notifications} on stdout. Keep it small and deterministic. "
        "`notifications` is what the owner actually sees when the job runs unattended — each entry "
        'should be {"title": "...", "text": "..."}, and an empty list means stay quiet.\n'
        "Give `schedule` (5-field cron) on op=author to register a recurring job, and `args` for the "
        "sample the gate exercises."
    )
    risk = Risk.ASK
    scopes = {"fs": ["capabilities"], "exec": ["capability"]}
    parameters = params(
        op=({"type": "string", "enum": ["author", "list", "get", "run", "verify", "remove", "schedule"]}, True),
        name=({"type": "string", "description": "short slug, e.g. disk_free_check. Required for op=author."}, False),
        description=({"type": "string", "description": "one line for the manifest: what it does"}, False),
        run_py=(
            {
                "type": "string",
                "description": (
                    "REQUIRED for op=author: the complete Python source of run.py, written by you. "
                    "Stored verbatim; the tool never fills this in. Pass \"\" for the other ops."
                ),
            },
            True,
        ),
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

    _TITLES = {
        "author": "Author a capability",
        "list": "List capabilities",
        "get": "Read a capability",
        "run": "Run a capability",
        "verify": "Verify a capability",
        "remove": "Delete a capability",
        "schedule": "Schedule a capability",
    }

    def title_for(self, args: dict[str, Any]) -> str:
        return self._TITLES.get(str(args.get("op", "")), self.title)

    def approval_labels(self, args: dict[str, Any]) -> tuple[str, str]:
        if str(args.get("op", "")) == "remove":
            return ("Delete it", "Keep it")
        return ("Do it", "Don't")

    def assess(self, args: dict[str, Any]) -> Assessment:
        """Say which operation this is, because `capability` is six tools in one."""
        op = str(args.get("op", ""))
        name = str(args.get("name", "")).strip()
        targeting = ("run", "verify", "schedule", "get")
        if not name and (op == "remove" or op in targeting):
            # The model sent the op without its target. Do not invent a noun for
            # it — say plainly that the call is incomplete, so the owner is not
            # asked to weigh a destructive action that would in fact do nothing.
            return Assessment(
                level=Level.DESTRUCTIVE if op == "remove" else Level.CAUTION,
                summary=f"do nothing: this {op} named no capability",
                reasons=(
                    "the call is missing `name`, so there is no target",
                    "approving it would change nothing — it should be retried with a target",
                ),
                note_text="incomplete call — approving it would change nothing",
            )
        if op == "author":
            schedule = args.get("schedule")
            tail = f", then schedule it to run unattended ({schedule})" if schedule else ""
            return Assessment(
                level=Level.CAUTION,
                summary=f"write a new program to disk and run it once to check it{tail}",
                reasons=("authored code runs on this machine, and a scheduled one runs with no model in the loop",),
            )
        if op == "run":
            return Assessment(level=Level.CAUTION, summary=f"run the authored capability {name!r}")
        if op == "verify":
            return Assessment(level=Level.CAUTION, summary=f"run {name!r} once, to check it works")
        if op == "schedule":
            return Assessment(level=Level.CAUTION, summary=f"schedule {name!r} to run unattended")
        if op == "remove":
            return Assessment(
                level=Level.DESTRUCTIVE,
                summary=f"delete the authored capability {name!r}",
                reasons=("its run.py and its remembered state are removed",),
            )
        return Assessment(summary=f"ask about the authored capabilities ({op})")

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
            missing = "name" if not name else "run_py"
            return ToolResult.error(
                f"op=author needs `name` and `run_py`; `{missing}` was missing, so nothing was "
                "created. run_py is the complete Python source of run.py — you write it, the tool "
                "only stores it. Retry the same call with the full source included."
            )
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

        # Melinoë gate. Egress needs an owner-approved grant before we exercise
        # it — and the ask happens here, as a real approval with buttons, so the
        # owner's yes is *recorded* rather than relayed and lost.
        note = ""
        if manifest.scopes.needs_egress():
            allowed, note = await self._egress_approved(manifest, ctx)
            report = await verify_capability(
                manifest, ctx.runner, sample_args=args.get("args") or {}, allow_egress=allowed
            )
        else:
            report = await verify_capability(manifest, ctx.runner, sample_args=args.get("args") or {})

        ctx.capabilities.register(manifest)  # registered either way; verified flag decides scheduling
        lines = [report.summary()]
        if note:
            lines.append(note)
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

    async def _egress_approved(self, manifest: Manifest, ctx: "Runtime") -> tuple[bool, str]:
        """Whether this capability may reach the network — asking the owner **once**.

        The Melinoë gate refuses an egressing capability until the owner has
        approved it, and the record it looks for is a standing grant keyed
        ``capability:<name>``. Until now nothing ever *wrote* that grant: the
        model relayed the question as text, heard "yes", and could not record
        it — so verify failed again and the turn circled into its step limit.
        The ask now happens here, as a real approval (buttons, the same shape
        the engine's ask-branch uses), and the answer is written where the gate
        reads it. After a yes, verify and the scheduler never ask again.
        """
        scope = f"capability:{manifest.name}"
        if ctx.approvals.grants.find("capability", scope) is not None:
            return True, ""
        if ctx.surface is None:
            return False, (
                f"{manifest.name!r} needs network access ({', '.join(manifest.scopes.net)}) "
                "and no surface is attached to ask through"
            )
        hosts = ", ".join(manifest.scopes.net)
        approved = await ctx.surface.approve(
            ApprovalRequest(
                action="capability",
                scope=scope,
                reason="it reaches the network on a schedule, so it asks once and is remembered",
                args={"name": manifest.name, "net": list(manifest.scopes.net)},
                action_hash=ctx.approvals.action_hash("capability", {"name": manifest.name}),
                title=f"Allow network access for {manifest.name!r}",
                summary=f"reach {hosts} on a schedule, without asking again",
                level="caution",
                note="egress — the approval is remembered as a standing grant",
                affirm_label=f"Allow {hosts[:60]}",
                deny_label="Don't allow",
            )
        )
        if not approved:
            return False, f"{manifest.name!r} was not allowed network access — it stays unverified"
        ctx.approvals.grants.add(
            Grant(action="capability", scope=scope, note=f"egress approved: {hosts}")
        )
        return True, f"network access allowed for {hosts} — remembered as a standing grant"

    async def _run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        manifest = ctx.capabilities.get(str(args.get("name", "")))
        if manifest is None:
            return ToolResult.error("no such capability")
        if manifest.verified_at is None:
            # The gate is the point: an unverified capability has never been
            # exercised under supervision, and running it by hand is that gate
            # walked around — which is how a turn spends fifteen steps reading
            # source instead of one step verifying.
            return ToolResult.error(
                f"{manifest.name!r} has not passed the Melinoë gate. Run `capability "
                f"op=verify name={manifest.name}` first — it asks the owner for any "
                "approval it needs, once, and remembers the answer."
            )
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
        note = ""
        allowed = not manifest.scopes.needs_egress()
        if manifest.scopes.needs_egress():
            allowed, note = await self._egress_approved(manifest, ctx)
        report = await verify_capability(manifest, ctx.runner, sample_args=args.get("args") or {}, allow_egress=bool(allowed))
        if report.ok:
            manifest.mark_verified()
            ctx.capabilities.register(manifest)
        output = report.summary() if not note else f"{report.summary()}\n{note}"
        return ToolResult(ok=report.ok, output=output)

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
