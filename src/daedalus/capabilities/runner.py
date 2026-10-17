"""Capability runner — child process, own venv, resource caps (ARCHITECTURE §9.3).

Protocol: JSON in on stdin (``{args, context, state}``), JSON out on stdout
(``{result, state_next, notifications}``). The manifest is the only scope
available.

The caps are enforced in two places, and the split is deliberate — SPEC §13 warns
that a *rushed* `RLIMIT_AS` kills healthy Python processes:

- **`mem_mb` is enforced on resident memory**, by watching ``/proc/<pid>/status``
  while the artifact runs. Resident is what the owner means by "512 MB"; it cannot
  kill a process for *reserving* address space it never touches; and the breach can
  be quoted afterwards. This is the cap.
- **`RLIMIT_AS` is a runaway backstop only**, set generously by
  :meth:`~.manifest.Caps.backstop_mb` so that one enormous allocation cannot land
  between two samples. It is not the declared cap, and must not be tightened to it.
- **`cpu` and `time_s`** are set in the child itself, before the artifact's code is
  exec'd, so they survive the exec and are inherited by anything it spawns.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..security.env import scrubbed
from .manifest import Caps, Manifest

MAX_OUTPUT = 256 * 1024

#: How often the runner looks at the child's resident memory. Far below the time it
#: takes a capability to do anything useful, and far above the cost of reading one
#: small file.
_SAMPLE_S = 0.1

#: The child is started as ``python -c <this>`` and then `exec`s the artifact, so
#: the limits are set in the very process that runs the code and survive the exec.
#: Deliberately *not* `preexec_fn`: that runs between fork and exec in a process
#: that may have threads, which Python documents as unsafe, and this daemon has them
#: (the dashboard server).
_LAUNCH = """
import os, resource, sys
backstop_mb, cpu, entry = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
cores = os.cpu_count() or 1
share = max(1, min(cores, int(cpu)))   # whole cores, rounded down: a cap is not exceeded
try:
    os.sched_setaffinity(0, set(range(share)))
except (AttributeError, OSError):
    pass
limit = backstop_mb * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
try:
    os.execv(sys.executable, [sys.executable, entry])
except OSError as exc:
    sys.stderr.write("capability launcher: cannot start %s: %s\\n" % (entry, exc))
    raise SystemExit(127)
"""


@dataclass(slots=True)
class RunResult:
    ok: bool = False
    exit_code: int = 0
    result: Any = None
    state_next: Any = None
    notifications: list[Any] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    duration_s: float = 0.0
    #: The highest resident memory the artifact reached, in MB. Kept whatever the
    #: outcome, because "it used 480 of its 512 MB" is the fact that stops the next
    #: question being "was that close or was that nothing".
    peak_mb: float = 0.0


@dataclass(slots=True)
class _Watch:
    """What the sampler saw, so the run can report it once the child is gone."""

    peak_mb: float = 0.0
    breach: str = ""


def _rss_mb(pid: int) -> float:
    """One process's resident memory in MB — 0.0 once it is gone."""
    try:
        with open(f"/proc/{pid}/status", "rb") as handle:
            for line in handle:
                if line.startswith(b"VmRSS:"):
                    return int(line.split()[1]) / 1024  # kB -> MB
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Stop the child **and anything it started**.

    The child is launched in its own session, so it leads a process group and its
    helpers are in it. Killing only the pid leaves them running, still carrying the
    caps they inherited — a timed-out capability with a live child is the whole
    failure the cap exists to prevent, one level down.

    The guard is about pid reuse, not politeness: once the child has been reaped its
    pid is free, and `killpg` on a recycled number would signal a group that has
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


def _last_line(text: str) -> str:
    for line in reversed(text.strip().splitlines()):
        if line.strip():
            return line.strip()[:400]
    return ""


