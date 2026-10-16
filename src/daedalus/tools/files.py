"""``files`` primitive — read / write / list / glob / grep."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..approvals.risk import Risk
from . import guard
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime

MAX_BYTES = 256 * 1024


class FilesTool(Tool):
    name = "files"
    title = "Read or write a file"
    description = (
        "Read, write, list, glob, or grep files inside the permitted workspace. "
        "Protected paths (.env, private docs) are refused."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["workspace"]}
    parameters = params(
        op=({"type": "string", "enum": ["read", "write", "append", "list", "glob", "grep", "mkdir"]}, True),
        path=({"type": "string"}, True),
        content=(
            {
                "type": "string",
                "description": (
                    "the text to write, for op=write/append. Pass \"\" for the ops that do not "
                    "write (read, list, glob, grep, mkdir)."
                ),
            },
            True,
        ),
        pattern=({"type": "string", "description": "glob or regex"}, False),
        max_results=({"type": "integer"}, False),
    )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", "")).strip()
        raw_path = str(args.get("path", ""))
        try:
            path = guard.resolve(raw_path, ctx.settings)
        except guard.OutOfBounds as exc:
            return ToolResult.error(str(exc))
        try:
            if op == "read":
                return self._read(path)
            if op == "write":
                return self._write(path, str(args.get("content", "")))
            if op == "append":
                return self._append(path, str(args.get("content", "")))
            if op == "list":
                return self._list(path)
            if op == "glob":
                return self._glob(path, str(args.get("pattern", "*")), int(args.get("max_results", 200)))
            if op == "grep":
                return self._grep(path, str(args.get("pattern", "")), int(args.get("max_results", 100)))
            if op == "mkdir":
                path.mkdir(parents=True, exist_ok=True)
                return ToolResult(output=f"created {path}")
        except OSError as exc:
            return ToolResult.error(f"{op} failed: {exc}")
        return ToolResult.error(f"unknown op {op!r}")

    @staticmethod
    def _read(path: Path) -> ToolResult:
        if not path.exists():
            return ToolResult.error(f"no such file: {path}")
        if path.is_dir():
            return ToolResult.error(f"{path} is a directory; use op=list")
        data = path.read_bytes()[:MAX_BYTES]
        return ToolResult(output=data.decode("utf-8", "replace"), data={"path": str(path), "bytes": len(data)})

    @staticmethod
    def _write(path: Path, content: str) -> ToolResult:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolResult(output=f"wrote {len(content)} bytes to {path}", data={"path": str(path)})

    @staticmethod
    def _append(path: Path, content: str) -> ToolResult:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(content)
        return ToolResult(output=f"appended {len(content)} bytes to {path}", data={"path": str(path)})

    @staticmethod
    def _list(path: Path) -> ToolResult:
        if not path.is_dir():
            return ToolResult.error(f"not a directory: {path}")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return ToolResult(output="\n".join(entries) or "(empty)", data={"entries": entries})

    @staticmethod
    def _glob(path: Path, pattern: str, max_results: int) -> ToolResult:
        hits = [str(p) for p in sorted(path.glob(pattern))][:max_results]
        return ToolResult(output="\n".join(hits) or "(no matches)", data={"matches": hits})

    @staticmethod
    def _grep(path: Path, pattern: str, max_results: int) -> ToolResult:
        if not pattern:
            return ToolResult.error("grep needs a pattern")
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return ToolResult.error(f"bad regex: {exc}")
        targets = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        lines: list[str] = []
        for target in targets:
            try:
                if target.stat().st_size > MAX_BYTES:
                    continue
                text = target.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    lines.append(f"{target}:{i}: {line.strip()[:200]}")
                    if len(lines) >= max_results:
                        return ToolResult(output="\n".join(lines), data={"matches": lines})
        return ToolResult(output="\n".join(lines) or "(no matches)", data={"matches": lines})
