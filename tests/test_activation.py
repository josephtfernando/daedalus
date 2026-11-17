"""Activating one seat (SPEC §7, ticket T13).

The `active` flag lives in the charter files and decides the identity a turn runs
as. Three claims carry it: nothing set leaves the default answering, activating a
seat changes both the charter the model sees and the memory namespace it reads
(from one resolution), and exactly one seat is active — a half-done switch is
refused rather than silently resolved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.cabinet.namespace import namespace_for
from daedalus.cabinet.seat import (
    CharterError,
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
from daedalus.runtime import Runtime
from daedalus.surface.base import InboundEvent

ATHENA = "athena"


def _reload(runtime: Runtime) -> Runtime:
    """Re-read the charter files, as a restart would."""
    runtime.seats = load_charters(runtime.settings.home)
    return runtime


def _active_keys(charters) -> list[str]:
    return sorted(key for key, seat in charters.items() if seat.active)


def test_a_home_with_nothing_set_answers_as_daedalus(tmp_path: Path, runtime: Runtime) -> None:
    # A fresh install: the common case must not change.
    charters = load_charters(tmp_path, seed=False)
    assert _active_keys(charters) == ["daedalus"]
    assert active_seat(charters).key == "daedalus"

    # A home where nobody claims the owner still has an identity.
    for seat in builtin_charters().values():
        path = charter_file(tmp_path, seat.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'name = "{seat.name}"\ncharter = "No one is active."\n', encoding="utf-8"
        )
    assert _active_keys(load_charters(tmp_path)) == ["daedalus"]

    # `active_seat` works on any mapping, including the built-ins.
    assert active_seat(builtin_charters()).key == "daedalus"

    # With nothing set the prompt is Daedalus's.
    prompt = build_system_prompt(runtime, InboundEvent(text="hello", user_id="owner"))
    assert "You are Daedalus" in prompt
    assert runtime.seat.key == "daedalus"


def test_activating_a_seat_makes_it_the_only_active_one(tmp_path: Path) -> None:
    # One edit, not two: writing only the chosen file would leave Daedalus active
    # and stop the next startup.
    set_active(tmp_path, ATHENA)
    charters = load_charters(tmp_path)
    assert _active_keys(charters) == [ATHENA]
    assert active_seat(charters).key == ATHENA
    assert "active = true" in charter_file(tmp_path, ATHENA).read_text()
    assert "active = true" not in charter_file(tmp_path, "daedalus").read_text()

    # Activating again leaves exactly one.
    set_active(tmp_path, "odysseus")
    assert _active_keys(load_charters(tmp_path)) == ["odysseus"]

    # `Melinoë` is how the owner says it; the file is `melinoe.toml`.
    seat = set_active(tmp_path, "Melinoë")
    assert seat.key == "melinoe"
    assert active_seat(load_charters(tmp_path)).key == "melinoe"

    # The choice survives a restart.
    set_active(tmp_path, ATHENA)
    assert load_charters(tmp_path)[ATHENA].active is True
    assert load_charters(tmp_path)["daedalus"].active is False

    # §13 leaves the count open, and an owner's own seat must be reachable.
    (tmp_path / "cabinet/seats/hestia.toml").write_text(
        'name = "Hestia"\ncharter = "The house."\n', encoding="utf-8"
    )
    set_active(tmp_path, "hestia")
    assert active_seat(load_charters(tmp_path)).key == "hestia"
    assert load_charters(tmp_path)[ATHENA].active is False


def test_a_half_done_switch_is_refused_and_an_unknown_seat_changes_nothing(
    tmp_path: Path,
) -> None:
    set_active(tmp_path, ATHENA)
    before = {
        path.name: path.read_text()
        for path in sorted((tmp_path / "cabinet/seats").glob("*.toml"))
    }

    with pytest.raises(UnknownSeat):
        set_active(tmp_path, "socrates")

    after = {
        path.name: path.read_text()
        for path in sorted((tmp_path / "cabinet/seats").glob("*.toml"))
    }
    assert after == before
    assert active_seat(load_charters(tmp_path)).key == ATHENA

    # Editing the flag by hand is the same act; a half-done switch is refused.
    daedalus = charter_file(tmp_path, "daedalus")
    daedalus.write_text(
        daedalus.read_text().replace("name =", "active = true\nname =", 1), encoding="utf-8"
    )
    with pytest.raises(CharterError) as refused:
        load_charters(tmp_path)
    assert "two seats are active" in str(refused.value)


async def test_a_nameless_turn_runs_as_the_active_seat(runtime: Runtime) -> None:
    set_active(runtime.settings.home, ATHENA)
    _reload(runtime)

    provider = ScriptedProvider([ChatResult(content="Ready.")])
    runtime.model = ModelClient(runtime.settings, providers=[provider])

    await Engine(runtime).run_turn(InboundEvent(text="let's drill", user_id="owner"))

    # The identity of the turn and the memory it reads come from one resolution.
    assert runtime.session.seat == ATHENA
    system = provider.calls[0][0]["content"]
    assert "You are Athena" in system
    assert "You are Daedalus" not in system
    assert runtime.memory.namespace == ATHENA

    # Naming a seat still wins over the active one.
    assert runtime.seat.key == ATHENA
    prompt = build_system_prompt(runtime, InboundEvent(text="hi", user_id="owner"), seat="calliope")
    assert "You are Calliope" in prompt

    # A name nobody answers to lands on the active seat; resolving refuses.
    charters = load_charters(runtime.settings.home)
    assert pick(charters, "professor").key == ATHENA
    assert pick(charters, None).key == ATHENA
    with pytest.raises(UnknownSeat):
        resolve(charters, "professor")

    # The seat object carries its own namespace when active.
    assert namespace_for(active_seat(load_charters(runtime.settings.home))) == ATHENA

    # A seat with a named namespace keeps it when it becomes active.
    charter_file(runtime.settings.home, ATHENA).write_text(
        'name = "Athena"\ncharter = "Drills."\nnamespace = "coding"\n', encoding="utf-8"
    )
    set_active(runtime.settings.home, ATHENA)
    assert load_charters(runtime.settings.home)[ATHENA].active is True
    assert namespace_for(load_charters(runtime.settings.home)[ATHENA]) == "coding"


def test_the_cli_activates_reports_and_marks_the_active_seat(settings: Settings, capsys) -> None:
    code = main(["--home", str(settings.home), "activate", "athena"])

    assert code == 0
    printed = capsys.readouterr().out
    assert "athena" in printed
    assert "active seat now" in printed
    assert active_seat(load_charters(settings.home)).key == ATHENA

    # An unknown seat exits 1 without touching anything.
    code = main(["--home", str(settings.home), "activate", "socrates"])
    assert code == 1
    assert "no seat called" in capsys.readouterr().out
    assert active_seat(load_charters(settings.home)).key == ATHENA

    # `seats` marks the active one.
    assert main(["--home", str(settings.home), "seats"]) == 0
    lines = capsys.readouterr().out.splitlines()
    mine = next(line for line in lines if "athena" in line and line.startswith(("*", " ")))
    theirs = next(line for line in lines if line.startswith(("*", " ")) and "daedalus" in line)
    assert mine.startswith("*")
    assert theirs.startswith(" ")
