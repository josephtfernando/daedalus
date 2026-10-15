"""``ask`` primitive — put a question to the owner and wait for an answer."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.risk import Risk
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime


class AskTool(Tool):
    name = "ask"
    description = "Ask the owner a question and wait for their answer."
    risk = Risk.AUTO
    scopes = {}
    parameters = params(
        question=({"type": "string"}, True),
        options=({"type": "array", "items": {"type": "string"}, "description": "optional choices"}, False),
    )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        question = str(args.get("question", "")).strip()
        if not question:
            return ToolResult.error("empty question")
        if ctx.surface is None:
            return ToolResult.error("no surface attached to ask through")
        options = args.get("options") or None
        answer = await ctx.surface.ask(question, options=options)
        return ToolResult(output=answer, data={"answer": answer})
