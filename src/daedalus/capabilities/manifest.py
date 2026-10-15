"""The capability manifest — the leash (ARCHITECTURE §8.2).

The manifest declares the capability's network / filesystem / secret scopes and
resource caps. Nothing else is available to it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w
import tomllib


@dataclass(slots=True)
class Scopes:
    net: list[str] = field(default_factory=list)
    fs: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"net": list(self.net), "fs": list(self.fs), "secrets": list(self.secrets)}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Scopes":
        data = data or {}
        return cls(
            net=list(data.get("net", [])),
            fs=list(data.get("fs", [])),
            secrets=list(data.get("secrets", [])),
        )

    def needs_egress(self) -> bool:
        return bool(self.net)


@dataclass(slots=True)
class Caps:
    cpu: float = 1.0
    mem_mb: int = 512
    time_s: int = 120

    def to_dict(self) -> dict[str, Any]:
        return {"cpu": self.cpu, "mem_mb": self.mem_mb, "time_s": self.time_s}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Caps":
        data = data or {}
        return cls(
            cpu=float(data.get("cpu", 1.0)),
            mem_mb=int(data.get("mem_mb", 512)),
            time_s=int(data.get("time_s", 120)),
        )


@dataclass(slots=True)
class Manifest:
    name: str
    version: str = "0.1.0"
    description: str = ""
    entry: str = "run.py"
    schedule: str | None = None
    deps: list[str] = field(default_factory=list)
    scopes: Scopes = field(default_factory=Scopes)
    caps: Caps = field(default_factory=Caps)
    trust_tier: str = "auto"
    io: dict[str, Any] = field(default_factory=dict)
    owner_approved_at: float | None = None
    verified_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "entry": self.entry,
            "deps": list(self.deps),
            "scopes": self.scopes.to_dict(),
            "caps": self.caps.to_dict(),
            "trust_tier": self.trust_tier,
            "io": self.io or {},
        }
        if self.schedule:
            out["schedule"] = self.schedule
        if self.owner_approved_at is not None:
            out["owner_approved_at"] = self.owner_approved_at
        if self.verified_at is not None:
            out["verified_at"] = self.verified_at
        return out

    def to_toml(self) -> str:
        return tomli_w.dumps(self.to_dict())

    def save(self, cap_dir: Path) -> None:
        cap_dir.mkdir(parents=True, exist_ok=True)
        (cap_dir / "manifest.toml").write_text(self.to_toml(), encoding="utf-8")

    @classmethod
    def from_path(cls, path: Path) -> "Manifest":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        return cls(
            name=str(data.get("name", "")),
            version=str(data.get("version", "0.1.0")),
            description=str(data.get("description", "")),
            entry=str(data.get("entry", "run.py")),
            schedule=(str(data["schedule"]) if data.get("schedule") else None),
            deps=list(data.get("deps", [])),
            scopes=Scopes.from_dict(data.get("scopes")),
            caps=Caps.from_dict(data.get("caps")),
            trust_tier=str(data.get("trust_tier", "auto")),
            io=dict(data.get("io", {})),
            owner_approved_at=data.get("owner_approved_at"),
            verified_at=data.get("verified_at"),
        )

    def mark_approved(self) -> None:
        self.owner_approved_at = time.time()

    def mark_verified(self) -> None:
        self.verified_at = time.time()

    def summary(self) -> str:
        bits = [f"- {self.name}: {self.description or '(no description)'}"]
        if self.schedule:
            bits.append(f"  schedule: {self.schedule}")
        if self.scopes.needs_egress():
            bits.append(f"  net: {', '.join(self.scopes.net)}")
        return "\n".join(bits)
