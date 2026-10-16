"""``process`` primitive — start / stop / monitor long-running processes."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from ..security.env import scrubbed
from . import guard
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime


@dataclass
class Managed:
    name: str
    command: str
    proc: asyncio.subprocess.Process
    started_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    _task: asyncio.Task | None = None


class ProcessManager:
    def __init__(self) -> None:
        self._procs: dict[str, Managed] = {}

    async def start(
        self, name: str, command: str, cwd: str | None = None, env: dict[str, str] | None = None
    ) -> Managed:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            executable="/bin/bash",
        )
        managed = Managed(name=name, command=command, proc=proc)
        managed._task = asyncio.create_task(self._drain(managed))
        self._procs[name] = managed
        return managed

    async def _drain(self, managed: Managed) -> None:
        assert managed.proc.stdout is not None
        async for raw in managed.proc.stdout:
            managed.log.append(raw.decode("utf-8", "replace"))
            if len(managed.log) > 500:
                del managed.log[:100]

    def get(self, name: str) -> Managed | None:
        return self._procs.get(name)

    def list(self) -> list[Managed]:
        return list(self._procs.values())

    async def stop(self, name: str) -> bool:
        managed = self._procs.get(name)
        if managed is None:
            return False
        managed.proc.terminate()
        try:
            await asyncio.wait_for(managed.proc.wait(), timeout=10)
        except TimeoutError:
            managed.proc.kill()
        return True

    async def stop_all(self) -> None:
        for name in list(self._procs):
            await self.stop(name)


class ProcessTool(Tool):
    name = "process"
    title = "Start a long-running process"
    description = "Start, list, read logs from, or stop long-running processes."
    risk = Risk.ASK
    scopes = {"fs": ["workspace"], "exec": ["process"]}
    parameters = params(
        op=({"type": "string", "enum": ["start", "list", "logs", "stop"]}, True),
        name=({"type": "string"}, True),
        command=(
            {
                "type": "string",
                "description": (
                    "the shell command to run, for op=start. Pass \"\" for op=list/logs/stop, which "
                    "act on an existing process."
                ),
            },
            True,
        ),
        cwd=({"type": "string"}, False),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", ""))
        name = str(args.get("name", "")) or "a process"
        if op in ("list", "logs"):
            return Assessment(level=Level.OK, summary=f"read what {name!r} is doing")
        if op == "stop":
            return Assessment(level=Level.CAUTION, summary=f"stop the process {name!r}")
        return Assessment(
            level=Level.CAUTION,
            summary=f"start {name!r} and leave it running",
            reasons=("it keeps running in the background after this turn ends",),
        )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        name = str(args.get("name", "")).strip()
        manager = ctx.processes
        if op == "start":
            command = str(args.get("command", "")).strip()
            if not command:
                return ToolResult.error("start needs a command")
            cwd = None
            if args.get("cwd"):
                try:
                    cwd = str(guard.resolve(str(args["cwd"]), ctx.settings))
                except guard.OutOfBounds as exc:
                    return ToolResult.error(str(exc))
            managed = await manager.start(name, command, cwd, env=scrubbed(ctx.settings))
            return ToolResult(output=f"started {name!r} (pid {managed.proc.pid})", data={"pid": managed.proc.pid})
        if op == "list":
            rows = [
                f"{m.name}\tpid={m.proc.pid}\trc={m.proc.returncode}\t{len(m.log)} lines"
                for m in manager.list()
            ]
            return ToolResult(output="\n".join(rows) or "(none)")
        if op == "logs":
            managed = manager.get(name)
            if managed is None:
                return ToolResult.error(f"no process {name!r}")
            return ToolResult(output="".join(managed.log[-200:]) or "(no output yet)")
        if op == "stop":
            ok = await manager.stop(name)
            return ToolResult(ok=ok, output=f"stopped {name!r}" if ok else f"no process {name!r}")
        return ToolResult.error(f"unknown op {op!r}")
