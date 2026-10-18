"""The delegation runner (T29, T30) — a task, a named workspace, a reviewable return.

Icarus's headline job. The shape matters more than the CLI: a delegation is a
task the agent gives the OpenCode delegate, pinned to **one named workspace**
under ``$DAEDALUS_HOME/delegate/ws`` — the personal tree is never one of them —
and the return is what the owner can review without re-reading the world: the
diff against the workspace's baseline, what was run, what it could not do.

Two bounds are enforced, not asked for:

- **No history out of a delegation.** ``git commit`` / ``push`` / ``merge`` / ``pull``
  are refused before spawn if the *task* asks for them, and refused at the PATH
  shim even if the tool tries them on its own (``--no-verify`` bypasses hooks, so
  hooks were never the enforcement — not running the subcommand is). Those acts
  are separate trust decisions (SPEC §3.5 files `git` as its own egress action);
  a delegate that could perform them quietly merges the two.
- **The declared caps are caps** — `mem_mb` sampled on resident memory,
  ``cpu`` as whole-core affinity, ``time_s`` as a wall-clock ceiling, all through
  the same `limits` launcher a capability runs under (one implementation, two
  callers — that is the whole reason `limits.py` exists). A breach stops the
  tree and is named as the reason, never recorded and ignored.

Containment is checked on the *text*, before any spawn — after the CLI is
running the task, refusing is not refusal but abandonment.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..limits import LAUNCH as _LAUNCH
from ..limits import Watch, kill_tree as _kill, watch_caps
from ..security.env import scrubbed

#: Refused subcommands, by exact word. Not a heuristic about intent: each of
#: these is an act on *history* that SPEC §3.5 keeps with the owner.
FOREVER_WITH_THE_OWNER = ("commit", "push", "merge", "pull", "rebase", "reset")

DEFAULT_CAPS = {"cpu": 1.0, "mem_mb": 1024, "time_s": 1800}

MAX_DIFF_CHARS = 8000
MAX_REPORT_CHARS = 3000

GIT_SHIM = """#!/bin/sh
# The delegation git shim (T29). On PATH ahead of the real git for every
# delegation process, so `git commit`, `push`, `merge`, `pull`, `rebase`, `reset`
# do not run — history stays with the owner (SPEC §3.5). A delegation's return
# is the diff, and the diff is made of edits, not of commits.
for word in "$@"; do
  case "$word" in
    commit|push|merge|pull|rebase|reset)
      echo "delegation shim: git '$word' stays with the owner — bring the diff back instead" >&2
      exit 126
      ;;
  esac
