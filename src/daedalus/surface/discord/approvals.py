"""Approval buttons (SPEC §6, §9 — "buttons only for approvals")."""

from __future__ import annotations

import asyncio
import logging

import discord

from ..base import ApprovalRequest

log = logging.getLogger("daedalus.surface.discord")


class ApprovalView(discord.ui.View):
    """A yes/no for one action, answerable only by the owner."""

    def __init__(self, request: ApprovalRequest, owner_ids: set[str], *, timeout: float = 600) -> None:
        super().__init__(timeout=timeout)
        self.request = request
        self.owner_ids = owner_ids
        self.result: bool | None = None
        self.future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        # Say what the choice is, not just that there is one.
        self.approve.label = request.affirm_label[:80]
        self.deny.label = request.deny_label[:80]

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) in self.owner_ids

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: ARG002
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not yours to approve.", ephemeral=True)
            return
        self.result = True
        self._finish()
        await self._say(interaction, self._resolved_text(True))

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="⛔")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: ARG002
        if not self._is_owner(interaction):
            await interaction.response.send_message("Not yours to deny.", ephemeral=True)
            return
        self.result = False
        self._finish()
        await self._say(interaction, self._resolved_text(False))

    @staticmethod
    async def _say(interaction: discord.Interaction, text: str) -> None:
        """Rewrite the question with the answer, tolerating a late click.

        Discord expires an interaction token after a few seconds. The decision
        above is already recorded by then — the future is resolved before this
        call — so a 404 here means "that message is too old to edit", not "the
        approval was lost". Raising would print a traceback and make a
        successful approval look like a crash.
        """
        try:
            await interaction.response.edit_message(content=text, view=None)
        except discord.NotFound:
            log.info("the approval question was too old to rewrite; the answer still stands")
        except discord.HTTPException as exc:  # noqa: BLE001 — the answer stands regardless
            log.warning("could not rewrite the approval question (%s)", exc)

    def _finish(self) -> None:
        if not self.future.done():
            self.future.set_result(bool(self.result))
        self.stop()

    def _resolved_text(self, approved: bool) -> str:
        mark = "✅ approved" if approved else "⛔ denied — nothing ran"
        return f"{self.request.describe()}\n\n— {mark}"

    async def on_timeout(self) -> None:
        log.info("the approval question expired unanswered — the action was not taken")
        if not self.future.done():
            self.future.set_result(False)
