"""Runtime assembly — one object the whole loop shares.

Built once at startup; injected into every tool as ``ctx`` so a primitive never
has to reach for a global.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .approvals.middleware import ApprovalMiddleware
from .cabinet.namespace import memory_for
from .cabinet.seat import DEFAULT_SEAT, Seat, active_seat, builtin_charters, load_charters, pick
from .calendar.store import CalendarStore
from .capabilities.registry import CapabilityRegistry
from .capabilities.runner import CapabilityRunner
from .config import Settings
from .delegate.runner import DelegateRunner
from .loop.context import history_key
from .loop.transcript import MAX_MESSAGES as TRANSCRIPT_LIMIT
from .loop.transcript import Transcript
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
    #: Every namespace of memory. The store a turn actually reads is
    #: :attr:`memory` — the active seat's handle onto this root.
    memory_root: MemoryStore
    calendar: CalendarStore
    capabilities: CapabilityRegistry
    runner: CapabilityRunner
    scheduler: Scheduler
    approvals: ApprovalMiddleware
    resources: ResourceRegistry
    secrets: SecretStore
    processes: ProcessManager
    quarantine: Quarantine
    #: The seats the owner has, charters and all. Loaded from `cabinet/seats/*.toml`
    #: at startup, so editing a charter changes what the next turn answers from.
    seats: dict[str, Seat] = field(default_factory=builtin_charters)
    surface: Surface | None = None
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Where `history` is kept between runs. ``None`` means in-memory only,
    #: which is what a hand-built Runtime in a test gets.
    transcript: Transcript | None = None
    session: Session | None = None
    #: One turn at a time, for the whole agent. A turn writes `session` (the
    #: ingress policy every tool reads, `local_only` included) and appends to the
    #: transcript, so two turns in flight are two turns overwriting each other's
    #: state and answering the owner out of order. There is one owner and one
    #: loop, so there is one turn at a time: a second message waits rather than
    #: races. See `Engine.run_turn`.
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The seat that last handled each conversation — channel id to seat key.
    #: Routing reads it so a follow-up with no subject of its own ("and the
    #: second one?") stays with the seat the owner was already talking to,
    #: instead of being re-judged on every message. In memory only: a restart
    #: loses the thread, and the next message is routed fresh, which is the
    #: honest outcome rather than a wrong continuity.
    last_seat: dict[str, str] = field(default_factory=dict)
    #: The delegation runner (T29/T30). Owns the workspaces under
    #: `delegate/ws` and the caps every delegate child runs under. Built
    #: against `settings` unless a caller supplies one (tests use a private
    #: root so real delegations never land in the owner's tree).
    delegate_runner: DelegateRunner | None = None

    # -- turn bookkeeping -------------------------------------------------

    def __post_init__(self) -> None:
        if self.delegate_runner is None:
            self.delegate_runner = DelegateRunner(self.settings.home / "delegate" / "ws")

    @property
    def seat(self) -> Seat:
        """The seat this turn runs as. Never nothing: a turn always has an identity."""
        return pick(self.seats, self.session.seat if self.session else None)

    @property
    def memory(self) -> MemoryStore:
        """The memory this turn reads and writes through: the active seat's namespace.

        A property rather than a field, so no tool call can reach the wrong
        namespace by forgetting an argument — `ctx.memory` *is* the seat's store.
        With no session (startup, the self-test, a CLI probe) it is the
        coordinator's, which is the namespace every fact written before seats had
        namespaces already lives in.
        """
        return memory_for(self.memory_root, self.seat)

    def remember(self, event: InboundEvent, user_text: str, assistant_text: str) -> None:
        """Add one exchange to this conversation's running record.

        Called on **every** way a turn can end, not only the ones that end in
        prose. The owner still said something when a turn was cut short by the
        repeat guard, the step limit, or a denial — and the next turn has to
        know it. Recording only the happy path is what let one evening of the
        acceptance transcript ask the same question four times with no memory of
        the three answers it had already been given.
        """
        if not user_text and not assistant_text:
            return
        key = history_key(event)
        thread = list(self.history.get(key) or [])
        if user_text:
            thread.append({"role": "user", "content": user_text})
        if assistant_text:
            thread.append({"role": "assistant", "content": assistant_text})
        thread = thread[-TRANSCRIPT_LIMIT:]
        self.history[key] = thread
        if self.transcript is not None:
            self.transcript.write(key, thread)

    def recent(self, key: str, limit: int) -> list[dict[str, Any]]:
        """The last few messages of a conversation, from memory or from disk.

        Memory first because it is the live copy; disk second, so a conversation
        that began before a restart still has its opening moves.
        """
        thread = self.history.get(key)
        if not thread and self.transcript is not None:
            thread = self.transcript.read(key)
            if thread:
                self.history[key] = thread
        return list(thread or [])[-limit:]

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

    async def wake(self, prompt: str, *, seat: str | None = None) -> str:
        """The agent choosing to speak: compose fresh, don't template (SPEC §9).

        Returns the message, or ``""`` when the agent decided there was nothing
        worth saying. Silence here is a *decision*, so this never falls back to
        "I came back with nothing" — that fallback is right for a reply to the
        owner and wrong for a proactive message.

        The gate on whether a wake may speak at all is deterministic and lives
        in the scheduler; this is only reached once it has said yes. A wake a
        scheduler job owns may name its **seat** (T21): the message is then that
        seat's — its charter, its memory namespace — rather than the active
        identity's, which is how Odysseus's weekly sweep is Odysseus's work and
        not the coordinator's wearing a hat. An unknown seat is refused here as
        loudly as in the scheduler: delivering it as someone else is the quiet
        substitution the cabinet exists to make impossible.
        """
        from .loop.engine import Engine

        if seat:
            from .cabinet.seat import UnknownSeat as _Unknown, resolve as _resolve

            try:
                seat = _resolve(self.seats, seat).key
            except _Unknown as exc:
                raise ValueError(
                    f"the wake names seat {seat!r}, and nobody in the cabinet answers to it"
                ) from exc

        # Reset the session before composing the briefing, so the wake turn
        # starts from nothing rather than inheriting the last turn's ambient
        # facts. (`briefing()` withholds `local_only` on its own account — see
        # its docstring — but the session should not survive the handoff.) The
        # wake speaks as the **named** seat when one owns the job, and as the
        # **active** seat otherwise: the agent reaches out as an identity the
        # owner can put a name to, never as a second anonymous one.
        self.session = Session(seat=seat or active_seat(self.seats).key, turn_ref="wake")
        event = InboundEvent(text=self.briefing(prompt), user_id="scheduler")
        # The seat is passed rather than left to resolve on its own: a wake is
        # not the owner's message and has no subject to route, so it speaks as
        # the active seat and no routing question is asked about it.
        result = await Engine(self).run_turn(event, seat=self.session.seat)
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
        memory_root=MemoryStore(settings.home / "memory"),
        calendar=CalendarStore(settings.home / "calendar"),
        capabilities=CapabilityRegistry(settings.home / "capabilities"),
        runner=CapabilityRunner(settings.home / "capabilities"),
        scheduler=Scheduler(settings.home / "state"),
        approvals=ApprovalMiddleware(settings),
        resources=ResourceRegistry(settings.home / "resources" / "registry.toml"),
        secrets=SecretStore(settings.home),
        processes=ProcessManager(settings.home / "state" / "processes.json"),
        quarantine=Quarantine(),
        seats=load_charters(settings.home),
        surface=surface,
        transcript=Transcript(settings.home / "state" / "history"),
    )


def _default_tools() -> ToolRegistry:
    from .tools import build_registry

    return build_registry()
