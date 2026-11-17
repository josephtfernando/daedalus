"""Rooms: bounded multi-seat exchanges (T32) — the bounds the owner adopted.

Only the coordinator convenes; a room caps at 8 messages; a seat joins at most 2
rooms a day and the coordinator convenes at most 1; a room nobody answers for 24
hours ends as a *named* dead end with its mail resolved; and an answer must be
``verify``-ed before it leaves. Every bound is enforced where the counting is true
— the broker — and every refusal says the bound it hit.
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


def _convene(
    broker: Broker,
    seats: tuple[str, ...] = ("athena", "icarus"),
    subject: str = "the workshop",
    *,
    now: float = NOW,
):
    return broker.convene(convener="daedalus", seats=list(seats), subject=subject, now=now)


def test_convening_respects_the_spend_caps_and_says_which_bound_it_hit(
    broker: Broker, tmp_path
) -> None:
    room = _convene(broker)
    assert room.open and room.convener == "daedalus"
    assert room.seats == ("daedalus", "athena", "icarus")
    assert room.message_count == 0
    assert broker.room(room.id) is not None, "a room is durable — it survives the convening turn"

    # A room of one (or none) is not a room.
    with pytest.raises(RoomError, match="besides the convener"):
        broker.convene(convener="daedalus", seats=["daedalus"], subject="a note to self", now=NOW)
    with pytest.raises(RoomError, match="besides the convener"):
        broker.convene(convener="daedalus", seats=[], subject="empty", now=NOW)

    # The coordinator convenes once a day — the cap counts days, not convenings ever.
    with pytest.raises(RoomError) as refused:
        _convene(broker, seats=("melinoe",), subject="a second spend")
    assert ROOM_CONVENE_CAP >= 1 and "already convened a room today" in str(refused.value)
    room = _convene(broker, seats=("melinoe",), subject="tomorrow's", now=NOW + 86400)
    assert room.open

    # The join cap counts OPEN rooms and skips the convener, whose bound is the
    # convene cap.
    joins = broker_for(tmp_path / "joins")
    joins.convene(
        convener="daedalus", seats=["athena"], subject="room one", now=NOW - 86400
    )
    room_two = joins.convene(
        convener="daedalus", seats=["athena", "icarus"], subject="room two", now=NOW
    )
    assert room_two.open, "two open rooms for a seat is inside the cap"
    with pytest.raises(RoomError) as refused:
        joins.convene(
            convener="daedalus",
            seats=["icarus", "athena"],
            subject="a third for athena",
            now=NOW + 86400,  # a new convene day; the open rooms still count
        )
    assert "athena" in str(refused.value)
    assert f"the cap is {ROOM_JOIN_CAP}" in str(refused.value)


def test_a_room_stops_at_its_hop_cap_and_only_admits_its_own(broker: Broker) -> None:
    room = _convene(broker)

    # A room is convened, not overheard.
    with pytest.raises(RoomError) as refused:
        broker.send(room_msg(room.id, "melinoe", "athena"))
    assert "not in the room" in str(refused.value)

    # T32's acceptance: an exchange that would exceed its bounds stops and says why.
    for i in range(ROOM_HOP_CAP):
        sender, recipient = ("daedalus", "athena") if i % 2 == 0 else ("athena", "icarus")
        broker.send(room_msg(room.id, sender, recipient, text=f"message {i}"))
    assert broker.room(room.id).message_count == ROOM_HOP_CAP

    with pytest.raises(RoomError) as refused:
        broker.send(room_msg(room.id, "daedalus", "athena", text="one more"))
    assert "spent its 8 messages" in str(refused.value)
    assert "two rooms" in str(refused.value), "the refusal names the bound and the way out"


def test_an_answer_leaves_only_after_a_verify_and_a_dead_room_is_a_named_end(
    broker: Broker, tmp_path
) -> None:
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

    # Intermediate chatter needs no verify: the rule is about *leaving*.
    assert broker.room(room.id).message_count == 1

    # A verify from another seat inside the exchange is what unbars the door.
    broker.send(room_msg(room.id, "icarus", "daedalus", "verify", "checked against the store"))
    broker.send(leaving)
    assert broker.room(room.id).message_count == 3, (
        "partial, verify, and the verified result — the refused leaving was not stored"
    )

    # A room nobody answers ends as a named dead end with its mail resolved.
    dead_broker = broker_for(tmp_path / "dead")
    quiet = dead_broker.convene(
        convener="daedalus", seats=["athena"], subject="the quiet one", now=NOW - 86400 - 3600
    )
    dead_broker.send(
        room_msg(quiet.id, "daedalus", "athena", "request", "anything?", when=NOW - 86400 - 3600)
    )
    assert dead_broker.pending("athena") == 1, "the mail is on the queue before the room dies"
    dead = dead_broker.sweep_dead_rooms(now=NOW)
    (the_room,) = dead
    assert the_room.id == quiet.id and the_room.status == "dead"
    assert the_room.subject == "the quiet one", "the dead end is *named* — that is the report"
    assert dead_broker.pending("athena") == 0, (
        "its unresolved mail resolves dead: no seat is offered a message from an exchange "
        "that is over"
    )
    assert dead_broker.sweep_dead_rooms(now=NOW) == [], "a dead room is not re-killed"

    # A live room is not swept.
    live_broker = broker_for(tmp_path / "live")
    live = live_broker.convene(
        convener="daedalus", seats=["athena"], subject="active", now=NOW - 3600
    )
    live_broker.send(
        room_msg(live.id, "athena", "daedalus", "partial", "still working", when=NOW - 1800)
    )
    assert live_broker.sweep_dead_rooms(now=NOW) == []
    assert live_broker.room(live.id).open


async def test_only_the_coordinators_turn_convenes_and_the_tick_names_what_it_ends(
    runtime,
) -> None:
    broker = broker_for(runtime.settings.home)
    tool = MessageTool()

    # Whose turn it is decides who convenes.
    runtime.session = Session(seat="athena")
    result = await tool.run(
        {"op": "convene", "seats": "icarus, melinoe", "text": "a room athena wants"}, runtime
    )
    assert not result.ok and "only the coordinator convenes" in result.output
    assert broker.rooms() == [], "nothing was created by the refusal"

    # A room left unanswered from yesterday, for the tick to end.
    stale = broker.convene(
        convener="daedalus",
        seats=["athena"],
        subject="the overnight one",
        now=datetime.now().timestamp() - 25 * 3600,
    )
    broker.send(
        room_msg(
            stale.id,
            "daedalus",
            "athena",
            "request",
            "before the sweep",
            when=datetime.now().timestamp() - 25 * 3600,
        )
    )

    # The coordinator convenes through the tool, and the room is listable.
    runtime.session = Session(seat="daedalus")
    result = await tool.run(
        {"op": "convene", "seats": "athena, icarus", "text": "the workshop"}, runtime
    )
    assert result.ok, result.output
    room_id = result.data["room"]
    assert broker.room(room_id).seats == ("daedalus", "athena", "icarus")
    listed = await tool.run({"op": "rooms", "text": ""}, runtime)
    assert listed.ok and "the workshop" in listed.output and room_id in listed.output

    # Unknown seats and a missing list are refused by name.
    result = await tool.run({"op": "convene", "seats": "zeus", "text": "nobody"}, runtime)
    assert not result.ok and "not seats in this cabinet" in result.output
    result = await tool.run({"op": "convene", "text": "no seats named"}, runtime)
    assert not result.ok and "needs `seats`" in result.output

    # The tick's own record names the dead room it ends.
    results = await runtime.scheduler.dispatch_tick(  # type: ignore[union-attr]
        runtime, when=datetime.now() + timedelta(minutes=1)
    )
    sweeps = [r for r in results if r.job == f"room:{stale.id}"]
    assert sweeps and sweeps[0].ran and "the overnight one" in sweeps[0].detail
    assert broker.room(stale.id).status == "dead"
