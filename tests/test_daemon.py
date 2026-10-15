"""The daemon lifecycle (SPEC §8, §9).

The bug this file exists for: `serve()` originally awaited `wait_ready()`
*before* calling `start()`. A surface signals readiness from inside its own
connection loop, so that ordering waits for a connection that never begins —
the process starts, dials nothing, opens no socket, logs nothing, and still
reports `active (running)` to systemd. It shipped that way, and the only
symptom was silence.

So these tests assert the *ordering*, not just the happy path: a fake surface
is only ever ready because its `start()` ran.
"""

from __future__ import annotations

import asyncio

import pytest

from daedalus.main import serve


class StubRuntime:
    """Just enough Runtime for the ticker and the shutdown path."""

    def __init__(self) -> None:
        self.ticks = 0
        self.wake = None
        self.closed = False
        self.scheduler = self

    async def dispatch_tick(self, rt, wake_handler=None) -> None:  # noqa: ANN001
        self.ticks += 1

    async def aclose(self) -> None:
        self.closed = True


class FakeSurface:
    """Readiness is reachable *only* through start(), like a real surface."""

    def __init__(self) -> None:
        self._ready = asyncio.Event()
        self._running = asyncio.Event()
        self.start_called = False
        self.closed = False

    async def start(self) -> None:
        self.start_called = True
        self._ready.set()          # a connection loop would do this on connect
        await self._running.wait()  # ...then serve until told to stop

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def send(self, text: str, *, thread: str | None = None) -> None: ...

    async def ask(self, question: str, *, options: list[str] | None = None) -> str:
        return ""

    async def approve(self, request) -> bool:  # noqa: ANN001
        return False

    def stop(self) -> None:
        self._running.set()


class FailingSurface(FakeSurface):
    """A surface that cannot come up — e.g. a rejected bot token."""

    async def start(self) -> None:
        self.start_called = True
        raise RuntimeError("DISCORD_BOT_TOKEN was rejected")


@pytest.mark.asyncio
async def test_serve_starts_the_surface_it_waits_on() -> None:
    """The deadlock guard: readiness is awaited with start() already running."""
    surface = FakeSurface()
    rt = StubRuntime()

    task = asyncio.create_task(serve(surface, rt, tick_seconds=3600))
    # Yield to the loop so serve() gets as far as it can. If it awaits
    # wait_ready() before starting, start_called is still False here and the
    # assertion below fails -- which is the whole point.
    for _ in range(5):
        await asyncio.sleep(0)

    assert surface.start_called, "serve() waited for readiness before starting"

    surface.stop()  # the bot shuts down
    assert await asyncio.wait_for(task, timeout=2) == 0
    assert rt.closed, "the runtime was not released on shutdown"


@pytest.mark.asyncio
async def test_serve_pumps_ticks_with_no_model_in_the_loop() -> None:
    surface = FakeSurface()
    rt = StubRuntime()

    task = asyncio.create_task(serve(surface, rt, tick_seconds=0.01))
    await asyncio.sleep(0.05)
    surface.stop()
    await asyncio.wait_for(task, timeout=2)

    assert rt.ticks >= 1, "the scheduler ticker never ran"


@pytest.mark.asyncio
async def test_serve_reports_a_surface_that_dies_before_ready() -> None:
    """A bad token must fail loudly, not hang the unit forever."""
    rt = StubRuntime()

    with pytest.raises(RuntimeError, match="rejected"):
        await asyncio.wait_for(serve(FailingSurface(), rt, tick_seconds=3600), timeout=2)

    assert rt.closed, "the runtime leaked when the surface failed to start"
