"""Resolve the code repo and the runtime state home.

Rule (ARCHITECTURE §5): the repo holds code; ``$DAEDALUS_HOME`` holds state.
"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """The code repo root (…/daedalus)."""
    return Path(__file__).resolve().parents[3]


def home() -> Path:
    """Runtime state home. Override with ``$DAEDALUS_HOME``."""
    raw = os.environ.get("DAEDALUS_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".daedalus"


def sub(name: str) -> Path:
    return home() / name


def ensure_tree(home: Path) -> Path:
    """Create the state tree under ``home`` with owner-only permissions."""
    home = home.expanduser()
    for part in (
        home,
        home / "memory/facets",
        home / "capabilities",
        home / "calendar",
        home / "resources/creds",
        home / "cabinet",
        home / "genui/components",
        home / "genui/tokens",
        home / "genui/drafts",
        home / "approvals",
        home / "state",
        home / "ui",
        home / "logs",
    ):
        part.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    return home


def ensure_home() -> Path:
    """Create the state tree if missing, with owner-only permissions."""
    return ensure_tree(home())
