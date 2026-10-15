"""``shell`` primitive — run a command, scoped and audited.

Risk ``ask``: an arbitrary command is not a *self-contained* action in the
sense of SPEC §6, so it asks until the owner grants it.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any, TYPE_CHECKING

from ..approvals.risk import Risk
from . import guard
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime

MAX_OUTPUT = 64 * 1024


class ShellTool(Tool):
    name = "shell"
    description = "Run a shell command in the workspace and return its output."
    risk = Risk.ASK
    scopes = {"fs": ["workspace"], "exec": ["shell"]}
    parameters = params(
        command=({"type": "string"}, True),
        cwd=({"type": "string"}, False),
        timeout_s=({"type": "integer", "default": 60}, False),
    )

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
