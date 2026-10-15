"""Surface port + shared event shapes (ARCHITECTURE §4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class InboundEvent:
    text: str
    user_id: str
    channel_id: str = ""
    thread_id: str | None = None
    is_dm: bool = True
    source: str = "discord"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ApprovalRequest:
    action: str
    scope: str
    reason: str
    args: dict[str, Any]
    action_hash: str

    def describe(self) -> str:
        detail = self.args.get("url") or self.args.get("command") or self.args.get("query") or ""
        head = f"**Approval needed** — `{self.action}` on `{self.scope}`"
        body = f"\n{detail}" if detail else ""
        return f"{head}\n_{self.reason}_{body}"


@runtime_checkable
class Surface(Protocol):
    async def send(self, text: str, *, thread: str | None = None) -> None: ...

    async def ask(self, question: str, *, options: list[str] | None = None) -> str: ...

    async def approve(self, request: ApprovalRequest) -> bool: ...
