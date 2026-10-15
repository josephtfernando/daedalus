"""Cross-process rolling-window rate-limit ledger (ARCHITECTURE §4, §8.5)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class RateLimitLedger:
    """Counts provider calls in a rolling window. Safe across processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS calls ("
                "  provider TEXT NOT NULL,"
                "  ts REAL NOT NULL)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS calls_provider_ts ON calls(provider, ts)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def record(self, provider: str, *, now: float | None = None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO calls(provider, ts) VALUES(?, ?)", (provider, now or time.time()))

    def count(self, provider: str, window_s: float, *, now: float | None = None) -> int:
        now = now or time.time()
        with self._conn() as c:
            (n,) = c.execute(
                "SELECT COUNT(*) FROM calls WHERE provider = ? AND ts > ?",
                (provider, now - window_s),
            ).fetchone()
        return int(n)

    def allow(
        self,
        provider: str,
        window_s: float,
        max_calls: int,
        *,
        now: float | None = None,
    ) -> bool:
        if max_calls <= 0:
            return True
        return self.count(provider, window_s, now=now) < max_calls

    def prune(self, older_than_s: float = 86400, *, now: float | None = None) -> None:
        now = now or time.time()
        with self._conn() as c:
            c.execute("DELETE FROM calls WHERE ts < ?", (now - older_than_s,))
