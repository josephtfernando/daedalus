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
    title = "Remember or recall something"
    description = (
        "Store a durable fact about the owner, or recall facts. "
        "Every fact carries provenance, confidence, and supersession."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["state"]}
    parameters = params(
        op=({"type": "string", "enum": ["write", "recall", "list", "supersede"]}, True),
        facet=({"type": "string", "description": "e.g. projects, body, research, application_pipeline"}, False),
        content=(
            {
                "type": "string",
                "description": (
                    "the fact to store, for op=write; the *new* fact, for op=supersede. Pass \"\" "
                    "for op=recall and op=list, which query instead of writing."
                ),
            },
            True,
        ),
        query=(
            {
                "type": "string",
                "description": (
                    "the search text, for op=recall. Pass \"\" for write, list, and supersede. An "
                    "empty query matches nothing, so a recall without it looks like an empty memory."
                ),
            },
            True,
        ),
        id=(
            {
                "type": "string",
                "description": (
                    "the entry to retire, for op=supersede — recall it first to get its id. "
                    "Pass \"\" for the other ops."
                ),
            },
            False,
        ),
        data_class=(
            {
                "type": "string",
                "enum": ["public", "private", "local_only"],
                "description": (
                    "how sensitive this fact is. 'local_only' means it must never reach a remote "
                    "model — use it for anything that must not leave the machine."
                ),
            },
            False,
        ),
        include_invalid=(
            {
                "type": "boolean",
                "default": False,
                "description": (
                    "for op=recall: include facts that have been superseded or expired. Off by "
                    "default, so recall returns what is true now."
                ),
            },
            False,
        ),
        confidence=({"type": "number", "default": 0.8}, False),
        origin=({"type": "string", "enum": ["trusted_owner", "local_derived", "untrusted_source"]}, False),
    )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        if op == "write":
            return self._write(args, ctx)
        if op == "supersede":
            return self._supersede(args, ctx)
        if op == "recall":
            query = str(args.get("query", "")).strip()
            hits = ctx.memory.recall(
                query,
                facet=args.get("facet"),
                include_invalid=bool(args.get("include_invalid", False)),
            )
            if not ctx.allows_local_only():
                hits = [h for h in hits if h.data_class is not DataClass.LOCAL_ONLY]
            return ToolResult(
                output="\n".join(f"- [{h.id}] [{h.facet}] {h.content}" for h in hits) or "(nothing found)",
                data={"count": len(hits)},
            )
        if op == "list":
            hits = ctx.memory.list_facet(args.get("facet"))
            if not ctx.allows_local_only():
                hits = [h for h in hits if h.data_class is not DataClass.LOCAL_ONLY]
            return ToolResult(
                output="\n".join(f"- [{h.id}] [{h.facet}] {h.content}" for h in hits) or "(empty)",
                data={"count": len(hits)},
            )
        return ToolResult.error(f"unknown op {op!r}")

    def _write(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        content = str(args.get("content", "")).strip()
        if not content:
            return ToolResult.error("write needs content")
        facet = str(args.get("facet", "uncategorized"))
        entry = ctx.memory.write(
            facet=facet,
            content=content,
            origin=Origin(args.get("origin", "trusted_owner")),
            data_class=DataClass(args.get("data_class", "private")),
            confidence=float(args.get("confidence", 0.8)),
            source_ref=ctx.session.turn_ref if ctx.session else None,
        )
        return ToolResult(output=f"remembered [{facet}] {entry.id}", data={"id": entry.id, "facet": facet})

    def _supersede(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        old_id = str(args.get("id", "")).strip()
        content = str(args.get("content", "")).strip()
        if not old_id or not content:
            return ToolResult.error(
                "op=supersede needs `id` (the entry to retire) and `content` (the fact that "
                "replaces it); recall the old fact first to get its id."
            )
        old = ctx.memory.get(old_id)
        if old is None:
            return ToolResult.error(f"no memory entry {old_id!r}")
        new = ctx.memory.write(
            facet=str(args.get("facet") or old.facet),
            content=content,
            origin=Origin(args.get("origin", "trusted_owner")),
            data_class=DataClass(args.get("data_class", old.data_class.value)),
            confidence=float(args.get("confidence", 0.8)),
            source_ref=ctx.session.turn_ref if ctx.session else None,
        )
        ok = ctx.memory.supersede(old_id, new)
        if not ok:
            return ToolResult.error(f"could not retire {old_id!r}")
        return ToolResult(
            output=f"remembered {new.id}; retired {old_id}",
            data={"id": new.id, "superseded": old_id},
        )
