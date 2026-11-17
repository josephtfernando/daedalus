"""The owner sees the room (T33) — the adopted surface, as the DM allows it.

The broker is the truth; every room line posts to the owner labeled with the
room, joining is replying, and a Discord **reply** to a room line resolves back
to the room. The turn is told which room the owner answered, with the words to
speak into it and the one to stop it — nothing is summarized, and the whole
exchange is one ``op=thread`` away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.cabinet.broker import RoomError, broker_for
from daedalus.cabinet.protocol import Message
from daedalus.config import Settings
from daedalus.loop.context import build_messages
from daedalus.model.providers import ScriptedProvider
from daedalus.runtime import Session, build_runtime
from daedalus.storage import paths
from daedalus.surface.base import InboundEvent
from daedalus.surface.cli import ScriptedSurface
from daedalus.tools.message import MessageTool


def _room_msg(room_id: str, sender: str, recipient: str, mtype: str = "partial", text: str = "…") -> Message:
    return Message(sender=sender, recipient=recipient, type=mtype, payload={"text": text}, correlation_id=room_id)


@pytest.fixture()
def coordinator(runtime):  # noqa: ANN001
    runtime.session = Session(seat="daedalus")
    return runtime


async def test_the_room_appears_where_the_owner_is_and_its_lines_are_the_seats_own(
    coordinator,
) -> None:
    result = await MessageTool().run(
        {"op": "convene", "seats": "athena, icarus", "text": "the workshop"}, coordinator
    )
    assert result.ok, result.output
    room_id = result.data["room"]

    posted = [t for t in coordinator.surface.threads if t[1] == room_id]
    assert posted and "the workshop" in posted[0][0], "the card names the subject"
    assert "Reply here to join" in posted[0][0], "the card says how to join"

    # Traffic is the seat's own line — never summarized into one voice.
    sent = await MessageTool().run(
        {
            "op": "send",
            "to": "athena",
            "type": "partial",
            "text": "the numbers are in",
            "correlation_id": room_id,
        },
        coordinator,
    )
    assert sent.ok, sent.output
    lines = [t for t in coordinator.surface.threads if t[1] == room_id and "the numbers are in" in t[0]]
    assert lines and "**Daedalus**: the numbers are in" in lines[0][0]

    # Point-to-point mail is not room traffic; the projection shows only rooms.
    before = len(coordinator.surface.threads)
    await MessageTool().run(
        {"op": "send", "to": "athena", "type": "request", "text": "ordinary mail"}, coordinator
    )
    assert len(coordinator.surface.threads) == before
    assert not any("ordinary mail" in text for text, _ in coordinator.surface.threads)


async def test_the_owner_can_stop_a_room_and_a_non_member_cannot(
    coordinator, tmp_path: Path
) -> None:
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
    assert broker.pending("athena") == 0, "stopped means its mail resolves"
    with pytest.raises(RoomError):
        broker.send(_room_msg(room_id, "athena", "daedalus"))
    closing = [t for t in coordinator.surface.threads if t[1] == room_id and "closed" in t[0]]
    assert closing, "the owner sees the room end"

    # A seat that is not in the room cannot close it, through the same tool.
    home = tmp_path / "other-home"
    paths.ensure_tree(home)
    other = build_runtime(
        Settings(home=home, env={}, heavy=[], cheap=None),
        surface=ScriptedSurface(approve=True),
        provider=ScriptedProvider([]),
    )
    other.session = Session(seat="daedalus")
    result = await MessageTool().run(
        {"op": "convene", "seats": "athena", "text": "not melinoe's room"}, other
    )
    other_room = result.data["room"]
    other.session = Session(seat="melinoe")
    refused = await MessageTool().run({"op": "close", "room": other_room}, other)
    assert not refused.ok and "not in the room" in refused.output
    assert broker_for(home).room(other_room).open


def test_a_reply_to_a_room_line_tells_the_turn_which_room_it_answered(coordinator) -> None:
    broker = broker_for(coordinator.settings.home)
    room = broker.convene(convener="daedalus", seats=["athena"], subject="the replied room")

    event = InboundEvent(
        text="that is enough for today", user_id="owner", channel_id="dm", thread_id=room.id
    )
    prompt = build_messages(coordinator, event, seat="daedalus")[0]["content"]

    assert "The owner, inside a room" in prompt
    assert "the replied room" in prompt and room.id in prompt
    assert f"op=thread correlation_id={room.id}" in prompt, "the full exchange is one read away"
    assert f"op=close room={room.id}" in prompt, "the stop is named where the owner can use it"

    # An ordinary DM carries no room section.
    plain = InboundEvent(text="plain message", user_id="owner", channel_id="dm")
    prompt = build_messages(coordinator, plain, seat="daedalus")[0]["content"]
    assert "The owner, inside a room" not in prompt

    # The pure resolution: a reply pointing at a room line joins that room;
    # anything else is a DM. A restart forgets the projection, and only it.
    from daedalus.surface.discord.bot import DiscordSurface

    surface = DiscordSurface.__new__(DiscordSurface)
    surface._room_lines = {"999": "room-abc"}

    class Ref:
        message_id = 999

    class Reply:
        reference = Ref()

    class Plain:
        reference = None

    assert surface._room_of_reply(Reply()) == "room-abc"
    assert surface._room_of_reply(Plain()) is None
    surface._room_lines.clear()
    assert surface._room_of_reply(Reply()) is None
