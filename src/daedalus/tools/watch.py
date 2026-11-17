"""``watch`` — be told when *that* happened, not when the clock says so.

`schedule` is for what recurs and `remind` for what happens once, at a named
moment. Neither can say "when the build finishes" or "when the export lands":
no clock knows those. This is the third trigger — a path, and a note about why
the owner cares.

A watch fires once by default. `once=false` with a cooldown is for a thing that
changes repeatedly (a log being written, a synced folder) where the owner wants
to hear about it but not on every tick.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from ..scheduler.watches import MAX_WATCHES, MIN_COOLDOWN_S
from . import guard
from .base import Tool, ToolResult, params, sent_args

if TYPE_CHECKING:
    from ..runtime import Runtime


def _flag(value: Any, default: bool = True) -> bool:
    """A flag the model may send as ``true`` or as the string ``"false"``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _ago(when: float | None) -> str:
    if not when:
        return "never"
    delta = time.time() - when
    if delta < 90:
        return "just now"
    if delta < 5400:
        return f"{delta / 60:.0f}m ago"
    if delta < 129600:
        return f"{delta / 3600:.0f}h ago"
    return f"{delta / 86400:.0f}d ago"


class WatchTool(Tool):
    name = "watch"
    title = "Watch a file or folder"
    description = (
        "Be told when a specific path changes — the trigger a clock cannot express "
        "('when the build finishes', 'when the export lands', 'when that file goes "
        "away'). op=add takes `path` and `note` (what to tell the owner when it fires), "
        "on=change (default: appeared or was modified) or on=missing (disappeared), "
        "once=true (default) and cooldown_s. op=list shows the watches, op=remove takes "
        "`id`. Fires within a minute of the change, through the same wake as a reminder; "
        "the owner asked for it, so quiet hours do not silence it."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["state"]}
    parameters = params(
        op=({"type": "string", "enum": ["add", "list", "remove"]}, True),
        path=(
            {
                "type": "string",
                "description": "the file or folder to watch (required for op=add; under the files tool's roots)",
            },
            False,
        ),
        note=(
            {
                "type": "string",
                "description": "what to tell the owner when it fires — the reason they care",
            },
            False,
        ),
        on=(
            {
                "type": "string",
                "enum": ["change", "missing"],
                "description": "change (appeared or modified) or missing (disappeared); default change",
            },
            False,
        ),
        once=(
            {
                "type": "boolean",
                "description": "remove the watch after it fires; default true",
            },
            False,
        ),
        cooldown_s=(
            {
                "type": "integer",
                "description": f"minimum seconds between firings for a repeating watch (min {int(MIN_COOLDOWN_S)})",
            },
            False,
        ),
        id=({"type": "string", "description": "the watch id from op=list (required for op=remove)"}, False),
    )

    def title_for(self, args: dict[str, Any]) -> str:
        op = str(args.get("op", ""))
        if op == "add":
            return f"Watch {args.get('path', 'a path')!r}"
        if op == "remove":
            return f"Stop watching {args.get('id', '')!r}"
        return "List the watches"

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", ""))
        if op == "add":
            when = str(args.get("on") or "change")
            return Assessment(
                level=Level.OK,
                summary=(
                    f"watch {args.get('path', '')!r} and speak when it {when} — a local stat "
                    "once a minute, no egress"
                ),
            )
        if op == "remove":
            return Assessment(level=Level.OK, summary=f"remove the watch {args.get('id', '')!r}")
        return Assessment(level=Level.OK, summary="list the watches")

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        sent = sent_args(args)
        store = ctx.scheduler.watches
        if op == "list":
            watches = store.all()
            if not watches:
                return ToolResult(output="no watches — op=add starts one")
            lines = []
            for watch in watches:
                mode = "disappears" if watch.on == "missing" else "changes"
                repeat = "once" if watch.once else f"repeat, {watch.cooldown_s:.0f}s cooldown"
                lines.append(
                    f"{watch.id} — {watch.path} ({mode}; {repeat}; last fired {_ago(watch.fired_at)})"
                    f"\n    {watch.note}"
                )
            return ToolResult(
                output="\n".join(lines), data={"ids": [w.id for w in watches]}
            )
        if op == "remove":
            watch_id = str(args.get("id", "")).strip()
            if not watch_id:
                return ToolResult.error(f"op=remove needs `id` — one from op=list. You sent: {sent}")
            if not store.remove(watch_id):
                return ToolResult.error(
                    f"no watch {watch_id!r} — op=list shows what is watching"
                )
            return ToolResult(output=f"stopped watching {watch_id}")
        if op == "add":
            raw = str(args.get("path", "")).strip()
            note = str(args.get("note", "")).strip()
            if not raw:
                return ToolResult.error(f"op=add needs `path` — the file or folder. You sent: {sent}")
            if not note:
                return ToolResult.error(
                    f"op=add needs `note` — what to tell the owner when it fires, so the "
                    f"message has a reason in it. You sent: {sent}"
                )
            try:
                path = guard.resolve(raw, ctx.settings)
            except guard.OutOfBounds as exc:
                return ToolResult.error(
                    f"{exc} — a watch stats a path, so it stays inside the same roots as "
                    "`files`. Add the folder to [files] roots if the owner wants it watched"
                )
            if not path.exists():
                # A watch on a path that is not there is a `change` watch that
                # fires when it appears — which is legitimate — but only if the
                # owner meant that. Say which reading was taken.
                if str(args.get("on") or "change") == "missing":
                    return ToolResult.error(
                        f"{raw!r} does not exist, so watching for it to disappear would never "
                        "fire. Use on=change to hear when it appears"
                    )
            try:
                watch = store.add(
                    path=str(path),
                    note=note,
                    on=str(args.get("on") or "change"),
                    once=_flag(args.get("once"), True),
                    cooldown_s=float(args.get("cooldown_s") or 600),
                )
            except ValueError as exc:
                return ToolResult.error(f"{exc} (the cap is {MAX_WATCHES} watches)")
            state = "it is there now" if watch.signature is not None else "it is not there yet"
            return ToolResult(
                output=(
                    f"watching {path} ({'for it to disappear' if watch.on == 'missing' else 'for a change'}), "
                    f"id {watch.id}, {state}. "
                    + ("It fires once and is then removed." if watch.once else f"It repeats, at most every {watch.cooldown_s:.0f}s.")
                ),
                data={"id": watch.id, "path": str(Path(watch.path))},
            )
        return ToolResult.error(f"unknown watch op {op!r} — add, list, remove. You sent: {sent}")
