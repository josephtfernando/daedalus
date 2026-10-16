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


def test_get_returns_an_entry_by_id(runtime: Runtime) -> None:
    entry = runtime.memory.write(facet="projects", content="an id lookup")
    got = runtime.memory.get(entry.id)
    assert got is not None
    assert got.content == "an id lookup"


def test_supersede_retires_the_old_fact(runtime: Runtime) -> None:
    old = runtime.memory.write(facet="body", content="weighs 80kg")
    new = runtime.memory.write(facet="body", content="weighs 78kg")

    assert runtime.memory.supersede(old.id, new) is True

    current = runtime.memory.recall("weighs")
    assert [h.content for h in current] == ["weighs 78kg"]

    history = runtime.memory.recall("weighs", include_invalid=True)
    retired = next(h for h in history if h.id == old.id)
    assert retired.superseded_by == new.id
    assert retired.valid_to is not None


def test_the_markdown_is_the_source_of_truth(runtime: Runtime) -> None:
    """The index is a derived cache. Lose it and the facts are still there."""
    entry = runtime.memory.write(facet="research", content="the cobalt paper")

    runtime.memory.index.reset()  # lose the cache
    assert runtime.memory.recall("cobalt") == []

    runtime.memory.rebuild_index()
    hits = runtime.memory.recall("cobalt")
    assert [h.id for h in hits] == [entry.id]


def test_supersession_survives_a_rebuild(runtime: Runtime) -> None:
    old = runtime.memory.write(facet="body", content="weighs 80kg")
    new = runtime.memory.write(facet="body", content="weighs 78kg")
    runtime.memory.supersede(old.id, new)

    runtime.memory.rebuild_index()

    assert [h.content for h in runtime.memory.recall("weighs")] == ["weighs 78kg"]


def test_both_timestamps_round_trip(runtime: Runtime) -> None:
    old = runtime.memory.write(facet="body", content="weighs 80kg")
    new = runtime.memory.write(facet="body", content="weighs 78kg")
    runtime.memory.supersede(old.id, new)
    runtime.memory.rebuild_index()

    retired = runtime.memory.get(old.id)
    assert retired is not None
    assert retired.valid_from <= retired.valid_to
    assert retired.superseded_by == new.id


def test_facet_markdown_is_an_obsidian_note(runtime: Runtime) -> None:
    runtime.memory.write(facet="body", content="ran 5k")
    text = (runtime.memory.facets_dir / "body.md").read_text()
    assert text.startswith("---\n"), "a vault note begins with frontmatter"
    assert "facet: body" in text
    assert "daedalus/memory/body" in text


def test_a_hand_written_note_is_adopted(runtime: Runtime) -> None:
    """The owner edits the markdown by hand; the index catches up."""
    path = runtime.memory.facets_dir / "projects.md"
    path.write_text("# projects\n\n- a note written by hand\n")
    runtime.memory.rebuild_index()
    hits = runtime.memory.recall("hand")
    assert hits and hits[0].content == "a note written by hand"


def test_a_hand_written_note_keeps_a_stable_id(runtime: Runtime) -> None:
    path = runtime.memory.facets_dir / "projects.md"
    path.write_text("# projects\n\n- a note written by hand\n")
    runtime.memory.rebuild_index()
    first = runtime.memory.recall("hand")[0].id
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

