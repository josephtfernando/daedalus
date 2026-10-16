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

import discord

from ...config import Settings
from ..base import ApprovalRequest, InboundEvent
from .approvals import ApprovalView

log = logging.getLogger("daedalus.surface.discord")

MAX_DISCORD = 1900


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
        #: The live work trace (see :meth:`progress`): the lines, the message
        #: they are edited into, and the throttling state.
        self._trace: list[str] = []
        self._trace_channel: discord.abc.Messageable | None = None
        self._trace_msg: discord.Message | None = None
        self._edit_task: asyncio.Task | None = None
        self._last_edit = 0.0
        self._dirty = False
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
            return self.client.get_channel(int(channel_id))  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None

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
        if channel is not None:
            try:
                typing = channel.typing()
                await typing.__aenter__()
            except Exception:  # noqa: BLE001 — cosmetic, never fatal
                log.debug("could not raise a typing indicator", exc_info=True)
                typing = None
        self._trace_channel = channel
        try:
            yield
        finally:
            self._trace_channel = None
            await self._end_trace()
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

    async def _end_trace(self) -> None:
        self._trace.clear()
        self._dirty = False
        message, self._trace_msg, self._edit_task = self._trace_msg, None, None
        if message is not None:
            with contextlib.suppress(Exception):
                await message.delete()

    # -- outbound ---------------------------------------------------------

    async def _owner_user(self) -> discord.User:
        for uid in self.owner_ids:
            try:
                return await self.client.fetch_user(int(uid))
            except (discord.NotFound, ValueError):
                continue
        raise RuntimeError("no valid owner id in the allowlist")

    async def send(self, text: str, *, thread: str | None = None) -> None:
        user = await self._owner_user()
        for chunk in chunk_text(text):
            await user.send(chunk)

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
        await user.send(request.describe(), view=view)
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

        # Never log the content: the log is not a place for the owner's words.
        log.info("inbound from the owner (%d characters)", len(message.content or ""))

        # A pending question swallows the next owner message.
        if self._awaiting is not None and not self._awaiting.done():
            self._awaiting.set_result(message.content)
            return

        event = InboundEvent(
            text=message.content,
            user_id=str(message.author.id),
            channel_id=str(message.channel.id),
            is_dm=True,
            source="discord",
        )
        try:
            await self.handler(event)
        except Exception:  # noqa: BLE001 — a bad turn must not kill the bot
            log.exception("turn failed")
            await message.channel.send("Something went wrong handling that — check the logs.")


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
