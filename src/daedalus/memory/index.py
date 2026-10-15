"""SQLite FTS5 recall index."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import MemoryEntry

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory USING fts5(
    id UNINDEXED,
    facet,
    content,
    origin UNINDEXED,
    data_class UNINDEXED,
    confidence UNINDEXED,
    source_ref UNINDEXED,
    created_at UNINDEXED
);
"""


class MemoryIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, entry: MemoryEntry) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO memory (id, facet, content, origin, data_class, confidence, source_ref, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    entry.id,
                    entry.facet,
                    entry.content,
                    entry.origin.value,
                    entry.data_class.value,
                    entry.confidence,
                    entry.source_ref or "",
                    entry.created_at,
                ),
            )

    def search(self, query: str, *, facet: str | None = None, limit: int = 10) -> list[MemoryEntry]:
        rows = self._query(query, facet=facet, limit=limit)
        if rows is None:
            rows = self._like(query, facet=facet, limit=limit)
        return [_row_to_entry(r) for r in rows]

    def _query(self, query: str, *, facet: str | None, limit: int) -> list[sqlite3.Row] | None:
        sql = "SELECT * FROM memory WHERE memory MATCH ?"
        params: list[object] = [_fts_query(query)]
        if facet:
            sql += " AND facet = ?"
            params.append(facet)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            with self._conn() as c:
                return list(c.execute(sql, params))
        except sqlite3.OperationalError:
            return None

    def _like(self, query: str, *, facet: str | None, limit: int) -> list[sqlite3.Row]:
        sql = "SELECT * FROM memory WHERE content LIKE ?"
        params: list[object] = [f"%{query}%"]
        if facet:
            sql += " AND facet = ?"
            params.append(facet)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            return list(c.execute(sql, params))

    def all_for_facet(self, facet: str | None, limit: int = 50) -> list[MemoryEntry]:
        sql = "SELECT * FROM memory"
        params: list[object] = []
        if facet:
            sql += " WHERE facet = ?"
            params.append(facet)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            return [_row_to_entry(r) for r in c.execute(sql, params)]

    def count(self) -> int:
        with self._conn() as c:
            (n,) = c.execute("SELECT COUNT(*) FROM memory").fetchone()
        return int(n)


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 OR-query over the tokens."""
    tokens = [t for t in "".join(ch if ch.isalnum() else " " for ch in query).split() if t]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
    from ..security.provenance import DataClass, Origin

    return MemoryEntry(
        id=row["id"],
        facet=row["facet"],
        content=row["content"],
        origin=Origin(row["origin"]) if row["origin"] else Origin.TRUSTED_OWNER,
        data_class=DataClass(row["data_class"]) if row["data_class"] else DataClass.PRIVATE,
        confidence=float(row["confidence"] or 0.0),
        source_ref=row["source_ref"] or None,
        created_at=float(row["created_at"] or 0.0),
    )
