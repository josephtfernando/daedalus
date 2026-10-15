"""Tool protocol (ARCHITECTURE §9.1).

The model sees only ``name + description + schema``. Risk and scopes are
invisible to it and enforced by the approval middleware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..approvals.risk import Risk

if TYPE_CHECKING:
    from ..runtime import Runtime


@dataclass(slots=True)
class ToolResult:
    ok: bool = True
    output: str = ""
    data: dict[str, Any] | None = None

    @classmethod
    def error(cls, message: str) -> "ToolResult":
        return cls(ok=False, output=message)

    def for_model(self) -> dict[str, Any]:
        return {"ok": self.ok, "output": self.output}


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk: Risk = Risk.AUTO
    scopes: dict[str, Any] = {}

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def risk_for(self, args: dict[str, Any]) -> Risk:
        """Risk can depend on the call. Default: the tool's static tier.

        The model never sees this and cannot influence it — it is read by the
        middleware from the tool itself.
        """
        return self.risk

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:  # pragma: no cover
        raise NotImplementedError(f"{self.name} does not implement run()")


_PROPS = "properties"


def params(**props: Any) -> dict[str, Any]:
    """Build an object schema; ``required`` props are marked with a tuple value."""
    required: list[str] = []
    properties: dict[str, Any] = {}
    for key, spec in props.items():
        if isinstance(spec, tuple):
            properties[key] = spec[0]
            if spec[1]:
                required.append(key)
        else:
            properties[key] = spec
    out: dict[str, Any] = {"type": "object", _PROPS: properties}
    if required:
        out["required"] = required
    return out
