"""The Discord surface (SPEC §9).

Two properties matter more than they look:

- **A dropped message is logged.** The surface first shipped with silent drops,
  so "the bot ignored me" and "the bot is dead" were indistinguishable from
  outside. These tests pin the logging, not just the dropping.
- **The typing indicator never costs a turn.** It is cosmetic; a hiccup raising
  one must not stop the work.
"""

from __future__ import annotations

import logging
from pathlib import Path

from daedalus.config import Settings
from daedalus.surface.discord.bot import DiscordSurface, chunk_text

OWNER = "560324452726865920"


def surface(owner_ids: set[str] | None = None, handler=None) -> DiscordSurface:  # noqa: ANN001
    settings = Settings(
        home=Path("/tmp/daedalus-surface-test"),
        env={},
        heavy=[],
        cheap=None,
        owner_users=owner_ids if owner_ids is not None else {OWNER},
    )
    return DiscordSurface(settings, handler or (lambda event: None))  # type: ignore[arg-type]


class StubAuthor:
    def __init__(self, uid: str, *, bot: bool = False) -> None:
        self.id = uid
        self.bot = bot


class StubMessage:
    def __init__(self, uid: str, channel: object) -> None:
        self.author = StubAuthor(uid)
        self.channel = channel
        self.content = "hello"


class NotAChannel:
    """Deliberately not a DMChannel — a guild text channel stands in for it."""

    id = 12345


async def test_a_stranger_is_dropped_and_logged(caplog) -> None:  # noqa: ANN001
    served: list[object] = []
    s = surface(handler=lambda event: served.append(event) or _noop())

    with caplog.at_level(logging.INFO, logger="daedalus.surface.discord"):
        await s.on_message(StubMessage("111111111111111111", NotAChannel()))

    assert served == [], "a message from a stranger reached the turn engine"
    assert "not on the allowlist" in caplog.text


async def test_a_guild_message_from_the_owner_is_dropped_and_logged(caplog) -> None:  # noqa: ANN001
    served: list[object] = []
    s = surface(handler=lambda event: served.append(event) or _noop())

    with caplog.at_level(logging.INFO, logger="daedalus.surface.discord"):
        # The owner, but not in a DM: DM-first, by design (SPEC §9).
        await s.on_message(StubMessage(OWNER, NotAChannel()))

    assert served == [], "a non-DM message was served"
    assert "DM-first" in caplog.text


async def test_a_dropped_message_never_reveals_its_content(caplog) -> None:  # noqa: ANN001
    """The log is not a place for the owner's words (AGENTS.md, SPEC §6)."""
    s = surface()

    with caplog.at_level(logging.DEBUG, logger="daedalus.surface.discord"):
        await s.on_message(StubMessage(OWNER, NotAChannel()))

    assert "hello" not in caplog.text


async def _noop() -> None: ...


class RecordingTyping:
    def __init__(self, log: list[str], *, explode: bool = False) -> None:
        self.log = log
        self.explode = explode

    async def __aenter__(self) -> None:
        if self.explode:
            raise RuntimeError("no typing for you")
        self.log.append("enter")

    async def __aexit__(self, *exc: object) -> None:
        self.log.append("exit")


class TypingChannel:
    def __init__(self, log: list[str], *, explode: bool = False) -> None:
        self.log = log
        self.explode = explode

    def typing(self) -> RecordingTyping:
        return RecordingTyping(self.log, explode=self.explode)


async def test_working_holds_a_typing_indicator_for_the_turn() -> None:
    s = surface()
    record: list[str] = []
    s.client.get_channel = lambda _id: TypingChannel(record)  # type: ignore[method-assign]

    async with s.working("12345"):
        record.append("working")

    assert record == ["enter", "working", "exit"]


async def test_working_releases_typing_when_the_turn_raises() -> None:
    s = surface()
    record: list[str] = []
    s.client.get_channel = lambda _id: TypingChannel(record)  # type: ignore[method-assign]

    try:
        async with s.working("12345"):
            raise ValueError("the turn blew up")
    except ValueError:
        pass

    assert record == ["enter", "exit"], "the indicator was left hanging"


async def test_a_typing_failure_does_not_kill_the_turn() -> None:
    """Cosmetic only: the work must still run."""
    s = surface()
    record: list[str] = []
    s.client.get_channel = lambda _id: TypingChannel(record, explode=True)  # type: ignore[method-assign]

    ran = False
    async with s.working("12345"):
        ran = True

    assert ran, "a typing hiccup stopped the turn"


async def test_working_without_a_channel_is_a_no_op() -> None:
    s = surface()
    async with s.working(None):
        pass
    async with s.working("not-an-id"):
        pass


async def test_approval_buttons_name_the_action() -> None:
    """Generic Approve/Deny makes the owner re-read the prompt to know what they
    are choosing. The buttons should say what the action is."""
    from daedalus.surface.base import ApprovalRequest
    from daedalus.surface.discord.approvals import ApprovalView

    request = ApprovalRequest(
        action="capability",
        scope="capability:remove",
        reason="it can change things on this machine, so it asks first",
        args={"op": "remove", "name": "probe"},
        action_hash="h",
        title="Delete a capability",
        affirm_label="Delete it",
        deny_label="Keep it",
    )

    view = ApprovalView(request, {OWNER})

    assert [c.label for c in view.children] == ["Delete it", "Keep it"]


def test_long_replies_are_chunked_under_the_discord_limit() -> None:
    body = "\n".join(f"line {i}" for i in range(500))
    chunks = chunk_text(body)
    assert all(len(c) <= 1900 for c in chunks)
    assert "".join(chunks).replace("\n", "") == body.replace("\n", "")
