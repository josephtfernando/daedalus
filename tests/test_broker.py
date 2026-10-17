"""Seat-to-seat messages: typed, durable, at-least-once (ARCHITECTURE §9.2, T14).

The claim under test is that the *message* is the interface and the queue is
where the guarantees live. Three ways of getting that wrong are worth a test
each: a queue that only lives in the process (a conversation that ends when the
daemon does), a retry that is indistinguishable from a new message (so the
receiver does the work twice and nobody can tell), and a reply that carries its
own correlation instead of the exchange it belongs to (so a thread is a set of
messages that happened to be near each other).

Every durability test builds a **second** broker on the same path. An in-memory
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


# -- the message shape -------------------------------------------------------


def test_the_types_are_exactly_the_sevens_the_spec_names() -> None:
    assert TYPES == (REQUEST, ACK, PARTIAL, RESULT, VERIFY, FAILURE, "cancel")


def test_an_unknown_type_is_refused_naming_the_vocabulary() -> None:
    """A seat that reads `restult` answers nothing, and nobody learns why."""
    with pytest.raises(MessageError) as refused:
        ask(type="restult")

    assert "restult" in str(refused.value)
    for known in TYPES:
        assert known in str(refused.value)


def test_an_ack_is_not_an_answer() -> None:
    """`ack` and `result` are different promises to a seat that is waiting."""
    assert TERMINAL == {"result", "failure", "cancel"}
    assert ask(type=ACK).terminal is False
    assert ask(type=PARTIAL).terminal is False
    assert ask(type=RESULT).terminal is True
    assert ask(type=FAILURE).terminal is True


def test_the_id_exists_before_the_message_is_ever_sent() -> None:
    """The whole of dedup rests on this.

    An id minted inside the send (or by the broker) is new on every retry, so a
    crash-and-resend is a second request and no downstream check can tell. The id
    is chosen by the sender, before the first attempt, which is the only
    arrangement where a retry is recognisable as the same message.
    """
    first = ask()

    assert first.id, "the message has no identity until someone else gives it one"
    assert ask().id != first.id, "two messages share an identity"


def test_a_retry_can_reuse_its_id_which_is_the_point() -> None:
    original = ask()
    retry = Message(
        sender=original.sender,
        recipient=original.recipient,
        type=original.type,
        payload=original.payload,
        id=original.id,
    )

    assert retry.id == original.id
    assert retry.correlation == original.correlation


def test_a_display_name_is_refused_with_the_key_spelled_out() -> None:
    """`Melinoë` is what the owner reads; `melinoe` is what can be addressed."""
    with pytest.raises(MessageError) as refused:
        Message(sender="icarus", recipient="Melinoë", type=REQUEST)

    assert "melinoe" in str(refused.value)
    assert "not a seat key" in str(refused.value)
    assert seat_key("  MELINOE  ") == "melinoe", "a key is not case- or space-sensitive"


def test_a_seat_cannot_send_itself_a_message() -> None:
    """The routing slip, not the feature: the sender's own key as the recipient."""
    with pytest.raises(MessageError) as refused:
        ask(sender="icarus", recipient="Icarus")

    assert "memory" in str(refused.value), "the fix was not named"


def test_the_payload_must_be_a_mapping_and_belongs_to_the_message() -> None:
    with pytest.raises(MessageError):
        Message(sender="icarus", recipient="melinoe", type=REQUEST, payload=["not", "a", "mapping"])

    facts = {"a": 1}
    message = Message(sender="icarus", recipient="melinoe", type=REQUEST, payload=facts)
    facts["a"] = 2

    assert message.payload == {"a": 1}, "the sender kept a handle on the stored message"


# -- a reply knows what it answers ------------------------------------------


def test_a_reply_carries_the_exchange_and_the_id_it_answers() -> None:
    """T14 (3/3), on the object. The durable version is the broker's test."""
    request = ask()

    answer = request.reply(type=RESULT, payload={"ok": True})

    assert answer.sender == "melinoe" and answer.recipient == "icarus", "the answer went back the way it came"
    assert answer.causation_id == request.id
    assert answer.answers == request.id
    assert answer.correlation_id == request.correlation == request.id
    assert answer.id not in ("", request.id)


