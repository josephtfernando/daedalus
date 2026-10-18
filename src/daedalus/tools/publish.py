"""``push`` — a file reaches the owner: a link with a lifetime, or a Taildrop copy.

SPEC §3.5 names the output channels `serve` and `push`; T25, T34 and T15 are the
tickets behind them, and this tool is all three legs in one: **`op=serve`**
publishes a named artifact behind a one-time token on the loopback server the
tailnet route already reaches (the dashboard's — T27 fixed the route to a fixed
port), **`op=push`** hands a file to a tailnet peer over Taildrop, and
**`op=takedown`** is the act that keeps the first act honest — the link dies and
the bytes go with it.

Two containment clauses, both structural:

- **The read is a `files` read.** The source path goes through the same
  `guard.resolve` the `files` primitive uses — the same permitted roots, the
  same blocked substrings (private docs, the Obsidian private tree, `.env`) —
  so publishing cannot reach anywhere reading cannot.
- **The bytes live in one place and die there.** A published copy is made
  under `serve/` — deliberately *not* `ui/`, where the plain artifact route
  would serve it to anyone on the tailnet without the token. The token route
  and the takedown are the only paths to those bytes.

The link is one-time by default (`fetches=1`) because "hand the owner a file" is
not "publish a URL": the first open spends it, and a pasted-elsewhere link opens
onto a 410. Lifetimes are default-24h and cannot be infinite.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from ..tools import guard
from ..genui.publications import Publications
from ..genui.publish import funnel_on
from .base import Tool, ToolResult, params, sent_args

if TYPE_CHECKING:
    from ..runtime import Runtime

#: The tailscale binary and the two calls this tool may make of it: reading
#: the tailnet's own DNS name, and Taildrop. Nothing else is constructed, and
#: the Funnel refusal inside `genui/publish.py` applies to the whole family:
#: a serve here never opens a URL past the tailnet.
BIN = "tailscale"


def _dns_name() -> str | None:
    """The tailnet's DNS name for this node — the URL prefix links resolve on."""
    try:
        proc = subprocess.run(
            [BIN, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        data = json.loads(proc.stdout)
        name = str(data["Self"]["DNSName"]).rstrip(".")
        return name or None
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _taildrop(file: Path, target: str) -> subprocess.CompletedProcess[str]:
    """The one command that moves bytes off this laptop: a Taildrop copy."""
    return subprocess.run(
        [BIN, "file", "cp", str(file), f"{target}:"],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _targets() -> list[str]:
    """The tailnet peers a copy may name, from the `file cp --targets` list."""
    try:
        proc = subprocess.run(
            [BIN, "file", "cp", "--targets"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.split()[0].rstrip(":") for line in proc.stdout.splitlines() if line.strip()]


#: A published artifact is copied before serving; the copy is the cap on what
#: a single serve may move. (The guard already bounds which files may be read;
#: this bounds how big the one that leaves may be.)
MAX_PUBLISH_BYTES = 64 * 1024 * 1024


def _sentinel(value: Any, fallback: Any) -> Any:
    return fallback if value in (None, "") else value


class PushTool(Tool):
    name = "push"
    title = "Serve or push a file to the owner"
    description = (
        "Give the owner a file three ways, all contained: op=serve publishes a "
        "named artifact behind a one-time token on the dashboard's tailnet URL "
        "(fetches=1 by default — the first open spends the link; lifetime 24h by "
        "default, never infinite), and op=takedown kills that link and deletes "
        "the bytes; op=push copies a file to a tailnet peer's Taildrop "
        "(op=peers lists who can receive); op=list shows the publications. The "
        "source path is checked by the same rules as the files tool: private "
        "docs and .env are never published."
    )
    risk = Risk.AUTO
    scopes = {}
    parameters = params(
        op=(
            {
                "type": "string",
                "enum": ["serve", "takedown", "list", "push", "peers"],
                "description": "serve publishes a one-time link; push copies to a device.",
            },
            True,
        ),
        path=(
            {
                "type": "string",
                "description": "The file to publish or push (required for serve and push).",
            },
            False,
        ),
        name=(
            {
                "type": "string",
                "description": (
                    "The published name (one plain file name); defaults to the "
                    "source file's own name."
                ),
            },
            False,
        ),
        lifetime_s={
            "type": "integer",
            "description": "Seconds the link lives; default 86400 (24h). No infinite links.",
        },
        fetches={
            "type": "integer",
            "description": "How many times the link may be fetched; default 1 (one-time).",
        },
        target={
            "type": "string",
            "description": "The tailnet peer to push to, from op=peers (required for push).",
        },
    )

    def title_for(self, args: dict[str, Any]) -> str:
        op = str(args.get("op", ""))
        if op == "serve":
            return f"Publish {args.get('name') or args.get('path', 'a file')!r} behind a one-time link"
        if op == "push":
            return f"Taildrop a file to {args.get('target', 'a tailnet peer')!r}"
        return f"{self.title} ({op})"

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", ""))
        if op == "serve":
            return Assessment(
                level=Level.CAUTION,
                summary=(
                    "publish a file behind a one-time token — the tailnet route, "
                    "one fetch by default, a lifetime, and a takedown"
                ),
            )
        if op == "push":
            return Assessment(
                level=Level.CAUTION,
                summary=f"copy a file to {args.get('target', '')!r} over Taildrop (tailnet only)",
            )
        if op == "takedown":
            return Assessment(
                level=Level.OK,
                summary=f"take down the published link {args.get('name', '')!r} and delete its bytes",
            )
        return Assessment(level=Level.OK, summary=f"read the push state ({op})")

    def risk_for(self, args: dict[str, Any]) -> Risk:
        return Risk.ASK if str(args.get("op", "")) in ("serve", "push") else Risk.AUTO

    # ------------------------------------------------------------------

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        sent = sent_args(args)
        pubs = ctx.publications
        serve_dir = ctx.settings.home / "serve"
        if op == "list":
            rows = []
            for pub in pubs.list():
                state = pub.dead(now=time.time()) or "live"
                rows.append(f"{pub.name}: {state} ({pub.fetches} fetches)")
            return ToolResult(output="\n".join(rows) or "(nothing published)")
        if op == "peers":
            people = _targets()
            return ToolResult(output="\n".join(people) or "(no reachable tailnet peers)")
        if op == "serve":
            return self._serve(args, ctx, pubs, serve_dir, sent)
        if op == "takedown":
            name = str(args.get("name", "")).strip()
            gone = pubs.takedown(name)
            if gone:
                (serve_dir / name).unlink(missing_ok=True)
                return ToolResult(output=f"{name!r} taken down; the bytes are gone and the link answers 410")
            return ToolResult.error(
                f"no live publication named {name!r} to take down — op=list shows what is live"
            )
        if op == "push":
            return self._push(args, ctx, sent)
        return ToolResult.error(
            f"unknown push op {op!r} — serve, takedown, list, push, peers. You sent: {sent}"
        )

    def _serve(
        self,
        args: dict[str, Any],
        ctx: "Runtime",
        pubs: Publications,
        serve_dir: Path,
        sent: str,
    ) -> ToolResult:
        raw = str(args.get("path", "")).strip()
        if not raw:
            return ToolResult.error(f"op=serve needs `path` — the file to publish. You sent: {sent}")
        try:
            source = guard.resolve(raw, ctx.settings)
        except guard.OutOfBounds as exc:
            return ToolResult.error(
                f"{exc} — a publication is a files read first; private paths and "
                "outside-the-workspace ones are refused here like anywhere else"
            )
        if not source.is_file():
            return ToolResult.error(f"{source} is not a file — nothing to publish. You sent: {sent}")
        if source.stat().st_size > MAX_PUBLISH_BYTES:
            return ToolResult.error(
                f"{source.name} is {source.stat().st_size / (1024 * 1024):.0f} MB — over the "
                f"{MAX_PUBLISH_BYTES // (1024 * 1024)} MB publication cap. Trim it or serve "
                "smaller pieces; the cap is the copy a serve owes"
            )
        funnel = funnel_on()
        if funnel is True:
            return ToolResult.error(
                "tailscale Funnel is on, which would put the link's contents on the public "
                "internet — this publish is refused, not warned about. Take Funnel down "
                f"(`{BIN} funnel --https=443 off`) and publish again (T25's gate, from T2's)"
            )
        if funnel is None:
            return ToolResult.error(
                "the tailnet's Funnel state could not be read just now — unknown is not "
                "good enough for an act that publishes bytes; check `tailscale serve status`"
            )
        name = str(_sentinel(args.get("name"), source.name))
        try:
            pub = pubs.publish(
                name,
                lifetime_s=float(_sentinel(args.get("lifetime_s"), 86400.0)),
                max_fetches=int(_sentinel(args.get("fetches"), 1)),
                created_by="push tool",
            )
        except ValueError as exc:
            return ToolResult.error(f"{exc} You sent: {sent}")
        # The copy is the publication's bytes — read once here, after the guard:
        serve_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, serve_dir / name)
        dns = _dns_name()
        link = f"https://{dns}/pub/{name}?t={pub.token}" if dns else (
            f"/pub/{name}?t={pub.token} on the dashboard's tailnet route "
            "(the tailnet DNS name could not be looked up just now)"
        )
        return ToolResult(
            ok=True,
            output=(
                f"published {name!r} from {source}. Link (one-time, 24h): {link}\n"
                "Say nothing about it in prose — the link is the message. "
                "It dies after the first fetch or the lifetime, whichever first; "
                f"`push op=takedown name={name}` kills it now."
            ),
            data={"name": name, "link": link, "expires_at": pub.expires_at},
        )

    def _push(self, args: dict[str, Any], ctx: "Runtime", sent: str) -> ToolResult:
        raw = str(args.get("path", "")).strip()
        target = str(args.get("target", "")).strip()
        if not raw or not target:
            return ToolResult.error(
                f"op=push needs `path` and `target` (a tailnet peer from op=peers; "
                f"Taildrop goes to a device, not into the air). You sent: {sent}"
            )
        known = _targets()
        if target not in known:
            return ToolResult.error(
                f"{target!r} is not a reachable tailnet peer — op=peers lists who can "
                f"receive (they answer: {', '.join(known) or 'none'}). "
                f"You sent: {sent}"
            )
        # The read is a files read, same as a serve: pushing cannot reach where
        # reading cannot (private paths, outside roots).
        try:
            source = guard.resolve(raw, ctx.settings)
        except guard.OutOfBounds as exc:
            return ToolResult.error(f"{exc} — a push is a files read first")
        try:
            result = _taildrop(source, target)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ToolResult.error(f"the Taildrop copy did not run cleanly: {exc}")
        if result.returncode != 0:
            return ToolResult.error(
                f"the copy to {target!r} failed: {(result.stderr or result.stdout).strip()[:300]}"
            )
        return ToolResult(
            ok=True,
            output=(
                f"sent to {target!r} over Taildrop — it arrives in that device's "
                "Taildrop inbox, never further"
            ),
        )
