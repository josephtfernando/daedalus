"""The Discord surface (SPEC §9).

Two properties matter more than they look:

- **A dropped message is logged.** The surface first shipped with silent drops,
  so "the bot ignored me" and "the bot is dead" were indistinguishable from
  outside. These tests pin the logging, not just the dropping.
- **The typing indicator never costs a turn.** It is cosmetic; a hiccup raising
  one must not stop the work.
"""

from __future__ import annotations

import asyncio
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


# -- the work trace: what is happening, visible while it happens --------------


class TraceMessage:
    """A Discord message that remembers its edits and deletions."""

    def __init__(self, body: str) -> None:
        self.content = body
        self.edits: list[str] = []
        self.deleted = False

    async def edit(self, *, content: str) -> "TraceMessage":
        self.content = content
        self.edits.append(content)
        return self

    async def delete(self) -> None:
        self.deleted = True


class TraceChannel(TypingChannel):
    def __init__(self) -> None:
        super().__init__([])
        self.sent: list[TraceMessage] = []

    async def send(self, body: str) -> TraceMessage:
        message = TraceMessage(body)
        self.sent.append(message)
        return message


async def test_the_turn_shows_its_tool_calls_and_clears_them_afterwards() -> None:
    """The owner watches the work happen, then gets a clean channel with the answer."""
    s = surface()
    s._TRACE_EDIT_EVERY = 0.0
    channel = TraceChannel()
    s.client.get_channel = lambda _id: channel  # type: ignore[method-assign]

    async with s.working("12345"):
        s.progress("▸ calendar op=write")
        await s._edit_task
        s.progress("calendar op=write ✓")
        await s._edit_task

    assert "▸ calendar op=write" in channel.sent[0].content
    assert "calendar op=write ✓" in channel.sent[0].edits[-1]
    assert channel.sent[0].deleted, "the trace is a courtesy, not channel clutter"


async def test_the_trace_is_throttled_under_discords_edit_limit() -> None:
    s = surface()
    channel = TraceChannel()
    s.client.get_channel = lambda _id: channel  # type: ignore[method-assign]

    async with s.working("12345"):
        for step in range(12):
            s.progress(f"tool {step}")
            await asyncio.sleep(0)
        if s._edit_task is not None:
            await s._edit_task

    assert len(channel.sent) == 1, "one message per turn, not one per tool"
    edits = 1 + len(channel.sent[0].edits)
    seconds = 12 / 30  # the burst above, at a realistic tool cadence
    assert edits <= max(1, int(seconds / s._TRACE_EDIT_EVERY) + 2), (
        "edits stay well inside Discord's rate limit"
    )
    assert channel.sent[0].content.count("`·") <= s._TRACE_LINES, "only the recent steps show"


async def test_a_trace_failure_never_reaches_the_turn() -> None:
    s = surface()
    channel = TraceChannel()

    async def broken_send(body: str) -> TraceMessage:
        raise RuntimeError("discord is unhappy")

    channel.send = broken_send  # type: ignore[method-assign]
    s.client.get_channel = lambda _id: channel  # type: ignore[method-assign]

    async with s.working("12345"):
        s.progress("▸ shell")
        if s._edit_task is not None:
            await asyncio.gather(s._edit_task, return_exceptions=True)

    assert s._trace == [], "the failed trace was dropped with the turn, not kept"
    assert s._trace_msg is None


async def test_a_turn_without_a_channel_keeps_no_trace_state() -> None:
    s = surface()
    async with s.working(None):
        s.progress("▸ shell")
        s.progress("shell ✓")

    assert s._trace == [] and s._trace_msg is None


# -- the held ledger: a stopped turn's trace stays for the owner -------------


async def test_a_turn_the_engine_stopped_leaves_its_ledger_behind() -> None:
    """hold_trace() is the engine's mark that it stopped the turn.

    On 18 Oct 22:52 the owner was told "I got stuck…schedule kept coming back
    with add needs cron" after watching a ledger delete itself — the one turn
    whose record he needed, and the one turn that had none. A stopped turn's
    ledger is now the channel's record; the reply summarizes it above.
    """
    s = surface()
    s._TRACE_EDIT_EVERY = 0.0
    channel = TraceChannel()
    s.client.get_channel = lambda _id: channel  # type: ignore[method-assign]

    async with s.working("12345"):
        s.progress("▸ schedule op=add")
        await s._edit_task
        s.progress("schedule op=add ✗ add needs cron — name… cron…")
        s.hold_trace()

    assert channel.sent, "the ledger was never shown"
    sent = channel.sent[0]
    assert not sent.deleted, "a stopped turn's ledger is the channel's record"
    assert "schedule op=add ✗" in sent.content, (
        "the final edit carries the whole ledger, last failing line included"
    )
    assert s._trace == [], "the turn's lines are spent; the message is what remains"
    assert s._hold is False, "the mark is spent with the turn"


async def test_an_answered_turn_still_clears_the_ledger() -> None:
    """The hold is a mark, not a standing behaviour: nothing marks it, nothing keeps."""
    s = surface()
    s._TRACE_EDIT_EVERY = 0.0
    channel = TraceChannel()
    s.client.get_channel = lambda _id: channel  # type: ignore[method-assign]

    async with s.working("12345"):
        s.progress("▸ calendar op=list")
        await s._edit_task

    assert channel.sent[0].deleted, "the trace is a courtesy, not channel clutter"


async def test_a_line_landing_inside_the_edit_interval_survives_the_hold() -> None:
    """The last line before a stopped turn is the one the owner most needs.

    At the default interval (2.5 s) a tool that fails right before the engine
    stops the turn would otherwise vanish — the ordinary flush is still asleep.
    The held path flushes once more as it closes, so the ledger ends complete
    rather than exactly one finding short. (The *deleted* contract is different:
    there the reply already carries the story, and a lost tail line is a lost
    courtesy, not a lost finding.)
    """
    s = surface()
    channel = TraceChannel()
    s.client.get_channel = lambda _id: channel  # type: ignore[method-assign]

    async with s.working("12345"):
        s.progress("▸ memory op=recall")
        await s._edit_task  # early flush; the next line lands inside the interval
        s.progress("memory op=recall ✗ (nothing found)")
        s.hold_trace()

    sent = channel.sent[0]
    assert "memory op=recall ✗" in sent.edits[-1], (
        "the held flush must include the line the throttled edit would have dropped"
    )
