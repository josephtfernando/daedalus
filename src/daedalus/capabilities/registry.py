"""File-first capability registry (ARCHITECTURE §11)."""

from __future__ import annotations

import shutil
from pathlib import Path

from .manifest import Manifest


class CapabilityRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, name: str) -> Path:
        return self.root / _safe(name)

    def list(self) -> list[Manifest]:
        out: list[Manifest] = []
        for manifest_path in sorted(self.root.glob("*/manifest.toml")):
            try:
                out.append(Manifest.from_path(manifest_path))
            except Exception:  # noqa: BLE001 — a broken manifest must not break startup
                continue
        return out

    def get(self, name: str) -> Manifest | None:
        path = self.dir(name) / "manifest.toml"
        if not path.exists():
            return None
        return Manifest.from_path(path)

    def register(self, manifest: Manifest) -> Manifest:
        manifest.save(self.dir(manifest.name))
        return manifest

    def remove(self, name: str) -> bool:
        target = self.dir(name)
        if target.exists():
            shutil.rmtree(target)
            return True
        return False

    def inventory(self) -> str:
        items = self.list()
        if not items:
            return "(no capabilities authored yet)"
        return "\n".join(m.summary() for m in items)


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name).strip("._") or "capability"
