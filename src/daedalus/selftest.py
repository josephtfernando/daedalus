"""End-to-end verification — what ``scripts/setup.sh`` ends on (SPEC §8.5 stage 12).

Runs offline by default (a scripted model), so it proves the loop and the
substrate without a network or a live credential. ``live=True`` additionally
probes the configured providers.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from .approvals.risk import Risk
from .config import Settings
from .loop.engine import Engine
from .model.client import ModelClient
from .model.providers import ChatResult, ScriptedProvider, ToolCall  # noqa: F401
from .runtime import Runtime, build_runtime
from .security.provenance import Labeled, Origin
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
            report.checks.append(_check_approvals(rt))
            report.checks.append(await _check_capability_gate(rt))
            report.checks.append(await _check_scheduler(rt))
            report.checks.append(await _check_loop(scratch, surface))
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
    return Check("memory", True, f"wrote+recalled {entry.id[:8]}")


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
