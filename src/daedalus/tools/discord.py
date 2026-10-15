"""``discord`` primitive — send a message to the owner, including proactively."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.risk import Risk
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime


class DiscordTool(Tool):
    name = "discord"
    description = "Send a message to the owner on Discord (proactive messages allowed)."
    risk = Risk.AUTO
    scopes = {}
    parameters = params(
        text=({"type": "string"}, True),
        thread=({"type": "string", "description": "optional thread id/name"}, False),
    )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        text = str(args.get("text", "")).strip()
        if not text:
            return ToolResult.error("empty message")
        if ctx.surface is None:
            return ToolResult(output="(no surface attached; message not delivered)")
        origin = str(args.get("thread") or "") or None
        await ctx.surface.send(text, thread=origin)
        return ToolResult(output="sent")
