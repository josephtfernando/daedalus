"""Time zones — which clock a wall time is on (RFC 5545 ``TZID``).

Times in the store are **floating local**: "09:00" means 09:00 on this laptop,
which is the honest default for a single-user agent that lives on one machine.
But an event can carry a zone — from a hand edit, or from a calendar file the
owner brought in — and then its wall time belongs to *that* zone: a 09:00 call
in London is 10:00 here, and it stays 10:00 here across a clock change while the
London clock moves.

Two frames exist and they are not the same thing:

- **Wall time in the event's own zone** — what the owner wrote, what the file
  says, what ``RRULE`` arithmetic preserves ("9:00, every weekday" stays 9:00
  across a DST boundary, which is the entire point).
- **Real instants** — what free/busy maths, ordering and overlap need, because
  two events in different zones can only be compared as moments, not as strings.

:func:`local_wall` and :func:`wall_in` are the only conversions, so there is one
place to be wrong rather than five. An unreadable zone **falls back to floating**
rather than guessing an offset: an hour that is merely local beats a wrong hour
delivered confidently.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, tzinfo
from pathlib import Path

try:  # pragma: no cover - stdlib on a normal install
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):  # type: ignore[no-redef]
        pass

_CACHE: dict[str, tzinfo] = {}
_LOCAL: tzinfo | None = None
_LOCAL_RESOLVED = False


def _load(name: str) -> tzinfo | None:
    if not name:
        return None
    if name in _CACHE:
        return _CACHE[name]
    if ZoneInfo is None:
        return None
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    _CACHE[name] = zone
    return zone


def local_zone() -> tzinfo | None:
    """The laptop's zone, resolved once. ``None`` when it cannot be named.

    ``$TZ`` first, then the ``/etc/localtime`` symlink — the two places a Linux
    box records it. A failed lookup is not fatal: the store falls back to
    floating times, which is what it would have been doing anyway.
    """
    global _LOCAL, _LOCAL_RESOLVED
    if _LOCAL_RESOLVED:
        return _LOCAL
    candidates: list[str] = []
    name = os.environ.get("TZ", "").strip().lstrip(":")
    if name:
        candidates.append(name)
    marker = "zoneinfo/"
    try:
        target = str(Path("/etc/localtime").resolve())
    except OSError:
        target = ""
    if marker in target:
        candidates.append(target.split(marker, 1)[1])
    for candidate in candidates:
        zone = _load(candidate)
        if zone is not None:
            _LOCAL = zone
            break
    _LOCAL_RESOLVED = True
    return _LOCAL


def local_name() -> str:
    zone = local_zone()
    return str(getattr(zone, "key", "") or os.environ.get("TZ", "") or "")


def zone_for(tzid: str) -> tzinfo | None:
    """The zone a ``TZID`` names, or ``None`` if it does not name one."""
    text = str(tzid or "").strip().strip('"')
    if not text:
        return None
    return _load(text)


def is_local(tzid: str) -> bool:
    """Whether ``tzid`` says nothing new — the owner's own clock."""
    text = str(tzid or "").strip()
    if not text:
        return True
    return text == local_name()


def aware(moment: datetime, tzid: str) -> datetime:
    """Attach the zone a wall time belongs to (the local zone when it names none).

    The naive fields are *kept*: this says "these digits are on this clock", not
    "convert these digits". ``ZoneInfo`` recomputes the offset per instant, so a
    later ``timedelta`` keeps the wall time and moves the real one — which is
    exactly what "9:00 every weekday" has to mean across a clock change.
    """
    if moment.tzinfo is not None:
        return moment
    zone = zone_for(tzid) or local_zone()
    if zone is None:
        return moment
    return moment.replace(tzinfo=zone)


def local_wall(moment: datetime) -> datetime:
    """A moment as naive wall time on this laptop — the frame everything compares in."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def wall_in(moment: datetime, zone: tzinfo | None) -> datetime:
    """A naive local moment as naive wall time in ``zone``."""
    if zone is None:
        return moment
    here = moment.tzinfo or local_zone() or timezone.utc
    return moment.replace(tzinfo=here).astimezone(zone).replace(tzinfo=None)


def in_zone(moment: datetime, zone: tzinfo | None) -> datetime:
    """A naive local moment as an aware datetime *on* ``zone``'s clock."""
    if zone is None:
        return moment
    here = local_zone() or timezone.utc
    return moment.replace(tzinfo=here).astimezone(zone)
