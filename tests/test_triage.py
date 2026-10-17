"""Who takes this message (SPEC §7).

Routing is a *preference*, and these tests are mostly about the ways it refuses
to be one: an answer that is not a seat, prose that merely mentions a seat, a
provider that is down, a message too short to be about anything. Every one of
those has to end with the owner's request still intact — on the seat that was
already handling the conversation, or on the coordinator.
"""

from __future__ import annotations

import asyncio

import pytest

from daedalus.cabinet.seat import builtin_charters
from daedalus.cabinet.triage import MAX_REASON, addressed, menu, read, triage
from daedalus.config import Settings
from daedalus.loop.engine import Engine
from daedalus.model.client import ModelClient
from daedalus.model.providers import ChatResult, ScriptedProvider
from daedalus.runtime import Runtime, build_runtime
from daedalus.surface.base import InboundEvent
from daedalus.surface.cli import ScriptedSurface


def run(coro):
    return asyncio.run(coro)


def an_ask(answer: str):
    """An `ask` that answers, and records that it was asked."""

    class Asker:
        def __init__(self) -> None:
            self.questions: list[str] = []

        async def __call__(self, question: str) -> str:
            self.questions.append(question)
            return answer

    return Asker()


def a_broken_ask():
    async def ask(question: str) -> str:
        raise RuntimeError("no provider available")

    return ask


# -- addressing --------------------------------------------------------------


def test_a_message_that_addresses_a_seat_asks_no_model() -> None:
    seats = builtin_charters()
    ask = an_ask("odysseus: never consulted")
    decision = run(triage("athena: drill me on graphs", seats, ask=ask))
    assert decision.seat.key == "athena"
    assert decision.by == "named"
    assert ask.questions == []


@pytest.mark.parametrize(
    "message,key",
    [
        ("@odysseus who is hiring in Chennai?", "odysseus"),
        ("Odysseus, find me the salary bands", "odysseus"),
        ("to melinoe: check this capability", "melinoe"),
        ("Icarus — build me a parser", "icarus"),
        ("dave: what is eating my disk", "dave"),
    ],
)
def test_every_way_of_addressing_a_seat_works(message: str, key: str) -> None:
    decision = run(triage(message, builtin_charters(), ask=an_ask("")))
    assert decision.seat.key == key
    assert decision.by == "named"


def test_a_leading_word_that_is_not_a_seat_is_an_ordinary_sentence() -> None:
    """SPEC §7: an unrecognised name does not stop the request."""
    seats = builtin_charters()
    ask = an_ask("daedalus: general")
    assert addressed("Right, so the deploy broke", seats) is None
    decision = run(triage("Right, so the deploy broke", seats, ask=ask))
    assert decision.by == "model"
    assert ask.questions, "an unaddressed message should reach the router"


def test_a_charter_alone_makes_a_ninth_seat_routable() -> None:
    """The menu is the cabinet — there is no second list of responsibilities."""
    from dataclasses import replace

    seats = builtin_charters()
    seats["synthia"] = replace(
        seats["athena"], key="synthia", name="Synthia", charter="Synthesizes nonsense."
    )
    assert "synthia (Synthia): Synthesizes nonsense." in menu(seats)
    decision = run(triage("do the thing", seats, ask=an_ask("synthia: it is hers")))
    assert decision.seat.key == "synthia"


# -- reading the answer ------------------------------------------------------


def test_a_made_up_seat_is_not_a_route() -> None:
    seats = builtin_charters()
    assert read("synthia: she can do it", seats) is None
    decision = run(triage("do the thing", seats, ask=an_ask("synthia: she can do it")))
    assert decision.seat.key == "daedalus"
    assert not decision.routed


def test_prose_that_mentions_a_seat_is_not_an_answer() -> None:
    """'not Athena, this is Odysseus's' contains both names; neither is a choice."""
    seats = builtin_charters()
    assert read("Not Athena, this one is Odysseus's.", seats) is None
    assert read("", seats) is None
    assert read("   \n\n", seats) is None


def test_the_display_name_reads_as_well_as_the_key() -> None:
    seats = builtin_charters()
    parsed = read("Athena: practice", seats)
    assert parsed is not None
    assert parsed[0].key == "athena"
    assert parsed[1] == "practice"


def test_a_chatty_answer_still_reads_when_the_name_leads() -> None:
    seats = builtin_charters()
    parsed = read("- **melinoe**: review it", seats)
    assert parsed is not None
    assert parsed[0].key == "melinoe"


def test_a_reason_is_one_line_and_bounded() -> None:
    seats = builtin_charters()
    parsed = read("heracles: " + "x" * 500, seats)
    assert parsed is not None
    assert len(parsed[1]) == MAX_REASON


# -- refusing to be a second owner -------------------------------------------


def test_a_follow_up_stays_with_the_seat_already_handling_it() -> None:
    decision = run(
        triage("and the second one?", builtin_charters(), ask=an_ask(""), previous="athena")
    )
    assert decision.seat.key == "athena"
    assert decision.by == "previous"


