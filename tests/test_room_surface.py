"""The owner sees the room (T33) — the adopted surface, as the DM allows it.

The adopted decision: the room appears where the owner already is, traffic as
the seats' own lines, joining by replying, the record kept. A Discord DM cannot
host real threads, so the *projection* is honest about what it is: the broker is
the truth, every room line posts to the owner labeled with the room, and a
Discord **reply** to a room line resolves back to the room — the turn is told
which room the owner answered, with the words to speak into it and the one to
stop it. Nothing is summarized; the whole exchange is one `op=thread` away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.cabinet.broker import RoomError, broker_for
from daedalus.cabinet.protocol import Message
from daedalus.loop.context import build_messages
from daedalus.runtime import Session
from daedalus.surface.base import InboundEvent
from daedalus.tools.message import MessageTool


def _room_msg(room_id: str, sender: str, recipient: str, mtype: str = "partial", text: str = "…") -> Message:
    return Message(sender=sender, recipient=recipient, type=mtype, payload={"text": text}, correlation_id=room_id)


@pytest.fixture()
def coordinator(runtime):  # noqa: ANN001
    runtime.session = Session(seat="daedalus")
    return runtime


# -- the room appears, and its lines are the seats' own -------------------------


async def test_convening_posts_the_room_card_where_the_owner_is(coordinator) -> None:
    result = await MessageTool().run(
        {"op": "convene", "seats": "athena, icarus", "text": "the workshop"}, coordinator
    )
    assert result.ok, result.output
    room_id = result.data["room"]

    posted = [t for t in coordinator.surface.threads if t[1] == room_id]
    assert posted and "the workshop" in posted[0][0], (
        "the room card names the subject — the owner sees what the room is about"
    )
    assert "Reply here to join" in posted[0][0], "the card says how to join"


async def test_each_line_in_a_room_posts_as_the_seat_that_said_it(coordinator) -> None:
    result = await MessageTool().run(
        {"op": "convene", "seats": "athena", "text": "the quiet room"}, coordinator
    )
    room_id = result.data["room"]

    sent = await MessageTool().run(
        {"op": "send", "to": "athena", "type": "partial", "text": "the numbers are in", "correlation_id": room_id},
        coordinator,
    )
    assert sent.ok, sent.output

    lines = [t for t in coordinator.surface.threads if t[1] == room_id and "the numbers are in" in t[0]]
    assert lines and "**Daedalus**: the numbers are in" in lines[0][0], (
        "traffic is the seat's own line — never summarized into one voice"
    )


async def test_mail_outside_a_room_posts_nothing(coordinator) -> None:
    await MessageTool().run(
        {"op": "send", "to": "athena", "type": "request", "text": "ordinary mail"}, coordinator
    )
    assert all(t[1] is None for t in coordinator.surface.threads), (
        "point-to-point mail is not room traffic; the room projection shows only rooms"
    )


# -- stopping it -----------------------------------------------------------------


async def test_the_owner_can_stop_the_room_and_it_says_what_ends(coordinator) -> None:
    result = await MessageTool().run(
        {"op": "convene", "seats": "athena", "text": "the stopped room"}, coordinator
    )
    room_id = result.data["room"]
    broker = broker_for(coordinator.settings.home)
    broker.send(_room_msg(room_id, "daedalus", "athena", "request", "on the queue"))
    assert broker.pending("athena") == 1

    closed = await MessageTool().run({"op": "close", "room": room_id}, coordinator)
    assert closed.ok, closed.output

    room = broker.room(room_id)
    assert room.status == "closed"
    assert broker.pending("athena") == 0, "stopped means its mail resolves — no seat works a dead exchange"
    with pytest.raises(RoomError):
        broker.send(_room_msg(room_id, "athena", "daedalus"))
    closing = [t for t in coordinator.surface.threads if t[1] == room_id and "closed" in t[0]]
    assert closing, "the owner sees the room end"


async def test_a_seat_that_is_not_in_the_room_cannot_close_it(coordinator) -> None:
    result = await MessageTool().run(
        {"op": "convene", "seats": "athena", "text": "not melinoe's room"}, coordinator
    )
    room_id = result.data["room"]
    coordinator.session = Session(seat="melinoe")

    refused = await MessageTool().run({"op": "close", "room": room_id}, coordinator)
    assert not refused.ok and "not in the room" in refused.output
    assert broker_for(coordinator.settings.home).room(room_id).open


# -- the turn is told which room the owner answered ------------------------------


def test_a_reply_to_a_room_line_reaches_the_prompt(coordinator) -> None:
    result = MessageTool.__new__(MessageTool)  # noqa: PLW017 — only the dataclass-free path is needed
    convened = coordinator.seats and True
    assert convened
    broker = broker_for(coordinator.settings.home)
    room = broker.convene(convener="daedalus", seats=["athena"], subject="the replied room")

    event = InboundEvent(
        text="that is enough for today", user_id="owner", channel_id="dm", thread_id=room.id
    )
    messages = build_messages(coordinator, event, seat="daedalus")
    prompt = messages[0]["content"]

    assert "The owner, inside a room" in prompt
    assert "the replied room" in prompt and room.id in prompt
    assert f"op=thread correlation_id={room.id}" in prompt, "the full exchange is one read away"
    assert f"op=close room={room.id}" in prompt, "the stop is named where the owner can use it"


def test_an_ordinary_dm_carries_no_room_section(coordinator) -> None:
    event = InboundEvent(text="plain message", user_id="owner", channel_id="dm")
    prompt = build_messages(coordinator, event, seat="daedalus")[0]["content"]
    assert "The owner, inside a room" not in prompt


# -- the bot resolves a reply to the room it answered -----------------------------


def test_the_bot_resolves_a_reply_to_its_room() -> None:
    """The pure resolution (kept separate for exactly this): a reply whose
    reference points at a room line joins that room; anything else is a DM."""
    from daedalus.surface.discord.bot import DiscordSurface

    s = DiscordSurface.__new__(DiscordSurface)
    s._room_lines = {"999": "room-abc"}

    class Ref:
        message_id = 999

    class Reply:
        reference = Ref()

    class Plain:
        reference = None

    assert s._room_of_reply(Reply()) == "room-abc"
    assert s._room_of_reply(Plain()) is None
    s._room_lines.clear()
    assert s._room_of_reply(Reply()) is None, (
        "a restart forgets the projection — the broker stays the truth, and "
        "nothing about the room is lost"
    )
