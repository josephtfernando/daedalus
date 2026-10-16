"""Reminders — the ``VALARM`` the owner set, and the ledger of what fired.

A reminder is **not** proactive speech. The owner asked for it and named the
time; the agent is passing on an instruction, not choosing to interrupt. So it
is not gated by quiet hours or the daily cap (SPEC §9) — the same reasoning that
keeps an authored capability's alert ungated: silencing a 07:00 reminder because
the agent's own cadence is asleep would defeat the thing the owner built.

What it *is* careful about is not saying the same thing twice.
``state/reminders.json`` records which nudges have been delivered, so a tick
that runs twice in a minute — or a daemon that restarts mid-minute — does not
nudge the owner again about the same meeting.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import zones
from .event import Event, Reminder


def key_for(event: Event, reminder: Reminder) -> str:
    """A stable identity for one nudge: this instance, and this alarm on it."""
    instance = event.recurrence_id or event.start
    return f"{event.uid}@{instance}|{reminder.offset_minutes}|{reminder.action}"


def render(event: Event, reminder: Reminder, now: datetime) -> str:
    """The message the owner reads. Short, and about *this* meeting."""
    minutes = int(round((zones.local_wall(event.span()[0]) - now).total_seconds() / 60))
    lines = [f"⏰ **{event.summary}** — {_soon(minutes)}"]
    if reminder.description:
        lines.append(reminder.description)
    where = event.when() + (f" @ {event.location}" if event.location else "")
    lines.append(where)
    return "\n".join(lines)


def _soon(minutes: int) -> str:
    if minutes <= 0:
        return "starting now"
    if minutes < 60:
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    hours, rest = divmod(minutes, 60)
    tail = f" {rest}m" if rest else ""
    return f"in {hours} hour{'s' if hours != 1 else ''}{tail}"


class ReminderLedger:
    """Which nudges have already been delivered (``state/reminders.json``)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, float]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        fired = data.get("fired") if isinstance(data, dict) else None
        if not isinstance(fired, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in fired.items():
            try:
                out[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def _write(self, fired: dict[str, float]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"fired": fired}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def fired(self, key: str) -> bool:
        return key in self._read()

    def note(self, key: str, when: datetime) -> None:
        fired = self._read()
        fired[key] = when.timestamp()
        self._write(fired)

    def prune(self, before: datetime) -> int:
        """Forget nudges older than ``before`` so the file cannot grow forever."""
        cutoff = before.timestamp()
        fired = self._read()
        keep = {key: value for key, value in fired.items() if value >= cutoff}
        removed = len(fired) - len(keep)
        if removed:
            self._write(keep)
        return removed

    def count(self) -> int:
        return len(self._read())
