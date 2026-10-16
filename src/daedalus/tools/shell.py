"""``shell`` primitive — run a command, scoped and audited.

Risk comes from the command, not from the tool (SPEC §6): a command that only
reads state is self-contained and runs; one that can change the machine asks
first; one the classifier refuses never runs. This is also why ``shell`` is not
*grantable* — there is no scope that describes "whatever commands I might run
next", so an approval here covers exactly the command that was approved.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level, classify_command
from ..approvals.risk import Risk
from ..security.env import scrubbed
from . import guard
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime

MAX_OUTPUT = 64 * 1024


class ShellTool(Tool):
    name = "shell"
    title = "Run a shell command"
    description = "Run a shell command in the workspace and return its output."
    risk = Risk.ASK
    grantable = False
    scopes = {"fs": ["workspace"], "exec": ["shell"]}
    parameters = params(
        command=({"type": "string"}, True),
        cwd=({"type": "string"}, False),
        timeout_s=({"type": "integer", "default": 60}, False),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        """Judge the command itself, so the owner is told what it does."""
        return classify_command(str(args.get("command", "")))

    def risk_for(self, args: dict[str, Any]) -> Risk:
        assessment = self.assess(args)
        # A refused command is not a choice to offer: the owner should not have
        # to read `rm -rf /` and decide. This is also why `files` must not be the
        # only tool that respects the out-of-bounds paths.
        if assessment.refused:
            return Risk.BLOCK
        # Reading is self-contained, so it does not need permission. Asking for
        # `git status` was how a prompt stopped carrying information: if every
        # command asks, "it asked" no longer means "it might change something".
        if assessment.level is Level.OK:
            return Risk.AUTO
        return Risk.ASK

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult.error("empty command")
        cwd = None
        if args.get("cwd"):
            try:
                cwd = str(guard.resolve(str(args["cwd"]), ctx.settings))
            except guard.OutOfBounds as exc:
                return ToolResult.error(str(exc))
        timeout = int(args.get("timeout_s", 60))
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env=scrubbed(ctx.settings),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            executable="/bin/bash",
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult.error(f"timed out after {timeout}s: {shlex.quote(command)}")
        text = out.decode("utf-8", "replace")[:MAX_OUTPUT]
        code = proc.returncode or 0
        return ToolResult(
            ok=code == 0,
            output=text or f"(no output, exit {code})",
            data={"exit_code": code, "command": command},
        )
