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
