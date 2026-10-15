"""Wake log + idempotency keys (ARCHITECTURE §8.5).

At-least-once tick delivery with dedup: a job that already ran for a given
scheduled minute is skipped, so a sleep/resume or a double tick never produces
duplicate external side effects.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Wake:
    key: str
    job: str
    ran_at: float
    ok: bool
    detail: str = ""


class WakeLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS agent_wakes ("
                "  key TEXT PRIMARY KEY, job TEXT, ran_at REAL, ok INTEGER, detail TEXT)"
            )

    def seen(self, key: str) -> bool:
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT 1 FROM agent_wakes WHERE key = ?", (key,)).fetchone()
        return row is not None

    def record(self, wake: Wake) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute(
                "INSERT OR REPLACE INTO agent_wakes VALUES (?,?,?,?,?)",
                (wake.key, wake.job, wake.ran_at, 1 if wake.ok else 0, wake.detail[:500]),
            )

    def recent(self, limit: int = 20) -> list[Wake]:
        with sqlite3.connect(self.path) as c:
            rows = c.execute(
                "SELECT key, job, ran_at, ok, detail FROM agent_wakes ORDER BY ran_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Wake(key=r[0], job=r[1], ran_at=r[2], ok=bool(r[3]), detail=r[4]) for r in rows]

    def last_run(self, job: str) -> float | None:
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT MAX(ran_at) FROM agent_wakes WHERE job = ?", (job,)).fetchone()
        return row[0] if row and row[0] else None

    def prune(self, older_than_s: float = 90 * 86400) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute("DELETE FROM agent_wakes WHERE ran_at < ?", (time.time() - older_than_s,))
