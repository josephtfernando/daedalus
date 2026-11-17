"""Resource caps that actually bite (T19, ARCHITECTURE §9.3, SPEC §13).

Every manifest declares `cpu / mem_mb / time_s`. There are three ways to get the
enforcement wrong, and each one is held here:

- **recorded and never applied** — then the artifact's promise is fiction;
- **applied as `RLIMIT_AS`** — which kills a healthy CPython for *reserving*
  address space it never touches, the trap SPEC §13 names;
- **applied to the child only** — so a capability that spawns help and then hangs
  leaves the helpers behind, still carrying the caps they were started with.

The runs use the *test* interpreter (`use_venv=False`): the launcher is stdlib-only,
so a per-capability venv would cost a second a case and test nothing about caps.
"""

from __future__ import annotations

import os
import time

import pytest

from daedalus.capabilities.manifest import Caps, Manifest
from daedalus.capabilities.runner import CapabilityRunner
from daedalus.tools.capability import CapabilityTool

#: Echoes its args and reports the cores it can actually see — the observable
#: proof that the `cpu` cap reached the artifact.
ECHO = (
    "import json, os, sys\n"
    "payload = json.load(sys.stdin)\n"
    "cores = len(os.sched_getaffinity(0))\n"
    "json.dump({'result': {'args': payload['args'], 'cores': cores}, 'state_next': {}}, sys.stdout)\n"
)

#: Allocates and *touches* about 4 MB every 5 ms, forever.
RUNAWAY = (
    "import json, sys, time\n"
    "blocks = []\n"
    "while True:\n"
    "    block = bytearray(4 * 1024 * 1024)\n"
    "    for i in range(0, len(block), 4096):\n"
    "        block[i] = 1\n"
    "    blocks.append(block)\n"
    "    time.sleep(0.005)\n"
)

#: Sleeps past any timeout, leaving a child of its own behind.
HANGS_WITH_A_CHILD = (
    "import pathlib, subprocess, time\n"
    "child = subprocess.Popen(['sleep', '300'])\n"
    "pathlib.Path('child.pid').write_text(str(child.pid))\n"
    "time.sleep(300)\n"
)

#: Exits by raising, so there is a real cause in stderr and no JSON on stdout.
CRASHES = "raise ValueError('the database is gone')\n"


def _allocate(mb: int, *, hold_s: float) -> str:
    """A run.py that touches `mb` of memory, holds it, then reports.

    Touching matters. `bytearray(n)` for a large n can come back as untouched
    copy-on-write zero pages, so a program that "allocated" 200 MB may have a
    resident footprint of nothing — and resident is what the cap is on. Writing one
    byte per page is what makes the number real. Holding matters too: a capability
    that spikes and exits inside one sampling interval is not the failure the cap
    is for, and is measured by the `RLIMIT_AS` backstop instead.
    """
    return (
        "import json, sys, time\n"
        f"block = bytearray({mb} * 1024 * 1024)\n"
        "for i in range(0, len(block), 4096):\n"
        "    block[i] = 1\n"
        f"time.sleep({hold_s})\n"
        "json.dump({'result': {'mb': len(block) // (1024 * 1024)}, 'state_next': {}}, sys.stdout)\n"
    )


def _cap(
    tmp_path,
    name: str,
    run_py: str,
    *,
    cpu: float = 1.0,
    mem_mb: int = 512,
    time_s: int = 120,
) -> tuple[CapabilityRunner, Manifest]:
    root = tmp_path / "capabilities"
    manifest = Manifest(name=name, caps=Caps(cpu=cpu, mem_mb=mem_mb, time_s=time_s))
    manifest.save(root / name)
    entry = root / name / "run.py"
    entry.write_text(run_py, encoding="utf-8")
    # The launcher execs the entry directly, caps surviving the exec — the
    # artifact contract is "an executable file", and authoring.py delivers it
    # (T41). A test writing its own artifact owes the same contract.
    entry.chmod(0o755)
    return CapabilityRunner(root, use_venv=False), manifest


