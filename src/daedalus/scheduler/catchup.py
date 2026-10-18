"""The catch-up queue: work that fell due while nobody was listening.

A tick is only ever *now*, and ``cron.matches`` asks whether a job's moment is
the present minute. So a job whose moment passed while the daemon was down was
not skipped and not recorded — it was **never asked about**. Nothing in the wake
log said 08:00 had gone missing, because from the scheduler's point of view 08:00
never was. The owner sees one thing: a morning with no briefing, and then two of
them the next time the machine comes up.

This module answers, per job: what should have run and did not, and which of
those is still worth running. That judgement is the whole ticket, and it is the
kind of call that goes wrong when it is implicit, so it is spelled out here:

* **Superseded.** Only the *latest* missed occurrence is a candidate. A job that
  missed 288 ticks over two days has one thing to say, and it has already been
  said by the newest of them — so the older 287 are counted, not queued. Without
  this, an daemon that was off overnight would run a five-minute job 288 times
  on startup, which is the pile the owner asked us to avoid.
* **Stale.** Even the latest one has a moment, and some moments have clearly
  passed. A disk check from six hours ago is not a late report, it is a *false*
  one: it would tell the owner the disk was low this morning. A briefing is
  different — it is about a day that is still today at nine o'clock — so the
  window is per **kind** (``1h`` for a capability, ``12h`` for a wake) and per
  **job** (:attr:`~.jobs.Job.late_within`, which overrides the default, so a job
  that knows its own tolerance can say so).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import cron
from .jobs import Job

#: How late a missed occurrence of each kind is still worth running. See the
#: module docstring: the difference is not severity but *tense*. A capability
#: probes a condition and reports it in the present; a wake talks about a day.
#: A reminder is never *scanned* here (the due pass owns one-shots — `plan`
#: skips them), but `window_for` is the announce-rule it shares: past the
#: window, a late delivery says so, and never silently pretends it was on time.
DEFAULT_LATE_WITHIN = {"capability": "1h", "wake": "12h", "remind": "1h"}

#: A gap smaller than this cannot hide a missed occurrence, and every check costs
#: a backwards cron scan — so it is not worth making. One minute plus a second of
#: slack, since a job's own run moves ``last_run`` to the tick that drove it.
NEGLIGIBLE = timedelta(seconds=61)

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(text: str) -> timedelta | None:
    """``"45m"``, ``"6h"``, ``"2d"`` → a timedelta; ``""`` or junk → ``None``."""
    token = str(text or "").strip().lower()
    if not token:
        return None
    unit = _UNITS.get(token[-1])
    if unit is None:
        return None
    try:
        amount = float(token[:-1])
    except ValueError:
        return None
    if amount < 0:
        return None
    return timedelta(seconds=amount * unit)


def window_for(job: Job) -> timedelta:
    """The job's own tolerance, else its kind's default, else the stricter one."""
    return (
        parse_window(job.late_within)
        or parse_window(DEFAULT_LATE_WITHIN.get(job.kind, ""))
        or timedelta(hours=1)
    )


def human(delta: timedelta) -> str:
    """A duration as the owner would say it: ``2m``, ``6h20m``, ``3d4h``."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, rest = divmod(seconds, 3600)
        return f"{hours}h" + (f"{rest // 60}m" if rest >= 60 else "")
    days, rest = divmod(seconds, 86400)
    return f"{days}d" + (f"{rest // 3600}h" if rest >= 3600 else "")


@dataclass(slots=True)
class Missed:
    """One job's missed moment, and the verdict on it."""

    job: Job
    #: The latest occurrence that fell while nobody was listening.
    when: datetime
    late: timedelta
    #: How many *earlier* occurrences this one stands in for. Never run.
    superseded: int = 0
    #: Why it is not worth running, or ``""`` when it is.
    reason: str = ""

    @property
    def worth_it(self) -> bool:
        return not self.reason


def plan(jobs: list[Job], now: datetime, *, horizon_days: int = 8) -> list[Missed]:
    """What each job missed, newest last, with the ones worth running marked.

    Returns *every* candidate, including the ones it means to skip: a skip is a
    decision the owner is entitled to see, and the caller records it. The caller
    is also expected to move ``last_run`` for a skip, so the decision is taken
    once rather than re-litigated on every tick from here on.
    """
    out: list[Missed] = []
    for job in jobs:
        if not job.enabled:
            continue
        if not job.cron:
            # A one-shot (`run_at`) has no cron to scan backwards with, and its
            # lateness is judged where it fires — the ordinary due pass delivers
            # it whenever it finds it, with the lateness announced, so the
            # catch-up scan has nothing to say about it.
            continue
        floor = job.last_run if job.last_run is not None else job.created_at
        floor_at = datetime.fromtimestamp(floor)
        if now - floor_at < NEGLIGIBLE:
            continue
        when = cron.previous_run(job.cron, now, after=floor_at, horizon_days=horizon_days)
        if when is None:
            continue
        late = now - when
        window = window_for(job)
        earlier_from = max(floor_at, when - timedelta(days=horizon_days))
        superseded = max(0, cron.count_occurrences(job.cron, earlier_from, when) - 1)
        out.append(
            Missed(
                job=job,
                when=when,
                late=late,
                superseded=superseded,
                reason="" if late <= window else f"{human(late)} late, past its {human(window)} window",
            )
        )
    out.sort(key=lambda missed: missed.when)
    return out
