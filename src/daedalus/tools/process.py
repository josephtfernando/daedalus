"""``process`` primitive — start / stop / monitor long-running processes.

A process outlives the turn that started it, which means it also outlives the
*daemon* that started it. Two things follow, and both were missing:

- **An ending has to be visible.** ``rc=None`` in a list the owner has to ask for
  is not a report — it is the same value for "still running" and "died at 04:00",
  so the one question worth asking ("did it work?") could not be answered from the
  output. Every process now records when and how it ended, and the ending is said
  plainly.
- **The registry has to outlive the daemon.** It used to be a dict on the manager,
  so a restart forgot every process it had started: they kept running, unfindable
  and unstoppable, and the owner's only clue was a laptop behaving oddly.
  ``state/processes.json`` is the record instead — file-first and hand-readable,
  like the rest of the state.

What this deliberately does **not** do is restart a process that died. A process
that exits may be exiting for a reason, and a supervisor that silently restarts it
is how one crash becomes a loop; the ending is recorded and reported, and what to
do about it is the owner's (or the loop's) call. Processes are also still stopped
on a clean shutdown — the registry's job is to make whatever survives a *crash*
findable and stoppable, not to change who owns the process while the daemon lives.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from ..security.env import scrubbed
from . import guard
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime

#: How many *ended* processes the registry keeps. Live ones are always kept; this
#: only bounds the history, so the file stays something a person can read.
KEEP_ENDED = 50

#: How long a process gets to react to SIGTERM before it is killed outright.
GRACE_S = 10.0


def _fingerprint(pid: int) -> str:
    """`pid`'s start time from `/proc`, which is what makes a pid *this* process.

    A pid is not an identity, it is a number that gets recycled. Recording the pair
    (pid, start time) is what lets a registry row from a previous daemon say "that
    was me" without ever saying it about whatever holds the number now — the
    difference between stopping an orphan and killing a stranger.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        # `comm` can contain spaces and parentheses, so the fields start after the
        # *last* ')'. Field 3 is the first of them and field 22 is `starttime`.
        rest = stat[stat.rindex(")") + 1 :].split()
        return rest[19]
    except (OSError, ValueError, IndexError):
        return ""


def _is_alive(pid: int, ticks: str) -> bool:
    """Whether the process we started is still there.

    An empty fingerprint means the pid could not be vouched for, so the answer is
    no: an entry that might be a stranger is never called ours, and is never
    signalled.
    """
    return pid > 0 and bool(ticks) and _fingerprint(pid) == ticks


def _signal(pid: int, sig: int) -> bool:
    """Signal the process **and its group**, falling back to the process alone.

    A process is started in its own session, so it leads a group and the usual call
    takes the whole tree — which matters, because killing a shell does not take the
    command it was running with it. The fallback is not a guess: a row from a
    hand-edited file may not lead a group at all, and a plain signal is then the
    only true thing to do.
    """
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        try:
            os.kill(pid, sig)
            return True
        except ProcessLookupError:
            return False
    except PermissionError:
        return False


class AlreadyRunning(RuntimeError):
    """Raised instead of replacing a live process under the same name.

    Two processes under one name is worse than an error: the second replaces the
    first in the registry, so the first keeps running with nothing able to list,
    read or stop it.
    """

    def __init__(self, managed: "Managed") -> None:
        since = time.strftime("%H:%M", time.localtime(managed.started_at))
        super().__init__(
            f"{managed.name!r} is already running (pid {managed.pid}, since {since}). Stop it "
            "first, or start this one under a different name — a second process under the same "
            "name would leave the first running with nothing able to reach it."
        )


@dataclass
class Managed:
    """One process, as the registry remembers it — live or long gone."""

    name: str
    command: str
    pid: int
    started_at: float = field(default_factory=time.time)
    ticks: str = ""
    ended_at: float | None = None
    exit_code: int | None = None
    log: list[str] = field(default_factory=list)
    #: The live handle. None for a process this daemon did not start (it outlived
    #: the run that did), which is why liveness is asked of the pid and not of this.
    proc: asyncio.subprocess.Process | None = None
    _task: asyncio.Task | None = None
    #: For an adopted child (a delegation): run when the registry records its
    #: ending, so the completion is announced rather than only askable. Failures
    #: in it are the caller's to log — never fatal.
    on_end: Any = None

    def alive(self) -> bool:
        return self.ended_at is None and _is_alive(self.pid, self.ticks)

    def describe(self) -> str:
        """One honest line — never a bare `rc=None`.

        The three states are genuinely different things and the old list collapsed
        two of them: *running*, *exited* (with a code and a time), and *gone with
        nobody watching* (a daemon restart or a hard kill, so the exit code was
        never ours to collect).
        """
        if self.ended_at is not None:
            at = time.strftime("%H:%M:%S", time.localtime(self.ended_at))
            code = "exit code unknown" if self.exit_code is None else f"rc={self.exit_code}"
            state = f"exited at {at} ({code})"
        elif _is_alive(self.pid, self.ticks):
            state = "running" if self.proc is not None else "running (started by an earlier run)"
        else:
            state = "ended with nobody watching — its exit code was never collected"
        output = f"{len(self.log)} lines" if self.proc is not None else "output not kept"
        return f"{state}, {output}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "pid": self.pid,
            "started_at": self.started_at,
            "ticks": self.ticks,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
        }


class ProcessManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._procs: dict[str, Managed] = self._load()

    # -- the registry ------------------------------------------------------

    def _load(self) -> dict[str, Managed]:
        """Read what previous runs left behind. A broken file is not fatal.

        Nothing is reconciled here: whether a row's process is still alive is asked
        of the pid every time it is needed, because the pid is the only thing that
        knows and it can change between two reads.
        """
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, Managed] = {}
        for key, row in data.items():
            if not isinstance(row, dict):
                continue
            try:
                managed = Managed(
                    name=str(row.get("name") or key),
                    command=str(row.get("command", "")),
                    pid=int(row.get("pid", 0)),
                    started_at=float(row.get("started_at", 0.0)),
                    ticks=str(row.get("ticks", "")),
                    ended_at=(
                        None if row.get("ended_at") is None else float(row["ended_at"])
                    ),
                    exit_code=(
                        None if row.get("exit_code") is None else int(row["exit_code"])
                    ),
                )
            except (TypeError, ValueError):
                continue
            out[managed.name] = managed
        return out

    def _save(self) -> None:
        """Rewrite the registry whole. It is small, and one writer means no merge."""
        live = [m for m in self._procs.values() if m.ended_at is None]
        ended = sorted(
            (m for m in self._procs.values() if m.ended_at is not None),
            key=lambda m: m.ended_at or 0.0,
            reverse=True,
        )[:KEEP_ENDED]
        keep = {m.name for m in live + ended}
        self._procs = {name: m for name, m in self._procs.items() if name in keep}
        ordered = sorted(self._procs.values(), key=lambda m: m.started_at, reverse=True)
        payload = {m.name: m.to_dict() for m in ordered}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError:
            # A process that is running must not be lost because a file could not be
            # written; the registry is a record of it, not the handle to it.
            pass

    # -- lifecycle ---------------------------------------------------------

    async def start(
        self, name: str, command: str, cwd: str | None = None, env: dict[str, str] | None = None
    ) -> Managed:
        existing = self._procs.get(name)
        if existing is not None and existing.alive():
            raise AlreadyRunning(existing)
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            executable="/bin/bash",
            # Its own session, so the whole tree can be stopped as one thing. A shell
            # that is killed does not take its children with it, and a "stopped"
            # process that is still working is the same lie as an unreported death.
            start_new_session=True,
        )
        managed = Managed(
            name=name,
            command=command,
            pid=proc.pid,
            ticks=_fingerprint(proc.pid),
            proc=proc,
        )
        managed._task = asyncio.create_task(self._drain(managed))
        self._procs[name] = managed
        self._save()
        return managed

    async def _drain(self, managed: Managed) -> None:
        """Keep its output, and record how it ended.

        The stream closing *is* the ending, whether the process finished by itself,
        was stopped, or was killed — so one place sees all three.
        """
        assert managed.proc is not None and managed.proc.stdout is not None
        async for raw in managed.proc.stdout:
            managed.log.append(raw.decode("utf-8", "replace"))
            if len(managed.log) > 500:
                del managed.log[:100]
        await managed.proc.wait()
        self._ended(managed, managed.proc.returncode)

    def _ended(self, managed: Managed, code: int | None) -> None:
        if managed.ended_at is not None:
            return
        managed.ended_at = time.time()
        managed.exit_code = code
        self._save()

    def get(self, name: str) -> Managed | None:
        return self._procs.get(name)

    def adopt(self, managed: Managed) -> Managed:
        """Register a child this manager did not **spawn** — a delegation (T30).

        The caps launcher builds the pipes and the watcher itself; what it owes the
        registry is the same thing a `start`ed process owes: findable while it
        runs, stoppable as a tree, and an ending that is recorded. The draining of
        its output belongs to the delegation, which needs the bytes for the
        reviewable result — so `adopt` registers, and `record_end` closes.
        """
        existing = self._procs.get(managed.name)
        if existing is not None and existing.alive():
            raise AlreadyRunning(existing)
        self._procs[managed.name] = managed
        self._save()
        return managed

    def record_end(self, managed: Managed, code: int | None) -> None:
        """The adopted child's ending, said in the registry the owner already reads."""
        if managed.proc is None:
            return
        self._ended(managed, code)
        if getattr(managed, "on_end", None) is not None:
            try:
                managed.on_end(managed)  # type: ignore[misc]
            except Exception:  # noqa: BLE001 — a failed notification never re-rolls the process
                pass

    def list(self) -> list[Managed]:
        return sorted(self._procs.values(), key=lambda m: m.started_at, reverse=True)

    async def stop(self, name: str) -> tuple[bool, str]:
        """``(stopped, what happened)`` — the sentence is half the answer.

        An entry that is already gone is *not* stopped: reporting success there
        would be a claim the owner cannot check, and the difference between "I
        stopped it" and "it died at four o'clock" is the whole question.
        """
        managed = self._procs.get(name)
        if managed is None:
            return False, f"nothing has been started under the name {name!r}"
        if not managed.alive():
            return False, f"{name!r} is not running: {managed.describe()}"

        if managed.proc is not None:
            _signal(managed.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(managed.proc.wait(), timeout=GRACE_S)
            except TimeoutError:
                _signal(managed.pid, signal.SIGKILL)
                await managed.proc.wait()
            self._ended(managed, managed.proc.returncode)
            code = "exit code unknown" if managed.exit_code is None else f"rc={managed.exit_code}"
            return True, f"stopped {name!r} (pid {managed.pid}, {code})"

        # No handle: it was started by an earlier run. The fingerprint has already
        # vouched for the pid, which is what makes this safe to signal — and it is
        # also the only way back to a process that outlived the daemon.
        if not _signal(managed.pid, signal.SIGTERM):
            self._ended(managed, None)
            return False, f"{name!r} had already exited — there was nothing to stop"
        return True, (
            f"asked {name!r} (pid {managed.pid}, started by an earlier run) to stop; its exit "
            "code is not ours to collect, so `list` will report it as unknown"
        )

    async def stop_all(self) -> None:
        """Stop what *this* run started, on the way out.

        Only handles we hold: a process left by an earlier run belongs to that run,
        and a daemon shutting down cleanly is not the moment to reach for a pid a
        previous process was responsible for.
        """
        for managed in list(self._procs.values()):
            if managed.proc is not None and managed.alive():
                await self.stop(managed.name)


class ProcessTool(Tool):
    name = "process"
    title = "Start a long-running process"
    description = "Start, list, read logs from, or stop long-running processes."
    risk = Risk.ASK
    scopes = {"fs": ["workspace"], "exec": ["process"]}
    parameters = params(
        op=({"type": "string", "enum": ["start", "list", "logs", "stop"]}, True),
        name=({"type": "string"}, True),
        command=(
            {
                "type": "string",
                "description": (
                    "the shell command to run, for op=start. Pass \"\" for op=list/logs/stop, which "
                    "act on an existing process."
                ),
            },
            True,
        ),
        cwd=({"type": "string"}, False),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", ""))
        name = str(args.get("name", "")) or "a process"
        if op in ("list", "logs"):
            return Assessment(level=Level.OK, summary=f"read what {name!r} is doing")
        if op == "stop":
            return Assessment(level=Level.CAUTION, summary=f"stop the process {name!r}")
        return Assessment(
            level=Level.CAUTION,
            summary=f"start {name!r} and leave it running",
            reasons=("it keeps running in the background after this turn ends",),
        )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        name = str(args.get("name", "")).strip()
        manager = ctx.processes
        if op == "start":
            command = str(args.get("command", "")).strip()
            if not command:
                return ToolResult.error("start needs a command")
            cwd = None
            if args.get("cwd"):
                try:
                    cwd = str(guard.resolve(str(args["cwd"]), ctx.settings))
                except guard.OutOfBounds as exc:
                    return ToolResult.error(str(exc))
            try:
                managed = await manager.start(name, command, cwd, env=scrubbed(ctx.settings))
            except AlreadyRunning as exc:
                return ToolResult.error(str(exc))
            return ToolResult(
                output=f"started {name!r} (pid {managed.pid})",
                data={"pid": managed.pid},
            )
        if op == "list":
            rows = [f"{m.name}\tpid={m.pid}\t{m.describe()}\t{m.command}" for m in manager.list()]
            return ToolResult(output="\n".join(rows) or "(none)")
        if op == "logs":
            managed = manager.get(name)
            if managed is None:
                return ToolResult.error(f"nothing has been started under the name {name!r}")
            if managed.proc is None:
                return ToolResult.error(
                    f"{name!r} was started by an earlier run, and its output went with it: "
                    f"({managed.describe()}). What it *did* is in the registry, not in a buffer."
                )
            return ToolResult(output="".join(managed.log[-200:]) or "(no output yet)")
        if op == "stop":
            ok, detail = await manager.stop(name)
            return ToolResult(ok=ok, output=detail)
        return ToolResult.error(f"unknown op {op!r}")
