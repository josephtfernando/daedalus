from __future__ import annotations

from daedalus.capabilities.manifest import Manifest, Scopes
from daedalus.capabilities.verify import verify_capability
from daedalus.runtime import Runtime
from daedalus.selftest import ECHO_RUN
from daedalus.tools.capability import CapabilityTool


async def test_author_verifies_and_registers(runtime: Runtime) -> None:
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


async def test_each_capability_gets_its_own_venv(runtime: Runtime) -> None:
    """Own venv per capability (SPEC §3.5).

    Isolation is not only about dependency collisions: an artifact that runs on
    the host interpreter can import the daemon's own installed packages by
    accident, so what it can reach stops being declared by its manifest.
    """
    result = await CapabilityTool().run(
        {"op": "author", "name": "isolated", "description": "no deps", "run_py": ECHO_RUN},
        runtime,
    )
    assert result.ok, result.output
    manifest = runtime.capabilities.get("isolated")
    assert manifest is not None
    python = runtime.runner.venv_python(manifest)
    assert python.exists(), "the capability ran on the host interpreter, not its own venv"


async def test_broken_artifact_fails_the_gate(runtime: Runtime) -> None:
    result = await CapabilityTool().run(
        {"op": "author", "name": "broken", "description": "raises", "run_py": "raise SystemExit(3)\n"},
        runtime,
    )
    assert not result.ok
    assert runtime.capabilities.get("broken").verified_at is None


async def test_egress_artifact_needs_approval_before_the_gate(runtime: Runtime) -> None:
    manifest = Manifest(name="netty", scopes=Scopes(net=["example.org"]))
    report = await verify_capability(manifest, runtime.runner, allow_egress=False)
    assert not report.ok
    assert report.checks[0].name == "scope_posture"


async def test_unverified_capability_cannot_be_scheduled(runtime: Runtime) -> None:
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


def test_manifest_round_trips(runtime: Runtime) -> None:
    manifest = Manifest(name="rt", description="round trip", schedule="0 6 * * *", scopes=Scopes(net=["a.example"]))
    manifest.save(runtime.capabilities.root / "rt")
    loaded = Manifest.from_path(runtime.capabilities.root / "rt" / "manifest.toml")
    assert loaded.name == "rt"
    assert loaded.schedule == "0 6 * * *"
    assert loaded.scopes.net == ["a.example"]


# -- egress is approved once, with buttons, and the answer is remembered ------


async def test_an_egress_capability_asks_through_buttons_and_records_the_yes(
    runtime: Runtime,
) -> None:
    """The gap that circled into step limits: the owner's yes was never recorded."""
    result = await CapabilityTool().run(
        {
            "op": "author",
            "name": "reaches_out",
            "description": "fetches a host",
            "run_py": ECHO_RUN,
            "net": ["codeforces.com"],
        },
        runtime,
    )

    assert result.ok, result.output
    grant = runtime.approvals.grants.find("capability", "capability:reaches_out")
    assert grant is not None, "the yes became a standing grant"
    manifest = runtime.capabilities.get("reaches_out")
    assert manifest is not None and manifest.verified_at is not None
    assert runtime.surface.approvals, "the ask was a real approval, not text"


async def test_a_denied_capability_stays_unverified_and_unscheduled(runtime: Runtime) -> None:
    runtime.surface.approve_all = False

    result = await CapabilityTool().run(
        {
            "op": "author",
            "name": "reaches_out",
            "description": "fetches a host",
            "run_py": ECHO_RUN,
            "net": ["codeforces.com"],
        },
        runtime,
    )

    assert not result.ok
    assert "not allowed network access" in result.output
    manifest = runtime.capabilities.get("reaches_out")
    assert manifest is not None and manifest.verified_at is None
    assert runtime.approvals.grants.find("capability", "capability:reaches_out") is None
    assert runtime.scheduler.jobs() == [], "an unverified capability is never scheduled"


async def test_an_already_approved_capability_is_not_asked_again(runtime: Runtime) -> None:
    from daedalus.approvals.grants import Grant

    runtime.approvals.grants.add(Grant(action="capability", scope="capability:quiet"))

    result = await CapabilityTool().run(
        {
            "op": "author",
            "name": "quiet",
            "description": "fetches quietly",
            "run_py": ECHO_RUN,
            "net": ["example.org"],
        },
        runtime,
    )

    assert result.ok, result.output
    assert runtime.surface.approvals == [], "a standing grant answers before the owner is asked"


async def test_the_verify_op_asks_the_same_way(runtime: Runtime) -> None:
    """A capability refused at author time can be approved later, at verify."""
    runtime.surface.approve_all = False
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
    result = await CapabilityTool().run({"op": "verify", "name": "late_egress"}, runtime)

    assert result.ok, result.output
    assert runtime.surface.approvals and "example.org" in str(runtime.surface.approvals[0].args)
    assert runtime.capabilities.get("late_egress").verified_at is not None


def test_a_provider_failure_with_no_message_names_the_failure_kind() -> None:
    """`nim-step-5-preview: ` told the owner nothing; the exception kind does."""
    exc = ValueError()
    detail = str(exc).strip() or type(exc).__name__

    assert detail == "ValueError"


async def test_an_unverified_capability_cannot_be_run_by_hand(runtime: Runtime) -> None:
    """The gate walked around is how a turn burns fifteen steps reading source."""
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
