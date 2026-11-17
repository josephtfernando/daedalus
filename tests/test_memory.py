"""Memory: facet-scoped, provenance-carrying facts over human-readable markdown.

The index is a derived cache. The notes are Obsidian files the owner may edit by
hand, a superseded fact is retired rather than erased, and a recalled fact keeps
the origin it was written with.
"""

from __future__ import annotations

from daedalus.runtime import Runtime
from daedalus.security.provenance import Origin


def test_a_fact_is_recalled_only_inside_its_facet_and_keeps_its_provenance(
    runtime: Runtime,
) -> None:
    # write, recall, and get-by-id: the round trip every stored fact survives
    entry = runtime.memory.write(facet="projects", content="Odysseus research thread started")
    hits = runtime.memory.recall("research", facet="projects")
    assert any(h.id == entry.id for h in hits)
    got = runtime.memory.get(entry.id)
    assert got is not None
    assert got.content == "Odysseus research thread started"

    # a facet is a wall: what body knows does not surface under projects
    runtime.memory.write(facet="body", content="slept badly")
    assert runtime.memory.recall("slept", facet="projects") == []
    assert runtime.memory.recall("slept", facet="body")

    # provenance survives the round trip, so a scraped claim stays untrusted
    claim = runtime.memory.write(
        facet="research", content="a scraped claim", origin=Origin.UNTRUSTED_SOURCE
    )
    match = next(h for h in runtime.memory.recall("scraped") if h.id == claim.id)
    assert match.origin is Origin.UNTRUSTED_SOURCE


def test_superseding_retires_the_old_fact_but_keeps_it_in_history(runtime: Runtime) -> None:
    old = runtime.memory.write(facet="body", content="weighs 80kg")
    new = runtime.memory.write(facet="body", content="weighs 78kg")

    assert runtime.memory.supersede(old.id, new) is True

    # current recall holds only what stands
    assert [h.content for h in runtime.memory.recall("weighs")] == ["weighs 78kg"]

    # history still has the retired fact, with the link and an end date
    history = runtime.memory.recall("weighs", include_invalid=True)
    retired = next(h for h in history if h.id == old.id)
    assert retired.superseded_by == new.id
    assert retired.valid_to is not None

    # ...and both survive a rebuild of the derived index
    runtime.memory.rebuild_index()
    assert [h.content for h in runtime.memory.recall("weighs")] == ["weighs 78kg"]
    reloaded = runtime.memory.get(old.id)
    assert reloaded is not None
    assert reloaded.valid_from <= reloaded.valid_to
    assert reloaded.superseded_by == new.id


def test_the_markdown_is_the_source_of_truth_and_a_person_can_edit_it(
    runtime: Runtime,
) -> None:
    entry = runtime.memory.write(facet="research", content="the cobalt paper")

    # the index is a cache: lose it and recall is empty until a rebuild restores it
    runtime.memory.index.reset()
    assert runtime.memory.recall("cobalt") == []
    runtime.memory.rebuild_index()
    assert [h.id for h in runtime.memory.recall("cobalt")] == [entry.id]

    # what it rebuilds from is an Obsidian note: frontmatter, facet, tag, origin
    runtime.memory.write(facet="body", content="ran 5k", origin=Origin.TRUSTED_OWNER)
    text = (runtime.memory.facets_dir / "body.md").read_text()
    assert text.startswith("---\n"), "a vault note begins with frontmatter"
    assert "facet: body" in text
    assert "daedalus/memory/body" in text
    assert "ran 5k" in text
    assert "origin=trusted_owner" in text

    # a hand-written note is adopted, and rebuilding gives it a stable id
    path = runtime.memory.facets_dir / "projects.md"
    path.write_text("# projects\n\n- a note written by hand\n")
    runtime.memory.rebuild_index()
    hits = runtime.memory.recall("hand")
    assert hits and hits[0].content == "a note written by hand"
    first = hits[0].id
    runtime.memory.rebuild_index()
    assert runtime.memory.recall("hand")[0].id == first


async def test_the_memory_tool_can_supersede(runtime: Runtime) -> None:
    from daedalus.tools.memory import MemoryTool

    first = await MemoryTool().run(
        {"op": "write", "facet": "body", "content": "weighs 80kg"}, runtime
    )
    old_id = first.data["id"]

    result = await MemoryTool().run(
        {"op": "supersede", "id": old_id, "content": "weighs 78kg"}, runtime
    )

    assert result.ok, result.output
    assert [h.content for h in runtime.memory.recall("weighs")] == ["weighs 78kg"]
