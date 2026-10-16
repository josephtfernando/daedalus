"""Proactive speech — when the agent may speak first (SPEC §9, §6).

A ``wake`` job is the agent *choosing to speak*, and the daily message is not a
template: it is composed fresh, or not at all. This module owns the "or not at
all" half.

Every gate here is **deterministic**. The model is never asked whether it is
allowed to speak — SPEC §6 puts the tier decision in code, and an agent that
can talk itself into a 03:00 notification has no quiet hours at all. Two failure
modes make a proactive agent worse than a silent one, and each gets a gate:

- **Speaking at the wrong hour.** *Quiet hours* silence wakes outright.
- **Speaking every time the timer fires.** A *daily cap* bounds how often the
  agent may open its mouth, so a cadence job cannot become a drip.

What is deliberately **not** gated: a notification from an authored capability.
An alert is not the agent choosing to speak — the owner defined that capability
and asked it to speak up. Silencing an alert at 03:00 would defeat the thing it
was built for.

Within those bounds the model still decides *substance*: it may answer with
:data:`SILENT` when there is nothing worth the owner's attention.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import Settings

#: What the model answers when it has nothing worth saying. A sentinel rather
#: than "an empty reply", because a model asked to say nothing will usually say
#: something, and the difference matters: an empty *reply* to the owner is a
#: fault to report, while an empty *wake* is a decision.
SILENT = "[silent]"


def _minute(text: str) -> int | None:
    """``"22:30"`` → 1350. ``None`` when it is not a time."""
    bits = str(text or "").strip().split(":")
    if len(bits) != 2:
        return None
    try:
        hours, minutes = int(bits[0]), int(bits[1])
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


def quiet_hours_cover(start: str, end: str, when: datetime) -> bool:
    """Whether ``when`` falls inside a quiet window that may cross midnight.

    An unset, unreadable or zero-length window covers nothing. A window of equal
    start and end is read as *not configured* rather than as *all day*: a typo
    should not silently switch the agent off, and ``enabled = false`` is the
    honest way to say that.
    """
    begin = _minute(start)
    finish = _minute(end)
    if begin is None or finish is None or begin == finish:
        return False
    now = when.hour * 60 + when.minute
    if begin < finish:
        return begin <= now < finish
    return now >= begin or now < finish


def _day(when: datetime) -> str:
    return when.strftime("%Y-%m-%d")


@dataclass
class ProactiveState:
    """The day's ledger: how often the agent has spoken, and what it said.

    File-first and hand-editable (``state/proactive.json``), like everything
    else the owner might want to inspect at 23:00 and ask "why did you ping me".
    """

    path: Path

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def spoken_today(self, when: datetime) -> int:
        data = self._read()
        if data.get("date") != _day(when):
            return 0
        try:
            return int(data.get("spoken", 0))
        except (TypeError, ValueError):
            return 0

    def last_text(self) -> str:
        return str(self._read().get("last", ""))

    def note_spoken(self, when: datetime, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": _day(when),
            "spoken": self.spoken_today(when) + 1,
            "last": str(text)[:500],
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    # -- the verdict -------------------------------------------------------

    def may_speak(self, settings: "Settings", when: datetime) -> tuple[bool, str]:
        """``(allowed, why not)``. The reason is shown to the owner and logged."""
        if not settings.proactive_enabled:
            return False, "proactive messages are off"
        if quiet_hours_cover(settings.quiet_start, settings.quiet_end, when):
            return False, f"quiet hours ({settings.quiet_start}–{settings.quiet_end})"
        spoken = self.spoken_today(when)
        if spoken >= settings.proactive_max_per_day:
            return False, (
                f"already spoke {spoken} time(s) today, cap {settings.proactive_max_per_day}"
            )
        return True, ""
