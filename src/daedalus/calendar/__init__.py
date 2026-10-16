"""The internal calendar — the agent's own store (SPEC §5.5).

``$DAEDALUS_HOME/calendar/`` holds ``events.ics`` (the truth), ``index.sqlite``
(a derived cache), and ``sync_state.json`` (the gcal cursor slot, empty until
gcal is wired). Mirrors :mod:`daedalus.memory`: the file is authoritative, the
index is rebuildable, and a hand edit is never overwritten silently.
"""

from .event import IMPORTED, INTERNAL, Event, normalise_when, parse_iso
from .store import CalendarStore, ImportReport

__all__ = [
    "IMPORTED",
    "INTERNAL",
    "CalendarStore",
    "Event",
    "ImportReport",
    "normalise_when",
    "parse_iso",
]
