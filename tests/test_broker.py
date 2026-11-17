"""Seat-to-seat messages: typed, durable, at-least-once (ARCHITECTURE §9.2, T14).

The message is the interface and the queue is where the guarantees live: a
message outlives the daemon that sent it, a retry is recognisable as the same
message rather than a second one, and a reply carries the exchange it answers.
Every durability test builds a *second* broker on the same path — an in-memory
implementation passes the rest of this file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from daedalus.cabinet.broker import Broker, UnknownMessage, broker_for, db_path
from daedalus.cabinet.protocol import (
    ACK,
    FAILURE,
    PARTIAL,
    REQUEST,
    RESULT,
    TERMINAL,
    TYPES,
    VERIFY,
    Message,
    MessageError,
    seat_key,
)


def ask(sender: str = "icarus", recipient: str = "melinoe", **kw) -> Message:
    body = {"question": "does run.py write outside its declared scopes?", "artifact": "disk_free_check"}
    body.update(kw.pop("payload", {}))
    return Message(sender=sender, recipient=recipient, type=kw.pop("type", VERIFY), payload=body, **kw)


def test_the_message_shape_is_typed_and_has_an_identity_before_it_is_sent() -> None:
    # The vocabulary is closed: a seat that reads `restult` answers nothing.
    assert TYPES == (REQUEST, ACK, PARTIAL, RESULT, VERIFY, FAILURE, "cancel")
    with pytest.raises(MessageError) as refused:
        ask(type="restult")
    assert "restult" in str(refused.value)
    for known in TYPES:
        assert known in str(refused.value)

    # `ack` and `result` are different promises to a seat that is waiting.
    assert TERMINAL == {"result", "failure", "cancel"}
    assert ask(type=ACK).terminal is False
    assert ask(type=PARTIAL).terminal is False
    assert ask(type=RESULT).terminal is True
    assert ask(type=FAILURE).terminal is True

    # The id exists before the first attempt, chosen by the sender — the whole of
    # dedup rests on this, and a retry can reuse it.
    original = ask()
    assert original.id, "the message has no identity until someone else gives it one"
    assert ask().id != original.id, "two messages share an identity"
    retry = Message(
        sender=original.sender,
        recipient=original.recipient,
        type=original.type,
        payload=original.payload,
        id=original.id,
    )
    assert retry.id == original.id
    assert retry.correlation == original.correlation


def test_a_message_that_cannot_be_addressed_or_stored_is_refused() -> None:
    # `Melinoë` is what the owner reads; `melinoe` is what can be addressed.
    with pytest.raises(MessageError) as refused:
        Message(sender="icarus", recipient="Melinoë", type=REQUEST)
    assert "melinoe" in str(refused.value)
    assert "not a seat key" in str(refused.value)
    assert seat_key("  MELINOE  ") == "melinoe", "a key is not case- or space-sensitive"

    # A seat cannot send itself a message; the refusal names the fix.
    with pytest.raises(MessageError) as refused:
        ask(sender="icarus", recipient="Icarus")
    assert "memory" in str(refused.value), "the fix was not named"

    # The payload is a mapping and belongs to the message once stored.
    with pytest.raises(MessageError):
        Message(sender="icarus", recipient="melinoe", type=REQUEST, payload=["not", "a", "mapping"])
    facts = {"a": 1}
    message = Message(sender="icarus", recipient="melinoe", type=REQUEST, payload=facts)
    facts["a"] = 2
    assert message.payload == {"a": 1}, "the sender kept a handle on the stored message"


def test_a_reply_carries_the_exchange_it_answers(tmp_path: Path) -> None:
    # On the object: correlation, causation, and the answer goes back the way it came.
    request = ask()
    answer = request.reply(type=RESULT, payload={"ok": True})
    assert answer.sender == "melinoe" and answer.recipient == "icarus"
    assert answer.causation_id == request.id
    assert answer.answers == request.id
    assert answer.correlation_id == request.correlation == request.id
    assert answer.id not in ("", request.id)

    # A second hop stays in the same exchange; causation is one hop.
    follow_up = answer.reply(type=ACK)
    assert follow_up.correlation_id == request.id, "the second hop opened a new exchange"
    assert follow_up.causation_id == answer.id, "causation stopped being one hop"
    assert follow_up.sender == "icarus" and follow_up.recipient == "melinoe"

    # Replying does not mutate what it answers.
    before = (request.correlation_id, request.causation_id, request.type)
    request.reply(type=RESULT)
    assert (request.correlation_id, request.causation_id, request.type) == before

    # The durable thread: ask → die before answering → pick it up → answer, across
    # three brokers on one path. The whole exchange is one read away.
    question = ask(type=VERIFY, payload={"artifact": "daily_focus"})
    broker_for(tmp_path).send(question)
    broker_for(tmp_path).inbox("melinoe")  # offered, and then the process dies

    reborn = broker_for(tmp_path)
    [delivery] = reborn.inbox("melinoe")
    assert delivery.attempts == 2, "the replay went unremarked"
    reply = delivery.message.reply(type=RESULT, payload={"ok": True, "verify": "clean"})
    reborn.send(reply)
    reborn.resolve(question.id, outcome="ok", note="verified")

    after = broker_for(tmp_path)
    assert after.inbox("melinoe") == [], "the answered question was offered again"
    assert after.pending("melinoe") == 0
    # The answer is itself a message Icarus has not read: an exchange is not over
    # the moment it is replied to.
    assert after.pending("icarus") == 1
    exchange = after.thread(question.id)
    assert [m.id for m in exchange] == [question.id, reply.id]
    assert after.thread(reply.correlation) == exchange, "the reply could not find its exchange"
    assert exchange[1].causation_id == question.id
    assert exchange[1].payload["verify"] == "clean"
    assert [m.type for m in exchange] == [VERIFY, RESULT]


def test_a_message_and_its_answer_survive_a_restart_and_a_retry_is_not_a_second_message(
    tmp_path: Path,
) -> None:
    # A seat message is not a variable that happens to be in scope.
    first = ask()
    broker_for(tmp_path).send(first)
    after = broker_for(tmp_path)
    [delivery] = after.inbox("melinoe")
    assert delivery.message.id == first.id
    assert delivery.message.payload == first.payload
    assert delivery.message.correlation == first.id
    assert after.get(first.id) is not None

    # At-least-once, made visible: the same message again, with its count. The
    # broker cannot know an earlier attempt finished, so it says how many it made.
    [again] = after.inbox("melinoe")
    assert again.message.id == first.id
    assert again.attempts == 2, "a replay was indistinguishable from first contact"

    # A retry at the door is deduped and keeps its place in the queue.
    broker = after
    second = ask()
    broker.send(second)
    retried = broker.send(first)
    assert retried.duplicate is True and retried.message.id == first.id
    assert broker.pending() == 2, "the retry was stored twice"
    assert [d.message.id for d in broker.inbox("melinoe")] == [first.id, second.id], (
        "the retry jumped the queue"
    )

    # A new id is a new message: the id is the sender's word, all the broker has.
    third = ask()
    broker.send(third)
    assert broker.pending() == 3

    # An answer is recorded once, the replay is reported, and it outlives the process.
    assert broker.resolve(first.id, outcome="ok") is True
    assert broker.resolve(first.id, outcome="ok") is False, "a replay overwrote the first answer"
    assert first.id not in [d.message.id for d in broker.inbox("melinoe")]
    broker_for(tmp_path).resolve(second.id)
    assert [d.message.id for d in broker_for(tmp_path).inbox("melinoe")] == [third.id]


def test_the_inbox_is_your_own_mail_in_arrival_order_and_the_broker_makes_its_own_directory(
    tmp_path: Path,
) -> None:
    # A hand-made home may not have cabinet/ yet.
    path = tmp_path / "elsewhere" / "cabinet" / "messages.db"
    Broker(path).send(ask())

    assert path.exists()

    broker = broker_for(tmp_path)
    first = ask(sender="icarus", recipient="melinoe")
    second = ask(sender="daedalus", recipient="melinoe", type=REQUEST)
    theirs = ask(sender="icarus", recipient="athena", type=REQUEST)
    for message in (first, second, theirs):
        broker.send(message)

    assert [d.message.id for d in broker.inbox("melinoe")] == [first.id, second.id]
    assert [d.message.id for d in broker.inbox("Athena")] == [theirs.id], (
        "a seat key is not case-sensitive"
    )
    assert broker.inbox("melinoe", limit=1)[0].message.id == first.id
    assert broker.pending() == 3
    assert broker.pending("melinoe") == 2


def test_a_wrong_answer_can_be_reopened_and_a_message_nobody_sent_is_refused(
    tmp_path: Path,
) -> None:
    broker = broker_for(tmp_path)

    # `False` would read as "already answered", which is a different fact.
    with pytest.raises(UnknownMessage):
        broker.resolve("01NOSUCHMESSAGE")
    with pytest.raises(UnknownMessage):
        broker.reopen("01NOSUCHMESSAGE")

    # Not crash recovery — the broker can see a missing answer, not a wrong one.
    message = ask()
    broker.send(message)
    broker.resolve(message.id)
    assert broker.reopen(message.id) is True
    [again] = broker.inbox("melinoe")
    assert again.message.id == message.id
    assert again.attempts == 2, "the history was lost, so the seat cannot know it is a repeat"
    assert broker.reopen(message.id) is False, "a message that is open was reopened"

    # A hand-edited database is a loud failure, not a message full of nothing.
    with sqlite3.connect(db_path(tmp_path)) as c:
        c.execute("UPDATE seat_messages SET payload = '{oops' WHERE id = ?", (message.id,))
    with pytest.raises(MessageError) as refused:
        broker.get(message.id)
    assert message.id in str(refused.value)
    assert "corrupt" in str(refused.value)
