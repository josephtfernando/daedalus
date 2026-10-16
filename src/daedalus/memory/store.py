"""Memory store — markdown facets on disk, a derived index alongside (SPEC §5).

File-first: the markdown **is** the memory. The SQLite index exists for recall
speed and can be rebuilt from the notes at any time (:meth:`rebuild_index`), so
an owner who edits a facet by hand is not fighting a second, hidden copy of the
truth.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..security.provenance import DataClass, Origin
from . import markdown as md
from .facets import normalize_facet
from .index import MemoryIndex
from .schema import MemoryEntry


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.facets_dir = root / "facets"
        self.facets_dir.mkdir(parents=True, exist_ok=True)
        self.index = MemoryIndex(root / "index.sqlite")
        # An index written before the bitemporal columns existed cannot be
        # altered in place; rebuild it from the notes rather than lose recall.
        if not self.index.has_columns():
            self.rebuild_index()

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
            content=md.normalise_content(content),
            origin=origin,
            data_class=data_class,
            confidence=confidence,
            source_ref=source_ref,
        )
        self.index.add(entry)
        self._append_markdown(entry)
        return entry

    def _facet_path(self, facet: str) -> Path:
        return self.facets_dir / f"{facet}.md"

    def _append_markdown(self, entry: MemoryEntry) -> None:
        path = self._facet_path(entry.facet)
        if not path.exists():
            path.write_text(md.header(entry.facet), encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(md.render_entry(entry))

    def supersede(self, old_id: str, new_entry: MemoryEntry) -> bool:
        """Close the validity of ``old_id`` and point it at ``new_entry``.

        Bitemporal and persisted to both the index and the note, so history is
        auditable and a refresh of the cache cannot resurrect the old fact.
        """
        old = self.get(old_id)
        if old is None:
            return False
        closed_at = time.time()
        if not self.index.update(old_id, valid_to=closed_at, superseded_by=new_entry.id):
            return False
        old.valid_to = closed_at
        old.superseded_by = new_entry.id
        self._rewrite_entry_meta(old)
        return True

    def _rewrite_entry_meta(self, entry: MemoryEntry) -> None:
        """Replace just this entry's metadata comment, leaving the rest intact."""
        path = self._facet_path(entry.facet)
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        marker = f"id={entry.id} "
        for line in text.splitlines(keepends=True):
            if "<!-- daedalus" in line and marker in line:
                old_line = line
                break
        else:
            return
        indent = old_line[: len(old_line) - len(old_line.lstrip())]
        text = text.replace(old_line, f"{indent}{md.meta_comment(entry)}\n", 1)
        path.write_text(text, encoding="utf-8")

    def rebuild_index(self) -> int:
        """Reconstruct the index from the facet notes. Returns entries indexed."""
        self.index.reset()
        total = 0
        for path in sorted(self.facets_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for entry in md.parse_facet(text, fallback_facet=path.stem):
                if entry.content:
                    self.index.add(entry)
                    total += 1
        return total

    # -- read -------------------------------------------------------------

    def recall(
        self,
        query: str,
        *,
        facet: str | None = None,
        limit: int = 10,
        include_invalid: bool = False,
    ) -> list[MemoryEntry]:
        return self.index.search(
            query,
            facet=normalize_facet(facet) if facet else None,
            limit=limit,
            include_invalid=include_invalid,
        )

    def list_facet(
        self, facet: str | None, limit: int = 50, *, include_invalid: bool = False
    ) -> list[MemoryEntry]:
        return self.index.all_for_facet(
            normalize_facet(facet) if facet else None,
            limit=limit,
            include_invalid=include_invalid,
        )

    def get(self, entry_id: str) -> MemoryEntry | None:
        return self.index.get(entry_id)

    def count(self) -> int:
        return self.index.count()

    def inventory(self, limit: int = 8, *, allow_local_only: bool = True) -> str:
        # Over-fetch before filtering: a page of local_only facts must not hide
        # the shareable ones behind them.
        rows = self.index.all_for_facet(None, limit=max(limit * 4, limit))
        if not allow_local_only:
            rows = [r for r in rows if r.data_class is not DataClass.LOCAL_ONLY]
        rows = rows[:limit]
        if not rows:
            return "(no memory yet)"
        return "\n".join(f"- [{r.facet}] {r.content}" for r in rows)
