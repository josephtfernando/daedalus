"""Provenance labels and data classes (SPEC §6, ARCHITECTURE §10).

Every value that can influence an action carries a label. The label — not the
payload's *content* — decides what the value is allowed to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Origin(str, Enum):
    """Where a value came from."""

    TRUSTED_OWNER = "trusted_owner"
    LOCAL_DERIVED = "local_derived"
    UNTRUSTED_SOURCE = "untrusted_source"


class DataClass(str, Enum):
    """How sensitive a value is."""

    PUBLIC = "public"
    PRIVATE = "private"
    LOCAL_ONLY = "local_only"


@dataclass(frozen=True, slots=True)
class Labeled:
    """A string value plus its provenance and data class."""

    value: str
    origin: Origin = Origin.TRUSTED_OWNER
    data_class: DataClass = DataClass.PRIVATE
    source_ref: str | None = None

    def may_authorize(self) -> bool:
        """Untrusted text may inform a draft but may never authorize anything."""
        return self.origin is not Origin.UNTRUSTED_SOURCE

    def may_egress(self) -> bool:
        """``local_only`` values never cross the wire, approved or not."""
        return self.data_class is not DataClass.LOCAL_ONLY

    def derive(self, value: str, *, source_ref: str | None = None) -> "Labeled":
        """Derive a new value from this one, downgrading origin to local_derived."""
        origin = self.origin
        if origin is Origin.UNTRUSTED_SOURCE:
            origin = Origin.LOCAL_DERIVED
        return Labeled(
            value=value,
            origin=origin,
            data_class=self.data_class,
            source_ref=source_ref or self.source_ref,
        )
