"""The seat bounds (T24): a recorded decision, enforced in code.

The owner decided 19 Oct, in writing: Heracles' turns run on the local chain — no
remote provider ever — and the charter's ``[bounds]`` table is where the decision
lives, editable by hand. The table parses or refuses loudly; the engine never
lets a bounded seat's turn touch a remote provider; and a bounded seat with
nothing local says so instead of quietly falling back.
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


def _charter(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "bounded.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_charter_bounds_table_parses_or_refuses_loudly(tmp_path: Path) -> None:
    seat = read_charter(
        _charter(
            tmp_path,
            'name = "B"\ncharter = "c"\n\n[bounds]\nremote_models = false\n'
            'read_roots = ["/srv/log"]\n',
        )
    )
    assert seat.bounds == Bounds(remote_models=False, read_roots=("/srv/log",))
    assert seat.local_models_only

    # A seat without bounds is unrestricted.
    seat = read_charter(_charter(tmp_path, 'name = "B"\ncharter = "c"\n'))
    assert seat.bounds is None and not seat.local_models_only

    # A typo that silently fell back to "unrestricted" is the one failure this
    # category cannot afford: unknown keys and wrong types are refused loudly.
    for body, expected in [
        ('name = "B"\ncharter = "c"\n[bounds]\nmaybe = true\n', "unknown bound"),
        ('name = "B"\ncharter = "c"\n[bounds]\nremote_models = "no"\n', "remote_models"),
        ('name = "B"\ncharter = "c"\n[bounds]\nread_roots = "x"\n', "read_roots"),
        ('name = "B"\ncharter = "c"\n[bounds]\nread_roots = [3]\n', "read_roots"),
    ]:
        with pytest.raises(CharterError, match=expected):
            read_charter(_charter(tmp_path, body))

    # The live charter records what the owner actually decided (19 Oct), not a
    # stricter or looser reading someone hoped for.
    live = read_charter(Path.home() / ".daedalus" / "cabinet" / "seats" / "heracles.toml")
    assert live.bounds is not None and not live.bounds.remote_models
    assert live.bounds.read_roots == (), "no files designated yet — the list is the owner's"


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


async def test_a_bounded_seat_keeps_its_turn_on_the_local_chain(runtime: Runtime) -> None:
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

    # An ordinary seat keeps the chain as it was — heavy first, remote answers.
    local = ScriptedProvider([ChatResult(content="local answer")], name="local", is_local=True)
    remote = ScriptedProvider([ChatResult(content="remote answer")], name="remote")
    runtime.model = ModelClient(runtime.settings, providers=[remote, local])

    result = await Engine(runtime).run_turn(
        InboundEvent(text="how did the week go?", user_id="owner", is_dm=True), seat="athena"
    )

    assert result.text == "remote answer" and len(local.calls) == 0, (
        "nothing about the bound touches seats without one"
    )


async def test_a_bounded_seat_with_nothing_local_refuses_in_its_own_name(
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
        "the charter's own clause — he says so instead of answering — is the turn's text"
    )
    assert "Heracles" in result.text, "the refusal is his — it carries his name"
