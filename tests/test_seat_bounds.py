"""The seat bounds (T24): a recorded decision, enforced in code.

The owner decided 19 Oct, in writing, in conversation: Heracles' turns run on
the local chain — no remote provider ever — and the charter's ``[bounds]``
table is where the decision lives, editable by hand. These tests hold three
truths together: the table parses or refuses loudly; the engine never lets a
bounded seat's turn touch a remote provider; and a bounded seat with nothing
local running says so instead of quietly falling back — the charter's own
"he says so instead of answering", as a code path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.cabinet.seat import Bounds, CharterError, load_charters, read_charter
from daedalus.loop.engine import Engine
from daedalus.model.client import ModelClient
from daedalus.model.providers import ChatResult, ScriptedProvider
from daedalus.runtime import Runtime
from daedalus.surface.base import InboundEvent


# -- the charter table: parse or refuse ---------------------------------------


def _charter(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "bounded.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_bounds_parse_from_the_charter(tmp_path: Path) -> None:
    seat = read_charter(
        _charter(
            tmp_path,
            'name = "B"\ncharter = "c"\n\n[bounds]\nremote_models = false\n'
            'read_roots = ["/srv/log"]\n',
        )
    )
    assert seat.bounds == Bounds(remote_models=False, read_roots=("/srv/log",))
    assert seat.local_models_only


def test_a_seat_without_bounds_is_unrestricted(tmp_path: Path) -> None:
    seat = read_charter(_charter(tmp_path, 'name = "B"\ncharter = "c"\n'))
    assert seat.bounds is None and not seat.local_models_only


def test_a_malformed_bound_is_refused_not_swallowed(tmp_path: Path) -> None:
    """A typo that silently fell back to 'unrestricted' is the one failure this
    category cannot afford — so unknown keys and wrong types are refused loudly."""
    with pytest.raises(CharterError, match="unknown bound"):
        read_charter(_charter(tmp_path, 'name = "B"\ncharter = "c"\n[bounds]\nmaybe = true\n'))
    with pytest.raises(CharterError, match="remote_models"):
        read_charter(_charter(tmp_path, 'name = "B"\ncharter = "c"\n[bounds]\nremote_models = "no"\n'))
    with pytest.raises(CharterError, match="read_roots"):
        read_charter(_charter(tmp_path, 'name = "B"\ncharter = "c"\n[bounds]\nread_roots = "x"\n'))


def test_heracles_real_charer_carries_the_recorded_decision() -> None:
    """The live charter records what the owner actually decided (19 Oct), not a
    stricter or looser reading someone hoped for."""
    seat = read_charter(Path.home() / ".daedalus" / "cabinet" / "seats" / "heracles.toml")
    assert seat.bounds is not None and not seat.bounds.remote_models
    assert seat.bounds.read_roots == (), "no files designated yet — the list is the owner's to fill"


def test_a_broken_still_breaks_startup(tmp_path: Path) -> None:
    with pytest.raises(CharterError):
        read_charter(_charter(tmp_path, 'name = "B"\ncharter = "c"\n[bounds]\nread_roots = [3]\n'))


# -- the engine: the bound is a property of the call -------------------------


def _bounded_heracles(runtime: Runtime) -> None:
    """Give the runtime a Heracles charter bound as the owner decided it."""
    home = runtime.settings.home
    seat_dir = home / "cabinet" / "seats"
    seat_dir.mkdir(parents=True, exist_ok=True)
    (seat_dir / "heracles.toml").write_text(
        'name = "Heracles"\ncharter = "the body"\n\n[bounds]\nremote_models = false\n',
        encoding="utf-8",
    )
    runtime.seats = load_charters(home)


def load_charters(home: Path):
    from daedalus.cabinet.seat import load_charters

    return load_charters(home, seed=True)


async def test_a_bounded_seat_never_touches_a_remote_provider(runtime: Runtime) -> None:
    _bounded_heracles(runtime)
    local = ScriptedProvider([ChatResult(content="from the local chain")], name="local", is_local=True)
    remote = ScriptedProvider([ChatResult(content="should never be reached")], name="remote")
    runtime.model = ModelClient(runtime.settings, providers=[remote], cheap=local)

    result = await Engine(runtime).run_turn(
        InboundEvent(text="how did the week go?", user_id="owner", is_dm=True), seat="heracles"
    )

    assert result.text == "from the local chain" and not result.error
    assert len(local.calls) >= 1 and len(remote.calls) == 0, (
        "the bound is a property of the call: the remote provider was in the chain, "
        "available, and never reached"
    )


async def test_a_bounded_seat_with_nothing_local_refuses_instead_of_falling_back(
    runtime: Runtime,
) -> None:
    _bounded_heracles(runtime)
    remote = ScriptedProvider([ChatResult(content="should never be reached")], name="remote")
    runtime.model = ModelClient(runtime.settings, providers=[remote])

    result = await Engine(runtime).run_turn(
        InboundEvent(text="how did the week go?", user_id="owner", is_dm=True), seat="heracles"
    )

    assert len(remote.calls) == 0, "falling back to the remote provider is the one act the bound forbids"
    assert not result.error or "local" in result.error.lower()
    assert "local" in result.text.lower() and "refus" in result.text.lower(), (
        "the charter's own clause — he says so instead of answering — is the turn's text, "
        "deterministically, not a stack trace"
    )


async def test_an_unbounded_seat_is_unchanged(runtime: Runtime) -> None:
    local = ScriptedProvider([ChatResult(content="local answer")], name="local", is_local=True)
    remote = ScriptedProvider([ChatResult(content="remote answer")], name="remote")
    runtime.model = ModelClient(runtime.settings, providers=[remote, local])

    result = await Engine(runtime).run_turn(
        InboundEvent(text="how did the week go?", user_id="owner", is_dm=True), seat="athena"
    )

    assert result.text == "remote answer" and len(local.calls) == 0, (
        "an ordinary seat keeps the chain as it was — heavy first, and the remote "
        "provider answers; nothing about the bound touches seats without one"
    )


async def test_the_engine_names_the_seat_that_refused(runtime: Runtime) -> None:
    _bounded_heracles(runtime)
    runtime.model = ModelClient(
        runtime.settings, providers=[ScriptedProvider([ChatResult(content="x")], name="remote")]
    )

    result = await Engine(runtime).run_turn(
        InboundEvent(text="anything", user_id="owner", is_dm=True), seat="heracles"
    )
    assert "Heracles" in result.text, "the refusal is his — it carries his name"
