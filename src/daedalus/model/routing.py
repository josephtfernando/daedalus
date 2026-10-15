"""Route selection: heavy chain vs the cheap local model (SPEC §4, ARCHITECTURE §9.4).

Deliberately simple in P0 — a seam, not a scheduler. The engine can pass an
explicit route; otherwise a length/keyword heuristic picks cheap for trivial
turns (and the cheap model is only used when it is actually reachable).
"""

from __future__ import annotations

_CHEAP_MARKERS = (
    "classify",
    "label this",
    "summarize in one line",
    "extract the",
    "is this",
    "yes or no",
    "one word",
)


def choose_route(text: str, *, explicit: str | None = None, cheap_available: bool = True) -> str:
    """Return ``"cheap"`` or ``"heavy"``."""
    if explicit in ("cheap", "heavy"):
        return explicit
    if not cheap_available:
        return "heavy"
    lowered = text.strip().lower()
    if len(lowered) <= 160 and any(marker in lowered for marker in _CHEAP_MARKERS):
        return "cheap"
    return "heavy"
