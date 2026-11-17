"""Surface port + shared event shapes (ARCHITECTURE §4)."""

from __future__ import annotations

import json
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
    # What the tool said about its own call (approvals/classify.py). Absent for
    # callers that do not have an assessment to give.
    title: str = ""
    summary: str = ""
    level: str = "ok"
    note: str = ""
    reasons: tuple[str, ...] = ()
    #: What the action literally does (approvals/explain.py) — the facts, not
    #: the risk label. This is the difference between a prompt the owner reads
    #: and one they click through.
    effects: tuple[str, ...] = ()
    #: What saying yes also remembers, when it remembers anything ("approving
    #: this host"). Consent that quietly becomes standing is worse than no
    #: consent at all, so the prompt says it before the click, not after.
    remembers: str = ""
    detail: str = ""
    # What the two buttons say. The tool supplies them so the choice names the
    # action rather than a generic "Approve".
    affirm_label: str = "Approve"
    deny_label: str = "Deny"

    def detail_text(self) -> str:
        """The concrete thing being authorized: the command, the URL, the args."""
        if self.detail:
            return self.detail[:_MAX_DETAIL]
        return _render_args(self.args)

    def describe(self) -> str:
        lines = [f"**Approval needed — {self.title or self.action}**", ""]
        # The title is already a sentence about the action; repeating it as the
        # summary would be noise. Only show a summary that says something more.
        if self.summary and self.summary != self.title:
            lines.append(f"**What it does:** {self.summary}")
        lines.append(f"**Risk:** {self.note or self.level}")
        for reason in self.reasons:
            lines.append(f"    • {reason}")
        # The facts, above the code: what it will touch, where it will write,
        # who it will talk to. A risk label without this is a mood; this is the
        # part the owner can actually judge.
        if self.effects:
            lines.extend(["", "**What this will do:**"])
            for effect in self.effects:
                lines.append(f"    • {effect}")
        detail = self.detail_text()
        if detail:
            lines.extend(["", f"```\n{detail}\n```"])
        if self.remembers:
            lines.extend(["", f"_Saying yes also remembers this: {self.remembers}._"])
        lines.extend(["", f"_{self.reason}_"])
        # What declining costs, said plainly: a prompt with no stated
        # consequence is a prompt that invites guessing.
        lines.append("_If you say no, nothing runs and the turn carries on._")
        return "\n".join(lines)


# The bits of a call worth showing the owner, in the order they read best.
_DETAIL_KEYS = (
    "command", "url", "method", "path", "query", "name", "target", "op",
    "language", "source", "text", "schedule", "cron", "kind", "facet", "run_py",
)
_MAX_DETAIL = 900
_MAX_VALUE = 300


def _render_args(args: dict[str, Any]) -> str:
    command = args.get("command")
    if isinstance(command, str) and command.strip():
        body = command.strip()
        extra = [f"{key}: {args[key]}" for key in ("cwd",) if args.get(key)]
        return (body + "\n\n" + "\n".join(extra) if extra else body)[:_MAX_DETAIL]

    lines: list[str] = []
    for key in _DETAIL_KEYS:
        value = args.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str):
            text = value if len(value) <= _MAX_VALUE else f"…({len(value)} characters)"
        else:
            text = json.dumps(value, default=str)
            if len(text) > _MAX_VALUE:
                text = f"…({len(text)} characters)"
        lines.append(f"{key}: {text}")
    return "\n".join(lines)[:_MAX_DETAIL]


@runtime_checkable
class Surface(Protocol):
    async def send(self, text: str, *, thread: str | None = None) -> None: ...

    async def ask(self, question: str, *, options: list[str] | None = None) -> str: ...

    async def approve(self, request: ApprovalRequest) -> bool: ...
