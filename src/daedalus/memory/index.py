"""SQLite FTS5 recall index — a derived cache over the facet markdown.

Nothing here is canonical: :mod:`daedalus.memory.markdown` holds the truth and
:meth:`MemoryStore.rebuild_index` can reconstruct this table at any time. Validity
is enforced on read: a superseded or expired entry is not recalled unless the
caller explicitly asks for history.

The table carries the **namespace** an entry belongs to (SPEC §7), so a read
through one identity cannot see another's facts without asking for them. An index
written before that column existed is rebuilt from the notes rather than guessed
at, which is what makes the namespaces of an existing install correct: everything
already on disk is the default one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .namespaces import DEFAULT_NAMESPACE
from .schema import MemoryEntry

# The metadata columns a current index must carry. A table written before these
# existed cannot be migrated in place — FTS5 has no ALTER TABLE — so the store
# rebuilds it from markdown instead of silently losing recall.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "valid_from",
    "valid_to",
    "superseded_by",
    "namespace",
)

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory USING fts5(
    id UNINDEXED,
    namespace UNINDEXED,
    facet,
    content,
    origin UNINDEXED,
    data_class UNINDEXED,
    confidence UNINDEXED,
    source_ref UNINDEXED,
    valid_from UNINDEXED,
    valid_to UNINDEXED,
    superseded_by UNINDEXED,
    created_at UNINDEXED
);
"""

_INSERT = (
    "INSERT INTO memory (id, namespace, facet, content, origin, data_class, confidence,"
    " source_ref, valid_from, valid_to, superseded_by, created_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
)

# An entry is current unless it has been superseded or its validity has closed.
_CURRENT = "(superseded_by IS NULL OR superseded_by = '') AND valid_to IS NULL"


class MemoryIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def reset(self) -> None:
        """Drop and recreate the table (the derived cache, not the truth)."""
        with self._conn() as c:
            c.execute("DROP TABLE IF EXISTS memory")
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # -- schema -----------------------------------------------------------

    def columns(self) -> set[str]:
        with self._conn() as c:
            return {row["name"] for row in c.execute("PRAGMA table_info(memory)")}

    def has_columns(self, names: tuple[str, ...] = REQUIRED_COLUMNS) -> bool:
        return set(names).issubset(self.columns())

    # -- write ------------------------------------------------------------

    def add(self, entry: MemoryEntry) -> None:
        with self._conn() as c:
            c.execute(
                _INSERT,
                (
                    entry.id,
                    entry.namespace,
                    entry.facet,
                    entry.content,
                    entry.origin.value,
                    entry.data_class.value,
                    entry.confidence,
                    entry.source_ref or "",
                    entry.valid_from,
                    entry.valid_to,
                    entry.superseded_by or "",
                    entry.created_at,
                ),
            )

    def update(
        self,
        entry_id: str,
        *,
        valid_to: float | None = None,
        superseded_by: str | None = None,
    ) -> bool:
        """Close an entry's validity. Returns False when no such id exists."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE memory SET valid_to = ?, superseded_by = ? WHERE id = ?",
                (valid_to, superseded_by or "", entry_id),
            )
            return cur.rowcount > 0

    # -- read -------------------------------------------------------------

    def get(self, entry_id: str) -> MemoryEntry | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM memory WHERE id = ?", (entry_id,)).fetchone()
        return _row_to_entry(row) if row else None

    def search(
        self,
        query: str,
        *,
        facet: str | None = None,
        namespace: str | None = None,
        limit: int = 10,
        include_invalid: bool = False,
    ) -> list[MemoryEntry]:
        rows = self._query(
            query, facet=facet, namespace=namespace, limit=limit, include_invalid=include_invalid
        )
        if rows is None:
            rows = self._like(
                query, facet=facet, namespace=namespace, limit=limit,
                include_invalid=include_invalid,
            )
        return [_row_to_entry(r) for r in rows]

    def _query(
        self,
        query: str,
        *,
        facet: str | None,
        namespace: str | None,
        limit: int,
        include_invalid: bool,
    ) -> list[sqlite3.Row] | None:
        sql = "SELECT * FROM memory WHERE memory MATCH ?"
        params: list[object] = [_fts_query(query)]
        if not include_invalid:
            sql += f" AND {_CURRENT}"
        sql, params = _narrow(sql, params, facet=facet, namespace=namespace)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            with self._conn() as c:
                return list(c.execute(sql, params))
        except sqlite3.OperationalError:
            return None

    def _like(
        self,
        query: str,
        *,
        facet: str | None,
        namespace: str | None,
        limit: int,
        include_invalid: bool,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM memory WHERE content LIKE ?"
        params: list[object] = [f"%{query}%"]
        if not include_invalid:
            sql += f" AND {_CURRENT}"
        sql, params = _narrow(sql, params, facet=facet, namespace=namespace)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            return list(c.execute(sql, params))

    def all_for_facet(
        self,
        facet: str | None,
        limit: int = 50,
        *,
        namespace: str | None = None,
        include_invalid: bool = False,
    ) -> list[MemoryEntry]:
        clauses: list[str] = []
        params: list[object] = []
        if not include_invalid:
            clauses.append(_CURRENT)
        if facet:
            clauses.append("facet = ?")
            params.append(facet)
        if namespace is not None:
            clauses.append("namespace = ?")
            params.append(namespace)
        sql = "SELECT * FROM memory"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            return [_row_to_entry(r) for r in c.execute(sql, params)]

    def count(self, *, namespace: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM memory"
        params: list[object] = []
        if namespace is not None:
            sql += " WHERE namespace = ?"
            params.append(namespace)
        with self._conn() as c:
            (n,) = c.execute(sql, params).fetchone()
        return int(n)


def _narrow(
    sql: str,
    params: list[object],
    *,
    facet: str | None,
    namespace: str | None,
) -> tuple[str, list[object]]:
    """Add the facet and namespace filters a caller asked for.

    ``namespace=None`` means *every* namespace, which is the honest reading of a
    query that did not name one; a caller that means the default says so by
    passing it. The store always passes its own, so no read can widen by accident.
    """
    if facet:
        sql += " AND facet = ?"
        params.append(facet)
    if namespace is not None:
        sql += " AND namespace = ?"
        params.append(namespace)
    return sql, params


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 OR-query over the tokens."""
    tokens = [t for t in "".join(ch if ch.isalnum() else " " for ch in query).split() if t]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
    from ..security.provenance import DataClass, Origin

    valid_to = row["valid_to"]
    return MemoryEntry(
        id=row["id"],
        facet=row["facet"],
        content=row["content"],
        origin=Origin(row["origin"]) if row["origin"] else Origin.TRUSTED_OWNER,
        data_class=DataClass(row["data_class"]) if row["data_class"] else DataClass.PRIVATE,
        confidence=float(row["confidence"] or 0.0),
        source_ref=row["source_ref"] or None,
        namespace=row["namespace"] or DEFAULT_NAMESPACE,
        valid_from=float(row["valid_from"] or 0.0),
        valid_to=float(valid_to) if valid_to not in (None, "") else None,
        superseded_by=row["superseded_by"] or None,
        created_at=float(row["created_at"] or 0.0),
    )