def test_a_second_hop_stays_in_the_same_exchange() -> None:
    """`Icarus → Melinoë (verify) → Icarus (result)`: one exchange, two hops."""
    request = ask(type=VERIFY)
    answer = request.reply(type=RESULT)
    follow_up = answer.reply(type=ACK)

    assert follow_up.correlation_id == request.id, "the second hop opened a new exchange"
    assert follow_up.causation_id == answer.id, "causation stopped being one hop"
    assert follow_up.sender == "icarus" and follow_up.recipient == "melinoe"


def test_a_reply_does_not_mutate_what_it_answers() -> None:
    request = ask()
    before = (request.correlation_id, request.causation_id, request.type)

    request.reply(type=RESULT)

    assert (request.correlation_id, request.causation_id, request.type) == before


# -- the queue ---------------------------------------------------------------


def test_a_message_survives_a_restart(tmp_path: Path) -> None:
    """T14 (1/3). A seat message is not a variable that happens to be in scope."""
    first = ask()
    broker_for(tmp_path).send(first)

    after = broker_for(tmp_path)
    [delivery] = after.inbox("melinoe")

    assert delivery.message.id == first.id
    assert delivery.message.payload == first.payload
    assert delivery.message.correlation == first.id
    assert after.get(first.id) is not None


def test_a_retried_send_is_deduped_and_keeps_its_place_in_the_queue(tmp_path: Path) -> None:
    """T14 (2/3), at the door. A retry is not a second message."""
    broker = broker_for(tmp_path)
    first, second = ask(), ask()
    broker.send(first)
    broker.send(second)

    again = broker.send(first)

    assert again.duplicate is True
    assert again.message.id == first.id
    assert broker.pending() == 2, "the retry was stored twice"
    assert [d.message.id for d in broker.inbox("melinoe")] == [first.id, second.id], (
        "the retry jumped the queue"
    )


def test_a_retry_the_broker_never_saw_is_a_new_message(tmp_path: Path) -> None:
    """The other half of the contract: a *new* id is a new message.

    Simulation of what a genuinely fresh request looks like — the broker cannot
    and must not guess that two messages with different ids are the same one. The
    id is the sender's word; a sender that mints a new one has said "this is
    new".
    """
    broker = broker_for(tmp_path)
    broker.send(ask())

    broker.send(ask())

    assert broker.pending() == 2


def test_an_answer_is_recorded_once_and_the_replay_is_reported(tmp_path: Path) -> None:
    """T14 (2/3), at the answer. The second resolve is the duplicate of the first."""
    broker = broker_for(tmp_path)
    message = ask()
    broker.send(message)

    assert broker.resolve(message.id, outcome="ok") is True
    assert broker.resolve(message.id, outcome="ok") is False, "a replay overwrote the first answer"
    assert broker.pending() == 0
    assert broker.inbox("melinoe") == []


def test_an_offered_message_nobody_answered_comes_back_with_its_count(tmp_path: Path) -> None:
    """At-least-once, made visible: `attempts=2` is the seat saying "not me first".

    The broker cannot know whether an earlier attempt finished its work, so it
    hands the message over again and says how many times it has done so. The
    seat decides; the broker refuses to invent the certainty it does not have.
    """
    broker = broker_for(tmp_path)
    broker.send(ask())

    [first] = broker.inbox("melinoe")
    [second] = broker.inbox("melinoe")

    assert first.attempts == 1
    assert second.attempts == 2, "a replay was indistinguishable from first contact"
    assert second.message.id == first.message.id


def test_a_resolution_outlives_the_process_that_made_it(tmp_path: Path) -> None:
    broker_for(tmp_path).send(ask())
    message = broker_for(tmp_path).inbox("melinoe")[0].message
    broker_for(tmp_path).resolve(message.id)

    assert broker_for(tmp_path).inbox("melinoe") == []


def test_the_inbox_is_your_own_mail_in_arrival_order(tmp_path: Path) -> None:
    broker = broker_for(tmp_path)
    first = ask(sender="icarus", recipient="melinoe")
    second = ask(sender="daedalus", recipient="melinoe", type=REQUEST)
    theirs = ask(sender="icarus", recipient="athena", type=REQUEST)
    for message in (first, second, theirs):
        broker.send(message)

    assert [d.message.id for d in broker.inbox("melinoe")] == [first.id, second.id]
    assert [d.message.id for d in broker.inbox("Athena")] == [theirs.id], "a seat key is not case-sensitive"
    assert broker.inbox("melinoe", limit=1)[0].message.id == first.id
    assert broker.pending() == 3
    assert broker.pending("melinoe") == 2


