"""Seat charters as files (SPEC §7, ticket T11).

The claim under test is that the **files are the source**. Two ways of getting
that wrong are worth a test each: a file that is edited and does not change what
the seat answers from (the charters are really still in the code, and the files
are decoration), and a file that is wrong and does not say so (the built-in stays
in place, the prompt still builds, and the owner reads a seat that is not the one
they wrote).

The built-ins are the fallback, so every test that means "fresh install" loads
with ``seed=False`` — the difference between a *fresh* install and one the tests
just wrote eight files into.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from daedalus.cabinet.seat import (
    BUILTIN,
    CHART_FIELDS,
    DEFAULT_SEAT,
    CharterError,
    builtin_charters,
    charter_dir,
    charter_file,
    load_charters,
    pick,
    read_charter,
    write_charter,
)
from daedalus.config import Settings
from daedalus.loop.context import build_system_prompt
from daedalus.runtime import Runtime, build_runtime
from daedalus.storage import paths
from daedalus.surface.base import InboundEvent

#: The eight, by key, exactly as SPEC §7 names them — `dave` lowercase and all.
SPEC_SEATS = ("daedalus", "athena", "odysseus", "calliope", "icarus", "heracles", "dave", "melinoe")


def breathe(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# -- the fallback, and the files it seeds ------------------------------------


def test_a_fresh_install_falls_back_to_the_built_in_charters(tmp_path: Path) -> None:
    charters = load_charters(tmp_path, seed=False)

    assert tuple(charters) == SPEC_SEATS
    assert charters["athena"].charter == BUILTIN["athena"].charter
    assert charters["athena"].from_file is False, "nothing was read, but a source was claimed"
    assert charters[DEFAULT_SEAT].active is True


def test_loading_writes_the_charters_out_so_there_is_something_to_edit(
    tmp_path: Path,
) -> None:
    """A fresh install has no files; the first load lays them down."""
    assert not charter_dir(tmp_path).exists()

    charters = load_charters(tmp_path)

    assert len(list(charter_dir(tmp_path).glob("*.toml"))) == len(SPEC_SEATS)
    for key, seat in charters.items():
        assert seat.from_file is True, f"{key} was not written out"
        again = read_charter(charter_file(tmp_path, key))
        assert (again.name, again.charter) == (seat.name, seat.charter)


def test_the_names_are_the_owners_names_not_the_keys(tmp_path: Path) -> None:
    """`melinoe.toml` holds Melinoë. The key is a file name; the name is a person."""
    charters = load_charters(tmp_path)

    assert charters["melinoe"].name == "Melinoë"
    assert charter_file(tmp_path, "melinoe").name == "melinoe.toml"
    assert "Melinoë" in charter_file(tmp_path, "melinoe").read_text(encoding="utf-8")


# -- the files win -----------------------------------------------------------


def test_editing_a_charter_changes_what_the_seat_answers_from(tmp_path: Path) -> None:
    load_charters(tmp_path)
    breathe(
        charter_file(tmp_path, "odysseus"),
        'name = "Odysseus"\ncharter = "The job hunt, and nothing else."\n',
    )

    charters = load_charters(tmp_path)

    assert charters["odysseus"].charter == "The job hunt, and nothing else."


def test_an_edited_charter_reaches_the_system_prompt(tmp_path: Path) -> None:
    """The whole ticket in one test: a file, an edit, and a different prompt."""
    home = tmp_path / "home"
    paths.ensure_tree(home)
    settings = Settings(home=home, env={}, heavy=[], cheap=None)
    runtime = build_runtime(settings)
    breathe(
        charter_file(home, "athena"),
        'name = "Athena"\ncharter = "Recursion drills, every morning, no exceptions."\n',
    )
    runtime.seats = load_charters(home)

    prompt = build_system_prompt(runtime, InboundEvent(text="drill me", user_id="owner"), seat="athena")

    assert "Recursion drills, every morning, no exceptions." in prompt
    assert BUILTIN["athena"].charter not in prompt, "the built-in charter is still in the prompt"


def test_the_default_seat_answers_when_no_seat_is_named(tmp_path: Path) -> None:
    charters = load_charters(tmp_path)

    assert pick(charters, None).key == DEFAULT_SEAT
    assert pick(charters, "").key == DEFAULT_SEAT


def test_an_unknown_seat_name_falls_back_to_the_default(tmp_path: Path) -> None:
    """Not an error worth stopping a turn for: the phrase is honest enough."""
    charters = load_charters(tmp_path)

    assert pick(charters, "hestia").key == DEFAULT_SEAT
    assert pick(charters, "ATHENA").key == "athena", "a seat is not case-sensitive"


def test_a_seeded_file_is_never_overwritten(tmp_path: Path) -> None:
    load_charters(tmp_path)
    breathe(
        charter_file(tmp_path, "icarus"),
        'name = "Icarus"\ncharter = "Prototypes only, and they must fly."\nactive = false\n',
    )

    charters = load_charters(tmp_path)

    assert charters["icarus"].charter == "Prototypes only, and they must fly."


def test_a_deleted_charter_falls_back_to_the_built_in(tmp_path: Path) -> None:
    """Deleting a file means "never mind", not "this seat has no charter"."""
    load_charters(tmp_path)
    charter_file(tmp_path, "heracles").unlink()

    charters = load_charters(tmp_path)

    assert charters["heracles"].charter == BUILTIN["heracles"].charter
    assert charter_file(tmp_path, "heracles").exists(), "the file was not laid down again"


def test_a_file_may_add_a_seat_the_code_does_not_know(tmp_path: Path) -> None:
    """§13 leaves the count open; a hand-written ninth has to work."""
    load_charters(tmp_path)
    breathe(
        charter_file(tmp_path, "hestia"),
        'name = "Hestia"\ncharter = "The house — plans, lists, the quiet admin."\n',
    )

    charters = load_charters(tmp_path)

    assert charters["hestia"].name == "Hestia"
    assert pick(charters, "hestia").charter.startswith("The house")
    assert pick(charters, "athena").name == "Athena", "the eighth was displaced"


# -- a bad file is reported, never swallowed ---------------------------------


def test_an_unreadable_charter_is_refused_naming_the_file(tmp_path: Path) -> None:
    load_charters(tmp_path)
    breathe(charter_file(tmp_path, "dave"), 'name = "dave"\ncharter = "broken\n')

    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)

    assert "dave.toml" in str(refused.value)
    assert "charter" in str(refused.value)


def test_a_charter_with_no_charter_is_refused(tmp_path: Path) -> None:
    """A seat with no charter is a seat with no identity, which is no seat."""
    load_charters(tmp_path)
    breathe(charter_file(tmp_path, "calliope"), 'name = "Calliope"\n')

    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)

    assert "calliope.toml" in str(refused.value)
    assert "needs a `name` and a `charter`" in str(refused.value)


def test_a_typo_in_a_field_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    """The failure the owner would never notice: the prompt builds, and is wrong."""
    load_charters(tmp_path)
    breathe(
        charter_file(tmp_path, "heracles"),
        'name = "Heracles"\ncharterr = "Lift heavy things."\n',
    )

    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)

    assert "charterr" in str(refused.value)
    for allowed in sorted(CHART_FIELDS):
        assert allowed in str(refused.value)


def test_a_non_boolean_active_is_refused(tmp_path: Path) -> None:
    load_charters(tmp_path)
    breathe(
        charter_file(tmp_path, "icarus"),
        'name = "Icarus"\ncharter = "Build things."\nactive = "sometimes"\n',
    )

    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)

    assert "`active` is true or false" in str(refused.value)


def test_a_charter_that_is_not_quoted_text_is_refused(tmp_path: Path) -> None:
    """The file says what it means with quotes, or it says nothing at all."""
    load_charters(tmp_path)
    breathe(charter_file(tmp_path, "dave"), 'charter = 11\nname = "dave"\n')

    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)

    assert "quoted strings" in str(refused.value)


# -- one seat at a time ------------------------------------------------------


def test_two_active_seats_are_refused_naming_both(tmp_path: Path) -> None:
    """'Who is answering?' must have one answer (SPEC §7)."""
    load_charters(tmp_path)
    path = charter_file(tmp_path, "athena")
    breathe(path, 'name = "Athena"\ncharter = "Drills."\nactive = true\n')

    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)

    assert "athena" in str(refused.value) and DEFAULT_SEAT in str(refused.value)


def test_no_active_seat_leaves_the_default_active(tmp_path: Path) -> None:
    """The other direction has an obvious answer, so it takes it."""
    load_charters(tmp_path)
    breathe(charter_file(tmp_path, DEFAULT_SEAT), 'name = "Daedalus"\ncharter = "Onwards."\n')

    charters = load_charters(tmp_path)

    assert charters[DEFAULT_SEAT].active is True
    assert sum(1 for seat in charters.values() if seat.active) == 1


def test_a_file_may_move_the_active_flag_to_another_seat(tmp_path: Path) -> None:
    load_charters(tmp_path)
    breathe(
        charter_file(tmp_path, DEFAULT_SEAT), 'name = "Daedalus"\ncharter = "Onwards."\n'
    )
    breathe(
        charter_file(tmp_path, "melinoe"),
        'name = "Melinoë"\ncharter = "Watching."\nactive = true\n',
    )

    charters = load_charters(tmp_path)

    assert charters["melinoe"].active is True
    assert charters[DEFAULT_SEAT].active is False
    assert sum(1 for seat in charters.values() if seat.active) == 1


# -- the written file --------------------------------------------------------


def test_writing_a_charter_keeps_the_quotes_and_em_dashes_intact(tmp_path: Path) -> None:
    """Names like Melinoë and summaries with quotes go through TOML unharmed."""
    seat = builtin_charters()["melinoe"]
    seat.charter = 'Watches the "quiet" parts — the ones nobody else reads.'

    write_charter(tmp_path, seat)
    again = read_charter(charter_file(tmp_path, "melinoe"))

    assert again.name == "Melinoë"
    assert again.charter == 'Watches the "quiet" parts — the ones nobody else reads.'


def test_a_charter_that_chooses_its_own_namespace_keeps_it(tmp_path: Path) -> None:
    """The namespace is T12's, and the file already speaks it."""
    load_charters(tmp_path)
    breathe(
        charter_file(tmp_path, "calliope"),
        'name = "Calliope"\ncharter = "Voice."\nnamespace = "calliope-voice"\n',
    )

    assert load_charters(tmp_path)["calliope"].namespace == "calliope-voice"