class CapabilityRunner:
    def __init__(self, root: Path, *, uv: str = "uv", use_venv: bool = True) -> None:
        self.root = root
        self.uv = uv
        self.use_venv = use_venv

    def cap_dir(self, manifest: Manifest) -> Path:
        return self.root / manifest.name

    def venv_python(self, manifest: Manifest) -> Path:
        return self.cap_dir(manifest) / ".venv" / "bin" / "python"

    async def ensure_env(self, manifest: Manifest) -> Path:
        """Create the capability's venv and install deps. Returns the python to use.

        Every capability gets its own venv, even one with no declared deps. A
        capability that ran on the host interpreter could import the daemon's own
        installed packages, so what it can reach would stop being described by its
        manifest — and the first dep it gains would resolve against a shared
        environment. Isolation is the point, not a side effect of having deps.
        """
        if not self.use_venv:
            return Path(sys.executable)
        cap_dir = self.cap_dir(manifest)
        venv = cap_dir / ".venv"
        python = venv / "bin" / "python"
        if not python.exists():
            await _sh([self.uv, "venv", str(venv)], cwd=cap_dir, timeout=180)
        if manifest.deps:
            await _sh(
                [self.uv, "pip", "install", "--python", str(python), *manifest.deps],
                cwd=cap_dir,
                timeout=600,
            )
        return python

    async def run(
        self,
        manifest: Manifest,
        *,
        args: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> RunResult:
        cap_dir = self.cap_dir(manifest)
        entry = cap_dir / manifest.entry
        if not entry.exists():
            return RunResult(ok=False, error=f"entry not found: {entry}")
        try:
            python = await self.ensure_env(manifest)
        except Exception as exc:  # noqa: BLE001
            return RunResult(ok=False, error=f"env setup failed: {exc}")

        payload = json.dumps({"args": args or {}, "context": context or {}, "state": state or {}})
        started = asyncio.get_event_loop().time()
        proc = await asyncio.create_subprocess_exec(
            str(python),
            "-c",
            _LAUNCH,
            str(manifest.caps.backstop_mb()),
            str(manifest.caps.cpu),
            str(entry),
            cwd=str(cap_dir),
            env=scrubbed(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Its own session, so a cap breach or a timeout can take the whole tree
            # rather than orphaning whatever the artifact spawned.
            start_new_session=True,
        )
        watch = _Watch()
        watcher = asyncio.create_task(self._watch(proc, manifest.caps, watch))
        try:
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(input=payload.encode()), timeout=manifest.caps.time_s
                )
            except TimeoutError:
                await _kill(proc)
                return RunResult(
                    ok=False,
                    error=f"timed out after {manifest.caps.time_s}s",
                    duration_s=asyncio.get_event_loop().time() - started,
                    peak_mb=watch.peak_mb,
                )
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        duration = asyncio.get_event_loop().time() - started
        stdout = out.decode("utf-8", "replace")[:MAX_OUTPUT]
        stderr = err.decode("utf-8", "replace")[:MAX_OUTPUT]
        result = RunResult(
            ok=(proc.returncode == 0),
            exit_code=proc.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration,
            peak_mb=watch.peak_mb,
        )
        if watch.breach:
            # We stopped it, so its own output says nothing about why: the sentence
            # naming the cap is the only true account of this run.
            result.ok = False
            result.error = watch.breach
            return result
        if result.exit_code < 0:
            result.ok = False
            result.error = f"killed by signal {-result.exit_code}"
            return result
        parsed = _parse_payload(stdout)
        if parsed is None:
            if result.ok:
                result.error = "stdout was not valid JSON"
                result.ok = False
            elif not result.error:
                # It already failed; say what it said, rather than blaming the
                # protocol for output that was never coming.
                result.error = _last_line(stderr) or f"exit {result.exit_code}"
            return result
        result.result = parsed.get("result")
        result.state_next = parsed.get("state_next")
        result.notifications = list(parsed.get("notifications", []) or [])
        return result

    async def _watch(self, proc: asyncio.subprocess.Process, caps: Caps, watch: _Watch) -> None:
        """Stop the artifact the moment its *resident* memory passes the cap.

        What this cannot see, and does not pretend to: memory held by a helper the
        artifact spawned (the cap is the artifact's), and an allocation big enough
        to land between two samples — which is what the generous `RLIMIT_AS`
        backstop is for.
        """
        while proc.returncode is None:
            watch.peak_mb = max(watch.peak_mb, _rss_mb(proc.pid))
            if watch.peak_mb > caps.mem_mb:
                watch.breach = (
                    f"hit its mem_mb={caps.mem_mb} cap: resident memory reached "
                    f"{watch.peak_mb:.0f} MB"
                )
                await _kill(proc)
                return
            await asyncio.sleep(_SAMPLE_S)

    def read_state(self, manifest: Manifest) -> dict[str, Any]:
        path = self.cap_dir(manifest) / "state" / "state.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def write_state(self, manifest: Manifest, state: Any) -> None:
        if state is None:
            return
        state_dir = self.cap_dir(manifest) / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def _parse_payload(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # tolerate leading/trailing noise: take the last {...}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


async def _sh(cmd: list[str], *, cwd: Path, timeout: int) -> tuple[int, str]:
    if shutil.which(cmd[0]) is None:
        raise RuntimeError(f"{cmd[0]} not found on PATH")
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode or 0, out.decode("utf-8", "replace")
