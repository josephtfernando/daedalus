from __future__ import annotations

from daedalus.runtime import Runtime
from daedalus.security.provenance import Origin


def test_write_then_recall(runtime: Runtime) -> None:
    entry = runtime.memory.write(facet="projects", content="Odysseus research thread started")
    hits = runtime.memory.recall("research", facet="projects")
    assert any(h.id == entry.id for h in hits)


def test_recall_is_scoped_by_facet(runtime: Runtime) -> None:
    runtime.memory.write(facet="body", content="slept badly")
    assert runtime.memory.recall("slept", facet="projects") == []
    assert runtime.memory.recall("slept", facet="body")


def test_markdown_is_human_readable(runtime: Runtime) -> None:
    runtime.memory.write(facet="body", content="ran 5k", origin=Origin.TRUSTED_OWNER)
    text = (runtime.memory.facets_dir / "body.md").read_text()
    assert "ran 5k" in text
    assert "origin=trusted_owner" in text


def test_provenance_survives_the_round_trip(runtime: Runtime) -> None:
    entry = runtime.memory.write(facet="research", content="a scraped claim", origin=Origin.UNTRUSTED_SOURCE)
    hits = runtime.memory.recall("scraped")
    match = next(h for h in hits if h.id == entry.id)
    assert match.origin is Origin.UNTRUSTED_SOURCE
