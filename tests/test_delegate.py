"""The delegation loop (T29, T30) — out to a workspace, back reviewable.

The tests use a stub command instead of the real OpenCode CLI: what is under test
here is the *shape* — containment before spawn, no history out of the run, the
diff against the baseline, the caps stopping a runaway, and a delegation that is
visible and cancellable from its first tick — not the model.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from daedalus.delegate.runner import ContainmentRefused, DelegateRunner
from daedalus.runtime import Runtime
from daedalus.tools.delegate import DelegateTool

PY = sys.executable


async def test_nothing_wider_than_the_declared_workspace_is_ever_reachable(
    runtime: Runtime,
) -> None:
    r = DelegateRunner(runtime.delegate_runner.root, python=PY)

    # a path outside the workspace is refused before anything runs or is created
    try:
        r.check("read /etc/hosts and summarise it", "outside")
        raised = False
    except ContainmentRefused as exc:
        raised = True
        assert "/etc/hosts" in str(exc) and "outside its declared workspace" in str(exc)
    assert raised, "a wider delegation than the shape promised is refused — before anything runs"
    assert not (runtime.delegate_runner.root / "outside").exists()

    # the personal tree is out of reach by shape, and the refusal names it
    result = await DelegateTool().run(
        {"op": "run", "task": f"read {runtime.settings.home}/SPEC.md", "name": "mine"}, runtime
    )
    assert not result.ok and "personal tree" in result.output

    # no path takes a commit: the shim refuses the command...
    committed = await r.run(
        "tidy the manifest",
        name="shim",
        cmd=["/bin/sh", "-c", "git commit -m smuggled"],
        caps={"time_s": 60},
    )
    assert not committed.ok and committed.exit_code == 126
    assert "stays with the owner" in committed.error

    # ...and the task text that asks for history never reaches a spawn
    with pytest.raises(ContainmentRefused) as stopped:
        r.check("git push the branch", "shim2")
    assert "push" in str(stopped.value)


async def test_a_delegation_returns_a_diff_a_record_and_a_capable_stop(
    runtime: Runtime,
) -> None:
    r = DelegateRunner(runtime.delegate_runner.root, python=PY)

    # the return is the diff against the baseline, with the run record beside it
    result = await r.run(
        "write a note",
        name="demo",
        cmd=["/bin/sh", "-c", "echo hello > note.txt"],
        caps={"time_s": 60},
    )
    assert result.ok, result.error
    assert "note.txt" in result.diff, "the return is the diff against the baseline"

    record = json.loads((runtime.delegate_runner.root / "demo" / ".delegation.json").read_text())
    assert record["task"] == "write a note"
    assert record["exit_code"] == 0 and record["caps"]["time_s"] == 60, (
        "the run record says what ran and under what caps"
    )

    # a cap stops a runaway, and the stop is visible with the cap named
    alloc = (
        "import time\n"
        "block = bytearray(200 * 1024 * 1024)\n"
        "for i in range(0, len(block), 4096):\n"
        "    block[i] = 1\n"
        "time.sleep(2)\n"
    )
    ws = runtime.delegate_runner.root
    ws.mkdir(parents=True, exist_ok=True)
    path = ws / "alloc.py"
    path.write_text(alloc)
    capped = await r.run(
        "eat memory",
        name="captest",
        cmd=[PY, str(path)],
        caps={"mem_mb": 64, "time_s": 60},
    )
    assert not capped.ok
    assert "mem_mb=64" in capped.error, "the stop is visible with the cap named"

    # T30 — registered from its first tick, stoppable as a tree, and its ending
    # recorded rather than left as rc=None
    tool = DelegateTool()
    started = await tool.run(
        {
            "op": "run",
            "task": "sleep for a while, do nothing",
            "name": "longrun",
            "time_s": 120,
        },
        runtime,
    )
    assert started.ok, started.output
    managed = runtime.processes.get("delegation longrun")
    assert managed is not None and managed.alive(), (
        "the delegation is in the registry from its first tick, not its last"
    )

    stopped, detail = await runtime.processes.stop("delegation longrun")
    assert stopped, detail
    assert not managed.alive()

    for _ in range(40):
        if not managed.alive() and managed.ended_at is not None:
            break
        await asyncio.sleep(0.05)
    assert managed.ended_at is not None, "the ending is recorded, not left as rc=None"
