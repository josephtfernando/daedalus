"""Seat charters as files (SPEC §7, ticket T11).

The files are the source: a charter edited on disk is what the seat answers
from, and a charter that is wrong refuses loudly instead of leaving the built-in
silently in place. The built-ins are only the fallback for a fresh install, so
tests that mean "fresh install" load with ``seed=False``.
"""

from __future__ import annotations

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
from daedalus.runtime import build_runtime
from daedalus.surface.base import InboundEvent

#: The eight, by key, exactly as SPEC §7 names them — `dave` lowercase and all.
SPEC_SEATS = ("daedalus", "athena", "odysseus", "calliope", "icarus", "heracles", "dave", "melinoe")


def breathe(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_the_eight_built_ins_are_the_fallback_and_the_first_load_writes_them_out(
    tmp_path: Path,
) -> None:
    assert tuple(BUILTIN) == SPEC_SEATS

    # A fresh install, before any file exists: the built-ins answer.
    fresh = load_charters(tmp_path, seed=False)
    assert tuple(fresh) == SPEC_SEATS
    assert fresh["athena"].charter == BUILTIN["athena"].charter
    assert fresh["athena"].from_file is False, "nothing was read, but a source was claimed"
    assert fresh[DEFAULT_SEAT].active is True
    assert not charter_dir(tmp_path).exists()

    # The fallback is copies, so one runtime cannot edit another's charters.
    poisoned = builtin_charters()
    poisoned["athena"].charter = "changed"
    poisoned["athena"].active = True
    assert builtin_charters()["athena"].charter == BUILTIN["athena"].charter
    assert sum(1 for seat in builtin_charters().values() if seat.active) == 1

    # The first load lays the files down, so there is something to edit.
    charters = load_charters(tmp_path)
    assert len(list(charter_dir(tmp_path).glob("*.toml"))) == len(SPEC_SEATS)
    for key, seat in charters.items():
        assert seat.from_file is True, f"{key} was not written out"
        again = read_charter(charter_file(tmp_path, key))
        assert (again.name, again.charter) == (seat.name, seat.charter)

    # The key is a file name; the name is the owner's.
    assert charters["melinoe"].name == "Melinoë"
    assert charter_file(tmp_path, "melinoe").name == "melinoe.toml"
    assert "Melinoë" in charter_file(tmp_path, "melinoe").read_text(encoding="utf-8")


def test_an_edited_charter_is_what_the_seat_answers_from(tmp_path: Path, settings: Settings) -> None:
    # The edit changes the answer.
    load_charters(tmp_path)
    breathe(
        charter_file(tmp_path, "odysseus"),
        'name = "Odysseus"\ncharter = "The job hunt, and nothing else."\n',
    )
    assert load_charters(tmp_path)["odysseus"].charter == "The job hunt, and nothing else."

    # And the edit reaches the system prompt the model sees.
    runtime = build_runtime(settings)
    breathe(
        charter_file(settings.home, "athena"),
        'name = "Athena"\ncharter = "Recursion drills, every morning, no exceptions."\n',
    )
    runtime.seats = load_charters(settings.home)
    prompt = build_system_prompt(
        runtime, InboundEvent(text="drill me", user_id="owner"), seat="athena"
    )
    assert "Recursion drills, every morning, no exceptions." in prompt
    assert BUILTIN["athena"].charter not in prompt, "the built-in charter is still in the prompt"

    # A seat nobody named carries its own charter, and no other seat's.
    prompt = build_system_prompt(
        runtime, InboundEvent(text="hi", user_id="owner"), seat="heracles"
    )
    assert BUILTIN["heracles"].charter in prompt
    assert BUILTIN["athena"].charter not in prompt

    # The loop builds a runtime from the owner's files, not the built-ins.
    assert tuple(runtime.seats) == SPEC_SEATS
    assert runtime.seats["dave"].from_file is True, "the charters were not laid down"


def test_the_directory_is_the_set_of_charters(tmp_path: Path) -> None:
    load_charters(tmp_path)

    # A seeded file is never overwritten.
    breathe(
        charter_file(tmp_path, "icarus"),
        'name = "Icarus"\ncharter = "Prototypes only, and they must fly."\nactive = false\n',
    )
    assert load_charters(tmp_path)["icarus"].charter == "Prototypes only, and they must fly."

    # Deleting a file means "never mind", not "this seat has no charter".
    charter_file(tmp_path, "heracles").unlink()
    charters = load_charters(tmp_path)
    assert charters["heracles"].charter == BUILTIN["heracles"].charter
    assert charter_file(tmp_path, "heracles").exists(), "the file was not laid down again"

    # §13 leaves the count open: a hand-written ninth joins beside the eight.
    breathe(
        charter_file(tmp_path, "hestia"),
        'name = "Hestia"\ncharter = "The house — plans, lists, the quiet admin."\n',
    )
    charters = load_charters(tmp_path)
    assert charters["hestia"].name == "Hestia"
    assert pick(charters, "hestia").charter.startswith("The house")
    assert pick(charters, "athena").name == "Athena", "the eighth was displaced"


def test_pick_always_answers_with_a_seat(tmp_path: Path) -> None:
    charters = load_charters(tmp_path)

    # No name at all means the one already carrying the conversation.
    assert pick(charters, None).key == DEFAULT_SEAT
    assert pick(charters, "").key == DEFAULT_SEAT

    # An unknown name is not an error worth stopping a turn for.
    assert pick(charters, "hestia").key == DEFAULT_SEAT
    assert pick(charters, "ATHENA").key == "athena", "a seat is not case-sensitive"


def test_a_bad_charter_file_is_refused_naming_the_file(tmp_path: Path) -> None:
    cases = [
        # unreadable TOML
        ("dave", 'name = "dave"\ncharter = "broken\n', ("dave.toml", "charter")),
        # a seat with no charter is a seat with no identity
        ("calliope", 'name = "Calliope"\n', ("calliope.toml", "needs a `name` and a `charter`")),
        # a typo'd field must not leave the built-in silently in place
        ("heracles", 'name = "Heracles"\ncharterr = "Lift heavy things."\n', ("charterr",)),
        # a non-boolean `active`
        (
            "icarus",
            'name = "Icarus"\ncharter = "Build things."\nactive = "sometimes"\n',
            ("`active` is true or false",),
        ),
        # a charter that is not quoted text
        ("dave", 'charter = 11\nname = "dave"\n', ("quoted strings",)),
    ]
    for index, (key, body, expected) in enumerate(cases):
        home = tmp_path / f"case{index}"
        load_charters(home)
        breathe(charter_file(home, key), body)

        with pytest.raises(CharterError) as refused:
            load_charters(home)

        for needle in expected:
            assert needle in str(refused.value), f"{key}: {needle!r} missing"
        if "charterr" in str(refused.value):
            for allowed in sorted(CHART_FIELDS):
                assert allowed in str(refused.value), f"the refusal did not name {allowed}"


def test_exactly_one_seat_is_active(tmp_path: Path) -> None:
    load_charters(tmp_path)

    # Two is refused, naming both: "who is answering?" has one answer.
    breathe(charter_file(tmp_path, "athena"), 'name = "Athena"\ncharter = "Drills."\nactive = true\n')
    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)
    assert "athena" in str(refused.value) and DEFAULT_SEAT in str(refused.value)

    # None has an obvious answer: the default claims the owner.
    breathe(charter_file(tmp_path, "athena"), 'name = "Athena"\ncharter = "Drills."\n')
    breathe(charter_file(tmp_path, DEFAULT_SEAT), 'name = "Daedalus"\ncharter = "Onwards."\n')
    charters = load_charters(tmp_path)
    assert charters[DEFAULT_SEAT].active is True
    assert sum(1 for seat in charters.values() if seat.active) == 1

    # A file may move the flag to another seat.
    breathe(
        charter_file(tmp_path, "melinoe"),
        'name = "Melinoë"\ncharter = "Watching."\nactive = true\n',
    )
    charters = load_charters(tmp_path)
    assert charters["melinoe"].active is True
    assert charters[DEFAULT_SEAT].active is False
    assert sum(1 for seat in charters.values() if seat.active) == 1


