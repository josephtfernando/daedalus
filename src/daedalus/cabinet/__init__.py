"""Cabinet: durable seat identities over the one loop (SPEC §7).

P0 wires no seat — the loop runs as the coordinator identity. The charters
exist so seats can be added later without touching the loop.
"""

from .seat import DEFAULT_SEAT, SEATS, Seat

__all__ = ["Seat", "SEATS", "DEFAULT_SEAT"]