async def test_a_breach_or_a_hang_is_stopped_and_the_declared_cap_is_named(tmp_path) -> None:
    # a runaway is stopped by resident memory, and the cap is named
    runner, manifest = _cap(tmp_path, "runaway", RUNAWAY, mem_mb=64)
    result = await runner.run(manifest)

    assert not result.ok
    assert "mem_mb=64" in result.error, result.error
    assert "resident memory reached" in result.error, result.error
    assert result.peak_mb > 64, "the breach was not what stopped it"
    assert result.exit_code != 0

    # the number is the manifest's: one artifact has room at 512 MB, a wall at 64
    body = _allocate(200, hold_s=0.6)
    roomy_runner, roomy = _cap(tmp_path, "roomy", body, mem_mb=512)
    bare_runner, bare = _cap(tmp_path, "bare", body, mem_mb=64)

    fits = await roomy_runner.run(roomy)
    stopped = await bare_runner.run(bare)

    assert fits.ok, fits.error
    assert fits.result["mb"] == 200
    assert fits.peak_mb > 150, "nothing was measuring the memory the cap is about"
    assert not stopped.ok
    assert "mem_mb=64" in stopped.error

    # a hang is stopped by time_s, with the number named
    hang_runner, hang = _cap(tmp_path, "hang", "import time\ntime.sleep(300)\n", time_s=1)
    hung = await hang_runner.run(hang)
    assert not hung.ok
    assert hung.error == "timed out after 1s", hung.error

    # one inside its caps is untouched — and measured
    echo_runner, echo = _cap(tmp_path, "echo", ECHO)
    fine = await echo_runner.run(echo, args={"hello": "there"})
    assert fine.ok, fine.error
    assert fine.result["args"] == {"hello": "there"}
    assert fine.exit_code == 0
    assert fine.peak_mb > 0, "the run was not measured at all"

    # a crash reports its cause, not the missing JSON it left behind
    crash_runner, crash = _cap(tmp_path, "crasher", CRASHES)
    crashed = await crash_runner.run(crash)
    assert not crashed.ok
    assert "the database is gone" in crashed.error, crashed.error


async def test_a_stopped_run_takes_its_children_with_it(tmp_path) -> None:
    """A capability that spawns help and is stopped must not leave the help running."""
    # under a timeout...
    runner, manifest = _cap(tmp_path, "spawner", HANGS_WITH_A_CHILD, time_s=1)
    result = await runner.run(manifest)
    assert not result.ok

    pid_file = runner.cap_dir(manifest) / "child.pid"
    assert pid_file.exists(), "the artifact never got as far as spawning its child"
    child = int(pid_file.read_text())
    assert not _alive(child), f"child {child} outlived the timeout"

    # ...and under a breach, where the sampler is what stops it, not the clock
    body = (
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen(['sleep', '300'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n"
        "blocks = []\n"
        "while True:\n"
        "    block = bytearray(4 * 1024 * 1024)\n"
        "    for i in range(0, len(block), 4096):\n"
        "        block[i] = 1\n"
        "    blocks.append(block)\n"
        "    time.sleep(0.005)\n"
    )
    runner, manifest = _cap(tmp_path, "breeder", body, mem_mb=64)
    result = await runner.run(manifest)
    assert not result.ok

    pid_file = runner.cap_dir(manifest) / "child.pid"
    assert pid_file.exists(), "the artifact never got as far as spawning its child"
    child = int(pid_file.read_text())
    assert not _alive(child), f"child {child} outlived the breach"


async def test_cpu_is_a_whole_core_share_and_a_zero_cap_is_refused_where_it_is_read(
    tmp_path, runtime
) -> None:
    one_runner, one = _cap(tmp_path, "one", ECHO, cpu=1.0)
    three_runner, three = _cap(tmp_path, "three", ECHO, cpu=3.0)
    odd_runner, odd = _cap(tmp_path, "odd", ECHO, cpu=2.5)
    # A cap can narrow the cores a run sees, never widen them: on a two-core
    # machine a `cpu = 3` run sees two, and the assertion must say so rather
    # than pass only on the machine it was written on.
    cores = len(os.sched_getaffinity(0))

    assert (await one_runner.run(one)).result["cores"] == min(1, cores)
    assert (await three_runner.run(three)).result["cores"] == min(3, cores)
    # whole cores, rounded *down*: the next core up would hand out more than the
    # manifest declared, and a cap that is exceeded is not a cap
    assert (await odd_runner.run(odd)).result["cores"] == min(2, cores)

    # a zero cap stops every run; it does not bound one — say so where it is read
    with pytest.raises(ValueError, match="positive"):
        Caps.from_dict({"mem_mb": 0})
    with pytest.raises(ValueError, match="positive"):
        Caps.from_dict({"time_s": 0})
    # sub-core shares cannot be expressed as affinity, so they are refused rather
    # than silently rounded up
    with pytest.raises(ValueError, match="under one core"):
        Caps.from_dict({"cpu": 0.5})
    assert Caps.from_dict({"cpu": 2.0, "mem_mb": 256, "time_s": 30}).mem_mb == 256

    # the authoring model is held to the same rule
    result = await CapabilityTool().run(
        {"op": "author", "name": "instant_death", "run_py": ECHO, "mem_mb": 0},
        runtime,
    )
    assert not result.ok
    assert "positive" in result.output


def _alive(pid: int, *, within: float = 3.0) -> bool:
    """Whether `pid` is still around, allowing the kernel time to reap it."""
    deadline = time.monotonic() + within
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.05)
