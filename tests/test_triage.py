"""Who takes this message (SPEC §7).

Routing is a *preference*, and these tests are mostly about the ways it refuses
to be one: an answer that is not a seat, prose that merely mentions a seat, a
provider that is down, a message too short to be about anything. Every one of
those has to end with the owner's request still intact — on the seat already
handling the conversation, or on the coordinator.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

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


def provider(runtime: Runtime, script: list[ChatResult]) -> ScriptedProvider:
    scripted = ScriptedProvider(script)
    runtime.model = ModelClient(runtime.settings, providers=[scripted])
    return scripted


def test_naming_a_seat_routes_to_it_without_asking_the_model() -> None:
    seats = builtin_charters()
    ask = an_ask("odysseus: never consulted")
    decision = run(triage("athena: drill me on graphs", seats, ask=ask))
    assert decision.seat.key == "athena"
    assert decision.by == "named"
    assert ask.questions == []

    # Every way of addressing a seat works.
    for message, key in [
        ("@odysseus who is hiring in Chennai?", "odysseus"),
        ("Odysseus, find me the salary bands", "odysseus"),
        ("to melinoe: check this capability", "melinoe"),
        ("Icarus — build me a parser", "icarus"),
        ("dave: what is eating my disk", "dave"),
    ]:
        decision = run(triage(message, builtin_charters(), ask=an_ask("")))
        assert decision.seat.key == key, message
        assert decision.by == "named", message

    # The menu is the cabinet — a charter alone makes a ninth seat routable.
    seats = builtin_charters()
    seats["synthia"] = replace(
        seats["athena"], key="synthia", name="Synthia", charter="Synthesizes nonsense."
    )
    assert "synthia (Synthia): Synthesizes nonsense." in menu(seats)
    decision = run(triage("do the thing", seats, ask=an_ask("synthia: it is hers")))
    assert decision.seat.key == "synthia"


def test_a_name_the_cabinet_does_not_know_is_an_ordinary_sentence() -> None:
    seats = builtin_charters()

    # SPEC §7: an unrecognised name does not stop the request.
    ask = an_ask("daedalus: general")
    assert addressed("Right, so the deploy broke", seats) is None
    decision = run(triage("Right, so the deploy broke", seats, ask=ask))
    assert decision.by == "model"
    assert ask.questions, "an unaddressed message should reach the router"

    # A made-up seat is not a route.
    assert read("synthia: she can do it", seats) is None
    decision = run(triage("do the thing", seats, ask=an_ask("synthia: she can do it")))
    assert decision.seat.key == "daedalus"
    assert not decision.routed

    # Prose that mentions a seat is not an answer.
    assert read("Not Athena, this one is Odysseus's.", seats) is None
    assert read("", seats) is None
    assert read("   \n\n", seats) is None


def test_an_answer_reads_only_when_a_known_name_leads() -> None:
    seats = builtin_charters()

    # The display name reads as well as the key.
    parsed = read("Athena: practice", seats)
    assert parsed is not None
    assert parsed[0].key == "athena"
    assert parsed[1] == "practice"

    # A chatty answer still reads when the name leads.
    parsed = read("- **melinoe**: review it", seats)
    assert parsed is not None
    assert parsed[0].key == "melinoe"

    # The reason is one line and bounded.
    parsed = read("heracles: " + "x" * 500, seats)
    assert parsed is not None
    assert len(parsed[1]) == MAX_REASON


def test_a_turn_never_loses_the_request_when_routing_cannot_decide() -> None:
    # A follow-up stays with the seat already handling it.
    decision = run(
        triage("and the second one?", builtin_charters(), ask=an_ask(""), previous="athena")
    )
    assert decision.seat.key == "athena"
    assert decision.by == "previous"

    # A broken router does not break the turn.
    decision = run(
        triage("drill me on graphs", builtin_charters(), ask=a_broken_ask(), previous="athena")
    )
    assert decision.seat.key == "athena"
    assert "could not run" in decision.reason

    # A message too short to be about anything spends no call.
    ask = an_ask("athena: guess")
    decision = run(triage("ok", builtin_charters(), ask=ask, previous="athena"))
    assert decision.seat.key == "athena"
    assert decision.by == "previous"
    assert ask.questions == []

    # Routing without an asker keeps the active seat.
    decision = run(triage("drill me on graphs", builtin_charters()))
    assert decision.seat.key == "daedalus"
    assert decision.by == "default"

    # `active = true` on Athena means her, not Daedalus, when nothing is sure.
    seats = builtin_charters()
    seats["daedalus"] = replace(seats["daedalus"], active=False)
    seats["athena"] = replace(seats["athena"], active=True)
    decision = run(triage("do the thing", seats, ask=an_ask("nobody: unclear")))
    assert decision.seat.key == "athena"


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


def test_the_ingress_remembers_the_seat_and_a_dead_provider_costs_nothing(
    settings: Settings,
) -> None:
    from daedalus.main import attribution, route

    # The owner is not told who they are already talking to.
    rt = build_runtime(
        settings, surface=ScriptedSurface(approve=True), provider=ScriptedProvider([])
    )
    provider(rt, [ChatResult(content="daedalus: priorities")])
    event = InboundEvent(text="what should I do today?", user_id="owner")
    decision = run(route(rt, event))
    assert decision.seat.key == "daedalus"
    assert attribution(rt, decision) == ""

    # The ingress remembers the seat for the next message.
    rt = build_runtime(
        settings, surface=ScriptedSurface(approve=True), provider=ScriptedProvider([])
    )
    provider(
        rt, [ChatResult(content="athena: DSA practice"), ChatResult(content="odysseus: jobs")]
    )
    first = InboundEvent(text="drill me on graphs", user_id="owner", channel_id="dm-1")
    second = InboundEvent(text="", user_id="owner", channel_id="dm-1")
    run(route(rt, first))
    decision = run(route(rt, second))
    assert decision.seat.key == "athena"
    assert decision.by == "previous"
    assert rt.last_seat["dm-1"] == "athena"

    # A classifier's bad day must not cost the owner their request.
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
    decision = run(route(rt, InboundEvent(text="what should I do today?", user_id="owner")))
    assert decision.seat.key == "daedalus"


def provider(runtime: Runtime, script: list[ChatResult]) -> ScriptedProvider:
    scripted = ScriptedProvider(script)
    runtime.model = ModelClient(runtime.settings, providers=[scripted])
    return scripted
