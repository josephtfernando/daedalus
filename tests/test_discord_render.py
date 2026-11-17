"""Discord cannot render Markdown tables; the surface must (SPEC §4).

The model emits tables because tables are a good way to lay out a workout
plan. Discord shows the pipes. The fix belongs in the surface, where the text
is known to be final — not in a prompt rule the model may forget, and not in a
streamed delta where a half-arrived table is not yet a table.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from daedalus.config import Settings
from daedalus.surface.discord.render import discord_markdown

TABLE = """Here is the plan.

| Workout | Duration | Notes |
|---|---|---|
| Run | 30-45 min | Easy pace |
| Circuit | 15 min | Two rounds |

Rest on day two."""


def test_a_real_table_becomes_one_monospace_block() -> None:
    """Columns stay aligned, content is preserved, the raw pipes are gone."""
    out = discord_markdown(TABLE)
    assert out.startswith("Here is the plan.\n\n```\n")
    assert "```" in out[5:]
    assert "| Workout | Duration  | Notes      |" in out
    assert "| Run     | 30-45 min | Easy pace  |" in out
    assert "|---|---|---|" not in out  # the raw separator is not sent
    assert out.endswith("\n\nRest on day two.")


def test_rendering_is_idempotent() -> None:
    """A rendered table is inside a fence, so a second pass must not touch it."""
    once = discord_markdown(TABLE)
    assert discord_markdown(once) == once


def test_prose_and_partial_tables_pass_straight_through() -> None:
    """A pipe in prose, or a table still being streamed, is not a table."""
    for text in (
        "Use a | to separate the columns.",
        "| Workout | Duration |\n|---|---|\n",  # no body rows yet
        "| Workout | Duration |\n|---|\n| Run | 30 min |",  # mismatched widths
    ):
        assert discord_markdown(text) == text


def test_a_table_already_in_a_code_block_is_left_alone() -> None:
    """Nesting fences would break the code block the model chose deliberately."""
    text = "```markdown\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
    assert discord_markdown(text) == text


async def test_send_renders_the_table_before_chunking(monkeypatch) -> None:  # noqa: ANN001
    """The ordinary delivery path — a turn with no stream — is covered too."""
    from daedalus.surface.discord.bot import DiscordSurface

    sent: list[str] = []

    async def send(text: str) -> SimpleNamespace:
        sent.append(text)
        return SimpleNamespace(id=1)

    settings = Settings(home=Path("/tmp"), env={}, heavy=[], cheap=None, owner_users={"1"})
    surface = DiscordSurface(settings, handler=lambda event: None)  # type: ignore[arg-type]
    monkeypatch.setattr(surface, "_owner_user", lambda: asyncio.sleep(0, result=SimpleNamespace(send=send)))

    await surface.send(TABLE)
    assert len(sent) == 1
    assert "```" in sent[0]
    assert "|---|---|---|" not in sent[0]


async def test_streamed_answer_is_rendered_after_the_raw_match() -> None:
    """The preview matches raw model text; the message on screen is rendered."""
    from daedalus.surface.discord.bot import Streamer

    class Message:
        def __init__(self, content: str) -> None:
            self.content = content

        async def edit(self, content: str) -> "Message":
            self.content = content
            return self

        async def delete(self) -> None:
            pass

    class Channel:
        def __init__(self) -> None:
            self.messages: list[Message] = []

        async def send(self, text: str) -> Message:
            message = Message(text)
            self.messages.append(message)
            return message

    channel = Channel()
    stream = Streamer(channel)  # type: ignore[arg-type]
    await stream.delta(TABLE)
    await asyncio.sleep(0.01)
    assert await stream.finish(TABLE) is True
    assert len(channel.messages) == 1
    assert "```" in channel.messages[0].content
    assert "|---|---|---|" not in channel.messages[0].content
