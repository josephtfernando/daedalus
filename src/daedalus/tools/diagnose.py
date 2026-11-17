"""``diagnose`` — the agent's own health, asked from the conversation.

The owner's question, in one sentence: *"why were you quiet?"* Until this
existed, answering it meant ssh-ing in and reading journald. Three ops, all
offline and all structure-only:

``status`` — what is configured and reachable right now: each provider in the
chain, the cap its credential has left in the rolling window, whether it is
benched, the disk under ``$DAEDALUS_HOME``, the dashboard, and the tailnet's
Funnel state (a public exposure is a fact worth seeing before it is a surprise).

``usage`` — the ledger every turn writes. Turns, failures, retries, tokens, and
per-provider latency, so "which model is slow" and "which turn was flaky" are
observations rather than impressions.

``last`` — the tail of that ledger, one line per turn.

Nothing here reads the owner's words: the usage ledger is structure by
construction, and this tool only aggregates it. Nothing here spends model quota
either — the probes are a stat, a socket, and a subprocess.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..approvals.classify import Assessment, Level
from ..approvals.risk import Risk
from .base import Tool, ToolResult, params

if TYPE_CHECKING:
    from ..runtime import Runtime


def _entries(home: Path) -> list[dict[str, Any]]:
    """Every usage line this machine has, oldest first.

    The rotated generation is read too: a turn from before the last rotation is
    still this week's turn, and a diagnostic that forgets it is wrong exactly
    when the file got big enough to matter.
    """
    out: list[dict[str, Any]] = []
    state = home / "state"
    for name in ("usage.jsonl.1", "usage.jsonl"):
        path = state / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                out.append(entry)
    return out


def _when(entry: dict[str, Any]) -> datetime | None:
    raw = entry.get("at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _recent(entries: list[dict[str, Any]], days: float) -> list[dict[str, Any]]:
    cutoff = time.time() - max(0.0, days) * 86400
    keep: list[dict[str, Any]] = []
    for entry in entries:
        at = _when(entry)
        if at is None:
            keep.append(entry)  # undated: never hide a line for lacking a field
        elif at.timestamp() >= cutoff:
            keep.append(entry)
    return keep


def _pct(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _num(entry: dict[str, Any], key: str) -> float:
    value = entry.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _disk(home: Path) -> str:
    try:
        usage = shutil.disk_usage(home)
    except OSError as exc:
        return f"unknown ({type(exc).__name__})"
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    return f"{free_gb:.1f} GB free of {total_gb:.0f} GB"


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def _funnel() -> str:
    """The tailnet's Funnel state, phrased so "unknown" cannot read as "off"."""
    from ..genui.publish import funnel_on

    state = funnel_on()
    if state is True:
        return "ON — the node is publicly reachable; publishes are refused until it is off"
    if state is False:
        return "off (tailnet only)"
    return "unknown — `tailscale serve status` did not answer"


def _usage_line(entries: list[dict[str, Any]], days: float) -> str:
    recent = _recent(entries, days)
    if not recent:
        return f"no turns recorded in the last {days:g} day(s)"
    turns = len(recent)
    errors = [e for e in recent if e.get("outcome") == "error"]
    retries = int(sum(_num(e, "retries") for e in recent))
    estimated = sum(1 for e in recent if e.get("tokens_estimated"))
    tokens_in = int(sum(_num(e, "tokens_in") for e in recent))
    tokens_out = int(sum(_num(e, "tokens_out") for e in recent))
    failures = Counter(
        str(e.get("failure") or "unknown") for e in errors
    )

    by_provider: dict[str, list[float]] = {}
    provider_errors: Counter[str] = Counter()
    for entry in recent:
        provider = str(entry.get("provider") or "(none)")
        by_provider.setdefault(provider, []).append(_num(entry, "seconds"))
        if entry.get("outcome") == "error":
            provider_errors[provider] += 1
    rows: list[str] = []
    for provider, seconds in sorted(by_provider.items(), key=lambda pair: -_pct(pair[1], 0.95)):
        rows.append(
            f"  {provider}: {len(seconds)} turn(s), p50 {_pct(seconds, 0.5):.1f}s, "
            f"p95 {_pct(seconds, 0.95):.1f}s, {provider_errors.get(provider, 0)} error(s)"
        )

    lines = [
        f"last {days:g} day(s): {turns} turn(s), {len(errors)} ended in an error, "
        f"{retries} retry(ies)",
        f"tokens: {tokens_in} in / {tokens_out} out"
        + (f" ({estimated} turn(s) estimated — the endpoint reported no usage)" if estimated else ""),
    ]
    if failures:
        top = ", ".join(f"{kind} ×{count}" for kind, count in failures.most_common(5))
        lines.append(f"failures: {top}")
    lines.append("by provider:")
    lines.extend(rows)
    return "\n".join(lines)


