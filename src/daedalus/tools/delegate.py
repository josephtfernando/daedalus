"""``delegate`` — send a task to the OpenCode delegate, one workspace, reviewable back.

Icarus's headline job (SPEC §7, T29/T30). The tool is thin on purpose; the shape
lives in :mod:`daedalus.delegate.runner`. What the tool adds is the loop's side
of the contract: a delegation is **registered before anyone waits on it**, so a
long one is visible in the process registry from its first tick, `op=cancel`
stops it tree-wide, and the ending is announced — including the non-zero ones,
because a delegation that died while the owner was reading something else is the
T41 lesson all over again if nobody is told.

Names: the registry row for a run is ``delegation <workspace>`` — one run at a
time per workspace, so the name the owner cancels is the name that makes sense.
The workspace on its own is what `op=result` and `op=diff` read: the workspace is
the reviewable thing; runs come and go.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from ..delegate.runner import ContainmentRefused, DelegateRunner, containment_refused
from .base import Tool, ToolResult, params, sent_args
from .process import Managed, _fingerprint

if TYPE_CHECKING:
    from ..runtime import Runtime


def _sentinel(value: Any, fallback: Any) -> Any:
    return fallback if value in (None, "") else value


class DelegateTool(Tool):
    name = "delegate"
    title = "Delegate a task to the OpenCode agent"
    description = (
        "Run one task in a named workspace under the loop's resource caps, and come "
        "back reviewable: a diff against the workspace's baseline plus a run record — "
        "what was run, what it could not do. The workspace is created for you under "
        "the delegate root and is the delegation's whole reach: paths outside it are "
        "refused before anything runs, and the personal tree is never a workspace. "
        "The delegate never commits, pushes, merges, pulls, rebases or resets — "
        "history is the owner's; phrase the task as work that leaves a diff. "
        "op=run returns immediately with an id; op=status watches it, op=cancel stops "
        "the tree, op=result reads the return, op=diff reads the diff against the "
        "baseline. When the run ends you are told, with its exit code."
    )
    parameters = params(
        op=(
            {
                "type": "string",
                "enum": ["run", "status", "result", "diff", "cancel", "workspaces"],
                "description": "run starts one; the rest are about the ones that exist.",
            },
            True,
        ),
        task=(
            {
                "type": "string",
                "description": "The task, as instructions to the delegate — required for op=run.",
            },
            False,
        ),
        name=(
            {
                "type": "string",
                "description": (
                    "One plain name (letters, digits, dot, dash) — the workspace the task "
                    "may touch, e.g. 'repo-watch'. Required for every op."
                ),
            },
            False,
        ),
        cpu={"type": "number", "description": "Whole-core CPU share, default 1.0."},
        mem_mb={"type": "integer", "description": "Resident-memory cap in MB, default 1024."},
        time_s={"type": "integer", "description": "Wall-clock ceiling in seconds, default 1800."},
    )

    def title_for(self, args: dict[str, Any]) -> str:
        op = str(args.get("op", ""))
        name = str(args.get("name", ""))
        if op == "run":
            return f"Delegate a task to the OpenCode agent in {name!r}"
        if op == "cancel":
            return f"Cancel the delegation in {name!r}"
        return f"{self.title} ({op} {name})" if name else f"{self.title} ({op})"

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", "run"))
        if op == "run":
            name = str(args.get("name", "")) or "a workspace"
            return Assessment(
                level=Level.CAUTION,
                summary=(
                    f"delegate to the OpenCode agent in {name!r} — it writes files there, "
                    "under resource caps, and returns a diff; no commits, no pushes"
                ),
            )
        if op == "cancel":
            return Assessment(
                level=Level.CAUTION,
                summary=f"cancel the delegation in {args.get('name', '')!r} and its whole process tree",
            )
        return Assessment(level=Level.OK, summary=f"read the delegation state ({op})")

    def risk_for(self, args: dict[str, Any]) -> Risk:
        return Risk.ASK if str(args.get("op", "")) == "run" else Risk.AUTO

    # ------------------------------------------------------------------

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        runner: DelegateRunner = ctx.delegate_runner
        sent = sent_args(args)
        try:
            if op == "workspaces":
                rows = sorted(p.name for p in runner.root.iterdir() if p.is_dir())
                return ToolResult(output="\n".join(rows) or "(no workspaces yet)")
            name = str(args.get("name", "")).strip()
            if not name:
                return ToolResult.error(
                    f"every delegate op needs `name` — the workspace the task may touch. "
                    f"You sent: {sent}"
                )
            if op == "run":
                return await self._run(args, ctx, runner, sent)
            if op == "status":
                managed = ctx.processes.get(f"delegation {name}")
                if managed is None:
                    return ToolResult.error(
                        f"no delegation known under {name!r} — op=run starts one; "
                        f"the registry names a run 'delegation {name}'"
                    )
                return ToolResult(output=f"delegation {name!r}: {managed.describe()}")
            if op == "result":
                return self._result(ctx, name)
            if op == "diff":
                return await self._diff(runner, name)
            if op == "cancel":
                ok, detail = await ctx.processes.stop(f"delegation {name}")
                return ToolResult(ok=ok, output=detail)
            return ToolResult.error(
                f"unknown delegate op {op!r} — run, status, result, diff, cancel, workspaces. "
                f"You sent: {sent}"
            )
        except ContainmentRefused as exc:
            return ToolResult.error(containment_refused(exc))
        except ValueError as exc:
            return ToolResult.error(f"{exc} You sent: {sent}")

    async def _run(
        self, args: dict[str, Any], ctx: "Runtime", runner: DelegateRunner, sent: str
    ) -> ToolResult:
        task = str(args.get("task", "")).strip()
        name = str(args.get("name", "")).strip()
        if not task:
            return ToolResult.error(
                f"op=run needs `task` — what the delegate is to do in {name!r}. You sent: {sent}"
            )
        caps = {
            "cpu": float(_sentinel(args.get("cpu"), 1.0)),
            "mem_mb": int(_sentinel(args.get("mem_mb"), 1024)),
            "time_s": int(_sentinel(args.get("time_s"), 1800)),
        }
        live = await runner.launch(task, name=name, caps=caps, forbidden=(ctx.settings.home,))
        registry_name = f"delegation {name}"
        managed = Managed(
            name=registry_name,
            command=" ".join(shlex.quote(a) for a in live.argv),
            pid=live.proc.pid,
            ticks=_fingerprint(live.proc.pid),
            proc=live.proc,
        )
        try:
            ctx.processes.adopt(managed)
        except Exception as exc:  # a live child with no registry row is worse than the reverse
            await _stop_live(live)
            return ToolResult.error(
                f"the delegation could not be registered ({exc}) and was stopped before it "
                "could run unregistered — being visible in the registry is not optional"
            )
        asyncio.create_task(self._collect_registered(runner, ctx, live, managed))
        return ToolResult(
            ok=True,
            output=(
                f"delegation {registry_name!r} started in {live.ws} — caps cpu={caps['cpu']} "
                f"mem_mb={caps['mem_mb']} time_s={caps['time_s']}. op=status to watch it; "
                "you will be told when it ends, with its exit code."
            ),
            data={"name": registry_name, "workspace": str(live.ws), "pid": live.proc.pid},
        )

    async def _collect_registered(
        self,
        runner: DelegateRunner,
        ctx: "Runtime",
        live: "DelegateRunner.Live",
        managed: Managed,
    ) -> None:
        """Wait the delegation out, close its registry row, announce the end.

        The announcement carries the exit code and the diff size — and the
        failures like the successes (T41's lesson): a delegation that died while
        the turn had moved on must not stay silent until someone asks.
        """
        result = await runner.collect(live)
        ctx.processes.record_end(managed, result.exit_code)
        lines = result.diff.count("\n") + 1 if result.diff else 0
        message = result.summary()
        if lines:
            message += f" — the diff is {lines} lines; `delegate op=diff name={live.name}` reads it"
        tail = ((result.report or result.error) or "").strip().splitlines()
        if tail:
            message += f"\nLast line from the run: {tail[-1][:200]}"
        try:
            await ctx.surface.send(message)
        except Exception:  # noqa: BLE001 — a failed announcement is not a failed delegation
            pass

    def _result(self, ctx: "Runtime", name: str) -> ToolResult:
        """The reviewable return: what was run, what came of it, what it could not do.

        Read from the workspace's own run record — the file the delegation laid
        down beside its work, not a summary re-derived from memory.
        """
        path = ctx.delegate_runner.workspace_dir(name) / ".delegation.json"
        if not path.exists():
            return ToolResult.error(
                f"no run record for {name!r} — if it never ran, op=run starts one; "
                "if it is still running, op=status watches it"
            )
        record = json.loads(path.read_text(encoding="utf-8"))
        lines = [
            (
                f"{name!r}: {'ok' if record['ok'] else 'failed'} — exit "
                f"{record.get('exit_code')}, {record['duration_s']:.0f}s, peaked at "
                f"{record['peak_mb']:.0f} MB of its {record['caps']['mem_mb']} MB"
            ),
            f"task: {record['task']}",
        ]
        if record.get("breach"):
            lines.append(f"stopped by caps: {record['breach']}")
        if record.get("diff"):
            diff = record["diff"]
            head = diff[:2000]
            more = "" if len(diff) <= 2000 else f"\n… ({len(diff)} chars — full diff is on disk)"
            lines.append(f"diff:\n{head}{more}")
        else:
            lines.append("diff: none — nothing changed against the baseline")
        if record.get("error"):
            lines.append(f"what it could not do: {record['error'][-300:]}")
        return ToolResult(ok=record["ok"], output="\n".join(lines), data=record)

    async def _diff(self, runner: DelegateRunner, name: str) -> ToolResult:
        ws = runner.workspace_dir(name)
        if not ws.exists():
            return ToolResult.error(f"no workspace {name!r} exists yet")
        d = await runner.diff(ws)
        return ToolResult(output=d or "(no changes against the baseline)")


async def _stop_live(live: "DelegateRunner.Live") -> None:
    from ..limits import kill_tree

    await kill_tree(live.proc)
