"""Memory entry shape and IDs (ARCHITECTURE §8.1)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..security.provenance import DataClass, Origin

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(now: float | None = None) -> str:
    """A monotonic-ish ULID (48-bit time + 80-bit randomness), no dependency."""
    ts = int((now or time.time()) * 1000)
    chars = []
    for _ in range(10):
        chars.append(_CROCKFORD[ts & 0x1F])
        ts >>= 5
    time_part = "".join(reversed(chars))
    rand = os.urandom(16)
    acc = int.from_bytes(rand, "big")
    tail = []
    for _ in range(16):
        tail.append(_CROCKFORD[acc & 0x1F])
        acc >>= 5
    return time_part + "".join(reversed(tail))


@dataclass(slots=True)
class MemoryEntry:
    facet: str
    content: str
    origin: Origin = Origin.TRUSTED_OWNER
    data_class: DataClass = DataClass.PRIVATE
    confidence: float = 0.8
    source_ref: str | None = None
    valid_from: float = field(default_factory=time.time)
    valid_to: float | None = None
    superseded_by: str | None = None
    created_at: float = field(default_factory=time.time)
    id: str = field(default_factory=ulid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "facet": self.facet,
            "content": self.content,
            "origin": self.origin.value,
            "data_class": self.data_class.value,
            "confidence": self.confidence,
            "source_ref": self.source_ref,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "superseded_by": self.superseded_by,
            "created_at": self.created_at,
        }
