"""``settings`` — the boundary between this agent and the rest of the disk.

Only one thing lives here tonight, and only it: the **servable root list**
(T34) — the folders a request may read from and publish out of. It is the
ticket's own phrase that defines the posture: the root list is *owner-owned
data*, so adding one is a change to a file the owner owns (``roots.toml``),
never to a constant, and it goes **through an approval** rather than taking
effect silently. The tool's risk for adding or removing is therefore CAUTION
with ``risk_for → Risk.ASK``: even the loop asking on the owner's behalf takes
the question to them. The dashboard's editor (T35) reaches these same ops
through ``genui/actions.py``'s control seam — which is the *same* authorization,
by construction, not courtesy.

What is deliberately not here: model tiers, quiet hours, the Discord token —
config the owner owns *by editing*. The roots are file-first (``roots.toml``,
loaded at startup and re-read on the next one), so a restart picks up the
change without migration and the file stays readable by hand.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli_w

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from .base import Tool, ToolResult, params, sent_args

if TYPE_CHECKING:
    from ..runtime import Runtime


def _write_roots(home: Path, roots: list[str]) -> None:
    """Persist the list to ``roots.toml`` — the owner-owned file, at 0600."""
    path = home / "roots.toml"
    path.write_text(tomli_w.dumps({"roots": roots}), encoding="utf-8")
    path.chmod(0o600)


def _live(ctx: "Runtime", roots: list[str]) -> None:
    """Make the change true for reads *now* — the guard reads this dict live."""
    files = dict(ctx.settings.raw.get("files", {}) or {})
    files["roots"] = roots
    ctx.settings.raw["files"] = files


def roots_of(ctx: "Runtime") -> list[str]:
    """The declared roots, as the owner spelled them (not resolved, not sorted)."""
    files = ctx.settings.raw.get("files", {}) or {}
    return [str(p) for p in (files.get("roots") or [])]


class SettingsTool(Tool):
    name = "settings"
    title = "Read or change the servable root list"
    description = (
        "The servable root list (T34): the folders a request may read from and "
        "publish out of. op=roots-list shows what is declared, plus the roots "
        "every install already has (home, the repo, the working directory). "
        "`roots-add` takes `path` — widening what may be read and published is "
        "an ASK: the owner approves it. `roots-remove` takes `path` — shrinking "
        "it is one too, because access ending is also a decision the owner "
        "owns. The list lives in roots.toml, owner-owned data, and a change "
        "takes effect immediately without a restart."
    )
    risk = Risk.AUTO
    scopes: dict[str, Any] = {}
    parameters = params(
        op=(
            {
                "type": "string",
                "enum": ["roots-list", "roots-add", "roots-remove"],
                "description": (
                    "Read the servable root list (`roots-list`), or change it "
                    "(`roots-add` needs `path`; `roots-remove` needs `path`)."
                ),
            },
            True,
        ),
        path={
            "type": "string",
            "description": "One absolute folder path (roots-add / roots-remove).",
        },
    )

    def title_for(self, args: dict[str, Any]) -> str:
        op = str(args.get("op", ""))
        if op == "roots-add":
            return f"Widen the servable roots with {args.get('path', '')!r}"
        if op == "roots-remove":
            return f"Shrink the servable roots (drop {args.get('path', '')!r})"
        return self.title

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", ""))
        if op == "roots-add":
            return Assessment(
                level=Level.CAUTION,
                summary=(
                    "widen what may be read and published — the new root joins "
                    "the files primitive's reach and the publish tool's"
                ),
            )
        if op == "roots-remove":
            return Assessment(
                level=Level.CAUTION,
                summary="shrink what may be read and published — access ends for that folder",
            )
        return Assessment(level=Level.OK, summary="read the servable root list")

    def risk_for(self, args: dict[str, Any]) -> Risk:
        op = str(args.get("op", ""))
        if op in ("roots-add", "roots-remove"):
            return Risk.ASK
        return Risk.AUTO

    # ------------------------------------------------------------------

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", "")).strip()
        sent = sent_args(args)
        declared = roots_of(ctx)

        if op == "roots-list":
            always = ", ".join(
                sorted(
                    {
                        str(ctx.settings.home),
                        str(Path.cwd()),
                    }
                )
            )
            return ToolResult(
                ok=True,
                output=(
                    "declared roots (roots.toml): "
                    + (", ".join(declared) or "(none — only the always-allowed reach)")
                    + "\nalways allowed: "
                    + always
                ),
                data={"declared": declared},
            )

        path = str(args.get("path", "")).strip()
        if not path:
            return ToolResult.error(
                f"op={op} needs `path` — the absolute folder to change. You sent: {sent}"
            )
        folder = Path(path).expanduser()
        if not folder.is_absolute():
            return ToolResult.error(
                f"{path!r} is relative — a root is one absolute folder, named the "
                f"way the owner would type it. You sent: {sent}"
            )

        if op == "roots-add":
            if not folder.is_dir():
                return ToolResult.error(
                    f"{folder} is not a directory on this disk — a root is reachable, "
                    f"not aspirational. You sent: {sent}"
                )
            if str(folder) in declared:
                return ToolResult(
                    ok=True,
                    output=f"{folder} is already a declared root — nothing changed",
                    data={"roots": declared},
                )
            wanted = [*declared, str(folder)]
            _write_roots(ctx.settings.home, wanted)
            _live(ctx, wanted)
            return ToolResult(
                ok=True,
                output=(
                    f"added {folder} to the servable roots — the guard and the tools "
                    "read the list live, so the change holds now; the file is "
                    "roots.toml, yours to edit by hand"
                ),
                data={"roots": wanted},
            )

        if op == "roots-remove":
            kept = [p for p in declared if Path(p) != folder]
            if len(kept) == len(declared):
                return ToolResult.error(
                    f"{folder} is not in the declared roots — op=roots-list shows what "
                    f"is. You sent: {sent}"
                )
            _write_roots(ctx.settings.home, kept)
            _live(ctx, kept)
            return ToolResult(
                ok=True,
                output=(
                    f"removed {folder} — its files are no longer readable or publishable "
                    "by the agent. Existing publications of its files are not "
                    "retroactively taken down; they die on their own lifetimes"
                ),
                data={"roots": kept},
            )

        return ToolResult.error(
            f"unknown settings op {op!r} — roots-list, roots-add, roots-remove. You sent: {sent}"
        )
