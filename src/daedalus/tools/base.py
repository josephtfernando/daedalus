"""Tool protocol (ARCHITECTURE §9.1).

The model sees only ``name + description + schema``. Risk and scopes are
invisible to it and enforced by the approval middleware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..approvals.classify import Assessment, Level
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
    # The human name for an approval prompt. The owner should read
    # "Run a shell command", never `shell` on `shell:*`.
    title: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk: Risk = Risk.AUTO
    scopes: dict[str, Any] = {}
    # Whether an approval for this tool may be remembered as a standing grant.
    # False when no scope on the call actually describes what was approved —
    # a `shell` approval covers one command and nothing else.
    grantable: bool = True

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def title_for(self, args: dict[str, Any]) -> str:
        """The headline for this call's approval prompt.

        The static ``title`` is right for a single-purpose tool. A
        multi-operation tool overrides this: otherwise a delete is announced as
        "Author a capability", which is the first thing the owner reads and the
        reason they stop trusting the prompt.
        """
        return self.title

    def approval_labels(self, args: dict[str, Any]) -> tuple[str, str]:
        """(affirm, deny) button labels for this call.

        Generic Approve/Deny forces a re-read to know what is being chosen. A
        tool that can say a sharper pair returns one here.
        """
        return ("Approve", "Deny")

    def risk_for(self, args: dict[str, Any]) -> Risk:
        """Risk can depend on the call. Default: the tool's static tier.

        The model never sees this and cannot influence it — it is read by the
        middleware from the tool itself.
        """
        return self.risk

    def assess(self, args: dict[str, Any]) -> Assessment:
        """Say what this call does, and how bad it could get — in the owner's words.

        Derived from the arguments alone. A tool that can say something sharper
        about its own danger overrides this; ``shell`` judges the actual command.

        The default level follows the tool's own risk tier and nothing else. A
        tool that asks for approval is, by definition, not known to be
        read-only — labelling an unknown ask-tier call "read-only" is how a
        dangerous prompt gets waved through.
        """
        return Assessment(
            level=Level.CAUTION if self.risk is Risk.ASK else Level.OK,
            summary=self.title or f"use the {self.name} tool",
        )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:  # pragma: no cover
        raise NotImplementedError(f"{self.name} does not implement run()")


_PROPS = "properties"


def sent_args(args: dict[str, Any]) -> str:
    """The argument names a call actually carried, so an error can echo them.

    A model that reached for a plausible alias (``id`` for ``uid``) is otherwise
    told only *what is missing*, and repeats the same wrong key — which is
    exactly the loop this is here to break, in ``calendar`` and ``schedule``
    alike. Empty placeholders are not named, so the list is what the model meant
    to say rather than what the schema padded the call with.
    """
    names = [
        key for key, value in args.items() if key != "op" and value not in ("", None, [], {})
    ]
    return ", ".join(f"`{name}`" for name in names) if names else "(no arguments)"


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
