"""Controls on the dashboard, routed through the approval split (SPEC §3.6).

*"Visual → dashboard, action → Discord."* The page may **propose**; what happens
next is not the page's to decide. A control is resolved to a **real tool call**
and put through the same :meth:`ApprovalMiddleware.authorize` the loop's own tool
calls go through — the tool's own risk tier, its own scopes, its own assessment —
so a control is a way *into* the same decision, never a way around it.

That is the whole reason this module exists as a seam rather than a handler: if
the dashboard grew its own idea of what a delete is allowed to do, there would be
two approval policies and the weaker one would win by being the one a browser
could reach.

**The vocabulary is closed.** What the page may propose is written down in
:data:`CONTROLS`, and anything else is refused before the middleware is reached.
The model never composes controls: the strip is built by code in
:mod:`.dashboard`, so a page a model authored carries no actions at all.

**Asking is not hanging.** A control that needs the owner returns *"waiting"*
immediately and the buttons are surfaced on Discord from the daemon's loop. The
HTTP request must not sit open for ten minutes waiting on a person.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..approvals.middleware import Decision
from ..surface.base import ApprovalRequest

if TYPE_CHECKING:
    from ..runtime import Runtime
    from ..tools.base import Tool

log = logging.getLogger("daedalus.genui.actions")

#: The controls the page may propose, as ``<tool>.<op>``. Reads are not here:
#: the page *is* the read. Writes are, and only these — a control is not a
#: general remote for every tool the loop has.
CONTROLS: frozenset[str] = frozenset(
    {
        "calendar.create",
        "calendar.update",
        "calendar.delete",
        # The roots editor (T35 / carried from T34): the page proposes, the
        # tool's own risk posture decides — and the tool is an ASK, so either
        # way the owner is the one who widens or shrinks reach.
        "settings.roots-add",
        "settings.roots-remove",
    }
)

#: How long a control may wait on the daemon's loop before the answer is
#: "it did not run". Generous, because a Discord approval is a person deciding.
_RUN_TIMEOUT = 60.0


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a control did — or, more often, why it did not."""

    #: ``done`` ran; ``waiting`` needs the owner and is on Discord now;
    #: ``refused`` did not run and says why.
    status: str
    message: str
    #: The HTTP status the answer deserves: 200, 202, 400, 403, 503.
    code: int
    detail: str = ""


class Controls:
    """The dashboard's side of the approval split (SPEC §3.6)."""

    def __init__(
        self,
        ctx: "Runtime",
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.ctx = ctx
        #: The daemon's loop. Given, the tool runs *on it* — one thread touches
        #: the calendar, and an approval can reach the surface. Absent (a unit
        #: test, a probe), the tool runs inline, and a control that needs the
        #: owner is refused: there is nobody attached to ask.
        self.loop = loop

    # -- the one entry point ----------------------------------------------

    def perform(self, control: str, args: dict[str, Any] | None = None) -> Outcome:
        """Propose one control. Returns what happened, never raises for a refusal."""
        name, _, op = str(control or "").strip().partition(".")
        args = dict(args or {})
        if not op or control not in CONTROLS:
            return Outcome(
                "refused",
                f"{control!r} is not a control this page offers — the page may "
                f"propose: {', '.join(sorted(CONTROLS))}",
                400,
            )
        tool = self.ctx.tools.get(name)
        if tool is None:
            return Outcome(
                "refused",
                f"{control!r} names {name!r}, which this install does not have",
                400,
            )
        args["op"] = op

        # The agenda prints one instance of a series as `uid@start` — the same
        # address the picker above offers — and the tool takes `uid` and
        # `recurrence_id` apart. Splitting it here is the one piece of argument
        # composition a control does, and it happens *before* the middleware so
        # the decision covers the call that will actually run.
        if str(args.get("uid", "")).count("@") and str(args["uid"]).rsplit("@", 1)[1][:1].isdigit():
            args["uid"], args["recurrence_id"] = str(args["uid"]).rsplit("@", 1)

        # The decision the tool's own call would get: the same function, fed the
        # same tool object, the same arguments. Nothing about "this came from a
        # page" relaxes it.
        decision = self.ctx.approvals.authorize(
            tool.name,
            args,
            risk=tool.risk_for(args),
            scopes=tool.scopes,
            assessment=tool.assess(args),
        )
        if decision.blocked:
            return Outcome("refused", f"refused by policy: {decision.reason}", 403)
        if decision.ask:
            return self._ask(decision, tool, args)
        return self._run(tool, args)

    # -- running -----------------------------------------------------------

    def _run(self, tool: "Tool", args: dict[str, Any]) -> Outcome:
        try:
            result = self._await(tool.run(args, self.ctx))
        except Exception as exc:  # noqa: BLE001 — a tool crash is an answer, not a crash
            log.exception("control %s failed", tool.name)
            return Outcome("refused", f"{tool.name} failed: {exc}", 400)
        if result.ok:
            return Outcome("done", result.output, 200, result.output)
        # A malformed control (no `start`, no such event) is the tool's own
        # refusal, and it says what it wanted — pass it through untouched.
        return Outcome("refused", result.output, 400, result.output)

    def _await(self, coro: Any) -> Any:
        """Run one coroutine to completion, on the daemon's loop when there is one."""
        if self.loop is not None and self.loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=_RUN_TIMEOUT)
        return asyncio.run(coro)

    # -- asking ------------------------------------------------------------

    def _ask(self, decision: Decision, tool: "Tool", args: dict[str, Any]) -> Outcome:
        if self.ctx.surface is None or self.loop is None:
            return Outcome(
                "refused",
                "this control needs the owner's approval and nothing is attached to "
                "ask — the dashboard's controls run with the daemon (`daedalus run`)",
                503,
            )
        assessment = tool.assess(args)
        request = ApprovalRequest(
            action=decision.action,
            scope=decision.scope,
            reason=decision.reason,
            args=args,
            action_hash=decision.action_hash,
            title=tool.title_for(args),
            summary=assessment.summary,
            level=assessment.level.value,
            note=assessment.note(),
            reasons=assessment.reasons,
            affirm_label=tool.approval_labels(args)[0],
            deny_label=tool.approval_labels(args)[1],
        )
        log.info("control %s needs approval (%s)", decision.action, decision.scope)
        asyncio.run_coroutine_threadsafe(
            self._approve_then_run(request, tool, args, decision, assessment), self.loop
        )
        return Outcome(
            "waiting",
            "waiting on your approval — the buttons are in Discord; the page will "
            "show the change once it is made",
            202,
            json.dumps(args, sort_keys=True, default=str),
        )

    async def _approve_then_run(
        self,
        request: ApprovalRequest,
        tool: "Tool",
        args: dict[str, Any],
        decision: Decision,
        assessment: Any,
    ) -> None:
        """The half of the engine's ask-branch that outlives the HTTP request."""
        approved = await self.ctx.surface.approve(request)
        if not approved:
            await self.ctx.surface.send(
                f"Denied from the dashboard — nothing ran: {request.describe()}"
            )
            return
        # The loop remembers an approval it was given in conversation; a yes
        # given on a button is the same kind of yes, so it is remembered the
        # same way — or a control would ask every single time.
        if tool.grantable and not assessment.dangerous:
            self.ctx.approvals.grant_from(decision, note="approved from the dashboard")
        result = await tool.run(args, self.ctx)
        await self.ctx.surface.send(
            f"Ran from the dashboard: {result.output or ('done' if result.ok else 'failed')}"
        )
