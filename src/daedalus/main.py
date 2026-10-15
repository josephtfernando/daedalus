"""``daedalus`` — the command-line entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__
from .config import Settings, load_settings
from .storage import paths


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

    async def handler(event: InboundEvent) -> None:
        result = await engine.run_turn(event)
        if result.text:
            await rt.surface.send(result.text)  # type: ignore[union-attr]

    surface = DiscordSurface(settings, handler)
    rt.set_surface(surface)

    async def ticker() -> None:
        while True:
            try:
                await rt.scheduler.dispatch_tick(rt, wake_handler=rt.wake)
            except Exception:  # noqa: BLE001
                logging.getLogger("daedalus.scheduler").exception("tick failed")
            await asyncio.sleep(args.tick_seconds)

    async def go() -> int:
        await surface.wait_ready()
        tick = asyncio.create_task(ticker())
        try:
            await surface.start()
        finally:
            tick.cancel()
            await rt.aclose()
        return 0

    return asyncio.run(go())


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
            if result.text:
                await surface.send(result.text)
        await rt.aclose()
        return 0

    return asyncio.run(go())


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

    p_run = sub.add_parser("run", help="start the daemon")
    p_run.add_argument("--cli", action="store_true", help="run in the terminal instead of Discord")
    p_run.add_argument("--tick-seconds", type=int, default=60)
    p_run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
