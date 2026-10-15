"""Standing grants — bounded, scoped, revocable (SPEC §6, ARCHITECTURE §8.5).

A grant lets one ``action`` on one ``scope`` proceed without asking again. The
first egress action is asked; on approval it becomes a grant so the same action
never asks twice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w
import tomllib


@dataclass(slots=True)
class Grant:
    action: str
    scope: str = "*"
    granted_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    note: str = ""

    def active(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return True
        return (now or time.time()) < self.expires_at


class Grants:
    """File-first grant store at ``approvals/grants.toml``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: list[Grant] = []
        self.load()

    def load(self) -> None:
        self._items = []
        if not self.path.exists():
            return
        data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        for row in data.get("grant", []):
            self._items.append(
                Grant(
                    action=str(row.get("action", "")),
                    scope=str(row.get("scope", "*")),
                    granted_at=float(row.get("granted_at", time.time())),
                    expires_at=row.get("expires_at"),
                    note=str(row.get("note", "")),
                )
            )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "grant": [
                {
                    "action": g.action,
                    "scope": g.scope,
                    "granted_at": g.granted_at,
                    **({"expires_at": g.expires_at} if g.expires_at is not None else {}),
                    "note": g.note,
                }
                for g in self._items
            ]
        }
        self.path.write_text(tomli_w.dumps(payload), encoding="utf-8")

    def all(self) -> list[Grant]:
        return list(self._items)

    def find(self, action: str, scope: str) -> Grant | None:
        for g in self._items:
            if not g.active():
                continue
            if g.action == action and g.scope in (scope, "*"):
                return g
        return None

    def add(self, grant: Grant) -> Grant:
        self._items = [g for g in self._items if not (g.action == grant.action and g.scope == grant.scope)]
        self._items.append(grant)
        self.save()
        return grant

    def revoke(self, action: str, scope: str | None = None) -> int:
        before = len(self._items)
        self._items = [
            g for g in self._items if not (g.action == action and (scope is None or g.scope == scope))
        ]
        removed = before - len(self._items)
        if removed:
            self.save()
        return removed
