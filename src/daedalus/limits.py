"""Resource-limit enforcement, shared by every child the agent supervises (T19, T30).

Two things run the agent's code in a child of its own and owe both of them the same
three bounds: an **authored capability** (``capabilities/runner.py``) and a
**delegation** (``delegate/runner.py``). Writing the mechanism twice is how the two
drift, so the launcher, the resident-memory sample and the tree kill live here and
both callers import them.

The split is the one SPEC §13 names, and it is deliberate:

- the declared ``mem_mb`` is enforced on **resident** memory, by sampling
  ``/proc/<pid>/status`` while the child runs — address space is not memory, and a
  tight ``RLIMIT_AS`` kills a healthy CPython for reserving what it never touches;
- a generous ``RLIMIT_AS`` stays only as a **backstop** for one allocation large
  enough to land between two samples;
- ``cpu`` is CPU affinity over whole cores and ``time_s`` is a wall-clock ceiling,
  both set in the child itself (via a stdlib launcher that ``exec``s the real
  program) so they survive the exec and are inherited by anything it spawns.

The child leads its own session, so killing it takes the whole tree: a killed shell
does not take the command it was running with it, and a "stopped" process that is
still working is the same lie as an unreported death.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

#: How often the sampler looks at a child's resident memory. Far below the time it
#: takes anything useful to run, and far above the cost of reading one small file.
SAMPLE_S = 0.1

#: ``python -c <this> <backstop_mb> <cpu> <argv…>`` — sets the limits, then becomes
#: the program. Deliberately *not* ``preexec_fn``: that runs between fork and exec
#: in a process that may have threads, which Python documents as unsafe, and this
#: daemon has them (the dashboard server).
LAUNCH = """
import os, resource, sys
backstop_mb, cpu = float(sys.argv[1]), float(sys.argv[2])
cores = os.cpu_count() or 1
share = max(1, min(cores, int(cpu)))   # whole cores, rounded down: a cap is not exceeded
try:
    os.sched_setaffinity(0, set(range(share)))
except (AttributeError, OSError):
    pass
limit = int(backstop_mb) * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
argv = sys.argv[3:]
try:
    os.execvp(argv[0], argv)
except OSError as exc:
    # A .py entry the kernel refuses to exec is a *Python source file* — no
    # exec bit (a capability written as 0644 before authoring chmods, T41) or
    # no shebang (plain Python). Both are normal shapes for the artifact
    # contract, so it runs as Python through the interpreter, limits intact;
    # anything else reports the real error and exits 127.
    if argv[0].endswith(".py"):
        try:
            os.execv(sys.executable, [sys.executable, *argv])
        finally:
            pass
    sys.stderr.write("limits: cannot start %s: %s\\n" % (argv[0], exc))
    raise SystemExit(127)
"""


@dataclass(slots=True)
class Watch:
    """What the sampler saw, so a run can report it once the child is gone."""

    peak_mb: float = 0.0
    breach: str = ""


async def watch_caps(proc: asyncio.subprocess.Process, *, mem_mb: int, watch: Watch | None = None) -> Watch:
    """Stop the child the moment its *resident* memory passes ``mem_mb``.

    Shared by every caller that owes a child the same cap (the extraction's
    point): a capability, a delegation. What this cannot see, and does not
    pretend to: memory held by a helper whose pages the parent's ``/proc`` entry
    does not carry, and an allocation big enough to land between two samples —
    which is what the generous ``RLIMIT_AS`` backstop is for.
    """
    watch = watch or Watch()
    while proc.returncode is None:
        watch.peak_mb = max(watch.peak_mb, rss_mb(proc.pid))
        if watch.peak_mb > mem_mb:
            watch.breach = (
                f"hit its mem_mb={mem_mb} cap: resident memory reached {watch.peak_mb:.0f} MB"
            )
            await kill_tree(proc)
            return watch
        await asyncio.sleep(SAMPLE_S)
    return watch


def rss_mb(pid: int) -> float:
    """One process's resident memory in MB — 0.0 once it is gone.

    Resident, not address space: this is the number the owner means by "512 MB",
    and it is the one the cap is quoted against afterwards.
    """
    try:
        with open(f"/proc/{pid}/status", "rb") as handle:
            for line in handle:
                if line.startswith(b"VmRSS:"):
                    return int(line.split()[1]) / 1024  # kB -> MB
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Stop the child **and anything it started**.

    The child is launched in its own session, so it leads a process group and its
    helpers are in it. Killing only the pid leaves them running, still carrying the
    caps they inherited — the whole failure the cap exists to prevent, one level
    down.

    The guard is about pid reuse, not politeness: once the child has been reaped its
    pid is free, and ``killpg`` on a recycled number would signal a group that has
    nothing to do with us.
    """
    if proc.returncode is None and Path(f"/proc/{proc.pid}").exists():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()