def _last_line(entries: list[dict[str, Any]], limit: int) -> str:
    if not entries:
        return "the usage ledger is empty — no turn has run on this machine yet"
    lines: list[str] = []
    for entry in entries[-limit:]:
        at = entry.get("at") or "?"
        outcome = entry.get("outcome") or "ok"
        failure = entry.get("failure")
        lines.append(
            f"{at} — {entry.get('seat', '?')} via {entry.get('provider') or '(none)'}, "
            f"{entry.get('steps', '?')} step(s), {int(_num(entry, 'tokens_in'))}/"
            f"{int(_num(entry, 'tokens_out'))} tokens, {_num(entry, 'seconds'):.1f}s, "
            f"{int(_num(entry, 'retries'))} retry(ies), {outcome}"
            + (f" ({failure})" if failure else "")
        )
    return "\n".join(lines)


class DiagnoseTool(Tool):
    name = "diagnose"
    title = "Check the agent's own health"
    description = (
        "Answer 'why were you quiet / slow / failing' from this machine's own facts. "
        "op=status: each provider in the chain, the cap its key has left in the rolling "
        "window, whether it is benched, disk headroom, the dashboard, and the tailnet's "
        "Funnel state. op=usage: turns, errors, retries, tokens and per-provider latency "
        "over the last `days` (default 7). op=last: the most recent turns, one line each. "
        "All offline; none of it reads the owner's words."
    )
    risk = Risk.AUTO
    scopes = {"fs": ["state"]}
    parameters = params(
        op=({"type": "string", "enum": ["status", "usage", "last"]}, True),
        days=({"type": "integer", "description": "how far back op=usage looks (default 7)"}, False),
        limit=({"type": "integer", "description": "how many turns op=last shows (default 5)"}, False),
    )

    def assess(self, args: dict[str, Any]) -> Assessment:
        op = str(args.get("op", ""))
        return Assessment(
            level=Level.OK,
            summary=f"read the agent's own {op or 'status'} record (no egress, no model call)",
        )

    async def run(self, args: dict[str, Any], ctx: "Runtime") -> ToolResult:
        op = str(args.get("op", ""))
        if op == "status":
            return await self._status(ctx)
        if op == "usage":
            days = max(0.5, float(args.get("days") or 7))
            return ToolResult(output=_usage_line(_entries(ctx.settings.home), days))
        if op == "last":
            limit = max(1, min(int(args.get("limit") or 5), 50))
            return ToolResult(output=_last_line(_entries(ctx.settings.home), limit))
        return ToolResult.error(f"unknown diagnose op {op!r} — status, usage, last")

    async def _status(self, ctx: "Runtime") -> ToolResult:
        home = ctx.settings.home
        model = ctx.model
        lines = [f"home: {home}", f"disk: {_disk(home)}"]
        lines.append(
            f"dashboard: port {ctx.settings.dashboard_port} — "
            + ("listening" if await asyncio.to_thread(_port_open, ctx.settings.dashboard_port) else "not listening")
        )
        lines.append(f"funnel: {await asyncio.to_thread(_funnel)}")
        lines.append(f"surface: {type(ctx.surface).__name__ if ctx.surface else 'none attached'}")

        lines.append("providers:")
        chain = ([model.cheap] if model.cheap else []) + list(model.providers)
        if not chain:
            lines.append("  (none configured)")
        for provider in chain:
            window, cap = model.limits_for(provider)
            used = model.ledger.count(model.credential(provider), window)
            used_text = f"{used}/{cap}" if cap > 0 else f"{used} (no cap set)"
            parts = [
                f"  {provider.name} ({provider.model})",
                f"available={'yes' if provider.available() else 'no'}",
                f"key={model.credential(provider)}",
                f"window {used_text} in {window:g}s",
            ]
            benched = model.cooling_for(provider)
            if benched:
                parts.append(f"BENCHED for {benched:.0f}s (out of quota)")
            health = model.health_for(provider)
            if health:
                parts.append(f"{health} consecutive failure(s)")
            lines.append(", ".join(parts))

        entries = _entries(home)
        if entries:
            last = entries[-1]
            lines.append(
                "last turn: "
                f"{last.get('at', '?')} — {last.get('provider') or '(none)'}, "
                f"{last.get('outcome', 'ok')}, {_num(last, 'seconds'):.1f}s, "
                f"{int(_num(last, 'retries'))} retry(ies)"
                + (f", {last['failure']}" if last.get("failure") else "")
            )
        else:
            lines.append("last turn: none recorded")
        return ToolResult(output="\n".join(lines))
