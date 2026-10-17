"""Long-running processes: an ending is visible, and the record outlives the daemon (T20).

The failure this ticket is about is a looked-for absence. A process that dies
leaves `rc=` set and nothing else, so the only way to learn *when* it died and
*with what* was to poll `list`; and because the registry was a dict on the manager,
a daemon restart did not just lose the history — it lost the processes, which kept
running as orphans with nothing able to list, read, or stop them.

So the tests come at it from both ends: a death has to be readable without asking,
and a record written by a previous run has to (a) still be listed, (b) have its
liveness asked of the real pid, and (c) never be used to signal a pid that has since
been given to something else.
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


async def test_a_process_that_exits_is_reported_without_asking(tmp_path) -> None:
    manager = _manager(tmp_path)
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


async def test_a_death_before_the_daemon_restarted_is_still_a_death(tmp_path) -> None:
    """The registry says what it knows — and says so when it does not know."""
    path = tmp_path / "state" / "processes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # A row whose process is long gone, as a hard kill would leave it: started, never
    # ended, and its pid since recycled or freed. Reported as exactly that.
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

    manager = _manager(tmp_path)
    ghost = manager.get("ghost")
    assert ghost is not None, "a record written by an earlier run was forgotten"

    line = ghost.describe()
    assert "nobody watching" in line, line
    assert "rc=" not in line, line
    assert not ghost.alive()


async def test_a_process_started_before_a_restart_is_still_listed_and_alive(tmp_path) -> None:
    """The daemon forgets; the pid does not."""
    manager = _manager(tmp_path)
    started = await manager.start("sleeper", "sleep 300")

    later = _manager(tmp_path)
    try:
        found = later.get("sleeper")
        assert found is not None, "the new manager lost a process the old one started"
        assert found.pid == started.pid
        assert found.alive(), found.describe()
        assert found.describe().startswith("running (started by an earlier run)")

        # And it can still be reached: the duplicate-name guard reads liveness from
        # the pid, so the new manager refuses to start a second one under the name.
        with pytest.raises(AlreadyRunning):
            await later.start("sleeper", "sleep 300")
    finally:
        await _reap(manager, later)


async def test_a_process_can_be_stopped_after_a_restart(tmp_path) -> None:
    manager = _manager(tmp_path)
    started = await manager.start("orphan", "sleep 300")

    later = _manager(tmp_path)
    try:
        ok, detail = await later.stop("orphan")
        assert ok, detail
        assert not await _alive(started), "the orphan survived the stop"
        # Its code was never ours to collect, and the report says so rather than
        # inventing a number.
        assert "not ours to collect" in detail, detail
    finally:
        await _reap(manager, later)


async def test_stopping_a_recycled_pid_would_be_a_killing_of_strangers(tmp_path) -> None:
    """A pid without a fingerprint is never signalled.

    A live process stands in for "whatever holds the number now": its pid is written
    into the registry with the *wrong* start time, which is exactly what a recycled
    pid looks like.
    """
    stranger = await asyncio.create_subprocess_exec(
        "sleep", "300", stdout=asyncio.subprocess.DEVNULL
    )
    try:
        path = tmp_path / "state" / "processes.json"
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
            manager = _manager(tmp_path)
            ok, detail = await manager.stop(label)
            assert not ok, detail
            assert "not running" in detail, detail
            assert stranger.returncode is None, f"{label}: signalled a process that was not ours"
    finally:
        stranger.kill()
        await stranger.wait()


async def test_a_second_process_under_one_name_is_refused(tmp_path) -> None:
    manager = _manager(tmp_path)
    started = await manager.start("twin", "sleep 300")
    try:
        with pytest.raises(AlreadyRunning) as caught:
            await manager.start("twin", "sleep 300")
        assert "already running" in str(caught.value)
        # Refused, not replaced: the first is still the one the registry knows.
        assert manager.get("twin").pid == started.pid
    finally:
        await _reap(manager)


async def test_stopping_a_process_stops_what_it_started(tmp_path) -> None:
    """Killing the shell is not stopping the work: the tree goes together.

    The child's pid is written to a file by the outer process, so the assertion is
    about a *specific* process rather than "nothing named sleep is left running".
    """
    manager = _manager(tmp_path)
    child_file = tmp_path / "grandchild.pid"
    managed = await manager.start(
        "parent", f"sh -c 'sleep 300 & echo $! > {child_file}; wait'"
    )
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


async def test_a_dead_name_can_be_used_again(tmp_path) -> None:
    """A name is a slot, not a tombstone: reusing one whose process is gone is normal."""
    manager = _manager(tmp_path)
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


async def test_the_registry_is_a_file_a_person_can_read(tmp_path) -> None:
    manager = _manager(tmp_path)
    managed = await manager.start("readable", "echo hi && sleep 300")
    try:
        data = json.loads(manager.path.read_text(encoding="utf-8"))
        row = data["readable"]
        assert row["command"] == "echo hi && sleep 300"
        assert row["pid"] == managed.pid
        assert row["ticks"] and row["ticks"] == managed.ticks
        assert row["ended_at"] is None
    finally:
        await _reap(manager)


async def test_history_is_bounded_but_live_processes_are_not_evicted(tmp_path) -> None:
    """The file stays readable: old endings give way, running processes never do."""
    manager = _manager(tmp_path)
    live = await manager.start("keeper", "sleep 300")
    try:
        for i in range(60):
            row = Managed(
                name=f"old{i}",
                command="true",
                pid=1,
                started_at=1000 + i,
                ticks="1",
                ended_at=1000 + i,
                exit_code=0,
            )
            manager._procs[row.name] = row
        manager._save()

        data = json.loads(manager.path.read_text(encoding="utf-8"))
        assert "keeper" in data, "a running process was evicted to make room for history"
        assert len(data) <= 51, f"history was not bounded: {len(data)} rows"
        # Newest endings survive; the oldest are the ones dropped.
        assert "old59" in data and "old0" not in data
        assert manager.get("keeper").pid == live.pid
    finally:
        await _reap(manager)


async def test_the_tool_says_which_of_the_three_things_happened(runtime) -> None:
    """Through the tool, because that sentence is what the model and owner read."""
    started = await ProcessTool().run(
        {"op": "start", "name": "probe", "command": "echo starting && sleep 300"}, runtime
    )
    assert started.ok, started.output
    assert "pid" in started.output
    manager = runtime.processes
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

        # A second one under the same name is refused, and the refusal is readable.
        twice = await ProcessTool().run(
            {"op": "start", "name": "probe", "command": "sleep 300"}, runtime
        )
        assert not twice.ok
        assert "already running" in twice.output

        stopped = await ProcessTool().run({"op": "stop", "name": "probe", "command": ""}, runtime)
        assert stopped.ok, stopped.output
        assert "stopped" in stopped.output

        # Stopping it twice is not a second stop, and says so.
        again = await ProcessTool().run({"op": "stop", "name": "probe", "command": ""}, runtime)
        assert not again.ok
        assert "not running" in again.output and "exited at" in again.output

        unknown = await ProcessTool().run({"op": "stop", "name": "nothing", "command": ""}, runtime)
        assert not unknown.ok
        assert "nothing has been started" in unknown.output
    finally:
        await _reap(manager)
