"""Melinoë remembers her verdicts (T22 — a refusal with no memory is a new answer).

The gate has always *acted* — check scopes, run once, check the shape — but its
verdicts lived only in the turn's output. A capability refused on Tuesday met a
Melinoë with no memory of having said no on Thursday. Her namespace, her record:
what she verified and what she refused, with the reason, so activating her means
answering *from* it, and re-authoring the same shape re-runs the gate. A
bookkeeping failure is logged, never fatal: the gate stands even if her bench
cannot take the note.
"""

from __future__ import annotations

from daedalus.cabinet.namespace import memory_for
from daedalus.loop.context import build_system_prompt
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


async def test_her_verdicts_are_scoped_recorded_answered_from_and_re_litigated(
    runtime: Runtime,
) -> None:
    # a refusal lands in her namespace, with the check that failed — and not in
    # the coordinator's, because whose verdicts these are is what a namespace is for
    refused = await author(runtime, "wide_hookup", BROKEN_RUN)
    assert not refused.ok

    bench = memory_for(runtime.memory_root, runtime.seats["melinoe"])
    coordinator = memory_for(runtime.memory_root, runtime.seats["daedalus"])
    mine = bench_text(runtime, "wide_hookup")
    assert "REFUSED" in mine, (
        "the refusal — capability, verdict, reason — is her record to answer from"
    )
    assert "run_once" in mine, "the reason names the check that failed, not just 'no'"
    assert coordinator.recall("wide_hookup") == [], (
        "the coordinator's recall must not inherit Melinoë's bench"
    )

    # a pass is recorded too
    passed = await author(runtime, "tidy_echo", ECHO_RUN)
    assert passed.ok
    assert "PASSED" in bench_text(runtime, "tidy_echo")

    # activating her puts her record in the prompt she answers from
    prompt = build_system_prompt(
        runtime, InboundEvent(text="why is wide_hookup not scheduled?", user_id="owner"), seat="melinoe"
    )
    assert "wide_hookup" in prompt and "REFUSED" in prompt, (
        "she answers from her own record: her prompt carries the verdict with its reason"
    )

    # re-authoring a refused capability is a new trial, not a carry-over: the gate
    # runs end to end again and only a fresh pass verifies
    first = await author(runtime, "second_chance", BROKEN_RUN)
    assert not first.ok
    pending = runtime.capabilities.get("second_chance")
    assert pending is not None and pending.verified_at is None

    second = await author(runtime, "second_chance", ECHO_RUN)
    assert second.ok, second.output
    reloaded = runtime.capabilities.get("second_chance")
    assert reloaded is not None and reloaded.verified_at is not None, (
        "only a fresh pass at the gate verifies — the second authoring was a new trial"
    )
    assert bench.recall("second_chance"), "the second verdict is on her record too"


async def test_the_gate_stands_even_if_her_memory_cannot_be_written(
    runtime: Runtime, caplog
) -> None:
    import logging
    import unittest.mock as mock

    from daedalus.capabilities.verify import VerifyReport

    # NOTE(bug?): the original test attached a Trap handler and cleaned up with
    # `logger.removeHandler(0)`, which removed nothing and left the handler on
    # `daedalus.capabilities` for the rest of the session. caplog holds the same
    # guarantee without the leak; the assertion below is unchanged.
    bench = memory_for(runtime.memory_root, runtime.seats["melinoe"])
    with mock.patch.object(bench, "write", side_effect=OSError("disk full")):
        with caplog.at_level(logging.WARNING, logger="daedalus.capabilities"):
            CapabilityTool().record_verdict(
                _manifest("unwritable"), VerifyReport(ok=False), runtime
            )  # must not raise

    assert caplog.records, "a verdict lost to a full disk is logged, not swallowed"


def _manifest(name: str):
    from daedalus.capabilities.manifest import Manifest

    return Manifest(name=name, description="x")
