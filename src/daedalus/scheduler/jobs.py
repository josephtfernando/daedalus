"""Scheduled jobs — file-first, hand-editable (ARCHITECTURE §8.5)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tomli_w
import tomllib

from . import cron


@dataclass(slots=True)
class Job:
    name: str
    cron: str = ""
    kind: str = "capability"  # capability | wake | remind
    target: str = ""
    enabled: bool = True
    last_run: float | None = None
    created_at: float = field(default_factory=time.time)
    #: How late a missed occurrence is still worth running — ``"45m"``, ``"6h"``, ``"2d"``.
    #: Empty means the default for the kind (see `catchup.py`): a probe reports a
    #: *condition*, and a condition six hours stale is not a late report but a false
    #: one, while a briefing is about a day that is still running. A job that knows
    #: its own tolerance should say so.
    late_within: str = ""
    #: Exactly one of `cron` and `run_at` is set. A ``run_at`` job (``kind="remind"``)
    #: is a *one-shot wake* — a reminder the owner (or the agent) set for one named
    #: moment, which fires once and then retires itself. It never recurs: a cron
    #: expression cannot say "2 hours from now, once", and the recurring shape of an
    #: absolute-date cron ("30 15 18 10 *") is a trap that fires yearly forever.
    #: Set from the P0 days: every wake the engine has had that was not periodic
    #: hit that wall (T42).
    run_at: float | None = None

    def _run_at_dt(self) -> datetime | None:
        return datetime.fromtimestamp(self.run_at) if self.run_at is not None else None

    def next_run(self) -> datetime | None:
        if self.run_at is not None:
            # A one-shot's next run is its named moment — until it has fired,
            # after which it is retired and listed no more.
            return self._run_at_dt() if self.last_run is None else None
        base = datetime.fromtimestamp(self.last_run) if self.last_run else datetime.now()
        return cron.next_run(self.cron, base)

    def next_run_text(self) -> str:
        nxt = self.next_run()
        return nxt.strftime("%Y-%m-%d %H:%M") if nxt else "never"

    def due(self, when: datetime | None = None) -> bool:
        when = when or datetime.now()
        if not self.enabled:
            return False
        if self.run_at is not None:
            moment = self._run_at_dt()
            return self.last_run is None and moment is not None and moment <= when
        if not cron.matches(self.cron, when):
            return False
        if self.last_run is not None:
            return datetime.fromtimestamp(self.last_run) < when.replace(second=0, microsecond=0)
        return True

    def idempotency_key(self, when: datetime | None = None) -> str:
        when = when or datetime.now()
        if self.run_at is not None:
            # Anchored to the *named* moment, not the minute the tick ran: a
            # crash between composing a reminder and retiring it must look like
            # "already ran", not a second delivery a minute later.
            moment = self._run_at_dt()
            if moment is not None:
                return f"{self.name}:{moment.strftime('%Y%m%dT%H%M')}"
        return f"{self.name}:{when.strftime('%Y%m%dT%H%M')}"


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._jobs: dict[str, Job] = {}
        self.load()

    def load(self) -> None:
        self._jobs = {}
        if not self.path.exists():
            return
        data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        for row in data.get("job", []):
            job = Job(
                name=str(row.get("name", "")),
                cron=str(row.get("cron", "")),
                kind=str(row.get("kind", "capability")),
                target=str(row.get("target", "")),
                enabled=bool(row.get("enabled", True)),
                last_run=row.get("last_run"),
                created_at=float(row.get("created_at", time.time())),
                late_within=str(row.get("late_within", "")),
                run_at=row.get("run_at"),
            )
            if job.name:
                self._jobs[job.name] = job

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "job": [
                {
                    "name": j.name,
                    **({"cron": j.cron} if j.cron else {}),
                    **({"run_at": j.run_at} if j.run_at is not None else {}),
                    "kind": j.kind,
                    "target": j.target,
                    "enabled": j.enabled,
                    "created_at": j.created_at,
                    **({"last_run": j.last_run} if j.last_run is not None else {}),
                    **({"late_within": j.late_within} if j.late_within else {}),
                }
                for j in self._jobs.values()
            ]
        }
        self.path.write_text(tomli_w.dumps(payload), encoding="utf-8")

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def get(self, name: str) -> Job | None:
        return self._jobs.get(name)

    def put(self, job: Job) -> Job:
        self._jobs[job.name] = job
        self.save()
        return job

    def remove(self, name: str) -> bool:
        if name in self._jobs:
            del self._jobs[name]
            self.save()
            return True
        return False
