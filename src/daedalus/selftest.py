"""End-to-end verification — what ``scripts/setup.sh`` ends on (SPEC §8.5 stage 12).

Runs offline by default (a scripted model), so it proves the loop and the
substrate without a network or a live credential. ``live=True`` additionally
probes the configured providers.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from dataclasses import dataclass, field, replace
from pathlib import Path

from .approvals.risk import Risk
from .config import Settings
from .loop.engine import Engine
from .model.client import ModelClient
from .model.providers import ChatResult, ScriptedProvider, ToolCall  # noqa: F401
from .runtime import Runtime, build_runtime
from .scheduler.jobs import Job
from .security.provenance import DataClass, Labeled, Origin
from .storage import paths
from .surface.base import InboundEvent
from .surface.cli import ScriptedSurface

ECHO_RUN = '''"""Authored capability: daily_focus — a deterministic artifact."""
import json
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    args = payload.get("args", {})
    state = payload.get("state", {})
    result = {"focus": args.get("focus", "one thing"), "seen": state.get("seen", 0)}
    state_next = {"seen": result["seen"] + 1}
    json.dump({"result": result, "state_next": state_next, "notifications": []}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def render(self) -> str:
        lines = []
        for c in self.checks:
            mark = "\u2713" if c.ok else "\u2717"
            tail = f"  {c.detail}" if c.detail else ""
            lines.append(f" {mark} {c.name}{tail}")
        passed = sum(1 for c in self.checks if c.ok)
        lines.append(f"\n{passed}/{len(self.checks)} checks passed")
        return "\n".join(lines)


async def run_selftest(settings: Settings, *, live: bool = False) -> Report:
    """Verify the install without writing to it.

    The real home is checked **read-only** (layout; and a self-cleaning
    round-trip through the credential store). Everything that *creates* state —
    memory, grants, an authored capability, a scheduled job, a turn — runs in a
    throwaway scratch home, because a verification gate that dirties the thing
    it verifies is not a gate.
    """
    report = Report()
    surface = ScriptedSurface(approve=True)
    scratch_home = Path(tempfile.mkdtemp(prefix="daedalus-selftest-"))
    try:
        paths.ensure_tree(scratch_home)

        report.checks.append(_check_layout(settings))

        scratch = replace(settings, home=scratch_home)
        rt = build_runtime(scratch, surface=surface)
        try:
            report.checks.append(_check_secrets(rt))
            report.checks.append(_check_memory(rt))
            report.checks.append(_check_calendar(rt))
            report.checks.append(_check_serve(scratch))
            report.checks.append(_check_dashboard(rt))
            report.checks.append(_check_tuners(scratch))
            report.checks.append(_check_approvals(rt))
            report.checks.append(await _check_capability_gate(rt))
            report.checks.append(await _check_caps(rt))
            report.checks.append(await _check_scheduler(rt))
            report.checks.append(await _check_proactive(rt))
            report.checks.append(await _check_catchup(scratch))
            report.checks.append(await _check_loop(scratch, surface))
            report.checks.append(await _check_daemon(scratch))
        finally:
            await rt.aclose()

        if live:
            report.checks.append(await _check_providers(settings))
    finally:
        shutil.rmtree(scratch_home, ignore_errors=True)
    return report


# -- individual checks -----------------------------------------------------


def _check_layout(settings: Settings) -> Check:
    """Read-only assertions about the real install — this check never writes."""
    home = settings.home
    if not home.exists():
        return Check("layout", False, f"{home} does not exist")
    mode = stat.S_IMODE(home.stat().st_mode)
    if mode != 0o700:
        return Check("layout", False, f"{home} is {oct(mode)}, expected 0o700")
    for part in ("memory", "capabilities", "approvals", "state", "resources/creds"):
        if not (home / part).exists():
            return Check("layout", False, f"missing {part}")

    # Secrets that are already present must be hardened. We assert, never create.
    for rel in (".env", "resources/master.key"):
        path = home / rel
        if not path.exists():
            continue
        file_mode = stat.S_IMODE(path.stat().st_mode)
        if file_mode != 0o600:
            return Check("layout", False, f"{rel} is {oct(file_mode)}, expected 0o600")

    for path in (home / "resources" / "creds").glob("*"):
        cred_mode = stat.S_IMODE(path.stat().st_mode)
        if cred_mode & 0o077:
            return Check("layout", False, f"creds/{path.name} is {oct(cred_mode)} — group/other can read it")

    return Check("layout", True, f"{home} at 0700")


def _check_secrets(rt: Runtime) -> Check:
    """Round-trip through a scratch store — proves the code, touches no real key."""
    rt.secrets.put("selftest_probe", "correct-horse-battery-staple")
    try:
        value = rt.secrets.get("selftest_probe")
        if value != "correct-horse-battery-staple":
            return Check("secrets", False, "round-trip failed")
        blob = (rt.secrets.dir / "selftest_probe.enc").read_bytes()
        if b"correct-horse" in blob or b"battery" in blob:
            return Check("secrets", False, "plaintext found at rest")
        key_mode = stat.S_IMODE(rt.secrets.key_path.stat().st_mode)
        if key_mode != 0o600:
            return Check("secrets", False, f"master key is {oct(key_mode)}, expected 0o600")
    finally:
        rt.secrets.delete("selftest_probe")
    return Check("secrets", True, "encrypted at rest, round-trips")


def _check_memory(rt: Runtime) -> Check:
    entry = rt.memory.write(facet="projects", content="selftest probe: daedalus P0")
    hits = rt.memory.recall("daedalus", facet="projects")
    if not any(h.id == entry.id for h in hits):
        return Check("memory", False, "written entry not recalled")

    # Supersession must retire the old fact, and it must survive a rebuild of the
    # index from the notes — the markdown is the source of truth, the index is a
    # cache. A superseded fact that comes back after a rebuild is a memory that
    # cannot be corrected.
    newer = rt.memory.write(facet="projects", content="selftest probe: daedalus P1")
    if not rt.memory.supersede(entry.id, newer):
        return Check("memory", False, "supersede did not take effect")
    rt.memory.rebuild_index()
    current = [h.content for h in rt.memory.recall("selftest", facet="projects")]
    if "selftest probe: daedalus P0" in current:
        return Check("memory", False, "a superseded fact returned after an index rebuild")

    # A `local_only` fact must not appear in a prompt that could go remote.
    rt.memory.write(
        facet="body", content="selftest local-only probe", data_class=DataClass.LOCAL_ONLY
    )
    from .loop.context import build_system_prompt

    prompt = build_system_prompt(rt, InboundEvent(text="hi", user_id="selftest"))
    if "selftest local-only probe" in prompt:
        return Check("memory", False, "a local_only fact was placed in a remote-bound prompt")

    return Check("memory", True, f"wrote+recalled+superseded {entry.id[:8]}")


def _check_calendar(rt: Runtime) -> Check:
    """The calendar is file-first, and gated like memory (SPEC §5.5, §6)."""
    from datetime import datetime, timedelta

    from .loop.context import build_system_prompt

    event = rt.calendar.add(summary="selftest probe", start="2099-01-01T09:00")
    if rt.calendar.get(event.uid) is None:
        return Check("calendar", False, "a written event was not read back")

    # A hand edit must be visible to the very next read: the index is a cache
    # over `events.ics`, not a second source of truth.
    rt.calendar.ics_path.write_text(
        rt.calendar.ics_path.read_text(encoding="utf-8").replace(
            "END:VCALENDAR",
            "BEGIN:VEVENT\r\nUID:selftest-hand\r\nDTSTAMP:20990101T090000Z\r\n"
            "DTSTART:20990102T090000\r\nSUMMARY:added by hand\r\nEND:VEVENT\r\n"
            "END:VCALENDAR",
        ),
        encoding="utf-8",
    )
    if not any(e.uid == "selftest-hand" for e in rt.calendar.all()):
        return Check("calendar", False, "a hand edit was hidden by the stale index")

    # A `local_only` event must not reach a prompt that could go remote — and it
    # has to be *in the window* for that to prove anything, so both events sit
    # inside the next few days rather than in a decade.
    soon = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    later = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M")
    rt.calendar.add(summary="selftest shareable event", start=soon)
    rt.calendar.add(summary="selftest local-only event", start=later, data_class=DataClass.LOCAL_ONLY)

    prompt = build_system_prompt(rt, InboundEvent(text="hi", user_id="selftest"))
    if "selftest shareable event" not in prompt:
        return Check("calendar", False, "an in-window event never reached the agenda")
    if "selftest local-only event" in prompt:
        return Check("calendar", False, "a local_only event was placed in a remote-bound prompt")

    # A rule has to answer for the *day*, not just exist as a row (SPEC §5.5):
    # "every Monday" is only real if Monday can be asked about.
    anchor = datetime.now().date() + timedelta(days=7)
    weekday = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[anchor.weekday()]
    series = rt.calendar.add(
        summary="selftest repeating probe",
        start=f"{anchor.isoformat()}T09:00",
        rrule=f"FREQ=WEEKLY;BYDAY={weekday}",
    )
    day_start = datetime.combine(anchor, datetime.min.time())
    on_day = rt.calendar.occurrences(day_start, day_start + timedelta(days=1))
    if not any(e.uid == series.uid for e in on_day):
        return Check("calendar", False, "a repeating event did not appear on the day it repeats")

    # Moving one instance must replace that occurrence and leave the series.
    slot = f"{anchor.isoformat()}T11:00"
    rt.calendar.update(
        series.uid, recurrence_id=f"{anchor.isoformat()}T09:00", scope="this", start=slot
    )
    after = rt.calendar.occurrences(day_start, day_start + timedelta(days=1))
    if [e.start for e in after if e.uid == series.uid] != [slot]:
        return Check("calendar", False, "moving one instance did not replace just that instance")

    # Free/busy and search answer *questions*, not just lists. Two overlapping
    # meetings have to become one occupied stretch, and the words have to find
    # the event — otherwise the calendar is a pretty file, not a usable one.
    next_day = anchor + timedelta(days=1)
    rt.calendar.add(summary="selftest meeting one", start=f"{next_day}T09:00", end=f"{next_day}T10:00")
    rt.calendar.add(summary="selftest meeting two", start=f"{next_day}T09:30", end=f"{next_day}T10:30")
    day_start = datetime.combine(next_day, datetime.min.time())
    blocks = rt.calendar.busy(day_start, day_start + timedelta(days=1))
    shape = [(b[0].strftime("%H:%M"), b[1].strftime("%H:%M")) for b in blocks]
    if shape != [("09:00", "10:30")]:
        return Check("calendar", False, f"overlapping events did not merge (got {shape})")
    if not rt.calendar.search("selftest meeting one"):
        return Check("calendar", False, "search did not find an event by its text")

    # And a change must be able to reach a whole *set*, not one row at a time:
    # "move all my standups to 10" is the edit people actually make, and it has
    # to move each one onto its own day rather than piling them onto one.
    rt.calendar.add(summary="selftest bulk probe", start=f"{next_day}T14:00", end=f"{next_day}T14:30")
    day_after = next_day + timedelta(days=1)
    rt.calendar.add(summary="selftest bulk probe", start=f"{day_after}T14:00", end=f"{day_after}T14:30")
    changed = rt.calendar.bulk_update("selftest bulk probe", at="10:00")
    if sorted(e.start for e in changed) != sorted(
        [f"{next_day}T10:00", f"{day_after}T10:00"]
    ):
        return Check("calendar", False, "a bulk retime did not move each event onto its own day")

    # And an outside calendar has to be able to come in — a calendar you cannot
    # bring your history into is a calendar you cannot move to.
    handover = rt.settings.home / "state" / "selftest-import.ics"
    handover.write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Google Inc//Google Calendar//EN\r\n"
        "X-WR-CALNAME:owner@example.com\r\n"
        "BEGIN:VTIMEZONE\r\nTZID:Europe/London\r\nEND:VTIMEZONE\r\n"
        "BEGIN:VEVENT\r\nUID:selftest-imported@example.com\r\n"
        "DTSTART;TZID=Europe/London:20990103T090000\r\n"
        "RDATE;TZID=Europe/London:20990104T090000\r\n"
        "SUMMARY:selftest imported probe\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        encoding="utf-8",
    )
    report = rt.calendar.import_ics(handover)
    arrived = rt.calendar.get("selftest-imported@example.com")
    if report.added != 1 or arrived is None or arrived.authority != "imported":
        return Check("calendar", False, "an outside calendar did not come in")
    # An `RDATE` the store reads but does not keep is an occurrence that quietly
    # stops happening, so the round trip is proven by asking for both days.
    # The window is scoped to this uid on purpose: the repeating probe above is
    # a *forever* weekly series and lands in 2099 like any other date, so asking
    # for the whole list asserted something this check does not mean — and it
    # failed on every run whose anchor weekday matched 2099-01-03.
    extra = [
        e
        for e in rt.calendar.occurrences(datetime(2099, 1, 3), datetime(2099, 1, 5))
        if e.uid == "selftest-imported@example.com"
    ]
    if [e.start for e in extra] != ["2099-01-03T09:00", "2099-01-04T09:00"]:
        return Check("calendar", False, "an imported RDATE did not arrive as an occurrence")
    if rt.calendar.import_ics(handover).added != 0:
        return Check("calendar", False, "importing the same file twice added it twice")

    return Check(
        "calendar", True, f"wrote+repeated+moved+searched+bulk+imported {event.uid[:8]}"
    )


def _check_dashboard(rt: Runtime) -> Check:
    """The page must show the owner's own data, and reach for nothing.

    SPEC §8.5 puts "dashboard render" among the things that end provisioning, and
    the two failures worth catching are the ones a rendered page hides perfectly.
    One is a page that goes looking for something on a network — invisible in a
    screenshot, obvious in a CSP violation. The other is a page that renders
    *around* the owner's data instead of showing it: a tidy empty grid looks like
    a working dashboard, and is the same thing as a broken one.

    So the check writes an event to the real scratch calendar, reads it back the
    way every other read does — as an *occurrence* — and insists the marker is on
    the page, in a document that names no remote origin, and that it never entered
    the spec the model would have held.
    """
    from datetime import datetime, timedelta

    from .genui.components import VOCABULARY
    from .genui.renderer import render_page
    from .genui.spec import Spec

    marker = "selftest dashboard probe"
    day = datetime.now().date()
    rt.calendar.add(
        summary=marker, start=f"{day.isoformat()}T15:00", end=f"{day.isoformat()}T15:30"
    )
    begin = datetime.combine(day, datetime.min.time())
    on_day = rt.calendar.occurrences(begin, begin + timedelta(days=1))
    if not any(event.summary == marker for event in on_day):
        return Check("dashboard", False, "the event the page was to show never came back")

    spec = Spec.parse(
        {
            "title": "Today",
            "blocks": [
                {
                    "component": "calendar",
                    "props": {
                        "view": "day",
                        "days": {"bind": "calendar.days"},
                        "today": day.isoformat(),
                    },
                }
            ],
        },
        vocabulary=VOCABULARY,
    )
    # The values are built here, from the store, and are the thing §6 keeps off the
    # wire: the model composes the keys, the laptop writes the values.
    page = render_page(
        spec,
        values={
            "calendar.days": [
                [day.isoformat(), day.strftime("%a"), " · ".join(e.summary for e in on_day)]
            ]
        },
    )
    if marker in json.dumps(spec.to_dict()):
        return Check("dashboard", False, "the owner's data was in the model-visible spec")
    if marker not in page:
        return Check("dashboard", False, "the page did not carry the owner's own event")
    for forbidden in ("<script", "http://", "https://", "url(", "@import"):
        if forbidden in page:
            return Check("dashboard", False, f"the page reaches for {forbidden!r}")
    if "Content-Security-Policy" not in page or "--d-accent:" not in page:
        return Check("dashboard", False, "the page carries no policy or no theme")
    return Check("dashboard", True, f"rendered the day around {marker!r}, nothing fetched")


def _check_tuners(scratch: Settings) -> Check:
    """A tune pends, the live theme does not move, and approval does (SPEC §3.6).

    "Seeing is the most accurate way to say yes" only holds if the thing being
    seen is not already live, so the check is the acceptance in miniature: the
    theme is read twice with a tune in between, and once after an approval.
    """
    from .genui.tuners import Tuners

    tuners = Tuners(scratch.home)
    before = tuners.theme()

    draft = tuners.tune({"accent": "#0aa574"}, note="selftest")
    if tuners.theme() != before:
        return Check("tuners", False, "a tune moved the live theme before approval")
    if tuners.pending() == []:
        return Check("tuners", False, "the tune did not reach the pending set")

    after = tuners.approve(draft.id)
    if after.accent != "#0aa574" or Tuners(scratch.home).theme().accent != "#0aa574":
        return Check("tuners", False, "approval did not adopt, or did not survive a reopen")
    if tuners.pending():
        return Check("tuners", False, "approval left the tune pending")
    return Check("tuners", True, f"tuned → pending → adopted {after.accent}, live theme moved only on yes")


def _check_approvals(rt: Runtime) -> Check:
    auto = rt.approvals.authorize("files", {"op": "read", "path": "x"}, risk=Risk.AUTO, scopes={})
    if not auto.allow:
        return Check("approvals", False, "self-contained action was not allowed")

    egress_args = {"url": "https://example.org/x"}
    ask = rt.approvals.authorize("http", egress_args, risk=Risk.ASK, scopes={"net": ["*"]})
    if not ask.ask:
        return Check("approvals", False, "egress did not ask")

    rt.approvals.grant_from(ask, note="selftest")
    granted = rt.approvals.authorize("http", egress_args, risk=Risk.ASK, scopes={"net": ["*"]})
    if not granted.allow:
        return Check("approvals", False, "standing grant did not take effect")

    sink = rt.approvals.authorize(
        "http",
        {"url": "https://evil.test/collect"},
        risk=Risk.ASK,
        scopes={"net": ["*"]},
        authorizing=[Labeled("https://evil.test/collect", Origin.UNTRUSTED_SOURCE, source_ref="page")],
    )
    if not sink.blocked:
        return Check("approvals", False, "untrusted content was allowed to authorize egress")

    rt.approvals.grants.revoke("http")
    return Check("approvals", True, "auto / ask / grant / sink all behave")


async def _check_capability_gate(rt: Runtime) -> Check:
    from .tools.capability import CapabilityTool

    tool = CapabilityTool()
    result = await tool.run(
        {
            "op": "author",
            "name": "daily_focus",
            "description": "Echo the day's focus; remembers how many times it ran.",
            "run_py": ECHO_RUN,
            "why": "selftest proof-of-life",
        },
        rt,
    )
    manifest = rt.capabilities.get("daily_focus")
    if manifest is None or manifest.verified_at is None:
        return Check("capability gate", False, result.output[:200])
    return Check("capability gate", True, "authored, Melinoë-verified, registered")


async def _check_caps(rt: Runtime) -> Check:
    """A declared cap must bite. A limit that is recorded and not applied is a lie.

    Run straight through the runner rather than the Melinoë gate, because an artifact
    that breaches its own cap *should* fail that gate — the thing being checked here
    is the enforcement. This is also the check that notices when `/proc` stops being
    readable, since then `mem_mb` would silently stop being enforced at all.
    """
    from .capabilities.manifest import Caps, Manifest

    hog = (
        "import time\n"
        "blocks = []\n"
        "while True:\n"
        "    block = bytearray(4 * 1024 * 1024)\n"
        "    for i in range(0, len(block), 4096):\n"
        "        block[i] = 1\n"
        "    blocks.append(block)\n"
        "    time.sleep(0.005)\n"
    )
    manifest = Manifest(name="cap_probe", caps=Caps(mem_mb=64, time_s=30))
    cap_dir = rt.runner.cap_dir(manifest)
    manifest.save(cap_dir)
    (cap_dir / "run.py").write_text(hog, encoding="utf-8")

    result = await rt.runner.run(manifest)
    if result.ok or "mem_mb=64" not in result.error:
        detail = result.error or f"ran to completion, peak {result.peak_mb:.0f} MB"
        return Check("resource caps", False, f"mem_mb=64 was not enforced: {detail}")
    return Check("resource caps", True, f"mem_mb=64 stopped a runaway at {result.peak_mb:.0f} MB")


async def _check_scheduler(rt: Runtime) -> Check:

    job = rt.scheduler.add_job(name="cap-daily_focus", cron="0 7 * * *", kind="capability", target="daily_focus")
    results = await rt.scheduler.dispatch_tick(rt, when=_at_minute("0 7 * * *"))
    ran = [r for r in results if r.job == job.name]
    if not ran or not ran[0].ok:
        detail = ran[0].detail if ran else "job was not due"
        return Check("scheduler tick", False, detail)
    again = await rt.scheduler.dispatch_tick(rt, when=_at_minute("0 7 * * *"))
    if any(r.job == job.name and r.ran for r in again):
        return Check("scheduler tick", False, "idempotency key did not dedup a repeat tick")
    return Check("scheduler tick", True, "ran the artifact with no model, deduped the repeat")


async def _check_proactive(rt: Runtime) -> Check:
    """Quiet hours, the daily cap, and the choice to stay silent (SPEC §9).

    The window is *pinned* for the probe rather than read from the owner's
    config: a check that starts failing because the owner moved their quiet
    hours would be reporting their preference, not a defect.
    """
    rt.settings = replace(
        rt.settings,
        proactive_enabled=True,
        quiet_start="22:30",
        quiet_end="07:30",
        proactive_max_per_day=4,
    )
    night = datetime(2026, 10, 16, 3, 0)
    noon = datetime(2026, 10, 16, 12, 0)
    before = len(rt.surface.sent)  # type: ignore[union-attr]

    async def speaks(prompt: str) -> str:
        return "Standup in ten minutes."

    rt.scheduler.add_job(name="probe-wake", cron="* * * * *", kind="wake", target="the check-in")
    await rt.scheduler.dispatch_tick(rt, wake_handler=speaks, when=night)
    if len(rt.surface.sent) != before:  # type: ignore[union-attr]
        return Check("proactive", False, "a wake spoke during quiet hours")

    await rt.scheduler.dispatch_tick(rt, wake_handler=speaks, when=noon)
    if len(rt.surface.sent) != before + 1:  # type: ignore[union-attr]
        return Check("proactive", False, "a wake in the open window never reached the owner")

    # The agent may look at the day and decide there is nothing worth saying.
    rt.scheduler.remove_job("probe-wake")
    rt.scheduler.add_job(name="probe-silence", cron="* * * * *", kind="wake", target="the check-in")

    async def stays_quiet(prompt: str) -> str:
        return ""

    await rt.scheduler.dispatch_tick(rt, wake_handler=stays_quiet, when=datetime(2026, 10, 16, 13, 0))
    if len(rt.surface.sent) != before + 1:  # type: ignore[union-attr]
        return Check("proactive", False, "a wake that chose silence produced a message")

    # Spend the day's allowance; the next wake must be silent without asking.
    for index in range(4):
        rt.scheduler.proactive.note_spoken(noon, f"message {index}")
    rt.scheduler.remove_job("probe-silence")
    rt.scheduler.add_job(name="probe-cap", cron="* * * * *", kind="wake", target="the check-in")
    await rt.scheduler.dispatch_tick(rt, wake_handler=speaks, when=datetime(2026, 10, 16, 14, 0))
    if len(rt.surface.sent) != before + 1:  # type: ignore[union-attr]
        return Check("proactive", False, "the daily cap did not stop a wake")

    # A reminder is the owner's instruction, not the agent's choice to speak: it
    # must reach them inside quiet hours, where a wake may not, and exactly once.
    rt.calendar.add(summary="selftest reminder probe", start="2026-10-16T03:10", reminders=[15])
    await rt.scheduler.fire_reminders(rt, datetime(2026, 10, 16, 3, 0))
    if len(rt.surface.sent) != before + 2:  # type: ignore[union-attr]
        return Check("proactive", False, "a reminder the owner set was silenced")
    await rt.scheduler.fire_reminders(rt, datetime(2026, 10, 16, 3, 1))
    if len(rt.surface.sent) != before + 2:  # type: ignore[union-attr]
        return Check("proactive", False, "the same reminder was delivered twice")

    return Check(
        "proactive", True, "quiet hours held, the cap bit, silence stayed silent, the reminder fired"
    )


async def _check_catchup(settings: Settings) -> Check:
    """A run missed while the daemon was down is caught up — T18.

    Probed on a pinned clock in its own throwaway home: the whole point is
    arithmetic between three timestamps, and a queue that fired into the surface
    the other probes are counting would make *them* flaky rather than this one red.
    """
    home = settings.home / "catchup-probe"
    paths.ensure_tree(home)
    surface = ScriptedSurface(approve=True)
    rt = build_runtime(replace(settings, home=home), surface=surface)
    try:
        now = datetime(2026, 10, 17, 14, 20)
        made = datetime(2026, 10, 14, 9, 0).timestamp()

        def job(name: str, cron: str, kind: str, target: str, last_run: datetime) -> Job:
            return rt.scheduler.store.put(
                Job(
                    name=name,
                    cron=cron,
                    kind=kind,
                    target=target,
                    last_run=last_run.timestamp(),
                    created_at=made,
                )
            )

        # Two wakes whose moment fell while the daemon was down — one is still
        # worth saying — and one probe whose moment has plainly passed.
        eight = job("probe-eight", "0 8 * * *", "wake", "the morning check-in", datetime(2026, 10, 16, 8, 0))
        job("probe-nine", "0 9 * * *", "wake", "the nine-o'clock check-in", datetime(2026, 10, 16, 9, 0))
        job("probe-disk", "0 8 * * *", "capability", "disk_free_check", datetime(2026, 10, 15, 8, 0))

        live = peak = 0

        async def handler(prompt: str) -> str:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.05)
            live -= 1
            return f"report for {prompt}"

        rt.scheduler.stagger_s = 0.0
        results = await rt.scheduler.dispatch_tick(rt, wake_handler=handler, when=now)
        if surface.sent != ["report for the morning check-in", "report for the nine-o'clock check-in"]:
            return Check("catch-up", False, f"the missed runs did not arrive in order: {surface.sent}")
        if peak != 1:
            return Check("catch-up", False, "two catch-up runs overlapped")
        disk = next((r for r in results if r.job == "probe-disk"), None)
        if disk is None or disk.ran is not False or "not caught up" not in disk.detail:
            return Check("catch-up", False, "a stale probe was not declined with a note")
        if not rt.scheduler.log.seen(eight.idempotency_key(datetime(2026, 10, 17, 8, 0))):
            return Check("catch-up", False, "the caught-up run left no record")
        again = await rt.scheduler.dispatch_tick(rt, wake_handler=handler, when=now + timedelta(minutes=3))
        if again:
            return Check("catch-up", False, f"the queue was not spent: {[r.job for r in again]}")
        return Check("catch-up", True, "missed runs arrived late, one at a time; the stale one was declined")
    finally:
        await rt.aclose()


async def _check_loop(settings: Settings, surface: ScriptedSurface) -> Check:
    script = [
        ChatResult(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="memory",
                    arguments={"op": "write", "facet": "projects", "content": "P0 loop verified"},
                )
            ]
        ),
        ChatResult(content="Noted — P0 loop verified."),
    ]
    rt = build_runtime(settings, surface=surface, provider=ScriptedProvider(script))
    engine = Engine(rt)
    event = InboundEvent(text="Remember that the P0 loop is verified.", user_id="owner", channel_id="selftest")
    result = await engine.run_turn(event)
    if "verified" not in result.text.lower():
        return Check("loop", False, f"unexpected reply: {result.text[:120]!r}")
    if "memory" not in result.tools_used:
        return Check("loop", False, "the model's tool call was not executed")
    hits = rt.memory.recall("verified")
    if not hits:
        return Check("loop", False, "the tool's side effect did not land")
    return Check("loop", True, f"free text → tool → reply in {result.steps} steps")


class _DaemonSurface:
    """A surface whose readiness is reachable *only* through `start()`.

    That is what a real surface does — it signals ready from inside its own
    connection loop — and it is precisely the ordering the daemon got wrong.
    """

    def __init__(self) -> None:
        self._ready = asyncio.Event()
        self._running = asyncio.Event()
        self.started = False

    async def start(self) -> None:
        self.started = True
        self._ready.set()
        await self._running.wait()

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def send(self, text: str, *, thread: str | None = None) -> None: ...

    async def ask(self, question: str, *, options: list[str] | None = None) -> str:
        return ""

    async def approve(self, request: object) -> bool:
        return False

    def stop(self) -> None:
        self._running.set()


def _check_serve(settings: Settings) -> Check:
    """The server must come up *private*, and must refuse anonymous writes.

    Both failure modes here are silent, which is why they are gated rather than
    assumed. A bind that is not the loopback is the whole publishing model gone,
    and a server that reads *unset* as *open* turns the dashboard into an
    unauthenticated remote control for the owner's calendar. Configuration
    cannot show either one; only knocking on the door can.
    """
    from .genui.server import LocalServer, control_token
    from .resources.secrets import SecretStore

    server = LocalServer(
        settings.home,
        token=control_token(SecretStore(settings.home)),
        port=0,
    )
    url = server.start()
    try:
        if not ipaddress.ip_address(server.host).is_loopback:
            return Check("serve", False, f"bound {server.host}, which is not the loopback")
        with urllib.request.urlopen(f"{url}/healthz", timeout=5) as response:
            if response.status != 200:
                return Check("serve", False, f"/healthz answered {response.status}")
        write = urllib.request.Request(f"{url}/tune", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(write, timeout=5)
        except urllib.error.HTTPError as refused:
            if refused.code != 401:
                return Check(
                    "serve", False, f"an unauthenticated write answered {refused.code}, not 401"
                )
        else:
            return Check("serve", False, "an unauthenticated write was accepted")
    except OSError as exc:
        return Check("serve", False, f"{type(exc).__name__}: {exc}")
    finally:
        server.stop()
    return Check("serve", True, f"loopback only, writes gated ({url})")


async def _check_daemon(settings: Settings) -> Check:
    """The daemon must actually *come up*, not merely be configured to.

    Its absence is why the Discord surface shipped silent: this gate reported
    green while the process opened no socket, dialed nothing, and logged
    nothing, because `serve()` awaited readiness before starting the very loop
    that produces it. Configuration checks cannot see that; only running it can.
    """
    from .main import serve

    surface = _DaemonSurface()
    rt = build_runtime(settings, surface=surface)  # type: ignore[arg-type]
    task = asyncio.create_task(serve(surface, rt, tick_seconds=3600))
    try:
        for _ in range(200):
            await asyncio.sleep(0)
            if surface.started:
                break
        if not surface.started:
            return Check("daemon", False, "serve() awaited readiness before starting the surface")
        surface.stop()
        code = await asyncio.wait_for(task, timeout=5)
    except asyncio.TimeoutError:
        return Check("daemon", False, "serve() did not shut down when the surface closed")
    finally:
        if not task.done():
            task.cancel()
    if code != 0:
        return Check("daemon", False, f"serve() exited {code}")
    return Check("daemon", True, "starts the surface, pumps ticks, shuts down cleanly")


async def _check_providers(settings: Settings) -> Check:
    """Walk the chain in order, stop at the first provider that answers.

    A fallback that is never probed is a fallback that might be dead, so this
    follows the same order the runtime would — with a shortened per-provider
    timeout so a cold start cannot stall the gate.
    """
    from .model.providers import HttpProvider

    if not settings.heavy:
        return Check("models (live)", False, "no providers configured")
    failures: list[str] = []
    for cfg in settings.heavy:
        probe = replace(cfg, timeout_s=min(cfg.timeout_s, 60))
        provider = HttpProvider(probe, settings.env)
        if not provider.available():
            failures.append(f"{cfg.name}: no key")
            continue
        try:
            result = await provider.complete(
                [{"role": "user", "content": "Reply with the single word: ok"}], temperature=0.0
            )
        except Exception as exc:  # noqa: BLE001
            hint = " — model ID is not in this provider's catalogue" if "404" in str(exc) else ""
            failures.append(f"{cfg.name}: {str(exc)[:100]}{hint}")
            continue
        if result.content:
            detail = cfg.name + (" (first in the chain)" if not failures else f" answered after {len(failures)} failed")
            return Check("models (live)", True, detail)
        failures.append(f"{cfg.name}: empty response")
    return Check("models (live)", False, "; ".join(failures) or "no provider had a usable key")


# -- helpers ---------------------------------------------------------------


def _at_minute(cron_expr: str):
    from .scheduler.cron import next_run

    nxt = next_run(cron_expr)
    if nxt is None:
        raise RuntimeError(f"could not resolve a minute for {cron_expr!r}")
    return nxt


def render_json(report: Report) -> str:
    return json.dumps(
        {"ok": report.ok, "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in report.checks]},
        indent=2,
    )


def run(settings: Settings, *, live: bool = False) -> Report:
    return asyncio.run(run_selftest(settings, live=live))
