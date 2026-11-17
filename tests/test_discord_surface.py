"""The Discord surface (SPEC §9).

Two properties matter more than they look. **A dropped message is logged**: the
surface first shipped with silent drops, so "the bot ignored me" and "the bot is
dead" were indistinguishable from outside — and the log never carries the
owner's words. **The typing indicator never costs a turn**: it is cosmetic, so a
hiccup raising one must not stop the work.
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


async def _noop() -> None: ...


async def test_a_message_that_is_not_served_is_dropped_and_logged_without_its_words(
    caplog,  # noqa: ANN001
) -> None:
    served: list[object] = []
    s = surface(handler=lambda event: served.append(event) or _noop())

    with caplog.at_level(logging.DEBUG, logger="daedalus.surface.discord"):
        # A stranger: not on the allowlist.
        await s.on_message(StubMessage("111111111111111111", NotAChannel()))
        assert served == [], "a message from a stranger reached the turn engine"
        assert "not on the allowlist" in caplog.text

        # The owner, but not in a DM: DM-first, by design (SPEC §9).
        await s.on_message(StubMessage(OWNER, NotAChannel()))
        assert served == [], "a non-DM message was served"
        assert "DM-first" in caplog.text

        # The log is not a place for the owner's words (AGENTS.md, SPEC §6).
        assert "hello" not in caplog.text


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


async def test_a_typing_indicator_never_costs_a_turn(caplog) -> None:  # noqa: ANN001
    s = surface()

    # Held for the turn.
    record: list[str] = []
    s.client.get_channel = lambda _id: TypingChannel(record)  # type: ignore[method-assign]
    async with s.working("12345"):
        record.append("working")
    assert record == ["enter", "working", "exit"]

    # Released when the turn raises — the indicator is not left hanging.
    record = []
    s.client.get_channel = lambda _id: TypingChannel(record)  # type: ignore[method-assign]
    try:
        async with s.working("12345"):
            raise ValueError("the turn blew up")
    except ValueError:
        pass
    assert record == ["enter", "exit"], "the indicator was left hanging"

    # A typing failure does not kill the turn: cosmetic only, the work still runs.
    record = []
    s.client.get_channel = lambda _id: TypingChannel(record, explode=True)  # type: ignore[method-assign]
    ran = False
    async with s.working("12345"):
        ran = True
    assert ran, "a typing hiccup stopped the turn"

    # Swallowed at debug, "no indicator" looked exactly like a dead daemon.
    with caplog.at_level(logging.WARNING, logger="daedalus.surface.discord"):
        async with s.working("12345"):
            pass
    assert "could not raise the typing indicator" in caplog.text

    # No resolvable channel: a no-op, never a failure.
    async with s.working(None):
        pass
    async with s.working("not-an-id"):
        pass

    # A DM the client's cache never saw is still one this surface can type into:
    # the channel is remembered from the message it arrived on. (The 16:21 turn
    # logged `no typing indicator: channel … is not in the cache` and the owner
    # watched silence for two minutes.)
    opened = TypingChannel([])
    s.client.get_channel = lambda _id: None  # type: ignore[method-assign]
    s._seen[794190518389178378] = opened
    async with s.working("794190518389178378"):
        opened.log.append("working")
    assert opened.log == ["enter", "working", "exit"], "an uncached DM produced no indicator"


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


async def test_the_trace_shows_the_work_while_it_happens_and_then_clears_itself() -> None:
    """The owner watches the work happen, then gets a clean channel with the answer."""
    # Shown, then a courtesy: no channel clutter after the answer.
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

    # One message per turn, edits throttled under Discord's rate limit, recent steps only.
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

    # A turn with no channel keeps no trace state at all.
    s = surface()
    async with s.working(None):
        s.progress("▸ shell")
        s.progress("shell ✓")
    assert s._trace == [] and s._trace_msg is None

    # A trace failure never reaches the turn, and is dropped with it.
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


# -- the held ledger: a stopped turn's trace stays for the owner -------------


async def test_a_stopped_turn_leaves_its_ledger_behind_and_spends_the_hold() -> None:
    """hold_trace() is the engine's mark that it stopped the turn.

    On 18 Oct 22:52 the owner was told "I got stuck…schedule kept coming back
    with add needs cron" after watching a ledger delete itself — the one turn
    whose record he needed, and the one turn that had none.
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

    # The last line before a stopped turn is the one the owner most needs: at the
    # default interval the ordinary flush is still asleep, so the held path
    # flushes once more as it closes.
    s = surface()
    channel = TraceChannel()
    s.client.get_channel = lambda _id: channel  # type: ignore[method-assign]
    async with s.working("12345"):
        s.progress("▸ memory op=recall")
        await s._edit_task  # early flush; the next line lands inside the interval
        s.progress("memory op=recall ✗ (nothing found)")
        s.hold_trace()
    assert "memory op=recall ✗" in channel.sent[0].edits[-1], (
        "the held flush must include the line the throttled edit would have dropped"
    )


# -- streaming: one message, edited while the model writes -------------------


