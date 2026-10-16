"""Scheduler — cron-pumped wake (SPEC §3.5, §9, ARCHITECTURE §7.3).

A scheduled capability runs its artifact directly with **no model in the loop**.
A ``wake`` job, by contrast, is the agent *choosing to speak*: the model
composes the message fresh (SPEC §9), and may choose not to.

The gate on *whether* a wake may speak lives in :mod:`.proactive` and is
deterministic — quiet hours and a daily cap, decided in code.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..calendar.reminders import ReminderLedger, key_for, render
from .cron import validate as validate_cron
from .jobs import Job, JobStore
from .proactive import ProactiveState
from .wakes import Wake, WakeLog

if TYPE_CHECKING:
    from ..runtime import Runtime

WakeHandler = Callable[[str], Awaitable[str]]

#: How far back a tick looks for reminders that have come due. Must exceed the
#: tick period, since a reminder falling between two ticks is delivered by the
#: later one; it is deliberately not longer, so a daemon that was down overnight
#: does not wake the owner with a pile of meetings that have already happened.
REMINDER_LOOKBACK = timedelta(minutes=15)
#: How long a delivered nudge is remembered, so the file cannot grow forever.
REMINDER_MEMORY = timedelta(days=7)


@dataclass(slots=True)
class TickResult:
    job: str
    ran: bool
    ok: bool = True
    detail: str = ""
    output: Any = None


@dataclass(slots=True)
class Scheduler:
    state_dir: Path
    jobs_path: Path = field(default=None)  # type: ignore[assignment]
    store: JobStore = field(init=False)
    log: WakeLog = field(init=False)
    proactive: ProactiveState = field(init=False)
    reminders: ReminderLedger = field(init=False)

    def __post_init__(self) -> None:
        if self.jobs_path is None:
            self.jobs_path = self.state_dir / "jobs.toml"
        self.store = JobStore(self.jobs_path)
        self.log = WakeLog(self.state_dir / "wakes.db")
        self.proactive = ProactiveState(self.state_dir / "proactive.json")
        self.reminders = ReminderLedger(self.state_dir / "reminders.json")

    # -- job management ---------------------------------------------------

    def jobs(self) -> list[Job]:
        return self.store.all()

    def get(self, name: str) -> Job | None:
        return self.store.get(name)

    def add_job(self, *, name: str, cron_expr: str | None = None, cron: str | None = None, kind: str = "capability", target: str = "") -> Job:
        expr = (cron_expr or cron or "").strip()
        validate_cron(expr)  # raises CronError (a ValueError)
        if kind not in ("capability", "wake"):
            raise ValueError(f"unknown job kind {kind!r}")
        return self.store.put(Job(name=name, cron=expr, kind=kind, target=target))

    def remove_job(self, name: str) -> bool:
        return self.store.remove(name)

    # -- the tick ---------------------------------------------------------

    def due(self, when: datetime | None = None) -> list[Job]:
        when = when or datetime.now()
        return [job for job in self.jobs() if job.due(when)]

    async def dispatch_tick(
        self,
        ctx: "Runtime",
        *,
        when: datetime | None = None,
        wake_handler: WakeHandler | None = None,
    ) -> list[TickResult]:
        when = when or datetime.now()
        results: list[TickResult] = []
        for job in self.due(when):
            key = job.idempotency_key(when)
            if self.log.seen(key):
                results.append(TickResult(job=job.name, ran=False, detail="already ran this minute"))
                continue
            result = await self._run_job(job, ctx, wake_handler, when)
            self.log.record(Wake(key=key, job=job.name, ran_at=time.time(), ok=result.ok, detail=result.detail))
            job.last_run = time.time()
            self.store.put(job)
            results.append(result)
        results.extend(await self.fire_reminders(ctx, when))
        return results

    async def fire_reminders(self, ctx: "Runtime", when: datetime | None = None) -> list[TickResult]:
        """Deliver any reminder that has come due since the last tick (SPEC §5.5).

        Deliberately **not** behind the proactive gate: the owner set this alarm
        and named the time, so quiet hours and the daily cap — which bound the
        agent's own choice to speak — have no say here.
        """
        now = when or datetime.now()
        since = now - REMINDER_LOOKBACK
        due = ctx.calendar.due_reminders(since, now)
        results: list[TickResult] = []
        for event, reminder, _fire_at in due:
            key = key_for(event, reminder)
            if self.reminders.fired(key):
                continue
            text = render(event, reminder, now)
            if ctx.surface is not None:
                await ctx.surface.send(text)
            self.reminders.note(key, now)
            results.append(
                TickResult(job=f"reminder:{event.summary}", ran=True, ok=True, detail="reminded", output=text)
            )
        self.reminders.prune(now - REMINDER_MEMORY)
        return results

    async def _run_job(
        self, job: Job, ctx: "Runtime", wake_handler: WakeHandler | None, when: datetime
    ) -> TickResult:
        if job.kind == "capability":
            manifest = ctx.capabilities.get(job.target)
            if manifest is None:
                return TickResult(job=job.name, ran=True, ok=False, detail=f"capability {job.target!r} not found")
            state = ctx.runner.read_state(manifest)
            run = await ctx.runner.run(manifest, context={"trigger": "schedule", "job": job.name}, state=state)
            if run.state_next is not None:
                ctx.runner.write_state(manifest, run.state_next)
            for note in run.notifications:
                if ctx.surface is not None:
                    await ctx.surface.send(_render_notification(job, note))
            return TickResult(
                job=job.name,
                ran=True,
                ok=run.ok,
                detail=run.error or f"exit {run.exit_code}",
                output=run.result,
            )
        if job.kind == "wake":
            if wake_handler is None:
                return TickResult(job=job.name, ran=True, ok=False, detail="no wake handler wired")
            allowed, why_not = self.proactive.may_speak(ctx.settings, when)
            if not allowed:
                # A decision, not a failure: the wake was due and the agent is
                # not permitted to speak. Recorded, so "why was it quiet" has an
                # answer that is not a guess.
                return TickResult(job=job.name, ran=False, ok=True, detail=f"stayed quiet — {why_not}")
            text = await wake_handler(job.target or job.name)
            if not text:
                # The agent looked at the day and found nothing worth the
                # owner's attention. Silence is the outcome, not an error.
                return TickResult(job=job.name, ran=True, ok=True, detail="nothing worth saying")
            if ctx.surface is not None:
                await ctx.surface.send(text)
            self.proactive.note_spoken(when, text)
            return TickResult(job=job.name, ran=True, ok=True, detail="spoke", output=text)
        return TickResult(job=job.name, ran=True, ok=False, detail=f"unknown kind {job.kind!r}")


def _render_notification(job: Job, note: Any) -> str:
    if isinstance(note, str):
        return note
    if isinstance(note, dict):
        # `job.target` is the capability the owner named (e.g. disk_free_check);
        # `job.name` is our internal handle for it (`cap-disk_free_check`), which
        # is noise in a message the owner reads every weekday morning.
        title = note.get("title") or job.target or job.name
        body = note.get("body") or note.get("text") or ""
        return f"**{title}**\n{body}".strip()
    return f"{job.name}: {note}"
