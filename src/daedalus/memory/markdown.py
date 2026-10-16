"""Facet markdown — the canonical, human-readable, Obsidian-compatible store.

The SQLite index is *derived*; this file is the source of truth. Every entry is
one list item, and its metadata — id, provenance, data class, confidence, the
bitemporal span, supersession — rides in a hidden HTML comment. Obsidian (and
any editor) renders the file as a clean note; :func:`parse_facet` reads the
metadata back out, so the index can be rebuilt from disk at any time.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ..security.provenance import DataClass, Origin
from .schema import MemoryEntry

TAG_ROOT = "daedalus/memory"

_COMMENT = re.compile(r"<!--\s*daedalus\s+(?P<meta>.*?)\s*-->")
_BULLET = "- "


def _frontmatter(facet: str) -> str:
    return (
        "---\n"
        f"facet: {facet}\n"
        "tags:\n"
        f"  - {TAG_ROOT}\n"
        f"  - {TAG_ROOT}/{facet}\n"
        "---\n\n"
    )


def header(facet: str) -> str:
    """The block that opens a facet note (frontmatter + title)."""
    return _frontmatter(facet) + f"# {facet}\n\n"


def meta_comment(entry: MemoryEntry) -> str:
    """The hidden comment carrying an entry's metadata."""
    valid_to = "-" if entry.valid_to is None else f"{entry.valid_to:.3f}"
    return (
        "<!-- daedalus "
        f"id={entry.id} "
        f"origin={entry.origin.value} "
        f"class={entry.data_class.value} "
        f"conf={entry.confidence:.2f} "
        f"valid_from={entry.valid_from:.3f} "
        f"valid_to={valid_to} "
        f"superseded_by={entry.superseded_by or '-'} "
        f"created_at={entry.created_at:.3f}"
        " -->"
    )


def render_entry(entry: MemoryEntry) -> str:
    """One entry as a list item plus its metadata comment."""
    return f"{_BULLET}{entry.content}\n  {meta_comment(entry)}\n"


def stable_id(facet: str, content: str) -> str:
    """A deterministic id for a bullet the owner wrote by hand.

    Stable across rebuilds, so adopting a handwritten note does not mint a new
    identity for it every time the index is reconstructed.
    """
    digest = hashlib.sha1(f"{facet}\x00{content}".encode("utf-8")).hexdigest()
    return "note-" + digest[:20]


def normalise_content(content: str) -> str:
    """Collapse a fact to a single line.

    Facts are one-liners; the markdown format keeps each on its bullet so the
    note stays a clean list and the entry round-trips exactly.
    """
    return " ".join(content.split())


def _parse_meta(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in raw.split():
        key, _, value = token.partition("=")
        if key and value:
            out[key] = value
    return out


def _number(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "", "-") else default
    except ValueError:
        return default


def parse_facet(text: str, *, fallback_facet: str) -> list[MemoryEntry]:
    """Read every entry back out of a facet note.

    Frontmatter supplies the facet when present; otherwise the file's stem does
    (a note the owner wrote by hand). A bullet with no metadata comment is still
    an entry: it becomes a trusted, private note with a stable id.
    """
    facet = fallback_facet
    if text.startswith("---"):
        block = text.split("---", 2)
        if len(block) >= 3:
            for line in block[1].splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "facet" and value.strip():
                    facet = value.strip()

    entries: list[MemoryEntry] = []
    pending: MemoryEntry | None = None
    for line in text.splitlines():
        if line.startswith(_BULLET):
            if pending is not None:
                entries.append(pending)
            content = normalise_content(line[len(_BULLET):])
            pending = MemoryEntry(
                facet=facet,
                content=content,
                id=stable_id(facet, content),
            )
            continue
        if pending is None:
            continue
        found = _COMMENT.search(line)
        if found:
            _apply_meta(pending, _parse_meta(found.group("meta")))
    if pending is not None:
        entries.append(pending)
    return entries


def _apply_meta(entry: MemoryEntry, meta: dict[str, Any]) -> None:
    if meta.get("id"):
        entry.id = str(meta["id"])
    if meta.get("origin"):
        try:
            entry.origin = Origin(meta["origin"])
        except ValueError:
            pass
    if meta.get("class"):
        try:
            entry.data_class = DataClass(meta["class"])
        except ValueError:
            pass
    entry.confidence = _number(meta.get("conf"), entry.confidence)
    entry.valid_from = _number(meta.get("valid_from"), entry.valid_from)
    entry.created_at = _number(meta.get("created_at"), entry.created_at)
    valid_to = meta.get("valid_to")
    entry.valid_to = None if valid_to in (None, "-") else _number(valid_to, 0.0)
    superseded_by = meta.get("superseded_by")
    entry.superseded_by = None if superseded_by in (None, "-") else superseded_by