class FakeMessage:
    def __init__(self, content: str, channel: "FakeChannel") -> None:
        self.content = content
        self.channel = channel
        self.edits: list[str] = []
        self.deleted = False

    async def edit(self, content: str) -> "FakeMessage":
        self.content = content
        self.edits.append(content)
        return self

    async def delete(self) -> None:
        self.deleted = True


class FakeChannel:
    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []

    async def send(self, text: str) -> FakeMessage:
        message = FakeMessage(text, self)
        self.messages.append(message)
        return message


async def test_a_streamed_preview_is_either_the_answer_or_removed() -> None:
    """The streamed message *is* the answer: delivery must not repeat it, and a
    preview that is not the answer must not sit beside the real one."""
    from daedalus.surface.discord.bot import Streamer

    # Adopted, not re-sent — and it carries the attribution.
    channel = FakeChannel()
    stream = Streamer(channel)  # type: ignore[arg-type]
    await stream.delta("Hello ")
    await stream.delta("there")
    await asyncio.sleep(0.01)
    assert await stream.finish("Hello there", header="**Melinoë**") is True
    assert len(channel.messages) == 1
    assert channel.messages[0].content == "**Melinoë**\n\nHello there"

    # The harness wrote the final text (an error line); the preview is deleted, not doubled.
    channel = FakeChannel()
    stream = Streamer(channel)  # type: ignore[arg-type]
    await stream.delta("half an ans")
    await asyncio.sleep(0.01)
    assert await stream.finish("I couldn't reach a model — nope") is False
    assert channel.messages[0].deleted
    assert channel.messages[0].content == "half an ans"

    # A restart clears the previous attempt before the next one begins.
    channel = FakeChannel()
    stream = Streamer(channel)  # type: ignore[arg-type]
    await stream.delta("first try")
    await asyncio.sleep(0.01)
    await stream.reset()
    assert channel.messages[0].deleted
    await stream.delta("second try")
    await asyncio.sleep(0.01)
    assert await stream.finish("second try") is True
    assert len(channel.messages) == 2
    assert channel.messages[1].content == "second try"

    # No text means the ordinary path says so — the preview must not.
    channel = FakeChannel()
    stream = Streamer(channel)  # type: ignore[arg-type]
    assert await stream.finish("") is False
    assert channel.messages == []

    # A delta after the end is ignored.
    channel = FakeChannel()
    stream = Streamer(channel)  # type: ignore[arg-type]
    assert await stream.finish("") is False
    await stream.delta("late")
    await asyncio.sleep(0.01)
    assert channel.messages == []

    # A preview is never a reason for a turn to fail.
    assert surface().open_stream("not-a-channel") is None

    # The turn raised: half an answer must not sit beside the error message.
    channel = FakeChannel()
    stream = Streamer(channel)  # type: ignore[arg-type]
    await stream.delta("half an ans")
    await asyncio.sleep(0.01)
    await stream.abandon()
    assert len(channel.messages) == 1
    assert channel.messages[0].deleted
    await stream.delta("late")  # closed: nothing more is shown
    await asyncio.sleep(0.01)
    assert len(channel.messages) == 1

    # `finally` runs on the happy path too, and abandoning there must be a no-op.
    channel = FakeChannel()
    stream = Streamer(channel)  # type: ignore[arg-type]
    await stream.delta("answer")
    await asyncio.sleep(0.01)
    assert await stream.finish("answer") is True
    await stream.abandon()
    assert len(channel.messages) == 1
    assert channel.messages[0].content == "answer"
    assert not channel.messages[0].deleted


