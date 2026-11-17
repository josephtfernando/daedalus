"""Capabilities: authoring runs the gate, egress is one owner approval, and
nothing unverified is scheduled or run.

An artifact's promise is only real once it has been verified in its own venv and
the owner has answered any egress ask. A standing grant answers next time; a
"no" leaves the capability unverified, ungranted, and unscheduled.
"""

from __future__ import annotations

from daedalus.capabilities.manifest import Manifest, Scopes
from daedalus.capabilities.verify import verify_capability
from daedalus.runtime import Runtime
from daedalus.selftest import ECHO_RUN
from daedalus.tools.capability import CapabilityTool


async def test_authoring_verifies_registers_and_gives_the_artifact_its_own_venv(
    runtime: Runtime,
) -> None:
    result = await CapabilityTool().run(
        {
            "op": "author",
            "name": "probe",
            "description": "echoes its args",
            "run_py": ECHO_RUN,
            "why": "test",
        },
        runtime,
    )
    assert result.ok, result.output
    manifest = runtime.capabilities.get("probe")
    assert manifest is not None
    assert manifest.verified_at is not None
    assert (runtime.capabilities.root / "probe" / "manifest.toml").exists()
    assert (runtime.capabilities.root / "probe" / "README.md").exists()
    # Own venv per capability (SPEC §3.5): on the host interpreter an artifact
    # could import the daemon's packages, so its reach would stop being what the
    # manifest declares.
    assert runtime.runner.venv_python(manifest).exists(), (
        "the capability ran on the host interpreter, not its own venv"
    )

    # a broken artifact fails the gate and stays unverified
    broken = await CapabilityTool().run(
        {"op": "author", "name": "broken", "description": "raises", "run_py": "raise SystemExit(3)\n"},
        runtime,
    )
    assert not broken.ok
    assert runtime.capabilities.get("broken").verified_at is None

    # the manifest on disk is the durable record, and it round-trips
    saved = Manifest(
        name="rt",
        description="round trip",
        schedule="0 6 * * *",
        scopes=Scopes(net=["a.example"]),
    )
    saved.save(runtime.capabilities.root / "rt")
    loaded = Manifest.from_path(runtime.capabilities.root / "rt" / "manifest.toml")
    assert loaded.name == "rt"
    assert loaded.schedule == "0 6 * * *"
    assert loaded.scopes.net == ["a.example"]


async def test_egress_is_asked_once_with_buttons_and_the_answer_is_remembered(
    runtime: Runtime,
) -> None:
    # the gate itself refuses egress before running anything
    netty = await verify_capability(
        Manifest(name="netty", scopes=Scopes(net=["example.org"])),
        runtime.runner,
        allow_egress=False,
    )
    assert not netty.ok
    assert netty.checks[0].name == "scope_posture"

    # a fresh egress capability asks through the approval surface, and the yes
    # becomes a standing grant
    asked = await CapabilityTool().run(
        {
            "op": "author",
            "name": "reaches_out",
            "description": "fetches a host",
            "run_py": ECHO_RUN,
            "net": ["codeforces.com"],
        },
        runtime,
    )
    assert asked.ok, asked.output
    assert runtime.surface.approvals, "the ask was a real approval, not text"
    assert runtime.approvals.grants.find("capability", "capability:reaches_out") is not None, (
        "the yes became a standing grant"
    )
    reaches = runtime.capabilities.get("reaches_out")
    assert reaches is not None and reaches.verified_at is not None

    # a standing grant answers before the owner is asked again
    from daedalus.approvals.grants import Grant

    runtime.approvals.grants.add(Grant(action="capability", scope="capability:quiet"))
    seen = len(runtime.surface.approvals)
    quiet = await CapabilityTool().run(
        {
            "op": "author",
            "name": "quiet",
            "description": "fetches quietly",
            "run_py": ECHO_RUN,
            "net": ["example.org"],
        },
        runtime,
    )
    assert quiet.ok, quiet.output
    assert len(runtime.surface.approvals) == seen, (
        "a standing grant answers before the owner is asked"
    )

    # a no leaves it unverified, ungranted, and unscheduled
    runtime.surface.approve_all = False
    denied = await CapabilityTool().run(
        {
            "op": "author",
            "name": "offline",
            "description": "fetches a host",
            "run_py": ECHO_RUN,
            "net": ["example.org"],
        },
        runtime,
    )
    assert not denied.ok
    assert "not allowed network access" in denied.output
    offline = runtime.capabilities.get("offline")
    assert offline is not None and offline.verified_at is None
    assert runtime.approvals.grants.find("capability", "capability:offline") is None
    assert runtime.scheduler.jobs() == [], "an unverified capability is never scheduled"

    # a capability refused at author time can still be approved later, at verify
    await CapabilityTool().run(
        {
            "op": "author",
            "name": "late_egress",
            "description": "fetches a host",
            "run_py": ECHO_RUN,
            "net": ["example.org"],
        },
        runtime,
    )
    assert runtime.capabilities.get("late_egress").verified_at is None

    runtime.surface.approve_all = True
    runtime.surface.approvals.clear()
    verified = await CapabilityTool().run({"op": "verify", "name": "late_egress"}, runtime)
    assert verified.ok, verified.output
    assert runtime.surface.approvals and "example.org" in str(runtime.surface.approvals[0].args)
    assert runtime.capabilities.get("late_egress").verified_at is not None


async def test_an_unverified_capability_can_neither_be_scheduled_nor_run(
    runtime: Runtime,
) -> None:
    await CapabilityTool().run(
        {"op": "author", "name": "sched", "description": "x", "run_py": ECHO_RUN},
        runtime,
    )
    # it verified, so scheduling is allowed; flip the flag to prove the guard
    manifest = runtime.capabilities.get("sched")
    manifest.verified_at = None
    runtime.capabilities.register(manifest)
    refused = CapabilityTool()._schedule({"name": "sched", "schedule": "0 7 * * *"}, runtime)
    assert not refused.ok

    # the gate walked around by hand is how a turn burns its steps: run is gated
    runtime.surface.approve_all = False
    await CapabilityTool().run(
        {
            "op": "author",
            "name": "gated",
            "description": "fetches a host",
            "run_py": ECHO_RUN,
            "net": ["example.org"],
        },
        runtime,
    )
    assert runtime.capabilities.get("gated").verified_at is None

    result = await CapabilityTool().run({"op": "run", "name": "gated"}, runtime)

    assert not result.ok
    assert "Melinoë gate" in result.output and "verify" in result.output


def test_the_prompt_tells_the_model_discord_renders_no_tables() -> None:
    from daedalus.loop.context import SYSTEM_PROMPT

    assert "no markdown tables" in SYSTEM_PROMPT
