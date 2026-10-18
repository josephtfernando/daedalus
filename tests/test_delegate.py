"""The delegation loop (T29, T30) — out to a workspace, back reviewable.

The tests use a stub command instead of the real OpenCode CLI: what is under
test here is the *shape* — containment before spawn, no history out of the run,
the diff against the baseline, the caps stopping a runaway — not the model.
"""

from __future__ import annotations

import sys

from daedalus.delegate.runner import ContainmentRefused, DelegateRunner
from daedalus.runtime import Runtime
from daedalus.tools.delegate import DelegateTool, _stop_live

PY = sys.executable


def runner(runtime: Runtime, name: str = "demo"):
    return DelegateRunner(runtime.delegate_runner.root, python=PY)


async def test_a_delegation_refuses_a_path_outside_its_workspace_before_any_spawn(
    runtime: Runtime,
) -> None:
    r = DelegateRunner(runtime.delegate_runner.root, python=PY)
    try:
        r.check("read /etc/hosts and summarise it", "outside")
        raised = False
    except ContainmentRefused as exc:
        raised = True
        assert "/etc/hosts" in str(exc) and "outside its declared workspace" in str(exc)
    assert raised, "a wider delegation than the shape promised is refused — before anything runs"
    # and the workspace for a refused delegation is never even created: the
    # refusal precedes every mutation, not just the spawn.
    assert not (runtime.delegate_runner.root / "outside").exists()


async def test_the_personal_tree_is_not_a_workspace(runtime: Runtime) -> None:
    """The refusal names what was attempted — the tree is out of the reach, by shape."""
    tool = DelegateTool()
    result = await tool.run(
        {"op": "run", "task": f"read {runtime.settings.home}/SPEC.md", "name": "mine"}, runtime
    )
    assert not result.ok and "personal tree" in result.output


async def test_a_delegation_returns_a_diff_and_a_run_record(runtime: Runtime) -> None:
    r = DelegateRunner(runtime.delegate_runner.root, python=PY)
    result = await r.run(
        "write a note",
        name="demo",
        cmd=["/bin/sh", "-c", "echo hello > note.txt"],
        caps={"time_s": 60},
    )
    assert result.ok, result.error
    assert "note.txt" in result.diff, "the return is the diff against the baseline"
    import json

    record = json.loads((result_diff_ws(runtime) / ".delegation.json").read_text())
    assert record["task"] == "write a note"
    assert record["exit_code"] == 0 and record["caps"]["time_s"] == 60, (
        "the run record says what ran and under what caps"
    )


def result_diff_ws(runtime: Runtime):
    return runtime.delegate_runner.root / "demo"


async def test_no_path_takes_a_commit(runtime: Runtime) -> None:
    """The shim is the enforcement, because --no-verify bypasses hooks (T29)."""
    r = DelegateRunner(runtime.delegate_runner.root, python=PY)
    # The task text itself never mentions history; the *command* tries it,
    # which is the path a model takes when it disobeys its instructions.
    result = await r.run(
        "tidy the manifest",
        name="shim",
        cmd=["/bin/sh", "-c", "git commit -m smuggled"],
        caps={"time_s": 60},
    )
    assert not result.ok and result.exit_code == 126
    assert "stays with the owner" in result.error

    # and the task that asks for history is refused before anything runs at all
    try:
        r.check("git push the branch", "shim2")
        assert False, "no push should ever be reachable by the task text"
    except ContainmentRefused as exc:
        assert "push" in str(exc)


async def test_a_cap_stops_a_runaway_and_is_named(runtime: Runtime) -> None:
    import tempfile

    alloc = (
        "import time\n"
        "block = bytearray(200 * 1024 * 1024)\n"
        "for i in range(0, len(block), 4096):\n"
        "    block[i] = 1\n"
        "time.sleep(2)\n"
    )
    r = DelegateRunner(runtime.delegate_runner.root, python=PY)
    ws = runtime.delegate_runner.root
    ws.mkdir(parents=True, exist_ok=True)
    path = ws / "alloc.py"
    path.write_text(alloc)
    result = await r.run(
        "eat memory",
        name="captest",
        cmd=[PY, str(path)],
        caps={"mem_mb": 64, "time_s": 60},
    )
    assert not result.ok
    assert "mem_mb=64" in result.error, "the stop is visible with the cap named"


async def test_a_delegation_is_registered_from_its_first_tick_and_cancellable(
    runtime: Runtime,
) -> None:
    """T30 — visible before it finishes, stoppable as a tree."""
    tool = DelegateTool()
    result = await tool.run(
        {
            "op": "run",
            "task": "sleep for a while, do nothing",
            "name": "longrun",
            "time_s": 120,
        },
        runtime,
    )
    assert result.ok, result.output
    managed = runtime.processes.get("delegation longrun")
    assert managed is not None and managed.alive(), (
        "the delegation is in the registry from its first tick, not its last"
    )

    # closing it is the tree-wide stop the registry already knows how to do
    stopped, detail = await runtime.processes.stop("delegation longrun")
    assert stopped, detail
    assert not managed.alive()
    # the collect task then closes the row rather than reporting a runaway
    import asyncio

    for _ in range(40):
        if not managed.alive() and managed.ended_at is not None:
            break
        await asyncio.sleep(0.05)
    assert managed.ended_at is not None, "the ending is recorded, not left as rc=None"
