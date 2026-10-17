"""Resource caps that actually bite (T19, ARCHITECTURE §9.3, SPEC §13).

Every manifest declares `cpu / mem_mb / time_s`. There are three ways to get the
enforcement wrong, and each one gets a test:

- **recorded and never applied** — then the artifact's promise is fiction, and both
  the model that wrote the manifest and the owner reading it believe a limit exists;
- **applied as `RLIMIT_AS`** — which kills a healthy CPython for *reserving* address
  space it never touches, the trap SPEC §13 names;
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
    (root / name / "run.py").write_text(run_py, encoding="utf-8")
    return CapabilityRunner(root, use_venv=False), manifest


async def test_a_runaway_is_stopped_and_the_cap_is_named(tmp_path) -> None:
    runner, manifest = _cap(tmp_path, "runaway", RUNAWAY, mem_mb=64)

    result = await runner.run(manifest)

    assert not result.ok
    assert "mem_mb=64" in result.error, result.error
    assert "resident memory reached" in result.error, result.error
    assert result.peak_mb > 64, "the breach was not what stopped it"
    assert result.exit_code != 0


async def test_the_number_comes_from_the_manifest_not_the_runner(tmp_path) -> None:
    """One artifact, two manifests: in one it has room, in the other a wall."""
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


async def test_a_capability_inside_its_caps_is_untouched(tmp_path) -> None:
    runner, manifest = _cap(tmp_path, "echo", ECHO)

    result = await runner.run(manifest, args={"hello": "there"})

    assert result.ok, result.error
    assert result.result["args"] == {"hello": "there"}
    assert result.exit_code == 0
    assert result.peak_mb > 0, "the run was not measured at all"


async def test_cpu_is_a_share_of_the_machine(tmp_path) -> None:
    if (os.cpu_count() or 1) < 3:
        pytest.skip("needs three cores for a share to be a share")

    one_runner, one = _cap(tmp_path, "one", ECHO, cpu=1.0)
    three_runner, three = _cap(tmp_path, "three", ECHO, cpu=3.0)
    odd_runner, odd = _cap(tmp_path, "odd", ECHO, cpu=2.5)

    assert (await one_runner.run(one)).result["cores"] == 1
    assert (await three_runner.run(three)).result["cores"] == 3
    # Whole cores, rounded *down*: granting the next core up would hand out more
    # than the manifest declared, and a cap that is exceeded is not a cap.
    assert (await odd_runner.run(odd)).result["cores"] == 2


async def test_a_hang_is_still_stopped_by_time_s(tmp_path) -> None:
    runner, manifest = _cap(tmp_path, "hang", "import time\ntime.sleep(300)\n", time_s=1)

    result = await runner.run(manifest)

    assert not result.ok
    assert result.error == "timed out after 1s", result.error


async def test_a_timeout_takes_the_children_too(tmp_path) -> None:
    """A capability that spawns help and hangs must not leave the help running."""
    runner, manifest = _cap(tmp_path, "spawner", HANGS_WITH_A_CHILD, time_s=1)

    result = await runner.run(manifest)
    assert not result.ok

    pid_file = runner.cap_dir(manifest) / "child.pid"
    assert pid_file.exists(), "the artifact never got as far as spawning its child"
    child = int(pid_file.read_text())
    assert not _alive(child), f"child {child} outlived the timeout"


async def test_a_breach_takes_the_children_too(tmp_path) -> None:
    """Same rule where the sampler is what stops it, not the clock."""
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


async def test_a_crash_reports_its_cause_not_the_protocol(tmp_path) -> None:
    """A failed run must say what went wrong, not blame the missing JSON."""
    runner, manifest = _cap(tmp_path, "crasher", CRASHES)

    result = await runner.run(manifest)

    assert not result.ok
    assert "the database is gone" in result.error, result.error


def test_a_cap_of_zero_is_refused_where_it_is_read() -> None:
    """A zero cap stops every run; it does not bound one. Say so, loudly."""
    with pytest.raises(ValueError, match="positive"):
        Caps.from_dict({"mem_mb": 0})
    with pytest.raises(ValueError, match="positive"):
        Caps.from_dict({"time_s": 0})
    # Sub-core shares cannot be expressed as affinity, and the smallest share that
    # *can* is one core — so the value is refused rather than silently rounded up.
    with pytest.raises(ValueError, match="under one core"):
        Caps.from_dict({"cpu": 0.5})
    # …and the refusals still allow the ordinary case.
    assert Caps.from_dict({"cpu": 2.0, "mem_mb": 256, "time_s": 30}).mem_mb == 256


async def test_the_authoring_model_must_declare_sane_caps(runtime) -> None:
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
