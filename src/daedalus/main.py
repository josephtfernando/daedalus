"""``daedalus`` — the command-line entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .config import Settings, load_settings
from .storage import paths

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps imports cheap
    from .loop.engine import TurnResult
    from .runtime import Runtime
    from .surface.base import Surface


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _settings(args: argparse.Namespace) -> Settings:
    home = Path(args.home).expanduser() if getattr(args, "home", None) else paths.home()
    home.mkdir(parents=True, exist_ok=True)
    paths.ensure_tree(home)
    return load_settings(home)


def cmd_version(_: argparse.Namespace) -> int:
    print(f"daedalus {__version__}")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from .selftest import render_json, run

    settings = _settings(args)
    report = run(settings, live=args.live)
    print(render_json(report) if args.json else report.render())
    return 0 if report.ok else 1


def cmd_ask(args: argparse.Namespace) -> int:
    from .loop.engine import Engine
    from .runtime import build_runtime
    from .surface.base import InboundEvent
    from .surface.cli import CliSurface

    settings = _settings(args)
    surface = CliSurface(auto_approve=args.yes)
    rt = build_runtime(settings, surface=surface)

    async def go() -> int:
        engine = Engine(rt)
        event = InboundEvent(text=" ".join(args.text), user_id="owner", is_dm=False, source="cli")
        result = await engine.run_turn(event)
        if result.error and not result.text:
            print(f"[error] {result.error}", file=sys.stderr)
            return 1
        if not result.text:
            print("(no reply)", file=sys.stderr)
        await rt.aclose()
        return 0

    return asyncio.run(go())


def cmd_wake(args: argparse.Namespace) -> int:
    """Ask the proactive path what it would say, without sending it.

    A probe, not a send: it prints the message — or the reason for silence — and
    spends none of the day's quota. The owner can see what a 07:00 wake looks
    like without waiting for cron, and without the act of checking changing the
    thing being checked.
    """
    from .runtime import build_runtime
    from .surface.cli import CliSurface

    settings = _settings(args)
    rt = build_runtime(settings, surface=CliSurface())

    async def go() -> int:
        allowed, why_not = rt.scheduler.proactive.may_speak(settings, datetime.now())
        if not allowed and not args.force:
            print(f"(silent — {why_not})")
            await rt.aclose()
            return 0
        text = await rt.wake(" ".join(args.text))
        print(text or "(nothing worth the interruption)")
        await rt.aclose()
        return 0

    return asyncio.run(go())


def cmd_calendar(args: argparse.Namespace) -> int:
    """Bring an outside calendar in.

    A command rather than a tool: importing reads a file from an arbitrary path,
    which is a wider reach than the ``calendar`` tool is allowed (it is scoped to
    ``state``). The owner runs this; the agent never goes looking for a file to
    swallow.
    """
    from .calendar import CalendarStore
    from .security.provenance import DataClass

    settings = _settings(args)
    store = CalendarStore(settings.home / "calendar")
    for path in args.files:
        try:
            report = store.import_ics(
                path,
                replace=args.replace,
                data_class=DataClass.LOCAL_ONLY if args.local_only else None,
            )
        except ValueError as exc:
            print(f"✗ {exc}")
            return 1
        print(report.render())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Serve the generated artifacts on the loopback (SPEC §3.5, §3.6).

    Loopback only, and it says so: this makes the dashboard reachable *from this
    laptop* and nothing else. Publishing it to the phone is a separate act with
    its own ticket (T2), because "open a port" and "open a port to the internet"
    should never be the same keystroke.
    """
    from .genui.server import LocalServer, control_token
    from .resources.secrets import SecretStore

    settings = _settings(args)
    server = LocalServer(
        settings.home,
        token=control_token(SecretStore(settings.home)),
        port=args.port,
    )
    url = server.start()
    print(f"serving {server.artifacts} at {url}  (loopback only — nothing is published)")
    print("ctrl-c to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("")
    finally:
        server.stop()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .loop.engine import Engine
    from .runtime import build_runtime
    from .surface.base import InboundEvent
    from .surface.cli import CliSurface

    settings = _settings(args)
    if settings.surface_mode != "discord" or args.cli:
        return _run_cli_repl(settings)

    from .surface.discord.bot import DiscordSurface

    if not settings.discord_token:
        print("DISCORD_BOT_TOKEN is not set — run scripts/setup.sh first.", file=sys.stderr)
        return 2

    rt = build_runtime(settings)
    engine = Engine(rt)

    log = logging.getLogger("daedalus.surface.discord")

    async def handler(event: InboundEvent) -> None:
        # The typing indicator is held for the work, not for the send: sending
        # the reply clears it, so it never lingers past the answer.
        async with surface.working(event.channel_id):
            result = await engine.run_turn(event)
        log.info(
            "turn done: %d step(s), tools=%s, provider=%s, %d char(s)%s",
            result.steps,
            ",".join(result.tools_used) or "none",
            result.provider or "none",
            len(result.text),
            f", error={result.error}" if result.error else "",
        )
        if not result.text:
            # Never be silent: a turn with no text is reported, not swallowed.
            log.warning("turn produced no text (%s)", result.error or "the model returned empty content")
        await deliver(rt.surface, result)  # type: ignore[arg-type]

    surface = DiscordSurface(settings, handler)
    rt.set_surface(surface)

    return asyncio.run(serve(surface, rt, args.tick_seconds))


def _run_cli_repl(settings: Settings) -> int:
    from .loop.engine import Engine
    from .runtime import build_runtime
    from .surface.base import InboundEvent
    from .surface.cli import CliSurface

    surface = CliSurface()
    rt = build_runtime(settings, surface=surface)
    engine = Engine(rt)

    async def go() -> int:
        print("daedalus — free text. Ctrl-D to leave.")
        while True:
            try:
                line = await asyncio.to_thread(input, "› ")
            except EOFError:
                break
            if not line.strip():
                continue
            result = await engine.run_turn(
                InboundEvent(text=line, user_id="owner", is_dm=False, source="cli")
            )
            await deliver(surface, result)
        await rt.aclose()
        return 0

    return asyncio.run(go())


NO_OUTPUT = (
    "I worked through that but came back with nothing to send — that's a fault on my side, "
    "not a refusal. Say the word and I'll try again."
)


async def deliver(surface: Surface, result: TurnResult) -> bool:
    """Send the turn's text, or say plainly that there wasn't any.

    Returns True if the no-output fallback was used. Silence is the one outcome
    that must never happen: an unanswered DM and a dead daemon are then
    indistinguishable from the outside, which is exactly how a real request went
    missing without leaving a trace.
    """
    if result.text:
        await surface.send(result.text)
        return False
    await surface.send(NO_OUTPUT)
    return True


async def _ticker(rt: Runtime, interval_s: float) -> None:
    """Pump scheduler ticks in-process, forever (SPEC §8).

    There is deliberately no separate `tick` verb — the daemon pumps its own
    ticks, so the timer unit triggers `daedalus run` rather than a one-shot.
    """
    while True:
        try:
            await rt.scheduler.dispatch_tick(rt, wake_handler=rt.wake)
        except Exception:  # noqa: BLE001 — a bad tick must not kill the daemon
            logging.getLogger("daedalus.scheduler").exception("tick failed")
        await asyncio.sleep(interval_s)


async def serve(surface: Surface, rt: Runtime, tick_seconds: float = 60) -> int:
    """Run the surface and the scheduler ticker together.

    **`start()` must be in flight before `wait_ready()` is awaited.** A surface
    signals readiness from inside its own connection loop, so awaiting
    readiness first waits for a connection that never begins: the daemon comes
    up, dials nothing, opens no socket, logs nothing, and still looks healthy
    to systemd. That is exactly how the Discord surface shipped silent.
    """
    serving = asyncio.create_task(surface.start())
    ready = asyncio.create_task(surface.wait_ready())
    try:
        await asyncio.wait({serving, ready}, return_when=asyncio.FIRST_COMPLETED)
        if serving.done() and not ready.done():
            # start() finished without ever becoming ready: it failed, or the
            # surface closed on its own. Surface the failure instead of hanging.
            exc = serving.exception()
            await rt.aclose()
            if exc is not None:
                raise exc
            return 1
    finally:
        ready.cancel()

    tick = asyncio.create_task(_ticker(rt, tick_seconds))
    try:
        await serving
    finally:
        tick.cancel()
        await rt.aclose()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daedalus", description="Single-user, local-first chief-of-staff agent.")
    parser.add_argument("--home", help="override $DAEDALUS_HOME")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_version = sub.add_parser("version", help="print the version")
    p_version.set_defaults(func=cmd_version)

    p_self = sub.add_parser("selftest", help="verify the install end to end")
    p_self.add_argument("--live", action="store_true", help="also probe the configured model providers")
    p_self.add_argument("--json", action="store_true", help="machine-readable output")
    p_self.set_defaults(func=cmd_selftest)

    p_ask = sub.add_parser("ask", help="send one free-text turn")
    p_ask.add_argument("text", nargs=argparse.REMAINDER)
    p_ask.add_argument("-y", "--yes", action="store_true", help="auto-approve actions")
    p_ask.set_defaults(func=cmd_ask)

    p_cal = sub.add_parser("calendar", help="the internal calendar: bring a file in")
    cal_sub = p_cal.add_subparsers(dest="calendar_action", required=True)
    p_import = cal_sub.add_parser("import", help="adopt the events in an .ics file")
    p_import.add_argument("files", nargs="+", help="one or more .ics files (a gcal export works)")
    p_import.add_argument(
        "--replace",
        action="store_true",
        help="overwrite an event that is already here instead of skipping it",
    )
    p_import.add_argument(
        "--local-only",
        action="store_true",
        help="mark everything imported local_only, so it never reaches a remote model",
    )
    p_import.set_defaults(func=cmd_calendar)

    p_serve = sub.add_parser("serve", help="serve the dashboard on the loopback")
    p_serve.add_argument(
        "--port", type=int, default=8787, help="loopback port (0 picks a free one)"
    )
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser("run", help="start the daemon")
    p_run.add_argument("--cli", action="store_true", help="run in the terminal instead of Discord")
    p_run.add_argument("--tick-seconds", type=int, default=60)
    p_run.set_defaults(func=cmd_run)

    p_wake = sub.add_parser("wake", help="probe a proactive message without sending it")
    p_wake.add_argument("text", nargs=argparse.REMAINDER, help="what brought it here (optional)")
    p_wake.add_argument("--force", action="store_true", help="ignore quiet hours and the daily cap")
    p_wake.set_defaults(func=cmd_wake)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
