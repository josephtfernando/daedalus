"""``code`` primitive — author and execute a code snippet in a scratch dir.

Risk ``ask``: arbitrary code execution, approved per action.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, TYPE_CHECKING

from ..approvals.risk import Risk
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime

LANG_EXT = {"python": ".py", "py": ".py", "node": ".mjs", "js": ".mjs", "bash": ".sh", "sh": ".sh"}
LANG_CMD = {
    "python": [sys.executable],
    "py": [sys.executable],
    "node": ["node"],
    "js": ["node"],
    "bash": ["bash"],
    "sh": ["bash"],
}
MAX_OUTPUT = 64 * 1024


class CodeTool(Tool):
    name = "code"
    description = (
        "Write and run a short script (python, node, or bash) in the workspace scratch dir. "
        "stdin is available; print results to stdout."
    )
    risk = Risk.ASK
    scopes = {"fs": ["workspace"], "exec": ["code"]}
    parameters = params(
        language=({"type": "string", "enum": ["python", "node", "bash"]}, True),
        source=({"type": "string"}, True),
        stdin=({"type": "string"}, False),
        timeout_s=({"type": "integer", "default": 60}, False),
        keep=({"type": "boolean", "default": False}, False),
    )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        language = str(args.get("language", "python")).lower()
        source = str(args.get("source", ""))
        if language not in LANG_CMD:
            return ToolResult.error(f"unsupported language {language!r}")
        scratch = ctx.settings.home / "state" / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        path = scratch / f"snippet{LANG_EXT[language]}"
        path.write_text(source, encoding="utf-8")

        proc = await asyncio.create_subprocess_exec(
            *LANG_CMD[language],
            str(path),
            cwd=str(scratch),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        timeout = int(args.get("timeout_s", 60))
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(input=str(args.get("stdin", "")).encode()), timeout=timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult.error(f"timed out after {timeout}s")
        text = out.decode("utf-8", "replace")[:MAX_OUTPUT]
        code = proc.returncode or 0
        if not args.get("keep"):
            path.unlink(missing_ok=True)
        return ToolResult(ok=code == 0, output=text or f"(no output, exit {code})", data={"exit_code": code})
