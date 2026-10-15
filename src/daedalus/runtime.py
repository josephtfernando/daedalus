"""Runtime assembly — one object the whole loop shares.

Built once at startup; injected into every tool as ``ctx`` so a primitive never
has to reach for a global.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .approvals.middleware import ApprovalMiddleware
from .cabinet.seat import DEFAULT_SEAT
from .capabilities.registry import CapabilityRegistry
from .capabilities.runner import CapabilityRunner
from .config import Settings
from .loop.context import history_key
from .memory.store import MemoryStore
from .model.client import ModelClient
from .model.providers import Provider
from .resources.registry import ResourceRegistry
from .resources.secrets import SecretStore
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


@dataclass
class Runtime:
    settings: Settings
    tools: ToolRegistry
    model: ModelClient
    memory: MemoryStore
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
        """The agent choosing to speak: compose fresh, don't template (SPEC §9)."""
        from .loop.engine import Engine

        event = InboundEvent(text=prompt or "Compose a short, useful message for the owner.", user_id="scheduler")
        engine = Engine(self)
        from .surface.base import InboundEvent as _E  # noqa: F401

        self.session = Session(seat=DEFAULT_SEAT, turn_ref="wake")
        result = await engine.run_turn(event)
        return result.text


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