async def test_a_send_cancelled_mid_flight_is_still_claimed() -> None:
    """The one way a preview could survive: a send cancelled after Discord made it.

    The edit task is cancelled when the answer ends; if that cancellation lands
    inside `channel.send`, the message exists but its id never reached the
    streamer. The send runs as its own future so `finish` can still claim it.
    """
    from daedalus.surface.discord.bot import Streamer

    class SlowChannel(FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.gate = asyncio.Event()

        async def send(self, text: str) -> FakeMessage:
            self.started.set()
            await self.gate.wait()
            return await super().send(text)

    channel = SlowChannel()
    stream = Streamer(channel)  # type: ignore[arg-type]
    await stream.delta("hello")
    await asyncio.wait_for(channel.started.wait(), timeout=1.0)

    finisher = asyncio.create_task(stream.finish("hello"))
    await asyncio.sleep(0.01)  # the edit task is cancelled while inside send
    channel.gate.set()

    assert await asyncio.wait_for(finisher, timeout=2.0) is True
    assert len(channel.messages) == 1
    assert channel.messages[0].content == "hello"
    assert not channel.messages[0].deleted


async def test_the_plain_path_chunks_long_replies_and_names_the_action_on_buttons() -> None:
    """Delivery is legible: nothing exceeds Discord's limit, and an approval
    button says what the action is rather than making the owner re-read the prompt."""
    from daedalus.surface.base import ApprovalRequest
    from daedalus.surface.discord.approvals import ApprovalView

    body = "\n".join(f"line {i}" for i in range(500))
    chunks = chunk_text(body)
    assert all(len(c) <= 1900 for c in chunks)
    assert "".join(chunks).replace("\n", "") == body.replace("\n", "")

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

    # A click Discord no longer recognises (its token expires in seconds) still
    # counts: the answer is recorded before the rewrite, so an expired question
    # must not turn a successful approval into a traceback.
    class ExpiredResponse:
        async def edit_message(self, **_kwargs: object) -> None:
            import discord

            raise discord.NotFound(_FakeResponse(status=404), "Unknown interaction")

    class ExpiredInteraction:
        user = type("U", (), {"id": OWNER})()
        response = ExpiredResponse()

    late = ApprovalView(request, {OWNER})
    late.result = True
    late._finish()  # the decision is recorded before the message is rewritten
    await late._say(ExpiredInteraction(), "approved")  # the expired edit must not raise
    assert late.future.done() and late.future.result() is True


class _FakeResponse:
    def __init__(self, status: int = 404) -> None:
        self.status = status
        self.reason = "Not Found"


# -- a message with no words is not a turn ---------------------------------


class WordlessDM:
    """A DM channel that passes the surface's DM check and records its replies."""

    id = 424242

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class StubAttachment:
    def __init__(self, filename: str, url: str, content_type: str = "") -> None:
        self.filename = filename
        self.url = url
        self.content_type = content_type


def dm_surface(home: Path, served: list[object]) -> DiscordSurface:
    settings = Settings(
        home=home, env={}, heavy=[], cheap=None, owner_users={OWNER}
    )
    return DiscordSurface(settings, handler=lambda event: served.append(event) or _noop())


async def test_an_unreadable_attachment_is_answered_honestly_and_a_wordless_message_is_dropped(
    monkeypatch, tmp_path: Path, caplog  # noqa: ANN001
) -> None:
    import discord

    from daedalus import inbox

    monkeypatch.setattr(discord, "DMChannel", WordlessDM)

    # A voice note this machine cannot read must not become an empty turn — and
    # the reply names the file rather than saying nothing useful.
    served: list[object] = []
    s = dm_surface(tmp_path, served)

    async def no_network(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("offline")

    monkeypatch.setattr(inbox, "save_url", no_network)
    message = StubMessage(OWNER, WordlessDM())
    message.content = ""
    message.attachments = [StubAttachment("memo.ogg", "https://cdn.test/memo.ogg", "audio/ogg")]
    await s.on_message(message)
    assert served == [], "an attachment that cannot be read must not become an empty turn"
    assert message.channel.sent and "memo.ogg" in message.channel.sent[0]

    # Words plus an unreadable file: the words are answered, and the model is told
    # which file went unread so it does not answer as though it heard the note.
    served = []
    s = dm_surface(tmp_path, served)
    monkeypatch.setattr(inbox.shutil, "which", lambda _name: None)

    async def local_copy(home, url, name, content_type, *, index=0, client=None):  # noqa: ANN001
        return inbox.save_bytes(home, name, b"OggS", content_type, index=index)

    monkeypatch.setattr(inbox, "save_url", local_copy)
    message = StubMessage(OWNER, WordlessDM())
    message.content = "what did I say in this?"
    message.attachments = [StubAttachment("memo.ogg", "https://cdn.test/memo.ogg", "audio/ogg")]
    await s.on_message(message)
    (event,) = served
    assert "what did I say in this?" in event.text
    assert "could not read" in event.text and "memo.ogg" in event.text
    assert "no local audio reader" in event.text

    # A message with no words and no attachment is dropped — and logged, because a
    # drop the owner cannot see is indistinguishable from a dead daemon.
    served = []
    s = surface(handler=lambda event: served.append(event) or _noop())
    message = StubMessage(OWNER, WordlessDM())
    message.content = "   "
    message.attachments = []
    with caplog.at_level(logging.INFO, logger="daedalus.surface.discord"):
        await s.on_message(message)
    assert served == []
    assert message.channel.sent == [], "nothing to apologise for either"
    assert "nothing to answer" in caplog.text


async def test_a_text_attachment_is_read_locally_and_labelled_as_untrusted_data(
    monkeypatch, tmp_path: Path
) -> None:
    """The file's words reach the model, labelled as data — and no upload happens."""
    import discord

    from daedalus import inbox

    served: list[object] = []
    s = dm_surface(tmp_path, served)
    monkeypatch.setattr(discord, "DMChannel", WordlessDM)

    async def local_copy(home, url, name, content_type, *, index=0, client=None):  # noqa: ANN001
        assert url.startswith("https://cdn.test/"), "the only fetch is Discord's own URL"
        return inbox.save_bytes(home, name, b"the numbers are 4 8 15 16 23 42", content_type, index=index)

    monkeypatch.setattr(inbox, "save_url", local_copy)
    message = StubMessage(OWNER, WordlessDM())
    message.content = "what do these mean?"
    message.attachments = [StubAttachment("numbers.txt", "https://cdn.test/numbers.txt", "text/plain")]

    await s.on_message(message)

    (event,) = served
    assert "what do these mean?" in event.text
    assert "4 8 15 16 23 42" in event.text
    assert "untrusted attachment text" in event.text
    assert event.meta["attachments"] == ["numbers.txt"]
    assert message.channel.sent == []
