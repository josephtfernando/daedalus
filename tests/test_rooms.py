"""Rooms: bounded multi-seat exchanges (T32) — the bounds the owner adopted.

The owner's decision of 19 Oct, adopted as drafted: only the coordinator
convenes; a room caps at 8 messages; a seat joins at most 2 rooms a day and the
coordinator convenes at most 1; a room nobody answers for 24 hours ends as a
*named* dead end with its mail resolved, not a queue that grows; and an answer
must be ``verify``-ed before it leaves the room.

Every bound is enforced where the counting is true — the broker — and every
refusal says the bound it hit, because a cap that stops silently is a cap
nobody can account for. The "only the coordinator" half is enforced one layer
up, in the tool that knows whose turn is asking.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from daedalus.cabinet.broker import (
    ROOM_CONVENE_CAP,
    ROOM_HOP_CAP,
    ROOM_JOIN_CAP,
    Broker,
    RoomError,
    broker_for,
)
from daedalus.cabinet.protocol import Message
from daedalus.runtime import Session
from daedalus.tools.message import MessageTool

#: Pinned so cap days and sweep cutoffs are the test's, not the wall clock's.
NOW = datetime(2026, 10, 20, 9, 0).timestamp()


def room_msg(
    room_id: str,
    sender: str,
    recipient: str,
    mtype: str = "partial",
    text: str = "…",
    *,
    when: float | None = None,
) -> Message:
    return Message(
        sender=sender,
        recipient=recipient,
        type=mtype,
        payload={"text": text},
        correlation_id=room_id,
        created_at=NOW if when is None else when,
    )


@pytest.fixture()
def broker(tmp_path):
    return broker_for(tmp_path)


def _convene(broker: Broker, seats: tuple[str, ...] = ("athena", "icarus"), subject: str = "the workshop", *, now: float = NOW):
    return broker.convene(convener="daedalus", seats=list(seats), subject=subject, now=now)


# -- convening: the spend the owner capped ------------------------------------


def test_the_coordinator_convenes_and_the_room_is_an_exchange(broker: Broker) -> None:
    room = _convene(broker)

    assert room.open and room.convener == "daedalus"
    assert room.seats == ("daedalus", "athena", "icarus")
    assert room.message_count == 0
    assert broker.room(room.id) is not None, "a room is durable — it survives the convening turn"


def test_a_room_of_one_is_refused(broker: Broker) -> None:
    with pytest.raises(RoomError, match="besides the convener"):
        broker.convene(convener="daedalus", seats=["daedalus"], subject="a note to self", now=NOW)
    with pytest.raises(RoomError, match="besides the convener"):
        broker.convene(convener="daedalus", seats=[], subject="empty", now=NOW)


def test_the_coordinator_convenes_once_a_day(broker: Broker) -> None:
    _convene(broker)

    with pytest.raises(RoomError) as refused:
        _convene(broker, seats=("melinoe",), subject="a second spend")
    assert ROOM_CONVENE_CAP >= 1 and "already convened a room today" in str(refused.value)

    # Tomorrow is a new day — the cap counts *days*, not convenings ever.
    room = _convene(broker, seats=("melinoe",), subject="tomorrow's", now=NOW + 86400)
    assert room.open


def test_a_seat_joins_at_most_two_open_rooms(broker: Broker) -> None:
    """The join cap — the bound that keeps rooms from becoming the drip.

    It counts OPEN rooms (an open room is still spending the seat's attention)
    and skips the convener, whose bound is the convene cap: he is in every room
    he convenes, so a cap that counted him could only ever fire on him first
    and the per-seat bound would be dead code.
    """
    broker.convene(convener="daedalus", seats=["athena"], subject="room one", now=NOW - 86400)
    room_two = broker.convene(
        convener="daedalus", seats=["athena", "icarus"], subject="room two", now=NOW
    )
    assert room_two.open, "two open rooms for a seat is inside the cap"

    with pytest.raises(RoomError) as refused:
        broker.convene(
            convener="daedalus", seats=["icarus", "athena"], subject="a third for athena",
            now=NOW + 86400,  # tomorrow: a new convene day, but the open rooms still count
        )
    assert "athena" in str(refused.value) and f"the cap is {ROOM_JOIN_CAP}" in str(refused.value)


# -- inside the room: the hop cap ---------------------------------------------


def test_the_room_spends_eight_messages_and_then_says_why_it_stops(broker: Broker) -> None:
    """T32's acceptance: an exchange that would exceed its bounds *stops and says why*."""
    room = _convene(broker)
    for i in range(ROOM_HOP_CAP):
        sender, recipient = ("daedalus", "athena") if i % 2 == 0 else ("athena", "icarus")
        broker.send(room_msg(room.id, sender, recipient, text=f"message {i}"))

    assert broker.room(room.id).message_count == ROOM_HOP_CAP

    with pytest.raises(RoomError) as refused:
        broker.send(room_msg(room.id, "daedalus", "athena", text="one more"))
    assert "spent its 8 messages" in str(refused.value) and "two rooms" in str(refused.value), (
        "the refusal names the bound and the way out"
    )


def test_a_room_is_convened_not_overheard(broker: Broker) -> None:
    room = _convene(broker)

    with pytest.raises(RoomError) as refused:
        broker.send(room_msg(room.id, "melinoe", "athena"))
    assert "not in the room" in str(refused.value)


# -- verify before leaving ------------------------------------------------------


def test_an_answer_leaves_only_after_it_is_verified(broker: Broker) -> None:
    room = _convene(broker)
    broker.send(room_msg(room.id, "daedalus", "athena", "partial", "the numbers"))

    leaving = Message(
        sender="athena",
        recipient="melinoe",  # outside the room: this is a result *leaving*
        type="result",
        payload={"text": "the answer"},
        correlation_id=room.id,
    )
    with pytest.raises(RoomError) as refused:
        broker.send(leaving)
    assert "verify" in str(refused.value) and "leaves the room" in str(refused.value)

    # A verify from another seat inside the exchange is what unbars the door.
    broker.send(room_msg(room.id, "icarus", "daedalus", "verify", "checked against the store"))
    broker.send(leaving)  # accepted now
    assert broker.room(room.id).message_count == 3, (
        "partial, verify, and the verified result — the first leaving was refused "
        "before it was stored, so it is not in the count"
    )


def test_intermediate_chatter_needs_no_verify(broker: Broker) -> None:
    """Intermediate traffic stays inside the room: the rule is about *leaving*."""
    room = _convene(broker)
    broker.send(room_msg(room.id, "athena", "icarus", "partial", "half of it"))
    assert broker.room(room.id).message_count == 1


# -- the dead end ----------------------------------------------------------------


def test_a_room_nobody_answers_ends_as_a_named_dead_end(broker: Broker) -> None:
    """T32's acceptance: no queue that grows — the exchange ends, and says so."""
    room = broker.convene(
        convener="daedalus", seats=["athena"], subject="the quiet one", now=NOW - 86400 - 3600
    )
    broker.send(
        room_msg(room.id, "daedalus", "athena", "request", "anything?", when=NOW - 86400 - 3600)
    )
    assert broker.pending("athena") == 1, "the mail is on the queue before the room dies"

    dead = broker.sweep_dead_rooms(now=NOW)

    (the_room,) = dead
    assert the_room.id == room.id and the_room.status == "dead"
    assert the_room.subject == "the quiet one", "the dead end is *named* — that is the report"
    assert broker.pending("athena") == 0, (
        "its unresolved mail is resolved dead: no seat is offered a message from an "
        "exchange that is over"
    )
    assert broker.sweep_dead_rooms(now=NOW) == [], "a dead room is not re-killed"


def test_a_live_room_is_not_swept(broker: Broker) -> None:
    room = broker.convene(convener="daedalus", seats=["athena"], subject="active", now=NOW - 3600)
    broker.send(room_msg(room.id, "athena", "daedalus", "partial", "still working", when=NOW - 1800))

    assert broker.sweep_dead_rooms(now=NOW) == []
    assert broker.room(room.id).open


# -- the tool: whose turn it is decides who convenes ----------------------------


async def test_only_the_coordinators_turn_convenes(runtime) -> None:
    runtime.session = Session(seat="athena")
    tool = MessageTool()
    result = await tool.run(
        {"op": "convene", "seats": "icarus, melinoe", "text": "a room athena wants"}, runtime
    )
    assert not result.ok and "only the coordinator convenes" in result.output
    assert broker_for(runtime.settings.home).rooms() == [], "nothing was created by the refusal"


async def test_the_coordinator_convenes_through_the_tool(runtime) -> None:
    runtime.session = Session(seat="daedalus")
    tool = MessageTool()
    result = await tool.run(
        {"op": "convene", "seats": "athena, icarus", "text": "the workshop"}, runtime
    )
    assert result.ok, result.output
    room_id = result.data["room"]
    broker = broker_for(runtime.settings.home)
    assert broker.room(room_id).seats == ("daedalus", "athena", "icarus")

    listed = await tool.run({"op": "rooms", "text": ""}, runtime)
    assert listed.ok and "the workshop" in listed.output and room_id in listed.output


async def test_the_tool_refuses_unknown_seats_and_a_missing_list(runtime) -> None:
    runtime.session = Session(seat="daedalus")
    tool = MessageTool()
    result = await tool.run({"op": "convene", "seats": "zeus", "text": "nobody"}, runtime)
    assert not result.ok and "not seats in this cabinet" in result.output
    result = await tool.run({"op": "convene", "text": "no seats named"}, runtime)
    assert not result.ok and "needs `seats`" in result.output


# -- the tick reports the dead end -------------------------------------------------


async def test_the_tick_names_the_rooms_it_ends(runtime) -> None:
    broker = broker_for(runtime.settings.home)
    stale = broker.convene(
        convener="daedalus", seats=["athena"], subject="the overnight one",
        now=datetime.now().timestamp() - 25 * 3600,
    )
    broker.send(
        room_msg(
            stale.id, "daedalus", "athena", "request", "before the sweep",
            when=datetime.now().timestamp() - 25 * 3600,
        )
    )

    results = await runtime.scheduler.dispatch_tick(  # type: ignore[union-attr]
        runtime, when=datetime.now() + timedelta(minutes=1)
    )
    sweeps = [r for r in results if r.job == f"room:{stale.id}"]
    assert sweeps and sweeps[0].ran and "the overnight one" in sweeps[0].detail, (
        "the dead room is named in the tick's own record — that is what "
        "'a reported dead end' means"
    )
    assert broker.room(stale.id).status == "dead"
