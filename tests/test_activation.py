"""Activating one seat (SPEC §7, ticket T13).

The `active` flag has lived in the charter files since T11 and been *read by
nobody*: `load_charters` refused two and stood up a default when none claimed the
owner, and then every turn ran as `daedalus` regardless. So the failure this
ticket fixes is a silent one — the owner writes `active = true` on `athena.toml`,
starts talking, and gets Daedalus, with the prompt building and the startup clean.

Three claims carry it:

- **Nothing set leaves the default answering.** The common case must not change.
- **Activating one seat changes the identity a turn runs as** — the charter the
  model sees and the memory namespace it reads, from the same resolution.
- **Exactly one seat is active**, always: switching is one edit, not two, and a
  half-done switch is refused rather than silently resolved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.cabinet.seat import (
    CharterError,
    Seat,
    UnknownSeat,
    active_seat,
    builtin_charters,
    charter_file,
    load_charters,
    pick,
    resolve,
    set_active,
)
from daedalus.config import Settings
from daedalus.loop.context import build_system_prompt
from daedalus.loop.engine import Engine
from daedalus.main import main
from daedalus.model.client import ModelClient
from daedalus.model.providers import ChatResult, ScriptedProvider
from daedalus.runtime import Runtime, build_runtime
from daedalus.surface.base import InboundEvent

ATHENA = "athena"


def _reload(runtime: Runtime) -> Runtime:
    """Re-read the charter files, as a restart would."""
    runtime.seats = load_charters(runtime.settings.home)
    return runtime


def _active_keys(charters) -> list[str]:
    return sorted(key for key, seat in charters.items() if seat.active)


# -- the default, unchanged --------------------------------------------------


def test_a_fresh_home_answers_as_daedalus(tmp_path: Path) -> None:
    charters = load_charters(tmp_path, seed=False)

    assert _active_keys(charters) == ["daedalus"]
    assert active_seat(charters).key == "daedalus"


def test_a_home_where_nobody_claims_the_owner_still_has_an_identity(tmp_path: Path) -> None:
    """A turn always has one; "which seat am I" cannot be left unanswered."""
    for seat in builtin_charters().values():
        (charter_file(tmp_path, seat.key)).parent.mkdir(parents=True, exist_ok=True)
        (charter_file(tmp_path, seat.key)).write_text(
            f'name = "{seat.name}"\ncharter = "No one is active."\n', encoding="utf-8"
        )

    charters = load_charters(tmp_path)

    assert _active_keys(charters) == ["daedalus"]


def test_with_nothing_set_the_prompt_is_daedaluss(runtime: Runtime) -> None:
    prompt = build_system_prompt(runtime, InboundEvent(text="hello", user_id="owner"))

    assert "You are Daedalus" in prompt
    assert runtime.seat.key == "daedalus"


# -- switching ---------------------------------------------------------------


def test_activating_a_seat_switches_the_active_one(tmp_path: Path) -> None:
    set_active(tmp_path, ATHENA)

    charters = load_charters(tmp_path)

    assert _active_keys(charters) == [ATHENA]
    assert active_seat(charters).key == ATHENA


def test_activating_writes_every_file_whose_flag_changed(tmp_path: Path) -> None:
    """One edit, not two: leaving Daedalus active would stop the next startup."""
    set_active(tmp_path, ATHENA)

    athena = charter_file(tmp_path, ATHENA).read_text()
    daedalus = charter_file(tmp_path, "daedalus").read_text()

    assert "active = true" in athena
    assert "active = true" not in daedalus


def test_activating_again_leaves_exactly_one_active(tmp_path: Path) -> None:
    set_active(tmp_path, ATHENA)
    set_active(tmp_path, "odysseus")

    charters = load_charters(tmp_path)

    assert _active_keys(charters) == ["odysseus"]


def test_the_display_name_switches_the_same_seat(tmp_path: Path) -> None:
    """`Melinoë` is how the owner says it; the file is `melinoe.toml`."""
    seat = set_active(tmp_path, "Melinoë")

    assert seat.key == "melinoe"
    assert active_seat(load_charters(tmp_path)).key == "melinoe"


def test_the_choice_survives_a_restart(tmp_path: Path) -> None:
    set_active(tmp_path, ATHENA)

    assert load_charters(tmp_path)[ATHENA].active is True
    assert load_charters(tmp_path)["daedalus"].active is False


def test_an_unknown_seat_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    set_active(tmp_path, ATHENA)
    before = {path.name: path.read_text() for path in sorted((tmp_path / "cabinet/seats").glob("*.toml"))}

    with pytest.raises(UnknownSeat):
        set_active(tmp_path, "socrates")

    after = {path.name: path.read_text() for path in sorted((tmp_path / "cabinet/seats").glob("*.toml"))}
    assert after == before
    assert active_seat(load_charters(tmp_path)).key == ATHENA


def test_a_hand_written_ninth_seat_can_be_activated(tmp_path: Path) -> None:
    """§13 leaves the count open, and an owner's own seat must be reachable."""
    load_charters(tmp_path)
    (tmp_path / "cabinet/seats/hestia.toml").write_text(
        'name = "Hestia"\ncharter = "The house."\n', encoding="utf-8"
    )

    set_active(tmp_path, "hestia")

    assert active_seat(load_charters(tmp_path)).key == "hestia"
    assert load_charters(tmp_path)[ATHENA].active is False