def test_a_charter_without_a_namespace_is_named_after_its_key(tmp_path: Path) -> None:
    charters = load_charters(tmp_path)

    assert charters["melinoe"].namespace == "melinoe"


def test_a_trait_list_rides_along(tmp_path: Path) -> None:
    """Unused today, but the file shape is the promise: a seat may carry more
    than one sentence about itself without a schema change."""
    load_charters(tmp_path)
    breathe(
        charter_file(tmp_path, "odysseus"),
        'name = "Odysseus"\ncharter = "Research."\ntraits = ["patient", "long horizon"]\n',
    )

    charters = load_charters(tmp_path)

    assert charters["odysseus"].traits == ["patient", "long horizon"]
    assert "patient" in charter_file(tmp_path, "odysseus").read_text(encoding="utf-8")


def test_a_trait_list_of_the_wrong_shape_is_refused(tmp_path: Path) -> None:
    load_charters(tmp_path)
    breathe(
        charter_file(tmp_path, "odysseus"),
        'name = "Odysseus"\ncharter = "Research."\ntraits = "patient"\n',
    )

    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)

    assert "`traits` is a list of strings" in str(refused.value)


def test_the_built_ins_are_copies_so_one_test_cannot_poison_another() -> None:
    first = builtin_charters()
    first["athena"].charter = "changed"
    first["athena"].active = True

    second = builtin_charters()

    assert second["athena"].charter == BUILTIN["athena"].charter
    assert second[DEFAULT_SEAT].active is True
    assert sum(1 for seat in second.values() if seat.active) == 1


