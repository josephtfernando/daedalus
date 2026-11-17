"""The daemon lifecycle (SPEC §8, §9).

The bug this file exists for: `serve()` originally awaited `wait_ready()`
*before* calling `start()`. A surface signals readiness from inside its own
connection loop, so that ordering waits for a connection that never begins —
the process starts, dials nothing, opens no socket, and still reports
`active (running)` to systemd. It shipped that way, and the only symptom was
silence. So these tests assert the *ordering*, not just the happy path.
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


async def test_serve_starts_the_surface_it_waits_on_and_releases_it_on_the_way_out() -> None:
    # The deadlock guard: readiness is awaited with start() already running.
    surface = FakeSurface()
    rt = StubRuntime()
    task = asyncio.create_task(serve(surface, rt, tick_seconds=0.01))
    # Yield to the loop so serve() gets as far as it can. If it awaits
    # wait_ready() before starting, start_called is still False here and the
    # assertion below fails -- which is the whole point.
    for _ in range(5):
        await asyncio.sleep(0)

    assert surface.start_called, "serve() waited for readiness before starting"

    # And it pumps ticks with no model in the loop.
    await asyncio.sleep(0.05)
    assert rt.ticks >= 1, "the scheduler ticker never ran"

    surface.stop()  # the bot shuts down
    assert await asyncio.wait_for(task, timeout=2) == 0
    assert rt.closed, "the runtime was not released on shutdown"

    # A bad token must fail loudly, not hang the unit forever — and not leak the runtime.
    rt = StubRuntime()
    with pytest.raises(RuntimeError, match="rejected"):
        await asyncio.wait_for(serve(FailingSurface(), rt, tick_seconds=3600), timeout=2)
    assert rt.closed, "the runtime leaked when the surface failed to start"


def test_the_ticker_aims_at_the_clock_and_an_overrun_is_caught_up_not_deferred() -> None:
    """Sleeping after the tick makes the period tick + interval, and it drifts.

    A job pinned to one minute — `0 8,20 * * *` — is due only while
    `datetime.now().minute` is exactly 0. Drift that crosses a minute boundary
    skips it silently, which is exactly the shape of the 07:00 job that never
    ran. The wait is computed against the clock, so each tick lands at the same
    offset past the minute however long the tick itself took.
    """
    from daedalus.main import _seconds_to_next_tick

    interval, tick_cost, now = 60.0, 0.4, 0.0
    for _ in range(5000):  # ~3.5 days of minutes
        now += tick_cost
        now += _seconds_to_next_tick(now, interval)
        assert now % interval <= tick_cost + 1e-9, "the tick drifted off its minute"

    # 12:00:00 exactly: the next boundary is a whole interval away, not zero.
    assert _seconds_to_next_tick(7200.0, 60.0) == pytest.approx(60.0)
    # 12:00:30, mid-minute: the next boundary is half a minute away.
    assert _seconds_to_next_tick(7230.0, 60.0) == pytest.approx(30.0)
    # A tick that overran into the next minute still aims at the boundary after.
    assert _seconds_to_next_tick(7200.5, 60.0) == pytest.approx(59.5)


def test_the_agent_stands_in_its_state_home_not_the_code_repo(
    settings, tmp_path, monkeypatch
) -> None:
    """A relative path the model invents is an agent artefact, not source.

    The daemon used to run from the code repo (systemd's WorkingDirectory, or
    wherever the owner started it), so a write to `notes.md` landed beside
    `src/` and showed up in `git status`. The state home is the agent's own
    neighbourhood; the repo stays reachable by its absolute root.
    """
    from pathlib import Path

    from daedalus.main import enter_state_home
    from daedalus.tools import guard

    monkeypatch.chdir(tmp_path)  # the directory the owner launched it from

    enter_state_home(settings)

    assert Path.cwd() == settings.home
    invented = guard.resolve("notes.md", settings)
    assert invented.parent == settings.home, "an invented relative path escaped into the repo"
    # the code repo is still explicitly reachable — it is just not the default
    assert guard.resolve(str(guard.paths.repo_root() / "AGENTS.md"), settings).is_file()