def test_the_files_are_the_mechanism_not_the_command(tmp_path: Path) -> None:
    """Editing the flag by hand is the same act as the CLI, because it is the same flag."""
    set_active(tmp_path, ATHENA)
    daedalus = charter_file(tmp_path, "daedalus")
    daedalus.write_text(
        daedalus.read_text().replace("name =", "active = true\nname =", 1), encoding="utf-8"
    )

    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)

    assert "two seats are active" in str(refused.value)


# -- what a turn actually runs as --------------------------------------------


async def test_a_nameless_turn_runs_as_the_active_seat(runtime: Runtime) -> None:
    set_active(runtime.settings.home, ATHENA)
    _reload(runtime)

    provider = ScriptedProvider([ChatResult(content="Ready.")])
    runtime.model = ModelClient(runtime.settings, providers=[provider])

    await Engine(runtime).run_turn(InboundEvent(text="let's drill", user_id="owner"))

    assert runtime.session.seat == ATHENA
    system = provider.calls[0][0]["content"]
    assert "You are Athena" in system
    assert "You are Daedalus" not in system


async def test_a_nameless_turn_reads_the_active_seats_memory(runtime: Runtime) -> None:
    set_active(runtime.settings.home, ATHENA)
    _reload(runtime)

    provider = ScriptedProvider([ChatResult(content="Ready.")])
    runtime.model = ModelClient(runtime.settings, providers=[provider])

    await Engine(runtime).run_turn(InboundEvent(text="hello", user_id="owner"))

    assert runtime.memory.namespace == ATHENA


def test_naming_a_seat_still_wins_over_the_active_one(runtime: Runtime) -> None:
    set_active(runtime.settings.home, ATHENA)
    _reload(runtime)

    assert runtime.seat.key == ATHENA
    prompt = build_system_prompt(runtime, InboundEvent(text="hi", user_id="owner"), seat="calliope")
    assert "You are Calliope" in prompt


def test_a_name_nobody_answers_to_lands_on_the_active_seat(runtime: Runtime) -> None:
    """A turn is not stopped for a phrase; it lands on whoever is answering."""
    set_active(runtime.settings.home, ATHENA)
    charters = load_charters(runtime.settings.home)

    assert pick(charters, "professor").key == ATHENA
    assert pick(charters, None).key == ATHENA
    with pytest.raises(UnknownSeat):
        resolve(charters, "professor")


def test_the_seat_object_carries_its_own_namespace_when_active(tmp_path: Path) -> None:
    from daedalus.cabinet.namespace import namespace_for

    set_active(tmp_path, ATHENA)

    assert namespace_for(active_seat(load_charters(tmp_path))) == ATHENA


def test_a_seat_with_a_named_namespace_keeps_it_when_it_becomes_active(tmp_path: Path) -> None:
    load_charters(tmp_path)
    charter_file(tmp_path, ATHENA).write_text(
        'name = "Athena"\ncharter = "Drills."\nnamespace = "coding"\n', encoding="utf-8"
    )

    set_active(tmp_path, ATHENA)

    from daedalus.cabinet.namespace import namespace_for

    assert load_charters(tmp_path)[ATHENA].active is True
    assert namespace_for(load_charters(tmp_path)[ATHENA]) == "coding"


# -- the CLI -----------------------------------------------------------------


def test_the_cli_activates_and_says_which_seat(settings: Settings, capsys) -> None:
    code = main(["--home", str(settings.home), "activate", "athena"])

    assert code == 0
    printed = capsys.readouterr().out
    assert "athena" in printed
    assert "active seat now" in printed
    assert active_seat(load_charters(settings.home)).key == ATHENA


def test_the_cli_refuses_an_unknown_seat_without_touching_anything(
    settings: Settings, capsys
) -> None:
    code = main(["--home", str(settings.home), "activate", "socrates"])

    assert code == 1
    assert "no seat called" in capsys.readouterr().out
    assert active_seat(load_charters(settings.home)).key == "daedalus"


def test_the_cli_seats_view_marks_the_active_one(settings: Settings, capsys) -> None:
    main(["--home", str(settings.home), "activate", "athena"])
    capsys.readouterr()

    assert main(["--home", str(settings.home), "seats"]) == 0

    lines = capsys.readouterr().out.splitlines()
    mine = next(line for line in lines if "athena" in line and line.startswith(("*", " ")))
    theirs = next(line for line in lines if line.startswith(("*", " ")) and "daedalus" in line)
    assert mine.startswith("*")
    assert theirs.startswith(" ")


def test_a_seat_is_a_seat_without_a_home_to_activate_in() -> None:
    """`active_seat` works on any mapping, including the built-ins."""
    assert active_seat(builtin_charters()).key == "daedalus"
    assert isinstance(Seat(name="x", charter="y"), Seat)
