"""The declarative resource registry (SPEC §3.5 Layer 3, ARCHITECTURE §8.3).

A resource is *access + credentials*, provisioned once and listed to the model
at startup. Adding one is a provisioning step, never a code change.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w
import tomllib


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
                added_at=float(row.get("added_at", time.time())),
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

    def inventory(self) -> str:
        if not self._items:
            return "(no resources provisioned)"
        return "\n".join(
            f"- {r.name} [{r.class_}/{r.access}] scopes={','.join(r.scopes) or '-'}" for r in self._items.values()
        )
