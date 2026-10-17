"""The declarative resource registry (SPEC §3.5 Layer 3, ARCHITECTURE §8.3).

A resource is *access + credentials*, provisioned once and listed to the model
at startup. Adding one is a provisioning step, never a code change.

Two kinds of resource need two different answers when they are listed:

- a **remote API** is present exactly when its credential is, so its scopes are
  the whole story;
- a **local CLI** has no credential to grant — the access *is* the binary on this
  machine — so its presence is a fact to be checked, not assumed. Provisioning
  one records the scopes it may act in; whether it is actually installed is asked
  of `PATH` every time the inventory is built, because a resource the model
  believes it has and does not is the failure that makes a long task die at the
  end (T28). The model is told it is absent, before it plans.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tomli_w
import tomllib

#: The delegate CLI (SPEC §5 names `opencode` as a local CLI; T29 fixes what a
#: delegation may touch). Kept here so the wizard, the tests and the delegate
#: cannot disagree about the name or the scopes in one place.
OPENCODE = "opencode"
#: What the delegate may act in: a **named workspace** it may read and write, and
#: the network its model needs. Never the personal tree — that is not a scope a
#: delegation can be given, it is a boundary (`delegate/workspace.py`).
OPENCODE_SCOPES = ("workspace", "net")


@dataclass(slots=True)
class Resource:
    name: str
    class_: str = "remote_api"  # remote_api | local_cli | device
    access: str = "token"  # oauth | token | local
    scopes: list[str] = field(default_factory=list)
    cred_ref: str = ""
    added_at: float = field(default_factory=time.time)
    note: str = ""


class ResourceRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: dict[str, Resource] = {}
        self.load()

    def load(self) -> None:
        self._items = {}
        if not self.path.exists():
            return
        data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        for row in data.get("resource", []):
            res = Resource(
                name=str(row.get("name", "")),
                class_=str(row.get("class", "remote_api")),
                access=str(row.get("access", "token")),
                scopes=list(row.get("scopes", [])),
                cred_ref=str(row.get("cred_ref", "")),
                added_at=_timestamp(row.get("added_at")),
                note=str(row.get("note", "")),
            )
            self._items[res.name] = res

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "resource": [
                {
                    "name": r.name,
                    "class": r.class_,
                    "access": r.access,
                    "scopes": r.scopes,
                    "cred_ref": r.cred_ref,
                    "added_at": r.added_at,
                    "note": r.note,
                }
                for r in self._items.values()
            ]
        }
        self.path.write_text(tomli_w.dumps(payload), encoding="utf-8")

    def add(self, resource: Resource) -> Resource:
        self._items[resource.name] = resource
        self.save()
        return resource

    def get(self, name: str) -> Resource | None:
        return self._items.get(name)

    def all(self) -> list[Resource]:
        return list(self._items.values())

    def remove(self, name: str) -> bool:
        if name in self._items:
            del self._items[name]
            self.save()
            return True
        return False

    def inventory(
        self, *, resolver: Callable[[str], str | None] | None = None
    ) -> str:
        """What the model is told it has, at startup.

        ``resolver`` is injectable so the presence of a local CLI can be a fact a
        test controls: the default is a real ``PATH`` lookup, and the message
        when it comes back empty is the whole point of the check — a delegation
        is a long task, and finding out a CLI is missing halfway through it is
        exactly what listing it at startup is meant to prevent.
        """
        if not self._items:
            return "(no resources provisioned)"
        which = resolver or shutil.which
        return "\n".join(self._describe(resource, which) for resource in self._items.values())

    @staticmethod
    def _describe(resource: Resource, which: Callable[[str], str | None]) -> str:
        scopes = ",".join(resource.scopes) or "-"
        line = f"- {resource.name} [{resource.class_}/{resource.access}] scopes={scopes}"
        if resource.class_ == "local_cli":
            found = which(resource.name)
            line += f" — installed at {found}" if found else " — NOT INSTALLED (not on PATH)"
        if resource.note:
            line += f" — {resource.note}"
        return line


def _timestamp(raw: object) -> float:
    """A stored ``added_at`` as epoch seconds, however it was written.

    A hand-edited or wizard-written entry can arrive as a TOML datetime, a quoted
    ISO string, or a number. Reading a registry must not be what stops a startup,
    so an unreadable value falls back to *now* rather than raising — the field is
    a note about when the owner granted access, not an identity.
    """
    if isinstance(raw, datetime):
        return raw.timestamp()
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


def provision_local_cli(
    registry: ResourceRegistry,
    name: str,
    *,
    scopes: Sequence[str],
    note: str = "",
) -> Resource:
    """Record a local CLI as a provisioned resource, once (SPEC §3.5 Layer 3).

    A local CLI is access to a binary already on the machine: there is no
    credential to grant and nothing for the owner to complete in a browser, so
    provisioning writes the entry — the scopes it may act in — and nothing else.
    Idempotent: re-provisioning an existing resource returns what is there rather
    than replacing scopes the owner may have narrowed by hand.
    """
    existing = registry.get(name)
    if existing is not None:
        return existing
    return registry.add(
        Resource(
            name=name,
            class_="local_cli",
            access="local",
            scopes=list(scopes),
            note=note,
        )
    )
