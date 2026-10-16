"""Runtime assembly — one object the whole loop shares.

Built once at startup; injected into every tool as ``ctx`` so a primitive never
has to reach for a global.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .approvals.middleware import ApprovalMiddleware
from .cabinet.seat import DEFAULT_SEAT
from .calendar.store import CalendarStore
from .capabilities.registry import CapabilityRegistry
from .capabilities.runner import CapabilityRunner
from .config import Settings
from .loop.context import history_key
from .memory.store import MemoryStore
from .model.client import ModelClient
from .model.providers import Provider
from .resources.registry import ResourceRegistry
from .resources.secrets import SecretStore
from .scheduler.proactive import SILENT
from .scheduler.scheduler import Scheduler
from .security.quarantine import Quarantine
from .storage import paths
from .surface.base import InboundEvent, Surface
from .tools.process import ProcessManager
from .tools.registry import ToolRegistry

log = logging.getLogger("daedalus.runtime")


@dataclass(slots=True)
class Session:
    """The current turn's ambient facts."""

    seat: str = DEFAULT_SEAT
    turn_ref: str = ""
    event: InboundEvent | None = None
    trusted_sources: list[str] = field(default_factory=list)
    untrusted_sources: list[str] = field(default_factory=list)
    # Whether `local_only` memory may enter this turn's prompt, set from the
    # resolved model chain. False by default: a turn that never declared a
    # local-only route must not leak local-only content.
    local_only_ok: bool = False


@dataclass
class Runtime:
    settings: Settings
    tools: ToolRegistry
    model: ModelClient
    memory: MemoryStore
    calendar: CalendarStore
    capabilities: CapabilityRegistry
    runner: CapabilityRunner
    scheduler: Scheduler
    approvals: ApprovalMiddleware
    resources: ResourceRegistry
    secrets: SecretStore
    processes: ProcessManager
    quarantine: Quarantine
    surface: Surface | None = None
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    session: Session | None = None

    # -- turn bookkeeping -------------------------------------------------

    def remember(self, event: InboundEvent, user_text: str, assistant_text: str) -> None:
        if not assistant_text:
            return
        key = history_key(event)
        thread = self.history.setdefault(key, [])
        thread.append({"role": "user", "content": user_text})
        thread.append({"role": "assistant", "content": assistant_text})
        del thread[:-24]

    def allows_local_only(self) -> bool:
        """Whether `local_only` values may take part in the current turn.

        Read from the session, which the engine sets from the resolved model
        chain. Absent a session, the answer is no — a caller that never
        declared a local route must not leak local-only content.
        """
        return bool(self.session and self.session.local_only_ok)

    def leak_guard(self, args: dict[str, Any]) -> str | None:
        """Bytes stay home: no secret value may ride out on an egress call."""
        blob = json.dumps(args, default=str)
        candidates: dict[str, str] = {}
        for name in self.secrets.names():
            value = self.secrets.get(name)
            if value:
                candidates[f"secret:{name}"] = value
        for key, value in {**self.settings.env, **os.environ}.items():
            if value and isinstance(value, str):
                candidates.setdefault(f"env:{key}", value)
        for label, value in candidates.items():
            if len(value) >= 8 and value in blob:
                return f"the outbound request contains the value of {label}"
        return None

    def set_surface(self, surface: Surface | None) -> None:
        self.surface = surface

    async def aclose(self) -> None:
        await self.processes.stop_all()

    # -- scheduled work ---------------------------------------------------

    async def wake(self, prompt: str) -> str:
        """The agent choosing to speak: compose fresh, don't template (SPEC §9).

        Returns the message, or ``""`` when the agent decided there was nothing
        worth saying. Silence here is a *decision*, so this never falls back to
        "I came back with nothing" — that fallback is right for a reply to the
        owner and wrong for a proactive message.

        The gate on whether a wake may speak at all is deterministic and lives
        in the scheduler; this is only reached once it has said yes.
        """
        from .loop.engine import Engine

        # Reset the session before composing the briefing, so the wake turn
        # starts from nothing rather than inheriting the last turn's ambient
        # facts. (`briefing()` withholds `local_only` on its own account — see
        # its docstring — but the session should not survive the handoff.)
        self.session = Session(seat=DEFAULT_SEAT, turn_ref="wake")
        event = InboundEvent(text=self.briefing(prompt), user_id="scheduler")
        result = await Engine(self).run_turn(event)
        text = (result.text or "").strip()
        if not text or text == SILENT:
            return ""
        return text

    def briefing(self, prompt: str, *, include_local_only: bool = False) -> str:
        """What the agent knows when it speaks first.

        "Not a template" still needs facts: the time, the day, what it already
        said, and how much room is left. Without the last one the model cannot
        judge whether this moment is worth the owner's attention — which is the
        whole decision it is being asked to make.

        ``include_local_only`` defaults to **False** rather than consulting the
        session: a wake is composed before its model chain is resolved, so it has
        no honest way to know where it is going. Withholding is the safe answer,
        and it means a DM that happened to route through a local model cannot
        hand the next wake the right to carry `local_only` values into a prompt
        that may well be remote.
        """
        now = datetime.now()
        ledger = self.scheduler.proactive
        last = ledger.last_text()
        lines = [
            "You are speaking first — nobody asked for this.",
            "",
            f"It is now {now.strftime('%A %d %B, %H:%M')}.",
            "",
            "Today and the next few days:",
            self.calendar.inventory(limit=5, allow_local_only=include_local_only),
            "",
            f"You have already spoken {ledger.spoken_today(now)} time(s) today, of "
            f"{self.settings.proactive_max_per_day} allowed.",
        ]
        if last:
            lines += [
                "",
                "Your last message to the owner was:",
                f"    {last}",
                "",
                "Do not repeat it.",
            ]
        if prompt:
            lines += ["", f"What brought you here: {prompt}"]
        lines += [
            "",
            "Say something only if it earns the interruption. If there is nothing worth the",
            f"owner's attention, reply with exactly {SILENT} and nothing else.",
        ]
        return "\n".join(lines)


def build_runtime(
    settings: Settings,
    *,
    surface: Surface | None = None,
    provider: Provider | None = None,
    tools: ToolRegistry | None = None,
) -> Runtime:
    paths.ensure_tree(settings.home)
    registry = tools or _default_tools()
    if provider is not None:
        model = ModelClient(settings, providers=[provider])
    else:
        model = ModelClient(settings)
    return Runtime(
        settings=settings,
        tools=registry,
        model=model,
        memory=MemoryStore(settings.home / "memory"),
        calendar=CalendarStore(settings.home / "calendar"),
        capabilities=CapabilityRegistry(settings.home / "capabilities"),
        runner=CapabilityRunner(settings.home / "capabilities"),
        scheduler=Scheduler(settings.home / "state"),
        approvals=ApprovalMiddleware(settings),
        resources=ResourceRegistry(settings.home / "resources" / "registry.toml"),
        secrets=SecretStore(settings.home),
        processes=ProcessManager(),
        quarantine=Quarantine(),
        surface=surface,
    )


def _default_tools() -> ToolRegistry:
    from .tools import build_registry

    return build_registry()
