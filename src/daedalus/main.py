"""``daedalus`` — the command-line entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .config import Settings, load_settings
from .storage import paths

log = logging.getLogger("daedalus.main")

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps imports cheap
    from .cabinet.triage import Decision
    from .loop.engine import TurnResult
    from .runtime import Runtime
    from .surface.base import InboundEvent, Surface


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
        # Routed like any other owner message: the seat that takes it answers as
        # itself, and this used to print nothing at all when it did.
        decision = await route(rt, event)
        result = await engine.run_turn(event, seat=decision.seat.key)
        await deliver(surface, result, header=attribution(rt, decision))
        if result.error and not result.text:
            print(f"[error] {result.error}", file=sys.stderr)
            await rt.aclose()
            return 1
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


def cmd_seats(args: argparse.Namespace) -> int:
    """Show the seats and where their charters are (SPEC §7).

    One of the eight is the identity a turn runs as; the rest exist so the owner
    can say "put on the research hat" and get a different charter over the same
    memory and the same tools.
    """
    from .cabinet import charter_dir, load_charters

    settings = _settings(args)
    try:
        charters = load_charters(settings.home)
    except ValueError as exc:
        print(f"✗ {exc}")
        return 1
    width = max(len(key) for key in charters)
    for key, seat in charters.items():
        mark = "*" if seat.active else " "
        print(f"{mark} {key.ljust(width)}  {seat.name}")
        print(f"  {' ' * width}  {seat.charter}")
    print(f"\n* active. Edit them in {charter_dir(settings.home)}")
    print("Switch the one you talk to with `daedalus activate <seat>`.")
    return 0


def cmd_activate(args: argparse.Namespace) -> int:
    """Make one seat the active one, and no other (SPEC §7, ticket T13).

    The active flag lives in the charter files, so "which seat" is a state change
    to the files rather than a second source of truth. This writes *every* file
    whose flag changes, because activating one seat means deactivating another —
    and two charters claiming the owner is refused at the next startup.
    """
    from .cabinet import UnknownSeat, set_active

    settings = _settings(args)
    try:
        seat = set_active(settings.home, args.name)
    except UnknownSeat:
        print(f"✗ no seat called {args.name!r} — see `daedalus seats`")
        return 1
    except ValueError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"* {seat.key} ({seat.name}) is the active seat now.")
    print(f"  {seat.charter}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Serve the generated artifacts on the loopback (SPEC §3.5, §3.6).

    Loopback only, and it says so: this makes the dashboard reachable *from this
    laptop* and nothing else. Publishing it to the phone is a separate act with
    its own ticket (T2), because "open a port" and "open a port to the internet"
    should never be the same keystroke.
    """
    from .calendar.store import CalendarStore
    from .genui.dashboard import VIEWS, calendar_page, write_page
    from .genui.server import LocalServer, control_token
    from .genui.tuners import Tuners
    from .resources.secrets import SecretStore

    settings = _settings(args)
    calendar = CalendarStore(settings.home / "calendar")
    server = LocalServer(
        settings.home,
        token=control_token(SecretStore(settings.home)),
        port=args.port,
        calendar=calendar,
        tuners=Tuners(settings.home),
    )
    # Today's view is drawn once at start so that `/` opens on the calendar rather
    # than on a directory of files. A broken read must not stop the server — the
    # owner opening the dashboard is how they find out something is wrong — so it
    # reports and carries on.
    try:
        write_page(calendar_page(calendar), settings.home, front=True)
    except (OSError, ValueError) as exc:
        print(f"the calendar dashboard did not render at startup: {type(exc).__name__}: {exc}")
    url = server.start()
    print(f"serving {server.artifacts} at {url}  (loopback only — nothing is published)")
    print(f"the calendar is at {url}/dashboard  ({' · '.join(VIEWS)}: {url}/week, {url}/month)")
    print("this viewer is read-only: the dashboard's controls run with the daemon (`daedalus run`)")
    published = None
    if args.publish:
        from .genui.publish import PublishError, publish

        try:
            published = publish(server.port)
        except PublishError as exc:
            print(f"✗ not published: {exc}")
            return 1
        if published.url:
            print(f"the dashboard is also at {published.url}/dashboard")
        print("ctrl-c to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("")
    finally:
        server.stop()
        if published is not None:
            from .genui.publish import unpublish

            print("taking the published route down (the port behind it is gone)")
            unpublish(server.port)
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
        # the reply clears it, so it never lingers past the answer. Routing is
        # part of the work — the question "who takes this" is asked with the
        # indicator up, not in silence.
        async with surface.working(event.channel_id):
            decision = await route(rt, event)
            result = await engine.run_turn(event, seat=decision.seat.key)
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
        await deliver(rt.surface, result, header=attribution(rt, decision))  # type: ignore[arg-type]

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
        dashboard = _start_dashboard(rt)
        try:
            while True:
                try:
                    line = await asyncio.to_thread(input, "› ")
                except EOFError:
                    break
                if not line.strip():
                    continue
                event = InboundEvent(text=line, user_id="owner", is_dm=False, source="cli")
                decision = await route(rt, event)
                result = await engine.run_turn(event, seat=decision.seat.key)
                await deliver(surface, result, header=attribution(rt, decision))
        finally:
            if dashboard is not None:
                dashboard.stop()
            await rt.aclose()
        return 0

    return asyncio.run(go())


NO_OUTPUT = (
    "I worked through that but came back with nothing to send — that's a fault on my side, "
    "not a refusal. Say the word and I'll try again."
)


async def deliver(surface: Surface, result: TurnResult, *, header: str = "") -> bool:
    """Send the turn's text, or say plainly that there wasn't any.

    ``header`` is who answered, when that is worth saying — see
    :func:`attribution`. It travels with the fallback too: a turn that produced
    nothing is still a turn some identity ran, and the owner should not have to
    guess which one came back empty.

    Returns True if the no-output fallback was used. Silence is the one outcome
    that must never happen: an unanswered DM and a dead daemon are then
    indistinguishable from the outside, which is exactly how a real request went
    missing without leaving a trace.
    """
    body = result.text or NO_OUTPUT
    await surface.send(f"{header}\n\n{body}" if header else body)
    return not result.text


async def route(runtime: Runtime, event: InboundEvent) -> Decision:
    """Who takes this message (SPEC §7).

    Runs at the ingress, before the turn starts, so the turn runs as one
    identity from its first token: the seat's charter, the seat's memory
    namespace, the seat's voice. The decision is written to
    ``runtime.last_seat`` so a follow-up with no subject of its own stays with
    the seat already handling this conversation.

    The routing question goes to the **cheap** route first (the heavy chain is
    still the fallback), and its failure is never the turn's: `triage` falls
    back, because losing an owner's request to a classifier's bad day is not an
    acceptable outcome.
    """
    from .cabinet.triage import triage

    channel = event.channel_id or event.user_id or ""

    async def ask(question: str) -> str:
        result = await runtime.model.chat(
            [{"role": "user", "content": question}], route="cheap"
        )
        return result.content or ""

    decision = await triage(
        event.text,
        runtime.seats,
        ask=ask,
        previous=runtime.last_seat.get(channel, ""),
    )
    runtime.last_seat[channel] = decision.seat.key
    # The seat and how it was chosen, never the reason: the reason is prose about
    # the owner's message, and the log is not a place for their words.
    log.info("routing: %s (%s)", decision.seat.key, decision.by)
    return decision


def attribution(runtime: Runtime, decision: Decision) -> str:
    """The line that says who answered — empty when nobody needs telling.

    Empty means the owner is talking to the identity they chose to talk to
    (the active seat), which needs no announcement. Anything else gets named,
    with the router's reason when the router is what chose — that is what makes
    a wrong route correctable instead of mysterious.
    """
    from .cabinet.seat import active_seat

    if decision.seat.key == active_seat(runtime.seats).key:
        return ""
    line = f"**{decision.seat.name}**"
    if decision.by == "model" and decision.reason:
        line += f" · {decision.reason}"
    return line


def _seconds_to_next_tick(now: float, interval_s: float) -> float:
    """How long to wait, from ``now``, for the next aligned tick boundary.

    Split out so the arithmetic can be tested without running the loop: the
    property that matters is that the tick lands on the same offset past every
    minute, forever, no matter how long a tick took.
    """
    nxt = (math.floor(now / interval_s) + 1) * interval_s
    return max(nxt - now, 0.0)


async def _ticker(rt: Runtime, interval_s: float) -> None:
    """Pump scheduler ticks in-process, forever (SPEC §8).

    There is deliberately no separate `tick` verb — the daemon pumps its own
    ticks, so the timer unit triggers `daedalus run` rather than a one-shot.

    Ticks are aligned to the wall clock, not spaced from the end of the last
    one. Sleeping ``interval_s`` after each tick makes the period *tick duration
    + interval*, so the tick time walks forward a little every minute; after a
    few hours it crosses into the next minute and a job pinned to a single
    minute — ``0 8,20 * * *`` — is simply never due. The drift is invisible
    until the one morning it matters, so the sleep is computed against the clock
    and a tick that overruns is caught up immediately instead of pushing the
    schedule further out.
    """
    while True:
        try:
            await rt.scheduler.dispatch_tick(rt, wake_handler=rt.wake)
        except Exception:  # noqa: BLE001 — a bad tick must not kill the daemon
            logging.getLogger("daedalus.scheduler").exception("tick failed")
        now = time.time()
        await asyncio.sleep(_seconds_to_next_tick(now, interval_s))


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
    dashboard = _start_dashboard(rt)
    try:
        await serving
    finally:
        tick.cancel()
        if dashboard is not None:
            dashboard.stop()
        await rt.aclose()
    return 0


def _start_dashboard(rt: "Runtime") -> "LocalServer | None":
    """Serve the dashboard with the daemon's controls attached (SPEC §3.6).

    The controls need **both** halves of the approval split: the middleware,
    which is built from the runtime, and a surface to put an approval button on.
    Both live with the daemon, so this is the only process that starts the
    dashboard with controls attached — `daedalus serve` stays a read-only viewer
    and says so if a control reaches it. A dashboard that cannot start is
    reported and dropped: it is a convenience, and the daemon must not die for it.
    """
    import asyncio as _asyncio

    from .genui.actions import Controls
    from .genui.server import LocalServer, control_token
    from .genui.tuners import Tuners

    try:
        token = control_token(rt.secrets)
        server = LocalServer(
            rt.settings.home,
            token=token,
            port=0,
            calendar=rt.calendar,
            controls=Controls(rt, loop=_asyncio.get_running_loop()),
            tuners=Tuners(rt.settings.home),
        )
        url = server.start()
    except Exception:  # noqa: BLE001 — the daemon outranks its dashboard
        logging.getLogger("daedalus.genui").warning(
            "the dashboard did not start; the daemon runs without it", exc_info=True
        )
        return None
    logging.getLogger("daedalus.genui").info("dashboard at %s/dashboard (loopback only)", url)
    print(f"the dashboard is at {url}/dashboard (loopback only)")
    if token:
        # The one bootstrap: opening it once hands the browser its session, so
        # the controls work and the token never has to appear in a document.
        print(f"  with controls: {url}/dashboard?t={token}")
    return server


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

    p_seats = sub.add_parser("seats", help="show the cabinet seats and their charters")
    p_seats.set_defaults(func=cmd_seats)

    p_act = sub.add_parser("activate", help="make one seat the active one")
    p_act.add_argument("name", help="the seat to talk to, by key or name")
    p_act.set_defaults(func=cmd_activate)

    p_serve = sub.add_parser("serve", help="serve the dashboard on the loopback")
    p_serve.add_argument(
        "--port", type=int, default=8787, help="loopback port (0 picks a free one)"
    )
    p_serve.add_argument(
        "--publish",
        action="store_true",
        help="also put the dashboard behind Tailscale Serve (never Funnel)",
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