def test_an_answer_to_a_message_nobody_sent_is_refused(tmp_path: Path) -> None:
    """`False` would read as "already answered", which is a different fact."""
    broker = broker_for(tmp_path)

    with pytest.raises(UnknownMessage):
        broker.resolve("01NOSUCHMESSAGE")
    with pytest.raises(UnknownMessage):
        broker.reopen("01NOSUCHMESSAGE")


def test_a_wrong_answer_can_be_put_back_on_the_queue(tmp_path: Path) -> None:
    """Not crash recovery — the broker cannot see a bad answer, only a missing one."""
    broker = broker_for(tmp_path)
    message = ask()
    broker.send(message)
    broker.resolve(message.id)

    assert broker.reopen(message.id) is True
    [again] = broker.inbox("melinoe")
    assert again.message.id == message.id
    assert again.attempts == 2, "the history was lost, so the seat cannot know it is a repeat"
    assert broker.reopen(message.id) is False, "a message that is open was reopened"


def test_a_thread_reads_the_whole_exchange_in_order(tmp_path: Path) -> None:
    """T14 (3/3): `thread()` on any member answers with the same exchange."""
    broker = broker_for(tmp_path)
    request = ask(type=VERIFY)
    broker.send(request)
    [delivery] = broker.inbox("melinoe")
    answer = delivery.message.reply(type=RESULT, payload={"ok": True, "findings": "none"})
    broker.send(answer)
    broker.resolve(request.id, outcome="ok", note="answered")

    thread = broker.thread(request.id)

    assert [m.id for m in thread] == [request.id, answer.id]
    assert broker.thread(answer.correlation) == thread, "the reply could not find its own exchange"
    assert thread[1].causation_id == thread[0].id
    assert thread[1].payload["findings"] == "none"


def test_a_corrupt_row_is_refused_rather_than_guessed_at(tmp_path: Path) -> None:
    """A hand-edited database is a loud failure, not a message full of nothing."""
    broker = broker_for(tmp_path)
    message = ask()
    broker.send(message)
    with sqlite3.connect(db_path(tmp_path)) as c:
        c.execute("UPDATE seat_messages SET payload = '{oops' WHERE id = ?", (message.id,))

    with pytest.raises(MessageError) as refused:
        broker.get(message.id)

    assert message.id in str(refused.value)
    assert "corrupt" in str(refused.value)


def test_the_broker_creates_its_own_directory(tmp_path: Path) -> None:
    """A fresh home has `cabinet/`; a home made by hand (a test, a CLI probe) may not."""
    path = tmp_path / "elsewhere" / "cabinet" / "messages.db"

    Broker(path).send(ask())

    assert path.exists()


def test_the_whole_ticket_in_one_flow(tmp_path: Path) -> None:
    """Ask → die → pick it up → answer, across three brokers on one path.

    The exchange is ARCHITECTURE §7.5's: Icarus asks Melinoë to verify something,
    the daemon stops before she answers, and the next process finds the question
    waiting — offered a second time, and answered once.
    """
    question = ask(type=VERIFY, payload={"artifact": "daily_focus"})
    broker_for(tmp_path).send(question)
    broker_for(tmp_path).inbox("melinoe")  # offered, and then the process dies

    reborn = broker_for(tmp_path)
    [delivery] = reborn.inbox("melinoe")
    assert delivery.message.id == question.id, "the question did not survive the restart"
    assert delivery.attempts == 2, "the replay went unremarked"

    reply = delivery.message.reply(type=RESULT, payload={"ok": True, "verify": "clean"})
    reborn.send(reply)
    reborn.resolve(question.id, outcome="ok", note="verified")

    after = broker_for(tmp_path)
    assert after.inbox("melinoe") == [], "the answered question was offered again"
    assert after.pending("melinoe") == 0
    # The answer is itself a message, and Icarus has not read it: a queue that
    # counted it as nothing would say an exchange is over the moment it is replied
    # to, before the seat that asked ever hears the outcome.
    assert after.pending("icarus") == 1
    exchange = after.thread(question.id)
    assert [m.type for m in exchange] == [VERIFY, RESULT]
    assert exchange[1].causation_id == question.id
    assert exchange[0].sender == "icarus" and exchange[1].sender == "melinoe"