def test_a_written_charter_round_trips_every_field_it_carries(tmp_path: Path) -> None:
    # Names like Melinoë and summaries with quotes go through TOML unharmed.
    seat = builtin_charters()["melinoe"]
    seat.charter = 'Watches the "quiet" parts — the ones nobody else reads.'
    write_charter(tmp_path, seat)
    again = read_charter(charter_file(tmp_path, "melinoe"))
    assert again.name == "Melinoë"
    assert again.charter == 'Watches the "quiet" parts — the ones nobody else reads.'

    # A charter without a namespace is named after its key; one that chooses its
    # own keeps it.
    charters = load_charters(tmp_path)
    assert charters["melinoe"].namespace == "melinoe"
    breathe(
        charter_file(tmp_path, "calliope"),
        'name = "Calliope"\ncharter = "Voice."\nnamespace = "calliope-voice"\n',
    )
    assert load_charters(tmp_path)["calliope"].namespace == "calliope-voice"

    # A trait list rides along, and a list of the wrong shape is refused.
    breathe(
        charter_file(tmp_path, "odysseus"),
        'name = "Odysseus"\ncharter = "Research."\ntraits = ["patient", "long horizon"]\n',
    )
    charters = load_charters(tmp_path)
    assert charters["odysseus"].traits == ["patient", "long horizon"]
    assert "patient" in charter_file(tmp_path, "odysseus").read_text(encoding="utf-8")

    breathe(
        charter_file(tmp_path, "odysseus"),
        'name = "Odysseus"\ncharter = "Research."\ntraits = "patient"\n',
    )
    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)
    assert "`traits` is a list of strings" in str(refused.value)
