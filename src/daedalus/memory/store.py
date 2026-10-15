"""Memory store — markdown facets on disk, FTS index alongside (SPEC §5)."""

from __future__ import annotations

import time
from pathlib import Path

from ..security.provenance import DataClass, Origin
from .facets import normalize_facet
from .index import MemoryIndex
from .schema import MemoryEntry


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.facets_dir = root / "facets"
        self.facets_dir.mkdir(parents=True, exist_ok=True)
        self.index = MemoryIndex(root / "index.sqlite")

    # -- write ------------------------------------------------------------

    def write(
        self,
        *,
        facet: str,
        content: str,
        origin: Origin = Origin.TRUSTED_OWNER,
        data_class: DataClass = DataClass.PRIVATE,
        confidence: float = 0.8,
        source_ref: str | None = None,
    ) -> MemoryEntry:
        entry = MemoryEntry(
            facet=normalize_facet(facet),
            content=content.strip(),
            origin=origin,
            data_class=data_class,
            confidence=confidence,
            source_ref=source_ref,
        )
        self.index.add(entry)
        self._append_markdown(entry)
        return entry

    def _append_markdown(self, entry: MemoryEntry) -> None:
        path = self.facets_dir / f"{entry.facet}.md"
        if not path.exists():
            path.write_text(f"# {entry.facet}\n\n", encoding="utf-8")
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.created_at))
        line = (
            f"- {entry.content}\n"
            f"  <!-- id={entry.id} origin={entry.origin.value} class={entry.data_class.value}"
            f" conf={entry.confidence:.2f} at={stamp} -->\n"
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def supersede(self, old_id: str, new_entry: MemoryEntry) -> None:
        """Mark an existing entry as superseded by a newer one."""
        old = self.get(old_id)
        if old is None:
            return
        old.superseded_by = new_entry.id
        old.valid_to = time.time()

    # -- read -------------------------------------------------------------

    def recall(self, query: str, *, facet: str | None = None, limit: int = 10) -> list[MemoryEntry]:
        return self.index.search(query, facet=normalize_facet(facet) if facet else None, limit=limit)

    def list_facet(self, facet: str | None, limit: int = 50) -> list[MemoryEntry]:
        return self.index.all_for_facet(normalize_facet(facet) if facet else None, limit=limit)

    def get(self, entry_id: str) -> MemoryEntry | None:
        hits = self.index.search(entry_id, limit=50)
        for hit in hits:
            if hit.id == entry_id:
                return hit
        return None

    def count(self) -> int:
        return self.index.count()

    def inventory(self, limit: int = 8) -> str:
        rows = self.index.all_for_facet(None, limit=limit)
        if not rows:
            return "(no memory yet)"
        return "\n".join(f"- [{r.facet}] {r.content}" for r in rows)
