"""Seat memory namespaces (SPEC §7, ticket T12).

Memory is namespaced: what one identity knows is not what another knows. Two
claims carry the ticket, and each has a way of being quietly false that a test
should be able to catch:

- **A fact written under seat A is not recalled under seat B.** The failure is a
  read that was not narrowed — the store returns everything it has and the seat's
  boundary turns out to be decoration.
- **A fact written before this ticket is still recalled by Daedalus.** The failure
  is the opposite mistake: scoping the new thing by moving the old thing, so the
  first fact the owner loses is the one they told the agent last month.
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
from daedalus.storage import paths
from daedalus.surface.base import InboundEvent
from tests.conftest import tool_call

ATHENA = "athena"


@pytest.fixture
def memory(tmp_path: Path) -> MemoryStore:
    """The root store, as an install that predates seats would have it."""
    return MemoryStore(tmp_path / "memory")


# -- one namespace cannot see another ----------------------------------------


def test_a_fact_written_under_one_seat_is_not_recalled_under_another(
    memory: MemoryStore,
) -> None:
    memory.scoped(ATHENA).write(facet="dsa", content="drill two pointers on sunday")

    assert memory.recall("pointers") == [], "the coordinator saw another seat's fact"
    assert memory.scoped(ATHENA).recall("pointers")


def test_each_seat_keeps_its_own_version_of_the_same_facet(memory: MemoryStore) -> None:
    """The facet names *what a fact is about*; the namespace names whose it is."""
    memoria = memory
    other = memory.scoped(ATHENA)
    memoria.write(facet="projects", content="the release is in November")
    other.write(facet="projects", content="the drills are in November")

    assert [h.content for h in memoria.list_facet("projects")] == ["the release is in November"]
    assert [h.content for h in other.list_facet("projects")] == ["the drills are in November"]


def test_the_default_namespace_is_the_root_that_was_already_there(memory: MemoryStore) -> None:
    """Not a special case in the code — a special case in the *tree*, on purpose."""
    memory.write(facet="body", content="ran 5k")

    assert memory.namespace == DEFAULT_NAMESPACE
    assert (memory.facets_dir / "body.md").exists()
    assert not (memory.root / "namespaces" / DEFAULT_NAMESPACE).exists()


def test_a_seat_writes_into_its_own_directory(memory: MemoryStore) -> None:
    memory.scoped(ATHENA).write(facet="dsa", content="two pointers")

    assert (memory.root / "namespaces" / ATHENA / "facets" / "dsa.md").exists()
    assert not (memory.facets_dir / "dsa.md").exists()


def test_a_handle_shares_the_root_and_the_index(memory: MemoryStore) -> None:
    """One cache over one tree; a handle only decides what a read is narrowed to."""
    entry = memory.scoped(ATHENA).write(facet="dsa", content="two pointers")

    assert memory.get(entry.id) is not None, "an id is unique across the tree"
    assert memory.get(entry.id).namespace == ATHENA
    assert memory.count() == 0 and memory.scoped(ATHENA).count() == 1


def test_asking_twice_returns_the_same_handle(memory: MemoryStore) -> None:
    assert memory.scoped(ATHENA) is memory.scoped(ATHENA)
    assert memory.scoped(DEFAULT_NAMESPACE) is memory


def test_asking_for_a_directory_it_never_uses_does_not_create_a_note(
    memory: MemoryStore,
) -> None:
    memory.scoped(ATHENA).list_facet(None)

    assert (memory.root / "namespaces" / ATHENA / "facets").is_dir()
    assert list((memory.root / "namespaces" / ATHENA / "facets").glob("*.md")) == []


# -- what was there before ---------------------------------------------------


def _note(facet: str, fact: str) -> str:
    from daedalus.memory import markdown as md

    return md.header(facet) + f"- {fact}\n"


def test_a_note_on_disk_is_daedaluss_and_is_recalled(memory: MemoryStore) -> None:
    """The whole upgrade path: files that predate the column, read as the default."""
    (memory.root / "facets").mkdir(parents=True, exist_ok=True)
    (memory.root / "facets" / "projects.md").write_text(
        _note("projects", "the release is in November"), encoding="utf-8"
    )
    memory.rebuild_index()

    hits = memory.recall("release")

    assert [h.content for h in hits] == ["the release is in November"]
    assert hits[0].namespace == DEFAULT_NAMESPACE
    assert memory.scoped(ATHENA).recall("release") == []


def test_an_index_from_before_the_column_is_rebuilt_not_guessed(tmp_path: Path) -> None:
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


def test_a_rebuild_keeps_every_namespace_where_it_was(memory: MemoryStore) -> None:
    """A scoped handle that rebuilt only itself would empty every other seat."""
    memory.write(facet="projects", content="the release is in November")
    memory.scoped(ATHENA).write(facet="dsa", content="drill two pointers")

    total = memory.rebuild_index()

    assert total == 2
    assert [h.content for h in memory.recall("release")] == ["the release is in November"]
    assert [h.content for h in memory.scoped(ATHENA).recall("pointers")] == [
        "drill two pointers"
    ]


def test_a_namespace_directory_the_store_never_wrote_is_picked_up(
    memory: MemoryStore,
) -> None:
    """The filesystem is the list of namespaces, not a roster of known seats."""
    directory = memory.root / "namespaces" / "hestia" / "facets"
    directory.mkdir(parents=True)
    (directory / "home.md").write_text(_note("home", "the boiler is serviced in March"), "utf-8")

    memory.rebuild_index()

    assert [h.content for h in memory.scoped("hestia").recall("boiler")] == [
        "the boiler is serviced in March"
    ]


def test_moving_a_note_moves_the_fact(memory: MemoryStore) -> None:
    """The directory *is* the namespace, so a move is a move — and that is the point."""
    memory.scoped(ATHENA).write(facet="dsa", content="drill two pointers")
    source = memory.root / "namespaces" / ATHENA / "facets" / "dsa.md"
    source.rename(memory.root / "facets" / "dsa.md")

    memory.rebuild_index()

    assert [h.content for h in memory.recall("pointers")] == ["drill two pointers"]
    assert memory.scoped(ATHENA).recall("pointers") == []


# -- names that have to survive becoming directories -------------------------


@pytest.mark.parametrize(
    "name", ["../evil", "athena/../evil", "My Seat", "", " ", ".hidden", "a b", "athena/x"]
)
def test_a_name_that_could_escape_the_tree_is_refused(name: str) -> None:
    with pytest.raises(NamespaceError):
        check(name)


@pytest.mark.parametrize("name", ["athena", "melinoe", "a", "seat-2", "seat_2", "9th"])
def test_a_plain_name_is_accepted(name: str) -> None:
    assert check(name) == name


def test_a_namespace_that_is_not_a_directory_name_never_becomes_a_path(
    tmp_path: Path,
) -> None:
    with pytest.raises(NamespaceError):
        MemoryStore(tmp_path / "memory", "../evil")

    assert not (tmp_path / "evil").exists()


def test_the_refusal_says_where_the_name_came_from() -> None:
    with pytest.raises(NamespaceError) as refused:
        check("My Seat", where="/home/x/cabinet/seats/hestia.toml")

    assert "/home/x/cabinet/seats/hestia.toml" in str(refused.value)


def test_the_layout_is_two_directories_and_nothing_clever(tmp_path: Path) -> None:
    assert facets_dir(tmp_path, DEFAULT_NAMESPACE) == tmp_path / "facets"
    assert facets_dir(tmp_path, "athena") == tmp_path / "namespaces" / "athena" / "facets"
    assert facets_dirs(tmp_path)[0] == (DEFAULT_NAMESPACE, tmp_path / "facets")


# -- the seat decides the namespace ------------------------------------------


def test_the_default_seat_reads_the_root_namespace() -> None:
    assert namespace_for(builtin_charters()["daedalus"]) == DEFAULT_NAMESPACE


def test_another_seat_reads_its_own(tmp_path: Path) -> None:
    charters = load_charters(tmp_path)

    assert namespace_for(charters[ATHENA]) == ATHENA
    assert namespace_for(charters["melinoe"]) == "melinoe"


def test_a_seat_named_by_its_display_name_finds_the_same_namespace(tmp_path: Path) -> None:
    """`Melinoë` is how the owner says it; the key is how the tree spells it."""
    charters = load_charters(tmp_path)
    seat = pick(charters, "Melinoë")

    assert seat.key == "melinoe"
    assert namespace_for(seat) == "melinoe"


def test_a_charter_may_name_its_seats_namespace(tmp_path: Path) -> None:
    load_charters(tmp_path)
    charter_file(tmp_path, ATHENA).write_text(
        'name = "Athena"\ncharter = "Drills."\nnamespace = "coding"\n', encoding="utf-8"
    )

    assert namespace_for(load_charters(tmp_path)[ATHENA]) == "coding"


def test_a_charter_file_with_a_namespace_that_is_not_a_directory_is_refused(
    tmp_path: Path,
) -> None:
    load_charters(tmp_path)
    charter_file(tmp_path, ATHENA).write_text(
        'name = "Athena"\ncharter = "Drills."\nnamespace = "../../private"\n', encoding="utf-8"
    )

    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)

    assert "athena.toml" in str(refused.value)
    assert "not a namespace name" in str(refused.value)


def test_the_coordinator_cannot_be_pointed_at_another_namespace(tmp_path: Path) -> None:
    """Every fact written before seats existed is in this one; moving it orphans them."""
    load_charters(tmp_path)
    charter_file(tmp_path, "daedalus").write_text(
        'name = "Daedalus"\ncharter = "Onwards."\nnamespace = "coordinator"\n', encoding="utf-8"
    )

    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)

    assert "daedalus cannot point its memory elsewhere" in str(refused.value)


def test_a_seat_whose_key_cannot_be_a_directory_is_refused(tmp_path: Path) -> None:
    load_charters(tmp_path)
    (tmp_path / "cabinet" / "seats" / "My Seat.toml").write_text(
        'name = "Mine"\ncharter = "Anything."\n', encoding="utf-8"
    )

    with pytest.raises(CharterError):
        load_charters(tmp_path)


# -- the loop reads through the right namespace ------------------------------


def test_with_no_session_memory_is_the_coordinators(runtime: Runtime) -> None:
    """Startup, the self-test, a CLI probe: no turn, so the default identity."""
    assert runtime.seat.key == "daedalus"
    assert runtime.memory.namespace == DEFAULT_NAMESPACE


def test_a_turn_as_a_seat_reads_and_writes_that_seats_memory(runtime: Runtime) -> None:
    runtime.session = Session(seat=ATHENA)
    runtime.memory.write(facet="dsa", content="drill two pointers on sunday")

    runtime.session = Session(seat="daedalus")

    assert runtime.memory.recall("pointers") == []
    assert (runtime.settings.home / "memory/namespaces/athena/facets/dsa.md").exists()


async def test_a_turn_as_a_seat_remembers_as_that_seat(runtime: Runtime) -> None:
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


def test_the_prompt_carries_only_this_seats_facts(runtime: Runtime) -> None:
    """The failure a namespace exists to prevent: another seat's facts in the brief."""
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


def test_the_seat_and_its_namespace_agree_about_the_name(runtime: Runtime) -> None:
    memory = memory_for(runtime.memory_root, pick(runtime.seats, "Athena"))

    assert memory.namespace == ATHENA


def test_a_fresh_home_still_answers_as_daedalus(settings: Settings) -> None:
    """The path the other tests do not take: build, no session, write, recall."""
    runtime = build_runtime(settings)

    entry = runtime.memory.write(facet="projects", content="selftest probe")

    assert [h.id for h in runtime.memory.recall("probe")] == [entry.id]
    assert (settings.home / "memory" / "facets" / "projects.md").exists()
