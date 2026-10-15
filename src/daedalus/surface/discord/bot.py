"""One bot, DM-first, free text (SPEC §9).

Only the allowlisted owner is ever served. A DM from anyone else is dropped
silently — the agent does not explain itself to strangers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

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
        self._ready.set()

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
            return  # single-user: everyone else is invisible
        if not isinstance(message.channel, discord.DMChannel):
            return  # DM-first

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
