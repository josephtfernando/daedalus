"""``inbox`` — the files the owner sent, read back on request.

Attachments arrive through the Discord surface and land under
``$DAEDALUS_HOME/inbox`` with their words already extracted locally (see
:mod:`daedalus.inbox`). This is the primitive for the second half of that
exchange: *"what did that screenshot say?"*, *"read me the PDF from this
morning"*, *"forget that file"*.

Reading is never egress. Text is read directly, images by local OCR, audio by a
local Whisper, PDFs by poppler — whichever of those this machine has. A missing
reader is reported with the exact thing to install, because "I cannot read
audio" without the fix is a shrug, and the owner sent the file expecting it to
be read.
"""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from .. import inbox
from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from .base import Tool, ToolResult, params, sent_args

if TYPE_CHECKING:
    from ..runtime import Runtime


def _age(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 3600 * 1.5:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 86400 * 1.5:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f}d ago"


class InboxTool(Tool):
    name = "inbox"
    title = "Read the owner's attachments"
    description = (
        "The files the owner sent in Discord, saved on this machine with their words "
        "already read locally. op=list shows what is here (newest first); op=read returns "
        "one file's text (images by OCR, audio by Whisper, PDFs by poppler — whichever is "
        "installed); op=forget deletes one file, or every file with name=all. Use it when "
        "the owner refers to something they sent earlier, or when a turn's attachment "
        "block was truncated and you need more."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["inbox"]}
    parameters = params(
        op=({"type": "string", "enum": ["list", "read", "forget"]}, True),
        name=(
            {
                "type": "string",
                "description": "the file name from op=list (required for read and forget; 'all' forgets everything)",
            },
            False,
        ),
        limit=({"type": "integer", "description": "how many entries op=list shows (default 20)"}, False),
    )

    def title_for(self, args: dict[str, Any]) -> str:
        op = str(args.get("op", ""))
        if op == "read":
            return f"Read the attachment {args.get('name', '')!r}"
        if op == "forget":
            return f"Forget the attachment {args.get('name', '')!r}"
        return "List the owner's attachments"

    def risk_for(self, args: dict[str, Any]) -> Risk:
        return Risk.ASK if str(args.get("op", "")) == "forget" else Risk.AUTO

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", ""))
        if op == "forget":
            name = str(args.get("name", "")) or "(all)"
            return Assessment(
                level=Level.CAUTION,
                summary=f"delete the saved attachment {name!r} — the file in the inbox goes with it",
            )
        if op == "read":
            return Assessment(level=Level.OK, summary=f"read the attachment {args.get('name', '')!r}")
        return Assessment(level=Level.OK, summary="list the saved attachments")

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        sent = sent_args(args)
        home = ctx.settings.home
        if op == "list":
            limit = max(1, min(int(args.get("limit") or 20), 100))
            saved = inbox.listing(home, limit=limit)
            if not saved:
                return ToolResult(output="the inbox is empty — the owner has not sent a file")
            now = time.time()
            lines = []
            for item in saved:
                try:
                    age = _age(now - item.path.stat().st_mtime)
                except OSError:
                    age = "unknown age"
                lines.append(
                    f"{item.name} — {item.kind}, {inbox.human_size(item.size)}, {age}"
                )
            return ToolResult(
                output="\n".join(lines),
                data={"files": [item.name for item in saved]},
            )
        name = str(args.get("name", "")).strip()
        if op == "read":
            if not name:
                return ToolResult.error(f"op=read needs `name` — a file from op=list. You sent: {sent}")
            path = inbox.resolve_name(home, name)
            if path is None:
                return ToolResult.error(
                    f"no attachment named {name!r} in the inbox — op=list shows what is there. "
                    f"You sent: {sent}"
                )
            extraction = await inbox.extract_async(
                path, "", settings_raw=ctx.settings.raw
            )
            if not extraction.ok:
                return ToolResult.error(
                    f"{name} could not be read: {extraction.note}. "
                    "Say which reader is missing rather than guessing at the contents."
                )
            return ToolResult(
                output=extraction.text,
                data={"name": name, "kind": extraction.kind, "reader": extraction.tool},
            )
        if op == "forget":
            if not name:
                return ToolResult.error(
                    f"op=forget needs `name` — one file from op=list, or 'all'. You sent: {sent}"
                )
            if name.lower() == "all":
                saved = inbox.listing(home, limit=1000)
                for item in saved:
                    item.path.unlink(missing_ok=True)
                return ToolResult(
                    output=f"forgot {len(saved)} attachment(s); the inbox is empty"
                )
            path = inbox.resolve_name(home, name)
            if path is None:
                return ToolResult.error(
                    f"no attachment named {name!r} to forget — op=list shows what is there"
                )
            path.unlink(missing_ok=True)
            return ToolResult(output=f"forgot {name!r}; the file is gone from the inbox")
        return ToolResult.error(
            f"unknown inbox op {op!r} — list, read, forget. You sent: {sent}"
        )
