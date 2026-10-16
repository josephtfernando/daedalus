"""Capability runner — child process, own venv, resource caps (ARCHITECTURE §9.3).

Protocol: JSON in on stdin (``{args, context, state}``), JSON out on stdout
(``{result, state_next, notifications}``). The manifest is the only scope
available.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..security.env import scrubbed
from .manifest import Manifest

MAX_OUTPUT = 256 * 1024


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
        """Create the capability's venv and install deps. Returns the python to use."""
        if not self.use_venv or not manifest.deps:
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
            str(entry),
            cwd=str(cap_dir),
            env=scrubbed(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=payload.encode()), timeout=manifest.caps.time_s
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return RunResult(
                ok=False,
                error=f"timed out after {manifest.caps.time_s}s",
                duration_s=asyncio.get_event_loop().time() - started,
            )
        duration = asyncio.get_event_loop().time() - started
        stdout = out.decode("utf-8", "replace")[:MAX_OUTPUT]
        stderr = err.decode("utf-8", "replace")[:MAX_OUTPUT]
        result = RunResult(
            ok=(proc.returncode == 0),
            exit_code=proc.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration,
        )
        parsed = _parse_payload(stdout)
        if parsed is None:
            if result.ok:
                result.error = "stdout was not valid JSON"
                result.ok = False
            return result
        result.result = parsed.get("result")
        result.state_next = parsed.get("state_next")
        result.notifications = list(parsed.get("notifications", []) or [])
        return result

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
