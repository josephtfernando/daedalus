"""Seat memory namespaces (SPEC §7, ticket T12).

Memory is namespaced: whatever one identity knows another cannot recall, and the
boundary is the directory a note lives in. The other half of the claim is the
upgrade path — every fact written before seats existed is still Daedalus's, in
the root namespace, because a new arrangement must not orphan the old memory.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from daedalus.cabinet.namespace import NamespaceError, memory_for, namespace_for
from daedalus.cabinet.seat import CharterError, builtin_charters, charter_file, load_charters, pick
from daedalus.config import Settings
from daedalus.loop.context import build_system_prompt
from daedalus.loop.engine import Engine
from daedalus.memory.namespaces import DEFAULT_NAMESPACE, check, facets_dir, facets_dirs
from daedalus.memory.store import MemoryStore
from daedalus.model.client import ModelClient
from daedalus.model.providers import ChatResult, ScriptedProvider
from daedalus.runtime import Runtime, Session, build_runtime
from daedalus.surface.base import InboundEvent
from tests.conftest import tool_call

ATHENA = "athena"


@pytest.fixture
def memory(tmp_path: Path) -> MemoryStore:
    """The root store, as an install that predates seats would have it."""
    return MemoryStore(tmp_path / "memory")


def _note(facet: str, fact: str) -> str:
    from daedalus.memory import markdown as md

    return md.header(facet) + f"- {fact}\n"


def test_a_fact_written_under_one_seat_is_invisible_to_every_other_reader(
    memory: MemoryStore,
) -> None:
    athena = memory.scoped(ATHENA)
    entry = athena.write(facet="dsa", content="drill two pointers on sunday")

    assert memory.recall("pointers") == [], "the coordinator saw another seat's fact"
    assert athena.recall("pointers")

    # One index, one tree: a handle only narrows what a read may see.
    assert memory.get(entry.id) is not None, "an id is unique across the tree"
    assert memory.get(entry.id).namespace == ATHENA
    assert memory.count() == 0 and athena.count() == 1
    assert memory.scoped(ATHENA) is memory.scoped(ATHENA)
    assert memory.scoped(DEFAULT_NAMESPACE) is memory

    # The facet names *what* a fact is about; the namespace names whose it is.
    memory.write(facet="projects", content="the release is in November")
    athena.write(facet="projects", content="the drills are in November")
    assert [h.content for h in memory.list_facet("projects")] == ["the release is in November"]
    assert [h.content for h in athena.list_facet("projects")] == ["the drills are in November"]

    # The seat's note is in its own directory, not the root's.
    assert (memory.root / "namespaces" / ATHENA / "facets" / "dsa.md").exists()
    assert not (memory.facets_dir / "dsa.md").exists()


def test_the_default_namespace_is_the_root_that_was_already_there(memory: MemoryStore) -> None:
    memory.write(facet="body", content="ran 5k")

    assert memory.namespace == DEFAULT_NAMESPACE
    assert (memory.facets_dir / "body.md").exists()
    assert not (memory.root / "namespaces" / DEFAULT_NAMESPACE).exists()

    # A note on disk from before the column was Daedalus's all along.
    (memory.root / "facets" / "projects.md").write_text(
        _note("projects", "the release is in November"), encoding="utf-8"
    )
    memory.rebuild_index()
    hits = memory.recall("release")
    assert [h.content for h in hits] == ["the release is in November"]
    assert hits[0].namespace == DEFAULT_NAMESPACE
    assert memory.scoped(ATHENA).recall("release") == []

    # Reading a namespace nobody has written to creates no note.
    memory.scoped(ATHENA).list_facet(None)
    assert (memory.root / "namespaces" / ATHENA / "facets").is_dir()
    assert list((memory.root / "namespaces" / ATHENA / "facets").glob("*.md")) == []


def _old_index(root: Path, *, stale_fact: str) -> None:
    """An index exactly as it was before the namespace column existed."""
    conn = sqlite3.connect(root / "index.sqlite")
    conn.executescript(
        """
        CREATE VIRTUAL TABLE memory USING fts5(
            id UNINDEXED, facet, content, origin UNINDEXED, data_class UNINDEXED,
            confidence UNINDEXED, source_ref UNINDEXED, valid_from UNINDEXED,
            valid_to UNINDEXED, superseded_by UNINDEXED, created_at UNINDEXED
        );
        """
    )
    conn.execute(
        "INSERT INTO memory (id, facet, content, origin, data_class, confidence,"
        " source_ref, valid_from, valid_to, superseded_by, created_at)"
        " VALUES ('stale','projects',?,'trusted_owner','private',0.8,'',0,NULL,'',0)",
        (stale_fact,),
    )
    conn.commit()
    conn.close()


def test_an_index_from_before_the_column_is_rebuilt_not_merged(tmp_path: Path) -> None:
    """An old index cannot gain a column, so the notes are read again instead.

    The stale row is the point: a rebuild that merged instead of replacing would
    leave a fact in the cache that is no longer in any file — the memory that
    cannot be corrected.
    """
    root = tmp_path / "memory"
    (root / "facets").mkdir(parents=True)
    (root / "facets" / "projects.md").write_text(
        _note("projects", "the release is in November"), encoding="utf-8"
    )
    _old_index(root, stale_fact="a fact that is no longer anywhere")

    store = MemoryStore(root)

    assert "namespace" in store.index.columns(), "the index was not rebuilt"
    assert [h.content for h in store.recall("release")] == ["the release is in November"]
    assert store.recall("no longer anywhere") == []


def test_a_rebuild_keeps_every_namespace_and_picks_up_one_it_never_wrote(
    memory: MemoryStore,
) -> None:
    memory.write(facet="projects", content="the release is in November")
    memory.scoped(ATHENA).write(facet="dsa", content="drill two pointers")

    total = memory.rebuild_index()

    assert total == 2
    assert [h.content for h in memory.recall("release")] == ["the release is in November"]
    assert [h.content for h in memory.scoped(ATHENA).recall("pointers")] == [
        "drill two pointers"
    ]

    # The filesystem is the list of namespaces, not a roster of known seats.
    directory = memory.root / "namespaces" / "hestia" / "facets"
    directory.mkdir(parents=True)
    (directory / "home.md").write_text(_note("home", "the boiler is serviced in March"), "utf-8")
    memory.rebuild_index()
    assert [h.content for h in memory.scoped("hestia").recall("boiler")] == [
        "the boiler is serviced in March"
    ]

    # The directory *is* the namespace, so moving a note moves the fact.
    source = memory.root / "namespaces" / ATHENA / "facets" / "dsa.md"
    source.rename(memory.root / "facets" / "dsa.md")
    memory.rebuild_index()
    assert [h.content for h in memory.recall("pointers")] == ["drill two pointers"]
    assert memory.scoped(ATHENA).recall("pointers") == []


def test_a_namespace_is_a_plain_directory_name_and_the_layout_is_two_levels(
    tmp_path: Path,
) -> None:
    # A name that could escape the tree.
    for name in ("../evil", "athena/../evil", "My Seat", "", " ", ".hidden", "a b", "athena/x"):
        with pytest.raises(NamespaceError):
            check(name)

    # A plain name.
    for name in ("athena", "melinoe", "a", "seat-2", "seat_2", "9th"):
        assert check(name) == name

    # A refused name never becomes a path, and the refusal says where it came from.
    with pytest.raises(NamespaceError):
        MemoryStore(tmp_path / "memory", "../evil")
    assert not (tmp_path / "evil").exists()

    with pytest.raises(NamespaceError) as refused:
        check("My Seat", where="/home/x/cabinet/seats/hestia.toml")
    assert "/home/x/cabinet/seats/hestia.toml" in str(refused.value)

    # The layout: the root's own facets/, then one directory per namespace.
    assert facets_dir(tmp_path, DEFAULT_NAMESPACE) == tmp_path / "facets"
    assert facets_dir(tmp_path, "athena") == tmp_path / "namespaces" / "athena" / "facets"
    assert facets_dirs(tmp_path)[0] == (DEFAULT_NAMESPACE, tmp_path / "facets")


def test_the_seat_decides_which_namespace_its_turn_reads(tmp_path: Path) -> None:
    assert namespace_for(builtin_charters()["daedalus"]) == DEFAULT_NAMESPACE

    charters = load_charters(tmp_path)
    assert namespace_for(charters[ATHENA]) == ATHENA
    assert namespace_for(charters["melinoe"]) == "melinoe"

    # `Melinoë` is how the owner says it; the key is how the tree spells it.
    seat = pick(charters, "Melinoë")
    assert seat.key == "melinoe"
    assert namespace_for(seat) == "melinoe"

    # A charter may name its seat's namespace.
    charter_file(tmp_path, ATHENA).write_text(
        'name = "Athena"\ncharter = "Drills."\nnamespace = "coding"\n', encoding="utf-8"
    )
    assert namespace_for(load_charters(tmp_path)[ATHENA]) == "coding"


def test_a_namespace_the_tree_cannot_hold_is_refused_at_the_charter(tmp_path: Path) -> None:
    load_charters(tmp_path)

    charter_file(tmp_path, ATHENA).write_text(
        'name = "Athena"\ncharter = "Drills."\nnamespace = "../../private"\n', encoding="utf-8"
    )
    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)
    assert "athena.toml" in str(refused.value)
    assert "not a namespace name" in str(refused.value)
    charter_file(tmp_path, ATHENA).write_text(
        'name = "Athena"\ncharter = "Drills."\n', encoding="utf-8"
    )

    # Every fact written before seats existed lives in this one; moving it orphans them.
    charter_file(tmp_path, "daedalus").write_text(
        'name = "Daedalus"\ncharter = "Onwards."\nnamespace = "coordinator"\n', encoding="utf-8"
    )
    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)
    assert "daedalus cannot point its memory elsewhere" in str(refused.value)
    charter_file(tmp_path, "daedalus").write_text(
        'name = "Daedalus"\ncharter = "Onwards."\n', encoding="utf-8"
    )

    # A key that cannot be a directory is not a seat the store can hold facts for.
    (tmp_path / "cabinet" / "seats" / "My Seat.toml").write_text(
        'name = "Mine"\ncharter = "Anything."\n', encoding="utf-8"
    )
    with pytest.raises(CharterError):
        load_charters(tmp_path)


def test_a_turn_reads_and_writes_through_its_seats_namespace(runtime: Runtime) -> None:
    # Startup, the self-test, a CLI probe: no turn, so the default identity.
    assert runtime.seat.key == "daedalus"
    assert runtime.memory.namespace == DEFAULT_NAMESPACE

    # A turn as a seat writes into that seat's directory, not the coordinator's.
    runtime.session = Session(seat=ATHENA)
    runtime.memory.write(facet="dsa", content="drill two pointers on sunday")
    runtime.session = Session(seat="daedalus")
    assert runtime.memory.recall("pointers") == []
    assert (runtime.settings.home / "memory/namespaces/athena/facets/dsa.md").exists()

    # The seat and its namespace agree about the name.
    assert memory_for(runtime.memory_root, pick(runtime.seats, "Athena")).namespace == ATHENA

    # The prompt carries only this seat's facts.
    runtime.memory.write(facet="projects", content="the release is in November")
    runtime.session = Session(seat=ATHENA)
    runtime.memory.write(facet="dsa", content="drill two pointers on sunday")
    event = InboundEvent(text="what next", user_id="owner")
    mine = build_system_prompt(runtime, event, seat=ATHENA)
    coordinator = build_system_prompt(runtime, event, seat="daedalus")
    assert "drill two pointers on sunday" in mine
    assert "the release is in November" not in mine
    assert "the release is in November" in coordinator
    assert "drill two pointers on sunday" not in coordinator


async def test_a_turn_remembers_as_the_seat_it_ran_as(runtime: Runtime, settings: Settings) -> None:
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("memory", op="write", facet="dsa", content="drill two pointers"),
                    ChatResult(content="Noted."),
                ]
            )
        ],
    )
    await Engine(runtime).run_turn(
        InboundEvent(text="remember the drill", user_id="owner"), seat=ATHENA
    )

    runtime.session = Session(seat="daedalus")
    assert runtime.memory.recall("pointers") == [], "the coordinator saw another seat's fact"
    runtime.session = Session(seat=ATHENA)
    assert [h.content for h in runtime.memory.recall("pointers")] == ["drill two pointers"]

    # The path the other tests do not take: build, no session, write, recall.
    fresh = build_runtime(settings)
    entry = fresh.memory.write(facet="projects", content="selftest probe")
    assert [h.id for h in fresh.memory.recall("probe")] == [entry.id]
    assert (settings.home / "memory" / "facets" / "projects.md").exists()
