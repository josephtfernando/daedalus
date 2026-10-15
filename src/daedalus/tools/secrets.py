"""``secrets`` primitive — scoped credential access, never logged.

Values returned here are ``local_only`` and never enter a remote prompt
(ARCHITECTURE §10).
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..approvals.risk import Risk
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime


class SecretsTool(Tool):
    name = "secrets"
    description = "List available credential names, or fetch one by name for local use."
    risk = Risk.AUTO
    scopes = {"secrets": ["*"]}
    parameters = params(
        op=({"type": "string", "enum": ["list", "get"]}, True),
        name=({"type": "string"}, False),
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
