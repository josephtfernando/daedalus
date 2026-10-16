"""``memory`` primitive — read and write facts (see memory/store.py)."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.risk import Risk
from ..security.provenance import DataClass, Origin
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime


class MemoryTool(Tool):
    name = "memory"
    description = (
        "Store a durable fact about the owner, or recall facts. "
        "Every fact carries provenance, confidence, and supersession."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["state"]}
    parameters = params(
        op=({"type": "string", "enum": ["write", "recall", "list"]}, True),
        facet=({"type": "string", "description": "e.g. projects, body, research, application_pipeline"}, False),
        content=(
            {
                "type": "string",
                "description": (
                    "the fact to store, for op=write. Pass \"\" for op=recall and op=list, which "
                    "query instead of writing."
                ),
            },
            True,
        ),
        query=(
            {
                "type": "string",
                "description": (
                    "the search text, for op=recall. Pass \"\" for op=write and op=list. An empty "
                    "query matches nothing, so a recall without it looks like an empty memory."
                ),
            },
            True,
        ),
        confidence=({"type": "number", "default": 0.8}, False),
        origin=({"type": "string", "enum": ["trusted_owner", "local_derived", "untrusted_source"]}, False),
    )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        if op == "write":
            content = str(args.get("content", "")).strip()
            if not content:
                return ToolResult.error("write needs content")
            facet = str(args.get("facet", "uncategorized"))
            entry = ctx.memory.write(
                facet=facet,
                content=content,
                origin=Origin(args.get("origin", "trusted_owner")),
                data_class=DataClass.PRIVATE,
                confidence=float(args.get("confidence", 0.8)),
                source_ref=ctx.session.turn_ref if ctx.session else None,
            )
            return ToolResult(output=f"remembered [{facet}] {entry.id}", data={"id": entry.id, "facet": facet})
        if op == "recall":
            query = str(args.get("query", "")).strip()
            hits = ctx.memory.recall(query, facet=args.get("facet"))
            return ToolResult(
                output="\n".join(f"- [{h.facet}] {h.content}" for h in hits) or "(nothing found)",
                data={"count": len(hits)},
            )
        if op == "list":
            hits = ctx.memory.list_facet(args.get("facet"))
            return ToolResult(output="\n".join(f"- [{h.facet}] {h.content}" for h in hits) or "(empty)")
        return ToolResult.error(f"unknown op {op!r}")
