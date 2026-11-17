"""Long-running processes: an ending is visible, and the record outlives the daemon (T20).

A process that dies leaves `rc=` set and nothing else, so the only way to learn
*when* it died and *with what* was to poll `list`; and because the registry was a
dict on the manager, a daemon restart did not just lose the history — it lost the
processes, which kept running as orphans with nothing able to list, read, or stop
them. A death must be readable without asking, a record written by a previous run
must still be listed, its liveness asked of the real pid, and a pid that has since
been given to something else must never be signalled.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path

import pytest

from daedalus.tools.process import AlreadyRunning, Managed, ProcessManager, ProcessTool, _fingerprint


def _manager(tmp_path: Path) -> ProcessManager:
    return ProcessManager(tmp_path / "state" / "processes.json")


async def _alive(managed: Managed, *, within: float = 3.0) -> bool:
    """Whether the real process is still there, allowing the kernel a moment."""
    deadline = time.monotonic() + within
    while True:
        mine = _fingerprint(managed.pid) == managed.ticks
        if not mine:
            return False
        if time.monotonic() >= deadline:
            return True
        await asyncio.sleep(0.05)


async def _reap(*managers: ProcessManager) -> None:
    """Never leave a sleep behind, however the test ended.

    Only processes this test holds a *handle* to, and by group: signalling a pid
    from the registry on the way out is the very thing the fingerprint guard exists
    to prevent, and a test that asserts the guard works must not depend on it
    holding.
    """
    for manager in managers:
        for managed in manager.list():
            if managed.proc is not None and managed.proc.returncode is None:
                try:
                    os.killpg(managed.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    managed.proc.kill()
                await managed.proc.wait()


async def test_an_ending_is_visible_without_asking_and_the_record_outlives_the_daemon(
    tmp_path,
) -> None:
    # a process that exits reports its ending, code, and keeps its output
    manager = _manager(tmp_path / "exits")
    managed = await manager.start("quick", "echo hello && exit 3")

    assert manager.get("quick") is not None
    finish = asyncio.get_event_loop().time() + 5
    while managed.ended_at is None and asyncio.get_event_loop().time() < finish:
        await asyncio.sleep(0.02)

    assert managed.ended_at is not None, "the ending was never recorded"
    assert managed.exit_code == 3
    line = managed.describe()
    assert "exited at" in line and "rc=3" in line, line
    assert "hello" in "".join(managed.log), "the output was not kept"

    # a row whose process is long gone, as a hard kill would leave it, is reported
    # as exactly that: started, never ended, and nobody watching the pid
    path = tmp_path / "ghost" / "state" / "processes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ghost": {
                    "name": "ghost",
                    "command": "sleep 1",
                    "pid": 999_999,
                    "started_at": time.time() - 3600,
                    "ticks": "1",
                    "ended_at": None,
                    "exit_code": None,
                }
            }
        ),
        encoding="utf-8",
    )

    ghost = _manager(tmp_path / "ghost").get("ghost")
    assert ghost is not None, "a record written by an earlier run was forgotten"

    ghost_line = ghost.describe()
    assert "nobody watching" in ghost_line, ghost_line
    assert "rc=" not in ghost_line, ghost_line
    assert not ghost.alive()

    # a process started by an earlier run is still listed, still alive, and still
    # owns its name: the duplicate-name guard reads liveness from the pid
    manager = _manager(tmp_path / "restart")
    started = await manager.start("sleeper", "sleep 300")
    later = _manager(tmp_path / "restart")
    try:
        found = later.get("sleeper")
        assert found is not None, "the new manager lost a process the old one started"
        assert found.pid == started.pid
        assert found.alive(), found.describe()
        assert found.describe().startswith("running (started by an earlier run)")

        with pytest.raises(AlreadyRunning):
            await later.start("sleeper", "sleep 300")
    finally:
        await _reap(manager, later)

    # ...and the orphan can be stopped after a restart, with an honest report that
    # its exit code was never ours to collect
    manager = _manager(tmp_path / "orphan")
    started = await manager.start("orphan", "sleep 300")
    later = _manager(tmp_path / "orphan")
    try:
        ok, detail = await later.stop("orphan")
        assert ok, detail
        assert not await _alive(started), "the orphan survived the stop"
        assert "not ours to collect" in detail, detail
    finally:
        await _reap(manager, later)


async def test_a_pid_is_only_signalled_when_it_is_still_the_process_we_started(
    tmp_path,
) -> None:
    # a recycled pid — or one with no fingerprint — is never signalled
    stranger = await asyncio.create_subprocess_exec(
        "sleep", "300", stdout=asyncio.subprocess.DEVNULL
    )
    try:
        path = tmp_path / "recycled" / "state" / "processes.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        for label, ticks in (("recycled", "1"), ("unvouched", "")):
            path.write_text(
                json.dumps(
                    {
                        label: {
                            "name": label,
                            "command": "sleep 300",
                            "pid": stranger.pid,
                            "started_at": time.time() - 60,
                            "ticks": ticks,
                            "ended_at": None,
                            "exit_code": None,
                        }
                    }
                ),
                encoding="utf-8",
            )
            ok, detail = await _manager(tmp_path / "recycled").stop(label)
            assert not ok, detail
            assert "not running" in detail, detail
            assert stranger.returncode is None, f"{label}: signalled a process that was not ours"
    finally:
        stranger.kill()
        await stranger.wait()

    # a second process under one name is refused, and refused is not replaced
    manager = _manager(tmp_path / "twin")
    started = await manager.start("twin", "sleep 300")
    try:
        with pytest.raises(AlreadyRunning) as caught:
            await manager.start("twin", "sleep 300")
        assert "already running" in str(caught.value)
        assert manager.get("twin").pid == started.pid
    finally:
        await _reap(manager)

    # a name whose process is gone is a slot, not a tombstone
    manager = _manager(tmp_path / "slot")
    first = await manager.start("disposable", "exit 0")
    deadline = asyncio.get_event_loop().time() + 5
    while first.ended_at is None and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert first.ended_at is not None, "the ending was never recorded"

    again = await manager.start("disposable", "sleep 300")
    try:
        assert again.pid != first.pid
        assert again.alive()
    finally:
        await _reap(manager)


async def test_stopping_a_process_stops_what_it_started(tmp_path) -> None:
    """Killing the shell is not stopping the work: the tree goes together."""
    manager = _manager(tmp_path / "tree")
    child_file = tmp_path / "grandchild.pid"
    await manager.start("parent", f"sh -c 'sleep 300 & echo $! > {child_file}; wait'")
    try:
        deadline = time.monotonic() + 5
        while not child_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert child_file.exists(), "the child was never started"
        child = int(child_file.read_text().strip())

        ok, detail = await manager.stop("parent")
        assert ok, detail

        gone = time.monotonic() + 3
        while _fingerprint(child) and time.monotonic() < gone:
            await asyncio.sleep(0.05)
        assert not _fingerprint(child), f"child {child} outlived the stop"
    finally:
        await _reap(manager)


async def test_the_registry_is_readable_to_a_person_and_the_tool_says_what_happened(
    tmp_path, runtime
) -> None:
    # the file on disk is JSON a person can read
    manager = _manager(tmp_path / "readable")
    managed = await manager.start("readable", "echo hi && sleep 300")
    try:
        row = json.loads(manager.path.read_text(encoding="utf-8"))["readable"]
        assert row["command"] == "echo hi && sleep 300"
        assert row["pid"] == managed.pid
        assert row["ticks"] and row["ticks"] == managed.ticks
        assert row["ended_at"] is None
    finally:
        await _reap(manager)

    # history is bounded, old endings give way, and a live process is never evicted
    manager = _manager(tmp_path / "bounded")
    live = await manager.start("keeper", "sleep 300")
    try:
        for i in range(60):
            manager._procs[f"old{i}"] = Managed(
                name=f"old{i}",
                command="true",
                pid=1,
                started_at=1000 + i,
                ticks="1",
                ended_at=1000 + i,
                exit_code=0,
            )
        manager._save()

        data = json.loads(manager.path.read_text(encoding="utf-8"))
        assert "keeper" in data, "a running process was evicted to make room for history"
        assert len(data) <= 51, f"history was not bounded: {len(data)} rows"
        assert "old59" in data and "old0" not in data
        assert manager.get("keeper").pid == live.pid
    finally:
        await _reap(manager)

    # through the tool, because that sentence is what the model and owner read
    started = await ProcessTool().run(
        {"op": "start", "name": "probe", "command": "echo starting && sleep 300"}, runtime
    )
    assert started.ok, started.output
    assert "pid" in started.output
    try:
        listed = await ProcessTool().run({"op": "list", "name": "probe", "command": ""}, runtime)
        assert "running" in listed.output, listed.output
        assert "probe" in listed.output

        logs = await ProcessTool().run({"op": "logs", "name": "probe", "command": ""}, runtime)
        deadline = asyncio.get_event_loop().time() + 5
        while "starting" not in logs.output and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
            logs = await ProcessTool().run({"op": "logs", "name": "probe", "command": ""}, runtime)
        assert "starting" in logs.output, logs.output

        # a second one under the same name is refused, and the refusal is readable
        twice = await ProcessTool().run(
            {"op": "start", "name": "probe", "command": "sleep 300"}, runtime
        )
        assert not twice.ok
        assert "already running" in twice.output

        stopped = await ProcessTool().run({"op": "stop", "name": "probe", "command": ""}, runtime)
        assert stopped.ok, stopped.output
        assert "stopped" in stopped.output

        # stopping it twice is not a second stop, and says so
        again = await ProcessTool().run({"op": "stop", "name": "probe", "command": ""}, runtime)
        assert not again.ok
        assert "not running" in again.output and "exited at" in again.output

        unknown = await ProcessTool().run({"op": "stop", "name": "nothing", "command": ""}, runtime)
        assert not unknown.ok
        assert "nothing has been started" in unknown.output
    finally:
        await _reap(runtime.processes)
