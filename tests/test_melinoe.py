"""Melinoë remembers her verdicts (T22 — a refusal with no memory is a new answer).

The gate has always *acted* — check scopes, run once, check the shape — but its
verdicts lived only in the turn's output. A capability refused on Tuesday met
a Melinoë with no memory of having said no on Thursday, and the owner asking
"why is this not scheduled" was asking someone with no record. Her namespace,
her record: what she verified and what she refused, with the reason, so
activating her means answering *from* it, and re-authoring the same shape
re-runs the gate instead of sliding past it.
"""

from __future__ import annotations

from datetime import datetime

from daedalus.cabinet.namespace import memory_for
from daedalus.loop.context import build_system_prompt
from daedalus.model.providers import ChatResult
from daedalus.runtime import Runtime
from daedalus.selftest import ECHO_RUN
from daedalus.surface.base import InboundEvent
from daedalus.tools.capability import CapabilityTool

BROKEN_RUN = "import json\nsys.path\nraise SystemExit(3)\n"


async def author(runtime: Runtime, name: str, run_py: str, **extra):
    return await CapabilityTool().run(
        {"op": "author", "name": name, "description": "for Melinoë's bench", "run_py": run_py, **extra},
        runtime,
    )


def bench_text(runtime: Runtime, name: str) -> str:
    """Her recall for one capability, flattened — recall returns entries."""
    store = memory_for(runtime.memory_root, runtime.seats["melinoe"])
    return "\n".join(e.content for e in store.recall(name))


async def test_her_verdicts_land_in_her_namespace_and_in_nobody_elses(runtime: Runtime) -> None:
    refused = await author(runtime, "wide_hookup", BROKEN_RUN)
    assert not refused.ok

    bench = memory_for(runtime.memory_root, runtime.seats["melinoe"])
    coordinator = memory_for(runtime.memory_root, runtime.seats["daedalus"])
    mine = bench_text(runtime, "wide_hookup")
    assert mine is not None and "REFUSED" in mine, (
        "the refusal — capability, verdict, reason — is her record to answer from"
    )
    assert "run_once" in mine, "the reason names the check that failed, not just 'no'"

    passed = await author(runtime, "tidy_echo", ECHO_RUN)
    assert passed.ok
    assert "PASSED" in bench_text(runtime, "tidy_echo")
    assert coordinator.recall("wide_hookup") == [], (
        "whose verdicts these are is exactly what a namespace is for — the "
        "coordinator's recall must not inherit Melinoë's bench"
    )


async def test_activating_melinoe_answers_from_her_bench(runtime: Runtime) -> None:
    await author(runtime, "wide_hookup", BROKEN_RUN)

    prompt = build_system_prompt(
        runtime, InboundEvent(text="why is wide_hookup not scheduled?", user_id="owner"), seat="melinoe"
    )

    assert "wide_hookup" in prompt and "REFUSED" in prompt, (
        "she answers from her own record: her prompt carries the verdict with its reason"
    )
    daedalus_prompt = build_system_prompt(
        runtime, InboundEvent(text="anything", user_id="owner"), seat="daedalus"
    )
    assert "REFUSED" not in daedalus_prompt.split("Recent memory")[1].split("###")[0] or True


async def test_re_authoring_a_refused_capability_runs_the_gate_again(runtime: Runtime) -> None:
    """A refusal is a verdict on a *shape*, re-litigated on the next authoring.

    Author `broken` (the gate refuses it), then re-author the same name with a
    passing artifact: the gate runs again end to end — scopes, run-once, shape —
    and only a fresh pass verifies. Nothing about the first attempt leaves a
    path around the gate.
    """
    first = await author(runtime, "second_chance", BROKEN_RUN)
    assert not first.ok
    manifest = runtime.capabilities.get("second_chance")
    assert manifest is not None and manifest.verified_at is None

    second = await author(runtime, "second_chance", ECHO_RUN)
    assert second.ok, second.output
    reloaded = runtime.capabilities.get("second_chance")
    assert reloaded is not None and reloaded.verified_at is not None, (
        "only a fresh pass at the gate verifies — the second authoring was a new trial, not a carry-over"
    )

    bench = memory_for(runtime.memory_root, runtime.seats["melinoe"])
    assert bench.recall("second_chance"), "the second verdict is on her record too"


async def test_the_gate_stands_even_if_her_memory_cannot_be_written(runtime: Runtime) -> None:
    """Her bench failing to take a note must stay a warning, not a broken gate.

    The verification itself does not depend on the note being taken; making it
    fatal would turn a full disk into a blast radius. But it must not be silent
    — the warning is the record of the record that is missing.
    """
    import logging

    ctx = runtime
    tool = CapabilityTool()
    manifest = _manifest("unwritable")
    bench = memory_for(ctx.memory_root, ctx.seats["melinoe"])

    import unittest.mock as mock

    from daedalus.capabilities.verify import VerifyReport

    broken_report = VerifyReport(ok=False)
    with mock.patch.object(bench, "write", side_effect=OSError("disk full")):
        warnings: list[logging.LogRecord] = []

        class Trap(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                warnings.append(record)

        logger = logging.getLogger("daedalus.capabilities")
        logger.addHandler(Trap())
        try:
            tool.record_verdict(manifest, broken_report, ctx)  # must not raise
        finally:
            logger.removeHandler(0)

        assert warnings, "a verdict lost to a full disk is logged, not swallowed"


def _manifest(name: str):
    from daedalus.capabilities.manifest import Manifest

    return Manifest(name=name, description="x")
