"""``secrets`` primitive — scoped credential access, never logged.

Values returned here are ``local_only`` and never enter a remote prompt
(ARCHITECTURE §10).
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime


class SecretsTool(Tool):
    name = "secrets"
    title = "Use a stored secret"
    description = "List available credential names, or fetch one by name for local use."
    risk = Risk.AUTO
    scopes = {"secrets": ["*"]}
    parameters = params(
        op=({"type": "string", "enum": ["list", "get"]}, True),
        name=({"type": "string"}, False),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", ""))
        if op == "list":
            return Assessment(level=Level.OK, summary="list the names of your provisioned credentials")
        name = str(args.get("name", "")) or "a credential"
        return Assessment(
            level=Level.CAUTION,
            summary=f"use the stored secret {name!r}",
            reasons=("its value is read into the workspace; it still never leaves the machine",),
        )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        store = ctx.secrets
        if op == "list":
            names = store.names()
            return ToolResult(output="\n".join(names) or "(no credentials provisioned)")
        if op == "get":
            name = str(args.get("name", "")).strip()
            value = store.get(name)
            if value is None:
                return ToolResult.error(f"no credential named {name!r}")
            return ToolResult(output=value, data={"local_only": True, "secret": True})
        return ToolResult.error(f"unknown op {op!r}")