def test_a_broken_router_does_not_break_the_turn() -> None:
    decision = run(
        triage("drill me on graphs", builtin_charters(), ask=a_broken_ask(), previous="athena")
    )
    assert decision.seat.key == "athena"
    assert "could not run" in decision.reason


def test_a_message_too_short_to_be_about_anything_spends_no_call() -> None:
    ask = an_ask("athena: guess")
    decision = run(triage("ok", builtin_charters(), ask=ask, previous="athena"))
    assert decision.seat.key == "athena"
    assert decision.by == "previous"
    assert ask.questions == []


def test_routing_without_an_asker_keeps_the_active_seat() -> None:
    decision = run(triage("drill me on graphs", builtin_charters()))
    assert decision.seat.key == "daedalus"
    assert decision.by == "default"


def test_an_active_seat_that_is_not_the_default_keeps_what_falls_through() -> None:
    """`active = true` on Athena means her, not Daedalus, when nothing is sure."""
    from dataclasses import replace

    seats = builtin_charters()
    seats["daedalus"] = replace(seats["daedalus"], active=False)
    seats["athena"] = replace(seats["athena"], active=True)
    decision = run(triage("do the thing", seats, ask=an_ask("nobody: unclear")))
    assert decision.seat.key == "athena"


# -- at the ingress ----------------------------------------------------------


@pytest.fixture()
def runtime(settings: Settings) -> Runtime:
    return build_runtime(
        settings, surface=ScriptedSurface(approve=True), provider=ScriptedProvider([])
    )


def provider(runtime: Runtime, script: list[ChatResult]) -> ScriptedProvider:
    scripted = ScriptedProvider(script)
    runtime.model = ModelClient(runtime.settings, providers=[scripted])
    return scripted


def test_a_routed_message_answers_as_that_seat(settings: Settings) -> None:
    from daedalus.main import attribution, route

    rt = build_runtime(
        settings, surface=ScriptedSurface(approve=True), provider=ScriptedProvider([])
    )
    scripted = provider(
        rt,
        [
            ChatResult(content="athena: DSA practice"),
            ChatResult(content="Graphs first — twenty minutes."),
        ],
    )
    event = InboundEvent(text="I keep failing the graph questions", user_id="owner", is_dm=True)

    async def go():
        decision = await route(rt, event)
        result = await Engine(rt).run_turn(event, seat=decision.seat.key)
        return decision, result

    decision, result = run(go())
    assert decision.seat.key == "athena"
    assert result.seat == "athena"
    # The turn's own prompt is Athena's: her charter, not the coordinator's.
    system = scripted.calls[-1][0]["content"]
    assert "Athena owns getting the owner better at coding" in system
    assert "Daedalus is the coordinator" not in system
    assert attribution(rt, decision) == "**Athena** · DSA practice"


def test_the_owner_is_not_told_who_they_are_already_talking_to(settings: Settings) -> None:
    from daedalus.main import attribution, route

    rt = build_runtime(
        settings, surface=ScriptedSurface(approve=True), provider=ScriptedProvider([])
    )
    provider(rt, [ChatResult(content="daedalus: priorities")])
    event = InboundEvent(text="what should I do today?", user_id="owner")
    decision = run(route(rt, event))
    assert decision.seat.key == "daedalus"
    assert attribution(rt, decision) == ""


def test_the_ingress_remembers_the_seat_for_the_next_message(settings: Settings) -> None:
    from daedalus.main import route

    rt = build_runtime(
        settings, surface=ScriptedSurface(approve=True), provider=ScriptedProvider([])
    )
    provider(rt, [ChatResult(content="athena: DSA practice"), ChatResult(content="odysseus: jobs")])
    first = InboundEvent(text="drill me on graphs", user_id="owner", channel_id="dm-1")
    second = InboundEvent(text="", user_id="owner", channel_id="dm-1")
    run(route(rt, first))
    decision = run(route(rt, second))
    assert decision.seat.key == "athena"
    assert decision.by == "previous"
    assert rt.last_seat["dm-1"] == "athena"


def test_a_provider_that_dies_leaves_the_message_on_the_active_seat(settings: Settings) -> None:
    """A classifier's bad day must not cost the owner their request."""
    rt = build_runtime(
        settings, surface=ScriptedSurface(approve=True), provider=ScriptedProvider([])
    )

    class Dead:
        name, model, window_s, max_calls, is_local = "dead", "dead", 60, 0, False

        def available(self) -> bool:
            return True

        async def complete(self, *a, **k):
            raise RuntimeError("connection refused")

    rt.model = ModelClient(rt.settings, providers=[Dead()])
    event = InboundEvent(text="what should I do today?", user_id="owner")

    async def go():
        from daedalus.main import route

        return await route(rt, event)

    decision = run(go())
    assert decision.seat.key == "daedalus"
