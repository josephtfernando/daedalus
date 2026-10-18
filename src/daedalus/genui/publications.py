"""Named artifacts with a lifetime — the server's way of saying "for you, once".

A publication is a file the loop hands the owner a *link* to, rather than the
file itself: ``/pub/<name>?t=<token>`` on the same loopback server the dashboard
already runs on (and that the tailnet route already reaches — T27 fixed that
route; this is what travels over it). The link carries four bounds, all
enforced here rather than remembered as good intentions:

- **One-time by default.** ``max_fetches=1``: the first successful GET spends
  the link. The phone opens it once; a leaked URL past that is a dead URL,
  which is the whole point of a one-time token (T34).
- **A lifetime.** ``expires_at`` is checked on every fetch — a publication with
  no sweep still dies on schedule, because death is a read-path fact, not a
  background job's.
- **Takedown is real.** ``takedown`` marks the record dead *and* removes the
  bytes; after it, the route answers 410 even to the correct token.
- **The token is the gate.** Within the tailnet, everyone the tailnet lets in
  sees the dashboard; a publication is narrower than that — a token, a name,
  and an endpoint that refuses without them.

Distinct refusal codes carry distinct facts: 404 (no such publication), 403
(wrong token), 410 (it existed and is gone — expired, spent, or taken down).
410 is the honest one: it says "the link was real once", which a 404 erases.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: A publication's name is one plain file name — never a path — because it is
#: both a key in the registry and a name under ``serve/``.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A torn or hand-edited file loses its publications rather than
        # resurrecting records the token check can no longer vouch for.
        return {}


@dataclass(slots=True)
class Publication:
    name: str
    token: str
    created_at: float
    created_by: str = ""
    expires_at: float | None = None
    max_fetches: int | None = 1
    fetches: int = 0
    taken_down: bool = False

    def dead(self, *, now: float) -> str:
        """"Why this publication will not serve," as a short reason — "" if alive."""
        if self.taken_down:
            return "taken down"
        if self.expires_at is not None and now >= self.expires_at:
            return "expired"
        if self.max_fetches is not None and self.fetches >= self.max_fetches:
            return "already fetched" if self.fetches == 1 else f"fetched {self.fetches} times"
        return ""


class Publications:
    """The registry of published artifacts, persisted beside the bytes it gates."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: dict[str, Publication] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for name, rec in _load(path).items():
            try:
                self._items[name] = Publication(**rec)
            except (TypeError, ValueError):
                continue  # a record that does not validate is not a publication

    def _reload(self) -> None:
        """Take the file's word for what is published, before gating on it.

        File-first — the same arrangement `jobs.toml` got — because the registry
        is written by more processes than the server that gates on it: the CLI
        that publishes and the daemon's route that serves are not the same
        runtime, and a registry reforged per read is what keeps `/pub/` truthful
        for a link opened seconds after another process created it.
        """
        fresh = _load(self.path)
        if not fresh and not self._items and not self.path.exists():
            return
        items: dict[str, Publication] = {}
        for name, rec in fresh.items():
            try:
                items[name] = Publication(**rec)
            except (TypeError, ValueError):
                continue
        self._items = items

    # -- persistence ---------------------------------------------------------

    def _save(self) -> None:
        self.path.write_text(
            json.dumps({k: asdict(v) for k, v in self._items.items()}, indent=2) + "\n",
            encoding="utf-8",
        )

    # -- the acts the tool asks for -----------------------------------------

    def publish(
        self,
        name: str,
        *,
        lifetime_s: float | None = 86400.0,
        max_fetches: int | None = 1,
        created_by: str = "loop",
    ) -> Publication:
        """Open one publication and return its record (with the fresh token)."""
        if not _NAME.fullmatch(name or ""):
            raise ValueError(
                f"a publication's name is one plain file name (letters, digits, "
                f"dot, dash) — not {name!r}"
            )
        self._reload()
        existing = self._items.get(name)
        if existing is not None and not existing.dead(now=time.time()):
            raise ValueError(
                f"{name!r} is already published and alive — take it down first "
                "(the takedown is what keeps 'one link, one meaning' true)"
            )
        if max_fetches is not None and max_fetches < 1:
            raise ValueError(f"max_fetches must be at least 1, not {max_fetches}")
        if lifetime_s is not None and lifetime_s <= 0:
            raise ValueError(f"lifetime must be positive, not {lifetime_s}")
        pub = Publication(
            name=name,
            token=secrets.token_urlsafe(16),
            created_at=time.time(),
            created_by=created_by,
            expires_at=(time.time() + lifetime_s) if lifetime_s is not None else None,
            max_fetches=max_fetches,
        )
        self._items[name] = pub
        self._save()
        return pub

    def admit(self, name: str, token: str) -> tuple[bool, str, int | None]:
        """Gate one fetch: (ok, reason-or-content-size, content length)."""
        self._reload()
        pub = self._items.get(name)
        if pub is None:
            return False, "no such publication", None
        now = time.time()
        if pub.dead(now=now):
            return False, f"the link has died: {pub.dead(now=now)}", None
        if pub.token != token:
            return False, "wrong token", None
        pub.fetches += 1
        self._save()  # the spending of a one-time link survives a daemon restart
        return True, "", pub.max_fetches

    def takedown(self, name: str) -> bool:
        self._reload()
        pub = self._items.get(name)
        if pub is None or pub.taken_down:
            return False
        pub.taken_down = True
        self._save()
        return True

    def get(self, name: str) -> Publication | None:
        return self._items.get(name)

    def list(self) -> list[Publication]:
        self._reload()
        return sorted(self._items.values(), key=lambda p: p.created_at, reverse=True)