def test_the_seats_are_exactly_the_eight_the_spec_names() -> None:
    assert tuple(BUILTIN) == SPEC_SEATS


# -- and the loop reads them -------------------------------------------------


def test_the_prompt_for_a_seat_carries_that_seats_charter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths.ensure_tree(home)
    runtime = build_runtime(Settings(home=home, env={}, heavy=[], cheap=None))

    prompt = build_system_prompt(runtime, InboundEvent(text="hi", user_id="owner"), seat="heracles")

    assert BUILTIN["heracles"].charter in prompt
    assert BUILTIN["athena"].charter not in prompt


def test_the_runtime_defaults_to_the_built_in_set() -> None:
    """`Runtime` carries the built-ins itself, so no caller has to hand seats in.

    The default is a factory rather than a shared dict: two runtimes must not be
    able to edit each other's charters.
    """
    seats = next(field for field in fields(Runtime) if field.name == "seats")

    assert seats.default_factory is builtin_charters
    assert len(seats.default_factory()) == len(SPEC_SEATS)


def test_the_loop_builds_a_runtime_with_the_owners_charters(settings: Settings) -> None:
    runtime = build_runtime(settings, surface=None)

    assert tuple(runtime.seats) == SPEC_SEATS
    assert runtime.seats["dave"].from_file is True, "the charters were not laid down"
