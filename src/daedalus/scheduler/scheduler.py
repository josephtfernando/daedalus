"""Scheduler — cron-pumped wake (SPEC §3.5, §9, ARCHITECTURE §7.3).

A scheduled capability runs its artifact directly with **no model in the loop**.
A ``wake`` job, by contrast, is the agent *choosing to speak*: the model
composes the message fresh (SPEC §9), and may choose not to.

The gate on *whether* a wake may speak lives in :mod:`.proactive` and is
deterministic — quiet hours and a daily cap, decided in code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..calendar.reminders import ReminderLedger, key_for, render
from ..cabinet.seat import UnknownSeat, resolve
from . import catchup
from .cron import validate as validate_cron
from .jobs import Job, JobStore
from .proactive import ProactiveState
from .wakes import Wake, WakeLog

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("daedalus.scheduler")

#: Composes a proactive message. The second argument is the seat the message
#: belongs to (a charter key) or ``None`` for the active seat; see `rt.wake`.
#: Broadly typed because a fake in a test is a normal citizen here.
WakeHandler = Callable[..., Awaitable[str]]

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
    #: Seconds between catch-up runs, so a start-up after an outage does not
    #: deliver the whole queue as one burst. The runs are already sequential; the
    #: pause is what keeps three notifications out of the same second.
    stagger_s: float = 2.0
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
        # Reload before every read: jobs are file-first, and the next tick is
        # where a hand edit becomes real (JobStore.reload's docstring).
        self.store.reload()
        return self.store.all()

    def get(self, name: str) -> Job | None:
        return self.store.get(name)

    def add_job(
        self,
        *,
        name: str,
        cron_expr: str | None = None,
        cron: str | None = None,
        kind: str = "capability",
        target: str = "",
        late_within: str = "",
        seat: str = "",
    ) -> Job:
        expr = (cron_expr or cron or "").strip()
        validate_cron(expr)  # raises CronError (a ValueError)
        if kind not in ("capability", "wake"):
            raise ValueError(f"unknown job kind {kind!r}")
        if seat and kind != "wake":
            raise ValueError(
                f"a {kind} job cannot belong to a seat — a capability runs headless and "
                "speaks through its own manifest. Only a wake (the agent composing a "
                "message) has an identity to carry."
            )
        window = late_within.strip()
        if window and catchup.parse_window(window) is None:
            raise ValueError(f"late_within must read like 45m, 6h or 2d — got {window!r}")
        return self.store.put(
            Job(name=name, cron=expr, kind=kind, target=target, late_within=window, seat=seat.strip())
        )

    def remove_job(self, name: str) -> bool:
        return self.store.remove(name)

    def add_reminder(
        self,
        *,
        name: str,
        when: datetime,
        note: str = "",
        late_within: str = "",
    ) -> Job:
        """One one-shot wake, for one named moment (T42).

        `cron` could never say this: no expression means "2 hours from now,
        once", and the recurring shape of an absolute-date one would fire
        yearly forever. A reminder has its own kind — it composes through the
        wake handler like a wake (fresh, as the active seat), but the
        proactive gate has no say, because **the owner named the time**: the
        gate bounds the agent's choice to speak, not the owner's (the same
        rule `fire_reminders` states for calendar alarms). It fires once and
        then retires.
        """
        if not name or not name.strip():
            raise ValueError("a reminder needs a name — its handle in the list")
        if not note or not note.strip():
            raise ValueError("a reminder needs a note — what it wakes the agent to say")
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        if when <= datetime.now():
            raise ValueError(
                f"the reminder's moment is already past — it resolved to {when:%Y-%m-%d %H:%M}, "
                "and a reminder that cannot be kept is refused, not quietly fired late"
            )
        window = late_within.strip()
        if window and catchup.parse_window(window) is None:
            raise ValueError(f"late_within must read like 45m, 6h or 2d — got {window!r}")
        return self.store.put(
            Job(name=name, kind="remind", target=note, late_within=window, run_at=when.timestamp())
        )

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
                if job.kind == "remind" and job.run_at is not None:
                    # A one-shot recorded by the log and still in the store is
                    # the crash-between-fire-and-retire case: it already ran,
                    # so it retires here — that is what "fires once" means.
                    self.remove_job(job.name)
                    results[-1].detail = "already fired (recovered after a crash); retired"
                continue
            result = await self._run_job(job, ctx, wake_handler, when)
            self.log.record(Wake(key=key, job=job.name, ran_at=time.time(), ok=result.ok, detail=result.detail))
            # Record when the tick was *driven*, not when the wall clock happened
            # to be. `due()` compares `when` against this, so writing real time
            # here makes a pinned sequence self-contradictory: the selftest probe
            # ticked a silent 03:00 and then a noon that had to speak, passed all
            # morning, and went red at 12:01 because noon was suddenly "already
            # run". A tick given a time must be governed by that time.
            if job.kind == "remind":
                if result.ok:
                    # Fires once. `last_run` is never moved past the moment,
                    # because `due()` for a one-shot reads `last_run is None`,
                    # and a reminder that has fired is gone, not dormant. A
                    # *failed* compose keeps the job: an owner-named reminder
                    # lost to a transient model outage is silent loss — the
                    # one failure this store must not pretend to succeed at
                    # (the lesson of the executable bit, T41).
                    self.remove_job(job.name)
                results.append(result)
                continue
            job.last_run = when.timestamp()
            self.store.put(job)
            results.append(result)
        results.extend(await self.fire_reminders(ctx, when))
        # Last, so that a slow drain can never delay a reminder the owner set
        # themselves. On a normal tick there is nothing here to do — a job whose
        # moment is *now* was handled above and has already moved `last_run` out
        # of this scan's range — which is what makes running it every tick
        # affordable, and what makes a tick that merely *overran* recover too.
        results.extend(await self.catch_up(ctx, wake_handler, when))
        return results

    async def catch_up(
        self,
        ctx: "Runtime",
        wake_handler: WakeHandler | None,
        when: datetime,
    ) -> list[TickResult]:
        """Run what fell due while the loop was down — one at a time, with a note.

        Called *after* the ordinary due pass, and looking strictly before the
        present minute, so the two passes cannot double up: a job whose moment is
        now is handled there, and moves ``last_run`` out of this scan's range.

        The missed moment decides *what* ran; the present moment decides *whether
        it may speak*. A briefing caught up at 03:00 is still a briefing, and
        quiet hours have not stopped applying just because the daemon was down.
        """
        results: list[TickResult] = []
        ran = 0
        for missed in catchup.plan(self.jobs(), when):
            job = missed.job
            key = job.idempotency_key(missed.when)
            if self.log.seen(key):
                # The wake log is the memory: a tick given this minute already ran
                # it, or an earlier start-up did. A restart loop cannot replay the
                # same missed morning.
                continue
            if not missed.worth_it:
                detail = f"missed {missed.when:%a %d %b %H:%M} — not caught up: {missed.reason}"
                log.info("job %s %s", job.name, detail)
                self.log.record(Wake(key=key, job=job.name, ran_at=time.time(), ok=True, detail=detail))
                # The decision is recorded *and* acted on, so it is taken once:
                # `last_run` now, and the next tick has nothing to re-litigate.
                job.last_run = when.timestamp()
                self.store.put(job)
                results.append(TickResult(job=job.name, ran=False, ok=True, detail=detail))
                continue
            if ran:
                await asyncio.sleep(self.stagger_s)
            ran += 1
            log.info(
                "catching up job %s: the %s run was missed (%s late, %d earlier folded in)",
                job.name,
                f"{missed.when:%H:%M}",
                catchup.human(missed.late),
                missed.superseded,
            )
            result = await self._run_job(job, ctx, wake_handler, when)
            note = f"caught up the missed {missed.when:%H:%M} run ({catchup.human(missed.late)} late)"
            if missed.superseded:
                note += f", {missed.superseded} earlier run(s) folded in"
            result.detail = f"{note}: {result.detail}" if result.detail else note
            self.log.record(Wake(key=key, job=job.name, ran_at=time.time(), ok=result.ok, detail=result.detail))
            job.last_run = when.timestamp()
            self.store.put(job)
            results.append(result)
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
                    # The owner's only other evidence that a scheduled capability
                    # spoke is the message itself, which nobody but the owner can
                    # see. "Did it deliver?" should be answerable from the journal.
                    log.info("capability %s delivered a notification", job.target)
            return TickResult(
                job=job.name,
                ran=True,
                ok=run.ok,
                detail=run.error or f"exit {run.exit_code}",
                output=run.result,
            )
        if job.kind == "remind":
            if wake_handler is None:
                return TickResult(job=job.name, ran=True, ok=False, detail="no wake handler wired")
            # **No proactive gate.** The owner named this moment; the gate
            # bounds the agent's own choice to speak, not the owner's — the
            # same rule `fire_reminders` states for calendar alarms.
            text = await wake_handler(job.target or job.name, seat=None)
            if not text:
                # The model looked at the named moment and said nothing — the
                # same judgment a wake exercises. It costs the reminder its one
                # firing, and that is right: retrying an owner alarm forever is
                # worse than one quiet miss, and silence is the outcome here as
                # it is for a wake.
                return TickResult(job=job.name, ran=True, ok=True, detail="nothing worth saying")
            if job.run_at is not None:
                moment = datetime.fromtimestamp(job.run_at)
                late: timedelta = when - moment
                if late > catchup.window_for(job):
                    text = (
                        f"(this reminder was set for {moment:%a %d %b %H:%M} — delivered "
                        f"{catchup.human(late)} late) {text}"
                    )
                    detail = (
                        f"reminded {catchup.human(late)} late (set for {moment:%a %d %b %H:%M})"
                    )
                else:
                    detail = "reminded"
            else:  # pragma: no cover — a remind job always carries a run_at
                detail = "reminded"
            if ctx.surface is not None:
                await ctx.surface.send(text)
            self.proactive.note_spoken(when, text)
            return TickResult(job=job.name, ran=True, ok=True, detail=detail, output=text)
        if job.kind == "wake":
            if wake_handler is None:
                return TickResult(job=job.name, ran=True, ok=False, detail="no wake handler wired")
            # A job may belong to a seat (T21). The wake then composes as that
            # seat — its charter, its namespace — rather than as whoever is
            # active; that is what "one thing the seat owns that runs" runs on.
            # An unknown seat is a broken job: recorded, not silently delivered
            # as some other identity.
            seat_key: str | None = None
            seat_name = ""
            if job.seat:
                try:
                    chosen = resolve(ctx.seats, job.seat)
                except UnknownSeat:
                    return TickResult(
                        job=job.name,
                        ran=True,
                        ok=False,
                        detail=f"seat {job.seat!r} names nobody in the cabinet — fix the job's `seat`",
                    )
                seat_key, seat_name = chosen.key, chosen.name
            allowed, why_not = self.proactive.may_speak(ctx.settings, when)
            if not allowed:
                # A decision, not a failure: the wake was due and the agent is
                # not permitted to speak. Recorded, so "why was it quiet" has an
                # answer that is not a guess.
                return TickResult(job=job.name, ran=False, ok=True, detail=f"stayed quiet — {why_not}")
            text = await wake_handler(job.target or job.name, seat=seat_key)
            if not text:
                # The agent looked at the day and found nothing worth the
                # owner's attention. Silence is the outcome, not an error.
                return TickResult(job=job.name, ran=True, ok=True, detail="nothing worth saying")
            if seat_name and not text.lstrip().lower().startswith(seat_name.lower()):
                # The reply names whoever took it (SPEC §7): a proactive
                # message from a seat the owner is not currently talking to,
                # unsigned, would read as the coordinator's.
                text = f"{seat_name}: {text}"
            if ctx.surface is not None:
                await ctx.surface.send(text)
            self.proactive.note_spoken(when, text)
            detail = "spoke" if not seat_name else f"spoke as {seat_name}"
            return TickResult(job=job.name, ran=True, ok=True, detail=detail, output=text)
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
