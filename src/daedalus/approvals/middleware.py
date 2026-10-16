"""The deterministic "dispose" step: decide allow / ask / block.

LLMs never own this decision (SPEC §1, ARCHITECTURE §2 P1). The middleware sees
the tool's declared scopes and risk, the authorizing provenance, and the grant
store — never the model's opinion.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import Settings
from ..security.sink import may_authorize
from ..security.provenance import Labeled
from .classify import Assessment
from .grants import Grant, Grants
from .risk import Risk


@dataclass(slots=True)
class Decision:
    allow: bool
    ask: bool
    risk: Risk
    reason: str
    action: str
    scope: str
    action_hash: str
    assessment: Assessment | None = None

    @property
    def blocked(self) -> bool:
        return not self.allow and not self.ask


class DecisionLog:
    """Append-only audit of every authorize decision."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS decisions ("
                "  ts REAL, action TEXT, scope TEXT, action_hash TEXT,"
                "  risk TEXT, decision TEXT, reason TEXT)"
            )

    def write(self, d: Decision, *, now: float | None = None) -> None:
        outcome = "allow" if d.allow else ("ask" if d.ask else "block")
        with sqlite3.connect(self.path) as c:
            c.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?)",
                (now or time.time(), d.action, d.scope, d.action_hash, d.risk.value, outcome, d.reason),
            )


class ApprovalMiddleware:
    def __init__(
        self,
        settings: Settings,
        *,
        grants: Grants | None = None,
        log: DecisionLog | None = None,
    ) -> None:
        self.settings = settings
        self.grants = grants or Grants(settings.home / "approvals" / "grants.toml")
        self.log = log or DecisionLog(settings.home / "approvals" / "log.db")

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def action_hash(tool_name: str, args: dict[str, Any]) -> str:
        blob = json.dumps({"tool": tool_name, "args": args}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def derive_scope(self, tool_name: str, args: dict[str, Any]) -> str:
        url = args.get("url")
        if isinstance(url, str) and url:
            host = urlparse(url).hostname
            if host:
                return f"net:{host}"
        target = args.get("target") or args.get("path")
        if isinstance(target, str) and target:
            return f"{tool_name}:{target}"
        # A tool that is several operations in one (`capability` authors, runs,
        # schedules, and deletes) must not let consent for one cover another.
        op = args.get("op")
        if isinstance(op, str) and op:
            return f"{tool_name}:{op}"
        return f"{tool_name}:*"

    # -- the decision -----------------------------------------------------

    def authorize(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        risk: Risk,
        scopes: dict[str, Any] | None = None,
        authorizing: list[Labeled] | None = None,
        assessment: Assessment | None = None,
    ) -> Decision:
        scopes = scopes or {}
        scope = self.derive_scope(tool_name, args)
        ahash = self.action_hash(tool_name, args)

        def decide(allow: bool, ask: bool, reason: str) -> Decision:
            d = Decision(
                allow=allow,
                ask=ask,
                risk=risk,
                reason=reason,
                action=tool_name,
                scope=scope,
                action_hash=ahash,
                assessment=assessment,
            )
            self.log.write(d)
            return d

        # 1. Sink policy — untrusted text can never authorize.
        if authorizing:
            ok, why = may_authorize(authorizing)
            if not ok:
                return decide(False, False, f"sink: {why}")

        # 2. Hard block — by policy, or because the classifier refused the call.
        if risk is Risk.BLOCK:
            if assessment is not None and assessment.reasons:
                return decide(False, False, f"refused: {assessment.summary} — {assessment.reasons[0]}")
            return decide(False, False, "tool risk tier is block")

        egress = bool(scopes.get("net"))

        # 3. Self-contained actions are auto.
        if not egress and risk is not Risk.ASK:
            return decide(True, False, "self-contained")

        # 4. Standing grant?
        grant = self.grants.find(tool_name, scope)
        if grant is not None:
            return decide(True, False, f"standing grant ({grant.scope})")

        # 5. Everything else asks.
        if egress:
            return decide(False, True, "this leaves your machine — approving it remembers the host")
        # Reached by an ask-tier local tool (shell, code, capability authoring).
        # Saying "egress" here was simply wrong, and told the owner the agent was
        # trying to leave the machine when it wanted to run `date`. The reason now
        # says why a local action needs consent at all.
        return decide(False, True, "it can change things on this machine, so it asks first")

    def grant_from(self, decision: Decision, *, note: str = "", expires_at: float | None = None) -> Grant:
        return self.grants.add(
            Grant(action=decision.action, scope=decision.scope, expires_at=expires_at, note=note)
        )
