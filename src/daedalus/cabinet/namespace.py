"""Seat memory namespaces (SPEC §7).

A seat is identity + charter + **memory namespace**: the facts one identity knows
and another does not. This module is the one place that decides *which* namespace
a seat reads and writes, so the prompt, the tools, and a CLI probe cannot disagree
about it.

The arrangement is asymmetric on purpose. The coordinator's namespace is the
**root** — the one every fact written before seats had namespaces already lives
in — so `local` history is not orphaned by the arrival of seats. A charter file
cannot move it (`cabinet/seat.py` refuses the attempt, naming the reason). Every
other seat gets its own, defaulting to its key.
"""

from __future__ import annotations

from ..memory.namespaces import DEFAULT_NAMESPACE, NamespaceError, check
from ..memory.store import MemoryStore
from .seat import DEFAULT_SEAT, Seat

__all__ = ["DEFAULT_NAMESPACE", "NamespaceError", "memory_for", "namespace_for"]


def namespace_for(seat: Seat) -> str:
    """The namespace this seat's facts live in.

    Read from the seat rather than from the session's spelling, so "Melinoë",
    "melinoe" and the key all land in the same place.
    """
    if seat.key == DEFAULT_SEAT:
        return DEFAULT_NAMESPACE
    return check(seat.namespace or seat.key)


def memory_for(root: MemoryStore, seat: Seat) -> MemoryStore:
    """The store a seat reads and writes through: one shared index, one namespace."""
    return root.scoped(namespace_for(seat))
