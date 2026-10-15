"""Risk tiers (SPEC §6)."""

from __future__ import annotations

from enum import Enum


class Risk(str, Enum):
    AUTO = "auto"
    ASK = "ask"
    BLOCK = "block"

    @classmethod
    def parse(cls, value: str | "Risk" | None, default: "Risk" = None) -> "Risk":  # type: ignore[assignment]
        if isinstance(value, Risk):
            return value
        if value is None:
            return default or cls.AUTO
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return default or cls.AUTO


ORDER = {Risk.AUTO: 0, Risk.ASK: 1, Risk.BLOCK: 2}


def stricter(a: Risk, b: Risk) -> Risk:
    return a if ORDER[a] >= ORDER[b] else b
