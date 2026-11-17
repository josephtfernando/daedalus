"""One bot, DM-first, free text (SPEC §9).

Only the allowlisted owner is ever served. A DM from anyone else is dropped
without a reply — the agent does not explain itself to strangers. But a drop is
never *silent in the log*: an ignored message and a dead daemon must not look
the same from the outside, which is exactly how the surface first shipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import discord

from ... import inbox
from ...config import Settings
from ..base import ApprovalRequest, InboundEvent
from .approvals import ApprovalView
from .render import discord_markdown

log = logging.getLogger("daedalus.surface.discord")

MAX_DISCORD = 1900

#: What the owner gets when a message carries no readable words at all. Silence
#: is worse than an honest "not yet" — it looks like a dead bot — and a turn on
#: an empty message has the model answer nothing in particular.
NO_WORDS = (
    "I couldn't read that attachment, and there's no text in the message. Text "
    "reads directly, and images, audio and PDFs need a local reader installed."
)


Handler = Callable[[InboundEvent], Awaitable[None]]


class DiscordSurface:
    def __init__(self, settings: Settings, handler: Handler) -> None:
        self.settings = settings
        self.handler = handler
        self.owner_ids = set(settings.owner_users)
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        self.client = discord.Client(intents=intents)
        self._awaiting: asyncio.Future[str] | None = None
        self._ready = asyncio.Event()
        #: Room lines this surface has posted (T33): the Discord message id of
        #: each room line, mapped to the room it belongs to. A DM cannot host
        #: real threads, so the room's *projection* is its labeled lines and the
        #: owner's reply to one — a Discord reply carries a reference to the
        #: original — is routed back into the room. The map is deliberately
        #: in-memory: the broker is the durable record; a restart loses nothing
        #: that op=thread cannot read back.
        self._room_lines: dict[str, str] = {}
        #: DM channels this surface has actually received from, keyed by id. The
        #: client's cache is not a reliable source for a DM that was opened after
        #: `on_ready`, and an indicator that cannot find its channel is a turn
        #: that looks dead.
        self._seen: dict[int, discord.abc.Messageable] = {}
        #: The live work trace (see :meth:`progress`): the lines, the message
        #: they are edited into, and the throttling state.
        self._trace: list[str] = []
        self._trace_channel: discord.abc.Messageable | None = None
        self._trace_msg: discord.Message | None = None
        self._edit_task: asyncio.Task | None = None
        self._last_edit = 0.0
        self._dirty = False
        #: Whether the trace of the current turn survives the turn — set by the
        #: engine when the turn stopped early (T42's 22:52 turn), unset again
        #: when the trace's fate is decided.
        self._hold = False
        self.client.event(self.on_ready)
        self.client.event(self.on_message)

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        token = self.settings.discord_token
        if not token:
            raise RuntimeError("DISCORD_BOT_TOKEN is not set")
        await self.client.start(token)

    async def close(self) -> None:
        if not self.client.is_closed():
            await self.client.close()

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def on_ready(self) -> None:
        log.info("discord ready as %s", self.client.user)
        # A wrong or un-resolvable allowlist means no DM is ever served, and the
        # symptom is pure silence. Say so at startup, loudly, rather than never.
        for uid in sorted(self.owner_ids):
            try:
                user = await self.client.fetch_user(int(uid))
            except ValueError:
                log.error("allowlist entry %r is not a Discord user id", uid)
                continue
            except discord.NotFound:
                log.error("allowlisted user %s does not exist — no DM will be served", uid)
                continue
            except discord.HTTPException:
                log.warning("could not resolve allowlisted user %s", uid)
                continue
            log.info("serving the owner as %s (%s), DM-first", user, uid)
        if not self.owner_ids:
            log.error("the allowlist is empty — every message will be dropped")
        self._ready.set()

    def _channel(self, channel_id: str | int | None) -> discord.abc.Messageable | None:
        if channel_id in (None, ""):
            return None
        try:
            key = int(channel_id)
        except (TypeError, ValueError):
            return None
        # A channel this surface has already received a message on is one it can
        # type into, whether or not discord.py's cache holds it. DMs are
        # sometimes absent from that cache (it is populated on ready, and a DM
        # opened later is not) — and "no typing indicator" is then a warning
        # about our own bookkeeping rather than anything the owner can see.
        seen = self._seen.get(key)
        if seen is not None:
            return seen
        return self.client.get_channel(key)  # type: ignore[return-value]

    @asynccontextmanager
    async def working(self, channel_id: str | int | None = None) -> AsyncIterator[None]:
        """Hold a typing indicator open for the duration of a turn.

        A turn can take a minute or more — a cold model, tool calls, an approval
        round-trip. Without this the owner watches a dead chat with no way to
        tell "thinking" from "broken", which is the failure we just spent an
        evening chasing. A typing hiccup must never kill the turn, hence the
        guarded enter/exit.

        Alongside the typing indicator, :meth:`progress` maintains one live
        status message — the work trace — so the owner can *see* the tool calls
        as they happen rather than watching silence. It is deleted when the turn
        ends: the answer replaces it, and a channel full of finished traces is
        noise.
        """
        channel = self._channel(channel_id)
        typing = None
        if channel is None:
            # A DM whose channel is not in the cache means the owner sees no
            # indicator at all. That used to be a debug line, so "no typing" was
            # indistinguishable from "no bot" — the exact confusion this
            # indicator exists to remove.
            log.warning(
                "no typing indicator: channel %r is not in the cache", channel_id
            )
        else:
            try:
                typing = channel.typing()
                await typing.__aenter__()
            except Exception:  # noqa: BLE001 — cosmetic, never fatal
                # Warning, not debug: a swallowed failure here looks exactly like
                # a dead daemon to the person waiting.
                log.warning("could not raise the typing indicator", exc_info=True)
                typing = None
        self._trace_channel = channel
        try:
            yield
        finally:
            await self._end_trace()
            self._trace_channel = None
            if typing is not None:
                with contextlib.suppress(Exception):
                    await typing.__aexit__(None, None, None)

    # -- the work trace -----------------------------------------------------

    #: How the work is shown while it happens (SPEC §9 — the owner is entitled
    #: to see what is being done in their name). One message per turn, edited in
    #: place; the lines are the same safe trace journald keeps: tool, op,
    #: outcome. Never the owner's words, never tool arguments.
    _TRACE_LINES = 9
    _TRACE_EDIT_EVERY = 2.5  # seconds — well under Discord's edit rate limit
    _TRACE_MAX = 1800  # characters — under the 2000-char message limit

    def progress(self, text: str) -> None:
        """Add one line to the live work trace. Fire-and-forget; never fatal.

        Edits are throttled, and a line that lands between two edits is **not**
        dropped: a dirty flag makes the finished edit re-schedule itself, so the
        trace always catches up with reality.
        """
        self._trace.append(str(text))
        if self._trace_channel is None:
            return
        self._dirty = True
        if self._edit_task is None or self._edit_task.done():
            delay = max(0.0, self._TRACE_EDIT_EVERY - (time.monotonic() - self._last_edit))
            self._edit_task = asyncio.create_task(self._push_trace(delay))

    async def _push_trace(self, delay: float = 0.0) -> None:
        if delay:
            await asyncio.sleep(delay)
        if self._trace_channel is None:
            return  # the turn ended while this edit waited its turn
        self._dirty = False
        shown = self._trace[-self._TRACE_LINES :]
        body = "working…" + "\n".join(f"\n`· {line}`" for line in shown)
        try:
            if self._trace_msg is None:
                self._trace_msg = await self._trace_channel.send(body[: self._TRACE_MAX])
            else:
                self._trace_msg = await self._trace_msg.edit(content=body[: self._TRACE_MAX])
            self._last_edit = time.monotonic()
        except Exception:  # noqa: BLE001 — a trace is a courtesy, never fatal
            log.debug("could not update the work trace", exc_info=True)
        finally:
            if self._dirty and self._trace_channel is not None:
                self._edit_task = asyncio.create_task(self._push_trace())

    def hold_trace(self) -> None:
        """Keep the current turn's ledger when the turn ends.

        The engine asks for this when it stopped the turn itself — a repeated
        failure, the step limit — because that is the one ledger the owner needs
        whole: the reply summarizes one failing call, the trace shows all of
        them (the 22:52 turn pasted "I got stuck…" with nothing left to scroll
        but a deleted trace). A turn that answered keeps the clean-channel
        contract: the ledger is deleted, the reply stands alone.
        """
        self._hold = True

    async def _end_trace(self) -> None:
        """Decide the ledger's fate: deleted when the turn answered, held when
        the engine stopped the turn.

        The held path takes one final edit first, so the ledger ends complete —
        a line that landed inside the edit interval is the one line the owner
        most needs to read, and dropping it because `working()` closed would be
        the ordinary flush losing exactly the evidence this exists to keep.
        """
        hold = self._hold
        self._hold = False
        if hold and self._trace_msg is not None:
            shown = self._trace[-self._TRACE_LINES :]
            self._trace.clear()
            self._dirty = False
            pending, self._edit_task = self._edit_task, None
            if pending is not None and not pending.done():
                pending.cancel()
            body = "working…" + "\n".join(f"\n`· {line}`" for line in shown)
            try:
                self._trace_msg = await self._trace_msg.edit(content=body[: self._TRACE_MAX])
            except Exception:  # noqa: BLE001 — cosmetic, never fatal
                log.debug("could not leave the work trace in place", exc_info=True)
            self._trace_msg = None
            return
        self._trace.clear()
        self._dirty = False
        message, self._trace_msg, self._edit_task = self._trace_msg, None, None
        if message is not None:
            with contextlib.suppress(Exception):
                await message.delete()
        self._dirty = False
        message, self._trace_msg, self._edit_task = self._trace_msg, None, None
        if message is not None:
            with contextlib.suppress(Exception):
                await message.delete()

    # -- outbound ---------------------------------------------------------

    def open_stream(self, channel_id: str | int | None) -> "Streamer | None":
        """A live preview of the answer, or ``None`` when there is nowhere to put it.

        The engine streams deltas only when a surface asks for them; this is
        that ask. ``None`` on an unresolvable channel is deliberate: a turn must
        never fail because its preview could not be opened, and the caller falls
        back to ordinary delivery.
        """
        channel = self._channel(channel_id)
        return Streamer(channel) if channel is not None else None

    async def _owner_user(self) -> discord.User:
        for uid in self.owner_ids:
            try:
                return await self.client.fetch_user(int(uid))
            except (discord.NotFound, ValueError):
                continue
        raise RuntimeError("no valid owner id in the allowlist")

    async def send(self, text: str, *, thread: str | None = None) -> None:
        user = await self._owner_user()
        # Discord has no GFM tables. Render them before chunking, so a table is
        # converted as a whole rather than cut in half at the message limit.
        for i, chunk in enumerate(chunk_text(discord_markdown(text))):
            posted = await user.send(chunk)
            # T33: the first chunk of a room line is the one a reply resolves.
            if thread and i == 0:
                self._room_lines[str(posted.id)] = thread

    async def ask(self, question: str, *, options: list[str] | None = None) -> str:
        user = await self._owner_user()
        loop = asyncio.get_event_loop()
        self._awaiting = loop.create_future()
        body = question
        if options:
            body += "\n" + "\n".join(f"• {o}" for o in options)
        await user.send(body)
        try:
            return await asyncio.wait_for(self._awaiting, timeout=1800)
        finally:
            self._awaiting = None

    async def approve(self, request: ApprovalRequest) -> bool:
        user = await self._owner_user()
        view = ApprovalView(request, self.owner_ids)
        # Say how long the question waits. A prompt that quietly expires is a
        # prompt that reads as a denial nobody made — the owner clicks, nothing
        # runs, and the turn says "denied".
        minutes = int(view.timeout // 60) if view.timeout else 0
        body = request.describe()
        if minutes:
            body += f"\n\n_This question waits {minutes} minutes, then the action is not taken._"
        await user.send(body, view=view)
        return await view.future

    # -- inbound ----------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if str(message.author.id) not in self.owner_ids:
            log.info("dropped a message from %s — not on the allowlist", message.author.id)
            return  # single-user: everyone else is invisible
        if not isinstance(message.channel, discord.DMChannel):
            log.info(
                "dropped a message from the owner in %s %s — the surface is DM-first (SPEC §9)",
                type(message.channel).__name__,
                message.channel.id,
            )
            return

        # A message with no words is not a turn *unless* it carries a file whose
        # words can be read on this machine. Attachments used to be invisible:
        # a voice note ran the loop on an empty user message, and the model
        # answered the void. They are read here, into the turn's text.
        self._seen[message.channel.id] = message.channel
        words = (message.content or "").strip()
        attachments = list(getattr(message, "attachments", None) or [])
        names: list[str] = []
        if attachments:
            words, names, unreadable = await self._read_attachments(attachments, words)
            if not words:
                # Say exactly what is missing — a missing reader, a failed
                # download — instead of asking the model to answer nothing.
                await message.channel.send("\n".join(unreadable) or NO_WORDS)
                return
        if not words:
            log.info("an empty message from the owner — nothing to answer")
            return

        # Never log the content: the log is not a place for the owner's words.
        log.info(
            "inbound from the owner (%d characters, %d attachment(s))",
            len(words),
            len(names),
        )

        # A pending question swallows the next owner message.
        if self._awaiting is not None and not self._awaiting.done():
            self._awaiting.set_result(message.content)
            return

        # T33: a *reply* to a room line is the owner joining that room. The
        # reference is resolved against the lines this surface posted, so the
        # room the owner answered is the room the turn is told about — and
        # nothing else about the reply is special: it is an ordinary turn whose
        # event carries the room's id.
        room_id = self._room_of_reply(message)
        if room_id:
            log.info("the owner replied into room %s", room_id)

        event = InboundEvent(
            text=words,
            user_id=str(message.author.id),
            channel_id=str(message.channel.id),
            thread_id=room_id,
            is_dm=True,
            source="discord",
            meta={"attachments": names} if names else {},
        )
        try:
            await self.handler(event)
        except Exception:  # noqa: BLE001 — a bad turn must not kill the bot
            log.exception("turn failed")
            await message.channel.send("Something went wrong handling that — check the logs.")

    async def _read_attachments(
        self, attachments: list[Any], words: str
    ) -> tuple[str, list[str], list[str]]:
        """Download and locally read the owner's attachments into the turn's text.

        Returns ``(text, saved names, unreadable notes)``. The **text** is what
        the model is asked to answer — the owner's own words plus one labelled
        block per file. A file that cannot be read still gets a line, so the
        model can say why rather than pretending it saw the picture. Nothing is
        uploaded: the reader is a local subprocess (`inbox.extract`).
        """
        blocks: list[str] = []
        names: list[str] = []
        notes: list[str] = []
        for index, item in enumerate(attachments[: inbox.MAX_ATTACHMENTS]):
            filename = str(getattr(item, "filename", "") or f"attachment{index + 1}")
            content_type = str(getattr(item, "content_type", "") or "")
            try:
                saved = await inbox.save_url(
                    self.settings.home,
                    str(getattr(item, "url", "") or ""),
                    filename,
                    content_type,
                    index=index,
                )
            except inbox.TooBig:
                notes.append(
                    f"{filename}: too large — I keep attachments up to "
                    f"{inbox.MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB"
                )
                continue
            except Exception as exc:  # noqa: BLE001 — a bad file must not cost the message
                log.warning("could not save an attachment (%s)", type(exc).__name__)
                notes.append(f"{filename}: the download failed ({type(exc).__name__})")
                continue
            extraction = await inbox.extract_async(
                saved.path, saved.content_type, settings_raw=self.settings.raw
            )
            blocks.append(inbox.describe(saved, extraction))
            if extraction.ok:
                names.append(saved.name)
                log.info(
                    "read attachment %s with %s (%d characters)",
                    saved.name,
                    extraction.tool or extraction.kind,
                    len(extraction.text),
                )
            else:
                notes.append(f"{saved.name}: {extraction.note}")
                log.info("could not read attachment %s: %s", saved.name, extraction.note)
        if len(attachments) > inbox.MAX_ATTACHMENTS:
            notes.append(
                f"({len(attachments) - inbox.MAX_ATTACHMENTS} more attachment(s) were not opened)"
            )
        if not blocks and not notes:
            return words, names, []
        text = "\n\n".join([part for part in [words, *blocks] if part])
        if notes and text:
            # The owner wrote something *and* attached a file this machine could
            # not read. The turn goes ahead on their words, but the model is told
            # which file went unread — otherwise it answers as though it saw it.
            text += "\n\n[attachments I could not read]\n" + "\n".join(notes)
        return text, names, notes

    def _room_of_reply(self, message: "discord.Message") -> str | None:
        """The room a reply answers, if the replied-to line was a room's.

        A pure resolution over the map — kept separate so the rule is testable
        without a live client: a reply whose reference points at a room line
        joins that room; anything else is an ordinary DM.
        """
        reference = getattr(message, "reference", None)
        if reference is None:
            return None
        replied_to = getattr(reference, "message_id", None)
        return self._room_lines.get(str(replied_to)) if replied_to is not None else None


class Streamer:
    """The answer as it arrives, edited into one message.

    Discord has no token stream, so the honest approximation is one message
    that is edited while the model writes. Three rules keep it safe:

    * **It is a preview.** The engine's final text is authoritative. If the
      preview and the final text disagree — an error reply, a step-limit note,
      anything the *harness* wrote rather than the model — the preview is
      deleted and ordinary delivery runs, so nothing is ever said twice.
    * **A restart clears it.** A tool step that has been superseded, or a
      provider that died after emitting, starts the next attempt from an empty
      message instead of appending to half an answer. No *retry* happens after
      output began (`ModelClient._call` refuses), so this only ever fires when
      the whole answer is being started over.
    * **A hiccup is cosmetic.** Editing is throttled, guarded and never raises:
      a rate-limited edit must not cost the turn.

    Streaming is a *perceived* latency win, not a real one — the same tokens,
    arriving while the model is still writing instead of after.
    """

    _EDIT_EVERY = 1.0  # seconds — well inside Discord's edit rate limit
    _MAX = MAX_DISCORD - 100  # leave room for the final edit's exact text

    def __init__(self, channel: discord.abc.Messageable) -> None:
        self.channel = channel
        self._text = ""
        self._shown = ""
        self._message: discord.Message | None = None
        #: The in-flight creation of the preview. Kept as a future rather than
        #: inferred from `_message` so that a cancelled edit task cannot lose a
        #: message that Discord has already created — the one way a preview
        #: could survive as a second copy of the answer.
        self._creating: asyncio.Future | None = None
        self._task: asyncio.Task | None = None
        self._last = 0.0
        self._closed = False

    async def delta(self, piece: str) -> None:
        if self._closed:
            return
        self._text += piece
        self._schedule()

    async def reset(self) -> None:
        """Throw the preview away and start the next attempt from empty."""
        if self._closed:
            return
        self._text = ""
        self._shown = ""
        self._last = 0.0
        await self._settle()
        await self._drop()

    async def finish(self, text: str, *, header: str = "") -> bool:
        """Adopt the preview as the answer, or clear it and decline.

        Returns True when the message already on screen *is* the answer, so the
        caller can skip the normal send. False means "nothing was delivered" —
        the preview was not the answer, or there was no text at all.
        """
        self._closed = True
        await self._settle()
        streamed = self._text.strip()
        if not text or self._message is None or streamed != text.strip():
            await self._drop()
            return False
        body = f"{header}\n\n{text}" if header else text
        # The equality check above uses the raw text: the preview must match
        # what the model wrote, not what we are about to render. What lands on
        # screen is the rendered form — the preview itself stays raw while it
        # is being written, because a half-arrived table is not a table.
        chunks = chunk_text(discord_markdown(body))
        try:
            await self._message.edit(content=chunks[0])
        except Exception:  # noqa: BLE001 — fall back to an ordinary send
            log.debug("could not adopt the streamed message", exc_info=True)
            await self._drop()
            return False
        for chunk in chunks[1:]:
            try:
                await self.channel.send(chunk)
            except Exception:  # noqa: BLE001 — the first chunk is already the answer
                log.warning("could not send the rest of a long streamed answer", exc_info=True)
                break
        self._message = None
        self._creating = None
        self._text = ""
        return True

    async def abandon(self) -> None:
        """Discard the preview without adopting anything.

        ``finish`` is the normal way out, and the only way out that can keep the
        message. This is the safety net for the turn that *raised*: a half-written
        preview must not outlive it, or the owner sees half an answer and an
        apology beside it. Idempotent, so the normal path can call it from a
        ``finally`` without checking whether ``finish`` already ran.
        """
        self._closed = True
        await self._settle()
        await self._drop()

    # -- internals ---------------------------------------------------------

    def _schedule(self) -> None:
        if self._task is None or self._task.done():
            delay = max(0.0, self._EDIT_EVERY - (time.monotonic() - self._last))
            self._task = asyncio.create_task(self._push(delay))

    async def _settle(self) -> None:
        """Stop editing, and learn the id of a message whose creation was in flight.

        Cancelling the edit task is not enough: the cancellation can land while
        ``channel.send`` is in flight, after Discord has created the message but
        before its id was assigned. The send therefore runs as its own future;
        this awaits that future, so a preview is never left behind unseen.
        """
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._message is None and self._creating is not None:
            with contextlib.suppress(Exception):
                self._message = await asyncio.wait_for(
                    asyncio.shield(self._creating), timeout=5.0
                )

    async def _send_once(self, body: str) -> discord.Message:
        """Create the preview message at most once, even across a cancellation."""
        if self._creating is None:
            self._creating = asyncio.ensure_future(self.channel.send(body))
        try:
            return await asyncio.shield(self._creating)
        except Exception:
            # A failed send is retryable on the next edit; a *cancelled* waiter
            # is not a failed send, and the future is left for `_settle`.
            self._creating = None
            raise

    async def _drop(self) -> None:
        message, self._message = self._message, None
        self._creating = None
        if message is not None:
            with contextlib.suppress(Exception):
                await message.delete()

    async def _push(self, delay: float = 0.0) -> None:
        if delay:
            await asyncio.sleep(delay)
        while not self._closed and self._shown != self._text:
            body = self._text[: self._MAX]
            try:
                if self._message is None:
                    self._message = await self._send_once(body)
                else:
                    await self._message.edit(content=body)
                self._shown = self._text
                self._last = time.monotonic()
            except Exception:  # noqa: BLE001 — cosmetic, never fatal
                log.debug("could not update the streamed answer", exc_info=True)
                return
            if self._shown != self._text:
                await asyncio.sleep(self._EDIT_EVERY)


def chunk_text(text: str, limit: int = MAX_DISCORD) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split = remaining.rfind("\n", 0, limit)
        if split <= 0:
            split = limit
        chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