done
exec git "$@"
"""


@dataclass(slots=True)
class DelegationResult:
    """One delegation, as its run record states it — never a bare exit code."""

    ok: bool
    name: str
    task: str
    workspace: str
    exit_code: int | None = None
    duration_s: float = 0.0
    peak_mb: float = 0.0
    #: The reviewable return: what changed (against the baseline), what was run,
    #: what it could not do.
    diff: str = ""
    report: str = ""
    error: str = ""
    breach: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0

    def run_record(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "workspace": self.workspace,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": round(self.duration_s, 3),
            "peak_mb": round(self.peak_mb, 1),
            "breach": self.breach,
            "error": self.error,
        }

    def summary(self) -> str:
        state = "finished" if self.ok else f"failed (exit {self.exit_code})"
        head = f"delegation {self.name!r} {state} in {self.duration_s:.0f}s"
        if self.breach:
            head += f" — {self.breach}"
        return head


class ContainmentRefused(ValueError):
    """The task is not a lawful delegation — refused before anything runs."""


def shim_dir(root: Path) -> Path:
    """Where the git shim lives, written once — it refuses history for every delegation."""
    d = root / ".shim"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "git"
    path.write_text(GIT_SHIM, encoding="utf-8")
    path.chmod(0o755)
    return d


def paths_outside(
    task: str, workspace: Path, *, forbidden: tuple[Path, ...] = ()
) -> list[str]:
    """Paths the task names that its workspace does not contain.

    Checked as the tokens they are: absolute paths (``/etc``, ``/home/...``),
    ``~`` paths, and ``..`` escapes. A path inside the workspace passes;
    the personal tree never does, and it is named as what it is — not just
    "outside" — because that refusal is the whole point (T29).
    """
    ws = workspace.resolve()
    offenders: list[str] = []
    for raw in re.findall(r"(?<![\w.-])([/~][\w.@+-][\w.@+/-]*)", task):
        p = Path(raw).expanduser()
        p = p.resolve()
        if p == ws or ws in p.parents or p in ws.parents:
            continue
        why = "outside its declared workspace"
        for bad in forbidden:
            if p == bad or bad in p.parents:
                why = f"the personal tree is not a workspace ({bad})"
                break
        offenders.append(f"{raw} — {why}")
    for token in task.split():
        clean = token.strip("`'\"(),;:")
        if "../" in clean or clean == ".." or clean.endswith("/.."):
            # A `..` in a task is an intent to leave the workspace, whatever it
            # resolves to — ellipsis ("...") is the only false positive the rule
            # guards against, and it never contains `../`.
            offenders.append(f"{clean} — walks out of its declared workspace")
    return offenders


class DelegateRunner:
    """Contains, caps, and returns one delegation. One workspace per name."""

    def __init__(self, root: Path, *, python: str | None = None) -> None:
        self.root = root
        #: The launcher's interpreter; None means *this* process's own, which is
        #: what the real runner uses. Tests pass theirs to stay out of the venvs.
        self.python = python

    # -- workspace ---------------------------------------------------------

    def workspace_dir(self, name: str) -> Path:
        """The one directory a delegation may touch, by strict name.

        A workspace is a single segment under the delegate root — never a path,
        never the personal tree, never anywhere else that happens to resolve.
        The personal tree is not a workspace the agent can declare; that is
        what "declared workspace" means.
        """
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""):
            raise ValueError(
                f"a workspace is one plain name (letters, digits, dot, dash) — not {name!r}"
            )
        return self.root / name

    async def prepare(self, name: str) -> Path:
        """Create the workspace with git and one baseline commit — ours, not the delegate's.

        The baseline is what makes the return reviewable at all: the diff an
        owner reads is against *something*. The daemon makes that something once,
        and the delegation itself never commits (the shim refuses it, and the
        daemon was never the delegate).
        """
        ws = self.workspace_dir(name)
        ws.mkdir(parents=True, exist_ok=True)
        if not (ws / ".git").exists():
            await self._git(ws, ["init", "-q"])
        head = await self._git(ws, ["rev-parse", "--verify", "HEAD"], check=False)
        if head is None:
            note = ws / "WORKSPACE.md"
            if not note.exists():
                note.write_text(
                    f"# {name}\n\nDelegation workspace. Edits land here and nowhere "
                    "else; history does not — commit/push/merge stay with the owner "
                    "(SPEC §3.5), and a reviewable return is a diff against this "
                    "baseline.\n",
                    encoding="utf-8",
                )
            await self._git(
                ws,
                ["-c", "user.name=Daedalus", "-c", "user.email=daedalus@localhost",
                 "add", "-A"],
            )
            await self._git(
                ws,
                ["-c", "user.name=Daedalus", "-c", "user.email=daedalus@localhost",
                 "commit", "-q", "-m", "baseline before delegation"],
            )
        return ws

    async def _git(self, ws: Path, args: list[str], *, check: bool = True) -> str | None:
        base = ["git", "-C", str(ws)]
        has_user = any(a == "-c" for a in args)
        argv = [*base, *args]
        if not has_user and args and args[-1] not in ("status", "diff"):
            argv = [*base, "-c", "user.name=Daedalus", "-c", "user.email=daedalus@localhost", *args]
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(ws), env=scrubbed(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            if check:
                raise RuntimeError(f"workspace git failed: {err.decode('utf-8', 'replace')[:300]}")
            return None
        return out.decode("utf-8", "replace").strip() or None

    # -- containment -------------------------------------------------------

    def check(self, task: str, name: str, *, forbidden: tuple[Path, ...] = ()) -> Path:
        """Refuse a delegation before the CLI is spawned (T29).

        Two refusals, one clause: a task that asks for history (`git commit`
        and friends) is not work, it is ownership; a task that names a path
        outside the declared workspace is a wider delegation than the shape
        promised. Both leave the machine untouched — there is no spawn to unwind.
        """
        ws = self.workspace_dir(name)  # the name itself is checked here
        lowered = f" {task.lower()} "
        asked = [w for w in FOREVER_WITH_THE_OWNER if re.search(rf"\b{w}\b", lowered)]
        if asked:
            raise ContainmentRefused(
                f"the task asks for git {', '.join(asked)} — history stays with the owner "
                "(SPEC §3.5). Phrase the task as work that leaves a diff to review."
            )
        offenders = paths_outside(task, ws, forbidden=forbidden)
        if offenders:
            raise ContainmentRefused(
                "refused before anything runs — the task names paths outside its declared "
                "workspace, and the declared workspace is the whole reach: "
                + "; ".join(offenders)
            )
        return ws

    # -- the run -----------------------------------------------------------

    def cmd(self, task: str) -> list[str]:
        """The delegate's command — tests override this; they must not pay for tokens."""
        return ["opencode", "run", task]

    @dataclass(slots=True)
    class Live:
        """A delegation in flight: enough to wait on it, cancel it, and return it."""

        proc: asyncio.subprocess.Process
        ws: Path
        task: str
        name: str
        cap: dict[str, Any]
        watch: Watch
        started_at: float
        argv: list[str]

    async def launch(
        self,
        task: str,
        *,
        name: str,
        caps: dict[str, Any] | None = None,
        cmd: list[str] | None = None,
        forbidden: tuple[Path, ...] = (),
    ) -> "DelegateRunner.Live":
        """Contain, prepare, spawn under caps — and return while it still runs.

        Splitting launch from collect (T30) is what makes a long delegation
        *visible* and cancellable: the spawn can be registered with the process
        registry before anyone waits on it.
        """
        ws = self.check(task, name, forbidden=forbidden)  # refusal precedes any mutation
        ws = await self.prepare(name)

        cap = {**DEFAULT_CAPS, **(caps or {})}
        backstop = max(int(cap["mem_mb"]) * 4, 2048)
        argv = list(cmd) if cmd is not None else self.cmd(task)
        env = scrubbed()
        env["PATH"] = f"{shim_dir(self.root)}:{env.get('PATH', '')}"

        proc = await asyncio.create_subprocess_exec(
            self.python or "python3",
            "-c",
            _LAUNCH,
            str(backstop),  # the runaway net; the *cap* is the sampled mem_mb in collect
            str(cap["cpu"]),
            *argv,
            cwd=str(ws),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Its own session, so a breach or a stop takes the whole tree rather
            # than orphaning whatever the delegate spawned.
            start_new_session=True,
        )
        return self.Live(
            proc=proc, ws=ws, task=task, name=name, cap=cap,
            watch=Watch(), started_at=time.time(), argv=argv,
        )

    async def collect(self, live: "DelegateRunner.Live") -> DelegationResult:
        """Wait the run out under its caps, and return the reviewable result."""
        proc, cap, watch = live.proc, live.cap, live.watch
        watcher = asyncio.create_task(watch_caps(proc, mem_mb=int(cap["mem_mb"]), watch=watch))
        try:
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=float(cap["time_s"]))
            except TimeoutError:
                await _kill(proc)
                result = DelegationResult(
                    ok=False, name=live.name, task=live.task, workspace=str(live.ws),
                    error=f"timed out after {cap['time_s']}s (the delegated time_s cap)",
                    started_at=live.started_at, ended_at=time.time(),
                    peak_mb=watch.peak_mb, breach=f"timed out after {cap['time_s']}s",
                )
                await self._finish(live, result)
                return result
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

        result = DelegationResult(
            ok=(proc.returncode == 0),
            name=live.name,
            task=live.task,
            workspace=str(live.ws),
            exit_code=proc.returncode,
            duration_s=time.time() - live.started_at,
            started_at=live.started_at,
            ended_at=time.time(),
            peak_mb=watch.peak_mb,
            report=(out.decode("utf-8", "replace") or "")[:MAX_REPORT_CHARS],
            error=(err.decode("utf-8", "replace") or "")[-MAX_REPORT_CHARS:],
        )
        if watch.breach:
            # We stopped it, so its own output says nothing about why: the
            # sentence naming the cap is the only true account of this run.
            result.ok = False
            result.error = result.breach = watch.breach
        await self._finish(live, result)
        return result

    async def _finish(self, live: "DelegateRunner.Live", result: DelegationResult) -> None:
        """Attach the reviewable return to the result and lay the run record down."""
        result.diff = await self.diff(live.ws)
        self._write_record(live.ws, result, live.cap)

    async def run(
        self,
        task: str,
        *,
        name: str,
        caps: dict[str, Any] | None = None,
        cmd: list[str] | None = None,
    ) -> DelegationResult:
        """One delegation end to end — the whole launch→collect arc in one call."""
        live = await self.launch(task, name=name, caps=caps, cmd=cmd)
        result = await self.collect(live)
        return result

    # -- the reviewable return ---------------------------------------------

    async def diff(self, ws: Path) -> str:
        """What changed, against the daemon's baseline — the owner's first question.

        New files are the likeliest change a delegation produces, and plain
        `git diff HEAD` does not show an *untracked* file at all. `add -N`
        (intent-to-add) makes them visible to diff without staging their
        content and without committing — index bookkeeping the daemon may do;
        a commit is the thing the delegate never does.
        """
        await self._git(ws, ["add", "-A", "-N"], check=False)
        parts: list[str] = []
        stat = await self._git(ws, ["-c", "core.pager=cat", "diff", "HEAD", "--stat"], check=False)
        if stat:
            parts.append(stat)
        full = await self._git(ws, ["-c", "core.pager=cat", "diff", "HEAD"], check=False)
        if full:
            parts.append(full[:MAX_DIFF_CHARS])
        return "\n".join(parts)[:MAX_DIFF_CHARS]

    def _write_record(self, ws: Path, result: DelegationResult, cap: dict[str, Any]) -> None:
        """The run record beside the work it describes — what ran, what came of it."""
        record = result.run_record()
        record["caps"] = cap
        record["diff"] = result.diff
        record["report"] = result.report
        record["error"] = result.error
        try:
            (ws / ".delegation.json").write_text(
                json.dumps(record, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            # The record's backup is the return value itself, never the reverse:
            # a failed write must not unrun the delegation.
            pass


def containment_refused(exc: ContainmentRefused) -> str:
    """The sentence the tool gives the model, with the way through, not just the wall."""
    return (
        f"{exc} — the shape of a delegation is: task, one named workspace, reviewable "
        "diff back. Name the workspace for the reach you actually need."
    )
