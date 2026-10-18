"""The inbox (T31): a message left for a seat meets it on that seat's next turn.

Broker-side mechanics were T14's (durable, dedup, attempts); what is under test
here is the *hand-over*: the offer appears in the turn, the answer closes the
exchange, a repeat of the offer is visible and named, reading keeps the
message, and a reply carries the exchange it answers.
"""

from __future__ import annotations

from typing import Any

from daedalus.cabinet.broker import broker_for
from daedalus.cabinet.protocol import Message
from daedalus.config import Settings
from daedalus.loop.context import build_messages
from daedalus.loop.engine import Engine
from daedalus.model.client import ModelClient
from daedalus.model.providers import ChatResult, ScriptedProvider
from daedalus.runtime import Runtime, build_runtime
from daedalus.surface.base import InboundEvent
from tests.conftest import tool_call


async def test_a_message_is_offered_on_the_seats_next_turn(runtime: Runtime) -> None:
    """Offer, resolve, and the thread that outlives the offer."""
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call(
                        "message",
                        op="answer",
                        id="INBOX-1",
                        type="result",
                        text="Started on graphs; BFS and union-find first.",
                    ),
                    ChatResult(content="Answered."),
                ]
            ),
        ],
    )
    engine = Engine(runtime)
    event = InboundEvent(text="stand by", user_id="owner", is_dm=True)
    msgs = build_messages(runtime, event, seat="athena")
    prompt = msgs[0]["content"]
    assert "## Inbox" not in prompt

    # A message arrives for her.
    broker = broker_for(runtime.settings.home)
    receipt = broker.send(
        Message(
            sender="dave",
            recipient="athena",
            type="request",
            payload={"text": "Drill BFS, then report."},
            id="INBOX-1",  # chosen before the first send — the protocol's own rule
        )
    )
    message_id = receipt.message.id

    # The offer: next turn as athena (scripted turn 2 answers with the tool).
    result = await engine.run_turn(event, seat="athena")
    assert result.tools_used == ["message"]
    assert not result.error

    resolved = broker.get(message_id)
    assert resolved is not None and not broker.pending("athena")
    thread = broker.thread(resolved.correlation)
    kinds = [(m.sender, m.type) for m in thread]
    assert ("dave", "request") in kinds and ("athena", "result") in kinds, (
        "the thread reads the exchange whole, from either side"
    )
    answers = [m for m in thread if m.sender == "athena"]
    assert answers and answers[0].causation_id == message_id, "a reply carries what it answers"


async def test_an_unanswered_message_is_offered_again_and_named(runtime: Runtime) -> None:
    """Second offer is another attempt, visible — never a silent never."""
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider([ChatResult(content="(thinking; not answering the queue)")]),
            ScriptedProvider([ChatResult(content="(still not answering)")]),
        ],
    )
    broker = broker_for(runtime.settings.home)
    broker.send(
        Message(sender="dave", recipient="melinoe", type="verify", payload={"text": "Verify the gate verdicts."})
    )
    engine = Engine(runtime)
    event = InboundEvent(text="hmm", user_id="owner", is_dm=True)
    first = await engine.run_turn(event, seat="melinoe")
    second = await engine.run_turn(event, seat="melinoe")
    assert broker.pending("melinoe") == 1, "reading never deletes; an unanswered message stays"
    inbox = broker.inbox("melinoe")
    assert inbox[0].attempts >= 3, (
        "the second turn that failed to answer is counted as another attempt"
    )
    msgs = build_messages(runtime, event, seat="melinoe", inbox=inbox_block_for(runtime, "melinoe"))
    assert "offered" in msgs[0]["content"], "the offer names its attempts, in the turn itself"
    assert first is not None and second is not None


def inbox_block_for(runtime: Runtime, seat_key: str) -> str:
    from daedalus.loop.context import inbox_block

    return inbox_block(runtime, seat_key)


async def test_the_answer_of_a_second_attempt_says_what_happened(runtime: Runtime) -> None:
    """A resolved message answered again: refused, and the refusal says 'the first answer stands'."""
    from daedalus.tools.message import MessageTool

    broker = broker_for(runtime.settings.home)
    sent = broker.send(
        Message(sender="dave", recipient="athena", type="request", payload={"text": "Two facts, please."})
    )
    tool = MessageTool()
    runtime.session = __import__("daedalus.runtime", fromlist=["Session"]).Session(seat="athena")
    ok = await tool.run({"op": "answer", "id": sent.message.id, "type": "result", "text": "Both."}, runtime)
    assert ok.ok, ok.output
    again = await tool.run(
        {"op": "answer", "id": sent.message.id, "type": "result", "text": "Double typo of the same."}, runtime
    )
    assert not again.ok and "first answer stands" in again.output
    assert len(broker.thread(sent.message.correlation)) == 2, (
        "the refused second answer is not in the thread — one answer, not two"
    )


async def test_sends_that_open_and_continue_exchanges(runtime: Runtime) -> None:
    """A send opens its own exchange; a causation continues one; a retry dedups."""
    from daedalus.tools.message import MessageTool

    broker = broker_for(runtime.settings.home)
    tool = MessageTool()
    runtime.session = __import__("daedalus.runtime", fromlist=["Session"]).Session(seat="athena")

    opener = await tool.run(
        {"op": "send", "to": "melinoe", "type": "request", "text": "How are Tuesday's verdicts looking?"},
        runtime,
    )
    assert opener.ok, opener.output
    correlation = opener.data["correlation_id"]

    follow = await tool.run(
        {
            "op": "send",
            "to": "melinoe",
            "type": "partial",
            "text": "Still reading the earlier ones.",
            "correlation_id": correlation,
            "causation_id": opener.data["id"],
        },
        runtime,
    )
    assert follow.ok

    retry = await tool.run(
        {"op": "send", "to": "melinoe", "type": "request", "text": "How are Tuesday's verdicts looking?", "id": opener.data["id"]},
        runtime,
    )
    assert retry.ok and broker.pending("melinoe") == 2, (
        "a retry with the same id is the same message — one queue row per id"
    )

    thread = broker.thread(correlation)
    assert [m.type for m in thread] == ["request", "partial"], (
        "the exchange reads back in the order it happened"
    )

    nobody = await tool.run({"op": "send", "to": "nobody", "type": "request", "text": "Hello?"}, runtime)
    assert not nobody.ok and "not a seat" in nobody.output, (
        "a message to nobody is a queue that never drains — refused by name"
    )
